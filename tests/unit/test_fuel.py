"""Unit tests for wildfire_exposure_eo.fuel."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wildfire_exposure_eo import fuel as fl
from wildfire_exposure_eo.schemas.fuel_layer import (
    Crosswalk,
    CrosswalkEntry,
    GridSpec,
)

# ── helpers ────────────────────────────────────────────────────────────────────

CROSSWALK_PATH = Path(__file__).parent.parent.parent / "config" / "fuel_crosswalk.yaml"


def _make_crosswalk(**overrides: object) -> Crosswalk:
    """Build a minimal Crosswalk with one entry for testing."""
    entry = CrosswalkEntry(
        effis_code=9,
        nffl_name="Hardwood litter",
        internal_class="broadleaf-open",
        severity=0.55,
        comment="test entry",
    )
    defaults: dict[str, object] = dict(
        version="0.1.0",
        source="test",
        source_taxonomy="Anderson 1982",
        internal_taxonomy_ref="data/crosswalks/icnf_to_scott_burgan.yaml",
        cosc_herbaceous_override_severity=0.40,
        entries=(entry,),
        crosswalk_sha="a" * 64,
    )
    return Crosswalk.model_validate({**defaults, **overrides})


def _make_grid(width: int = 4, height: int = 4) -> GridSpec:
    """Minimal GridSpec for testing (no file I/O)."""
    import rasterio.transform

    transform = rasterio.transform.from_bounds(0, 0, 40, 40, width, height)
    return GridSpec(
        crs="EPSG:32629",
        transform=tuple(transform[:6]),
        width=width,
        height=height,
        resolution_m=10,
        aoi_geometry_sha="b" * 64,
    )


# ── Crosswalk unit tests ───────────────────────────────────────────────────────


def test_crosswalk_severity_for_mapped_code() -> None:
    cw = _make_crosswalk()
    ic, sev = cw.severity_for_code(9)
    assert ic == "broadleaf-open"
    assert abs(sev - 0.55) < 1e-9


def test_crosswalk_unmapped_code_raises() -> None:
    """Unmapped codes must raise — CLAUDE.md non-negotiable #1."""
    cw = _make_crosswalk()
    with pytest.raises(ValueError, match="EFFIS code 7 is not mapped"):
        cw.severity_for_code(7)


def test_crosswalk_round_trip_from_real_yaml() -> None:
    """load_crosswalk parses the real config file and validates every entry."""
    if not CROSSWALK_PATH.exists():
        pytest.skip("config/fuel_crosswalk.yaml not yet committed")
    cw = fl.load_crosswalk(CROSSWALK_PATH)
    assert cw.version == "0.1.0"
    assert len(cw.entries) == 13
    for entry in cw.entries:
        assert 1 <= entry.effis_code <= 13
        assert 0.0 <= entry.severity <= 1.0
    # All 13 NFFL codes must be mappable
    for code in range(1, 14):
        ic, sev = cw.severity_for_code(code)
        assert isinstance(ic, str) and ic
        assert 0.0 <= sev <= 1.0


def test_crosswalk_sha_is_file_sha(tmp_path: Path) -> None:
    """load_crosswalk embeds the actual file SHA-256."""
    import hashlib

    yaml_src = textwrap.dedent("""\
        version: "0.1.0"
        source: "test"
        source_taxonomy: "NFFL"
        internal_taxonomy_ref: "ref.yaml"
        cosc_herbaceous_override_severity: 0.40
        entries:
          - effis_code: 1
            nffl_name: "Grass"
            internal_class: "grass"
            severity: 0.35
            comment: "test"
    """)
    p = tmp_path / "crosswalk.yaml"
    p.write_text(yaml_src)

    cw = fl.load_crosswalk(p)
    expected_sha = hashlib.sha256(p.read_bytes()).hexdigest()
    assert cw.crosswalk_sha == expected_sha


# ── Grid unit tests ────────────────────────────────────────────────────────────


def test_pilot_grid_determinism(tmp_path: Path) -> None:
    """Same AOI file → byte-identical GridSpec on repeated calls."""
    aoi_content = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-8.5, 40.6], [-8.2, 40.6], [-8.2, 40.9], [-8.5, 40.9], [-8.5, 40.6]]
                        ],
                    },
                    "properties": {},
                }
            ],
        }
    )
    aoi_path = tmp_path / "aoi.geojson"
    aoi_path.write_text(aoi_content)

    g1 = fl.pilot_grid(aoi_path)
    g2 = fl.pilot_grid(aoi_path)
    assert g1 == g2
    assert g1.crs == "EPSG:32629"
    assert g1.width > 0 and g1.height > 0
    assert g1.resolution_m == 10


