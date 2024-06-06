import json

import bcdata
from jsonschema import validate
from datetime import datetime

from shapely.geometry.linestring import LineString
from shapely.geometry.multilinestring import MultiLineString
from shapely.geometry.multipoint import MultiPoint
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon


def replace_date_placeholder(sources):
    """replace date placeholder with today's date"""
    dated = sources
    for i, source in enumerate(sources):
        if "{current_date}" in source["query"]:
            dated[i]["query"] = dated[i]["query"].replace(
                "{current_date}", datetime.today().strftime("%Y-%m-%d")
            )
    return dated


def validate_sources_json(sources):
    # is json valid according to schema?
    with open("source.schema.json", "r") as f:
        schema = json.load(f)
    validate(instance=sources, schema=schema)


def validate_bcgw(sources):
    """validate bcdata sources agains bcdc api and wfs"""
    for layer in [s for s in sources if s["source_type"] == "BCGW"]:

        # does source exist as written?
        table = layer["source"].upper()
        print(table)
        if table not in bcdata.list_tables():
            raise ValueError(
                f"{table} does not exist in BCGW or is not available via WFS"
            )

        # does specified name_column exist in source?
        if "name_column" in layer.keys():
            column = layer["name_column"].upper()
            table_def = bcdata.get_table_definition(table)
            if column not in [c["column_name"] for c in table_def["schema"]]:
                raise ValueError(f"{column} is not present in BCGW table {table}")

        # does query return values?
        if layer["query"]:
            if bcdata.get_count(table, query=layer["query"]) == 0:
                raise ValueError("Provided query {query} returns no data for {table}")

# read list of source data
with open("sources.json", "r") as f:
    sources = json.load(f)

# replace_date_placeholder(sources)
validate_sources_json(sources)
#validate_bcgw(sources)

"""
# download all bcgw sources to /data as parquet
for i, layer in enumerate([s for s in sources if s["source_type"] == "BCGW"]):
    # load all features to geopandas dataframe
    df = bcdata.get_data(
        layer["source"],
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
        # log.info(f"Writing data to {out_file}")
        df.to_parquet("r"+str(i+1).zfill(2) + layer["alias"] + ".parquet")

    # else:
    #    log.info("No data returned, parquet file not created")
"""