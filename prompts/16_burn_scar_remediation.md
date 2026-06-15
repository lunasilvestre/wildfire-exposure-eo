# Prompt 16 — Burn-scar inference remediation (WU-10)

> Sonnet build, effort high. Run after WU-9 (`15_geobrowser.md`) is merged
> and green on `main`. Read this end-to-end, confirm prerequisites, execute
> the deliverables in phase order, leave a `prompts/_session_log.md` entry.
> Do not deviate from the deliverables without writing the question to
> `prompts/_HIL.md`.

## Mission

Reduce the burn-scar COG's massive over-prediction — 47 % of pilot-AOI pixels
≥0.5, ~950× the ICNF-mapped burned area — without breaking the pipeline
contracts that downstream WUs (exposure scoring, geobrowser) depend on. Fix
the compositing strategy, tighten the inference window and masking, then
add a proper false-positive validation harness so the improvement is measured,
not assumed.

**Before touching any code, run Phase 0 to determine which root cause is
dominant.** The Phase 1 implementation strategy follows from that result.

## Context — confirmed root causes

`docs/burn_scar_audit.md` documents the over-prediction and its origins:

- **PRIMARY (confirmed):** `infer_burn_probability` in `burn_scar.py` line 490
  builds a 179-scene per-pixel MAX composite (`np.fmax`). Any single-scene
  false positive persists across the entire 12-month stack. The audit records
  a pilot-AOI median score of 0.47 and mean 0.50 — a near-uniform distribution.
- **SECONDARY (inferred):** Domain shift. The `ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars`
  checkpoint was trained on US HLS tiles. The domain-shift sanity check
  (`scripts/09_burn_scar_sanity.py`) measured RECALL on large known-fire boxes
  but never measured the false-positive rate on unburned land; the "outside
  perimeter" mean probability was 0.21–0.30, already elevated.
- **CONTRIBUTING:** `config/burn_scar.yaml` `scl_mask_classes` omits SCL 5
  (bare soil), leaving post-harvest bare soil and agriculture spectrally
  confused with char; 13.1 % of pixels score ≥0.9; the trailing window spans
  the off-season (months with no fire risk).

The existing validation gap: no PR curve exists, no FPR measurement on
unburned land, no threshold chosen with precision/recall trade-off in view.

### Phase-0 result — EXECUTED 2026-06-15 (verdict: MAX_COMPOSITE_DOMINANT)

The Phase-0 diagnostic below has already been run against the **displayed** COG's
window. Using the production pipeline (`burn_scar._scene_probability`, pinned
checkpoint `a3f2c410…`) on one clean pre-fire scene over the pilot AOI
(`S2A_MSIL2A_20250607T113911_R080_T29TNF`, 2025-06-07, 17.8 % cloud, GPU):

| metric | single pre-fire scene | published 179-scene MAX composite |
|---|---|---|
| median prob | **0.039** | 0.47 |
| mean prob | 0.097 | 0.50 |
| fraction ≥ 0.5 | **3.6 %** | 47.3 % |
| pixels < 0.1 | **77 %** | — |

`frac≥0.5 = 3.6 % < 10 %` → **MAX-composite inflation is the dominant cause**;
the model is well-behaved per-scene on unburned land. **Implication for Phase 1:**
the reducer swap (1a) is the high-leverage fix and is expected to be largely
sufficient on its own. The bare-soil mask (1c) and any threshold work are
*secondary refinements*, not the main lever — keep them in scope but do not let
them gate the primary fix. The diagnostic script ships at
`scripts/16_burn_scar_prefire_diag.py`; sidecar at
`outputs/diagnostics/burn_scar_prefire_diag_*.json`.

## Prerequisites (confirm before starting)

- [ ] `uv run pytest -q` green on a clean checkout.
- [ ] `data/aoi/pilot.geojson` and `data/aoi/smoke.geojson` load.
- [ ] The existing burn-scar COG (`outputs/cogs/burn_scar_20260610T072820Z.tif`)
      and the ICNF perimeter layer are present (needed for Phase 0 baseline).
- [ ] Read `CLAUDE.md` non-negotiables: especially #1 (no invented identifiers),
      #2 (explicit CRS), #4 (seed 42), #6 (no probability claims), #8 (no
      new deps without justification).
- [ ] Read `docs/burn_scar_audit.md` for the confirmed numbers quoted above.
- [ ] Read `config/burn_scar.yaml` for the current config knob names.
- [ ] Read `src/wildfire_exposure_eo/burn_scar.py` around line 490 (the
      `np.fmax` composite site) and `src/wildfire_exposure_eo/features.py`
      around `recent_burn_share_12mo` (line 239) before writing any code.
- [ ] Run `uv run wildfire-exposure-eo audit` — fix any RED rows unrelated to
      this WU before proceeding.

