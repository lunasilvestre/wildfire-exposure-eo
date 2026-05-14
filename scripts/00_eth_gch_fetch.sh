#!/usr/bin/env bash
# ETH Global Canopy Height 10 m 2020 v1 — direct-download fetch.
#
# PRE_DEV_CHECKLIST item C — confirms a HEAD-verified canonical URL pattern for
# the Lang et al. 2023 GCH product. Run with `bash scripts/00_eth_gch_fetch.sh`
# (no args) to download the tile(s) intersecting the pilot AOI into
# ./data/eth_gch/raw/.
#
# Source: official tile browser at https://langnico.github.io/globalcanopyheight/
# DOI:    10.3929/ethz-b-000609802         License: CC BY 4.0
# Verified 2026-05-07 — Range-GET of bytes 0..15 returns TIFF magic 49 49 2A 00.
#
# Tile naming: 3-degree COG blocks identified by SW corner.
#   ETH_GlobalCanopyHeight_10m_2020_<NS><LAT_2D><EW><LON_3D>_Map.tif
# For the Pampilhosa da Serra / Pedrógão Grande pilot AOI (-8.30 to -7.70 lon,
# 39.80 to 40.30 lat) the SW corner falls in the N39W009 block.

set -euo pipefail

DEST="${DEST:-data/eth_gch/raw}"
mkdir -p "$DEST"

UA="wildfire-exposure-eo/0.0.1 (+https://github.com/lunasilvestre/wildfire-exposure-eo)"
BASE="https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download?path=%2F3deg_cogs"

# Tiles required for the pilot AOI. Extend this list when the AOI changes.
TILES=(
  "N39W009"
)

for tile in "${TILES[@]}"; do
  fname="ETH_GlobalCanopyHeight_10m_2020_${tile}_Map.tif"
  out="${DEST}/${fname}"
  url="${BASE}&files=${fname}"
  echo "[fetch] ${tile} -> ${out}"
  curl -A "$UA" -fSL --retry 3 --retry-delay 2 -o "$out" "$url"
done

echo "Done. Files in ${DEST}/"
