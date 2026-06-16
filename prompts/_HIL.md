# Human-in-the-loop approval flags

Questions that cross a non-negotiable, a pipeline contract, or the repo's
public surface. Each must be answered by the human before the corresponding
change ships. Append new flags at the top; do not delete answered ones —
they are the audit trail.

---

## WU-19 — network / topology exposure (2026-06-16)

Branch `wu19-network-topology-exposure` (isolated worktree) builds the topology
EXTRACTOR only: `src/wildfire_exposure_eo/topology.py`, `scripts/19_topology_audit.py`,
tests, and schema/provenance wiring. `config/exposure_score.yaml` weights are
UNCHANGED — topology ships as a REPORTED / AVAILABLE secondary feature, not in the
weighted score (the weight integration is serialized later, WU-19 phase 3). Neither
flag below is actioned; branch not pushed/merged.

### FLAG C — DATA CONTRACT: new optional topology fields on `ScoredAsset`/`AssetFeatures` — OPEN

`AssetFeatures` gained three optional fields (`feeder_count`,
`network_component_size`, `network_exposure_propagated`), all defaulting to `None`
(= not covered by the graph; never imputed). This is an ADDITIVE schema change —
existing committed rows still validate — but it is still a data-contract change. No
committed parquet was regenerated. **Question for the human:** when topology enters
the weighted score (phase 3, `config/exposure_score.yaml` weight re-normalisation),
that triggers a re-score + re-publish follow-on (the published exposure parquet +
`docs/validation_report.md` would need regenerating). That step is NOT taken here —
confirm before it ships.

### FLAG D — #1 / HONESTY: the connectivity heuristics are INFERRED, not OSM-given — OPEN

OSM does not record power-flow direction or the water supply relation. The power
graph snaps line endpoints to nodes within a documented tolerance (default 50 m);
the water graph links a treatment plant to a reservoir within a documented distance
(default 2000 m). Both are flagged INFERRED in `POWER_TOPOLOGY_METHOD` /
`WATER_TOPOLOGY_METHOD` and carried in `TopologyProvenance`, and the graph is
undirected (no upstream/downstream is claimed). **Question for the human:** the
exact caveat wording for any PUBLIC surface (README, site) where the topology
feature appears should be confirmed before publication — the feature must be
presented as inferred connectivity feeding a *relative exposure rank*, never as
OSM-verified topology or a probability/forecast.

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
