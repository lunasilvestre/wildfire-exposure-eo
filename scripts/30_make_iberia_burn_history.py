"""Build the Iberia burn-history validation layer (ICNF-PT + EFFIS-ES) for the geobrowser.

This is a reproducible fetch -> simplify -> merge -> upload pipeline that produces a
single simplified GeoJSON of observed burned-area *perimeters* across the Iberian
Peninsula, for the thematic geobrowser. It is a HISTORY layer (perimeters of fires
that already happened), NOT a forecast (non-negotiable #6).

Two authoritative public, no-auth sources, each with a different resolution and
temporal coverage (the asymmetry is labelled honestly in the output and the report):

* ``ICNF`` (Portugal) — Áreas Ardidas, ArcGIS REST MapServer, fine-grained official
  perimeters in EPSG:3763. Fetched via the existing
  :func:`wildfire_exposure_eo.burns.fetch_burns` over ``data/aoi/portugal_national.geojson``.
  The MapServer is queried for 1975-2025, but its 1975-1989 layer returns attributes
  WITHOUT geometry (verified), so the effective fine-grained perimeter coverage is
  1990-2025. A 10 ha minimum-mapping-unit floor is applied to make ICNF comparable to
  EFFIS's coarser regime (it retains ~98% of total burned hectares).

* ``EFFIS`` (Spain side) — Copernicus EMS / EFFIS ``modis.ba.poly.<year>`` burned-area
  perimeters from the public mapserv WFS at
  ``https://maps.effis.emergency.copernicus.eu/effis``. MODIS/VIIRS-era only
  (per-year poly layers exist 2016-2025), ~30 ha minimum mapping unit, hence
  COARSER and temporally SHORTER than ICNF. EU Data License, no auth.
  Probe order from the work-unit spec was: (a) GWIS WMS GetCapabilities at
  ``ies-ows.jrc.ec.europa.eu/gwis`` — raster-only (MCD64A1 coverages, FWI), NO
  vector perimeter layer; (b) the EFFIS download portal — raw perimeters are
  gated behind a manual Data Request Form; (c) an EFFIS WFS — the mapserv WMS
  endpoint ALSO serves OGC WFS GetFeature returning full GML polygon geometries,
  which is what we use. WFS 1.0.0 is used deliberately: it returns lon,lat axis
  order; WFS 1.1.0 returns swapped lat,lon for EPSG:4326.

Outputs (all EPSG:4326, written under ``outputs/geobrowser/``):

* ``icnf_burns_pt_<rid>.geojson``      — ICNF Portugal, source='ICNF'
* ``effis_burns_es_<rid>.geojson``     — EFFIS Spain-side, source='EFFIS'
* ``iberia_burn_history_<rid>.geojson``— merged, sorted by vintage_year, uploaded to R2

``rid`` is a single UTC ``YYYYmmddTHHMMSSZ`` run-id shared by all three artefacts.

Run:
    uv run python scripts/30_make_iberia_burn_history.py
    uv run python scripts/30_make_iberia_burn_history.py --no-upload   # skip R2
    uv run python scripts/30_make_iberia_burn_history.py --no-effis    # ICNF-PT only

CRS is explicit everywhere (non-negotiable #2). No invented identifiers — every
feature's year/area comes from the live source attributes (non-negotiable #1).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import shape as _shape

from wildfire_exposure_eo.burns import fetch_burns
from wildfire_exposure_eo.stac import code_commit_sha, load_aoi_geometry

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

log = logging.getLogger("iberia_burn_history")

# ── constants ──────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[1]
_OUT_DIR = _ROOT / "outputs" / "geobrowser"
_PARQUET_DIR = _ROOT / "outputs" / "parquet"
_PT_AOI = _ROOT / "data" / "aoi" / "portugal_national.geojson"

_EPSG_OUTPUT = "EPSG:4326"

#: Douglas-Peucker tolerance in degrees, applied AFTER reprojection to EPSG:4326.
#: 0.0009 deg ~ 100 m at Iberian latitudes — display-appropriate at Iberia zoom and
#: a ~4x vertex reduction on the vertex-dense ICNF perimeters. The full national
#: 1990-2025 ICNF history is intrinsically large (tens of thousands of polygons),
#: so this is tuned for a peninsula-scale validation overlay, not a cadastral copy.
_SIMPLIFY_TOL_DEG = 0.0009

#: GeoJSON coordinate precision (decimal places). 6 dp ~ 0.11 m — well below the
#: simplification tolerance, so lossless relative to the simplified geometry while
#: trimming float-string bloat.
_COORD_PRECISION_DP = 6

#: Minimum-mapping-unit floor (hectares) for the ICNF layer. EFFIS maps fires from
#: ~30 ha up; ICNF publishes perimeters down to a fraction of a hectare. Applying a
#: 10 ha floor to ICNF makes the two sources methodologically comparable, drops ~30k
#: sub-display slivers, and still retains ~98% of total ICNF burned hectares. EFFIS
#: is NOT floored again — its source MMU already exceeds this.
_ICNF_MIN_AREA_HA = 10.0

#: Soft size ceiling (MB) for the merged GeoJSON. If exceeded, simplify progressively
#: harder. The full multi-source 35-year history will not fit the original ~8 MB
#: aspiration without destroying per-fire granularity, so this is an upper guard, not
#: a hard target; the file is served from R2 (byte-range capable).
_MAX_MERGED_MB = 16.0

#: Iberia display bbox (lon,lat) — matches IBERIA_BBOX used by 25_make_fwi_cogs.py
#: and data/aoi/iberia.geojson.
_IBERIA_BBOX = (-9.8, 35.9, 3.5, 44.0)

#: EFFIS burned-area perimeter source (Copernicus EMS / EFFIS), public, no auth.
_EFFIS_WFS = "https://maps.effis.emergency.copernicus.eu/effis"
_EFFIS_LAYER_FMT = "modis.ba.poly.{year}"  # per-year poly layers exist 2016-2025
_EFFIS_YEARS = range(2016, 2026)
_EFFIS_LICENSE = "EU Data License (Copernicus EMS / EFFIS) — free, no auth"
_EFFIS_ATTRIBUTION = (
    "EFFIS / Copernicus Emergency Management Service — burned areas (MODIS/VIIRS, >=30 ha)"
)
_ICNF_LICENSE = "ICNF open data, attribution required"
_ICNF_ATTRIBUTION = "ICNF — Áreas Ardidas em Portugal Continental"

#: Mainland-Portugal national polygon (lon,lat) used to drop EFFIS PT-side features
#: so ICNF stays the single source of truth over Portugal. Matches the fetch extent
#: in data/aoi/portugal_national.geojson.
_PT_NATIONAL_COORDS = [
    [-9.55, 36.8],
    [-6.0, 36.8],
    [-6.0, 42.2],
    [-9.55, 42.2],
    [-9.55, 36.8],
]

_USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 burns (+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)
_REQUEST_TIMEOUT = 300
_RETRIES = 3

_R2_BUCKET = "r2:wildfire-exposure-eo"
_R2_PUBLIC_HOST = "https://wildfire.cheias.pt"

#: Final-output columns, in order. Geometry last.
_KEEP_COLS = ["source", "vintage_year", "area_ha", "geometry"]


# ── shared assembly ──────────────────────────────────────────────────────────


def _assemble_layer(
    *,
    source: str,
    geometry: Any,
    vintage_year: Any,
    area_ha: Any,
    min_area_ha: float = 0.0,
) -> gpd.GeoDataFrame:
    """Build a simplified, source-tagged GeoDataFrame and drop empty/null geoms.

    Applies an optional minimum-mapping-unit floor (``min_area_ha``) on the source
    ``area_ha`` BEFORE simplification, then simplifies with Douglas-Peucker (the
    inputs are already in EPSG:4326). Returns an EPSG:4326 GeoDataFrame with exactly
    the canonical columns ``source, vintage_year, area_ha, geometry``. The ``Any``
    parameter types absorb the GeoDataFrame/Series union pyright infers from
    ``frame[col]`` indexing; values are coerced to numpy arrays below.
    """
    years = np.asarray(vintage_year, dtype="int64")
    areas = np.asarray(pd.to_numeric(area_ha, errors="coerce"), dtype="float64").round(2)
    geom_series = gpd.GeoSeries(geometry, crs=_EPSG_OUTPUT)

    if min_area_ha > 0.0:
        big = np.nan_to_num(areas, nan=0.0) >= min_area_ha
        years = years[big]
        areas = areas[big]
        geom_series = geom_series.iloc[np.flatnonzero(big)]

    simplified = geom_series.simplify(_SIMPLIFY_TOL_DEG, preserve_topology=True)
    out = gpd.GeoDataFrame(
        {
            "source": source,
            "vintage_year": years,
            "area_ha": areas,
            "geometry": simplified.to_numpy(),
        },
        crs=_EPSG_OUTPUT,
    )
    keep = (~out.geometry.is_empty) & out.geometry.notna()
    return gpd.GeoDataFrame(out.loc[keep].reset_index(drop=True), crs=_EPSG_OUTPUT)


# ── ICNF (Portugal) ──────────────────────────────────────────────────────────


def build_icnf_pt(rid: str, *, commit_sha: str) -> tuple[Path, gpd.GeoDataFrame]:
    """Fetch ICNF national burns, apply the MMU floor, simplify, write the PT GeoJSON.

    Reuses the audited :func:`wildfire_exposure_eo.burns.fetch_burns` (ArcGIS REST,
    EPSG:3763 -> 4326, per-row provenance + vintage_year) over the national PT
    extent, then reads the GeoParquet back, applies the ICNF MMU floor, simplifies,
    tags source='ICNF'.

    Vintage coverage note: the ICNF MapServer is queried for 1975-2025, but its
    1975-1989 layer (id 14) returns attributes WITHOUT geometry through the REST
    query endpoint (verified: geometryType=None even in native SR with no filter),
    so it is skipped upstream and the effective fine-grained perimeter coverage is
    1990-2025. This is an upstream data-availability limit, not a fetch bug.
    """
    _, aoi_sha = load_aoi_geometry(_PT_AOI)
    parquet_path = _PARQUET_DIR / f"icnf_burns_pt_{rid}.parquet"
    log.info("ICNF: fetching national burns 1975-2025 -> %s", parquet_path.name)
    fetch_burns(
        _PT_AOI,
        parquet_path,
        start_year=1975,
        end_year=2025,
        run_id=rid,
        code_commit_sha=commit_sha,
        aoi_geometry_sha=aoi_sha,
        # National-extent queries on the largest fire-year layers (e.g. 2017,
        # the Pedrógão Grande / Góis year) return ArcGIS error 500 on the pages
        # that contain the mega-fire perimeters (tens of thousands of vertices
        # each) because the full-geometry payload exceeds the server's response
        # cap. 1000 and 500 records/page both fail mid-pagination; 100/page is
        # verified to keep every page (offset 0..2765) under the cap.
        batch_size=100,
    )

    gdf = gpd.read_parquet(parquet_path)
    if gdf.empty:
        raise RuntimeError(
            f"ICNF fetch returned 0 features over {_PT_AOI.name} — refusing to ship an "
            "empty PT layer. Check the MapServer is reachable."
        )
    # The GeoParquet stores geometry as WKB and a live geometry column; ensure CRS.
    if "geometry" not in gdf.columns or gdf.geometry.isna().all():
        gdf = gdf.set_geometry(gpd.GeoSeries.from_wkb(gdf["geometry_wkb"]))
    gdf = gdf.set_crs(_EPSG_OUTPUT, allow_override=True)

    out = _assemble_layer(
        source="ICNF",
        geometry=gdf.geometry,
        vintage_year=gdf["vintage_year"],
        area_ha=gdf["area_ha"],
        min_area_ha=_ICNF_MIN_AREA_HA,
    )

    geojson_path = _OUT_DIR / f"icnf_burns_pt_{rid}.geojson"
    _write_geojson(out, geojson_path)
    log.info(
        "ICNF: %d perimeters, years %d-%d -> %s",
        len(out),
        int(out["vintage_year"].min()),
        int(out["vintage_year"].max()),
        geojson_path.name,
    )
    return geojson_path, out


# ── EFFIS (Spain side) ─────────────────────────────────────────────────────────


def _fetch_effis_year(year: int) -> gpd.GeoDataFrame:
    """WFS GetFeature one EFFIS burned-area year, clipped to the Iberia bbox.

    Uses WFS 1.0.0 (lon,lat axis order). The GML response carries no parseable
    srsName for the reader, so CRS is set explicitly to EPSG:4326 (non-negotiable
    #2). Returns an empty frame on a year with no features.
    """
    minx, miny, maxx, maxy = _IBERIA_BBOX
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typename": _EFFIS_LAYER_FMT.format(year=year),
        "srsName": "EPSG:4326",
        "bbox": f"{minx},{miny},{maxx},{maxy}",
    }
    text = _get_with_retry(_EFFIS_WFS, params=params)
    if "ServiceException" in text[:2000]:
        raise RuntimeError(f"EFFIS WFS error for {year}: {text[:500]}")

    with tempfile.NamedTemporaryFile("w", suffix=".gml", delete=False) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        gdf = gpd.read_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    if gdf.empty:
        return gdf
    # GML reads back with CRS=None — pin it explicitly, never implicitly.
    gdf = gdf.set_crs(_EPSG_OUTPUT, allow_override=True)
    return gdf


def build_effis_es(rid: str) -> tuple[Path, gpd.GeoDataFrame]:
    """Fetch EFFIS perimeters 2016-2025 over Iberia, drop mainland-PT, simplify.

    Mainland Portugal is removed (ICNF owns PT — no double coverage) using the EFFIS
    ``COUNTRY`` attribute (== 'PT') OR a geometric guard: representative point inside
    the PT national polygon.
    """
    pt_national: BaseGeometry = _shape({"type": "Polygon", "coordinates": [_PT_NATIONAL_COORDS]})

    frames: list[gpd.GeoDataFrame] = []
    for year in _EFFIS_YEARS:
        log.info("EFFIS: WFS GetFeature %s", _EFFIS_LAYER_FMT.format(year=year))
        gy = _fetch_effis_year(year)
        if gy.empty:
            log.info("EFFIS: year %d returned 0 features", year)
            continue
        gy["vintage_year"] = _years_from_firedate(gy, fallback=year)
        frames.append(gy)

    if not frames:
        raise RuntimeError(
            "EFFIS WFS returned 0 features across 2016-2025 — the endpoint may be down. "
            "Re-run, or pass --no-effis to ship ICNF-PT only (and flag the Spain gap)."
        )

    raw = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=_EPSG_OUTPUT)
    n_total = len(raw)

    # Drop mainland Portugal: ICNF owns PT. Belt-and-braces: COUNTRY attribute OR
    # representative point inside the PT national polygon.
    in_pt_poly = raw.geometry.representative_point().within(pt_national)
    if "COUNTRY" in raw.columns:
        is_pt = raw["COUNTRY"].astype("string").str.upper().eq("PT").fillna(False) | in_pt_poly
    else:
        is_pt = in_pt_poly
    es_side = gpd.GeoDataFrame(raw.loc[~is_pt].copy(), crs=_EPSG_OUTPUT)
    log.info("EFFIS: dropped %d PT-side features (ICNF owns PT)", n_total - len(es_side))

    if "AREA_HA" in es_side.columns:
        area_ha = es_side["AREA_HA"]
    else:
        area_ha = pd.Series([pd.NA] * len(es_side), index=es_side.index)
    out = _assemble_layer(
        source="EFFIS",
        geometry=es_side.geometry,
        vintage_year=es_side["vintage_year"],
        area_ha=area_ha,
    )

    geojson_path = _OUT_DIR / f"effis_burns_es_{rid}.geojson"
    _write_geojson(out, geojson_path)
    log.info(
        "EFFIS: %d perimeters (Spain side), years %d-%d -> %s",
        len(out),
        int(out["vintage_year"].min()),
        int(out["vintage_year"].max()),
        geojson_path.name,
    )
    return geojson_path, out


def _years_from_firedate(gdf: gpd.GeoDataFrame, *, fallback: int) -> pd.Series:
    """Parse the 4-digit year from EFFIS ``FIREDATE`` (e.g. '2025-01-06 01:11:00').

    Falls back to the per-year layer's year (the source layer name, not an invented
    value) when FIREDATE is missing or unparseable.
    """
    if "FIREDATE" not in gdf.columns:
        return pd.Series([fallback] * len(gdf), index=gdf.index, dtype="int64")
    parsed = pd.to_datetime(gdf["FIREDATE"], errors="coerce").dt.year
    return parsed.fillna(fallback).astype("int64")


# ── merge + write ────────────────────────────────────────────────────────────


def merge_and_write(
    rid: str,
    icnf: gpd.GeoDataFrame,
    effis: gpd.GeoDataFrame | None,
) -> Path:
    """Concat ICNF + EFFIS, sort by vintage_year, write the combined GeoJSON.

    If the merged file exceeds the soft ceiling, re-simplify progressively harder
    (up to 3 extra rounds) and rewrite. The combined 35-year multi-source history
    will not shrink to the original ~8 MB aspiration without destroying per-fire
    granularity, so the ceiling is a guard, not a hard target.
    """
    parts = [icnf[_KEEP_COLS]]
    if effis is not None and not effis.empty:
        parts.append(effis[_KEEP_COLS])
    concatenated = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=_EPSG_OUTPUT)
    order = np.lexsort(
        (
            concatenated["source"].to_numpy(),
            concatenated["vintage_year"].to_numpy(),
        )
    )
    merged = gpd.GeoDataFrame(concatenated.iloc[order].reset_index(drop=True), crs=_EPSG_OUTPUT)

    out_path = _OUT_DIR / f"iberia_burn_history_{rid}.geojson"
    _write_geojson(merged, out_path)
    size_mb = out_path.stat().st_size / 1_000_000

    for round_idx in range(1, 4):
        if size_mb <= _MAX_MERGED_MB:
            break
        harder = _SIMPLIFY_TOL_DEG * (2**round_idx)
        log.warning(
            "Merged GeoJSON is %.1f MB (> %.0f MB) — re-simplifying round %d at tol=%.5f deg",
            size_mb,
            _MAX_MERGED_MB,
            round_idx,
            harder,
        )
        resimplified = merged.geometry.simplify(harder, preserve_topology=True)
        merged = gpd.GeoDataFrame(merged.assign(geometry=resimplified.to_numpy()), crs=_EPSG_OUTPUT)
        merged = gpd.GeoDataFrame(
            merged.loc[~merged.geometry.is_empty & merged.geometry.notna()].reset_index(drop=True),
            crs=_EPSG_OUTPUT,
        )
        _write_geojson(merged, out_path)
        size_mb = out_path.stat().st_size / 1_000_000

    log.info(
        "MERGED: %d perimeters (%d ICNF + %d EFFIS), %.2f MB -> %s",
        len(merged),
        int((merged["source"] == "ICNF").sum()),
        int((merged["source"] == "EFFIS").sum()),
        size_mb,
        out_path.name,
    )
    return out_path


# ── R2 upload + verify ─────────────────────────────────────────────────────────


def upload_to_r2(path: Path) -> str:
    """Copy the combined GeoJSON to Cloudflare R2 and verify CORS + byte-range.

    Returns the public href. Raises on a missing rclone or a failed verify.
    """
    if shutil.which("rclone") is None:
        raise RuntimeError(
            "rclone not found on PATH — cannot upload to R2. Install rclone and configure "
            "the 'r2:' remote, or pass --no-upload."
        )
    dst = f"{_R2_BUCKET}/{path.name}"
    log.info("rclone copyto %s -> %s", path.name, dst)
    result = subprocess.run(
        ["rclone", "copyto", str(path), dst],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"rclone upload failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    href = f"{_R2_PUBLIC_HOST}/{path.name}"
    _verify_r2(href)
    return href


def _verify_r2(href: str) -> None:
    """Confirm a cross-origin range GET returns HTTP 206 + access-control-allow-origin:*.

    The request MUST carry an ``Origin`` header: R2 echoes ``access-control-allow-origin``
    only on CORS requests, exactly like a browser ``fetch`` — a bare GET without
    ``Origin`` never sees the header, so testing without it gives a false negative.
    No ``Referer`` is sent (the burn-scar COG 403 lesson is about Referer-based hotlink
    protection on image .tif; a foreign Referer is what triggers it, so we omit it).
    A transient 501 then a 206 on retry is normal for R2 custom domains still
    initialising, so we retry a few times.
    """
    origin = "https://lunasilvestre.github.io"
    for attempt in range(1, 6):
        resp = requests.get(
            href,
            headers={
                "Range": "bytes=0-1023",
                "Origin": origin,
                "User-Agent": _USER_AGENT,
            },
            timeout=60,
        )
        acao = resp.headers.get("access-control-allow-origin")
        log.info("verify %s (attempt %d): HTTP %d, ACAO=%r", href, attempt, resp.status_code, acao)
        if resp.status_code == 206 and acao == "*":
            log.info("R2 verify OK: 206 + access-control-allow-origin:* (cross-origin GET)")
            return
        if attempt < 5:
            time.sleep(3)
    raise RuntimeError(
        f"R2 verify failed for {href}: expected HTTP 206 + access-control-allow-origin:* "
        "for a cross-origin range GET after retries. Check the object uploaded and the "
        "bucket's CORS policy."
    )


# ── helpers ────────────────────────────────────────────────────────────────────


def _get_with_retry(url: str, *, params: dict[str, str]) -> str:
    """GET with exponential back-off; returns response text. Raises on final failure."""
    last_exc: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _RETRIES:
                wait = 2**attempt
                log.warning(
                    "GET %s error (%d/%d): %s — retry in %ds",
                    url,
                    attempt + 1,
                    _RETRIES + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
    raise RuntimeError(f"GET {url} failed after {_RETRIES + 1} attempts: {last_exc}") from last_exc


def _write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    """Write a GeoDataFrame to GeoJSON (EPSG:4326) at fixed coordinate precision.

    CRS is asserted explicitly (non-negotiable #2). Coordinate precision is capped at
    ``_COORD_PRECISION_DP`` decimal places to trim float-string bloat without moving
    the simplified geometry.
    """
    if gdf.crs is None:
        raise ValueError(f"refusing to write {path.name}: CRS is None (non-negotiable #2)")
    out = gdf
    # Douglas-Peucker can introduce a handful of self-intersections; repair them so
    # every written layer is OGC-valid (buffer(0) is a no-op on valid geometry and a
    # cheap repair on the rare invalid one).
    invalid = ~out.geometry.is_valid
    if bool(invalid.any()):
        out = out.copy()
        out.loc[invalid, out.geometry.name] = out.loc[invalid].geometry.buffer(0)
        log.info("repaired %d invalid geometries in %s", int(invalid.sum()), path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(path, driver="GeoJSON", COORDINATE_PRECISION=_COORD_PRECISION_DP)


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Iberia burn-history layer.")
    parser.add_argument("--no-upload", action="store_true", help="Skip the R2 upload + verify.")
    parser.add_argument(
        "--no-effis",
        action="store_true",
        help="Ship ICNF-PT only (use if EFFIS WFS is unreachable; flag the Spain gap).",
    )
    parser.add_argument("--run-id", default=None, help="Override the run-id (default: UTC now).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    rid = args.run_id or _run_id()
    commit_sha = code_commit_sha(cwd=_ROOT)
    log.info("run_id=%s commit=%s", rid, commit_sha)
    log.info("ICNF license: %s | %s", _ICNF_LICENSE, _ICNF_ATTRIBUTION)
    log.info("EFFIS license: %s | %s", _EFFIS_LICENSE, _EFFIS_ATTRIBUTION)

    icnf_path, icnf = build_icnf_pt(rid, commit_sha=commit_sha)

    effis: gpd.GeoDataFrame | None = None
    if args.no_effis:
        log.warning("EFFIS skipped (--no-effis) — shipping ICNF-PT only; SPAIN GAP flagged.")
    else:
        _, effis = build_effis_es(rid)

    merged_path = merge_and_write(rid, icnf, effis)

    print(f"ICNF-PT  GeoJSON: {icnf_path}", file=sys.stderr)
    if effis is not None:
        print(f"EFFIS-ES GeoJSON: {_OUT_DIR / f'effis_burns_es_{rid}.geojson'}", file=sys.stderr)
    print(f"MERGED   GeoJSON: {merged_path}", file=sys.stderr)

    if args.no_upload:
        log.info("Upload skipped (--no-upload). Merged file at %s", merged_path)
        return 0

    href = upload_to_r2(merged_path)
    print(f"R2 href: {href}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
