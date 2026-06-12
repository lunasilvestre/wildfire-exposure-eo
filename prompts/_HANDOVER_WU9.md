# Handover — WU-9 (public GitHub Pages geobrowser + geodata publishing)

> For a **fresh Claude Code orchestrator session** (Opus-tier; the watchdog
> burns the block too, so a fresh lean context is the point). Read this, then
> `.claude/skills/usage-throttle/SKILL.md` (gate semantics + orchestrator
> playbook — the operating manual), then `prompts/15_geobrowser.md` (the WU-9
> build spec). Do not re-derive what they answer.

## State at handover (2026-06-12)

- **Stretch 2 is COMPLETE**: WU-5 (fuel layer), WU-6 (scoring), WU-7 (validation
  + README close-out), WU-8 (maps/figures) all on `main`, CI green at `abec5d1`.
  Each WU independently Fable-reviewed. README reads honestly with figures
  embedded; `docs/validation_report.md` committed.
- **WU-9 is the next (likely final) work-unit.** Spec: `prompts/15_geobrowser.md`
  (Nelson-approved). It (a) publishes the geographic outputs at full fidelity —
  scored GeoParquet (~780 KB) + fuel COG (~0.9 MB) as committed STAC assets, the
  burn-scar COG (~36 MB) as a GitHub **Release** asset (over the 2 000 kB cap);
  (b) builds a pure-static **GitHub Pages geobrowser** (MapLibre, client-side
  full-fidelity COG/vector — no downsampling) presenting study + inputs +
  outputs; (c) adds **Mermaid** pipeline / reproduction / lineage diagrams.
- **Resolved `_HIL` items** (deleted): the old "committed interactive map"
  question is superseded by WU-9; fig3 keeps "inference probability" (repo
  convention + non-negotiable #6 — that's the Prithvi detection output, not the
  exposure rank).
- **Durable context:** OB1 (search `wildfire-exposure-eo stretch-2`), file-memory
  `project_marathon_state.md` + `feedback_publish_geographic_outputs.md`, the
  `prompts/_session_log.md` stretch-2 orchestration entry, and the git log.

## Your job

1. **Gate on entry** — `scripts/dev/check_usage.sh`. Exit 3 = throttled: wait
   for the next block reset before launching (you burn the block too).
2. **Launch the driver for WU-9 on FABLE, detached:**
   ```bash
   CLOSEOUT_MODEL_DEFAULT=fable setsid bash -c \
     'echo $$ > outputs/logs/closeout_wu9.pid; \
      exec scripts/dev/run_closeout.sh WU-9' \
     >> outputs/logs/closeout_wu9_$(date +%Y%m%d).log 2>&1 &
   ```
   WU-9 hits the driver's `*)` case, so `CLOSEOUT_MODEL_DEFAULT=fable` makes the
   build Fable; the review is Fable by default. The build reads
   `prompts/15_geobrowser.md` via the WU-9 row in `00_CLOSEOUT_PLAN.md`.
3. **Arm one lean Monitor** on that log: `^=== WU-[0-9]+ : done|^REVIEW: |
   THROTTLED|HIL required|exited non-zero|produced no commits|CI red|no CI run
   appeared|Stretch complete`, plus a driver-liveness check (the driver's
   `&&`-chained gate lines exit silently via `set -e` on failure). Stay silent
   between events.
4. **On THROTTLED (exit 3):** re-arm a **time-based** waiter for the next block
   reset, then relaunch WU-9. WU-9 is a heavy Fable build + Fable review — expect
   it may span a reset.
5. **Do NOT kill the driver on a review/build throttle.** WU/review sessions
   self-gate on entry (`check_usage.sh`) and exit cheaply when throttled — there
   is no extra-usage bleed path. Let the driver exit, then reset-wait. (A prior
   orchestrator over-rotated and killed a cleanly self-throttled review —
   harmless but wasteful.)
6. **After WU-9's review is green on `main`: enable GitHub Pages via `gh api`**
   (Nelson approved). The site is under `docs/` with `docs/.nojekyll`; serve
   `/docs` on `main`. Starting point (verify the payload):
   ```bash
   gh api -X POST repos/lunasilvestre/wildfire-exposure-eo/pages \
     -f 'source[branch]=main' -f 'source[path]=/docs'
   gh api repos/lunasilvestre/wildfire-exposure-eo/pages -q .html_url   # the live URL
   ```
   Then report the Pages URL to Nelson.
7. **Headless caveat:** the WU-9 build cannot open a browser, so it cannot
   confirm the map *visually renders*. After Pages is live, surface the build's
   "human visual-check list" (in the WU-9 session-log entry) so Nelson confirms
   the live site.
8. **Anything off-playbook** — a rebuild, a data-contract/CLI/pinned-dep gate
   failure, or anything the playbook doesn't cover — stop, surface to Nelson, do
   not improvise.

## Invariants (refuse work that breaks them)

- One session owns the tree; never edit `scripts/dev/*.sh` while the driver runs;
  never leave the tree dirty between steps.
- Zero spend into extra-usage credits — the gate is the law; your wake-ups burn
  the same block. Keep the Monitor lean; stay silent between events.
- CLAUDE.md non-negotiables — and WU-9 is **public surface**: no
  probability/risk/forecast language for the exposure **rank** (#6); no
  production claims (#9); explicit CRS everywhere (#2); no invented identifiers
  (#1); every number reproducible (fact-check checklist).
- `prompts/_HIL.md` (the driver's HIL sentinel) is the only approved way a WU
  asks Nelson anything.
