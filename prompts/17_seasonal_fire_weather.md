# Prompt 17 — Seasonal / fire-weather signal (Pillar 0)

> **DRAFT skeleton (2026-06-16).** Foundation pillar — restores the "this season"
> signal the score currently lacks. Read end-to-end, confirm prerequisites,
> execute in phase order, leave a `prompts/_session_log.md` entry. Any deviation
> from the deliverables → write the question to `prompts/_HIL.md` and wait.
> Buildable in ~1–2 sessions. Isolated worktree recommended (Wave 1).

## Mission

Add an **open, public, programmatic** fire-weather / seasonal signal as a score
feature, so the exposure rank reflects current/seasonal fire danger — not only
structural fuel + terrain + history. This is the foundation for freshness
(pillar 3 depends on it) and the honest answer to the "find a MEJOR/DGT
equivalent" question, using public data only.

**Scope guard.** This is *exposure-rank* work, not forecasting. The fire-weather
feature is one normalised input among the existing six. No probability, no
"chance of fire", no calibrated-forecast language (#6). We detect/ingest danger
*indices*, we do not predict ignition.

## Phase 0 — Verify a GREEN public source BEFORE any code (hard gate, #1)

The v0.2.0 changelog records *why FWI was dropped*: no GREEN public programmatic
source verified in-session (IPMA has no public REST API; Copernicus CDS needs an
account). **Do not repeat that failure silently.** Phase 0 must end with a named,
reachable, license-clear source whose product ID is recorded — or a STOP.

Evaluate, in order of preference, and log the verdict for each:

1. **xclim `indices.fire` (Canadian FWI) over ERA5** (Apache-2.0). Confirm the
   ERA5 access model actually works in-session: `cdsapi` + credentials, or a
   pre-staged ERA5 subset. If ERA5 needs an account that is not available
   in-session, say so — that is a RED, not a workaround.
2. **EFFIS / Copernicus CEMS fire danger** (GEFF engine) ready-made danger
   rasters, incl. seasonal lead. Confirm a programmatic download path (cdsapi or
   a documented open URL) and the license.
3. **SeasFire datacube** (Zarr, open) as a seasonal-context fallback feature.

Decision rule: pick the first source that is **GREEN** (reachable, programmatic,
license-clear, real product ID). If **none** verify GREEN, STOP and surface to
`prompts/_HIL.md` — the project ships structural-only and the README "this
season" wording must be softened (public surface → human approval). **Never
invent a product ID or fabricate FWI values to "unblock".**

Deliverable: `scripts/17_fire_weather_audit.py` — probes each candidate, prints
reachability + product ID + license, writes a verdict JSON to
`outputs/diagnostics/17_fire_weather_audit.json`.

## Phase 1 — Feature extractor

- New module `src/wildfire_exposure_eo/fire_weather.py` — fetches/loads the
  chosen source, computes a per-AOI fire-danger surface with **explicit CRS**
  (#2), and exposes a per-asset aggregator (zonal stat over the class buffer,
  same pattern as `features.py`). Determinism: any RNG seeded 42 (#4).
- New config `config/fire_weather.yaml` — source ID, vintage/window, index
  choice, season months, license attribution. No identifiers hardcoded in `.py`.
- Wire into `features.py` as a new feature column (e.g. `fwi_p95_recent_season`
  revived, or `fire_danger_seasonal` — pick one name, document it).

## Phase 2 — Score integration (SERIALIZED weight edit — see operationalization §4)

- Bump `config/exposure_score.yaml` → **v0.3.0**: add the new feature with a
  weight, **re-normalise all weights to sum to exactly 1.0** (CI asserts), add a
  changelog entry citing this WU. This edit is the coordination point with
  pillars 1 and 4 — land it alone, do not let another WU touch the file
  concurrently.
- Provenance: the source product ID + vintage must appear in the per-asset
  provenance dict and the run manifest.

## Verify-then-act

Run on `data/aoi/smoke.geojson` first; log STAC/source candidate IDs before
loading; only then run the pilot AOI. Session log shows smoke before full.

## Tests required

- Unit: feature aggregator on a synthetic danger raster (known answer).
- Schema: `ScoredAsset` (or the run manifest schema) accepts the new provenance
  fields; weights-sum-to-1.0 holds at v0.3.0.
- Smoke: `scripts/17_fire_weather_audit.py --smoke` exits 0.

## Gates (all must pass)

```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run pyright src tests scripts && uv run pytest
```

If a new dep is needed (e.g. `xclim`, `cdsapi`): non-negotiable #8 — one-line
justification in the PR body + pinned version, surfaced to the human first.

## Done-when

- Phase 0 verdict recorded; a GREEN source named **or** a STOP surfaced to `_HIL.md`.
- `fire_weather.py` + `config/fire_weather.yaml` shipped; feature wired into `features.py`.
- `exposure_score.yaml` at v0.3.0, weights sum 1.0, changelog cites this WU.
- Provenance carries the source ID + vintage.
- Tests + four gates green; session-log entry with the Phase-0 verdict.

## HIL flags

- **FLAG (public surface):** if Phase 0 is RED, softening the README "this
  season" wording needs human approval.
- **FLAG (data contract):** bumping `exposure_score.yaml` changes every score —
  if re-publishing the pilot artefacts is intended, that is a separate re-score +
  validation-refresh follow-on (do not regenerate the published parquet here
  without sign-off).
