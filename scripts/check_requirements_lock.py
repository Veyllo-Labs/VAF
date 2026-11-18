# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Check requirements.lock for every operating system and Python the CI installs it on.

requirements.lock is one universal, hash-pinned resolution of requirements.txt (``uv pip
compile --universal``, the command is in its header): an entry can carry an environment
marker, and one package can appear twice under markers that exclude each other (numpy for
Python 3.10 and for 3.11 and later). The CI installs it with ``pip install --require-hashes``
on Linux, macOS and Windows with every Python of the matrices in .github/workflows/ci.yml and
ci-nightly.yml. pip cannot prove a foreign target from this machine: a dry run with
``--platform``/``--python-version`` evaluates the lock's markers against the running
interpreter (measured: a 3.10 dry run tried an entry marked for 3.13 and later). So the proof is
read from metadata.

Default mode (network, metadata only): for every target of the matrices it selects the entries
whose markers apply, and reports
  - two entries of one package that apply at once;
  - an entry with no wheel pip would install there, or one whose Requires-Python excludes the
    target (a release with only an sdist is listed as built on install, not as a problem);
  - a requirement of the wheel pip would pick there (its PEP 658 metadata, read per wheel, since
    two wheels of one release can declare different requirements) that no applying entry
    satisfies, with the extras the tree asks for;
  - a requirement of requirements.txt (following ``-r``) whose marker applies and that no
    applying entry satisfies;
  - a published file of a pinned release whose hash the lock does not carry.

``--drift [--report]`` (offline) compares the distributions installed in the running
interpreter with the entries whose markers apply to it and prints package / locked / installed
for every difference: a wrong version, a locked package that is missing, an installed package
the lock does not name (pip, the project itself and the build tools the interpreter ships are
not counted unless the lock names them). Exit 1 on a difference; with ``--report`` it prints a
Markdown table for a CI step summary and exits 0.

The PyPI reads follow scripts/check_vllm_lock.py and use its ``_get_json`` and ``_get_text``,
each retried on a network error or a 5xx answer. Its target model is
not shared: those targets are Linux glibc floors, this lock also needs macOS and Windows tags,
and its parser keeps one entry per name, which cannot hold a forked entry.

    venv/bin/python scripts/check_requirements_lock.py [--requirements FILE] [path/to/a.lock]
    python scripts/check_requirements_lock.py --drift [--report]

