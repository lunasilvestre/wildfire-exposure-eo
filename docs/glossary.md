# Glossary

> Working vocabulary for `wildfire-exposure-eo`. Each entry is 1–3 sentences with a citation URL where applicable. Entries are grouped by domain. Terms shared between groups are cross-referenced inline.

This document closes [PRE_DEV_CHECKLIST.md](../PRE_DEV_CHECKLIST.md) items **E.1 (utility vegetation-management vocabulary)** and **F (Glossary committed)**. Continue to extend during dev — the keyword density of this file is the project's external memory.

---

## Project terms

**Exposure score.** The project's primary per-asset output: a 0–1 *relative* ranking of wildfire exposure derived from fuel class, canopy height, slope, historical-burn frequency, and (optionally) fire-weather. Computed as a transparent linear combination of normalised features — weights live in `config/exposure_score.yaml`. **Not a calibrated probability**: only ranks within the AOI are meaningful. Per [`CLAUDE.md`](../CLAUDE.md) non-negotiable #6, the README, docs, and validation report must use *exposure / rank / relative* language, never *risk probability* or *chance of fire*.

**Asset class (project taxonomy).** One of the 13 OSM-derived critical-infrastructure classes enumerated in [`data/taxonomy/critical_infrastructure.yaml`](../data/taxonomy/critical_infrastructure.yaml) (e.g. `power.transmission_line`, `emergency.hospital`, `transport.railway`). Each carries a buffer radius and a portfolio-aggregation criticality weight. The taxonomy file is the citable artifact, versioned independently.

**Internal fuel class.** One of the 9 model-predicted classes enumerated in `internal_classes` of [`data/crosswalks/icnf_to_scott_burgan.yaml`](../data/crosswalks/icnf_to_scott_burgan.yaml) (e.g. `shrub-tall`, `conifer-closed`, `non-fuel`). Crosswalked downstream to FBFM40 for fire-behaviour modelling and to NFFL-13 via EFFIS for international readability; crosswalked upstream from DGT COSc + COS weak labels.

---

## EO / STAC terms

**STAC (SpatioTemporal Asset Catalog).** A specification that standardises how geospatial assets — satellite scenes, derived products, point clouds — are described and discovered. The project will publish its own STAC 1.1 catalog. See <https://stacspec.org/>.

**Collection.** A STAC grouping that shares common metadata (extent, license, providers) across many items. One collection per source dataset is the project default (e.g. `s2-l2a-pilot`, `icnf-areas-ardidas`, `predictions-fuel-class`).

**Item.** A single STAC entity — typically one scene or one acquisition — with geometry, datetime, properties, and one or more assets. Items are GeoJSON Features at the wire format.

**Asset.** A pointer (HREF + media type + role) to one file referenced by an item — e.g. a COG band, a thumbnail, a metadata XML, a JSON sidecar.

**COG (Cloud-Optimized GeoTIFF).** A GeoTIFF organised so HTTP range-reads can fetch only the needed tiles and overviews. The project's raster outputs ship as COGs. See <https://www.cogeo.org/>.

**GeoParquet.** A columnar Parquet variant with a geometry column and CRS metadata. Used here for vector outputs (predictions, perimeters, OSM extracts) where row-group pruning beats Shapefile/GeoPackage on cloud storage. See <https://geoparquet.org/>.

**eoAPI.** Open-source stack that bundles `stac-fastapi`, `titiler`, and `tipg` behind a single deployment, exposing STAC, raster, and vector APIs. The project mirrors the conventions used in `cheias-pt-stac`. See <https://eoapi.dev/>.

**VEDA-UI.** NASA's Visualization, Exploration, and Data Analysis user interface, built around stories and map blocks fed by a STAC catalog. Already used in `cheias-pt-veda-ui`.

**Sentinel-2 L2A.** Surface-reflectance product from ESA's Sentinel-2 mission, atmospherically corrected from L1C with Sen2Cor. Native 10/20/60 m bands. Primary optical input.

