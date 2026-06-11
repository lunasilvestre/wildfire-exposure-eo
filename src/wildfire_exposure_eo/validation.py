"""Temporal-leakage-safe validation of the exposure rank vs subsequent ICNF burns.

WU-7 (prompt 11). The exposure rank is a *relative screening rank* — these
functions measure whether higher-ranked assets burned more often in years
strictly **after** the score-input window (the methodology §12 leakage rule),
never whether the score is a calibrated probability. Lift and Spearman are
monotone-association diagnostics; they say nothing about absolute fire chance.

``assert_no_temporal_leakage`` is the hard gate: it raises unless every burn
used as a validation label post-dates the score-input window. Fire is spatially
autocorrelated, so even a clean temporal split lets the score flatter itself
(plan caveat #1) — the leakage rule is necessary, not sufficient, and the
report states that plainly.

scipy is used for the Spearman p-value only; it is already resolved in the
locked environment as a transitive dependency, so this adds no top-level dep
(prompt 11 sanctions "numpy/scipy-only").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from wildfire_exposure_eo.features import ASSET_CRS, DateRange

if TYPE_CHECKING:
    import geopandas as gpd

logger = logging.getLogger(__name__)


def assert_no_temporal_leakage(score_window: DateRange, burns: gpd.GeoDataFrame) -> None:
    """The §12 hard rule: every validation burn must post-date the score window.

    ``burns`` is the set of perimeters used as validation labels. Raises
    ``ValueError`` unless ``min(vintage_year) > score_window.end.year``. An empty
    validation set passes vacuously — there is nothing that could leak.

    This is deliberately a raised exception rather than a bare ``assert`` so the
    check survives ``python -O`` and cannot be silently optimised away.
    """
    if "vintage_year" not in burns.columns:
        raise ValueError("burns missing 'vintage_year' column")
    if len(burns) == 0:
        return
    min_year = int(burns["vintage_year"].min())
    if min_year <= score_window.end.year:
        raise ValueError(
            f"temporal leakage: validation burns include vintage_year {min_year} "
            f"<= score-input window end year {score_window.end.year} "
            f"(window {score_window.start}..{score_window.end}); validate only on "
            "years strictly after the score-input window (methodology §12)"
        )


def asset_burn_labels(
    assets: gpd.GeoDataFrame, burns: gpd.GeoDataFrame, *, years: list[int]
) -> pd.Series:
    """Boolean per asset: does its buffer intersect any burn in ``years``?

    ``assets`` carries one row per asset with an ``asset_id`` column and the
    *buffered* geometry (e.g. from :func:`wildfire_exposure_eo.features.buffer_assets`,
    EPSG:32629). Both layers are reprojected to EPSG:32629 explicitly before the
    overlay (non-negotiable #2 — no implicit reprojection). The test is a
    polygon-intersects against the union of the selected-vintage burn perimeters.

    Returns a ``pd.Series[bool]`` named ``burned`` indexed by ``asset_id``,
    preserving the input row order.
    """
    if "asset_id" not in assets.columns:
        raise ValueError("assets missing 'asset_id' column")
    if "vintage_year" not in burns.columns:
        raise ValueError("burns missing 'vintage_year' column")
    if assets.crs is None:
        raise ValueError("assets has no CRS — refusing to assume one")
    if burns.crs is None:
        raise ValueError("burns has no CRS — refusing to assume one")

    idx = pd.Index(assets["asset_id"], name="asset_id")
    a_metric = assets.to_crs(ASSET_CRS)
    sel = burns[burns["vintage_year"].isin(years)]
    if len(sel) == 0:
        logger.warning(
            "[validation] no burns in validation years %s — all labels False", sorted(years)
        )
        return pd.Series(np.zeros(len(idx), dtype=bool), index=idx, name="burned")

    union = sel.to_crs(ASSET_CRS).union_all()
    hit = a_metric.geometry.intersects(union).to_numpy()
    return pd.Series(np.asarray(hit, dtype=bool), index=idx, name="burned")


def lift_table(
    scores: pd.Series | np.ndarray, labels: pd.Series | np.ndarray, *, deciles: int = 10
) -> pd.DataFrame:
    """Decile lift of a relative score against a binary burn outcome.

    Assets are ordered by **descending** score (stable tie-break by input order,
    for determinism) and split into ``deciles`` near-equal-count groups; group 1
    holds the highest-scoring assets. Per group: ``n_assets``, ``n_burned``,
    ``burn_rate`` and ``lift`` (= ``burn_rate / base_rate``), plus the cumulative
    burn-rate and cumulative lift down to that group. ``lift`` is ``NaN`` when the
    base rate is zero (no burned assets — the smoke AOI).

    A descriptive screening diagnostic over a *rank*, never a calibrated forecast.
    """
    if deciles < 1:
        raise ValueError(f"deciles must be >= 1, got {deciles}")
    s = np.asarray(scores, dtype="float64")
    y = np.asarray(labels, dtype="float64")
    if s.shape != y.shape:
        raise ValueError(f"scores {s.shape} and labels {y.shape} length mismatch")
    if s.size == 0:
        raise ValueError("empty scores/labels")

    base_rate = float(y.mean())
    order = np.argsort(-s, kind="stable")
    groups = np.array_split(order, deciles)

    rows: list[dict[str, float | int]] = []
    cum_assets = 0
    cum_burned = 0.0
    for i, g in enumerate(groups, start=1):
        n_a = int(g.size)
        if n_a == 0:
            continue
        n_b = float(y[g].sum())
        cum_assets += n_a
        cum_burned += n_b
        burn_rate = n_b / n_a
        rows.append(
            {
                "decile": i,
                "n_assets": n_a,
                "n_burned": int(n_b),
                "burn_rate": burn_rate,
                "lift": burn_rate / base_rate if base_rate > 0 else float("nan"),
                "cumulative_burn_rate": cum_burned / cum_assets,
                "cumulative_lift": (
                    (cum_burned / cum_assets) / base_rate if base_rate > 0 else float("nan")
                ),
            }
        )
    return pd.DataFrame(rows)


def spearman_rank(
    scores: pd.Series | np.ndarray, labels: pd.Series | np.ndarray
) -> tuple[float, float]:
    """Spearman rank correlation ``(rho, two-sided p-value)`` of score vs label.

    With a binary label this is the rank-biserial form of the correlation: it
    measures only monotone association between the relative rank and subsequent
    burning, never calibration. Uses ``scipy.stats.spearmanr`` (transitive dep;
    no new top-level requirement).
    """
    from scipy import stats

    s = np.asarray(scores, dtype="float64")
    y = np.asarray(labels, dtype="float64")
    if s.shape != y.shape:
        raise ValueError(f"scores {s.shape} and labels {y.shape} length mismatch")
    result = stats.spearmanr(s, y)
    return float(result.statistic), float(result.pvalue)  # pyright: ignore[reportAttributeAccessIssue]
