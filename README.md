# wildfire-exposure-eo

A STAC-native pipeline that scores OpenStreetMap-derived critical infrastructure by wildfire exposure for a Portuguese pilot AOI, combining Sentinel-2 fuel-class mapping, canopy-height features, ICNF burned-area history, and topographic + fire-weather priors. Built on public Earth Observation data with full provenance per asset, validated against historical burn polygons.

> **Status.** Pre-development. See [`PRE_DEV_CHECKLIST.md`](PRE_DEV_CHECKLIST.md) before opening a Claude Code session. See [`CLAUDE.md`](CLAUDE.md) for session conventions.

## Why this exists

Critical infrastructure — power lines, substations, transformers, water-treatment plants, telecom towers, hospitals, schools, emergency-services facilities — is unevenly exposed to wildfire risk. Static hazard maps published by national agencies tend to be land-cover-driven, slow to update, and asset-agnostic. This project produces a per-asset wildfire-exposure score, derived from current-year EO data and validated against historical burn outcomes, scoped initially to a single Portuguese fire district as a methodology demonstrator.

The methodology generalizes nationally and beyond. The pilot is intentionally small so that every claim in the README is reproducible end-to-end on a fresh clone in under 30 minutes on CPU with pretrained checkpoints.

## What this is not

- Not a fire-spread or fire-behaviour simulation. We score relative exposure, not absolute probability.
- Not a replacement for utility-grade vegetation management products (e.g., Overstory). Those use sub-meter commercial imagery, proprietary canopy-instance segmentation, and customer-validated ground truth. This is a public-data demonstrator that mirrors the *operational shape* of those products under open-data constraints.
- Not deployed as a live service. The deliverable is a reproducible repo + STAC catalog + GeoParquet asset table.

## Architecture

```
┌─────────────────────┐      ┌──────────────────────┐      ┌────────────────────┐
│  Critical infra     │      │  Earth-observation   │      │  Reference layers  │
│  (OSM via Overpass) │      │  (Sentinel-2/1, HLS, │      │  (ICNF burns, COS, │
│  → GeoParquet       │      │   DEM, canopy ht)    │      │   FWI, Scott&Burgan│
│                     │      │  → STAC items + COGs │      │   crosswalk)       │
└──────────┬──────────┘      └──────────┬───────────┘      └─────────┬──────────┘
           │                            │                            │
           └─────────┬──────────────────┴──────────────┬─────────────┘
                     │                                 │
                     ▼                                 ▼
           ┌───────────────────────┐       ┌─────────────────────────┐
           │  Fuel-class model     │       │  Per-asset feature      │
           │  (TerraTorch-fine-   │       │  extraction (rasterio + │
           │   tuned Prithvi-EO    │       │   stackstac, buffered   │
           │   or SegFormer base)  │       │   per asset class)      │
           └───────────┬───────────┘       └────────────┬────────────┘
                       │                                │
                       └──────────────┬─────────────────┘
                                      ▼
                         ┌───────────────────────────┐
                         │  Exposure score           │
                         │  (composite, calibrated)  │
                         │  → GeoParquet + STAC      │
                         │  → COG (raster)           │
                         └───────────────┬───────────┘
                                         ▼
                         ┌───────────────────────────┐
                         │  Validation               │
                         │  (ICNF burns 2017–24)     │
                         │  Lift / Spearman / Brier  │
                         └───────────────────────────┘
```

## Pilot AOI

Default: **Pampilhosa da Serra + Pedrógão Grande district**, central Portugal. ~30 × 30 km bbox covering a region with documented high historical burn frequency, sparse population, high-voltage REN transmission, and dense distribution-network OSM coverage. Frozen as a single GeoJSON in `data/aoi/pilot.geojson` so every artifact references the same geometry.

Generalizable to any AOI by editing `data/aoi/pilot.geojson` and re-running the pipeline.

## Data sources

All sources are public, STAC-native where possible, COG-friendly, and citable.

### Earth observation

