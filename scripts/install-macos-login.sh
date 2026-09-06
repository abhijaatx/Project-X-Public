#!/bin/bash
set -euo pipefail

LABEL="com.abhijaat.project-x-host"
PORT="5001"
PIN=""
PROJECT_DIR=""
REPOSITORY_URL="https://github.com/abhijaatx/Project-X-Public.git"

usage() {
  cat <<'EOF'
Usage: install-macos-login.sh [--pin PIN] [--project-dir PATH] [--port PORT]

Installs Project X as a macOS LaunchAgent that starts at login and restarts
after a crash. The PIN may contain up to eight characters. If --pin is omitted,
an eight-digit PIN is generated and printed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pin)
      [[ $# -ge 2 ]] || { echo "Missing value for --pin" >&2; exit 2; }
      PIN="$2"
      shift 2
      ;;
    --project-dir)
      [[ $# -ge 2 ]] || { echo "Missing value for --project-dir" >&2; exit 2; }
      PROJECT_DIR="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "Missing value for --port" >&2; exit 2; }
      PORT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is only for macOS." >&2
  exit 1
fi

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "Port must be a number from 1 to 65535." >&2
  exit 2
fi

if [[ -z "$PIN" ]]; then
  PIN="$(LC_ALL=C tr -dc '0-9' </dev/urandom | head -c 8 || true)"
fi
if [[ -z "$PIN" || ${#PIN} -gt 8 || "$PIN" == *$'\n'* || "$PIN" == *$'\r'* ]]; then
  echo "PIN must contain 1 to 8 characters on a single line." >&2
  exit 2
fi

if [[ -z "$PROJECT_DIR" ]]; then
  # LaunchAgents can be denied access to Documents/Desktop by macOS privacy
  # controls, so keep the unattended checkout in Application Support.
  PROJECT_DIR="$HOME/Library/Application Support/Project X/source"
fi

PROJECT_DIR="${PROJECT_DIR/#\~/$HOME}"
if [[ -d "$PROJECT_DIR/.git" ]]; then
  echo "[Project X] Updating $PROJECT_DIR..."
  git -C "$PROJECT_DIR" pull --ff-only
elif [[ -e "$PROJECT_DIR" ]]; then
  echo "$PROJECT_DIR exists but is not a Git checkout." >&2
  exit 1
else
  echo "[Project X] Cloning into $PROJECT_DIR..."
  git clone "$REPOSITORY_URL" "$PROJECT_DIR"
fi

PYTHON3="$(command -v python3 || true)"
if [[ -z "$PYTHON3" ]]; then
  echo "Python 3 is required. Install it with: brew install python" >&2
  exit 1
fi
if ! "$PYTHON3" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "[Project X] Creating Python environment..."
  if [[ -e "$PROJECT_DIR/.venv" ]]; then
    mv "$PROJECT_DIR/.venv" "$PROJECT_DIR/.venv.incomplete.$(date +%Y%m%d%H%M%S)"
  fi
  "$PYTHON3" -m venv "$PROJECT_DIR/.venv"
fi

echo "[Project X] Installing dependencies..."
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"

AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/Project X"
PLIST="$AGENTS_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$AGENTS_DIR" "$LOG_DIR"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true

# Replace a manually launched Project X host on the requested port. Refuse to
# terminate an unrelated service that happens to use the same port.
for PID in $(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
  COMMAND="$(ps -p "$PID" -o command= 2>/dev/null || true)"
  if [[ "$COMMAND" == *"/server.py --host 0.0.0.0 --port $PORT"* ]]; then
    echo "[Project X] Stopping the existing host on port $PORT..."
    kill -TERM "$PID" 2>/dev/null || true
  else
    echo "Port $PORT is already used by another process: $COMMAND" >&2
    exit 1
  fi
done

for _ in {1..10}; do
  nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1 || break
  sleep 0.5
done
if nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
  echo "The existing process did not release port $PORT." >&2
  exit 1
fi

rm -f "$PLIST"
plutil -create xml1 "$PLIST"
plutil -insert Label -string "$LABEL" "$PLIST"
plutil -insert ProgramArguments -array "$PLIST"
plutil -insert ProgramArguments.0 -string "$VENV_PYTHON" "$PLIST"
plutil -insert ProgramArguments.1 -string "$PROJECT_DIR/start.py" "$PLIST"
plutil -insert ProgramArguments.2 -string "--no-tunnel" "$PLIST"
plutil -insert ProgramArguments.3 -string "--host" "$PLIST"
plutil -insert ProgramArguments.4 -string "0.0.0.0" "$PLIST"
plutil -insert ProgramArguments.5 -string "--port" "$PLIST"
plutil -insert ProgramArguments.6 -string "$PORT" "$PLIST"
plutil -insert WorkingDirectory -string "$PROJECT_DIR" "$PLIST"
plutil -insert EnvironmentVariables -dictionary "$PLIST"
plutil -insert EnvironmentVariables.WEBREMOTE_PIN -string "$PIN" "$PLIST"
plutil -insert EnvironmentVariables.PYTHONUNBUFFERED -string "1" "$PLIST"
plutil -insert RunAtLoad -bool true "$PLIST"
plutil -insert KeepAlive -bool true "$PLIST"
plutil -insert ProcessType -string Background "$PLIST"
plutil -insert ThrottleInterval -integer 10 "$PLIST"
plutil -insert StandardOutPath -string "$LOG_DIR/host.log" "$PLIST"
plutil -insert StandardErrorPath -string "$LOG_DIR/host-error.log" "$PLIST"
chmod 600 "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

READY=false
for _ in {1..20}; do
  if nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 1
done
if [[ "$READY" == true ]] && launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo
  echo "Project X now starts automatically at login."
  echo "PIN: $PIN"
  echo "Port: $PORT"
  echo "Log: $LOG_DIR/host.log"
  echo "Uninstall: curl -fsSL https://raw.githubusercontent.com/abhijaatx/Project-X-Public/main/scripts/uninstall-macos-login.sh | bash"
else
  echo "Project X did not start on port $PORT. Check: $LOG_DIR/host-error.log" >&2
  exit 1
fi
