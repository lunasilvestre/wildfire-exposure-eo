"""ICNF Áreas Ardidas ingestion — ArcGIS REST query, geometry conversion, GeoParquet output."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import MultiPolygon, Point, Polygon

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

from wildfire_exposure_eo.schemas.burn_perimeter import (
    BurnPerimeterProvenance,
    IcnfLayerDescriptor,
)

log = logging.getLogger(__name__)

ICNF_MAPSERVER_URL = "https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer"
_USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 burns (+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)
_EPSG_NATIVE = "EPSG:3763"
_EPSG_OUTPUT = "EPSG:4326"
_REQUEST_TIMEOUT = 60
_RETRIES = 2

# ArcGIS REST field names for year and area (case-insensitive search)
_YEAR_FIELDS = ("ano", "year", "ano_")
_AREA_FIELDS = ("area_ha", "area_ha_", "areaha", "area")


def discover_icnf_layers(
    *,
    mapserver_url: str = ICNF_MAPSERVER_URL,
) -> list[IcnfLayerDescriptor]:
    """Query the ICNF MapServer index and return one descriptor per available layer.

    Makes one GET request to ``{mapserver_url}?f=json`` to enumerate layers, then
    one count-only query per layer to populate ``feature_count_total``.  Year is
    parsed from the layer name; layers whose names contain no 4-digit year are skipped
    with a warning so we never invent identifiers.
    """
    index_url = f"{mapserver_url}?f=json"
    log.info("Querying MapServer index: %s", index_url)
    resp = _get_with_retry(index_url)
    data = resp.json()

    raw_layers: list[dict[str, Any]] = data.get("layers", [])
    if not raw_layers:
        raise RuntimeError(
            f"MapServer index at {index_url} returned no layers — "
            "check the URL or whether the service is available."
        )
    log.info("MapServer has %d layers", len(raw_layers))

    descriptors: list[IcnfLayerDescriptor] = []
    for layer in raw_layers:
        layer_id: int = layer["id"]
        name: str = layer.get("name", "")
        year = _parse_layer_year(name)
        if year is None:
            log.warning("Layer %d (%r): cannot parse year from name — skipping", layer_id, name)
            continue

        count = _fetch_layer_count(layer_id, mapserver_url=mapserver_url)
        descriptors.append(
            IcnfLayerDescriptor(
                layer_id=layer_id,
                year=year,
                name=name,
                feature_count_total=count,
            )
        )
        log.info("Layer %d  year=%d  name=%r  features=%d", layer_id, year, name, count)

    return sorted(descriptors, key=lambda d: d.year)


def fetch_icnf_layer(
    layer: IcnfLayerDescriptor,
    aoi: Any,  # shapely BaseGeometry in EPSG:4326
    *,
    batch_size: int = 1000,
    mapserver_url: str = ICNF_MAPSERVER_URL,
) -> gpd.GeoDataFrame:
    """Fetch all features for one layer intersecting the AOI bbox.

    Issues paginated ArcGIS REST queries (``f=json``) in the server's native
    ``EPSG:3763`` and reprojects to ``EPSG:4326`` before returning.  The AOI
    geometry is supplied in EPSG:4326; ``inSR=4326`` is passed so the server
    filters in the correct coordinate space.

    Returns an empty GeoDataFrame (EPSG:4326) when no features intersect the AOI.
    """
    bounds = aoi.bounds  # (minx, miny, maxx, maxy) in EPSG:4326
    geometry_param = json.dumps(
        {
            "xmin": bounds[0],
            "ymin": bounds[1],
            "xmax": bounds[2],
            "ymax": bounds[3],
        }
    )

    query_url = f"{mapserver_url}/{layer.layer_id}/query"
    all_features: list[dict[str, Any]] = []
    offset = 0

    while True:
        params: dict[str, str | int] = {
            "where": "1=1",
            "geometry": geometry_param,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "resultOffset": offset,
            "resultRecordCount": batch_size,
            "returnExceededLimitFeatures": "true",
            "f": "json",
        }
        log.info(
            "Layer %d year=%d: query offset=%d batch=%d",
            layer.layer_id,
            layer.year,
            offset,
            batch_size,
        )
        resp = _get_with_retry(query_url, params=params)
        payload = resp.json()

        if "error" in payload:
            raise RuntimeError(f"ArcGIS REST error for layer {layer.layer_id}: {payload['error']}")

        features: list[dict[str, Any]] = payload.get("features", [])
        all_features.extend(features)
        log.info(
            "Layer %d: page offset=%d returned %d features (total so far: %d)",
            layer.layer_id,
            offset,
            len(features),
            len(all_features),
        )

        if not payload.get("exceededTransferLimit", False) or not features:
            break
        offset += len(features)

    if not all_features:
        log.info("Layer %d year=%d: no features in AOI", layer.layer_id, layer.year)
        return _empty_gdf()

    rows = _parse_features(all_features, layer)
    if not rows:
        log.info("Layer %d year=%d: no parseable geometries", layer.layer_id, layer.year)
        return _empty_gdf()

    gdf = gpd.GeoDataFrame(rows, geometry="geometry")
    # Set native CRS (EPSG:3763) before reprojecting — never implicit
    gdf = gdf.set_crs(_EPSG_NATIVE)
    log.info(
        "Layer %d: reprojecting %d features %s → %s",
        layer.layer_id,
        len(gdf),
        _EPSG_NATIVE,
        _EPSG_OUTPUT,
    )
    gdf = gdf.to_crs(_EPSG_OUTPUT)
    return gdf


def combine_burns(
    per_year: dict[int, gpd.GeoDataFrame],
) -> gpd.GeoDataFrame:
    """Concatenate per-year frames; apply stable sort; assign canonical row IDs.

    Sort order: ``(vintage_year ascending, area_ha descending, feature_id ascending)``.
    """
    frames = [df for df in per_year.values() if not df.empty]
    if not frames:
        return _empty_gdf()

    combined: gpd.GeoDataFrame = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs=_EPSG_OUTPUT
    )
    combined = combined.sort_values(  # pyright: ignore[reportAssignmentType]
        ["vintage_year", "area_ha", "feature_id"],
        ascending=[True, False, True],
    ).reset_index(drop=True)

    combined["row_id"] = [
        f"icnf:{r['vintage_year']}:{r['feature_id']}"
        for _, r in combined[["vintage_year", "feature_id"]].iterrows()
    ]
    return combined


def write_burns_geoparquet(
    gdf: gpd.GeoDataFrame,
    path: Path,
    *,
    run_provenance: BurnPerimeterProvenance,
) -> Path:
    """Write the combined burn-perimeter GeoDataFrame as a GeoParquet.

    CRS is pinned to EPSG:4326; compression is snappy.  Per-row provenance is
    stored as a nested struct column — the provenance object is the same for
    every row in a single run (vintage-specific fields live in the row itself).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    out = gdf.copy()
    out["geometry_wkb"] = out["geometry"].apply(lambda g: g.wkb)
    prov_dict = run_provenance.model_dump(mode="json")
    out["provenance"] = [prov_dict] * len(out)
    out.to_parquet(path, compression="snappy", index=False)
    log.info("Wrote %d rows → %s", len(out), path)
    return path