| Layer | Source | Resolution | Access | Role |
|---|---|---|---|---|
| Sentinel-2 L2A | Microsoft Planetary Computer STAC | 10 m | `pystac-client` + `stackstac` | Optical baseline, NDVI/NBR, fuel-class input |
| Sentinel-1 GRD | Microsoft Planetary Computer STAC | 10 m | `pystac-client` + `stackstac` | SAR backscatter, cloud-resilient vegetation structure |
| HLS S30/L30 | NASA LP DAAC STAC | 30 m | `pystac-client` | Harmonized multi-sensor for inter-annual analysis |
| Copernicus DEM GLO-30 | MS Planetary Computer STAC | 30 m | `pystac-client` | Slope, aspect, TPI |
| ETH Global Canopy Height (2020) | Lang et al. / MS PC | 10 m | direct download or STAC | Canopy-height feature |
| Meta Canopy Height (2024) | Meta open release | 1 m | direct tiles | Higher-resolution canopy comparison (optional) |
| ESA WorldCover 2021 | MS Planetary Computer STAC | 10 m | `pystac-client` | Land-cover prior |
| Dynamic World | Google EE / public mirror | 10 m | optional | Near-real-time land cover (optional) |

### Reference / validation

| Layer | Source | Format | Role |
|---|---|---|---|
| ICNF Áreas Ardidas | ICNF (Instituto da Conservação da Natureza e Florestas) | annual polygons (Shapefile/GPKG) | Validation ground truth, history feature |
| ICNF Carta de Combustíveis Florestais | ICNF | raster | National fuel-class taxonomy |
| Scott & Burgan FBFM40 | USFS / NREL | reference document | International fuel-model crosswalk |
| EFFIS Burned Area | JRC / Copernicus | annual polygons | Cross-border validation, EU-wide future scope |
| VIIRS NRT Active Fire | NASA FIRMS | CSV / GeoJSON | Recent fires (optional, contextual) |
| IPMA Daily FWI | IPMA | grid | Fire-weather multiplier (optional) |
| Carta de Ocupação do Solo (COS) | DGT (Direção-Geral do Território) | vector | Portuguese land-cover reference |

### Critical infrastructure

OSM is the universe of asset candidates, queried via Overpass with a frozen taxonomy. The taxonomy is itself a citable artifact, defined in `data/taxonomy/critical_infrastructure.yaml`. Each class carries:

- the OSM tag set that defines membership,
- a buffer-radius default for feature extraction,
- a criticality weight for portfolio aggregation,
- a license attribution string.

Initial classes:

```
power.transmission_line       (power=line, voltage>=60000)
power.distribution_line       (power=line, voltage<60000 OR power=minor_line)
power.substation              (power=substation)
power.transformer             (power=transformer)
power.tower                   (power=tower)
emergency.fire_station        (amenity=fire_station)
emergency.hospital            (amenity=hospital)
emergency.police              (amenity=police)
education.school              (amenity=school)
telecom.tower                 (man_made=communications_tower OR tower:type=communication)
water.treatment_plant         (man_made=water_works OR water=wastewater)
water.reservoir               (landuse=reservoir OR natural=water + reservoir tag)
transport.railway             (railway=rail)
```

Class list is intentionally short for the pilot. Extending is a YAML edit.

## Modeling approach

Two stages, each with a documented baseline and an optional foundation-model variant. The baseline must work on its own; the foundation-model variant is a credibility-builder, not a dependency.

### Stage 1 — fuel-class segmentation

**Task.** Pixel-level segmentation of Sentinel-2 imagery into a coarse fuel-class taxonomy aligned to ICNF's Carta de Combustíveis Florestais and crosswalked to Scott & Burgan FBFM40 for international readability. Target: ~6–8 classes (e.g., conifer-closed, conifer-open, broadleaf-closed, broadleaf-open, shrub-tall, shrub-low, grass, non-fuel).

**Baseline (must ship).** SegFormer-B0 trained from a small labeled subset derived from ICNF's existing fuel raster as a weak label, with a pre-fire / post-fire NDVI/NBR delta as auxiliary input. Pure PyTorch + `transformers`, no TerraTorch. This is the reproducibility floor.

