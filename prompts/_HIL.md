# Human-in-the-loop approval flags

Questions that cross a non-negotiable, a pipeline contract, or the repo's
public surface. Each must be answered by the human before the corresponding
change ships. Append new flags at the top; do not delete answered ones —
they are the audit trail.

---

## WU-10 — burn-scar remediation (2026-06-15)

Status of the two flags from `prompts/16_burn_scar_remediation.md`. The WU-10
branch `wu10-burn-scar-remediation` implements Phase 1 (reducer + season window
+ SCL-5 mask) and Phase 2 (FP validation harness) and produces a CANDIDATE
remediated COG under `outputs/` only. Neither flag below has been actioned.

### FLAG A — DATA CONTRACT: composite reducer change + COG replacement — CLOSED (2026-06-16)

**Resolution (Pillar 6 / WU-22, 2026-06-16):** the premise "no live artefact
has been touched" was **false** when this flag was written. The publish has
already happened:

- STAC item `stac/burn-scar-recent/burn-scar-20260615T192025Z/` is on `main`.
- `docs/app/data/style_data.json` → `burn_scar.href` →
  `https://wildfire.cheias.pt/burn_scar_3857_20260615T192025Z.tif` (run-id
  `20260615T192025Z`, reducer p85, tile_origin_jitter, 109 scenes).
- The R2 upload was handed to the human in the "WU-10 burn-scar PUBLISH"
  session-log entry (2026-06-15) with the exact `rclone copyto` commands and
  sha256 checksums.

**Call (a) — FLAG A is fully closed.** The exposure parquet
(`exposure_20260611T170549Z.parquet`, scored at `988e59f2…`, validated at
`4877c5d…`) does **not** depend on the new COG in the backdated validation
window: `recent_burn_share_12mo` is **correctly nulled** in the backdated run
(the scoring code guards against leakage, as documented in
`docs/validation_report.md` and confirmed by `src/wildfire_exposure_eo/features.py`).
The validation-report numbers cite the backdated parquet, which is unaffected by
the reducer change. No re-score or re-validation is required to maintain the
current report's integrity.

**Follow-on (Pillar 2, not blocking):** a forward-looking re-score — using the
new p85 COG with a window that includes the recent season — would incorporate
`recent_burn_share_12mo` and potentially improve the score. That is the
exposure re-score item listed in the original FLAG A step 5. It is now a
**non-blocking follow-on**: it improves future score quality but does not fix
any current inconsistency. It belongs to Pillar 2 (widen validation) or a
standalone re-score WU when the feature is confirmed stable.

**Original FLAG A text preserved below for the audit trail:**

> Switching the shipped reducer from `max` to `p85` (and adding the fire-season
> filter + SCL-5 mask) produces a new burn-scar COG that, if published, would
> supersede already-published artefacts:
>
> - the committed STAC item under `stac/burn-scar-recent/` (href + checksums change);
> - the R2-hosted display COG at `wildfire.cheias.pt` (the geobrowser reads it);
> - `recent_burn_share_12mo` for every scored asset (exposure parquet + the
>   numbers in `docs/validation_report.md` would change).
>
> What this WU did NOT do (correctly, per the HIL boundary):
> - did not upload anything to Cloudflare R2;
> - did not overwrite / repoint the live `stac/burn-scar-recent/` item;
> - did not change `docs/app/data/style_data.json` hrefs;
> - did not regenerate the exposure parquet or `docs/validation_report.md`;
> - did not push the branch to origin.
>
> **Question for the human (answered — DONE):** the reducer was approved and
> published. See "WU-10 burn-scar PUBLISH" session-log entry (2026-06-15).

### FLAG B — PIPELINE CONTRACT: land-cover / NDVI guard in feature extraction — OPEN, DEFERRED

**Status (2026-06-16):** open and explicitly deferred. Not in the Pillar 6
(housekeeping) or any current Wave-1 WU scope. `features.py` is unchanged.

Adding an NDVI guard or a fuel-class restriction inside
`recent_burn_share_12mo` in `features.py` would change the feature value for
every scored asset and invalidate the current exposure parquet +
`docs/validation_report.md`. The evidence base for the decision is:

- The Phase-2 cross-tab of predicted-positive vs fuel class (`docs/burn_scar_audit.md`,
  "WU-10 tiling artifact — diagnosis and de-grid") and the PR-curve JSON
  under `outputs/diagnostics/16_pr_curve_degrid_*.json`.
- The SCL-5 (bare soil) mask is already applied per-scene before compositing
  (WU-10 Phase 1); that partially addresses the concern.

**Question for the human (still open):** is a land-cover/NDVI guard wanted as
a separate WU, or does the reducer + SCL-5 + season-window remediation suffice?
This is not blocking the current program — Pillar 2 (widen validation) will
provide more signal to evaluate whether the guard is needed.

This flag will be resolved when either (a) the Pillar 2 multi-AOI validation
makes the answer clear, or (b) the human explicitly approves or declines the
guard as a follow-on WU.
