#!/usr/bin/env bash
# Usage throttle gate for marathon close-out runs.
# Exit 0 = proceed. Exit 3 = throttle (caller must checkpoint and stop).
#
# Primary source: claude-usage-tracker's data/latest.jsonl — real percentages
# from the claude.ai usage API, refreshed every 15 min by the crontab entry
# on atlas (claude-track --quiet). These ARE the in-app /usage meter numbers.
# Fallback: ccusage token-count heuristic. Budget calibrated 2026-06-10:
# ccusage 19,235,855 block tokens <-> in-app "56% used" => ~34M tokens/block
# in ccusage counting (cache reads dominate).
#
# Env knobs:
#   CLOSEOUT_SESSION_PCT_MAX     throttle at/above this session % (default 80)
#   CLOSEOUT_WEEKLY_PCT_MAX      throttle at/above this weekly all-models % (default 90)
#   CLOSEOUT_TRACKER_LATEST      tracker snapshot path (default: atlas location)
#   CLOSEOUT_TRACKER_MAX_AGE_MIN max snapshot age before fallback (default 35)
#   CLOSEOUT_BLOCK_TOKEN_BUDGET  fallback budget, tokens per 5h block (default 34_000_000)
set -uo pipefail

PCT_MAX="${CLOSEOUT_SESSION_PCT_MAX:-${CLOSEOUT_BLOCK_PCT_MAX:-80}}"
WEEKLY_MAX="${CLOSEOUT_WEEKLY_PCT_MAX:-90}"
LATEST="${CLOSEOUT_TRACKER_LATEST:-$HOME/Documents/dev/claude-usage-tracker/data/latest.jsonl}"
MAX_AGE_MIN="${CLOSEOUT_TRACKER_MAX_AGE_MIN:-35}"
BUDGET="${CLOSEOUT_BLOCK_TOKEN_BUDGET:-34000000}"

# --- Primary: claude-usage-tracker snapshot ---------------------------------
if [ -r "$LATEST" ] && command -v jq >/dev/null; then
  age_min=$(( ( $(date +%s) - $(stat -c %Y "$LATEST") ) / 60 ))
  if [ "$age_min" -le "$MAX_AGE_MIN" ]; then
    session=$(jq -r '.session_pct // empty' "$LATEST")
    weekly=$(jq -r '.weekly_all_pct // 0' "$LATEST")
    reset=$(jq -r '.session_reset // "unknown"' "$LATEST")
    if [ -n "$session" ]; then
      echo "check_usage: session ${session}% (throttle at ${PCT_MAX}%), weekly ${weekly}% (max ${WEEKLY_MAX}%) — tracker tick ${age_min} min old"
      echo "check_usage: session resets at ${reset}"
      over=$(awk -v s="$session" -v w="$weekly" -v sm="$PCT_MAX" -v wm="$WEEKLY_MAX" \
        'BEGIN { print (s >= sm || w >= wm) ? 1 : 0 }')
      [ "$over" = 1 ] && exit 3
      exit 0
    fi
  else
    echo "check_usage: tracker snapshot ${age_min} min old (> ${MAX_AGE_MIN}) — falling back to ccusage heuristic" >&2
  fi
fi

# --- Fallback: ccusage token-count heuristic --------------------------------
json=$(npx --yes ccusage@latest blocks --active --json 2>/dev/null) || {
  echo "check_usage: ccusage unavailable — proceeding WITHOUT throttle guard" >&2
  exit 0
}

CCUSAGE_JSON="$json" python3 - "$BUDGET" "$PCT_MAX" <<'PY'
import json, os, sys
budget, pct_max = int(sys.argv[1]), float(sys.argv[2])
data = json.loads(os.environ["CCUSAGE_JSON"])
blocks = data.get("blocks", data if isinstance(data, list) else [])
active = [b for b in blocks if b.get("isActive")] or blocks[-1:] if blocks else []
if not active:
    print("check_usage: no active block — fresh window, proceeding")
    sys.exit(0)
b = active[-1]
used = b.get("totalTokens") or sum(
    b.get(k, 0) for k in ("inputTokens", "outputTokens",
                          "cacheCreationInputTokens", "cacheReadInputTokens"))
pct = 100.0 * used / budget
print(f"check_usage: {used:,} tokens this block ≈ {pct:.0f}% of "
      f"{budget:,} budget (throttle at {pct_max:.0f}%)")
if reset := b.get("endTime"):
    print(f"check_usage: block resets at {reset}")
sys.exit(3 if pct >= pct_max else 0)
PY
