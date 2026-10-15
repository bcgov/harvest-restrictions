"""One-off: check a new overlay run's draft matches the reviewed draft from before the migration

The reviewed draft is the current version of each file under the old draft/ prefix; the new
run is the most recent complete run under drafts/ (or --run_id). Each pair of files must match:

- csvs byte for byte
- geopackages by FIT_changedetector (https://github.com/bcgov/FIT_changedetector) - every
  record UNCHANGED, with none NEW, DELETED or MODIFIED. changedetector normalizes geometries
  and compares them at a coordinate precision (--precision, default 0.01), so row order and
  vertex order between runs don't matter.

Records are keyed on a hash of all their fields plus geometry (geometry alone isn't unique -
both layers hold repeated geometries), so any changed record shows as one DELETED and one NEW.
harvest_restrictions_id is ignored: the per-tile overlays run in parallel, so it's numbered
differently each run. Repeated identical records are set aside by changedetector, so each
layer's record count is checked separately.

For each layer, a few diagnostics are printed for both drafts - records, empty geometries,
repeated geometries and total area - to help explain any difference. The files are downloaded
to --work_dir (default ./compare_drafts, cleared first) and kept, and when a layer differs,
its differing records are written there with `changedetector diff2gdb`
(<layer>_diff.gdb), to open in QGIS/ArcGIS.

usage (in the runner container, which has the aws cli and credentials):

    pip install fit_changedetector
    python migrate/compare_drafts.py [--run_id RUN_ID]
    python migrate/compare_drafts.py --old_dir DIR --new_dir DIR    # compare local copies

Where records differ in harvest_restrictions, its content is compared too - each record's
designation lists sorted, and area totalled per distinct combination of attributes, so
records cut into different pieces, or listing same-source designations in a different order,
compare equal if they cover the same land with the same designations (see compare_content).

Exits 0 if everything matches, 2 if records differ but content matches (EQUIVALENT), 1
otherwise.
"""

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import zipfile

import geopandas
import pandas
import pyogrio
import shapely

# datarelease and release.json live at the repo root, one level up from this script
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import datarelease  # noqa: E402

FILES = [
    "harvest_restrictions.gpkg.zip",
    "harvest_restrictions_sources.gpkg.zip",
    "land_designations_summary.csv",
    "harvest_restrictions_summary.csv",
    "sources.csv",
]
# per layer, fields left out of the comparison
IGNORE_FIELDS = {"harvest_restrictions": ["harvest_restrictions_id"]}
# per layer, how to compare content when records differ (see compare_content): the
# ;-separated list fields, aligned entry by entry, and the fields taken from the lists' first
# entries (which of several same-source designations comes first was arbitrary before
# overlay.sql ordered the lists by primary key, so they can legitimately differ)
CONTENT = {
    "harvest_restrictions": {
        "lists": [
            "all_land_desig_names",
            "all_land_desig_type_ranks",
            "all_land_desig_type_codes",
            "all_land_desig_type_names",
            "all_land_desig_primary_keys",
            "all_harv_restrict_class_ranks",
            "all_harv_restrict_class_names",
        ],
        "order_dependent": ["land_designation_name", "land_designation_primary_key"],
    },
}
# the changedetector cli installed alongside this python, if there is one
CHANGEDETECTOR = os.path.join(os.path.dirname(sys.executable), "changedetector")
if not os.path.exists(CHANGEDETECTOR):
    CHANGEDETECTOR = "changedetector"
# changedetector categories that mean a difference
CHANGES = ["NEW", "DELETED", "MODIFIED_BOTH", "MODIFIED_ATTR", "MODIFIED_GEOM"]