**Sentinel-1 GRD.** Ground Range Detected SAR product from ESA's Sentinel-1, multi-looked and projected to ground range. C-band, dual-polarisation. Primary radar input — useful for cloud-penetrating moisture and structure proxies.

**Cop-DEM GLO-30.** Copernicus 30 m global Digital Elevation Model derived from TanDEM-X. Topographic baseline (slope, aspect, TPI features).

**ESA WorldCover 2021.** Global 10 m land-cover product (11 classes) produced from S1+S2. Used for masking, baseline class priors, and audit cross-checks.

**ETH GCH (Global Canopy Height).** ETH Zürich's 10 m global canopy-height model from Sentinel-2 + GEDI, vintage 2020. Single-vintage product — accept temporal mismatch in design. See <https://langnico.github.io/globalcanopyheight/>.

**HLS (Harmonized Landsat-Sentinel).** NASA product that harmonises L8/L9 OLI and S2 MSI to a 30 m grid with consistent BRDF/atmospheric correction. Used as a fallback when S2 alone has insufficient cloud-free coverage.

**NDVI / NBR.** Normalised Difference Vegetation Index and Normalised Burn Ratio. NDVI = (NIR−R)/(NIR+R) for greenness; NBR = (NIR−SWIR2)/(NIR+SWIR2) for burn severity (dNBR is the pre/post difference).

**FWI (Fire Weather Index).** Canadian Forest Service composite index combining temperature, humidity, wind, and rain history. Used as a meteorological covariate; ECMWF publishes a global gridded FWI via Copernicus EFFIS.

---

## Fire-science terms

**Fuel class / fuel model.** A categorical description of surface fuels (grass, shrub, timber-litter, slash) that a fire-spread model consumes. The project predicts a coarse internal class and crosswalks to FBFM40. See `data/crosswalks/icnf_to_scott_burgan.yaml`.

**FBFM40 (Scott & Burgan, 2005).** The 40-model standard fire behaviour fuel-model set used by US LANDFIRE — extends Anderson's 13 models with finer resolution in grass, shrub, and timber-litter regimes. Codes use letter prefixes (NB, GR, GS, SH, TU, TL, SB) plus a digit. See <https://landfire.gov/fuel/fbfm40>.

**Anderson 13 / NFFL.** The original 13 fire behaviour fuel models (Anderson, 1982), still widely used in Portuguese and Mediterranean literature alongside FBFM40. Often referenced as NFFL (Northern Forest Fire Laboratory).

**Fuel load.** The dry mass of available fuel per unit area, typically in kg/m² or t/ha, partitioned by size class (1-hr / 10-hr / 100-hr / live).

**Canopy base height (CBH).** Height above ground of the lowest canopy fuels capable of sustaining vertical fire spread — the rung between surface fire and crown fire.

**Canopy bulk density (CBD).** Mass of available canopy fuel per unit canopy volume (kg/m³). With CBH it sets the threshold for active crown fire under given wind conditions.

**Fireshed.** A landscape-scale unit defined by the set of ignitions that could plausibly reach a given asset — analogous to a watershed, but for fire arrival. Increasingly used in US wildfire-risk literature.

**Burn severity.** The ecological/structural impact of a fire, distinct from intensity. Operationally proxied with dNBR / RdNBR thresholds.

**dNBR.** Differenced NBR (pre minus post). Standard burn-severity proxy with empirical thresholds (Key & Benson, 2006).

**Defensible space / faixa de gestão de combustíveis.** A zone around an asset (building, power line, road) where vegetation is mechanically reduced to slow fire arrival. In Portugal, regulated under DL 124/2006 (and successor DL 82/2021).

---

## Domain — utility vegetation management

> The canonical vocabulary used across the utility VM industry — vendor case studies, utility engineering reports, and academic literature. This project adopts the idiom directly so its outputs read fluently to practitioners.

