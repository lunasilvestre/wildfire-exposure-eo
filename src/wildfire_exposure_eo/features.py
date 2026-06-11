"""Per-asset zonal features for the exposure rank (WU-6, prompt 10).

Each feature is a pure function over a buffered-asset GeoDataFrame (buffers
computed once in EPSG:32629 from the per-class taxonomy radius) and one source
artefact, returning a ``pd.Series`` indexed by ``asset_id``. ``NaN`` means the
feature could not be computed for that asset — it is never imputed.

Terminology guard (CLAUDE.md non-negotiable #6): these features feed a relative,
AOI-normalised screening *rank*, not a probability of fire. The
``recent_burn_share_12mo`` feature in particular is an upward-biased relative
rank input derived from a max-composite burn-scar COG, not a burned-area
estimate.

Heavy/network dependencies (stackstac, rioxarray, exactextract, pystac_client)
are imported inside the functions that need them, so the module stays importable
in lightweight CLI/test contexts. Buffering is metric (EPSG:32629); each raster
is read in its native CRS and the buffered geometries are reprojected to that
CRS exactly once before the zonal join (non-negotiable #2).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pandas as pd
import requests

from wildfire_exposure_eo.burn_scar import (
    S2_COLLECTION,
    _apply_gdal_http_defaults,
    _boa_offset,
    _pc_sas_token,
    months_back,
)
from wildfire_exposure_eo.stac import PC_STAC_URL, _default_client_factory, _item_datetime

if TYPE_CHECKING:
    import geopandas as gpd
    import pystac
    import xarray as xr
    from pystac_client import Client
    from shapely.geometry.base import BaseGeometry

    from wildfire_exposure_eo.schemas.scored_asset import ScoredAssetProvenance

logger = logging.getLogger(__name__)

#: Metric CRS for buffering and the slope/NBR working grids.
ASSET_CRS = "EPSG:32629"
COP_DEM_COLLECTION = "cop-dem-glo-30"
#: S2 SCL classes treated as invalid (no-data, cloud, shadow, snow, saturated).
DEFAULT_SCL_MASK = (0, 1, 3, 8, 9, 10, 11)
#: Default binarisation threshold for the burn-scar probability COG.
BURN_SCAR_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Date windows — every feature respects an explicit window-end (backdatable).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DateRange:
    """A closed date interval ``[start, end]`` used to window feature inputs."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"DateRange start {self.start} after end {self.end}")

    def overlaps(self, other: DateRange) -> bool:
        return self.start <= other.end and other.start <= self.end


def twelve_month_window(window_end: date) -> DateRange:
    """The trailing 12-month window ending at ``window_end``."""
    return DateRange(months_back(window_end, 12), window_end)


def recent_season_year(window_end: date) -> int:
    """Latest year whose late-summer window (ends Sep 30) is ≤ ``window_end``."""
    return window_end.year if window_end >= date(window_end.year, 9, 30) else window_end.year - 1


def spring_window(year: int) -> DateRange:
    """Spring composite window (Mar 1 – May 31) for ``year``."""
    return DateRange(date(year, 3, 1), date(year, 5, 31))


def summer_window(year: int) -> DateRange:
    """Late-summer composite window (Aug 1 – Sep 30) for ``year``."""
    return DateRange(date(year, 8, 1), date(year, 9, 30))


# ---------------------------------------------------------------------------
# IO + provenance helpers
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes (streamed)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_assets(parquet_path: Path) -> gpd.GeoDataFrame:
    """Load the WU-2 OSM asset GeoParquet; assert EPSG:4326 (no implicit CRS)."""
    import geopandas as gpd

    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None:
        raise ValueError(f"{parquet_path} has no CRS — refusing to assume one")
    if gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path} CRS is {gdf.crs} — expected EPSG:4326")
    return gdf


def load_burns(parquet_path: Path) -> gpd.GeoDataFrame:
    """Load the WU-4 ICNF burns GeoParquet (EPSG:4326, carries ``vintage_year``)."""
    import geopandas as gpd

    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path} CRS is {gdf.crs} — expected EPSG:4326")
    if "vintage_year" not in gdf.columns:
        raise ValueError(f"{parquet_path} missing 'vintage_year' column")
    return gdf


