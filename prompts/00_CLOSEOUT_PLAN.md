# Prompt 00 — Close-out plan (orchestrator)

> **This file is the direction.** Every remaining Claude Code session on this
> repo starts here, picks exactly one work-unit, executes its loop, and stops.
> Decision record: 2026-06-09, Cowork interview with Nelson — `ob1:7102ad38`.

## Goal

Close `wildfire-exposure-eo` as a finished, honest, public-data demonstrator:
a thin end-to-end slice from OSM assets to validated per-asset exposure
ranks — **GPU-first on atlas (RTX 3090) for inference, CPU-reproducible for
the demo** — CI green on `main`, README telling the truth, shippable as a
portfolio piece **this week**.

## Scope decision (what changed vs. the original plan)

**KILLED — do not build, do not leave TODOs implying they're coming:**

- §7 SegFormer baseline training and §8 foundation-model fine-tuning. No
  training of any kind — GPU-first means *inference* on atlas, not
  restoring fine-tuning. The CPU demo path must work without atlas.
- §6 weak-label fusion *as a training-label pipeline*. The COSc + COS join
  survives only as an input to the fuel-layer derivation below.

**KEPT (confirmed by Nelson 2026-06-09):**

- EFFIS pan-European fuel map + DGT COS/COSc as the fuel layer (no ML).
- Pretrained `Prithvi-EO-2.0-300M-BurnScars` **inference only** (prompt 09)
  for the `recent_burn_share_12mo` feature. **GPU-first, ON the critical
  path** (amended 2026-06-09, `ob1:2dea2f03`): runs early as WU-1 on atlas
  and produces the pre-baked burn-scar COG the CPU demo ships with.
- ETH GCH canopy-height features.
- Full validation vs. ICNF burns (lift / Spearman, temporal-leakage rule).

**Narrative (amended 2026-06-09, `ob1:a7b7386b`).** The framing is **civic
tech**, not vendor-adjacent: *which schools, substations, water plants and
fire stations in a Portuguese fire district are most exposed this season —
ranked from open data, fully reproducible, with per-asset provenance.* The
README, validation report and WU-8 maps all speak to municípios / civil
protection / open-data audiences first. Do not frame the project around any
specific company's product; commercial adjacency may appear only as one line
in a "related work" note. No new foundation models on the critical path —
TerraMind and friends belong in a future-work paragraph, nothing more.

**Rationale.** The methodology is current (Prithvi-EO-2.0 BurnScars, TerraTorch
1.0, TerraMind all active as of June 2026) — the risk is time, not
obsolescence. The repo has been dark on GitHub with broken CI since May 6
while being the named portfolio piece for active applications.

## Repo state (as of 2026-06-09)

- Last commits: `dab6bb2`, `c21d38b` (May 6). CI on `main` failed with
  "No jobs were run" — diagnose in WU-0 (suspects: missing `.python-version`
  on the pushed tree, workflow not on default branch at trigger time).
- **Uncommitted but gated work in the tree** from the May 14/15 sessions:
  prompts 01 + 03 deliverables (`audit.py` completion, `stac.py`,
  `schemas/`, 46 passing tests, CLI). See `prompts/_session_log.md`.
- The May 14 parallel sessions on prompts 02 / 04 / 05 died without
  deliverables. Their scope is re-dispatched as WU-2..WU-4. Do not look for
  their transcripts; the tree + session log are the state.

## Work-units (strictly sequential — one CC session each)

