from pathlib import Path

from vaf.core import headless_runner


def test_maybe_open_draft_in_editor_skips_when_editor_already_open(monkeypatch, tmp_path: Path):
    created = []

    monkeypatch.setattr(headless_runner.Platform, "data_dir", staticmethod(lambda: tmp_path))
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
    )

    assert created == []
    assert not (tmp_path / "drafts" / "sess-1" / "entwurf.md").exists()


def test_maybe_open_draft_in_editor_creates_draft_when_editor_empty(monkeypatch, tmp_path: Path):
    created = []

    monkeypatch.setattr(headless_runner.Platform, "data_dir", staticmethod(lambda: tmp_path))
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
    )

    draft_path = tmp_path / "drafts" / "sess-2" / "entwurf.md"
    assert draft_path.exists()
    assert draft_path.read_text(encoding="utf-8") == content.strip()
    assert created == [("sess-2", str(draft_path.resolve()), "Entwurf")]
