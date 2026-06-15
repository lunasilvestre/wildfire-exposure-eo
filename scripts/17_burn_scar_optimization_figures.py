"""WU-10 burn-scar optimization figures (seed 42, explicit CRS).

Regenerates the two figures referenced from ``docs/burn_scar_optimization.md``:

* ``fig6_burn_scar_max_vs_degrid.png`` — side-by-side full-AOI composites:
  the previous 179-scene per-pixel MAX composite (the over-prediction "wash"
  plus a phase-locked grid) versus the de-grid p85 composite. Both panels use
  the same YlOrRd 0..1 scale so the wash collapse is visible at a glance.
* ``fig7_burn_scar_degrid_alpha_truth.png`` — the de-grid p85 composite rendered
  with the SITE display rule (value-driven alpha: transparent below 0.3, opacity
  ramping linearly to full at 1.0), with the ICNF 2023-2025 burn perimeters
  (the recent multi-year persistent-scar detection truth) overlaid.

Both COGs are read in their authoritative EPSG:4326 and rendered in EPSG:4326;
the AOI and ICNF perimeters are asserted EPSG:4326 before plotting
(CLAUDE.md non-negotiable #2). The COG value is a burn-scar inference score — a
relative model score — NEVER a calibrated probability, a risk, or a forecast.
Burn SCARS detected = post-event spectral signatures of fires that already
happened. Not ignition prediction.

Determinism: seed 42 (no RNG is actually used; the figures are a pure function
of the committed COGs and the ICNF burns parquet).

Usage::

    uv run python scripts/17_burn_scar_optimization_figures.py \\
        --max-cog   outputs/cogs/burn_scar_20260610T072820Z.tif \\
        --degrid-cog outputs/cogs/burn_scar_20260615T192025Z.tif \\
        --burns     outputs/parquet/icnf_burns_20260610T164453Z.parquet
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import Normalize

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
_COG_DIR = _ROOT / "outputs" / "cogs"
_PARQUET_DIR = _ROOT / "outputs" / "parquet"
_FIGS_DIR = _ROOT / "docs" / "figures"
_AOI_PILOT = _ROOT / "data" / "aoi" / "pilot.geojson"

SEED = 42
#: Site display rule (mirrors docs/app/app.js): transparent below this, then a
#: linear opacity ramp to full at 1.0. Keep in sync with app.js.
ALPHA_FLOOR = 0.3
#: Recent multi-year vintages used as the detection truth overlay.
TRUTH_YEARS = [2023, 2024, 2025]

matplotlib.rcParams["figure.dpi"] = 150
matplotlib.rcParams["font.size"] = 9


def _load_figures_module() -> object:
    """Import 12_make_figures.py (digit-leading module name) for its helpers."""
    spec = importlib.util.spec_from_file_location(
        "make_figures_12", _SCRIPTS / "12_make_figures.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_fig = _load_figures_module()


def _downsampled_4326(
    cog_path: Path, *, max_rows: int = 600
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Reuse 12_make_figures.raster_to_epsg4326, then downsample for file size."""
    data, lons, lats, _ = _fig.raster_to_epsg4326(cog_path)  # type: ignore[attr-defined]
    step = max(1, data.shape[0] // max_rows)
    return data[::step, ::step], lons[::step, ::step], lats[::step, ::step]


def _value_driven_rgba(data: np.ndarray) -> np.ndarray:
    """YlOrRd colours with the site's value-driven alpha (transparent < floor).

    Returns an (H, W, 4) RGBA array: hue from YlOrRd over [0, 1]; alpha 0 below
    ALPHA_FLOOR, then a linear ramp 0 -> 1 over [ALPHA_FLOOR, 1.0]. NaN -> fully
    transparent. This is the same rule docs/app/app.js applies client-side.
    """
    cmap = plt.get_cmap("YlOrRd")
    norm = Normalize(vmin=0.0, vmax=1.0)
    rgba = cmap(norm(np.nan_to_num(data, nan=0.0)))
    span = max(1.0 - ALPHA_FLOOR, 1e-6)
    alpha = np.clip((data - ALPHA_FLOOR) / span, 0.0, 1.0)
    alpha[~np.isfinite(data)] = 0.0
    rgba[..., 3] = alpha
    return rgba


def make_fig_max_vs_degrid(max_cog: Path, degrid_cog: Path, out: Path) -> None:
    """Side-by-side full-AOI MAX vs de-grid p85 composites, shared YlOrRd scale."""
    aoi = gpd.read_file(_AOI_PILOT)
    assert aoi.crs is not None and aoi.crs.to_epsg() == 4326, f"AOI CRS {aoi.crs}"

    max_d, max_lon, max_lat = _downsampled_4326(max_cog)
    deg_d, deg_lon, deg_lat = _downsampled_4326(degrid_cog)

    max_frac = float(np.mean(max_d[np.isfinite(max_d)] >= 0.5))
    deg_frac = float(np.mean(deg_d[np.isfinite(deg_d)] >= 0.5))

    fig, axes = plt.subplots(1, 2, figsize=(13, 6), sharex=True, sharey=True)
    max_title = f"Before: 179-scene per-pixel MAX\n(frac ≥ 0.5 = {max_frac * 100:.0f}%)"
    deg_title = f"After: de-grid p85 composite\n(frac ≥ 0.5 = {deg_frac * 100:.1f}%)"
    panels = [
        (axes[0], max_lon, max_lat, max_d, max_title),
        (axes[1], deg_lon, deg_lat, deg_d, deg_title),
    ]
    im = None
    for ax, lon, lat, d, title in panels:
        im = ax.pcolormesh(lon, lat, d, cmap="YlOrRd", vmin=0.0, vmax=1.0, shading="auto")
        aoi.boundary.plot(ax=ax, color="#444444", linewidth=0.8, linestyle="--")
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("Longitude (°E)", fontsize=8)
    axes[0].set_ylabel("Latitude (°N)", fontsize=8)
    assert im is not None
    cbar = fig.colorbar(im, ax=axes, fraction=0.025, pad=0.02)
    cbar.set_label("Burn-scar inference score (relative, not a probability)", fontsize=8)

    fig.suptitle(
        "WU-10 burn-scar over-prediction fix: MAX wash → de-grid p85 (full pilot AOI, EPSG:4326)",
        fontsize=11,
    )
    _fig._add_attribution(axes[0])  # type: ignore[attr-defined]
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


def make_fig_degrid_alpha_truth(degrid_cog: Path, burns_pq: Path, out: Path) -> None:
    """De-grid p85 with the site value-driven-alpha display + ICNF 2023-25 truth."""
    aoi = gpd.read_file(_AOI_PILOT)
    assert aoi.crs is not None and aoi.crs.to_epsg() == 4326, f"AOI CRS {aoi.crs}"
    burns = gpd.read_parquet(burns_pq)
    assert burns.crs is not None and burns.crs.to_epsg() == 4326, f"burns CRS {burns.crs}"
    truth = burns[burns["vintage_year"].isin(TRUTH_YEARS)].copy()

    data, lons, lats = _downsampled_4326(degrid_cog)
    rgba = _value_driven_rgba(data)

    fig, ax = plt.subplots(figsize=(10, 7))
    # A precomputed RGBA array renders cleanly via imshow with a geographic
    # extent. lats run top->bottom in the COG (origin upper), so use origin
    # "upper" and the full lon/lat bounds for the extent.
    extent = (float(lons.min()), float(lons.max()), float(lats.min()), float(lats.max()))
    ax.imshow(rgba, extent=extent, origin="upper", interpolation="nearest", zorder=2)
    ax.set_aspect("auto")

    aoi.boundary.plot(ax=ax, color="#444444", linewidth=0.8, linestyle="--", zorder=3)
    if not truth.empty:
        truth_label = (
            f"ICNF burn perimeters {TRUTH_YEARS[0]}–{TRUTH_YEARS[-1]} ({len(truth)} polygons)"
        )
        truth.boundary.plot(
            ax=ax,
            color="#1f4fd8",
            linewidth=0.9,
            zorder=4,
            label=truth_label,
        )
        ax.legend(loc="upper right", fontsize=7)

    # Colorbar reflecting the YlOrRd hue scale (alpha handled by the display).
    sm = plt.cm.ScalarMappable(cmap=plt.get_cmap("YlOrRd"), norm=Normalize(vmin=0.0, vmax=1.0))
    cbar = fig.colorbar(sm, ax=ax, fraction=0.03, pad=0.01)
    cbar.set_label(
        f"Burn-scar inference score (relative; shown ≥ {ALPHA_FLOOR:.1f} only)", fontsize=8
    )

    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        "De-grid p85 burn-scar composite — site display (value-driven alpha)\n"
        f"transparent below {ALPHA_FLOOR:g}, opacity ramping to full at 1.0; "
        f"ICNF {TRUTH_YEARS[0]}–{TRUTH_YEARS[-1]} burns overlaid",
        fontsize=10,
        pad=10,
    )
    ax.set_xlim(lons.min(), lons.max())
    ax.set_ylim(lats.min(), lats.max())
    _fig._add_attribution(ax)  # type: ignore[attr-defined]
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