def download_old(bucket, out_dir):
    """the current version of each file under the old draft/ prefix, printing its tags"""
    for name in FILES:
        key = f"harvest_restrictions/draft/{name}"
        subprocess.run(
            ["aws", "s3", "cp", f"s3://{bucket}/{key}", os.path.join(out_dir, name)],
            check=True,
        )
        tagging = subprocess.run(
            ["aws", "s3api", "get-object-tagging", "--bucket", bucket, "--key", key],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        tags = {t["Key"]: t["Value"] for t in json.loads(tagging)["TagSet"]}
        print(f"old draft/{name}: {tags}")


def download_new(bucket, run_id, out_dir):
    """the files of run_id (default: the most recent complete run) under drafts/"""
    config = datarelease.load_config(os.path.join(REPO_ROOT, "release.json"))
    store = datarelease.S3Store(bucket, config["prefix"])
    if not run_id:
        manifests = [
            k
            for k in store.list("drafts/")
            if os.path.basename(k) == datarelease.MANIFEST
        ]
        if not manifests:
            sys.exit("no complete run found under drafts/")
        run_id = sorted(manifests)[-1].split("/")[1]
    for name in FILES + [datarelease.MANIFEST]:
        store.download(datarelease.draft_key(run_id, name), os.path.join(out_dir, name))
    with open(os.path.join(out_dir, datarelease.MANIFEST)) as f:
        manifest = json.load(f)
    print(f"new drafts/{run_id}/: commit={manifest['commit']}, run_id={run_id}")


def compare_csv(old, new):
    if filecmp.cmp(old, new, shallow=False):
        return []
    old_df, new_df = pandas.read_csv(old), pandas.read_csv(new)
    if list(old_df.columns) != list(new_df.columns):
        return [f"columns differ: {list(old_df.columns)} vs {list(new_df.columns)}"]
    if len(old_df) != len(new_df):
        return [f"row counts differ: {len(old_df)} vs {len(new_df)}"]
    diff = old_df.compare(new_df)
    return [f"{len(diff)} rows differ:\n{diff.head(10).to_string()}"]


def unzip(path):
    """extract the single file in a zipped geopackage next to it, returning its path"""
    with zipfile.ZipFile(path) as z:
        (name,) = z.namelist()
        z.extract(name, os.path.dirname(path))
    return os.path.join(os.path.dirname(path), name)


def describe(path, layer):
    """records, empty geometries, repeated geometries and total area of a layer"""
    df = geopandas.read_file(path, layer=layer, columns=[])
    empty = df.geometry.isna() | df.geometry.is_empty
    wkb = shapely.to_wkb(shapely.normalize(df.geometry[~empty].values))
    repeated = int(pandas.Series(wkb).duplicated(keep=False).sum())
    return {
        "records": len(df),
        "empty geometries": int(empty.sum()),
        "records with a repeated geometry": repeated,
        "area (ha)": round(df.geometry.area.sum() / 10000, 2),
    }


def content_areas(path, layer, conf):
    """total area per distinct record content, with list order and record splits ignored

    Each record's list fields are sorted together, entry by entry (keeping the n-th entry of
    each list together), and the records grouped on all their fields, with their areas summed
    - so records cut into different pieces, or listing designations in a different order, but
    covering the same land with the same designations, give the same totals
    """
    df = geopandas.read_file(path, layer=layer)
    df["_area"] = df.geometry.area
    drop = IGNORE_FIELDS.get(layer, []) + conf["order_dependent"] + [df.geometry.name]
    df = df.drop(columns=[c for c in drop if c in df.columns])
    lists = [df[c].fillna("").astype(str).str.split(";") for c in conf["lists"]]
    normalized = []
    for i, entries in enumerate(zip(*lists)):
        if len({len(e) for e in entries}) != 1:
            raise ValueError(f"{layer} record {i}: list fields have different lengths")
        normalized.append([";".join(col) for col in zip(*sorted(zip(*entries)))])
    df[conf["lists"]] = pandas.DataFrame(normalized, index=df.index)
    fields = [c for c in df.columns if c != "_area"]
    return df.astype({c: str for c in fields}).groupby(fields)["_area"].sum()


def compare_content(old, new, layer, conf, area_tolerance):
    old_areas = content_areas(old, layer, conf)
    new_areas = content_areas(new, layer, conf)
    joined = pandas.concat([old_areas.rename("old"), new_areas.rename("new")], axis=1)
    only_old = joined["new"].isna()
    only_new = joined["old"].isna()
    differ = (
        ~only_old & ~only_new & ((joined["old"] - joined["new"]).abs() > area_tolerance)
    )
    print(
        f"    content: {len(old_areas)} vs {len(new_areas)} distinct contents, "
        f"max area difference {(joined['old'] - joined['new']).abs().max():.4f} m2"
    )
    problems = []
    for label, mask in [
        ("only in old", only_old),
        ("only in new", only_new),
        (f"area differs by more than {area_tolerance} m2", differ),
    ]:
        if mask.any():
            sample = joined[mask].head(5).reset_index()
            problems.append(
                f"{layer} content: {int(mask.sum())} {label}, e.g.:\n{sample.to_string()}"
            )
    return problems


def compare_gpkg(old_zip, new_zip, precision, area_tolerance, work_dir):
    """returns (problems, layers whose records differ but whose content matches)"""
    old, new = unzip(old_zip), unzip(new_zip)
    old_layers = [layer for layer, _ in pyogrio.list_layers(old)]
    new_layers = [layer for layer, _ in pyogrio.list_layers(new)]
    if old_layers != new_layers:
        return [f"layers differ: {old_layers} vs {new_layers}"], []

    problems, equivalent = [], []
    for layer in old_layers:
        old_info, new_info = describe(old, layer), describe(new, layer)
        print(f"  {layer}:")
        print(f"    {'':<38} {'old':>16} {'new':>16}")
        for key in old_info:
            print(f"    {key:<38} {old_info[key]:>16} {new_info[key]:>16}")
        # identical records are set aside before comparing, so count records separately
        layer_problems = []
        if old_info["records"] != new_info["records"]:
            layer_problems.append(
                f"{layer}: record counts differ: "
                f"{old_info['records']} vs {new_info['records']}"
            )

        ignore = IGNORE_FIELDS.get(layer, [])
        fields = [
            f for f in pyogrio.read_info(old, layer=layer)["fields"] if f not in ignore
        ]
        args = [
            old,
            new,
            "--layer-a",
            layer,
            "--layer-b",
            layer,
            "--hash-fields",
            ",".join(fields + ["geometry"]),
            "--hash-key",
            "_fcd_key",
            "--allow-duplicates",
            "--precision",
            str(precision),
            "--quiet",
        ]
        if ignore:
            args += ["--ignore-fields", ",".join(ignore)]
        result = subprocess.run(
            [CHANGEDETECTOR, "diff", "--count", *args], capture_output=True, text=True
        )
        if result.returncode != 0:
            problems.append(f"{layer}: changedetector failed:\n{result.stderr.strip()}")
            continue
        counts = json.loads(result.stdout)
        changed = {k: counts[k] for k in CHANGES if counts.get(k)}
        if not changed and not layer_problems:
            print(f"    changedetector: {counts['UNCHANGED']} records unchanged")
            continue
        layer_problems.append(f"{layer}: changedetector: {counts}")
        gdb = os.path.join(work_dir, f"{layer}_diff.gdb")
        subprocess.run(
            [CHANGEDETECTOR, "diff2gdb", *args, "--out-file", gdb], check=True
        )
        layer_problems.append(f"  differing records written to {gdb}")

        # records differ - but the content may still be the same
        if layer in CONTENT:
            content_problems = compare_content(
                old, new, layer, CONTENT[layer], area_tolerance
            )
            if not content_problems:
                for problem in layer_problems:
                    print(f"    records differ - {problem}")
                print("    content matches - same designations over the same areas")
                equivalent.append(layer)
                continue
            layer_problems += content_problems
        problems += layer_problems
    return problems, equivalent


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run_id", help="new run to compare (default: the most recent)"
    )
    parser.add_argument(
        "--old_dir", help="compare local copies of the old draft instead"
    )
    parser.add_argument("--new_dir", help="compare local copies of the new run instead")
    parser.add_argument(
        "--work_dir",
        default="compare_drafts",
        help="where to download the drafts to (cleared first)",
    )
    parser.add_argument(
        "--area_tolerance",
        type=float,
        default=1.0,
        help="largest area difference (m2) allowed per distinct content, when comparing "
        "content (default 1)",
    )
    parser.add_argument(
        "--precision",
        type=float,
        default=0.01,
        help="coordinate precision for comparing geometries (default 0.01)",
    )
    args = parser.parse_args()

    old_dir, new_dir, work_dir = args.old_dir, args.new_dir, args.work_dir
    if old_dir and new_dir:
        os.makedirs(work_dir, exist_ok=True)
    else:
        bucket = os.environ.get("BUCKET") or sys.exit("set $BUCKET")
        shutil.rmtree(args.work_dir, ignore_errors=True)
        old_dir = os.path.join(args.work_dir, "old")
        new_dir = os.path.join(args.work_dir, "new")
        os.makedirs(old_dir)
        os.makedirs(new_dir)
        download_old(bucket, old_dir)
        download_new(bucket, args.run_id, new_dir)

    mismatched, equivalent = False, []
    for name in FILES:
        print(f"{name}:")
        old, new = os.path.join(old_dir, name), os.path.join(new_dir, name)
        if name.endswith(".csv"):
            problems = compare_csv(old, new)
        else:
            problems, layers = compare_gpkg(
                old, new, args.precision, args.area_tolerance, work_dir
            )
            equivalent += layers
        for problem in problems:
            print(f"  DIFFERS - {problem}")
        if not problems and name.endswith(".csv"):
            print("  identical")
        mismatched = mismatched or bool(problems)

    if mismatched:
        print("\nMISMATCH")
        sys.exit(1)
    if equivalent:
        print(
            f"\nEQUIVALENT - records differ in {', '.join(equivalent)}, but the content "
            "(designations and their areas) matches"
        )
        sys.exit(2)
    print("\nall files match")
    sys.exit(0)


if __name__ == "__main__":
    main()
