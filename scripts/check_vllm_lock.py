# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Check vaf/core/vllm_requirements.lock for every machine the vLLM verdict calls supported.

pip-compile resolves for the interpreter and platform it runs on, but the managed vLLM
environment installs this one lock with ``--require-hashes --no-deps --only-binary=:all:``
on Linux x86_64 and aarch64 with Python 3.11 to 3.14 (``local_engine.PYTHON_RANGE``). For
each of those targets, at the oldest glibc the verdict accepts (``local_engine.MIN_GLIBC``;
a wheel is counted only when pip would install it there), this script reads, from PyPI, the
core metadata (PEP 658) of the very wheel pip would pick for every pinned release, evaluates
its requirements' markers for that target and checks that each requirement is in the lock,
at a version that satisfies it, with the extras it asks for. It also checks that the lock
carries the hash of every file PyPI publishes for each pinned release.

Measured when the lock was introduced: a resolution on Python 3.13 alone is untrue for 3.11
and 3.12 (mistral-common requires numpy<2.4 there), which is why vllm_requirements.in caps
numpy. Run it after every regeneration (docs/setup/RELEASING.md); it needs the network and
the ``packaging`` library, nothing is downloaded but metadata.

The ROCm lock (vllm_requirements_rocm.lock, selected by its file name or ``--rocm``) has one
target, x86_64 with Python 3.12 at ``local_engine.MIN_GLIBC_ROCM``. Its releases from vLLM's
ROCm index (``vllm_server.ROCM_INDEX``: every name that index lists) are read from there: the
files from its simple page, the core metadata by HTTP range reads of the wheel's
``*.dist-info/METADATA`` (that index publishes no PEP 658 metadata and no digests). Such a
release must carry exactly one hash, the one file the index serves; pip verifies it at install
time. The PyPI half is checked as above.

    venv/bin/python scripts/check_vllm_lock.py [--rocm] [path/to/a.lock]
