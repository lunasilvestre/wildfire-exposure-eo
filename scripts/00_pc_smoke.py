"""Microsoft Planetary Computer access smoke test.

Verifies that `pystac-client` can reach the PC STAC root and resolve the four
collections required by the wildfire-exposure-eo pilot:

    - sentinel-2-l2a
    - sentinel-1-grd
    - cop-dem-glo-30
    - esa-worldcover

Exits 0 on full success, 1 otherwise. Designed for CI smoke and the
PRE_DEV_CHECKLIST item B.20.

Run:
    uv run python scripts/00_pc_smoke.py
"""

from __future__ import annotations

import sys

from pystac_client import Client

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
REQUIRED_COLLECTIONS = (
    "sentinel-2-l2a",
    "sentinel-1-grd",
    "cop-dem-glo-30",
    "esa-worldcover",
)


def main() -> int:
    try:
        client = Client.open(PC_STAC_URL)
    except Exception as exc:
        print(f"FAIL  could not open {PC_STAC_URL}: {exc}", file=sys.stderr)
        return 1

    print(f"OK    opened {PC_STAC_URL}")

    failures: list[str] = []
    for cid in REQUIRED_COLLECTIONS:
        try:
            collection = client.get_collection(cid)
        except Exception as exc:
            failures.append(cid)
            print(f"FAIL  {cid}: {exc}", file=sys.stderr)
            continue
        title = collection.title or "(no title)"
        print(f"OK    {cid}  ->  {title}")

    if failures:
        print(f"\n{len(failures)} collection(s) failed: {failures}", file=sys.stderr)
        return 1

    print(f"\nAll {len(REQUIRED_COLLECTIONS)} collections reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
