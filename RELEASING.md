# Releasing OpenSeer

OpenSeer is distributed as a macOS `.app` bundle attached to GitHub
Releases. We don't ship to PyPI today — `pip install -e .` from a
source checkout is the source-side path for contributors.

## Cut a release

1. Bump `[project].version` in `pyproject.toml`,
   `_SERVER_VERSION` in `openseer/mcp_server.py`, and
   `CFBundleShortVersionString` in `apps/macos/Resources/Info.plist`.
2. Commit and push the version bump.
3. Build the macOS bundle and DMG:

```bash
apps/macos/scripts/build_app.sh
apps/macos/scripts/build_dmg.sh
```

4. Create and push a matching tag:

```bash
git tag v0.0.1
git push origin main --tags
```

The tag must exactly match the version in `pyproject.toml` with a
leading `v`. GitHub Actions then builds the Python wheel + sdist and
creates a GitHub Release with those artifacts attached.

5. Upload the DMG to the auto-created release:

```bash
gh release upload v0.0.1 apps/macos/dist/OpenSeer-0.0.1.dmg
```

## What the workflow does

On tag push, GitHub Actions:

1. Builds the source distribution and wheel.
2. Runs `twine check` on the distributions.
3. Creates a GitHub Release and attaches the built artifacts.

The workflow can also be run manually from GitHub Actions to
validate `python -m build` against a branch without cutting a
release.
