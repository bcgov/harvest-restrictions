import csv
import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

import bcdata
import click
import geopandas
import jsonschema
import pandas
from pyproj import CRS
from shapely.geometry.linestring import LineString
from shapely.geometry.multilinestring import MultiLineString
from shapely.geometry.multipoint import MultiPoint
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon
from slugify import slugify
from sqlalchemy import create_engine, inspect, text

LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"
LOG = logging.getLogger(__name__)

# the cache of sources transformed to the standard column layout, written by cache and read by
# load-db - not to be confused with harvest_restrictions/sources/, which holds the sources not
# available for download elsewhere, in their native format
CACHE_PATH = (
    f"s3://{os.environ['BUCKET']}/harvest_restrictions/cache"
    if os.environ.get("BUCKET")
    else None
)

# everything generated locally is written under here, laid out as in the bucket - each overlay
# run's draft in drafts/<run_id>/, each release built in releases/<release_tag>/
OUTPUTS = "outputs"

# everything published is under this prefix in the bucket
PREFIX = "harvest_restrictions"

# the spatial outputs, each published zipped
OVERLAY_GPKG = "harvest_restrictions.gpkg"
OVERLAY_ZIP = OVERLAY_GPKG + ".zip"
SOURCES_GPKG = "harvest_restrictions_sources.gpkg"
SOURCES_ZIP = SOURCES_GPKG + ".zip"
SOURCES_CSV = "sources.csv"

# the area totals compared to the previous release and logged with each release, by log name -
# each log's columns, the first being its key. overlay builds each log's totals with
# sql/<log>.sql, and compares them to the previous release's in <log>_summary.csv
LOGS = {
    "land_designations": [
        "land_designation_type_rank",
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
        "land_designation_type_code",
        "land_designation_type_name",
    ],
    "harvest_restrictions": [
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
    ],
}
AREA = "area_ha"


def summary(log):
    return f"{log}_summary.csv"


# what each run publishes under drafts/<run_id>/, besides the manifest (written last)
DRAFT_FILES = [OVERLAY_ZIP, SOURCES_ZIP, SOURCES_CSV] + [summary(log) for log in LOGS]
MANIFEST = "manifest.json"
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"

# each release's archive, under releases/
ARCHIVE = "harvest_restrictions_{release_tag}.zip"

SOURCES_CSV_COLUMNS = [
    "harvest_restriction",
    "alias",
    "description",
    "source",
    "source_type",
    "layer",
    "query",
    "name_field",
]

# note that the all_* lists are exported as JSON arrays to retain nulls and keep the arrays aligned
DUMP_SQL = """select
  harvest_restrictions_id,
  land_designation_name,
  land_designation_type_rank,
  land_designation_type_code,
  land_designation_type_name,
  land_designation_primary_key,
  harvest_restriction_class_rank,
  harvest_restriction_class_name,
  array_to_json(trim_array(all_land_desig_names, 1)) as all_land_desig_names,
  array_to_json(trim_array(all_land_desig_type_ranks, 1)) as all_land_desig_type_ranks,
  array_to_json(trim_array(all_land_desig_type_codes, 1)) as all_land_desig_type_codes,
  array_to_json(trim_array(all_land_desig_type_names, 1)) as all_land_desig_type_names,
  array_to_json(trim_array(all_land_desig_primary_keys, 1)) as all_land_desig_primary_keys,
  array_to_json(trim_array(all_harv_restrict_class_ranks, 1)) as all_harv_restrict_class_ranks,
  array_to_json(trim_array(all_harv_restrict_class_names, 1)) as all_harv_restrict_class_names,
  map_tile_250k,
  geom
from harvest_restrictions
where
all_harv_restrict_class_ranks @> ARRAY[6] and
all_harv_restrict_class_ranks != ARRAY[6]"""


verbose_opt = click.option("--verbose", "-v", count=True, help="Increase verbosity.")
quiet_opt = click.option("--quiet", "-q", count=True, help="Decrease verbosity.")


def configure_logging(verbosity):
    log_level = max(10, 30 - 10 * verbosity)
    logging.basicConfig(stream=sys.stderr, level=log_level, format=LOG_FORMAT)


def run(cmd):
    """run a shell command, logging it first"""
    LOG.info(cmd)
    subprocess.run(cmd, shell=True, check=True)


def zip_files(out_zip, files):
    """zip files into out_zip, each under its basename

    Uses sozip (bundled with GDAL) rather than plain zip, so the result is seek-optimized -
    GDAL's /vsizip/ reader can read a layer from it, even remotely, fetching just the bytes it
    needs instead of decompressing the whole archive.
    """
    subprocess.run(
        ["sozip", "--overwrite", "--junk-paths", out_zip, *files], check=True
    )