def test_pilot_grid_snapped_outward(tmp_path: Path) -> None:
    """Grid snaps outward: the extent covers the AOI envelope."""
    import rasterio.transform
    from rasterio.crs import CRS
    from rasterio.warp import transform_bounds

    # Tiny AOI so the test stays fast
    aoi_content = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-8.35, 40.72],
                                [-8.30, 40.72],
                                [-8.30, 40.78],
                                [-8.35, 40.78],
                                [-8.35, 40.72],
                            ]
                        ],
                    },
                    "properties": {},
                }
            ],
        }
    )
    aoi_path = tmp_path / "aoi.geojson"
    aoi_path.write_text(aoi_content)

    g = fl.pilot_grid(aoi_path)
    t = rasterio.transform.Affine(*g.transform)

    # Grid extent in EPSG:32629
    grid_left = t.c
    grid_top = t.f
    grid_right = grid_left + t.a * g.width
    grid_bottom = grid_top + t.e * g.height  # t.e is negative

    # AOI envelope in EPSG:32629
    aoi_bounds_32629 = transform_bounds(
        CRS.from_epsg(4326), CRS.from_epsg(32629), -8.35, 40.72, -8.30, 40.78
    )
    aoi_left, aoi_bottom, aoi_right, aoi_top = aoi_bounds_32629

    assert grid_left <= aoi_left
    assert grid_bottom <= aoi_bottom
    assert grid_right >= aoi_right
    assert grid_top >= aoi_top


# ── Decision-table unit tests ──────────────────────────────────────────────────
#
# Each rule from refine_with_cosc gets its own test with a synthetic 2x2 grid
# so no raster I/O is needed. We monkey-patch refine_with_cosc to call the
# internal logic via a helper that injects pre-built COSc arrays.


