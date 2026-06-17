#!/usr/bin/env bash
# Thin cron wrapper for the every-2-days operational refresh (WU-26).
#
# Runs scripts/26_operational_refresh.py to: pull the latest EWDS FWI, regenerate
# + upload the six display COGs to R2, patch the style_data.json FWI overlay, and
# emit the "assets to watch" two-axis decision product.
#
# Honest framing (CLAUDE.md #6/#9): the watch list is OPERATIONAL TRIAGE, not a
# forecast/probability/ignition prediction; FWI is observed reanalysis (~2-day lag).
#
# Secrets: the EWDS key is read from ~/.cdsapirc (or the CDSAPI_KEY env). It is
# NEVER passed on the command line, logged, or committed. The 'r2:' rclone remote
# must be configured for the upload step.
#
# Exit codes: 0 = published; non-zero = refresh failed (last-good artefacts kept,
# nothing published). Designed to be safe to re-run.
#
# Install instructions: see docs/operationalization.md (§ "Operational refresh
# cadence"). DO NOT install the cron without human sign-off.
set -euo pipefail

# Resolve the repo root from this script's location (cron has no cwd guarantees).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

LOG_DIR="${REPO_ROOT}/outputs/logs/operational_refresh"
mkdir -p "${LOG_DIR}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="${LOG_DIR}/refresh_${STAMP}.log"

# Pass any extra args straight through (e.g. --aoi monchique, --top-n 30).
echo "[run_operational_refresh] ${STAMP} starting (args: $*)" | tee -a "${LOG_FILE}"
if uv run python scripts/26_operational_refresh.py "$@" >>"${LOG_FILE}" 2>&1; then
  echo "[run_operational_refresh] ${STAMP} OK" | tee -a "${LOG_FILE}"
  exit 0
else
  rc=$?
  echo "[run_operational_refresh] ${STAMP} FAILED (exit ${rc}); last-good artefacts kept" \
    | tee -a "${LOG_FILE}"
  exit "${rc}"
fi
