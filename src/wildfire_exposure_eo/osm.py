"""OSM asset extraction — Overpass API query, geometry conversion, GeoParquet output."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import pandas as pd
import requests
import yaml
from pydantic import BaseModel, ConfigDict
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

from wildfire_exposure_eo.schemas.osm_asset import OsmAssetProvenance

log = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
_FALLBACK_ENDPOINT = "https://overpass.kumi.systems/api/interpreter"
_USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 osm (+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)


# ── taxonomy models ────────────────────────────────────────────────────────────


class InfrastructureClass(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    class_id: str
    name: str
    osm_filters: list[str]
    buffer_radius_m: float
    criticality_weight: float


class Taxonomy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    taxonomy_sha: str
    classes: list[InfrastructureClass]


# ── result container ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OverpassResult:
    elements: list[dict[str, Any]]
    osm_snapshot_iso: datetime
    endpoint_used: str
    query_sha: str


# ── public functions ───────────────────────────────────────────────────────────


def load_taxonomy(path: Path) -> Taxonomy:
    """Load and Pydantic-validate the critical_infrastructure.yaml taxonomy."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    data = yaml.safe_load(raw)
    classes = [InfrastructureClass(class_id=cid, **attrs) for cid, attrs in data["classes"].items()]
    return Taxonomy(version=data["version"], taxonomy_sha=sha, classes=classes)


