#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "============================================================"
echo "Building ModernEDM.app (Standalone macOS Application Bundle)"
echo "============================================================"

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt pyinstaller

"$PYTHON_BIN" build_release.py

# Optional: Create a drag-and-drop macOS .dmg if hdiutil is available
if command -v hdiutil >/dev/null 2>&1; then
    DMG_PATH="dist/ModernEDM-macOS.dmg"
    rm -f "$DMG_PATH"
    hdiutil create -volname "Modern EDM" \
        -srcfolder "dist/ModernEDM.app" \
        -ov -format UDZO "$DMG_PATH"
    echo "[SUCCESS] Created macOS Disk Image: $DMG_PATH"
fi
