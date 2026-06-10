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
# Gate rule (Nelson, 2026-06-10): start a WU only if enough of the session
# block remains for a full build+review (~35%), UNLESS the reset is imminent —
# then the WU mostly runs in the next block and high usage is fine:
#   allowed_used% = max(FLOOR, CEIL - minutes_to_reset)
#   e.g. floor 50 / ceil 92:  90% & 2 min -> pass;  80% & 10 min -> pass;
#        70% & 20 min -> pass;  70% & 3 h -> throttle.
#
# Env knobs:
#   CLOSEOUT_SESSION_PCT_MAX     floor: max used% far from reset (default 50)
#   CLOSEOUT_PCT_CEIL            ceiling for the time-relief ramp (default 92)
#   CLOSEOUT_WEEKLY_PCT_MAX      throttle at/above this weekly all-models % (default 90)
#   CLOSEOUT_TRACKER_LATEST      tracker snapshot path (default: atlas location)
#   CLOSEOUT_TRACKER_MAX_AGE_MIN max snapshot age before fallback (default 35)
#   CLOSEOUT_BLOCK_TOKEN_BUDGET  fallback budget, tokens per 5h block (default 34_000_000)
set -uo pipefail

PCT_FLOOR="${CLOSEOUT_SESSION_PCT_MAX:-50}"
PCT_CEIL="${CLOSEOUT_PCT_CEIL:-92}"
WEEKLY_MAX="${CLOSEOUT_WEEKLY_PCT_MAX:-90}"
LATEST="${CLOSEOUT_TRACKER_LATEST:-$HOME/Documents/dev/claude-usage-tracker/data/latest.jsonl}"
MAX_AGE_MIN="${CLOSEOUT_TRACKER_MAX_AGE_MIN:-35}"
BUDGET="${CLOSEOUT_BLOCK_TOKEN_BUDGET:-34000000}"

# minutes until a given ISO-8601 timestamp (fractional seconds tolerated);
# prints 99999 if unparsable/empty.
mins_until() {
  local ts="${1:-}" epoch
  [ -n "$ts" ] || { echo 99999; return; }
  epoch=$(date -ud "$(printf '%s' "$ts" | sed 's/\.[0-9]*//')" +%s 2>/dev/null) \
    || { echo 99999; return; }
  local m=$(( (epoch - $(date +%s)) / 60 ))
  [ "$m" -lt 0 ] && m=0
  echo "$m"
}

verdict() { # $1=used% $2=mins_to_reset $3=weekly%  -> prints allowed, sets exit
  local used="$1" mins="$2" weekly="$3"
  local allowed over
  allowed=$(awk -v f="$PCT_FLOOR" -v c="$PCT_CEIL" -v m="$mins" \
    'BEGIN { a = c - m; if (a < f) a = f; printf "%.0f", a }')
  echo "check_usage: session ${used}% used, ${mins} min to reset — allowed up to ${allowed}% (floor ${PCT_FLOOR}, ceil ${PCT_CEIL}); weekly ${weekly}% (max ${WEEKLY_MAX}%)"
  over=$(awk -v s="$used" -v a="$allowed" -v w="$weekly" -v wm="$WEEKLY_MAX" \
    'BEGIN { print (s > a || w >= wm) ? 1 : 0 }')
  [ "$over" = 1 ] && return 3
  return 0
}

# --- Primary: claude-usage-tracker snapshot ---------------------------------
if [ -r "$LATEST" ] && command -v jq >/dev/null; then
  age_min=$(( ( $(date +%s) - $(stat -c %Y "$LATEST") ) / 60 ))
  if [ "$age_min" -le "$MAX_AGE_MIN" ]; then
    session=$(jq -r '.session_pct // empty' "$LATEST")
    weekly=$(jq -r '.weekly_all_pct // 0' "$LATEST")
    reset=$(jq -r '.session_reset // empty' "$LATEST")
    if [ -n "$session" ]; then
      mins=$(mins_until "$reset")
      echo "check_usage: tracker tick ${age_min} min old; session resets at ${reset:-unknown}"
      verdict "$session" "$mins" "$weekly"
      exit $?
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

read -r used_pct mins_left < <(CCUSAGE_JSON="$json" python3 - "$BUDGET" <<'PY'
import datetime as dt, json, os, sys
budget = int(sys.argv[1])
data = json.loads(os.environ["CCUSAGE_JSON"])
blocks = data.get("blocks", data if isinstance(data, list) else [])
active = [b for b in blocks if b.get("isActive")] or blocks[-1:] if blocks else []
if not active:
    print("0 99999")
    sys.exit(0)
b = active[-1]
used = b.get("totalTokens") or sum(
    b.get(k, 0) for k in ("inputTokens", "outputTokens",
                          "cacheCreationInputTokens", "cacheReadInputTokens"))
pct = 100.0 * used / budget
mins = 99999
if end := b.get("endTime"):
    try:
        t = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
        mins = max(0, int((t - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60))
    except ValueError:
        pass
print(f"{pct:.0f} {mins}")
PY
) || { echo "check_usage: fallback parse failed — proceeding WITHOUT guard" >&2; exit 0; }

echo "check_usage: ccusage fallback — ~${used_pct}% of ${BUDGET} token budget"
verdict "$used_pct" "$mins_left" 0
exit $?