def buffer_assets(assets: gpd.GeoDataFrame, taxonomy: dict[str, Any]) -> gpd.GeoDataFrame:
    """Buffer each asset by its class radius in EPSG:32629.

    Returns a GeoDataFrame in EPSG:32629 with ``asset_id`` as a column (kept for
    exactextract ``include_cols``) and the buffered polygon as the geometry.
    Deterministically ordered by ``(asset_class, osm_type, osm_id)``.
    """
    classes = taxonomy["classes"]
    radius = {cid: float(c["buffer_radius_m"]) for cid, c in classes.items()}
    unknown = sorted(set(assets["asset_class"]) - set(radius))
    if unknown:
        raise ValueError(f"asset_class(es) not in taxonomy: {unknown}")

    out = assets.sort_values(["asset_class", "osm_type", "osm_id"]).reset_index(drop=True)
    metric = out.to_crs(ASSET_CRS)
    radii = np.array([radius[c] for c in out["asset_class"]], dtype="float64")
    metric["geometry"] = metric.geometry.buffer(radii)
    return cast("gpd.GeoDataFrame", metric)


# ---------------------------------------------------------------------------
# Zonal-statistics core (exactextract: coverage-exact, ≤10 ms/asset target)
# ---------------------------------------------------------------------------
def _zonal(da: xr.DataArray, buffers: gpd.GeoDataFrame, op: str) -> pd.Series:
    """Coverage-weighted zonal stat ``op`` of single-band ``da`` over ``buffers``.

    ``buffers`` (EPSG:32629) are reprojected to the raster's CRS exactly once.
    Returns a ``pd.Series`` indexed by ``asset_id``; ``NaN`` where the buffer
    covers no valid pixel.
    """
    from exactextract import exact_extract

    if da.rio.crs is None:
        raise ValueError("zonal raster has no CRS")
    vec = buffers[["asset_id", "geometry"]].to_crs(da.rio.crs)
    df = cast(
        "pd.DataFrame", exact_extract(da, vec, [op], include_cols=["asset_id"], output="pandas")
    )
    stat_cols = [c for c in df.columns if c != "asset_id"]
    series = df.set_index("asset_id")[stat_cols[0]].astype("float64")
    series.name = op
    return cast("pd.Series", series)


def _open_band(cog_path: Path, band: int, nodata: float) -> xr.DataArray:
    """Open one band of a COG as a single-band DataArray with explicit nodata."""
    import rioxarray

    da = cast("xr.DataArray", rioxarray.open_rasterio(cog_path))  # (band, y, x)
    sel = da.sel(band=band)
    return sel.rio.write_nodata(nodata)


# ---------------------------------------------------------------------------
# Feature functions — each (buffers, source[, window]) -> pd.Series[asset_id]
# ---------------------------------------------------------------------------
def fuel_class_severity_weight(buffers: gpd.GeoDataFrame, fuel_cog_path: Path) -> pd.Series:
    """Zonal mean of the WU-5 fuel-severity band (band 2, severity×100 → 0–1)."""
    da = _open_band(fuel_cog_path, band=2, nodata=255)
    series = _zonal(da, buffers, "mean") / 100.0
    series.name = "fuel_class_severity_weight"
    return series


def canopy_height_p90_m(buffers: gpd.GeoDataFrame, gch_cog_path: Path) -> pd.Series:
    """Zonal p90 of ETH Global Canopy Height (metres) inside the buffer."""
    import rasterio

    with rasterio.open(gch_cog_path) as ds:
        nodata = ds.nodata if ds.nodata is not None else 255.0
    da = _open_band(gch_cog_path, band=1, nodata=float(nodata))
    series = _zonal(da, buffers, "quantile(q=0.9)")
    series.name = "canopy_height_p90_m"
    return series


def slope_max_deg(buffers: gpd.GeoDataFrame, slope_da: xr.DataArray) -> pd.Series:
    """Zonal max of the Cop-DEM Horn slope (degrees) inside the buffer."""
    series = _zonal(slope_da, buffers, "max")
    series.name = "slope_max_deg"
    return series


def historical_burn_share(
    buffers: gpd.GeoDataFrame, burns: gpd.GeoDataFrame, window: DateRange
) -> pd.Series:
    """Area share of each buffer intersecting ICNF burns with vintage ≤ window.end.

    Vector overlay in EPSG:32629. Only vintages on or before the window end are
    eligible — the WU-7 leakage rule then validates strictly *after* the window.
    """
    eligible = burns[burns["vintage_year"] <= window.end.year]
    idx = buffers["asset_id"].to_numpy()
    if eligible.empty:
        return pd.Series(np.zeros(len(idx)), index=idx, name="historical_burn_share")
    union = eligible.to_crs(ASSET_CRS).union_all()
    inter = buffers.geometry.intersection(union).area.to_numpy()
    share = np.clip(inter / buffers.geometry.area.to_numpy(), 0.0, 1.0)
    return pd.Series(share, index=idx, name="historical_burn_share")