def build_overpass_query(
    klass: InfrastructureClass,
    bbox: tuple[float, float, float, float],
) -> str:
    """Build the Overpass QL query for one infrastructure class over a bbox.

    bbox is (min_lon, min_lat, max_lon, max_lat) in EPSG:4326.
    Overpass expects (south, west, north, east).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    overpass_bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"

    lines: list[str] = ["[out:json][timeout:60];", "("]
    for filt in klass.osm_filters:
        elem_type = filt.split("[")[0].strip()
        if elem_type not in ("node", "way", "relation"):
            log.warning("Unrecognised element type in filter %r — skipping", filt)
            continue
        tag_part = filt[len(elem_type) :]
        lines.append(f"  {elem_type}{tag_part}({overpass_bbox});")
    lines += [");", "out body;", ">;", "out skel qt;"]
    return "\n".join(lines)


def query_overpass(
    query: str,
    *,
    endpoint: str = _DEFAULT_ENDPOINT,
    timeout_s: int = 60,
    retries: int = 2,
    fallback_endpoint: str | None = _FALLBACK_ENDPOINT,
) -> OverpassResult:
    """Issue the Overpass POST, parse JSON, return an OverpassResult.

    Retries the primary endpoint with exponential backoff on 5xx; falls back
    to fallback_endpoint after primary exhaustion.
    """
    query_sha = hashlib.sha256(query.encode()).hexdigest()
    endpoints_to_try: list[str] = [endpoint]
    if fallback_endpoint and fallback_endpoint != endpoint:
        endpoints_to_try.append(fallback_endpoint)

    last_exc: Exception | None = None
    for ep in endpoints_to_try:
        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    ep,
                    data={"data": query},
                    headers={"User-Agent": _USER_AGENT},
                    timeout=timeout_s,
                )
                if resp.status_code >= 500:
                    last_exc = requests.HTTPError(
                        f"Overpass returned {resp.status_code}", response=resp
                    )
                    if attempt < retries:
                        wait = 2**attempt
                        log.warning(
                            "Overpass %s returned %d (attempt %d/%d) — retrying in %ds",
                            ep,
                            resp.status_code,
                            attempt + 1,
                            retries + 1,
                            wait,
                        )
                        time.sleep(wait)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                osm3s = payload.get("osm3s", {})
                snapshot_str = osm3s.get("timestamp_osm_base", "")
                try:
                    snapshot_dt = datetime.fromisoformat(snapshot_str.rstrip("Z") + "+00:00")
                except (ValueError, AttributeError):
                    snapshot_dt = datetime.now(UTC)
                    log.warning("No osm3s.timestamp_osm_base in response — using current UTC")
                elements: list[dict[str, Any]] = payload.get("elements", [])
                log.info("Overpass %s → %d elements (snapshot %s)", ep, len(elements), snapshot_dt)
                return OverpassResult(
                    elements=elements,
                    osm_snapshot_iso=snapshot_dt,
                    endpoint_used=ep,
                    query_sha=query_sha,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < retries:
                    wait = 2**attempt
                    log.warning(
                        "Overpass %s request error (attempt %d/%d): %s — retrying in %ds",
                        ep,
                        attempt + 1,
                        retries + 1,
                        exc,
                        wait,
                    )
                    time.sleep(wait)

    raise RuntimeError(
        f"Overpass exhausted all endpoints and retries. Last error: {last_exc}"
    ) from last_exc


def geometrise(
    elements: list[dict[str, Any]],
    *,
    klass: InfrastructureClass,
) -> gpd.GeoDataFrame:
    """Convert Overpass JSON elements to a GeoDataFrame in EPSG:4326.

    Nodes → Point; closed ways → Polygon (area classes) or LineString;
    open ways → LineString; relations with outer members → MultiPolygon.
    """
    node_coords: dict[int, tuple[float, float]] = {
        el["id"]: (el["lon"], el["lat"])
        for el in elements
        if el.get("type") == "node" and "lat" in el and "lon" in el
    }

    _area_classes = frozenset(
        {
            "power.substation",
            "emergency.fire_station",
            "emergency.hospital",
            "emergency.police",
            "education.school",
            "water.treatment_plant",
            "water.reservoir",
        }
    )
    is_area_class = klass.class_id in _area_classes

    rows: list[dict[str, Any]] = []
    for el in elements:
        # Only process elements with a "tags" key — those are actual query-result elements.
        # Bare skel nodes (from >; out skel qt;) provide coordinates but have no "tags" entry.
        if "tags" not in el:
            continue
        el_type = el.get("type")
        el_id: int = el.get("id", 0)
        tags: dict[str, str] = {str(k): str(v) for k, v in el.get("tags", {}).items()}
        geom: BaseGeometry | None = None

        if el_type == "node":
            if "lat" not in el or "lon" not in el:
                continue
            geom = Point(el["lon"], el["lat"])

        elif el_type == "way":
            nds = el.get("nodes", [])
            coords = [node_coords[n] for n in nds if n in node_coords]
            if len(coords) < 2:
                log.debug("way/%d: insufficient resolved coords — skipping", el_id)
                continue
            is_closed = len(nds) >= 4 and nds[0] == nds[-1]
            geom = Polygon(coords) if is_closed and is_area_class else LineString(coords)

        elif el_type == "relation":
            members = el.get("members", [])
            outer_way_refs = [
                m["ref"] for m in members if m.get("role") == "outer" and m.get("type") == "way"
            ]
            if not outer_way_refs:
                log.debug("relation/%d: no outer way members — skipping", el_id)
                continue
            way_index: dict[int, dict[str, Any]] = {
                e["id"]: e for e in elements if e.get("type") == "way"
            }
            polys: list[Polygon] = []
            for ref in outer_way_refs:
                way_el = way_index.get(ref)
                if way_el is None:
                    continue
                nds = way_el.get("nodes", [])
                coords = [node_coords[n] for n in nds if n in node_coords]
                if len(coords) >= 4:
                    polys.append(Polygon(coords))
            if not polys:
                log.debug("relation/%d: no outer polygons built — skipping", el_id)
                continue
            geom = MultiPolygon(polys) if len(polys) > 1 else polys[0]

        else:
            continue

        if geom is None or geom.is_empty:
            continue

        centroid = geom.centroid
        rows.append(
            {
                "osm_type": el_type,
                "osm_id": el_id,
                "geometry": geom,
                "centroid_lon": centroid.x,
                "centroid_lat": centroid.y,
                "tags": tags,
            }
        )

    if not rows:
        empty_df = pd.DataFrame(
            columns=pd.Index(  # pyright: ignore[reportArgumentType]
                ["osm_type", "osm_id", "geometry", "centroid_lon", "centroid_lat", "tags"]
            )
        )
        return gpd.GeoDataFrame(empty_df, geometry="geometry").set_crs("EPSG:4326")

    gdf = gpd.GeoDataFrame(rows, geometry="geometry").set_crs("EPSG:4326")
    return gdf


def write_geoparquet(
    gdf: gpd.GeoDataFrame,
    path: Path,
    *,
    run_provenance: dict[str, Any],
) -> Path:
    """Write a GeoParquet with per-row provenance columns. CRS is EPSG:4326."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = gdf.copy()
    asset_class = run_provenance["asset_class"]
    prov_without_class = {k: v for k, v in run_provenance.items() if k != "asset_class"}

    out["asset_id"] = [f"osm:{t}/{i}" for t, i in zip(out["osm_type"], out["osm_id"], strict=True)]
    out["asset_class"] = asset_class
    out["geometry_wkb"] = out["geometry"].apply(lambda g: g.wkb)
    # Serialize tags as JSON strings to avoid pyarrow struct-promotion null values when
    # writing mixed-key dicts to parquet. OsmAssetProvenance.tags validator auto-parses.
    out["tags"] = out["tags"].apply(json.dumps)
    out["provenance"] = [prov_without_class] * len(out)

    out = out.sort_values(["asset_class", "osm_type", "osm_id"]).reset_index(drop=True)
    out.to_parquet(path, compression="snappy", index=False)
    log.info("Wrote %d rows → %s", len(out), path)
    return path


