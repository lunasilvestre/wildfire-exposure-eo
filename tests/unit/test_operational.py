"""Unit tests for the operational refresh watch-list join (WU-26).

Covers the transparent two-axis triage: FWI normalisation, the watch-priority
product, the per-asset nearest-cell FWI sampling, the known-answer ordering
(structural rank crossed with current fire weather), the graceful-failure /
never-impute path, and the honest-framing string guards (#6 / #9).
"""

from __future__ import annotations

import numpy as np
import pytest
import rioxarray  # noqa: F401  (registers the .rio accessor used on DataArrays)
import xarray as xr

from wildfire_exposure_eo import operational as op


def _fwi_surface(west: float, east: float) -> xr.DataArray:
    """A 2x2 EPSG:4326 FWI surface: ``west`` in the west column, ``east`` in the east."""
    lat = np.array([40.8, 40.6], dtype="float64")
    lon = np.array([-8.6, -8.3], dtype="float64")
    vals = np.array([[west, east], [west, east]], dtype="float32")
    da = xr.DataArray(vals, dims=("y", "x"), coords={"y": lat, "x": lon})
    return da.rio.write_crs("EPSG:4326")


def _assets():  # type: ignore[no-untyped-def]
    """Two assets: W is structurally top (0.9) in the calm west; E is mid (0.5) in the hot east."""
    import geopandas as gpd
    from shapely.geometry import Point

    return gpd.GeoDataFrame(
        {
            "asset_id": ["W", "E"],
            "osm_type": ["node", "node"],
            "osm_id": [1, 2],
            "asset_class": ["power.tower", "power.substation"],
            "criticality_weight": [0.4, 0.9],
            "exposure_score": [0.9, 0.5],
            "exposure_rank": [1, 2],
            "geometry": [Point(-8.59, 40.7), Point(-8.31, 40.7)],
        },
        crs="EPSG:4326",
    )


# ---------------------------------------------------------------------------
# FWI normalisation + watch-priority product
# ---------------------------------------------------------------------------
def test_normalize_fwi_clips_and_saturates() -> None:
    assert op.normalize_fwi(0.0) == 0.0
    assert op.normalize_fwi(25.0) == pytest.approx(0.5)  # ref 50 -> 25/50
    assert op.normalize_fwi(50.0) == 1.0  # at the extreme boundary
    assert op.normalize_fwi(80.0) == 1.0  # saturates above the reference
    assert op.normalize_fwi(-5.0) == 0.0  # defensive floor


def test_normalize_fwi_none_and_nonfinite_pass_through_as_none() -> None:
    assert op.normalize_fwi(None) is None
    assert op.normalize_fwi(float("nan")) is None
    assert op.normalize_fwi(float("inf")) is None


def test_normalize_fwi_rejects_nonpositive_ref() -> None:
    with pytest.raises(ValueError, match="fwi_ref must be positive"):
        op.normalize_fwi(10.0, ref=0.0)


def test_compute_watch_priority_is_product_of_axes() -> None:
    # exposure_score * clip(fwi/50, 0, 1)
    fwi_norm, prio = op.compute_watch_priority(0.5, 60.0)
    assert fwi_norm == 1.0
    assert prio == pytest.approx(0.5)
    fwi_norm, prio = op.compute_watch_priority(0.9, 10.0)
    assert fwi_norm == pytest.approx(0.2)
    assert prio == pytest.approx(0.18)


def test_compute_watch_priority_missing_fwi_returns_none_never_imputed() -> None:
    assert op.compute_watch_priority(0.9, None) == (None, None)
    assert op.compute_watch_priority(0.9, float("nan")) == (None, None)


# ---------------------------------------------------------------------------
# Per-asset nearest-cell sampling
# ---------------------------------------------------------------------------
def test_sample_fwi_at_points_nearest_cell_known_answer() -> None:
    surface = _fwi_surface(west=10.0, east=60.0)
    series = op.sample_fwi_at_points(surface, _assets())
    assert series["W"] == pytest.approx(10.0)
    assert series["E"] == pytest.approx(60.0)


def test_sample_fwi_at_points_rejects_non_4326_surface() -> None:
    surface = _fwi_surface(10.0, 60.0).rio.reproject("EPSG:3857")
    with pytest.raises(ValueError, match="EPSG:4326"):
        op.sample_fwi_at_points(surface, _assets())


