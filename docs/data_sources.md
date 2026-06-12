# Data sources

Detailed evaluation of every public source the project consumes. Filled in during the first dev session (after `audit` ships); the structure below is the contract.

For each source: URL, access mechanism, license, update cadence, known gaps, decision (PRIMARY / AUXILIARY / FUTURE).

## Earth observation

### Sentinel-2 L2A — `PRIMARY`

- **STAC.** [Microsoft Planetary Computer — `sentinel-2-l2a`](https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a)
- **Access.** `pystac-client` + `stackstac` for in-memory dask-backed loading.
- **License.** Copernicus open data, attribution required: "Contains modified Copernicus Sentinel data <year>".
- **Cadence.** ~5-day revisit at the equator; ~3 days at PT latitudes due to overlap.
- **Known gaps.** Cloud cover variability across late autumn / early winter; mitigated via filtering + Sentinel-1 fallback.
- **Used for.** Optical baseline; NDVI, NBR, fuel-class segmentation input.

### Sentinel-1 GRD — `PRIMARY`

- **STAC.** [MS PC — `sentinel-1-grd`](https://planetarycomputer.microsoft.com/dataset/sentinel-1-grd)
- **Access.** `pystac-client` + `stackstac`. Use IW mode, both VV and VH polarizations.
- **License.** Copernicus open data, attribution required: "Contains modified Copernicus Sentinel data <year>".
- **Cadence.** ~6 days from Sentinel-1A alone (post-2022 constellation status to be re-checked at audit time).
- **Known gaps.** Sentinel-1B lost in 2021; until Sentinel-1C is fully nominal, revisit at PT latitudes is irregular. Verify item density during `audit`.
- **Used for.** Cloud-resilient vegetation structure; cross-pol ratio as a complementary fuel-class signal.

### Cop-DEM GLO-30 — `PRIMARY`

- **STAC.** [MS PC — `cop-dem-glo-30`](https://planetarycomputer.microsoft.com/dataset/cop-dem-glo-30)
- **Access.** `pystac-client` + `stackstac` (raster) or `rioxarray` for AOI clip.
- **License.** Cop-DEM ESA open data, attribution required: "© DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018 provided under COPERNICUS by the European Union and ESA; all rights reserved."
- **Cadence.** Single fixed vintage (snapshot 2010–2018 acquisition). No temporal updates expected.
- **Known gaps.** GLO-30 has voids in steep terrain and over water; project AOI is hilly but well-covered.
- **Used for.** Slope, aspect, TPI features.

### ESA WorldCover 2021 — `PRIMARY`

- **STAC.** [MS PC — `esa-worldcover`](https://planetarycomputer.microsoft.com/dataset/esa-worldcover)
- **Access.** `pystac-client` + `stackstac`.
- **License.** CC-BY 4.0, attribution: "ESA WorldCover 2021 © ESA".
- **Cadence.** Two vintages published (2020 and 2021); successor product (WorldCover 10 m v3) expected but not on schedule. Treat as static.
- **Known gaps.** 11-class taxonomy is coarser than COS/COSc; used for cross-validation, not as primary fuel signal.
- **Used for.** Land-cover prior; non-fuel class assignment outside vegetated regions.

### ETH Global Canopy Height 2020 — `PRIMARY`

- **Source.** Lang et al. 2023 (UZH). DOI: [10.3929/ethz-b-000609802](https://doi.org/10.3929/ethz-b-000609802).
- **Access.** Direct COG tiles via libdrive.ethz.ch. **Audit on 2026-05-07 confirmed not present on MS PC** (enumerated all 135 collections; zero matches on `canopy`, `gch`, `eth`, `forest height`). Earlier note that it was "also mirrored on MS PC" is retracted.
- **Canonical URL pattern.** From the official tile browser at [langnico.github.io/globalcanopyheight](https://langnico.github.io/globalcanopyheight/assets/tile_index.html):
  ```
  https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download?path=%2F3deg_cogs&files=ETH_GlobalCanopyHeight_10m_2020_<NS><LAT_2D><EW><LON_3D>_Map.tif
  ```
  Tiles are 3-degree COGs named by SW corner (e.g. `N39W009` for the Pampilhosa pilot AOI). Verified 2026-05-07 — Range-GET of bytes 0..15 returns TIFF magic `49 49 2A 00`. Mirrored as one-liner in [scripts/00_eth_gch_fetch.sh](../scripts/00_eth_gch_fetch.sh).
- **License.** CC BY 4.0.
- **Cadence.** Single static vintage (2020 acquisition); no update schedule announced.
- **Known gap.** Single 2020 vintage. No temporal updates — for inter-annual change use Meta CH or recompute. Underestimates height in the tallest stands (>30 m) per Lang et al. validation.
- **Used for.** Canopy-height feature; height-vs-conductor proxy where infra is power-line.

### Meta Canopy Height 2024 — `AUXILIARY`

- **Source.** [Meta — High Resolution Canopy Height Map](https://research.facebook.com/publications/very-high-resolution-canopy-height-maps-from-rgb-imagery-using-self-supervised-vision-transformer-and-convolutional-decoder-trained-on-aerial-lidar/) (Tolan et al. 2024); 1 m global from RGB aerial + self-supervised ViT.
- **Access.** [Hugging Face mirror](https://huggingface.co/datasets/facebook/canopy_height_map) or AWS Open Data tile-grid distribution. Direct GeoTIFF tiles.
- **License.** CC-BY-NC 4.0 (non-commercial). **Attribution required.** Caveat noted under `docs/limitations.md`: incompatible with a commercial product launch; suitable for a public-data demonstrator.
- **Cadence.** Single 2024 vintage; no update schedule announced.
- **Known gaps.** Underlying RGB aerial coverage is uneven outside the US; PT coverage exists but should be sanity-checked against the published tile manifest. Vertical accuracy not as well-documented as ETH GCH.
- **Why auxiliary.** Higher resolution but harder to align with Sentinel-2 derived fuel; included for cross-validation, not a feature in the primary score. License is the deciding factor — keep auxiliary unless project becomes commercially licensable.

### HLS S30/L30 — `AUXILIARY`

- **STAC.** [NASA LP DAAC — HLSS30](https://lpdaac.usgs.gov/products/hlss30v002/) + [HLSL30](https://lpdaac.usgs.gov/products/hlsl30v002/).
- **Access.** `pystac-client` against [NASA CMR-STAC](https://cmr.earthdata.nasa.gov/stac/); auth via `~/.netrc` with Earthdata Login.
- **License.** US Federal Government open data; NASA Open Data Policy. No restrictions.
- **Cadence.** 2–3 day combined L8/L9/S2 cadence at PT latitudes (harmonized to a 30 m grid).
- **Known gaps.** Coarser than native S2 L2A; HLS v2.0 only goes back to 2013 for Landsat, 2015 for S2. Used for time-series harmonization, not single-scene work.
- **Why auxiliary.** Used for inter-annual comparisons where harmonization with Landsat matters; not on the primary critical path.

### Dynamic World — `FUTURE`

- **Source.** [Dynamic World V1](https://dynamicworld.app/), Brown et al. 2022 (Google + WRI); near-real-time 10 m land cover from S2.
- **Access.** Native via [Google Earth Engine](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1). Community mirrors exist on AWS Open Data; STAC mirror under consideration upstream.
- **License.** CC-BY 4.0.
- **Cadence.** Per-S2-scene (near-daily at PT latitudes).
- **Known gaps.** Pulling from EE requires service-account auth and `earthengine-api`; adds an auth surface the pilot doesn't need. 9-class taxonomy is coarser than COS, similar coarseness to WorldCover.
- **Why future.** Strong fit for the operational "current-state" use case but the EE auth dependency conflicts with the project's reproducible-by-anyone goal. Revisit when a public STAC mirror lands.

## Fire and reference

### ICNF Áreas Ardidas — `PRIMARY`

- **Source.** [ICNF — Cartografia Nacional de Áreas Ardidas](https://www.icnf.pt/florestas/gfr/gfrgestaoinformacao/areasardidasporanocartografianacional). Also reachable via ICNF ArcGIS REST MapServer (see `scripts/00_icnf_fetch.sh`).
- **Access.** Direct shapefile / GPKG downloads + REST query interface. Codified in `scripts/00_icnf_fetch.sh` for the 1975–2025 vintages.
- **License.** ICNF open data, attribution required to "ICNF – Áreas Ardidas em Portugal Continental, <year>".
- **Cadence.** Annual publication, typically ~6–12 months after fire season ends. 2025 vintage already published as of 2026-05.
- **Coverage.** 1975 to most recent published year.
- **Known gaps.** Polygons aggregate to ≥1 ha burned area; small fires under that threshold are excluded. Pre-1990 vintages are coarser (some only at concelho level). The ~1-year publication lag is the motivation for the Stage 1b Prithvi burn-scar inference.
- **Used for.** Validation ground truth (lift / Spearman / Brier against the exposure score); per-asset `historical_burn_count_25y`, `historical_burn_share` features.

### DGT COSc — `PRIMARY`

- **Source.** DGT (Direção-Geral do Território) — *Carta de Ocupação do Solo Conjuntural*, produced by the SMOS programme from Sentinel-2 via ML + expert rules.
- **Landing page.** [COSc2023 — DGT](https://www.dgterritorio.gov.pt/COSc2023-Carta-de-Ocupacao-do-Solo-Conjuntural-de-2023).
- **Access.** Direct GeoTIFF download from [DGT Centro de Dados (CDD)](https://cdd.dgterritorio.gov.pt/); scripted via the [DGT CDD Downloader QGIS plugin](https://plugins.qgis.org/plugins/dgt_cdd_downloader/) or `scripts/00_dgt_fetch.sh`.
- **License.** CC-BY 4.0, attribution to DGT.
- **Cadence.** Annual; latest published vintages COSc 2023 and COSc 2024 Pré-Verão (verified 2026-05-07).
- **Resolution.** 10 m raster.
- **Classes.** 4 fuel-cover classes (Dense Forest, Open Forest, Shrubland, Spontaneous herbaceous) within the broader COSc nomenclature.
- **Known gaps.** 4-class fuel taxonomy is coarse — no species split (broadleaf vs conifer, eucalyptus vs pine); resolved by combining with DGT COS species codes (see next entry). Trees / shrubs near the COSc class boundaries are inherently noisy due to S2 mixed pixels at 10 m.
- **Used for.** Coarse weak labels for fuel-class segmentation training. Replaces ICNF CCF as the operational training input until the CCF raster is accessible (see below).

### DGT COS 2018 / 2023 — `FUTURE` (species-level fine labels; not in the shipped path)

- **Source.** DGT — *Carta de Ocupação do Solo*, formal national LULC vector.
- **Access.** [DGT Dados abertos](https://www.dgterritorio.gov.pt/dados-abertos); GeoPackage, EPSG:3763. **The COS 2023_v1 GeoPackage zip (`geo2.dgterritorio.gov.pt/cos/S2/COS2023v1/COS2023v1-S2-gpkg.zip`) returns 404 as of 2026-06-12** — DGT appears to have moved/removed it. `fetch-rasters` therefore treats `cos` as **opt-in only** (`--only cos`); the default fetch and the CPU demo exclude it.
- **License.** CC-BY 4.0.
- **Cadence.** Major releases every ~5 years (2007, 2010, 2015, 2018, 2023).
- **Known gaps.** Vector polygons (not raster) — requires per-asset rasterisation to the project's grid. Multi-year publication gap means COS species codes can lag actual forest state by 2–4 years; for the Sever do Vouga pilot AOI, the eucalyptus-vs-pine boundary changes faster than this cadence captures. Mitigate with COSc-derived recency cross-check.
- **Used for.** *Future work* — species-level fine labels (broadleaf/conifer, Pinus/Eucalyptus/Quercus splits) for a possible fuel-class refinement. The shipped fuel layer (`fuel.py`) uses EFFIS + DGT COSc only; COS is not consumed.

### ICNF Carta de Combustíveis Florestais — `FUTURE`

- **Source.** ICNF — national authority fuel-class raster.
- **Access.** No public direct-download URL as of 2026-05-07 (verified against `geocatalogo.icnf.pt`, `sigservices.icnf.pt`, `fogos.icnf.pt`). Likely requires a free [geocatálogo](https://geocatalogo.icnf.pt/) account or an institutional request to ICNF.
- **Reference document.** [*Modelos de combustível florestal para Portugal — Documento de referência, versão de 2021*](https://www.researchgate.net/publication/357812218_Modelos_de_combustivel_florestal_para_Portugal_-_Documento_de_referencia_versao_de_2021) (ResearchGate PDF).
- **Methodology paper.** [Aparício & Fernandes 2020 — *A national fuel type mapping method improvement using Sentinel-2 satellite data*, Geocarto International](https://www.tandfonline.com/doi/full/10.1080/10106049.2020.1756460).
- **License.** ICNF open data once obtained, attribution required. Effective licensing posture is **registration-gated** for the raster itself.
- **Cadence.** Reference document published 2021; previous versions 2014 / 2017. ICNF's *Plano Nacional de Defesa da Floresta Contra Incêndios* (PNDFCI) cycle drives refresh, roughly every 5 years.
- **Status.** Held provisionally in `data/crosswalks/icnf_to_scott_burgan.yaml` under `icnf_taxonomy:` from Fernandes 2009 + Sá et al. 2023. Replace when the actual CCF legend is captured.
- **Known gaps.** No public direct-download URL is the primary gap (see Access). Coarser than landscape variability — class boundaries are 1:25 000 cartography aggregated upward, missing within-stand heterogeneity that COSc 10 m captures.
- **Used for.** National-authority alignment target. Not on the critical path for the pilot.

### EFFIS European Fuel Map — `REFERENCE`

- **Source.** JRC / Copernicus EFFIS — Pan-European fuel map, 42 vegetation complexes mapped to 13 NFFL fire-behavior model classes. Methodology in [Aragoneses et al. 2023 — *Classification and mapping of European fuels using a hierarchical, multipurpose fuel classification system*, ESSD](https://essd.copernicus.org/articles/15/1287/2023/).
- **Technical background.** [EFFIS — Fuels](https://forest-fire.emergency.copernicus.eu/about-effis/technical-background/fuels).
- **Access.** Direct GeoTIFF download from [EFFIS Data and Services](https://forest-fire.emergency.copernicus.eu/applications/data-and-services); WMS endpoints also published from the same portal. No auth. Codified in `scripts/00_effis_fetch.sh`.
- **License.** Free, no auth; Copernicus open-data attribution required ("EFFIS / JRC, European Commission").
- **Cadence.** Single-vintage 2023 publication; EFFIS does not currently announce a refresh schedule. Treat as a stable reference layer.
- **Known gaps.** ~100 m grid is coarser than Sentinel-2 native resolution; class assignments are EU-wide statistical, not field-validated for Portuguese landscapes. The crosswalk to FBFM40 inherits both source uncertainties.
- **Used for.** International readability — NFFL is the predecessor of FBFM40, so non-Portuguese reviewers can interpret the project's fuel classes without a Portugal-specific lookup. Also serves as a fallback fuel-class source if both DGT COSc and ICNF CCF were unavailable.

### Scott & Burgan FBFM40 — `REFERENCE` (no fetch needed)

- **Source.** Scott, J. H., & Burgan, R. E. (2005). [Standard fire behavior fuel models: a comprehensive set for use with Rothermel's surface fire spread model](https://www.fs.usda.gov/treesearch/pubs/9521). USFS RMRS-GTR-153.
- **Access.** Public-domain US Federal Government publication; reference table + class definitions in `data/crosswalks/icnf_to_scott_burgan.yaml`. LANDFIRE numeric encoding documented at <https://landfire.gov/fuel/fbfm40>.
- **License.** US Federal Government public domain.
- **Cadence.** Static (2005 framework, unchanged).
- **Known gaps.** Calibrated to North American fuel beds; Mediterranean and Atlantic-Iberian vegetation requires careful crosswalking — see commentary in the crosswalk YAML under each `comment:` field.
- **Used for.** International fire-behaviour modelling reference. Crosswalked from internal classes via NFFL (EFFIS) for international readers.

### EFFIS Burned Area — `AUXILIARY`

- **Source.** [JRC / Copernicus EFFIS — Current Situation Viewer](https://forest-fire.emergency.copernicus.eu/apps/effis_current_situation/) and [Statistics Portal](https://forest-fire.emergency.copernicus.eu/apps/effis.statistics/estimates).
- **Access.** Download via [EFFIS Data and Services](https://forest-fire.emergency.copernicus.eu/applications/data-and-services); WMS endpoints also published.
- **License.** Free, no auth.
- **Cadence.** Updated daily during fire season (MODIS- and VIIRS-derived perimeters).
- **Known gaps.** Coarser than ICNF Áreas Ardidas over Portugal (375 m VIIRS basis); useful for cross-border / EU-wide context but not as a primary PT validation source.
- **Why auxiliary.** Cross-border validation, EU-wide future scope; for the PT pilot, ICNF Áreas Ardidas is authoritative.

### VIIRS NRT Active Fire — `AUXILIARY`

- **Source.** [NASA FIRMS — Fire Information for Resource Management System](https://firms.modaps.eosdis.nasa.gov/).
- **Access.** Direct downloads at <https://firms.modaps.eosdis.nasa.gov/active_fire/> (CSV / shapefile / KML); REST API with free account.
- **License.** NASA Open Data Policy, attribution required.
- **Cadence.** Near-real-time, refreshed hourly (URT product), 375 m VIIRS thermal-anomaly detections.
- **Known gaps.** Active-fire detections (thermal anomaly), not burn extent; cloud-occluded; geolocation accuracy ~375 m.
- **Why auxiliary.** Recent active-fire context; useful for the demo's "current fire-season" framing alongside Prithvi-Burn-Scar, but not a feature in the score (granularity too coarse for per-asset features).

### IPMA Daily Fire-Weather Index — `AUXILIARY`

- **Source.** [IPMA — Risco de Incêndio Rural](https://www.ipma.pt/pt/riscoincendio/) (daily IRIR / FWI grids); Copernicus EMS publishes a Pan-European [GFWED-style FWI](https://cds.climate.copernicus.eu/cdsapp#!/dataset/cems-fire-historical) as an alternative.
- **Access.** IPMA publishes daily GeoTIFF and forecast bulletins; no public REST API documented. Copernicus CDS requires an account.
- **License.** IPMA: free for non-commercial / scientific use, attribution required. Copernicus: CC-BY 4.0.
- **Cadence.** Daily, near-real-time; historical archive on Copernicus CDS goes back to 1980.
- **Known gaps.** ~9 km resolution (too coarse for per-asset features at AOI scale); used as an AOI-level seasonal multiplier in the score, not a per-asset feature.
- **Why auxiliary.** Strong operational signal but seasonal; included as an optional multiplier in the exposure score, behind a config flag (`fwi_p95_recent_season` in `config/exposure_score.yaml`).

## Critical infrastructure

### OSM (via Overpass) — `PRIMARY`

- **Source.** [OpenStreetMap](https://www.openstreetmap.org/) via [Overpass API](https://overpass-api.de/) — primary endpoint `https://overpass-api.de/api/interpreter`, secondary `https://overpass.kumi.systems/api/interpreter`. Convenience access via `osmnx`.
- **Access.** Overpass QL queries derived from `data/taxonomy/critical_infrastructure.yaml`; snapshot date pinned in the asset GeoParquet's `osm_snapshot_iso` provenance field.
- **License.** ODbL — attribution and share-alike. Attribution string: "© OpenStreetMap contributors, ODbL".
- **Cadence.** Continuous editing; for reproducibility we pin to a snapshot date and re-query against an archival mirror (Geofabrik daily PT extracts at <https://download.geofabrik.de/europe/portugal.html>) if Overpass returns volatile counts.
- **Known gaps.** OSM coverage is uneven — rural distribution networks in central Portugal are under-tagged compared to urban areas. `audit` reports feature counts per class so coverage gaps are visible up front; sparse classes can be carried as YELLOW rather than degraded silently.
- **Used for.** Universe of asset candidates. Frozen taxonomy in `data/taxonomy/critical_infrastructure.yaml` defines class membership; the taxonomy itself is a citable artifact.

### Carta de Ocupação do Solo (COS) — see *Fire and reference* above

The formal DGT COS layer was originally scoped as a PRIMARY input for a species-aware fuel-class model; that model was scoped out, so COS is now **future work** — unused by the shipped EFFIS + DGT COSc crosswalk, and its download URL currently 404s. See the `DGT COS 2018 / 2023 — FUTURE` entry under *Fire and reference*.

## Inference models (derived data sources)

Models that *produce* per-asset feature inputs to the exposure score. Treated as data sources in their own right because their version, weights, and inference cadence are part of the score's provenance and must be cited the same way a satellite source is.

### Prithvi-EO 2.0 Burn-Scar — `PRIMARY` (Stage 1b)

- **Source.** [NASA-IMPACT / IBM — Prithvi-EO 2.0](https://github.com/NASA-IMPACT/Prithvi-EO-2.0); burn-scar downstream task is one of the canonical reference applications of the model family (heritage from Prithvi v1's HLS-Burn-Scars fine-tune).
- **Access.** Hugging Face model hub via TerraTorch (`terratorch>=1.2`). Exact HF model ID is verified at audit time per [`CLAUDE.md`](../CLAUDE.md) non-negotiable #1 and pinned in `config/burn_scar.yaml`; placeholder marker `TBD-verified-at-audit` in `inventory.yaml` under the `burn-scar-recent` collection until then.
- **License.** Apache 2.0 (Prithvi-EO 2.0 model weights and code).
- **Cadence.** Inference run on-demand against the trailing 12 months of Sentinel-2 L2A imagery. Re-runnable monthly without retraining; fine-tuning is out of scope for the pilot.
- **Known gaps.** Trained primarily on HLS imagery; performance on native Sentinel-2 L2A in Atlantic-Iberian landscapes is the open empirical question — documented in `docs/burn_scar_audit.md` after Stage 1b ships. Pilot uses frozen-backbone inference only.
- **Used for.** Per-asset `recent_burn_share_12mo` feature in Stage 2; fills the gap between the latest ICNF Áreas Ardidas vintage (annual, ~1-year lag) and "right now". See [`prompts/09_burn_scar_inference.md`](../prompts/09_burn_scar_inference.md).

## Cross-check — open-EO and utility-VM canonical practice

§H of `PRE_DEV_CHECKLIST.md` requires explicit justification for any canonical source we skipped. The matrix below names every standard the project should be expected to have an opinion on, plus this project's stance.

### Open-EO canonical sources (the shared idiom of the modern open-EO ecosystem)

| Canonical | This project's stance | Justification |
|---|---|---|
| **STAC 1.1** for catalog and ingestion | **adopted** (`pystac-client`, `pystac`, `stac-validator`); STAC catalog under `stac/` is a first-class deliverable | Modern EO best practice; aligns with the eoAPI ecosystem and the `cheias-pt-stac` sibling repo. |
| **COG** for every raster output | **adopted**; no GeoTIFF non-COG, no Shapefile, no GeoPackage as primary output | Codified in [`CLAUDE.md`](../CLAUDE.md) non-negotiable #5. |
| **GeoParquet** for vector outputs | **adopted** for `exposure-assets` collection (per-asset scored output) | Aligned with the wider GeoParquet 1.1 momentum; plays with DuckDB out of the box. |
| **`stackstac` / `odc-stac`** for in-memory STAC reads | **adopted** | No manual scene downloads; deterministic + dask-friendly. |
| **TorchGeo** for datasets / samplers | **adopted** (`torchgeo>=0.9`) | Canonical PyTorch-side EO datasets layer; eliminates rolling our own tile-grid logic. |
| **TerraTorch** for foundation-model fine-tuning | **adopted** (`terratorch>=1.2`) for the foundation-model variant and Stage 1b burn-scar inference | Modern path for Prithvi / Clay; behind a SegFormer baseline so the pipeline doesn't depend on it. |
| **eoAPI / VEDA-UI** for serving / visualisation | **available, not required** | Project is a reproducible repo, not a deployed site; reuse the `cheias-pt-*` deployment patterns if the demo needs a viewer later. |
| **Sentinel-2 L2A** | **adopted as PRIMARY** | See entry above. |
| **Sentinel-1 GRD** | **adopted as PRIMARY** | See entry above. |
| **Cop-DEM GLO-30** | **adopted as PRIMARY** | See entry above. |
| **ESA WorldCover** | **adopted as PRIMARY** (priors / non-fuel mask) | See entry above. |
| **HLS (Harmonized Landsat-Sentinel)** | **AUXILIARY** | Coarser than native S2 L2A; reserved for time-series harmonization, not primary. |
| **Pinned versions + lockfile** | **adopted**; `uv.lock` is the source of truth at install time | Lower-bound-only ranges in `pyproject.toml` per the Schreiner principle. |
| **No proprietary or paywalled layers in the primary path** | **adopted** | Every PRIMARY source is openly downloadable; Meta CH (CC-BY-NC) and ICNF CCF (registration) are explicitly NOT primary. |

### Utility vegetation-management operational signals

The utility VM industry — published case studies from multiple vendors, utility engineering reports, and the IQGeo / SAP-WMS integration patterns that have become standard — emphasises a specific operational shape that informs this project's design even though we don't share the commercial stack.

| Operational signal | This project's stance | How it's expressed |
|---|---|---|
| **Per-asset scoring** (span-level, tree-level) | **adopted at asset-level** (OSM-derived); per-tree is explicitly out of scope (Sentinel-2 resolution insufficient) | See README → Modeling, Stage 2. |
| **Fuel-class taxonomy** (Scott & Burgan FBFM40 as the international anchor) | **adopted as the international reference**; internal 9-class taxonomy crosswalks to FBFM40 and NFFL-13 | `data/crosswalks/icnf_to_scott_burgan.yaml`. |
| **Time-aware burn signal** (recent + historical) | **adopted as a two-feature blend** in the score | `historical_burn_share` (ICNF, decadal) + `recent_burn_share_12mo` (Prithvi-Burn-Scar, current season). |
| **Provenance per row** | **adopted as non-negotiable** (`CLAUDE.md` #3); every scored asset row carries `model_id`, `model_version`, source STAC IDs, `run_id`, `code_commit_sha` | See README → Outputs → Provenance schema. |
| **Auditable score formula** | **adopted** — transparent linear combination, weights in YAML | `config/exposure_score.yaml`, weights sum to 1.0 (CI-asserted). |
| **Honest scope vs. commercial** | **adopted** — no calibrated-probability claims, no "production-ready" framing | `CLAUDE.md` non-negotiable #6 and #9; explicit limitations doc. |
| **Commercial sub-meter imagery (Planet, Maxar)** | **explicitly not adopted** | Project is a public-data demonstrator; resolution gap acknowledged in `docs/limitations.md`. |
| **LiDAR-derived canopy structure** | **substituted with ETH GCH / Meta CH** | Portugal's national LiDAR coverage is uneven and not yet open; canopy-height proxies are the practical alternative. |
| **Aerial / drone imagery** | **explicitly not adopted** | Same reason as above; the operational shape is what's being demonstrated, not the resolution. |

### Sources deliberately not in the primary path

| Source | Why excluded | When to revisit |
|---|---|---|
| Planet PlanetScope / RapidEye / SkySat | Commercial (paywalled); incompatible with the project's open-data scope | If the project ever needs to demonstrate per-tree (rather than per-asset) scoring as a separate phase. |
| Maxar WorldView | Same | Same. |
| Sentinel Hub processing API | Adds an auth layer + cost surface | Not needed — MS PC + NASA LP DAAC + Earth Search cover the same data via STAC. |
| Google Earth Engine | Auth surface conflicts with reproducible-by-anyone goal | Once Dynamic World has a STAC mirror, or if a specific EE-only product becomes load-bearing. |
| Portuguese e-Redes private datasets | Operator-private; explicitly excluded by `CLAUDE.md` non-negotiable #7 | Never, within this project's scope. |
| REN private datasets | Same | Same. |
| DGEG (energy regulator) private datasets | Same | Same. |
| ICNF SGIF cause-attribution data | Behind ICNF account / institutional request | Parallel future-alignment with the ICNF CCF acquisition. |
