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


# ===========================================================================
# EWDS current-season FWI source (CEMS Early Warning Data Store)
# ===========================================================================
# A SECOND, INDEPENDENT fire-weather source alongside the GWIS backtest above.
# Where GWIS carries a ~2014..2020 reanalysis archive, the CEMS EWDS
# `cems-fire-historical-v1` reanalysis is updated daily and reaches the current
# season (~2-day lag). We ingest the FULL Canadian FWI *system* (FWI + its five
# components) plus the U.S. NFDRS burning index as AVAILABLE-but-UNWEIGHTED
# relative inputs. These are observed reanalysis danger *indices* — one
# normalised input each to a relative within-AOI screening rank, never a
# probability or a forecast of ignition (CLAUDE.md non-negotiable #6).
#
# Determinism (#4): a single-day pull is a fixed function of ``(valid_date,
# config)``; there is no RNG, but the entry points accept and thread ``seed``
# for contract uniformity. CRS (#2): the netCDF arrives in EPSG:4326 — the CRS
# is asserted explicitly and the 0..360 longitude convention is normalised to
# -180..180 before the grid is tagged. Credentials (security): the EWDS key is
# read from ``CDSAPI_KEY`` or parsed from ``~/.cdsapirc``; it is NEVER logged.
# The public EWDS api-base lives in config and overrides the ``~/.cdsapirc``
# url (which points at the CDS, not the EWDS).


@dataclass(frozen=True)
class EwdsFwiVariable:
    """One requested FWI variable: CEMS request name, netCDF var, feature column."""

    request_name: str
    netcdf_var: str
    feature_name: str


@dataclass(frozen=True)
class EwdsFwiConfig:
    """Parsed ``ewds_fwi:`` block of ``config/fire_weather.yaml`` (validated)."""

    version: str
    api_base: str
    dataset: str
    product_type: str
    dataset_type: str
    system_version: str
    grid: str
    data_format: str
    crs: str
    product_id: str
    provider: str
    doi: str
    license: str
    attribution: str
    variables: tuple[EwdsFwiVariable, ...]
    zonal_stat: str

    def __post_init__(self) -> None:
        if not self.variables:
            raise ValueError("ewds_fwi.variables must be non-empty")
        # The dotted system-version forms return HTTP 400; the underscore form is
        # the only one the EWDS process accepts (verified live 2026-06-16).
        if "." in self.system_version:
            raise ValueError(
                f"system_version {self.system_version!r} uses a dotted form; EWDS requires the "
                "underscore form (e.g. '4_1') — dotted forms return HTTP 400"
            )
        if self.data_format != "netcdf":
            raise ValueError(f"unsupported data_format {self.data_format!r} (only 'netcdf')")
        feats = [v.feature_name for v in self.variables]
        if len(set(feats)) != len(feats):
            raise ValueError(f"duplicate feature names in ewds_fwi.variables: {feats}")
        ncs = [v.netcdf_var for v in self.variables]
        if len(set(ncs)) != len(ncs):
            raise ValueError(f"duplicate netcdf vars in ewds_fwi.variables: {ncs}")

    @property
    def feature_names(self) -> tuple[str, ...]:
        """Per-component feature column names, in config order."""
        return tuple(v.feature_name for v in self.variables)


def load_ewds_fwi_config(path: Path) -> EwdsFwiConfig:
    """Load + validate the ``ewds_fwi:`` block of ``config/fire_weather.yaml``."""
    import yaml

    payload = yaml.safe_load(path.read_text())
    src = payload["ewds_fwi"]
    variables = tuple(
        EwdsFwiVariable(
            request_name=str(req_name),
            netcdf_var=str(spec["netcdf"]),
            feature_name=str(spec["feature"]),
        )
        for req_name, spec in src["variables"].items()
    )
    return EwdsFwiConfig(
        version=str(payload["version"]),
        api_base=str(src["api_base"]),
        dataset=str(src["dataset"]),
        product_type=str(src["product_type"]),
        dataset_type=str(src["dataset_type"]),
        system_version=str(src["system_version"]),
        grid=str(src["grid"]),
        data_format=str(src["data_format"]),
        crs=str(src["crs"]),
        product_id=str(src["product_id"]),
        provider=str(src["provider"]),
        doi=str(src["doi"]),
        license=str(src["license"]),
        attribution=str(src["attribution"]),
        variables=variables,
        zonal_stat=str(src["zonal_stat"]),
    )


