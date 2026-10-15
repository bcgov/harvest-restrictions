import csv
import glob
import json
import logging
import os
import subprocess
import sys
from datetime import datetime

import bcdata
import click
import geopandas
import jsonschema
from pyproj import CRS
from shapely.geometry.linestring import LineString
from shapely.geometry.multilinestring import MultiLineString
from shapely.geometry.multipoint import MultiPoint
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon
from slugify import slugify
from sqlalchemy import create_engine, inspect, text

import datarelease

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


def zip_file(local_file):
    """compress local_file into local_file + '.zip', containing just that one file

    Uses sozip (bundled with GDAL) rather than plain zip, so the result is seek-optimized -
    GDAL's /vsizip/ reader can fetch just the bytes it needs instead of decompressing the
    whole archive.
    """
    out_zip = local_file + ".zip"
    subprocess.run(
        ["sozip", "--overwrite", "--junk-paths", out_zip, local_file], check=True
    )
    return out_zip


# what overlay publishes and release releases - see datarelease
RELEASE_CONFIG = "release.json"

# the files overlay publishes, by the release.json output names they're published as
OVERLAY_ZIP = "harvest_restrictions.gpkg.zip"
SOURCES_GPKG = "harvest_restrictions_sources.gpkg"
SOURCES_ZIP = SOURCES_GPKG + ".zip"
SOURCES_CSV = "sources.csv"
RELEASE_FILES = [OVERLAY_ZIP, SOURCES_ZIP, SOURCES_CSV]

# overlay's per-category area totals (compared to the previous release, and logged), by the
# release.json log they're for
LAND_DESIGNATIONS = "land_designations.csv"
HARVEST_RESTRICTIONS = "harvest_restrictions.csv"
RELEASE_TOTALS = {
    "land_designations": LAND_DESIGNATIONS,
    "harvest_restrictions": HARVEST_RESTRICTIONS,
}

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
    default="harvest_restrictions.gpkg",
    help="Output geopackage path",
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

    # read the release config, and check it matches what overlay publishes, before the
    # long-running overlay - rather than failing once it's done
    config = datarelease.load_config(RELEASE_CONFIG)
    datarelease.check_inputs(config, RELEASE_FILES, RELEASE_TOTALS)

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
    export_overlay(db_url, out_file, "GPKG")
    LOG.info(f"Overlay results written to {out_file}")

    # dump source designations table to geopackage
    subprocess.run(
        ["ogr2ogr", "-f", "GPKG", SOURCES_GPKG, f"PG:{db_url}", designations_table],
        check=True,
    )
    LOG.info(f"{designations_table} written to {SOURCES_GPKG}")

    # summarize results
    run(f"{psql} -f sql/land_designations.sql --csv > {LAND_DESIGNATIONS}")
    run(f"{psql} -f sql/harvest_restrictions.sql --csv > {HARVEST_RESTRICTIONS}")

    # flatten sources.json to a csv, for review alongside the rest of this run's draft output
    write_sources_csv("sources.json", SOURCES_CSV)

    # publish outputs to object storage as a draft, with summaries comparing this run's area
    # totals to the most recently released version. The totals themselves aren't published -
    # the summaries carry the same totals (in their "current" column) plus the diffs. Spatial
    # outputs are published zipped - out_file's zip is published as OVERLAY_ZIP whatever
    # out_file is named, which is fine as datarelease reads single-file zips without naming
    # the file inside
    run_id = datarelease.publish_draft(
        config,
        datarelease.S3Store(bucket, config["prefix"]),
        files={
            OVERLAY_ZIP: zip_file(out_file),
            SOURCES_ZIP: zip_file(SOURCES_GPKG),
            SOURCES_CSV: SOURCES_CSV,
        },
        current=RELEASE_TOTALS,
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
@verbose_opt
@quiet_opt
def release(run_id, bucket, verbose, quiet):
    """Release an overlay run of the checked-out commit, which must be tagged with the release tag

    Publishes the release's archive under releases/, its change log rows under
    logs/<release_tag>/, and the fixed-name latest files under latest/, as configured in release.json - see
    datarelease.release()
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )
    config = datarelease.load_config(RELEASE_CONFIG)
    datarelease.release(config, datarelease.S3Store(bucket, config["prefix"]), run_id)


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
    from the newest unreleased run, which may still be under review.
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )
    config = datarelease.load_config(RELEASE_CONFIG)
    datarelease.prune_drafts(
        config, datarelease.S3Store(bucket, config["prefix"]), older_than, dry_run
    )


if __name__ == "__main__":
    cli()
