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

Throttle thresholds: session ≥ 80% (`CLOSEOUT_SESSION_PCT_MAX`) or weekly
all-models ≥ 90% (`CLOSEOUT_WEEKLY_PCT_MAX`).
