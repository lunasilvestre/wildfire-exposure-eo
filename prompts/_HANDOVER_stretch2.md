# Handover — stretch 2 orchestration (WU-5 → WU-8)

> For a **fresh Claude Code session on Opus** acting as marathon watchdog.
> Read this, then `.claude/skills/usage-throttle/SKILL.md` (gate semantics +
> orchestrator playbook — it is the operating manual), then the stretch-1
> entry in `prompts/_session_log.md`. Do not re-derive what they answer.

## State at handover (2026-06-11)

- Stretch 1 (WU-2..4) complete: OSM assets, static rasters, ICNF burns all
  on `main`, CI green. Stretch-1 narrative: session log 2026-06-10 entries;
  OB1 ids: 10ef1401 (marathon decision), 3f6dceb0 (gate v3), daa52e1c
  (stretch-1 close).
- Prompts 06 / 10 / 11 / 12 (WU-5..8) drafted and approved by Nelson at the
  attended checkpoint (approval recorded in the chat + this commit). The
  pre-approved decisions (deps, FWI fallback, burn-share threshold, README
  proposal flow) live INSIDE each prompt — builders and reviewers read them
  there.

## Your job

1. Launch the driver detached, exactly like stretch 1:
   ```bash
   setsid bash -c 'echo $$ > outputs/logs/closeout_stretch2.pid; \
     exec scripts/dev/run_closeout.sh WU-5 WU-6 WU-7 WU-8' \
     >> outputs/logs/closeout_stretch2_$(date +%Y%m%d).log 2>&1 &
   ```
   The driver handles per-WU usage gating (time-sensitive, 50 % floor),
   Sonnet builds for WU-5/WU-8, default-model builds for WU-6/WU-7,
   the independent review pass, gates ×2, push, CI watch.
2. Arm one lean Monitor on the log: `^=== WU-[0-9]+ : done|^REVIEW: |
   THROTTLED|HIL required|exited non-zero|produced no commits|CI red|
   no CI run appeared|Stretch complete`. Stay silent between events.
3. **Expected halt:** WU-7 ends by writing `prompts/_HIL.md` (README
   close-out approval — public surface). This is by design, exit code 2.
   Notify Nelson, wait. After he approves and deletes `_HIL.md`: relaunch
   the driver with `WU-8` only.
4. On THROTTLED (exit 3): re-arm a **time-based** waiter for the next block
   reset with the remaining WUs (playbook rule — never a gate-based waiter
   from below the threshold).
5. Anything off-playbook: stop, surface to Nelson, do not improvise.

## Invariants (refuse work that breaks them)

- One session owns the tree; never edit `scripts/dev/*.sh` while the driver
  runs; never leave the tree dirty between WUs.
- Zero spend into extra-usage credits — the gate is the law; you are also
  bound by it (your wake-ups burn the same block).
- CLAUDE.md non-negotiables; the driver's HIL sentinel is the only approved
  way a WU asks Nelson anything.
