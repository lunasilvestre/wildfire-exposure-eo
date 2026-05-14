#!/usr/bin/env bash
# ICNF direct-download URLs.
#
# PRE_DEV_CHECKLIST item B.23 — confirm and record one-liner fetch commands
# for the ICNF datasets we depend on. Run with `bash scripts/00_icnf_fetch.sh`
# (no args) to download everything into ./data/icnf/raw/. Each block is a
# self-contained curl one-liner and can be copy-pasted independently.
#
# All URLs were HEAD-verified on 2026-05-07 (HTTP 200). See provenance notes
# at the bottom of this file.
#
# CRS note: ICNF data is published in EPSG:3763 (ETRS89 / Portugal TM06).
# Reproject downstream, do not assume WGS84.

set -euo pipefail

DEST="${DEST:-data/icnf/raw}"
mkdir -p "$DEST"

UA="wildfire-exposure-eo/0.0.1 (+https://github.com/lunasilvestre/wildfire-exposure-eo)"

# ---------------------------------------------------------------------------
# 1. Áreas Ardidas (Burned Areas), 1975-2025
# ---------------------------------------------------------------------------
# Source: ICNF ArcGIS REST MapServer at sigservices.icnf.pt.
# We hit the per-layer /query endpoint with f=geojson to get a clean GeoJSON
# dump. Layer IDs are NOT contiguous and group some pre-2009 years; the
# mapping below was confirmed against the live MapServer/layers response.
#
# Service root:
#   https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer
#
# Per-layer download (one-liner, repeats for each layer id):
#   curl -sS -A "$UA" -G \
#     --data-urlencode "where=1=1" \
#     --data-urlencode "outFields=*" \
#     --data-urlencode "f=geojson" \
#     "https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer/{LAYER_ID}/query" \
#     -o "$DEST/areas_ardidas_{NAME}.geojson"

declare -A AREAS_ARDIDAS_LAYERS=(
  [14]="1975_1989"
  [13]="1990_1999"
  [12]="2000_2008"
  [11]="2009"
  [10]="2010"
  [9]="2011"
  [8]="2012"
  [7]="2013"
  [6]="2014"
  [5]="2015"
  [4]="2016"
  [3]="2017"
  [2]="2018"
  [1]="2019"
  [0]="2020"
  [15]="2021"
  [17]="2022"
  [18]="2023"
  [19]="2024"
  [20]="2025"
)

fetch_areas_ardidas() {
  local layer_id="$1"
  local layer_name="$2"
  local out="$DEST/areas_ardidas_${layer_name}.geojson"
  echo "  -> layer ${layer_id} (${layer_name})  ->  ${out}"
  curl -sS -A "$UA" -G \
    --data-urlencode "where=1=1" \
    --data-urlencode "outFields=*" \
    --data-urlencode "f=geojson" \
    "https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer/${layer_id}/query" \
    -o "$out"
}

echo "[1/2] Áreas Ardidas (ArcGIS REST -> GeoJSON, EPSG:3763)"
for layer_id in "${!AREAS_ARDIDAS_LAYERS[@]}"; do
  fetch_areas_ardidas "$layer_id" "${AREAS_ARDIDAS_LAYERS[$layer_id]}"
done

# ---------------------------------------------------------------------------
# 2. Carta de Combustíveis Florestais (Forest Fuel Map)
# ---------------------------------------------------------------------------
# As of 2026-05-07, ICNF does NOT publish a direct-download URL for the
# national "Carta de Combustíveis Florestais" (the fuel-class raster/
# vector). The fuel map originating from UTAD (Fernandes et al.) is
# referenced in the literature but is not surfaced as a public asset on
# geocatalogo.icnf.pt, sigservices.icnf.pt, or fogos.icnf.pt. Confirmed by
# walking the DFCI service folder and the geoCATALOGO catalog page.
#
# DECISION (2026-05-07): the pilot uses DGT COSc as the PRIMARY fuel-cover
# input (10 m, annual, CC-BY 4.0, 4-class fuel stratum) and EFFIS as the
# REFERENCE fuel layer (NFFL crosswalk). ICNF CCF stays as future-work
# alignment. See:
#   - scripts/00_dgt_fetch.sh   (DGT COSc2023 + COSc2024 Pré-Verão)
#   - scripts/00_effis_fetch.sh (EFFIS European Fuel Map, LAEA)
#
# The two ICNF-side substitutes below are kept because they are useful in
# their own right — RPFGC as a feature layer, Perigosidade as a validation
# reference for the exposure ranking. Neither is a fuel-class map.
#
# 2a. Faixas de Gestão de Combustível (RPFGC, national, 2018-02-23)
#     The fuel-management-strip overlay — vector polygons of where fuel
#     management is mandated. NOT a fuel-class map; useful as a feature.
echo "[2a/2] RPFGC (fuel management strips, national, 2018)"
curl -sS -A "$UA" -L \
  -o "$DEST/RPFGCnac_23022018_ETRS89PT_NUTS3.zip" \
  "https://fogos.icnf.pt/download/FaixasGestaoCombustivel/RPFGCnac_23022018_ETRS89PT_NUTS3.zip"

# 2b. Perigosidade Estrutural 2020-2030
#     Structural fire-hazard raster — downstream of fuel data, useful as a
#     reference layer to validate our exposure ranking against.
echo "[2b/2] Perigosidade Estrutural 2020-2030 (raster zip)"
curl -sS -A "$UA" -L \
  -o "$DEST/perigosidade_estrutural_2020_2030.zip" \
  "https://geocatalogo.icnf.pt/dados/perigosidade_estrutural_2020_2030.zip"

echo
echo "Done. Files written to: $DEST"
echo
echo "Carta de Combustíveis Florestais — no public direct-download URL from ICNF."
echo "  Pilot decision (2026-05-07): use DGT COSc as primary fuel-cover input."
echo "    -> scripts/00_dgt_fetch.sh   (DGT COSc2023 + COSc2024 Pré-Verão)"
echo "    -> scripts/00_effis_fetch.sh (EFFIS European Fuel Map, NFFL reference)"
echo "  ICNF CCF remains a future-work item (account on geocatalogo.icnf.pt or"
echo "  email to geral@icnf.pt) — not blocking the pilot."

# ---------------------------------------------------------------------------
# Provenance (verified 2026-05-07)
# ---------------------------------------------------------------------------
# - ArcGIS REST root
#     https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer
#     HEAD: HTTP 200, application/json. 20 layers covering 1975-2025.
# - RPFGC zip
#     https://fogos.icnf.pt/download/FaixasGestaoCombustivel/RPFGCnac_23022018_ETRS89PT_NUTS3.zip
#     HEAD: HTTP 200, application/x-zip-compressed, 22.3 MB.
# - Perigosidade Estrutural zip
#     https://geocatalogo.icnf.pt/dados/perigosidade_estrutural_2020_2030.zip
#     HEAD: HTTP 200, application/x-zip-compressed.
# - Carta de Combustíveis Florestais
#     No public URL discovered as of 2026-05-07. Searched: geocatalogo.icnf.pt,
#     sigservices.icnf.pt (BDG and DFCI folders), fogos.icnf.pt/download,
#     www.icnf.pt/florestas/gfr/...dfciinformacaocartografica,
#     www.isa.ulisboa.pt/cef/public/portalGeog. None expose it as a download.
#     Pilot uses DGT COSc + EFFIS instead — see scripts/00_dgt_fetch.sh and
#     scripts/00_effis_fetch.sh. ICNF CCF tracked as future-work alignment.
