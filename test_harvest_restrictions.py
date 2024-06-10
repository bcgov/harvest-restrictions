import os
from harvest_restrictions import download_bcgw, validate_sources

test_json = [
    {
        "name": "National Park",
        "alias": "park_national",
        "class_number": 1,
        "class_name": "Protected",
        "source": "WHSE_ADMIN_BOUNDARIES.CLAB_NATIONAL_PARKS",
        "source_type": "BCGW",
        "name_column": "ENGLISH_NAME",
        "query": None
    }
]


def test_download_bcgw():
    sources = validate_sources(test_json)
    download_bcgw(sources[0], ".")
    assert os.path.exists("r01_park_national.parquet")