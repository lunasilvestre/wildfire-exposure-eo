---
name: usage-throttle
description: Token-budget discipline for unattended ("marathon") close-out sessions in this repo. Use at the start of any WU session launched by scripts/dev/run_closeout.sh, before any long or token-heavy phase (large file sweeps, verification passes), and whenever the user mentions usage limits, 5x plan, throttling, marathon mode, or continuous runs.
---

# Usage throttle

This repo's close-out may run unattended on a Claude Max subscription.
Subscription blocks (5-hour windows) and weekly caps are finite; the user
has an extra-usage credit buffer that should be treated as an emergency
reserve, not working capital.

## Rules for any unattended session

1. **Gate on entry.** Run `scripts/dev/check_usage.sh`. Exit code 3 means
   throttled: do not start substantive work — checkpoint (commit anything
   clean, append `prompts/_session_log.md`) and end the session.
2. **Effort discipline.** Unattended WUs run at high effort, never
   ultracode — workflow fan-out multiplies token burn and is reserved for
   the attended WU-7 verification pass.
3. **Waits are cheap, context is not.** Network fetches, GPU inference and
   test runs cost wall-clock, not tokens — never busy-poll by re-running
   commands that re-print large output. Redirect long output to
   `outputs/logs/<wu>-<step>.log` and tail the last 30 lines.
4. **Don't re-read what you know.** Read big docs once; for code you just
   wrote, trust the tree. Avoid `git diff` of generated artifacts.
5. **Mid-session check.** Before any phase you expect to be token-heavy
   (full-AOI runs with verbose logs, multi-file refactors), re-run the gate.
   If throttled mid-WU: finish the smallest coherent step, commit, log,
   stop — a clean half-WU beats a corrupted full one.
6. **Never bleed silently into extra-usage credits.** If the gate is
   unavailable (ccusage missing) AND the session is unattended, mention the
   blind spot in the session log so the human can check `/usage` manually.

## Data sources

The gate reads, in order of preference:

1. **claude-usage-tracker** (`~/Documents/dev/claude-usage-tracker/data/latest.jsonl`)
   — real session/weekly percentages from the claude.ai usage API, refreshed
   every 15 min by the crontab entry on atlas. These ARE the in-app `/usage`
   meter numbers; no calibration needed. Skipped if the snapshot is older
   than 35 min (cron broken / cookie expired).
2. **ccusage fallback** — token-count heuristic. `CLOSEOUT_BLOCK_TOKEN_BUDGET`
   (default 34M tokens/block) was calibrated 2026-06-10 against the in-app
   meter: ccusage reported 19,235,855 block tokens at "56% used" → ≈34M
   tokens per block in ccusage counting (cache reads dominate). Re-check the
   alignment occasionally and adjust the default in
   `scripts/dev/check_usage.sh` if it drifts.

## Gate rule (time-sensitive, Nelson 2026-06-10)

A WU (build + independent review) costs **~35% of a 5h block** with Sonnet
builds and Fable reviews. The gate therefore requires real headroom — unless
the block reset is imminent, in which case the WU mostly runs in the next
block and high usage is irrelevant:

```
allowed_used% = max(FLOOR, CEIL − minutes_to_reset)    # FLOOR 50, CEIL 92
90% used &  2 min to reset → pass     70% used & 20 min → pass
80% used & 10 min to reset → pass     70% used &  3 h   → throttle
```

Weekly all-models ≥ 90% always throttles. Knobs: `CLOSEOUT_SESSION_PCT_MAX`
(floor), `CLOSEOUT_PCT_CEIL`, `CLOSEOUT_WEEKLY_PCT_MAX`.

## Marathon operations doctrine (lessons, 2026-06-10)

1. **The watchdog burns the block too.** An interactive monitoring session
   re-reads its whole context per wake-up (~1–2%/turn at long context on the
   top model). Keep monitor filters lean (verdicts, completions, failures —
   not phase markers) and stay silent between events.
2. **Resume-waiter conditions.** A "relaunch when the gate passes" waiter
   only waits if usage is currently *above* threshold — if you stopped below
   it, it fires immediately. When the intent is "after the reset", gate on
   the reset *time* first, then re-check the usage gate.
3. **Never edit a script a running bash is executing** — bash re-reads at
   saved byte offsets; the running instance keeps the old loop body and may
   mis-parse the file tail. Queue the patch; apply after the driver exits.
4. **Never leave the tree dirty while WU sessions run** — they are
   instructed to stop-and-ask on unexpected git status. Stash interrupted
   partials with a descriptive message instead.
5. **Headless `-p` sessions are one-shot.** They must never arm monitors or
   background jobs expecting re-invocation; long waits are synchronous
   polls, and the machine-readable verdict goes in the final line.

## Orchestrator playbook (the watchdog session)

- Run the watchdog as a **fresh session per stretch** on a cheaper strong
  model (Opus-tier suffices once this playbook exists) — state lives in
  `prompts/_session_log.md`, the driver log, and OB1, not in chat history.
  Context length × wake-up count dominates watchdog cost, not model price.
- Monitor filter: `REVIEW:` verdicts, `WU-n : done`, THROTTLED / HIL /
  exit / "produced no commits" lines only. Stay silent between events.
- A quiet driver log is normal — WU sessions write to their own files
  under `outputs/logs/`. Stall triage order: process tree (a child means a
  tool is running), session transcript mtime under
  `~/.claude/projects/<project-slug>/`, CPU delta over 10 s.
- Give an apparently stalled API call **≥ 10 min** (SDK retry envelope)
  before killing. And remember silence is ambiguous: a *finished* session
  is exactly as quiet as a wedged one — pair any mtime watcher with a
  process-liveness check and the driver log's phase markers.
- Intervention = process-group kill via the recorded pid, stash partials
  with a descriptive message, verify the tree is clean, then re-arm:
  **time-based waiter** when the intent is "after the block reset",
  gate-based waiter only when currently above threshold.
- Escalate to the human instead of improvising when: a WU needs a rebuild,
  a gate failure touches the data contract / CLI surface / pinned deps,
  or anything off-playbook appears.
