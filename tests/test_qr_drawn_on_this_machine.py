# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every QR code the web UI shows is drawn on this machine, and no web image is fetched from a
third-party renderer.

A QR carries a credential (a 2FA secret, a device pairing value, the WhatsApp link's pairing
reference and keys). An image URL that hands it to a renderer elsewhere sends it, together with
the browser's address and VAF's origin, to a stranger's server, and the screen breaks on an
install without public internet. The one renderer is `vaf.api.auth_routes.qr_png_base64`; its
callers answer the PNG `no-store` and never log the value.

What is scanned: (a) every file under `web/` and `vaf/` (not `node_modules`, `.next`, bytecode)
for the hosts of known QR and chart renderers; (b) the image sinks of the web sources (`src=`,
`srcSet=`, `poster=`, CSS `url(`) for an absolute `http(s)://` value, with one named exception;
(d) the `qrcode` imports in `vaf/`. Not scanned, and named here so the next reader knows: a URL
assembled into a variable before it reaches `src` is invisible to (b), which is why (a) names
the renderer hosts themselves.

Named boundary: the renderer lives in the harness (`vaf/api`) because only harness HTTP routes
show a QR. The framework and the CLI show none (the setup command declines a terminal QR on
purpose); if either ever needs one, `qr_png_base64` moves down to `vaf/core` then.
"""
import asyncio
import ast
import base64
import json
import logging
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
VAF = ROOT / "vaf"
WA_WIZARD = WEB / "components" / "connections" / "WhatsAppSetupWizard.tsx"
LOCALES = ("en", "de", "ja", "ko", "th", "tr", "zh")

_PRUNED_DIRS = {"node_modules", ".next", "__pycache__", "out"}

# Hosts of public QR and chart renderers. Spelled without their scheme so a protocol-relative
# or plain-text mention is found too.
_RENDERER_HOSTS = (
    "qrserver",
    "chart.googleapis.com",
    "chart.apis.google.com",
    "quickchart.io",
    "goqr.me",
    "qr-code-generator.com",
    "tec-it.com",
    "image-charts.com",
    "qrickit",
)

# An image sink with an absolute URL as its value. `src`, `srcSet`, `poster` as a JSX
# attribute or a property assignment, and CSS `url(`.
_ABSOLUTE_SINK = re.compile(
    r"""(?:\b(?:src|srcset|poster)\s*=\s*\{?\s*[`'"](?P<attr>https?://[^`'"]*)"""
    r"""|\burl\(\s*['"]?(?P<css>https?://[^'")\s]*))""",
    re.IGNORECASE,
)

# The one allowed absolute image URL, with the reason it is not a leak.
_ALLOWED_ABSOLUTE_IMAGES = {
    ("web/components/connections/GitHubDashboard.tsx", "https://github.com/${acc.login}.png"):
        "the avatar of a connected GitHub account, fetched from GitHub by the account's own "
        "public login; it carries no secret and goes to the service the account belongs to",
}


def _walk(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            yield Path(dirpath) / name


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _renderer_hits(text: str) -> list:
    low = text.lower()
    return [host for host in _RENDERER_HOSTS if host in low]


def _web_sources():
    for sub in ("app", "components", "lib", "hooks"):
        base = WEB / sub
        if not base.is_dir():
            continue
        for path in _walk(base):
            if path.suffix in (".ts", ".tsx") or (sub == "app" and path.suffix == ".css"):
                yield path


def _absolute_sinks(text: str) -> list:
    return [m.group("attr") or m.group("css") for m in _ABSOLUTE_SINK.finditer(text)]


def _all_absolute_sinks() -> set:
    found = set()
    for path in _web_sources():
        for url in _absolute_sinks(path.read_text(encoding="utf-8", errors="ignore")):
            found.add((_rel(path), url))
    return found


# ── (a) no renderer host anywhere in web/ or vaf/ ──────────────────────────────────────────

def test_no_file_names_a_third_party_qr_or_chart_renderer():
    """MUTATION: put `https://quickchart.io/qr?text=` into any component, or the host into any
    vaf/ file - red."""
    offenders = []
    for base in (WEB, VAF):
        for path in _walk(base):
            try:
                text = path.read_bytes().decode("utf-8", errors="ignore")
            except OSError:
                continue
            hits = _renderer_hits(text)
            if hits:
                offenders.append((_rel(path), hits))
    assert offenders == [], f"draw the QR with vaf.api.auth_routes.qr_png_base64: {offenders}"


# ── (b) no absolute image URL in the web sources, but the named one ────────────────────────

def test_no_web_image_is_fetched_from_an_absolute_url():
    """MUTATION: `<img src={`https://quickchart.io/qr?text=${v}`} />` in any component - red."""
    unexplained = sorted(_all_absolute_sinks() - set(_ALLOWED_ABSOLUTE_IMAGES))
    assert unexplained == [], (
        "serve the image from VAF itself (a data URL or a local route), or add the site to "
        f"_ALLOWED_ABSOLUTE_IMAGES with the reason it carries nothing private: {unexplained}")


def test_the_allowed_list_names_only_sites_that_still_exist():
    """An entry for code that moved or was converted would silently allow its next occupant.
    MUTATION: remove the GitHub avatar line - red."""
    stale = sorted(set(_ALLOWED_ABSOLUTE_IMAGES) - _all_absolute_sinks())
    assert stale == [], f"remove these entries, they no longer match an image sink: {stale}"


# ── (c) the scanners see a synthetic offender ──────────────────────────────────────────────

def test_the_scanners_see_a_synthetic_offender():
    """A guard that finds nothing proves nothing."""
    line = ('<img src={`https://api.qrserver.com/v1/create-qr-code/?data=${encodeURIComponent(v)}`} />\n'
            "img.src = 'https://quickchart.io/qr?text=x';\n"
            '<video poster="http://example.org/p.png" />\n'
            "<img srcSet='https://cdn.example.org/a.png 2x' />\n"
            ".x { background: url(https://example.org/bg.png); }\n"
            ".y { background: url('/local.png'); }\n"
            "<img src={'data:image/png;base64,' + qrPng} />\n")
    assert _renderer_hits(line) == ["qrserver", "quickchart.io"]
    assert _absolute_sinks(line) == [
        "https://api.qrserver.com/v1/create-qr-code/?data=${encodeURIComponent(v)}",
        "https://quickchart.io/qr?text=x",
        "http://example.org/p.png",
        "https://cdn.example.org/a.png 2x",
        "https://example.org/bg.png",
    ]


# ── (d) one renderer ───────────────────────────────────────────────────────────────────────

def _qrcode_importers() -> set:
    found = set()
    for path in _walk(VAF):
        if path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"""(?:import_module|__import__)\(\s*['"]qrcode""", source):
            found.add(_rel(path))
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "qrcode" for a in node.names):
                found.add(_rel(path))
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "qrcode":
                found.add(_rel(path))
    return found


def test_qrcode_is_imported_in_one_place():
    """MUTATION: `import qrcode` in vaf/api/whatsapp_routes.py - red."""
    assert _qrcode_importers() == {"vaf/api/auth_routes.py"}


# ── (e) the WhatsApp wizard shows the server's PNG ─────────────────────────────────────────

def test_the_whatsapp_wizard_shows_the_servers_png():
    """One image, a data URL built from the answer's `qr_png`, its alt text from the catalogue.
    MUTATION: the qrserver `<img>` back, or a hardcoded alt - red."""
    src = WA_WIZARD.read_text(encoding="utf-8")
    images = re.findall(r"<img\b[^>]*>", src, re.S)
    assert len(images) == 1, images
    assert "src={'data:image/png;base64,' + qrPng}" in images[0]
    assert "alt={t('qrAlt')}" in images[0]
    assert "setQrPng(String(data.qr_png || ''))" in src
    assert not re.search(r"data\.qr\b", src), "the raw value is no longer part of the answer"
    assert "encodeURIComponent(qr" not in src
    for locale in LOCALES:
        cat = json.loads((WEB / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
        alt = cat["settings"]["whatsappWizard"]["qrAlt"]
        assert isinstance(alt, str) and "WhatsApp" in alt, locale


# ── (f) the route ──────────────────────────────────────────────────────────────────────────

_VALUE = "2@synthREF0001ab12cd34,AAAAnoiseKEYsynthetic=,BBBBidentityKEYsynthetic=,CCCCadvSECRETsynthetic="
_VALUE2 = "2@synthREF0002ab12cd34,AAAAnoiseKEYsynthetic=,BBBBidentityKEYsynthetic=,CCCCadvSECRETsynthetic="


class _FakeProc:
    pid = 4242
    returncode = None

    def __init__(self, values):
        self.stdout = [json.dumps({"type": "qr", "qr": v}) + "\n" for v in values]
        self.stderr = []
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """The real `_run_qr_login`, with Node replaced by a process that prints QR events."""
    import vaf.api.whatsapp_bridge as bridge
    import vaf.api.whatsapp_routes as routes
    import vaf.core.log_helper as log_helper
    import vaf.core.whatsapp_auth as wa
    from vaf.core import node_runtime

    lines = []
    procs = []
    monkeypatch.setattr(log_helper, "log_whatsapp_qr", lambda msg, *a, **k: lines.append(str(msg)))
    monkeypatch.setattr(wa, "get_whatsapp_auth_dir", lambda username: tmp_path / "wa")
    monkeypatch.setattr(node_runtime, "find", lambda name: "node")
    monkeypatch.setattr(bridge, "ensure_bridge_deps", lambda *a, **k: (True, ""))
    monkeypatch.setattr(routes, "_qr_state", {})
    monkeypatch.setattr(routes, "_qr_procs", {})
    monkeypatch.setattr(routes, "get_current_vaf_user", lambda request: {"username": "alice"})

    def run(values=(_VALUE,)):
        proc = _FakeProc(values)
        procs.append(proc)
        monkeypatch.setattr(bridge, "spawn_node_bridge", lambda *a, **k: proc)
        routes._run_qr_login("alice")
        return proc

    def poll():
        response = asyncio.run(routes.get_qr_code(object()))
        return response, json.loads(response.body)

    return type("Flow", (), {"run": staticmethod(run), "poll": staticmethod(poll),
                             "lines": lines, "routes": routes})


def test_the_answer_carries_the_png_and_not_the_value(flow):
    """MUTATION: answer `"qr": state["qr"]` again (and keep the value in the state) - red."""
    flow.run()
    response, body = flow.poll()
    assert body["status"] == "qr"
    assert "qr" not in body
    assert base64.b64decode(body["qr_png"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert _VALUE.encode() not in response.body
    assert _VALUE not in json.dumps(flow.routes._qr_state)


def test_the_png_comes_from_the_one_renderer_once_per_code(flow, monkeypatch):
    """MUTATION: draw inside `get_qr_code` on every poll (three draws), or with a local
    `qrcode.make` (no draw recorded) - red."""
    import vaf.api.auth_routes as auth_routes

    drawn = []
    real = auth_routes.qr_png_base64
    monkeypatch.setattr(auth_routes, "qr_png_base64", lambda text: drawn.append(text) or real(text))
    flow.run((_VALUE,))
    for _ in range(3):
        flow.poll()
    assert drawn == [_VALUE]
    flow.run((_VALUE, _VALUE2))
    flow.poll()
    assert drawn == [_VALUE, _VALUE, _VALUE2]


@pytest.mark.parametrize("state", ["connected", "error", "qr", "waiting", "raises"])
def test_every_answer_is_no_store(flow, monkeypatch, state):
    """MUTATION: drop the header on any one branch - red."""
    routes = flow.routes
    if state == "raises":
        def refuse(request):
            raise RuntimeError("no session")
        monkeypatch.setattr(routes, "get_current_vaf_user", refuse)
    else:
        routes._qr_state["alice"] = {
            "connected": {"connected": True, "phone": "+49123456789", "ts": 1},
            "error": {"error": "Node.js not found.", "ts": 0},
            "qr": {"qr_png": "iVBORw0KGgo=", "ts": 1},
            "waiting": {"message": "Preparing", "ts": 0},
        }[state]
    response, body = flow.poll()
    assert body["status"] == ("error" if state == "raises" else state)
    assert response.headers["cache-control"] == "no-store"


def test_the_value_is_never_logged(flow, caplog):
    """MUTATION: `log_whatsapp_qr(f"... {qr_data}")` in the qr branch - red."""
    caplog.set_level(logging.DEBUG)
    flow.run((_VALUE, _VALUE2))
    flow.poll()
    assert flow.lines, "the flow logs its steps; the collector saw nothing"
    for value in (_VALUE, _VALUE2):
        assert not any(value in line for line in flow.lines)
        assert value not in caplog.text
        assert not any(value in str(r.args) for r in caplog.records)


def test_a_code_that_cannot_be_drawn_ends_in_an_error_the_wizard_shows(flow, monkeypatch):
    """The flow runs in a thread: a failure there must end in a state the wizard shows, and the
    error must not quote the value. MUTATION: remove the try around the draw (the outer except
    stores the exception's text, which quotes the value) - red."""
    import vaf.api.auth_routes as auth_routes

    def cannot_draw(text):
        raise ValueError(f"data too long: {text}")
    monkeypatch.setattr(auth_routes, "qr_png_base64", cannot_draw)
    proc = flow.run()
    assert flow.routes._qr_state["alice"]["error"] == flow.routes._QR_DRAW_FAILED
    assert proc.terminated
    response, body = flow.poll()
    assert body == {"status": "error", "error": flow.routes._QR_DRAW_FAILED}
    assert _VALUE.encode() not in response.body
    assert not any(_VALUE in line for line in flow.lines)
