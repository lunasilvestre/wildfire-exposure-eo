# data/aoi — AOI files

All GeoJSON files in this directory use CRS84 (WGS 84, `urn:ogc:def:crs:OGC:1.3:CRS84`,
coordinates in longitude/latitude order). CRS is declared explicitly in every file.

## Frozen pilot AOI

| File | Region | Event | Area |
|---|---|---|---|
| `pilot.geojson` | Sever do Vouga / Albergaria-a-Velha (Aveiro) | September 2024 fire complex (~47,000 ha district) | ~30 × 30 km (~902 km²) |

**Do not modify `pilot.geojson`.** It is frozen per CLAUDE.md non-negotiable #10 and
`docs/aoi_rationale.md`. `alt_aveiro_sever_do_vouga.geojson` mirrors the pilot geometry
and is retained as an archive.

## Validation AOIs (WU-18 Pillar 2)

Added 2026-06-16 for widened validation (see `docs/operationalization.md` §5b and
`prompts/18_widen_validation.md`). Each is **bigger than the pilot** and anchors on a
major mainland-Portugal fire with ICNF burned-area coverage. Purpose: push N(burned
assets) into the dozens so lift / Spearman become interpretable.

Each AOI ships a matching `smoke_<slug>.geojson` (~1 km × 1 km tile with verified OSM
critical-infra and ICNF burn intersection — or DRAFT if unverified).

### 1. `pedrogao_grande.geojson` — Pedrógão Grande / Pampilhosa da Serra

- **Anchor event**: June–October 2017 mega-fire complex (66 fatalities; ~30,000 ha in
  the Pedrógão event; ~45,000 ha in the full June–October complex)
- **Area**: ~51 × 56 km (~2,846 km²) — intentionally oversized to capture the full
  complex extent
- **Stress axis**: **Large historical scar + scale** — the largest, oldest, densest
  ICNF burned-area archive in continental Portugal; the biggest single lever on N(burned
  assets) in the validation set
