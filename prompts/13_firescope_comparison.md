# Prompt 13 — FireScope comparison (WU-9, post-ship)

> **DRAFT (2026-06-11, Nelson + orchestration session).** Post-ship work —
> NOT part of the close-out plan's Definition of DONE and not in the
> stretch-2 driver queue. Refine at a post-ship checkpoint before execution.
> Purpose framing from Nelson: our pipeline is an honest first approach but
> may be insufficient to graduate as a reference by itself — benchmark it
> against the field's current public reference.

## Purpose

Quantify how the repo's transparent per-asset exposure rank relates to, and
validates against, **FireScope** (INSAIT, CVPR 2026, arXiv:2511.17171 — the
public Europe-wide wildfire risk map, CC-BY-4.0): per-asset rank agreement on
the pilot AOI, head-to-head historical validation against ICNF burns under
our own WU-7 harness, on the **widest evaluation universe the data
supports**, documented with analytics-grade figures and an explicit verdict.

## Phase 0 — verify before anything (non-negotiable #1)

The HF dataset `INSAIT-Institute/firescope-risk-2026` has an empty card,
`imagefolder` packaging, ~12.3 GB, broken viewer. Before any design:
list the repo file tree (`huggingface_hub` API), determine: actual contents
(georeferenced rasters? tiles? CRS? resolution? vintage), whether parquet
sidecars exist. Decision rule (Nelson): **if parquet exists, consume it
remotely/wisely via DuckDB** (`httpfs`/`hf://`) without a full download;
otherwise **download to atlas** (~12.3 GB, document sha256s in a manifest)
and build an explicit-CRS reader. If the dataset turns out not to contain
the usable Europe risk raster, stop and report — check their GitHub
release/paper artefacts as the alternative source; do not improvise.

## Deliverables (sketch — tighten at the checkpoint)

1. `firescope.py` — reader + explicit-CRS alignment of the FireScope raster
   to our grids; provenance (dataset revision sha, file shas, license note).
2. **Asset-level comparison (pilot AOI):** zonal FireScope risk per asset
   buffer → percentile rank → Spearman + rank-agreement plots vs. our
   exposure rank; top-decile overlap table; per-class breakdown.
3. **Head-to-head historical validation:** both rankers through the
   *identical* WU-7 leakage-clean harness (backdated window, post-window
   ICNF vintages): lift + Spearman side by side. Run on the pilot AOI and,
   if FireScope coverage + compute permit, on the **widest universe**:
   every ICNF region with post-window burns (raster-vs-burns at district or
   national scale — assets only exist for the pilot, so the wide run is
   raster-level, not asset-level; say so plainly).
4. **Analytics report** `docs/firescope_comparison.md` + figures via a
   `scripts/13_*.py` (house rule: every number script-generated): delta
   maps (our rank − FireScope rank, pilot), lift curves overlay, historical
   performance table, and a written **verdict** of the repo's method
   against the state-of-the-art prediction engine, citing their paper and
   firescope.ai/research. Honest in both directions: where the learned
   model wins, say so; where the transparent screen holds up, say that too.
5. README: one results line linking the comparison report (public surface →
   HIL/attended approval, as ever).

## Constraints

- **Vocabulary firewall (#6):** FireScope outputs are "risk" *in their
  terminology* (attributed, quoted); everything ours remains *exposure
  rank*. The comparison never converts our rank into a probability.
- CC-BY-4.0 attribution wherever their data appears (figures, report,
  README line). No redistribution of their raster in this repo — derived
  comparison artefacts only, under `outputs/` or `docs/figures/`.
- No training, no fine-tuning, no model execution in this WU (that is
  prompt 14's delirium). Consumption of their published raster only.
- New deps need #8 justification (likely only `huggingface_hub`, pinned).

## Done when (sketch)

Phase-0 findings logged; comparison + validation reproducible from scripts;
report + figures committed; verdict written; session log entry; CI green.
