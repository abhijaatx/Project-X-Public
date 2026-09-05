#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
cd "$PROJECT_DIR"

if ! xcrun --find lipo >/dev/null 2>&1; then
  echo "[Project X] Xcode Command Line Tools are required to build the macOS app."
  echo "Install them with: xcode-select --install"
  exit 1
fi

mkdir -p vendor release
ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" ]]; then
  CLOUDFLARED_ARCH="arm64"
else
  CLOUDFLARED_ARCH="amd64"
fi

if [[ ! -x vendor/cloudflared ]]; then
  echo "[Project X] Downloading Cloudflare Tunnel for macOS ${CLOUDFLARED_ARCH}..."
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_DIR"' EXIT
  curl --fail --location --retry 3 \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${CLOUDFLARED_ARCH}.tgz" \
    --output "$TEMP_DIR/cloudflared.tgz"
  tar -xzf "$TEMP_DIR/cloudflared.tgz" -C vendor
  chmod +x vendor/cloudflared
fi

PYTHON_BIN="${PROJECT_X_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi
"$PYTHON_BIN" -m pip install --upgrade -r requirements-build.txt
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean project-x.spec

rm -f "release/Project-X-macOS-${ARCH}.dmg"
DMG_STAGE="$(mktemp -d)"
trap 'rm -rf "$DMG_STAGE"' EXIT
ditto "dist/Project X.app" "$DMG_STAGE/Project X.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create \
  -volname "Project X" \
  -srcfolder "$DMG_STAGE" \
  -ov -format UDZO \
  "release/Project-X-macOS-${ARCH}.dmg"

echo "[Project X] Built release/Project-X-macOS-${ARCH}.dmg"
