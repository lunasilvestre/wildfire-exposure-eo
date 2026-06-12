# HIL — items needing Nelson's decision (WU-8 independent review, 2026-06-12)

Two items surfaced by the WU-8 review. Neither blocks the project; both touch
the public surface, so they need your ratification or a decision.

## 1. The committed interactive map is now the smoke sample — ratify or pick an option

**What the review found.** The WU-8 build committed `docs/figures/exposure_map.html`
that was actually the **smoke-AOI map** (14 assets, "rank N / 14") while the README
presented it as the pilot interactive map. Root cause: the script wrote the HTML
without a `_smoke` suffix, so the smoke gate run clobbered the pilot map before
commit — and the real pilot map (14.4 MB) could never pass the repo's 2 000 kB
pre-commit `check-added-large-files` cap anyway. The WU-8 session-log line
"HTML 0.7 MB" was the smoke file; "14.4 MB pilot" never shipped.

**What the review changed (honesty fix, already committed).**
- `--smoke` now writes `exposure_map_smoke.html`; the pilot map keeps
  `exposure_map.html` and is gitignored (too big to commit).
- The committed sample is now honestly named `docs/figures/exposure_map_smoke.html`
  and the README says exactly that, plus the one-command pilot regeneration path.

**Decision needed — what should ship long-term as prompt-12 deliverable #2?**
- **(A) Slim the pilot map under 2 MB** — replace 3 045 inline popup strings with a
  single `folium.GeoJson` layer + field-based popups, drop or downsample the
  base64 fuel overlay. Recommended: keeps the "stranger sees ranked pilot
  infrastructure" promise. ~1 session of work + review.
- **(B) Hook exception** for `docs/figures/exposure_map.html` (raise the cap for
  this one path) and commit the 14.4 MB map. Against the spirit of the cap.
- **(C) Accept the status quo** (smoke sample committed, pilot regenerable
  locally). Zero extra work; weakest visual story for repo visitors.

## 2. Prompt 12 said "the word 'probability' must not appear in any figure" — fig3 uses it

Prompt 12's constraint is stricter than CLAUDE.md #6. Fig3 (burn-scar composite)
necessarily describes the Prithvi output, which the whole repo consistently calls
"burn-scar **inference probability** … not a calibrated probability, not a fire
forecast" (see `burn_scar.py`, STAC metadata, README). The review judged repo
convention + #6 as governing, kept the term, and tightened it to "inference
probability" everywhere in fig3; fig1/fig4 captions and HTML popups use it only
in negations ("not a fire-probability estimate"). The build had not flagged this
deviation; the review does so now. If you want the word gone from fig3 entirely
(e.g., "model output score"), say so and it is a 5-minute caption change +
regeneration.
