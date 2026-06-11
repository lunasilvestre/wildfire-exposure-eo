# Prompt 14 — FireScope local runs with fuel + meteo coupling (WU-10, "runout delirium")

> **DRAFT (2026-06-11, Nelson).** Explicitly exploratory — Nelson's term:
> *delirium*. Post-ship, post-WU-9, GPU (atlas), strictly time-boxed, zero
> claims land in the README without a fresh attended approval. This prompt
> exists so the idea is captured with its risks; expect the feasibility
> phase to kill or reshape it.

## The idea

Run FireScope's model **locally** (their code is on GitHub per the CVPR 2026
paper, arXiv:2511.17171) to produce *updated* risk rasters for the pilot
AOI, coupling two inputs the published 2026 map cannot have: (a) **fuel
variation** — our WU-5 EFFIS+COSc fuel layer as it changes (e.g. post-burn
reclassification), and (b) **meteo forecasting** — IPMA forecast fields in
place of climatology, to see whether the coupled output shifts asset-level
rankings ahead of the published annual map.

## Feasibility gate (do this first; be prepared to stop)

1. License + availability: their GitHub code license, model weights
   availability (HF? release?), and whether local inference is supported
   outside their training harness. No weights → stop, document, close.
2. Compute envelope: VLM-based reasoning-to-generation — estimate VRAM
   (RTX 3090 = 24 GB) and per-raster wall-clock from their docs/issues
   before downloading anything heavy.
3. Input contract: what exactly the model consumes (S2 composites? climate
   normals? resolution? tiling). Whether our fuel layer / IPMA fields can
   *legitimately* substitute an input, or would constitute an out-of-
   distribution hack — if the latter, the run is an experiment about model
   sensitivity, not an "updated risk map"; the write-up must say which.

## If feasible (sketch)

- Baseline reproduction: their published raster vs. a local vanilla run on
  the pilot AOI (sanity: do we reproduce their own output?).
- Coupled runs: vary fuel input (current WU-5 layer vs. post-burn variant);
  swap climatology for IPMA forecast fields; quantify raster deltas and
  asset-rank deltas with the WU-9 machinery.
- Write-up `docs/firescope_local_experiments.md`: honest framing — this is
  a sensitivity study on a third-party model outside its published
  operating envelope; "their model, our perturbations"; no operational
  claims (#9), no probability language for anything of ours (#6).

## Hard limits

- Time-box: two atlas sessions max for feasibility + first coupled run;
  then attended go/no-go with Nelson.
- No fine-tuning, no training, no redistribution of their weights.
- Nothing from this WU touches README, score config, or the shipped
  pipeline; it lives entirely in `docs/` + `outputs/`.
