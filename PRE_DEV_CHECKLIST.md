# Pre-development checklist

Complete every item below **before** the first development Claude Code session. The point is to refuse to start coding until the data, accounts, hardware, and reference frame are verified — most failure modes in EO/ML projects are caused by skipping this.

Estimated time: **3–4 hours** of focused work.

---

## A. Hardware & local environment

- [ ] **`atlas` (RTX 3090) reachable.** SSH, GPU visible to `nvidia-smi`, free disk ≥ 100 GB on training volume.
- [ ] **CUDA toolchain.** Driver ≥ 535, CUDA 12.1 runtime present, `python -c "import torch; print(torch.cuda.is_available())"` returns `True`.
- [ ] **Local dev machine.** `uv` installed, `uv --version` ≥ 0.4. `pre-commit` installed system-wide.
- [ ] **Disk plan.** Decide where `outputs/` lives. Default: project-local. Confirm at least 50 GB free.
- [ ] **VS Code / editor.** Pyright LSP enabled, ruff extension active, working against the project venv.

## B. Accounts & API access

- [ ] **GitHub.** New empty public repo `lunasilvestre/wildfire-exposure-eo` created. Default branch `main`. Branch protection: required PR reviews on, required status checks on once CI exists.
- [ ] **Microsoft Planetary Computer.** Free account; verify `pystac-client` can list `sentinel-2-l2a`, `sentinel-1-grd`, `cop-dem-glo-30`, `esa-worldcover` collections. Save a short script under `scripts/00_pc_smoke.py` that queries and exits 0.
- [ ] **NASA Earthdata Login.** Required for HLS access via LP DAAC STAC. Token saved in `~/.netrc`.
- [ ] **OSM Overpass.** No account needed. Note primary endpoint + at least one fallback.
- [ ] **ICNF data download.** Confirm direct-download URLs for: Áreas Ardidas (annual, 1990–latest), Carta de Combustíveis Florestais. Save a one-liner `wget`/`curl` command per dataset under `scripts/00_icnf_fetch.sh`.
- [ ] **DGT INSPIRE.** Browse the DGT geoportal and confirm COS 2018 (or latest) is fetchable.
- [ ] **Cloudflare R2 (optional).** If artifacts will be published online: bucket created, API token scoped to that bucket only.

## C. Data-source health checks (the audit run)

The CLI's `audit` command must return all GREEN before development starts. Implementation note: `audit` is the first script to write — keep it tiny, reuse it forever.

- [ ] `uv run wildfire-exposure-eo audit --aoi data/aoi/pilot.geojson` returns:
  - GREEN for Sentinel-2 L2A availability ≥ 50 cloud-free items in the past 24 months
  - GREEN for Sentinel-1 GRD availability ≥ 100 items in the past 24 months
  - GREEN for Cop-DEM GLO-30 coverage of the AOI
  - GREEN for ETH GCH access (download or STAC)
  - GREEN for OSM Overpass response containing ≥ 100 features for at least 3 infrastructure classes
  - GREEN for ICNF Áreas Ardidas containing burns intersecting the AOI in the last 25 years
  - GREEN for ESA WorldCover 2021 raster covering the AOI

If any row is RED or YELLOW, document the failure in `docs/data_sources.md` before proceeding. A YELLOW (e.g., flaky endpoint) becomes a CI flake-mitigation later — note it, don't paper over it.

## D. AOI freeze

- [ ] **Pilot AOI selected.** Default: a 30 × 30 km bbox covering the Pampilhosa da Serra / Pedrógão Grande district. Adjust if a stronger justification arises (REN line density, recent burn history, OSM coverage). Document the choice in `docs/aoi_rationale.md`.
- [ ] **AOI committed.** `data/aoi/pilot.geojson` exists, validates, and contains exactly one polygon feature with a `name` property and an `iso3166_2: "PT-..."` property.
- [ ] **Smoke AOI committed.** `data/aoi/smoke.geojson` is a 1 × 1 km sub-tile of the pilot AOI for fast development loops.

## E. Reference reading completed

The team (you + Claude Code) operates on the same reference frame. Do this once, then the assertions in CLAUDE.md mean what they say.