def recent_burn_share_12mo(
    buffers: gpd.GeoDataFrame,
    burn_scar_cog_path: Path,
    cog_window: DateRange,
    window: DateRange,
    *,
    threshold: float = BURN_SCAR_THRESHOLD,
    nodata: float = -9999.0,
) -> pd.Series | None:
    """Share of buffer pixels with burn-scar probability ≥ ``threshold``.

    Returns ``None`` (feature absent for the whole run) when the fixed burn-scar
    COG cannot honour the requested trailing-12-month window: either the windows
    do not overlap, or the COG composites scenes observed *after* the requested
    window end — using those would leak post-window observations into a
    backdated run, breaking WU-7's temporal-leakage rule (prompt 10).

    The value is an *upward-biased relative rank input*: the WU-1 max-composite
    over ~180 scenes retains single-scene false positives, so the share over-
    counts true burned area. It is a rank ingredient, not a burned-area estimate.
    """
    requested = twelve_month_window(window.end)
    if not requested.overlaps(cog_window) or cog_window.end > requested.end:
        logger.info(
            "[features] recent_burn_share_12mo: burn-scar COG window %s..%s cannot honour "
            "requested window %s..%s (no overlap, or COG composites scenes observed after "
            "the requested end — temporal leakage) — feature absent",
            cog_window.start,
            cog_window.end,
            requested.start,
            requested.end,
        )
        return None
    import xarray as xr

    da = _open_band(burn_scar_cog_path, band=1, nodata=nodata)
    binarised = xr.where(da == nodata, np.nan, (da >= threshold).astype("float32"))
    binarised = binarised.rio.write_crs(da.rio.crs).rio.write_nodata(np.nan)
    series = _zonal(binarised, buffers, "mean")
    series.name = "recent_burn_share_12mo"
    return series


def nbr_delta_recent(buffers: gpd.GeoDataFrame, nbr_delta_da: xr.DataArray) -> pd.Series:
    """Zonal mean of the spring-minus-late-summer median-NBR delta inside the buffer."""
    series = _zonal(nbr_delta_da, buffers, "mean")
    series.name = "nbr_delta_recent"
    return series


# ---------------------------------------------------------------------------
# Network raster builders (slope, NBR delta) — STAC + stackstac
# ---------------------------------------------------------------------------
def horn_slope(z: np.ndarray, cell_x: float, cell_y: float) -> np.ndarray:
    """Slope in degrees from a DEM array via Horn's 3×3 method (GDAL/ArcGIS std).

    ``cell_x``/``cell_y`` are pixel sizes in the DEM's (metric) CRS. Edges are
    edge-padded; NaN elevations propagate to NaN slope. Result clipped to [0, 90].
    """
    zp = np.pad(z.astype("float64"), 1, mode="edge")
    a, b, c = zp[:-2, :-2], zp[:-2, 1:-1], zp[:-2, 2:]
    d, f = zp[1:-1, :-2], zp[1:-1, 2:]
    g, h, i = zp[2:, :-2], zp[2:, 1:-1], zp[2:, 2:]
    dz_dx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * cell_x)
    dz_dy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * cell_y)
    slope = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
    return np.clip(slope, 0.0, 90.0)


#: Planetary Computer universal href-signing endpoint. Used for Cop-DEM, whose
#: storage account the per-collection token endpoint does not authorise
#: (observed HTTP 403, 2026-06-11) — the /sign endpoint signs the exact href.
PC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"


def _sign(item: pystac.Item, assets: tuple[str, ...], collection: str) -> pystac.Item:
    """Clone ``item`` with PC SAS-signed hrefs on ``assets`` (per-collection token)."""
    token = _pc_sas_token(collection)
    clone = item.clone()
    for key in assets:
        asset = clone.assets.get(key)
        if asset is None:
            raise ValueError(f"item {item.id} missing asset {key!r}")
        sep = "&" if "?" in asset.href else "?"
        asset.href = f"{asset.href}{sep}{token}"
    return clone


