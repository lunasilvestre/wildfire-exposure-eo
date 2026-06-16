"""Unit tests for the exposure-rank composition (WU-6)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from wildfire_exposure_eo.schemas.scored_asset import FEATURE_NAMES
from wildfire_exposure_eo.scoring import ExposureConfig, compose_exposure, load_exposure_config

CONFIG_PATH = Path("config/exposure_score.yaml")


def _config() -> ExposureConfig:
    return load_exposure_config(CONFIG_PATH)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "fuel_class_severity_weight": [0.1, 0.5, 0.9, 0.3],
            "canopy_height_p90_m": [2.0, 20.0, 8.0, 15.0],
            "slope_max_deg": [5.0, 30.0, 12.0, 25.0],
            "historical_burn_share": [0.0, 0.4, 0.1, 0.2],
            "recent_burn_share_12mo": [0.0, 0.2, 0.05, 0.01],
            "nbr_delta_recent": [-0.1, 0.3, 0.1, 0.05],
        },
        index=pd.Index([f"a{i}" for i in range(4)], name="asset_id"),
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_real_config_weights_sum_to_one() -> None:
    cfg = _config()
    assert cfg.version == "0.3.1"
    assert abs(sum(cfg.weights.values()) - 1.0) < 1e-9
    assert "fwi_p95_recent_season" not in cfg.weights  # dropped at 0.2.0
    # 0.3.1 UNWEIGHTS fire weather: Wave-2 validation showed backdated FWI does not
    # improve burn discrimination, so the full EWDS FWI system stays
    # AVAILABLE-but-UNWEIGHTED (operational overlay, not the validated score).
    for fwi_feat in ("fwi_fwi_current", "fwi_bui_current", "fwi_dc_current", "fwi_dmc_current"):
        assert fwi_feat not in cfg.weights


def test_config_rejects_weights_not_summing_to_one() -> None:
    with pytest.raises(ValueError, match="sum to"):
        ExposureConfig(
            version="x",
            formula="linear_combination",
            normalization="percentile_rank_within_aoi",
            weights={"fuel_class_severity_weight": 0.5},
        )


def test_config_rejects_unknown_feature() -> None:
    with pytest.raises(ValueError, match="unknown feature"):
        ExposureConfig(
            version="x",
            formula="linear_combination",
            normalization="percentile_rank_within_aoi",
            weights={"made_up_feature": 1.0},
        )


# ---------------------------------------------------------------------------
# compose_exposure
# ---------------------------------------------------------------------------


def test_scores_in_unit_interval_and_ranks_contiguous() -> None:
    out = compose_exposure(_frame(), _config())
    assert out["exposure_score"].between(0.0, 1.0).all()
    assert sorted(out["exposure_rank"]) == [1, 2, 3, 4]
    # Highest composite gets rank 1.
    assert out["exposure_rank"].idxmin() == out["exposure_score"].idxmax()


def test_rank_invariant_under_monotone_feature_transform() -> None:
    cfg = _config()
    base = compose_exposure(_frame(), cfg)
    transformed = _frame()
    # Strictly increasing transforms preserve within-AOI percentile ranks.
    transformed["canopy_height_p90_m"] = np.exp(transformed["canopy_height_p90_m"] / 10.0)
    transformed["slope_max_deg"] = transformed["slope_max_deg"] ** 3
    out = compose_exposure(transformed, cfg)
    pd.testing.assert_series_equal(out["exposure_rank"], base["exposure_rank"])


def test_percentile_ranks_in_unit_interval() -> None:
    # The composite is a weighted mean of within-AOI percentile ranks ∈ (0, 1].
    out = compose_exposure(_frame(), _config())
    assert (out["exposure_score"] > 0.0).all()
    assert (out["exposure_score"] <= 1.0).all()


def test_missing_feature_renormalises_without_imputation() -> None:
    cfg = _config()
    df = _frame()
    df.loc["a3", "recent_burn_share_12mo"] = np.nan
    out = compose_exposure(df, cfg)
    present = out.loc["a3", "features_present"]
    assert "recent_burn_share_12mo" not in present
    # Present = every FEATURE_NAMES column actually supplied in this frame, minus
    # the one nulled for a3 (features absent from the frame are simply not listed).
    supplied = {c for c in FEATURE_NAMES if c in df.columns}
    assert set(present) == supplied - {"recent_burn_share_12mo"}

    # Hand-recompute a3: weighted mean of its percentile ranks over the 5
    # present features, renormalised by their weights (no zero imputation).
    ranks = {c: df[c].rank(pct=True, method="average").loc["a3"] for c in present}
    num = sum(cfg.weights[c] * ranks[c] for c in present)
    den = sum(cfg.weights[c] for c in present)
    assert out.loc["a3", "exposure_score"] == pytest.approx(num / den)


@given(
    values=st.lists(
        st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=32,
    )
)
def test_property_ranks_in_unit_interval_and_order_preserving(values: list[float]) -> None:
    # Property (prompt 10, deliverable 5): percentile ranks always land in (0, 1]
    # and the composite preserves the raw-value order (equal values tie exactly).
    df = pd.DataFrame(
        {"fuel_class_severity_weight": values},
        index=pd.Index([f"a{i}" for i in range(len(values))], name="asset_id"),
    )
    out = compose_exposure(df, _config())
    scores = out["exposure_score"].to_numpy()
    assert ((scores > 0.0) & (scores <= 1.0)).all()
    arr = np.asarray(values)
    for i in range(len(arr)):
        for j in range(len(arr)):
            if arr[i] < arr[j]:
                assert scores[i] < scores[j]
            elif arr[i] == arr[j]:
                assert scores[i] == scores[j]


def test_missing_entire_feature_column_is_dropped() -> None:
    cfg = _config()
    df = _frame().drop(columns=["recent_burn_share_12mo"])
    out = compose_exposure(df, cfg)
    assert "recent_burn_share_12mo" not in out.columns
    for present in out["features_present"]:
        assert "recent_burn_share_12mo" not in present
    assert out["exposure_score"].between(0.0, 1.0).all()
