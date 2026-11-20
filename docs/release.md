# Releasing agentcrash-core to PyPI

Releases are tag-driven. Pushing a `v*` tag runs [release.yml](../.github/workflows/release.yml),
which gates, builds, smoke-tests, and publishes to PyPI with trusted publishing
(no token). Pushing to `develop` or `main` alone never publishes.

## Prerequisites (one-time, owner)

PyPI trusted publisher plus the `pypi` GitHub environment, as documented in
[13-contributing-and-workflow.md](adlc/13-contributing-and-workflow.md) section 8.
Without it the `Publish to PyPI` job fails on OIDC.

## Release steps

1. Bump the version in both places (the release job refuses a tag that
   does not match both):
   - `version` in [pyproject.toml](../pyproject.toml)
   - `__version__` in [src/agentcrash/__init__.py](../src/agentcrash/__init__.py)
2. Add a `CHANGELOG.md` entry. While the package is `0.x`, a minor release
   may break the API, so every break gets a migration row.
3. Run the gates locally (Windows PowerShell):
   - `uv run --no-sync ruff check .`
   - `uv run --no-sync pytest -q`
   - `black --check .` runs in CI (it cannot run on this machine: App Control
     blocks its native module, so rely on the CI Format gate)
   - `bench/run_overhead.py` must stay within its budgets (NFR-PERF targets)
   - `.venv\Scripts\python.exe -m agentcrash --version` must print the new version
4. Commit on `develop`, then tag that commit and push both:
   - `git commit -m 'release: vX.Y.Z'`
   - `git tag vX.Y.Z`
   - `git push origin develop vX.Y.Z`
5. Watch the `release` workflow for the tag: Gates (3.11-3.13) →
   Build sdist + wheel (`twine check --strict`, clean-venv smoke install) →
   Publish to PyPI. The run must conclude `success`.

## After the release

- Verify on PyPI: `https://pypi.org/project/agentcrash-core/<version>/`
  and `pip index versions agentcrash-core` (or `pip install agentcrash-core==<version>`
  in a clean venv).
- Per repo convention, merge the release commit to `main` (see section 8 of
  [13-contributing-and-workflow.md](adlc/13-contributing-and-workflow.md)).

## Rules that bite

- PyPI uploads are permanent: a version number can never be reused. A bad
  release is yanked on pypi.org and fixed with a new patch version, never
  re-uploaded.
- Tag, `pyproject.toml`, and `__version__` must all agree, or the build job
  fails before anything publishes.
- Rollback: yank the release on pypi.org, then publish a fixed patch version.
  Event and policy schemas are versioned (`v1`), so a code rollback inside a
  major schema version needs no data migration.
