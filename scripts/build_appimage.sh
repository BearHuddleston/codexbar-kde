#!/usr/bin/env bash
# Build a reproducible x86_64 AppImage from a pinned manylinux_2_28 Python base.
# The AppImage bundles Python and PyQt6, but the codexbar CLI remains host-side.

set -euo pipefail
export LC_ALL=C
export TZ=UTC
export PYTHONHASHSEED=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APP=codexbar-kde
ARCH=${ARCH:-x86_64}
APPDIR="$REPO_ROOT/build/AppDir"
OUT="$REPO_ROOT/dist/CodexBar_KDE-${ARCH}.AppImage"
CACHE_DIR=${APPIMAGE_CACHE_DIR:-"${XDG_CACHE_HOME:-$HOME/.cache}/codexbar-kde-appimage"}
OFFLINE=${APPIMAGE_OFFLINE:-0}
REQUIREMENTS="$REPO_ROOT/packaging/appimage-requirements.txt"

if [[ "$ARCH" != "x86_64" ]]; then
    echo "unsupported ARCH=$ARCH: only the x86_64 inputs have verified hashes" >&2
    exit 2
fi

PYTHON_BASE_NAME=python3.11.14-cp311-cp311-manylinux_2_28_x86_64.AppImage
PYTHON_BASE_URL="https://github.com/niess/python-appimage/releases/download/python3.11/$PYTHON_BASE_NAME"
PYTHON_BASE_SHA256=89ff05124b2fcbecbd46006be5b477760907cccb13518054fa67a4356b8215f3
APPIMAGETOOL_NAME=appimagetool-1.9.1-x86_64.AppImage
APPIMAGETOOL_URL=https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
APPIMAGETOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0

mkdir -p "$CACHE_DIR"

fetch_verified() {
    local url=$1 sha256=$2 destination=$3
    if [[ ! -f "$destination" ]]; then
        if [[ "$OFFLINE" == 1 ]]; then
            echo "offline input missing: $destination" >&2
            exit 2
        fi
        echo ">> downloading $(basename "$destination")"
        curl --fail --location --silent --show-error \
            --output "$destination.tmp" "$url"
        mv "$destination.tmp" "$destination"
    fi
    printf '%s  %s\n' "$sha256" "$destination" | sha256sum --check --status || {
        echo "checksum mismatch: $destination" >&2
        exit 2
    }
    chmod 755 "$destination"
}

PYTHON_BASE="$CACHE_DIR/$PYTHON_BASE_NAME"
APPIMAGETOOL="$CACHE_DIR/$APPIMAGETOOL_NAME"
fetch_verified "$PYTHON_BASE_URL" "$PYTHON_BASE_SHA256" "$PYTHON_BASE"
fetch_verified "$APPIMAGETOOL_URL" "$APPIMAGETOOL_SHA256" "$APPIMAGETOOL"

rm -rf "$APPDIR" "$REPO_ROOT/build/python-base"
mkdir -p "$REPO_ROOT/build/python-base"
(
    cd "$REPO_ROOT/build/python-base"
    "$PYTHON_BASE" --appimage-extract >/dev/null
)
mv "$REPO_ROOT/build/python-base/squashfs-root" "$APPDIR"
rmdir "$REPO_ROOT/build/python-base"

WHEELHOUSE="$CACHE_DIR/wheels-cp311-x86_64"
mkdir -p "$WHEELHOUSE"
if [[ "$OFFLINE" != 1 ]]; then
    "$APPDIR/AppRun" -I -m pip download \
        --only-binary=:all: --no-deps --require-hashes \
        --dest "$WHEELHOUSE" -r "$REQUIREMENTS"
fi
SITE_PACKAGES="$APPDIR/opt/python3.11/lib/python3.11/site-packages"
"$APPDIR/AppRun" -I -m pip install \
    --no-index --no-deps --no-compile --require-hashes \
    --find-links "$WHEELHOUSE" --target "$SITE_PACKAGES" \
    -r "$REQUIREMENTS"

