"""Pick effective ~1 km smoke tiles for each candidate AOI.

For every `pilot.geojson` / `alt_*.geojson` under `data/aoi/`, this script:
  1. Issues a single Overpass query for power=tower nodes, building ways,
     and major-highway ways inside the parent AOI bbox.
  2. Queries the ICNF Áreas Ardidas MapServer (recent sampled years) for
     burn polygons intersecting the bbox.
  3. Generates a non-overlapping ~1 km × 1 km grid of candidate tiles.
  4. Scores each tile by: (a) qualifies = has ≥1 of each OSM class AND
     intersects ≥1 burn polygon; (b) tiebreak = total feature count.
  5. Writes the chosen tile to `data/aoi/smoke_<slug>.geojson`. The file
     for the frozen pilot is mirrored to `data/aoi/smoke.geojson`.

Run:
    uv run python scripts/pick_smoke_tile.py
    uv run python scripts/pick_smoke_tile.py --dry-run

Notes:
  - Overpass queries are throttled by `--sleep` between AOIs (default 4 s).
  - "Major highway" matches motorway|trunk|primary|secondary|tertiary|
    residential|unclassified (same as the audit's OSM check).
  - ICNF layers queried: 2017, 2020, 2021, 2022, 2023, 2024, 2025
    (mirrors `wildfire_exposure_eo.audit.ICNF_RECENT_LAYERS`).
  - The pilot file in `data/aoi/` is treated as the canonical pilot. If its
    bbox matches an alt, the alt file is skipped (dedup).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

# Constants mirror src/wildfire_exposure_eo/audit.py — keep in sync.
USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 pick-smoke (+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
ICNF_AREAS_ARDIDAS_MAPSERVER = (
    "https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer"
)
ICNF_RECENT_LAYERS: dict[int, str] = {
    20: "2025",
    19: "2024",
    18: "2023",
    17: "2022",
    15: "2021",
    0: "2020",
    6: "2017",
}

AOI_DIR = Path("data/aoi")
PILOT_FILE = AOI_DIR / "pilot.geojson"
SMOKE_FILE = AOI_DIR / "smoke.geojson"

# ~1 km cell at lat 40°: 1° lat ≈ 111 km, 1° lon ≈ 85 km.
CELL_DEG_LAT = 0.0090
CELL_DEG_LON = 0.0118

HIGHWAY_RE = "^(motorway|trunk|primary|secondary|tertiary|residential|unclassified)$"


# ---------------------------------------------------------------------------
# AOI discovery + I/O
# ---------------------------------------------------------------------------


def load_aoi(path: Path) -> tuple[dict[str, Any], tuple[float, float, float, float]]:
    """Return the parsed GeoJSON and the WGS84 bbox of every coordinate inside it."""
    payload = json.loads(path.read_text())
    coords: list[tuple[float, float]] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            if node and all(isinstance(x, int | float) for x in node[:2]) and len(node) >= 2:
                coords.append((float(node[0]), float(node[1])))
            else:
                for child in node:
                    walk(child)

    if payload.get("type") == "FeatureCollection":
        for feat in payload.get("features", []):
            walk(feat.get("geometry", {}).get("coordinates"))
    elif payload.get("type") == "Feature":
        walk(payload.get("geometry", {}).get("coordinates"))
    else:
        walk(payload.get("coordinates"))

    if not coords:
        raise ValueError(f"no coordinates in {path}")
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    return payload, (min(xs), min(ys), max(xs), max(ys))


def slug_from_filename(path: Path) -> str:
    """`alt_pedrogao_grande` → `pedrogao_grande`; `pilot` → `pilot`."""
    stem = path.stem
    if stem.startswith("alt_"):
        return stem[len("alt_") :]
    return stem


def discover_aois(aoi_dir: Path) -> list[Path]:
    """Return pilot.geojson plus every alt_*.geojson, in alphabetical order."""
    out: list[Path] = []
    if (aoi_dir / "pilot.geojson").exists():
        out.append(aoi_dir / "pilot.geojson")
    out.extend(sorted(aoi_dir.glob("alt_*.geojson")))
    return out


# ---------------------------------------------------------------------------
# Overpass: features inside the AOI bbox
# ---------------------------------------------------------------------------


def query_overpass(bbox: tuple[float, float, float, float], timeout: int = 60) -> dict[str, Any]:
    """Return raw Overpass JSON for power=tower, building, highway in the bbox.

    Tries the primary endpoint, falls back to community mirrors on 5xx / timeout.
    """
    xmin, ymin, xmax, ymax = bbox
    bbox_str = f"{ymin},{xmin},{ymax},{xmax}"
    body = (
        f"[out:json][timeout:{timeout}];"
        f"("
        f'  node["power"="tower"]({bbox_str});'
        f'  way["building"]({bbox_str});'
        f'  way["highway"~"{HIGHWAY_RE}"]({bbox_str});'
        f");"
        f"out geom;"
    )
    last_err: Exception | None = None
    for url in OVERPASS_ENDPOINTS:
        try:
            resp = requests.post(
                url,
                data={"data": body},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout + 10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"  [overpass] {url} failed: {exc}; trying next endpoint", file=sys.stderr)
            last_err = exc
            time.sleep(2)
    raise RuntimeError(f"all Overpass endpoints failed: {last_err}")


def overpass_features(payload: dict[str, Any]) -> tuple[list[Point], list[Point], list[Point]]:
    """Split the Overpass response into three lists of representative Points (one per feature)."""
    towers: list[Point] = []
    buildings: list[Point] = []
    highways: list[Point] = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {}) or {}
        if el.get("type") == "node" and tags.get("power") == "tower":
            towers.append(Point(el["lon"], el["lat"]))
        elif el.get("type") == "way":
            geom = el.get("geometry") or []
            if not geom:
                continue
            cx = sum(p["lon"] for p in geom) / len(geom)
            cy = sum(p["lat"] for p in geom) / len(geom)
            pt = Point(cx, cy)
            if "building" in tags:
                buildings.append(pt)
            elif "highway" in tags:
                highways.append(pt)
    return towers, buildings, highways


# ---------------------------------------------------------------------------
# ICNF: burn polygons intersecting the AOI bbox
# ---------------------------------------------------------------------------


def query_icnf_burns(bbox: tuple[float, float, float, float]) -> list[Polygon]:
    """Return burn polygons (in WGS84) from sampled recent layers intersecting the bbox."""
    xmin, ymin, xmax, ymax = bbox
    polys: list[Polygon] = []
    for layer_id, year in ICNF_RECENT_LAYERS.items():
        url = f"{ICNF_AREAS_ARDIDAS_MAPSERVER}/{layer_id}/query"
        try:
            resp = requests.get(
                url,
                params={
                    "where": "1=1",
                    "geometry": f"{xmin},{ymin},{xmax},{ymax}",
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326",
                    "outSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "returnGeometry": "true",
                    "outFields": "OBJECTID",
                    "f": "geojson",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            print(f"  [warn] ICNF layer {year} failed: {exc}", file=sys.stderr)
            continue
        for feat in payload.get("features", []):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception:
                continue
            if g.is_empty:
                continue
            if isinstance(g, Polygon):
                polys.append(g)
            elif isinstance(g, MultiPolygon):
                polys.extend(list(g.geoms))
    return polys


# ---------------------------------------------------------------------------
# Tile scoring
# ---------------------------------------------------------------------------


def _count_in_cell(tree: STRtree | None, points: list[Point], cell: Polygon) -> int:
    if not points or tree is None:
        return 0
    idxs = tree.query(cell)
    return sum(1 for i in idxs if cell.contains(points[i]))


def grid_cells(bbox: tuple[float, float, float, float]) -> list[Polygon]:
    """Return non-overlapping ~1 km cells fully inside the bbox (border-clipped)."""
    xmin, ymin, xmax, ymax = bbox
    cells: list[Polygon] = []
    y = ymin
    while y + CELL_DEG_LAT <= ymax + 1e-9:
        x = xmin
        while x + CELL_DEG_LON <= xmax + 1e-9:
            cells.append(box(x, y, x + CELL_DEG_LON, y + CELL_DEG_LAT))
            x += CELL_DEG_LON
        y += CELL_DEG_LAT
    return cells


def score_cells(
    cells: list[Polygon],
    towers: list[Point],
    buildings: list[Point],
    highways: list[Point],
    burns: list[Polygon],
) -> list[dict[str, Any]]:
    tower_tree = STRtree(towers) if towers else None
    bldg_tree = STRtree(buildings) if buildings else None
    hwy_tree = STRtree(highways) if highways else None
    burn_union = unary_union(burns) if burns else None

    results: list[dict[str, Any]] = []
    for i, cell in enumerate(cells):
        n_t = _count_in_cell(tower_tree, towers, cell)
        n_b = _count_in_cell(bldg_tree, buildings, cell)
        n_h = _count_in_cell(hwy_tree, highways, cell)
        intersects_burn = bool(burn_union and burn_union.intersects(cell))
        qualifies = n_t >= 1 and n_b >= 1 and n_h >= 1 and intersects_burn
        results.append(
            {
                "idx": i,
                "cell": cell,
                "towers": n_t,
                "buildings": n_b,
                "highways": n_h,
                "intersects_burn": intersects_burn,
                "qualifies": qualifies,
                "score": n_t + n_b + n_h,
            }
        )
    return results


def pick_best(scored: list[dict[str, Any]]) -> dict[str, Any] | None:
    qualifying = [s for s in scored if s["qualifies"]]
    if qualifying:
        return max(qualifying, key=lambda s: s["score"])
    # Fallback: prefer cells that intersect a burn AND have something
    burn_cells = [s for s in scored if s["intersects_burn"] and s["score"] > 0]
    if burn_cells:
        return max(burn_cells, key=lambda s: s["score"])
    # Last resort: highest score, if any
    nonzero = [s for s in scored if s["score"] > 0]
    if nonzero:
        return max(nonzero, key=lambda s: s["score"])
    return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def smoke_geojson_for_cell(
    cell: Polygon,
    parent: dict[str, Any],
    parent_path: Path,
    score: dict[str, Any],
) -> dict[str, Any]:
    parent_props = (parent.get("features") or [{}])[0].get("properties", {}) or {}
    parent_name = parent_props.get("name", parent_path.stem)
    iso = parent_props.get("iso3166_2", "")
    bbox = cell.bounds
    return {
        "type": "FeatureCollection",
        "name": f"smoke tile picked for {parent_path.stem}",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": f"smoke tile inside {parent_name}",
                    "iso3166_2": iso,
                    "description": (
                        f"~1 km × 1 km tile picked by scripts/pick_smoke_tile.py against "
                        f"{parent_path.name}. Qualifies = "
                        f"power_tower≥1 AND building≥1 AND highway≥1 AND intersects ICNF burn."
                    ),
                    "bbox_wgs84": [bbox[0], bbox[1], bbox[2], bbox[3]],
                    "picker_score": {
                        "towers": score["towers"],
                        "buildings": score["buildings"],
                        "highways": score["highways"],
                        "intersects_burn": score["intersects_burn"],
                        "qualifies": score["qualifies"],
                        "feature_total": score["score"],
                    },
                },
                "geometry": mapping(cell),
            }
        ],
    }


def write_geojson(path: Path, data: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] would write {path}")
        return
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"  [write] {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def process_aoi(
    aoi_path: Path,
    sleep_after: float,
    dry_run: bool,
    output_slug: str | None = None,
) -> tuple[Path, dict[str, Any] | None]:
    print(f"\n=== {aoi_path.name} ===")
    parent, bbox = load_aoi(aoi_path)
    print(f"  bbox: {bbox}")

    print("  [overpass] querying OSM features ...")
    op = query_overpass(bbox)
    towers, buildings, highways = overpass_features(op)
    print(
        f"  [overpass] towers={len(towers)}  buildings={len(buildings)}  highways={len(highways)}"
    )

    print("  [icnf] querying burn polygons ...")
    burns = query_icnf_burns(bbox)
    print(f"  [icnf] burn polygons in bbox: {len(burns)}")

    cells = grid_cells(bbox)
    print(f"  [grid] {len(cells)} candidate ~1 km cells")
    scored = score_cells(cells, towers, buildings, highways, burns)
    n_qualifying = sum(1 for s in scored if s["qualifies"])
    print(f"  [score] cells qualifying (all-3-OSM AND burn): {n_qualifying}")

    pick = pick_best(scored)
    if pick is None:
        print("  [pick] no cell scored above zero — skipping")
        return aoi_path, None

    print(
        f"  [pick] best cell idx={pick['idx']}  "
        f"towers={pick['towers']} bldgs={pick['buildings']} hwys={pick['highways']} "
        f"burn={pick['intersects_burn']} qualifies={pick['qualifies']}"
    )

    slug = output_slug or slug_from_filename(aoi_path)
    out_path = AOI_DIR / f"smoke_{slug}.geojson"
    payload = smoke_geojson_for_cell(pick["cell"], parent, aoi_path, pick)
    write_geojson(out_path, payload, dry_run)

    if sleep_after > 0:
        time.sleep(sleep_after)
    return aoi_path, pick


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Compute picks, but don't write any files."
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=4.0,
        help="Seconds to sleep between AOIs (Overpass courtesy). Default: 4.",
    )
    args = parser.parse_args()

    aois = discover_aois(AOI_DIR)
    if not aois:
        print("no AOIs found in data/aoi/", file=sys.stderr)
        return 1
    print(f"Found {len(aois)} AOI(s) to process: {[p.name for p in aois]}")

    # Dedup by bbox: pilot.geojson typically equals one alt. Process pilot first;
    # if an alt matches the pilot, skip the alt but transfer its slug to the pilot
    # so the pilot's output file is named after the alt (matching the existing
    # smoke_<slug>.geojson convention in data/aoi/).
    seen: dict[tuple[float, float, float, float], Path] = {}
    pilot_inherited_slug: str | None = None
    work: list[Path] = []
    for path in aois:
        try:
            _, bbox = load_aoi(path)
        except Exception as exc:
            print(f"  [skip] {path.name}: {exc}", file=sys.stderr)
            continue
        if bbox in seen:
            prior = seen[bbox]
            print(f"  [dedup] {path.name} matches {prior.name}; skipping")
            if prior.name == "pilot.geojson" and pilot_inherited_slug is None:
                pilot_inherited_slug = slug_from_filename(path)
                print(f"           pilot will inherit slug '{pilot_inherited_slug}' for output")
            continue
        seen[bbox] = path
        work.append(path)

    pilot_pick: dict[str, Any] | None = None
    pilot_parent: dict[str, Any] | None = None
    summary: list[tuple[str, dict[str, Any] | None]] = []
    for path in work:
        out_slug = pilot_inherited_slug if path.name == "pilot.geojson" else None
        _, pick = process_aoi(
            path, sleep_after=args.sleep, dry_run=args.dry_run, output_slug=out_slug
        )
        summary.append((path.name, pick))
        if path.name == "pilot.geojson":
            pilot_pick = pick
            pilot_parent, _ = load_aoi(path)

    # Mirror the pilot's pick to data/aoi/smoke.geojson for the canonical smoke tile.
    if pilot_pick and pilot_parent:
        payload = smoke_geojson_for_cell(pilot_pick["cell"], pilot_parent, PILOT_FILE, pilot_pick)
        write_geojson(SMOKE_FILE, payload, args.dry_run)

    print("\n=== summary ===")
    for name, pick in summary:
        if pick is None:
            print(f"  {name:<40} (no pick)")
        else:
            print(
                f"  {name:<40} towers={pick['towers']:<3} bldgs={pick['buildings']:<4} "
                f"hwys={pick['highways']:<3} burn={pick['intersects_burn']!s:<5} "
                f"qualifies={pick['qualifies']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