---

## Phase 0 — Decisive diagnostic (measure before changing the model)

> **STATUS: DONE (2026-06-15).** Verdict MAX_COMPOSITE_DOMINANT — see the
> Phase-0 result table in Context above. The script is committed at
> `scripts/16_burn_scar_prefire_diag.py` (it accepts `--scene-id`/`--aoi` and
> lists+logs candidates per verify-then-act). The spec below is retained for
> provenance; an executor may optionally re-run it for the leakage-safe
> backdated window as confirmation, but the result will not change the Phase 1
> emphasis.

**Goal:** determine whether MAX-composite inflation or per-scene domain-shift
FP is the dominant root cause. This controls Phase 1 emphasis.

### Deliverable: `scripts/16_burn_scar_prefire_diag.py`

A deterministic script (seed 42, `rng = np.random.default_rng(42)`) that:

1. **Lists candidate STAC items first** (CLAUDE.md verify-then-act protocol):
   - Call `query_recent_s2` with a restricted window covering the earliest
     month of the 12-month trailing window (approximately 2024-06 to 2024-07)
     over `data/aoi/pilot.geojson`.
   - Log every returned item `id` to stdout before loading any raster data.
   - Apply deterministic ordering: sort by `datetime` ascending, tie-break by
     `id`. Pick the first item that passes the cloud-cover filter.

2. **Run single-scene inference** on that one pre-fire scene using the
   existing `infer_burn_probability` machinery (pass a single-item list).

3. **Report**:
   - Probability histogram (10 bins, 0–1) printed to stdout.
   - `frac_above_05`: fraction of valid pixels with score ≥ 0.5.
   - Save a compact summary JSON to `outputs/diagnostics/16_prefire_diag.json`
     containing `item_id`, `item_datetime`, `frac_above_05`, `median_prob`,
     `mean_prob`, `hist_counts`, `hist_edges`.

4. **Decision rule** (print to stdout, record in session log):
   - If `frac_above_05 > 0.40` → domain shift is dominant; Phase 1 should
     prioritise bare-soil masking, season restriction, and threshold tuning
     via the Phase 2 PR curve; the reducer swap is still beneficial but not
     sufficient on its own.
   - If `frac_above_05 < 0.10` → MAX-composite inflation is dominant; the
     reducer swap alone should substantially fix the COG.
   - Otherwise (0.10–0.40) → both causes are significant; implement all Phase
     1 changes.

**Smoke gate:** run `python scripts/16_burn_scar_prefire_diag.py --smoke`
(restrict to `data/aoi/smoke.geojson`) and confirm it exits 0 before the full
pilot run.

---

## Phase 1 — Remediation

Implement all three changes regardless of the Phase 0 decision rule (the rule
affects emphasis / session-log framing, not scope). **Do not re-run the full
inference pipeline yet** — Phase 2 must be in place first so the improvement
is measured on commit.

### 1a. Robust composite reducer

- Add a `reducer` config knob to `config/burn_scar.yaml` under `inference:`:

  ```yaml
  # Composite reducer applied across the scene stack.
  # Options: max | median | p75 | p85 | p90 | consensus_N
  # (consensus_N: fraction of scenes scoring >=0.5 must exceed N/10, e.g.
  # consensus_5 = majority vote across scenes).
  # Default: p85 — keeps the upper tail but discards single-scene spikes.
  reducer: "p85"
  ```

- Implement the reducer at line 490 in `burn_scar.py` inside
  `infer_burn_probability`. The current accumulator pattern (`composite =
  np.fmax(composite, prob)`) must be replaced with a stack-and-reduce
  pattern: accumulate scene arrays into a list, then apply the chosen reducer
  after the loop. Supported reducers: `max` (backward-compat alias), `median`,
  `p75`, `p85`, `p90`, `consensus_N` where N is parsed from the string.
  Raise `ValueError` on unrecognised strings.
- `max` must remain the backward-compat default if the key is absent in old
  configs (but the shipped `burn_scar.yaml` sets `p85`).
- Determinism: the stack-and-reduce path must produce the same output for
  `max` as the old `np.fmax` path (assert in a unit test).
- Keep the existing `BurnScarRun` provenance schema — add `reducer` as a new
  field (schema change: add with a default so old instances deserialise).

### 1b. Fire-season window restriction

- Add two config knobs to `config/burn_scar.yaml` under `inference:`:

  ```yaml
  # Only include S2 scenes whose acquisition month falls within this range.
  # Months are 1-indexed. Default Jun–Oct aligns with ICNF's principal fire
  # season for mainland Portugal.
  season_start_month: 6
  season_end_month: 10
  ```