def parse_sources(sources):
    """validate and parse sources data structure"""
    # validate sources against schema doc
    with open("source.schema.json", "r") as f:
        schema = json.load(f)
    jsonschema.validate(instance=sources, schema=schema)

    # sources are presumed to be ordered by importance/hierarchy,
    # sources occuring earlier in the list override sources lower in the list.
    # add an index (base 1) indicating the hierchy level of a given source
    sources = [dict(d, index=index + 1) for (index, d) in enumerate(sources)]

    parsed = sources
    for i, source in enumerate(sources):
        # replace string {CURRENT_DATE} with todays date
        if source["query"] and "{CURRENT_DATE}" in source["query"]:
            parsed[i]["query"] = parsed[i]["query"].replace(
                "{CURRENT_DATE}", datetime.today().strftime("%Y-%m-%d")
            )
        # slugify the alias
        alias = source["alias"]
        slug = slugify(alias, separator="_", lowercase=True)
        if slug != alias:
            parsed[i]["alias"] = slug
            LOG.warning(
                "{alias} - alias adjusted to {slug}, consider editing alias in config file"
            )

    LOG.info("Source json is valid")
    return parsed


def write_sources_csv(sources_file, out_file):
    """flatten sources_file to a csv, for publishing alongside release outputs"""
    with open(sources_file, "r") as f:
        sources = json.load(f)
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["index"] + SOURCES_CSV_COLUMNS)
        for index, source in enumerate(sources, start=1):
            writer.writerow(
                [
                    index,
                    source["data"].get("harvest_restriction"),
                    source["alias"],
                    source["description"],
                    source["source"],
                    source["source_type"],
                    source.get("layer"),
                    source["query"],
                    source["field_mapper"].get("name"),
                ]
            )


def validate_file(source):
    """simple validation of file based sources
    - file exists
    - schema is as expected
    """
    # load file
    df = geopandas.read_file(
        os.path.expandvars(source["source"]),
        layer=source["layer"],
        where=source["query"],
    )

    # are expected columns present?
    columns = [x.lower() for x in df.columns]
    # is primary key present and not null?
    if "primary_key" in source and source["primary_key"]:
        if source["primary_key"].lower() not in columns:
            raise ValueError(
                f"Validation error: {source['alias']} - primary key is not present - {source['primary_key']}"
            )
    for column in source["field_mapper"].values():
        if column and column.lower() not in columns:
            raise ValueError(
                f"Validation error: {source['alias']} - column {column} is not present, modify config 'field_mapper'"
            )

    # is there data?
    count = len(df.index)
    if count == 0:
        raise ValueError(
            f"Validation error: {source['alias']} - no data returned, check source and query"
        )

    # presume layer is defined correctly if no errors are raised
    LOG.info(f"Validation successful: {source['alias']} - record count: {str(count)}")


def validate_bcgw(source):
    """validate bcdata sources against bcdc api and wfs"""
    # does source exist as written?
    table = source["source"].upper()
    if table not in bcdata.list_tables():
        raise ValueError(
            f"Validation error: {source['alias']} - {table} does not exist in BCGW or is not available via WFS"
        )

    # get columns present in source from data catalogue
    table_def = bcdata.get_table_definition(table)
    columns = [c["column_name"] for c in table_def["schema"]]

    # is primary key present and not null?
    if "primary_key" in source and source["primary_key"]:
        if source["primary_key"] not in columns:
            raise ValueError(
                f"Validation error: {source['alias']} - Primary key - {source['primary_key']} is not present in {table}"
            )

    # required columns in field mapping present?
    for column in source["field_mapper"].values():
        if column:  # allow null source columns (adds the new column, but with no values from source)
            if column.upper() not in columns:
                raise ValueError(
                    f"Validation error: {source['alias']} - column {column} is not present in {table}, modify config 'field_mapper'"
                )

    # is there data?
    count = bcdata.get_count(table, query=source["query"])
    if count == 0:
        raise ValueError(
            f"Validation error: {source['alias']} - no data returned, check query against {table}"
        )

    # presume source is defined correctly if no errors are raised
    LOG.info(f"Validation successful: {source['alias']} - record count: {str(count)}")


def to_multipart(df):
    """
    geopandas has no built-in func for dumping singlepart to multipart
    https://gis.stackexchange.com/questions/311320/casting-geometry-to-multi-using-geopandas
    """
    df["geom"] = [
        MultiPoint([feature]) if isinstance(feature, Point) else feature
        for feature in df["geom"]
    ]
    df["geom"] = [
        MultiLineString([feature]) if isinstance(feature, LineString) else feature
        for feature in df["geom"]
    ]
    df["geom"] = [
        MultiPolygon([feature]) if isinstance(feature, Polygon) else feature
        for feature in df["geom"]
    ]
    return df


def validate_sources(sources, validate_data=True, alias=None):
    """
    Validate json, whether data sources exist, and assign hierarchy index
    based on position in list
    """
    for source in sources:
        if source["source_type"] == "BCGW":
            validate_bcgw(source)
        elif source["source_type"] == "FILE":
            validate_file(source)

    LOG.info("Validation successful: all layers appear valid")

    # return validated (and indexed/dated) sources as ordered dictionary
    return sources