- **Copernicus EMS**: partial activation
- **ICNF coverage**: dense, finalised annual release
- **Source of fire-area figure**: ICNF Áreas Ardidas
  (<https://www.icnf.pt/florestas/gfr/gfrint/ifiarf>, accessed 2026-06-16)
- **Promoted from**: `alt_pedrogao_grande.geojson` (bbox unchanged)

### 2. `serra_da_estrela.geojson` — Serra da Estrela (Manteigas / Covilhã / Guarda)

- **Anchor event**: August 2022 fire — ~25,000 ha, Portugal's largest single fire in
  50 years; Copernicus EMS EMSR618
- **Area**: ~41 × 41 km (~1,678 km²)
- **Stress axis**: **High-mountain / protected-area regime** — distinct biome (shrub +
  pine), different fire-weather drivers, EMSR618 as independent ground-truth perimeter
- **Copernicus EMS**: **EMSR618** (<https://emergency.copernicus.eu/mapping/list-of-components/EMSR618>)
- **ICNF coverage**: strong, finalised
- **Source of fire-area figure**: ICNF Áreas Ardidas + Copernicus EMS EMSR618
  (accessed 2026-06-16); never averaged with EFFIS (different methodology)
- **Promoted from**: `alt_serra_da_estrela.geojson` — bbox expanded west to −7.85°
  and south to 40.15° (from original −7.727° / 40.215°) to include Covilhã and fuller
  western slope, making the AOI clearly larger than the pilot

### 3. `peneda_geres.geojson` — Peneda-Gerês National Park (Viana do Castelo / Braga flank)

- **Anchor event**: July–August 2025 Lindoso/Ponte da Barca fire (~5,800 ha in-park;
  ~7,500 ha perimeter — **PRELIMINARY ICNF figures, publication lag expected**)
- **Area**: ~39 × 35 km (~1,345 km²)
- **Stress axis**: **Norte-mountain distribution shift** — granite/shrub regime,
  Portugal's only national park, different fire-weather drivers from the Centro pilot;
  tests score transferability
- **Copernicus EMS**: activation pending verification (check Copernicus EMS for 2025
  PNPG activation code)
- **ICNF coverage**: **preliminary** — 2025 figures not yet in canonical annual release;
  supplement with EFFIS rapid-mapping if needed; document source explicitly
- **Source of fire-area figure**: ICNF preliminary 2025 season data
  (<https://www.icnf.pt/florestas/gfr/gfrint/ifiarf>, accessed 2026-06-16); national
  season total >200,000 ha
- **Promoted from**: `alt_peneda_geres.geojson` — bbox expanded west to −8.52°, south
  to 41.68°, east to −8.05° (from original −8.452° / 41.715° / −8.087°) to include
  Arcos de Valdevez and Ponte da Barca

### 4. `monchique.geojson` — Serra de Monchique / Silves (Algarve WUI) — ⚠ DRAFT BBOX

- **Anchor event**: August 2018 fire — ~27,200 ha; Copernicus EMS EMSR303; threatened
  Monchique town (WUI)
- **Area**: ~40 × 45 km (~1,774 km²) — best-effort estimate
- **Stress axis**: **Wildland-urban interface** (documented threat to a populated
  centre) + **Algarve Mediterranean regime** — different climate zone from all other
  AOIs; exercises the score on thermophilous shrub + eucalyptus + cork-oak fuel mix
- **Copernicus EMS**: **EMSR303** (<https://emergency.copernicus.eu/mapping/list-of-components/EMSR303>)
- **ICNF coverage**: strong — 2018 perimeters in canonical annual release
- **Source of fire-area figure**: ICNF Áreas Ardidas 2018
  (<https://www.icnf.pt/florestas/gfr/gfrint/ifiarf>, accessed 2026-06-16); cited from
  ICNF only, not averaged with EFFIS
- **Authored new** — no prior alt file; bbox derived from `docs/operationalization.md`
  §5b DRAFT working value `[-8.72, 37.18, -8.38, 37.45]` and expanded
- **⚠ TODO(provenance)**: Verify final bbox against EMSR303 delineation and ICNF 2018
  Áreas Ardidas before freezing. Current coordinates are best-effort. See also
  `monchique.geojson` internal note.

## Alternative / archive AOIs

These files are retained for reference and backwards compatibility. They are **not**
used by the pipeline — use the canonical names above.

| File | Notes |
|---|---|
| `alt_aveiro_sever_do_vouga.geojson` | Mirrors `pilot.geojson`; kept for historic traceability |
| `alt_pedrogao_grande.geojson` | Superseded by `pedrogao_grande.geojson`; bbox unchanged |
| `alt_serra_da_estrela.geojson` | Superseded by `serra_da_estrela.geojson`; original smaller bbox archived |
| `alt_peneda_geres.geojson` | Superseded by `peneda_geres.geojson`; original smaller bbox archived |

## Smoke tiles

Each validation AOI has a `smoke_<slug>.geojson` (~1 km × 1 km tile) for use with
`wildfire-exposure-eo --smoke`. Smoke tiles are picked to satisfy:
`power_tower ≥ 1 AND building ≥ 1 AND highway ≥ 1 AND intersects_ICNF_burn`.

| File | Status | Picker source |
|---|---|---|
| `smoke.geojson` | Verified | Mirrors `smoke_aveiro_sever_do_vouga.geojson` |
| `smoke_aveiro_sever_do_vouga.geojson` | Verified | `scripts/pick_smoke_tile.py` |
| `smoke_pedrogao_grande.geojson` | Verified | `scripts/pick_smoke_tile.py` |
| `smoke_serra_da_estrela.geojson` | Verified (against original alt bbox; still inside expanded bbox) | `scripts/pick_smoke_tile.py` |
| `smoke_peneda_geres.geojson` | Verified (against original alt bbox; still inside expanded bbox) | `scripts/pick_smoke_tile.py` |
| `smoke_monchique.geojson` | **DRAFT** — centred on Monchique town; picker_score unverified | Manual pick pending `scripts/pick_smoke_tile.py` |
