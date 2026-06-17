# Pipeline diagrams

Three Mermaid diagrams of the shipped pipeline. Every node names a real
module, CLI command, config file, or artefact in this repository — no
aspirational boxes. The geobrowser (`docs/index.html`) renders these same
blocks; GitHub renders them below.

The exposure score shown throughout is a **relative, AOI-normalised screening
rank** — not a probability of fire (CLAUDE.md non-negotiable #6).

## 1. Pipeline DAG (data flow)

<!-- The README architecture section embeds this same diagram. -->

```mermaid
flowchart TD
    subgraph sources["Open data sources"]
        OSM["OSM via Overpass"]
        S2["Sentinel-2 L2A<br>(Planetary Computer STAC)"]
        DEM["Copernicus DEM GLO-30<br>(Planetary Computer STAC)"]
        GCH["ETH Global Canopy Height 2020"]
        EFFIS["EFFIS European Fuel Map"]
        COSC["DGT COSc 2024 land cover"]
        ICNF["ICNF Áreas Ardidas perimeters"]
    end

    OSM -->|"fetch-osm (osm.py)"| ASSETS["osm_assets_&lt;run_id&gt;.parquet"]
    EFFIS -->|"fetch-rasters (static_rasters.py)"| CACHE["data/cache/ static rasters"]
    COSC -->|"fetch-rasters (static_rasters.py)"| CACHE
    GCH -->|"fetch-rasters (static_rasters.py)"| CACHE
    ICNF -->|"fetch-burns (burns.py)"| BURNS["icnf_burns_&lt;run_id&gt;.parquet"]

    CACHE -->|"fuel-layer (fuel.py +<br>config/fuel_crosswalk.yaml)"| FUEL["fuel_class_&lt;run_id&gt;.tif (COG)"]
    S2 -->|"infer-burn-scar (burn_scar.py,<br>Prithvi-EO-2.0-300M-BurnScars, GPU)"| BSCAR["burn_scar_&lt;run_id&gt;.tif (COG)"]

    ASSETS --> FEAT["features.py — per-asset zonal stats<br>(exactextract, class-specific buffers)"]
    FUEL --> FEAT
    BSCAR --> FEAT
    BURNS --> FEAT
    CACHE --> FEAT
    S2 -->|"NBR delta via stac.py resolver"| FEAT
    DEM -->|"slope via stac.py resolver"| FEAT

    FEAT -->|"score (scoring.py +<br>config/exposure_score.yaml v0.3.1)"| EXP["exposure_&lt;run_id&gt;.parquet<br>(GeoParquet, ScoredAsset schema,<br>full per-row provenance)"]
    EXP -->|"scripts/11_validate.py (validation.py:<br>leakage gate, lift, Spearman, ablation)"| VAL["docs/validation_report.md +<br>metrics_&lt;run_id&gt;.json"]
    EXP --> STAC["stac/ catalog<br>(exposure-assets, fuel-layer,<br>burn-scar-recent)"]
    FUEL --> STAC
    BSCAR --> STAC
```

## 2. Reproduction flowchart (CPU demo path + GPU route)

The CPU path mirrors [`docs/demo.md`](demo.md) step by step (~2.5 min
cache-warm on the 1 km² smoke AOI). The GPU route reproduces the pre-baked
burn-scar COG and is **not** required for the demo.

```mermaid
flowchart TD
    START(["fresh clone +<br>uv sync --locked --extra dev"]) --> A1

    subgraph cpu["CPU demo path — docs/demo.md (smoke AOI)"]
        A1["uv run wildfire-exposure-eo audit<br>--aoi data/aoi/smoke.geojson"]
        A1 --> A2["uv run wildfire-exposure-eo fetch-osm --smoke"]
        A2 --> A3["uv run wildfire-exposure-eo fetch-rasters --smoke<br>--only eth-gch,effis,cosc"]
        A3 --> A4["uv run wildfire-exposure-eo fetch-burns --smoke"]
        A4 --> A5["uv run wildfire-exposure-eo fuel-layer --smoke"]
        A5 --> A6["uv run wildfire-exposure-eo score --smoke<br>--window-end 2026-06-09"]
        A6 --> A7["uv run python scripts/11_validate.py --smoke"]
    end

    subgraph gpu["GPU route — atlas host, prompts/09_burn_scar_inference.md"]
        G1["uv run wildfire-exposure-eo infer-burn-scar<br>(burn_scar.py, terratorch, CUDA)"]
        G1 --> G2["pre-baked burn_scar COG under outputs/cogs/<br>+ STAC item under stac/burn-scar-recent/"]
    end

    G2 -.->|"consumed as committed artefact<br>(recent_burn_share_12mo feature)"| A6
    A7 --> DONE(["validation report + metrics JSON"])
```

## 3. Provenance / lineage (one scored row → its exact inputs)

Field values shown are from the published run `20260617T035233Z` (the
backdated pilot run the committed validation report describes — STAC item
`exposure-assets-20260617T035233Z`).

```mermaid
flowchart LR
    ROW["ScoredAsset row<br>(any of the 3045 assets)"] --> PROV["provenance dict<br>(per-row, schema-enforced)"]

    PROV --> RID["run_id<br>20260617T035233Z"]
    PROV --> SHA["code_commit_sha<br>71681fe0508c…"]
    PROV --> MV["model_version 0.3.1 + config_sha<br>→ config/exposure_score.yaml"]
    PROV --> XWALK["crosswalk_sha<br>→ config/fuel_crosswalk.yaml"]
    PROV --> WIN["window_start..window_end<br>2023-12-31..2024-12-31"]
    PROV --> AOI["aoi_geometry_sha<br>→ data/aoi/pilot.geojson"]

    PROV --> OSMSHA["osm_parquet_sha"] --> OSMPQ["osm_assets parquet<br>(fetch-osm output)"]
    PROV --> BURNSHA["burns_parquet_sha"] --> BURNPQ["icnf_burns parquet<br>(fetch-burns output)"]
    PROV --> FUELSHA["fuel_cog_sha"] --> FUELCOG["fuel_class COG<br>→ STAC item fuel-layer-20260611T090120Z"]
    PROV --> GCHSHA["gch_cache_sha"] --> GCHC["ETH GCH cache raster"]
    PROV --> BSSHA["burn_scar_cog_sha = null<br>(backdated run: feature dropped,<br>never leaked)"]

    PROV --> S2IDS["s2_item_ids<br>(56 Sentinel-2 L2A STAC ids)"]
    PROV --> DEMIDS["dem_item_ids<br>Copernicus_DSM_COG_10_N40_00_W009_00_DEM"]

    SHA --> REPRO["git checkout 71681fe0 +<br>re-run score → byte-identical row"]
```
