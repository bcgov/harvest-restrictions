import os

import pytest

from harvest_restrictions import download_source, validate_sources, parse_sources


@pytest.fixture
def test_data():
    return [
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


def test_parse_sources(test_data):
    sources = parse_sources(test_data)
    assert sources[0]["index"] == 1


def test_parse_alias(test_data):
    source = test_data[0]
    source["alias"] = "National Réserve"
    sources = parse_sources([source])
    assert sources[0]["alias"] == "national_reserve"


def test_download_bcgw(test_data, tmpdir):
    sources = [s for s in parse_sources(test_data) if s["alias"] == "park_national"]
    sources = validate_sources(sources)
    download_source(sources[0], tmpdir)
    assert os.path.exists(os.path.join(tmpdir, "rr_01_park_national.parquet"))


def test_download_to_s3(test_data):
    sources = [s for s in parse_sources(test_data) if s["alias"] == "park_national"]
    sources = validate_sources(sources)
    download_source(
        sources[0],
        os.path.expandvars(
            "s3://$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/test"
        ),
    )
    # presume this succeeds if no error is raised


def test_invalid_bcgw(test_data):
    sources = [s for s in parse_sources(test_data) if s["alias"] == "park_national"]
    sources[0]["name_column"] = "INVALID_COLUMN"
    with pytest.raises(ValueError):
        sources = validate_sources(sources)


def test_download_file(test_data, tmpdir):
    sources = [
        s for s in parse_sources(test_data) if s["alias"] == "crd_water_supply_area"
    ]
    sources = validate_sources(sources)
    download_source(sources[0], tmpdir)
    assert os.path.exists(os.path.join(tmpdir, "rr_02_crd_water_supply_area.parquet"))


def test_invalid_file(test_data):
    sources = [
        s for s in parse_sources(test_data) if s["alias"] == "crd_water_supply_area"
    ]
    sources[0]["name_column"] = "INVALID_COLUMN"
    with pytest.raises(ValueError):
        sources = validate_sources(sources)
