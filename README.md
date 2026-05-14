# wildfire-exposure-eo

A STAC-native pipeline that scores OpenStreetMap-derived critical infrastructure by wildfire exposure for a Portuguese pilot AOI, combining Sentinel-2 fuel-class mapping, canopy-height features, ICNF burned-area history, and topographic + fire-weather priors. Built on public Earth Observation data with full provenance per asset, validated against historical burn polygons.

> **Status.** Pre-development. See [`PRE_DEV_CHECKLIST.md`](PRE_DEV_CHECKLIST.md) before opening a Claude Code session. See [`CLAUDE.md`](CLAUDE.md) for session conventions.

## Why this exists

Critical infrastructure — power lines, substations, transformers, water-treatment plants, telecom towers, hospitals, schools, emergency-services facilities — is unevenly exposed to wildfire risk. Static hazard maps published by national agencies tend to be land-cover-driven, slow to update, and asset-agnostic. This project produces a per-asset wildfire-exposure score, derived from current-year EO data and validated against historical burn outcomes, scoped initially to a single Portuguese fire district as a methodology demonstrator.

The methodology generalizes nationally and beyond. The pilot is intentionally small so that the demo target — end-to-end on a fresh clone in *target wall-clock* under 30 minutes on CPU with pretrained checkpoints — is achievable. Some stages (foundation-model fuel-class fine-tuning, full Prithvi-Burn-Scar inference) run on the `atlas` GPU host and are skipped or replaced with pre-baked artefacts in the CPU demo; see [`docs/methodology.md`](docs/methodology.md).

## What this is not

- Not a fire-spread or fire-behaviour simulation. We score relative exposure, not absolute probability.
- Not a replacement for commercial utility vegetation management products. Those use sub-meter commercial imagery, proprietary canopy-instance segmentation, and customer-validated ground truth. This is a public-data demonstrator that mirrors the *operational shape* of those products under open-data constraints.
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

**Sever do Vouga / Albergaria-a-Velha / Oliveira de Azeméis** (Aveiro, PT-01) — bbox `[-8.598, 40.605, -8.242, 40.875]`, ~30 × 30 km. Chosen for documented historical burn frequency, mixed eucalyptus / Pinus pinaster / shrubland cover typical of the Atlantic Centro-Norte regime, dense REN + distribution OSM coverage, and sparse population. See `docs/aoi_rationale.md` for the full justification and the three alternatives retained on disk. Frozen as a single GeoJSON in `data/aoi/pilot.geojson` so every artifact references the same geometry.

A 1 × 1 km smoke AOI under `data/aoi/smoke.geojson`, centred on Sever do Vouga town, is the development-loop target.

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
| DGT COSc 2023/2024 | DGT (SMOS, Sentinel-2 ML pipeline) | 10 m raster, CC-BY 4.0 | **PRIMARY** — coarse fuel-cover weak labels (4 classes) for fuel-class training |
| DGT COS 2018 / 2023 | DGT INSPIRE | GeoPackage, CC-BY 4.0 | **PRIMARY** — species-level fine labels (Pinus, Eucalyptus, Quercus splits) |
| EFFIS European Fuel Map | JRC / Copernicus | GeoTIFF, free | **REFERENCE** — international NFFL-13 crosswalk anchor |
| Scott & Burgan FBFM40 | USFS / LANDFIRE | reference document | International fuel-model framework |
| ICNF Áreas Ardidas | ICNF (Instituto da Conservação da Natureza e Florestas) | annual polygons (Shapefile/GPKG) | Validation ground truth, history feature |
| ICNF Carta de Combustíveis Florestais | ICNF | raster | **FUTURE** — national alignment target; no public direct-download URL (2026-05-07) |
| EFFIS Burned Area | JRC / Copernicus | annual polygons | Cross-border validation, EU-wide future scope |
| VIIRS NRT Active Fire | NASA FIRMS | CSV / GeoJSON | Recent fires (optional, contextual) |
| IPMA Daily FWI | IPMA | grid | Fire-weather multiplier (optional) |

The full taxonomy chain — DGT COSc + COS (operational inputs) → 9 internal classes (model output) → ICNF CCF (future alignment) + NFFL-13 via EFFIS (international reference) + FBFM40 (fire-behaviour modelling) — is documented in [`data/crosswalks/icnf_to_scott_burgan.yaml`](data/crosswalks/icnf_to_scott_burgan.yaml).

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

**Task.** Pixel-level segmentation of Sentinel-2 imagery into the project's 9-class internal fuel taxonomy (`non-fuel`, `grass`, `shrub-low`, `shrub-tall`, `broadleaf-open`, `broadleaf-closed`, `conifer-open`, `conifer-closed`, `mixed-forest`), crosswalked to NFFL-13 via EFFIS for international readability and to Scott & Burgan FBFM40 for fire-behaviour modelling. ICNF's Carta de Combustíveis Florestais is the alignment target once obtainable, but is *not* the training input — see `data/crosswalks/icnf_to_scott_burgan.yaml` for the full chain.

