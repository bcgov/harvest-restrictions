import os
from harvest_restrictions import download_source, validate_sources, parse_sources

test_json = [
    {
        "name": "National Park",
        "alias": "park_national",
        "class_number": 1,
        "source": "WHSE_ADMIN_BOUNDARIES.CLAB_NATIONAL_PARKS",
        "source_type": "BCGW",
        "primary_key": None,
        "name_column": "ENGLISH_NAME",
        "query": None,
    }
]


def test_download_bcgw():
    sources = parse_sources(test_json)
    sources = validate_sources(sources)
    download_source(sources[0], ".")
    assert os.path.exists("rr_01_park_national.parquet")
