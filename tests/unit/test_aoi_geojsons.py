"""Unit tests for WU-18 Phase A — validation AOI geojson files.

Checks that each new validation AOI geojson:
- loads as valid GeoJSON with a single Polygon feature,
- declares CRS explicitly as CRS84,
- has a valid bbox (west < east, south < north, within mainland-PT bounds),
- is larger than the frozen pilot AOI (in square degrees),
- has the required metadata properties.

The pilot.geojson is also loaded to establish the reference size; it must NOT
be modified by any of these tests (read-only fixture).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_AOI_DIR = Path("data/aoi")
_PILOT = _AOI_DIR / "pilot.geojson"

_VALIDATION_AOIS = [
    "pedrogao_grande",
    "serra_da_estrela",
    "peneda_geres",
    "monchique",
]

_CRS84_NAME = "urn:ogc:def:crs:OGC:1.3:CRS84"

# Bounding box for continental Portugal (generous margins)
_PT_WEST, _PT_SOUTH, _PT_EAST, _PT_NORTH = -9.6, 36.8, -6.1, 42.2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_geojson(path: Path) -> dict:  # type: ignore[type-arg]
    with path.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _bbox_km2(coords: list) -> float:
    """Approximate area in km² using a mid-latitude flat-Earth approximation."""
    lons = [c[0] for c in coords[0]]
    lats = [c[1] for c in coords[0]]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    lat_mid = (south + north) / 2
    deg_lon_km = 111.32 * math.cos(math.radians(lat_mid))
    deg_lat_km = 111.32
    return (east - west) * deg_lon_km * (north - south) * deg_lat_km


# ---------------------------------------------------------------------------
# Reference: frozen pilot AOI size
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pilot_km2() -> float:
    data = _load_geojson(_PILOT)
    coords = data["features"][0]["geometry"]["coordinates"]
    return _bbox_km2(coords)


# ---------------------------------------------------------------------------
# Parametric tests over all validation AOIs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _VALIDATION_AOIS)
class TestValidationAOI:
    """Each validation AOI must pass all Phase-A structural gates."""

    def _data(self, slug: str) -> dict:  # type: ignore[type-arg]
        path = _AOI_DIR / f"{slug}.geojson"
        assert path.exists(), f"Missing AOI file: {path}"
        return _load_geojson(path)

    def test_loads_as_feature_collection(self, slug: str) -> None:
        data = self._data(slug)
        assert data["type"] == "FeatureCollection", (
            f"{slug}: top-level type must be FeatureCollection"
        )
        assert "features" in data and len(data["features"]) == 1, (
            f"{slug}: must have exactly one feature"
        )

    def test_crs_explicit_and_crs84(self, slug: str) -> None:
        data = self._data(slug)
        assert "crs" in data, f"{slug}: missing top-level 'crs' key"
        crs = data["crs"]
        assert crs.get("type") == "name", f"{slug}: crs.type must be 'name'"
        props = crs.get("properties", {})
        assert props.get("name") == _CRS84_NAME, (
            f"{slug}: crs.properties.name must be '{_CRS84_NAME}'"
        )

    def test_geometry_is_polygon(self, slug: str) -> None:
        data = self._data(slug)
        geom = data["features"][0]["geometry"]
        assert geom["type"] == "Polygon", f"{slug}: geometry must be Polygon"
        coords = geom["coordinates"]
        assert len(coords) == 1, f"{slug}: Polygon must have exactly one ring"
        ring = coords[0]
        assert len(ring) >= 4, f"{slug}: ring must have at least 4 points"
        # Closed ring: first == last
        assert ring[0] == ring[-1], f"{slug}: ring must be closed (first == last point)"

    def test_bbox_within_mainland_portugal(self, slug: str) -> None:
        data = self._data(slug)
        coords = data["features"][0]["geometry"]["coordinates"]
        lons = [c[0] for c in coords[0]]
        lats = [c[1] for c in coords[0]]
        assert min(lons) >= _PT_WEST, f"{slug}: western bound too far west"
        assert max(lons) <= _PT_EAST, f"{slug}: eastern bound too far east"
        assert min(lats) >= _PT_SOUTH, f"{slug}: southern bound too far south"
        assert max(lats) <= _PT_NORTH, f"{slug}: northern bound too far north"

    def test_bbox_valid_orientation(self, slug: str) -> None:
        data = self._data(slug)
        coords = data["features"][0]["geometry"]["coordinates"]
        lons = [c[0] for c in coords[0]]
        lats = [c[1] for c in coords[0]]
        assert min(lons) < max(lons), f"{slug}: west must be < east"
        assert min(lats) < max(lats), f"{slug}: south must be < north"

    def test_larger_than_pilot(self, slug: str, pilot_km2: float) -> None:
        data = self._data(slug)
        coords = data["features"][0]["geometry"]["coordinates"]
        area_km2 = _bbox_km2(coords)
        assert area_km2 > pilot_km2, (
            f"{slug}: AOI area {area_km2:.0f} km² must be larger than pilot {pilot_km2:.0f} km²"
        )

    def test_required_properties_present(self, slug: str) -> None:
        data = self._data(slug)
        props = data["features"][0]["properties"]
        required = [
            "name",
            "iso3166_2",
            "bbox_wgs84",
            "anchor_fire_year",
            "anchor_fire_ha_approx",
            "anchor_fire_source",
        ]
        for key in required:
            assert key in props, f"{slug}: missing required property '{key}'"

    def test_bbox_wgs84_matches_geometry(self, slug: str) -> None:
        data = self._data(slug)
        props = data["features"][0]["properties"]
        coords = data["features"][0]["geometry"]["coordinates"]
        lons = [c[0] for c in coords[0]]
        lats = [c[1] for c in coords[0]]
        bbox = props["bbox_wgs84"]
        assert len(bbox) == 4, f"{slug}: bbox_wgs84 must have 4 elements [W, S, E, N]"
        west, south, east, north = bbox
        # Allow small floating-point tolerance
        tol = 1e-6
        assert abs(west - min(lons)) < tol, (
            f"{slug}: bbox_wgs84[W]={west} != geometry west={min(lons)}"
        )
        assert abs(south - min(lats)) < tol, (
            f"{slug}: bbox_wgs84[S]={south} != geometry south={min(lats)}"
        )
        assert abs(east - max(lons)) < tol, (
            f"{slug}: bbox_wgs84[E]={east} != geometry east={max(lons)}"
        )
        assert abs(north - max(lats)) < tol, (
            f"{slug}: bbox_wgs84[N]={north} != geometry north={max(lats)}"
        )

    def test_anchor_fire_source_cites_icnf_or_effis(self, slug: str) -> None:
        data = self._data(slug)
        props = data["features"][0]["properties"]
        source = props.get("anchor_fire_source", "")
        assert "ICNF" in source or "EFFIS" in source, (
            f"{slug}: anchor_fire_source must cite ICNF or EFFIS explicitly; got: {source!r}"
        )

    def test_smoke_tile_exists(self, slug: str) -> None:
        smoke_path = _AOI_DIR / f"smoke_{slug}.geojson"
        assert smoke_path.exists(), (
            f"Missing smoke tile: {smoke_path}. Each validation AOI must have a "
            f"matching smoke_<slug>.geojson."
        )

    def test_smoke_tile_crs_explicit(self, slug: str) -> None:
        smoke_path = _AOI_DIR / f"smoke_{slug}.geojson"
        if not smoke_path.exists():
            pytest.skip(f"smoke tile not found: {smoke_path}")
        data = _load_geojson(smoke_path)
        assert "crs" in data, f"smoke_{slug}: missing 'crs'"
        props = data["crs"].get("properties", {})
        assert props.get("name") == _CRS84_NAME, f"smoke_{slug}: crs must be CRS84"

    def test_smoke_tile_inside_aoi(self, slug: str) -> None:
        smoke_path = _AOI_DIR / f"smoke_{slug}.geojson"
        if not smoke_path.exists():
            pytest.skip(f"smoke tile not found: {smoke_path}")
        aoi_data = self._data(slug)
        smoke_data = _load_geojson(smoke_path)
        aoi_coords = aoi_data["features"][0]["geometry"]["coordinates"]
        smoke_coords = smoke_data["features"][0]["geometry"]["coordinates"]
        aoi_lons = [c[0] for c in aoi_coords[0]]
        aoi_lats = [c[1] for c in aoi_coords[0]]
        smoke_lons = [c[0] for c in smoke_coords[0]]
        smoke_lats = [c[1] for c in smoke_coords[0]]
        # Smoke tile must be entirely inside the AOI
        assert min(smoke_lons) >= min(aoi_lons) - 1e-6, f"smoke_{slug}: tile extends west of AOI"
        assert max(smoke_lons) <= max(aoi_lons) + 1e-6, f"smoke_{slug}: tile extends east of AOI"
        assert min(smoke_lats) >= min(aoi_lats) - 1e-6, f"smoke_{slug}: tile extends south of AOI"
        assert max(smoke_lats) <= max(aoi_lats) + 1e-6, f"smoke_{slug}: tile extends north of AOI"


# ---------------------------------------------------------------------------
# Pilot is unchanged (regression)
# ---------------------------------------------------------------------------


def test_pilot_geojson_untouched() -> None:
    """Pilot bbox must remain exactly as frozen."""
    data = _load_geojson(_PILOT)
    coords = data["features"][0]["geometry"]["coordinates"]
    lons = [c[0] for c in coords[0]]
    lats = [c[1] for c in coords[0]]
    assert round(min(lons), 6) == pytest.approx(-8.598, abs=1e-6)
    assert round(max(lons), 6) == pytest.approx(-8.242, abs=1e-6)
    assert round(min(lats), 6) == pytest.approx(40.605, abs=1e-6)
    assert round(max(lats), 6) == pytest.approx(40.875, abs=1e-6)