def download_source(source):
    """download data from source to a standardized geodataframe"""

    # download WFS
    if source["source_type"] == "BCGW":
        df = bcdata.get_data(
            source["source"],
            query=source["query"],
            as_gdf=True,
            lowercase=True,
        )
        # if primary key is not provided in config, default to the pk noted in bcdata
        if ("primary_key" not in source.keys() or not source["primary_key"]) and source[
            "source"
        ].lower() in bcdata.primary_keys:
            source["primary_key"] = bcdata.primary_keys[source["source"].lower()]
        else:
            source["primary_key"] = None

    # download file
    elif source["source_type"] == "FILE":
        df = geopandas.read_file(
            os.path.expandvars(source["source"]),
            layer=source["layer"],
            where=source["query"],
        )
        if not df.crs:
            raise ValueError(
                "Source does not have a defined projection/coordinate reference system"
            )
        # reproject to BC Albers if necessary
        if df.crs != CRS.from_user_input(3005):
            df = df.to_crs("EPSG:3005")
        # lowercasify column names
        df.columns = [x.lower() for x in df.columns]

    # standardize/tidy the data
    df = df.rename_geometry("geom")
    df = to_multipart(df)  # sources can have mixed types, just make everything multi

    # standardize columns, adding data as required
    df["__index__"] = source["index"]
    df["__description__"] = source["description"]
    df["__alias__"] = source["alias"].lower()
    df["__primary_key__"] = ""
    if "primary_key" in source and source["primary_key"]:
        df["__primary_key__"] = df[source["primary_key"].lower()].astype(
            "str"
        )  # handle pks as strings

    # rename columns that we want to retain
    for key, value in source["field_mapper"].items():
        if value:
            df["__" + key + "__"] = df[
                value.lower()
            ]  # all incoming data is already lowercasified
        else:
            df["__" + key + "__"] = None

    # add additional constant data
    if source["data"]:
        for key, value in source["data"].items():
            df["__" + key + "__"] = value

    # retain only columns that have just been added
    columns = (
        ["index", "description", "alias", "primary_key"]
        + list(source["field_mapper"])
        + list(source["data"])
    )
    df = df[["__" + c + "__" for c in columns] + ["geom"]]

    # strip the __ prefix/suffix
    df = df.rename(columns={"__" + c + "__": c for c in columns})

    return df


# --- drafts and releases ---
#
# overlay publishes each run under its own drafts/<run_id>/ prefix, with summaries comparing
# the run's area totals to the latest release, and a manifest written last to mark the run
# complete. Once a run has been reviewed, its commit is tagged and release publishes it:
#
# - releases/harvest_restrictions_<release_tag>.zip, never overwritten
# - the release's change log rows under logs/<release_tag>/, never rewritten
# - fixed-name latest files under latest/, overwritten by each release
# - combined change logs under logs/, rebuilt from every release's rows
#
# prune-drafts deletes drafts that are no longer needed. Nothing a later step depends on is
# ever overwritten, so the bucket needs no versioning.


def run_prefix(run_id):
    """the drafts/ prefix holding everything one run published - each run gets its own, and
    never writes to it again"""
    return f"drafts/{run_id}"


def draft_key(run_id, name):
    return f"{run_prefix(run_id)}/{name}"


def release_log_key(release_tag, log):
    """one release's change log rows - written once, never rewritten"""
    return f"logs/{release_tag}/{log}.csv"


def combined_log_key(log):
    """a combined change log, rebuilt from every release's rows on each release - next to (not
    in) the per-release logs/<release_tag>/ folders, so it's never read as one"""
    return f"logs/{log}.csv"