"""
from __future__ import annotations

import concurrent.futures
import email.parser
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "vaf" / "core" / "vllm_requirements.lock"
LOCK_ROCM = ROOT / "vaf" / "core" / "vllm_requirements_rocm.lock"
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


def _targets(rocm: bool = False):
    """(arch, python major, python minor, glibc floor): the machines the support verdict calls
    supported, at the oldest glibc it accepts (local_engine.MIN_GLIBC). The ROCm build has
    one: x86_64, Python 3.12, local_engine.MIN_GLIBC_ROCM."""
    sys.path.insert(0, str(ROOT))
    from vaf.core.local_engine import MIN_GLIBC, MIN_GLIBC_ROCM, PYTHON_RANGE, ROCM_PYTHON
    if rocm:
        return [("x86_64", ROCM_PYTHON[0], ROCM_PYTHON[1], MIN_GLIBC_ROCM)]
    (major, low), (_, high) = PYTHON_RANGE
    return [(arch, major, minor, MIN_GLIBC[arch]) for arch in ARCHES for minor in range(low, high)]


def _rocm_index() -> str:
    sys.path.insert(0, str(ROOT))
    from vaf.core.vllm_server import ROCM_INDEX
    return ROCM_INDEX.rstrip("/") + "/"


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


_LINK = re.compile(r'<a href="([^"]+)"[^>]*>([^<]+)</a>')


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def index_names(index: str) -> set:
    """The canonical names a simple index lists on its root page."""
    return {canonicalize_name(text.strip().rstrip("/")) for _href, text in _LINK.findall(_get_text(index))}


def index_files(index: str, name: str) -> list:
    """[{filename, url}] of the wheels a simple index serves for ``name`` (HTML page)."""
    page = urllib.parse.urljoin(index, f"{name}/")
    return [{"filename": urllib.parse.unquote(text.strip()),
             "url": urllib.parse.urljoin(page, href.split("#", 1)[0])}
            for href, text in _LINK.findall(_get_text(page)) if text.strip().endswith(".whl")]


class _RangeFile(io.RawIOBase):
    """A remote file read by HTTP range requests, enough for zipfile to find one member
    without downloading the wheel (a torch wheel is 1.3 GB). Reads ahead 256 KiB."""

    CHUNK = 256 * 1024

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            self.size = int(r.headers["Content-Length"])
        self.pos = 0
        self._buf_start, self._buf = 0, b""

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = max(0, min(self.size, base + offset))
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        end = min(self.size, self.pos + n)
        if not (self._buf_start <= self.pos and end <= self._buf_start + len(self._buf)):
            stop = min(self.size, max(end, self.pos + self.CHUNK))
            req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{stop - 1}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.status != 206:
                    raise OSError(f"{self.url}: no range request ({r.status})")
                self._buf_start, self._buf = self.pos, r.read()
        start = self.pos - self._buf_start
        data = self._buf[start:start + (end - self.pos)]
        self.pos += len(data)
        return data


def remote_metadata(url: str) -> str:
    """The core metadata of a remote wheel, from ``<name>.dist-info/METADATA`` by range reads."""
    with zipfile.ZipFile(_RangeFile(url)) as wheel:
        member = next(n for n in wheel.namelist()
                      if n.count("/") == 1 and n.endswith(".dist-info/METADATA"))
        return wheel.read(member).decode("utf-8", "replace")


def _check(name, entry, entries, targets, meta_cache, index=None, wants=None):
    """The problems of one pinned release. ``index``: the ROCm index serves this release
    (its files and metadata are read from there), else PyPI. ``wants``: a set that collects
    (dependency, extra) instead of reporting an extra the lock does not name (see main)."""
    version, extras, hashes = entry
    out = []
    files = []
    if index:
        candidates = index_files(index, name)
        if len(hashes) != 1:
            # The index serves one file per release and publishes no digest; pip checks the
            # one hash at install time.
            out.append(f"{name}=={version}: {len(hashes)} hashes in the lock, the ROCm index "
                       "serves one file")
    else:
        simple = _get_json(f"https://pypi.org/simple/{name}/", "application/vnd.pypi.simple.v1+json")
        candidates = simple["files"]
        release = _get_json(f"https://pypi.org/pypi/{name}/{version}/json")
        published = {f["digests"]["sha256"]: f["filename"] for f in release["urls"]}
        missing_hashes = sorted(fn for h, fn in published.items() if h not in hashes)
        if missing_hashes:
            out.append(f"{name}=={version}: no hash in the lock for {missing_hashes[:3]}")
    for f in candidates:
        try:
            if f["filename"].endswith(".whl") and parse_wheel_filename(f["filename"])[1] == Version(version):
                files.append(f)
        except Exception:
            continue
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
            if index:
                url = f["url"]
                if url not in meta_cache:
                    meta_cache[url] = email.parser.Parser().parsestr(remote_metadata(url))
            else:
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
                    if wants is not None:
                        wants.add((dep, extra))
                        continue
                    out.append(f"{name}=={version} [{where}] needs {dep}[{extra}]: the lock "
                               "does not install that extra")
    return out


def is_rocm(argv, lock: Path) -> bool:
    """The ROCm mode: ``--rocm``, or a lock whose file name says rocm."""
    return "--rocm" in argv or "rocm" in lock.name


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    paths = [a for a in argv if not a.startswith("--")]
    rocm = "--rocm" in argv
    lock = Path(paths[0]) if paths else (LOCK_ROCM if rocm else LOCK)
    rocm = is_rocm(argv, lock)
    entries = parse_lock(lock.read_bytes().decode("utf-8"))
    targets = _targets(rocm)
    index = _rocm_index() if rocm else None
    served = index_names(index) if index else set()
    meta_cache: dict = {}
    local = [f"{name}=={version}: a local version that no index of the lock serves"
             for name, (version, _extras, _hashes) in entries.items()
             if "+" in version and name not in served]
    # uv writes no [extra] on a lock entry (pip-compile does), so in the ROCm mode the extras
    # the tree asks for are worked out instead: every extra a requirement names is added to
    # its package, and the check runs again until no new one appears; the requirements of
    # each such extra are then checked like any other (the metadata is cached).
    while True:
        wants: set = set()
        problems = list(local)
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            futures = [pool.submit(_check, n, e, entries, targets, meta_cache,
                                   index if n in served else None, wants if rocm else None)
                       for n, e in entries.items()]
            for fut in futures:
                problems += fut.result()
        if not wants:
            break
        for dep, extra in wants:
            version, extras, hashes = entries[dep]
            entries[dep] = (version, extras | {extra}, hashes)
    for line in sorted(set(problems)):
        print(line)
    names = ", ".join(f"{a} cp{ma}{mi} glibc {g[0]}.{g[1]}" for a, ma, mi, g in targets)
    print(f"{len(entries)} pinned releases checked for {names}: "
          f"{len(set(problems))} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
