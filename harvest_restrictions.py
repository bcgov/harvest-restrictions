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
DB = create_engine(os.environ.get("DATABASE_URL"))


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
    if source["primary_key"]:
        if source["primary_key"] not in columns:
            raise ValueError(
                f"Primary key - {source['primary_key']} is not present in source"
            )
    for column in source["field_mapper"].values():
        if column.lower() not in columns:
            raise ValueError(
                f"Column {column} is not present in source, modify config 'field_mapper'"
            )

    # is there data?
    alias = source["alias"]
    if len(df.index) == 0:
        raise ValueError(f"{alias} - no data returned for given source and query")

    # presume layer is defined correctly if no errors are raised
    LOG.info(f"{alias} - validates successfully")


def validate_bcgw(source):
    """validate bcdata sources against bcdc api and wfs"""
    # does source exist as written?
    alias = source["alias"]
    table = source["source"].upper()
    query = source["query"]
    if table not in bcdata.list_tables():
        raise ValueError(
            f"{alias} - {table} does not exist in BCGW or is not available via WFS"
        )

    # get columns present in source from data catalogue
    table_def = bcdata.get_table_definition(table)
    columns = [c["column_name"] for c in table_def["schema"]]

    # is primary key present?
    if source["primary_key"]:
        if source["primary_key"] not in columns:
            raise ValueError(
                f"Primary key - {source['primary_key']} is not present in source {table}"
            )

    # are other required columns in field mapping present?
    for column in source["field_mapper"].values():
        if (
            column
        ):  # allow null source columns (adds the new column, but with no values from source)
            if column.upper() not in columns:
                raise ValueError(
                    f"Column {column} is not present in source, modify config 'field_mapper'"
                )

    # does query return values?
    if source["query"]:
        if bcdata.get_count(table, query=source["query"]) == 0:
            raise ValueError(
                f"{alias} - provided query {query} returns no data for {table}"
            )

    # that is it for validation, presume layer is defined correctly if no errors are raised
    LOG.info(f"{alias} - layer validates successfully")


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

    LOG.info("Validation successful - all layers appear valid")

    # return validated (and indexed/dated) sources as ordered dictionary
    return sources


def download(source):
    """download layer from source to a standardized geodataframe"""

    # download WFS
    if source["source_type"] == "BCGW":
        df = bcdata.get_data(
            source["source"],
            crs="EPSG:3005",
            query=source["query"],
            as_gdf=True,
            lowercase=True,
        )

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
    if source["primary_key"]:
        df["__primary_key__"] = source["primary_key"]
    else:
        df["__primary_key__"] = ""

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


# def process(out_path):
#    """clean and overlay input data, dump to file if specified"""
#    DB.execute("create table designations_cleaned ")


@click.group()
def cli():
    pass


@cli.command()
@click.argument("alias", required=False)
@click.option(
    "--sources_file",
    "-s",
    type=click.Path(exists=True),
    required=False,
    default="sources.json",
)
@verbose_opt
@quiet_opt
def validate(alias, sources_file, verbose, quiet):
    """ensure sources json file is valid, and that data sources exist"""
    configure_logging((verbose - quiet))

    # load sources file
    with open(sources_file, "r") as f:
        sources = parse_sources(json.load(f))

    # if specified, use only one source
    if alias:
        sources = [s for s in sources if s["alias"] == alias]

    validate_sources(sources)


@cli.command()
@click.argument("alias", required=False)
@click.option(
    "--sources_file",
    "-s",
    type=click.Path(exists=True),
    default="sources.json",
    help="Path to configuration file listing data sources as json",
)
@click.option(
    "--out_path",
    "-o",
    type=click.Path(exists=True),
    default=None,
    help="Output path to cache data (local folder or object storage)",
)
@click.option(
    "--no_validate",
    "-nv",
    is_flag=True,
    help="Do not validate sources_file",
)
@verbose_opt
@quiet_opt
def setup(alias, sources_file, out_path, no_validate, verbose, quiet):
    configure_logging((verbose - quiet))

    # load sources file
    with open(sources_file, "r") as f:
        sources = parse_sources(json.load(f))

    # if specified, use only one source
    if alias:
        sources = [s for s in sources if s["alias"] == alias]

    # default to validating each data source, but provide option
    # to disable (just to speed testing)
    if not no_validate:
        sources = validate_sources(sources)

    # download each data source
    for source in sources:
        df = download(source)

        # load to postgres, writing everything to the same initial table
        LOG.info(f"Writing {source['alias']} to postgres")
        df.to_postgis("restrictions_source", DB, if_exists="append")

        # dump to file if out_path specified
        if out_path:
            out_file = os.path.join(
                out_path,
                (
                    "rr_"
                    + str(source["index"]).zfill(2)
                    + "_"
                    + source["alias"].lower()
                    + ".parquet"
                ),
            )
            LOG.info(f"Writing {alias} to {out_file}")
            df.to_parquet(out_file)

    # download additional supporting datasets
    if not alias:
        for table in [
            "WHSE_BASEMAPPING.BCGS_20K_GRID",
            "WHSE_WILDLIFE_MANAGEMENT.CRIMS_MARINE_ECOSECTION",
            "WHSE_LEGAL_ADMIN_BOUNDARIES.ABMS_PROVINCE_SP",
            "WHSE_BASEMAPPING.NTS_250K_GRID",
        ]:
            bcdata.bc2pg(table, os.environ["DATABASE_URL"])


if __name__ == "__main__":
    cli()