class S3Store:
    """the objects under PREFIX in an s3 bucket, via the aws cli

    Keys passed to and returned by every method are relative to PREFIX.
    """

    def __init__(self, bucket):
        self.bucket = bucket

    def url(self, key):
        return f"s3://{self.bucket}/{PREFIX}/{key}"

    def upload(self, local_file, key, tags=None):
        subprocess.run(["aws", "s3", "cp", local_file, self.url(key)], check=True)
        if tags:
            tagging = {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}
            subprocess.run(
                [
                    "aws",
                    "s3api",
                    "put-object-tagging",
                    "--bucket",
                    self.bucket,
                    "--key",
                    f"{PREFIX}/{key}",
                    "--tagging",
                    json.dumps(tagging),
                ],
                check=True,
            )

    def download(self, key, local_file):
        """download to local_file, creating its directory if needed"""
        os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
        subprocess.run(["aws", "s3", "cp", self.url(key), local_file], check=True)

    def list(self, prefix):
        """sorted keys under prefix"""
        out = subprocess.run(
            [
                "aws",
                "s3api",
                "list-objects-v2",
                "--bucket",
                self.bucket,
                "--prefix",
                f"{PREFIX}/{prefix}",
                "--output",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        # the cli prints nothing at all when no objects match
        contents = json.loads(out).get("Contents", []) if out.strip() else []
        return sorted(c["Key"][len(PREFIX) + 1 :] for c in contents)

    def delete_prefix(self, prefix):
        """delete every object under prefix/"""
        subprocess.run(
            ["aws", "s3", "rm", "--recursive", self.url(prefix) + "/"], check=True
        )


def git(*args):
    return subprocess.check_output(["git", *args]).decode("ascii").strip()


def committed_head():
    """the checked-out commit, refusing if any tracked file has uncommitted changes

    A run is recorded as the commit's, and released as the commit's output - so it must be
    exactly what the commit produces. Untracked files are ignored: the analysis can only use
    one if a tracked file refers to it, and that change would itself be uncommitted.
    """
    changes = git("status", "--porcelain", "--untracked-files=no")
    if changes:
        raise ValueError(
            "Tracked files have uncommitted changes - commit or stash them first, so the "
            f"run is the commit's output:\n{changes}"
        )
    return git("rev-parse", "HEAD")


def unzip_file(in_zip, out_file):
    """extract the single file in in_zip to out_file - whatever it's named in the zip"""
    with zipfile.ZipFile(in_zip) as z:
        (member,) = z.namelist()
        with z.open(member) as src, open(out_file, "wb") as dst:
            shutil.copyfileobj(src, dst)


def add_gpkg_layer(out_file, source_file, layer_name):
    """add source_file's layer to geopackage out_file (which must exist) as layer_name"""
    subprocess.run(
        ["ogr2ogr", "-f", "GPKG", "-update", out_file, source_file, "-nln", layer_name],
        check=True,
    )


def read_release_logs(store, work_dir):
    """read every release's change log rows (logs/<release_tag>/<log>.csv), downloading them
    to work_dir

    returns {log: df} with each df the rows of all releases combined and ordered by
    released_at, or None if no release has written that log yet
    """
    names = {f"{log}.csv": log for log in LOGS}
    frames = {log: [] for log in LOGS}
    for key in store.list("logs/"):
        parts = key.split("/")
        log = names.get(parts[2]) if len(parts) == 3 else None
        if log:
            local_file = os.path.join(work_dir, key)
            store.download(key, local_file)
            frames[log].append(pandas.read_csv(local_file))

    histories = {}
    for log, release_frames in frames.items():
        if not release_frames:
            histories[log] = None
            continue
        history = pandas.concat(release_frames, ignore_index=True)
        # released_at orders releases - tag names and commit dates needn't follow release order
        released_at = pandas.to_datetime(
            history["released_at"], utc=True, format="ISO8601"
        )
        histories[log] = history.iloc[released_at.argsort(kind="stable")].reset_index(
            drop=True
        )
    return histories


def latest_release(history):
    """tag of the most recent release in a history from read_release_logs(), or None"""
    if history is None:
        return None
    return history["release_tag"].iloc[-1]


def build_change(current_file, history, previous_release, columns):
    """merge this run's totals with the previous release's, keyed on the first column

    Retains every category present in either side (outer join) rather than just those in
    the previous release. Descriptive columns are filled from whichever side has them, so a
    category new to this run (or one dropped since the previous release) still gets its
    labels. Totals/diff/pct_diff are left as genuine NaN (not 0) for a category missing from
    one side, since 0 would misleadingly imply the category existed with a zero total.
    """
    key, *descriptive = columns

    previous = history[history["release_tag"] == previous_release][
        columns + [AREA]
    ].rename(columns={AREA: previous_release})
    current = pandas.read_csv(current_file)[columns + [AREA]].rename(
        columns={AREA: "current"}
    )

    merged = previous.merge(current, how="outer", on=key, suffixes=("_previous", ""))
    for col in descriptive:
        previous_col = merged.pop(f"{col}_previous")
        merged[col] = merged[col].combine_first(previous_col)

    merged["diff"] = merged["current"] - merged[previous_release]
    merged["pct_diff"] = (merged["diff"] / merged[previous_release]) * 100

    # key, descriptive columns, current, previous, diff, pct_diff - explicit rather than
    # relying on whatever order the merge happens to produce
    column_order = [key, *descriptive, "current", previous_release, "diff", "pct_diff"]
    return (
        merged[column_order]
        .round({previous_release: 0, "current": 0, "diff": 0, "pct_diff": 2})
        .set_index(key)
    )


def publish_draft(store, run_id, run_dir, totals_dir, commit):
    """publish run_dir's files under drafts/<run_id>/, as a run of commit

    run_dir holds the run's published files, other than the summaries - which are written
    there by this, comparing the area totals in totals_dir (<log>.csv) to the most recent
    release. The manifest is published last - its presence marks the run as complete, so
    release never picks up a run that failed partway through publishing.

    commit is committed_head() as it was when the run started - refuses to publish if the
    checkout has changed since, as the run then isn't commit's output.
    """
    head = committed_head()
    if head != commit:
        raise ValueError(
            f"The checkout moved from {commit} to {head} during the run - re-run against "
            "the commit to publish"
        )

    with tempfile.TemporaryDirectory() as logs_dir:
        histories = read_release_logs(store, logs_dir)
    previous_release = None
    for log, columns in LOGS.items():
        history = histories[log]
        if history is None:
            raise ValueError(
                f"No {log} change log found under {store.url('logs/')} "
                "- release at least one version before comparing to it"
            )
        previous_release = latest_release(history)
        summary_file = os.path.join(run_dir, summary(log))
        build_change(
            os.path.join(totals_dir, f"{log}.csv"), history, previous_release, columns
        ).to_csv(summary_file)
        LOG.info(f"{summary_file} written, comparing to {previous_release}")

    for name in DRAFT_FILES:
        store.upload(os.path.join(run_dir, name), draft_key(run_id, name))
        LOG.info(f"{name} published to {store.url(draft_key(run_id, name))}")

    manifest = {
        "run_id": run_id,
        "commit": commit,
        # the release this run's summaries are compared against
        "previous_release": previous_release,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": DRAFT_FILES,
    }
    manifest_file = os.path.join(run_dir, MANIFEST)
    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=2)
    store.upload(manifest_file, draft_key(run_id, MANIFEST))
    LOG.info(f"Run {run_id} published to {store.url(run_prefix(run_id))}/")


def find_run(store, commit, run_id, work_dir):
    """return the manifest of the run of commit to release

    run_id pins a specific run; otherwise the most recent complete run of commit is used.
    Only runs with a manifest (i.e. that finished publishing) are considered.
    """
    manifest_keys = [
        k for k in store.list("drafts/") if os.path.basename(k) == MANIFEST
    ]
    if run_id:
        candidates = [k for k in manifest_keys if k == draft_key(run_id, MANIFEST)]
    else:
        # run_ids are UTC timestamps, so this is newest first
        candidates = sorted(manifest_keys, reverse=True)

    for key in candidates:
        local_file = os.path.join(work_dir, key)
        store.download(key, local_file)
        with open(local_file) as f:
            manifest = json.load(f)
        if manifest["commit"] == commit:
            return manifest
        if run_id:
            raise ValueError(
                f"Run {run_id} is of commit {manifest['commit']}, not {commit} - "
                "check out that commit to release it"
            )

    run_desc = f"run {run_id}" if run_id else "run"
    raise ValueError(
        f"No complete {run_desc} of commit={commit} found under {store.url('drafts/')} "
        "- run the analysis against this commit before releasing it"
    )


def publish_release(store, run_id=None, out_dir=OUTPUTS):
    """release a run of the checked-out commit, which must be tagged with the release tag

    Defaults to the commit's most recent complete run; run_id pins a specific run. Refuses to
    release (before publishing anything) a tag that's already released, a run whose summaries
    compare against an older release than the latest, or a run missing any of its files.

    Everything is downloaded and built in out_dir/releases/<release_tag>/ before anything is
    published, each file at the path of its key in the bucket. The release's change log rows
    are published last: they mark the release as done, so a failure partway through
    publishing can be retried, and the retry overwrites whatever had been published so far.

    The released run's drafts/<run_id>/ prefix is left in place - prune-drafts deletes it.
    """
    commit = git("rev-parse", "HEAD")
    short_commit = git("rev-parse", "--short", "HEAD")
    release_tag = git("describe", "--tags", "--exact-match")
    release_date = git("log", "-1", "--format=%aI", commit)

    # cleared at the start of every run, so leftovers from a failed attempt are never reused
    work_dir = os.path.join(out_dir, "releases", release_tag)
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir)

    def local(key):
        return os.path.join(work_dir, key)

    # refuse to release a tag that's already logged. Each log's rows are published as a
    # separate file - if only some made it (a failure between the uploads), the release didn't
    # finish, so its partial rows are dropped here and the retry publishes them again
    histories = read_release_logs(store, work_dir)
    logged_in = [
        log
        for log, history in histories.items()
        if history is not None and release_tag in history["release_tag"].values
    ]
    if len(logged_in) == len(LOGS):
        raise ValueError(
            f"Release {release_tag} is already in {store.url(f'logs/{release_tag}')}/ "
            "- it has already been released"
        )
    for log in logged_in:
        LOG.warning(
            f"Release {release_tag} is only partially logged (in {log}) - re-publishing it"
        )
        history = histories[log]
        histories[log] = history[history["release_tag"] != release_tag]

    # resolve which run of this commit to release - once resolved it's pinned for everything
    # below, so every file in the release comes from the same run
    manifest = find_run(store, commit, run_id, work_dir)
    run_id = manifest["run_id"]
    LOG.info(f"Releasing {release_tag}: commit={commit}, run_id={run_id}")

    # the run's summaries compare against whichever release was latest when it ran. If
    # another release has happened since, their diffs are against an older release
    previous_release = latest_release(next(iter(histories.values())))
    if manifest["previous_release"] != previous_release:
        raise ValueError(
            f"Summaries for run_id={run_id} compare against {manifest['previous_release']}, "
            f"but the latest release is now {previous_release} - their diffs are against an "
            f"older release. Re-run the analysis to compare against {previous_release}, "
            "then release that run"
        )

    published = store.list(run_prefix(run_id) + "/")
    missing = [f for f in DRAFT_FILES if draft_key(run_id, f) not in published]
    if missing:
        raise ValueError(
            f"Run {run_id} is missing {', '.join(missing)} under "
            f"{store.url(run_prefix(run_id))}/ - re-run the analysis"
        )

    def run_file(name):
        return local(draft_key(run_id, name))

    for name in DRAFT_FILES:
        store.download(draft_key(run_id, name), run_file(name))

    # the latest files - the geopackages unzipped, the csvs as is
    csvs = [SOURCES_CSV] + [summary(log) for log in LOGS]
    latest_keys = [f"latest/{name}" for name in [OVERLAY_GPKG, SOURCES_GPKG] + csvs]
    os.makedirs(local("latest"))
    unzip_file(run_file(OVERLAY_ZIP), local(f"latest/{OVERLAY_GPKG}"))
    unzip_file(run_file(SOURCES_ZIP), local(f"latest/{SOURCES_GPKG}"))
    for name in csvs:
        shutil.copy(run_file(name), local(f"latest/{name}"))

    # the archive - one geopackage holding both layers, plus the csvs. The geopackage isn't
    # published on its own, so it's built outside the published keys, in bundle/
    bundle = local(f"bundle/{OVERLAY_GPKG}")
    os.makedirs(os.path.dirname(bundle))
    shutil.copy(local(f"latest/{OVERLAY_GPKG}"), bundle)
    add_gpkg_layer(bundle, local(f"latest/{SOURCES_GPKG}"), "designations")
    archive_key = f"releases/{ARCHIVE.format(release_tag=release_tag)}"
    os.makedirs(local("releases"))
    zip_files(local(archive_key), [bundle] + [run_file(name) for name in csvs])
    LOG.info(f"{archive_key} built")

    # this release's change log rows, from each summary's "current" column - a category with no
    # current value existed in the previous release but not this run, and is excluded rather
    # than logged as zero. released_at (when this release was made) orders the releases when
    # the logs are combined
    released_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    release_logs = []
    combined_logs = []
    for log, columns in LOGS.items():
        rows = (
            pandas.read_csv(run_file(summary(log)))[columns + ["current"]]
            .rename(columns={"current": AREA})
            .dropna(subset=[AREA])
        )
        rows["release_tag"] = release_tag
        rows["release_date"] = release_date
        rows["commit"] = short_commit
        rows["run_id"] = run_id
        rows["released_at"] = released_at

        release_log = release_log_key(release_tag, log)
        os.makedirs(os.path.dirname(local(release_log)), exist_ok=True)
        rows.to_csv(local(release_log), index=False)
        release_logs.append(release_log)

        # the combined log, rebuilt from every release's rows (fetched above) plus this one's
        if histories[log] is not None:
            rows = pandas.concat([histories[log], rows], ignore_index=True)
        rows.to_csv(local(combined_log_key(log)), index=False)
        combined_logs.append(combined_log_key(log))

    # everything is built - publish. First the archive, never overwritten once released, then
    # the latest files and combined change logs, and finally this release's change log rows,
    # marking the release as done
    tags = {"commit": commit, "run_id": run_id, "release": release_tag}
    for key in [archive_key] + latest_keys + combined_logs + release_logs:
        store.upload(local(key), key, tags)
        LOG.info(f"{local(key)} published to {store.url(key)}")
    LOG.info(f"Released {release_tag}")