# ---------------------------------------------------------------------------
# Credentials — env first, then ~/.cdsapirc; the key is never logged/returned
# in any provenance dict (security: secrets stay local).
# ---------------------------------------------------------------------------
def load_ewds_key(rc_path: Path | None = None) -> str:
    """Resolve the Copernicus EWDS API key: ``CDSAPI_KEY`` env, else ``~/.cdsapirc``.

    Only the *key* is taken from ``~/.cdsapirc``; its ``url:`` points at the CDS
    and is deliberately ignored — the EWDS api-base lives in config and is
    public. The returned key is a secret: callers must never log it.
    """
    import os

    env = os.environ.get("CDSAPI_KEY")
    if env:
        return env.strip()
    rc = rc_path if rc_path is not None else Path.home() / ".cdsapirc"
    if not rc.exists():
        raise ValueError(
            "no EWDS credentials: set CDSAPI_KEY or provide ~/.cdsapirc with a 'key:' line "
            "(get a Copernicus EWDS key at https://ewds.climate.copernicus.eu/)"
        )
    for line in rc.read_text().splitlines():
        if line.strip().startswith("key:"):
            return line.split(":", 1)[1].strip()
    raise ValueError(f"{rc} has no 'key:' line")


# ---------------------------------------------------------------------------
# Single-day fetch of ALL requested FWI components (one netCDF, explicit CRS,
# longitude normalised 0..360 -> -180..180).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EwdsFwiSurface:
    """One day's per-component FWI surfaces plus the netCDF valid date.

    ``components`` maps each feature name to a CRS-tagged ``(y, x)`` DataArray in
    ``EPSG:4326`` (longitude already normalised to -180..180). ``valid_date`` is
    the netCDF ``valid_time`` (the observed reanalysis day, ~2 days behind real
    time). ``is_null`` flags an out-of-range request whose surfaces are all NaN
    or all-zero — the feature is then absent for the run (never imputed).
    """

    components: dict[str, xr.DataArray]
    valid_date: date
    requested_date: date
    is_null: bool


def _normalize_longitude(da: xr.DataArray) -> xr.DataArray:
    """Map a DataArray's longitude from the 0..360 convention to -180..180.

    EWDS netCDF returns longitude in 0..360 (351.4 == -8.6). We shift any value
    >180 by -360 and re-sort ascending so the grid is monotonic in -180..180.
    The transform is a pure relabel of the x-coordinate; values are untouched.
    """
    lon_name = "x" if "x" in da.coords else "longitude"
    lon = da[lon_name].values
    shifted = np.where(lon > 180.0, lon - 360.0, lon)
    da = da.assign_coords({lon_name: shifted})
    return da.sortby(lon_name)


def _fetch_ewds_fwi_day(
    config: EwdsFwiConfig,
    bbox_4326: tuple[float, float, float, float],
    when: date,
    *,
    key: str,
    client: Any | None = None,
) -> tuple[dict[str, xr.DataArray], date]:
    """Pull all configured FWI components for ``when`` as CRS-tagged DataArrays.

    ``bbox_4326`` is ``(minlon, minlat, maxlon, maxlat)``; the EWDS ``area`` is
    ``[N, W, S, E]``. One netCDF carries every requested variable; each is read
    out under its real netCDF name (the request names differ — mapped in
    config), CRS-asserted to EPSG:4326, and longitude-normalised. Returns the
    per-feature DataArrays and the netCDF ``valid_time`` date.
    """
    import tempfile

    import cdsapi
    import rioxarray  # noqa: F401  (registers the .rio accessor used below)
    import xarray as xr

    minlon, minlat, maxlon, maxlat = bbox_4326
    area = [round(maxlat, 3), round(minlon, 3), round(minlat, 3), round(maxlon, 3)]
    request: dict[str, Any] = {
        "product_type": [config.product_type],
        "dataset_type": config.dataset_type,
        "system_version": [config.system_version],
        "year": [f"{when.year:04d}"],
        "month": [f"{when.month:02d}"],
        "day": [f"{when.day:02d}"],
        "grid": config.grid,
        "variable": [v.request_name for v in config.variables],
        "data_format": config.data_format,
        "area": area,
    }
    cli: Any = client
    if cli is None:
        # The ~/.cdsapirc url is the CDS; override to the public EWDS api-base.
        cli = cdsapi.Client(
            url=config.api_base, key=key, quiet=True, progress=False, wait_until_complete=True
        )
    logger.info(
        "[fire_weather/ewds] %s: requesting %d FWI components from %s (%s, %s)",
        when.isoformat(),
        len(config.variables),
        config.api_base,
        config.dataset,
        config.dataset_type,
    )
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "ewds_fwi.nc"
        cli.retrieve(config.dataset, request, str(target))
        ds = cast("xr.Dataset", xr.open_dataset(target, engine="netcdf4")).load()

    # valid_time is the observed reanalysis day (~2-day lag); take the first.
    valid = _coerce_valid_date(ds)

    components: dict[str, xr.DataArray] = {}
    for var in config.variables:
        if var.netcdf_var not in ds.variables:
            raise ValueError(
                f"EWDS netCDF missing variable {var.netcdf_var!r} for request "
                f"{var.request_name!r} (got {sorted(str(v) for v in ds.data_vars)})"
            )
        da = cast("xr.DataArray", ds[var.netcdf_var])
        # Collapse the singleton time dim deterministically (first valid day).
        for tdim in ("valid_time", "time"):
            if tdim in da.dims:
                da = da.isel({tdim: 0}, drop=True)
        da = da.rename({"latitude": "y", "longitude": "x"})
        da = _normalize_longitude(da)
        da = da.rio.write_crs(config.crs)
        if da.rio.crs is None or da.rio.crs.to_epsg() != int(config.crs.split(":")[1]):
            raise ValueError(
                f"EWDS FWI surface CRS {da.rio.crs} != configured {config.crs} — refusing "
                "implicit reprojection (CLAUDE.md non-negotiable #2)"
            )
        components[var.feature_name] = da.astype("float32").rio.write_nodata(np.nan)
    return components, valid


