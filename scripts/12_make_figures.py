"""Generate visual deliverables for wildfire-exposure-eo (WU-8, prompt 12).

Produces five static PNG figures under docs/figures/ and one self-contained
interactive HTML map.  All outputs are deterministic (seed 42, fixed figure
sizes, sorted draw order).

Usage::

    uv run python scripts/12_make_figures.py          # pilot AOI (all artefacts)
    uv run python scripts/12_make_figures.py --smoke  # smoke AOI (gate check)

CI does NOT regenerate pilot figures; they are committed artefacts.  Pilot
regeneration requires the relevant outputs/ parquet and COG files (WU-1..7).

Generated artefacts committed under docs/figures/ are the one exception to
"no generated files in git" — they are project documentation.

Attribution: OSM contributors (basemap tiles);
Sentinel-2 © ESA/Copernicus (burn-scar inference inputs);
ICNF (burn-perimeter reference data);
EFFIS / Copernicus Emergency Management Service (fuel map);
DGT (COSc land cover); ETH GCH — Lang et al. 2023 (canopy height).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import warnings
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.axes
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import rioxarray  # noqa: F401  — required for .rio accessor
import yaml
from matplotlib.colors import BoundaryNorm, ListedColormap
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject

# Repo-root import shim so the script runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wildfire_exposure_eo.scoring import load_exposure_config

matplotlib.rcParams["figure.dpi"] = 150
matplotlib.rcParams["font.size"] = 9
matplotlib.rcParams["axes.titlesize"] = 10

_ROOT = Path(__file__).resolve().parents[1]
_PARQUET_DIR = _ROOT / "outputs" / "parquet"
_COG_DIR = _ROOT / "outputs" / "cogs"
_VAL_DIR = _ROOT / "outputs" / "validation"
_FIGS_DIR = _ROOT / "docs" / "figures"
_CROSSWALK = _ROOT / "config" / "fuel_crosswalk.yaml"
_SCORE_YAML = _ROOT / "config" / "exposure_score.yaml"
_AOI_PILOT = _ROOT / "data" / "aoi" / "pilot.geojson"
_AOI_SMOKE = _ROOT / "data" / "aoi" / "smoke.geojson"

_FIGSIZE = (10, 7)
_SEED = 42
rng = np.random.default_rng(_SEED)

# --------------------------------------------------------------------------- #
# Attribution footer (every figure)
# --------------------------------------------------------------------------- #
_ATTRIB = (
    "OSM contributors · ESA/Copernicus (Sentinel-2 / burn-scar) · ICNF · "
    "EFFIS/CEMS · DGT · ETH GCH (Lang et al. 2023)"
)


# --------------------------------------------------------------------------- #
# Data-loading helpers (unit-testable)
# --------------------------------------------------------------------------- #


def _latest(prefix: str, folder: Path, suffix: str, *, smoke: bool) -> Path:
    """Return the newest matching artefact in *folder* (timestamps sort lexically)."""
    pat = f"{prefix}_smoke_*{suffix}" if smoke else f"{prefix}_*{suffix}"
    cands = sorted(folder.glob(pat))
    if not smoke:
        cands = [c for c in cands if "_smoke_" not in c.name]
    if not cands:
        raise FileNotFoundError(
            f"No artefact matching {pat!r} in {folder}. Run the relevant WU pipeline step first."
        )
    return cands[-1]


def load_exposure(*, smoke: bool) -> gpd.GeoDataFrame:
    """Load the latest exposure parquet (pilot or smoke).

    Prefers the backdated pilot parquet that validation used.
    Excludes ablation parquets (those carry a different schema).
    """
    # Use a date-pattern prefix to exclude ablation files (exposure_ablation_*)
    pat = "exposure_smoke_*.parquet" if smoke else "exposure_[0-9]*.parquet"
    cands = sorted(_PARQUET_DIR.glob(pat))
    if not cands:
        raise FileNotFoundError(
            f"No artefact matching {pat!r} in {_PARQUET_DIR}. Run the WU-6 pipeline step first."
        )
    path = cands[-1]
    gdf = gpd.read_parquet(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif str(gdf.crs.to_epsg()) != "4326":
        gdf = gdf.to_crs("EPSG:4326")
    # Parse features JSON → columns
    feat_df = gdf["features"].apply(json.loads)
    for col in (
        "fuel_class_severity_weight",
        "canopy_height_p90_m",
        "slope_max_deg",
        "historical_burn_share",
        "nbr_delta_recent",
    ):
        gdf[col] = feat_df.apply(lambda d, c=col: d.get(c, float("nan")))
    # Normalised rank: 0 (lowest exposure) … 1 (highest exposure)
    n = len(gdf)
    gdf["rank_norm"] = 1.0 - (gdf["exposure_rank"] - 1) / max(n - 1, 1)
    return gdf


def load_fuel_cog_path(*, smoke: bool) -> Path:
    """Path to the latest fuel-class COG (pilot or smoke)."""
    return _latest("fuel_class", _COG_DIR, ".tif", smoke=smoke)


def load_burn_scar_cog_path(*, smoke: bool) -> Path:
    """Path to the latest burn-scar COG (pilot or smoke)."""
    return _latest("burn_scar", _COG_DIR, ".tif", smoke=smoke)


def load_burns(*, smoke: bool) -> gpd.GeoDataFrame:
    """Load the latest ICNF burns parquet (pilot or smoke)."""
    path = _latest("icnf_burns", _PARQUET_DIR, ".parquet", smoke=smoke)
    gdf = gpd.read_parquet(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif str(gdf.crs.to_epsg()) != "4326":
        gdf = gdf.to_crs("EPSG:4326")
    return gdf


def load_metrics(*, smoke: bool) -> dict:  # type: ignore[type-arg]
    """Load the latest validation metrics JSON."""
    path = _latest("metrics", _VAL_DIR, ".json", smoke=smoke)
    return json.loads(path.read_text())


def load_crosswalk() -> dict[int, dict]:  # type: ignore[type-arg]
    """Parse fuel_crosswalk.yaml → {effis_code: {nffl_name, internal_class, severity}}."""
    raw = yaml.safe_load(_CROSSWALK.read_text())
    return {int(e["effis_code"]): e for e in raw["entries"]}


def load_aoi(*, smoke: bool) -> gpd.GeoDataFrame:
    """Load the AOI polygon."""
    return gpd.read_file(_AOI_SMOKE if smoke else _AOI_PILOT)


def raster_to_epsg4326(src_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Reproject raster band 1 to EPSG:4326, return (data, lons, lats, nodata).

    Returns 2-D arrays suitable for ``plt.pcolormesh``.
    """
    with rasterio.open(src_path) as src:
        src_crs = src.crs
        src_nodata = src.nodata if src.nodata is not None else 255.0
        if src_crs.to_epsg() == 4326:
            data = src.read(1).astype("float32")
            h, w = data.shape
            xs = np.linspace(src.bounds.left, src.bounds.right, w)
            ys = np.linspace(src.bounds.top, src.bounds.bottom, h)
            lons, lats = np.meshgrid(xs, ys)
            data[data == src_nodata] = float("nan")
            return data, lons, lats, src_nodata
        # Reproject to EPSG:4326
        dst_crs = CRS.from_epsg(4326)
        transform, width, height = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds
        )
        # width/height: rasterio stubs type as int|None; None is unreachable here.
        dst = np.full((height, width), fill_value=src_nodata, dtype="float32")  # pyright: ignore[reportArgumentType,reportCallIssue]
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src_crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=Resampling.nearest,
        )
        xs = np.array([transform.c + transform.a * (i + 0.5) for i in range(width)])  # pyright: ignore[reportArgumentType]
        ys = np.array([transform.f + transform.e * (j + 0.5) for j in range(height)])  # pyright: ignore[reportArgumentType]
        lons, lats = np.meshgrid(xs, ys)
        dst[dst == src_nodata] = float("nan")
        return dst, lons, lats, src_nodata