- In `query_recent_s2` (or a new `filter_to_season` helper called immediately
  after the STAC search), drop items whose `datetime.month` falls outside
  `[season_start_month, season_end_month]`. Log how many items are dropped.
- The smoke AOI (`data/aoi/smoke.geojson`) must still return ≥1 scene after
  filtering; if the smoke window spans only off-season months, expand the
  smoke test window to include at least one Jun–Oct month.
- Add `season_start_month` and `season_end_month` to `BurnScarRun` provenance.

### 1c. Tighten SCL masking — bare-soil

- Add SCL class 5 (bare soil) to `scl_mask_classes` in `config/burn_scar.yaml`:

  ```yaml
  scl_mask_classes: [0, 1, 3, 5, 8, 9, 10, 11]
  ```

- No code change needed if `burn_scar.py` already reads this list from config.
  Confirm that `_mask_scene` in `burn_scar.py` (around line 338, parameter
  `scl_mask_classes`) reads the value dynamically — do not hardcode SCL 5.

**Out of scope for 1c (requires separate approval):** an NDVI guard or a
land-cover mask inside `recent_burn_share_12mo` in `features.py`. See the
Human-Approval Flags section below.

---

## Phase 2 — Validation harness

### Deliverable: `scripts/16_burn_scar_validate.py`

New script (confirm `16_` prefix is the next available in `scripts/`). It:

1. **Rasterises ICNF perimeters** onto the burn-scar COG grid:
   - Load the matched ICNF perimeters (same overlap window as the audit:
     `2025-06-09..2025-12-31`, MapServer layer 20).
   - Rasterise with explicit CRS assertion: assert `gdf.crs == "EPSG:4326"` and
     `cog.rio.crs == "EPSG:4326"` before any spatial operation.
   - Output: a boolean mask array, same shape as the COG.

2. **Stratified true-negative sample** (N = 10 000, seed 42):
   - Use `rng = np.random.default_rng(42)`.
   - Stratify by fuel class: load the existing fuel COG, read the fuel-class
     value at each pixel, draw pixels proportional to fuel-class prevalence
     from the unburned (ICNF mask = False) set.
   - Log stratum sizes to stdout.

3. **Threshold sweep** (0.1 to 0.9, step 0.05):
   - For each threshold: compute precision, recall, F1, IoU, FPR on the union
     of the ICNF-positive pixels and the stratified TN sample.
   - Write the full sweep to `outputs/diagnostics/16_pr_curve.json`.

4. **Cross-tab** predicted-positive vs fuel class — log as a markdown table
   to stdout (helps diagnose which fuel types drive FPs).

5. **Headline at chosen threshold** (default 0.5, CLI flag `--threshold`):
   report precision, recall, F1, IoU, FPR.

6. **Temporal-leakage assertion**: assert that no STAC item in `BurnScarRun`
   has `datetime` after the ICNF perimeter's fire-end date. Raise if violated.