The lock defaults to requirements.lock and its input to requirements.txt; the small locks of
the CI tools are checked with their own input (``--requirements .github/requirements/pip.in
.github/requirements/pip.lock``). The hash check runs both ways: every file PyPI publishes for
a pinned release must be in the lock, and every hash in the lock must be one of them.
"""
from __future__ import annotations

import concurrent.futures
import email.parser
import http.client
import importlib.metadata
import json
import re
import shlex
import sys
import time
import urllib.error
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from packaging import tags
from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
REQUIREMENTS = ROOT / "requirements.txt"
WORKFLOWS = (ROOT / ".github" / "workflows" / "ci.yml", ROOT / ".github" / "workflows" / "ci-nightly.yml")

# The distribution this repository builds (pyproject.toml [project].name); CI installs it
# editable with --no-deps, so it is never in the lock.
PROJECT = "vaf"
# Installed but unlocked, and still no drift: pip comes by hash from .github/requirements/
# pip.lock, and an interpreter can ship setuptools and wheel. When the lock names one of
# them, it is compared. A package a CI runner's own Python ships belongs here too (the drift
# message names this set when it reports one).
_UNLOCKED_TOOLS = frozenset({"pip", "setuptools", "wheel", PROJECT})

# A comment starts at a '#' that begins the line or follows whitespace, as pip reads it.
_COMMENT = re.compile(r"(^|\s+)#.*$")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


@dataclass(frozen=True)
class Entry:
    """One pinned line of the lock."""

    name: str  # canonical
    version: str
    extras: frozenset
    marker: Marker | None
    hashes: frozenset
    line: int  # first physical line, 1-based
    requirement: str  # the requirement text, options dropped

    def applies(self, env: dict) -> bool:
        return self.marker is None or self.marker.evaluate({**env, "extra": ""})

    def __str__(self) -> str:
        return f"{self.name}=={self.version}"


def logical_lines(text: str) -> list:
    """(first line number, text) of every non-empty logical line, read as pip 26 reads a
    requirements file (pip/_internal/req/req_file.py: join_lines, then ignore_comments).

    A line ending in a backslash is continued, its backslashes stripped and the next line
    glued on with no separator; a line that starts with a comment ends the continuation. The
    comments are dropped from the joined text afterwards, so a '#' inside a continued line
    swallows the rest of it. pip's ${VAR} expansion is not done: a lock never carries one.
    """
    joined = []
    buf: list = []
    start = 0
    for no, line in enumerate(text.splitlines(), 1):
        if not line.endswith("\\") or _COMMENT.match(line):
            if _COMMENT.match(line):
                line = " " + line
            if buf:
                buf.append(line)
                joined.append((start, "".join(buf)))
                buf = []
            else:
                joined.append((no, line))
        else:
            if not buf:
                start = no
            buf.append(line.strip("\\"))
    if buf:
        joined.append((start, "".join(buf)))
    out = []
    for no, line in joined:
        line = _COMMENT.sub("", line).strip()
        if line:
            out.append((no, line))
    return out


def _split_options(line: str):
    """(requirement, option tokens) the way pip's break_args_options splits a line: on single
    spaces, at the first token that starts with '-'; the options are then shell-split."""
    tokens = line.split(" ")
    cut = next((i for i, tok in enumerate(tokens) if tok.startswith("-")), len(tokens))
    return " ".join(tokens[:cut]), shlex.split(" ".join(tokens[cut:]))


def parse_lock(text: str) -> list:
    """Every entry of a hash-pinned lock, in file order. Raises ValueError on a line that is not
    a pinned requirement with sha256 hashes (an index option, a URL, a range) and on a layout
    pip would refuse (a requirement packaging cannot parse, a malformed hash)."""
    entries = []
    for no, line in logical_lines(text):
        text_req, options = _split_options(line)
        if not text_req.strip():
            raise ValueError(f"line {no}: an option line has no place in the lock: {line[:80]}")
        hashes = []
        values = iter(options)
        for tok in values:
            if tok == "--hash":
                tok = "--hash=" + next(values, "")
            m = _HASH.fullmatch(tok)
            if not m:
                raise ValueError(f"line {no}: unexpected option {tok[:80]}")
            hashes.append(m.group(1))
        req = Requirement(text_req)
        specs = list(req.specifier)
        if req.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise ValueError(f"line {no}: not pinned with ==: {text_req[:80]}")
        if not hashes:
            raise ValueError(f"line {no}: {req.name} carries no hash")
        entries.append(Entry(canonicalize_name(req.name), specs[0].version, frozenset(req.extras),
                             req.marker, frozenset(hashes), no, text_req))
    return entries


def top_level(path: Path = REQUIREMENTS) -> list:
    """The requirements of a requirements file, following ``-r`` relative to the including file."""
    out = []
    for _no, line in logical_lines(path.read_text(encoding="utf-8")):
        parts = line.split(None, 1)
        if parts[0] in ("-r", "--requirement") and len(parts) == 2:
            out += top_level(path.parent / parts[1].strip())
        elif parts[0].startswith("--requirement="):
            out += top_level(path.parent / parts[0].split("=", 1)[1])
        elif not line.startswith("-"):
            out.append(Requirement(line))
    return out


# ---------------------------------------------------------------------------------------------
# Targets: the CI matrices, and what pip on each runner image installs.

# setup-python installs the newest patch release of a minor version, so a marker on a patch
# level (``python_full_version < '3.11.3'``) is evaluated as for a release past every such
# boundary.
_PATCH = 99
# The runner images (actions/runner-images README, read 2026-11): ubuntu-latest is Ubuntu
# 24.04 (glibc 2.39), macos-latest macOS 26 on arm64 (Darwin 25), windows-latest Windows
# Server 2025 on x64. The Darwin release is what platform_release reads on macOS.
_RUNNERS = {
    "ubuntu-latest": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
                      "platform_machine": "x86_64", "platform_release": "6.8.0"},
    "macos-latest": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix",
                     "platform_machine": "arm64", "platform_release": "25.0.0"},
    "windows-latest": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
                       "platform_machine": "AMD64", "platform_release": "10"},
}
_GLIBC = (2, 39)
# The two macOS versions pip can see on macos-latest. macOS 26 reports itself as 16.0 to a
# program built against an older SDK, and pip's tag code maps only the older 10.16
# compatibility value back to the real version, so pip under such a Python accepts wheels up
# to macosx_16_0 and under a Python built against the new SDK up to macosx_26_0. Which build
# setup-python serves for each Python is not measured from here, so the checker takes both
# views: a release must have a wheel under each (one for 17 to 26 only is reported), and the
# requirements of the wheel each view picks are checked (a macosx_26_0 wheel that pip prefers
# over a pure one cannot bring an unlocked requirement past the check).
_MACOS_VIEWS = ((16, 0), (26, 0))
# PEP 600: the legacy manylinux names and the glibc each one stands for.
_LEGACY_MANYLINUX = {(2, 17): "manylinux2014", (2, 12): "manylinux2010", (2, 5): "manylinux1"}


def _platforms(runner: str, view: int = 0) -> list:
    """The platform tags pip accepts on ``runner``, under the ``view``-th way it can see the
    machine (only macOS has two, _MACOS_VIEWS)."""
    if runner == "ubuntu-latest":
        out = []
        for minor in range(_GLIBC[1], 4, -1):
            out.append(f"manylinux_2_{minor}_x86_64")
            if (2, minor) in _LEGACY_MANYLINUX:
                out.append(f"{_LEGACY_MANYLINUX[(2, minor)]}_x86_64")
        return out + ["linux_x86_64"]
    if runner == "macos-latest":
        return list(tags.mac_platforms(_MACOS_VIEWS[view], "arm64"))
    if runner == "windows-latest":
        return ["win_amd64"]
    raise ValueError(f"no platform model for runner {runner!r}: add it to _RUNNERS")


@dataclass(frozen=True)
class Target:
    runner: str
    python: tuple  # (major, minor)

    @property
    def label(self) -> str:
        return f"{self.runner} {self.python[0]}.{self.python[1]}"

    def env(self) -> dict:
        """A complete marker environment: every key is set, so nothing of the host leaks in
        (Marker.evaluate fills missing keys from the running interpreter)."""
        full = f"{self.python[0]}.{self.python[1]}.{_PATCH}"
        return {
            **{k: v for k, v in _RUNNERS[self.runner].items()},
            "implementation_name": "cpython", "implementation_version": full,
            "platform_python_implementation": "CPython", "platform_version": "",
            "python_version": f"{self.python[0]}.{self.python[1]}", "python_full_version": full,
        }

    def tag_priority(self, view: int = 0) -> dict:
        """{wheel tag: rank} in pip's order of preference for this target (0 is best), under
        one of the ways pip can see the machine."""
        interp = f"cp{self.python[0]}{self.python[1]}"
        plats = _platforms(self.runner, view)
        order = list(tags.cpython_tags(self.python, [interp], plats))
        order += list(tags.compatible_tags(self.python, interp, plats))
        rank: dict = {}
        for i, tag in enumerate(order):
            rank.setdefault(tag, i)
        return rank

    def tag_views(self) -> list:
        """One tag_priority per way pip can see the machine: two on macOS, one elsewhere."""
        count = len(_MACOS_VIEWS) if self.runner == "macos-latest" else 1
        return [self.tag_priority(view) for view in range(count)]


def matrix_targets(workflows=WORKFLOWS) -> list:
    """Every (runner, Python) pair of the CI matrices, in first-seen order."""
    seen = []
    for wf in workflows:
        src = Path(wf).read_text(encoding="utf-8")
        for m in re.finditer(r"matrix:\s*'(\{.*?\})'", src, re.S):
            for combo in json.loads(m.group(1)).get("include", []):
                major, minor = (int(p) for p in str(combo["python-version"]).split(".")[:2])
                target = Target(combo["os"], (major, minor))
                _platforms(target.runner)  # an unknown runner fails here, not silently later
                if target not in seen:
                    seen.append(target)
    if not seen:
        raise ValueError("no matrix found in the CI workflows")
    return seen


# ---------------------------------------------------------------------------------------------
# Metadata source. Tests hand in their own object with the same four methods.

# lock-sync is a required check and reads PyPI about 1500 times: one 5xx or timeout among them
# must not block a push. A 4xx (a release that is not there) is an answer, not a hiccup.
_RETRIES = 3
_BACKOFF_S = 2.0


def with_retries(read, *args, sleep=time.sleep):
    """``read(*args)``, tried up to _RETRIES times on a network error or a 5xx answer."""
    for attempt in range(1, _RETRIES + 1):
        try:
            return read(*args)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == _RETRIES:
                raise
        except (OSError, http.client.HTTPException):
            if attempt == _RETRIES:
                raise
        sleep(_BACKOFF_S * attempt)
    raise AssertionError("unreachable")


class PyPI:
    """Reads release files, digests and per-wheel core metadata from pypi.org (cached)."""

    def __init__(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from check_vllm_lock import _get_json, _get_text
        self._get_json = lambda *args: with_retries(_get_json, *args)
        self._get_text = lambda url: with_retries(_get_text, url)
        self._simple: dict = {}
        self._release: dict = {}
        self._meta: dict = {}

    def _simple_files(self, name: str) -> list:
        if name not in self._simple:
            self._simple[name] = self._get_json(f"https://pypi.org/simple/{name}/",
                                                "application/vnd.pypi.simple.v1+json")["files"]
        return self._simple[name]

    def _release_json(self, name: str, version: str) -> dict:
        key = (name, version)
        if key not in self._release:
            self._release[key] = self._get_json(f"https://pypi.org/pypi/{name}/{version}/json")
        return self._release[key]

    def files(self, name: str, version: str) -> list:
        """[{filename, url, requires-python, core-metadata}] of one release."""
        want = Version(version)
        out = []
        for f in self._simple_files(name):
            fn = f["filename"]
            try:
                if fn.endswith(".whl"):
                    if parse_wheel_filename(fn)[1] != want:
                        continue
                elif not _sdist_version_matches(fn, name, want):
                    continue
            except Exception:
                continue
            out.append({"filename": fn, "url": f["url"], "requires-python": f.get("requires-python"),
                        "core-metadata": bool(f.get("core-metadata") or f.get("data-dist-info-metadata"))})
        return out

    def hashes(self, name: str, version: str) -> dict:
        """{sha256: filename} of every file PyPI publishes for the release."""
        return {f["digests"]["sha256"]: f["filename"] for f in self._release_json(name, version)["urls"]}

    def requires_dist(self, file: dict):
        """The Requires-Dist lines of a wheel's PEP 658 metadata, or None without it."""
        if not file.get("core-metadata"):
            return None
        url = file["url"] + ".metadata"
        if url not in self._meta:
            msg = email.parser.Parser().parsestr(self._get_text(url))
            self._meta[url] = list(msg.get_all("Requires-Dist") or [])
        return self._meta[url]

    def sdist_requires(self, name: str, version: str) -> list:
        return list(self._release_json(name, version)["info"].get("requires_dist") or [])


