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
- **License.** Copernicus open data.
- **Cadence.** ~6 days from Sentinel-1A alone (post-2022 constellation status to be re-checked at audit time).
- **Used for.** Cloud-resilient vegetation structure; cross-pol ratio as a complementary fuel-class signal.

### Cop-DEM GLO-30 — `PRIMARY`

- **STAC.** [MS PC — `cop-dem-glo-30`](https://planetarycomputer.microsoft.com/dataset/cop-dem-glo-30)
- **License.** ESA Cop-DEM access, attribution required.
- **Used for.** Slope, aspect, TPI features.

### ESA WorldCover 2021 — `PRIMARY`

- **STAC.** [MS PC — `esa-worldcover`](https://planetarycomputer.microsoft.com/dataset/esa-worldcover)
- **License.** Open, attribution: "ESA WorldCover 2021 © ESA".
- **Used for.** Land-cover prior; non-fuel class assignment outside vegetated regions.

### ETH Global Canopy Height 2020 — `PRIMARY`

- **Source.** Lang et al. 2023 (UZH); also mirrored on MS PC.
- **Access.** Direct COG tiles or STAC; resolved at audit time.
- **License.** CC BY 4.0.
- **Known gap.** Single 2020 vintage. No temporal updates — for inter-annual change use Meta CH or recompute.
- **Used for.** Canopy-height feature; height-vs-conductor proxy where infra is power-line.

### Meta Canopy Height 2024 — `AUXILIARY`

- **Source.** Meta open release, 1 m global.
- **Access.** Direct tile download.
- **License.** Open with attribution.
- **Why auxiliary.** Higher resolution but harder to align with Sentinel-2 derived fuel; included for cross-validation, not as a feature in the primary score.

### HLS S30/L30 — `AUXILIARY`

- **STAC.** [NASA LP DAAC](https://lpdaac.usgs.gov/products/hlss30v002/) + [HLSL30](https://lpdaac.usgs.gov/products/hlsl30v002/).
- **Access.** `pystac-client` against NASA-CMR; auth via `~/.netrc`.
- **Why auxiliary.** Used for inter-annual comparisons where harmonization with Landsat matters; not on the primary critical path.

### Dynamic World — `FUTURE`

- **Source.** Google EE; community mirrors exist.
- **Why future.** Near-real-time land cover; valuable for operational use, but pulling it from EE adds an auth surface that we don't need for the pilot.

## Fire and reference

### ICNF Áreas Ardidas — `PRIMARY`

- **Source.** [ICNF — Cartografia Nacional de Áreas Ardidas](https://www.icnf.pt/florestas/gfr/gfrgestaoinformacao/areasardidasporanocartografianacional).
- **Access.** Direct shapefile / GPKG downloads, annual.
- **License.** ICNF open, attribution required.
- **Coverage.** 1990 to most recent published year.
- **Used for.** Validation ground truth; `historical_burn_count_25y`, `historical_burn_share` features.

### ICNF Carta de Combustíveis Florestais — `PRIMARY`

- **Source.** ICNF.
- **Access.** Direct download (GeoTIFF + legend).
- **License.** ICNF open, attribution required.
- **Used for.** Fuel-class taxonomy crosswalk, weak labels for fuel-class segmentation training.

### Scott & Burgan FBFM40 — `REFERENCE` (no fetch needed)

- **Source.** USFS — Scott, J. H., & Burgan, R. E. (2005). Standard fire behavior fuel models.
- **Used for.** International crosswalk for ICNF classes; documented in `data/crosswalks/icnf_to_scott_burgan.yaml`.

### EFFIS Burned Area — `AUXILIARY`

- **Source.** JRC / Copernicus EFFIS.
- **Why auxiliary.** Cross-border validation, EU-wide future scope; for the PT pilot, ICNF is authoritative.

### VIIRS NRT Active Fire — `AUXILIARY`

- **Source.** NASA FIRMS.
- **Why auxiliary.** Recent fires; useful as a contextual layer; not a feature in the score.

### IPMA Daily Fire-Weather Index — `AUXILIARY`

- **Source.** [IPMA](https://www.ipma.pt/).
- **Why auxiliary.** Strong operational signal but seasonal; included as an optional multiplier in the exposure score, behind a config flag.

## Critical infrastructure

### OSM (via Overpass) — `PRIMARY`

- **Source.** [OpenStreetMap](https://www.openstreetmap.org/) via [Overpass](https://overpass-api.de/) or `osmnx`.
- **License.** ODbL — attribution and share-alike.
- **Used for.** Universe of asset candidates. Frozen taxonomy in `data/taxonomy/critical_infrastructure.yaml` defines class membership.

### Carta de Ocupação do Solo (COS) — `AUXILIARY`

- **Source.** [DGT](https://www.dgterritorio.gov.pt/) INSPIRE.
- **License.** Open, attribution required.
- **Why auxiliary.** Higher-quality national land-cover layer; useful for validating WorldCover priors over PT but not on the primary critical path.

## Best-practice signals captured

This list is what the project explicitly leans on, in DevSeed-flavoured EO best practice:

- **STAC 1.1** for catalog and ingestion (`pystac-client`, `pystac`, `stac-validator`).
- **COG** for every raster output.
- **GeoParquet** for vector outputs (modern columnar format; plays with DuckDB).
- **stackstac / odc-stac** for in-memory dask-backed loading; no scene downloads.
- **TorchGeo** datasets and samplers; no rolling-our-own tile-grid logic.
- **TerraTorch** for foundation-model fine-tuning, behind a baseline that doesn't depend on it.
- **Pinned versions** in `pyproject.toml`; `uv` lockfile is the source of truth.
- **No proprietary or paywalled layers** in the primary path.
