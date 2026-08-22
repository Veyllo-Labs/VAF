# Embedder contract suite (breaking-change tests)

This directory pins the public embedding contract of VAF - the surface
documented in [docs/EMBEDDING.md](../../docs/EMBEDDING.md) under "What is and
isn't stable" - as executable tests. A failure here means the promised
surface changed: for VAF's own CI that is a breaking change to either revert
or ship deliberately (with the matching EMBEDDING.md edit, a CHANGELOG entry
and an updated pin in the same commit); for an embedder it is the early
warning that an upgrade will break their integration.

## What is covered

One file per contract module:

| File | Pins |
|---|---|
| `test_contract_facade.py` | `vaf.__all__`, lazy exports, `__version__` |
| `test_contract_agent.py` | `Agent` construction, signatures, eager validations, `VAF_NONINTERACTIVE` |
| `test_contract_coreagent_surface.py` | `CoreAgent` documented signatures (subset pins) |
| `test_contract_tool_caller.py` | `ToolCaller.execute()` pipeline, return prefixes, events, gate |
| `test_contract_allowlist.py` | `set_account_allowlist_resolver` contract, and `account_allows_tool` (its read side, for surfaces that list tools) |
| `test_contract_basetool_and_jail.py` | `BaseTool` declarations, `file_access` TypeError, `self.log`, `user_jail` |
| `test_contract_markers_and_slim_import.py` | `vaf.markers` values, `import vaf` stays cheap |
| `test_contract_pdf_extract.py` | `extract_pdf_markdown` signature, result dict, missing-extra ImportError |
| `test_contract_session_turn_context.py` | session/turn context API (`vaf.core.subagent_ipc`) |
| `test_contract_entry_points.py` | the `vaf.tools` entry-point group loader |

The suite is offline by design: no network, no API keys, no Docker, no model
downloads, and no writes outside pytest temp directories. Internals under
`vaf.core.*` are deliberately NOT pinned beyond the names EMBEDDING.md itself
documents - a test here that blocks an internal refactor is a bug in the
test. Where a test must reach past the facade (an isolation fixture, the
entry-point loader seam), the import carries a comment saying so.

## Running it as an embedder (vendoring)

Copy this directory out of the VAF tag you built against and run it in your
own CI against every VAF version you consider upgrading to:

```bash
# once, at adoption time (pin the tag you build on):
git clone --depth 1 --branch <your-pinned-tag> https://github.com/Veyllo-Labs/VAF.git
cp -r VAF/tests/contract ./ci/vaf_contract

# in CI, against the candidate upgrade:
pip install --pre "vaf==<candidate-version>"
python -m pytest ./ci/vaf_contract -q
```

Do NOT re-copy the directory when you upgrade the package: the point of the
vendored copy is that it encodes the contract of the version you BUILT
against, so a newer VAF that breaks it fails your CI. Re-vendor only when you
deliberately adopt a new baseline (and re-read the EMBEDDING.md diff when you
do).

Two mechanics worth knowing:

- The suite detects that it runs outside the VAF repo (no parent
  `conftest.py`) and then isolates `HOME`, `USERPROFILE`, the XDG/Windows
  store variables and `VAF_LOG_DIR` at collection time, so nothing touches
  the CI user's real home or config directories. Run it in a fresh pytest
  process without plugins that import `vaf` earlier.
- Run it from a working directory that does not contain a `vaf/` source
  tree, or that tree shadows the installed package under test.
- Keep the copied directory's name a valid Python identifier (`vaf_contract`,
  not `vaf-contract`): the directory is a package (`__init__.py`), which is
  what keeps its `conftest.py` from ever shadowing your project's own
  top-level conftest.

## Keeping it current (VAF maintainers)

The suite runs as part of the normal repo test run, so drift is caught
mechanically, not by review discipline:

- `test_contract_facade.py` pins `vaf.__all__` exactly. Adding a facade
  export fails the suite until the export gets its contract tests and its
  EMBEDDING.md section - that is the update reminder, by design.
- When a documented behavior changes deliberately, the pin here, the
  EMBEDDING.md sentence and the CHANGELOG entry change in the same commit.
  A contract-test diff IS the machine-readable list of breaking changes for
  a release.

House rules for new tests here: public imports only (facade plus the
`vaf.core.subagent_ipc` names EMBEDDING.md documents); every deeper import
needs a justifying comment; pin error strings by prefix/substring, never by
full prose; marker VALUES are exact contract; everything must pass both
in-repo and vendored (`STANDALONE` mode in `conftest.py`).
