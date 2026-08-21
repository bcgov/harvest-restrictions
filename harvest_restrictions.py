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
from sqlalchemy import create_engine

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
    """Download sources defined in provided file"""
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
    "--dry_run", "-t", is_flag=True, help="Validate sources_file only, do not load data"
)
@verbose_opt
@quiet_opt
def load_db(
    sources_file, in_path, db_url, out_table, source_alias, dry_run, verbose, quiet
):
    """Rather than use a FDW to connect directly to files, load them to the db"""
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


OVERLAY_SQL = """select
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
            OVERLAY_SQL,
        ],
        check=True,
    )


def s3_key(key):
    return f"harvest_restrictions/{key}"


def s3_get_tags(bucket, key, version_id=None):
    """return the tags currently set on an s3 object, optionally a specific version, as a dict"""
    cmd = ["aws", "s3api", "get-object-tagging", "--bucket", bucket, "--key", s3_key(key)]
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


def s3_add_tags(bucket, key, tags, version_id=None):
    """merge tags into whatever tags an s3 object (or object version) already has"""
    existing = s3_get_tags(bucket, key, version_id)
    existing.update(tags)
    s3_put_tags(bucket, key, existing, version_id)


def s3_upload_and_tag(bucket, local_file, key, tags):
    """upload local_file to object storage, tagging the resulting object version"""
    subprocess.run(
        ["aws", "s3", "cp", local_file, f"s3://{bucket}/{s3_key(key)}"], check=True
    )
    s3_put_tags(bucket, key, tags)


def s3_find_version(bucket, key, **tags):
    """find the most recent version of an s3 object matching all given tags

    a tag value of None means the tag key must be present, with any value - e.g.
    s3_find_version(bucket, key, commit=sha, run_id=None) requires a specific commit but
    accepts any run_id, while s3_find_version(bucket, key, commit=sha, run_id=rid) pins both.

    returns (version_id, tags) for the first (most recent) match, or (None, None)
    """
    out = subprocess.run(
        ["aws", "s3api", "list-object-versions", "--bucket", bucket, "--prefix", s3_key(key)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    versions = [v for v in json.loads(out).get("Versions", []) if v["Key"] == s3_key(key)]
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


def build_gpkg(bucket, commit, out_file, version_id=None, run_id=None):
    """download the commit-tagged geoparquet and convert it to a geopackage

    version_id can be passed in if already known (avoids a redundant lookup), otherwise the
    most recent version tagged commit=<commit> is used (optionally narrowed to a specific
    run_id, to pin an exact run when a commit has been run through overlay more than once).
    Returns the parquet version_id used.
    """
    parquet_key = "harvest_restrictions.parquet"
    if version_id is None:
        tag_filter = {"commit": commit}
        if run_id:
            tag_filter["run_id"] = run_id
        version_id, _ = s3_find_version(bucket, parquet_key, **tag_filter)
        if not version_id:
            raise ValueError(
                f"No s3://{bucket}/{s3_key(parquet_key)} object tagged commit={commit}"
                + (f", run_id={run_id}" if run_id else "")
                + " found - run overlay against this commit first"
            )
    s3_download_version(bucket, parquet_key, version_id, parquet_key)
    subprocess.run(
        ["ogr2ogr", "-f", "GPKG", out_file, parquet_key, "-nln", "harvest_restrictions"],
        check=True,
    )
    return version_id


def s3_download_release(bucket, key, local_file):
    """download the most recently released version of key to local_file

    returns True if a released version was found and downloaded, False otherwise
    """
    version_id, _ = s3_find_version(bucket, key, release=None)
    if not version_id:
        return False
    s3_download_version(bucket, key, version_id, local_file)
    return True


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

LAND_DESIGNATIONS_LOG = "land_designations_log.csv"
HARVEST_RESTRICTIONS_LOG = "harvest_restrictions_log.csv"


def log(bucket):
    """Compare current overlay summaries to the most recently released version, writing a review report

    The full change log is a long/tidy append-only record (one row per category per release), kept
    in land_designations_log.csv / harvest_restrictions_log.csv. This report is a small, disposable
    summary/rollup of that log against the current run, regenerated fresh on every overlay run.
    """
    if not s3_download_release(bucket, LAND_DESIGNATIONS_LOG, LAND_DESIGNATIONS_LOG):
        raise ValueError(
            f"No released {LAND_DESIGNATIONS_LOG} found in s3://{bucket}/harvest_restrictions/ "
            "- release at least one version before comparing to it"
        )
    if not s3_download_release(bucket, HARVEST_RESTRICTIONS_LOG, HARVEST_RESTRICTIONS_LOG):
        raise ValueError(
            f"No released {HARVEST_RESTRICTIONS_LOG} found in s3://{bucket}/harvest_restrictions/ "
            "- release at least one version before comparing to it"
        )
    d_history = pandas.read_csv(LAND_DESIGNATIONS_LOG)
    h_history = pandas.read_csv(HARVEST_RESTRICTIONS_LOG)

    # most recent release in the history
    previous_release = d_history.sort_values("release_date")["release_tag"].iloc[-1]

    d_previous = d_history[d_history["release_tag"] == previous_release][
        D_COLUMNS + ["area_ha"]
    ].rename(columns={"area_ha": previous_release})
    h_previous = h_history[h_history["release_tag"] == previous_release][
        H_COLUMNS + ["area_ha"]
    ].rename(columns={"area_ha": previous_release})

    d_summary = pandas.read_csv("current_land_designations.csv")[
        ["land_designation_type_rank", "area_ha"]
    ].rename(columns={"area_ha": "current"})
    h_summary = pandas.read_csv("current_harvest_restrictions.csv")[
        ["harvest_restriction_class_rank", "area_ha"]
    ].rename(columns={"area_ha": "current"})

    # join the previous release to the current summary
    d = d_previous.merge(d_summary, how="outer", on="land_designation_type_rank").fillna(0)
    h = h_previous.merge(h_summary, how="outer", on="harvest_restriction_class_rank").fillna(0)

    # calculate diff and pct diff against the previous release
    d["diff"] = d["current"] - d[previous_release]
    h["diff"] = h["current"] - h[previous_release]
    d["pct_diff"] = (d["diff"] / d[previous_release]) * 100
    h["pct_diff"] = (h["diff"] / h[previous_release]) * 100

    # clean up
    d = d.round({previous_release: 0, "current": 0, "diff": 0, "pct_diff": 2}).set_index(
        "land_designation_type_rank"
    )
    h = h.round({previous_release: 0, "current": 0, "diff": 0, "pct_diff": 2}).set_index(
        "harvest_restriction_class_rank"
    )

    # dump results to csv
    d.to_csv("land_designations_summary.csv")
    h.to_csv("harvest_restrictions_summary.csv")
    LOG.info("land_designations_summary.csv and harvest_restrictions_summary.csv written")


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
    help="Object storage bucket to publish outputs to, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def overlay(db_url, out_file, designations_table, bucket, verbose, quiet):
    """Run per-tile overlay of cached sources in postgres, publishing results/summaries to object storage"""
    configure_logging((verbose - quiet))

    if not db_url:
        raise ValueError(
            "Target database url not provided, set --db_url or $DATABASE_URL"
        )
    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("ascii").strip()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    psql = f"psql {db_url} -v ON_ERROR_STOP=1"

    # load 250k grid
    run("bcdata bc2pg WHSE_BASEMAPPING.NTS_250K_GRID")

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
    run(f"{psql} -f sql/land_designations.sql --csv > current_land_designations.csv")
    run(f"{psql} -f sql/harvest_restrictions.sql --csv > current_harvest_restrictions.csv")

    # compare current summaries to the most recently released version
    log(bucket)

    # publish outputs to object storage, tagged with the current commit and this run
    for local_file, key in [
        (out_file, os.path.basename(out_file)),
        (sources_file, sources_file),
        ("current_land_designations.csv", "current_land_designations.csv"),
        ("current_harvest_restrictions.csv", "current_harvest_restrictions.csv"),
        ("land_designations_summary.csv", "land_designations_summary.csv"),
        ("harvest_restrictions_summary.csv", "harvest_restrictions_summary.csv"),
    ]:
        s3_upload_and_tag(bucket, local_file, key, {"commit": commit, "run_id": run_id})
        LOG.info(
            f"{local_file} published to s3://{bucket}/{s3_key(key)}, "
            f"tagged commit={commit}, run_id={run_id}"
        )
    LOG.info(f"run_id={run_id} - pass --run_id to release/preview to pin this exact run")


@cli.command()
@click.option(
    "--run_id",
    default=None,
    help="Pin a specific overlay run to release, if this commit has been run more than once. Defaults to the most recent run of this commit",
)
@click.option(
    "--out_file",
    "-o",
    default="harvest_restrictions.gpkg.zip",
    help="Output geopackage path",
)
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to publish outputs to, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def release(run_id, out_file, bucket, verbose, quiet):
    """Tag the current commit's published overlay outputs as a release, and publish the geopackage deliverable

    Runs against the current commit's already-published, already-reviewed outputs only - it does not
    require a database connection, so it can run standalone (e.g. in a workflow triggered by a tag push,
    with no postgres service and no overlay having just run in the same job).
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("ascii").strip()
    short_commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode("ascii").strip()
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

    # resolve which run of this commit to release - defaults to the most recent, but
    # once resolved it's pinned for every key below, so all 6 published objects for
    # this release come from the same overlay run even if the commit was run more than once
    parquet_key = "harvest_restrictions.parquet"
    d_summary_key = "current_land_designations.csv"
    h_summary_key = "current_harvest_restrictions.csv"
    if not run_id:
        _, parquet_tags = s3_find_version(bucket, parquet_key, commit=commit)
        if not parquet_tags:
            raise ValueError(
                f"No s3://{bucket}/{s3_key(parquet_key)} object tagged commit={commit} found "
                "- run overlay against this commit before releasing it"
            )
        run_id = parquet_tags.get("run_id")
    LOG.info(f"Releasing commit={commit}, run_id={run_id}")

    # tag this commit's already-published outputs with the release
    tag_filter = {"commit": commit}
    if run_id:
        tag_filter["run_id"] = run_id
    version_ids = {}
    for key in [
        parquet_key,
        "harvest_restrictions_sources.parquet",
        d_summary_key,
        h_summary_key,
        "land_designations_summary.csv",
        "harvest_restrictions_summary.csv",
    ]:
        version_id, _ = s3_find_version(bucket, key, **tag_filter)
        if not version_id:
            raise ValueError(
                f"No s3://{bucket}/{s3_key(key)} object tagged commit={commit}"
                + (f", run_id={run_id}" if run_id else "")
                + " found - run overlay against this commit before releasing it"
            )
        s3_add_tags(bucket, key, {"release": release_tag}, version_id)
        LOG.info(f"{key} (commit {commit}, run_id {run_id}) tagged release={release_tag}")
        version_ids[key] = version_id

    # build the geopackage deliverable from that same reviewed parquet version - the
    # geopackage itself is only ever built at release time
    build_gpkg(bucket, commit, out_file, version_ids[parquet_key])
    s3_upload_and_tag(
        bucket, out_file, os.path.basename(out_file), {"commit": commit, "release": release_tag}
    )
    LOG.info(f"{out_file} published to s3://{bucket}/{s3_key(os.path.basename(out_file))}, tagged release={release_tag}")

    # append this release's summaries to the long-format change history
    s3_download_version(bucket, d_summary_key, version_ids[d_summary_key], d_summary_key)
    s3_download_version(bucket, h_summary_key, version_ids[h_summary_key], h_summary_key)

    d_row = pandas.read_csv(d_summary_key)[D_COLUMNS + ["area_ha"]]
    h_row = pandas.read_csv(h_summary_key)[H_COLUMNS + ["area_ha"]]
    for row in (d_row, h_row):
        row["release_tag"] = release_tag
        row["release_date"] = release_date
        row["commit"] = short_commit
        row["run_id"] = run_id

    if s3_download_release(bucket, LAND_DESIGNATIONS_LOG, LAND_DESIGNATIONS_LOG):
        d_history = pandas.concat([pandas.read_csv(LAND_DESIGNATIONS_LOG), d_row], ignore_index=True)
    else:
        d_history = d_row
    if s3_download_release(bucket, HARVEST_RESTRICTIONS_LOG, HARVEST_RESTRICTIONS_LOG):
        h_history = pandas.concat([pandas.read_csv(HARVEST_RESTRICTIONS_LOG), h_row], ignore_index=True)
    else:
        h_history = h_row

    d_history.to_csv(LAND_DESIGNATIONS_LOG, index=False)
    h_history.to_csv(HARVEST_RESTRICTIONS_LOG, index=False)
    s3_upload_and_tag(
        bucket, LAND_DESIGNATIONS_LOG, LAND_DESIGNATIONS_LOG, {"commit": commit, "release": release_tag}
    )
    s3_upload_and_tag(
        bucket, HARVEST_RESTRICTIONS_LOG, HARVEST_RESTRICTIONS_LOG, {"commit": commit, "release": release_tag}
    )
    LOG.info(f"Appended release {release_tag} to {LAND_DESIGNATIONS_LOG} and {HARVEST_RESTRICTIONS_LOG}")


