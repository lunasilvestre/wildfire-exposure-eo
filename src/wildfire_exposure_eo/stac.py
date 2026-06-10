"""STAC item resolution against Microsoft Planetary Computer.

Builds a deterministic manifest of S2 L2A, S1 GRD, Cop-DEM GLO-30, and ESA
WorldCover items intersecting a Polygon AOI. Output is JSON only — no rasters
are read or downloaded here.

Implements the two-pass S2 cloud-cover asymmetry from `docs/methodology.md` §3:
strict 30% for spring, relaxed 60% for summer with explicit provenance of the
relaxation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from wildfire_exposure_eo.schemas import StacItemRef, StacManifest, StacWindow

if TYPE_CHECKING:
    import pystac
    from pystac_client import Client
    from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Asset keys we actually use downstream. Kept narrow so `stackstac` reads are
# minimal and reproducible. Adding to these lists is a deliberate scope change.
S2_ASSETS: tuple[str, ...] = ("B02", "B03", "B04", "B08", "B11", "B12", "SCL")
S1_ASSETS: tuple[str, ...] = ("vh", "vv")
DEM_ASSETS: tuple[str, ...] = ("data",)
WORLDCOVER_ASSETS: tuple[str, ...] = ("map",)

SUMMER_RELAXED_REASON = (
    "Cloud-cover relaxed from 30% (spring) to 60% (summer): Atlantic-coast PT-01 "
    "summer cloud cover wipes out a strict filter. Per docs/methodology.md §3, "
    "downstream code masks pixels via the S2 SCL band."
)


def load_aoi_geometry(aoi_path: Path) -> tuple[BaseGeometry, str]:
    """Load AOI GeoJSON and return (union geometry, canonical-GeoJSON SHA-256).

    The SHA is computed over the geometry alone, with `sort_keys=True` and the
    compact `(",", ":")` separators, so re-saving with different Feature
    properties leaves the hash unchanged.
    """
    payload = json.loads(aoi_path.read_text())
    geoms: list[BaseGeometry] = []
    if payload.get("type") == "FeatureCollection":
        for feat in payload.get("features", []):
            geom = feat.get("geometry")
            if geom:
                geoms.append(shape(geom))
    elif payload.get("type") == "Feature":
        geom = payload.get("geometry")
        if geom:
            geoms.append(shape(geom))
    elif "coordinates" in payload:
        geoms.append(shape(payload))
    if not geoms:
        raise ValueError(f"no geometry found in {aoi_path}")
    union = unary_union(geoms) if len(geoms) > 1 else geoms[0]
    canonical = json.dumps(mapping(union), sort_keys=True, separators=(",", ":"))
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return union, sha


def _strip_sas(href: str) -> str:
    """Drop the SAS-token query string (and fragment) from a blob URL."""
    parsed = urlparse(href)
    if not parsed.query and not parsed.fragment:
        return href
    return urlunparse(parsed._replace(query="", fragment=""))


def _href_root_for_item(item: pystac.Item) -> str:
    """Return the un-signed blob directory the item lives under.

    Strategy: pick the alphabetically-first asset key, strip its SAS token,
    drop the filename component to obtain the per-item directory. Stable
    across runs since asset-key ordering is deterministic.
    """
    for key in sorted(item.assets):
        href = item.assets[key].href
        if not href:
            continue
        cleaned = _strip_sas(href)
        if "/" in cleaned:
            return cleaned.rsplit("/", 1)[0]
        return cleaned
    return f"stac-item://{item.collection_id}/{item.id}"


def _item_bbox(item: pystac.Item) -> tuple[float, float, float, float]:
    if item.bbox is not None and len(item.bbox) >= 4:
        return (
            float(item.bbox[0]),
            float(item.bbox[1]),
            float(item.bbox[2]),
            float(item.bbox[3]),
        )
    geom = item.geometry
    if geom:
        b = shape(geom).bounds
        return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    raise ValueError(f"item {item.id} has neither bbox nor geometry")


def _item_datetime(item: pystac.Item) -> datetime:
    if item.datetime is not None:
        return item.datetime
    props = item.properties or {}
    raw = props.get("start_datetime") or props.get("end_datetime")
    if isinstance(raw, str):
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    raise ValueError(f"item {item.id} has no datetime")


def _to_item_ref(
    item: pystac.Item,
    *,
    collection: str,
    assets_referenced: tuple[str, ...],
    extra: dict[str, str | int | float] | None = None,
) -> StacItemRef:
    props = item.properties or {}
    cc_raw = props.get("eo:cloud_cover")
    cc: float | None = float(cc_raw) if isinstance(cc_raw, int | float) else None
    return StacItemRef(
        collection=collection,
        item_id=item.id,
        datetime_iso=_item_datetime(item),
        bbox=_item_bbox(item),
        cloud_cover=cc,
        assets_referenced=assets_referenced,
        href_root=_href_root_for_item(item),
        extra=extra or {},
    )


def _sorted_refs(refs: list[StacItemRef]) -> list[StacItemRef]:
    return sorted(refs, key=lambda r: (r.datetime_iso, r.item_id))


def _log_candidates(label: str, collection: str, items: list[pystac.Item]) -> None:
    logger.info("[stac] %s/%s: %d candidate item(s)", collection, label, len(items))
    for it in items:
        dt = it.datetime.isoformat() if it.datetime else "<no-dt>"
        logger.info("[stac]   %s  %s", dt, it.id)


def resolve_sentinel_2(
    aoi: BaseGeometry,
    window_start: date,
    window_end: date,
    *,
    max_cloud_cover: int,
    client: Client,
    label: str = "spring",
) -> list[StacItemRef]:
    """Resolve S2 L2A items intersecting `aoi` in [`window_start`, `window_end`]."""
    search = client.search(
        collections=["sentinel-2-l2a"],
        intersects=mapping(aoi),
        datetime=f"{window_start.isoformat()}/{window_end.isoformat()}",
        query={"eo:cloud_cover": {"lte": max_cloud_cover}},
    )
    items = list(search.items())
    _log_candidates(label, "sentinel-2-l2a", items)
    refs = [
        _to_item_ref(it, collection="sentinel-2-l2a", assets_referenced=S2_ASSETS) for it in items
    ]
    return _sorted_refs(refs)


def resolve_sentinel_1(
    aoi: BaseGeometry,
    window_start: date,
    window_end: date,
    *,
    mode: str = "IW",
    polarizations: tuple[str, ...] = ("VV", "VH"),
    client: Client,
    label: str = "all",
) -> list[StacItemRef]:
    """Resolve S1 GRD items intersecting `aoi` in [`window_start`, `window_end`]."""
    search = client.search(
        collections=["sentinel-1-grd"],
        intersects=mapping(aoi),
        datetime=f"{window_start.isoformat()}/{window_end.isoformat()}",
        query={"sar:instrument_mode": {"eq": mode}},
    )
    items = list(search.items())
    _log_candidates(label, "sentinel-1-grd", items)
    extra: dict[str, str | int | float] = {
        "mode": mode,
        "polarizations": ",".join(polarizations),
    }
    refs = [
        _to_item_ref(it, collection="sentinel-1-grd", assets_referenced=S1_ASSETS, extra=extra)
        for it in items
    ]
    return _sorted_refs(refs)


def resolve_cop_dem(
    aoi: BaseGeometry,
    *,
    client: Client,
    label: str = "static",
) -> list[StacItemRef]:
    """Resolve Cop-DEM GLO-30 items intersecting `aoi` (static layer)."""
    search = client.search(
        collections=["cop-dem-glo-30"],
        intersects=mapping(aoi),
    )
    items = list(search.items())
    _log_candidates(label, "cop-dem-glo-30", items)
    refs = [
        _to_item_ref(it, collection="cop-dem-glo-30", assets_referenced=DEM_ASSETS) for it in items
    ]
    return _sorted_refs(refs)


def resolve_worldcover(
    aoi: BaseGeometry,
    *,
    vintage: int = 2021,
    client: Client,
    label: str | None = None,
) -> list[StacItemRef]:
    """Resolve ESA WorldCover items intersecting `aoi` for the given vintage year."""
    label = label or f"v{vintage}"
    search = client.search(
        collections=["esa-worldcover"],
        intersects=mapping(aoi),
        datetime=f"{vintage}-01-01/{vintage}-12-31",
    )
    items = list(search.items())
    _log_candidates(label, "esa-worldcover", items)
    refs = [
        _to_item_ref(it, collection="esa-worldcover", assets_referenced=WORLDCOVER_ASSETS)
        for it in items
    ]
    return _sorted_refs(refs)


def _default_client_factory(url: str) -> Any:
    """Open a real pystac-client `Client`. Indirected so tests can monkeypatch."""
    from pystac_client import Client

    return Client.open(url)


def code_commit_sha(*, cwd: Path | None = None) -> str:
    """Return `git rev-parse HEAD` (with a `-dirty` suffix if the tree has
    uncommitted changes), or `'unknown'` if not in a git repo.

    The suffix matters for provenance: a bare SHA claims the artifact is
    reproducible from that commit, which is false when the producing code
    was uncommitted at run time.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        sha = out.stdout.strip()
        if not sha:
            return "unknown"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return f"{sha}-dirty" if status.stdout.strip() else sha
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"


