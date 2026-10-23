# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The hand-maintained mirrors of the dependency manifests stay in sync.

Two files restate what requirements.txt, pyproject.toml and the two package.json
files declare, and both are edited by hand:

- The DEPENDENCIES map in ``bootstrap()`` (vaf/main.py) probes each import name on
  every process start and, on a miss, offers ``pip install -r requirements.txt``.
  A pip name in the map that requirements.txt does not carry is therefore an
  endless prompt: pip installs the file, the probe still misses, the next start
  asks again (the rumps incident; the same shape a dropped manifest line takes
  when its map entry is forgotten).
- docs/legal/THIRD_PARTY.md states that it inventories every DIRECT dependency
  with its licence. A row for a package no manifest names, or a manifest entry
  with no row, is a false licence statement in a public repository. Measured
  before this guard existed: seven Python and four web dependencies had no row,
  and one web row named a package that is not a dependency.

Neither drift was caught by anything, so both are pinned here.

A third mirror is an UPPER BOUND. requirements.txt is what CI and the release job
install, pyproject.toml is what a pip install of the package resolves, and the
THIRD_PARTY row states the range. A cap in one and not the other hands one of the
three a major release nobody tested: CI installed mcp 2.x from an uncapped
requirements.txt while the lock held 1.x, and the remote MCP tests failed on a
renamed class.

A fourth is a FLOOR, for the packages whose floor a VAF behaviour needs. An update
(``pip install -r requirements.txt``) keeps an installed version that satisfies the
floor, so a floor below the version a behaviour needs leaves old installs without it:
uvicorn's sans-I/O WebSocket protocol, the one the door names, makes a sender wait
from 0.52.1 (the manifests floor uvicorn at 0.53, the first release the door's tests
ran on), and ``websockets.connect`` is the asyncio client the proxy's relay calls from
websockets 14.0. Only these floors are compared.
"""
import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm(name: str) -> str:
    """One spelling per package: no extras, no markdown, no platform note."""
    name = name.strip().strip("`").strip("*")
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)
    return name.strip().lower().replace("_", "-")


def _requirements_txt_names() -> set:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = _NAME_RE.match(line)
        if m:
            names.add(_norm(m.group(1)))
    return names


def _pyproject_names() -> set:
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    specs = list(data["project"]["dependencies"])
    for extra_specs in data["project"]["optional-dependencies"].values():
        specs.extend(extra_specs)
    names = set()
    for spec in specs:
        m = _NAME_RE.match(spec.strip())
        if m and m.group(1).lower() != "vaf":  # the self-referential `all` extra
            names.add(_norm(m.group(1)))
    return names


def _package_json_names(rel: str) -> set:
    data = json.loads((ROOT / rel).read_text(encoding="utf-8-sig"))
    names = set()
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        names.update(_norm(k) for k in data.get(field, {}))
    return names


def _bootstrap_dependency_map_keys() -> list:
    tree = ast.parse((ROOT / "vaf/main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "bootstrap"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "DEPENDENCIES" for t in sub.targets
            ):
                assert isinstance(sub.value, ast.Dict)
                return [k.value for k in sub.value.keys]
    raise AssertionError("bootstrap() DEPENDENCIES map not found in vaf/main.py")


def _third_party_rows() -> dict:
    """Section title -> package names of the table rows under it."""
    sections: dict = {}
    current = None
    for line in (ROOT / "docs/legal/THIRD_PARTY.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, set())
            continue
        if current is None or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] == "Package" or set(cells[0]) <= set("-: "):
            continue
        sections[current].add(_norm(cells[0]))
    return sections


def _rows_for(prefix: str) -> set:
    rows = set()
    for title, names in _third_party_rows().items():
        if title.startswith(prefix):
            rows |= names
    return rows


def test_bootstrap_dependency_map_names_only_requirements_entries():
    missing = [k for k in _bootstrap_dependency_map_keys() if _norm(k) not in _requirements_txt_names()]
    assert not missing, (
        "vaf/main.py bootstrap() DEPENDENCIES names packages that requirements.txt "
        f"does not install: {missing}. Every fresh install would prompt for them "
        "on every start (pip installs the file, the probe still misses)."
    )


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib requires Python 3.11+")
def test_third_party_inventory_matches_python_manifests():
    manifests = _requirements_txt_names() | _pyproject_names()
    rows = _rows_for("Python")
    assert rows - manifests == set(), (
        "docs/legal/THIRD_PARTY.md lists Python packages that no manifest declares "
        f"(stale rows): {sorted(rows - manifests)}"
    )
    assert manifests - rows == set(), (
        "requirements.txt / pyproject.toml declare Python packages that "
        f"docs/legal/THIRD_PARTY.md has no licence row for: {sorted(manifests - rows)}"
    )


def test_third_party_inventory_matches_web_manifest():
    deps = _package_json_names("web/package.json")
    rows = _rows_for("Web UI")
    assert rows - deps == set(), (
        f"docs/legal/THIRD_PARTY.md lists web packages that web/package.json does not declare: {sorted(rows - deps)}"
    )
    assert deps - rows == set(), (
        f"web/package.json declares packages without a licence row in docs/legal/THIRD_PARTY.md: {sorted(deps - rows)}"
    )


def test_third_party_inventory_matches_whatsapp_bridge_manifest():
    deps = _package_json_names("vaf/whatsapp_node/package.json")
    rows = _rows_for("WhatsApp bridge")
    assert rows == deps, (
        "docs/legal/THIRD_PARTY.md and vaf/whatsapp_node/package.json disagree: "
        f"stale rows {sorted(rows - deps)}, missing rows {sorted(deps - rows)}"
    )


def _upper_bounds(spec: str) -> frozenset:
    """The `<` / `<=` clauses of one requirement's version range, markers dropped."""
    rng = spec.split(";", 1)[0]
    return frozenset(c.replace(" ", "") for c in rng.split(",") if c.strip().startswith("<"))


