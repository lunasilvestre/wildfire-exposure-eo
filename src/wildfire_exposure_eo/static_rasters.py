"""Static raster fetch — ETH GCH, EFFIS fuel map, DGT COSc, DGT COS.

All URLs are sourced from docs/data_sources.md and scripts/00_*_fetch.sh.
No URLs are invented; all were verified on 2026-05-07 per CLAUDE.md non-negotiable #1.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

from wildfire_exposure_eo.schemas.static_raster_manifest import (
    FetchRecord,
    StaticRasterManifest,
)

log = logging.getLogger(__name__)

_USER_AGENT = "wildfire-exposure-eo/0.0.1 (+https://github.com/lunasilvestre/wildfire-exposure-eo)"

# ── ETH GCH constants ──────────────────────────────────────────────────────────

_ETH_GCH_BASE_URL = (
    "https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download"
    "?path=%2F3deg_cogs&files={filename}"
)
_ETH_GCH_FILENAME = "ETH_GlobalCanopyHeight_10m_2020_{tile_id}_Map.tif"
_ETH_GCH_LICENSE = "CC-BY 4.0"
_ETH_GCH_ATTRIBUTION = (
    "Lang, N., Jetz, W., Schindler, K., Wegner, J.D. (2023). "
    "A high-resolution canopy height model of the Earth. Nature Ecology & Evolution. "
    "DOI: 10.3929/ethz-b-000609802"
)

# TIFF little-endian magic (bytes 0-3)
_TIFF_LE_MAGIC = b"\x49\x49\x2a\x00"
# TIFF big-endian magic (bytes 0-3) — also valid TIFF
_TIFF_BE_MAGIC = b"\x4d\x4d\x00\x2a"

# ── EFFIS constants ────────────────────────────────────────────────────────────

# Entry URL (cert valid); redirects to data.effis (cert EXPIRED as of 2026-05-07).
# verify=False is required for the redirect target — documented in scripts/00_effis_fetch.sh.
_EFFIS_URL = (
    "https://forest-fire.emergency.copernicus.eu"
    "/effis/applications/data-and-services/FuelMap_LAEA.zip"
)
_EFFIS_TIFF_INSIDE_ZIP = "FuelMap_LAEA.tif"
_EFFIS_LICENSE = "Free, no auth (Copernicus open data)"
_EFFIS_ATTRIBUTION = "EFFIS / JRC, European Commission"

# ── DGT COSc constants ─────────────────────────────────────────────────────────

_DGT_COSC_URL_TEMPLATE = "https://geo2.dgterritorio.gov.pt/cosc/COSc{vintage_key}.zip"
_DGT_COSC_VINTAGE_KEYS: dict[str, str] = {
    "2023": "2023",
    "2024_pre_verao": "2024_preverao",
}
_DGT_COSC_TIFF_PREFIX = "COSc"
_DGT_COSC_LICENSE = "CC-BY 4.0"
_DGT_COSC_ATTRIBUTION = "Direção-Geral do Território (DGT) — Carta de Ocupação do Solo Conjuntural"

# ── DGT COS constants ──────────────────────────────────────────────────────────

_DGT_COS_URL_TEMPLATE = (
    "https://geo2.dgterritorio.gov.pt/cos/S2/COS{vintage_key}/COS{vintage_key}-S2-gpkg.zip"
)
_DGT_COS_VINTAGE_KEYS: dict[str, str] = {
    "2018_v3": "2018v3",
    "2023_v1": "2023v1",
}
_DGT_COS_GPKG_PREFIX = "COS"
_DGT_COS_LICENSE = "CC-BY 4.0"
_DGT_COS_ATTRIBUTION = (
    "Direção-Geral do Território (DGT) — Carta de Uso e Ocupação do Solo (Série 2)"
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_sidecar_sha(path: Path) -> str | None:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.exists():
        return sidecar.read_text().strip()
    return None


def _write_sidecar_sha(path: Path, sha: str) -> None:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar.write_text(sha)


def _range_get_magic(url: str, *, verify: bool = True) -> bytes:
    """Return the first 16 bytes of a remote resource via Range-GET."""
    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT, "Range": "bytes=0-15"},
        timeout=30,
        verify=verify,
    )
    resp.raise_for_status()
    return resp.content[:16]


def _download_with_retries(
    url: str,
    dest: Path,
    *,
    verify: bool = True,
    max_retries: int = 2,
    backoff_base: float = 2.0,
) -> int:
    """Download url to dest, retrying on transient errors. Returns bytes written."""
    attempt = 0
    while True:
        try:
            with requests.get(
                url,
                stream=True,
                headers={"User-Agent": _USER_AGENT},
                timeout=120,
                verify=verify,
            ) as resp:
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                n = 0
                with dest.open("wb") as f:
                    for chunk in resp.iter_content(65536):
                        f.write(chunk)
                        n += len(chunk)
            return n
        except requests.HTTPError as exc:
            # 404 and 4xx client errors are permanent — do not retry.
            if exc.response is not None and 400 <= exc.response.status_code < 500:
                raise
            if attempt >= max_retries:
                raise
            wait = backoff_base**attempt
            log.warning(
                "transient HTTP error %s — retry %d/%d in %.0fs",
                exc,
                attempt + 1,
                max_retries,
                wait,
            )
            time.sleep(wait)
            attempt += 1
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise
            wait = backoff_base**attempt
            log.warning(
                "request error %s — retry %d/%d in %.0fs",
                exc,
                attempt + 1,
                max_retries,
                wait,
            )
            time.sleep(wait)
            attempt += 1


def _extract_from_zip(zip_path: Path, prefix: str, dest_dir: Path) -> Path:
    """Extract the first file whose name starts with prefix from zip_path into dest_dir."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        candidates = [n for n in zf.namelist() if Path(n).name.startswith(prefix)]
        if not candidates:
            raise ValueError(
                f"No file with prefix {prefix!r} found in {zip_path}. Contents: {zf.namelist()}"
            )
        # Pick the first non-directory match. Raster/GIS data files (.tif/.tiff/.gpkg)
        # are prioritised over sidecar files (.lyr/.qml/.sld) so that zips containing
        # both style files and the actual data file resolve correctly. Within the same
        # priority tier, the shortest path wins.
        _raster_exts = {".tif", ".tiff", ".gpkg"}

        def _zip_sort_key(name: str) -> tuple[int, int]:
            ext = Path(name).suffix.lower()
            return (0 if ext in _raster_exts else 1, len(name))

        candidates = [c for c in candidates if not c.endswith("/")]
        candidates.sort(key=_zip_sort_key)
        target_name = candidates[0]
        out_path = dest_dir / Path(target_name).name
        with zf.open(target_name) as src, out_path.open("wb") as dst:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
        log.info("extracted %s → %s", target_name, out_path)
        return out_path