@cli.command()
@click.option(
    "--commit",
    default=None,
    help="Commit to preview, defaults to the current HEAD",
)
@click.option(
    "--run_id",
    default=None,
    help="Pin a specific overlay run to preview, if this commit has been run more than once. Defaults to the most recent run of this commit",
)
@click.option(
    "--out_file",
    "-o",
    default="harvest_restrictions.gpkg.zip",
    help="Output geopackage path",
)
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to read published outputs from, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def preview(commit, run_id, out_file, bucket, verbose, quiet):
    """Build a preview geopackage for a commit's already-published overlay output, for review (e.g. by a client)

    This does not tag anything as a release and does not touch the change log - it is safe to run
    (and re-run) against draft work with no lasting effect. Once a version is actually approved, tag it
    and run release instead.
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )
    if not commit:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("ascii").strip()

    build_gpkg(bucket, commit, out_file, run_id=run_id)
    LOG.info(f"Preview geopackage for commit {commit} written to {out_file}")

    # pull the change summary already published alongside that commit's overlay output
    tag_filter = {"commit": commit}
    if run_id:
        tag_filter["run_id"] = run_id
    for key in ["land_designations_summary.csv", "harvest_restrictions_summary.csv"]:
        version_id, _ = s3_find_version(bucket, key, **tag_filter)
        if version_id:
            s3_download_version(bucket, key, version_id, key)
            LOG.info(f"{key} written")
        else:
            LOG.warning(f"No {key} found tagged commit={commit}")


if __name__ == "__main__":
    cli()
