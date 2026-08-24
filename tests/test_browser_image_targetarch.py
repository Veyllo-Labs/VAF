# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Guards for the architecture selection in the vaf-browser image (docker/browser/Dockerfile).

The regression this pins: `ARG TARGETARCH` used to carry `=amd64`. BuildKit fills the
predefined platform ARGs only when they declare no value of their own, so the default won and
every build on a non-amd64 host silently resolved to amd64. Measured on Apple Silicon with
BuildKit active: TARGETPLATFORM arrived as linux/arm64 and the in-image `dpkg --print-architecture`
reported arm64, while TARGETARCH stayed amd64. The build then downloaded the amd64 KasmVNC .deb
and apt refused it against the arm64 base with "Depends: libxext6:amd64 but it is not installable"
(exit 100).

The second, quieter half of the same defect was the catch-all branch: it did not only pick the
amd64 checksum, it also ASSIGNED `TARGETARCH=amd64`, so the wrong value was baked into the
download URL as well. An unknown or empty architecture must now stop the build instead of guessing.

Why it stayed invisible for so long: on an amd64 host the default happens to be correct, so
neither CI (ubuntu-latest and windows-latest are amd64) nor an amd64 developer machine can ever
observe it. Only an arm64 host can.

Pure text assertions on the Dockerfile - no Docker, no image build.
"""
import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "browser" / "Dockerfile"


def _code() -> str:
    # ignore comment lines: the fix is explained in comments that name the old value on purpose
    src = DOCKERFILE.read_text(encoding="utf-8")
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def test_targetarch_is_declared_without_a_default():
    decls = [ln.strip() for ln in _code().splitlines() if re.match(r"^ARG\s+TARGETARCH\b", ln.strip())]
    assert decls == ["ARG TARGETARCH"], (
        f"found {decls}: a default makes 'BuildKit did not set it' indistinguishable from a real "
        f"amd64 target, so every arm64 host builds an arm64 image around an amd64 KasmVNC package"
    )


def test_an_unknown_architecture_is_refused_not_silently_amd64():
    code = _code()
    # the catch-all must not re-introduce the assignment that baked amd64 into the download URL.
    # Two things are references rather than assignments and must not trip this: the value quoted
    # back in a diagnostic ("TARGETARCH='$TARGETARCH'") and the repair command the same diagnostic
    # prints ("--build-arg TARGETARCH=arm64"). Hence: a word character after the "=", and no echo
    # on the line. The branch this pins carried its assignment bare, next to KASM_SHA.
    assignments = [
        ln.strip()
        for ln in code.splitlines()
        if re.search(r"TARGETARCH=[A-Za-z0-9]", ln)
        and not ln.strip().startswith("ARG ")
        and "echo" not in ln
    ]
    assert not assignments, (
        f"TARGETARCH is assigned a literal in {assignments}; that was the half of the defect that "
        f"put the wrong architecture into the .deb URL"
    )
    # both the empty and the unsupported case must end the build rather than guess
    assert code.count("exit 1") >= 2, "an empty or unsupported TARGETARCH must fail the build loudly"


def test_every_architecture_the_case_handles_has_a_pinned_checksum():
    code = _code()
    arches = {m for m in re.findall(r"^\s*(\w+)\)\s*KASM_SHA=", code, re.M)}
    pinned = {m.lower() for m in re.findall(r"^ARG KASMVNC_SHA256_(\w+)=", code, re.M)}
    assert arches == {"amd64", "arm64"}, f"architectures handled: {arches}"
    assert arches <= pinned, f"an architecture is selected without a pinned checksum: {arches - pinned}"
    # the resolved value, not a literal, has to reach the download URL
    assert "${TARGETARCH}.deb" in code
