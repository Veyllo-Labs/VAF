# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A prompt that answers somebody else carries what a reply needs, not the owner's profile.

A contact's turn (Front Office) and an embedder's allowed set both run under the owner's
identity. Their system prompt carried the owner's profile digest (`known_facts`, the top facts
of the general memory), the machine's home and workspace paths, the `<workspace>` block and the
profile details (city, country, preferences, dos and don'ts), so the model could repeat them to
a stranger. They now stay out unless the owner opened their general memory for Front Office
turns; the name, language, main messenger and time settings the reply needs stay in.

MUTATIONS: read the digest regardless of the switch; never open it; keep the full environment
line or the workspace block for somebody else; drop the profile-detail condition; drop
`or front_office`; remove the keyword at either agent call site; remove the cross-chat gate
clause for an allowed set.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
IDENTITY = {"name": "Alice", "preferred_language": "de", "main_messenger": "whatsapp",
            "timezone": "Europe/Berlin", "city": "CITY7QMARK", "country": "COUNTRY7QMARK",
            "preferences": "PREF7QMARK", "dos": "DOS7QMARK", "donts": "DONTS7QMARK"}
DETAILS = ("CITY7QMARK", "COUNTRY7QMARK", "PREF7QMARK", "DOS7QMARK", "DONTS7QMARK")


@pytest.fixture
def prompt(monkeypatch, tmp_path):
    import vaf.auth.user_workspace as uw
    import vaf.core.front_office_profile as fop
    from vaf.core.config import Config
    from vaf.core.system_prompt import SystemPromptManager

    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app", raising=False)
    cache = tmp_path / "app" / "user_profile_cache"
    cache.mkdir(parents=True)
    (cache / f"{SCOPE}.txt").write_bytes(b"FACTS7QMARK Alice works at the harbour")
    monkeypatch.setattr(uw, "get_user_workspace",
                        lambda username: SimpleNamespace(get_user_identity=lambda: dict(IDENTITY)))
    switch = {"use_general_memory": False}
    monkeypatch.setattr(fop, "load_front_office_profile", lambda username: dict(switch))
    manager = SystemPromptManager(tools=[], agent_instance=None)

    def _build(**kw):
        return manager.build_prompt(username="alice", user_scope_id=SCOPE, **kw)
    _build.switch = switch
    return _build


def test_the_owner_sees_everything(prompt):
    text = prompt()
    assert "known_facts: FACTS7QMARK" in text
    assert "home: " in text and "Never invent or shorten" in text
    for marker in DETAILS:
        assert marker in text


@pytest.mark.parametrize("kw", [{"front_office": True}, {"answers_somebody_else": True}])
def test_somebody_else_gets_no_profile_and_no_paths(prompt, kw):
    text = prompt(**kw)
    assert "known_facts:" not in text and "FACTS7QMARK" not in text
    assert str(Path.home()) not in text
    assert "home: " not in text and "this chat's workspace" not in text
    assert "Never invent or shorten" not in text
    assert "os: " in text
    for marker in DETAILS:
        assert marker not in text, marker
    for line in ("name: Alice", "preferred_language: de", "main_messenger: whatsapp",
                 "timezone: Europe/Berlin"):
        assert line in text, line


def test_the_owners_switch_opens_the_profile_to_a_contact_only(prompt):
    prompt.switch["use_general_memory"] = True
    contact = prompt(front_office=True)
    assert "known_facts: FACTS7QMARK" in contact
    for marker in DETAILS:
        assert marker in contact
    assert "home: " not in contact, "the switch opens the memory, never the machine's paths"
    # An embedder's set has no such switch and stays closed.
    embedder = prompt(answers_somebody_else=True)
    assert "FACTS7QMARK" not in embedder
    for marker in DETAILS:
        assert marker not in embedder


def test_both_agent_prompts_say_when_somebody_else_is_answered():
    src = (ROOT / "vaf" / "core" / "agent.py").read_bytes().decode("utf-8")
    calls = [m.end() for m in re.finditer(r"prompt_manager\.build_prompt\(", src)]
    assert len(calls) == 2
    for pos in calls:
        window = src[pos:src.index("\n        )", pos)]
        assert 'answers_somebody_else=getattr(self, "_allowed_tools", None) is not None' in window
    gate = src[src.index("def _refresh_cross_chat_hints"):]
    gate = gate[:gate.index("\n    def ")]
    assert 'if getattr(self, "_allowed_tools", None) is not None:\n            return' in gate


def test_the_workspace_block_is_the_owners_only(prompt, tmp_path):
    """MUTATION: build `<workspace>` for somebody else - red."""
    info = {"cwd": str(tmp_path / "WS7QMARK"), "project_root": "None", "is_in_project": False}
    from vaf.core.system_prompt import SystemPromptManager
    agent = SimpleNamespace(workspace=SimpleNamespace(get_context_info=lambda: dict(info)))
    manager = SystemPromptManager(tools=[], agent_instance=agent)
    owner = manager.build_prompt(username="alice", user_scope_id=SCOPE)
    assert "<workspace>" in owner and "WS7QMARK" in owner
    contact = manager.build_prompt(username="alice", user_scope_id=SCOPE, front_office=True)
    assert "<workspace>" not in contact and "WS7QMARK" not in contact
