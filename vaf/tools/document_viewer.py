"""
Two tools for the Web UI document panel:

- document_viewer: Open a document in the Document Viewer (Anhänge list). The file content
  is read and shown in the right-hand attachments list so the user and agent can refer to it.

- document_editor: Open a document in the Document Editor (single-file editor). The file
  opens in the right-hand editor so the user can view and edit it.
"""
import base64
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from vaf.core.config import Config
from vaf.tools.base import BaseTool


def _path_from_string(path_str: str) -> Path:
    """Accept a path or file:// URL and return a resolved Path (cross-platform)."""
    s = (path_str or "").strip()
    if not s:
        return Path(".")
    if s.lower().startswith("file://"):
        parsed = urlparse(s)
        s = url2pathname(parsed.path)
    return Path(s).resolve()


class DocumentViewerTool(BaseTool):
    """
    Open a document in the Document Viewer panel (Anhänge list).
    Use when the user wants to "attach" or "add" a document to the chat so both
    user and agent can see its content in the right panel (list of documents).
    """

    name = "document_viewer"
    description = """Open a document in the Document Viewer (Anhänge list) so its content is visible in the right panel.
Use when the user asks to "attach", "add to chat", or "show me the content" of a file in the document list.
Pass the full file path. The document appears in the right-hand Document Viewer (attachments list)."""

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Full path to the file to open in the Document Viewer (e.g. C:\\Users\\...\\report.pdf).",
            }
        },
        "required": ["path"],
    }

    def run(self, **kwargs) -> str:
        path_str = kwargs.get("path") or ""
        if not path_str or not path_str.strip():
            return "Error: path is required."

        path = _path_from_string(path_str)
        if not path.exists():
            return f"Error: File not found: {path}"
        if not path.is_file():
            return f"Error: Not a file: {path}"

        try:
            from vaf.core.subagent_ipc import get_current_session_id
            from vaf.core.session import SessionManager
            from vaf.core.web_interface import get_web_interface
            from vaf.tools.librarian import LibrarianTool
        except ImportError as e:
            return f"Error: Could not load dependencies: {e}"

        session_id = get_current_session_id()
        if not session_id:
            return "Error: No active session (Document Viewer is only available in the Web UI with an active chat session)."

        try:
            librarian = LibrarianTool()
            content = librarian._read_file(path, enable_chunking=True)
        except Exception as e:
            return f"Error: Could not read file: {e}"

        name = path.name
        new_doc = {"name": name, "content": content, "path": str(path)}
        try:
            with open(path, "rb") as f:
                raw = f.read()
            new_doc["data"] = base64.b64encode(raw).decode("ascii")
            mime_type, _ = mimetypes.guess_type(str(path))
            if mime_type:
                new_doc["mimeType"] = mime_type
        except Exception:
            pass

        try:
            session_mgr = SessionManager()
            session = session_mgr.load(session_id)
            if not getattr(session, "runtime_state", None):
                session.runtime_state = {}
            sidebar = session.runtime_state.get("sidebar_documents") or []
            if any(d.get("name") == name for d in sidebar):
                return f'Document "{name}" is already open in the Document Viewer (Anhänge).'
            sidebar.append(new_doc)
            session.runtime_state["sidebar_documents"] = sidebar
            session_mgr.save(session, sync_state=False)
        except FileNotFoundError:
            from vaf.core.session import Session
            new_sess = Session(
                id=session_id,
                name=f"Session {session_id}",
                runtime_state={"sidebar_documents": [new_doc]},
            )
            session_mgr = SessionManager()
            session_mgr.save(new_sess, sync_state=False)
            sidebar = [new_doc]
        except Exception as e:
            return f"Error: Could not save to session: {e}"

        if bool(Config.get("attachment_rag_enabled", False)):
            try:
                from vaf.memory.attachment_rag import index_session_attachments_sync
                index_session_attachments_sync(
                    session_id=session_id,
                    user_scope_id=kwargs.get("user_scope_id"),
                    documents=sidebar,
                )
            except Exception:
                pass

        try:
            get_web_interface()._push_session_update(session_id, {
                "type": "sidebar_documents_set",
                "contents": sidebar,
                "sessionId": session_id,
            })
        except Exception:
            pass

        return f"Document \"{name}\" has been opened in the Document Viewer (Anhänge). The user can see it in the right panel."