def fetch_osm(
    aoi_path: Path,
    taxonomy_path: Path,
    out_path: Path,
    *,
    endpoint: str = _DEFAULT_ENDPOINT,
    fallback_endpoint: str | None = _FALLBACK_ENDPOINT,
    run_id: str,
    code_commit_sha: str,
    aoi_geometry_sha: str,
    inter_class_sleep_s: float = 2.0,
) -> Path:
    """Orchestrate: load taxonomy, query Overpass per class, write combined GeoParquet.

    Returns the path to the written GeoParquet.
    """
    bbox = _aoi_bbox(aoi_path)
    taxonomy = load_taxonomy(taxonomy_path)

    frames: list[gpd.GeoDataFrame] = []

    for idx, klass in enumerate(taxonomy.classes):
        if idx > 0 and inter_class_sleep_s > 0:
            log.debug("Sleeping %.1fs between Overpass queries …", inter_class_sleep_s)
            time.sleep(inter_class_sleep_s)
        query = build_overpass_query(klass, bbox)
        log.info("Querying Overpass for class %s …", klass.class_id)
        result = query_overpass(
            query,
            endpoint=endpoint,
            fallback_endpoint=fallback_endpoint,
        )
        gdf = geometrise(result.elements, klass=klass)

        if gdf.empty:
            log.warning("[YELLOW] %s — 0 elements", klass.class_id)
            continue
        log.info("[GREEN]  %s — %d elements", klass.class_id, len(gdf))

        prov = OsmAssetProvenance(
            osm_snapshot_iso=result.osm_snapshot_iso,
            overpass_endpoint=result.endpoint_used,
            overpass_query_sha=result.query_sha,
            taxonomy_sha=taxonomy.taxonomy_sha,
            taxonomy_version=taxonomy.version,
            run_id=run_id,
            code_commit_sha=code_commit_sha,
            aoi_path=str(aoi_path),
            aoi_geometry_sha=aoi_geometry_sha,
        )
        run_prov: dict[str, Any] = {
            "asset_class": klass.class_id,
            **prov.model_dump(mode="json"),
        }

        gdf = gdf.copy()
        gdf["asset_id"] = [
            f"osm:{t}/{i}" for t, i in zip(gdf["osm_type"], gdf["osm_id"], strict=True)
        ]
        gdf["asset_class"] = klass.class_id
        gdf["geometry_wkb"] = gdf["geometry"].apply(lambda g: g.wkb)
        # Serialize tags as JSON strings (see write_geoparquet docstring).
        gdf["tags"] = gdf["tags"].apply(json.dumps)
        prov_dict = {k: v for k, v in run_prov.items() if k != "asset_class"}
        gdf["provenance"] = [prov_dict] * len(gdf)
        frames.append(gdf)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not frames:
        log.warning("No elements found for any class — writing empty GeoParquet")
        cols = [
            "asset_id",
            "osm_type",
            "osm_id",
            "asset_class",
            "geometry",
            "geometry_wkb",
            "centroid_lon",
            "centroid_lat",
            "tags",
            "provenance",
        ]
        empty_df = pd.DataFrame(columns=pd.Index(cols))
        empty = gpd.GeoDataFrame(empty_df, geometry="geometry").set_crs("EPSG:4326")
        empty.to_parquet(out_path, compression="snappy", index=False)
        return out_path

    combined: gpd.GeoDataFrame = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs="EPSG:4326"
    )
    combined = combined.sort_values(  # pyright: ignore[reportAssignmentType]
        ["asset_class", "osm_type", "osm_id"]
    ).reset_index(drop=True)
    combined.to_parquet(out_path, compression="snappy", index=False)
    log.info("Combined GeoParquet: %d rows → %s", len(combined), out_path)
    return out_path


def _aoi_bbox(aoi_path: Path) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat) from the first AOI feature."""
    payload = json.loads(aoi_path.read_text())
    features = payload.get("features", [])
    if not features:
        raise ValueError(f"No features in AOI {aoi_path}")
    geom = features[0]["geometry"]
    coords = _flatten_coords(geom["coordinates"], geom["type"])
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (min(lons), min(lats), max(lons), max(lats))


def _flatten_coords(coords: Any, geom_type: str) -> list[tuple[float, float]]:
    if geom_type == "Point":
        return [(coords[0], coords[1])]
    if geom_type in ("LineString", "MultiPoint"):
        return [(c[0], c[1]) for c in coords]
    if geom_type in ("Polygon", "MultiLineString"):
        return [(c[0], c[1]) for ring in coords for c in ring]
    if geom_type == "MultiPolygon":
        return [(c[0], c[1]) for poly in coords for ring in poly for c in ring]
    raise ValueError(f"Unsupported geometry type: {geom_type}")
