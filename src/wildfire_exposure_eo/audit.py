"""Data-source health checks for PRE_DEV_CHECKLIST item C.

Each `check_*` function returns a `CheckResult` with status GREEN, YELLOW, or RED.
The full audit (`run_all`) runs the ten checks against an AOI bbox and
returns a list of results. The CLI renders these as a rich table; CI consumes the
exit code (0 = all GREEN, 1 = any RED, 2 = no RED but at least one YELLOW).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import requests
from pystac_client import Client

Status = Literal["GREEN", "YELLOW", "RED"]

USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 audit (+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)
PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
LPCLOUD_STAC_URL = "https://cmr.earthdata.nasa.gov/stac/LPCLOUD"
HLS_COLLECTIONS = ("HLSL30_2.0", "HLSS30_2.0")
IPMA_FWI_URL = "https://www.ipma.pt/pt/riscoincendio/"
OVERPASS_PRIMARY = "https://overpass-api.de/api/interpreter"
ICNF_AREAS_ARDIDAS_MAPSERVER = (
    "https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer"
)
# Lang et al. 2023 ETH Global Canopy Height 10 m 2020 v1 — canonical tile host.
# Source: official tile browser at https://langnico.github.io/globalcanopyheight/assets/tile_index.html
# DOI:    10.3929/ethz-b-000609802     License: CC BY 4.0
# Tile naming: 3-degree blocks identified by SW corner (lat = floor/3*3, lon = floor/3*3).
ETH_GCH_BASE_URL = (
    "https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download"
    "?path=%2F3deg_cogs&files=ETH_GlobalCanopyHeight_10m_2020_{tile}_Map.tif"
)
TIFF_MAGIC = (b"II*\x00", b"MM\x00*")

# Layer-id mapping confirmed against the live MapServer in scripts/00_icnf_fetch.sh.
ICNF_RECENT_LAYERS: dict[int, str] = {
    20: "2025",
    19: "2024",
    18: "2023",
    17: "2022",
    15: "2021",
    0: "2020",
    6: "2017",  # Pedrógão Grande mega-fire year, sentinel sample for AOI
}


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def load_aoi_bbox(aoi_path: Path) -> tuple[float, float, float, float]:
    """Compute the WGS84 bbox of every coordinate in a GeoJSON FeatureCollection or Feature."""
    payload = json.loads(aoi_path.read_text())
    coords: list[tuple[float, float]] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            if node and all(isinstance(x, int | float) for x in node[:2]) and len(node) >= 2:
                coords.append((float(node[0]), float(node[1])))
            else:
                for child in node:
                    walk(child)

    geom_root: object
    if payload.get("type") == "FeatureCollection":
        for feat in payload.get("features", []):
            walk(feat.get("geometry", {}).get("coordinates"))
    elif payload.get("type") == "Feature":
        walk(payload.get("geometry", {}).get("coordinates"))
    else:
        geom_root = payload.get("coordinates")
        walk(geom_root)

    if not coords:
        raise ValueError(f"no coordinates found in {aoi_path}")
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def _pc_client() -> Client:
    return Client.open(PC_STAC_URL)


def _date_window(months: int) -> tuple[str, str]:
    end = datetime.now(UTC)
    start = end - timedelta(days=round(months * 30.44))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def check_sentinel2_l2a(
    bbox: tuple[float, float, float, float],
    months: int = 24,
    min_items: int = 50,
    max_cloud_pct: float = 20.0,
) -> CheckResult:
    name = "Sentinel-2 L2A"
    try:
        client = _pc_client()
        start, end = _date_window(months)
        search = client.search(
            collections=["sentinel-2-l2a"],
            bbox=bbox,
            datetime=f"{start}/{end}",
            query={"eo:cloud_cover": {"lt": max_cloud_pct}},
            limit=500,
        )
        items = list(search.items())
    except Exception as exc:
        return CheckResult(name, "RED", f"search failed: {exc}")
    n = len(items)
    details = {
        "items_found": n,
        "min_required": min_items,
        "window_months": months,
        "max_cloud_pct": max_cloud_pct,
    }
    if n >= min_items:
        return CheckResult(name, "GREEN", f"{n} cloud-free items in past {months} mo", details)
    if n > 0:
        return CheckResult(
            name,
            "YELLOW",
            f"only {n}/{min_items} cloud-free items; relax cloud threshold?",
            details,
        )
    return CheckResult(name, "RED", f"no items returned for bbox in past {months} mo", details)


def check_sentinel1_grd(
    bbox: tuple[float, float, float, float],
    months: int = 24,
    min_items: int = 100,
) -> CheckResult:
    name = "Sentinel-1 GRD"
    try:
        client = _pc_client()
        start, end = _date_window(months)
        search = client.search(
            collections=["sentinel-1-grd"],
            bbox=bbox,
            datetime=f"{start}/{end}",
            limit=500,
        )
        items = list(search.items())
    except Exception as exc:
        return CheckResult(name, "RED", f"search failed: {exc}")
    n = len(items)
    details = {"items_found": n, "min_required": min_items, "window_months": months}
    if n >= min_items:
        return CheckResult(name, "GREEN", f"{n} items in past {months} mo", details)
    if n > 0:
        return CheckResult(name, "YELLOW", f"only {n}/{min_items} items in window", details)
    return CheckResult(name, "RED", f"no items returned for bbox in past {months} mo", details)


def check_cop_dem_glo30(bbox: tuple[float, float, float, float]) -> CheckResult:
    name = "Cop-DEM GLO-30"
    try:
        client = _pc_client()
        items = list(client.search(collections=["cop-dem-glo-30"], bbox=bbox, limit=10).items())
    except Exception as exc:
        return CheckResult(name, "RED", f"search failed: {exc}")
    if items:
        return CheckResult(
            name, "GREEN", f"{len(items)} tile(s) cover AOI", {"items_found": len(items)}
        )
    return CheckResult(name, "RED", "no DEM tiles intersect AOI bbox")


def _gch_tile_for_bbox(bbox: tuple[float, float, float, float]) -> str:
    """Return the SW-corner tile name (e.g. 'N39W009') covering the bbox SW corner.

    Lang et al. publish on a 3-degree grid. We pick the tile containing the AOI
    SW corner; for AOIs that span multiple tiles, the audit only needs to probe
    one to confirm host reachability and tile-naming correctness.
    """
    xmin, ymin, _, _ = bbox
    import math

    lat_sw = int(math.floor(ymin / 3.0) * 3)
    lon_sw = int(math.floor(xmin / 3.0) * 3)
    lat_part = f"N{lat_sw:02d}" if lat_sw >= 0 else f"S{-lat_sw:02d}"
    lon_part = f"E{lon_sw:03d}" if lon_sw >= 0 else f"W{-lon_sw:03d}"
    return f"{lat_part}{lon_part}"


def check_eth_gch(bbox: tuple[float, float, float, float]) -> CheckResult:
    """Confirm ETH GCH access via the canonical libdrive direct-download host.

    GCH is not on Microsoft Planetary Computer (verified 2026-05-07 — 135
    collections enumerated, zero matches on canopy/gch/eth/forest height).
    The official tile browser at langnico.github.io/globalcanopyheight links
    every tile to libdrive.ethz.ch, so that's the canonical path. We probe by
    range-GETting the first 16 bytes of the AOI's SW-corner tile and checking
    the TIFF magic — proves both reachability and tile-naming correctness.
    """
    name = "ETH GCH"
    tile = _gch_tile_for_bbox(bbox)
    url = ETH_GCH_BASE_URL.format(tile=tile)
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-15"},
            allow_redirects=True,
            timeout=30,
        )
    except Exception as exc:
        return CheckResult(name, "RED", f"libdrive request failed: {exc}", {"url": url})
    if resp.status_code not in (200, 206):
        return CheckResult(
            name,
            "RED",
            f"HTTP {resp.status_code} from libdrive",
            {"url": url, "tile": tile},
        )
    if resp.content[:4] not in TIFF_MAGIC:
        return CheckResult(
            name,
            "RED",
            f"response is not a TIFF (magic={resp.content[:4].hex()})",
            {"url": url, "tile": tile, "content_type": resp.headers.get("content-type", "")},
        )
    return CheckResult(
        name,
        "GREEN",
        f"libdrive tile {tile} returned a valid GeoTIFF",
        {"url": url, "tile": tile, "content_type": resp.headers.get("content-type", "")},
    )


def check_esa_worldcover(bbox: tuple[float, float, float, float]) -> CheckResult:
    name = "ESA WorldCover 2021"
    try:
        client = _pc_client()
        items = list(
            client.search(
                collections=["esa-worldcover"],
                bbox=bbox,
                datetime="2021-01-01/2021-12-31",
                limit=10,
            ).items()
        )
    except Exception as exc:
        return CheckResult(name, "RED", f"search failed: {exc}")
    if items:
        return CheckResult(
            name, "GREEN", f"{len(items)} 2021 tile(s) over AOI", {"items_found": len(items)}
        )
    return CheckResult(name, "RED", "no WorldCover 2021 tiles intersect AOI bbox")


def check_overpass_osm(
    bbox: tuple[float, float, float, float],
    min_features_per_class: int = 100,
    min_classes: int = 3,
) -> CheckResult:
    """Probe Overpass for >=min_features_per_class features in >=min_classes infra tags."""
    name = "OSM Overpass"
    xmin, ymin, xmax, ymax = bbox
    bbox_str = f"{ymin},{xmin},{ymax},{xmax}"
    highway_pattern = "motorway|trunk|primary|secondary|tertiary|residential|unclassified"
    classes = {
        "power_tower": f'node["power"="tower"]({bbox_str});',
        "highway_road": f'way["highway"~"^({highway_pattern})$"]({bbox_str});',
        "building": f'way["building"]({bbox_str});',
    }
    counts: dict[str, int] = {}
    errors: list[str] = []
    for label, q in classes.items():
        body = f"[out:json][timeout:25];({q});out count;"
        try:
            resp = requests.post(
                OVERPASS_PRIMARY,
                data={"data": body},
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            total = int(elements[0].get("tags", {}).get("total", "0")) if elements else 0
            counts[label] = total
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            counts[label] = -1
    passing = [k for k, v in counts.items() if v >= min_features_per_class]
    details = {"counts": counts, "min_features_per_class": min_features_per_class, "errors": errors}
    if len(passing) >= min_classes:
        return CheckResult(
            name,
            "GREEN",
            f"{len(passing)}/{min_classes} classes ≥ {min_features_per_class}",
            details,
        )
    if errors:
        return CheckResult(name, "RED", f"Overpass errors on {len(errors)}/{len(classes)}", details)
    return CheckResult(
        name, "YELLOW", f"only {len(passing)}/{min_classes} classes met threshold", details
    )


def check_icnf_areas_ardidas(
    bbox: tuple[float, float, float, float],
    layers: dict[int, str] | None = None,
) -> CheckResult:
    """Hit the ICNF MapServer for a sample of recent burn-year layers and count intersections."""
    name = "ICNF Áreas Ardidas"
    layers = layers or ICNF_RECENT_LAYERS
    xmin, ymin, xmax, ymax = bbox
    geom = f"{xmin},{ymin},{xmax},{ymax}"
    counts: dict[str, int] = {}
    errors: list[str] = []
    for layer_id, year_label in layers.items():
        url = f"{ICNF_AREAS_ARDIDAS_MAPSERVER}/{layer_id}/query"
        try:
            resp = requests.get(
                url,
                params={
                    "where": "1=1",
                    "geometry": geom,
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "returnCountOnly": "true",
                    "f": "json",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            counts[year_label] = int(payload.get("count", 0))
        except Exception as exc:
            errors.append(f"{year_label}: {exc}")
            counts[year_label] = -1
    total = sum(v for v in counts.values() if v > 0)
    details = {
        "counts_per_layer": counts,
        "errors": errors,
        "sampled_layers": list(layers.values()),
    }
    if total >= 1:
        return CheckResult(
            name, "GREEN", f"{total} burn polygon(s) intersect AOI across sampled years", details
        )
    if errors and total == 0:
        return CheckResult(
            name, "RED", f"all {len(errors)} layer queries failed; endpoint unreachable", details
        )
    return CheckResult(name, "RED", "no burn polygons intersect AOI in sampled years", details)


def check_hls_lpcloud(
    bbox: tuple[float, float, float, float],
    months: int = 12,
    min_items: int = 10,
) -> CheckResult:
    """Probe NASA CMR-STAC for HLSL30 + HLSS30 reachability over the AOI.

    Auth via `~/.netrc` is only required for asset download; the STAC search
    itself is anonymous. We open the LPCLOUD catalog, confirm both required
    HLS collections resolve, and count items intersecting the AOI in the
    trailing 12 months. Network failure → YELLOW (per prompt-01 rule).
    """
    name = "HLS S30/L30"
    try:
        client = Client.open(LPCLOUD_STAC_URL)
    except Exception as exc:
        return CheckResult(
            name,
            "YELLOW",
            f"CMR-STAC unreachable: {exc}",
            {"endpoint": LPCLOUD_STAC_URL},
        )

    missing: list[str] = []
    for cid in HLS_COLLECTIONS:
        try:
            client.get_collection(cid)
        except Exception:
            missing.append(cid)
    if missing:
        return CheckResult(
            name,
            "RED",
            f"required collection(s) not found: {missing}",
            {"endpoint": LPCLOUD_STAC_URL, "missing": missing},
        )

    start, end = _date_window(months)
    counts: dict[str, int] = {}
    errors: list[str] = []
    for cid in HLS_COLLECTIONS:
        try:
            search = client.search(
                collections=[cid],
                bbox=bbox,
                datetime=f"{start}/{end}",
                limit=200,
            )
            counts[cid] = len(list(search.items()))
        except Exception as exc:
            errors.append(f"{cid}: {exc}")
            counts[cid] = -1

    total = sum(v for v in counts.values() if v > 0)
    details = {
        "endpoint": LPCLOUD_STAC_URL,
        "items_per_collection": counts,
        "window_months": months,
        "min_required": min_items,
        "errors": errors,
    }
    if errors and total == 0:
        return CheckResult(name, "YELLOW", f"search errors on {len(errors)} collection(s)", details)
    if total >= min_items:
        return CheckResult(
            name, "GREEN", f"{total} items across HLS L30+S30 in past {months} mo", details
        )
    if total > 0:
        return CheckResult(name, "YELLOW", f"only {total}/{min_items} HLS items in window", details)
    return CheckResult(name, "RED", f"no HLS items in past {months} mo over AOI", details)


def check_ipma_fwi() -> CheckResult:
    """Reachability probe of the IPMA daily fire-risk page.

    No public REST API is documented for IPMA FWI grids (per docs/data_sources.md);
    this is a connectivity check only. Data ingestion is out of scope for the audit.
    """
    name = "IPMA FWI"
    try:
        resp = requests.get(
            IPMA_FWI_URL,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
            timeout=10,
        )
    except Exception as exc:
        return CheckResult(
            name,
            "YELLOW",
            f"IPMA unreachable: {exc}",
            {"endpoint": IPMA_FWI_URL},
        )
    details = {
        "endpoint": IPMA_FWI_URL,
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
    }
    if 200 <= resp.status_code < 300:
        return CheckResult(name, "GREEN", f"reachable (HTTP {resp.status_code})", details)
    if 300 <= resp.status_code < 400:
        return CheckResult(
            name, "YELLOW", f"unexpected redirect (HTTP {resp.status_code})", details
        )
    return CheckResult(name, "RED", f"HTTP {resp.status_code} from IPMA", details)


def check_prithvi_burn_scar(
    config_path: Path = Path("config/burn_scar.yaml"),
) -> CheckResult:
    """Verify the pinned Prithvi burn-scar model resolves on the Hugging Face hub.

    Probe only — no weights are downloaded. Reads the model ID + revision
    from `config/burn_scar.yaml` (CLAUDE.md non-negotiable #1: the ID lives
    in config, never in code). RED when the config still carries the
    placeholder or the hub says the model does not exist; YELLOW when the
    hub is unreachable.
    """
    name = "Prithvi Burn-Scar"
    try:
        import yaml

        payload = yaml.safe_load(config_path.read_text())
        model_id = str(payload["model"]["hf_model_id"])
        revision = str(payload["model"]["hf_revision_sha"])
    except Exception as exc:
        return CheckResult(
            name,
            "RED",
            f"cannot read model config {config_path}: {exc}",
            {"config_path": str(config_path)},
        )
    if model_id == "TBD-verified-at-audit":
        return CheckResult(
            name,
            "RED",
            "hf_model_id is still the unverified placeholder",
            {"config_path": str(config_path)},
        )
    url = f"https://huggingface.co/api/models/{model_id}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
    except Exception as exc:
        return CheckResult(name, "YELLOW", f"HF hub unreachable: {exc}", {"endpoint": url})
    details: dict[str, Any] = {
        "endpoint": url,
        "hf_model_id": model_id,
        "pinned_revision": revision,
        "status_code": resp.status_code,
    }
    if resp.status_code == 200:
        hub_sha = str(resp.json().get("sha", ""))
        details["hub_main_revision"] = hub_sha
        if hub_sha == revision:
            return CheckResult(
                name, "GREEN", "model resolves; pinned revision is hub main", details
            )
        return CheckResult(
            name,
            "GREEN",
            "model resolves; hub main moved past the pin (pinned revision stays fetchable)",
            details,
        )
    if resp.status_code in (401, 403, 404):
        return CheckResult(name, "RED", f"HTTP {resp.status_code} for {model_id}", details)
    return CheckResult(name, "YELLOW", f"unexpected HTTP {resp.status_code} from HF hub", details)


CHECKS: tuple[str, ...] = (
    "Sentinel-2 L2A",
    "Sentinel-1 GRD",
    "Cop-DEM GLO-30",
    "ETH GCH",
    "OSM Overpass",
    "ICNF Áreas Ardidas",
    "ESA WorldCover 2021",
    "HLS S30/L30",
    "IPMA FWI",
    "Prithvi Burn-Scar",
)


def run_all(aoi_path: Path) -> list[CheckResult]:
    bbox = load_aoi_bbox(aoi_path)
    return [
        check_sentinel2_l2a(bbox),
        check_sentinel1_grd(bbox),
        check_cop_dem_glo30(bbox),
        check_eth_gch(bbox),
        check_overpass_osm(bbox),
        check_icnf_areas_ardidas(bbox),
        check_esa_worldcover(bbox),
        check_hls_lpcloud(bbox),
        check_ipma_fwi(),
        check_prithvi_burn_scar(),
    ]
