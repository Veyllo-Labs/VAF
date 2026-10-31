# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Check vaf/core/vllm_requirements.lock for every machine the vLLM verdict calls supported.

pip-compile resolves for the interpreter and platform it runs on, but the managed vLLM
environment installs this one lock with ``--require-hashes --no-deps --only-binary=:all:`` on
Linux x86_64 and aarch64 with Python 3.11 to 3.14 (``local_engine.PYTHON_RANGE``). For each
of those targets, at the oldest glibc the verdict accepts (``local_engine.MIN_GLIBC``; a wheel
is counted only when pip would install it there), this script reads, from PyPI, the core metadata (PEP 658) of the very wheel
pip would pick for every pinned release, evaluates its requirements' markers for that target
and checks that each requirement is in the lock, at a version that satisfies it, with the
extras it asks for. It also checks that the lock carries the hash of every file PyPI
publishes for each pinned release.

Measured when the lock was introduced: a resolution on Python 3.13 alone is untrue for 3.11
and 3.12 (mistral-common requires numpy<2.4 there), which is why vllm_requirements.in caps
numpy. Run it after every regeneration (docs/setup/RELEASING.md); it needs the network and
the ``packaging`` library, nothing is downloaded but metadata.

    venv/bin/python scripts/check_vllm_lock.py [path/to/a.lock]
"""
from __future__ import annotations

import concurrent.futures
import email.parser
import json
import re
import sys
import urllib.request
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "vaf" / "core" / "vllm_requirements.lock"
ARCHES = ("x86_64", "aarch64")
_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([^\]]*)\])?==(\S+)", re.M)


def parse_lock(text: str):
    """{canonical name: (version, extras, hashes)} of every pinned entry."""
    entries = {}
    starts = list(_ENTRY.finditer(text))
    for i, m in enumerate(starts):
        block = text[m.start():starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        extras = {x.strip() for x in (m.group(2) or "").split(",") if x.strip()}
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block))
        entries[canonicalize_name(m.group(1))] = (m.group(3), extras, hashes)
    return entries


def _targets():
    """(arch, python major, python minor, glibc floor): the machines the support verdict calls
    supported, at the oldest glibc it accepts (local_engine.MIN_GLIBC)."""
    sys.path.insert(0, str(ROOT))
    from vaf.core.local_engine import MIN_GLIBC, PYTHON_RANGE
    (major, low), (_, high) = PYTHON_RANGE
    return [(arch, major, minor, MIN_GLIBC[arch]) for arch in ARCHES for minor in range(low, high)]


# The legacy manylinux names and the glibc each one stands for (PEP 600).
_LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}


def _platform_fits(platform: str, arch: str, glibc) -> bool:
    """True for a platform tag pip installs on Linux ``arch`` with glibc ``glibc``: ``any`` or
    a manylinux tag at or below that glibc. musllinux and a bare ``linux_*`` tag never fit (pip
    installs neither from PyPI on a glibc system)."""
    if platform == "any":
        return True
    if not platform.endswith("_" + arch):
        return False
    stem = platform[:-len(arch) - 1]
    if stem in _LEGACY_MANYLINUX:
        return _LEGACY_MANYLINUX[stem] <= tuple(glibc)
    m = re.fullmatch(r"manylinux_(\d+)_(\d+)", stem)
    return bool(m) and (int(m.group(1)), int(m.group(2))) <= tuple(glibc)


def _get_json(url: str, accept: str = "application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _fits(filename: str, arch: str, major: int, minor: int, glibc) -> bool:
    try:
        _, _, _, tags = parse_wheel_filename(filename)
    except Exception:
        return False
    cp = f"cp{major}{minor}"
    for t in tags:
        if not _platform_fits(t.platform, arch, glibc):
            continue
        if t.abi == "none" and (t.interpreter.startswith(f"py{major}") or t.interpreter == cp):
            return True
        if t.interpreter == cp and t.abi == cp:
            return True
        if t.abi == "abi3" and t.interpreter.startswith(f"cp{major}"):
            if int(t.interpreter[len(f"cp{major}"):] or 0) <= minor:
                return True
    return False


def _check(name, entry, entries, targets, meta_cache):
    version, extras, hashes = entry
    out = []
    simple = _get_json(f"https://pypi.org/simple/{name}/", "application/vnd.pypi.simple.v1+json")
    files = []
    for f in simple["files"]:
        try:
            if f["filename"].endswith(".whl") and parse_wheel_filename(f["filename"])[1] == Version(version):
                files.append(f)
        except Exception:
            continue
    release = _get_json(f"https://pypi.org/pypi/{name}/{version}/json")
    published = {f["digests"]["sha256"]: f["filename"] for f in release["urls"]}
    missing_hashes = sorted(fn for h, fn in published.items() if h not in hashes)
    if missing_hashes:
        out.append(f"{name}=={version}: no hash in the lock for {missing_hashes[:3]}")
    for arch, major, minor, glibc in targets:
        where = f"{arch} cp{major}{minor} glibc {glibc[0]}.{glibc[1]}"
        wheels = [f for f in files if _fits(f["filename"], arch, major, minor, glibc)]
        if not wheels:
            out.append(f"{name}=={version}: no wheel for {where}")
            continue
        env = {"python_version": f"{major}.{minor}", "python_full_version": f"{major}.{minor}.0",
               "platform_machine": arch, "platform_system": "Linux", "sys_platform": "linux",
               "os_name": "posix", "implementation_name": "cpython",
               "platform_python_implementation": "CPython"}
        for f in wheels:
            if not (f.get("core-metadata") or f.get("data-dist-info-metadata")):
                out.append(f"{name}: {f['filename']} has no PEP 658 metadata to check")
                continue
            url = f["url"] + ".metadata"
            if url not in meta_cache:
                with urllib.request.urlopen(url, timeout=60) as r:
                    meta_cache[url] = email.parser.Parser().parsestr(
                        r.read().decode("utf-8", "replace"))
            for line in meta_cache[url].get_all("Requires-Dist") or []:
                req = Requirement(line)
                if req.marker and not any(req.marker.evaluate({**env, "extra": x})
                                          for x in [""] + sorted(extras)):
                    continue
                dep = canonicalize_name(req.name)
                if dep not in entries:
                    out.append(f"{name}=={version} [{where}] needs {line}: not in the lock")
                    continue
                if not req.specifier.contains(entries[dep][0], prereleases=True):
                    out.append(f"{name}=={version} [{where}] needs {line}, the lock has "
                               f"{entries[dep][0]}")
                for extra in req.extras - entries[dep][1]:
                    out.append(f"{name}=={version} [{where}] needs {dep}[{extra}]: the lock "
                               "does not install that extra")
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    lock = Path(argv[0]) if argv else LOCK
    entries = parse_lock(lock.read_bytes().decode("utf-8"))
    targets = _targets()
    meta_cache: dict = {}
    problems = []
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        futures = [pool.submit(_check, n, e, entries, targets, meta_cache) for n, e in entries.items()]
        for fut in futures:
            problems += fut.result()
    for line in sorted(set(problems)):
        print(line)
    names = ", ".join(f"{a} cp{ma}{mi} glibc {g[0]}.{g[1]}" for a, ma, mi, g in targets)
    print(f"{len(entries)} pinned releases checked for {names}: "
          f"{len(set(problems))} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
