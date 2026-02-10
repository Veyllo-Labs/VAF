"""
Document Viewer tool: open a document in the Web UI sidebar so the user sees it
and the agent can use its content in follow-up turns.
Main agent and sub-agents (e.g. Librarian) can use this when they find a document
the user asked to open.
"""
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

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
    Open a document in the Document Viewer sidebar (Web UI).
    Use this when the user asks to "open", "show", or "display" a document,
    or when you find a file and want to make it visible and available for questions.
    """

    name = "document_viewer"
    description = """Open a document in the Document Viewer sidebar so the user can see it and you can use its content in follow-up turns.
Use this when:
- The user asks to "open", "show", "display", or "find and open" a document.
- You or the Librarian found a file and the user wants to see it or ask questions about it.
Pass the full file path (e.g. from list_files or librarian_agent result). Supported: PDF, Word, Excel, PowerPoint, text, and similar."""

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Full path to the file to open in the Document Viewer (e.g. C:\\Users\\...\\report.pdf or /home/user/docs/file.docx).",
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
        new_doc = {"name": name, "content": content}

        try:
            session_mgr = SessionManager()
            session = session_mgr.load(session_id)
            if not getattr(session, "runtime_state", None):
                session.runtime_state = {}
            sidebar = session.runtime_state.get("sidebar_documents") or []
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

        try:
            get_web_interface()._push_session_update(session_id, {
                "type": "sidebar_documents_set",
                "contents": sidebar,
                "sessionId": session_id,
            })
        except Exception as e:
            pass  # Session is updated; UI push is best-effort

        return f"Document \"{name}\" has been opened in the Document Viewer. The user can see it in the right sidebar, and you can refer to its content in your next answers."
