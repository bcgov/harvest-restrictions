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

    # if today's date is required in any source query, add it
    replace_date_placeholder(sources)

    LOG.info("Source json is valid")
    return sources


def validate_file(layer):
    """simple validation of file based sources
    - file exists
    - schema is as expected
    """
    source = layer["source"]
    alias = layer["alias"]
    query = layer["query"]

    # load file
    df = geopandas.read_file(
        os.path.expandvars(source), layer=layer["layer"], where=query
    )

    # are expected columns present?
    df.columns = [x.lower() for x in df.columns]
    for col in ["primary_key", "name_column"]:
        if layer[col]:
            column = layer[col].lower()
            if column not in df.columns:
                raise ValueError(f"{alias} - {column} is not present in source")

    # is there data?
    if len(df.index) == 0:
        raise ValueError(f"{alias} - no data returned for given source and query")

    # presume layer is defined correctly if no errors are raised
    LOG.info(f"{alias} - validates successfully")


def validate_bcgw(layer):
    """validate bcdata sources against bcdc api and wfs"""
    # does source exist as written?
    alias = layer["alias"]
    table = layer["source"].upper()
    query = layer["query"]
    if table not in bcdata.list_tables():
        raise ValueError(
            f"{alias} - {table} does not exist in BCGW or is not available via WFS"
        )

    # do specified name/pk columns exist in source?
    table_def = bcdata.get_table_definition(table)
    for col in ["primary_key", "name_column"]:
        if layer[col]:
            column = layer[col].upper()
            if column not in [c["column_name"] for c in table_def["schema"]]:
                raise ValueError(
                    f"{alias} - {column} is not present in BCGW table {table}"
                )

    # does query return values?
    if layer["query"]:
        if bcdata.get_count(table, query=layer["query"]) == 0:
            raise ValueError(
                f"{alias} - provided query {query} returns no data for {table}"
            )

    # that is it for validation, presume layer is defined correctly if no errors are raised
    LOG.info(f"{alias} - layer validates successfully")


def replace_date_placeholder(sources):
    """replace date placeholder with today's date"""
    dated = sources
    for i, source in enumerate(sources):
        if source["query"] and "{current_date}" in source["query"]:
            dated[i]["query"] = dated[i]["query"].replace(
                "{current_date}", datetime.today().strftime("%Y-%m-%d")
            )
    return dated


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
    for layer in sources:
        if layer["source_type"] == "BCGW":
            validate_bcgw(layer)
        elif layer["source_type"] == "FILE":
            validate_file(layer)

    LOG.info("Validation successful - all layers appear valid")

    # return validated (and indexed/dated) sources as ordered dictionary
    return sources


def download_source(layer, out_path="data"):
    """download layer from source and save to parquet in out_path"""

    table, alias = (layer["source"], layer["alias"])

    # download WFS
    if layer["source_type"] == "BCGW":
        df = bcdata.get_data(
            table,
            crs="EPSG:3005",
            query=layer["query"],
            as_gdf=True,
            lowercase=True,
        )

    # download file
    elif layer["source_type"] == "FILE":
        df = geopandas.read_file(
            os.path.expandvars(layer["source"]), layer=layer["layer"]
        )
        if df.crs != CRS.from_user_input(3005):
            df = df.to_crs("EPSG:3005")
        # lowercasify column names
        df.columns = [x.lower() for x in df.columns]

    # tidy the dataframe
    df = df.rename_geometry("geom")
    df = to_multipart(df)  # sources can have mixed types, just make everything multi

    # add new columns
    df["rr_restriction"] = layer["name"]
    df["rr_alias"] = layer["alias"].lower()
    df["rr_class_number"] = layer["class_number"]
    # load pk/name if present, otherwise set to empty string
    if layer["primary_key"]:
        df["rr_source_primary_key"] = layer["primary_key"]
    else:
        df["rr_source_primary_key"] = ""
    if layer["name_column"]:
        df["rr_restriction_name"] = df[layer["name_column"].lower()]
    else:
        df["rr_restriction_name"] = ""

    # retain only columns of interest
    df = df[
        [
            "rr_restriction",
            "rr_alias",
            "rr_class_number",
            "rr_source_primary_key",
            "rr_restriction_name",
            "geom",
        ]
    ]

    # dump to file
    out_file = (
        "rr_" + str(layer["index"]).zfill(2) + "_" + layer["alias"].lower() + ".parquet"
    )
    LOG.info(f"Writing {alias} to {out_path}")
    df.to_parquet(os.path.join(out_path, out_file))


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
    required=False,
    default="sources.json",
)
@click.option(
    "--out_path",
    "-o",
    default="data",
    help="Output path (local folder or object storage)",
)
@click.option(
    "--no_validate",
    "-nv",
    is_flag=True,
    help="Do not validate sources_file",
)
@verbose_opt
@quiet_opt
def download(alias, sources_file, out_path, no_validate, verbose, quiet):
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

    # create default output location if it does not exist
    # (any other path provided is presumed to already exist,
    # job will fail if it does not)
    if out_path == "data":
        Path(out_path).mkdir(parents=True, exist_ok=True)

    # download each data source as individual .parquet file
    for layer in sources:
        download_source(layer, out_path)


if __name__ == "__main__":
    cli()