| WU | Prompt | Deliverable | Gate notes |
|---|---|---|---|
| **WU-0** | this file, §WU-0 below | Existing work committed; CI green on `main`; push | The four commands + CI run visible green on GitHub |
| **WU-1** *(atlas, GPU)* | `09_burn_scar_inference.md` | Pretrained Prithvi burn-scar COG over the pilot AOI → feeds `recent_burn_share_12mo`; pre-baked artifact for the CPU demo | Depends only on WU-0 (committed STAC resolver + AOI). Smoke AOI inference before pilot. Session lands code + launches the pilot run; a long pilot inference may churn after the session ends — log it, don't wait |
| **WU-2** | `02_extract_osm.md` | `osm.py`, `fetch-osm`, asset GeoParquet | As written |
| **WU-3** | `04_static_raster_fetch.md` | `static_rasters.py`, `fetch-rasters`, cache + manifest | As written; EFFIS/COS are now *score inputs*, not training inputs |
| **WU-4** | `05_icnf_burns_ingestion.md` | `burns.py`, `fetch-burns`, burns GeoParquet | As written |
| **WU-5** | `06_fuel_layer.md` — **write it first** (see below) | Fuel-class raster on the pilot grid from EFFIS + COSc via the Scott & Burgan crosswalk | No ML; pure raster reclass + resample, CRS explicit |
| **WU-6** | `10_asset_features_score.md` — **write it first** | Per-asset features (zonal stats per `docs/methodology.md` §10, incl. `recent_burn_share_12mo` from WU-1) + composite exposure **rank** per `config/exposure_score.yaml` | Non-negotiable #6: rank, never probability |
| **WU-7** | `11_validation_closeout.md` — **write it first** | `validation_report.md` (lift / Spearman vs. ICNF, leakage rule §12), README close-out, `--smoke` demo path documented | Fact-checking checklist applies to every number |
| **WU-8** | `12_maps_story.md` — **write it first** | Visual deliverables: static map figures (assets coloured by exposure rank over an S2 true-colour composite; fuel layer; burn-scar COG; ICNF burn overlays; lift curve) + **one** exported self-contained HTML map under `docs/figures/`, embedded in README | New viz dep (e.g. `folium`/`lonboard`) needs the non-negotiable #8 justification + pin. Figures are generated by a script under `scripts/`, never hand-made |

**Concurrency rule.** Repo edits stay strictly sequential — one CC session
owns the tree at a time (parallel sessions are how May 14 stranded the work).
The one allowed overlap: WU-1's *pilot-AOI inference job* may keep running on
atlas after its session has committed, logged, and stopped; WU-2 may then
start. If the job is still running when WU-6 needs the COG, WU-6 waits.

**"Write it first" protocol (WU-4/5/6):** the prompt file does not exist yet.
The first half of the session drafts it in the house style of prompts 02–05
(Purpose / Prerequisites / Deliverables with Pydantic schemas / Done-when),
sourcing specs from `docs/methodology.md` §6, §10–§12, §14 and
`config/exposure_score.yaml`. Present the draft to the human for approval
**before implementing** (CLAUDE.md "Don't surprise the human"). Then execute it.

### WU-0 detail (do this one first, today)

1. `git status` — review the diff; everything listed in
   `prompts/_session_log.md` 2026-05-14/15 entries is expected. Anything
   unexpected: stop and ask.
2. Run the four gates (`ruff check`, `ruff format --check`, `pyright`,
   `pytest`). They were green on May 15; if drift, fix before committing.
3. Commit in two logical commits: (a) prompts + docs + session log,
   (b) `src/` + `tests/` (audit completion + STAC resolver).
4. Diagnose CI "No jobs were run" on the GitHub Actions run for `c21d38b`;
   fix; push; confirm green on GitHub. The two follow-ups flagged in the
   2026-05-15 session-log entry (CI spot-check of a committed STAC-manifest
   sample; `docs/samples/stac_smoke_aveiro.json` fixture) need human
   approval in-session — ask, don't assume.
5. Session-log entry; stop.

## Session loop (every WU, no exceptions)

```
read CLAUDE.md end-to-end
read this file + the WU's prompt file end-to-end; confirm prerequisites
TodoWrite the plan
implement (smoke AOI before pilot AOI — verify-then-act)
gates: uv run ruff check . && uv run ruff format --check . \
  && uv run pyright src tests scripts && uv run pytest
(+ stac-validator / validate-schema when touched)
append prompts/_session_log.md entry
commit (small, scoped); push only if CI was green at session start
STOP — do not start the next WU in the same session
```

