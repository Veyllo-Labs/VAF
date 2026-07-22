# Releasing VAF

VAF is distributed over two channels, and every release feeds both:

- **Source checkouts** (desktop/server installs): a git checkout updated in
  place via `vaf update`, which targets the latest **published GitHub Release**
  (a git tag `v<version>`).
- **The PyPI package** (library embedders): the same tag also builds and
  publishes the sdist+wheel to PyPI, so `pip install --pre vaf` serves the
  release. pip installs update via `pip install -U --pre vaf`, never via
  `vaf update` (the self-updater refuses non-source layouts).

Cutting a release is therefore the act that makes a new version available to
all clients on both channels.

## Versioning

- Single source of truth: `vaf/version.py` (`__version__`).
- Everything that shows or reports a version derives from it: the CLI (`vaf --version`), the About screen, the backend APIs (`GET /api/version` and the FastAPI app versions), and the MCP handshake. Never hardcode a version string; the web UI fetches `/api/version`.
- Semantic Versioning `MAJOR.MINOR.PATCH`; prereleases use PEP 440 suffixes
  (`a0`, `b1`, `rc1`). Example: `0.1.0a0`.
- The git tag is the version with a `v` prefix: `__version__ = "0.1.0"` → tag `v0.1.0`.
- A tag whose version is exactly `X.Y.Z` is published as a normal release; a tag
  with a prerelease suffix is published as a GitHub *prerelease*.

### Prereleases and `vaf update`

`vaf update` reads the releases **list** (not `/releases/latest`, which excludes prereleases) and
picks the newest **eligible** release. Eligibility defaults to **auto**: a build that is itself a
prerelease (e.g. `0.1.0a0`) tracks prereleases, while a stable build tracks stable releases only.
Override per install with the `update_include_prereleases` config key (`null` = auto, `true` =
always, `false` = stable-only) or per command with `vaf update --pre` / `--stable`. So during the
alpha, clients running `0.1.0aN` see and install newer `aN` releases automatically — without
waiting for a stable `X.Y.Z`.

### Recovering or pinning a version

- **Resume an interrupted update.** Each update writes a breadcrumb before it
  mutates anything; if it is interrupted (power loss, killed mid-checkout),
  `vaf update --recover` restores the previous version. If the recovery checkout
  is itself blocked (e.g. by colliding local files), it stops and keeps the
  breadcrumb so you can resolve the conflict and re-run it — it never reports
  success while the tree is in a mixed state.
- **Pin to a specific release.** `vaf update --tag v<version>` updates to that tag
  instead of the latest. Pinning to an older release is a downgrade — the updater
  warns before proceeding.
- **Preview only.** `vaf update --dry-run` prints the planned steps and the
  rollback anchor without changing anything.

## Backward-compatibility rules

- Do not break the public surface (`from vaf import Agent`, `BaseTool`, documented
  config keys) without a `MAJOR` bump and a deprecation note. See
  [../ARCHITECTURE.md](../ARCHITECTURE.md).
- Config migrations are **additive only**: a migration may add keys, never remove
  or rename a key that an older version still reads (a client may roll back). Add
  migrations in `vaf/core/migrations.py`.
- DB schema changes follow the same additive rule. A new **column** on an existing
  model needs no migration — the schema reconcile in `vaf/memory/database.py` adds
  any missing column automatically on the next start (and during `vaf update`). A
  non-additive change (a new index, a rename, a backfill) goes in
  `vaf/memory/db_migrations.py` as an idempotent ordered migration. Changing the
  embedding model to a different **dimension** is a breaking change with no in-place
  migration: the reconcile detects and loudly reports the mismatch, and the memory
  store must be re-embedded/reset.

## Cutting a release

1. Make sure `main` is green in CI and everything intended for the release is merged.
2. Bump `vaf/version.py` (the single source of truth) to the new version, and bump
   `web/package.json` to the npm/semver spelling of the same version
   (e.g. `0.1.0a0` -> `0.1.0-alpha.0`, `0.1.0` -> `0.1.0`). The release workflow fails
   the build if the two disagree, so they must stay in lockstep.
3. In `CHANGELOG.md`, move the relevant `[Unreleased]` notes into a new
   `## [X.Y.Z] - YYYY-MM-DD` section.
4. Commit (on request) and push to `main`.
5. Tag and push the tag:
   ```bash
   git tag v$(python -c "import vaf; print(vaf.__version__)")
   git push origin --tags
   ```
6. The tag triggers `.github/workflows/release.yml`, which re-runs the test gate
   and, on success, creates the GitHub Release (notes pulled from the matching
   `CHANGELOG.md` section) and then publishes the sdist+wheel to PyPI
   (`publish-pypi` job). Source checkouts update via git; pip installs via pip.

## The PyPI channel

- **Publishing is tokenless** (Trusted Publishing / OIDC): PyPI trusts this
  repo's `release.yml` running in the GitHub environment `pypi`. There is no
  API token to rotate or leak. First-time setup: the trusted publisher must be
  registered on pypi.org (project `vaf`, owner `Veyllo-Labs`, repo `VAF`,
  workflow `release.yml`, environment `pypi`) and the `pypi` environment must
  exist in the repo settings.
- **PyPI versions are immutable.** A published version can be yanked but never
  replaced - a broken upload burns that version number for good. Rehearse the
  full build+upload round-trip against TestPyPI first: run the manual
  `publish-testpypi.yml` workflow (its trusted publisher lives on
  test.pypi.org with environment `testpypi`), then verify with
  `pip install --pre -i https://test.pypi.org/simple/ --extra-index-url
  https://pypi.org/simple/ "vaf==<version>"` - pin the rehearsed version:
  unpinned, pip may resolve `vaf` from real PyPI once the project exists
  there.
- **Alpha releases need `--pre`**: plain `pip install vaf` resolves
  prereleases only while no stable release exists; documenting
  `pip install --pre vaf` stays correct either way until the first stable.
- **Keep the channels in lockstep**: never tag without the PyPI publish
  succeeding (a stale PyPI package is worse for framework credibility than
  none). If `publish-pypi` fails after the GitHub Release was created,
  nothing has been uploaded yet (the upload is the last step) - first fix
  the cause and RE-RUN the failed job; the version is not burned. Re-tag as
  the next patch/prerelease version only when the tagged commit or the
  built artifact itself is at fault.

## Verifying

- The release appears at `https://github.com/Veyllo-Labs/VAF/releases`.
- PyPI shows the new version at `https://pypi.org/project/vaf/`, and
  `pip install --pre "vaf==<version>"` works from a clean environment.
- `gh api repos/Veyllo-Labs/VAF/releases` lists it (the first entry is the newest). Note
  `releases/latest` only returns **stable** releases, so during the alpha it stays empty.
- On any installed checkout, `vaf update check` reports the new version (a `0.1.0aN` build sees
  newer prereleases automatically; see *Prereleases and `vaf update`*).
