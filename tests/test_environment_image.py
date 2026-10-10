# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The sandbox environments' image: what goes into it, when it is built, and what
runs while it is missing. No test builds: the suite sets VAF_SANDBOX_ENV_NO_BUILD
session-wide, and the tests below drive the stubbed docker seam."""
import os
import re
import types
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from vaf.core import containers
from vaf.core import environment_image as ei


def _done(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# -- what goes into the image ----------------------------------------------------

def test_the_dockerfile_ships_inside_the_package_and_needs_no_context():
    """A wheel install has no docker/ directory, so the file lives in vaf/ and goes to
    `docker build -` on stdin. Without a build context a COPY or ADD cannot work."""
    import vaf
    package = Path(vaf.__file__).resolve().parent
    assert ei._DOCKERFILE.is_file() and package in ei._DOCKERFILE.parents
    assert not re.search(r"^\s*(COPY|ADD)\b", ei.dockerfile_text(), re.M)


def test_downloads_are_pinned_and_checked():
    text = ei.dockerfile_text()
    assert re.search(r"^ARG NODE_VERSION=\d+\.\d+\.\d+$", text, re.M)
    for arch in ("X64", "ARM64"):
        assert re.search(rf"^ARG NODE_SHA256_{arch}=[0-9a-f]{{64}}$", text, re.M), arch
    assert "sha256sum -c -" in text
    assert re.search(r"pytest==\d+\.\d+\.\d+", text)
    assert "FROM python:3.12-slim-bookworm" in text


def test_the_image_runs_as_its_own_user_with_the_tools_the_lanes_need():
    text = ei.dockerfile_text()
    assert re.search(r"^USER sandbox$", text, re.M)
    for tool in ("git", "tinyproxy", "chromium-headless-shell", "gcc"):
        assert tool in text, tool
    assert "safe.directory '*'" in text


# -- which image -----------------------------------------------------------------

def test_the_tag_follows_the_dockerfile(monkeypatch):
    first = ei.image_tag()
    assert re.fullmatch(r"vaf-sandbox-env:[0-9a-f]{12}", first)
    monkeypatch.setattr(ei, "dockerfile_text", lambda: "FROM scratch\n")
    assert ei.image_tag() != first


@pytest.fixture
def docker_seam(monkeypatch):
    """A scriptable docker: which tags exist, how old they are, what a build does."""
    state = types.SimpleNamespace(present=set(), created={}, build_rc=0, calls=[])
    monkeypatch.delenv("VAF_SANDBOX_ENV_NO_BUILD", raising=False)
    monkeypatch.setattr(ei, "_build_lock", lambda: None)

    def _docker(args, timeout=60, *, input=None, env=None):
        state.calls.append((list(args), input))
        if args[:2] == ["image", "inspect"]:
            tag = args[2]
            if tag not in state.present:
                return _done(1, "", "No such image")
            if "{{.Created}}" in args:
                return _done(0, state.created.get(tag, datetime.now(timezone.utc).isoformat()) + "\n")
            return _done(0, "sha256:abc\n")
        if args[:2] == ["image", "ls"]:
            return _done(0, "\n".join(sorted(state.present)) + "\n")
        if args[:2] == ["image", "rm"]:
            state.present.discard(args[2])
            return _done(0)
        if args[0] == "build":
            if state.build_rc == 0:
                state.present.add(args[args.index("-t") + 1])
            return _done(state.build_rc, "", "" if state.build_rc == 0 else "E: network down")
        raise AssertionError(args)

    monkeypatch.setattr(containers, "docker", _docker)
    return state


def _builds(state):
    return [args for args, _ in state.calls if args[0] == "build"]


def test_a_present_fresh_image_is_used_as_it_is(docker_seam):
    tag = ei.image_tag()
    docker_seam.present.add(tag)
    assert ei.ensure_image() == tag
    assert _builds(docker_seam) == []


def test_a_missing_image_is_built_from_stdin(docker_seam):
    """MUTATION: build with a context directory instead of `-` - red."""
    tag = ei.image_tag()
    assert ei.ensure_image() == tag
    (args, given), = [(a, i) for a, i in docker_seam.calls if a[0] == "build"]
    assert args == ["build", "-t", tag, "-"]
    assert given == ei.dockerfile_text()


def test_an_image_past_its_age_is_rebuilt_fresh(docker_seam, monkeypatch):
    tag = ei.image_tag()
    docker_seam.present.add(tag)
    docker_seam.created[tag] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    monkeypatch.setenv("VAF_SANDBOX_ENV_IMAGE_MAX_AGE_DAYS", "14")
    assert ei.ensure_image() == tag
    assert _builds(docker_seam) == [["build", "-t", tag, "--pull", "--no-cache", "-"]]

    docker_seam.calls.clear()
    monkeypatch.setenv("VAF_SANDBOX_ENV_IMAGE_MAX_AGE_DAYS", "0")      # 0 switches the age off
    docker_seam.created[tag] = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
    assert ei.ensure_image() == tag
    assert _builds(docker_seam) == []


def test_a_failed_build_keeps_an_older_copy_and_says_none_without_one(docker_seam, monkeypatch):
    tag = ei.image_tag()
    docker_seam.build_rc = 1
    assert ei.ensure_image() is None
    docker_seam.present.add(tag)
    docker_seam.created[tag] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    monkeypatch.setenv("VAF_SANDBOX_ENV_IMAGE_MAX_AGE_DAYS", "14")
    assert ei.ensure_image() == tag


def test_builds_are_refused_while_switched_off(docker_seam, monkeypatch):
    monkeypatch.setenv("VAF_SANDBOX_ENV_NO_BUILD", "1")
    assert ei.ensure_image() is None
    assert _builds(docker_seam) == []
    ei.start_background_build()
    assert _builds(docker_seam) == []


def test_the_age_default_matches_the_config_default():
    from vaf.core.config import Config
    assert Config.DEFAULTS["sandbox_env_image_max_age_days"] == ei.DEFAULT_MAX_AGE_DAYS


def test_the_suite_never_builds():
    """The session-wide fixture in conftest; without it a test reaching a build path
    would start a 1.6 GB build on the developer's machine."""
    assert os.environ.get("VAF_SANDBOX_ENV_NO_BUILD") == "1"


