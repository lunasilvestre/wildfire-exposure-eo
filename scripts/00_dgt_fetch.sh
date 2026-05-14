#!/usr/bin/env bash
# DGT direct-download URLs.
#
# Covers two PRE_DEV_CHECKLIST items:
#   B.23 (fuel cover)  — COSc (annual 10 m raster, Sentinel-2-derived)
#   B.24 (DGT INSPIRE) — COS  (multi-year vector polygons, INSPIRE-compliant)
#
# Primary source for the pilot's fuel-cover input is DGT's Carta de
# Ocupação do Solo Conjuntural (COSc): a 10 m annual land-cover raster
# derived from Sentinel-2 by DGT/SMOS. COSc explicitly publishes a
# fuel-cover stratum with 4 classes (Dense Forest, Open Forest,
# Shrubland, Spontaneous herbaceous).
#
# COS (Série 2) is the formal multi-year land-use map and is used here as
# the structural reference layer for ICNF burned-area validation
# (COS 2018 covers the historical baseline; COS 2023 the latest state).
#
# Per the rationale recorded in CLAUDE.md non-negotiable #1, every URL
# below was verified on 2026-05-07 (HEAD HTTP 200, content-type and size
# captured). Provenance block at the bottom of this file.
#
# CRS note: COSc rasters AND COS vectors are published in ETRS89 /
# PT-TM06 (EPSG:3763). Reproject downstream, do not assume WGS84.
#
# License: CC-BY 4.0. Cite "Direção-Geral do Território (DGT)".
#
# Run: bash scripts/00_dgt_fetch.sh
# Override destination: DEST=/path/to/dir bash scripts/00_dgt_fetch.sh

set -euo pipefail

DEST="${DEST:-data/dgt/raw}"
mkdir -p "$DEST"

UA="wildfire-exposure-eo/0.0.1 (+https://github.com/lunasilvestre/wildfire-exposure-eo)"

# ---------------------------------------------------------------------------
# 1. COSc2023 — Carta de Ocupação do Solo Conjuntural 2023 (annual, definitive)
# ---------------------------------------------------------------------------
# Latest definitive annual edition. 10 m raster, EPSG:3763. ZIP contains
# the GeoTIFF plus QGIS/ArcGIS style files and the nomenclature legend.
# The fuel-cover layer (4 classes) is derived from this source per the
# DGT/SMOS methodology (Costa et al. 2022, doi:10.3390/rs14081865).
echo "[1/2] COSc2023 (definitive, 10 m raster, EPSG:3763)"
curl -fSL -A "$UA" \
  -o "$DEST/COSc2023.zip" \
  "https://geo2.dgterritorio.gov.pt/cosc/COSc2023.zip"

# ---------------------------------------------------------------------------
# 2. COSc2024 Pré-Verão — early-season 2024 edition
# ---------------------------------------------------------------------------
# "Pré-verão" = pre-summer release, produced before each fire season for
# operational fire-management use. Use this when a within-season snapshot
# matters (validation against ICNF burned-area perimeters from the
# 2024 season). Definitive COSc2024 will replace this when published.
echo "[2/4] COSc2024 Pré-Verão (operational pre-fire-season edition)"
curl -fSL -A "$UA" \
  -o "$DEST/COSc2024_preverao.zip" \
  "https://geo2.dgterritorio.gov.pt/cosc/COSc2024_preverao.zip"

# ---------------------------------------------------------------------------
# 3. COS 2018 v3 — Carta de Uso e Ocupação do Solo, Série 2 (INSPIRE)
# ---------------------------------------------------------------------------
# Vector land-use polygons (GeoPackage), continental Portugal, 83-class
# nomenclature. EPSG:3763. The "Série 2" / v3 republication aligns COS
# with the COSc methodology and is the current canonical edition.
# Used here as the multi-year baseline for ICNF burned-area validation.
echo "[3/4] COS 2018 v3 (Série 2, GPKG, ~900 MB)"
curl -fSL -A "$UA" \
  -o "$DEST/COS2018v3-S2-gpkg.zip" \
  "https://geo2.dgterritorio.gov.pt/cos/S2/COS2018/COS2018v3-S2-gpkg.zip"

# ---------------------------------------------------------------------------
# 4. COS 2023 v1 — Carta de Uso e Ocupação do Solo, Série 2 (latest)
# ---------------------------------------------------------------------------
# Latest definitive multi-year land-use edition. Same Série 2 schema as
# COS 2018 v3, so the two compose into a temporal stack for trend work.
echo "[4/4] COS 2023 v1 (Série 2, GPKG, ~900 MB)"
curl -fSL -A "$UA" \
  -o "$DEST/COS2023v1-S2-gpkg.zip" \
  "https://geo2.dgterritorio.gov.pt/cos/S2/COS2023/COS2023v1-S2-gpkg.zip"