7. **Append to `docs/burn_scar_audit.md`** (do NOT overwrite the existing
   content):

   ```markdown
   ## WU-10 validation — remediation results

   <!-- generated by: scripts/16_burn_scar_validate.py at <commit> -->

   | metric | baseline (WU-1 COG) | remediated COG |
   |---|---|---|
   | frac_above_05 | 0.473 | ... |
   | FPR@0.5 | ... | ... |
   | precision@0.5 | ... | ... |
   | recall@0.5 | ... | ... |
   | F1@0.5 | ... | ... |
   | IoU@0.5 | ... | ... |
   ```

   Use only "inference score / above threshold / matched area" language — never
   "probability", "risk", "forecast" (non-negotiable #6). Restate: "Burn SCARS
   detected — post-event spectral signatures of fires that already happened.
   Not ignition prediction."

8. **Smoke gate:** `python scripts/16_burn_scar_validate.py --smoke` must
   exit 0 (validates on the 1 km × 1 km AOI; fewer pixels, same code path).

---

## Human-Approval Flags — do not ship without sign-off

These two changes cross pipeline-contract lines. Surface each as a question
in `prompts/_HIL.md` and wait for explicit approval before implementing.

**FLAG A — DATA CONTRACT (composite reducer + COG replacement):**
Changing the reducer from `max` to `p85` and re-running inference produces a
new burn-scar COG that supersedes the already-published artefacts:
- The committed STAC item under `stac/burn-scar-recent/` (href and checksums
  will change).
- The R2-hosted display COG at `wildfire.cheias.pt` (the geobrowser reads from
  there — see `memory/reference_cors_github_releases_no_cors.md`).
- The `recent_burn_share_12mo` values for all scored assets (exposure parquet
  changes, validation report numbers change).

Requires: sign-off, re-run of full inference pipeline on pilot AOI, re-upload
to R2, re-point of the STAC item href, re-run of `scripts/11_validate.py` to
refresh `docs/validation_report.md`, and a downstream follow-on WU for
exposure re-scoring. **Do not regenerate the exposure parquet in this WU.**

**FLAG B — PIPELINE CONTRACT (land-cover mask in feature extraction):**
Adding an NDVI guard or fuel-class restriction inside `recent_burn_share_12mo`
in `features.py` changes the feature value for every scored asset and
invalidates the current exposure parquet and `docs/validation_report.md`.
This is a separate, larger change. Do not implement in this WU unless the
human explicitly approves it here.

---

## Explicitly out of scope

- Otsu / auto-threshold tuning — defer until the Phase 2 PR curve exists and
  has been reviewed by the human.
- A bitemporal dNBR change-detector rewrite — the model transfers on real
  fires (IoU 0.67–0.75 on the sanity-check fires); the problem is false
  positives, not recall.
- Re-running the full exposure score or regenerating `docs/validation_report.md`
  (downstream follow-on WU, gated on FLAG A approval).
- Fine-tuning the Prithvi checkpoint on Portuguese labels (forbidden in this
  project's scope — separate work-unit if ever approved).
- Adding `scikit-image` or any other new dependency without the non-negotiable
  #8 written justification in the PR description and a pinned version.

---

## Tests required

New feature ships with at minimum:

- **Unit tests** in `tests/unit/test_burn_scar.py`:
  - `test_reducer_max_backward_compat`: assert that `reducer="max"` with the
    new stack-and-reduce path produces the same output as the old `np.fmax`
    accumulator on a 3-scene synthetic stack.
  - `test_reducer_p85`: assert that `reducer="p85"` on a known synthetic stack
    returns the 85th-percentile per pixel.
  - `test_reducer_consensus`: assert that `reducer="consensus_5"` on a 10-scene
    synthetic stack flags a pixel as 1.0 only when ≥5 scenes score ≥0.5.
  - `test_reducer_unknown_raises`: assert `ValueError` on `reducer="bogus"`.
  - `test_season_filter_drops_off_season_items`: assert that items outside
    `[season_start_month, season_end_month]` are excluded.
  - `test_burn_scar_run_reducer_field`: assert `BurnScarRun.model_validate`
    accepts a `reducer` field and that the default serialises/deserialises.

- **Schema test** in `tests/schemas/` (or extend existing): assert that a
  `BurnScarRun` dict with the new `reducer`, `season_start_month`, and
  `season_end_month` fields validates without error.

- **Smoke check** for the diagnostic script: `python scripts/16_burn_scar_prefire_diag.py --smoke`
  exits 0 and writes `outputs/diagnostics/16_prefire_diag.json`.

---

## Gates (all must pass before declaring done)

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests scripts
uv run pytest -q
```

If the STAC item is updated (after FLAG A approval):

```bash
uv run stac-validator stac/catalog.json --recursive
```

If the parquet schema changes (after FLAG A + FLAG B approval):

```bash
uv run python -m wildfire_exposure_eo.cli validate-schema outputs/parquet/<latest>.parquet
```

Smoke run before pilot on every compute-heavy script (per CLAUDE.md
verify-then-act protocol). The session log must show the smoke run succeeded
before the full-pilot run is attempted.

---

## Done-when

- Phase 0 complete: `scripts/16_burn_scar_prefire_diag.py` runs, produces
  `outputs/diagnostics/16_prefire_diag.json`, and the decision-rule result is
  recorded in the session log.
- Phase 1 implemented: `reducer`, `season_start_month`, `season_end_month`
  knobs in `config/burn_scar.yaml`; compositing site in `burn_scar.py`
  refactored; SCL 5 added to `scl_mask_classes`.
- Phase 2 implemented: `scripts/16_burn_scar_validate.py` runs, threshold sweep
  JSON written under `outputs/diagnostics/`, and results appended to
  `docs/burn_scar_audit.md` with the `<!-- generated by: ... -->` citation.
- All unit tests pass; schema test passes.
- Four gates (ruff / format / pyright / pytest) green.
- Human-Approval FLAGS A and B surfaced in `prompts/_HIL.md`; neither
  implemented until sign-off received.
- `prompts/_session_log.md` entry appended with: Phase 0 decision-rule result,
  which Phase 1 changes were implemented, Phase 2 headline metrics, and the
  explicit list of changes blocked on human approval.

## Session-log

Append a terse entry: Phase 0 `frac_above_05` result and decision-rule
outcome; Phase 1 changes shipped (reducer chosen, season window, SCL mask);
Phase 2 PR-curve headline (precision/recall/F1/IoU at 0.5 for both the
baseline and remediated COG if re-inference was approved); gates passed; FLAGS
A and B status (pending / approved / deferred).