def _sdist_version_matches(filename: str, name: str, version: Version) -> bool:
    stem = filename
    for ext in (".tar.gz", ".zip", ".tar.bz2"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    else:
        return False
    head, _, tail = stem.rpartition("-")
    try:
        return canonicalize_name(head) == canonicalize_name(name) and Version(tail) == version
    except Exception:
        return False


def _is_sdist(filename: str) -> bool:
    return filename.endswith((".tar.gz", ".zip", ".tar.bz2"))


def _python_ok(requires_python, target: Target) -> bool:
    if not requires_python:
        return True
    try:
        return SpecifierSet(requires_python).contains(target.env()["python_full_version"], prereleases=True)
    except Exception:
        return True


def pick(files: list, target: Target, rank: dict | None = None):
    """(the file pip installs on ``target``, None) or (None, why). An sdist is picked only when
    no wheel fits. ``rank``: one of target.tag_views() (default: the first)."""
    rank = target.tag_priority() if rank is None else rank
    best = None
    excluded_by_python = []
    for f in files:
        if not f["filename"].endswith(".whl"):
            continue
        try:
            wheel_tags = parse_wheel_filename(f["filename"])[3]
        except Exception:
            continue
        ranks = [rank[t] for t in wheel_tags if t in rank]
        if not ranks:
            continue
        if not _python_ok(f.get("requires-python"), target):
            excluded_by_python.append(f["filename"])
            continue
        if best is None or min(ranks) < best[0]:
            best = (min(ranks), f)
    if best:
        return best[1], None
    sdists = [f for f in files if _is_sdist(f["filename"])]
    for f in sdists:
        if _python_ok(f.get("requires-python"), target):
            return f, None
        excluded_by_python.append(f["filename"])
    if excluded_by_python:
        return None, f"requires another Python ({excluded_by_python[0]})"
    return None, "no wheel or sdist installs there"


def _applies(req: Requirement, env: dict, extra: str = "") -> bool:
    return req.marker is None or req.marker.evaluate({**env, "extra": extra})


def check(entries: list, requirements: list, targets: list, source, workers: int = 6):
    """(problems, notes) of the lock for every target. ``source`` provides files, hashes,
    requires_dist and sdist_requires (PyPI, or a test's stand-in)."""
    problems: set = set()
    sdist_targets: dict = defaultdict(list)
    releases = sorted({(e.name, e.version) for e in entries})

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        files = dict(zip(releases, pool.map(lambda r: source.files(*r), releases)))
        published = dict(zip(releases, pool.map(lambda r: source.hashes(*r), releases)))

    for e in entries:
        missing = sorted(fn for h, fn in published[(e.name, e.version)].items() if h not in e.hashes)
        if missing:
            problems.add(f"{e} (line {e.line}): no hash in the lock for {missing[:3]}")
        # The other direction too. uv keeps the hashes the committed lock already has for a pin
        # it keeps (measured: a version edited by hand came back with the old release's
        # hashes), so regenerating and diffing cannot see a hand-edited hash; only this can.
        foreign = sorted(e.hashes - set(published[(e.name, e.version)]))
        if foreign:
            problems.add(f"{e} (line {e.line}): {len(foreign)} hash(es) in the lock that PyPI does "
                         f"not publish for this release ({foreign[0][:12]}...)")

    plans = []  # (target, {name: entry}, {name: [the file each view picks, in view order]})
    wanted_files = {}
    for target in targets:
        env = target.env()
        views = target.tag_views()
        chosen: dict = {}
        for e in entries:
            if not e.applies(env):
                continue
            if e.name in chosen:
                problems.add(f"[{target.label}] {e.name}: two entries apply at once "
                             f"(lines {chosen[e.name].line} and {e.line})")
                continue
            chosen[e.name] = e
        picked = {}
        for name, e in chosen.items():
            # Installable under every view, and each view's wheel is checked below.
            results = [pick(files[(e.name, e.version)], target, rank) for rank in views]
            why = next((w for f, w in results if f is None), None)
            if why is not None:
                problems.add(f"[{target.label}] {e}: {why}")
                continue
            picked[name] = [f for f, _ in results]
            for f in {f["filename"]: f for f in picked[name]}.values():
                if _is_sdist(f["filename"]):
                    if target.label not in sdist_targets[str(e)]:
                        sdist_targets[str(e)].append(target.label)
                else:
                    wanted_files[f["url"]] = f
        plans.append((target, chosen, picked))

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        meta = dict(zip(wanted_files, pool.map(source.requires_dist, wanted_files.values())))

    for target, chosen, picked in plans:
        env = target.env()
        # Each view is an install of its own, resolved on its own: an extra that one view's
        # wheel asks for must not reach a wheel only the other view picks (a merged graph
        # demands requirements neither real install has). Downloads are shared above, and the
        # problems set folds what both views report alike.
        for view in range(len(target.tag_views())):
            deps_of = {}
            for name, picked_files in picked.items():
                f = picked_files[view]
                if _is_sdist(f["filename"]):
                    lines = source.sdist_requires(chosen[name].name, chosen[name].version)
                elif meta[f["url"]] is None:
                    problems.add(f"[{target.label}] {chosen[name]}: {f['filename']} has no PEP 658 "
                                 "metadata to check")
                    lines = []
                else:
                    lines = meta[f["url"]]
                deps_of[name] = [Requirement(line) for line in dict.fromkeys(lines)]
            for who, req in _needed(chosen, deps_of, requirements, env):
                dep = canonicalize_name(req.name)
                have = chosen.get(dep)
                if have is None:
                    problems.add(f"[{target.label}] {who} needs {req}: not in the lock for this target")
                elif not req.specifier.contains(have.version, prereleases=True):
                    problems.add(f"[{target.label}] {who} needs {req}, the lock has {have.version}")
    notes = [f"{release}: no wheel, built from its sdist on install "
             f"({len(labels)} of {len(targets)} targets)" for release, labels in sorted(sdist_targets.items())]
    return sorted(problems), notes


def _needed(chosen: dict, deps_of: dict, requirements: list, env: dict) -> list:
    """(who, requirement) for every requirement that applies on the target: requirements.txt's,
    then those of every applying entry, with the extras the tree asks for (repeated until no
    new extra appears)."""
    extras: dict = defaultdict(set)
    top = []
    for req in requirements:
        if _applies(req, env):
            top.append(("requirements.txt", req))
            extras[canonicalize_name(req.name)] |= {canonicalize_name(x) for x in req.extras}
    while True:
        before = sum(len(v) for v in extras.values())
        pairs = list(top)
        for name, reqs in deps_of.items():
            who = str(chosen[name])
            for extra in [""] + sorted(extras[name]):
                for req in reqs:
                    if _applies(req, env, extra):
                        pairs.append((who, req))
                        extras[canonicalize_name(req.name)] |= {canonicalize_name(x) for x in req.extras}
        if sum(len(v) for v in extras.values()) == before:
            break
    unique = {}
    for who, req in pairs:
        unique.setdefault((who, str(req)), (who, req))
    return list(unique.values())


# ---------------------------------------------------------------------------------------------
# Drift: the running interpreter against the lock.

def installed_distributions() -> dict:
    """{canonical name: version} of the running interpreter; the first one on sys.path wins, as
    for an import."""
    out = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            out.setdefault(canonicalize_name(name), dist.version)
    return out


def drift(entries: list, installed: dict, env: dict) -> list:
    """[(name, locked, installed)] for every difference; '-' marks an absent side."""
    applying: dict = {}
    rows = []
    for e in entries:
        if not e.applies(env):
            continue
        if e.name in applying:
            rows.append((e.name, f"{applying[e.name].version} and {e.version} (two entries apply)",
                         installed.get(e.name, "-")))
            continue
        applying[e.name] = e
    for name, e in applying.items():
        have = installed.get(name)
        if have is None:
            rows.append((name, e.version, "-"))
        elif Version(have) != Version(e.version):
            rows.append((name, e.version, have))
    for name, have in installed.items():
        if name not in applying and name not in _UNLOCKED_TOOLS:
            rows.append((name, "-", have))
    return sorted(rows)


def _drift_main(lock: Path, report: bool) -> int:
    env = default_environment()
    rows = drift(parse_lock(lock.read_bytes().decode("utf-8")), installed_distributions(), env)
    where = f"Python {env['python_full_version']} on {env['sys_platform']}"
    if report:
        print(f"### Installed versions against requirements.lock ({where})")
        print()
        if not rows:
            print("Every installed version is the locked one.")
            return 0
        print(f"{len(rows)} difference(s): a red run here is most likely one of these releases.")
        print()
        print("| Package | Locked | Installed |")
        print("|---|---|---|")
        for name, locked, have in rows:
            print(f"| {name} | {locked} | {have} |")
        return 0
    for name, locked, have in rows:
        print(f"{name}: locked {locked}, installed {have}")
    print(f"{len(rows)} difference(s) between requirements.lock and {where}.")
    if any(locked == "-" for _name, locked, _have in rows):
        print("A package marked 'locked -' is installed but not in the lock. If the interpreter "
              "itself ships it (a CI runner's Python, not anything this repository installs), "
              "add its name to _UNLOCKED_TOOLS in scripts/check_requirements_lock.py; "
              "otherwise something installed past the lock.")
    return 1 if rows else 0


_USAGE = ("usage: check_requirements_lock.py [--requirements FILE] [path/to/a.lock]\n"
          "       check_requirements_lock.py --drift [--report] [path/to/requirements.lock]")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    requirements = REQUIREMENTS
    if "--requirements" in argv:
        i = argv.index("--requirements")
        if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
            print(_USAGE, file=sys.stderr)
            return 2
        requirements = Path(argv[i + 1])
        del argv[i:i + 2]
    flags = {a for a in argv if a.startswith("--")}
    unknown = flags - {"--drift", "--report"}
    if unknown or ("--report" in flags and "--drift" not in flags):
        print(_USAGE, file=sys.stderr)
        return 2
    paths = [a for a in argv if not a.startswith("--")]
    lock = Path(paths[0]) if paths else LOCK
    if "--drift" in flags:
        return _drift_main(lock, "--report" in flags)
    entries = parse_lock(lock.read_bytes().decode("utf-8"))
    targets = matrix_targets()
    problems, notes = check(entries, top_level(requirements), targets, PyPI())
    for line in notes:
        print(f"note: {line}")
    for line in problems:
        print(line)
    print(f"{len(entries)} entries ({len({(e.name, e.version) for e in entries})} releases) checked "
          f"for {len(targets)} targets ({', '.join(t.label for t in targets)}): "
          f"{len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