**Strike tree.** A tree positioned and structured such that, on failure, it would strike a powerline span. A strike-tree population is the universe of trees the utility could in principle hit; the model's job is to rank them.

**Hazard tree.** A tree with elevated failure probability — defects, decline, dieback, lean — independently of whether it would strike a line. A hazard tree that is also a strike tree is the priority for removal.

**Hot spot.** A geographic concentration of risk on the network where multiple trees, asset types, or weather drivers compound. Often surfaced as a small percentage of network area accounting for a large share of risk.

**Span.** The conductor segment between two adjacent structures (poles, towers). Span-level analysis is the standard unit of reporting for vegetation risk.

**Span-level / circuit-level.** Spatial granularity of risk reporting. *Span-level* surfaces individual hazard trees against individual conductor segments; *circuit-level* aggregates upward to the substation feeder, useful for cycle scheduling.

**Clear span.** A span whose vegetation analysis returns no apparent risk. Operationally valuable because it can be removed from a trim cycle, freeing crew time.

**High-Reliability Zone (HRZ).** The portion of a circuit between a substation and the first protective device. Failures here propagate furthest, so HRZs concentrate hazard-tree mitigation budget.

**Encroachment.** Vegetation growing into the conductor's clearance envelope. A primary trigger for trim-cycle scheduling.

**Trim cycle / cycle-based trimming.** Calendar-driven vegetation management — every circuit gets trimmed every N years regardless of state. Predictable but inefficient.

**Condition-based trimming.** Trim decisions driven by observed/inferred risk per span instead of calendar. The modern industry direction across utility VM products and regulatory reform.

**SAIDI / SAIFI.** Standard utility reliability metrics — System Average Interruption Duration Index (minutes per customer per year) and Frequency Index (interruptions per customer per year). Vegetation-driven outages are a major contributor.

---

## Portuguese terms

**ICNF (Instituto da Conservação da Natureza e das Florestas).** Portuguese national agency for nature conservation and forests; publishes annual *áreas ardidas*, the *Carta de Combustíveis Florestais* (CCF), and protected-area cartography. See <https://www.icnf.pt/>.

**AGIF (Agência para a Gestão Integrada de Fogos Rurais).** Portuguese rural-fire integrated-management agency; publishes the *Normas Técnicas* for the defence-network cartography and coordinates the SGIFR. See <https://www.agif.pt/>.

**SGIFR (Sistema de Gestão Integrada de Fogos Rurais).** Integrated rural-fire management system, the legal/operational framework that succeeds the 2006 forest-defence regime. Established by DL 82/2021.

**SGIF (Sistema de Gestão de Informação de Incêndios Florestais).** ICNF's operational fire-information database (ignitions, perimeters, causes). Backbone of the public *fogos.icnf.pt* portal.

**Áreas Ardidas.** Annual ICNF burned-area perimeter dataset for continental Portugal, published since 1990. Primary supervised-signal source for the project. See <https://geocatalogo.icnf.pt/>.

**COS (Carta de Ocupação do Solo).** DGT's *formal* national land-cover/land-use cartography, vector GeoPackage with species-level forest codes. Published on a multi-year cadence (latest publicly available: COS 2018 v3 and COS 2023 v1). Used here as the species-level fine-label input that resolves COSc's broadleaf/conifer ambiguity. See <https://www.dgterritorio.gov.pt/dados-abertos>.

**COSc (Carta de Ocupação do Solo Conjuntural).** DGT's *annual* ML-derived companion to COS, produced from Sentinel-2 by the SMOS pipeline. 10 m raster with 4 dedicated fuel-cover classes (Dense Forest, Open Forest, Shrubland, Spontaneous herbaceous) inside the broader nomenclature. Coarser than COS but updated yearly. **Primary operational training input** for fuel-class segmentation; replaces the ICNF CCF in the operational chain until that raster is obtainable. See <https://www.dgterritorio.gov.pt/COSc2023-Carta-de-Ocupacao-do-Solo-Conjuntural-de-2023>.

