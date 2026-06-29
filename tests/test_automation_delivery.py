# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Automation result delivery + the {date} workflow-resolver fix.

Covers three fixes:
  A) The workflow variable resolver used to raise KeyError on any unknown {var} (a single
     hallucinated {date} killed a whole scheduled automation: "Missing variable: 'date'").
     Now temporal built-ins are always seeded and unknown simple vars pass through literally.
  B) Automation results are delivered to the configured main messenger with the output FILE
     attached (Telegram/WhatsApp/Discord), via the canonical send_to_main_messenger, resolving the
     TASK OWNER (never the local admin) — no cross-user leak.
  C) File output is conditional (chat-only unless the user wants a file) and the create_scheduled_task
     workflow prompt is file-NEUTRAL so it drives neither the file gate nor the workflow-gen by itself.
"""
import re
import sys
import types

import pytest

from vaf.workflows.engine import WorkflowEngine, _temporal_builtins
from vaf.tools.automation import AutomationTool


# ── A) engine: temporal built-ins + non-fatal resolver ───────────────────────────────────────────

def test_temporal_builtins_present_and_filename_safe():
    b = _temporal_builtins()
    for k in ("date", "today", "current_date", "time", "now", "datetime",
              "iso_date", "timestamp", "year", "month", "day"):
        assert k in b, k
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", b["date"])
    assert re.fullmatch(r"\d{8}_\d{6}", b["timestamp"])
    # values that commonly land in write_file paths must be filename-safe (no ':' or '/')
    for k in ("date", "today", "current_date", "time", "timestamp", "year", "month", "day"):
        assert ":" not in b[k] and "/" not in b[k], k


def _bare_engine() -> WorkflowEngine:
    return WorkflowEngine.__new__(WorkflowEngine)  # _resolve_template doesn't touch self


def test_resolve_template_resolves_seeded_builtins():
    eng = _bare_engine()
    out = eng._resolve_template("report_{date}.html", dict(_temporal_builtins()))
    assert "{date}" not in out and _temporal_builtins()["date"] in out


def test_resolve_template_unknown_var_is_non_fatal_passthrough():
    eng = _bare_engine()
    # the bug: this used to raise KeyError -> "Missing variable" -> whole workflow failed
    out = eng._resolve_template("hello {totally_unknown_xyz} world", {})
    assert out == "hello {totally_unknown_xyz} world"


def test_resolve_template_nested_missing_still_raises():
    eng = _bare_engine()
    with pytest.raises(KeyError):
        eng._resolve_template("{step.field}", {})


def test_resolve_template_user_var_overrides_builtin():
    eng = _bare_engine()
    merged = dict(_temporal_builtins())
    merged["date"] = "USER_SUPPLIED"
    assert eng._resolve_template("{date}", merged) == "USER_SUPPLIED"


def test_engine_seeds_builtins_wiring_present():
    import inspect
    import vaf.workflows.engine as _eng
    src = inspect.getsource(_eng)
    assert "_temporal_builtins()" in src
    assert "outputs.setdefault" in src


# ── C) file-intent detector + conditional create_scheduled_task ──────────────────────────────────

def test_prompt_wants_file_detector():
    f = AutomationTool._prompt_wants_file
    for s in ("Wetter als HTML", "speichere das Ergebnis", "save it to a file",
              "erstelle eine Datei", "export the data", "report.pdf", "als PDF",
              "lade die Daten als CSV"):
        assert f(s) is True, s
    for s in ("Sag mir täglich das Wetter für Berlin", "Tell me the price every day",
              "Erinnere mich ans Wasser trinken", "update my profile daily", ""):
        assert f(s) is False, s  # note: 'profile' must NOT match 'file'


def test_create_scheduled_task_workflow_is_file_neutral():
    from vaf.workflows.workflows.create_scheduled_task import WORKFLOW
    defaults = WORKFLOW["defaults"]
    # the always-HTML-to-Desktop defaults are gone
    assert "output_path" not in defaults and "format" not in defaults
    args = WORKFLOW["steps"][0]["args"]
    # output_path is no longer a forced step arg (with the non-fatal resolver it would otherwise
    # resolve to the literal string "{output_path}" and force a file every time)
    assert "output_path" not in args and "format" not in args
    boiler = args["prompt"]
    # With a NEUTRAL task, the boilerplate alone must trip NEITHER the workflow-gen gate (P2) nor
    # the file gate (C1) — only the embedded user task may.
    neutral = boiler.replace("{task_description}", "Tell me the weather for Berlin")
    assert AutomationTool._prompt_needs_workflow_gen(neutral) is False
    assert AutomationTool._prompt_wants_file(neutral) is False
    # With a FILE task, both fire — driven by the user's words, not the boilerplate.
    filey = boiler.replace("{task_description}", "Weather for Berlin, save as HTML")
    assert AutomationTool._prompt_needs_workflow_gen(filey) is True
    assert AutomationTool._prompt_wants_file(filey) is True


# ── B) messenger delivery forwards the file per channel ──────────────────────────────────────────

def test_send_to_main_messenger_forwards_file_telegram(monkeypatch, tmp_path):
    import vaf.core.messaging_connections as mc
    import vaf.core.telegram_reply as tr
    f = tmp_path / "report.html"
    f.write_text("<html></html>")
    monkeypatch.setattr(mc, "get_messaging_connections", lambda **k: {"main_messenger": "telegram"})
    monkeypatch.setattr(mc, "get_telegram_chat_id", lambda *a, **k: "12345")
    calls = []
    monkeypatch.setattr(tr, "send_telegram_reply",
                        lambda chat_id, text, **kw: (calls.append((chat_id, text, kw)), True)[1])
    ok, ch = mc.send_to_main_messenger("scope", "user", "Body text", file_path=str(f))
    assert ok is True and ch == "telegram"
    assert len(calls) == 2                                   # text message, then the attachment
    assert calls[0][2].get("file_path") is None              # 1st: plain text
    assert calls[1][2].get("file_path") == str(f)            # 2nd: carries the file


def test_send_to_main_messenger_text_only_when_no_file(monkeypatch):
    import vaf.core.messaging_connections as mc
    import vaf.core.telegram_reply as tr
    monkeypatch.setattr(mc, "get_messaging_connections", lambda **k: {"main_messenger": "telegram"})
    monkeypatch.setattr(mc, "get_telegram_chat_id", lambda *a, **k: "12345")
    calls = []
    monkeypatch.setattr(tr, "send_telegram_reply",
                        lambda chat_id, text, **kw: (calls.append(kw), True)[1])
    ok, ch = mc.send_to_main_messenger("scope", "user", "Body text")  # no file_path
    assert ok is True and ch == "telegram"
    assert len(calls) == 1 and "file_path" not in calls[0]   # existing behavior unchanged


def test_send_to_main_messenger_forwards_file_discord(monkeypatch, tmp_path):
    import vaf.core.messaging_connections as mc
    import vaf.core.discord_send as ds
    from vaf.core.config import Config
    f = tmp_path / "report.html"
    f.write_text("x")
    monkeypatch.setattr(mc, "get_messaging_connections", lambda **k: {"main_messenger": "discord"})
    monkeypatch.setattr(mc, "get_discord_user_id", lambda *a, **k: "uid-1")
    orig_get = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: {"bot_token": "tok"} if key == "discord_config" else orig_get(key, default)
    ))
    calls = []
    monkeypatch.setattr(ds, "send_discord_dm",
                        lambda token, uid, text, **kw: (calls.append((token, uid, text, kw)), True)[1])
    ok, ch = mc.send_to_main_messenger("scope", "user", "Body", file_path=str(f))
    assert ok is True and ch == "discord"
    assert len(calls) == 2
    assert calls[0][3].get("file_path") is None
    assert calls[1][3].get("file_path") == str(f)


def test_send_to_main_messenger_forwards_file_whatsapp(monkeypatch, tmp_path):
    import vaf.core.messaging_connections as mc
    import vaf.core.whatsapp_reply as wr
    f = tmp_path / "report.html"
    f.write_text("x")
    monkeypatch.setattr(mc, "get_messaging_connections", lambda **k: {"main_messenger": "whatsapp"})
    monkeypatch.setattr(mc, "get_whatsapp_chat_jid", lambda *a, **k: "jid@s.whatsapp.net")
    monkeypatch.setattr(wr, "send_whatsapp_reply", lambda user, jid, text, **kw: True)
    captured = {}
    fake_bridge = types.ModuleType("vaf.api.whatsapp_bridge")
    def _swc(username, jid, text, document_path=None, **kw):
        captured["document_path"] = document_path
        return "sent"
    fake_bridge.send_whatsapp_with_confirmation = _swc
    monkeypatch.setitem(sys.modules, "vaf.api.whatsapp_bridge", fake_bridge)
    ok, ch = mc.send_to_main_messenger("scope", "user", "Body", file_path=str(f))
    assert ok is True and ch == "whatsapp"
    assert captured.get("document_path") == str(f)           # WhatsApp uses the document-capable path


# ── B) discord low-level: multipart with file, JSON without ──────────────────────────────────────

class _Resp:
    ok = True
    status_code = 200
    text = ""


def test_send_discord_message_multipart_with_file(monkeypatch, tmp_path):
    import vaf.core.discord_send as ds
    f = tmp_path / "r.html"
    f.write_text("<html>")
    captured = {}
    monkeypatch.setattr(ds.requests, "post",
                        lambda url, **kw: (captured.update(url=url, kw=kw), _Resp())[1])
    assert ds.send_discord_message("tok", "chan", "caption", file_path=str(f)) is True
    assert "files" in captured["kw"]                          # multipart upload
    assert "payload_json" in captured["kw"]["data"]
    assert "json" not in captured["kw"]                       # NOT the text-only JSON path


def test_send_discord_message_json_without_file(monkeypatch):
    import vaf.core.discord_send as ds
    captured = []
    monkeypatch.setattr(ds.requests, "post",
                        lambda url, **kw: (captured.append(kw), _Resp())[1])
    assert ds.send_discord_message("tok", "chan", "hello", chunk=False) is True
    assert "json" in captured[0] and "files" not in captured[0]


# ── B) _push_result_to_web_ui threads the file + resolves the task owner ──────────────────────────

def test_push_result_forwards_file_and_task_scope(monkeypatch, tmp_path):
    import vaf.core.automation as auto
    import vaf.core.web_interface as wifc
    f = tmp_path / "out.html"
    f.write_text("<html>")
    captured = {}
    monkeypatch.setattr("vaf.core.messaging_connections.send_to_main_messenger",
                        lambda scope, username, text, file_path=None:
                        (captured.update(scope=scope, text=text, file_path=file_path), (True, "telegram"))[1])
    monkeypatch.setattr("vaf.core.user_notifications.append_notification", lambda *a, **k: {})
    # short-circuit the Web UI half quickly (it is wrapped in try/except anyway)
    monkeypatch.setattr(wifc, "get_web_interface", lambda: (_ for _ in ()).throw(RuntimeError("no web")))
    task = types.SimpleNamespace(name="My Task", user_scope_id="scope-xyz")
    auto._push_result_to_web_ui(task, "success", "Summary body", output_file=str(f))
    assert captured.get("file_path") == str(f)                # the file is attached on the messenger
    assert captured.get("scope") == "scope-xyz"               # delivered to the TASK's scope


def test_resolve_username_admin_scope_returns_admin(monkeypatch):
    import vaf.core.automation as auto
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    admin_scope = get_local_admin_scope_id()
    assert auto._resolve_username(admin_scope) == (get_local_admin_username() or "admin")
    # empty scope (single-user/local) also resolves to admin
    assert auto._resolve_username(None) == (get_local_admin_username() or "admin")
