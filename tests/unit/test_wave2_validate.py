"""Unit tests for scripts/24_wave2_validate.py (Wave-2 multi-AOI validation).

Pure-function checks, no parquet/network: the ``_compute`` metric wrapper and the
``_assert_no_leakage`` §12 gate. The metric wrapper must (a) drop NaN scores before
ranking, (b) flag a degenerate (zero-burn) split, and (c) reproduce a known
top-decile lift on a hand-built monotone case. The leakage gate must raise iff any
validation burn shares or predates the window-end year.
"""

from __future__ import annotations

import importlib
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

# Repo-root import shim (mirrors the script itself); digit-prefixed module name.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
wave2 = importlib.import_module("24_wave2_validate")


def _burns(years: list[int]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"vintage_year": years},
        geometry=[Point(0, 0) for _ in years],
        crs="EPSG:4326",
    )


# ---------------------------------------------------------------------------
# _assert_no_leakage — the §12 hard rule, replicated at AOI granularity
# ---------------------------------------------------------------------------


def test_leakage_passes_when_all_burns_after_window_year() -> None:
    wave2._assert_no_leakage(date(2018, 8, 6), _burns([2019, 2020, 2025]))  # no raise


def test_leakage_passes_on_empty_validation_set() -> None:
    wave2._assert_no_leakage(date(2025, 8, 15), _burns([]))  # vacuously safe


def test_leakage_raises_when_a_burn_shares_window_year() -> None:
    with pytest.raises(ValueError, match="temporal leakage"):
        wave2._assert_no_leakage(date(2018, 8, 6), _burns([2018, 2019]))


def test_leakage_raises_when_a_burn_predates_window_year() -> None:
    with pytest.raises(ValueError, match="temporal leakage"):
        wave2._assert_no_leakage(date(2018, 8, 6), _burns([2017, 2020]))


# ---------------------------------------------------------------------------
# _compute — lift + Spearman wrapper
# ---------------------------------------------------------------------------


def test_compute_flags_degenerate_when_no_burned() -> None:
    scores = pd.Series([0.1, 0.5, 0.9], index=["a", "b", "c"])
    labels = pd.Series([False, False, False], index=["a", "b", "c"])
    m = wave2._compute(scores, labels)
    assert m["degenerate"] is True
    assert m["n_burned"] == 0
    assert m["n"] == 3


def test_compute_drops_nan_scores_before_ranking() -> None:
    # The NaN-scored asset must not enter the lift table at all.
    scores = pd.Series([0.9, np.nan, 0.1], index=["a", "b", "c"])
    labels = pd.Series([True, True, False], index=["a", "b", "c"])
    m = wave2._compute(scores, labels, deciles=2)
    assert m["degenerate"] is False
    assert m["n"] == 2  # the NaN-scored row is excluded
    assert m["n_burned"] == 1


def test_compute_perfect_ranking_top_decile_lift() -> None:
    # 100 assets, 10 burned, all 10 at the very top of the score order →
    # top decile (10 assets) holds all burns: burn_rate 1.0, base 0.1, lift 10×.
    n = 100
    scores = pd.Series(np.linspace(1.0, 0.0, n), index=[f"x{i}" for i in range(n)])
    labels = pd.Series([i < 10 for i in range(n)], index=scores.index)
    m = wave2._compute(scores, labels, deciles=10)
    assert m["n"] == n
    assert m["n_burned"] == 10
    assert m["top_decile_lift"] == pytest.approx(10.0)
    assert m["spearman_rho"] > 0.0


def test_compute_high_base_rate_caps_lift_below_inverse_base() -> None:
    # base rate 0.6 → max possible top-decile lift is 1/0.6 ≈ 1.67×, documenting
    # the Pedrógão-grande saturation caveat the report relies on.
    n = 100
    scores = pd.Series(np.linspace(1.0, 0.0, n), index=[f"x{i}" for i in range(n)])
    labels = pd.Series([i < 60 for i in range(n)], index=scores.index)
    m = wave2._compute(scores, labels, deciles=10)
    assert m["n_burned"] == 60
    assert m["top_decile_lift"] <= 1.0 / 0.6 + 1e-9