**SMOS (Sistema de Monitorização da Ocupação do Solo).** DGT's land-occupation monitoring system — the ML pipeline (Sentinel-2 + expert rules) that produces COSc annually. *Not to be confused* with ESA's SMOS soil-moisture satellite mission. See <https://www.dgterritorio.gov.pt/cartografia/cartografia-tematica>.

**DGT (Direção-Geral do Território).** Portuguese national mapping agency; publishes COS, INSPIRE-compliant geoportal, and reference orthophotography. See <https://www.dgterritorio.gov.pt/>.

**REN (Redes Energéticas Nacionais / Reserva Ecológica Nacional).** Two distinct meanings — context-dependent. *REN-energia* is the high-voltage transmission TSO; *REN-ecológica* is the regulatory ecological reserve land class.

**e-Redes.** The Portuguese DSO (medium- and low-voltage distribution operator), formerly EDP Distribuição. The strike-tree framing applies primarily to e-Redes' rural distribution network.

**Carta de Combustíveis Florestais (CCF).** ICNF's national surface fuel-class cartography. Source taxonomy for the crosswalk in `data/crosswalks/icnf_to_scott_burgan.yaml`.

**Faixa de gestão de combustíveis (FGC).** Fuel-management strip — vegetation buffer zone around a defined target (building, road, power line). Three regulated levels: primária (landscape-scale), secundária (around assets), terciária (around buildings).

**RPFGC (Rede Primária de Faixas de Gestão de Combustível).** Primary network of fuel-management strips, designed at district scale to break large-fire propagation.

**ZIF (Zona de Intervenção Florestal).** Forest-intervention zone — a contractually-managed forest aggregate established to overcome smallholder fragmentation. Relevant covariate for fuel-treatment likelihood.

**PNPG (Parque Nacional da Peneda-Gerês).** Portugal's only national park, in the north (Viana do Castelo / Braga / Vila Real). Anchors `alt_peneda_geres.geojson`.

**Reacendimento.** A re-ignition inside or on the perimeter of a recently extinguished fire. ICNF tracks these explicitly under cause code 711.

---

## OSM tags used

> This section mirrors `data/taxonomy/critical_infrastructure.yaml` (v0.1.0-draft, 15 classes). Whenever that file changes, this section must be updated in the same commit. Tags are quoted in the OSM `key=value` form; class IDs reference the taxonomy schema.

### Power network (`power.*`)

**`power=line` + `voltage` ≥ 60 kV** — class `power.transmission_line`. High-voltage transmission conductors. The voltage filter is regex-based (`^([6-9][0-9]{4}|[1-9][0-9]{5,})$`); refine against actual OSM coverage during audit. Buffer 30 m, criticality 1.00.

**`power=line` (lower voltage) + `power=minor_line`** — class `power.distribution_line`. Distribution conductors below 60 kV or with unspecified voltage. The strike-tree framing applies primarily here. Buffer 20 m, criticality 0.70.

**`power=substation`** (node / way / relation) — class `power.substation`. Substations are HRZ anchors; failure here propagates furthest. Buffer 100 m, criticality 0.95.

**`power=transformer`** (node / way) — class `power.transformer`. Pole-mounted and pad-mounted transformers. Buffer 30 m, criticality 0.50.

**`power=tower`** (node) — class `power.tower`. Steel/concrete transmission lattices. Density per km² is a useful exposure proxy. Buffer 20 m, criticality 0.40.

**`voltage=*`** — used as a filter on `power=line` to split transmission from distribution, not as a class on its own. See OSM wiki: <https://wiki.openstreetmap.org/wiki/Key:voltage>.

### Emergency services (`emergency.*`)

**`amenity=fire_station`** (node / way) — class `emergency.fire_station`. Forward-deployed suppression assets; co-determines response-time exposure. Buffer 75 m, criticality 0.95.

