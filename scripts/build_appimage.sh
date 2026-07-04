#!/usr/bin/env bash
# Build a self-contained AppImage for CodexBar KDE.
#
# Requirements:
#   - python3 (used to create a local build venv with PyInstaller + PyQt6)
#   - appimagetool (auto-downloaded to ~/.cache/appimage-tools if missing)
#   - FUSE for *running* AppImages (building uses --appimage-extract-and-run)
#
# Usage:
#   bash scripts/build_appimage.sh
#
# Output: dist/CodexBar_KDE-x86_64.AppImage
#
# Note: the AppImage bundles Python + PyQt6 but NOT the codexbar CLI.
# It still expects /usr/bin/codexbar (or --codexbar-bin PATH) at runtime.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APP=codexbar-kde
VENV=.venv-build
APPDIR=build/AppDir
ARCH=${ARCH:-x86_64}
OUT="dist/CodexBar_KDE-${ARCH}.AppImage"

# --- appimagetool ---------------------------------------------------------
APPIMAGETOOL=$(command -v appimagetool || true)
if [[ -z "$APPIMAGETOOL" ]]; then
    APPIMAGETOOL="$HOME/.cache/appimage-tools/appimagetool-${ARCH}.AppImage"
    if [[ ! -x "$APPIMAGETOOL" ]]; then
        echo ">> downloading appimagetool"
        mkdir -p "$(dirname "$APPIMAGETOOL")"
        curl -sL -o "$APPIMAGETOOL" \
            "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
        chmod +x "$APPIMAGETOOL"
    fi
fi

# --- build venv -----------------------------------------------------------
if [[ ! -x "$VENV/bin/pyinstaller" ]]; then
    echo ">> creating build venv"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" -q install --upgrade pip pyinstaller PyQt6
fi

# --- PyInstaller onedir bundle ---------------------------------------------
echo ">> running PyInstaller"
ENTRY=build/entry.py
mkdir -p build
printf 'import sys\nfrom codexbar_kde.app import main\nsys.exit(main())\n' > "$ENTRY"
"$VENV/bin/pyinstaller" --noconfirm --clean \
    --name "$APP" --onedir --windowed \
    --paths src \
    --distpath build/pyinstaller \
    --workpath build/pyinstaller-work \
    --specpath build \
    "$ENTRY"

# --- assemble AppDir --------------------------------------------------------
echo ">> assembling AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a "build/pyinstaller/$APP" "$APPDIR/usr/bin/$APP.dist"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/codexbar-kde.dist/codexbar-kde" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CodexBar KDE
Comment=AI subscription usage dashboard (CodexBar CLI frontend)
Exec=$APP
Icon=$APP
Categories=Utility;Monitor;Qt;
Terminal=false
StartupWMClass=codexbar-kde
X-AppImage-Name=CodexBar KDE
EOF
cp "$APPDIR/$APP.desktop" "$APPDIR/usr/share/applications/"

cp assets/$APP.svg "$APPDIR/$APP.svg"
cp assets/$APP.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
cp assets/$APP.svg "$APPDIR/.DirIcon"

# --- pack -------------------------------------------------------------------
echo ">> packing AppImage"
mkdir -p dist
ARCH=$ARCH "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
echo ">> built $OUT"
du -h "$OUT"