# ── public functions ───────────────────────────────────────────────────────────


def compute_eth_gch_tile_ids(aoi: BaseGeometry) -> list[str]:
    """Compute the 3-degree ETH GCH tile IDs that cover the AOI bounding box.

    Tile names follow the SW-corner convention: N<lat_2d><E|W><lon_3d>.
    Each tile is a 3° × 3° block. The SW corner latitude and longitude are
    multiples of 3 obtained by flooring the coordinate to the nearest multiple.

    Example: lat=40.73, lon=-8.37 → tile N39W009 (SW corner at lat=39, lon=-9).
    """
    min_lon, min_lat, max_lon, max_lat = aoi.bounds

    # Floor to the nearest multiple of 3 (correct for negative numbers too,
    # since math.floor rounds toward negative infinity).
    lat_start = int(math.floor(min_lat / 3) * 3)
    lat_end = int(math.floor(max_lat / 3) * 3)
    lon_start = int(math.floor(min_lon / 3) * 3)
    lon_end = int(math.floor(max_lon / 3) * 3)

    tiles: list[str] = []
    for lat in range(lat_start, lat_end + 1, 3):
        ns = "N" if lat >= 0 else "S"
        lat_abs = abs(lat)
        for lon in range(lon_start, lon_end + 1, 3):
            ew = "E" if lon >= 0 else "W"
            lon_abs = abs(lon)
            tile_id = f"{ns}{lat_abs:02d}{ew}{lon_abs:03d}"
            tiles.append(tile_id)

    return sorted(set(tiles))