rm -rf "$SITE_PACKAGES/codexbar_kde"
cp -a "$REPO_ROOT/src/codexbar_kde" "$SITE_PACKAGES/"
rm -f \
    "$APPDIR/python3.11.14.desktop" \
    "$APPDIR/python.png" \
    "$APPDIR/usr/share/applications/python3.11.14.desktop" \
    "$APPDIR/usr/share/metainfo/python3.11.14.appdata.xml"
mkdir -p \
    "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps" \
    "$APPDIR/usr/share/licenses/codexbar-kde" \
    "$APPDIR/usr/share/metainfo"
DESKTOP_ID=io.github.BearHuddleston.codexbar_kde
cp "$REPO_ROOT/packaging/$DESKTOP_ID.desktop" "$APPDIR/$DESKTOP_ID.desktop"
cp "$REPO_ROOT/packaging/$DESKTOP_ID.desktop" "$APPDIR/usr/share/applications/"
cp "$REPO_ROOT/packaging/$DESKTOP_ID.appdata.xml" "$APPDIR/usr/share/metainfo/"
cp "$REPO_ROOT/assets/codexbar-kde.svg" "$APPDIR/codexbar-kde.svg"
rm -f "$APPDIR/.DirIcon"
cp "$REPO_ROOT/assets/codexbar-kde.svg" "$APPDIR/.DirIcon"
cp "$REPO_ROOT/assets/codexbar-kde.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
cp "$REPO_ROOT/LICENSE" "$APPDIR/usr/share/licenses/codexbar-kde/"

# The Python base uses AppRun as a symlink. Replace it without following it.
rm "$APPDIR/AppRun"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$HERE/opt/python3.11/bin/python3.11"
SITE="$HERE/opt/python3.11/lib/python3.11/site-packages"
export SSL_CERT_FILE="$HERE/opt/_internal/certs.pem"
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONHOME PYTHONPATH QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH
export QT_PLUGIN_PATH="$SITE/PyQt6/Qt6/plugins"
export LD_LIBRARY_PATH="$SITE/PyQt6/Qt6/lib"
exec "$PYTHON" -I -c 'from codexbar_kde.app import main; raise SystemExit(main())' "$@"
EOF
chmod 755 "$APPDIR/AppRun"

# Verify the assembled directory before normalizing timestamps and packing it.
QT_QPA_PLATFORM=offscreen "$APPDIR/AppRun" --test-render

if [[ -z "${SOURCE_DATE_EPOCH:-}" ]]; then
    SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
fi
case "$SOURCE_DATE_EPOCH" in
    ''|*[!0-9]*)
        echo "SOURCE_DATE_EPOCH must be an integer Unix timestamp" >&2
        exit 2
        ;;
esac
export SOURCE_DATE_EPOCH

python3 - "$APPDIR" "$SOURCE_DATE_EPOCH" <<'PY'
import os
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
epoch = int(sys.argv[2])
for cache in root.rglob("__pycache__"):
    if cache.is_dir():
        shutil.rmtree(cache)
for bytecode in list(root.rglob("*.pyc")) + list(root.rglob("*.pyo")):
    bytecode.unlink(missing_ok=True)
for path in sorted(root.rglob("*"), reverse=True):
    os.utime(path, (epoch, epoch), follow_symlinks=False)
os.utime(root, (epoch, epoch), follow_symlinks=False)
PY

RUNTIME="$REPO_ROOT/build/runtime-${ARCH}"
offset=$("$PYTHON_BASE" --appimage-offset)
dd if="$PYTHON_BASE" of="$RUNTIME" bs="$offset" count=1 status=none
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run \
    --runtime-file "$RUNTIME" "$APPDIR" "$OUT"

echo ">> built $OUT"
sha256sum "$OUT"