def _sign_via_endpoint(item: pystac.Item, assets: tuple[str, ...]) -> pystac.Item:
    """Clone ``item`` with hrefs signed by PC's universal /sign endpoint.

    More robust than the per-collection token for storage accounts the token
    endpoint does not authorise (e.g. Cop-DEM's ``elevationeuwest``).
    """
    clone = item.clone()
    for key in assets:
        asset = clone.assets.get(key)
        if asset is None:
            raise ValueError(f"item {item.id} missing asset {key!r}")
        resp = requests.get(PC_SIGN_URL, params={"href": asset.href}, timeout=30)
        resp.raise_for_status()
        asset.href = str(resp.json()["href"])
    return clone


def build_slope_raster(
    aoi: BaseGeometry, *, client: Client | None = None
) -> tuple[xr.DataArray, list[str]]:
    """Resolve Cop-DEM GLO-30 over ``aoi``, mosaic at 30 m in EPSG:32629, derive slope.

    Returns ``(slope_da, dem_item_ids)``. Item IDs are logged before any read
    (verify-then-act) and carried into provenance.
    """
    import rioxarray  # noqa: F401  (registers the .rio accessor used below)
    import stackstac
    import xarray as xr
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from shapely.geometry import mapping

    _apply_gdal_http_defaults()  # make hung PC blob reads fail fast (WU-1 lesson)
    cli: Any = client if client is not None else _default_client_factory(PC_STAC_URL)
    search = cli.search(collections=[COP_DEM_COLLECTION], intersects=mapping(aoi))
    items = sorted(search.items(), key=lambda it: (_item_datetime(it), it.id))
    if not items:
        raise ValueError(f"no {COP_DEM_COLLECTION} items intersect the AOI")
    logger.info("[features] cop-dem: %d tile(s)", len(items))
    for it in items:
        logger.info("[features]   %s", it.id)
    signed = [_sign_via_endpoint(it, ("data",)) for it in items]
    bounds = transform_bounds("EPSG:4326", ASSET_CRS, *aoi.bounds)
    stack = stackstac.stack(
        signed,
        assets=["data"],
        bounds=bounds,
        epsg=32629,
        resolution=30,
        dtype=np.dtype("float32"),
        fill_value=np.float32(np.nan),  # pyright: ignore[reportArgumentType]
        rescale=False,
        resampling=Resampling.bilinear,
    )
    dem = stack.sel(band="data").median(dim="time", skipna=True).compute()
    slope = horn_slope(dem.values, 30.0, 30.0)
    slope_da = xr.DataArray(slope, dims=("y", "x"), coords={"y": dem.y, "x": dem.x})
    slope_da = slope_da.rio.write_crs(ASSET_CRS).rio.write_nodata(np.nan)
    return slope_da, [it.id for it in items]


def _nbr_composite(
    items: list[pystac.Item],
    bounds: tuple[float, float, float, float],
    *,
    scl_mask: tuple[int, ...],
) -> xr.DataArray:
    """Median NBR over the given S2 items on the 32629/10 m AOI grid (NaN where masked)."""
    import stackstac
    import xarray as xr
    from rasterio.enums import Resampling

    signed = [_sign(it, ("B08", "B12", "SCL"), S2_COLLECTION) for it in items]
    stack = stackstac.stack(
        signed,
        assets=["B08", "B12", "SCL"],
        bounds=bounds,
        epsg=32629,
        resolution=10,
        dtype=np.dtype("float32"),
        fill_value=np.float32(np.nan),  # pyright: ignore[reportArgumentType]
        rescale=False,
        resampling=Resampling.nearest,
    )
    offsets = xr.DataArray(
        np.array([_boa_offset(it) for it in items], dtype="float32"),
        dims="time",
        coords={"time": stack.time},
    )
    b08 = (stack.sel(band="B08") - offsets) / 10000.0
    b12 = (stack.sel(band="B12") - offsets) / 10000.0
    scl = stack.sel(band="SCL")
    valid = (b08 > 0) & ~scl.isin(list(scl_mask))
    denom = b08 + b12
    nbr = xr.where(valid & (denom != 0), (b08 - b12) / denom, np.nan)
    return nbr.median(dim="time", skipna=True).compute()


