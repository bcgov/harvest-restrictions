import json
import logging
import os
from pathlib import Path
import sys

import bcdata
import click
from cligj import verbose_opt, quiet_opt
from datetime import datetime
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
from sqlalchemy import create_engine

LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"
LOG = logging.getLogger(__name__)


def configure_logging(verbosity):
    log_level = max(10, 30 - 10 * verbosity)
    logging.basicConfig(stream=sys.stderr, level=log_level, format=LOG_FORMAT)


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
        if (
            column
        ):  # allow null source columns (adds the new column, but with no values from source)
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


if __name__ == "__main__":
    cli()
