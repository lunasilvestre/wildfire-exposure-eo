# AOI rationale

> **Decision (2026-05-07)**: Pilot AOI is **Aveiro / Sever do Vouga** (`data/aoi/pilot.geojson`, mirrored as `data/aoi/alt_aveiro_sever_do_vouga.geojson`). Three alternatives are retained on disk for re-selection if the data audit fails on the pilot.

This document closes [PRE_DEV_CHECKLIST.md](../PRE_DEV_CHECKLIST.md) item **D — AOI freeze**.

## Candidates considered

Each candidate is a 30 × 30 km bbox (the legacy 2017 AOI is ~52 × 55 km, retained as historical precedent) anchored on a post-2020 mega-fire signature in continental Portugal. All four files are committed under `data/aoi/`.

| Slug | Anchor event | Year | Burned area | District | File |
|---|---|---|---|---|---|
| `pedrogao_grande` | Pedrógão Grande + Pampilhosa complex | 2017 | ~500,000 ha (national) | PT-10 Leiria | `alt_pedrogao_grande.geojson` |
| `serra_da_estrela` | Serra da Estrela / Manteigas fire | 2022 | ~25,000 ha | PT-09 Guarda | `alt_serra_da_estrela.geojson` |
| `aveiro_sever_do_vouga` | Aveiro Sept-2024 complex | 2024 | ~47,000 ha (district) | PT-01 Aveiro | `alt_aveiro_sever_do_vouga.geojson` ← chosen |
| `peneda_geres` | PNPG fire | 2025 | part of >200,000 ha season | PT-16 Viana do Castelo | `alt_peneda_geres.geojson` |

## Selection criteria

The AOI must serve three downstream consumers: the **data audit** (Sentinel-2/-1, Cop-DEM, ETH GCH, OSM, ICNF, ESA WorldCover all GREEN), the **modelling pipeline** (enough fuel-class diversity, enough recent burn pixels for supervised signal), and the **operational narrative** — strike-tree / hazard-tree framing on power infrastructure, the standard utility-VM idiom.

## Comparison

| Criterion | Pedrógão (2017) | Serra Estrela (2022) | **Aveiro (2024)** | Peneda-Gerês (2025) |
|---|---|---|---|---|
| Recent severe burn signal | ⚠ 9-year-old | ✓ | **✓** | ✓ very recent |
| Wildland-urban interface (WUI) | ✓✓ | ✓ (low pop) | **✓✓✓** (residential damage) | ✓ |
| Power-infra exposure (e-Redes / REN) | ✓✓ | ✓ | **✓✓✓** (dense Centro-Norte grid) | ✓ (sparse) |
| OSM critical-infra coverage | ✓✓ (validated) | ✓ | **✓✓** | ⚠ thinner |
| Fuel mix relevance | mixed pine/eucalyptus | shrub + pine | **eucalyptus-heavy** (Portuguese signature fuel) | granite + shrub |
| Copernicus EMS reference perimeter | partial | ✓ EMSR618 | partial | ✓ activated |
| Narrative freshness | dated | iconic | **current + actionable** | recent but remote |
| ICNF Áreas Ardidas density | ✓✓✓ | ✓✓ | **✓✓** | ✓ |

## Why Aveiro / Sever do Vouga

The utility-VM framing — *strike trees, hazard trees, span-level vegetation risk on power infrastructure* — needs an AOI where (a) recent burn perimeters intersect (b) dense distribution and transmission grids inside (c) inhabited terrain. Aveiro 2024 is the only candidate that scores ✓✓ or better on all three. It is also the most defensible "fresh" demonstrator AOI — recent enough that the data layers are abundant, old enough that ICNF perimeters are finalised. Eucalyptus dominance gives the fuel-class crosswalk a meaningful workout (FBFM40 GR/SH/TL classes will all be exercised); the WUI damage gives the model a concrete exposure target.

The other three remain valuable as **transfer-learning checkpoints** once the pilot pipeline runs end-to-end:
- **Pedrógão Grande (2017)** — the long-tail historical baseline; ICNF density makes it the natural choice for back-testing.
- **Serra da Estrela (2022)** — protected-area regime, EMSR618 perimeters give a clean held-out test set.
- **Peneda-Gerês (2025)** — distribution-shift probe (granite/shrub regime, Norte-mountain weather drivers).

## Risks accepted with this choice

1. **Cloud cover on Sentinel-2 over Aveiro is structurally higher than over the Centro interior** (Atlantic proximity). The audit step must verify ≥ 50 cloud-free items in the past 24 months — if RED, fall back to `alt_serra_da_estrela.geojson` first, then `alt_pedrogao_grande.geojson`.
2. **2024 ICNF Áreas Ardidas perimeters may not yet be in the canonical annual release** (publication lag). If the audit reports missing 2024 burns, supplement with EFFIS rapid-mapping perimeters and document in `docs/data_sources.md`.
3. **OSM `power=*` density in Aveiro is good but not validated** — `scripts/00_overpass_smoke.py` was originally tuned to the Pampilhosa bbox. Re-run the smoke probe against the new `smoke.geojson` before declaring item C green.

## Smoke tile

`data/aoi/smoke.geojson` is a ~1 × 1 km tile centred on Sever do Vouga town (40.733°N, −8.367°W), inside the pilot AOI. Geometry mirrors `smoke_aveiro_sever_do_vouga.geojson`. Each alternative AOI ships a matching `smoke_<slug>.geojson` so that switching AOIs is a single CLI flag, not a re-tile job:

- `smoke_pedrogao_grande.geojson` — Pedrógão Grande town
- `smoke_serra_da_estrela.geojson` — Manteigas town
- `smoke_aveiro_sever_do_vouga.geojson` — Sever do Vouga town (= `smoke.geojson`)
- `smoke_peneda_geres.geojson` — Lindoso village (PNPG)

## Provenance

- 2026-05-07 — Initial draft, AOI alternatives generated, pilot moved from Pedrógão to Aveiro.
- Earlier: pilot.geojson originally targeted Pampilhosa da Serra / Pedrógão Grande based on `scripts/00_overpass_smoke.py` validation. Now archived as `alt_pedrogao_grande.geojson`.