def build_nbr_delta_raster(
    aoi: BaseGeometry,
    window_end: date,
    *,
    spring_max_cloud: int = 30,
    summer_max_cloud: int = 60,
    scl_mask: tuple[int, ...] = DEFAULT_SCL_MASK,
    client: Client | None = None,
) -> tuple[xr.DataArray, list[str]]:
    """Spring-minus-late-summer median-NBR delta on the 32629/10 m AOI grid.

    The season year is the most recent one whose late-summer window has ended on
    or before ``window_end`` (backdatable). Positive delta = NBR declined from
    spring into late summer (drier / more stressed vegetation). Cloud-cover
    thresholds follow methodology §3: strict 30 % for the spring composite,
    relaxed 60 % for late summer (dry-season scenes are scarcer), with per-pixel
    SCL masking on both. Returns ``(delta_da, s2_item_ids)``; item IDs are
    logged before any read.
    """
    import rioxarray  # noqa: F401  (registers the .rio accessor used below)
    from rasterio.warp import transform_bounds
    from shapely.geometry import mapping

    _apply_gdal_http_defaults()  # make hung PC blob reads fail fast (WU-1 lesson)
    cli: Any = client if client is not None else _default_client_factory(PC_STAC_URL)
    year = recent_season_year(window_end)
    bounds = transform_bounds("EPSG:4326", ASSET_CRS, *aoi.bounds)

    item_ids: list[str] = []
    composites: dict[str, xr.DataArray] = {}
    seasons = (
        ("spring", spring_window(year), spring_max_cloud),
        ("summer", summer_window(year), summer_max_cloud),
    )
    for label, win, max_cloud in seasons:
        search = cli.search(
            collections=[S2_COLLECTION],
            intersects=mapping(aoi),
            datetime=f"{win.start.isoformat()}/{win.end.isoformat()}",
            query={"eo:cloud_cover": {"lte": max_cloud}},
        )
        items = sorted(search.items(), key=lambda it: (_item_datetime(it), it.id))
        if not items:
            raise ValueError(
                f"no {S2_COLLECTION} items for {label} {win.start}..{win.end} "
                f"(cloud≤{max_cloud}%) — widen the window or relax cloud cover"
            )
        logger.info(
            "[features] nbr %s %s..%s (cloud≤%d%%): %d scene(s)",
            label,
            win.start,
            win.end,
            max_cloud,
            len(items),
        )
        for it in items:
            logger.info("[features]   %s  %s", _item_datetime(it).isoformat(), it.id)
            item_ids.append(it.id)
        composites[label] = _nbr_composite(items, bounds, scl_mask=scl_mask)

    delta = (composites["spring"] - composites["summer"]).compute()
    delta = delta.rio.write_crs(ASSET_CRS).rio.write_nodata(np.nan)
    return delta, item_ids


# ---------------------------------------------------------------------------
# Burn-scar COG window (for the backdating null rule)
# ---------------------------------------------------------------------------
def burn_scar_window(cog_path: Path) -> tuple[DateRange, float]:
    """Read ``(window, binarisation_threshold)`` from a burn-scar COG sidecar JSON."""
    sidecar = cog_path.with_suffix(".json")
    payload = json.loads(sidecar.read_text())
    window = DateRange(
        date.fromisoformat(str(payload["window_start"])),
        date.fromisoformat(str(payload["window_end"])),
    )
    return window, float(payload.get("binarisation_threshold", BURN_SCAR_THRESHOLD))


