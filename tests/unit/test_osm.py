"""Unit tests for osm.py — taxonomy, query construction, geometry, provenance."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

from wildfire_exposure_eo import osm as osm_mod
from wildfire_exposure_eo.schemas import OsmAsset, OsmAssetProvenance

# ── fixtures ───────────────────────────────────────────────────────────────────

_TAXONOMY_PATH = Path("data/taxonomy/critical_infrastructure.yaml")
_FIXTURES = Path("tests/fixtures/overpass")


def _make_klass(
    class_id: str,
    osm_filters: list[str],
    *,
    name: str = "Test class",
    buffer_radius_m: float = 30.0,
    criticality_weight: float = 0.5,
) -> osm_mod.InfrastructureClass:
    return osm_mod.InfrastructureClass(
        class_id=class_id,
        name=name,
        osm_filters=osm_filters,
        buffer_radius_m=buffer_radius_m,
        criticality_weight=criticality_weight,
    )


def _load_fixture(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((_FIXTURES / name).read_text())


# ── taxonomy round-trip ────────────────────────────────────────────────────────


def test_load_taxonomy_round_trip() -> None:
    taxonomy = osm_mod.load_taxonomy(_TAXONOMY_PATH)
    assert taxonomy.version == "0.1.0-draft"
    assert len(taxonomy.classes) == 13
    assert len(taxonomy.taxonomy_sha) == 64  # SHA-256 hex

    # SHA is stable across loads
    taxonomy2 = osm_mod.load_taxonomy(_TAXONOMY_PATH)
    assert taxonomy.taxonomy_sha == taxonomy2.taxonomy_sha


def test_load_taxonomy_class_ids() -> None:
    taxonomy = osm_mod.load_taxonomy(_TAXONOMY_PATH)
    ids = {klass.class_id for klass in taxonomy.classes}
    expected = {
        "power.transmission_line",
        "power.distribution_line",
        "power.substation",
        "power.transformer",
        "power.tower",
        "emergency.fire_station",
        "emergency.hospital",
        "emergency.police",
        "education.school",
        "telecom.tower",
        "water.treatment_plant",
        "water.reservoir",
        "transport.railway",
    }
    assert ids == expected


def test_load_taxonomy_class_fields() -> None:
    taxonomy = osm_mod.load_taxonomy(_TAXONOMY_PATH)
    tower = next(k for k in taxonomy.classes if k.class_id == "power.tower")
    assert tower.name == "Pylon / tower"
    assert tower.buffer_radius_m == 20
    assert tower.criticality_weight == 0.40
    assert len(tower.osm_filters) == 1
    assert 'node["power"="tower"]' in tower.osm_filters[0]


# ── query construction ─────────────────────────────────────────────────────────

_BBOX: tuple[float, float, float, float] = (-8.7, 40.6, -8.5, 40.7)


def test_build_query_node_only() -> None:
    klass = _make_klass("power.tower", ['node["power"="tower"]'])
    q = osm_mod.build_overpass_query(klass, _BBOX)
    assert "[out:json]" in q
    assert "[timeout:60]" in q
    # Overpass bbox order: south, west, north, east
    assert "(40.6,-8.7,40.7,-8.5)" in q
    assert 'node["power"="tower"]' in q
    assert "out body;" in q
    assert "out skel qt;" in q


def test_build_query_way_only() -> None:
    klass = _make_klass("transport.railway", ['way["railway"="rail"]'])
    q = osm_mod.build_overpass_query(klass, _BBOX)
    assert 'way["railway"="rail"]' in q
    assert "node" not in q.split("(")[0]  # no stray node prefix before union


def test_build_query_multi_filter() -> None:
    klass = _make_klass(
        "power.distribution_line",
        [
            'way["power"="line"]["voltage"!~"^([6-9][0-9]{4}|[1-9][0-9]{5,})$"]',
            'way["power"="minor_line"]',
        ],
    )
    q = osm_mod.build_overpass_query(klass, _BBOX)
    assert 'way["power"="line"]' in q
    assert 'way["power"="minor_line"]' in q
    # Both ways should appear in the union block
    assert q.count("way[") == 2


def test_build_query_relation_bearing() -> None:
    klass = _make_klass(
        "power.substation",
        [
            'node["power"="substation"]',
            'way["power"="substation"]',
            'relation["power"="substation"]',
        ],
    )
    q = osm_mod.build_overpass_query(klass, _BBOX)
    assert 'node["power"="substation"]' in q
    assert 'way["power"="substation"]' in q
    assert 'relation["power"="substation"]' in q


# ── geometry construction ──────────────────────────────────────────────────────


def test_geometrise_nodes_produce_points() -> None:
    elements = _load_fixture("nodes_power_tower.json")["elements"]
    klass = _make_klass("power.tower", ['node["power"="tower"]'])
    gdf = osm_mod.geometrise(elements, klass=klass)

    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    assert len(gdf) == 2
    for geom in gdf.geometry:
        assert isinstance(geom, Point)


def test_geometrise_open_way_produces_linestring() -> None:
    elements = _load_fixture("way_open_railway.json")["elements"]
    klass = _make_klass("transport.railway", ['way["railway"="rail"]'])
    gdf = osm_mod.geometrise(elements, klass=klass)

    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    assert len(gdf) == 1
    assert isinstance(gdf.geometry.iloc[0], LineString)


def test_geometrise_closed_way_area_class_produces_polygon() -> None:
    elements = _load_fixture("way_closed_hospital.json")["elements"]
    klass = _make_klass("emergency.hospital", ['way["amenity"="hospital"]'])
    gdf = osm_mod.geometrise(elements, klass=klass)

    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    assert len(gdf) == 1
    assert isinstance(gdf.geometry.iloc[0], Polygon)


def test_geometrise_closed_way_non_area_class_produces_linestring() -> None:
    """A closed way for a non-area class (e.g. transmission_line) → LineString."""
    elements = _load_fixture("way_closed_hospital.json")["elements"]
    klass = _make_klass("transport.railway", ['way["railway"="rail"]'])
    gdf = osm_mod.geometrise(elements, klass=klass)

    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    assert len(gdf) == 1
    assert isinstance(gdf.geometry.iloc[0], LineString)


def test_geometrise_relation_with_outer_produces_polygon() -> None:
    elements = _load_fixture("relation_substation.json")["elements"]
    klass = _make_klass(
        "power.substation",
        ['relation["power"="substation"]'],
    )
    gdf = osm_mod.geometrise(elements, klass=klass)

    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    # Relation produces one geometry; single outer ring → Polygon (not Multi)
    assert len(gdf) == 1
    assert isinstance(gdf.geometry.iloc[0], Polygon)


def test_geometrise_empty_elements_returns_empty_geodataframe() -> None:
    elements = _load_fixture("empty_result.json")["elements"]
    klass = _make_klass("power.tower", ['node["power"="tower"]'])
    gdf = osm_mod.geometrise(elements, klass=klass)

    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    assert len(gdf) == 0
    assert isinstance(gdf, gpd.GeoDataFrame)


# ── provenance population ──────────────────────────────────────────────────────


def _make_fake_overpass_response(elements: list[dict]) -> MagicMock:  # type: ignore[type-arg]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": "2026-06-01T00:00:00Z"},
        "elements": elements,
    }
    mock_resp.raise_for_status.return_value = None
    return mock_resp


def test_provenance_fully_populated(tmp_path: Path) -> None:
    """Stubbed-network end-to-end: every OsmAssetProvenance field is non-empty."""
    elements = _load_fixture("nodes_power_tower.json")["elements"]
    mock_resp = _make_fake_overpass_response(elements)

    with patch("wildfire_exposure_eo.osm.requests.post", return_value=mock_resp):
        result = osm_mod.query_overpass(
            "[out:json]; node; out;",
            endpoint="https://overpass-api.de/api/interpreter",
        )

    prov = OsmAssetProvenance(
        osm_snapshot_iso=result.osm_snapshot_iso,
        overpass_endpoint=result.endpoint_used,
        overpass_query_sha=result.query_sha,
        taxonomy_sha="a" * 64,
        taxonomy_version="0.1.0-draft",
        run_id="2026-06-01T000000Z-test",
        code_commit_sha="abc123",
        aoi_path="data/aoi/smoke.geojson",
        aoi_geometry_sha="b" * 64,
    )
    # All fields non-empty
    for field_name, value in prov.model_dump().items():
        assert value, f"Provenance field {field_name!r} is empty"

    # Validates cleanly
    OsmAssetProvenance.model_validate(prov.model_dump())


def test_provenance_snapshot_timestamp() -> None:
    """Snapshot timestamp comes from Overpass osm3s field, not local clock."""
    mock_resp = _make_fake_overpass_response([])
    with patch("wildfire_exposure_eo.osm.requests.post", return_value=mock_resp):
        result = osm_mod.query_overpass("[out:json]; node; out;")
    assert result.osm_snapshot_iso == datetime(2026, 6, 1, tzinfo=UTC)
    assert result.endpoint_used == osm_mod._DEFAULT_ENDPOINT


# ── empty-result handling ──────────────────────────────────────────────────────


def test_empty_class_does_not_raise(tmp_path: Path) -> None:
    """A class returning zero elements must not raise; produces zero rows."""
    mock_resp = _make_fake_overpass_response([])

    with patch("wildfire_exposure_eo.osm.requests.post", return_value=mock_resp):
        result = osm_mod.query_overpass("[out:json]; node; out;")

    klass = _make_klass("power.tower", ['node["power"="tower"]'])
    gdf = osm_mod.geometrise(result.elements, klass=klass)
    assert len(gdf) == 0


def test_query_overpass_retries_on_5xx() -> None:
    """5xx responses trigger retries; success on the third attempt is returned."""
    bad = MagicMock()
    bad.status_code = 503

    good_resp = _make_fake_overpass_response([])
    good_resp.status_code = 200

    with (
        patch(
            "wildfire_exposure_eo.osm.requests.post",
            side_effect=[bad, bad, good_resp],
        ),
        patch("wildfire_exposure_eo.osm.time.sleep"),
    ):
        result = osm_mod.query_overpass("[out:json]; node; out;", retries=2)
    assert isinstance(result, osm_mod.OverpassResult)


def test_query_overpass_falls_back_to_secondary() -> None:
    """After primary exhaustion, tries fallback endpoint and returns result."""
    bad = MagicMock()
    bad.status_code = 503

    good_resp = _make_fake_overpass_response([])
    good_resp.status_code = 200

    with (
        patch(
            "wildfire_exposure_eo.osm.requests.post",
            side_effect=[bad, bad, bad, good_resp],
        ),
        patch("wildfire_exposure_eo.osm.time.sleep"),
    ):
        result = osm_mod.query_overpass(
            "[out:json]; node; out;",
            retries=2,
            fallback_endpoint="https://overpass.kumi.systems/api/interpreter",
        )
    assert result.endpoint_used == "https://overpass.kumi.systems/api/interpreter"


# ── OsmAsset schema round-trip ─────────────────────────────────────────────────


def test_osm_asset_schema_round_trip() -> None:
    """A row from geometrise can be lifted to OsmAsset and validates cleanly."""
    elements = _load_fixture("nodes_power_tower.json")["elements"]
    klass = _make_klass("power.tower", ['node["power"="tower"]'])
    gdf = osm_mod.geometrise(elements, klass=klass)
    row = gdf.iloc[0]

    prov = OsmAssetProvenance(
        osm_snapshot_iso=datetime(2026, 6, 1, tzinfo=UTC),
        overpass_endpoint="https://overpass-api.de/api/interpreter",
        overpass_query_sha="a" * 64,
        taxonomy_sha="b" * 64,
        taxonomy_version="0.1.0-draft",
        run_id="2026-06-01T000000Z-test",
        code_commit_sha="abc123",
        aoi_path="data/aoi/smoke.geojson",
        aoi_geometry_sha="c" * 64,
    )
    asset = OsmAsset(
        asset_id=f"osm:node/{row.osm_id}",
        osm_type="node",
        osm_id=row.osm_id,
        asset_class="power.tower",
        geometry_wkb=row.geometry.wkb,
        centroid_lon=row.centroid_lon,
        centroid_lat=row.centroid_lat,
        tags=row.tags,
        provenance=prov,
    )
    OsmAsset.model_validate(asset.model_dump())
