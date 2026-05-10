#!/usr/bin/env bash
# Build a self-contained OpenSeer.app:
#   1. Compile the Swift GUI (release).
#   2. Download python-build-standalone if not cached.
#   3. Lay out the .app bundle with bundled Python + openseer pkg.
#   4. Drop a launcher shim at MacOS/openseer.
#   5. Ad-hoc codesign so Gatekeeper at least sees a signature.
#
# The bundled Python is relocatable; the shim sets PYTHONPATH so
# the GUI's `openseer …` invocations hit the bundled package, not
# whatever happens to be on the user's $PATH.
#
# Usage:
#   apps/macos/scripts/build_app.sh           # arm64 only
#   apps/macos/scripts/build_app.sh universal # arm64 + x86_64
#
# Output: apps/macos/dist/OpenSeer.app
set -euo pipefail

ARCH_MODE="${1:-arm64}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$APP_ROOT/../.." && pwd)"
DIST="$APP_ROOT/dist"
CACHE="$APP_ROOT/.build-cache"
APP="$DIST/OpenSeer.app"

PYTHON_VERSION="3.12.8"
PBS_RELEASE="20250106"
PBS_ARCH="aarch64-apple-darwin"
if [[ "$ARCH_MODE" == "universal" || "$ARCH_MODE" == "x86_64" ]]; then
    echo "ERROR: only arm64 is supported right now (python-build-standalone"
    echo "       is per-arch; universal builds need lipo'd python which is"
    echo "       a separate rabbit hole)."
    exit 1
fi
PBS_TARBALL="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${PBS_ARCH}-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_TARBALL}"

echo "==> Cleaning previous bundle"
rm -rf "$DIST"
mkdir -p "$DIST" "$CACHE"

echo "==> Building Swift GUI (release / arm64)"
(
    cd "$APP_ROOT"
    swift build -c release --arch arm64
)
SWIFT_BIN="$APP_ROOT/.build/arm64-apple-macosx/release/OpenSeerGUI"
if [[ ! -x "$SWIFT_BIN" ]]; then
    echo "ERROR: swift build did not produce $SWIFT_BIN"
    exit 1
fi

echo "==> Fetching python-build-standalone (${PYTHON_VERSION})"
TARBALL_PATH="$CACHE/$PBS_TARBALL"
if [[ ! -f "$TARBALL_PATH" ]]; then
    curl -fL --progress-bar -o "$TARBALL_PATH" "$PBS_URL"
fi
PY_DIR="$CACHE/python-${PYTHON_VERSION}"
if [[ ! -d "$PY_DIR" ]]; then
    rm -rf "$PY_DIR.tmp"
    mkdir -p "$PY_DIR.tmp"
    tar -xzf "$TARBALL_PATH" -C "$PY_DIR.tmp"
    # Tarball top-level is "python/"; lift contents so PY_DIR/bin works
    mv "$PY_DIR.tmp/python" "$PY_DIR"
    rm -rf "$PY_DIR.tmp"
fi

echo "==> Assembling .app skeleton"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$APP_ROOT/Resources/Info.plist" "$APP/Contents/Info.plist"
cp "$SWIFT_BIN" "$APP/Contents/MacOS/OpenSeerGUI"
chmod +x "$APP/Contents/MacOS/OpenSeerGUI"

# App icon — regenerate the .icns if the source has been updated
# more recently, then drop it into Resources/.
if [[ -f "$APP_ROOT/Resources/AppIcon-source.png" ]]; then
    if [[ ! -f "$APP_ROOT/Resources/AppIcon.icns" \
          || "$APP_ROOT/Resources/AppIcon-source.png" \
              -nt "$APP_ROOT/Resources/AppIcon.icns" ]]; then
        bash "$HERE/build_icon.sh"
    fi
    cp "$APP_ROOT/Resources/AppIcon.icns" "$APP/Contents/Resources/"
fi

echo "==> Copying bundled Python into Resources/python"
cp -R "$PY_DIR" "$APP/Contents/Resources/python"

echo "==> Installing openseer + deps into Resources/site-packages"
SITE="$APP/Contents/Resources/site-packages"
mkdir -p "$SITE"
"$APP/Contents/Resources/python/bin/python3" -m pip install \
    --quiet --upgrade pip
"$APP/Contents/Resources/python/bin/python3" -m pip install \
    --quiet --target "$SITE" "$REPO_ROOT"

