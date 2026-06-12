"""Measure the smoke-AOI demo path end-to-end (WU-7, prompt 11 deliverable 4).

Runs the seven demo steps sequentially via subprocess, measuring wall-clock
per step, and emits the markdown timing table for ``docs/demo.md`` so every
number in that doc is script output (CLAUDE.md fact-checking checklist).

Steps: audit -> fetch-osm -> fetch-rasters -> fetch-burns -> fuel-layer ->
score -> validate. CPU-only; uses the pre-baked WU-1 burn-scar COG already
in ``outputs/cogs/``. ``--window-end 2026-06-09`` pins the score step to the
smoke burn-scar COG's window end so all six features activate and the run
is date-independent.

Audit exit codes are GREEN/RED/YELLOW = 0/1/2; YELLOW is accepted for the
audit step (the 1 km² smoke tile is too sparse for the OSM class-count
probe). Any other non-zero exit aborts.

Network: Overpass, ICNF ArcGIS REST, MS Planetary Computer. The
fetch-rasters step is a cache check when ``data/cache/`` is warm; on a
fresh clone it downloads ~600 MB (see docs/demo.md).
"""

import subprocess
import sys
import time
from datetime import UTC, datetime

STEPS: list[tuple[str, list[str]]] = [
    (
        "audit",
        ["uv", "run", "wildfire-exposure-eo", "audit", "--aoi", "data/aoi/smoke.geojson"],
    ),
    ("fetch-osm", ["uv", "run", "wildfire-exposure-eo", "fetch-osm", "--smoke"]),
    (
        # Only the layers the score path actually consumes (ETH-GCH, EFFIS, COSc).
        # The species-level DGT COS GeoPackage ("cos") is future work — unused by
        # fuel.py — and its DGT download URL is currently 404, so the demo skips it.
        "fetch-rasters",
        [
            "uv",
            "run",
            "wildfire-exposure-eo",
            "fetch-rasters",
            "--smoke",
            "--only",
            "eth-gch,effis,cosc",
        ],
    ),
    ("fetch-burns", ["uv", "run", "wildfire-exposure-eo", "fetch-burns", "--smoke"]),
    ("fuel-layer", ["uv", "run", "wildfire-exposure-eo", "fuel-layer", "--smoke"]),
    (
        "score",
        [
            "uv",
            "run",
            "wildfire-exposure-eo",
            "score",
            "--smoke",
            "--window-end",
            "2026-06-09",
        ],
    ),
    (
        "validate",
        [
            "uv",
            "run",
            "python",
            "scripts/11_validate.py",
            "--smoke",
            "--out",
            "outputs/logs/wu7-demo-validate.out.md",
        ],
    ),
]


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    print(f"=== WU-7 DEMO TIMING RUN START {utcnow()} ===", flush=True)
    total0 = time.monotonic()
    timings: list[tuple[str, float]] = []
    for name, cmd in STEPS:
        print(f"=== STEP {name} START {utcnow()} ===", flush=True)
        t0 = time.monotonic()
        rc = subprocess.run(cmd).returncode
        elapsed = time.monotonic() - t0
        timings.append((name, elapsed))
        print(f"=== STEP {name} END rc={rc} elapsed={elapsed:.1f}s ===", flush=True)
        if name == "audit" and rc == 2:
            print("=== NOTE: audit YELLOW accepted on the smoke tile ===", flush=True)
            continue
        if rc != 0:
            print(f"=== ABORT: step {name} failed with rc={rc} ===", flush=True)
            return rc
    total = time.monotonic() - total0
    print("=== MARKDOWN TABLE (paste into docs/demo.md) ===", flush=True)
    print("| step | wall-clock |", flush=True)
    print("|---|---:|", flush=True)
    for name, elapsed in timings:
        print(f"| `{name}` | {elapsed:.0f} s |", flush=True)
    print(f"| **total** | **{total / 60:.1f} min** |", flush=True)
    print(f"=== WU-7 DEMO TIMING RUN COMPLETE total={total:.1f}s ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
