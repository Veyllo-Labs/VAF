# Releasing VAF

VAF is distributed as a git checkout and updated in place via `vaf update`, which
targets the latest **published GitHub Release** (a git tag `v<version>`). Cutting
a release is therefore the act that makes a new version available to all clients.

## Versioning

- Single source of truth: `vaf/version.py` (`__version__`).
- Everything that shows or reports a version derives from it: the CLI (`vaf --version`), the About screen, the backend APIs (`GET /api/version` and the FastAPI app versions), and the MCP handshake. Never hardcode a version string; the web UI fetches `/api/version`.
- Semantic Versioning `MAJOR.MINOR.PATCH`; prereleases use PEP 440 suffixes
  (`a0`, `b1`, `rc1`). Example: `2.6.0a0`.
- The git tag is the version with a `v` prefix: `__version__ = "2.6.0"` → tag `v2.6.0`.
- A tag whose version is exactly `X.Y.Z` is published as a normal release; a tag
  with a prerelease suffix is published as a GitHub *prerelease*.

### Prereleases and `vaf update`

`vaf update` reads the releases **list** (not `/releases/latest`, which excludes prereleases) and
picks the newest **eligible** release. Eligibility defaults to **auto**: a build that is itself a
prerelease (e.g. `2.6.0a0`) tracks prereleases, while a stable build tracks stable releases only.
Override per install with the `update_include_prereleases` config key (`null` = auto, `true` =
always, `false` = stable-only) or per command with `vaf update --pre` / `--stable`. So during the
alpha, clients running `2.6.0aN` see and install newer `aN` releases automatically — without
waiting for a stable `X.Y.Z`.

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
   `CHANGELOG.md` section). No artifact upload is needed — clients update via git.

## Verifying

- The release appears at `https://github.com/Veyllo-Labs/VAF/releases`.
- `gh api repos/Veyllo-Labs/VAF/releases` lists it (the first entry is the newest). Note
  `releases/latest` only returns **stable** releases, so during the alpha it stays empty.
- On any installed checkout, `vaf update check` reports the new version (a `2.6.0aN` build sees
  newer prereleases automatically; see *Prereleases and `vaf update`*).
