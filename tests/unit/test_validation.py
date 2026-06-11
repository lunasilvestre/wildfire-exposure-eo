"""Unit + property tests for the WU-7 validation primitives (prompt 11)."""

from __future__ import annotations

from datetime import date

import geopandas as gpd
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from shapely.geometry import Point, box

from wildfire_exposure_eo.features import DateRange
from wildfire_exposure_eo.validation import (
    assert_no_temporal_leakage,
    asset_burn_labels,
    lift_table,
    spearman_rank,
)


def _burns(years: list[int]) -> gpd.GeoDataFrame:
    """A minimal burns layer carrying only ``vintage_year`` (geometry unused here)."""
    return gpd.GeoDataFrame(
        {"vintage_year": years},
        geometry=[Point(0, 0) for _ in years],
        crs="EPSG:4326",
    )


# ---------------------------------------------------------------------------
# assert_no_temporal_leakage — the §12 hard rule
# ---------------------------------------------------------------------------


def test_leakage_passes_when_all_burns_after_window() -> None:
    window = DateRange(date(2024, 1, 1), date(2024, 12, 31))
    assert_no_temporal_leakage(window, _burns([2025, 2025, 2026]))  # no raise


def test_leakage_raises_on_in_window_burn() -> None:
    window = DateRange(date(2024, 1, 1), date(2024, 12, 31))
    with pytest.raises(ValueError, match="temporal leakage"):
        assert_no_temporal_leakage(window, _burns([2024, 2025]))


def test_leakage_raises_on_pre_window_burn() -> None:
    window = DateRange(date(2024, 1, 1), date(2024, 12, 31))
    with pytest.raises(ValueError, match="temporal leakage"):
        assert_no_temporal_leakage(window, _burns([2010, 2025]))


def test_leakage_empty_validation_set_passes_vacuously() -> None:
    window = DateRange(date(2024, 1, 1), date(2024, 12, 31))
    assert_no_temporal_leakage(window, _burns([]))  # nothing can leak


