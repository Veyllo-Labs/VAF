# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P3.6: no agent mail tool may sit on the FastAPI route module's import path (the
teardown blocker P7 deletes), and every mail verb has a .run() boundary test. The
broader email_sync_store/email_transport imports stay as the flag-off fallback
until P7, so this guard is scoped to email_routes for now."""
import ast
import pathlib

import vaf.tools as _tools_pkg

# The 9 modules that hold the 11 mail verbs (forward/archive/delete live in manage_mail).
_TOOL_FILES = ["mail_inbox", "read_mail", "find_mail", "send_mail", "label_mail",
               "mark_mail_answered", "list_email_accounts", "reply_mail", "manage_mail"]


def _imported_modules(path: pathlib.Path) -> set:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for n in node.names:
                mods.add(n.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_no_mail_tool_imports_email_routes():
    root = pathlib.Path(_tools_pkg.__file__).parent
    for name in _TOOL_FILES:
        offenders = [m for m in _imported_modules(root / f"{name}.py") if "email_routes" in m]
        assert not offenders, f"{name} imports {offenders}"


def test_list_email_accounts_run(monkeypatch):
    import vaf.tools.list_email_accounts as le
    monkeypatch.setattr(le, "list_accounts_with_labels_for_user",
                        lambda u, user_scope_id=None: [{"email": "a@x", "label": "Work"}])
    assert "a@x" in le.ListEmailAccountsTool().run()


def test_archive_and_delete_run(monkeypatch):
    import vaf.tools.manage_mail as mm

    class FakeSvc:
        def archive(self, pk):
            return {"ok": True, "dest": "All Mail"}

        def trash(self, pk):
            return {"ok": True, "dest": "Trash"}

    monkeypatch.setattr(mm, "_v2_required", lambda: None)
    monkeypatch.setattr(mm, "_service", lambda scope: FakeSvc())
    monkeypatch.setattr(mm, "_pk_by_message_id", lambda svc, mid: 5)
    monkeypatch.setattr(mm, "_write_note", lambda: "")
    assert "Archived" in mm.ArchiveMailTool().run(message_id="<m@x>")
    assert "trash" in mm.DeleteMailTool().run(message_id="<m@x>").lower()