**Foundation-model variant (nice-to-have).** Prithvi-EO 2.0 or Clay v1.5, fine-tuned with LoRA via TerraTorch. Documented as a comparison: same val split, same metrics, side-by-side IoU and class-confusion table. Demonstrates current-EO-ML literacy without making the pipeline depend on it.

### Stage 2 — per-asset feature extraction + exposure score

For each OSM asset, buffer by a class-specific radius (e.g., 30 m for power lines, 100 m for substations, 50 m for fire stations), extract:

| Feature | Source | Aggregator |
|---|---|---|
| `fuel_class_dominant` | Stage-1 output | mode |
| `fuel_class_severity_weight` | crosswalk table | weighted mean of per-class severity scores |
| `canopy_height_max_m` | ETH GCH | max |
| `canopy_height_p90_m` | ETH GCH | 90th percentile |
| `slope_mean_deg` | DEM | mean |
| `slope_max_deg` | DEM | max |
| `aspect_southness` | DEM | sin/cos transform, mean |
| `historical_burn_count_25y` | ICNF Áreas Ardidas | count of polygons intersecting the buffer in last 25 years |
| `historical_burn_share` | ICNF Áreas Ardidas | fraction of buffer area burned in last 25 years |
| `nbr_delta_recent` | Sentinel-2 spring vs late-summer | mean |
| `fwi_p95_recent_season` | IPMA | 95th percentile (optional) |

The composite exposure score is a transparent linear combination of normalized features with documented weights. No black-box ensemble. The point is auditability: any utility analyst should be able to read the score formula in five lines of YAML.

```yaml
# config/exposure_score.yaml — illustrative
weights:
  fuel_class_severity_weight: 0.30
  canopy_height_p90_m: 0.20
  slope_max_deg: 0.10
  historical_burn_share: 0.20
  nbr_delta_recent: 0.10
  fwi_p95_recent_season: 0.10
normalization: percentile_rank_within_aoi
```

Calibration is reported, not promised. We publish a calibration plot against the historical-burn validation, not a probability claim.

## Validation

The killer feature. Operates entirely on public data with no leakage:

1. Freeze the OSM snapshot at a date `T₀` (default: 2017-01-01).
2. Compute exposure scores using only EO data prior to `T₀`.
3. Take ICNF burned-area polygons from `T₀` to present.
4. Compute, per asset class:
   - **Lift chart** — P(asset intersects burn zone | top-decile score) / P(asset intersects burn zone | random).
   - **Spearman rank correlation** — exposure rank vs. nearest-burn distance.
   - **Brier score** for binary "asset-in-burned-area" prediction (post-calibration).
   - **Class-stratified confusion** — separately for each infrastructure class.

Results land in `docs/validation_report.md` and are regenerated by `make validate`. No fudging — even a negative result is a publishable result with a clear narrative ("this signal works for transmission corridors but not for emergency-services siting because…").

## Stack

### Pinned

```
Python 3.11
uv (lockfile)
PyTorch 2.4 + CUDA 12.1
TorchGeo >= 0.6
TerraTorch >= 0.5 (optional path)
transformers >= 4.45 (SegFormer baseline)
pystac-client >= 0.8
stackstac >= 0.5
odc-stac >= 0.3
rioxarray, rasterio, xarray, dask
geopandas >= 1.0, shapely 2.x
duckdb (for GeoParquet workflows)
fastapi, asyncpg, sqlalchemy 2.x async
alembic
pgstac-py (catalog ingestion, optional)
pytest, hypothesis, ruff, pyright, pre-commit
```

### Why these and not others

