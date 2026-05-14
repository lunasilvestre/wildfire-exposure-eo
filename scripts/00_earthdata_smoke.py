"""NASA Earthdata Login + LP DAAC STAC access smoke test.

Verifies, in order:

    1. `~/.netrc` credentials are accepted by URS (earthaccess.login).
    2. The LP DAAC `LPCLOUD` STAC catalog is reachable via pystac-client.
    3. The two HLS collections we plan to use resolve:
           - HLSL30_2.0  (Harmonized Landsat,  30 m)
           - HLSS30_2.0  (Harmonized Sentinel-2, 30 m)
    4. A token-gated asset fetch round-trips: search one recent HLS item over
       the pilot AOI bbox, request the first byte of one COG asset using the
       Earthdata-authenticated session.

Exits 0 on full success, 1 otherwise. Designed for CI smoke and the
PRE_DEV_CHECKLIST item B.21.

Run:
    uv run python scripts/00_earthdata_smoke.py
"""

from __future__ import annotations

import sys

import earthaccess
from pystac_client import Client

LPCLOUD_STAC_URL = "https://cmr.earthdata.nasa.gov/stac/LPCLOUD"
REQUIRED_COLLECTIONS = ("HLSL30_2.0", "HLSS30_2.0")

# Portugal-wide bbox; we just need any HLS item to resolve, the AOI is not
# frozen yet at the point this script runs (CLAUDE.md non-negotiable #10
# applies to source code, not pre-dev smoke probes).
PROBE_BBOX = (-9.6, 36.9, -6.1, 42.2)
PROBE_DATETIME = "2025-06-01/2025-09-30"


def main() -> int:
    # 1. URS auth via ~/.netrc
    try:
        auth = earthaccess.login(strategy="netrc")
    except Exception as exc:
        print(f"FAIL  earthaccess.login(netrc): {exc}", file=sys.stderr)
        return 1
    if not auth.authenticated:
        print("FAIL  earthaccess reports not authenticated", file=sys.stderr)
        return 1
    print("OK    URS authentication via ~/.netrc")

    # 2. LP DAAC STAC root
    try:
        client = Client.open(LPCLOUD_STAC_URL)
    except Exception as exc:
        print(f"FAIL  could not open {LPCLOUD_STAC_URL}: {exc}", file=sys.stderr)
        return 1
    print(f"OK    opened {LPCLOUD_STAC_URL}")

    # 3. Required collections
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

    # 4. Token-gated asset fetch — Range request for the first 1 KB of one COG
    search = client.search(
        collections=["HLSL30_2.0"],
        bbox=PROBE_BBOX,
        datetime=PROBE_DATETIME,
        max_items=1,
    )
    items = list(search.items())
    if not items:
        print("FAIL  no HLSL30 items returned for probe bbox/window", file=sys.stderr)
        return 1
    item = items[0]
    cog_assets = [a for a in item.assets.values() if (a.href or "").endswith(".tif")]
    if not cog_assets:
        print(f"FAIL  item {item.id} has no .tif assets", file=sys.stderr)
        return 1
    asset_href = cog_assets[0].href

    session = earthaccess.get_requests_https_session()
    try:
        resp = session.get(
            asset_href,
            headers={"Range": "bytes=0-1023"},
            allow_redirects=True,
            timeout=30,
        )
    except Exception as exc:
        print(f"FAIL  token-gated fetch raised: {exc}", file=sys.stderr)
        return 1
    if resp.status_code not in (200, 206):
        print(
            f"FAIL  token-gated fetch returned HTTP {resp.status_code} for {asset_href}",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK    token-gated fetch  HTTP {resp.status_code}  {len(resp.content)} bytes  ({item.id})"
    )

    print(
        f"\nAll checks passed. {len(REQUIRED_COLLECTIONS)} collection(s) reachable, "
        f"Earthdata token works."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
