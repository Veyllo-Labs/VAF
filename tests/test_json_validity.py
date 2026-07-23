# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every tracked JSON file must parse; npm manifests must be structurally sane.

During the 2026-07-23 dependency-security update a hand edit put a stray closing
brace into web/package.json. The break surfaced only when the next npm invocation
refused the file - after the edit, not at commit time. JSON that does not parse (or
parses but silently drops data, as duplicate keys do) must fail fast, locally and in
CI, before it can land in history.

Checks:

- every git-tracked *.json file parses as strict UTF-8 JSON (BOM tolerated);
- no JSON object in those files carries a duplicate key (legal JSON, but in
  hand-maintained files virtually always an editing accident that silently discards
  the first value);
- the npm manifests keep the structure npm relies on: dependency maps are flat
  string-to-string objects, and a package listed in "overrides" that is also a
  direct dependency must use the IDENTICAL spec string - npm hard-errors on a
  mismatch ("Override for X conflicts with direct dependency"), which is exactly how
  the first postcss override attempt failed on 2026-07-23;
- each manifest agrees with its package-lock.json at the root (name, version, and
  every direct dependency spec verbatim in the lock's root node), so a package.json
  edit cannot ship without its lock regeneration.

The LOCAL pre-commit hook (never committed; see test_public_repo_hygiene.py for the
other layers) additionally parses every staged *.json before the commit exists. If
the hook is missing (fresh clone), recreate the JSON layer by adding to
.git/hooks/pre-commit before the final "exit 0":

    for f in $(git diff --cached --name-only --diff-filter=ACM -- '*.json'); do
        git show ":$f" | python3 -c \
            "import json,sys; json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))" \
            || { echo "pre-commit hook: $f is not valid JSON." >&2; exit 1; }
    done
"""
import json
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

_NPM_MANIFESTS = (
    Path("web/package.json"),
    Path("vaf/whatsapp_node/package.json"),
)

_DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _tracked_json_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.json"],
        cwd=_REPO,
        capture_output=True,
        check=True,
    )
    return [rel for rel in out.stdout.decode().split("\0") if rel]


def _reject_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate object key {key!r}")
        seen.add(key)
    return dict(pairs)


def _parse_strict(raw: bytes):
    """The one parser every check here uses: UTF-8 (BOM tolerated), duplicate keys rejected."""
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_keys)


def _load_json(rel: Path) -> dict:
    data = _parse_strict((_REPO / rel).read_bytes())
    assert isinstance(data, dict), f"{rel}: top level must be a JSON object"
    return data


def test_every_tracked_json_file_parses() -> None:
    failures = []
    for rel in _tracked_json_files():
        path = _REPO / rel
        if not path.exists():
            continue  # tracked but deleted in the working tree; git will drop it
        try:
            _parse_strict(path.read_bytes())
        except (ValueError, UnicodeDecodeError) as exc:
            failures.append(f"{rel}: {exc}")
    assert not failures, "Tracked JSON files failed strict parsing:\n" + "\n".join(failures)


@pytest.mark.parametrize("rel", _NPM_MANIFESTS, ids=str)
def test_npm_manifest_structure(rel: Path) -> None:
    data = _load_json(rel)
    for key in ("name", "version"):
        assert isinstance(data.get(key), str) and data[key], (
            f"{rel}: '{key}' must be a non-empty string"
        )
    for field in _DEP_FIELDS:
        block = data.get(field)
        if block is None:
            continue
        assert isinstance(block, dict), f"{rel}: '{field}' must be an object"
        for pkg, spec in block.items():
            assert isinstance(spec, str) and spec, (
                f"{rel}: {field}[{pkg!r}] must be a non-empty version spec string"
            )
    overrides = data.get("overrides")
    if overrides is not None:
        assert isinstance(overrides, dict), f"{rel}: 'overrides' must be an object"
        for pkg, spec in overrides.items():
            # npm allows nested override objects; today this repo uses flat strings only.
            assert isinstance(spec, (str, dict)) and spec, (
                f"{rel}: overrides[{pkg!r}] must be a non-empty spec string or object"
            )


@pytest.mark.parametrize("rel", _NPM_MANIFESTS, ids=str)
def test_npm_override_specs_match_direct_dependencies(rel: Path) -> None:
    data = _load_json(rel)
    overrides = data.get("overrides", {})
    conflicts = []
    for field in _DEP_FIELDS:
        for pkg, spec in data.get(field, {}).items():
            ov = overrides.get(pkg)
            if isinstance(ov, str) and not ov.startswith("$") and ov != spec:
                conflicts.append(
                    f"{rel}: '{pkg}' is {spec!r} in {field} but {ov!r} in overrides"
                )
    assert not conflicts, (
        "An override for a direct dependency must repeat its spec verbatim "
        "(npm: 'Override for X conflicts with direct dependency'):\n"
        + "\n".join(conflicts)
    )


@pytest.mark.parametrize("rel", _NPM_MANIFESTS, ids=str)
def test_npm_manifest_agrees_with_its_lockfile(rel: Path) -> None:
    lock_rel = rel.parent / "package-lock.json"
    data = _load_json(rel)
    lock = _load_json(lock_rel)
    assert lock.get("name") == data["name"], f"{lock_rel}: 'name' differs from {rel}"
    assert lock.get("version") == data["version"], (
        f"{lock_rel}: 'version' differs from {rel} - regenerate the lock"
    )
    root = lock.get("packages", {}).get("", {})
    drift = []
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        have = root.get(field, {})
        for pkg, spec in data.get(field, {}).items():
            if have.get(pkg) != spec:
                drift.append(
                    f"{pkg}: manifest {field} wants {spec!r}, lock root has {have.get(pkg)!r}"
                )
    assert not drift, (
        f"{rel} and {lock_rel} disagree - regenerate the lock "
        "(npm install, or npm update <pkg> --package-lock-only while the app runs):\n"
        + "\n".join(drift)
    )


def test_parser_rejects_the_stray_brace_incident() -> None:
    """The exact 2026-07-23 accident: one extra closing brace at the end of the file."""
    text = (_REPO / "web/package.json").read_bytes().decode("utf-8-sig").rstrip()
    assert text.endswith("}")
    broken = text + "\n}\n"
    with pytest.raises(ValueError):
        _parse_strict(broken.encode())


def test_parser_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate object key"):
        _parse_strict(b'{"a": 1, "a": 2}')