def build_manifest(
    aoi_path: Path,
    *,
    spring_start: date,
    spring_end: date,
    spring_cloud: int,
    summer_start: date,
    summer_end: date,
    summer_cloud: int,
    worldcover_vintage: int = 2021,
    catalog_url: str = PC_STAC_URL,
    client: Client | None = None,
    run_id: str | None = None,
    resolved_at_utc: datetime | None = None,
    commit_sha: str | None = None,
) -> StacManifest:
    """Compose a full STAC manifest for the AOI + windows.

    Deterministic for fixed `aoi_path` / windows / catalog: items within each
    `StacWindow` are sorted by `(datetime, item_id)`. The provenance fields
    `run_id`, `resolved_at_utc`, and `code_commit_sha` are *not* deterministic
    by design — they identify *this run*, not the data.
    """
    aoi, aoi_sha = load_aoi_geometry(aoi_path)
    resolved_at = resolved_at_utc or datetime.now(UTC)
    run_id = run_id or resolved_at.strftime("%Y%m%dT%H%M%SZ")
    sha = commit_sha or code_commit_sha(cwd=Path.cwd())

    cli: Any = client if client is not None else _default_client_factory(catalog_url)

    spring = resolve_sentinel_2(
        aoi,
        spring_start,
        spring_end,
        max_cloud_cover=spring_cloud,
        client=cli,
        label="spring",
    )
    summer = resolve_sentinel_2(
        aoi,
        summer_start,
        summer_end,
        max_cloud_cover=summer_cloud,
        client=cli,
        label="summer",
    )
    s1_start = min(spring_start, summer_start)
    s1_end = max(spring_end, summer_end)
    s1 = resolve_sentinel_1(aoi, s1_start, s1_end, client=cli, label="all")
    dem = resolve_cop_dem(aoi, client=cli)
    wc = resolve_worldcover(aoi, vintage=worldcover_vintage, client=cli)

    windows: dict[str, tuple[StacWindow, ...]] = {
        "sentinel-2-l2a": (
            StacWindow(
                label="spring",
                start=spring_start,
                end=spring_end,
                max_cloud_cover=spring_cloud,
                items=tuple(spring),
                items_returned=len(spring),
                relaxed_threshold_reason=None,
            ),
            StacWindow(
                label="summer",
                start=summer_start,
                end=summer_end,
                max_cloud_cover=summer_cloud,
                items=tuple(summer),
                items_returned=len(summer),
                relaxed_threshold_reason=SUMMER_RELAXED_REASON,
            ),
        ),
        "sentinel-1-grd": (
            StacWindow(
                label="all",
                start=s1_start,
                end=s1_end,
                max_cloud_cover=None,
                items=tuple(s1),
                items_returned=len(s1),
                relaxed_threshold_reason=None,
            ),
        ),
        "cop-dem-glo-30": (
            StacWindow(
                label="static",
                start=date(1970, 1, 1),
                end=date(9999, 12, 31),
                max_cloud_cover=None,
                items=tuple(dem),
                items_returned=len(dem),
                relaxed_threshold_reason=None,
            ),
        ),
        "esa-worldcover": (
            StacWindow(
                label=f"v{worldcover_vintage}",
                start=date(worldcover_vintage, 1, 1),
                end=date(worldcover_vintage, 12, 31),
                max_cloud_cover=None,
                items=tuple(wc),
                items_returned=len(wc),
                relaxed_threshold_reason=None,
            ),
        ),
    }

    totals = {coll: sum(w.items_returned for w in winds) for coll, winds in windows.items()}

    return StacManifest(
        run_id=run_id,
        code_commit_sha=sha,
        aoi_path=str(aoi_path),
        aoi_geometry_sha=aoi_sha,
        resolved_at_utc=resolved_at,
        stac_catalog_url=catalog_url,
        collections=windows,
        totals=totals,
    )


def write_manifest(manifest: StacManifest, path: Path) -> Path:
    """Write a manifest JSON with `indent=2` + `sort_keys=True` for diff-friendliness.

    `sort_keys` only sorts dict keys; item-array ordering (already sorted by
    `(datetime, item_id)` per `build_manifest`) is preserved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return path