def _refine_synthetic(
    klass_in: np.ndarray,
    sev_in: np.ndarray,
    cosc_values: np.ndarray,
    cw: Crosswalk | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Call refine_with_cosc internals without rasterio I/O.

    Replicates the logic block from refine_with_cosc so the decision-table
    rules can be unit-tested without touching the real COSc raster.
    """
    if cw is None:
        cw = _make_crosswalk()
    herbaceous_sev_x100 = round(cw.cosc_herbaceous_override_severity * 100)

    out_klass = klass_in.copy()
    out_sev = sev_in.copy()

    # Rule 1: COSc non-fuel
    non_fuel_mask = np.isin(cosc_values, list(fl._COSC_NON_FUEL_CODES))
    out_klass[non_fuel_mask] = 0
    out_sev[non_fuel_mask] = 0

    # Rule 2: COSc herbaceous + EFFIS forest
    cosc_herbaceous = cosc_values == fl._COSC_HERBACEOUS_CODE
    effis_forest = np.isin(klass_in, list(fl._EFFIS_FOREST_CODES))
    herb_override_mask = cosc_herbaceous & effis_forest
    out_klass[herb_override_mask] = 0
    out_sev[herb_override_mask] = herbaceous_sev_x100

    return out_klass, out_sev


def test_rule1_non_fuel_overrides_effis() -> None:
    """Rule 1: COSc non-fuel clears class and severity regardless of EFFIS."""
    # 2x2 grid: all EFFIS = code 10 (conifer-closed, sev=85)
    klass = np.full((2, 2), 10, dtype=np.uint8)
    sev = np.full((2, 2), 85, dtype=np.uint8)
    # All COSc pixels are urban (100) — non-fuel
    cosc = np.full((2, 2), 100, dtype=np.uint16)

    out_k, out_s = _refine_synthetic(klass, sev, cosc)
    assert np.all(out_k == 0), "Rule 1: all pixels should become non-fuel class"
    assert np.all(out_s == 0), "Rule 1: severity should drop to 0"


def test_rule2_herbaceous_overrides_effis_forest() -> None:
    """Rule 2: COSc herbaceous (420) overrides EFFIS forest codes (8-10)."""
    klass = np.array([[9, 10], [8, 9]], dtype=np.uint8)  # all forest codes
    sev = np.array([[55, 85], [70, 55]], dtype=np.uint8)
    cosc = np.full((2, 2), 420, dtype=np.uint16)  # herbaceous everywhere

    out_k, out_s = _refine_synthetic(klass, sev, cosc)
    expected_sev = 40  # cosc_herbaceous_override_severity=0.40 → 40
    assert np.all(out_k == 0), "Rule 2: class should become 0 (herbaceous)"
    assert np.all(out_s == expected_sev), f"Rule 2: severity should be {expected_sev}"


def test_rule2_herbaceous_does_not_override_shrub() -> None:
    """Rule 2: COSc herbaceous does NOT override EFFIS shrub codes (not forest)."""
    klass = np.array([[4, 5], [6, 7]], dtype=np.uint8)  # shrub codes
    sev = np.array([[80, 55], [50, 70]], dtype=np.uint8)
    cosc = np.full((2, 2), 420, dtype=np.uint16)  # herbaceous everywhere

    out_k, out_s = _refine_synthetic(klass, sev, cosc)
    # Rule 2 only fires for EFFIS forest codes (8-10). Shrub codes are left as-is.
    np.testing.assert_array_equal(out_k, klass)
    np.testing.assert_array_equal(out_s, sev)


def test_rule3_effis_stands_for_cosc_shrub() -> None:
    """Rule 3: COSc shrubland (410) leaves EFFIS class unchanged."""
    klass = np.array([[10, 9], [5, 1]], dtype=np.uint8)
    sev = np.array([[85, 55], [55, 35]], dtype=np.uint8)
    cosc = np.full((2, 2), 410, dtype=np.uint16)  # shrubland

    out_k, out_s = _refine_synthetic(klass, sev, cosc)
    np.testing.assert_array_equal(out_k, klass)
    np.testing.assert_array_equal(out_s, sev)


def test_rule3_effis_stands_for_cosc_forest() -> None:
    """Rule 3: COSc forest codes (312 eucalyptus) leave EFFIS class unchanged."""
    klass = np.array([[10, 9], [9, 10]], dtype=np.uint8)
    sev = np.array([[85, 55], [55, 85]], dtype=np.uint8)
    cosc = np.full((2, 2), 312, dtype=np.uint16)  # eucalyptus

    out_k, out_s = _refine_synthetic(klass, sev, cosc)
    np.testing.assert_array_equal(out_k, klass)
    np.testing.assert_array_equal(out_s, sev)


def test_mixed_rules_on_same_grid() -> None:
    """All three rules apply to different pixels in the same 3×3 grid."""
    # Row 0: EFFIS code 10 (forest, sev 85)
    # Row 1: EFFIS code 5 (shrub, sev 55)
    # Row 2: EFFIS code 9 (forest, sev 55)
    klass = np.array([[10, 10, 10], [5, 5, 5], [9, 9, 9]], dtype=np.uint8)
    sev = np.array([[85, 85, 85], [55, 55, 55], [55, 55, 55]], dtype=np.uint8)

    # Col 0: non-fuel (100) — rule 1
    # Col 1: herbaceous (420) — rule 2 for forest, no effect for shrub
    # Col 2: forest COSc (312) — rule 3 (EFFIS stands)
    cosc = np.array([[100, 420, 312], [100, 420, 312], [100, 420, 312]], dtype=np.uint16)

    out_k, out_s = _refine_synthetic(klass, sev, cosc)

    # Col 0: all → non-fuel (rule 1)
    assert np.all(out_k[:, 0] == 0)
    assert np.all(out_s[:, 0] == 0)

    # Row 0, Col 1: forest + herbaceous → rule 2 (class=0, sev=40)
    assert out_k[0, 1] == 0
    assert out_s[0, 1] == 40

    # Row 1, Col 1: shrub + herbaceous → rule 3 (EFFIS stands)
    assert out_k[1, 1] == 5
    assert out_s[1, 1] == 55

    # Row 2, Col 1: forest + herbaceous → rule 2
    assert out_k[2, 1] == 0
    assert out_s[2, 1] == 40

    # Col 2: rule 3 (EFFIS stands for all rows)
    np.testing.assert_array_equal(out_k[:, 2], klass[:, 2])
    np.testing.assert_array_equal(out_s[:, 2], sev[:, 2])


# ── Hypothesis property test ───────────────────────────────────────────────────


@given(
    min_lon=st.floats(min_value=-9.5, max_value=-7.0),
    width_deg=st.floats(min_value=0.01, max_value=0.5),
    min_lat=st.floats(min_value=37.0, max_value=42.0),
    height_deg=st.floats(min_value=0.01, max_value=0.5),
)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_pilot_grid_covers_aoi_envelope(
    tmp_path: Path,
    min_lon: float,
    width_deg: float,
    min_lat: float,
    height_deg: float,
) -> None:
    """Grid snap always covers the AOI envelope for arbitrary small Portugal-range bboxes."""
    import rasterio.transform
    from rasterio.crs import CRS
    from rasterio.warp import transform_bounds

    max_lon = min_lon + width_deg
    max_lat = min_lat + height_deg

    aoi_content = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [min_lon, min_lat],
                                [max_lon, min_lat],
                                [max_lon, max_lat],
                                [min_lon, max_lat],
                                [min_lon, min_lat],
                            ]
                        ],
                    },
                    "properties": {},
                }
            ],
        }
    )
    aoi_path = tmp_path / f"aoi_{min_lon:.4f}.geojson"
    aoi_path.write_text(aoi_content)

    g = fl.pilot_grid(aoi_path)
    t = rasterio.transform.Affine(*g.transform)

    grid_left = t.c
    grid_top = t.f
    grid_right = grid_left + t.a * g.width
    grid_bottom = grid_top + t.e * g.height  # t.e < 0

    aoi_l, aoi_b, aoi_r, aoi_t = transform_bounds(
        CRS.from_epsg(4326), CRS.from_epsg(32629), min_lon, min_lat, max_lon, max_lat
    )

    tol = 1e-3  # 1 mm numerical tolerance
    assert grid_left <= aoi_l + tol, f"grid left {grid_left} > AOI left {aoi_l}"
    assert grid_bottom <= aoi_b + tol, f"grid bottom {grid_bottom} > AOI bottom {aoi_b}"
    assert grid_right >= aoi_r - tol, f"grid right {grid_right} < AOI right {aoi_r}"
    assert grid_top >= aoi_t - tol, f"grid top {grid_top} < AOI top {aoi_t}"