def test_leakage_missing_column_raises() -> None:
    window = DateRange(date(2024, 1, 1), date(2024, 12, 31))
    bad = gpd.GeoDataFrame({"x": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    with pytest.raises(ValueError, match="vintage_year"):
        assert_no_temporal_leakage(window, bad)


@given(
    end_year=st.integers(min_value=1990, max_value=2030),
    burn_years=st.lists(st.integers(min_value=1980, max_value=2040), min_size=1, max_size=12),
)
def test_property_leakage_fires_iff_any_burn_in_or_before_window(
    end_year: int, burn_years: list[int]
) -> None:
    # The assertion must fire exactly when some validation burn year is <= the
    # score-window end year (methodology §12). Random window/year combinations.
    window = DateRange(date(end_year, 1, 1), date(end_year, 12, 31))
    should_raise = min(burn_years) <= end_year
    if should_raise:
        with pytest.raises(ValueError):
            assert_no_temporal_leakage(window, _burns(burn_years))
    else:
        assert_no_temporal_leakage(window, _burns(burn_years))


# ---------------------------------------------------------------------------
# asset_burn_labels — buffered-asset overlay against selected-vintage burns
# ---------------------------------------------------------------------------


def test_asset_burn_labels_overlay_and_year_filter() -> None:
    # Buffered assets in the metric CRS (EPSG:32629), as produced by buffer_assets.
    assets = gpd.GeoDataFrame(
        {"asset_id": ["a0", "a1", "a2"]},
        geometry=[box(0, 0, 10, 10), box(100, 100, 110, 110), box(1000, 1000, 1010, 1010)],
        crs="EPSG:32629",
    )
    burns = gpd.GeoDataFrame(
        {"vintage_year": [2025, 2010]},
        # 2025 burn overlaps a0; 2010 burn overlaps a1 but is the wrong vintage.
        geometry=[box(5, 5, 15, 15), box(105, 105, 115, 115)],
        crs="EPSG:32629",
    )
    labels = asset_burn_labels(assets, burns, years=[2025])
    assert labels.loc["a0"]  # overlaps the 2025 burn
    assert not labels.loc["a1"]  # only overlaps a 2010 burn, excluded by year filter
    assert not labels.loc["a2"]  # overlaps nothing
    assert labels.dtype == bool
    assert list(labels.index) == ["a0", "a1", "a2"]


def test_asset_burn_labels_no_burns_in_years_all_false() -> None:
    assets = gpd.GeoDataFrame({"asset_id": ["a0"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:32629")
    burns = gpd.GeoDataFrame(
        {"vintage_year": [2010]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:32629"
    )
    labels = asset_burn_labels(assets, burns, years=[2025])
    assert not labels.any()


def test_asset_burn_labels_reprojects_4326_burns() -> None:
    # Burns supplied in EPSG:4326 must be reprojected explicitly, not assumed.
    assets = gpd.GeoDataFrame(
        {"asset_id": ["a0"]},
        geometry=[box(499_900, 4_400_000, 500_100, 4_400_200)],  # ~ -8.0E, 39.7N in 32629
        crs="EPSG:32629",
    )
    burns_metric = gpd.GeoDataFrame(
        {"vintage_year": [2025]},
        geometry=[box(499_900, 4_400_000, 500_100, 4_400_200)],
        crs="EPSG:32629",
    )
    burns_4326 = burns_metric.to_crs("EPSG:4326")
    labels = asset_burn_labels(assets, burns_4326, years=[2025])
    assert labels.loc["a0"]


# ---------------------------------------------------------------------------
# lift_table
# ---------------------------------------------------------------------------


def test_lift_table_top_decile_concentrates_burns() -> None:
    # Scores perfectly ordered: the top assets burned, the rest did not.
    n = 100
    scores = np.linspace(1.0, 0.0, n)
    labels = np.zeros(n)
    labels[:10] = 1  # the 10 highest-scoring assets all burned
    table = lift_table(scores, labels, deciles=10)
    assert len(table) == 10
    assert int(table.iloc[0]["n_burned"]) == 10
    base_rate = labels.mean()
    assert table.iloc[0]["lift"] == pytest.approx((10 / 10) / base_rate)  # = 10x
    assert table.iloc[-1]["lift"] == pytest.approx(0.0)
    # Cumulative lift at the last decile collapses to 1.0 (all assets included).
    assert table.iloc[-1]["cumulative_lift"] == pytest.approx(1.0)


def test_lift_table_zero_base_rate_is_nan_not_crash() -> None:
    table = lift_table(np.linspace(1, 0, 20), np.zeros(20), deciles=10)
    assert bool(table["lift"].isna().all())
    assert bool((table["n_burned"] == 0).all())


def test_lift_table_deterministic_under_ties() -> None:
    scores = np.array([0.5] * 10)
    labels = np.array([1, 0] * 5)
    a = lift_table(scores, labels, deciles=5)
    b = lift_table(scores, labels, deciles=5)
    assert a.equals(b)


def test_lift_table_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        lift_table(np.zeros(5), np.zeros(4))


# ---------------------------------------------------------------------------
# spearman_rank
# ---------------------------------------------------------------------------


def test_spearman_positive_when_high_scores_burn() -> None:
    n = 50
    scores = np.linspace(0.0, 1.0, n)
    labels = (scores > 0.7).astype(float)  # high scores burned
    rho, p = spearman_rank(scores, labels)
    assert rho > 0.0
    assert 0.0 <= p <= 1.0


def test_spearman_near_zero_when_unrelated() -> None:
    scores = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    labels = np.array([1.0, 0.0, 1.0, 0.0, 1.0, 0.0])
    rho, _p = spearman_rank(scores, labels)
    assert abs(rho) < 0.5