**Baseline (must ship).** SegFormer-B0 trained on weak labels combining (a) DGT COSc 2024 4-class fuel-cover for the fuel / non-fuel and shrub / forest splits, (b) DGT COS 2023 species codes for the broadleaf / conifer / mixed and Pinus / Eucalyptus / Quercus splits, with Sentinel-2 NDVI/NBR seasonal delta + Sentinel-1 cross-pol ratio as auxiliary input features for canopy openness. Pure PyTorch + `transformers`, no TerraTorch. This is the reproducibility floor.

**Foundation-model variant (nice-to-have).** Prithvi-EO 2.0 or Clay v1.5, fine-tuned with LoRA via TerraTorch. Documented as a comparison: same val split, same metrics, side-by-side IoU and class-confusion table. Demonstrates current-EO-ML literacy without making the pipeline depend on it.

### Stage 1b — burn-scar inference (recent burns)

**Task.** Detect burn scars in Sentinel-2 imagery over the past 12 months across the AOI, producing a per-pixel burn-probability raster. This fills the gap between the latest published [ICNF Áreas Ardidas](docs/data_sources.md) vintage (~1-year lag) and "right now"; it captures the current fire season the historical layer cannot.

**Model.** [Prithvi-EO 2.0](https://github.com/NASA-IMPACT/Prithvi-EO-2.0) with the burn-scar downstream task — the canonical reference application of the model family and the one with the most public validation. Fine-tune is invoked via TerraTorch with a YAML config; backbone weights are frozen for inference-only operation. The exact Hugging Face model ID is verified at audit time and recorded in the provenance dict (see CLAUDE.md non-negotiable #1).

**Output.** A burn-probability COG over the AOI, threshold-binarised for the per-asset `recent_burn_share_12mo` feature in Stage 2.

**What this is not.** Not ignition prediction. We detect burn *scars* — visible post-event spectral signatures — not forecasts of where fires will start. See CLAUDE.md anti-patterns.

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
| `recent_burn_share_12mo` | **Stage 1b — Prithvi-Burn-Scar → S2** | fraction of buffer area flagged as burned in past 12 months |
| `nbr_delta_recent` | Sentinel-2 spring vs late-summer | mean |
| `fwi_p95_recent_season` | IPMA | 95th percentile (optional) |

The composite exposure score is a transparent linear combination of normalized features with documented weights. No black-box ensemble. The point is auditability: any utility analyst should be able to read the score formula in five lines of YAML.

Materialised in [`config/exposure_score.yaml`](config/exposure_score.yaml):

```yaml
# config/exposure_score.yaml
weights:
  fuel_class_severity_weight: 0.30
  canopy_height_p90_m: 0.20
  slope_max_deg: 0.10
  historical_burn_share: 0.15        # decadal pattern (ICNF, ~1-yr lag)
  recent_burn_share_12mo: 0.10       # current season (Prithvi-Burn-Scar, monthly)
  nbr_delta_recent: 0.05
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

- **STAC + stackstac/odc-stac** is the modern best practice for satellite ingestion. Eliminates manual scene download, makes provenance auto-citable.
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
- Target: `uv run wildfire-exposure-eo demo` runs end-to-end on a fresh clone in under 30 minutes on CPU using pretrained checkpoints distributed as GitHub release attachments. The foundation-model variant and full Prithvi-Burn-Scar inference are gated behind explicit flags and `atlas` GPU access — see [`docs/methodology.md`](docs/methodology.md) → "Demo command, 30-minute CPU budget".
- A STAC 1.1 catalog under `stac/` validates with `stac-validator --recursive`.
- A GeoParquet asset table under `outputs/parquet/` validates against the documented schema.
- `docs/validation_report.md` reports lift, Spearman, and Brier against ICNF burns 2017–24, with a calibration plot.
- `docs/limitations.md` enumerates honest scope boundaries.
- Training is reproducible on `atlas` (RTX 3090); training run log + metrics committed under `docs/training_runs/`.
- `CLAUDE.md` enforced by CI and pre-commit (see [`CLAUDE.md`](CLAUDE.md)).

## Acknowledgments

This repo is heavily AI-augmented. Most of the prose, the YAML, the documentation scaffolding, and a lot of the structural code came out of Claude Code sessions. The decisions — pilot AOI, taxonomy boundaries, score formula, *"drop the upper-bound pins"*, *"the README sounds like job-application bait, fix it"*, *"Prithvi burn-scar is not ignition prediction"* — came out of me pushing back on it.

Think of it as pair programming where one partner never sleeps and the other one has the taste. The [`CLAUDE.md`](CLAUDE.md) in this repo is longer than most production READMEs because that's where the actual contract lives: the AI does the verbose work, the human keeps judgment.

If *"AI slop"* is hovering in the back of your mind while reading this — fair. The CI gates (`ruff`, `pyright`, schema validation, weights-sum-to-1.0 assertion), the verify-then-act protocol, and the anti-pattern list in [`CLAUDE.md`](CLAUDE.md) are there for exactly that reason. They don't care who typed which character; they care whether the thing is right.

*(Yes, Claude wrote this acknowledgments section too. The recursion is intentional.)*

## License

MIT. See [`LICENSE`](LICENSE).

## Citation

If you reference this in a talk, paper, or write-up:

```
Silvestre, N. et Claude (Anthropic) (2026). wildfire-exposure-eo: a STAC-native pipeline for
scoring critical infrastructure by wildfire exposure.
https://github.com/lunasilvestre/wildfire-exposure-eo
```
