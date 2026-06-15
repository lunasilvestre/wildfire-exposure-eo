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
from matplotlib.colors import BoundaryNorm, ListedColormap, Normalize
from matplotlib.patheffects import withStroke
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

_DIAG_DIR = _ROOT / "outputs" / "diagnostics"

_FIGSIZE = (10, 7)
_SEED = 42
rng = np.random.default_rng(_SEED)

# Canonical de-grid p85 burn-scar composite (WU-10) — the published recent-scar
# detector. Pinned explicitly because the lexical-latest glob would otherwise
# pick the pre-de-grid `burn_scar_wu10multi_p85_*` run; this is the adopted COG
# (run 20260615T192025Z, tile_origin_jitter=true). Its provenance sidecar carries
# a different stem, so it is resolved separately in load_burn_scar_pilot().
_BURN_SCAR_DEGRID_COG = "burn_scar_20260615T192025Z.tif"
_BURN_SCAR_DEGRID_SIDECAR = "burn_scar_wu10degrid_p85_20260615T192025Z.json"

# Site display rule for the burn-scar layer (mirrors docs/app/app.js and
# scripts/17): transparent below ALPHA_FLOOR, then opacity ramps as
# BASE + (1-BASE) * t**GAMMA with t = (p - floor) / (1 - floor). Keep in sync.
_ALPHA_FLOOR = 0.25
_ALPHA_BASE = 0.30
_ALPHA_GAMMA = 0.6

# Recent multi-year ICNF vintages used as the burn-scar detection truth.
_TRUTH_YEARS = (2023, 2024, 2025)

# Municipality seats of the pilot AOI (Aveiro, PT-01) — public town-centre
# coordinates, ordered as they sit in the AOI (NW → S). Used only to label the
# three named municipalities on the headline map; not an analysis input.
_MUNICIPALITIES: tuple[tuple[str, float, float], ...] = (
    ("Oliveira de Azeméis", -8.4773, 40.8404),
    ("Albergaria-a-Velha", -8.4814, 40.6919),
    ("Sever do Vouga", -8.3686, 40.7300),
)

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


def load_burn_scar_pilot() -> tuple[Path, dict]:  # type: ignore[type-arg]
    """Resolve the canonical de-grid p85 burn-scar COG and its provenance sidecar.

    Returns ``(cog_path, sidecar_dict)``. Pinned to the adopted WU-10 run; the
    COG and its sidecar carry different stems, so we resolve the sidecar by name
    rather than ``cog_path.with_suffix('.json')``. Falls back to the lexical
    latest only if the pinned COG is absent (keeps the script runnable on a
    partial checkout).
    """
    cog = _COG_DIR / _BURN_SCAR_DEGRID_COG
    sidecar = _COG_DIR / _BURN_SCAR_DEGRID_SIDECAR
    if cog.exists() and sidecar.exists():
        return cog, json.loads(sidecar.read_text())
    cog = load_burn_scar_cog_path(smoke=False)
    return cog, json.loads(cog.with_suffix(".json").read_text())


def load_multiyear_detection(*, smoke: bool) -> dict:  # type: ignore[type-arg]
    """Load the latest WU-10 multi-year burn-scar detection metrics JSON.

    Produced by ``scripts/16_burn_scar_multiyear_validate.py`` — the recent-scar
    detection validation (no leakage gate; that gate binds the forecasting score,
    not detection). Returns the parsed payload; raises if none is present.
    """
    pat = "16_multiyear_detection_smoke.json" if smoke else "16_multiyear_detection_[0-9]*.json"
    cands = sorted(_DIAG_DIR.glob(pat))
    if not smoke:
        cands = [c for c in cands if "_smoke" not in c.name]
    if not cands:
        raise FileNotFoundError(
            f"No artefact matching {pat!r} in {_DIAG_DIR}. "
            "Run scripts/16_burn_scar_multiyear_validate.py first."
        )
    return json.loads(cands[-1].read_text())


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