## Marathon mode (amended 2026-06-09, `ob1:10ef1401`)

Unattended continuous execution is allowed in **two stretches**, driven by
`scripts/dev/run_closeout.sh` (one fresh headless session per WU, gates +
CI check + HIL sentinel between WUs, usage gate via the `usage-throttle`
project skill):

- **Stretch 1 — WU-2 → WU-4** (specs already exist; no approval points).
  May start as soon as WU-1's code lands.
- **Human checkpoint** — the four "write it first" prompts (WU-5..WU-8) are
  batch-drafted in one attended session and approved together. This is the
  one place a human must read before code gets written.
- **Stretch 2 — WU-5 → WU-8**, except the final README status change
  (public surface) which still requires explicit approval.

Sessions needing a human mid-stretch write `prompts/_HIL.md`, commit it,
and exit; the driver halts. Unattended effort is `high`, never ultracode.

## Stop conditions (halt and surface to the human)

- Any CLAUDE.md non-negotiable would be violated to make progress.
- A gate fails and the fix requires touching the data contract, CLI surface,
  or pinned deps.
- An external endpoint (Overpass, ICNF ArcGIS, DGT, EFFIS) is down — record
  in `docs/data_sources.md`, don't paper over.
- A WU exceeds ~2× its expected effort. Expected: WU-0 ≤ 1 h; WU-1 one
  atlas session (excl. pilot-inference wall-clock); WU-2..WU-4 one session
  each; WU-5..WU-7 one session each incl. prompt drafting.
- atlas unreachable or `torch.cuda.is_available()` false at WU-1 start —
  surface to the human; fall back to shipping without
  `recent_burn_share_12mo` only with explicit approval.

## Methodological caveats (encode in the WU-6 / WU-7 prompts)

1. **Feature/label circularity.** `historical_burn_count_25y` is a feature
   and ICNF polygons are the validation labels. The leakage split (validate
   only on years strictly after the score-input window) is necessary but not
   sufficient — fire is spatially autocorrelated, so lift will flatter the
   score. WU-7 must report an ablation: lift/Spearman **with and without**
   the burn-history features, and say plainly which features carry the
   signal. A validation section that hides this would be marketing, not
   validation.
2. **Prithvi domain shift.** The BurnScars checkpoint was trained on US HLS
   fire scenes; Portuguese eucalyptus/pinus mosaics are out-of-domain. WU-1
   must include a cheap sanity check before the COG is trusted: run
   inference over 2–3 known ICNF burn perimeters from the most recent
   published vintage and report IoU/agreement in the session log. If
   agreement is poor, the feature ships with a documented reliability
   caveat (or is dropped — human call).
3. **Fuel-map scale mismatch.** EFFIS fuel classes are coarse relative to
   10 m asset buffers; COSc refinement only partially compensates. State
   the effective resolution honestly in `validation_report.md`; never imply
   parcel-level fuel precision.
4. **The score is a screening rank.** Weights in `exposure_score.yaml` are
   expert-set, not learned. The defensible claim is "a transparent,
   reproducible prioritization screen validated against subsequent burns" —
   not prediction. README and report language must keep this line.

## Definition of DONE for the project

- `uv run wildfire-exposure-eo` demo path: fresh clone → audit → fetch-osm →
  fetch-rasters → fetch-burns → fuel-layer → score → validate on the smoke
  AOI, CPU-only with the pre-baked WU-1 burn-scar COG, under the §14
  30-minute budget. The GPU path (atlas, prompt 09) is documented as the
  reproduction route for that artifact.
- CI green on `main`; `validation_report.md` committed with reproducible
  numbers; README status section updated from "Pre-development" to a dated,
  honest "Demonstrator complete — scope" note with the civic-tech framing
  (requires human approval: public surface).
- WU-8 figures committed under `docs/figures/` and embedded in the README —
  a reviewer should *see* ranked infrastructure on satellite imagery within
  ten seconds of opening the repo, before reading a single paragraph.
- No dangling references to killed phases (§7/§8 training) anywhere in
  README, docs, or prompts.
