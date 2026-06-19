"""Faithful, reproducible record of the full-Iberia display + validation COGs.

The thematic Iberia geobrowser (Inputs / Interim / Output / Validation) renders
four peninsula-scale rasters that were first generated ad-hoc and uploaded to
Cloudflare R2. CLAUDE.md's fact-checking checklist requires every shipped
artefact to be reproducible from a script in this repo; this module IS that
script. It is a faithful RECORD of how each COG was actually built — the heavy
downloads + GDAL warps that WOULD reproduce them byte-for-faithful — not a
re-run on import. Running ``--layer all`` re-executes the full pipeline; the
defaults are tuned so a re-run lands on the same resolutions and projections as
the live artefacts.

Source of truth. The ``slope`` and ``canopy`` functions transcribe the two
detached scripts that built the live COGs (``/tmp/make_iberia_slope.py`` and
``/tmp/make_iberia_canopy.py``) step for step. The ``fuel`` and ``firescope``
functions follow the same shape using the audited library fetchers and the
pinned public sources.

Live artefacts this record maps to (R2 object names, run-id timestamps as
shipped — kept here so the record is traceable to what the geobrowser loads):

* fuel      ``fuel_class_iberia_3857_<rid>.tif``      rid 20260619T123103Z
* slope     ``slope_iberia_3857_<rid>.tif``           rid 20260619T124721Z
* canopy    ``canopy_height_iberia_3857_<rid>.tif``   rid 20260619T124520Z
* firescope ``firescope_iberia_3857_<rid>.tif``       rid 20260619T122124Z

A fresh run produces a NEW ``rid`` (UTC now) and therefore new object names; the
timestamps above are the record of the originally-shipped objects, not a target
to overwrite. Use ``--run-id`` to pin one.

Honesty (non-negotiable #6). Every layer here is an *observed* / *relative*
input, never a forecast:

* fuel  — a static observed fuel-complex classification (NFFL behaviour models).
* slope — observed terrain steepness, in degrees, on an equal-area grid.
* canopy— an observed 2020 canopy-height estimate.
* firescope — a third party's learned *relative wildfire-risk RANK* (uint8,
  undocumented units). It is treated as a rank, NEVER a probability and NEVER an
  ignition forecast, and our own exposure rank is likewise never converted to
  one. It is a VALIDATION / cross-comparison reference layer, not a project
  output.

CRS is explicit at every hop (non-negotiable #2). No identifiers are invented
(non-negotiable #1): the EFFIS / ETH / Cop-DEM / FireScope identifiers are
either resolved live from the source or reused from the audited library
constants (``static_rasters``, ``features``, ``firescope``).

Run::

    uv run python scripts/29_make_iberia_inputs.py --layer slope
    uv run python scripts/29_make_iberia_inputs.py --layer all
    uv run python scripts/29_make_iberia_inputs.py --layer fuel --no-upload
    uv run python scripts/29_make_iberia_inputs.py --layer firescope --dry-run

``--dry-run`` prints the resolved plan (URLs, CRS hops, output name) without
fetching or warping — the cheap way to inspect the record without the heavy
downloads.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import rasterio
import rasterio.shutil
from shapely.geometry import box, mapping

from wildfire_exposure_eo import firescope
from wildfire_exposure_eo import static_rasters as sr
from wildfire_exposure_eo.features import (
    COP_DEM_COLLECTION,
    PC_STAC_URL,
    _default_client_factory,
    _sign_via_endpoint,
)
from wildfire_exposure_eo.stac import code_commit_sha

log = logging.getLogger("iberia_inputs")

# ── constants ──────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[1]
_AOI_IBERIA = _ROOT / "data" / "aoi" / "iberia.geojson"
_CACHE_STATIC = _ROOT / "outputs" / "cache" / "static"
_OUT_DIR = _ROOT / "outputs" / "geobrowser"
_TMP_DIR = _ROOT / "outputs" / "tmp"

#: Iberia display bbox (lon,lat, EPSG:4326) — matches data/aoi/iberia.geojson and
#: the IBERIA_BBOX used by scripts/25 + scripts/30.
_IBERIA_BBOX = (-9.8, 35.9, 3.5, 44.0)

#: Display CRS for every output COG.
_EPSG_DISPLAY = "EPSG:3857"
#: Equal-area metric CRS used for slope (so degrees stay accurate across the three
#: UTM zones Iberia spans).
_EPSG_EQUAL_AREA = "EPSG:3035"

_R2_BUCKET = "r2:wildfire-exposure-eo"
_R2_PUBLIC_HOST = "https://wildfire.cheias.pt"

#: Run-id timestamps of the artefacts currently live on R2 (record only — a fresh
#: run mints a new rid). Mirrored in the module docstring.
SHIPPED_RIDS = {
    "fuel": "20260619T123103Z",
    "slope": "20260619T124721Z",
    "canopy": "20260619T124520Z",
    "firescope": "20260619T122124Z",
}

#: GDAL HTTP knobs for the network /vsicurl/ reads (Cop-DEM, FireScope).
_GDAL_HTTP_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_TIMEOUT": "180",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "3",
}


# ── small helpers ────────────────────────────────────────────────────────────


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _gdal(cmd: Sequence[str], *, env_extra: dict[str, str] | None = None) -> None:
    """Run a GDAL CLI command, raising on failure. Logs the command first."""
    log.info("$ %s", " ".join(cmd))
    env = {**os.environ, **(env_extra or {})}
    subprocess.run(list(cmd), check=True, env=env)


def _describe_cog(path: Path, label: str) -> dict[str, Any]:
    """Open a COG, assert + return its CRS/shape/nodata and a decimated value range.

    CRS is asserted to be the display CRS (non-negotiable #2) — a COG that warped
    to the wrong projection is a bug, caught here before upload.
    """
    with rasterio.open(path) as ds:
        if str(ds.crs) != _EPSG_DISPLAY:
            raise RuntimeError(
                f"{label}: COG CRS is {ds.crs!r}, expected {_EPSG_DISPLAY!r} (non-negotiable #2)"
            )
        nodata = ds.nodata
        decim = ds.read(
            1,
            out_shape=(1, max(1, ds.height // 32), max(1, ds.width // 32)),
        ).astype("float64")
        vals = decim[decim != nodata] if nodata is not None else decim.ravel()
        rng = (round(float(vals.min()), 3), round(float(vals.max()), 3)) if vals.size else None
        info = {
            "crs": str(ds.crs),
            "width": ds.width,
            "height": ds.height,
            "nodata": None if nodata is None else float(nodata),
            "decimated_value_range": rng,
            "size_mb": round(path.stat().st_size / 1e6, 1),
        }
    log.info(
        "%s: COG %s %dx%d nodata=%s decim_range=%s (%.1f MB)",
        label,
        info["crs"],
        info["width"],
        info["height"],
        info["nodata"],
        info["decimated_value_range"],
        info["size_mb"],
    )
    return info


def _write_provenance(path: Path, prov: dict[str, Any]) -> Path:
    """Write a provenance sidecar JSON next to the COG and return its path.

    The sidecar carries the mandatory provenance fields (source, run_id,
    code_commit_sha, fetched_at_utc, resolution, license, attribution) so the
    artefact is self-describing wherever it travels (non-negotiable #3 spirit).
    """
    sidecar = path.with_suffix(path.suffix + ".prov.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")
    log.info("provenance sidecar -> %s", sidecar)
    return sidecar


def _upload_to_r2(path: Path) -> str:
    """Copy the COG to R2 and return its public href (raises if rclone is absent)."""
    if shutil.which("rclone") is None:
        raise RuntimeError(
            "rclone not found on PATH — cannot upload to R2. Install + configure the "
            "'r2:' remote, or pass --no-upload."
        )
    dst = f"{_R2_BUCKET}/{path.name}"
    last_err = ""
    for attempt in (1, 2, 3):
        result = subprocess.run(
            ["rclone", "copyto", str(path), dst],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode == 0:
            log.info("uploaded %s -> %s (attempt %d)", path.name, dst, attempt)
            return f"{_R2_PUBLIC_HOST}/{path.name}"
        last_err = result.stderr.strip()
        log.warning("upload attempt %d failed (transient 501 likely): %s", attempt, last_err)
    raise RuntimeError(f"rclone upload of {path.name} failed after 3 attempts: {last_err}")


# ── fuel: EFFIS European Fuel Map ───────────────────────────────────────────────


def make_fuel(rid: str, *, commit_sha: str, dry_run: bool, upload: bool) -> Path | None:
    """Build the full-Iberia EFFIS fuel-class display COG (NFFL behaviour models).

    Pipeline (faithful to the shipped ``fuel_class_iberia_3857_<rid>.tif``):

    1. ``static_rasters.fetch_effis_fuel_map(cache_dir=outputs/cache/static)`` ->
       ``effis_european_fuel_map.tif`` (the EFFIS European Fuel Map, EPSG:3035,
       ~250 m NFFL classes). The library fetcher handles the EFFIS expired-cert
       redirect and the in-zip filename fix (task #13: the GeoTIFF inside the zip
       was renamed upstream to ``FuelMap2000_NFFL_LAEA.tif``).
    2. ``gdalwarp`` EPSG:3035 -> EPSG:3857, clipped to the Iberia bbox, ``-r near``
       (NEAREST — these are categorical class codes; averaging would invent fuel
       classes that do not exist).
    3. ``rasterio.shutil.copy`` to a GoogleMapsCompatible-tiled COG, NEAREST
       resampling on overviews too (categorical-safe at every zoom).

    The output is a static, observed fuel-complex classification — not a forecast.
    """
    out = _OUT_DIR / f"fuel_class_iberia_3857_{rid}.tif"
    fetched_at = datetime.now(UTC).isoformat()
    prov: dict[str, Any] = {
        "layer": "fuel",
        "artifact": out.name,
        "source": "EFFIS European Fuel Map (JRC / Copernicus), NFFL behaviour models",
        "source_url": sr._EFFIS_URL,
        "source_crs": _EPSG_EQUAL_AREA,
        "display_crs": _EPSG_DISPLAY,
        "resolution": "~250 m native (EPSG:3035); reprojected to EPSG:3857 display COG",
        "resampling": "NEAREST (categorical class codes — averaging would invent classes)",
        "license": sr._EFFIS_LICENSE,
        "attribution": sr._EFFIS_ATTRIBUTION,
        "run_id": rid,
        "code_commit_sha": commit_sha,
        "fetched_at_utc": fetched_at,
        "aoi": str(_AOI_IBERIA.relative_to(_ROOT)),
        "iberia_bbox_4326": list(_IBERIA_BBOX),
        "is_forecast": False,
        "note": "Static observed fuel-complex classification; not a forecast.",
    }

    if dry_run:
        log.info("[fuel] DRY-RUN plan:\n%s", json.dumps(prov, indent=2))
        return None

    rec = sr.fetch_effis_fuel_map(cache_dir=_CACHE_STATIC)
    effis_tif = Path(rec.local_path)
    log.info("[fuel] EFFIS fuel map (EPSG:3035) at %s", effis_tif)

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = _TMP_DIR / f"fuel_iberia_3857raw_{rid}.tif"
    minx, miny, maxx, maxy = _IBERIA_BBOX
    _gdal(
        [
            "gdalwarp",
            "-t_srs",
            _EPSG_DISPLAY,
            "-te",
            str(minx),
            str(miny),
            str(maxx),
            str(maxy),
            "-te_srs",
            "EPSG:4326",
            "-r",
            "near",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-overwrite",
            str(effis_tif),
            str(raw),
        ]
    )
    rasterio.shutil.copy(
        raw,
        out,
        driver="COG",
        TILING_SCHEME="GoogleMapsCompatible",
        RESAMPLING="NEAREST",
        OVERVIEW_RESAMPLING="NEAREST",
        COMPRESS="DEFLATE",
    )
    prov["cog"] = _describe_cog(out, "fuel")
    raw.unlink(missing_ok=True)
    _write_provenance(out, prov)

    if upload:
        prov["r2_href"] = _upload_to_r2(out)
        _write_provenance(out, prov)
        log.info("[fuel] DONE href=%s", prov["r2_href"])
    else:
        log.info("[fuel] DONE (no upload) %s", out)
    return out


# ── slope: Copernicus DEM GLO-30, computed in EPSG:3035 ─────────────────────────


def make_slope(rid: str, *, commit_sha: str, dry_run: bool, upload: bool) -> Path | None:
    """Build the full-Iberia slope display COG from Cop-DEM GLO-30.

    Slope is derived on the equal-area metric grid (EPSG:3035) so degrees stay
    accurate across the three UTM zones Iberia spans, then warped to a 3857 display
    COG. Faithful to the shipped ``slope_iberia_3857_<rid>.tif``:

    1. PC STAC search ``cop-dem-glo-30`` intersecting the Iberia bbox; items sorted
       by id (deterministic), each signed via the PC SAS endpoint -> ``/vsicurl/``
       hrefs (verify-then-act: every candidate id is listed before load).
    2. ``gdalbuildvrt`` over the signed tiles.
    3. ``gdalwarp`` -> EPSG:3035, 30 m, bilinear -> equal-area DEM mosaic.
    4. ``gdaldem slope`` (degrees) on the metric grid (``-compute_edges``).
    5. ``gdalwarp`` -> EPSG:3857, 40 m, bilinear -> display grid.
    6. ``rasterio.shutil.copy`` to a plain COG (AVERAGE overviews — slope is
       continuous, so averaging on zoom-out is correct).

    Output is observed terrain steepness in degrees — not a forecast.
    """
    out = _OUT_DIR / f"slope_iberia_3857_{rid}.tif"
    fetched_at = datetime.now(UTC).isoformat()
    aoi_geom = box(*_IBERIA_BBOX)
    prov: dict[str, Any] = {
        "layer": "slope",
        "artifact": out.name,
        "source": "Copernicus DEM GLO-30 (via Microsoft Planetary Computer STAC)",
        "source_collection": COP_DEM_COLLECTION,
        "source_stac": PC_STAC_URL,
        "compute_crs": _EPSG_EQUAL_AREA,
        "display_crs": _EPSG_DISPLAY,
        "resolution": "30 m DEM mosaic on EPSG:3035; slope warped to 40 m EPSG:3857 display",
        "method": "gdaldem slope (degrees) on the equal-area grid for cross-UTM-zone accuracy",
        "license": (
            "Cop-DEM ESA open data, attribution required: (c) DLR e.V. 2010-2014 and "
            "(c) Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the "
            "European Union and ESA; all rights reserved."
        ),
        "attribution": "Copernicus DEM GLO-30 (ESA / DLR / Airbus)",
        "run_id": rid,
        "code_commit_sha": commit_sha,
        "fetched_at_utc": fetched_at,
        "aoi": str(_AOI_IBERIA.relative_to(_ROOT)),
        "iberia_bbox_4326": list(_IBERIA_BBOX),
        "is_forecast": False,
        "note": "Observed terrain steepness (degrees); not a forecast.",
    }

    if dry_run:
        log.info("[slope] DRY-RUN plan:\n%s", json.dumps(prov, indent=2))
        return None

    cli = _default_client_factory(PC_STAC_URL)
    items = sorted(
        cli.search(collections=[COP_DEM_COLLECTION], intersects=mapping(aoi_geom)).items(),
        key=lambda it: it.id,
    )
    log.info("[slope] %d cop-dem tiles intersect Iberia", len(items))
    if not items:
        raise RuntimeError(f"no {COP_DEM_COLLECTION} items intersect the Iberia bbox")
    hrefs: list[str] = []
    for it in items:
        log.info("[slope]   tile id=%s", it.id)
        signed = _sign_via_endpoint(it, ("data",))
        href = signed.assets["data"].href
        hrefs.append("/vsicurl/" + href if href.startswith("http") else href)
    prov["source_item_ids"] = [it.id for it in items]

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    lst = _TMP_DIR / f"copdem_{rid}.txt"
    lst.write_text("\n".join(hrefs))
    vrt = _TMP_DIR / f"copdem_iberia_{rid}.vrt"
    _gdal(["gdalbuildvrt", "-input_file_list", str(lst), str(vrt)])

    dem3035 = _TMP_DIR / f"copdem_iberia_3035_{rid}.tif"
    _gdal(
        [
            "gdalwarp",
            "-t_srs",
            _EPSG_EQUAL_AREA,
            "-te_srs",
            "EPSG:4326",
            "-te",
            "-9.8",
            "35.9",
            "3.5",
            "44.0",
            "-tr",
            "30",
            "30",
            "-r",
            "bilinear",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-co",
            "BIGTIFF=YES",
            "-overwrite",
            str(vrt),
            str(dem3035),
        ],
        env_extra=_GDAL_HTTP_ENV,
    )
    log.info("[slope] DEM mosaic (3035, 30 m) built")

    slope3035 = _TMP_DIR / f"slope_iberia_3035_{rid}.tif"
    _gdal(
        [
            "gdaldem",
            "slope",
            str(dem3035),
            str(slope3035),
            "-compute_edges",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-co",
            "BIGTIFF=YES",
        ]
    )
    log.info("[slope] slope (degrees) computed on metric grid")

    slope3857raw = _TMP_DIR / f"slope_iberia_3857raw_{rid}.tif"
    _gdal(
        [
            "gdalwarp",
            "-t_srs",
            _EPSG_DISPLAY,
            "-tr",
            "40",
            "40",
            "-r",
            "bilinear",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-co",
            "BIGTIFF=YES",
            "-overwrite",
            str(slope3035),
            str(slope3857raw),
        ]
    )
    rasterio.shutil.copy(
        slope3857raw, out, driver="COG", OVERVIEW_RESAMPLING="AVERAGE", COMPRESS="DEFLATE"
    )
    prov["cog"] = _describe_cog(out, "slope")
    for tmp in (dem3035, slope3035, slope3857raw):
        tmp.unlink(missing_ok=True)
    _write_provenance(out, prov)

    if upload:
        prov["r2_href"] = _upload_to_r2(out)
        _write_provenance(out, prov)
        log.info("[slope] DONE href=%s", prov["r2_href"])
    else:
        log.info("[slope] DONE (no upload) %s", out)
    return out


# ── canopy: ETH Global Canopy Height 10 m 2020 ─────────────────────────────────


def make_canopy(rid: str, *, commit_sha: str, dry_run: bool, upload: bool) -> Path | None:
    """Build the full-Iberia ETH GCH canopy-height display COG.

    Faithful to the shipped ``canopy_height_iberia_3857_<rid>.tif``:

    1. ``static_rasters.compute_eth_gch_tile_ids`` over the Iberia AOI geometry ->
       the 3-degree ETH GCH tile IDs; ``fetch_eth_gch_tile`` each (ocean tiles
       404 — skipped gracefully). Cached under outputs/cache/static.
    2. ``gdalbuildvrt`` over the downloaded 10 m tiles.
    3. ``gdalwarp`` -> EPSG:3857, ``-tr 40 -r average`` (~30 m ground at mid-Iberia
       latitude; average is correct for a continuous height field).
    4. ``rasterio.shutil.copy`` to a PLAIN COG (no GoogleMapsCompatible UPPER snap
       — that 4x-bloated the FireScope COG; overviews handle zoom-out).

    Output is an observed 2020 canopy-height estimate (Lang et al. 2023) — not a
    forecast.
    """
    out = _OUT_DIR / f"canopy_height_iberia_3857_{rid}.tif"
    fetched_at = datetime.now(UTC).isoformat()
    prov: dict[str, Any] = {
        "layer": "canopy",
        "artifact": out.name,
        "source": "ETH Global Canopy Height 10 m 2020 (Lang et al. 2023)",
        "source_doi": "10.3929/ethz-b-000609802",
        "source_url_pattern": sr._ETH_GCH_BASE_URL,
        "source_crs": "EPSG:4326 (per-tile COG)",
        "display_crs": _EPSG_DISPLAY,
        "resolution": "10 m native; downsampled to ~30 m ground (EPSG:3857, -tr 40, average)",
        "resampling": "AVERAGE (continuous canopy-height field)",
        "license": sr._ETH_GCH_LICENSE,
        "attribution": sr._ETH_GCH_ATTRIBUTION,
        "vintage": "2020",
        "run_id": rid,
        "code_commit_sha": commit_sha,
        "fetched_at_utc": fetched_at,
        "aoi": str(_AOI_IBERIA.relative_to(_ROOT)),
        "iberia_bbox_4326": list(_IBERIA_BBOX),
        "is_forecast": False,
        "note": "Observed 2020 canopy-height estimate; not a forecast.",
    }

    aoi_geom = gpd.read_file(_AOI_IBERIA).geometry.iloc[0]
    tiles = sr.compute_eth_gch_tile_ids(aoi_geom)
    prov["candidate_tile_ids"] = tiles
    log.info("[canopy] %d candidate 3deg tiles: %s", len(tiles), tiles)

    if dry_run:
        log.info("[canopy] DRY-RUN plan:\n%s", json.dumps(prov, indent=2))
        return None

    paths: list[str] = []
    for tile in tiles:
        try:
            rec = sr.fetch_eth_gch_tile(tile, cache_dir=_CACHE_STATIC)
            paths.append(rec.local_path)
            log.info("[canopy] fetched %s -> %s", tile, rec.local_path)
        except Exception as exc:  # ocean tiles 404 — skip gracefully
            log.info("[canopy] skip %s: %s", tile, repr(exc)[:160])
    prov["fetched_tile_ids"] = [Path(p).name for p in paths]
    if not paths:
        raise RuntimeError("[canopy] no ETH GCH tiles fetched over Iberia")
    log.info("[canopy] %d tiles downloaded", len(paths))

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    vrt = _TMP_DIR / f"canopy_iberia_{rid}.vrt"
    _gdal(["gdalbuildvrt", str(vrt), *paths])

    raw = _TMP_DIR / f"canopy_iberia_raw_{rid}.tif"
    _gdal(
        [
            "gdalwarp",
            "-t_srs",
            _EPSG_DISPLAY,
            "-te",
            "-9.8",
            "35.9",
            "3.5",
            "44.0",
            "-te_srs",
            "EPSG:4326",
            "-tr",
            "40",
            "40",
            "-r",
            "average",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-overwrite",
            str(vrt),
            str(raw),
        ]
    )
    rasterio.shutil.copy(raw, out, driver="COG", OVERVIEW_RESAMPLING="AVERAGE", COMPRESS="DEFLATE")
    prov["cog"] = _describe_cog(out, "canopy")
    raw.unlink(missing_ok=True)
    _write_provenance(out, prov)

    if upload:
        prov["r2_href"] = _upload_to_r2(out)
        _write_provenance(out, prov)
        log.info("[canopy] DONE href=%s", prov["r2_href"])
    else:
        log.info("[canopy] DONE (no upload) %s", out)
    return out


# ── firescope: INSAIT/ETH oracle_unet relative-risk RANK (validation ref) ───────


def make_firescope(rid: str, *, commit_sha: str, dry_run: bool, upload: bool) -> Path | None:
    """Build the Iberia clip of the FireScope ``oracle_unet`` relative-risk RANK.

    FireScope (INSAIT Institute + ETH Zürich, arXiv:2511.17171) publishes a
    Europe-wide ~30 m uint8 raster ``oracle_unet.tif`` whose band is *their*
    "risk" in *their* terminology — its units are undocumented, so it is treated
    strictly as a RELATIVE RISK RANK, never a probability and never an ignition
    forecast (non-negotiable #6). It is a VALIDATION / cross-comparison reference
    layer in the geobrowser, not a project output. CC-BY-4.0 attribution travels
    with every derived artefact.

    Faithful to the shipped ``firescope_iberia_3857_<rid>.tif``:

    1. ``gdalwarp`` the pinned-revision ``/vsicurl/`` raster -> EPSG:3857, clipped
       to the Iberia bbox, ``-r near`` (NEAREST — preserves the source uint8 rank
       values), ``-dstnodata 255`` (the declared FireScope nodata sentinel). The
       12.3 GB source is byte-range read; it is never fully downloaded.
    2. ``rasterio.shutil.copy`` to a GoogleMapsCompatible-tiled COG, NEAREST.

    Identifiers are reused from the audited :mod:`wildfire_exposure_eo.firescope`
    constants (HF dataset id, pinned revision, attribution) — none invented
    (non-negotiable #1). The HF revision is pinned in the URL so the record points
    at the exact bytes that were warped.
    """
    out = _OUT_DIR / f"firescope_iberia_3857_{rid}.tif"
    fetched_at = datetime.now(UTC).isoformat()
    # Pin the HF revision in the resolve URL (the audited module's RASTER_URL pins
    # 'main'; this record needs the exact revision the live COG was warped from).
    pinned_url = (
        f"https://huggingface.co/datasets/{firescope.HF_DATASET_ID}"
        f"/resolve/{firescope.HF_DATASET_REVISION}/{firescope.RASTER_FILENAME}"
    )
    vsicurl = f"/vsicurl/{pinned_url}"
    prov: dict[str, Any] = {
        "layer": "firescope",
        "artifact": out.name,
        "source": "FireScope oracle_unet (INSAIT Institute + ETH Zürich)",
        "source_dataset": firescope.HF_DATASET_ID,
        "source_revision": firescope.HF_DATASET_REVISION,
        "source_url": pinned_url,
        "source_crs": firescope.FIRESCOPE_CRS,
        "display_crs": _EPSG_DISPLAY,
        "nodata": firescope.FIRESCOPE_NODATA,
        "resolution": "~30 m uint8 source; reprojected/clipped to EPSG:3857 display COG",
        "resampling": "NEAREST (preserves the source uint8 rank values)",
        "license": firescope.LICENSE,
        "attribution": firescope.ATTRIBUTION,
        "arxiv": firescope.ARXIV_ID,
        "run_id": rid,
        "code_commit_sha": commit_sha,
        "fetched_at_utc": fetched_at,
        "iberia_bbox_4326": list(_IBERIA_BBOX),
        "is_forecast": False,
        "semantics": (
            "Relative wildfire-risk RANK (uint8, undocumented units) — NOT a probability "
            "and NOT an ignition forecast. Validation / cross-comparison reference layer."
        ),
    }

    if dry_run:
        log.info("[firescope] DRY-RUN plan:\n%s", json.dumps(prov, indent=2))
        return None

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = _TMP_DIR / f"firescope_iberia_3857raw_{rid}.tif"
    _gdal(
        [
            "gdalwarp",
            "-t_srs",
            _EPSG_DISPLAY,
            "-te",
            "-9.8",
            "35.9",
            "3.5",
            "44.0",
            "-te_srs",
            "EPSG:4326",
            "-r",
            "near",
            "-dstnodata",
            str(firescope.FIRESCOPE_NODATA),
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-overwrite",
            vsicurl,
            str(raw),
        ],
        env_extra={**_GDAL_HTTP_ENV, **firescope.GDAL_VSICURL_ENV},
    )
    rasterio.shutil.copy(
        raw,
        out,
        driver="COG",
        TILING_SCHEME="GoogleMapsCompatible",
        RESAMPLING="NEAREST",
        OVERVIEW_RESAMPLING="NEAREST",
        COMPRESS="DEFLATE",
    )
    prov["cog"] = _describe_cog(out, "firescope")
    raw.unlink(missing_ok=True)
    _write_provenance(out, prov)

    if upload:
        prov["r2_href"] = _upload_to_r2(out)
        _write_provenance(out, prov)
        log.info("[firescope] DONE href=%s", prov["r2_href"])
    else:
        log.info("[firescope] DONE (no upload) %s", out)
    return out


# ── CLI ────────────────────────────────────────────────────────────────────────

_BUILDERS = {
    "fuel": make_fuel,
    "slope": make_slope,
    "canopy": make_canopy,
    "firescope": make_firescope,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Faithful reproducible record of the full-Iberia geobrowser COGs.",
    )
    parser.add_argument(
        "--layer",
        choices=["fuel", "slope", "canopy", "firescope", "all"],
        required=True,
        help="Which Iberia input/validation COG to (re)build.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved plan (URLs, CRS hops, output name) without fetching or warping.",
    )
    parser.add_argument("--no-upload", action="store_true", help="Skip the R2 upload.")
    parser.add_argument("--run-id", default=None, help="Override the run-id (default: UTC now).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    rid = args.run_id or _run_id()
    commit_sha = code_commit_sha(cwd=_ROOT)
    log.info("run_id=%s commit=%s layer=%s dry_run=%s", rid, commit_sha, args.layer, args.dry_run)
    log.info("shipped artefact rids (record): %s", SHIPPED_RIDS)

    layers = ["fuel", "slope", "canopy", "firescope"] if args.layer == "all" else [args.layer]
    upload = not args.no_upload and not args.dry_run
    for layer in layers:
        _BUILDERS[layer](rid, commit_sha=commit_sha, dry_run=args.dry_run, upload=upload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
