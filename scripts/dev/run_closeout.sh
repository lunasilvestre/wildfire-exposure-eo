#!/usr/bin/env bash
# Marathon driver — runs close-out work-units unattended, one FRESH headless
# Claude Code session per WU (keeps context small; matches the plan's
# one-WU-per-session loop). Stops on: gate failure, red CI, HIL sentinel,
# usage throttle, or session error.
#
#   scripts/dev/run_closeout.sh WU-2 WU-3 WU-4        # first unattended stretch
#   scripts/dev/run_closeout.sh WU-5 WU-6 WU-7 WU-8   # after human prompt-draft review
#
# Permission posture (pick ONE, set CLOSEOUT_PERM):
#   acceptEdits  (default) — auto-accepts file edits; bash still gated by
#                allowlist in .claude/settings.json. Safest useful mode.
#   auto         — Claude Code auto mode (classifier-gated), if your CC
#                version ships it.
#   yolo         — --dangerously-skip-permissions. Only inside a container /
#                throwaway environment. Not recommended on a workstation.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PERM="${CLOSEOUT_PERM:-acceptEdits}"
case "$PERM" in
  yolo) PERM_FLAGS=(--dangerously-skip-permissions) ;;
  *)    PERM_FLAGS=(--permission-mode "$PERM") ;;
esac

HIL=prompts/_HIL.md
for wu in "$@"; do
  echo "=== $wu : usage gate ==="
  scripts/dev/check_usage.sh || { echo "THROTTLED before $wu — rerun after block reset"; exit 3; }

  # Model per WU: implementation WUs run on Sonnet (~3x cheaper per token,
  # and Max meters Sonnet against its own separate weekly pool); the
  # judgment-heavy WUs (WU-6 scoring semantics, WU-7 validation honesty)
  # inherit the session default model. Effort goes via --settings — a
  # "/effort high" inside a -p prompt is plain text, not a command.
  case "$wu" in
    WU-2|WU-3|WU-4|WU-5|WU-8) MODEL="${CLOSEOUT_MODEL_IMPL:-sonnet}" ;;
    *)                        MODEL="${CLOSEOUT_MODEL_DEFAULT:-}" ;;
  esac
  MODEL_FLAGS=()
  [ -n "$MODEL" ] && MODEL_FLAGS=(--model "$MODEL")

  echo "=== $wu : session (model=${MODEL:-default}, effort=high) ==="
  claude -p "Read prompts/00_CLOSEOUT_PLAN.md and CLAUDE.md end-to-end, then execute ${wu} ONLY, following the session loop (smoke before pilot, gates, session-log entry, scoped commits). UNATTENDED RUN RULES: redirect long-running command output to a file under outputs/logs/ and poll it instead of streaming; if a stop condition fires or anything needs human approval, write the question to ${HIL}, commit it, and end the session immediately." \
    "${MODEL_FLAGS[@]}" --settings '{"effortLevel":"high"}' \
    "${PERM_FLAGS[@]}" || { echo "$wu session exited non-zero"; exit 1; }

  echo "=== $wu : gates ==="
  uv run ruff check . && uv run ruff format --check . \
    && uv run pyright src tests scripts && uv run pytest -q

  [ -f "$HIL" ] && { echo "HIL required after $wu — see $HIL"; exit 2; }

  git push
  if command -v gh >/dev/null; then
    # gh run watch with no run ID needs a TTY to prompt — resolve the run
    # for HEAD explicitly (registration on GitHub can lag the push).
    sha=$(git rev-parse HEAD); run_id=
    for _ in $(seq 1 18); do
      run_id=$(gh run list --commit "$sha" -L1 --json databaseId -q '.[0].databaseId' 2>/dev/null)
      [ -n "$run_id" ] && break
      sleep 10
    done
    [ -n "$run_id" ] || { echo "no CI run appeared for $sha after $wu"; exit 1; }
    gh run watch "$run_id" --exit-status || { echo "CI red after $wu"; exit 1; }
  fi
  echo "=== $wu : done ==="
done
echo "Stretch complete."
