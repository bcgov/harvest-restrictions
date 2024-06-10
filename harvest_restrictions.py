import json
import logging
import os
from pathlib import Path
import sys

import bcdata
import click
from cligj import verbose_opt, quiet_opt
from datetime import datetime
import jsonschema

from shapely.geometry.linestring import LineString
from shapely.geometry.multilinestring import MultiLineString
from shapely.geometry.multipoint import MultiPoint
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon


LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"
LOG = logging.getLogger(__name__)


def configure_logging(verbosity):
    log_level = max(10, 30 - 10 * verbosity)
    logging.basicConfig(stream=sys.stderr, level=log_level, format=LOG_FORMAT)


def validate_sources_json(sources):
    # is json valid according to schema?
    with open("source.schema.json", "r") as f:
        schema = json.load(f)
    jsonschema.validate(instance=sources, schema=schema)
    LOG.info("Source json is valid")


def validate_bcgw(layer):
    """validate bcdata sources agains bcdc api and wfs"""
    # does source exist as written?
    name = layer["name"]
    table = layer["source"].upper()
    if table not in bcdata.list_tables():
        raise ValueError(f"{table} does not exist in BCGW or is not available via WFS")

    # does specified name_column exist in source?
    if layer["name_column"]:
        column = layer["name_column"].upper()
        table_def = bcdata.get_table_definition(table)
        if column not in [c["column_name"] for c in table_def["schema"]]:
            raise ValueError(f"{column} is not present in BCGW table {table}")

    # does query return values?
    if layer["query"]:
        if bcdata.get_count(table, query=layer["query"]) == 0:
            raise ValueError("Provided query {query} returns no data for {table}")

    # that is it for validation, presume layer is defined correctly if no errors are raised
    LOG.info(f"Layer {name} validates successfully")


def replace_date_placeholder(sources):
    """replace date placeholder with today's date"""
    dated = sources
    for i, source in enumerate(sources):
        if source["query"] and "{current_date}" in source["query"]:
            dated[i]["query"] = dated[i]["query"].replace(
                "{current_date}", datetime.today().strftime("%Y-%m-%d")
            )
    return dated


def download_bcgw(layer, out_path="data"):
    """download layer from bcgw source and save to parquet"""
    # load all features to geopandas dataframe
    table = layer["source"]
    name = layer["name"]
    df = bcdata.get_data(
        table,
        crs="EPSG:3005",
        query=layer["query"],
        as_gdf=True,
        lowercase=True,
    )

    # only operate on dataframe if there is data
    if len(df.index != 0):
        # tidy the dataframe
        df = df.rename_geometry("geom")

        # add new columns
        df["restriction"] = layer["name"]
        df["alias"] = layer["alias"].lower()
        df["class_number"] = layer["class_number"]
        df["class_name"] = layer["class_name"]
        if layer["name_column"]:
            df["restriction_name"] = df[layer["name_column"].lower()]
        else:
            df["restriction_name"] = ""

        # retain only columns of interest
        columns = [
            "restriction",
            "alias",
            "class_number",
            "class_name",
            "restriction_name",
        ]
        df = df[columns + ["geom"]]

        # because responses can have mixed singlepart/multipart types, just cast to everything to multipart
        # https://gis.stackexchange.com/questions/311320/casting-geometry-to-multi-using-geopandas
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

        # dump to file
        out_file = (
            "r"
            + str(layer["index"]).zfill(2)
            + "_"
            + layer["alias"].lower()
            + ".parquet"
        )
        LOG.info(f"Writing {name} to {out_path}")
        df.to_parquet(os.path.join(out_path, out_file))
    else:
        LOG.warning(
            f"No data returned for {name}, parquet file not created. Check layer definition in sources.json"
        )


def validate_sources(sources):
    """
    Validate json, whether data sources exist, and assign hierarchy index
    based on position in list
    """

    # validate the sources json against the json schema
    validate_sources_json(sources)

    # add (base 1) hierarchy index based on position in list
    sources = [dict(d, index=index + 1) for (index, d) in enumerate(sources)]

    # if today's date is required in any source query, add it
    replace_date_placeholder(sources)

    # validate WFS/BCGW layers
    for layer in [s for s in sources if s["source_type"] == "BCGW"]:
        validate_bcgw(layer)

    # todo validate file based layers

    LOG.info("Definitions of all layers appear valid")

    # return validated/indexed/dated json object
    return sources


@click.group()
def cli():
    pass


@cli.command()
@click.argument(
    "sources_file", type=click.Path(exists=True), required=False, default="sources.json"
)
@verbose_opt
@quiet_opt
def validate(sources_file, verbose, quiet):
    """ensure sources json file is valid, and that data sources exist"""
    configure_logging((verbose - quiet))
    with open(sources_file, "r") as f:
        validate_sources(json.load(f))


@cli.command()
@click.argument(
    "sources_file", type=click.Path(exists=True), required=False, default="sources.json"
)
@click.option(
    "--out_path",
    "-o",
    default="data",
    help="Output path (local folder or object storage)",
)
@verbose_opt
@quiet_opt
def download(sources_file, out_path, verbose, quiet):
    configure_logging((verbose - quiet))
    with open(sources_file, "r") as f:
        sources = json.load(f)
    sources = validate_sources(sources)

    # create default output location if it does not exist
    if out_path == "data":
        Path(out_path).mkdir(parents=True, exist_ok=True)

    # download BCGW layers
    for layer in [s for s in sources if s["source_type"] == "BCGW"]:
        download_bcgw(layer, out_path)


if __name__ == "__main__":
    cli()
