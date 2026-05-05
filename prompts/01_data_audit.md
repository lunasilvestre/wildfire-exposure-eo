# Prompt 01 — Data audit

## Purpose

Implement `wildfire-exposure-eo audit`: a single CLI command that probes every data source listed in `README.md` against the pilot AOI and prints a coloured table of GREEN / YELLOW / RED results, plus a machine-readable JSON report at `outputs/audit/<run_id>.json`.

This is the first dev task. It buys reproducibility for everything that follows: every later session should run `audit` first.

## Prerequisites (do not start without these)

- [ ] `PRE_DEV_CHECKLIST.md` complete through section C.
- [ ] `data/aoi/pilot.geojson` and `data/aoi/smoke.geojson` committed.
- [ ] `pyproject.toml` resolves with `uv sync --locked`.
- [ ] Read `CLAUDE.md` end-to-end. The non-negotiables are not aspirational.

## Deliverables

1. `src/wildfire_exposure_eo/audit.py` — pure functions that probe each source. Each function takes an AOI geometry + a `pystac_client.Client` (or equivalent) and returns a `SourceHealth` Pydantic model.
2. `src/wildfire_exposure_eo/cli.py` — Typer subcommand `audit` that runs all probes in parallel via `asyncio.gather` (where the underlying clients support it) or a thread pool, prints a Rich table, and writes JSON.
3. `src/wildfire_exposure_eo/schemas/source_health.py` — Pydantic v2 model:
   ```python
   class SourceHealth(BaseModel):
       source_id: str
       status: Literal["GREEN", "YELLOW", "RED"]
       items_found: int | None
       endpoint: str
       message: str
       elapsed_ms: int
       checked_at_utc: datetime
   ```
4. `tests/unit/test_audit.py` — at least: one test per source that mocks the client and asserts the function returns the right SourceHealth shape on success and on failure.
5. `tests/integration/test_audit_smoke.py` — runs `audit` against the smoke AOI with `--no-network` short-circuit; should exit 0.
6. `prompts/_session_log.md` — append: prompt name, date, commits produced, gates passed.

## Sources to probe

In order of priority. RED on any of items 1–4 blocks the project; RED on 5–9 is recoverable.

1. **Sentinel-2 L2A** — Microsoft Planetary Computer STAC, count items intersecting AOI in last 24 months with cloud cover ≤ 30 %.
2. **Sentinel-1 GRD** — MS PC STAC, IW mode, last 24 months.
3. **Cop-DEM GLO-30** — MS PC STAC, AOI intersection.
4. **ESA WorldCover 2021** — MS PC STAC, AOI intersection.
5. **ETH Global Canopy Height 2020** — direct download URL or STAC item, HEAD request returns 200.
6. **OSM Overpass** — query `power=line` within AOI, expect ≥ 1 result; record total feature count across all classes in `data/taxonomy/critical_infrastructure.yaml`.
7. **ICNF Áreas Ardidas** — direct download endpoint reachable, last shapefile has features intersecting AOI in last 25 years.
8. **HLS S30/L30** — NASA LP DAAC STAC reachable; auth via `~/.netrc`.
9. **IPMA daily FWI** — endpoint reachable (probe only; data ingestion is later).

## Constraints

- **Network failure must produce YELLOW, not RED, with a clear message.** Distinguish "endpoint unavailable" from "endpoint says no data".
- **No long-running queries.** Each probe must time-out at 10 s. Use `asyncio.timeout` or `httpx` timeouts.
- **No real downloads.** `audit` only checks reachability and item counts. It must not download a single COG.
- **Deterministic ordering.** The output table is sorted by source priority, not by completion time.
- **AOI is read from `data/aoi/pilot.geojson` only.** No hardcoded coordinates anywhere.
- **All probe results land in the JSON report**, even GREENs, with full timing data.
- **CLAUDE.md non-negotiable #6 still applies.** Don't print probability-flavoured language anywhere; the audit reports availability, period.

## Test gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests
uv run pytest -q
uv run wildfire-exposure-eo audit --aoi data/aoi/smoke.geojson --offline   # smoke
```

All five must pass before the task is complete.

## Verification (do this before declaring done)

1. Run `audit` against the real pilot AOI on a clean checkout: `uv run wildfire-exposure-eo audit`.
2. Inspect the JSON report. Every required field on `SourceHealth` must be populated for every source.
3. Manually re-query at least two sources (e.g., curl the STAC endpoint) and confirm the item counts match.
4. Run the offline smoke variant — confirm it produces YELLOWs, not RED, with messages explaining the offline mode.
5. `git diff` the README — if you added any narrative claim, it must be regeneratable from `audit`.

## Out of scope for this prompt

- Downloading any imagery.
- Training or scoring.
- Anything that touches `outputs/parquet/` or `outputs/cogs/`.
- STAC catalog construction.

Surface anything in this list as a question before doing it.

## Done when

- All test gates pass.
- A run of `audit` against the pilot AOI produces all GREENs for sources 1–4 (or a documented YELLOW with rationale).
- `prompts/_session_log.md` is updated.
- A PR exists on `main` with the change, green CI, and a one-paragraph description.