- **STAC + stackstac/odc-stac** is the modern best practice for satellite ingestion. Eliminates manual scene download, makes provenance auto-citable. DevSeed-aligned.
- **TorchGeo** for samplers/transforms is canonical. We're not reinventing tile-grid logic.
- **TerraTorch** is included as the modern path for foundation-model fine-tuning, but the project does not depend on it. The SegFormer baseline ships first.
- **GeoParquet** for vector outputs (not Shapefile, not GeoPackage as primary). Modern, columnar, plays with DuckDB and the broader Lake stack.
- **COG everywhere** for rasters. Cloud-friendly, range-readable, STAC-compatible.
- **No PostGIS for the pilot.** Asset volumes are small (<100k features at AOI scale). DuckDB + GeoParquet is faster, simpler, and zero-infra. PostGIS is documented as the production path in `docs/scaling.md`.
- **No live API for the pilot.** Optional `serve` command using FastAPI + DuckDB if needed for a demo, but the primary deliverable is the reproducible repo + STAC catalog + GeoParquet table.

## Outputs

Every output is a public, machine-readable, schema-validated file. No custom binary formats.

```
outputs/
├── stac/                                # STAC 1.1 catalog
│   ├── catalog.json
│   ├── fuel-class/                      # collection: per-tile fuel-class COGs
│   ├── exposure-raster/                 # collection: per-tile exposure-score COGs
│   └── exposure-assets/                 # collection: per-run asset GeoParquet
├── cogs/                                # raster outputs (matched by STAC items above)
│   ├── fuel_class_<run_id>.tif
│   └── exposure_<run_id>.tif
├── parquet/
│   └── exposure_assets_<run_id>.parquet # GeoParquet, one row per asset, full provenance
└── docs/
    ├── validation_report.md
    └── data_lineage.md
```

### Provenance schema (every scored asset row)

```python
{
  "asset_id": "osm:way/12345678",
  "asset_class": "power.transmission_line",
  "geometry": "<WKB, EPSG:4326>",
  "buffer_radius_m": 30,
  "fuel_class_dominant": "shrub-tall",
  "fuel_class_severity_weight": 0.72,
  "canopy_height_p90_m": 11.4,
  "slope_max_deg": 24.1,
  "historical_burn_count_25y": 2,
  "exposure_score": 0.81,
  "score_components": {...},  # all normalized features
  "provenance": {
    "model_id": "fuel-class-segformer-b0",
    "model_version": "0.3.1",
    "sentinel_2_stac_ids": ["S2A_MSIL2A_..."],
    "sentinel_1_stac_ids": ["..."],
    "dem_stac_id": "cop-dem-glo-30/...",
    "canopy_stac_id": "eth-gch-2020/...",
    "icnf_burns_vintage": "2024-12",
    "osm_snapshot_iso": "2024-12-01T00:00:00Z",
    "code_commit_sha": "abcd1234",
    "run_id": "2026-05-05T14-22-03Z-x7p2",
    "config_sha": "..."
  }
}
```

This schema is the load-bearing artifact. If the schema validates, the result is publishable.

## Repository layout

```
wildfire-exposure-eo/
├── README.md                       # this file
├── CLAUDE.md                       # Claude Code session conventions, must-read first
├── PRE_DEV_CHECKLIST.md            # to be completed before development starts
├── pyproject.toml                  # uv-managed, pinned
├── uv.lock
├── inventory.yaml                  # source of truth for STAC catalog (cheias-pt-stac pattern)
├── data/
│   ├── aoi/pilot.geojson           # frozen pilot AOI
│   ├── taxonomy/critical_infrastructure.yaml
│   └── crosswalks/
│       ├── icnf_to_scott_burgan.yaml
│       └── osm_tags.yaml
├── config/
│   ├── exposure_score.yaml
│   └── model_configs/
├── src/wildfire_exposure_eo/
│   ├── osm.py                      # Overpass query + GeoParquet writer
│   ├── stac.py                     # pystac-client wrappers, deterministic ordering
│   ├── imagery.py                  # stackstac loaders, AOI clipping
│   ├── canopy.py                   # ETH GCH + Meta CH loaders
│   ├── dem.py                      # DEM features (slope, aspect, TPI)
│   ├── burns.py                    # ICNF + EFFIS loaders + burn-history features
│   ├── fuel/
│   │   ├── baseline_segformer.py
│   │   └── foundation_terratorch.py
│   ├── features.py                 # per-asset feature extraction
│   ├── score.py                    # composite exposure score
│   ├── validation.py               # lift, Spearman, Brier
│   ├── catalog.py                  # STAC catalog construction
│   └── cli.py                      # `wildfire-exposure-eo <command>`
├── scripts/
│   ├── 00_fetch_osm.py
│   ├── 01_query_stac.py
│   ├── 02_train_fuel.py
│   ├── 03_score_assets.py
│   ├── 04_validate.py
│   └── 05_build_catalog.py
├── notebooks/                      # exploratory only, kept out of the critical path
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── golden/                     # frozen-output regression tests
│   └── conftest.py
├── docs/
│   ├── data_sources.md             # detailed evaluation, links
│   ├── methodology.md              # the long-form spec
│   ├── validation_report.md        # generated by `make validate`
│   ├── scaling.md                  # PostGIS / production path
│   └── limitations.md              # honest scope boundaries
├── prompts/                        # canonical Claude Code prompts per work-unit
│   ├── 01_data_audit.md
│   ├── 02_train_fuel_baseline.md
│   ├── 03_train_fuel_foundation.md
│   ├── 04_score_assets.md
│   └── 05_validate.md
├── stac/                           # generated STAC catalog (committed)
└── .github/workflows/
    ├── ci.yml                      # ruff, pyright, pytest, stac-validator
    └── validate-catalog.yml        # on STAC change, validate
```