def test_a_successful_build_removes_the_images_of_earlier_dockerfiles(docker_seam):
    tag = ei.image_tag()
    docker_seam.present.update({"vaf-sandbox-env:000000000000", "vaf-sandbox-env:111111111111"})
    assert ei.ensure_image() == tag
    assert docker_seam.present == {tag}


def test_the_scratch_lane_falls_back_instead_of_waiting(docker_seam):
    """python_sandbox must never go dark behind a build or an offline machine."""
    assert ei.usable_image() == ei.FALLBACK_IMAGE
    assert _builds(docker_seam) == []
    docker_seam.present.add(ei.image_tag())
    assert ei.usable_image() == ei.image_tag()


# -- the age reader in containers ------------------------------------------------

def test_docker_timestamps_with_nanoseconds_parse():
    t = containers.parse_docker_time("2026-10-01T08:15:30.123456789Z")
    assert t == datetime(2026, 10, 1, 8, 15, 30, 123456, tzinfo=timezone.utc)
    assert containers.parse_docker_time("2026-10-01T08:15:30+02:00").utcoffset() == timedelta(hours=2)


def test_image_age_is_none_when_it_cannot_be_known(monkeypatch):
    monkeypatch.setattr(containers, "docker", lambda *a, **k: _done(1, "", "No such image"))
    assert containers.image_age_days("x") is None
    monkeypatch.setattr(containers, "docker", lambda *a, **k: _done(0, "not a time\n"))
    assert containers.image_age_days("x") is None