# ---------------------------------------------------------------------------
# Orchestration: assemble features, compose the rank, write both parquets.
# ---------------------------------------------------------------------------
def _asset_metadata(assets: gpd.GeoDataFrame, taxonomy: dict[str, Any]) -> gpd.GeoDataFrame:
    """Deterministically-ordered per-asset metadata (1 = ``buffer_assets`` order)."""
    classes = taxonomy["classes"]
    crit = {cid: float(c["criticality_weight"]) for cid, c in classes.items()}
    meta = assets.sort_values(["asset_class", "osm_type", "osm_id"]).reset_index(drop=True)
    meta["criticality_weight"] = [crit[c] for c in meta["asset_class"]]
    cols = [
        "asset_id",
        "asset_class",
        "osm_type",
        "osm_id",
        "criticality_weight",
        "centroid_lon",
        "centroid_lat",
        "geometry_wkb",
        "geometry",
    ]
    return cast("gpd.GeoDataFrame", meta[cols])


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of a scoring run (paths + verification summary)."""

    features_path: Path
    exposure_path: Path
    n_assets: int
    #: Zonal cross-product wall-clock per asset (the ≤10 ms/asset target metric).
    ms_per_asset: float
    #: One-time network raster-building wall-clock (Cop-DEM + S2), seconds.
    build_seconds: float
    features_present_global: tuple[str, ...]
    sample_row: dict[str, Any]


def run_scoring(
    *,
    aoi_path: Path,
    taxonomy_path: Path,
    exposure_config_path: Path,
    crosswalk_sha: str,
    osm_parquet: Path,
    burns_parquet: Path,
    fuel_cog: Path,
    gch_cog: Path,
    burn_scar_cog: Path,
    window_end: date,
    run_id: str,
    code_commit_sha: str,
    features_out: Path,
    exposure_out: Path,
    slope_da: xr.DataArray | None = None,
    nbr_delta_da: xr.DataArray | None = None,
    client: Client | None = None,
) -> ScoreResult:
    """End-to-end: features → composite rank → two GeoParquet artefacts.

    ``slope_da`` / ``nbr_delta_da`` may be injected (tests / pre-built rasters);
    when ``None`` they are built from STAC (Cop-DEM, Sentinel-2) over the AOI.
    Every feature respects ``window_end`` (backdatable); the burn-scar COG is the
    one fixed-window input and yields a null ``recent_burn_share_12mo`` outside
    its window. Returns a :class:`ScoreResult` with a sample row and ms/asset.
    """
    import time

    import yaml

    from wildfire_exposure_eo.schemas.scored_asset import (
        FEATURE_NAMES,
        ScoredAsset,
        ScoredAssetProvenance,
    )
    from wildfire_exposure_eo.scoring import compose_exposure, load_exposure_config
    from wildfire_exposure_eo.stac import load_aoi_geometry

    taxonomy = yaml.safe_load(taxonomy_path.read_text())
    config = load_exposure_config(exposure_config_path)
    aoi_geom, aoi_sha = load_aoi_geometry(aoi_path)

    assets = load_assets(osm_parquet)
    burns = load_burns(burns_parquet)
    buffers = buffer_assets(assets, taxonomy)
    meta = _asset_metadata(assets, taxonomy)
    asset_index = pd.Index(buffers["asset_id"], name="asset_id")
    window = DateRange(months_back(window_end, 12), window_end)

    # Build the network rasters first (Cop-DEM slope, S2 NBR delta). This is a
    # one-time fixed cost independent of asset count, so it is timed separately
    # from the zonal cross-product that the ≤10 ms/asset target governs.
    t_build = time.perf_counter()
    if slope_da is None:
        slope_da, dem_item_ids = build_slope_raster(aoi_geom, client=client)
    else:
        dem_item_ids = []
    if nbr_delta_da is None:
        nbr_delta_da, s2_item_ids = build_nbr_delta_raster(aoi_geom, window_end, client=client)
    else:
        s2_item_ids = []
    build_seconds = time.perf_counter() - t_build

    # Zonal cross-product: the per-asset performance target applies here.
    cog_window, cog_threshold = burn_scar_window(burn_scar_cog)
    t_zonal = time.perf_counter()
    series: dict[str, pd.Series] = {
        "fuel_class_severity_weight": fuel_class_severity_weight(buffers, fuel_cog),
        "canopy_height_p90_m": canopy_height_p90_m(buffers, gch_cog),
        "historical_burn_share": historical_burn_share(buffers, burns, window),
        "slope_max_deg": slope_max_deg(buffers, slope_da),
        "nbr_delta_recent": nbr_delta_recent(buffers, nbr_delta_da),
    }
    recent = recent_burn_share_12mo(
        buffers, burn_scar_cog, cog_window, window, threshold=cog_threshold
    )
    burn_scar_sha: str | None = None
    if recent is not None:
        series["recent_burn_share_12mo"] = recent
        burn_scar_sha = sha256_file(burn_scar_cog)
    zonal_seconds = time.perf_counter() - t_zonal

    features_df = pd.DataFrame(index=asset_index)
    for name in FEATURE_NAMES:
        if name in series:
            features_df[name] = series[name].reindex(asset_index).to_numpy()

    composed = compose_exposure(features_df, config)

    provenance = ScoredAssetProvenance(
        model_version=config.version,
        config_sha=sha256_file(exposure_config_path),
        crosswalk_sha=crosswalk_sha,
        run_id=run_id,
        code_commit_sha=code_commit_sha,
        aoi_path=str(aoi_path),
        aoi_geometry_sha=aoi_sha,
        window_start=window.start,
        window_end=window.end,
        osm_parquet_sha=sha256_file(osm_parquet),
        burns_parquet_sha=sha256_file(burns_parquet),
        fuel_cog_sha=sha256_file(fuel_cog),
        gch_cache_sha=sha256_file(gch_cog),
        burn_scar_cog_sha=burn_scar_sha,
        dem_item_ids=tuple(dem_item_ids),
        s2_item_ids=tuple(s2_item_ids),
        burn_share_threshold=cog_threshold,
    )

    sample_row = _write_outputs(
        meta=meta,
        composed=composed,
        provenance=provenance,
        features_out=features_out,
        exposure_out=exposure_out,
    )
    # Fail loudly if the committed contract drifts (CLAUDE.md schema-validate).
    ScoredAsset.model_validate(sample_row)

    present = tuple(c for c in FEATURE_NAMES if c in features_df.columns)
    return ScoreResult(
        features_path=features_out,
        exposure_path=exposure_out,
        n_assets=len(assets),
        ms_per_asset=zonal_seconds / max(len(assets), 1) * 1000.0,
        build_seconds=build_seconds,
        features_present_global=present,
        sample_row=sample_row,
    )


def _write_outputs(
    *,
    meta: gpd.GeoDataFrame,
    composed: pd.DataFrame,
    provenance: ScoredAssetProvenance,
    features_out: Path,
    exposure_out: Path,
) -> dict[str, Any]:
    """Write features + exposure GeoParquet; return one validated sample row dict."""
    import geopandas as gpd

    from wildfire_exposure_eo.schemas.scored_asset import FEATURE_NAMES

    feature_cols = [c for c in FEATURE_NAMES if c in composed.columns]
    by_id = composed.reset_index()
    merged = meta.merge(by_id, on="asset_id", how="left")

    prov_json = provenance.model_dump_json()

    # --- features parquet: raw per-asset features (no score) ---
    feat_gdf = gpd.GeoDataFrame(
        merged[
            [
                "asset_id",
                "asset_class",
                "osm_type",
                "osm_id",
                "centroid_lon",
                "centroid_lat",
                *feature_cols,
                "features_present",
            ]
        ].copy(),
        geometry=cast("Any", merged["geometry"]),
        crs="EPSG:4326",
    )
    feat_gdf["features_present"] = feat_gdf["features_present"].apply(json.dumps)
    features_out.parent.mkdir(parents=True, exist_ok=True)
    feat_gdf.to_parquet(features_out, compression="snappy", index=False)

    # --- exposure parquet: scored rows (one ScoredAsset per row) ---
    def _features_json(row: pd.Series) -> str:
        return json.dumps(
            {c: (None if bool(pd.isna(row[c])) else float(row[c])) for c in feature_cols}
        )

    exp = merged.copy()
    exp["features"] = exp.apply(_features_json, axis=1)
    exp["features_present"] = exp["features_present"].apply(json.dumps)
    exp["provenance"] = prov_json
    exp["exposure_rank"] = exp["exposure_rank"].astype("int64")
    exp_gdf = gpd.GeoDataFrame(
        exp[
            [
                "asset_id",
                "osm_type",
                "osm_id",
                "asset_class",
                "criticality_weight",
                "centroid_lon",
                "centroid_lat",
                "geometry_wkb",
                "features",
                "features_present",
                "exposure_score",
                "exposure_rank",
                "provenance",
            ]
        ].copy(),
        geometry=cast("Any", exp["geometry"]),
        crs="EPSG:4326",
    )
    exposure_out.parent.mkdir(parents=True, exist_ok=True)
    exp_gdf.to_parquet(exposure_out, compression="snappy", index=False)

    top = exp.sort_values("exposure_rank").iloc[0]
    return {
        "asset_id": top["asset_id"],
        "osm_type": top["osm_type"],
        "osm_id": int(top["osm_id"]),
        "asset_class": top["asset_class"],
        "criticality_weight": float(top["criticality_weight"]),
        "centroid_lon": float(top["centroid_lon"]),
        "centroid_lat": float(top["centroid_lat"]),
        "geometry_wkb": top["geometry_wkb"],
        "features": _features_json(top),
        "features_present": top["features_present"],  # already a JSON string
        "exposure_score": float(top["exposure_score"]),
        "exposure_rank": int(top["exposure_rank"]),
        "provenance": prov_json,
    }