def delete_drafts(store, older_than=None, dry_run=False, out_dir=OUTPUTS):
    """delete runs' drafts/<run_id>/ prefixes that are no longer needed, and their local copies

    By default only released runs are deleted - everything they published was copied into
    the release. With older_than (days), unreleased runs older than that are deleted too,
    apart from the newest unreleased run, which may still be under review.
    """
    # a run is released once its rows are in every change log - a run in only some is a
    # release that failed partway through publishing, whose retry still needs the draft
    with tempfile.TemporaryDirectory() as logs_dir:
        histories = read_release_logs(store, logs_dir)
    released = set.intersection(
        *(
            set() if history is None else set(history["run_id"].dropna().astype(str))
            for history in histories.values()
        )
    )

    # runs are drafts/<run_id>/<file>, with run_id a UTC timestamp - anything else under drafts/
    # isn't a run's, and is left alone
    run_times = {}
    for key in store.list("drafts/"):
        parts = key.split("/")
        if len(parts) != 3 or parts[1] in run_times:
            continue
        try:
            run_times[parts[1]] = datetime.strptime(parts[1], RUN_ID_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    run_ids = sorted(run_times)
    unreleased = [r for r in run_ids if r not in released]
    now = datetime.now(timezone.utc)

    for run_id in run_ids:
        if run_id in released:
            reason = "released"
        elif (
            older_than is not None
            and run_id != unreleased[-1]
            and (now - run_times[run_id]).days > older_than
        ):
            reason = f"unreleased, older than {older_than} days"
        else:
            continue

        prefix = run_prefix(run_id)
        local_dir = os.path.join(out_dir, prefix)
        if not os.path.isdir(local_dir):
            local_dir = None
        if dry_run:
            LOG.info(f"Would delete {store.url(prefix)}/ ({reason})")
            if local_dir:
                LOG.info(f"Would delete {local_dir}/")
        else:
            store.delete_prefix(prefix)
            LOG.info(f"Deleted {store.url(prefix)}/ ({reason})")
            if local_dir:
                shutil.rmtree(local_dir)
                LOG.info(f"Deleted {local_dir}/")


@click.group()
def cli():
    pass


@cli.command()
@click.argument("sources_file", type=click.Path(exists=True), default="sources.json")
@click.option(
    "--source_alias",
    "-s",
    default=None,
    help="Validate and download just the specified source",
)
@click.option(
    "--dry_run", "-t", is_flag=True, help="Validate sources_file only, do not download"
)
@click.option(
    "--out_path",
    "-o",
    type=click.Path(),
    default=CACHE_PATH,
    help="Output path to write data (local or s3://), defaults to s3://$BUCKET/harvest_restrictions/cache if $BUCKET is set",
)
@verbose_opt
@quiet_opt
def cache(sources_file, source_alias, dry_run, out_path, verbose, quiet):
    """Download sources defined in provided sources.json file"""
    configure_logging((verbose - quiet))

    if not out_path and not dry_run:
        raise ValueError("Output path not provided, set --out_path or $BUCKET")

    # load sources file
    with open(sources_file, "r") as f:
        sources = parse_sources(json.load(f))

    # if specified, use only one source
    if source_alias:
        if source_alias not in [s["alias"] for s in sources]:
            raise ValueError(f"Source {source_alias} is not present in {sources_file}")
        else:
            sources = [s for s in sources if s["alias"] == source_alias]

    sources = validate_sources(sources)

    # download each data source, dump to file
    if not dry_run:
        for source in sources:
            df = download_source(source)
            layer = (
                "hr_" + str(source["index"]).zfill(2) + "_" + source["alias"].lower()
            )
            # parquet is one file per layer and direct write to s3 is supported
            out_file = os.path.join(out_path, layer + ".parquet")
            df.to_parquet(out_file)

            LOG.info(f"{source['alias']} written to {out_file}")


@cli.command()
@click.option(
    "--path",
    "-p",
    type=click.Path(),
    default=CACHE_PATH,
    help="Path to clear cached source parquet files from (local or s3://), defaults to s3://$BUCKET/harvest_restrictions/cache if $BUCKET is set",
)
@click.option(
    "--dry_run",
    "-t",
    is_flag=True,
    help="List files that would be removed, do not delete",
)
@verbose_opt
@quiet_opt
def clear_cache(path, dry_run, verbose, quiet):
    """Delete cached parquet files written by cache (hr_*.parquet)"""
    configure_logging((verbose - quiet))

    if not path:
        raise ValueError("Path not provided, set --path or $BUCKET")

    if path.startswith("s3://"):
        cmd = [
            "aws",
            "s3",
            "rm",
            path,
            "--recursive",
            "--exclude",
            "*",
            "--include",
            "hr_*.parquet",
        ]
        if dry_run:
            cmd.append("--dryrun")
        subprocess.run(cmd, check=True)
    else:
        files = glob.glob(os.path.join(path, "hr_*.parquet"))
        for f in files:
            if dry_run:
                LOG.info(f"Would remove {f}")
            else:
                os.remove(f)
                LOG.info(f"Removed {f}")

    verb = "Would clear" if dry_run else "Cleared"
    LOG.info(f"{verb} cached source parquet files from {path}")


@cli.command()
@click.argument("sources_file", type=click.Path(exists=True), default="sources.json")
@click.option(
    "--in_path",
    "-p",
    type=click.Path(),
    default=CACHE_PATH,
    help="Path to read data (local or s3://), defaults to s3://$BUCKET/harvest_restrictions/cache if $BUCKET is set",
)
@click.option(
    "--db_url",
    "-db",
    help="Target database url, defaults to $DATABASE_URL environment variable if set",
    default=os.environ.get("DATABASE_URL"),
)
@click.option(
    "--out_table",
    "-o",
    help="Target output table. Appended to if the table already exists.",
)
@click.option(
    "--source_alias",
    "-s",
    default=None,
    help="Load just the specified source",
)
@click.option(
    "--truncate",
    is_flag=True,
    help="Truncate --out_table before loading, so re-running against it doesn't duplicate "
    "previously loaded rows. No effect without --out_table, since each per-source table is "
    "already replaced fresh on every load.",
)
@click.option(
    "--dry_run", "-t", is_flag=True, help="Validate sources_file only, do not load data"
)
@verbose_opt
@quiet_opt
def load_db(
    sources_file,
    in_path,
    db_url,
    out_table,
    source_alias,
    truncate,
    dry_run,
    verbose,
    quiet,
):
    """Load source layers from parquet cache to the postgresql db"""
    configure_logging((verbose - quiet))

    if not in_path and not dry_run:
        raise ValueError("Input path not provided, set --in_path or $BUCKET")

    # connect to db
    db = create_engine(db_url)

    # load sources file
    with open(sources_file, "r") as f:
        sources = parse_sources(json.load(f))

    # if specified, use only one source
    if source_alias:
        if source_alias not in [s["alias"] for s in sources]:
            raise ValueError(f"Source {source_alias} is not present in {sources_file}")
        else:
            sources = [s for s in sources if s["alias"] == source_alias]

    # only validate on dry-run
    if dry_run:
        sources = validate_sources(sources)

    else:
        if truncate:
            if out_table and inspect(db).has_table(out_table):
                with db.begin() as conn:
                    conn.execute(text(f"TRUNCATE TABLE {out_table}"))
                LOG.info(f"Truncated {out_table}")
            elif not out_table:
                LOG.warning("--truncate has no effect without --out_table")

        for source in sources:
            layer = (
                "hr_" + str(source["index"]).zfill(2) + "_" + source["alias"].lower()
            )
            in_file = os.path.join(in_path, layer + ".parquet")
            df = geopandas.read_parquet(in_file)
            # if out_table specified, write to that table, appending if it exists
            if out_table:
                df.to_postgis(out_table, db, if_exists="append")
                LOG.info(f"{source['alias']} written to {out_table}")
            # if out_table not provided, write to table with the layer name, overwriting if it exists
            else:
                df.to_postgis(layer, db, if_exists="replace")
                LOG.info(f"{source['alias']} written to {layer}")


@cli.command()
@click.option(
    "--db_url",
    "-db",
    help="Target database url, defaults to $DATABASE_URL environment variable if set",
    default=os.environ.get("DATABASE_URL"),
)
@click.option(
    "--designations_table",
    default="designations",
    help="Name of the source designations table (as loaded by load-db), dumped to geopackage alongside overlay outputs",
)
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to write outputs, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def overlay(db_url, designations_table, bucket, verbose, quiet):
    """Run per-tile overlay and write output and rollup/summaries to object storage

    Everything published is also kept locally, in outputs/drafts/<run_id>/
    """
    configure_logging((verbose - quiet))

    if not db_url:
        raise ValueError(
            "Target database url not provided, set --db_url or $DATABASE_URL"
        )
    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )

    # the run is published as this commit's - refuse to start with uncommitted changes, rather
    # than once the long-running overlay is done
    commit = committed_head()

    # this run's draft, written locally as it is published - just the published files.
    # Everything else (the unzipped geopackages, the area totals the summaries are built
    # from) is written to a temporary directory, removed when overlay exits
    run_id = datetime.now(timezone.utc).strftime(RUN_ID_FORMAT)
    run_dir = os.path.join(OUTPUTS, run_prefix(run_id))
    os.makedirs(run_dir)
    tmp_dir = tempfile.TemporaryDirectory()

    def tmp(name):
        return os.path.join(tmp_dir.name, name)

    def draft(name):
        return os.path.join(run_dir, name)

    psql = f"psql {db_url} -v ON_ERROR_STOP=1"

    # load 250k grid
    run("bcdata bc2pg WHSE_BASEMAPPING.NTS_250K_GRID")

    # clear any existing data from output table
    run(f'{psql} -c "TRUNCATE harvest_restrictions"')

    # run overlays in parallel per tile
    run(
        f'{psql} -tXA -c "SELECT DISTINCT map_tile '
        "FROM whse_basemapping.nts_250k_grid "
        'ORDER BY map_tile" '
        f"| parallel --tag {psql} -f sql/overlay.sql -v tile={{1}}"
    )

    # dump result to geopackage
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            tmp(OVERLAY_GPKG),
            f"PG:{db_url}",
            "-nlt",
            "MULTIPOLYGON",
            "-nln",
            "harvest_restrictions",
            "-sql",
            DUMP_SQL,
        ],
        check=True,
    )
    LOG.info(f"Overlay results written to {tmp(OVERLAY_GPKG)}")

    # dump source designations table to geopackage
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            tmp(SOURCES_GPKG),
            f"PG:{db_url}",
            designations_table,
        ],
        check=True,
    )
    LOG.info(f"{designations_table} written to {tmp(SOURCES_GPKG)}")

    # summarize results
    for log in LOGS:
        run(f"{psql} -f sql/{log}.sql --csv > {tmp(log + '.csv')}")

    # flatten sources.json to a csv, for review alongside the rest of this run's draft output
    write_sources_csv("sources.json", draft(SOURCES_CSV))

    # publish outputs to object storage as a draft, with summaries comparing this run's area
    # totals to the most recently released version. The totals themselves aren't published -
    # the summaries carry the same totals (in their "current" column) plus the diffs.
    zip_files(draft(OVERLAY_ZIP), [tmp(OVERLAY_GPKG)])
    zip_files(draft(SOURCES_ZIP), [tmp(SOURCES_GPKG)])
    publish_draft(S3Store(bucket), run_id, run_dir, tmp_dir.name, commit)
    tmp_dir.cleanup()
    LOG.info(
        f"run_id={run_id} - written to {run_dir}/. Pass --run_id to release to pin this exact run"
    )


