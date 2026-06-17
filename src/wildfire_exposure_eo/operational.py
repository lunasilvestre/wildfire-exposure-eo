"""Operational refresh — the "assets to watch" two-axis decision product (WU-26).

The operational spine crosses the repo's two axes every two days (matching the
~2-day EWDS reanalysis lag):

* **Axis 1 — validated structural exposure.** ``exposure_score`` from the scored
  pilot assets: a relative, AOI-normalised screening rank in [0, 1], validated
  against ICNF burns (v0.3.1, FWI UNWEIGHTED per Wave-2). This is the slow axis;
  it does not change between refreshes.
* **Axis 2 — current observed fire weather.** The CEMS EWDS FWI reanalysis at
  each asset's location: an OBSERVED danger *index* (~2-day lag, 0.25° regional
  grid). This is the fast axis; it is what makes the watch list "current".

The triage priority is their product:

    watch_priority = exposure_score * fwi_norm
    fwi_norm       = clip(fwi_current / FWI_REF, 0, 1)

Both factors live in [0, 1], so ``watch_priority`` does too. The product means an
asset surfaces only when it is *both* structurally exposed *and* under elevated
current fire weather — exactly the operational-triage question "which validated
high-exposure assets should I monitor *now*". A structurally-top asset under calm
weather sinks; a structurally-mid asset under extreme weather rises but is capped
by its structural factor.

Terminology guard (CLAUDE.md non-negotiable #6 + #9): ``watch_priority`` is
OPERATIONAL TRIAGE — "validated high-exposure assets currently under elevated
OBSERVED fire weather, prioritise monitoring". It is NOT a forecast, NOT a
probability, NOT a prediction of ignition; FWI is observed reanalysis. No
production / operationally-validated claims attach to it.

Determinism (#4): the join is a pure function of (assets, FWI surface, config);
there is no RNG. CRS (#2): the FWI surface is sampled in its native EPSG:4326 at
each asset's EPSG:4326 representative point — no implicit reprojection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd
    import xarray as xr

#: FWI normalisation reference: raw FWI mapped to ``fwi_norm == 1.0`` (saturates
#: above). The Canadian FWI has no fixed ceiling, so the watch list normalises by
#: the lower bound of the EFFIS "Extreme" fire-danger class (FWI = 50). Mapping
#: the operationally-actionable range onto [0, 1] and saturating at "extreme" is
#: the transparent choice: an asset's FWI factor reaches 1.0 exactly when its
#: current fire weather enters the extreme band. EFFIS fire-danger classes:
#: Low <11.2, Moderate 11.2-21.3, High 21.3-38.0, Very high 38.0-50.0,
#: Extreme 50.0-70.0, Very extreme >70.0; see
#: https://forest-fire.emergency.copernicus.eu/about-effis/technical-background/fire-danger-forecast
#: (accessed 2026-06-17). NOT a probability or a forecast (#6).
FWI_REF: float = 50.0

FWI_REF_RATIONALE: str = (
    "FWI normalised by 50.0 = the lower bound of the EFFIS 'Extreme' fire-danger "
    "class (FWI 50.0-70.0). fwi_norm reaches 1.0 exactly when current fire "
    "weather enters the extreme band; values above saturate. EFFIS fire-danger "
    "class scheme, https://forest-fire.emergency.copernicus.eu/about-effis/"
    "technical-background/fire-danger-forecast (accessed 2026-06-17). This is an "
    "OBSERVED danger index, not a probability or forecast."
)

#: The triage formula, spelled out for the artefact header (the contract).
WATCH_PRIORITY_FORMULA: str = (
    "watch_priority = exposure_score * fwi_norm; "
    "fwi_norm = clip(fwi_current / 50.0, 0, 1); "
    "exposure_score = validated structural AOI-relative rank in [0,1] (v0.3.1, "
    "FWI unweighted); fwi_current = current observed EWDS FWI at the asset's "
    "0.25deg grid cell. Operational triage, NOT a forecast/probability (#6)."
)


def normalize_fwi(value: float | None, ref: float = FWI_REF) -> float | None:
    """Normalise a raw FWI value to [0, 1] by ``ref`` (clipped, saturating).

    Returns ``None`` for a missing (uncovered) FWI value — never imputed. ``ref``
    must be positive (it is the value mapped to 1.0). Negative FWI is clamped to
    0.0 (FWI is non-negative in practice; this is a defensive floor).
    """
    if value is None:
        return None
    import math

    if not math.isfinite(value):
        return None
    if ref <= 0.0:
        raise ValueError(f"fwi_ref must be positive, got {ref}")
    return float(min(1.0, max(0.0, value / ref)))


def compute_watch_priority(
    exposure_score: float, fwi_current: float | None, ref: float = FWI_REF
) -> tuple[float | None, float | None]:
    """Return ``(fwi_norm, watch_priority)`` for one asset.

    ``watch_priority = exposure_score * fwi_norm``. Both are ``None`` when
    ``fwi_current`` is missing (the asset is carried but not ranked — never
    imputed). ``exposure_score`` is the validated structural rank in [0, 1].
    """
    fwi_norm = normalize_fwi(fwi_current, ref)
    if fwi_norm is None:
        return None, None
    return fwi_norm, float(exposure_score) * fwi_norm


def sample_fwi_at_points(surface: xr.DataArray, points: gpd.GeoDataFrame) -> pd.Series:
    """Nearest-grid-cell FWI value at each asset's representative point.

    The EWDS FWI grid is coarse (0.25°); sampling the *containing* cell at each
    asset point is the correct, transparent join (a buffer zonal-mean over a grid
    finer than a single cell would just re-derive the same value). ``surface`` is
    in EPSG:4326 (asserted) and ``points`` are reprojected to EPSG:4326 exactly
    once (non-negotiable #2). Returns a ``pd.Series`` indexed by ``asset_id`` with
    ``NaN`` where the point falls outside the surface's finite coverage.
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    if surface.rio.crs is None:
        raise ValueError("FWI surface has no CRS")
    if surface.rio.crs.to_epsg() != 4326:
        raise ValueError(
            f"FWI surface CRS {surface.rio.crs} != EPSG:4326 — refusing implicit "
            "reprojection (CLAUDE.md non-negotiable #2)"
        )
    pts = points[["asset_id", "geometry"]].to_crs("EPSG:4326")
    reps = pts.geometry.representative_point()
    xs = xr.DataArray(reps.x.to_numpy(), dims="asset")
    ys = xr.DataArray(reps.y.to_numpy(), dims="asset")
    sampled = surface.sel(x=xs, y=ys, method="nearest").to_numpy().astype("float64")
    # Out-of-grid points snap to an edge cell; mask any non-finite as missing.
    sampled = np.where(np.isfinite(sampled), sampled, np.nan)
    return pd.Series(sampled, index=pts["asset_id"].to_numpy(), name="fwi_current")


def _round_or_none(value: float | None, ndigits: int) -> float | None:
    """Round a value, passing ``None`` through (for clean artefact serialisation)."""
    import math

    if value is None or not math.isfinite(value):
        return None
    return round(float(value), ndigits)


def build_watch_list(
    assets: gpd.GeoDataFrame,
    fwi_current: pd.Series,
    *,
    ref: float = FWI_REF,
) -> pd.DataFrame:
    """Join scored assets with sampled FWI → a ranked watch-list DataFrame.

    ``assets`` must carry (at least) ``asset_id, osm_type, osm_id, asset_class,
    criticality_weight, exposure_score, exposure_rank`` and a point geometry in
    EPSG:4326. ``fwi_current`` is the per-asset sampled FWI (indexed by
    ``asset_id``; ``NaN`` = uncovered). Returns a DataFrame with one row per
    asset, columns matching :class:`WatchListItem`, sorted by ``watch_priority``
    descending (uncovered assets — ``watch_priority`` NaN — sink to the bottom,
    tie-broken deterministically by ``exposure_rank`` then ``asset_id``).

    No geometry column is returned; the orchestrator re-attaches geometry for the
    GeoParquet export. Pure function, no RNG (non-negotiable #4).
    """
    import math

    import pandas as pd

    if assets.crs is None or assets.crs.to_epsg() != 4326:
        raise ValueError(f"assets CRS is {assets.crs} — expected EPSG:4326 (#2)")
    reps = assets.geometry.representative_point()
    rows: list[dict[str, object]] = []
    for (_, asset), lon, lat in zip(assets.iterrows(), reps.x, reps.y, strict=True):
        aid = str(asset["asset_id"])
        raw = fwi_current.get(aid)
        raw_f = float(raw) if raw is not None and math.isfinite(float(raw)) else None
        fwi_norm, priority = compute_watch_priority(float(asset["exposure_score"]), raw_f, ref)
        rows.append(
            {
                "asset_id": aid,
                "osm_type": str(asset["osm_type"]),
                "osm_id": int(asset["osm_id"]),
                "asset_class": str(asset["asset_class"]),
                "criticality_weight": float(asset["criticality_weight"]),
                "lon": round(float(lon), 6),
                "lat": round(float(lat), 6),
                "exposure_score": float(asset["exposure_score"]),
                "exposure_rank": int(asset["exposure_rank"]),
                "fwi_current": _round_or_none(raw_f, 4),
                "fwi_norm": _round_or_none(fwi_norm, 6),
                "watch_priority": _round_or_none(priority, 6),
            }
        )
    df = pd.DataFrame(rows)
    # Deterministic order: priority desc (NaN last), then structural rank asc, then id.
    df = df.sort_values(
        by=["watch_priority", "exposure_rank", "asset_id"],
        ascending=[False, True, True],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)
    # Keep the nullable FWI columns as object dtype so missing values stay real
    # ``None`` (not NaN): the WatchListItem schema is ``float | None``, and a NaN
    # would be neither a valid float bound nor ``None`` on serialisation. We never
    # impute (#6) — missing stays missing, distinguishable from a true 0.0.
    for col in ("fwi_current", "fwi_norm", "watch_priority"):
        df[col] = df[col].astype("object").where(df[col].notna(), None)
    return df


def watch_list_markdown(
    df: pd.DataFrame,
    *,
    top_n: int,
    run_id: str,
    fwi_valid_date: str,
    formula: str = WATCH_PRIORITY_FORMULA,
    ref: float = FWI_REF,
) -> str:
    """Render the top-N watch list as a human-readable Markdown brief.

    The header states the honest framing (operational triage, not a forecast) and
    the exact formula; the table has one row per watched asset with asset class,
    location, structural rank, current FWI, and ``watch_priority``. Deterministic.
    """
    import math

    lines: list[str] = []
    lines.append("# Assets to watch — operational triage")
    lines.append("")
    lines.append(
        f"> Run `{run_id}` · current fire weather valid **{fwi_valid_date}** "
        "(EWDS FWI reanalysis, ~2-day lag, 0.25° regional grid)."
    )
    lines.append(">")
    lines.append(
        "> **Operational triage, not a forecast.** This list ranks *validated, "
        "high structural-exposure* assets that are *currently under elevated "
        "OBSERVED fire weather* — i.e. where to direct monitoring attention now. "
        "It is NOT a probability of fire, NOT a prediction of ignition. FWI is an "
        "observed danger index, not a forecast."
    )
    lines.append(">")
    lines.append(f"> Formula: `{formula}`")
    lines.append(f"> FWI normalisation reference (fwi_norm = 1.0 at): FWI = {ref:g}.")
    lines.append("")
    lines.append(
        "| # | Asset class | Location (lon, lat) | Structural rank | Current FWI | watch_priority |"
    )
    lines.append("|--:|---|---|--:|--:|--:|")
    shown = df.head(top_n)
    for i, (_, r) in enumerate(shown.iterrows(), start=1):
        fwi = r["fwi_current"]
        prio = r["watch_priority"]
        fwi_s = f"{float(fwi):.1f}" if fwi is not None and math.isfinite(float(fwi)) else "n/a"
        prio_s = f"{float(prio):.4f}" if prio is not None and math.isfinite(float(prio)) else "n/a"
        loc = f"{float(r['lon']):.4f}, {float(r['lat']):.4f}"
        lines.append(
            f"| {i} | {r['asset_class']} | {loc} | "
            f"#{int(r['exposure_rank'])} ({float(r['exposure_score']):.3f}) | "
            f"{fwi_s} | {prio_s} |"
        )
    lines.append("")
    return "\n".join(lines)