def test_sample_fwi_at_points_reprojects_assets_to_4326() -> None:
    surface = _fwi_surface(west=10.0, east=60.0)
    assets = _assets().to_crs("EPSG:32629")  # metric CRS; must be reprojected once
    series = op.sample_fwi_at_points(surface, assets)
    assert series["W"] == pytest.approx(10.0)
    assert series["E"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Known-answer watch-list ordering: the two axes trade off as designed
# ---------------------------------------------------------------------------
def test_build_watch_list_ordering_crosses_both_axes() -> None:
    # West: structural 0.9 but calm FWI 10 (norm 0.2) -> priority 0.18.
    # East: structural 0.5 but extreme FWI 60 (norm 1.0) -> priority 0.50.
    # E must outrank W even though W is the structurally-top asset.
    surface = _fwi_surface(west=10.0, east=60.0)
    series = op.sample_fwi_at_points(surface, _assets())
    df = op.build_watch_list(_assets(), series, ref=op.FWI_REF)
    assert list(df["asset_id"]) == ["E", "W"]
    e = df[df["asset_id"] == "E"].iloc[0]
    w = df[df["asset_id"] == "W"].iloc[0]
    assert e["watch_priority"] == pytest.approx(0.5)
    assert w["watch_priority"] == pytest.approx(0.18)
    # Calm-everywhere: structural order is preserved (FWI factor identical).
    calm = op.sample_fwi_at_points(_fwi_surface(5.0, 5.0), _assets())
    df_calm = op.build_watch_list(_assets(), calm, ref=op.FWI_REF)
    assert list(df_calm["asset_id"]) == ["W", "E"]


def test_build_watch_list_uncovered_fwi_sinks_and_is_not_imputed() -> None:
    import pandas as pd

    series = pd.Series({"W": 60.0, "E": np.nan}, name="fwi_current")
    df = op.build_watch_list(_assets(), series, ref=op.FWI_REF)
    # E has no FWI -> watch_priority None, sorted last; W is ranked.
    assert list(df["asset_id"]) == ["W", "E"]
    e = df[df["asset_id"] == "E"].iloc[0]
    assert e["fwi_current"] is None
    assert e["fwi_norm"] is None
    assert e["watch_priority"] is None  # never imputed


def test_build_watch_list_rows_validate_against_schema() -> None:
    from wildfire_exposure_eo.schemas import WatchListItem

    surface = _fwi_surface(west=10.0, east=60.0)
    series = op.sample_fwi_at_points(surface, _assets())
    df = op.build_watch_list(_assets(), series, ref=op.FWI_REF)
    for record in df.to_dict(orient="records"):
        WatchListItem.model_validate(record)


def test_build_watch_list_rejects_non_4326_assets() -> None:
    import pandas as pd

    series = pd.Series({"W": 10.0, "E": 60.0}, name="fwi_current")
    bad = _assets().to_crs("EPSG:32629")
    with pytest.raises(ValueError, match="expected EPSG:4326"):
        op.build_watch_list(bad, series, ref=op.FWI_REF)


# ---------------------------------------------------------------------------
# Honest framing (#6 / #9): the brief and the constants must not overclaim
# ---------------------------------------------------------------------------
def test_markdown_brief_states_triage_not_forecast() -> None:
    surface = _fwi_surface(west=10.0, east=60.0)
    series = op.sample_fwi_at_points(surface, _assets())
    df = op.build_watch_list(_assets(), series, ref=op.FWI_REF)
    md = op.watch_list_markdown(df, top_n=5, run_id="20260611T000000Z", fwi_valid_date="2026-06-11")
    low = md.lower()
    assert "operational triage" in low
    assert "not a forecast" in low
    assert "observed" in low
    # The probability/forecast/ignition words appear ONLY inside an explicit
    # negation ("not a probability ...", "not a prediction of ignition").
    assert "not a probability of fire" in low
    assert "not a prediction of ignition" in low
    # Must NOT overclaim production / operational validation (#9).
    for forbidden in ("production-ready", "operationally validated", "utility-grade"):
        assert forbidden not in low


def test_formula_and_rationale_constants_are_honest() -> None:
    low_formula = op.WATCH_PRIORITY_FORMULA.lower()
    assert "watch_priority = exposure_score * fwi_norm" in low_formula
    assert "not a forecast" in low_formula or "operational triage" in low_formula
    assert op.FWI_REF == 50.0
    # The reference is cited to EFFIS, not invented (#1).
    assert "effis" in op.FWI_REF_RATIONALE.lower()
    assert "50" in op.FWI_REF_RATIONALE