echo
echo "Done. Files written to: $DEST"
echo
echo "Next:"
echo "  1. Unzip COSc archives and locate the GeoTIFF (e.g. COSc2023.tif)."
echo "  2. The 4-class fuel-cover stratum is derived from the COSc level-3"
echo "     nomenclature; see"
echo "     https://www.dgterritorio.gov.pt/sites/default/files/documentos-publicos/Nomenclatura_COSc.pdf"
echo "  3. Unzip COS archives and locate the .gpkg (vector polygons,"
echo "     EPSG:3763). Convert to GeoParquet downstream — GPKG is allowed"
echo "     as input but forbidden as output per CLAUDE.md non-negotiable #5."
echo "  4. Cross-check against EFFIS European Fuel Map (scripts/00_effis_fetch.sh)"
echo "     for international/NFFL crosswalk."

# ---------------------------------------------------------------------------
# Provenance (verified 2026-05-07)
# ---------------------------------------------------------------------------
# - Resource discovery
#     https://dados.gov.pt/api/1/datasets/carta-de-ocupacao-do-solo-conjuntural-2023/
#     Returns 3 resources (zip, WMS, OGC API). The zip resource URL is the
#     canonical bulk download.
# - COSc2023.zip
#     https://geo2.dgterritorio.gov.pt/cosc/COSc2023.zip
#     HEAD: HTTP 200, application/zip, 188,627,143 bytes (~180 MB),
#     Last-Modified: Thu, 25 Jan 2024 09:42:11 GMT.
# - COSc2024_preverao.zip
#     https://geo2.dgterritorio.gov.pt/cosc/COSc2024_preverao.zip
#     HEAD: HTTP 200, application/zip (size and Last-Modified not pinned
#     here; verify on first download).
# - COS2018v3-S2-gpkg.zip
#     https://geo2.dgterritorio.gov.pt/cos/S2/COS2018/COS2018v3-S2-gpkg.zip
#     HEAD: HTTP 200, application/zip, 900,176,075 bytes (~858 MB),
#     Last-Modified: Thu, 18 Dec 2025 15:56:51 GMT.
# - COS2023v1-S2-gpkg.zip
#     https://geo2.dgterritorio.gov.pt/cos/S2/COS2023/COS2023v1-S2-gpkg.zip
#     HEAD: HTTP 200, application/zip, 898,115,957 bytes (~857 MB),
#     Last-Modified: Tue, 15 Jul 2025 15:17:49 GMT.
# - INSPIRE record (canonical metadata identifier)
#     b498e89c-1093-4793-ad22-63516062891b
#     SNIG view: https://snig.dgterritorio.gov.pt/rndg/srv/api/records/b498e89c-1093-4793-ad22-63516062891b/formatters/snig-view
#     The inspire-geoportal.ec.europa.eu mirror was 404 on 2026-05-07;
#     SNIG is the working source of truth.
# - Methodology citation
#     Costa, H.; Benevides, P.; Moreira, F.D.; Moraes, D.; Caetano, M.,
#     2022. "Spatially Stratified and Multi-Stage Approach for National
#     Land Cover Mapping Based on Sentinel-2 Data and Expert Knowledge."
#     Remote Sensing 14, 1865. doi:10.3390/rs14081865
# - Alternative access (not used here, recorded for completeness)
#     - COSc WMS: https://geo2.dgterritorio.gov.pt/wms/COSc2023?service=WMS&REQUEST=GetCapabilities&VERSION=1.3.0
#     - COSc OGC API: https://ogcapi.dgterritorio.gov.pt/collections/cosc2023
#     - COS  WMS: https://geo2.dgterritorio.gov.pt/geoserver/COS-S2/wms?service=wms&version=1.3.0&request=GetCapabilities
#     - COS  OGC API root: https://ogcapi.dgterritorio.gov.pt/
#     - Shapefile mirrors (NOT used — GPKG preferred):
#         https://geo2.dgterritorio.gov.pt/cos/S2/COS2018/COS2018v3-S2-shp.zip
#         https://geo2.dgterritorio.gov.pt/cos/S2/COS2023/COS2023v1-S2-shp.zip
#     - Bulk portal (interactive): https://cdd.dgterritorio.gov.pt/
