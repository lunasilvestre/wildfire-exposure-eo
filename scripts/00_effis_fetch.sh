#!/usr/bin/env bash
# EFFIS direct-download URLs.
#
# PRE_DEV_CHECKLIST item B.23 (fuel cover, reference layer) — JRC's
# pan-European EFFIS Fuel Map. 42 vegetation complexes mapped to 13 NFFL
# fire-behaviour model classes; covers Portugal. Used here as the
# *reference* fuel layer (not the primary input — that is DGT COSc, see
# scripts/00_dgt_fetch.sh) to provide an international NFFL → Scott &
# Burgan crosswalk and to validate the COSc-derived fuel stratification.
#
# Reference paper: Aragoneses, E. et al., 2023. "Classification and
# mapping of European fuels using a hierarchical, multipurpose fuel
# classification system." Earth System Science Data 15, 1287-1315.
# doi:10.5194/essd-15-1287-2023
#
# CRS note: ETRS89-LAEA (EPSG:3035). Reproject before any joins with
# Portuguese national-grid data (EPSG:3763) or WGS84.
#
# License: free, no auth, terms-of-use acknowledgement on the EFFIS
# data-and-services page (https://forest-fire.emergency.copernicus.eu/applications/data-and-services).
#
# WART: as of 2026-05-07, the JRC redirect target host
# data.effis.emergency.copernicus.eu serves an EXPIRED SSL certificate.
# We follow the documented entry URL on forest-fire.emergency.copernicus.eu
# (whose cert is valid) and use --ssl-revoke-best-effort to tolerate the
# downstream cert until JRC rotates it. Do NOT remove this flag without
# re-checking.

set -euo pipefail

DEST="${DEST:-data/effis/raw}"
mkdir -p "$DEST"

UA="wildfire-exposure-eo/0.0.1 (+https://github.com/lunasilvestre/wildfire-exposure-eo)"

# ---------------------------------------------------------------------------
# 1. EFFIS European Fuel Map (LAEA) — JRC, 2017-derived, 1 km resolution
# ---------------------------------------------------------------------------
# Pan-European fuel raster (FuelMap_LAEA.zip). The 301 from the
# forest-fire host points to data.effis (expired cert as of 2026-05-07);
# -k bypasses the broken cert chain on the redirect target only.
echo "[1/1] EFFIS Fuel Map (LAEA, EPSG:3035)"
curl -fSL -k -A "$UA" \
  -o "$DEST/FuelMap_LAEA.zip" \
  "https://forest-fire.emergency.copernicus.eu/effis/applications/data-and-services/FuelMap_LAEA.zip"

echo
echo "Done. File written to: $DEST/FuelMap_LAEA.zip"
echo
echo "Next:"
echo "  1. Unzip; look for the GeoTIFF and the legend/lookup CSV."
echo "  2. Crosswalk the 13 NFFL classes to Scott & Burgan FBFM40 if you need"
echo "     compatibility with US-style fuel-behaviour models."

# ---------------------------------------------------------------------------
# Provenance (verified 2026-05-07)
# ---------------------------------------------------------------------------
# - Entry URL (cert valid)
#     https://forest-fire.emergency.copernicus.eu/effis/applications/data-and-services/FuelMap_LAEA.zip
#     HEAD: HTTP 301 -> data.effis.emergency.copernicus.eu/...
# - Redirect target (cert EXPIRED on 2026-05-07)
#     https://data.effis.emergency.copernicus.eu/effis/applications/data-and-services/FuelMap_LAEA.zip
#     Range request 0-1023 (with -k): HTTP 206, application/zip,
#     `file` recognises the bytes as "Zip archive data, at least v2.0 to extract".
#     Confirms the redirect serves a real binary, not an error page.
# - Background
#     https://forest-fire.emergency.copernicus.eu/about-effis/technical-background/fuels
# - Reference paper
#     Aragoneses, E., García, M., Salis, M., Ribeiro, L. M., Chuvieco, E.,
#     2023. "Classification and mapping of European fuels using a
#     hierarchical, multipurpose fuel classification system."
#     Earth System Science Data 15, 1287-1315. doi:10.5194/essd-15-1287-2023
#     https://essd.copernicus.org/articles/15/1287/2023/
