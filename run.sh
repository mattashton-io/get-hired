#!/usr/bin/env bash
# ==============================================================================
# get-hired: Unified Operator Script (Seamless Daemon, Dry-Run, & macOS Service)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"
LOG_FILE="${SCRIPT_DIR}/data/daemon.log"
PID_FILE="${SCRIPT_DIR}/data/daemon.pid"
PLIST_NAME="com.gethired.daemon.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
TARGET_PLIST="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"

mkdir -p "${SCRIPT_DIR}/data"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Error: Virtual environment python not found at ${VENV_PYTHON}."
  echo "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

get_pid() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && ps -p "${pid}" >/dev/null 2>&1; then
      echo "${pid}"
      return 0
    fi
  fi
  # Fallback check for running python daemon process matching this repo
  pgrep -f "${SCRIPT_DIR}/.venv/bin/python -m src.cli daemon" || true
}

cmd_start() {
  local pid
  pid="$(get_pid)"
  if [[ -n "${pid}" ]]; then
    echo "Daemon is already running with PID ${pid}."
    return 0
  fi

  echo "Starting get-hired scheduler daemon in background..."
  nohup "${VENV_PYTHON}" -m src.cli daemon >> "${LOG_FILE}" 2>&1 &
  local new_pid=$!
  echo "${new_pid}" > "${PID_FILE}"
  sleep 1

  if ps -p "${new_pid}" >/dev/null 2>&1; then
    echo "✓ Daemon started successfully (PID: ${new_pid})."
    echo "  Logs: tail -f data/daemon.log"
  else
    echo "✗ Daemon failed to start. Recent logs:"
    tail -n 20 "${LOG_FILE}"
    exit 1
  fi
}

cmd_stop() {
  local pid
  pid="$(get_pid)"
  if [[ -z "${pid}" ]]; then
    echo "Daemon is not currently running."
    rm -f "${PID_FILE}"
    return 0
  fi

  echo "Stopping daemon (PID: ${pid})..."
  kill "${pid}" 2>/dev/null || true

  for _ in {1..10}; do
    if ! ps -p "${pid}" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done

  if ps -p "${pid}" >/dev/null 2>&1; then
    echo "Process did not exit cleanly; forcing termination..."
    kill -9 "${pid}" 2>/dev/null || true
  fi

  rm -f "${PID_FILE}"
  echo "✓ Daemon stopped."
}

cmd_status() {
  local pid
  pid="$(get_pid)"
  echo "=================================================="
  echo " get-hired Agent Status"
  echo "=================================================="
  if [[ -n "${pid}" ]]; then
    echo "Status:   RUNNING (PID: ${pid})"
    ps -u -p "${pid}" | tail -n 1
  else
    echo "Status:   STOPPED"
  fi
  echo "Log:      ${LOG_FILE}"

  if [[ -f "${SCRIPT_DIR}/data/applications_ledger.db" ]]; then
    echo "--- Ledger Telemetry ---"
    "${VENV_PYTHON}" -c '
from src.config import get_settings
from src.state.db import ApplicationsLedgerDB
s = get_settings()
db = ApplicationsLedgerDB(s.ledger_db_path, s.target_companies_path)
t = db.get_pipeline_telemetry()
print(f"Companies Tracked: {t[\"companies_total\"]} | Roles Evaluated: {t[\"roles_evaluated\"]} | Apps: {t[\"applications_created\"]}")
pending = len(db.get_pending_hitl_applications())
print(f"Pending Candidate HITL Approvals: {pending}")
' 2>/dev/null || true
  fi
  echo "=================================================="
}

cmd_logs() {
  touch "${LOG_FILE}"
  exec tail -f "${LOG_FILE}"
}

cmd_install_service() {
  echo "Installing native macOS LaunchAgent..."
  mkdir -p "${LAUNCH_AGENTS_DIR}"

  cat <<EOF > "${TARGET_PLIST}"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gethired.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_PYTHON}</string>
        <string>-m</string>
        <string>src.cli</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.local/bin</string>
        <key>DRY_RUN</key>
        <string>false</string>
    </dict>
</dict>
</plist>
EOF

  launchctl unload "${TARGET_PLIST}" 2>/dev/null || true
  launchctl load -w "${TARGET_PLIST}"
  echo "✓ LaunchAgent installed & loaded at ${TARGET_PLIST}."
  echo "  It will start automatically on Mac boot and keep running in the background."
  echo "  To uninstall: ./run.sh uninstall-service"
}

cmd_uninstall_service() {
  if [[ -f "${TARGET_PLIST}" ]]; then
    launchctl unload "${TARGET_PLIST}" 2>/dev/null || true
    rm -f "${TARGET_PLIST}"
    echo "✓ LaunchAgent unloaded and removed."
  else
    echo "LaunchAgent plist not found."
  fi
}

case "${1:-status}" in
  start)
    cmd_start
    ;;
  stop)
    cmd_stop
    ;;
  restart)
    cmd_stop
    sleep 1
    cmd_start
    ;;
  status)
    cmd_status
    ;;
  logs)
    cmd_logs
    ;;
  dry-run)
    DRY_RUN=true "${VENV_PYTHON}" -m src.cli dry-run
    ;;
  digest)
    "${VENV_PYTHON}" -m src.cli digest
    ;;
  install-service)
    cmd_install_service
    ;;
  uninstall-service)
    cmd_uninstall_service
    ;;
  help|--help|-h)
    echo "Usage: ./run.sh [start|stop|restart|status|logs|dry-run|digest|install-service|uninstall-service]"
    ;;
  *)
    echo "Unknown command: ${1}"
    echo "Usage: ./run.sh [start|stop|restart|status|logs|dry-run|digest|install-service|uninstall-service]"
    exit 1
    ;;
esac

