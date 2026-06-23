# Changelog

All notable changes to VAF are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VAF aims to follow
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`, with PEP 440
prerelease suffixes such as `a0` / `b1` / `rc1`).

Each released version has a matching git tag `v<version>` and a GitHub Release.
To update an installed VAF, run `vaf update`.

## [Unreleased]

### Added
- Embeddable library surface: `from vaf import Agent` (`docs/EMBEDDING.md`,
  `docs/ARCHITECTURE.md`); slim base install plus optional extras in `setup.py`.
- Entry-point tool discovery: third-party tools via the `vaf.tools` group.
- Tool input validation & repair before dispatch (`docs/agents/TOOL_INPUT_REPAIR.md`).
- Self-update: `vaf update check` / `vaf update`, an opt-in startup
  update-available hint, and a tag-triggered GitHub release workflow.
- Web search result cache: identical `web_search` queries are served from a
  short-lived file cache (default 15 min; `web_search_cache_enabled` /
  `web_search_cache_ttl_seconds`), skipping the providers and synthesis.

### Fixed
- Filesystem alias resolution now matches only on a path boundary.
- `send_mail` no longer silently drops a single string attachment path.

<!--
Template for a new release (see docs/setup/RELEASING.md):

## [X.Y.Z] - YYYY-MM-DD
### Added
### Changed
### Fixed
### Removed
-->