def _coerce_valid_date(ds: xr.Dataset) -> date:
    """Read the netCDF ``valid_time``/``time`` coordinate as a single ``date``."""
    import pandas as pd

    for name in ("valid_time", "time"):
        if name in ds.coords or name in ds.variables:
            raw = ds[name].values
            first = np.asarray(raw).ravel()[0]
            return cast("date", pd.Timestamp(first).date())
    raise ValueError(
        f"EWDS netCDF has no valid_time/time coordinate (got {sorted(str(c) for c in ds.coords)})"
    )


def build_ewds_fwi_surface(
    aoi: BaseGeometry,
    when: date,
    config: EwdsFwiConfig,
    *,
    key: str | None = None,
    client: Any | None = None,
    seed: int = DEFAULT_SEED,
) -> EwdsFwiSurface:
    """Fetch all configured FWI components for ``when`` over ``aoi``.

    A single EWDS netCDF download carries every requested component; each is
    returned as a CRS-tagged (EPSG:4326), longitude-normalised surface keyed by
    feature name. If every component surface is all-NaN or all-zero (an
    out-of-range request) the result is flagged ``is_null`` and the caller drops
    the features for the whole run — never imputed. ``seed`` is accepted for
    contract uniformity; a single-day pull is deterministic without RNG.
    """
    _ = seed  # no RNG; threaded for contract uniformity (non-negotiable #4)
    resolved_key = key if key is not None else load_ewds_key()
    minx, miny, maxx, maxy = aoi.bounds
    bbox_4326 = (float(minx), float(miny), float(maxx), float(maxy))
    components, valid = _fetch_ewds_fwi_day(
        config, bbox_4326, when, key=resolved_key, client=client
    )
    is_null = all(
        bool(
            np.all(~np.isfinite(da.values))
            or np.all(np.abs(np.nan_to_num(da.values)) < _NULL_FWI_EPS)
        )
        for da in components.values()
    )
    return EwdsFwiSurface(
        components=components, valid_date=valid, requested_date=when, is_null=is_null
    )


# ---------------------------------------------------------------------------
# Per-asset, per-component zonal aggregator (reuses features._zonal).
# ---------------------------------------------------------------------------
def ewds_fwi_components(
    buffers: gpd.GeoDataFrame,
    surface: EwdsFwiSurface,
    config: EwdsFwiConfig,
) -> dict[str, pd.Series] | None:
    """Zonal ``config.zonal_stat`` of each FWI component surface over the buffers.

    Returns a dict ``{feature_name: pd.Series[asset_id]}`` (one entry per
    configured component), or ``None`` when the surfaces are out-of-range null —
    in which case the features are absent for the whole run and never imputed
    (mirrors :func:`fire_danger_seasonal`). ``buffers`` are in ``ASSET_CRS``
    (EPSG:32629) and reprojected to the surface CRS exactly once inside
    ``_zonal`` (non-negotiable #2).
    """
    if surface.is_null:
        logger.info(
            "[fire_weather/ewds] surfaces are all-null for requested %s — features absent",
            surface.requested_date,
        )
        return None
    if buffers.crs is None or buffers.crs.to_epsg() != int(ASSET_CRS.split(":")[1]):
        raise ValueError(
            f"buffers CRS is {buffers.crs} — expected {ASSET_CRS} (call features.buffer_assets)"
        )
    out: dict[str, pd.Series] = {}
    for var in config.variables:
        da = surface.components[var.feature_name]
        series = _zonal(da, buffers, config.zonal_stat)
        series.name = var.feature_name
        out[var.feature_name] = series
    return out


