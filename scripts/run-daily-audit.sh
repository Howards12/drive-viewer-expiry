#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
	PYTHON="${PROJECT_ROOT}/.venv/bin/python"
else
	PYTHON="python3"
fi

# Forward optional config when set (launchd EnvironmentVariables, cron, or interactive shell).
for _v in SPREADSHEET_ID SHEET_TAB ACTIVITY_SHEET_TAB ACTIVITY_HOURS EXPIRY_HOURS FOLDER_ID; do
	if [[ -n "${!_v+x}" ]]; then
		export "${_v}"
	fi
done

# Permission expiry rows → main tab (--sheet-tab); then Drive Activity → --activity-tab.
"${PYTHON}" set_viewer_expiry.py --also-log-access

# Access-only (no expiry tab updates):
# "${PYTHON}" set_viewer_expiry.py --sync-access-activity
