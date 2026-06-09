# Pre-development checklist

Complete every item below **before** the first development Claude Code session. The point is to refuse to start coding until the data, accounts, hardware, and reference frame are verified — most failure modes in EO/ML projects are caused by skipping this.

Estimated time: **3–4 hours** of focused work.

---

## A. Hardware & local environment

- [x] **`atlas` (RTX 3090) reachable.** SSH, GPU visible to `nvidia-smi`, free disk ≥ 100 GB on training volume.
- [x] **CUDA toolchain.** Driver 595.71.05 (≥ 535), CUDA 13.2 runtime present, `python -c "import torch; print(torch.cuda.is_available())"` returns `True`.
- [x] **Local dev machine.** `uv` installed, `uv --version` ≥ 0.4. `pre-commit` installed system-wide.
- [x] **Disk plan.** Decide where `outputs/` lives. Default: project-local. Confirm at least 50 GB free.
- [x] **VS Code / editor.** Pylance (Pyright LSP) + `charliermarsh.ruff` v2026.40.0 installed; `.vscode/settings.json` pins interpreter to `.venv/bin/python`, sets Ruff as default Python formatter (format-on-save + fixAll + organizeImports), Pylance type-checking mode `standard`.

## B. Accounts & API access

- [x] **GitHub.** New empty public repo `lunasilvestre/wildfire-exposure-eo` created. Default branch `main`. Branch protection: required PR reviews on, required status checks on once CI exists.
- [x] **Microsoft Planetary Computer.** Free account; verify `pystac-client` can list `sentinel-2-l2a`, `sentinel-1-grd`, `cop-dem-glo-30`, `esa-worldcover` collections. Save a short script under `scripts/00_pc_smoke.py` that queries and exits 0.
- [x] **NASA Earthdata Login.** Required for HLS access via LP DAAC STAC. Token saved in `~/.netrc`.
- [x] **OSM Overpass.** No account needed. Note primary endpoint + at least one fallback.
- [x] **ICNF data download.** Direct-download URLs confirmed (HEAD HTTP 200) and codified in `scripts/00_icnf_fetch.sh`: Áreas Ardidas 1975–2025 via the ICNF ArcGIS REST MapServer, plus RPFGC (fuel-management strips) and Perigosidade Estrutural 2020–2030 as substitutes. The national Carta de Combustíveis Florestais has **no public direct-download URL** (verified 2026-05-07 against geocatalogo.icnf.pt, sigservices.icnf.pt, fogos.icnf.pt). Pilot pivots to DGT COSc + EFFIS for fuel cover (see next item and `scripts/00_effis_fetch.sh`); ICNF CCF tracked as future-work alignment.
- [x] **DGT INSPIRE.** COS 2018 v3 and COS 2023 v1 (Série 2, GeoPackage, EPSG:3763) confirmed fetchable from `geo2.dgterritorio.gov.pt` (HEAD HTTP 200, ~858 MB each); COSc 2023 and COSc 2024 Pré-Verão likewise. Codified in `scripts/00_dgt_fetch.sh`. INSPIRE record id `b498e89c-1093-4793-ad22-63516062891b` (SNIG canonical; the inspire-geoportal.ec.europa.eu mirror was 404 on 2026-05-07).
- [x] **EFFIS European Fuel Map.** Pan-European 13-class NFFL fuel-class GeoTIFF confirmed fetchable from `forest-fire.emergency.copernicus.eu/applications/data-and-services` (no auth, free). Codified in `scripts/00_effis_fetch.sh`. Reference paper: [Aragoneses et al. 2023, ESSD](https://essd.copernicus.org/articles/15/1287/2023/). Used as the international-readability crosswalk anchor; see `data/crosswalks/icnf_to_scott_burgan.yaml` under `source_inputs.international_reference`.
- [ ] **Cloudflare R2 (optional).** If artifacts will be published online: bucket created, API token scoped to that bucket only.

## C. Data-source health checks (the audit run)

The CLI's `audit` command must return all GREEN before development starts. Implementation note: `audit` is the first script to write — keep it tiny, reuse it forever.

- [x] `uv run wildfire-exposure-eo audit --aoi data/aoi/pilot.geojson` returns:
  - GREEN for Sentinel-2 L2A availability ≥ 50 cloud-free items in the past 24 months
  - GREEN for Sentinel-1 GRD availability ≥ 100 items in the past 24 months
  - GREEN for Cop-DEM GLO-30 coverage of the AOI
  - GREEN for ETH GCH access (download or STAC)
  - GREEN for OSM Overpass response containing ≥ 100 features for at least 3 infrastructure classes
  - GREEN for ICNF Áreas Ardidas containing burns intersecting the AOI in the last 25 years
  - GREEN for ESA WorldCover 2021 raster covering the AOI

If any row is RED or YELLOW, document the failure in `docs/data_sources.md` before proceeding. A YELLOW (e.g., flaky endpoint) becomes a CI flake-mitigation later — note it, don't paper over it.

## D. AOI freeze

- [x] **Pilot AOI selected.** Aveiro / Sever do Vouga (PT-01) — see `docs/aoi_rationale.md`. Three alternatives retained on disk for re-selection.
- [x] **AOI committed.** `data/aoi/pilot.geojson` is a single Polygon feature with `name = "Sever do Vouga / Albergaria-a-Velha / Oliveira de Azeméis"` and `iso3166_2 = "PT-01"`. Bbox `[-8.598, 40.605, -8.242, 40.875]` (~30 × 30 km).
- [x] **Smoke AOI committed.** `data/aoi/smoke.geojson` is a ~1 × 1 km tile centred on Sever do Vouga town (40.733°N, -8.367°W); verified inside the pilot bbox.

## E. Reference reading completed

The team (you + Claude Code) operates on the same reference frame. Do this once, then the assertions in CLAUDE.md mean what they say.

- [x] **Utility vegetation-management vocabulary.** Standard industry idiom captured in `docs/glossary.md` → *Domain — utility vegetation management*: *strike trees, hazard trees, hot spots, clear spans, span-level, fuel load, canopy base height, canopy bulk density, condition-based vs cycle-based trimming*.
- [x] **Scott & Burgan FBFM40.** Skim the LANDFIRE / USFS reference. Note class definitions in `data/crosswalks/icnf_to_scott_burgan.yaml` (stub OK for now, fill in during dev).
- [x] **Fuel-class taxonomy chain captured.** v0.2.0-stub in `data/crosswalks/icnf_to_scott_burgan.yaml` ships: operational DGT COSc (4 classes) + COS (species splits) as training inputs, 9-class internal taxonomy as model output, full FBFM40 reference table (40 classes), NFFL-13 anchor via EFFIS for international readability, and a PROVISIONAL ICNF taxonomy (Fernandes 2009 + Sá et al. 2023). The ICNF CCF raster has no public direct-download URL (verified 2026-05-07) and is held as `national_alignment_target` in `source_inputs`; replace `icnf_taxonomy:` when the actual CCF legend is captured.
- [x] **STAC 1.1 spec.** Already operationally fluent — `cheias-pt-stac` ships a STAC 1.1.0 catalog with 9 collections / 1,684 items in production. Conventions captured in `inventory.yaml`.
- [x] **TorchGeo intro.** Samplers / datasets reviewed; decision is to roll a custom dataset for the fuel-class task given the COSc + COS weak-label fusion in `docs/methodology.md` §6.
- [x] **TerraTorch quickstart.** Confirmed reachable; integration point is `prompts/09_burn_scar_inference.md` (Stage 1b) and the foundation-model variant in Stage 1.
- [x] **eoAPI / VEDA-UI conventions.** Already operationally fluent — `cheias-pt-eoapi` and `cheias-pt-veda-ui` shipped. Conventions imported into `inventory.yaml` and `docs/data_sources.md` cross-check.

## F. Glossary committed

The vocabulary list is the single highest-leverage document. External readers reference it. Claude Code references it. It is the keyword index for the entire project.

- [x] `docs/glossary.md` exists and covers every required group:
  - **Project terms:** *exposure score, asset class, internal fuel class* — defines the headline outputs and their relationship to the taxonomies.
  - **EO/STAC terms:** STAC, Collection, Item, Asset, COG, GeoParquet, eoAPI, VEDA-UI, Sentinel-2 L2A, Sentinel-1 GRD, Cop-DEM GLO-30, ESA WorldCover, ETH GCH, HLS, NDVI/NBR, FWI.
  - **Fire-science terms:** fuel class / fuel model, FBFM40, Anderson-13 / NFFL, fuel load, CBH, CBD, fireshed, burn severity, dNBR, defensible space.
  - **Domain (utility vegetation management):** strike tree, hazard tree, hot spot, span, span-/circuit-level, clear span, HRZ, encroachment, trim cycle, condition-based trimming, SAIDI/SAIFI.
  - **Portuguese terms:** ICNF, AGIF, SGIFR, SGIF, Áreas Ardidas, COS, **COSc**, **SMOS**, DGT, REN, e-Redes, CCF, FGC, RPFGC, ZIF, PNPG, reacendimento.
  - **OSM tags used:** every key=value pair in `data/taxonomy/critical_infrastructure.yaml` v0.1.0 (power.*, emergency.*, education.*, telecom.*, water.*, transport.*) plus a *Candidates for future taxonomy expansion* section flagging `power=pole`, `highway=*`, `pipeline=*`, etc.
- [x] Each entry is 1–3 sentences with a citation URL where applicable. Cross-references between sections are inlined for terms shared across domains (e.g. *faixa de gestão de combustíveis* ↔ *defensible space*).

## G. Working agreement with Claude Code

- [x] `CLAUDE.md` reviewed and accepted. 10 non-negotiables in force; 7 anti-patterns (incl. the new "burn-scar ≠ ignition prediction" one); fact-check and verify-then-act protocols documented.
- [x] `prompts/01_data_audit.md`, `prompts/02_extract_osm.md`, `prompts/03_extract_stac.md`, `prompts/04_static_raster_fetch.md`, `prompts/05_icnf_burns_ingestion.md`, and `prompts/09_burn_scar_inference.md` drafted; `prompts/_session_log.md` ready to accept session entries.
- [x] `inventory.yaml` committed with 4 collections (`fuel-class`, `burn-scar-recent`, `exposure-raster`, `exposure-assets`), all mirroring the cheias-pt-stac pattern.
- [x] `pyproject.toml` ships with lower-bound-only dependency policy (Schreiner principle, documented inline) and the 2026 ML stack (torch>=2.6, torchgeo>=0.9, terratorch>=1.2, transformers>=4.50, lightning, peft, safetensors). `uv.lock` regenerated; `uv sync --locked --extra dev` is the install command for dev sessions (bare `--locked` strips the dev extras).
- [x] `.pre-commit-config.yaml` committed with: `ruff` (auto-fix), `ruff-format`, `pyright` (manual stage), `end-of-file-fixer`, `trailing-whitespace`, `check-merge-conflict`, `check-yaml`, `check-toml`, `check-added-large-files` (2 MB cap), `check-case-conflict`, `mixed-line-ending`, plus `uv-lock` to enforce pyproject ↔ uv.lock consistency. **Activate locally** with `uv run pre-commit install && uv run pre-commit install --hook-type pre-push`.
- [x] `.github/workflows/ci.yml` runs on push/PR to `main` with `concurrency` cancellation: `uv sync --locked --extra dev`, `ruff check`, `ruff format --check`, `pyright src tests scripts`, `pytest -q`, plus `uv lock --locked` for lockfile drift. Two additional jobs gated on `lint-and-test`: `validate-stac` (when `stac/catalog.json` exists) and `validate-schemas` (asserts `config/exposure_score.yaml` weights sum to 1.0, taxonomy YAML fields present, crosswalk top-level keys present, committed sample STAC manifest parses as `StacManifest`).

## H. Modern data-source evaluation captured

- [x] `docs/data_sources.md` carries every source from README → *Data sources* with the six required fields (URL, Access, License, Cadence, Gaps, Decision). 18 source entries plus a *Prithvi-EO 2.0 Burn-Scar* entry under a new *Inference models (derived data sources)* section — derived models are treated as data sources because their version + weights + cadence are part of the score's provenance.
- [x] Open-EO + utility-VM canonical-stack cross-check codified at the bottom of [`docs/data_sources.md`](docs/data_sources.md) — three tables. (a) Open-EO canonical sources (STAC 1.1, COG, GeoParquet, stackstac/odc-stac, TorchGeo, TerraTorch, eoAPI/VEDA-UI, S2 L2A, S1 GRD, Cop-DEM, WorldCover, HLS, pinned-lockfile, no-paywalled-primary) — every row adopted or justified. (b) Utility vegetation-management operational signals (per-asset scoring, FBFM40 taxonomy, time-aware burn, provenance, auditable score, honest scope, commercial imagery, LiDAR, aerial/drone) — every row taken or explicitly waived. (c) Sources deliberately not in the primary path (Planet, Maxar, Sentinel Hub, GEE, e-Redes/REN/DGEG, ICNF SGIF) with a `when to revisit` note per row.

## I. Final pre-flight

- [x] **End-to-end dry run captured in [`docs/methodology.md`](docs/methodology.md)** — 15-phase sequence with per-phase deliverable + prompt-file mapping; seven anticipated stuck-points called out with remediation pre-positioned: cloud-cover asymmetry at §3, COSc + COS label fusion at §6, SegFormer multi-band input at §7, TerraTorch API churn at §8, per-asset zonal-stats throughput at §10, AOI-relative normalisation scope at §11, temporal leakage at §12, and the 30-minute CPU budget at §14.
- [x] **Checkpoint distribution decided: GitHub release attachments** (default). Rationale + alternatives (Hugging Face Hub, Cloudflare R2, Zenodo) documented in `docs/methodology.md` → "Checkpoint distribution decision". Multi-file release scheme accounts for the 2 GB per-attachment ceiling. Hugging Face Hub remains the post-launch migration path; Zenodo for the paper-anchored archival drop.
- [x] **README soften pass complete.** Two absolute-promise lines reframed: line 11 (intro paragraph) and the Definition-of-done bullet on the 30-minute demo. Both now say *target wall-clock* and explicitly cross-reference `docs/methodology.md` for the demo's CPU/GPU split. The Definition-of-done block still names the 30-minute target as the shipping gate.
- [x] Commit. Tag `pre-dev-v0`. Annotated tag on `dab6bb2` (*pre-dev: scaffold, taxonomy, glossary, methodology, CI*), pushed to `origin`.

---

## When this checklist is fully complete

Open the first prompt file (`prompts/01_data_audit.md`) and start the first Claude Code session. Until then, every item above is more valuable than any line of code you'd write.
