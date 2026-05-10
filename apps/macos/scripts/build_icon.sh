#!/usr/bin/env bash
# Generate apps/macos/Resources/AppIcon.icns from AppIcon-source.png
# (a 512×512 master). Builds the iconset with all the @1x / @2x
# sizes macOS expects (16…512), then runs `iconutil` to compile
# the .icns blob the .app bundles.
#
# We don't ship a 1024×1024 master because the source we have is
# 512; macOS upscales for Launchpad just fine.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$HERE/.." && pwd)"
SRC="$APP_ROOT/Resources/AppIcon-source.png"
ICONSET="$APP_ROOT/Resources/AppIcon.iconset"
OUT="$APP_ROOT/Resources/AppIcon.icns"

if [[ ! -f "$SRC" ]]; then
    echo "ERROR: $SRC missing — drop a 512×512 master PNG there."
    exit 1
fi

rm -rf "$ICONSET"
mkdir -p "$ICONSET"

# (size, filename)
gen() {
    local size=$1 name=$2
    sips -z "$size" "$size" "$SRC" --out "$ICONSET/$name" >/dev/null
}

gen 16   icon_16x16.png
gen 32   icon_16x16@2x.png
gen 32   icon_32x32.png
gen 64   icon_32x32@2x.png
gen 128  icon_128x128.png
gen 256  icon_128x128@2x.png
gen 256  icon_256x256.png
gen 512  icon_256x256@2x.png
gen 512  icon_512x512.png
# We only have a 512 master; 1024 would just be an upscale, skip it.

iconutil -c icns "$ICONSET" -o "$OUT"
rm -rf "$ICONSET"

echo "Wrote $OUT"
ls -lh "$OUT"
