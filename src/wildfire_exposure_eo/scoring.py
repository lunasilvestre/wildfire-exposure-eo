"""Compose per-asset features into the exposure rank (WU-6, prompt 10).

The score is a deliberately transparent linear combination of within-AOI
percentile ranks (``config/exposure_score.yaml``). No learned weights, no
black box: a reviewer can reproduce any row by hand from the features parquet
and the YAML.

Terminology guard (CLAUDE.md non-negotiable #6): ``exposure_score`` is a
relative, AOI-normalised screening rank in [0, 1] — never a probability of fire.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from wildfire_exposure_eo.schemas.scored_asset import FEATURE_NAMES


class ExposureConfig(BaseModel):
    """Parsed ``config/exposure_score.yaml``; weights must sum to 1.0."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    version: str = Field(..., min_length=1)
    formula: str = Field(..., min_length=1)
    normalization: str = Field(..., min_length=1)
    weights: dict[str, float]

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ExposureConfig:
        total = sum(self.weights.values())
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"exposure weights sum to {total}, expected 1.0")
        unknown = sorted(set(self.weights) - set(FEATURE_NAMES))
        if unknown:
            raise ValueError(f"exposure weights reference unknown feature(s): {unknown}")
        return self


def load_exposure_config(path: Path) -> ExposureConfig:
    """Load and validate the exposure-score config YAML."""
    import yaml

    return ExposureConfig.model_validate(yaml.safe_load(path.read_text()))


def compose_exposure(features_df: pd.DataFrame, config: ExposureConfig) -> pd.DataFrame:
    """Compose within-AOI percentile ranks into the composite exposure rank.

    Returns ``a relative, AOI-normalised screening rank — not a probability of
    fire`` per asset: each feature is percentile-ranked within the AOI (ties:
    average rank, so the rank lies in (0, 1]); the ranks are combined with the
    YAML weights, renormalised per row over the features actually present so a
    missing feature is never silently imputed as zero.

    ``features_df`` is indexed by ``asset_id``; only columns that are both
    present and weighted contribute. The result carries the original feature
    columns plus ``exposure_score`` ∈ [0, 1], integer ``exposure_rank``
    (1 = most exposed, deterministic tie-break by input row order), and a
    ``features_present`` list per row.
    """
    weighted = [c for c in FEATURE_NAMES if c in features_df.columns and c in config.weights]
    if not weighted:
        raise ValueError("no weighted feature columns present in features_df")

    ranks = pd.DataFrame(index=features_df.index)
    for col in weighted:
        ranks[col] = features_df[col].rank(pct=True, method="average")

    weights = pd.Series({c: config.weights[c] for c in weighted}, dtype="float64")
    present_mask = ranks[weighted].notna()
    weighted_ranks = ranks[weighted].mul(weights, axis=1)
    num = weighted_ranks.sum(axis=1, skipna=True)
    den = present_mask.mul(weights, axis=1).sum(axis=1)
    exposure_score = (num / den).where(den > 0)

    out = features_df.copy()
    out["exposure_score"] = exposure_score
    out["exposure_rank"] = exposure_score.rank(ascending=False, method="first").astype("int64")
    out["features_present"] = [
        [c for c in FEATURE_NAMES if c in features_df.columns and pd.notna(features_df.loc[idx, c])]
        for idx in features_df.index
    ]
    return out
