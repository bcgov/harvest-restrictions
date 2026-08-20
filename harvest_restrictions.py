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
def download(sources_file, source_alias, dry_run, out_path, verbose, quiet):
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
def cache2pg(
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
    default="harvest_restrictions.gpkg.zip",
    help="Output geopackage path",
)
@verbose_opt
@quiet_opt
def overlay(db_url, out_file, verbose, quiet):
    """Run per-tile overlay of cached sources in postgres and dump results to file"""
    configure_logging((verbose - quiet))

    if not db_url:
        raise ValueError(
            "Target database url not provided, set --db_url or $DATABASE_URL"
        )

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

    # dump result to file
    sql = """select
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
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            out_file,
            f"PG:{db_url}",
            "-nlt",
            "MULTIPOLYGON",
            "-nln",
            "harvest_restrictions",
            "-sql",
            sql,
        ],
        check=True,
    )
    LOG.info(f"Overlay results written to {out_file}")

    # summarize results
    run(f"{psql} -f sql/land_designations.sql --csv > current_land_designations.csv")
    run(f"{psql} -f sql/harvest_restrictions.sql --csv > current_harvest_restrictions.csv")


@cli.command(name="log")
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket holding previous release logs, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def log_cmd(bucket, verbose, quiet):
    """Compare current overlay summaries to previous releases, writing updated change logs"""
    configure_logging((verbose - quiet))

    s3 = f"s3://{bucket}/harvest_restrictions"

    # current release column header comes from git tag
    tag = subprocess.check_output(["git", "describe", "--tags"]).decode("ascii").strip()

    # read data
    d_log = pandas.read_csv(os.path.join(s3, "log_land_designations.csv"))
    d_summary = pandas.read_csv("current_land_designations.csv")
    h_log = pandas.read_csv(os.path.join(s3, "log_harvest_restrictions.csv"))
    h_summary = pandas.read_csv("current_harvest_restrictions.csv")

    # log columns - retain only the categories and area_ha of previous releases
    d_columns = [
        "land_designation_type_rank",
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
        "land_designation_type_code",
        "land_designation_type_name",
    ]
    h_columns = [
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
    ]

    # extract release tags from columns, discarding any with DRAFT in the name
    releases = list(
        set(d_log.columns).difference(set(d_columns + ["diff", "pct_diff"]))
    )
    releases = [r for r in releases if "DRAFT" not in r.upper()]
    releases = sorted(releases)
    # strip existing diff columns
    d_log = d_log[d_columns + releases]
    h_log = h_log[h_columns + releases]

    # summary columns - drop everything but keys and current area totals
    d_summary = d_summary[["land_designation_type_rank", "area_ha"]]
    h_summary = h_summary[["harvest_restriction_class_rank", "area_ha"]]

    # join the log to the latest summary
    d = d_log.merge(d_summary, how="outer", on="land_designation_type_rank").fillna(0)
    h = h_log.merge(h_summary, how="outer", on="harvest_restriction_class_rank").fillna(
        0
    )

    # use current tag as new column name
    d = d.rename(columns={"area_ha": tag})
    h = h.rename(columns={"area_ha": tag})

    # calculate diff and pct diff
    previous_tag = releases[-1]
    d["diff"] = d[tag] - d[previous_tag]
    h["diff"] = h[tag] - h[previous_tag]
    d["pct_diff"] = (d["diff"] / d[previous_tag]) * 100
    h["pct_diff"] = (h["diff"] / h[previous_tag]) * 100

    # clean up
    d = d.round({tag: 0, "diff": 0, "pct_diff": 2}).set_index(
        "land_designation_type_rank"
    )
    h = h.round({tag: 0, "diff": 0, "pct_diff": 2}).set_index(
        "harvest_restriction_class_rank"
    )
    d_columns.remove("land_designation_type_rank")
    h_columns.remove("harvest_restriction_class_rank")

    # dump results to csv
    d[d_columns + releases + [tag, "diff", "pct_diff"]].to_csv(
        "log_land_designations.csv"
    )
    h[h_columns + releases + [tag, "diff", "pct_diff"]].to_csv(
        "log_harvest_restrictions.csv"
    )
    LOG.info("log_land_designations.csv and log_harvest_restrictions.csv written")


if __name__ == "__main__":
    cli()
