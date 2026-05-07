# Releasing OpenSeer

OpenSeer is a Python package. There is no npm package in this repository.

## One-time PyPI setup

The release workflow uses PyPI Trusted Publishing, so no long-lived PyPI API
token is needed in GitHub Secrets.

In PyPI, create or configure the `openseer` project with this trusted
publisher:

- Owner: `TobyGE`
- Repository name: `OpenSeer`
- Workflow name: `release.yml`
- Environment name: `pypi`

The workflow file is `.github/workflows/release.yml`.

## Cut a release

1. Bump `[project].version` in `pyproject.toml`.
2. Commit and push the version bump.
3. Create and push a matching tag:

```bash
git tag v0.0.1
git push origin main --tags
```

The tag must exactly match the version in `pyproject.toml` with a leading `v`.
For example, `version = "0.0.1"` must be released as tag `v0.0.1`.

## What the workflow does

On tag push, GitHub Actions will:

1. Build the source distribution and wheel.
2. Run `twine check` on the distributions.
3. Create a GitHub Release and attach the built artifacts.
4. Publish the same artifacts to PyPI via Trusted Publishing.

The workflow can also be run manually from GitHub Actions. Manual runs build
and validate artifacts by default; set `publish_pypi=true` only when you want
to publish from a manual run.
