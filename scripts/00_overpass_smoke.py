"""OSM Overpass API access smoke test.

Probes the primary Overpass endpoint plus at least one fallback with a tiny
bounded query (count of `power=tower` nodes in a 0.05 deg box near Pampilhosa
da Serra). The query is intentionally cheap so we can hit every endpoint
without abusing volunteer-run mirrors.

Endpoints (per https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances):

    PRIMARY:   https://overpass-api.de/api/interpreter           (DE, main instance)
    FALLBACK:  https://overpass.kumi.systems/api/interpreter     (community mirror)
    FALLBACK:  https://overpass.private.coffee/api/interpreter   (community mirror)

Exits 0 if at least the primary is reachable and returns valid JSON.
Records per-endpoint status to stdout for the PRE_DEV_CHECKLIST item B.22.

Run:
    uv run python scripts/00_overpass_smoke.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ENDPOINTS = (
    ("PRIMARY ", "https://overpass-api.de/api/interpreter"),
    ("FALLBACK", "https://overpass.kumi.systems/api/interpreter"),
    ("FALLBACK", "https://overpass.private.coffee/api/interpreter"),
)

# Default bbox: ~50 km box covering Pampilhosa da Serra / Pedrógão Grande.
# Wide enough to guarantee a non-zero count (proves we're actually getting OSM
# data, not just a valid empty envelope), still cheap enough not to abuse
# volunteer mirrors. Override with `--aoi <path>` for the project's smoke tiles.
DEFAULT_BBOX = (39.80, -8.30, 40.30, -7.70)  # (south, west, north, east)


def query_for_bbox(bbox: tuple[float, float, float, float]) -> str:
    """Return an Overpass `out count` query for power=tower in (south,west,north,east)."""
    s, w, n, e = bbox
    return f"[out:json][timeout:25];" f'(node["power"="tower"]({s},{w},{n},{e}););' f"out count;"


def bbox_from_aoi(path: Path) -> tuple[float, float, float, float]:
    """Read a GeoJSON file and return (south, west, north, east) of its outer extent."""
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
    return (min(ys), min(xs), max(ys), max(xs))


USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 smoke-test (+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)


def probe(url: str, query: str) -> tuple[bool, str]:
    try:
        t0 = time.perf_counter()
        resp = requests.post(
            url,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        dt_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as exc:
        return False, f"request raised: {exc}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    try:
        payload = resp.json()
    except ValueError as exc:
        return False, f"non-JSON response: {exc}"

    elements = payload.get("elements", [])
    if not elements:
        return False, "JSON had no 'elements' field"

    # `out count` returns a single element with tags.total (string, per spec)
    total_str = elements[0].get("tags", {}).get("total", "0")
    try:
        total = int(total_str)
    except ValueError:
        return False, f"non-integer count: {total_str!r}"
    if total <= 0:
        return False, f"count was {total} — bbox should contain >0 towers"
    return True, f"HTTP 200  {dt_ms:.0f} ms  power=tower count={total}"


def run_for_bbox(bbox: tuple[float, float, float, float], label_prefix: str = "") -> int:
    """Probe every endpoint for one bbox; return same exit semantics as main()."""
    query = query_for_bbox(bbox)
    primary_ok = False
    fallback_ok = False
    if label_prefix:
        print(f"--- {label_prefix} bbox(s,w,n,e)={bbox} ---")
    for label, url in ENDPOINTS:
        ok, info = probe(url, query)
        prefix = "OK   " if ok else "FAIL "
        print(f"{prefix} {label}  {url}  ->  {info}")
        if ok:
            if label.strip() == "PRIMARY":
                primary_ok = True
            else:
                fallback_ok = True
    print()
    if primary_ok and fallback_ok:
        print("Primary + at least one fallback reachable.")
        return 0
    if primary_ok:
        print("Primary reachable; no fallback responded — note this in docs/data_sources.md.")
        return 0
    if fallback_ok:
        print(
            "WARNING: primary Overpass unreachable, only fallback(s) responded. "
            "Proceed but flag as YELLOW in audit.",
            file=sys.stderr,
        )
        return 0
    print("FAIL: no Overpass endpoint responded.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Overpass reachability + power=tower count smoke test."
    )
    parser.add_argument(
        "--aoi",
        type=Path,
        action="append",
        help=(
            "Path to an AOI GeoJSON. Probes Overpass with that file's bbox. "
            "Repeat the flag to probe multiple AOIs. If omitted, the legacy "
            "Pampilhosa default bbox is used."
        ),
    )
    args = parser.parse_args()

    if not args.aoi:
        return run_for_bbox(DEFAULT_BBOX)

    rcs: list[int] = []
    for path in args.aoi:
        try:
            bbox = bbox_from_aoi(path)
        except Exception as exc:
            print(f"FAIL  {path}: {exc}", file=sys.stderr)
            rcs.append(1)
            continue
        rcs.append(run_for_bbox(bbox, label_prefix=path.name))
        time.sleep(2)  # courtesy gap between AOIs
    return 1 if any(rc != 0 for rc in rcs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