- [ ] **Overstory product surface.** Read [Overstory case studies](https://www.overstory.com/case-studies). Capture vocabulary into `docs/glossary.md`: *strike trees, hazard trees, hot spots, clear spans, span-level, fuel load, canopy base height, canopy bulk density, condition-based vs cycle-based trimming*.
- [ ] **Scott & Burgan FBFM40.** Skim the LANDFIRE / USFS reference. Note class definitions in `data/crosswalks/icnf_to_scott_burgan.yaml` (stub OK for now, fill in during dev).
- [ ] **ICNF fuel-class taxonomy.** Open the Carta de Combustíveis Florestais legend; capture the official class names + codes into `data/crosswalks/icnf_to_scott_burgan.yaml`.
- [ ] **STAC 1.1 spec.** Skim the [STAC 1.1.0 specification](https://stacspec.org/) — collections, items, common metadata. You'll be writing one.
- [ ] **TorchGeo intro.** Read the TorchGeo tutorial on samplers + datasets. Decide whether you'll use `RasterDataset` or roll a custom dataset (likely custom, given fuel-class crosswalk needs).
- [ ] **TerraTorch quickstart.** Read enough to confirm Prithvi-EO 2.0 is reachable from atlas. The foundation-model path is optional — but knowing the cost of using it is mandatory.
- [ ] **DevSeed eoAPI / VEDA-UI README.** Already shipped via `cheias-pt-*`. Re-read to remember the conventions you've established for yourself.

## F. Glossary committed

The vocabulary list is the single highest-leverage document. Hiring managers read it. Claude Code references it. It is the keyword index for the entire project.

- [ ] `docs/glossary.md` exists with at minimum:
  - **EO/STAC terms:** STAC, COG, GeoParquet, asset, item, collection, FBFM40, FWI, NDVI, NBR
  - **Fire-science terms:** fuel class, fuel load, canopy base height, canopy bulk density, fire-weather index, fireshed
  - **Domain (Overstory-coded):** span, circuit, hot spot, hazard tree, strike tree, condition-based trimming
  - **Portuguese terms:** ICNF, AGIF, COS, DGT, REN, e-Redes, área ardida, faixa de gestão de combustíveis
  - **OSM tags used:** every OSM key/value in `data/taxonomy/critical_infrastructure.yaml`
- [ ] Each entry: 1–3 sentences + a citation URL where applicable. No jargon dumps without explanation.

## G. Working agreement with Claude Code

- [ ] `CLAUDE.md` reviewed and accepted. Anything you disagree with — edit it now, before the first session.
- [ ] `prompts/01_data_audit.md` exists (stub OK) so the first dev session has a target.
- [ ] `inventory.yaml` skeleton committed (mirroring `cheias-pt-stac/inventory.yaml`).
- [ ] `pyproject.toml` exists with the pinned stack (see README → Stack), `uv sync` runs cleanly.
- [ ] `pre-commit` hooks installed: `ruff`, `ruff-format`, `pyright` (manual stage), end-of-file-fixer, trailing-whitespace, no-merge-conflict.
- [ ] `.github/workflows/ci.yml` exists and runs on push/PR with: `uv sync`, `ruff check`, `ruff format --check`, `pyright`, `pytest -q`.

## H. Modern data-source evaluation captured

- [ ] `docs/data_sources.md` exists and contains, for each source listed in README → Data sources:
  - Source URL
  - Access mechanism (STAC catalog URL, direct download, API)
  - License + attribution string
  - Update cadence
  - Known gaps or quirks (e.g., ETH GCH is single-vintage 2020)
  - Decision: **PRIMARY**, **AUXILIARY**, or **FUTURE**
- [ ] Cross-check against DevSeed and Overstory public stacks — if you skipped a source they consider canonical (Sentinel-2 L2A, Cop-DEM, ESA WorldCover, STAC, COG, GeoParquet), justify in writing.

## I. Final pre-flight

- [ ] One end-to-end dry run of the planned dev sequence on a whiteboard or in `docs/methodology.md`. Identify where you'd get stuck. Fix the prerequisite *now*.
- [ ] Decide the demo's pretrained-checkpoint distribution path. Hugging Face? GitHub release attachments? R2? Default: GitHub release attachments under 2 GB total.
- [ ] Skim the README out loud. Anything that reads as a promise the project doesn't yet meet — soften it now.
- [ ] Commit. Tag `pre-dev-v0`.

---

## When this checklist is fully complete

Open the first prompt file (`prompts/01_data_audit.md`) and start the first Claude Code session. Until then, every item above is more valuable than any line of code you'd write.
