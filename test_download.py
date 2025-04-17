import os

import pytest

from download import download_source, validate_sources, parse_sources


@pytest.fixture
def test_data():
    return [
        {
            "alias": "park_national",
            "description": "National Park",
            "source_type": "BCGW",
            "source": "WHSE_ADMIN_BOUNDARIES.CLAB_NATIONAL_PARKS",
            "query": None,
            "primary_key": None,
            "field_mapper": {"name": "ENGLISH_NAME"},
            "data": {
                "harvest_restriction": 1,
                "og_restriction": 0,
                "mining_restriction": 0,
            },
        },
        {
            "alias": "crd_water_supply_area",
            "description": "CRD Water Supply Area",
            "source_type": "FILE",
            "source": "/vsizip//vsis3/$BUCKET/dss_projects_2024/harvest_restrictions/sources/CRD.gdb.zip",
            "layer": "WSA_Boundary",
            "query": None,
            "primary_key": None,
            "field_mapper": {"name": "Name"},
            "data": {
                "harvest_restriction": 3,
                "og_restriction": 0,
                "mining_restriction": 0,
            },
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


def test_download_bcgw(test_data):
    sources = [s for s in parse_sources(test_data) if s["alias"] == "park_national"]
    sources = validate_sources(sources)
    df = download_source(sources[0])
    assert len(df) > 0


def test_invalid_bcgw(test_data):
    sources = [s for s in parse_sources(test_data) if s["alias"] == "park_national"]
    sources[0]["field_mapper"] = {"name": "INVALID_COLUMN"}
    with pytest.raises(ValueError):
        sources = validate_sources(sources)


def test_download_file(test_data, tmpdir):
    sources = [
        s for s in parse_sources(test_data) if s["alias"] == "crd_water_supply_area"
    ]
    sources = validate_sources(sources)
    df = download_source(sources[0])
    assert len(df) > 0


def test_invalid_file(test_data):
    sources = [
        s for s in parse_sources(test_data) if s["alias"] == "crd_water_supply_area"
    ]
    sources[0]["field_mapper"] = {"name": "INVALID_COLUMN"}
    with pytest.raises(ValueError):
        sources = validate_sources(sources)