@cli.command()
@click.option(
    "--run_id",
    default=None,
    help="Pin a specific overlay run to release, if this commit has been run more than once. Defaults to the most recent run of this commit",
)
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to publish outputs to, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def release(run_id, bucket, verbose, quiet):
    """Release an overlay run of the checked-out commit, which must be tagged with the release tag

    Publishes the release's archive under releases/, its change log rows under
    logs/<release_tag>/, and the fixed-name latest files under latest/ - see publish_release().
    The release is built locally first, in outputs/releases/<release_tag>/
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )
    publish_release(S3Store(bucket), run_id)


@cli.command()
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to prune drafts from, defaults to $BUCKET environment variable if set",
)
@click.option(
    "--older_than",
    type=int,
    default=None,
    help="Also delete unreleased runs older than this many days (the newest unreleased run is always kept)",
)
@click.option(
    "--dry_run",
    "-t",
    is_flag=True,
    help="List runs that would be deleted, do not delete",
)
@verbose_opt
@quiet_opt
def prune_drafts(bucket, older_than, dry_run, verbose, quiet):
    """Delete overlay runs' drafts/<run_id>/ prefixes that are no longer needed

    By default only released runs are deleted - everything they published was copied into
    the release. With --older_than, unreleased runs older than that are deleted too, apart
    from the newest unreleased run, which may still be under review. Local copies of the
    deleted runs, in outputs/drafts/<run_id>/, are deleted too.
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )
    delete_drafts(S3Store(bucket), older_than, dry_run)


if __name__ == "__main__":
    cli()