# ---------------------------------------------------------------------------
# Provenance helper (records dataset DOI, system version, valid date; no key).
# ---------------------------------------------------------------------------
def ewds_fwi_provenance(
    config: EwdsFwiConfig,
    surface: EwdsFwiSurface,
) -> dict[str, Any]:
    """Provenance dict for the EWDS FWI components (carried into the run manifest).

    Records the dataset product id, DOI, dataset-type, system-version, the
    requested and netCDF ``valid_time`` dates, and the request->netcdf variable
    map — enough to reproduce the pull from ``code_commit_sha`` (non-negotiable
    #3). The API key is NEVER included. ``fwi_valid_date`` is the observed
    reanalysis day (~2-day lag), distinct from the requested date.
    """
    return {
        "fwi_product_id": config.product_id,
        "fwi_provider": config.provider,
        "fwi_doi": config.doi,
        "fwi_license": config.license,
        "fwi_config_version": config.version,
        "fwi_dataset_type": config.dataset_type,
        "fwi_system_version": config.system_version,
        "fwi_requested_date": surface.requested_date.isoformat(),
        "fwi_valid_date": surface.valid_date.isoformat(),
        "fwi_variable_map": {v.request_name: v.netcdf_var for v in config.variables},
        "fwi_feature_names": list(config.feature_names),
        "fwi_attribution": config.attribution,
    }


# ---------------------------------------------------------------------------
# Display-COG export of the EWDS FWI surfaces for the geobrowser overlay.
# Each component is reprojected EPSG:4326 -> EPSG:3857 (BILINEAR — these are
# continuous danger *indices*, not categorical codes) and written as a
# GoogleMapsCompatible COG so maplibre-cog-protocol can render it client-side.
# ---------------------------------------------------------------------------

#: Web-Mercator display CRS for the geobrowser COG overlays (maplibre-cog-protocol
#: renders EPSG:3857 COGs only — the EWDS netCDF arrives in EPSG:4326).
FWI_DISPLAY_CRS = "EPSG:3857"


def fwi_component_value_range(surface: EwdsFwiSurface, feature_name: str) -> tuple[float, float]:
    """Finite (min, max) of one component surface — for the overlay colour ramp.

    Raises if the component is entirely non-finite (an out-of-range pull); the
    caller should have checked ``surface.is_null`` first.
    """
    da = surface.components[feature_name]
    vals = np.asarray(da.values, dtype="float64")
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        raise ValueError(f"component {feature_name!r} has no finite values")
    return float(np.min(vals)), float(np.max(vals))


def write_fwi_component_cog(
    surface: EwdsFwiSurface,
    feature_name: str,
    dst_path: Path,
) -> None:
    """Reproject one EWDS FWI component to EPSG:3857 and write a display COG.

    The component arrives in EPSG:4326 (explicit CRS asserted at fetch). It is
    reprojected to :data:`FWI_DISPLAY_CRS` with NEAREST resampling — the FWI
    field is genuinely a coarse 0.25° (~28 km) grid, and the honesty bar
    (non-negotiable #6) forbids interpolating it to look finer than it is, so we
    keep the cells discrete rather than bilinearly smoothing them across the
    warp. ``NaN`` nodata is set on the array AND written into the COG's nodata
    tag (``nodata=np.nan`` below) so maplibre-cog-protocol sees ``metadata.noData``
    and the client paints every out-of-grid / ocean / no-coverage pixel FULLY
    TRANSPARENT — over the wide Iberia display extent most cells outside the
    landmass are nodata and must show the basemap through, never a filled blanket.
    Output is a GoogleMapsCompatible COG (DEFLATE). Source CRS must be explicit
    (non-negotiable #2).
    """
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    from rasterio.enums import Resampling

    da = surface.components[feature_name]
    if da.rio.crs is None:
        raise ValueError(f"component {feature_name!r} has no CRS (cannot reproject)")
    da3857 = da.rio.reproject(FWI_DISPLAY_CRS, resampling=Resampling.nearest, nodata=np.nan)
    da3857 = da3857.rio.write_nodata(np.nan, encoded=False)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    da3857.rio.to_raster(
        dst_path,
        driver="COG",
        compress="DEFLATE",
        dtype="float32",
        nodata=float("nan"),
        BIGTIFF="IF_SAFER",
    )