def _add_north_arrow(ax: matplotlib.axes.Axes) -> None:
    """Small north arrow at the upper-left of the axes (axes-fraction coords)."""
    x, y = 0.045, 0.88
    ax.annotate(
        "N",
        xy=(x, y),
        xytext=(x, y - 0.075),
        xycoords="axes fraction",
        textcoords="axes fraction",
        ha="center",
        va="center",
        fontsize=9,
        fontweight="bold",
        color="#222222",
        arrowprops={"arrowstyle": "-|>", "lw": 1.6, "color": "#222222"},
        path_effects=[withStroke(linewidth=2.5, foreground="white")],
    )


def _label_municipalities(ax: matplotlib.axes.Axes, bounds: tuple[float, ...]) -> None:
    """Label the three AOI municipalities at their public town-centre seats.

    Only labels seats that fall inside the current axes extent so smoke-AOI
    variants (a 1 km tile) are not cluttered with off-frame labels.
    """
    minx, miny, maxx, maxy = bounds
    for name, lon, lat in _MUNICIPALITIES:
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            continue
        ax.plot(
            lon,
            lat,
            marker="o",
            markersize=4,
            markerfacecolor="white",
            markeredgecolor="#222222",
            markeredgewidth=0.8,
            zorder=6,
        )
        ax.annotate(
            name,
            xy=(lon, lat),
            xytext=(4, 4),
            textcoords="offset points",
            fontsize=7.5,
            fontweight="bold",
            color="#1a1a1a",
            zorder=6,
            path_effects=[withStroke(linewidth=2.2, foreground="white")],
        )


def _value_driven_rgba(data: np.ndarray, cmap_name: str = "YlOrRd") -> np.ndarray:
    """Burn-scar colours with the site's value-driven alpha (transparent < floor).

    Mirrors docs/app/app.js and scripts/17: hue from *cmap_name* over [0, 1];
    alpha 0 below ``_ALPHA_FLOOR``, then ``_ALPHA_BASE + (1-_ALPHA_BASE)*t**GAMMA``
    with ``t = (p - floor)/(1 - floor)``. NaN → fully transparent.
    """
    cmap = plt.get_cmap(cmap_name)
    norm = Normalize(vmin=0.0, vmax=1.0)
    rgba = cmap(norm(np.nan_to_num(data, nan=0.0)))
    span = max(1.0 - _ALPHA_FLOOR, 1e-6)
    t = np.clip((data - _ALPHA_FLOOR) / span, 0.0, 1.0) ** _ALPHA_GAMMA
    alpha = _ALPHA_BASE + (1.0 - _ALPHA_BASE) * t
    alpha[(data < _ALPHA_FLOOR) | ~np.isfinite(data)] = 0.0
    rgba[..., 3] = alpha
    return rgba


def _draw_context_base(
    ax: matplotlib.axes.Axes,
    aoi: gpd.GeoDataFrame,
    fuel_path: Path | None,
) -> None:
    """Soft geographic backdrop from the repo's own layers (no slippy tiles).

    Fills the AOI with a faint land tone and, when a fuel COG is available,
    paints fuelled (vegetated) land as a muted green wash so the map reads as a
    landscape rather than dots on white. Non-fuel / nodata stays neutral. All
    explicit EPSG:4326.
    """
    aoi_4326 = aoi.to_crs("EPSG:4326")
    aoi_4326.plot(ax=ax, facecolor="#f3f1ea", edgecolor="none", zorder=0)
    if fuel_path is not None:
        loaded: tuple[np.ndarray, np.ndarray, np.ndarray, float] | None
        try:
            loaded = raster_to_epsg4326(fuel_path)
        except Exception:
            loaded = None
        if loaded is not None:
            data, lons, lats, _ = loaded
            # Fuelled land (any non-zero fuel code) → faint green; else transparent.
            veg = np.isfinite(data) & (data > 0)
            shade = np.full((*data.shape, 4), 0.0, dtype=float)
            shade[veg] = (0.62, 0.71, 0.55, 0.35)  # muted sage, low opacity
            extent = (
                float(lons.min()),
                float(lons.max()),
                float(lats.min()),
                float(lats.max()),
            )
            ax.imshow(
                shade,
                extent=extent,
                origin="upper",
                interpolation="nearest",
                zorder=1,
            )
    aoi_4326.boundary.plot(ax=ax, color="#444444", linewidth=1.0, linestyle="--", zorder=5)


