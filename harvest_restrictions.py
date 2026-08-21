import csv
import glob
import json
import logging
import os
import subprocess
import sys
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


# inlined from cligj (unmaintained), as per
# https://github.com/rasterio/rasterio/pull/3364
verbose_opt = click.option("--verbose", "-v", count=True, help="Increase verbosity.")

quiet_opt = click.option("--quiet", "-q", count=True, help="Decrease verbosity.")


def configure_logging(verbosity):
    log_level = max(10, 30 - 10 * verbosity)
    logging.basicConfig(stream=sys.stderr, level=log_level, format=LOG_FORMAT)


def run(cmd):
    """run a shell command, logging it first"""
    LOG.info(cmd)
    subprocess.run(cmd, shell=True, check=True)


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
    default=".",
    help="Output path to write data (local or s3://)",
)
@verbose_opt
@quiet_opt
def cache(sources_file, source_alias, dry_run, out_path, verbose, quiet):
    """Download sources defined in provided sources.json file"""
    configure_logging((verbose - quiet))

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
    default=".",
    help="Path to clear cached source parquet files from (local or s3://)",
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
    default=".",
    help="Path to read data (local or s3://)",
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


DUMP_SQL = """select
  harvest_restrictions_id,
  land_designation_name,
  land_designation_type_rank,
  land_designation_type_code,
  land_designation_type_name,
  land_designation_primary_key,
  harvest_restriction_class_rank,
  harvest_restriction_class_name,
  array_to_string(trim_array(all_land_desig_names, 1), ';') as all_land_desig_names,
  array_to_string(trim_array(all_land_desig_type_ranks, 1), ';') as all_land_desig_type_ranks,
  array_to_string(trim_array(all_land_desig_type_codes, 1), ';') as all_land_desig_type_codes,
  array_to_string(trim_array(all_land_desig_type_names, 1), ';') as all_land_desig_type_names,
  array_to_string(trim_array(all_land_desig_primary_keys, 1), ';') as all_land_desig_primary_keys,
  array_to_string(trim_array(all_harv_restrict_class_ranks, 1), ';') as all_harv_restrict_class_ranks,
  array_to_string(trim_array(all_harv_restrict_class_names, 1), ';') as all_harv_restrict_class_names,
  map_tile_250k,
  geom
from harvest_restrictions
where
all_harv_restrict_class_ranks @> ARRAY[6] and
all_harv_restrict_class_ranks != ARRAY[6]"""


def export_overlay(db_url, out_file, out_format):
    """export the overlay result table from postgres to out_file, in the given ogr2ogr format"""
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            out_format,
            out_file,
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


def s3_key(key):
    return f"harvest_restrictions/{key}"


def draft_key(name):
    """derive the draft (overlay-published) s3 key for a local file/base name

    overlay() publishes its unreviewed, continuously-overwritten output under the draft/
    prefix, so it never collides with the plain-named "latest confirmed release" pointers
    that release() publishes separately at the root.
    """
    return f"draft/{name}"


def s3_get_tags(bucket, key, version_id=None):
    """return the tags currently set on an s3 object, optionally a specific version, as a dict"""
    cmd = [
        "aws",
        "s3api",
        "get-object-tagging",
        "--bucket",
        bucket,
        "--key",
        s3_key(key),
    ]
    if version_id:
        cmd += ["--version-id", version_id]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return {t["Key"]: t["Value"] for t in json.loads(out)["TagSet"]}


def s3_put_tags(bucket, key, tags, version_id=None):
    """replace the full tag set on an s3 object, optionally a specific version"""
    tagging = {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}
    cmd = [
        "aws",
        "s3api",
        "put-object-tagging",
        "--bucket",
        bucket,
        "--key",
        s3_key(key),
        "--tagging",
        json.dumps(tagging),
    ]
    if version_id:
        cmd += ["--version-id", version_id]
    subprocess.run(cmd, check=True)


def s3_upload_and_tag(bucket, local_file, key, tags):
    """upload local_file to object storage, tagging the resulting object version"""
    subprocess.run(
        ["aws", "s3", "cp", local_file, f"s3://{bucket}/{s3_key(key)}"], check=True
    )
    s3_put_tags(bucket, key, tags)


def s3_delete(bucket, key):
    """delete an s3 object

    On a versioned bucket this adds a delete marker as the new current version rather than
    erasing history - prior versions (including whatever release() just read) stay retrievable
    by version id, same as any other noncurrent version, subject to the bucket's lifecycle policy.
    """
    subprocess.run(["aws", "s3", "rm", f"s3://{bucket}/{s3_key(key)}"], check=True)


