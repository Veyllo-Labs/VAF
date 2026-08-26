# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser image age gate, tested without docker.

The class this pins: the browser image is built from an UNPINNED Debian
Chromium, and compose's cached `--build` never re-runs that apt layer, so the
engine ages silently forever unless something forces a cache-less, base-pulling
rebuild. The gate is that something; these tests pin its decisions (when it
fires, how it derives the build command from BOTH compose variants, and that a
failure is an event rather than a blocked start).
"""
import types

import vaf.core.service_stack as ss


def _proc(code=0, out="", err=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


class _FakeRun:
    """Scriptable subprocess.run for the docker calls the gate makes."""

    def __init__(self, image="vaf-vaf-browser", created="2026-08-01T00:00:00.000000000Z",
                 build_rc=0):
        self.calls = []
        self.image = image
        self.created = created
        self.build_rc = build_rc

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if "inspect" in cmd and "{{.Config.Image}}" in cmd[-1]:
            return _proc(0, self.image + "\n") if self.image else _proc(1, "", "no such container")
        if "image" in cmd and "inspect" in cmd:
            return _proc(0, self.created + "\n") if self.created else _proc(1, "", "no such image")
        if "build" in cmd:
            return _proc(self.build_rc, "", "boom" if self.build_rc else "")
        return _proc(0)


def test_age_is_read_through_the_container_not_a_hardcoded_image_name(monkeypatch):
    """The compose service has no image: key, so the built name is
    project-dependent (v2 dash, legacy underscore) - the pinned container name
    is the only stable route to it."""
    fake = _FakeRun()
    monkeypatch.setattr(ss.subprocess, "run", fake)
    age = ss._browser_image_age_days()
    assert age is not None and age > 0
    first = fake.calls[0]
    assert "vaf-browser" in first and "{{.Config.Image}}" in first[-1]
    second = fake.calls[1]
    assert second[1] == "image" and "vaf-vaf-browser" in second


def test_a_missing_container_answers_none_and_the_gate_stands_down(monkeypatch):
    fake = _FakeRun(image="")
    monkeypatch.setattr(ss.subprocess, "run", fake)
    assert ss._browser_image_age_days() is None
    calls = []
    monkeypatch.setattr(ss.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _proc(0))
    monkeypatch.setattr(ss, "_browser_image_age_days", lambda: None)
    ss._maybe_rebuild_stale_browser_image(["docker", "compose", "up", "-d"], {})
    assert calls == []


def test_a_stale_image_derives_the_build_command_from_both_compose_variants(monkeypatch):
    """`base` already carries `up -d ...`; appending would produce
    `up -d build ...`, an invalid command - the gate must cut at "up"."""
    monkeypatch.setattr(ss, "_browser_image_age_days", lambda: 30.0)
    monkeypatch.setattr(ss, "_browser_image_max_age_days", lambda: 14)
    for base, prefix in (
        (["docker", "compose", "-f", "docker-compose.memory.yml", "up", "-d", "--quiet-pull"],
         ["docker", "compose", "-f", "docker-compose.memory.yml"]),
        (["docker-compose", "-f", "docker-compose.memory.yml", "up", "-d"],
         ["docker-compose", "-f", "docker-compose.memory.yml"]),
    ):
        calls = []
        monkeypatch.setattr(ss.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)) or _proc(0))
        ss._maybe_rebuild_stale_browser_image(list(base), {})
        assert calls == [prefix + ["build", "--pull", "--no-cache", "vaf-browser"]]


def test_a_fresh_image_is_left_alone(monkeypatch):
    monkeypatch.setattr(ss, "_browser_image_age_days", lambda: 3.0)
    monkeypatch.setattr(ss, "_browser_image_max_age_days", lambda: 14)
    calls = []
    monkeypatch.setattr(ss.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _proc(0))
    ss._maybe_rebuild_stale_browser_image(["docker", "compose", "up", "-d"], {})
    assert calls == []


def test_a_zero_budget_disables_the_gate(monkeypatch):
    monkeypatch.setattr(ss, "_browser_image_age_days", lambda: 400.0)
    monkeypatch.setattr(ss, "_browser_image_max_age_days", lambda: 0)
    calls = []
    monkeypatch.setattr(ss.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _proc(0))
    ss._maybe_rebuild_stale_browser_image(["docker", "compose", "up", "-d"], {})
    assert calls == []


def test_a_failed_fresh_build_is_an_event_never_a_blocked_start(monkeypatch):
    monkeypatch.setattr(ss, "_browser_image_age_days", lambda: 30.0)
    monkeypatch.setattr(ss, "_browser_image_max_age_days", lambda: 14)
    monkeypatch.setattr(ss.subprocess, "run", lambda cmd, **kw: _proc(1, "", "apt mirror down"))
    events = []
    import vaf.core.security_events as sev
    monkeypatch.setattr(sev, "log_security_event", lambda kind, **kw: events.append((kind, kw)))
    ss._maybe_rebuild_stale_browser_image(["docker", "compose", "up", "-d"], {})
    assert [k for k, _ in events] == ["browser_image_stale"]
    assert "apt mirror down" in events[0][1]["detail"]


def test_the_budget_reads_env_first_then_config(monkeypatch):
    monkeypatch.delenv("VAF_BROWSER_IMAGE_MAX_AGE_DAYS", raising=False)
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: 21))
    assert ss._browser_image_max_age_days() == 21
    monkeypatch.setenv("VAF_BROWSER_IMAGE_MAX_AGE_DAYS", "7")
    assert ss._browser_image_max_age_days() == 7


def test_dashboard_derivation_is_honest_about_the_unknown(monkeypatch):
    from vaf.api.security_routes import derive_browser_engine
    assert derive_browser_engine(None, 14, "") is None
    fresh = derive_browser_engine(3.2, 14, "Chrome/151.0.7922.137")
    assert fresh == {"age_days": 3.2, "budget_days": 14, "stale": False,
                     "browser_version": "Chrome/151.0.7922.137"}
    stale = derive_browser_engine(30.0, 14, "")
    assert stale is not None and stale["stale"] is True
    unbudgeted = derive_browser_engine(400.0, 0, "")
    assert unbudgeted is not None and unbudgeted["stale"] is False
