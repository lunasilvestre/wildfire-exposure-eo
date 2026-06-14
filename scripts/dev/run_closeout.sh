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

# Reasoning effort per phase (Claude Code --effort: low|medium|high|xhigh).
# Builds default to high; the independent review defaults to xhigh so it
# exhausts the use-cases a deterministic gate can't. Override via env.
BUILD_EFFORT="${CLOSEOUT_EFFORT_BUILD:-high}"
REVIEW_EFFORT="${CLOSEOUT_EFFORT_REVIEW:-xhigh}"

HIL=prompts/_HIL.md
for wu in "$@"; do
  echo "=== $wu : usage gate ==="
  scripts/dev/check_usage.sh || { echo "THROTTLED before $wu — rerun after block reset"; exit 3; }

  # Model per WU (pinned EXPLICITLY per WU — never "inherit the default",
  # which would silently follow whatever model the orchestrator session is
  # on). Implementation WUs run on Sonnet (~3x cheaper per token, and Max
  # meters Sonnet against its own separate weekly pool, so it barely touches
  # the shared 5h block). Judgment-heavy WUs build on the strongest model
  # (Opus 4.8). NOTE 2026-06-14: Fable is no longer available — the former
  # Fable pins (WU-7 build, the review below) now default to Opus 4.8. The
  # independent Opus review below, at extra-high effort (REVIEW_EFFORT), is
  # the quality gate on every WU. Effort goes via --effort, not a "/effort"
  # line inside the -p prompt (that would be plain text, not a command).
  case "$wu" in
    WU-2|WU-3|WU-4|WU-5|WU-8) MODEL="${CLOSEOUT_MODEL_IMPL:-sonnet}" ;;
    WU-7)                     MODEL="${CLOSEOUT_MODEL_WU7:-opus}" ;;
    *)                        MODEL="${CLOSEOUT_MODEL_DEFAULT:-opus}" ;;
  esac
  MODEL_FLAGS=()
  [ -n "$MODEL" ] && MODEL_FLAGS=(--model "$MODEL")

  WU_BASE=$(git rev-parse HEAD)

  echo "=== $wu : session (model=${MODEL:-default}, effort=high) ==="
  claude -p "Read prompts/00_CLOSEOUT_PLAN.md and CLAUDE.md end-to-end, then execute ${wu} ONLY, following the session loop (smoke before pilot, gates, session-log entry, scoped commits). UNATTENDED RUN RULES: NEVER invent identifiers, endpoints, or spec details — if a value cannot be verified by querying, leave a '# TODO(provenance):' marker and surface it in the session log; if a deliverable cannot be completed without guessing, write the question to ${HIL}, commit it, and end the session. Redirect long-running command output to a file under outputs/logs/ and poll it instead of streaming; if a stop condition fires or anything needs human approval, write the question to ${HIL}, commit it, and end the session immediately." \
    "${MODEL_FLAGS[@]}" --effort "$BUILD_EFFORT" \
    "${PERM_FLAGS[@]}" || { echo "$wu session exited non-zero"; exit 1; }

  echo "=== $wu : gates ==="
  uv run ruff check . && uv run ruff format --check . \
    && uv run pyright src tests scripts && uv run pytest -q

  [ -f "$HIL" ] && { echo "HIL required after $wu — see $HIL"; exit 2; }

  # A build session that committed nothing is an anomaly, not a success —
  # refuse to push/continue unattended.
  [ "$(git rev-parse HEAD)" = "$WU_BASE" ] \
    && { echo "$wu produced no commits — halting for a human look"; exit 1; }

  # Independent review pass, pinned to the strong model explicitly (the
  # reviewer is the system's insurance — it must not silently follow the
  # orchestrator session's model). Catches what deterministic gates can't:
  # plausible-but-wrong logic, self-confirming tests, unverified
  # identifiers. Disable with CLOSEOUT_REVIEW=off; override model with
  # CLOSEOUT_MODEL_REVIEW.
  if [ "${CLOSEOUT_REVIEW:-on}" != "off" ]; then
    REVIEW_MODEL="${CLOSEOUT_MODEL_REVIEW:-opus}"
    REVIEW_FLAGS=(--model "$REVIEW_MODEL")

    echo "=== $wu : review (model=${REVIEW_MODEL}) ==="
    claude -p "You are the INDEPENDENT REVIEWER for ${wu}; a separate session built it in commits ${WU_BASE}..HEAD. Read CLAUDE.md, prompts/00_CLOSEOUT_PLAN.md, and the WU's prompt file end-to-end, then adversarially review 'git diff ${WU_BASE}..HEAD' against the prompt's deliverables and every CLAUDE.md non-negotiable. Hunt specifically for: invented or unverified identifiers (verify STAC/OSM/HF IDs against live sources when cheap); implicit CRS or silent reprojection; missing provenance fields; self-confirming tests that encode the implementation's own misunderstanding; hardcoded AOI coordinates; wrong output formats; probability language for the exposure rank. Re-run any verification you need (gates, --smoke runs, ad-hoc probes). Minor defects: fix them directly with scoped commits and append a one-line review note to the WU entry in prompts/_session_log.md. Structural failures (missing deliverable, wrong approach, needs a rewrite): write your assessment to ${HIL}, commit it, and end the session. UNATTENDED RUN RULES: never guess values you cannot verify; redirect long output to outputs/logs/ and poll. You are a HEADLESS one-shot session: never arm monitors, background jobs, or anything expecting to resume after your final message — wait synchronously (poll the log file) for any run you start, and finish everything before ending. Your ABSOLUTE LAST line of output must start exactly with 'REVIEW: ' — 'REVIEW: PASS', 'REVIEW: FIXED <n>', or 'REVIEW: HIL'." \
      "${REVIEW_FLAGS[@]}" --effort "$REVIEW_EFFORT" \
      "${PERM_FLAGS[@]}" || { echo "$wu review exited non-zero"; exit 1; }

    echo "=== $wu : gates (post-review) ==="
    uv run ruff check . && uv run ruff format --check . \
      && uv run pyright src tests scripts && uv run pytest -q

    [ -f "$HIL" ] && { echo "HIL required after $wu review — see $HIL"; exit 2; }
  fi

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
