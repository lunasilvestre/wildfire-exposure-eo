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

### FLAG A — DATA CONTRACT: composite reducer change + COG replacement — PENDING

Switching the shipped reducer from `max` to `p85` (and adding the fire-season
filter + SCL-5 mask) produces a new burn-scar COG that, if published, would
supersede already-published artefacts:

- the committed STAC item under `stac/burn-scar-recent/` (href + checksums change);
- the R2-hosted display COG at `wildfire.cheias.pt` (the geobrowser reads it);
- `recent_burn_share_12mo` for every scored asset (exposure parquet + the
  numbers in `docs/validation_report.md` would change).

What this WU did NOT do (correctly, per the HIL boundary):
- did not upload anything to Cloudflare R2;
- did not overwrite / repoint the live `stac/burn-scar-recent/` item;
- did not change `docs/app/data/style_data.json` hrefs;
- did not regenerate the exposure parquet or `docs/validation_report.md`;
- did not push the branch to origin.

To accept the remediation, the human must approve and a follow-on WU must:
1. run the full inference pipeline on the pilot AOI with the chosen reducer
   (the WU-10 candidate COGs already exist under `outputs/cogs/` — see the
   session-log entry for the recommended reducer + paths);
2. re-upload the display COG to R2;
3. re-point the STAC item href + refresh checksums;
4. re-run `scripts/11_validate.py` to refresh `docs/validation_report.md`;
5. spawn a downstream WU to re-score exposure (parquet refresh).

**Question for the human:** approve adopting the recommended reducer as the
shipped `config/burn_scar.yaml` default and scheduling the publish + re-score
follow-on? (The config change itself is committed on the WU-10 branch but no
live artefact has been touched.)

### FLAG B — PIPELINE CONTRACT: land-cover / NDVI guard in feature extraction — DEFERRED (not implemented)

Adding an NDVI guard or a fuel-class restriction inside
`recent_burn_share_12mo` in `features.py` would change the feature value for
every scored asset and invalidate the current exposure parquet +
`docs/validation_report.md`. Out of scope for WU-10 and NOT implemented here.
`features.py` is unchanged on this branch.

**Question for the human:** is a land-cover/NDVI guard wanted as a separate
WU, or does the reducer + SCL-5 + season-window remediation suffice? (The
Phase-2 cross-tab of predicted-positive vs fuel class — in the PR-curve JSON
and `docs/burn_scar_audit.md` — is the evidence base for this decision.)