## CLI surface

```bash
uv run wildfire-exposure-eo audit            # data-availability check, no compute
uv run wildfire-exposure-eo fetch-osm        # OSM Overpass query → GeoParquet
uv run wildfire-exposure-eo query-stac       # STAC item resolution → manifest
uv run wildfire-exposure-eo train-fuel       # fuel-class segmentation training
uv run wildfire-exposure-eo score            # per-asset feature extraction + scoring
uv run wildfire-exposure-eo validate         # historical-burn validation
uv run wildfire-exposure-eo build-catalog    # STAC catalog assembly
uv run wildfire-exposure-eo demo             # end-to-end on pilot AOI, pretrained checkpoints
```

`demo` is the headline: a single-command end-to-end run on a fresh clone with pretrained checkpoints, target wall-clock under 30 minutes on CPU.

## Definition of done

- Public repo, MIT license, green CI on `main`.
- `uv run wildfire-exposure-eo demo` runs end-to-end on a fresh clone in under 30 minutes on CPU using pretrained checkpoints.
- A STAC 1.1 catalog under `stac/` validates with `stac-validator --recursive`.
- A GeoParquet asset table under `outputs/parquet/` validates against the documented schema.
- `docs/validation_report.md` reports lift, Spearman, and Brier against ICNF burns 2017–24, with a calibration plot.
- `docs/limitations.md` enumerates honest scope boundaries.
- Training is reproducible on `atlas` (RTX 3090); training run log + metrics committed under `docs/training_runs/`.
- `CLAUDE.md` enforced by CI and pre-commit (see [`CLAUDE.md`](CLAUDE.md)).

## What this signals

For a hiring manager at Overstory or DevSeed-style EO consultancies, this repo is meant to demonstrate, in order:

1. **Operational understanding** — span/asset-level scoring with full provenance, not pixel art.
2. **Modern EO best practice** — STAC-native, COG-only outputs, GeoParquet vector outputs, foundation-model literacy with classical baselines.
3. **Engineering discipline** — `uv`, `ruff`, `pyright`, alembic-style migrations (where relevant), pinned deps, schema validation, deterministic runs, golden-file regression tests.
4. **Domain literacy** — ICNF fuel taxonomy, Scott & Burgan crosswalk, Portuguese fire context, OSM critical-infrastructure taxonomy as a citable artifact.
5. **Honest scope** — clearly bounded pilot, transparent score formula, validation against held-out historical outcomes, explicit limitations.

## License

MIT. See [`LICENSE`](LICENSE).

## Citation

If you reference this in a talk, paper, or hiring conversation:

```
Silvestre, N. (2026). wildfire-exposure-eo: a STAC-native pipeline for
scoring critical infrastructure by wildfire exposure.
https://github.com/lunasilvestre/wildfire-exposure-eo
```
