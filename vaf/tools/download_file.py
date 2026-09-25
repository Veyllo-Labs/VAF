# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""download_file: a file from the web, saved as-is into the person's own files.

A jar, an archive, a texture pack or a PDF could only be downloaded with a shell command
(measured in a long build session: every download was a curl through host_bash, an account
permission outside the file jail). This tool writes under the WRITE jail instead
(`file_access = "write"`): a relative path means the chat's workspace, and nothing lands
outside the caller's own tree.

A tool of its own rather than a parameter of webfetch, deliberately: reading a page writes
nothing, and a write parameter would have put the whole page reader under the write jail and
into the lanes that must not create files (a thinking run proposes, it does not download).

At most MAX_DOWNLOAD_BYTES, checked against the declared length before anything is written and
again while streaming; the transfer goes to a `.part` file that is removed on every failure, so
a file under the requested name is always complete.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from vaf.tools.base import BaseTool

MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024


class DownloadFileTool(BaseTool):
    name = "download_file"
    category = "web"
    permission_level = "write"
    side_effect_class = "reversible"
    identity_kwargs = ("user_role", "user_scope_id")
    file_access = "write"
    description = (
        "Download a file from a URL (an archive, an image, a PDF, a jar) and save it as-is. "
        "`save_to` is where it goes: a path relative to this chat's workspace, or an absolute "
        "path in your files; a folder (ending in '/') keeps the file's own name, and leaving it "
        "out saves into the workspace under that name. To READ a page, use webfetch instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The file's URL."},
            "save_to": {"type": "string", "description": "Where to save it (default: this chat's workspace)."},
            "timeout": {"type": "integer", "description": "Seconds without data before giving up (default 60)."},
        },
        "required": ["url"],
    }
    input_aliases = {"save_to": ["path", "destination", "target"]}

    def budget_seconds(self, args):
        # A large file takes minutes; `timeout` bounds the silence between chunks, not the
        # whole transfer.
        return 900

    def run(self, **kwargs) -> str:
        import requests
        from vaf.core.subagent_ipc import get_current_session_id
        from vaf.tools._browser_headers import browser_headers
        from vaf.tools.filesystem import is_safe_path

        url = str(kwargs.get("url") or "").strip()
        if not url:
            return "Error: no URL given."
        if not urlparse(url).scheme:
            url = "https://" + url
        if urlparse(url).scheme not in ("http", "https"):
            return "Error: only http and https URLs can be downloaded."
        try:
            timeout = max(5, min(int(kwargs.get("timeout") or 60), 600))
        except (TypeError, ValueError):
            timeout = 60

        save_to = str(kwargs.get("save_to") or "").strip()
        session_id = get_current_session_id()
        target = Path(os.path.expanduser(save_to)) if save_to else Path("")
        if not save_to or not target.is_absolute():
            ws = None
            if session_id:
                try:
                    from vaf.core.session import get_session_workspace_dir
                    ws = get_session_workspace_dir(session_id, create=True)
                except Exception:
                    ws = None
            if not ws:
                return ("Error: without an absolute save_to the file goes to this chat's "
                        "workspace, and this chat has none. Pass an absolute path.")
            target = Path(ws) / save_to if save_to else Path(ws)
        name = Path(urlparse(url).path).name or "download"
        if not save_to or save_to.endswith(("/", "\\")) or target.is_dir():
            target = target / name
        safe, resolved = is_safe_path(str(target))
        if not safe:
            return resolved
        target = Path(resolved)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"Error: cannot create {target.parent}: {e}"

        tmp = target.with_name(target.name + ".part")
        written = 0
        kind = ""
        try:
            with requests.get(url, headers=browser_headers(), timeout=timeout, stream=True,
                              allow_redirects=True) as res:
                if res.status_code != 200:
                    return f"Error: the site answered {res.status_code}; nothing was saved."
                try:
                    declared = int(res.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    declared = 0    # malformed: the streaming cap below still holds
                if declared > MAX_DOWNLOAD_BYTES:
                    return (f"Error: the file is {declared} bytes, more than the "
                            f"{MAX_DOWNLOAD_BYTES} a download may be; nothing was saved.")
                kind = res.headers.get("Content-Type", "")
                with open(tmp, "wb") as fh:
                    for chunk in res.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise ValueError(f"more than {MAX_DOWNLOAD_BYTES} bytes")
                        fh.write(chunk)
            os.replace(tmp, target)
        except requests.exceptions.SSLError as e:
            tmp.unlink(missing_ok=True)
            return (f"Error: the site's TLS certificate could not be verified, so nothing was "
                    f"saved ({e}).")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return f"Error downloading {url}: {e}; nothing was saved."
        if session_id:
            try:
                from vaf.core.web_interface import notify_file_created
                notify_file_created(session_id, str(target), title=target.name)
            except Exception:
                pass
        return f"Saved {written} bytes to {target}" + (f" ({kind})" if kind else "") + "."