def s3_find_version(bucket, key, **tags):
    """find the most recent version of an s3 object matching all given tags

    a tag value of None means the tag key must be present, with any value - e.g.
    s3_find_version(bucket, key, commit=sha, run_id=None) requires a specific commit but
    accepts any run_id, while s3_find_version(bucket, key, commit=sha, run_id=rid) pins both.

    returns (version_id, tags) for the first (most recent) match, or (None, None)
    """
    out = subprocess.run(
        [
            "aws",
            "s3api",
            "list-object-versions",
            "--bucket",
            bucket,
            "--prefix",
            s3_key(key),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    versions = [
        v for v in json.loads(out).get("Versions", []) if v["Key"] == s3_key(key)
    ]
    versions.sort(key=lambda v: v["LastModified"], reverse=True)
    for v in versions:
        version_tags = s3_get_tags(bucket, key, v["VersionId"])
        if all(
            k in version_tags and (expected is None or version_tags[k] == expected)
            for k, expected in tags.items()
        ):
            return v["VersionId"], version_tags
    return None, None


def s3_download_version(bucket, key, version_id, local_file):
    subprocess.run(
        [
            "aws",
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            s3_key(key),
            "--version-id",
            version_id,
            local_file,
        ],
        check=True,
    )


def s3_download_tagged(bucket, key, tag_filter, local_file=None):
    """download the most recent version of an s3 object matching tag_filter to local_file

    Raises if no matching version is found. local_file defaults to key's basename.
    """
    local_file = local_file or os.path.basename(key)
    version_id, _ = s3_find_version(bucket, key, **tag_filter)
    if not version_id:
        tag_desc = ", ".join(f"{k}={v}" for k, v in tag_filter.items() if v)
        raise ValueError(
            f"No s3://{bucket}/{s3_key(key)} object tagged {tag_desc} found - "
            "run overlay against this commit before releasing it"
        )
    s3_download_version(bucket, key, version_id, local_file)
    return local_file


def add_gpkg_layer(out_file, source_file, layer_name, spatial=True):
    """add source_file as a new layer (or non-spatial table) in out_file, creating it if needed"""
    cmd = ["ogr2ogr", "-f", "GPKG"]
    if os.path.exists(out_file):
        cmd.append("-update")
    cmd += [out_file, source_file, "-nln", layer_name]
    if not spatial:
        cmd += ["-nlt", "NONE", "-oo", "AUTODETECT_TYPE=YES"]
    subprocess.run(cmd, check=True)


def s3_download_current(bucket, key, local_file):
    """download the current (latest) version of an s3 object to local_file

    returns True if the object exists and was downloaded, False otherwise
    """
    result = subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{s3_key(key)}", local_file],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


D_COLUMNS = [
    "land_designation_type_rank",
    "harvest_restriction_class_rank",
    "harvest_restriction_class_name",
    "land_designation_type_code",
    "land_designation_type_name",
]
H_COLUMNS = [
    "harvest_restriction_class_rank",
    "harvest_restriction_class_name",
]

LAND_DESIGNATIONS = "land_designations.csv"
HARVEST_RESTRICTIONS = "harvest_restrictions.csv"
LAND_DESIGNATIONS_SUMMARY = "land_designations_summary.csv"
HARVEST_RESTRICTIONS_SUMMARY = "harvest_restrictions_summary.csv"
LAND_DESIGNATIONS_LOG = "LOG_land_designations.csv"
HARVEST_RESTRICTIONS_LOG = "LOG_harvest_restrictions.csv"
SOURCES_CSV = "sources.csv"

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


def build_change(current_file, history, previous_release, key, columns):
    """merge this run's area totals with the previous release's, keyed on the given column

    Retains every category present in either side (outer join) rather than just those in
    the previous release. Descriptive columns are filled from whichever side has them, so a
    category new to this run (or one dropped since the previous release) still gets its
    labels. Area/diff/pct_diff are left as genuine NaN (not 0) for a category missing from
    one side, since 0 would misleadingly imply the category existed with no area.
    """
    descriptive = [c for c in columns if c != key]

    previous = history[history["release_tag"] == previous_release][
        columns + ["area_ha"]
    ].rename(columns={"area_ha": previous_release})
    current = pandas.read_csv(current_file)[columns + ["area_ha"]].rename(
        columns={"area_ha": "current"}
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


def log(bucket):
    """Compare current overlay results to the most recently released version, writing a change report

    The full change log is a long/tidy append-only record (one row per category per release), kept
    in LOG_land_designations.csv / LOG_harvest_restrictions.csv. This report is a small, disposable
    rollup of that log against the current run, regenerated fresh on every overlay run.
    """
    if not s3_download_current(bucket, LAND_DESIGNATIONS_LOG, LAND_DESIGNATIONS_LOG):
        raise ValueError(
            f"No {LAND_DESIGNATIONS_LOG} found in s3://{bucket}/harvest_restrictions/ "
            "- release at least one version before comparing to it"
        )
    if not s3_download_current(
        bucket, HARVEST_RESTRICTIONS_LOG, HARVEST_RESTRICTIONS_LOG
    ):
        raise ValueError(
            f"No {HARVEST_RESTRICTIONS_LOG} found in s3://{bucket}/harvest_restrictions/ "
            "- release at least one version before comparing to it"
        )
    d_history = pandas.read_csv(LAND_DESIGNATIONS_LOG)
    h_history = pandas.read_csv(HARVEST_RESTRICTIONS_LOG)

    # most recent release in the history
    previous_release = d_history.sort_values("release_date")["release_tag"].iloc[-1]

    d = build_change(
        LAND_DESIGNATIONS,
        d_history,
        previous_release,
        "land_designation_type_rank",
        D_COLUMNS,
    )
    h = build_change(
        HARVEST_RESTRICTIONS,
        h_history,
        previous_release,
        "harvest_restriction_class_rank",
        H_COLUMNS,
    )

    # dump results to csv
    d.to_csv(LAND_DESIGNATIONS_SUMMARY)
    h.to_csv(HARVEST_RESTRICTIONS_SUMMARY)
    LOG.info(f"{LAND_DESIGNATIONS_SUMMARY} and {HARVEST_RESTRICTIONS_SUMMARY} written")


@cli.command()
@click.option(
    "--db_url",
    "-db",
    help="Target database url, defaults to $DATABASE_URL environment variable if set",
    default=os.environ.get("DATABASE_URL"),
)
@click.option(
    "--out_file",
    "-o",
    default="harvest_restrictions.parquet",
    help="Output geoparquet path",
)
@click.option(
    "--designations_table",
    default="designations",
    help="Name of the source designations table (as loaded by load-db), dumped to geoparquet alongside overlay outputs",
)
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to write outputs, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def overlay(db_url, out_file, designations_table, bucket, verbose, quiet):
    """Run per-tile overlay and write output and rollup/summaries to object storage"""
    configure_logging((verbose - quiet))

    if not db_url:
        raise ValueError(
            "Target database url not provided, set --db_url or $DATABASE_URL"
        )
    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )

    commit = (
        subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("ascii").strip()
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

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

    # dump result to geoparquet
    export_overlay(db_url, out_file, "Parquet")
    LOG.info(f"Overlay results written to {out_file}")

    # dump source designations table to geoparquet
    sources_file = "harvest_restrictions_sources.parquet"
    subprocess.run(
        ["ogr2ogr", "-f", "Parquet", sources_file, f"PG:{db_url}", designations_table],
        check=True,
    )
    LOG.info(f"{designations_table} written to {sources_file}")

    # summarize results
    run(f"{psql} -f sql/land_designations.sql --csv > {LAND_DESIGNATIONS}")
    run(f"{psql} -f sql/harvest_restrictions.sql --csv > {HARVEST_RESTRICTIONS}")

    # compare current summaries to the most recently released version
    log(bucket)

    # flatten sources.json to a csv, for review alongside the rest of this run's draft output
    write_sources_csv("sources.json", SOURCES_CSV)

    # publish outputs to object storage, tagged with the current commit and this run, under the
    # draft/ prefix so they never collide with the plain-named "latest confirmed release"
    # pointers release() publishes separately at the root. The raw land_designations.csv/
    # harvest_restrictions.csv sql output is a local scratch file only, consumed above by log() -
    # not published, since land_designations_summary.csv/harvest_restrictions_summary.csv already
    # carry the same current-run totals (in their "current" column) plus the diff against the
    # previous release
    for local_file, key in [
        (out_file, draft_key(os.path.basename(out_file))),
        (sources_file, draft_key(sources_file)),
        (LAND_DESIGNATIONS_SUMMARY, draft_key(LAND_DESIGNATIONS_SUMMARY)),
        (HARVEST_RESTRICTIONS_SUMMARY, draft_key(HARVEST_RESTRICTIONS_SUMMARY)),
        (SOURCES_CSV, draft_key(SOURCES_CSV)),
    ]:
        s3_upload_and_tag(bucket, local_file, key, {"commit": commit, "run_id": run_id})
        LOG.info(
            f"{local_file} published to s3://{bucket}/{s3_key(key)}, "
            f"tagged commit={commit}, run_id={run_id}"
        )
    LOG.info(f"run_id={run_id} - pass --run_id to release to pin this exact run")


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
@click.option(
    "--clean_draft",
    "-c",
    is_flag=True,
    help="Delete overlay()'s draft/-prefixed objects for this run after a successful release",
)
@verbose_opt
@quiet_opt
def release(run_id, bucket, clean_draft, verbose, quiet):
    """Publish a dated release from the current commit's already-published, already-reviewed overlay output

    Publishes a single geopackage under releases/, release-tag-stamped and never overwritten, so
    every past release stays retrievable regardless of any noncurrent-version lifecycle policy:
    releases/harvest_restrictions_<tag>.gpkg. It bundles every release deliverable as one table
    each - a single file, directly readable by ogr/QGIS with no unzip step:

    - harvest_restrictions, designations - spatial layers (the overlay result and its source
      designations)
    - land_designations_summary, harvest_restrictions_summary - non-spatial tables, the reviewed
      diff report that was approved for this release
    - sources - non-spatial table, a flattened sources.json as it stood for this release

    Every table is sourced from overlay()'s already-published draft/ output for the given
    commit/run_id, rather than freshly recomputed here, so a release always reflects exactly
    what was reviewed.

    Also overwrites five fixed-name "latest" objects at the root, all plain names with no
    suffix - the same five deliverables as separate files rather than one geopackage, for
    scripts/mapping applications that want the current release without tracking release tags:
    harvest_restrictions.gpkg, harvest_restrictions_sources.gpkg, land_designations_summary.csv,
    harvest_restrictions_summary.csv, sources.csv. overlay()'s own draft-tier output lives
    separately under the draft/ prefix (see draft_key()), so the two naming schemes never
    collide and this pointer always reflects only the last confirmed release. All five are
    fully redundant with the releases/ geopackage, so it's fine to prune their version history
    under any lifecycle policy.

    Also appends this release's totals to the durable change log. Does not require a database
    connection, so it can run standalone (e.g. in a workflow triggered by a tag push, with no
    postgres service and no overlay having just run in the same job).

    With --clean_draft, also deletes overlay()'s draft/-prefixed objects for the run just
    released, once everything above has published successfully. On a versioned bucket this is
    non-destructive - it adds a delete marker rather than erasing the tagged version, so the
    data stays retrievable by version id under the bucket's lifecycle policy, same as any other
    noncurrent version.
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )

    commit = (
        subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("ascii").strip()
    )
    short_commit = (
        subprocess.check_output(["git", "rev-parse", "--short", "HEAD"])
        .decode("ascii")
        .strip()
    )
    release_tag = (
        subprocess.check_output(["git", "describe", "--tags", "--exact-match"])
        .decode("ascii")
        .strip()
    )
    release_date = (
        subprocess.check_output(["git", "log", "-1", "--format=%aI", commit])
        .decode("ascii")
        .strip()
    )

    # resolve which run of this commit to release - defaults to the most recent, but once
    # resolved it's pinned for everything below, so every table in the release geopackage comes
    # from the same overlay run even if the commit was run more than once
    if not run_id:
        _, found_tags = s3_find_version(
            bucket, draft_key("harvest_restrictions.parquet"), commit=commit
        )
        if not found_tags:
            raise ValueError(
                f"No s3://{bucket}/{s3_key(draft_key('harvest_restrictions.parquet'))} object "
                f"tagged commit={commit} found - run overlay against this commit before releasing it"
            )
        run_id = found_tags.get("run_id")
    LOG.info(f"Releasing commit={commit}, run_id={run_id}")
    tag_filter = {"commit": commit}
    if run_id:
        tag_filter["run_id"] = run_id

    tags = {"commit": commit, "run_id": run_id, "release": release_tag}

    # build the single dated release geopackage, one table at a time - add_gpkg_layer creates it
    # fresh on the first call and appends on every call after. Each spatial layer is also built
    # as its own standalone geopackage, for the fixed-name "latest" pointers at the root - for
    # scripts/mapping applications that want the current release without tracking release tags.
    # These are plain names at the root - it's overlay()'s draft-tier keys that live under the
    # separate draft/ prefix, so the two naming schemes never collide
    gpkg_file = f"harvest_restrictions_{release_tag}.gpkg"

    for parquet_key, layer_name, latest_file in [
        (
            "harvest_restrictions.parquet",
            "harvest_restrictions",
            "harvest_restrictions.gpkg",
        ),
        (
            "harvest_restrictions_sources.parquet",
            "designations",
            "harvest_restrictions_sources.gpkg",
        ),
    ]:
        local_parquet = s3_download_tagged(bucket, draft_key(parquet_key), tag_filter)
        add_gpkg_layer(gpkg_file, local_parquet, layer_name)
        LOG.info(f"{layer_name} layer added to {gpkg_file}, from {parquet_key}")

        add_gpkg_layer(latest_file, local_parquet, layer_name)
        s3_upload_and_tag(bucket, latest_file, latest_file, tags)
        LOG.info(f"{latest_file} published to s3://{bucket}/{s3_key(latest_file)}")

    # the summary csvs and sources listing are added to the release geopackage as non-spatial
    # tables, and published separately as their own fixed-name "latest" pointers, at the same
    # plain name they're read from locally. All three are downloaded from overlay()'s draft/
    # copy for this commit/run_id rather than regenerated here, so the release always reflects
    # exactly what was reviewed
    for key, table_name in [
        (LAND_DESIGNATIONS_SUMMARY, "land_designations_summary"),
        (HARVEST_RESTRICTIONS_SUMMARY, "harvest_restrictions_summary"),
        (SOURCES_CSV, "sources"),
    ]:
        s3_download_tagged(bucket, draft_key(key), tag_filter, local_file=key)
        add_gpkg_layer(gpkg_file, key, table_name, spatial=False)
        LOG.info(f"{table_name} table added to {gpkg_file}, from {key}")

        s3_upload_and_tag(bucket, key, key, tags)
        LOG.info(f"{key} published to s3://{bucket}/{s3_key(key)}")

    # publish the dated geopackage under releases/ - never overwritten, so past releases stay
    # retrievable regardless of any noncurrent-version lifecycle policy
    s3_upload_and_tag(bucket, gpkg_file, f"releases/{gpkg_file}", tags)
    LOG.info(
        f"{gpkg_file} published to s3://{bucket}/{s3_key(f'releases/{gpkg_file}')}"
    )

    # append this release's totals to the durable change log - release is the only writer of
    # these two files, so the current version is always the complete up-to-date history. Sourced
    # from the "current" column of the summary csvs just downloaded above, rather than a
    # separately-published current-only file - a category with no "current" value there existed
    # in the previous release but not this run, and is excluded rather than logged as zero area
    d_row = (
        pandas.read_csv(LAND_DESIGNATIONS_SUMMARY)[D_COLUMNS + ["current"]]
        .rename(columns={"current": "area_ha"})
        .dropna(subset=["area_ha"])
    )
    h_row = (
        pandas.read_csv(HARVEST_RESTRICTIONS_SUMMARY)[H_COLUMNS + ["current"]]
        .rename(columns={"current": "area_ha"})
        .dropna(subset=["area_ha"])
    )
    for row in (d_row, h_row):
        row["release_tag"] = release_tag
        row["release_date"] = release_date
        row["commit"] = short_commit
        row["run_id"] = run_id

    if s3_download_current(bucket, LAND_DESIGNATIONS_LOG, LAND_DESIGNATIONS_LOG):
        d_history = pandas.concat(
            [pandas.read_csv(LAND_DESIGNATIONS_LOG), d_row], ignore_index=True
        )
    else:
        d_history = d_row
    if s3_download_current(bucket, HARVEST_RESTRICTIONS_LOG, HARVEST_RESTRICTIONS_LOG):
        h_history = pandas.concat(
            [pandas.read_csv(HARVEST_RESTRICTIONS_LOG), h_row], ignore_index=True
        )
    else:
        h_history = h_row

    d_history.to_csv(LAND_DESIGNATIONS_LOG, index=False)
    h_history.to_csv(HARVEST_RESTRICTIONS_LOG, index=False)
    s3_upload_and_tag(bucket, LAND_DESIGNATIONS_LOG, LAND_DESIGNATIONS_LOG, tags)
    s3_upload_and_tag(bucket, HARVEST_RESTRICTIONS_LOG, HARVEST_RESTRICTIONS_LOG, tags)
    LOG.info(
        f"Appended release {release_tag} to {LAND_DESIGNATIONS_LOG} and {HARVEST_RESTRICTIONS_LOG}"
    )

    if clean_draft:
        for draft in [
            draft_key("harvest_restrictions.parquet"),
            draft_key("harvest_restrictions_sources.parquet"),
            draft_key(LAND_DESIGNATIONS_SUMMARY),
            draft_key(HARVEST_RESTRICTIONS_SUMMARY),
            draft_key(SOURCES_CSV),
        ]:
            s3_delete(bucket, draft)
            LOG.info(f"Deleted draft object s3://{bucket}/{s3_key(draft)}")


if __name__ == "__main__":
    cli()
