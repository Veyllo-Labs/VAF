# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""render_check - one rendered look at a page, for the build-run-inspect-fix loop.

The thin tool face over vaf.core.browser_render.render_page(). Two callers,
one class: the main agent gets it through tool auto-discovery (default
constructor), the coder registers RenderCheckTool(base_dir=...) in its
local_tools so relative file targets resolve against the project being built.

What the caller gets back is deliberately a DEVELOPER's report, not prose:
final URL and title, page errors, console output, failed network requests,
and the rendered text - the things that answer "did my page actually work".
The screenshot lands in the chat session's workspace (when there is one), so
a vision-capable setup can inspect it with analyze_image; without a session
or without vision the text report already carries the loop. That degradation
is the design, not a fallback: the tool must be useful with nothing but text.
"""

from __future__ import annotations

import base64
import os
from typing import Optional

from vaf.tools.base import BaseTool


class RenderCheckTool(BaseTool):
    """Render a URL or workspace file in the sandbox browser and report on it."""

    identity_kwargs = ("user_scope_id",)
    name = "render_check"
    category = "web"
    permission_level = "write"
    side_effect_class = "reversible"
    # Same stance as browser_agent: the probe drives the sandbox browser and
    # can reach host services through host.docker.internal - not a surface to
    # hand to remote messenger channels.
    channel_restrictions = ("telegram", "whatsapp", "discord")

    description = (
        "Open a URL or an HTML file from the project workspace in the sandbox browser, "
        "wait for it to load, and report what a developer checks first: page errors, "
        "console output, failed network requests (HTTP >= 400), the final URL/title, and "
        "the rendered text. A screenshot is saved into the chat workspace (inspect it "
        "with analyze_image when layout matters). Use it after writing or changing a web "
        "page to verify it actually renders - it is a single-look probe, NOT a browser "
        "agent: no clicking, no forms; for multi-step flows use browser_agent. "
        "localhost URLs are reachable only if the dev server listens on 0.0.0.0 "
        "(e.g. `next dev -H 0.0.0.0`, `python -m http.server --bind 0.0.0.0`); a server "
        "bound to 127.0.0.1 is invisible to the sandbox browser."
    )

    input_examples = [
        {"target": "index.html"},
        {"target": "http://localhost:3000/"},
        {"target": "https://example.com/", "wait_ms": 3000},
    ]

    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "What to render: an http(s) URL, or a path to an existing file in "
                    "the project workspace (relative paths allowed)."
                ),
            },
            "wait_ms": {
                "type": "integer",
                "description": (
                    "Extra settle time after the load event, for pages that render "
                    "client-side. Default 1500, max 10000."
                ),
                "default": 1500,
            },
        },
        "required": ["target"],
    }

    def __init__(self, base_dir: Optional[str] = None):
        # The coder passes its project directory so `index.html` means the file
        # it just wrote; the main agent's default constructor leaves targets to
        # resolve as the caller typed them (absolute, or URL).
        self.base_dir = base_dir

    def run(self, **kwargs) -> str:
        from vaf.core.browser_render import render_page

        target = (kwargs.get("target") or "").strip()
        if not target:
            return "Error: target parameter is required (URL or workspace file path)."
        try:
            wait_ms = min(10000, max(0, int(kwargs.get("wait_ms") or 1500)))
        except Exception:
            wait_ms = 1500

        if not target.lower().startswith(("http://", "https://")):
            if self.base_dir and not os.path.isabs(os.path.expanduser(target)):
                target = os.path.join(self.base_dir, target)
            # The shared file rule first, the core's projects-root jail second:
            # the jail decides WHERE is renderable, this screen decides WHAT -
            # a project's own .env is inside the jail and still not a page
            # anyone should render into a report.
            from vaf.tools.filesystem import is_safe_path
            ok, msg = is_safe_path(target)
            if not ok:
                return f"render_check refused: {msg}"

        # 6000, not the core's 8000: the coder keeps the FIRST 8000 chars of a
        # tool result, and the rendered text closes this report - headroom for
        # the error/console lines above it keeps the text from being the part
        # that falls off.
        result = render_page(target, user_scope_id=kwargs.get("user_scope_id"),
                             wait_ms=wait_ms, max_text=6000)
        return self._format(result)

    # ── the report ────────────────────────────────────────────────────────────

    def _format(self, r: dict) -> str:
        if not r.get("ok"):
            prefix = "Browser busy" if r.get("busy") else "render_check failed"
            return f"{prefix}: {r.get('error') or 'unknown error'}"

        lines = [f"Rendered: {r.get('url', '')}"]
        if r.get("rewritten"):
            lines.append(
                "(localhost was rewritten to host.docker.internal - the sandbox "
                "browser reached your HOST machine; the server must listen on 0.0.0.0)")
        if r.get("title"):
            lines.append(f"Title: {r['title']}")

        errs = r.get("page_errors") or []
        lines.append(f"\nPage errors ({len(errs)}):" if errs else "\nPage errors: none")
        lines.extend(f"  - {e}" for e in errs)

        failed = r.get("failed_requests") or []
        if failed:
            lines.append(f"Failed requests ({len(failed)}):")
            lines.extend(f"  - {f}" for f in failed)
        else:
            lines.append("Failed requests: none")

        console = r.get("console") or []
        if console:
            lines.append(f"Console ({len(console)}):")
            lines.extend(f"  - {c}" for c in console)
        else:
            lines.append("Console: quiet")

        shot_note = self._save_screenshot(r.get("screenshot_b64") or "")
        if shot_note:
            lines.append(shot_note)

        text = (r.get("text") or "").strip()
        if text:
            lines.append("\nRendered text:\n" + text)
        else:
            lines.append("\nRendered text: EMPTY - the page produced no visible text "
                         "(blank page, render failure, or purely graphical content).")
        return "\n".join(lines)

    def _save_screenshot(self, b64: str) -> str:
        """Into the chat session's workspace, where analyze_image's jail allows
        it. No session (coder child, automation) means no file - the text
        report stands on its own there, by design."""
        if not b64:
            return ""
        try:
            from vaf.core.session import get_session_workspace_dir
            from vaf.core.subagent_ipc import get_current_session_id
            session_id = (get_current_session_id() or "").strip()
            if not session_id:
                return ""
            ws = get_session_workspace_dir(session_id, create=True)
            if not ws:
                return ""
            path = os.path.join(str(ws), "render_check.jpg")
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            return ("Screenshot: render_check.jpg (chat workspace; view it with "
                    "analyze_image image_path='render_check.jpg' if layout matters)")
        except Exception:
            return ""