def main() -> int:
    np.random.default_rng(SEED)  # determinism handshake (no RNG draw is needed)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-cog",
        type=Path,
        default=_COG_DIR / "burn_scar_20260610T072820Z.tif",
        help="previous 179-scene MAX composite COG (EPSG:4326)",
    )
    parser.add_argument(
        "--degrid-cog",
        type=Path,
        default=_COG_DIR / "burn_scar_20260615T192025Z.tif",
        help="de-grid p85 composite COG (EPSG:4326)",
    )
    parser.add_argument(
        "--burns",
        type=Path,
        default=None,
        help="ICNF burns parquet (EPSG:4326); default = newest in outputs/parquet",
    )
    parser.add_argument("--out-dir", type=Path, default=_FIGS_DIR)
    args = parser.parse_args()

    burns = args.burns
    if burns is None:
        cands = sorted(
            c for c in _PARQUET_DIR.glob("icnf_burns_*.parquet") if "_smoke_" not in c.name
        )
        if not cands:
            parser.error("no icnf_burns_*.parquet found; pass --burns")
        burns = cands[-1]

    for p in (args.max_cog, args.degrid_cog, burns):
        if not p.exists():
            parser.error(f"missing input: {p}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[fig17] MAX cog: {args.max_cog.name}")
    print(f"[fig17] de-grid cog: {args.degrid_cog.name}")
    print(f"[fig17] burns: {burns.name}")
    make_fig_max_vs_degrid(
        args.max_cog, args.degrid_cog, args.out_dir / "fig6_burn_scar_max_vs_degrid.png"
    )
    make_fig_degrid_alpha_truth(
        args.degrid_cog, burns, args.out_dir / "fig7_burn_scar_degrid_alpha_truth.png"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