# ── internal helpers ───────────────────────────────────────────────────────────


def _get_with_retry(
    url: str,
    *,
    params: dict[str, str | int] | None = None,
    retries: int = _RETRIES,
    timeout: int = _REQUEST_TIMEOUT,
) -> requests.Response:
    """GET with exponential back-off; raises on final failure."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=timeout,
            )
            if resp.status_code >= 500:
                last_exc = requests.HTTPError(f"Server returned {resp.status_code}", response=resp)
                if attempt < retries:
                    wait = 2**attempt
                    log.warning(
                        "GET %s → %d (attempt %d/%d) — retry in %ds",
                        url,
                        resp.status_code,
                        attempt + 1,
                        retries + 1,
                        wait,
                    )
                    time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                wait = 2**attempt
                log.warning(
                    "GET %s error (attempt %d/%d): %s — retry in %ds",
                    url,
                    attempt + 1,
                    retries + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
    raise RuntimeError(f"GET {url} failed after {retries + 1} attempts: {last_exc}") from last_exc


def _fetch_layer_count(layer_id: int, *, mapserver_url: str) -> int:
    """Return the total feature count for a layer (no AOI filter)."""
    url = f"{mapserver_url}/{layer_id}/query"
    params: dict[str, str | int] = {
        "where": "1=1",
        "returnCountOnly": "true",
        "f": "json",
    }
    try:
        resp = _get_with_retry(url, params=params)
        return int(resp.json().get("count", 0))
    except Exception as exc:
        log.warning("Layer %d: count request failed (%s) — using 0", layer_id, exc)
        return 0


def _parse_layer_year(name: str) -> int | None:
    """Extract the first (start) year from an ICNF layer name.

    Returns None if no 4-digit year can be found — caller must skip the layer
    (never invent a year per non-negotiable #1).

    Examples:
        "Áreas Ardidas 2020"     → 2020
        "Áreas Ardidas 1975-1989" → 1975
        "areas_ardidas_2009"      → 2009
    """
    # Extract all 4-digit sequences in the fire-year range; \b does not work with
    # underscore-separated names like "areas_ardidas_2009" because _ is a \w char.
    years = [int(y) for y in re.findall(r"\d{4}", name) if 1900 <= int(y) <= 2100]
    return years[0] if years else None


def _parse_features(
    features: list[dict[str, Any]],
    layer: IcnfLayerDescriptor,
) -> list[dict[str, Any]]:
    """Convert raw ArcGIS REST feature dicts into row dicts for a GeoDataFrame.

    Geometries are returned in the server's native CRS (EPSG:3763); area_ha is
    read from attributes when available (preferred) and computed later otherwise.
    vintage_year is read from the ``ANO`` attribute when present; falls back to
    the layer's year field with an INFO log (not invented — layer year derives
    from the server's layer name).
    """
    rows = []
    for feat in features:
        attrs: dict[str, Any] = feat.get("attributes") or {}
        geom_dict: dict[str, Any] = feat.get("geometry") or {}

        geom = _parse_arcgis_geometry(geom_dict)
        if geom is None or geom.is_empty:
            continue

        # Feature ID — prefer OBJECTID, fall back to FID
        feature_id: int = int(
            attrs.get("OBJECTID") or attrs.get("objectid") or attrs.get("FID") or 0
        )

        # Vintage year — prefer ANO attribute over layer year
        vintage_year = _extract_year_attr(attrs)
        if vintage_year is None:
            log.info(
                "Layer %d: feature %d has no ANO attribute — using layer year %d",
                layer.layer_id,
                feature_id,
                layer.year,
            )
            vintage_year = layer.year

        # Area — prefer attribute, mark 0/-1 for fallback computation later
        area_ha = _extract_area_attr(attrs)

        rows.append(
            {
                "feature_id": feature_id,
                "vintage_year": vintage_year,
                "area_ha": area_ha,
                "geometry": geom,
            }
        )

    return rows


def _extract_year_attr(attrs: dict[str, Any]) -> int | None:
    """Return the year from ICNF feature attributes, or None if absent."""
    for key in attrs:
        if key.lower().rstrip("_") in _YEAR_FIELDS:
            val = attrs[key]
            if val is not None:
                try:
                    year = int(val)
                    if 1970 <= year <= 2100:
                        return year
                except (ValueError, TypeError):
                    pass
    return None


def _extract_area_attr(attrs: dict[str, Any]) -> float:
    """Return area_ha from feature attributes; 0.0 signals fallback-needed."""
    for key in attrs:
        if key.lower().rstrip("_") in _AREA_FIELDS:
            val = attrs[key]
            if val is not None:
                try:
                    area = float(val)
                    if area > 0:
                        return area
                except (ValueError, TypeError):
                    pass
    return 0.0


def _parse_arcgis_geometry(geom_dict: dict[str, Any]) -> BaseGeometry | None:
    """Convert an ArcGIS REST geometry dict to a shapely geometry.

    Supports esriGeometryPolygon (rings) and esriGeometryPoint (x/y).
    For polygons with multiple rings each ring is treated as an outer polygon
    (burn perimeters are rarely multi-part with holes; a simple union suffices).
    """
    if not geom_dict:
        return None

    rings = geom_dict.get("rings")
    if rings is not None:
        polys = [Polygon(ring) for ring in rings if len(ring) >= 4]
        polys = [p for p in polys if not p.is_empty]
        if not polys:
            return None
        return MultiPolygon(polys) if len(polys) > 1 else polys[0]

    x = geom_dict.get("x")
    y = geom_dict.get("y")
    if x is not None and y is not None:
        return Point(float(x), float(y))

    return None


def _empty_gdf() -> gpd.GeoDataFrame:
    """Return an empty GeoDataFrame with the canonical burn columns in EPSG:4326."""
    cols = [
        "row_id",
        "feature_id",
        "vintage_year",
        "area_ha",
        "geometry",
        "geometry_wkb",
        "provenance",
    ]
    empty = pd.DataFrame(columns=pd.Index(cols))  # pyright: ignore[reportArgumentType]
    return gpd.GeoDataFrame(empty, geometry="geometry").set_crs(_EPSG_OUTPUT)


def fill_missing_areas(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Compute area_ha in EPSG:3763 for rows where it is 0 (attribute absent or invalid).

    Mutates a copy; never computes area in degree-units (EPSG:4326).
    """
    mask = gdf["area_ha"] == 0.0
    if not mask.any():
        return gdf
    out = gdf.copy()
    projected = gdf[mask].to_crs(_EPSG_NATIVE)
    out.loc[mask, "area_ha"] = projected.geometry.area / 10_000
    return out


def fetch_burns(
    aoi_path: Path,
    out_path: Path,
    *,
    start_year: int = 1975,
    end_year: int = 2025,
    mapserver_url: str = ICNF_MAPSERVER_URL,
    run_id: str,
    code_commit_sha: str,
    aoi_geometry_sha: str,
    batch_size: int = 1000,
) -> Path:
    """Orchestrate: discover layers, fetch per-year frames, combine, write GeoParquet.

    Returns the path to the written GeoParquet.  Layers outside [start_year, end_year]
    are skipped.  An empty GeoParquet is written (and returned) if no features intersect
    the AOI for any year in the requested range — never raises on zero results.
    """
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union as _union

    aoi_geojson = json.loads(aoi_path.read_text())
    features = aoi_geojson.get("features", [aoi_geojson])
    aoi_geom = _union([_shape(f["geometry"]) for f in features])

    layers = discover_icnf_layers(mapserver_url=mapserver_url)
    layers_in_range = [lyr for lyr in layers if start_year <= lyr.year <= end_year]
    if not layers_in_range:
        log.warning(
            "No layers found in year range %d–%d (total layers: %d)",
            start_year,
            end_year,
            len(layers),
        )

    fetched_at = datetime.now(UTC)
    per_year: dict[int, gpd.GeoDataFrame] = {}

    for layer in layers_in_range:
        log.info("Fetching layer %d  year=%d  name=%r", layer.layer_id, layer.year, layer.name)
        gdf = fetch_icnf_layer(
            layer,
            aoi_geom,
            batch_size=batch_size,
            mapserver_url=mapserver_url,
        )
        if gdf.empty:
            log.info("Layer %d year=%d: no features in AOI — skipping", layer.layer_id, layer.year)
            continue
        gdf = fill_missing_areas(gdf)
        per_year[layer.year] = gdf

    combined = combine_burns(per_year)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    prov = BurnPerimeterProvenance(
        icnf_layer_id=-1,  # -1 = combined (multiple layers)
        icnf_layer_name="combined",
        vintage_year=-1,  # -1 = multiple vintages in the output
        mapserver_url=mapserver_url,
        fetched_at_utc=fetched_at,
        run_id=run_id,
        code_commit_sha=code_commit_sha,
        aoi_path=str(aoi_path),
        aoi_geometry_sha=aoi_geometry_sha,
    )
    return write_burns_geoparquet(combined, out_path, run_provenance=prov)