def fetch_eth_gch_tile(
    tile_id: str,
    *,
    cache_dir: Path,
    force: bool = False,
) -> FetchRecord:
    """Fetch one ETH GCH 3-degree COG tile.

    Verifies TIFF magic via Range-GET before committing to the full download.
    Idempotent: skips if the local file exists and its SHA-256 matches the sidecar.
    """
    filename = _ETH_GCH_FILENAME.format(tile_id=tile_id)
    url = _ETH_GCH_BASE_URL.format(filename=filename)
    dest_dir = cache_dir / "eth-gch-2020"
    dest = dest_dir / filename

    fetched_at = datetime.now(UTC)

    # Check idempotency
    if not force and dest.exists():
        expected_sha = _read_sidecar_sha(dest)
        if expected_sha is not None:
            actual_sha = _sha256_file(dest)
            if actual_sha == expected_sha:
                log.info("cache hit: %s (sha256 match)", dest)
                return FetchRecord(
                    source_id="eth-gch-2020",
                    vintage="2020",
                    tile_id=tile_id,
                    source_url=url,
                    local_path=str(dest),
                    bytes_downloaded=dest.stat().st_size,
                    sha256=actual_sha,
                    fetched_at_utc=fetched_at,
                    cache_hit=True,
                    license=_ETH_GCH_LICENSE,
                    attribution=_ETH_GCH_ATTRIBUTION,
                )

    # Pre-download magic-byte verification via Range-GET (cheap check before GB download)
    log.info("verifying TIFF magic for tile %s ...", tile_id)
    magic = _range_get_magic(url)
    if magic[:4] not in (_TIFF_LE_MAGIC, _TIFF_BE_MAGIC):
        raise ValueError(
            f"ETH GCH tile {tile_id}: unexpected magic bytes {magic[:4].hex()} "
            f"at {url} — expected TIFF LE {_TIFF_LE_MAGIC.hex()} "
            f"or BE {_TIFF_BE_MAGIC.hex()}"
        )
    log.info("TIFF magic OK for tile %s — downloading ...", tile_id)

    dest_dir.mkdir(parents=True, exist_ok=True)
    n_bytes = _download_with_retries(url, dest)
    sha = _sha256_file(dest)
    _write_sidecar_sha(dest, sha)
    log.info("ETH GCH %s: %d bytes → %s", tile_id, n_bytes, dest)

    return FetchRecord(
        source_id="eth-gch-2020",
        vintage="2020",
        tile_id=tile_id,
        source_url=url,
        local_path=str(dest),
        bytes_downloaded=n_bytes,
        sha256=sha,
        fetched_at_utc=fetched_at,
        cache_hit=False,
        license=_ETH_GCH_LICENSE,
        attribution=_ETH_GCH_ATTRIBUTION,
    )