**`amenity=hospital`** (node / way) — class `emergency.hospital`. Casualty-care assets, highest portfolio criticality. Buffer 100 m, criticality 1.00.

**`amenity=police`** (node / way) — class `emergency.police`. Public-safety assets, also evacuation-coordination nodes in Portuguese fire response. Buffer 75 m, criticality 0.80.

### Education (`education.*`)

**`amenity=school`** (node / way) — class `education.school`. Schools are special-population assets — children, evacuation difficulty, and (in summer) closed-but-flammable WUI buildings. Buffer 75 m, criticality 0.85.

### Telecommunications (`telecom.*`)

**`man_made=communications_tower`** (node / way) and **`tower:type=communication`** (node) — class `telecom.tower`. Cell towers and broadcast masts; fire damage to these compounds with power outages to disable evacuation comms. Buffer 30 m, criticality 0.60.

### Water infrastructure (`water.*`)

**`man_made=water_works`** (node / way) and **`man_made=wastewater_plant`** (way) — class `water.treatment_plant`. Treatment-plant assets; service-area population is the implicit exposure population. Buffer 100 m, criticality 0.85.

**`landuse=reservoir`** (way) and **`natural=water` + `water=reservoir`** (way) — class `water.reservoir`. Two equivalent OSM idioms for the same thing; both must be queried because tagging conventions vary by mapper. Reservoirs are exposure assets *and* firefighting water sources — note the dual role in downstream interpretation. Buffer 100 m, criticality 0.50.

### Transport (`transport.*`)

**`railway=rail`** (way) — class `transport.railway`. Rail infrastructure. Historically a non-trivial Portuguese ignition vector (brake-block sparking) as well as an evacuation route. Buffer 25 m, criticality 0.60.

### Candidates for future taxonomy expansion (not in `critical_infrastructure.yaml` v0.1.0)

These tags appear in upstream utility-VM and wildfire-risk literature but are not in the current canonical taxonomy. They're listed here to keep the option visible — promote to `critical_infrastructure.yaml` if the audit step shows they're load-bearing for the pilot AOI.

- **`power=pole`** — Wooden/concrete distribution poles. e-Redes' rural network is heavily pole-based; could justify a `power.pole` class distinct from `power.tower`.
- **`highway=*`** — Roads as both access routes for suppression and ignition vectors (sparks from machinery, discarded cigarettes). Candidate values: `motorway`, `trunk`, `primary`, `secondary`, `tertiary`, `unclassified`, `service`, `track`.
- **`landuse=forest`, `natural=wood`** — OSM-tagged forest cover. Lower authority than COS or ESA WorldCover but useful as a spatial-complementarity check in OSM-rich areas.
- **`landuse=residential`, `place=village`, `place=hamlet`** — WUI proxies; defensible-space regulation (DL 124/2006, DL 82/2021) applies around these.
- **`pipeline=*`, `man_made=pipeline`** — Pipelines, especially `substance=gas`. Critical-infrastructure exposure outside the electrical grid.

---

## References

- Scott, J. H., & Burgan, R. E. (2005). *Standard fire behavior fuel models: a comprehensive set for use with Rothermel's surface fire spread model.* USFS RMRS-GTR-153.
- Anderson, H. E. (1982). *Aids to determining fuel models for estimating fire behavior.* USFS GTR-INT-122.
- Fernandes, P. M. (2009). Combining forest structure data and fuel modelling to classify fire hazard in Portugal. *Annals of Forest Science*, 66(4), 415.
- Sá, A.C.L., Benali, A., Aparicio, B.A., et al. (2023). A method to produce a flexible and customized fuel models dataset. *MethodsX*, 10, 102218.
- Key, C. H., & Benson, N. C. (2006). Landscape assessment (LA): sampling and analysis methods. USFS RMRS-GTR-164-CD.
- *eoAPI — Earth Observation API stack.* <https://eoapi.dev/>.
- STAC. *SpatioTemporal Asset Catalog 1.1.0 specification.* <https://stacspec.org/>.
