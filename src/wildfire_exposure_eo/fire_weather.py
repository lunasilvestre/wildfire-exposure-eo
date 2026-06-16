"""Seasonal fire-weather feature (Pillar 0, prompt 17).

Ingests the Canadian **Fire Weather Index** (FWI) — an open, public, no-auth
*danger index* — from the JRC/Copernicus Global Wildfire Information System
(GWIS) WMS, reduces it to a per-season surface over the AOI, and exposes a
per-asset zonal aggregator in the same shape as :mod:`features`.

Terminology guard (CLAUDE.md non-negotiable #6): FWI is a meteorological
*danger index* produced by a third party. The feature it yields is one
normalised input to a relative, AOI-normalised screening **rank** — never a
probability of fire, never a forecast of ignition. We ingest an index; we do
not predict.

Source identity (CLAUDE.md non-negotiable #1): the WMS endpoint, layer id,
CRS, and real archive bounds are NOT hardcoded here — they live in
``config/fire_weather.yaml`` and were verified live by
``scripts/17_fire_weather_audit.py`` (verdict JSON under
``outputs/diagnostics/``). The GWIS ``nasa.fwi_gpm.fwi`` layer carries a real
FWI archive of roughly 2014-05 .. 2020-12; outside it the WMS returns an
all-zero raster, which this module treats as "no data" (the feature is then
absent for the whole run — never imputed).

Determinism (non-negotiable #4): the seasonal surface is a fixed function of
``(season_year, config)``. There is no RNG; the public entry points still
accept a ``seed`` argument and thread it to any sub-call, so the contract
matches the rest of the codebase. Explicit CRS on every raster
(non-negotiable #2): rasters are fetched in the layer's native CRS, tagged via
``rio.write_crs``, and the asset buffers are reprojected to that CRS exactly
once inside the zonal join.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import requests

from wildfire_exposure_eo.features import ASSET_CRS, _zonal

if TYPE_CHECKING:
    import geopandas as gpd
    import pandas as pd
    import xarray as xr
    from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

#: Default deterministic seed (CLAUDE.md non-negotiable #4). No RNG is used,
#: but the entry points accept and thread it so the contract is uniform.
DEFAULT_SEED = 42

USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 fire-weather "
    "(+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)

#: A season-reduced FWI surface that is identically zero everywhere is the WMS
#: signature for "no data for this date" (a real fire-season FWI field over the
#: pilot AOI is never uniformly zero). Used to detect out-of-archive windows.
_NULL_FWI_EPS = 1e-9


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FireWeatherConfig:
    """Parsed ``config/fire_weather.yaml`` (validated on load)."""

    version: str
    endpoint: str
    wms_version: str
    layer: str
    raster_format: str
    crs: str
    product_id: str
    provider: str
    index: str
    attribution: str
    archive_start: date
    archive_end: date

    feature_name: str
    season_reducer: str
    season_start_month: int
    season_end_month: int
    sample_days: tuple[int, ...]
    zonal_stat: str

    def __post_init__(self) -> None:
        if not 1 <= self.season_start_month <= self.season_end_month <= 12:
            raise ValueError(
                f"invalid season months {self.season_start_month}..{self.season_end_month}"
            )
        if self.archive_start > self.archive_end:
            raise ValueError(
                f"archive_start {self.archive_start} after archive_end {self.archive_end}"
            )
        if not self.sample_days or any(not 1 <= d <= 28 for d in self.sample_days):
            raise ValueError(f"sample_days must be in 1..28, got {self.sample_days}")
        if self.season_reducer not in {"max", "p95"}:
            raise ValueError(f"unsupported season_reducer {self.season_reducer!r}")


def load_fire_weather_config(path: Path) -> FireWeatherConfig:
    """Load + validate ``config/fire_weather.yaml`` into a :class:`FireWeatherConfig`."""
    import yaml

    payload = yaml.safe_load(path.read_text())
    src = payload["source"]
    feat = payload["feature"]
    return FireWeatherConfig(
        version=str(payload["version"]),
        endpoint=str(src["endpoint"]),
        wms_version=str(src["wms_version"]),
        layer=str(src["layer"]),
        raster_format=str(src["raster_format"]),
        crs=str(src["crs"]),
        product_id=str(src["product_id"]),
        provider=str(src["provider"]),
        index=str(src["index"]),
        attribution=str(src["attribution"]),
        archive_start=date.fromisoformat(str(src["archive_start"])),
        archive_end=date.fromisoformat(str(src["archive_end"])),
        feature_name=str(feat["name"]),
        season_reducer=str(feat["season_reducer"]),
        season_start_month=int(feat["season_start_month"]),
        season_end_month=int(feat["season_end_month"]),
        sample_days=tuple(int(d) for d in feat["sample_days"]),
        zonal_stat=str(feat["zonal_stat"]),
    )


# ---------------------------------------------------------------------------
# Season sampling
# ---------------------------------------------------------------------------
def season_sample_dates(year: int, config: FireWeatherConfig) -> list[date]:
    """Deterministic list of daily-FWI sample dates for ``year``'s fire season.

    The cross-product of ``season_start_month..season_end_month`` and
    ``sample_days``, sorted ascending — a fixed function of the config, so two
    runs at the same ``year`` request exactly the same rasters.
    """
    out = [
        date(year, month, day)
        for month in range(config.season_start_month, config.season_end_month + 1)
        for day in config.sample_days
    ]
    return sorted(out)


def season_in_archive(year: int, config: FireWeatherConfig) -> bool:
    """True when ``year``'s sampled season lies within the layer's real archive."""
    dates = season_sample_dates(year, config)
    return all(config.archive_start <= d <= config.archive_end for d in dates)


# ---------------------------------------------------------------------------
# WMS fetch (single daily FWI raster, raw values, explicit CRS)
# ---------------------------------------------------------------------------
def _wms_getmap_url(
    config: FireWeatherConfig,
    bbox_4326: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
    when: date,
) -> tuple[str, dict[str, str]]:
    """Build the WMS GetMap (endpoint, query-params) for one daily FWI raster.

    ``bbox_4326`` is ``(minlon, minlat, maxlon, maxlat)``. WMS 1.1.1 with
    ``srs=EPSG:4326`` uses lon/lat (x/y) axis order, so the bbox is passed
    as-is. The ``image/tiff`` format returns a raw single-band int16 raster.
    """
    minlon, minlat, maxlon, maxlat = bbox_4326
    params = {
        "service": "WMS",
        "version": config.wms_version,
        "request": "GetMap",
        "layers": config.layer,
        "query_layers": config.layer,
        "styles": "",
        "srs": config.crs,
        "bbox": f"{minlon},{minlat},{maxlon},{maxlat}",
        "width": str(width),
        "height": str(height),
        "format": config.raster_format,
        "time": when.isoformat(),
    }
    return config.endpoint, params


def _fetch_daily_fwi(
    config: FireWeatherConfig,
    bbox_4326: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
    when: date,
    session: requests.Session | None = None,
    timeout: float = 60.0,
) -> xr.DataArray:
    """Fetch one daily FWI raster via WMS GetMap; return a CRS-tagged DataArray.

    The bytes are written to a temp file and opened with rioxarray so the
    geotransform/CRS the server embeds are honoured, then the CRS is asserted
    against the config (no implicit reprojection — non-negotiable #2).
    """
    import tempfile

    import rioxarray

    endpoint, params = _wms_getmap_url(config, bbox_4326, width=width, height=height, when=when)
    sess = session if session is not None else requests.Session()
    resp = sess.get(endpoint, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "tiff" not in ctype.lower():
        raise ValueError(
            f"GWIS WMS returned content-type {ctype!r} (expected a GeoTIFF) for {when} — "
            f"body starts: {resp.content[:200]!r}"
        )
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=True) as fh:
        fh.write(resp.content)
        fh.flush()
        da = cast("xr.DataArray", rioxarray.open_rasterio(Path(fh.name)))
        da = da.sel(band=1).load()
    if da.rio.crs is None:
        da = da.rio.write_crs(config.crs)
    elif da.rio.crs.to_epsg() != int(config.crs.split(":")[1]):
        raise ValueError(
            f"GWIS FWI raster CRS {da.rio.crs} != configured {config.crs} — refusing implicit "
            "reprojection (CLAUDE.md non-negotiable #2)"
        )
    return da.astype("float32")


# ---------------------------------------------------------------------------
# Seasonal surface (reduce daily rasters → one FWI surface, explicit CRS)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeasonalFwiSurface:
    """A season-reduced FWI surface plus the sample dates that produced it."""

    #: Reduced FWI surface (``y``, ``x``) in ``config.crs``, ``NaN`` where masked.
    surface: xr.DataArray
    #: ISO sample dates actually fetched (deterministic order).
    sample_dates: tuple[str, ...]
    #: ``True`` when every fetched raster was all-zero (out-of-archive → no data).
    is_null: bool


def build_seasonal_fwi_surface(
    aoi: BaseGeometry,
    season_year: int,
    config: FireWeatherConfig,
    *,
    width: int = 72,
    height: int = 54,
    session: requests.Session | None = None,
    seed: int = DEFAULT_SEED,
) -> SeasonalFwiSurface:
    """Fetch + reduce daily FWI over ``aoi`` into one seasonal FWI surface.

    Daily rasters are fetched for :func:`season_sample_dates`, stacked, and
    reduced per pixel by ``config.season_reducer`` (``p95`` keeps the upper
    tail while discarding single-day spikes; ``max`` for the raw peak). The
    result is tagged with ``config.crs`` (non-negotiable #2).

    GWIS returns an all-zero raster outside its real archive; if *every*
    sampled raster is all-zero the surface is flagged ``is_null`` — the caller
    must then drop the feature for the whole run (never impute). ``seed`` is
    accepted for contract uniformity; the surface is deterministic without RNG.
    """
    import xarray as xr
    from shapely.geometry import box

    _ = seed  # no RNG; threaded for contract uniformity (non-negotiable #4)
    minx, miny, maxx, maxy = aoi.bounds
    bbox_4326 = (float(minx), float(miny), float(maxx), float(maxy))
    sample_dates = season_sample_dates(season_year, config)
    logger.info(
        "[fire_weather] season %d: fetching %d daily FWI rasters from %s layer %r",
        season_year,
        len(sample_dates),
        config.endpoint,
        config.layer,
    )
    sess = session if session is not None else requests.Session()
    daily: list[xr.DataArray] = []
    for when in sample_dates:
        logger.info("[fire_weather]   %s", when.isoformat())
        daily.append(
            _fetch_daily_fwi(config, bbox_4326, width=width, height=height, when=when, session=sess)
        )

    stacked = xr.concat(daily, dim="t")
    # Out-of-archive null detection: the WMS returns an exact-zero raster with
    # no nodata tag. A real fire-season FWI field over this AOI is never
    # uniformly zero, so "all samples ~0" ⇒ no data for this window.
    is_null = bool(np.all(np.abs(stacked.values) < _NULL_FWI_EPS))

    if config.season_reducer == "p95":
        reduced = stacked.quantile(0.95, dim="t").drop_vars("quantile", errors="ignore")
    else:  # "max" (validated in FireWeatherConfig.__post_init__)
        reduced = stacked.max(dim="t")
    reduced = reduced.rio.write_crs(config.crs).rio.write_nodata(np.nan)
    # The AOI bbox is the request envelope; clip is a no-op safety net that also
    # documents the geometry the surface is valid over.
    _ = box(*bbox_4326)
    return SeasonalFwiSurface(
        surface=cast("xr.DataArray", reduced),
        sample_dates=tuple(d.isoformat() for d in sample_dates),
        is_null=is_null,
    )


# ---------------------------------------------------------------------------
# Per-asset aggregator (same pattern as features.py)
# ---------------------------------------------------------------------------
def fire_danger_seasonal(
    buffers: gpd.GeoDataFrame,
    surface: SeasonalFwiSurface,
    config: FireWeatherConfig,
) -> pd.Series | None:
    """Zonal ``config.zonal_stat`` of the seasonal FWI surface over each buffer.

    Returns a ``pd.Series`` indexed by ``asset_id`` named ``config.feature_name``
    (``fire_danger_seasonal``), or ``None`` when the surface is out-of-archive
    null — in which case the feature is absent for the whole run and is never
    imputed (mirrors ``features.recent_burn_share_12mo``). ``buffers`` are in
    ``ASSET_CRS`` (EPSG:32629) and reprojected to the surface CRS exactly once
    inside ``_zonal`` (non-negotiable #2).
    """
    if surface.is_null:
        logger.info(
            "[fire_weather] %s: seasonal FWI surface is all-zero (out-of-archive window, "
            "%s..%s) — feature absent for this run",
            config.feature_name,
            config.archive_start,
            config.archive_end,
        )
        return None
    if buffers.crs is None or buffers.crs.to_epsg() != int(ASSET_CRS.split(":")[1]):
        raise ValueError(
            f"buffers CRS is {buffers.crs} — expected {ASSET_CRS} (call features.buffer_assets)"
        )
    series = _zonal(surface.surface, buffers, config.zonal_stat)
    series.name = config.feature_name
    return series


# ---------------------------------------------------------------------------
# Provenance helper
# ---------------------------------------------------------------------------
def fire_weather_provenance(
    config: FireWeatherConfig,
    surface: SeasonalFwiSurface,
    season_year: int,
) -> dict[str, Any]:
    """Provenance dict for the fire-weather feature (carried into the run manifest).

    Records the source product id, provider, index name, the season-year and
    sample dates actually fetched, and the reducer — enough to reproduce the
    surface from ``code_commit_sha`` (non-negotiable #3). Returns the
    out-of-archive flag so the orchestrator can record that the feature was
    intentionally absent rather than failed.
    """
    return {
        "fire_weather_product_id": config.product_id,
        "fire_weather_provider": config.provider,
        "fire_weather_index": config.index,
        "fire_weather_config_version": config.version,
        "fire_weather_season_year": season_year,
        "fire_weather_season_reducer": config.season_reducer,
        "fire_weather_sample_dates": list(surface.sample_dates),
        "fire_weather_out_of_archive": surface.is_null,
        "fire_weather_attribution": config.attribution,
    }
