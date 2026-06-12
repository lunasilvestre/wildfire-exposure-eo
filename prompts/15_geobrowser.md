# Prompt 15 — Public GitHub Pages geobrowser + geodata publishing (WU-9)

> Fable build, effort high. Run after WU-5..8 are complete and green on `main`
> (HEAD ≥ `abec5d1`). Read this end-to-end, confirm prerequisites, execute the
> deliverables, leave a `prompts/_session_log.md` entry. Do not deviate from the
> deliverables without writing the question to `prompts/_HIL.md`.

## Mission

Publish the pipeline's geographic outputs **openly and at full fidelity**, and
present the whole study — inputs, method, outputs, and how to reproduce it — in
a **pure-static GitHub Pages geobrowser**. No downsampling: the authoritative
geodata is published in compact geographic formats and rendered client-side.

## Prerequisites (confirm before starting)

- `uv run pytest -q` green on a clean checkout; `data/aoi/pilot.geojson` loads.
- The scored-asset GeoParquet (the **backdated** pilot `exposure_*.parquet` the
  validation used — see the WU-7 session-log entry), the fuel-class COG, and the
  burn-scar COG exist under `outputs/`. If a needed artefact is missing,
  regenerate it via the documented CLI step (see `docs/demo.md`); do **not**
  invent data.
- Read: CLAUDE.md non-negotiables (esp. #1 no invented ids, #2 explicit CRS,
  #5 STAC/GeoParquet/COG outputs, #6 no-probability, #9 no production claims,
  #10 frozen AOI), `README.md`, `docs/validation_report.md`, `docs/demo.md`,
  the committed `stac/` catalog.

## Deliverables

### 1. Publish the geodata (STAC-native)

- **Scored-asset GeoParquet** (~780 KB) → new STAC `exposure-assets` collection
  + item committed under `stac/exposure-assets/<run_id>/`, with the GeoParquet
  committed as the item's data asset. Explicit CRS (EPSG:4326); preserve the
  full per-row provenance. Wire the collection into `stac/catalog.json`.
- **Fuel-class COG** (~0.9 MB) → commit the actual COG as the STAC asset under
  `stac/fuel-layer/...` (the item currently points at a gitignored `outputs/`
  path — point it at the committed file).
- **Burn-scar COG** (~36 MB, over the repo's 2 000 kB `check-added-large-files`
  cap) → upload as a **GitHub Release** asset (`gh release create`/`upload`);
  set the STAC item's asset `href` to the release download URL and record the
  release tag. Do not commit the 36 MB file to the tree.
- `uv run stac-validator validate stac/catalog.json --recursive` must pass.

### 2. Pure-static GitHub Pages geobrowser (under `docs/`)

- `docs/index.html` (+ `docs/app/` assets) using **MapLibre GL JS** from a
  pinned CDN, **no API key** (free/OSM raster basemap or a keyless style).
- Add `docs/.nojekyll` so Pages serves the static files verbatim.
- **Layers** (each toggleable; each with attribution + a one-line honest caption):
  - *Inputs:* AOI boundary · OSM critical-infrastructure assets · fuel-class
    (COG) · burn-scar inference-probability (COG) · ICNF burn perimeters.
  - *Output (default-on, the headline):* exposure **rank** — assets coloured by
    rank, same encoding as `docs/figures/fig1_exposure_map.png`.
- **Full-fidelity rendering, no downsampling:** render the COGs client-side via
  a pinned MapLibre COG protocol (byte-range reads) **or** a committed PMTiles
  raster pyramid — your choice, but no quality loss. Vectors as GeoJSON or
  PMTiles. The burn-scar COG is read from its Release URL (verify CORS +
  byte-range work from a static page; if they don't, host a PMTiles pyramid of
  it in-repo if it fits the cap, else document the constraint in the session log).
- **Study panel:** the civic-tech framing; the validation headline (numbers
  verbatim from `docs/validation_report.md`, with the "5 positives → does not
  resolve" caveat); scope boundaries (rank **not** probability; AOI-relative;
  public-data demonstrator); and download links to the published GeoParquet,
  COGs, and the STAC catalog.

### 3. Diagrams / flowcharts (Mermaid)

Committed in markdown (renders on GitHub) **and** embedded in the site
(MermaidJS CDN or pre-rendered SVG). All node labels use the **real** modules /
CLI commands / artefacts — no aspirational boxes.

- **Pipeline DAG:** OSM + S2 + Cop-DEM + ETH-GCH + EFFIS + DGT-COSc + ICNF →
  fuel-class crosswalk (`fuel.py`) + Prithvi burn-scar COG (`burn_scar.py`) →
  per-asset features (`features.py`, exactextract) → composite exposure rank
  (`scoring.py`) → validation (`scripts/11_validate.py`: lift / Spearman /
  ablation) → outputs (GeoParquet + COG + STAC).
- **Reproduction flowchart:** the CPU demo path (`audit → fetch-osm →
  fetch-rasters → fetch-burns → fuel-layer → score → validate`) and the GPU
  burn-scar route, with the actual commands — mirror `docs/demo.md`.
- **Provenance/lineage diagram:** how a scored row traces to source STAC ids +
  config + code-commit sha.
- Replace the README's ASCII architecture diagram with the Mermaid pipeline DAG.

### 4. Reproduction docs

The site and README state plainly how to reproduce everything: link
`docs/demo.md` (CPU path), the GPU burn-scar route, and the published geodata.

## Non-negotiables for this WU (it is PUBLIC SURFACE)

- **No probability / risk / forecast language for the exposure score** anywhere
  a visitor sees it — it is a relative **rank** (#6). The one allowed
  "probability" is the Prithvi *burn-scar inference probability* (the detection
  output, per repo convention); keep it precise and caveated ("not a calibrated
  probability, not a fire forecast").
- No *production-ready* / *operational* claims (#9).
- Explicit CRS on every geodata load/transform; document the display CRS (#2).
- No invented identifiers (#1); every number reproducible from the report / a
  script (fact-check checklist).

## Done-when

- Geodata published: `exposure-assets` STAC item + committed GeoParquet asset;
  fuel COG committed as a STAC asset; burn-scar COG on a Release with the STAC
  href set; `stac-validator … --recursive` passes.
- Site committed under `docs/` with `.nojekyll`; `index.html` + assets present;
  every geodata file it references exists (or the Release URL resolves).
- Mermaid diagrams committed and syntactically valid; README ASCII diagram
  replaced.
- Four gates (ruff / format / pyright / pytest) green; CI green on `main`.
- **Headless honesty:** a `claude -p` session cannot open a browser, so it
  CANNOT confirm the map visually renders. Do everything verifiable headlessly
  (geodata valid, links/Release URL resolve, HTML well-formed, Mermaid valid,
  gates green) and **list in the session log exactly what needs a human visual
  check on the live Pages URL.** Do not claim the map "works" — claim the
  artefacts are produced and structurally valid.
- GitHub Pages **enablement is the orchestrator's job** (via `gh api`) after this
  WU's independent review is green — not this build session's.

## Session-log

Append a terse entry: deliverables shipped, the COG-rendering approach chosen
and why, the Release tag/URL, and the explicit human-visual-check list.
