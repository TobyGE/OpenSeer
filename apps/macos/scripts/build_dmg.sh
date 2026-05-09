#!/usr/bin/env bash
# Wrap apps/macos/dist/OpenSeer.app into a DMG.
# Run build_app.sh first.
#
# Output: apps/macos/dist/OpenSeer-<version>.dmg
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$HERE/.." && pwd)"
DIST="$APP_ROOT/dist"
APP="$DIST/OpenSeer.app"

if [[ ! -d "$APP" ]]; then
    echo "ERROR: $APP not found. Run build_app.sh first."
    exit 1
fi

# Pull version from Info.plist.
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
    "$APP/Contents/Info.plist")"
DMG="$DIST/OpenSeer-${VERSION}.dmg"
STAGE="$DIST/.dmg-stage"

echo "==> Building $DMG"
rm -f "$DMG"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
# Drag-to-Applications affordance: a symlink the user can drop
# OpenSeer.app onto inside the mounted volume.
ln -s /Applications "$STAGE/Applications"

hdiutil create \
    -volname "OpenSeer ${VERSION}" \
    -srcfolder "$STAGE" \
    -fs HFS+ \
    -format UDZO \
    -ov \
    "$DMG" >/dev/null

rm -rf "$STAGE"

echo "==> Done."
ls -lh "$DMG"
echo
echo "Distribute by uploading $DMG. Recipients on macOS without Apple"
echo "Developer ID notarization will see Gatekeeper warn 'unidentified"
echo "developer'; first launch needs right-click → Open, or:"
echo "    xattr -d com.apple.quarantine /Applications/OpenSeer.app"