def raster_band2_to_epsg4326(src_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Same as raster_to_epsg4326 but reads band 2 (severity × 100)."""
    with rasterio.open(src_path) as src:
        src_crs = src.crs
        src_nodata = src.nodata if src.nodata is not None else 255.0
        if src_crs.to_epsg() == 4326:
            data = src.read(2).astype("float32")
            h, w = data.shape
            xs = np.linspace(src.bounds.left, src.bounds.right, w)
            ys = np.linspace(src.bounds.top, src.bounds.bottom, h)
            lons, lats = np.meshgrid(xs, ys)
            data[data == src_nodata] = float("nan")
            return data, lons, lats, src_nodata
        dst_crs = CRS.from_epsg(4326)
        transform, width, height = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds
        )
        dst = np.full((height, width), fill_value=src_nodata, dtype="float32")  # pyright: ignore[reportArgumentType,reportCallIssue]
        reproject(
            source=rasterio.band(src, 2),
            destination=dst,
            src_transform=src.transform,
            src_crs=src_crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=Resampling.nearest,
        )
        xs = np.array([transform.c + transform.a * (i + 0.5) for i in range(width)])  # pyright: ignore[reportArgumentType]
        ys = np.array([transform.f + transform.e * (j + 0.5) for j in range(height)])  # pyright: ignore[reportArgumentType]
        lons, lats = np.meshgrid(xs, ys)
        dst[dst == src_nodata] = float("nan")
        return dst, lons, lats, src_nodata


def _add_scale_bar(ax: matplotlib.axes.Axes, lat_ref: float, km: float = 5.0) -> None:
    """Add a simple km scale bar at the lower-left of the axes."""
    # Convert km to degrees longitude at the reference latitude
    deg_per_km_lon = 1.0 / (111.32 * np.cos(np.radians(lat_ref)))
    bar_lon = deg_per_km_lon * km
    x0, y0 = 0.07, 0.05  # axes fraction
    x1 = x0 + bar_lon / (ax.get_xlim()[1] - ax.get_xlim()[0])
    ax.annotate(
        "",
        xy=(x1, y0),
        xytext=(x0, y0),
        xycoords="axes fraction",
        textcoords="axes fraction",
        arrowprops={"arrowstyle": "-", "lw": 2, "color": "black"},
    )
    ax.annotate(
        f"{km:.0f} km",
        xy=((x0 + x1) / 2, y0 + 0.025),
        xycoords="axes fraction",
        ha="center",
        fontsize=7,
        color="black",
    )


def _add_attribution(ax: matplotlib.axes.Axes) -> None:
    ax.annotate(
        _ATTRIB,
        xy=(0.5, -0.04),
        xycoords="axes fraction",
        ha="center",
        va="top",
        fontsize=6,
        color="#555555",
    )


# --------------------------------------------------------------------------- #
# Figure 1 — exposure map (assets coloured by rank)
# --------------------------------------------------------------------------- #


def make_fig1(gdf: gpd.GeoDataFrame, aoi: gpd.GeoDataFrame, out: Path) -> None:
    """Assets coloured by exposure rank (viridis, 1 = highest exposure)."""
    fig, ax = plt.subplots(figsize=_FIGSIZE)

    # Background: fuel severity as light gray reference layer (already local)
    # (intentionally omitted — no S2 composite available without network fetch;
    # geographic context comes from asset positions and AOI outline)

    # AOI outline
    aoi.to_crs("EPSG:4326").boundary.plot(ax=ax, color="#444444", linewidth=0.8, linestyle="--")

    # Sort by rank descending so high-rank (most exposed) plots on top
    gdf_sorted = gdf.sort_values("exposure_rank", ascending=False)

    # Use centroids for all geometry types (lines/polygons → centroid).
    # Geographic CRS centroid warning is harmless for point-plot purposes.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        centroids = gdf_sorted.geometry.centroid

    sc = ax.scatter(
        centroids.x,
        centroids.y,
        c=gdf_sorted["rank_norm"],
        cmap="viridis",
        s=12,
        linewidths=0.2,
        edgecolors="#333333",
        alpha=0.85,
        zorder=3,
    )
    cbar = fig.colorbar(sc, ax=ax, fraction=0.03, pad=0.01)
    cbar.set_label("exposure rank (relative, AOI-normalised)\n1 = highest · 0 = lowest", fontsize=8)

    # Mark top-10 most exposed assets
    top10 = gdf_sorted.head(10)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _cx = top10.geometry.centroid.x
        _cy = top10.geometry.centroid.y
    ax.scatter(
        _cx,
        _cy,
        c="red",
        s=30,
        marker="*",
        zorder=4,
        label="Top-10 most exposed",
    )
    ax.legend(loc="upper right", fontsize=7)

    minx, miny, maxx, maxy = gdf_sorted.total_bounds
    ax.set_xlim(minx - 0.02, maxx + 0.02)
    ax.set_ylim(miny - 0.02, maxy + 0.02)
    _add_scale_bar(ax, (miny + maxy) / 2, km=5.0)

    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        "Critical infrastructure — wildfire exposure rank (pilot AOI, T₀ = 2024-12-31)",
        fontsize=10,
    )
    ax.text(
        0.5,
        1.01,
        "Rank is relative and AOI-normalised; it is not a fire-probability estimate.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7,
        color="#666666",
    )
    _add_attribution(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


# --------------------------------------------------------------------------- #
# Figure 2 — fuel layer
# --------------------------------------------------------------------------- #


def make_fig2(fuel_path: Path, crosswalk: dict[int, dict], out: Path) -> None:  # type: ignore[type-arg]
    """Fuel classes with NFFL class names in the legend."""
    data, lons, lats, _ = raster_to_epsg4326(fuel_path)

    # Collect codes present in this raster (excluding 0 = non-fuel, nan = nodata)
    flat = data[~np.isnan(data)].astype(int)
    codes_present = sorted({int(c) for c in flat if c != 0})

    # Build discrete colormap over the present codes
    n_codes = len(codes_present)
    cmap_base = plt.get_cmap("tab10", max(n_codes, 1))
    colors = [cmap_base(i) for i in range(n_codes)]

    # Map each code to an integer index for display
    code_to_idx = {code: i + 1 for i, code in enumerate(codes_present)}
    display = np.full_like(data, fill_value=np.nan)
    display[data == 0] = 0  # non-fuel → index 0
    for code, idx in code_to_idx.items():
        display[data == code] = idx

    all_colors = ["#cccccc", *colors]  # index 0 = non-fuel
    cmap = ListedColormap(all_colors)
    bounds = list(range(len(all_colors) + 1))
    norm = BoundaryNorm(bounds, cmap.N)

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.pcolormesh(lons, lats, display, cmap=cmap, norm=norm, shading="auto")

    # Legend patches
    patches = [mpatches.Patch(color="#cccccc", label="Non-fuel (0)")]
    for code in codes_present:
        entry = crosswalk.get(code, {})
        label = f"NFFL {code}: {entry.get('nffl_name', '?')} ({entry.get('internal_class', '?')})"
        patches.append(mpatches.Patch(color=cmap_base(code_to_idx[code] - 1), label=label))

    ax.legend(handles=patches, loc="upper right", fontsize=6, framealpha=0.85)
    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        "Fuel-class layer (EFFIS NFFL-13 + DGT COSc crosswalk)",
        fontsize=10,
    )
    ax.text(
        0.5,
        1.01,
        "Effective resolution ≈ 250 m (EFFIS native); COSc refinement at 10 m grid spacing.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7,
        color="#666666",
    )
    _add_attribution(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


# --------------------------------------------------------------------------- #
# Figure 3 — burn-scar composite
# --------------------------------------------------------------------------- #


def make_fig3(burn_path: Path, out: Path) -> None:
    """Burn-scar max-probability composite with 0.5 threshold contour."""
    data, lons, lats, _ = raster_to_epsg4326(burn_path)

    # Downsample to cap the pixel count (keeps file size under 2 MB).
    step = max(1, data.shape[0] // 600)
    data = data[::step, ::step]
    lons = lons[::step, ::step]
    lats = lats[::step, ::step]

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    im = ax.pcolormesh(
        lons,
        lats,
        data,
        cmap="YlOrRd",
        vmin=0.0,
        vmax=1.0,
        shading="auto",
    )
    cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.01)
    cbar.set_label("Prithvi burn-scar probability (per-pixel, max composite)", fontsize=8)

    # 0.5 threshold contour — suppress UserWarning for all-NaN slices
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            ax.contour(lons, lats, data, levels=[0.5], colors=["blue"], linewidths=0.8)
            ax.plot([], [], color="blue", linewidth=0.8, label="p = 0.50 threshold")
            ax.legend(loc="upper right", fontsize=7)
        except Exception:
            pass

    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        "Prithvi-EO-2.0 burn-scar probability composite\n"
        "(12-month trailing window, T₀ = 2026-06-09)",
        fontsize=10,
    )
    ax.text(
        0.5,
        1.01,
        "Max-composite over 179 Sentinel-2 scenes; values represent max single-scene probability, "
        "not frequency. Use as a relative rank input, not a threshold.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7,
        color="#666666",
        wrap=True,
    )
    _add_attribution(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


# --------------------------------------------------------------------------- #
# Figure 4 — exposure map with ICNF overlay
# --------------------------------------------------------------------------- #


def make_fig4(
    gdf: gpd.GeoDataFrame,
    burns: gpd.GeoDataFrame,
    aoi: gpd.GeoDataFrame,
    val_year: int,
    out: Path,
) -> None:
    """Exposure rank (dimmed) with ICNF validation-year burn perimeters on top."""
    fig, ax = plt.subplots(figsize=_FIGSIZE)

    aoi.to_crs("EPSG:4326").boundary.plot(ax=ax, color="#444444", linewidth=0.8, linestyle="--")

    # Assets as dimmed scatter
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        centroids = gdf.geometry.centroid
    ax.scatter(
        centroids.x,
        centroids.y,
        c=gdf["rank_norm"],
        cmap="viridis",
        s=8,
        alpha=0.4,
        linewidths=0,
        zorder=2,
    )

    # ICNF validation perimeters — filter to validation year
    burns_year = burns[burns["vintage_year"] == val_year].copy()
    if not burns_year.empty:
        burns_year.boundary.plot(
            ax=ax,
            color="#e31a1c",
            linewidth=1.2,
            zorder=4,
            label=f"ICNF burn perimeters {val_year} ({len(burns_year)} polygons)",
        )
        burns_year.plot(
            ax=ax,
            facecolor="#e31a1c",
            alpha=0.18,
            edgecolors="none",
            zorder=3,
        )
    else:
        ax.text(
            0.5,
            0.5,
            f"No ICNF burns in AOI for {val_year}",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=9,
            color="gray",
        )

    ax.legend(loc="upper right", fontsize=7)
    minx, miny, maxx, maxy = gdf.total_bounds
    ax.set_xlim(minx - 0.02, maxx + 0.02)
    ax.set_ylim(miny - 0.02, maxy + 0.02)
    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        f"Exposure rank vs ICNF burn perimeters {val_year} "
        f"(assets dimmed; red outline = validated burn)",
        fontsize=10,
    )
    ax.text(
        0.5,
        1.01,
        "ICNF perimeters are strictly after the score's input window (T₀ = 2024-12-31). "
        "Red = area that burned; asset colour = exposure rank.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7,
        color="#666666",
    )
    _add_attribution(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


# --------------------------------------------------------------------------- #
# Figure 5 — lift curve
# --------------------------------------------------------------------------- #


def make_fig5(metrics: dict, out: Path) -> None:  # type: ignore[type-arg]
    """Cumulative lift curves (full config vs ablation) from WU-7 metrics JSON."""
    fig, ax = plt.subplots(figsize=(8, 5))

    full = metrics["full"]
    ablation = metrics["ablation"]
    val_years = metrics.get("validation_years", [])

    if full.get("degenerate") or "lift_table" not in full:
        # Degenerate case (smoke tile: no burned assets) — show informative placeholder
        n = full.get("n", 0)
        ax.text(
            0.5,
            0.5,
            f"Degenerate case: {full.get('n_burned', 0)} burned assets in this AOI\n"
            f"(n = {n}, base rate = 0.0).\n"
            "Lift curve produced from pilot parquet; see docs/validation_report.md.",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=10,
            color="gray",
        )
        ax.set_title(
            "Lift curve (smoke AOI — degenerate; use pilot for real evaluation)",
            fontsize=10,
        )
        _add_attribution(ax)
        fig.tight_layout()
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  saved {out.name} (degenerate placeholder)")
        return

    deciles = [d["decile"] for d in full["lift_table"]]
    full_lift = [d["cumulative_lift"] for d in full["lift_table"]]
    abl_lift = [d["cumulative_lift"] for d in ablation["lift_table"]]

    ax.plot(
        deciles, full_lift, "o-", color="#1f77b4", linewidth=1.8, label="Full config (6 features)"
    )
    ax.plot(
        deciles,
        abl_lift,
        "s--",
        color="#ff7f0e",
        linewidth=1.8,
        label="Ablation (burn-history features removed)",
    )
    ax.axhline(1.0, color="gray", linewidth=0.8, linestyle=":", label="Baseline (random rank)")

    ax.set_xlabel("Decile (top → bottom of exposure rank)", fontsize=9)
    ax.set_ylabel("Cumulative lift over base rate", fontsize=9)
    year_label = str(val_years[0]) if val_years else "?"
    ax.set_title(
        f"Cumulative lift: exposure rank vs subsequent ICNF burns "
        f"({year_label} perimeters, "
        f"n = {full['n']:,} assets, {full['n_burned']} burned)",
        fontsize=10,
    )
    ax.set_xticks(deciles)
    ax.set_xticklabels([f"{d}" for d in deciles], fontsize=8)
    ax.legend(fontsize=8)

    n_burned = full["n_burned"]
    ax.text(
        0.5,
        0.03,
        f"With only {n_burned} burned assets, a single asset moving deciles changes "
        "lift by 2.00×.\nThis run does not resolve which features carry the signal. "
        "Read docs/validation_report.md.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7,
        color="#666666",
    )
    _add_attribution(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


# --------------------------------------------------------------------------- #
# HTML map
# --------------------------------------------------------------------------- #


def _top3_features(feat_dict: dict, weights: dict) -> str:  # type: ignore[type-arg]
    """Return HTML describing the top-3 contributing features by score contribution."""
    scored = {}
    for name, val in feat_dict.items():
        if isinstance(val, int | float) and not np.isnan(val):
            w = weights.get(name, 0.0)
            scored[name] = val * w
    top3 = sorted(scored.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
    lines = []
    for name, contrib in top3:
        raw_val = feat_dict.get(name, float("nan"))
        lines.append(f"<li><b>{name}</b>: {raw_val:.3g} (contrib {contrib:.3g})</li>")
    return "<ul>" + "".join(lines) + "</ul>" if lines else ""


def make_html_map(
    gdf: gpd.GeoDataFrame,
    fuel_path: Path,
    burns: gpd.GeoDataFrame,
    aoi: gpd.GeoDataFrame,
    out: Path,
) -> None:
    """Folium self-contained interactive HTML map."""
    import base64
    import io

    import folium
    from folium.plugins import MeasureControl

    cfg = load_exposure_config(_SCORE_YAML)
    weights: dict[str, float] = dict(cfg.weights)

    # Centre on AOI centroid
    bounds = gdf.total_bounds  # [minx, miny, maxx, maxy]
    centre = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    m = folium.Map(location=centre, zoom_start=11, tiles="OpenStreetMap")
    MeasureControl(position="topright").add_to(m)

    # ---------- Assets layer ----------
    cmap = plt.get_cmap("viridis")
    assets_group = folium.FeatureGroup(name="Assets (coloured by exposure rank)", show=True)

    gdf_sorted = gdf.sort_values("exposure_rank", ascending=False)
    for _, row in gdf_sorted.iterrows():
        try:
            feat_dict = json.loads(str(row["features"]))
        except Exception:
            feat_dict = {}
        rank = int(row["exposure_rank"])
        norm_rank = float(row["rank_norm"])
        rgba = cmap(norm_rank)
        colour = f"#{int(rgba[0] * 255):02x}{int(rgba[1] * 255):02x}{int(rgba[2] * 255):02x}"
        top3_html = _top3_features(feat_dict, weights)
        popup_html = (
            f"<b>{row['asset_id']}</b><br>"
            f"Class: {row['asset_class']}<br>"
            f"Exposure rank: <b>{rank}</b> / {len(gdf)}<br>"
            f"Score: {row['exposure_score']:.3f}<br>"
            f"<hr>Top-3 feature contributions:{top3_html}"
            "<small style='color:#888'>"
            "Rank is relative (AOI-normalised); not a probability.</small>"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            centroid = row.geometry.centroid
        folium.CircleMarker(
            location=[centroid.y, centroid.x],
            radius=5 if rank <= len(gdf) // 10 else 3,
            color=colour,
            fill=True,
            fill_color=colour,
            fill_opacity=0.8,
            weight=0.5,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"{row['asset_class']} | rank {rank}",
        ).add_to(assets_group)
    assets_group.add_to(m)

    # ---------- Fuel layer (image overlay) ----------
    data, lons, lats, _ = raster_to_epsg4326(fuel_path)
    # Build RGBA image for folium overlay
    valid_mask = ~np.isnan(data)
    display = np.zeros((*data.shape, 4), dtype=np.uint8)
    # Non-fuel (code 0): light gray, semi-transparent
    nonfuel_mask = valid_mask & (data == 0)
    display[nonfuel_mask] = [180, 180, 180, 80]
    # Fuel codes: coloured by code index (crosswalk loaded for fig2 only)
    fuel_codes = sorted({int(c) for c in data[valid_mask] if c != 0})
    if fuel_codes:
        code_cmap = plt.get_cmap("tab10", len(fuel_codes))
        # Build integer version once (fill NaN with -1 before cast)
        data_int = np.where(valid_mask, data, -1.0).astype(np.int32)
        for i, code in enumerate(fuel_codes):
            mask = data_int == code
            rgba = code_cmap(i)
            display[mask] = [int(rgba[0] * 255), int(rgba[1] * 255), int(rgba[2] * 255), 160]

    if lons.size > 0:
        lat_min = float(lats.min())
        lat_max = float(lats.max())
        lon_min = float(lons.min())
        lon_max = float(lons.max())
        buf = io.BytesIO()
        plt.imsave(buf, display, format="png")
        buf.seek(0)
        img_b64 = base64.b64encode(buf.read()).decode()
        fuel_group = folium.FeatureGroup(name="Fuel-class layer (EFFIS + COSc)", show=False)
        folium.raster_layers.ImageOverlay(  # pyright: ignore[reportAttributeAccessIssue]
            image=f"data:image/png;base64,{img_b64}",
            bounds=[[lat_min, lon_min], [lat_max, lon_max]],
            opacity=0.6,
            name="Fuel layer",
        ).add_to(fuel_group)
        fuel_group.add_to(m)

    # ---------- ICNF perimeters layer ----------
    if not burns.empty:
        icnf_group = folium.FeatureGroup(name="ICNF burn perimeters (all vintages)", show=False)
        for _, row in burns.iterrows():
            year = int(row["vintage_year"])
            colour = "#e31a1c" if year >= 2024 else "#fd8d3c"
            try:
                gjson = row.geometry.__geo_interface__
                folium.GeoJson(
                    gjson,
                    style_function=lambda _f, c=colour: {
                        "fillColor": c,
                        "color": c,
                        "weight": 1,
                        "fillOpacity": 0.2,
                    },
                    tooltip=f"ICNF {year}",
                ).add_to(icnf_group)
            except Exception:
                pass
        icnf_group.add_to(m)

    # ---------- AOI outline ----------
    aoi_group = folium.FeatureGroup(name="AOI boundary", show=True)
    for _, row in aoi.iterrows():
        with contextlib.suppress(Exception):
            folium.GeoJson(
                row.geometry.__geo_interface__,
                style_function=lambda _f: {
                    "fillColor": "none",
                    "color": "#333333",
                    "weight": 1.5,
                    "dashArray": "5 5",
                },
            ).add_to(aoi_group)
    aoi_group.add_to(m)

    folium.LayerControl().add_to(m)

    # Footer with attribution
    attrib_html = (
        "<div style='position:fixed;bottom:10px;left:50%;transform:translateX(-50%);"
        "background:rgba(255,255,255,0.85);padding:4px 10px;font-size:10px;"
        "border-radius:4px;z-index:9999;'>" + _ATTRIB + "</div>"
    )
    m.get_root().html.add_child(folium.Element(attrib_html))  # pyright: ignore[reportAttributeAccessIssue]

    m.save(str(out))
    size_mb = out.stat().st_size / 1e6
    print(f"  saved {out.name} ({size_mb:.1f} MB)")
    if size_mb > 25:
        print(f"  WARNING: {out.name} exceeds 25 MB sanity limit ({size_mb:.1f} MB)")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--smoke", action="store_true", help="Use smoke AOI artefacts (fast gate check)"
    )
    args = parser.parse_args()
    smoke: bool = args.smoke

    _FIGS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Loading artefacts (smoke={smoke}) …")
    gdf = load_exposure(smoke=smoke)
    fuel_path = load_fuel_cog_path(smoke=smoke)
    burn_path = load_burn_scar_cog_path(smoke=smoke)
    burns = load_burns(smoke=smoke)
    metrics = load_metrics(smoke=smoke)
    aoi = load_aoi(smoke=smoke)
    crosswalk = load_crosswalk()

    val_years = metrics.get("validation_years", [])
    val_year: int = int(val_years[0]) if val_years else int(burns["vintage_year"].max())

    suffix = "_smoke" if smoke else ""

    print("Generating figures …")

    make_fig1(gdf, aoi, _FIGS_DIR / f"fig1_exposure_map{suffix}.png")
    make_fig2(fuel_path, crosswalk, _FIGS_DIR / f"fig2_fuel_layer{suffix}.png")
    make_fig3(burn_path, _FIGS_DIR / f"fig3_burn_scar{suffix}.png")
    make_fig4(gdf, burns, aoi, val_year, _FIGS_DIR / f"fig4_icnf_overlay{suffix}.png")
    make_fig5(metrics, _FIGS_DIR / f"fig5_lift_curve{suffix}.png")
    make_html_map(gdf, fuel_path, burns, aoi, _FIGS_DIR / "exposure_map.html")

    print("\nDone. Artefacts under docs/figures/:")
    for f in sorted(_FIGS_DIR.iterdir()):
        size = f.stat().st_size
        unit = "kB" if size < 1_000_000 else "MB"
        val = size / 1_000 if size < 1_000_000 else size / 1_000_000
        print(f"  {f.name}  ({val:.0f} {unit})")

    # Sanity: verify no prohibited language in output files.
    # "probability" alone is allowed (Prithvi outputs per-pixel probability);
    # prohibited forms are those that imply calibrated fire-risk forecasts.
    prohibited = ("risk probability", "chance of fire", "fire probability", "risk score")
    all_clean = True
    for f in _FIGS_DIR.glob("*.html"):
        text = f.read_text(errors="replace").lower()
        for term in prohibited:
            if term in text:
                print(f"  WARN: '{term}' found in {f.name} — review caption language")
                all_clean = False
    if all_clean:
        print("  Language check: no prohibited risk-probability terms found.")


if __name__ == "__main__":
    main()