def fetch_effis_fuel_map(
    *,
    cache_dir: Path,
    force: bool = False,
) -> FetchRecord:
    """Fetch the EFFIS pan-European fuel map GeoTIFF.

    The EFFIS entry URL redirects to data.effis.emergency.copernicus.eu, which
    served an expired SSL certificate as of 2026-05-07 (documented in
    scripts/00_effis_fetch.sh). verify=False is applied only to this fetch.
    """
    dest_dir = cache_dir / "effis"
    # Extract the TIF from the ZIP; store the TIF as the canonical cached artifact.
    dest_tif = dest_dir / "effis_european_fuel_map.tif"
    zip_path = dest_dir / "FuelMap_LAEA.zip"

    fetched_at = datetime.now(UTC)

    # Check idempotency on the extracted TIF
    if not force and dest_tif.exists():
        expected_sha = _read_sidecar_sha(dest_tif)
        if expected_sha is not None:
            actual_sha = _sha256_file(dest_tif)
            if actual_sha == expected_sha:
                log.info("cache hit: %s (sha256 match)", dest_tif)
                return FetchRecord(
                    source_id="effis-fuel-map",
                    vintage="2023",
                    tile_id=None,
                    source_url=_EFFIS_URL,
                    local_path=str(dest_tif),
                    bytes_downloaded=dest_tif.stat().st_size,
                    sha256=actual_sha,
                    fetched_at_utc=fetched_at,
                    cache_hit=True,
                    license=_EFFIS_LICENSE,
                    attribution=_EFFIS_ATTRIBUTION,
                )

    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading EFFIS fuel map (SSL verify=False — known expired cert on redirect) ...")
    # verify=False is intentional; see EFFIS URL note above.
    n_bytes = _download_with_retries(_EFFIS_URL, zip_path, verify=False)

    # Extract the GeoTIFF from the ZIP
    extracted = _extract_from_zip(zip_path, _EFFIS_TIFF_INSIDE_ZIP, dest_dir)
    # Rename to canonical name if needed
    if extracted != dest_tif:
        extracted.rename(dest_tif)

    sha = _sha256_file(dest_tif)
    _write_sidecar_sha(dest_tif, sha)
    log.info("EFFIS fuel map: %d bytes (zip) → %s", n_bytes, dest_tif)

    return FetchRecord(
        source_id="effis-fuel-map",
        vintage="2023",
        tile_id=None,
        source_url=_EFFIS_URL,
        local_path=str(dest_tif),
        bytes_downloaded=n_bytes,
        sha256=sha,
        fetched_at_utc=fetched_at,
        cache_hit=False,
        license=_EFFIS_LICENSE,
        attribution=_EFFIS_ATTRIBUTION,
    )


def fetch_dgt_cosc(
    vintage: str = "2024_pre_verao",
    *,
    cache_dir: Path,
    force: bool = False,
) -> FetchRecord:
    """Fetch DGT COSc land-cover raster.

    vintage: "2023" | "2024_pre_verao" (default: "2024_pre_verao")
    URLs verified in scripts/00_dgt_fetch.sh (2026-05-07).
    """
    vintage_key = _DGT_COSC_VINTAGE_KEYS.get(vintage)
    if vintage_key is None:
        raise ValueError(
            f"Unknown COSc vintage {vintage!r}. Known: {sorted(_DGT_COSC_VINTAGE_KEYS)}"
        )
    url = _DGT_COSC_URL_TEMPLATE.format(vintage_key=vintage_key)
    dest_dir = cache_dir / "dgt-cosc"
    zip_path = dest_dir / f"COSc{vintage_key}.zip"
    dest_tif = dest_dir / f"cosc_{vintage}.tif"

    fetched_at = datetime.now(UTC)

    if not force and dest_tif.exists():
        expected_sha = _read_sidecar_sha(dest_tif)
        if expected_sha is not None:
            actual_sha = _sha256_file(dest_tif)
            if actual_sha == expected_sha:
                log.info("cache hit: %s (sha256 match)", dest_tif)
                return FetchRecord(
                    source_id="dgt-cosc",
                    vintage=vintage,
                    tile_id=None,
                    source_url=url,
                    local_path=str(dest_tif),
                    bytes_downloaded=dest_tif.stat().st_size,
                    sha256=actual_sha,
                    fetched_at_utc=fetched_at,
                    cache_hit=True,
                    license=_DGT_COSC_LICENSE,
                    attribution=_DGT_COSC_ATTRIBUTION,
                )

    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading DGT COSc %s ...", vintage)
    n_bytes = _download_with_retries(url, zip_path)

    extracted = _extract_from_zip(zip_path, _DGT_COSC_TIFF_PREFIX, dest_dir)
    if extracted != dest_tif:
        extracted.rename(dest_tif)

    sha = _sha256_file(dest_tif)
    _write_sidecar_sha(dest_tif, sha)
    log.info("DGT COSc %s: %d bytes (zip) → %s", vintage, n_bytes, dest_tif)

    return FetchRecord(
        source_id="dgt-cosc",
        vintage=vintage,
        tile_id=None,
        source_url=url,
        local_path=str(dest_tif),
        bytes_downloaded=n_bytes,
        sha256=sha,
        fetched_at_utc=fetched_at,
        cache_hit=False,
        license=_DGT_COSC_LICENSE,
        attribution=_DGT_COSC_ATTRIBUTION,
    )


