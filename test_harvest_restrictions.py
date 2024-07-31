import os

import pytest

from harvest_restrictions import download_source, validate_sources, parse_sources


SOURCES = [
    {
        "name": "National Park",
        "alias": "park_national",
        "class_number": 1,
        "source": "WHSE_ADMIN_BOUNDARIES.CLAB_NATIONAL_PARKS",
        "source_type": "BCGW",
        "primary_key": None,
        "name_column": "ENGLISH_NAME",
        "query": None,
    },
    {
        "name": "CRD Water Supply Area",
        "alias": "crd_water_supply_area",
        "class_number": 3,
        "source": "/vsizip//vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/CRD.gdb.zip",
        "source_type": "FILE",
        "layer": "WSA_Boundary",
        "primary_key": None,
        "name_column": "Name",
        "query": None,
    },
]


def test_download_bcgw(tmpdir):
    sources = [s for s in parse_sources(SOURCES) if s["alias"] == "park_national"]
    sources = validate_sources(sources)
    download_source(sources[0], tmpdir)
    assert os.path.exists(os.path.join(tmpdir, "rr_01_park_national.parquet"))


def test_invalid_bcgw():
    sources = [s for s in parse_sources(SOURCES) if s["alias"] == "park_national"]
    sources[0]["name_column"] = "INVALID_COLUMN"
    with pytest.raises(ValueError):
        sources = validate_sources(sources)


def test_download_file(tmpdir):
    sources = [
        s for s in parse_sources(SOURCES) if s["alias"] == "crd_water_supply_area"
    ]
    sources = validate_sources(sources)
    download_source(sources[0], tmpdir)
    assert os.path.exists(os.path.join(tmpdir, "rr_02_crd_water_supply_area.parquet"))


def test_invalid_file():
    sources = [
        s for s in parse_sources(SOURCES) if s["alias"] == "crd_water_supply_area"
    ]
    sources[0]["name_column"] = "INVALID_COLUMN"
    with pytest.raises(ValueError):
        sources = validate_sources(sources)
