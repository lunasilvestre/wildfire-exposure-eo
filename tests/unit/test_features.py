"""Unit tests for per-asset feature functions (WU-6)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, box

from wildfire_exposure_eo import features as feat

# ---------------------------------------------------------------------------
# Date windows
# ---------------------------------------------------------------------------


def test_date_range_rejects_inverted() -> None:
    with pytest.raises(ValueError, match="after end"):
        feat.DateRange(date(2026, 6, 1), date(2026, 1, 1))


def test_date_range_overlaps() -> None:
    a = feat.DateRange(date(2025, 1, 1), date(2025, 12, 31))
    assert a.overlaps(feat.DateRange(date(2025, 6, 1), date(2026, 6, 1)))
    assert not a.overlaps(feat.DateRange(date(2026, 1, 1), date(2026, 6, 1)))


def test_twelve_month_window() -> None:
    w = feat.twelve_month_window(date(2026, 6, 11))
    assert w.start == date(2025, 6, 11)
    assert w.end == date(2026, 6, 11)


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        (date(2026, 6, 11), 2025),  # before late-summer → previous year
        (date(2026, 9, 30), 2026),  # exactly the boundary → this year
        (date(2024, 12, 31), 2024),
    ],
)
def test_recent_season_year(end: date, expected: int) -> None:
    assert feat.recent_season_year(end) == expected


def test_season_windows() -> None:
    assert feat.spring_window(2025) == feat.DateRange(date(2025, 3, 1), date(2025, 5, 31))
    assert feat.summer_window(2025) == feat.DateRange(date(2025, 8, 1), date(2025, 9, 30))


# ---------------------------------------------------------------------------
# Horn slope
# ---------------------------------------------------------------------------


def test_horn_slope_planar_ramp() -> None:
    # z = 0.5 * x (metres); cell 30 m → constant slope atan(0.5) = 26.565°.
    z = np.tile(np.arange(8) * 15.0, (8, 1))
    slope = feat.horn_slope(z, 30.0, 30.0)
    assert slope[3, 3] == pytest.approx(26.565, abs=1e-2)


def test_horn_slope_flat_is_zero() -> None:
    z = np.full((6, 6), 100.0)
    assert np.allclose(feat.horn_slope(z, 30.0, 30.0), 0.0)


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------

_TAXONOMY = {
    "classes": {
        "education.school": {"buffer_radius_m": 75, "criticality_weight": 0.85},
        "power.tower": {"buffer_radius_m": 20, "criticality_weight": 0.40},
    }
}


def _assets() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "asset_id": ["osm:node/2", "osm:node/1"],
            "asset_class": ["power.tower", "education.school"],
            "osm_type": ["node", "node"],
            "osm_id": [2, 1],
            "geometry": [Point(-8.20, 39.95), Point(-8.21, 39.96)],
        },
        crs="EPSG:4326",
    )


def test_buffer_assets_crs_radius_and_order() -> None:
    buffers = feat.buffer_assets(_assets(), _TAXONOMY)
    assert buffers.crs is not None
    assert buffers.crs.to_epsg() == 32629
    # Deterministic order: (asset_class, osm_type, osm_id) → school then tower.
    assert list(buffers["asset_id"]) == ["osm:node/1", "osm:node/2"]
    areas = dict(zip(buffers["asset_id"], buffers.geometry.area, strict=True))
    # Slightly under π·r²: shapely approximates the disc with quad_segs=8.
    assert areas["osm:node/1"] == pytest.approx(np.pi * 75**2, rel=5e-3)
    assert areas["osm:node/2"] == pytest.approx(np.pi * 20**2, rel=5e-3)


def test_buffer_assets_unknown_class_raises() -> None:
    bad = _assets()
    bad.loc[0, "asset_class"] = "telecom.tower"
    with pytest.raises(ValueError, match="not in taxonomy"):
        feat.buffer_assets(bad, _TAXONOMY)


# ---------------------------------------------------------------------------
# historical_burn_share (vector overlay)
# ---------------------------------------------------------------------------


def test_historical_burn_share_partial_overlap_and_vintage_filter() -> None:
    # Two 100x100 m buffers in EPSG:32629.
    a = box(560000, 4420000, 560100, 4420100)  # half covered by an eligible burn
    b = box(561000, 4420000, 561100, 4420100)  # untouched
    buffers = gpd.GeoDataFrame({"asset_id": ["A", "B"], "geometry": [a, b]}, crs="EPSG:32629")
    burns_32629 = gpd.GeoDataFrame(
        {
            "vintage_year": [2018, 2099],
            "geometry": [
                box(560000, 4420000, 560050, 4420100),  # left half of A, eligible
                box(561000, 4420000, 561100, 4420100),  # covers B but vintage in the future
            ],
        },
        crs="EPSG:32629",
    )
    burns = burns_32629.to_crs("EPSG:4326")
    window = feat.DateRange(date(2010, 1, 1), date(2020, 12, 31))
    share = feat.historical_burn_share(buffers, burns, window)
    assert share["A"] == pytest.approx(0.5, rel=1e-3)
    assert share["B"] == pytest.approx(0.0, abs=1e-6)  # future vintage filtered out


def test_historical_burn_share_no_eligible_burns_is_zero() -> None:
    buffers = gpd.GeoDataFrame(
        {"asset_id": ["A"], "geometry": [box(560000, 4420000, 560100, 4420100)]},
        crs="EPSG:32629",
    )
    burns = gpd.GeoDataFrame(
        {"vintage_year": [2099], "geometry": [box(560000, 4420000, 560100, 4420100)]},
        crs="EPSG:32629",
    ).to_crs("EPSG:4326")
    window = feat.DateRange(date(2010, 1, 1), date(2020, 12, 31))
    share = feat.historical_burn_share(buffers, burns, window)
    assert share["A"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# recent_burn_share_12mo backdating rule + sidecar parsing
# ---------------------------------------------------------------------------


def test_recent_burn_share_out_of_window_returns_none() -> None:
    buffers = gpd.GeoDataFrame(
        {"asset_id": ["A"], "geometry": [box(560000, 4420000, 560100, 4420100)]},
        crs="EPSG:32629",
    )
    cog_window = feat.DateRange(date(2025, 6, 9), date(2026, 6, 9))
    # Backdated request: trailing 12 months end 2024-12-31 → no overlap → None.
    window = feat.DateRange(date(2024, 1, 1), date(2024, 12, 31))
    result = feat.recent_burn_share_12mo(buffers, Path("does-not-exist.tif"), cog_window, window)
    assert result is None


def test_recent_burn_share_future_overhang_returns_none() -> None:
    # COG window overlaps the requested window but extends past its end: scenes
    # observed after window-end would leak into a backdated run → feature absent.
    buffers = gpd.GeoDataFrame(
        {"asset_id": ["A"], "geometry": [box(560000, 4420000, 560100, 4420100)]},
        crs="EPSG:32629",
    )
    cog_window = feat.DateRange(date(2025, 6, 9), date(2026, 6, 9))
    window = feat.DateRange(date(2024, 8, 1), date(2025, 8, 1))  # overlaps, end < cog end
    result = feat.recent_burn_share_12mo(buffers, Path("does-not-exist.tif"), cog_window, window)
    assert result is None


def test_burn_scar_window_reads_sidecar(tmp_path: Path) -> None:
    cog = tmp_path / "burn_scar_x.tif"
    cog.with_suffix(".json").write_text(
        json.dumps(
            {
                "window_start": "2025-06-09",
                "window_end": "2026-06-09",
                "binarisation_threshold": 0.5,
            }
        )
    )
    window, threshold = feat.burn_scar_window(cog)
    assert window == feat.DateRange(date(2025, 6, 9), date(2026, 6, 9))
    assert threshold == pytest.approx(0.5)
