import json

import bcdata
from jsonschema import validate
from datetime import datetime


def validate_sources(sources):

    # is json valid according to schema?
    with open("source.schema.json", "r") as f:
        schema = json.load(f)
    validate(instance=sources, schema=schema)

    # validate bcgw sources
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
        if "query" in layer.keys():
            query = layer["query"]
            if bcdata.get_count(table, query=query) == 0:
                raise ValueError("Provided query {query} returns no data for {table}")

    # consider evaluating file sources as well but the reader should throw appropriate errors?


# read list of source data
with open("sources.json", "r") as f:
    sources = json.load(f)

# replace date placeholder with today's date (not needed for current sources)
# sources_dated = sources
# for i, source in enumerate(sources):
#    if "query" in source.keys() and "{current_date}" in source["query"]:
#        sources_dated[i]["query"] = sources_dated[i]["query"].replace("{current_date}", datetime.today().strftime("%Y-%m-%d"))
# sources = sources_dated

# validate
validate_sources(sources)
