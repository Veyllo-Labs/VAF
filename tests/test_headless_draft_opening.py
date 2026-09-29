# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The editor draft of a text the user asked for. It lives in the chat's workspace
(`drafts/entwurf.md`), where the file routes let the chat's account read and save it; the
data directory it used to live in is served to admins only."""
from pathlib import Path

from vaf.core import headless_runner

SCOPE = "ab12cd34-0000-0000-0000-000000000000"


def test_maybe_open_draft_in_editor_skips_when_editor_already_open(monkeypatch, tmp_path: Path):
    created = []

    monkeypatch.setattr(headless_runner.Platform, "documents_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        "vaf.core.web_interface.notify_document_created",
        lambda session_id, path, title="Entwurf": created.append((session_id, path, title)),
    )

    headless_runner._maybe_open_draft_in_editor(
        "sess-1",
        "Schreib mir einen Text über Qualitätssicherung",
        "A" * 300,
        "web",
        editor_has_content=True,
        user_scope_id=SCOPE,
    )

    assert created == []
    assert not list(tmp_path.rglob("entwurf.md"))


def test_maybe_open_draft_in_editor_creates_draft_when_editor_empty(monkeypatch, tmp_path: Path):
    created = []

    monkeypatch.setattr(headless_runner.Platform, "documents_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        "vaf.core.web_interface.notify_document_created",
        lambda session_id, path, title="Entwurf": created.append((session_id, path, title)),
    )

    content = "Ein neuer Entwurf fuer den Dokumenteditor. " * 10
    headless_runner._maybe_open_draft_in_editor(
        "sess-2",
        "Schreib mir einen Text ueber Testabdeckung",
        content,
        "web",
        editor_has_content=False,
        user_scope_id=SCOPE,
    )

    draft_path = tmp_path / "VAF_Projects" / "ab12cd34" / "sess-2" / "drafts" / "entwurf.md"
    assert draft_path.exists()
    assert draft_path.read_text(encoding="utf-8") == content.strip()
    assert created == [("sess-2", str(draft_path.resolve()), "Entwurf")]
