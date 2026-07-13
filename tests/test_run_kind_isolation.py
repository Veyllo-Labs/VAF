# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""run_kind instance truth vs process-global env (incident 2026-07-13 07:01).

Env vars are shared across threads: while an automation run had VAF_IN_AUTOMATION
set, a concurrent thinking run's ask_user took the automation-HANDOFF branch and
snapshotted 82 messages of chat history (incl. third-party medical data) into a
handoff bundle - three times in three weeks, always in the 07:00 window. Decisions
that vary per run now key on the per-instance Agent._run_kind; env remains only a
fallback for constructors that do not pass the kwarg.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import vaf.core.agent as agent_mod
import vaf.core.handoff_bundle as hb
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


# ── ask_user branch: instance truth beats a concurrent automation's env ───────

def _run_ask_user(monkeypatch, tmp_path, agent, env_automation):
    _isolate(monkeypatch, tmp_path)
    if env_automation:
        monkeypatch.setenv("VAF_IN_AUTOMATION", "1")
    else:
        monkeypatch.delenv("VAF_IN_AUTOMATION", raising=False)
    calls = []
    from vaf.tools.ask_user import AskUserTool
    tool = AskUserTool()
    monkeypatch.setattr(tool, "_run_automation_handoff",
                        lambda **kw: calls.append("handoff") or "handoff")
    import vaf.core.thinking_mode as tm
    monkeypatch.setattr(tm, "deliver_tracked_message",
                        lambda *a, **kw: calls.append("tracked") or {"id": "r1", "delivered": True})
    tool.run(message="Frage?", _agent=agent, user_scope_id="s1", username="u")
    return calls


def test_thinking_agent_never_takes_handoff_despite_automation_env(monkeypatch, tmp_path):
    # THE incident: concurrent automation env must not flip a thinking question
    # into a handoff bundle.
    calls = _run_ask_user(monkeypatch, tmp_path,
                          SimpleNamespace(_run_kind="thinking", history=[]), env_automation=True)
    assert calls == ["tracked"]


def test_chat_agent_with_none_kind_never_takes_handoff(monkeypatch, tmp_path):
    calls = _run_ask_user(monkeypatch, tmp_path,
                          SimpleNamespace(_run_kind=None, history=[]), env_automation=True)
    assert calls == ["tracked"]


def test_automation_agent_takes_handoff_without_env(monkeypatch, tmp_path):
    # Inverse race: the automation's own env may already be popped by an
    # overlapping run finishing first - instance truth still hands off.
    calls = _run_ask_user(monkeypatch, tmp_path,
                          SimpleNamespace(_run_kind="automation", history=[]), env_automation=False)
    assert calls == ["handoff"]


def test_env_fallback_without_instance_kind(monkeypatch, tmp_path):
    # Foreign objects without the attribute (tests, embedders) keep env semantics.
    calls = _run_ask_user(monkeypatch, tmp_path,
                          SimpleNamespace(history=[]), env_automation=True)
    assert calls == ["handoff"]


# ── registration + dispatch use instance truth (source guards) ────────────────

def _load_tools_src():
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    m = re.search(r"def _load_tools\(self.*?(?=\n    def )", src, re.DOTALL)
    assert m, "_load_tools not found"
    return m.group(0)


def test_load_tools_registration_gates_use_instance_truth():
    body = _load_tools_src()
    assert "_rk_thinking" in body and "_rk_automation" in body
    assert 'os.environ.get("VAF_THINKING_MODE"' not in body, (
        "_load_tools regained an env-based registration gate (thread race)"
    )
    assert 'os.environ.get("VAF_IN_AUTOMATION"' not in body, (
        "_load_tools regained an env-based registration gate (thread race)"
    )


def test_dispatch_always_injects_agent_into_ask_user():
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    m = re.search(r'if name == "ask_user":.*?if name in \("update_intent"', src, re.DOTALL)
    assert m, "ask_user dispatch block not found"
    block = m.group(0)
    assert 'tool_args["_agent"] = self' in block
    assert "os.environ" not in block, "ask_user dispatch block reads env again"


def test_background_construction_sites_pass_run_kind():
    for mod_path, kind in (
        ("vaf/core/thinking_mode.py", "thinking"),
        ("vaf/core/automation.py", "automation"),
        ("vaf/core/gateway.py", "chat"),
        ("vaf/core/headless_runner.py", "chat"),
    ):
        src = Path("/mnt/veyllo1/VAF") .joinpath(mod_path).read_text(encoding="utf-8")
        assert f'run_kind="{kind}"' in src, f"{mod_path} lost its explicit run_kind"


# ── bundle data minimization ──────────────────────────────────────────────────

def test_sanitize_history_strips_image_payloads_and_caps():
    hist = [
        {"role": "user", "content": [
            {"type": "text", "text": "hier das foto"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA" + "A" * 5000}},
        ]},
        {"role": "assistant", "content": "x" * 10000},
    ]
    out = hb._sanitize_history(hist)
    assert out[0]["content"] == "hier das foto"
    assert "base64" not in str(out)
    assert len(out[1]["content"]) <= hb._MAX_MSG_CONTENT + 20
    assert out[1]["content"].endswith("...[truncated]")


def test_resolved_bundle_drops_history(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    b = hb.create("scope-x", history=[{"role": "user", "content": "sensitive"}],
                  summary="s", question="q")
    assert hb.load("scope-x", b["id"])["history"]
    hb.update_status("scope-x", b["id"], "resolved")
    reloaded = hb.load("scope-x", b["id"])
    assert reloaded["status"] == "resolved"
    assert "history" not in reloaded, "resolved bundle kept its privacy residue"