def _ranges() -> dict:
    """Manifest -> {package: version range as written}."""
    import tomllib

    def split(spec):
        m = _NAME_RE.match(spec.strip())
        return (_norm(m.group(1)), spec.strip()[m.end():]) if m else (None, "")

    out = {"requirements.txt": {}, "pyproject.toml": {}, "THIRD_PARTY.md": {}}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name, rng = split(line.split("#", 1)[0])
        if name:
            out["requirements.txt"][name] = rng
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    specs = list(data["project"]["dependencies"])
    for extra_specs in data["project"]["optional-dependencies"].values():
        specs.extend(extra_specs)
    for spec in specs:
        name, rng = split(spec)
        if name and name != "vaf":
            out["pyproject.toml"][name] = rng
    for line in (ROOT / "docs/legal/THIRD_PARTY.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 3 and cells[1][:1] in "<>=~!":
            out["THIRD_PARTY.md"][_norm(cells[0])] = cells[1]
    return out


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib requires Python 3.11+")
def test_an_upper_bound_is_the_same_in_every_manifest_that_names_the_package():
    """MUTATION: drop `,<2` from the mcp line of pyproject.toml and this goes red."""
    ranges = _ranges()
    capped = {n for r in ranges.values() for n, rng in r.items() if _upper_bounds(rng)}
    assert capped, "no capped package found: the parser no longer reads the manifests"
    drift = {}
    for name in sorted(capped):
        seen = {src: sorted(_upper_bounds(r[name])) for src, r in ranges.items() if name in r}
        if len({tuple(v) for v in seen.values()}) > 1:
            drift[name] = seen
    assert not drift, (
        "An upper bound differs between the manifests that name the package "
        f"(requirements.txt is what CI and releases install, pyproject.toml what pip resolves): {drift}"
    )


def _floors(package: str) -> dict:
    """Manifest -> the ``>=`` floor it writes for one package, extras and markers dropped."""
    floors = {}
    for src, ranges in _ranges().items():
        if package not in ranges:
            continue
        rng = re.sub(r"^\[.*?\]", "", ranges[package].split(";", 1)[0].strip())
        found = [c.strip()[2:].strip() for c in rng.split(",") if c.strip().startswith(">=")]
        floors[src] = found[0] if found else None
    return floors


def _assert_floor(package: str, minimum: str, why: str) -> None:
    from packaging.version import Version

    floors = _floors(package)
    assert set(floors) == {"requirements.txt", "pyproject.toml", "THIRD_PARTY.md"}, (
        f"{package} is missing from a manifest: {floors}")
    assert len(set(floors.values())) == 1, (
        f"{package}'s floor differs between the manifests (requirements.txt is what CI and "
        f"releases install, pyproject.toml what pip resolves): {floors}")
    floor = floors["requirements.txt"]
    assert floor is not None and Version(floor) >= Version(minimum), (
        f"{package}'s floor {floor} is below {minimum}: {why}")


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib requires Python 3.11+")
def test_the_uvicorn_floor_is_where_a_websocket_makes_its_sender_wait():
    """The door serves uvicorn's sans-I/O WebSocket protocol (vaf/network/door.py
    WS_PROTOCOL), which defines its own pause_writing, and so makes a sender wait while its
    client reads nothing, from uvicorn 0.52.1. The floor is 0.53.0, the first release the
    door's tests ran on and the one the docs name. CI and releases install requirements.txt
    and a pip install resolves pyproject.toml, so each of them must hold uvicorn at or above
    it. MUTATION: the pyproject.toml floor back to >=0.27.0, requirements.txt to >=0.52.0, or
    all three at >=0.52.1 - red."""
    _assert_floor("uvicorn", "0.53.0",
                  "the door's WebSocket makes a sender wait only from 0.52.1, and 0.53.0 is the "
                  "first release its tests ran on (vaf/network/door.py WS_PROTOCOL)")


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib requires Python 3.11+")
def test_the_websockets_floor_has_the_client_the_relay_calls():
    """The proxy's WebSocket relay calls websockets.connect(..., additional_headers=...)
    (vaf/network/https_proxy.py). Up to websockets 13.1 that name is the legacy client, which
    takes extra_headers and hands an unknown keyword on to loop.create_connection, so every
    relayed socket would fail; from 14.0 it is the asyncio client. uvicorn[standard] lets
    13.x in. MUTATION: the THIRD_PARTY.md floor back to >=12.0 - red."""
    _assert_floor("websockets", "14.0",
                  "websockets.connect would be the legacy client (vaf/network/https_proxy.py)")