class DocumentEditorTool(BaseTool):
    """
    Open a document in the Document Editor panel (single-file editor).
    Use when the user asks to "open" or "edit" a document in the editor.
    """

    name = "document_editor"
    description = """Open a document in the Document Editor panel so the user can view and edit it.
Use when the user asks to "open", "show in editor", or "edit" a document.
Pass the full file path. The document opens in the right-hand Document Editor (single-file editor)."""

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Full path to the file to open in the Document Editor (e.g. C:\\Users\\...\\report.docx).",
            }
        },
        "required": ["path"],
    }

    def run(self, **kwargs) -> str:
        path_str = kwargs.get("path") or ""
        if not path_str or not path_str.strip():
            return "Error: path is required."

        path = _path_from_string(path_str)
        if not path.exists():
            return f"Error: File not found: {path}"
        if not path.is_file():
            return f"Error: Not a file: {path}"

        try:
            from vaf.core.subagent_ipc import get_current_session_id
            from vaf.core.web_interface import notify_document_created
        except ImportError as e:
            return f"Error: Could not load dependencies: {e}"

        session_id = get_current_session_id()
        if not session_id:
            return "Error: No active session (Document Editor is only available in the Web UI with an active chat session)."

        try:
            notify_document_created(session_id, str(path), title=path.name)
        except Exception as e:
            return f"Error: Could not open document in Web UI: {e}"

        return f"Document \"{path.name}\" has been opened in the Document Editor. The user can view and edit it in the right panel."


class ReplaceEditorSelectionTool(BaseTool):
    """
    Replace the text of a marked selection in the Document Editor.
    Use when the user has marked a region (e.g. placeholder) and asked you to fill or edit it.
    selection_index is 0-based: 0 = first marked region, 1 = second, etc.
    """

    name = "replace_editor_selection"
    description = """Replace the text at a marked selection in the Document Editor.
Use when the user has marked a region in the editor (e.g. a placeholder like [NAME] or a sentence) and asked you to fill or edit it.
selection_index: 0 = first marked region, 1 = second, etc. (order matches the chips shown in the prompt).
new_text: the exact text to put in place of the marked region."""

    parameters = {
        "type": "object",
        "properties": {
            "selection_index": {
                "type": "integer",
                "description": "Zero-based index of the marked selection (0 = first, 1 = second, ...).",
            },
            "new_text": {
                "type": "string",
                "description": "The text to replace the marked region with.",
            },
        },
        "required": ["selection_index", "new_text"],
    }

    def run(self, **kwargs) -> str:
        selection_index = kwargs.get("selection_index", 0)
        new_text = kwargs.get("new_text") or ""
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            from vaf.core.web_interface import get_web_interface
            from vaf.core.session import SessionManager
        except ImportError as e:
            return f"Error: Could not load dependencies: {e}"

        session_id = get_current_session_id()
        if not session_id:
            return "Error: No active session (replace in editor is only available in the Web UI with an active chat session)."

        if selection_index < 0:
            return "Error: selection_index must be >= 0."

        start, end = None, None
        try:
            session_mgr = SessionManager()
            session = session_mgr.load(session_id)
            selections = (getattr(session, "runtime_state", None) or {}).get("editor_selections") or []
            if 0 <= selection_index < len(selections):
                s = selections[selection_index]
                if isinstance(s, dict):
                    start = s.get("start")
                    end = s.get("end")
        except Exception:
            pass

        try:
            get_web_interface().emit_editor_apply_edit(
                session_id, selection_index, new_text, start=start, end=end
            )
        except Exception as e:
            return f"Error: Could not send edit to Web UI: {e}"

        return "The marked region in the Document Editor has been updated with the new text."