def fetch_dgt_cos(
    vintage: str = "2023_v1",
    *,
    cache_dir: Path,
    force: bool = False,
) -> FetchRecord:
    """Fetch DGT COS land-use GeoPackage.

    vintage: "2018_v3" | "2023_v1" (default: "2023_v1")
    URLs verified in scripts/00_dgt_fetch.sh (2026-05-07).
    The GPKG is an INPUT to the pipeline (data/cache/), not a pipeline output.
    CLAUDE.md non-negotiable #5 applies to outputs only.
    """
    vintage_key = _DGT_COS_VINTAGE_KEYS.get(vintage)
    if vintage_key is None:
        raise ValueError(f"Unknown COS vintage {vintage!r}. Known: {sorted(_DGT_COS_VINTAGE_KEYS)}")
    url = _DGT_COS_URL_TEMPLATE.format(vintage_key=vintage_key)
    dest_dir = cache_dir / "dgt-cos"
    zip_path = dest_dir / f"COS{vintage_key}-S2-gpkg.zip"
    dest_gpkg = dest_dir / f"cos_{vintage}.gpkg"

    fetched_at = datetime.now(UTC)

    if not force and dest_gpkg.exists():
        expected_sha = _read_sidecar_sha(dest_gpkg)
        if expected_sha is not None:
            actual_sha = _sha256_file(dest_gpkg)
            if actual_sha == expected_sha:
                log.info("cache hit: %s (sha256 match)", dest_gpkg)
                return FetchRecord(
                    source_id="dgt-cos",
                    vintage=vintage,
                    tile_id=None,
                    source_url=url,
                    local_path=str(dest_gpkg),
                    bytes_downloaded=dest_gpkg.stat().st_size,
                    sha256=actual_sha,
                    fetched_at_utc=fetched_at,
                    cache_hit=True,
                    license=_DGT_COS_LICENSE,
                    attribution=_DGT_COS_ATTRIBUTION,
                )

    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading DGT COS %s ...", vintage)
    n_bytes = _download_with_retries(url, zip_path)

    extracted = _extract_from_zip(zip_path, _DGT_COS_GPKG_PREFIX, dest_dir)
    if extracted != dest_gpkg:
        extracted.rename(dest_gpkg)

    sha = _sha256_file(dest_gpkg)
    _write_sidecar_sha(dest_gpkg, sha)
    log.info("DGT COS %s: %d bytes (zip) → %s", vintage, n_bytes, dest_gpkg)

    return FetchRecord(
        source_id="dgt-cos",
        vintage=vintage,
        tile_id=None,
        source_url=url,
        local_path=str(dest_gpkg),
        bytes_downloaded=n_bytes,
        sha256=sha,
        fetched_at_utc=fetched_at,
        cache_hit=False,
        license=_DGT_COS_LICENSE,
        attribution=_DGT_COS_ATTRIBUTION,
    )


def build_fetch_manifest(
    records: list[FetchRecord],
    *,
    aoi_path: str,
    run_id: str,
    code_commit_sha: str,
    aoi_geometry_sha: str,
    resolved_at_utc: datetime,
) -> StaticRasterManifest:
    """Compose per-source FetchRecords into a single StaticRasterManifest."""
    totals_by_source: dict[str, int] = {}
    for rec in records:
        totals_by_source[rec.source_id] = (
            totals_by_source.get(rec.source_id, 0) + rec.bytes_downloaded
        )
    return StaticRasterManifest(
        run_id=run_id,
        code_commit_sha=code_commit_sha,
        aoi_path=aoi_path,
        aoi_geometry_sha=aoi_geometry_sha,
        resolved_at_utc=resolved_at_utc,
        records=records,
        totals_bytes=sum(rec.bytes_downloaded for rec in records),
        totals_by_source=totals_by_source,
    )


def write_manifest(manifest: StaticRasterManifest, path: Path) -> Path:
    """Write the manifest as JSON with stable key ordering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return path