# --------------------------------------------------------------------------- #
# Figure 1 — exposure map (assets coloured by rank)
# --------------------------------------------------------------------------- #


def make_fig1(
    gdf: gpd.GeoDataFrame,
    aoi: gpd.GeoDataFrame,
    out: Path,
    *,
    aoi_label: str,
    t0: str,
    fuel_path: Path | None = None,
) -> None:
    """Headline map: critical-infrastructure assets coloured by exposure rank.

    Geographic context is built from the repo's own layers (no slippy-map
    tiles): the AOI is filled and outlined, fuelled land is a muted green wash
    from the fuel COG, and the three pilot municipalities are labelled at their
    public town-centre seats. The most-exposed decile is emphasised. *aoi_label*
    and *t0* come from the loaded artefacts, never hardcoded.
    """
    fig, ax = plt.subplots(figsize=_FIGSIZE)

    # Soft geographic backdrop from existing repo data (AOI fill + fuel wash).
    _draw_context_base(ax, aoi, fuel_path)

    # Sort by rank descending so high-rank (most exposed) plots on top.
    gdf_sorted = gdf.sort_values("exposure_rank", ascending=False)
    n = len(gdf_sorted)
    decile_cut = max(1, n // 10)

    # Use centroids for all geometry types (lines/polygons → centroid).
    # Geographic-CRS centroid warning is harmless for point-plot purposes.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        cx = gdf_sorted.geometry.centroid.x.to_numpy()
        cy = gdf_sorted.geometry.centroid.y.to_numpy()

    # Top decile drawn larger with a white halo so it pops off the basemap.
    is_top = (gdf_sorted["exposure_rank"] <= decile_cut).to_numpy()
    sizes: list[float] = [34.0 if t else 11.0 for t in is_top]
    edges: list[str] = ["white" if t else "#33333355" for t in is_top]
    lws: list[float] = [0.7 if t else 0.2 for t in is_top]

    sc = ax.scatter(
        cx,
        cy,
        c=gdf_sorted["rank_norm"],
        cmap="viridis",
        s=sizes,
        linewidths=lws,
        edgecolors=edges,
        alpha=0.9,
        zorder=3,
    )
    cbar = fig.colorbar(sc, ax=ax, fraction=0.035, pad=0.01)
    cbar.set_label(
        "wildfire exposure rank (relative, within AOI)\nmost exposed (rank 1) → least exposed",
        fontsize=8,
    )
    cbar.set_ticks([0.0, 1.0])
    cbar.set_ticklabels(["least", "most"])

    # Star the top-10 to anchor the eye, then describe the decile in the legend.
    top10 = gdf_sorted.head(10)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _tx = top10.geometry.centroid.x
        _ty = top10.geometry.centroid.y
    ax.scatter(
        _tx,
        _ty,
        facecolors="none",
        edgecolors="#d7191c",
        s=110,
        marker="*",
        linewidths=1.1,
        zorder=4,
        label="Top-10 most exposed",
    )
    ax.scatter(
        [],
        [],
        s=34,
        c="#440154",
        edgecolors="white",
        linewidths=0.7,
        label=f"Top decile (top {decile_cut} of {n:,})",
    )
    leg = ax.legend(loc="lower left", fontsize=7, framealpha=0.9)
    leg.set_zorder(7)

    # Frame on the AOI (where the context base, fuel wash and municipality
    # labels live) padded slightly — this is the AOI-relative analysis frame.
    # A few line-asset centroids fall just outside; the AOI is the honest extent.
    abx = aoi.to_crs("EPSG:4326").total_bounds
    minx, miny, maxx, maxy = abx
    padx = (maxx - minx) * 0.04
    pady = (maxy - miny) * 0.04
    ax.set_xlim(minx - padx, maxx + padx)
    ax.set_ylim(miny - pady, maxy + pady)
    ax.set_aspect("auto")
    _label_municipalities(ax, (minx, miny, maxx, maxy))
    _add_scale_bar(ax, (miny + maxy) / 2, km=5.0)
    _add_north_arrow(ax)

    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        f"Where is critical infrastructure most exposed to wildfire? ({aoi_label})",
        fontsize=12,
        fontweight="bold",
        pad=20,  # leave room for the subtitle line at y=1.01
    )
    ax.text(
        0.5,
        1.012,
        f"{len(gdf):,} OSM assets ranked by a transparent open-data exposure score "
        f"(inputs ≤ {t0}). Rank is relative within the AOI — not a fire probability.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7.5,
        color="#555555",
    )
    _add_attribution(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


# --------------------------------------------------------------------------- #
# Figure 2 — fuel layer
# --------------------------------------------------------------------------- #


def make_fig2(
    fuel_path: Path,
    crosswalk: dict[int, dict],  # type: ignore[type-arg]
    out: Path,
    *,
    aoi: gpd.GeoDataFrame | None = None,
) -> None:
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
    ax.pcolormesh(lons, lats, display, cmap=cmap, norm=norm, shading="auto", zorder=1)

    if aoi is not None:
        aoi.to_crs("EPSG:4326").boundary.plot(
            ax=ax, color="#222222", linewidth=1.0, linestyle="--", zorder=3
        )
        _label_municipalities(ax, (lons.min(), lats.min(), lons.max(), lats.max()))
        _add_scale_bar(ax, float((lats.min() + lats.max()) / 2), km=5.0)
        _add_north_arrow(ax)

    # Legend patches
    patches = [mpatches.Patch(color="#cccccc", label="Non-fuel (0)")]
    for code in codes_present:
        entry = crosswalk.get(code, {})
        label = f"NFFL {code}: {entry.get('nffl_name', '?')} ({entry.get('internal_class', '?')})"
        patches.append(mpatches.Patch(color=cmap_base(code_to_idx[code] - 1), label=label))

    leg = ax.legend(handles=patches, loc="upper right", fontsize=6, framealpha=0.9)
    leg.set_zorder(5)
    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        "Fuel-class layer (EFFIS NFFL-13 + DGT COSc crosswalk)",
        fontsize=12,
        fontweight="bold",
        pad=18,  # leave room for the caveat line at y=1.01
    )
    ax.text(
        0.5,
        1.01,
        "Effective resolution ≈ 250 m (EFFIS native); COSc refinement at 10 m grid spacing.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7.5,
        color="#555555",
    )
    _add_attribution(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {out.name}")


# --------------------------------------------------------------------------- #
# Figure 3 — burn-scar composite
# --------------------------------------------------------------------------- #


def make_fig3(
    burn_path: Path,
    sidecar: dict,  # type: ignore[type-arg]
    aoi: gpd.GeoDataFrame,
    burns: gpd.GeoDataFrame,
    out: Path,
    *,
    reducer_label: str = "de-grid p85",
) -> None:
    """De-grid p85 burn-scar composite, rendered with the site display rule.

    The analysis COG stays a continuous relative-score raster; only the
    rendering changes (value-driven alpha: transparent below the floor, opacity
    ramping to full). The ICNF 2023-2025 recent-burn perimeters are overlaid as
    the detection truth. Scene count / window come from the COG provenance
    sidecar (never hardcoded). All explicit EPSG:4326.
    """
    n_scenes = len(sidecar.get("s2_item_ids", []))
    window_start = sidecar.get("window_start", "?")
    window_end = sidecar.get("window_end", "?")

    data, lons, lats, _ = raster_to_epsg4326(burn_path)
    # Downsample to cap the pixel count (keeps file size modest).
    step = max(1, data.shape[0] // 700)
    data = data[::step, ::step]
    lons = lons[::step, ::step]
    lats = lats[::step, ::step]

    fig, ax = plt.subplots(figsize=_FIGSIZE)

    # Soft context: AOI fill + outline (no fuel wash here — the scar layer needs
    # an uncluttered, light backdrop so genuine scars read against it).
    aoi_4326 = aoi.to_crs("EPSG:4326")
    aoi_4326.plot(ax=ax, facecolor="#f3f1ea", edgecolor="none", zorder=0)

    # Value-driven alpha rendering (mirrors the site / scripts/17).
    rgba = _value_driven_rgba(data)
    extent = (float(lons.min()), float(lons.max()), float(lats.min()), float(lats.max()))
    ax.imshow(rgba, extent=extent, origin="upper", interpolation="nearest", zorder=2)

    aoi_4326.boundary.plot(ax=ax, color="#444444", linewidth=1.0, linestyle="--", zorder=3)

    # ICNF recent-burn truth (2023-2025) overlaid as outlines.
    truth = burns[burns["vintage_year"].isin(_TRUTH_YEARS)].to_crs("EPSG:4326")
    if not truth.empty:
        truth.boundary.plot(
            ax=ax,
            color="#1f4fd8",
            linewidth=0.9,
            zorder=4,
            label=(
                f"ICNF burns {_TRUTH_YEARS[0]}–{_TRUTH_YEARS[-1]} (truth, {len(truth)} polygons)"
            ),
        )
        ax.legend(loc="upper right", fontsize=7, framealpha=0.9)

    # Colorbar reflects the YlOrRd hue (alpha is handled by the display rule).
    sm = plt.cm.ScalarMappable(cmap=plt.get_cmap("YlOrRd"), norm=Normalize(vmin=0.0, vmax=1.0))
    cbar = fig.colorbar(sm, ax=ax, fraction=0.035, pad=0.01)
    cbar.set_label(
        f"burn-scar inference score (relative; shown ≥ {_ALPHA_FLOOR:g} only)",
        fontsize=8,
    )

    _label_municipalities(ax, (extent[0], extent[2], extent[1], extent[3]))
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("auto")
    _add_scale_bar(ax, (extent[2] + extent[3]) / 2, km=5.0)
    _add_north_arrow(ax)

    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        "Recent burn scars detected by Prithvi-EO-2.0 (Sentinel-2)",
        fontsize=12,
        fontweight="bold",
        pad=22,
    )
    ax.text(
        0.5,
        1.012,
        f"{reducer_label} composite over {n_scenes} scenes "
        f"({window_start} → {window_end}); the detector fires inside the recent ICNF "
        "burns. Burn scars of fires that already happened — not ignition prediction.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=7.5,
        color="#555555",
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
    *,
    t0: str,
    fuel_path: Path | None = None,
) -> None:
    """Exposure rank (dimmed) with ICNF validation-year burn perimeters on top.

    *t0* (score input-window end) comes from the metrics JSON, never hardcoded.
    """
    fig, ax = plt.subplots(figsize=_FIGSIZE)

    _draw_context_base(ax, aoi, fuel_path)

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
        alpha=0.45,
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

    leg = ax.legend(loc="lower left", fontsize=7, framealpha=0.9)
    leg.set_zorder(7)
    # Frame on the AOI (the analysis frame, where the context base lives).
    minx, miny, maxx, maxy = aoi.to_crs("EPSG:4326").total_bounds
    padx = (maxx - minx) * 0.04
    pady = (maxy - miny) * 0.04
    ax.set_xlim(minx - padx, maxx + padx)
    ax.set_ylim(miny - pady, maxy + pady)
    ax.set_aspect("auto")
    _label_municipalities(ax, (minx, miny, maxx, maxy))
    _add_scale_bar(ax, (miny + maxy) / 2, km=5.0)
    _add_north_arrow(ax)
    ax.set_xlabel("Longitude (°E)", fontsize=8)
    ax.set_ylabel("Latitude (°N)", fontsize=8)
    ax.set_title(
        f"Exposure rank vs ICNF burn perimeters {val_year} "
        f"(assets dimmed; red outline = validated burn)",
        fontsize=12,
        fontweight="bold",
        pad=18,  # leave room for the caveat line at y=1.01
    )
    ax.text(
        0.5,
        1.01,
        f"ICNF perimeters are strictly after the score's input window (T₀ = {t0}). "
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


def _pick_detection_window(detection: dict, label: str) -> dict | None:  # type: ignore[type-arg]
    """Return the detection window dict matching *label* (e.g. '2023-2025')."""
    for w in detection.get("windows", []):
        if w.get("label") == label:
            return w
    return None


def _draw_lift_panel(ax: matplotlib.axes.Axes, metrics: dict) -> None:  # type: ignore[type-arg]
    """Small companion panel: cumulative lift of the forecasting exposure rank."""
    full = metrics.get("full", {})
    ablation = metrics.get("ablation", {})
    val_years = metrics.get("validation_years", [])
    if full.get("degenerate") or "lift_table" not in full:
        ax.text(
            0.5,
            0.5,
            "Lift panel needs the pilot\nvalidation metrics (degenerate here).",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=8,
            color="gray",
        )
        ax.set_title("Forecasting screen — cumulative lift", fontsize=9)
        return

    deciles = [d["decile"] for d in full["lift_table"]]
    full_lift = [d["cumulative_lift"] for d in full["lift_table"]]
    abl_lift = [d["cumulative_lift"] for d in ablation["lift_table"]]
    ax.plot(deciles, full_lift, "o-", color="#1f77b4", linewidth=1.6, ms=4, label="Full (6 feats)")
    ax.plot(deciles, abl_lift, "s--", color="#ff7f0e", linewidth=1.4, ms=4, label="Ablation")
    ax.axhline(1.0, color="gray", linewidth=0.8, linestyle=":", label="Random")
    ax.set_xlabel("Exposure-rank decile (top → bottom)", fontsize=8)
    ax.set_ylabel("Cumulative lift over base rate", fontsize=8)
    ax.set_xticks(deciles)
    ax.tick_params(labelsize=7)
    year_label = str(val_years[0]) if val_years else "?"
    ax.set_title(
        f"Forecasting screen — cumulative lift\n"
        f"(exposure rank vs subsequent {year_label} burns; "
        f"{full['n_burned']} of {full['n']:,} assets burned)",
        fontsize=9,
    )
    ax.legend(fontsize=7, loc="upper right")
    ax.text(
        0.5,
        0.02,
        f"Only {full['n_burned']} burned assets — does not resolve which features\n"
        "carry the signal. See docs/validation_report.md.",
        ha="center",
        va="bottom",
        transform=ax.transAxes,
        fontsize=6.5,
        color="#777777",
    )


def make_fig5(metrics: dict, detection: dict, out: Path) -> None:  # type: ignore[type-arg]
    """Validation figure — burn-scar detection skill + forecasting-screen lift.

    The primary panel tells the WU-10 story: the de-grid burn-scar layer is a
    sound recent-scar *detector*. It plots precision and recall against the
    score threshold (multi-year ICNF 2023-2025 truth), marks the best-F1
    operating point, and reads off precision at the feature's binarisation
    thresholds. A companion panel keeps the forecasting-screen cumulative lift.
    Both stories come from committed JSON (cited in the caption); no probability
    claim is made — detection thresholds a relative score, lift ranks assets.
    """
    window = _pick_detection_window(detection, "2023-2025")
    if window is None:
        # Fall back to whatever single window is present (smoke path: 2025-only).
        wins = detection.get("windows", [])
        window = wins[0] if wins else None

    if window is None or not window.get("sweep"):
        # No usable detection sweep — emit the lift-only panel (informative).
        fig, ax = plt.subplots(figsize=(8, 5))
        _draw_lift_panel(ax, metrics)
        ax.text(
            0.5,
            1.02,
            "Detection sweep unavailable for this AOI; showing forecasting lift only.",
            ha="center",
            va="bottom",
            transform=ax.transAxes,
            fontsize=8,
            color="gray",
        )
        _add_attribution(ax)
        fig.tight_layout()
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  saved {out.name} (lift-only fallback)")
        return

    sweep = sorted(window["sweep"], key=lambda s: s["threshold"])
    thr = np.array([s["threshold"] for s in sweep])
    prec = np.array([s["precision"] for s in sweep])
    rec = np.array([s["recall"] for s in sweep])
    f1 = np.array([s["f1"] for s in sweep])

    best_thr = float(window["best_f1_threshold"])
    best_f1 = float(window["best_f1"])
    best_p = float(window["best_f1_precision"])
    best_r = float(window["best_f1_recall"])
    coverage = float(window["coverage"])
    label = window.get("label", "?")

    # Precision at the feature's binarisation thresholds (read off the sweep).
    def _prec_at(t: float) -> float:
        i = int(np.argmin(np.abs(thr - t)))
        return float(prec[i])

    fig = plt.figure(figsize=(13, 5.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.45, 1.0], wspace=0.28)
    ax = fig.add_subplot(gs[0, 0])
    ax_lift = fig.add_subplot(gs[0, 1])

    # --- Primary panel: precision / recall / F1 vs threshold ---
    ax.plot(thr, prec, "-", color="#1b7837", linewidth=2.0, marker="o", ms=3.5, label="Precision")
    ax.plot(thr, rec, "-", color="#2166ac", linewidth=2.0, marker="s", ms=3.5, label="Recall")
    ax.plot(thr, f1, "--", color="#762a83", linewidth=1.6, label="F1")
    ax.axhline(
        coverage,
        color="gray",
        linewidth=0.9,
        linestyle=":",
        label=f"Burned base rate ({coverage * 100:.1f}%)",
    )

    # Best-F1 operating point.
    ax.axvline(best_thr, color="#d7191c", linewidth=1.0, linestyle="-", alpha=0.6)
    ax.scatter(
        [best_thr],
        [best_f1],
        s=90,
        facecolors="none",
        edgecolors="#d7191c",
        linewidths=1.8,
        zorder=5,
    )
    ax.annotate(
        f"best F1 = {best_f1:.2f} @ thr {best_thr:.2f}\n"
        f"(precision {best_p:.2f}, recall {best_r:.2f})",
        xy=(best_thr, best_f1),
        xytext=(best_thr + 0.06, best_f1 + 0.10),
        fontsize=7.5,
        color="#d7191c",
        fontweight="bold",
        arrowprops={"arrowstyle": "->", "color": "#d7191c", "lw": 1.0},
        path_effects=[withStroke(linewidth=2.2, foreground="white")],
    )

    ax.set_xlabel("Burn-scar inference score threshold", fontsize=9)
    ax.set_ylabel("Precision / recall / F1", fontsize=9)
    ax.set_ylim(0.0, 1.02)
    ax.set_xlim(float(thr.min()), float(thr.max()))
    ax.set_title(
        f"Burn-scar layer is a sound recent-scar detector\n"
        f"(vs ICNF {label} truth — {coverage * 100:.0f}% of the AOI; "
        f"precision {_prec_at(0.5):.2f} @0.5, {_prec_at(0.7):.2f} @0.7)",
        fontsize=10,
        fontweight="bold",
    )
    ax.legend(fontsize=7.5, loc="center right", framealpha=0.9)
    ax.text(
        0.02,
        0.02,
        "Detection thresholds a relative score against burns that already happened —\n"
        "no probability claim, no forecast. Burn scars detected, not ignition predicted.",
        ha="left",
        va="bottom",
        transform=ax.transAxes,
        fontsize=6.5,
        color="#777777",
    )

    # --- Companion panel: forecasting cumulative lift ---
    _draw_lift_panel(ax_lift, metrics)

    fig.suptitle(
        "Validation — two questions, two truths: detection skill and a forecasting screen",
        fontsize=11.5,
        fontweight="bold",
        y=1.0,
    )
    _add_attribution(ax)
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

    # folium is an optional `viz` extra — not installed by the default/CI sync
    # (`uv sync --extra dev`), only with `--extra viz`. Suppress the missing-import
    # error there; this function only runs when the map is built (viz present).
    import folium  # pyright: ignore[reportMissingImports]
    from folium.plugins import MeasureControl  # pyright: ignore[reportMissingImports]

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
    burns = load_burns(smoke=smoke)
    metrics = load_metrics(smoke=smoke)
    aoi = load_aoi(smoke=smoke)
    crosswalk = load_crosswalk()
    detection = load_multiyear_detection(smoke=smoke)

    # Burn-scar source: pinned canonical de-grid p85 COG for the pilot; lexical
    # latest for the smoke variant.
    if smoke:
        burn_path = load_burn_scar_cog_path(smoke=True)
        burn_sidecar = json.loads(burn_path.with_suffix(".json").read_text())
    else:
        burn_path, burn_sidecar = load_burn_scar_pilot()

    val_years = metrics.get("validation_years", [])
    val_year: int = int(val_years[0]) if val_years else int(burns["vintage_year"].max())

    suffix = "_smoke" if smoke else ""
    aoi_label = "smoke AOI" if smoke else "pilot AOI"
    # Score input-window end, from the metrics JSON (matches the scored parquet).
    t0 = str(metrics.get("window_end", "?"))

    print("Generating figures …")

    make_fig1(
        gdf,
        aoi,
        _FIGS_DIR / f"fig1_exposure_map{suffix}.png",
        aoi_label=aoi_label,
        t0=t0,
        fuel_path=fuel_path,
    )
    make_fig2(fuel_path, crosswalk, _FIGS_DIR / f"fig2_fuel_layer{suffix}.png", aoi=aoi)
    reducer_label = str(burn_sidecar.get("reducer", "de-grid p85"))
    if "p85" in reducer_label and "grid" not in reducer_label:
        reducer_label = f"de-grid {reducer_label}"
    make_fig3(
        burn_path,
        burn_sidecar,
        aoi,
        burns,
        _FIGS_DIR / f"fig3_burn_scar{suffix}.png",
        reducer_label=reducer_label,
    )
    make_fig4(
        gdf,
        burns,
        aoi,
        val_year,
        _FIGS_DIR / f"fig4_icnf_overlay{suffix}.png",
        t0=t0,
        fuel_path=fuel_path,
    )
    make_fig5(metrics, detection, _FIGS_DIR / f"fig5_lift_curve{suffix}.png")
    # The HTML map gets the same smoke suffix as the PNGs — a smoke run must
    # never clobber the pilot map. The pilot map (~14 MB) exceeds the repo's
    # 2 MB committed-file cap and is NOT committed; the committed sample is
    # the smoke-AOI map (see README).
    make_html_map(gdf, fuel_path, burns, aoi, _FIGS_DIR / f"exposure_map{suffix}.html")

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