echo "==> Writing openseer launcher shim"
# Shim lives under Resources/, NOT MacOS/. Only the main bundle
# executable belongs in Contents/MacOS — extra interpreter scripts
# there confuse codesign (it tries to Mach-O-sign them). The GUI's
# OpenSeerEnv.locateBinary() looks at Resources/openseer first.
cat > "$APP/Contents/Resources/openseer" <<'SHIM'
#!/bin/bash
# Launcher shim: routes `openseer …` invocations from the GUI to
# the bundled Python + bundled openseer package. Also extends PATH
# with common Homebrew / npm-global locations because GUI children
# inherit the sparse /usr/bin:/bin PATH that Finder hands out.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE"
export PYTHONPATH="$RES/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$RES/python/bin/python3" -m openseer.cli "$@"
SHIM
chmod +x "$APP/Contents/Resources/openseer"

echo "==> Stripping caches to slim the bundle"
find "$SITE" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$APP/Contents/Resources/python/lib" -type d -name "__pycache__" -prune -exec rm -rf {} + || true
# Test stubs ship with some pyobjc subpackages; harmless to leave.

echo "==> Ad-hoc codesigning (inside-out: every Mach-O, then bundle)"
# `codesign --deep` is unreliable for embedded Python: it skips many
# .so / .dylib files inside site-packages, leaving "sealed resource
# missing" errors that make TCC reject the bundle (so even after
# the user toggles Accessibility on, AXIsProcessTrusted stays
# false). We sign every Mach-O leaf first, then the python3
# launcher, the GUI binary, and finally the bundle.
SIGN_ID="${OPENSEER_SIGN_IDENTITY:-}"
SIGN_KEYCHAIN="${OPENSEER_SIGN_KEYCHAIN:-$HOME/Library/Keychains/openseer-signing.keychain-db}"
SIGN_KEYCHAIN_PASSWORD="${OPENSEER_SIGN_KEYCHAIN_PASSWORD:-openseer-dev}"
if [[ -z "$SIGN_ID" ]]; then
    if [[ -f "$SIGN_KEYCHAIN" ]]; then
        security unlock-keychain -p "$SIGN_KEYCHAIN_PASSWORD" "$SIGN_KEYCHAIN" 2>/dev/null || true
        KEYCHAIN_LIST=("$SIGN_KEYCHAIN")
        while IFS= read -r kc; do
            [[ -n "$kc" && "$kc" != "$SIGN_KEYCHAIN" ]] && KEYCHAIN_LIST+=("$kc")
        done < <(security list-keychains -d user | tr -d ' "')
        security list-keychains -d user -s "${KEYCHAIN_LIST[@]}" 2>/dev/null || true
        SIGN_ID="$(
            security find-identity -v -p codesigning "$SIGN_KEYCHAIN" 2>/dev/null \
                | awk -F'[ )]+' '/"OpenSeer Local Dev"/ { print $3; exit }'
        )"
    fi
    if [[ -n "$SIGN_ID" ]]; then
        :
    elif security find-identity -v -p codesigning 2>/dev/null \
        | grep -q '"OpenSeer Local Dev"'; then
        SIGN_ID="OpenSeer Local Dev"
    else
        SIGN_ID="-"   # ad-hoc fallback
    fi
fi
echo "==> Code signing identity: $SIGN_ID"
SIGN_ARGS=(--force --sign "$SIGN_ID" --timestamp=none)
if [[ "$SIGN_ID" != "-" && -f "$SIGN_KEYCHAIN" ]]; then
    SIGN_ARGS+=(--keychain "$SIGN_KEYCHAIN")
fi

# 1) every .dylib / .so in the bundled Python tree + site-packages
find "$APP/Contents/Resources" \
    \( -name "*.dylib" -o -name "*.so" \) -type f -print0 |
    xargs -0 -n 32 codesign "${SIGN_ARGS[@]}" 2>/dev/null || true

# 2) python launcher binaries (python3 is the real Mach-O; the
#    others are symlinks)
codesign "${SIGN_ARGS[@]}" \
    "$APP/Contents/Resources/python/bin/python3.12" 2>/dev/null || true

# 3) openseer shim — it's a #!/bin/bash script so codesign skips
#    it. Nothing to sign.

# 4) GUI binary — pin its identifier to the bundle id
codesign "${SIGN_ARGS[@]}" \
    --identifier com.openseer.OpenSeer \
    "$APP/Contents/MacOS/OpenSeerGUI"

# 5) the .app itself
codesign "${SIGN_ARGS[@]}" \
    --identifier com.openseer.OpenSeer \
    "$APP"

echo "==> Verifying signature"
if ! codesign --verify --deep --strict "$APP" 2>&1; then
    echo
    echo "ERROR: signature verification failed — TCC would refuse"
    echo "       this bundle, so refusing to produce a broken release."
    exit 1
fi
echo "    signature OK"

echo
echo "==> Done."
echo "    Bundle: $APP"
du -sh "$APP" || true
echo
echo "Run it:"
echo "    open \"$APP\""
