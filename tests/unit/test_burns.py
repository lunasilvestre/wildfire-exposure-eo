"""Unit tests for burns.py — layer discovery, pagination, CRS, provenance, ordering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from wildfire_exposure_eo import burns as burns_mod
from wildfire_exposure_eo.schemas import BurnPerimeter, BurnPerimeterProvenance, IcnfLayerDescriptor

_FIXTURES = Path("tests/fixtures/icnf")


# ── helpers ────────────────────────────────────────────────────────────────────


def _mock_response(body: dict | list | str, *, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    if isinstance(body, dict | list):
        resp.json = MagicMock(return_value=body)
    else:
        resp.json = MagicMock(return_value=json.loads(body))
    return resp


def _make_descriptor(year: int, layer_id: int = 3, name: str = "") -> IcnfLayerDescriptor:
    return IcnfLayerDescriptor(
        layer_id=layer_id,
        year=year,
        name=name or f"Áreas Ardidas {year}",
        feature_count_total=10,
    )


def _make_provenance(year: int = 2020) -> BurnPerimeterProvenance:
    return BurnPerimeterProvenance(
        icnf_layer_id=3,
        icnf_layer_name=f"Áreas Ardidas {year}",
        vintage_year=year,
        mapserver_url=burns_mod.ICNF_MAPSERVER_URL,
        fetched_at_utc=datetime(2026, 6, 1, tzinfo=UTC),
        run_id="test-run",
        code_commit_sha="abc123",
        aoi_path="data/aoi/smoke.geojson",
        aoi_geometry_sha="d" * 64,
    )


# ── year parsing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Áreas Ardidas 2020", 2020),
        ("Áreas Ardidas 1975-1989", 1975),
        ("Áreas Ardidas 2000-2008", 2000),
        ("areas_ardidas_2009", 2009),
        ("Incêndios 2017", 2017),
        ("Areas Ardidas 1990-1999", 1990),
    ],
)
def test_parse_layer_year(name: str, expected: int) -> None:
    assert burns_mod._parse_layer_year(name) == expected


def test_parse_layer_year_no_year() -> None:
    assert burns_mod._parse_layer_year("Service Layer") is None
    assert burns_mod._parse_layer_year("") is None


# ── layer discovery ────────────────────────────────────────────────────────────


def test_discover_icnf_layers_year_span() -> None:
    """discover_icnf_layers returns descriptors covering 1975 and 2024; each validates."""
    index_data = json.loads((_FIXTURES / "mapserver_index.json").read_text())

    def _fake_get(url: str, **kwargs: object) -> MagicMock:
        if "query" in url:
            # count-only requests
            return _mock_response({"count": 42})
        return _mock_response(index_data)

    with patch("wildfire_exposure_eo.burns._get_with_retry", side_effect=_fake_get):
        descriptors = burns_mod.discover_icnf_layers()

    years = [d.year for d in descriptors]
    assert 1975 in years, f"Expected 1975 in years; got {years}"
    assert 2024 in years, f"Expected 2024 in years; got {years}"
    assert years == sorted(years), "Descriptors must be sorted by year ascending"
    # Every descriptor Pydantic-validates
    for d in descriptors:
        IcnfLayerDescriptor.model_validate(d.model_dump())


def test_discover_icnf_layers_skips_unparseable_names() -> None:
    """Layers whose names contain no year are silently skipped (never invent)."""
    index_data = {
        "layers": [
            {"id": 0, "name": "Áreas Ardidas 2020"},
            {"id": 99, "name": "Service Group"},
        ]
    }

    def _fake_get(url: str, **kwargs: object) -> MagicMock:
        if "query" in url:
            return _mock_response({"count": 5})
        return _mock_response(index_data)

    with patch("wildfire_exposure_eo.burns._get_with_retry", side_effect=_fake_get):
        descriptors = burns_mod.discover_icnf_layers()

    assert len(descriptors) == 1
    assert descriptors[0].year == 2020


def test_discover_icnf_layers_feature_count() -> None:
    """feature_count_total comes from the count-only query, not invented."""
    index_data = {"layers": [{"id": 3, "name": "Áreas Ardidas 2017"}]}

    def _fake_get(url: str, **kwargs: object) -> MagicMock:
        if "query" in url:
            return _mock_response({"count": 1234})
        return _mock_response(index_data)

    with patch("wildfire_exposure_eo.burns._get_with_retry", side_effect=_fake_get):
        descriptors = burns_mod.discover_icnf_layers()

    assert descriptors[0].feature_count_total == 1234


# ── pagination ─────────────────────────────────────────────────────────────────


def _make_feature(object_id: int, year: int, *, area_ha: float = 10.0) -> dict:
    """Build a minimal ArcGIS REST feature with a 100m×100m square in EPSG:3763."""
    x, y = -45000 + object_id * 200, 190000
    return {
        "attributes": {"OBJECTID": object_id, "ANO": year, "AREA_HA": area_ha},
        "geometry": {"rings": [[[x, y], [x + 100, y], [x + 100, y + 100], [x, y + 100], [x, y]]]},
    }


def test_fetch_icnf_layer_pagination() -> None:
    """fetch_icnf_layer loops on resultOffset until exceededTransferLimit=false."""
    layer = _make_descriptor(2017, layer_id=3)
    aoi = Polygon([(-8.5, 39.5), (-7.5, 39.5), (-7.5, 40.5), (-8.5, 40.5)])

    page1_features = [_make_feature(i, 2017) for i in range(1000)]
    page2_features = [_make_feature(1000 + i, 2017) for i in range(1000)]
    page3_features = [_make_feature(2000 + i, 2017) for i in range(500)]

    page_responses = [
        _mock_response({"features": page1_features, "exceededTransferLimit": True}),
        _mock_response({"features": page2_features, "exceededTransferLimit": True}),
        _mock_response({"features": page3_features, "exceededTransferLimit": False}),
    ]
    call_count = 0

    def _fake_get(url: str, **kwargs: object) -> MagicMock:
        nonlocal call_count
        resp = page_responses[call_count]
        call_count += 1
        return resp

    with patch("wildfire_exposure_eo.burns._get_with_retry", side_effect=_fake_get):
        gdf = burns_mod.fetch_icnf_layer(layer, aoi)

    assert len(gdf) == 2500, f"Expected 2500 rows, got {len(gdf)}"
    assert call_count == 3, f"Expected 3 GET calls, got {call_count}"


def test_fetch_icnf_layer_single_page() -> None:
    """fetch_icnf_layer handles a single page (exceededTransferLimit=false on first call)."""
    layer = _make_descriptor(2020, layer_id=0)
    aoi = Polygon([(-8.5, 39.5), (-7.5, 39.5), (-7.5, 40.5), (-8.5, 40.5)])

    fixture = json.loads((_FIXTURES / "layer_query_2020.json").read_text())

    with patch(
        "wildfire_exposure_eo.burns._get_with_retry",
        return_value=_mock_response(fixture),
    ):
        gdf = burns_mod.fetch_icnf_layer(layer, aoi)

    assert len(gdf) == 2
    assert gdf.crs is not None
    assert gdf.crs.to_epsg() == 4326


# ── CRS reprojection ───────────────────────────────────────────────────────────


def test_fetch_icnf_layer_crs_reprojection_area() -> None:
    """A fixture feature in EPSG:3763 reprojects to EPSG:4326 with area within 1% of expected.

    The fixture polygon is a 1000 m × 1000 m square in EPSG:3763 → area_ha = 100.0 (exact).
    After round-trip through EPSG:4326 and back to 3763 for area computation, the
    geopandas area must be within 1% of 100 ha.
    """
    layer = _make_descriptor(2017, layer_id=3)
    aoi = Polygon([(-8.5, 39.5), (-7.5, 39.5), (-7.5, 40.5), (-8.5, 40.5)])

    # 1000 m x 1000 m square at (-45000, 192000) in EPSG:3763 -> exactly 100 ha
    feature = {
        "attributes": {"OBJECTID": 999, "ANO": 2017, "AREA_HA": 100.0},
        "geometry": {
            "rings": [
                [
                    [-45000, 192000],
                    [-44000, 192000],
                    [-44000, 193000],
                    [-45000, 193000],
                    [-45000, 192000],
                ]
            ]
        },
    }
    payload = {"features": [feature], "exceededTransferLimit": False}

    with patch(
        "wildfire_exposure_eo.burns._get_with_retry",
        return_value=_mock_response(payload),
    ):
        gdf = burns_mod.fetch_icnf_layer(layer, aoi)

    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326

    # Reproject back to EPSG:3763 to compute area; must be within 1% of 100 ha
    area_m2 = float(gdf.to_crs("EPSG:3763").geometry.iloc[0].area)
    area_ha = area_m2 / 10_000
    assert (
        abs(area_ha - 100.0) / 100.0 < 0.01
    ), f"Area {area_ha:.4f} ha deviates >1% from expected 100 ha after CRS round-trip"


# ── empty AOI ──────────────────────────────────────────────────────────────────


def test_fetch_icnf_layer_empty_aoi() -> None:
    """Zero features for an AOI produces an empty GeoDataFrame — no exception."""
    layer = _make_descriptor(2020)
    aoi = Polygon([(-8.5, 39.5), (-7.5, 39.5), (-7.5, 40.5), (-8.5, 40.5)])

    with patch(
        "wildfire_exposure_eo.burns._get_with_retry",
        return_value=_mock_response({"features": [], "exceededTransferLimit": False}),
    ):
        gdf = burns_mod.fetch_icnf_layer(layer, aoi)

    assert gdf.empty
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326


# ── provenance ─────────────────────────────────────────────────────────────────


def test_burn_perimeter_provenance_all_fields() -> None:
    """BurnPerimeterProvenance validates with all required fields populated."""
    prov = _make_provenance()
    BurnPerimeterProvenance.model_validate(prov.model_dump())
    assert prov.license == "ICNF open data, attribution required"
    assert "ICNF" in prov.attribution


def test_burn_perimeter_provenance_vintage_matches_layer() -> None:
    """vintage_year on the provenance matches the layer year."""
    prov = _make_provenance(year=2017)
    assert prov.vintage_year == 2017
    assert prov.icnf_layer_name == "Áreas Ardidas 2017"


def test_burn_perimeter_schema_validates() -> None:
    """BurnPerimeter.model_validate succeeds for a synthetic record."""
    from shapely.geometry import Point

    pt = Point(-8.0, 40.0)
    prov = _make_provenance()
    BurnPerimeter.model_validate(
        {
            "row_id": "icnf:2020:201",
            "vintage_year": 2020,
            "icnf_feature_id": 201,
            "geometry_wkb": pt.wkb,
            "area_ha": 89.5,
            "provenance": prov,
        }
    )


# ── combine_burns ordering ─────────────────────────────────────────────────────


def test_combine_burns_year_ordering() -> None:
    """combined frame is sorted (year asc, area_ha desc, feature_id asc)."""

    def _df(year: int, feature_ids: list[int], areas: list[float]) -> gpd.GeoDataFrame:
        rows = [
            {
                "feature_id": fid,
                "vintage_year": year,
                "area_ha": area,
                "geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            }
            for fid, area in zip(feature_ids, areas, strict=True)
        ]
        return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")

    per_year = {
        2020: _df(2020, [201, 202], [45.0, 89.5]),
        2017: _df(2017, [101, 102, 103], [312.1, 1204.7, 65853.4]),
    }
    combined = burns_mod.combine_burns(per_year)

    assert len(combined) == 5
    years = combined["vintage_year"].tolist()
    assert years[0] == 2017 and years[-1] == 2020, f"Not year-sorted: {years}"

    # Within year 2017: area_ha desc
    y2017 = combined[combined["vintage_year"] == 2017]
    areas_2017 = y2017["area_ha"].tolist()
    assert areas_2017 == sorted(areas_2017, reverse=True), f"Not area-sorted desc: {areas_2017}"

    # row_id canonical format
    assert combined["row_id"].iloc[0].startswith("icnf:2017:")


def test_combine_burns_empty_inputs() -> None:
    """combine_burns on an empty dict returns an empty GeoDataFrame."""
    result = burns_mod.combine_burns({})
    assert result.empty
    assert result.crs is not None and result.crs.to_epsg() == 4326
