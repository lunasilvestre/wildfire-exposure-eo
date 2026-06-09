# Roadmap — what exists, what remains

> Companion to [`prompts/00_CLOSEOUT_PLAN.md`](../prompts/00_CLOSEOUT_PLAN.md)
> (the executable direction). This document is the human-readable picture.
> Status date: 2026-06-09, post-WU-0 (CI green on `main`).

## The narrative, in three sentences

Critical infrastructure in Portuguese fire districts — schools, substations,
water-treatment plants, fire stations — is unevenly exposed to wildfire, and
the public hazard maps are land-cover-driven, slow, and asset-agnostic. This
repo ranks every OSM-mapped asset in a pilot district by relative wildfire
exposure using only open data (Sentinel-2, EFFIS fuels, COS land cover,
canopy height, ICNF burn history), with per-asset provenance and validation
against two decades of real burn perimeters. It is a civic-tech
demonstrator: any município, civil-protection office, or researcher can
re-run it on a fresh clone, swap the AOI polygon, and get the same artifacts
for their own district.

## What exists today

```mermaid
flowchart LR
    subgraph done["Shipped (CI-gated, on main)"]
        A["audit\n9/9 sources GREEN"] --> B["resolve-stac\nS2/S1/DEM/WorldCover\nmanifests, deterministic"]
        B --> C["schemas/\nPydantic v2, frozen"]
        C --> D["46 tests\nunit + integration"]
    end
    D --> E["CI green on main\n3 jobs, June 9"]
```

Plus the non-code substrate: frozen AOI (`data/aoi/pilot.geojson`, Sever do
Vouga ~30×30 km), infrastructure taxonomy (13 classes), fuel crosswalk
(EFFIS/COS → Scott & Burgan), verified fetch scripts for every source, and
prompts 01–05 + 09 as executable work-unit specs.

## Where it's going (WU sequence)

```mermaid
flowchart TD
    WU0["WU-0 ✅ repo live, CI green"] --> WU1["WU-1 ⚡ Prithvi burn-scar COG\npretrained, atlas RTX 3090"]
    WU1 --> WU2["WU-2 OSM asset extract"]
    WU1 --> WU3["WU-3 static rasters\nEFFIS · COS · canopy · DEM"]
    WU1 --> WU4["WU-4 ICNF burns 1975–2025"]
    WU3 --> WU5["WU-5 fuel layer\nreclass, no ML"]
    WU2 --> WU6["WU-6 per-asset features\n→ exposure RANK"]
    WU5 --> WU6
    WU4 --> WU6
    WU1 -.recent_burn_share_12mo.-> WU6
    WU6 --> WU7["WU-7 validation\nlift / Spearman vs ICNF"]
    WU4 --> WU7
    WU7 --> WU8["WU-8 maps + story\nranked assets on S2 imagery"]
```

(Repo edits are strictly sequential — the DAG above shows *data*
dependencies, not session parallelism. See the concurrency rule in the
close-out plan.)

## The final artifact set

| Artifact | Format | Audience |
|---|---|---|
| Ranked asset table | GeoParquet + STAC | analysts, downstream tools |
| Exposure + fuel + burn-scar rasters | COG | GIS users |
| `validation_report.md` | Markdown, reproducible numbers | reviewers |
| Static map figures + one interactive HTML map | PNG / self-contained HTML in `docs/figures/` | everyone — this is the ten-second proof |
| 30-min CPU demo path (`--smoke`) | CLI | anyone with a laptop |

## What this is **not** (unchanged)

No fire-spread simulation, no probability claims (ranks only), no
fine-tuning or training of any model, no private operator data, no
production claims. Future-work notes may mention foundation-model upgrades
(e.g. TerraMind) in one paragraph — they are not on any path here.

<!-- maintained alongside prompts/00_CLOSEOUT_PLAN.md; update both or neither -->
