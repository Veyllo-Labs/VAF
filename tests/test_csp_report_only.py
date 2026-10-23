# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A script policy that only reports, first.

Pages carried `frame-ancestors 'none'` and nothing about scripts. Every page now carries a
report-only Content-Security-Policy with a fresh nonce (web/lib/csp.ts, set in web/proxy.ts,
rendered per request by web/app/layout.tsx), and what it would have blocked lands in the webui
log through POST /api/security/csp-report: deduplicated, capped, without query strings. It
blocks nothing yet; the reports are what an enforcing policy will be measured against.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from auth_harness import Harness

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
_TS = WEB / "node_modules" / "typescript"


def _policy_from_node():
    """The REAL builder in web/lib/csp.ts, transpiled with the app's TypeScript and run in node."""
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const src = require('fs').readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2020}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const a = m.exports.newNonce(), b = m.exports.newNonce();"
        "process.stdout.write(JSON.stringify({a, b, prod: m.exports.reportOnlyPolicy(a, false),"
        " dev: m.exports.reportOnlyPolicy(a, true)}));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(WEB / "lib" / "csp.ts")],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_pages_get_a_report_only_policy():
    """The real builder, run in node: a fresh nonce per call, the directives and the report path.
    MUTATION: make the nonce once at module load in csp.ts - red."""
    got = _policy_from_node()
    assert got["a"] != got["b"] and len(got["a"]) >= 32
    policy = got["prod"]
    directives = dict(d.split(" ", 1) for d in policy.split("; "))
    assert directives["script-src"] == f"'self' 'nonce-{got['a']}' 'strict-dynamic'"
    assert "'unsafe-eval'" in got["dev"].split("script-src", 1)[1].split(";", 1)[0]
    assert directives["style-src"] == "'self' 'unsafe-inline'"
    assert directives["object-src"] == "'none'" and directives["frame-ancestors"] == "'none'"
    assert directives["report-uri"] == "/api/security/csp-report"
    assert "ws://127.0.0.1:*" in directives["connect-src"] and "wss://localhost:*" in directives["connect-src"]


def test_the_page_policy_is_wired_report_only():
    """Read from the sources, so it runs where web/node_modules is not installed: the header is
    report-only, the proxy makes a nonce per page and sets no enforcing policy, only pages get
    it, the layout renders per request with the nonce, and a ran-out cookie says why.
    MUTATION: name the header Content-Security-Policy; make the nonce once at module load in
    the proxy; drop the reason from the redirect - red."""
    csp = (WEB / "lib" / "csp.ts").read_text(encoding="utf-8")
    assert "'nonce-${nonce}'" in csp and "report-uri ${CSP_REPORT_PATH}" in csp
    assert "export function newNonce(): string {\n    return btoa(crypto.randomUUID());" in csp.replace("\r\n", "\n")
    proxy = (WEB / "proxy.ts").read_text(encoding="utf-8")
    assert "'Content-Security-Policy-Report-Only'" in proxy
    assert not re.search(r"['\"]Content-Security-Policy['\"]", proxy), "an enforcing policy from the page proxy"
    page = proxy[proxy.index("function pageWithPolicy"):]
    assert "const nonce = newNonce();" in page.split("}", 1)[0], "a nonce per page"
    assert not re.search(r"^const \w+ = newNonce\(\)", proxy, re.M), "a nonce made once at module load"
    guards = proxy[proxy.index("function applyAuthGuards"):proxy.index("function pageWithPolicy")]
    assert guards.index("if (isPublicAsset)") < guards.index("pageWithPolicy(request)"), \
        "/api, /_next and the static files return before a policy is set"
    redirect = guards[guards.index("if (!isLoginRoute && !isAuthenticated)"):]
    assert "if (hasStaleToken) url.searchParams.set('reason', 'ended');" in redirect.split("return res;", 1)[0]
    layout = (WEB / "app" / "layout.tsx").read_text(encoding="utf-8")
    assert "export default async function RootLayout" in layout
    assert '(await headers()).get("x-nonce")' in layout and "nonce={nonce}" in layout


@pytest.fixture
def sink(monkeypatch):
    """The report route behind the network layers, a signed-in browser, and the webui log."""
    import vaf.api.security_routes as security_routes
    import vaf.core.log_helper as log_helper
    lines = []
    monkeypatch.setattr(log_helper, "append_domain_log", lambda domain, message: lines.append((domain, message)))
    monkeypatch.setattr(security_routes, "_csp_day", {"day": "", "seen": set(), "dropped_said": False})
    h = Harness(monkeypatch)
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    app = h.app(extra_routes=[("/api/security/csp-report", security_routes.csp_report, ["POST"])])
    browser = h.client("192.168.94.1", app=app)
    h.sign_in(browser, "alice", "alice-password-1", None)
    return h, app, browser, lines


def _report(directive="script-src-elem", blocked="https://cdn.example/x.js", page="https://10.0.0.2:8443/"):
    return {"csp-report": {"effective-directive": directive, "blocked-uri": blocked, "document-uri": page,
                           "violated-directive": directive}}


def _post(client, body, **kw):
    return client.post("/api/security/csp-report", content=json.dumps(body),
                       headers={"Content-Type": "application/csp-report"}, **kw)


def test_the_report_sink_dedupes_caps_and_strips(sink):
    """MUTATION: no dedupe; keep the query; read a declared oversize body - red."""
    from vaf.api.security_routes import CSP_REPORT_DAILY_LINES
    h, app, browser, lines = sink
    for _ in range(3):
        assert _post(browser, _report()).status_code == 204
    assert lines == [("webui", '[CSP] reported by "alice": script-src-elem blocked '
                               "https://cdn.example/x.js on https://10.0.0.2:8443/")]
    _post(browser, _report(blocked="https://other.example/y.js?token=secret#frag",
                           page="https://10.0.0.2:8443/chat?id=1"))
    assert lines[-1] == ("webui", '[CSP] reported by "alice": script-src-elem blocked '
                                  "https://other.example/y.js on https://10.0.0.2:8443/chat")
    assert "secret" not in str(lines)
    # The Reporting API's shape is read too.
    _post(browser, [{"type": "csp-violation", "body": {"effectiveDirective": "img-src",
                                                         "blockedURL": "https://img.example/a.png",
                                                         "documentURL": "https://10.0.0.2:8443/"}}])
    assert lines[-1][1] == ('[CSP] reported by "alice": img-src blocked https://img.example/a.png '
                            "on https://10.0.0.2:8443/")
    # A body over 16 KiB, declared or sent in pieces, is refused and not written.
    before = len(lines)
    big = _report(blocked="https://cdn.example/" + "x" * 17 * 1024)
    assert _post(browser, big).status_code == 413
    pieces = iter([json.dumps(big).encode()[i:i + 4096] for i in range(0, 18 * 1024, 4096)])
    r = browser.post("/api/security/csp-report", content=pieces,
                     headers={"Content-Type": "application/csp-report"})
    assert r.status_code == 413 and len(lines) == before
    # The day's cap: then one line that says so, then nothing.
    n = 0
    while len(lines) < CSP_REPORT_DAILY_LINES:
        n += 1
        _post(browser, _report(blocked=f"https://cdn.example/{n}.js"))
    for k in range(3):
        _post(browser, _report(blocked=f"https://late.example/{k}.js"))
    assert len(lines) == CSP_REPORT_DAILY_LINES + 1
    assert "further CSP reports today are dropped" in lines[-1][1]


def test_a_stranger_cannot_write_reports(sink):
    h, app, _browser, lines = sink
    r = _post(h.client("192.168.94.9", app=app), _report())
    assert r.status_code == 401 and lines == []


def test_a_report_is_one_log_line(sink):
    """A line break in a field cannot forge a second line."""
    _h, _app, browser, lines = sink
    _post(browser, _report(blocked="https://cdn.example/a.js\n2026-01-01 [CSP] forged", directive="script-src\nx"))
    assert len(lines) == 1 and "\n" not in lines[0][1]


def test_a_report_keeps_no_credentials_and_names_who_sent_it(sink):
    """A URL in a report can carry `user:password@`; the line keeps origin and path only. Any
    signed-in account can write lines, so each names its reporter, and the machine itself is
    "this machine". MUTATION: rebuild the origin from netloc; drop the reporter - red."""
    h, app, browser, lines = sink
    _post(browser, _report(blocked="https://user:s3cret@evil.example:8080/p?q=1#f"))
    assert lines[-1][1] == ('[CSP] reported by "alice": script-src-elem blocked '
                            "https://evil.example:8080/p on https://10.0.0.2:8443/")
    assert "s3cret" not in str(lines)
    _post(h.client(None, app=app), _report())
    assert lines[-1][1].startswith("[CSP] reported by this machine: script-src-elem blocked")


def test_a_report_cannot_pose_as_another_reporter(sink):
    """The reporter comes first and the sender's fields hold no space, so text in a reported
    URL cannot read as the reporter. MUTATION: the reporter at the end again; keep spaces in
    a field - red."""
    _h, _app, browser, lines = sink
    _post(browser, _report(blocked="https://x.example/ (reported by this machine)",
                           directive="script-src-elem  'unsafe-inline'"))
    assert lines[-1][1] == ('[CSP] reported by "alice": script-src-elem blocked '
                            "https://x.example/%20(reported%20by%20this%20machine) on "
                            "https://10.0.0.2:8443/")
    assert lines[-1][1].count(" ") == 8


def test_a_url_the_parser_refuses_is_written_as_unknown_and_the_batch_goes_on(sink):
    """urlsplit raises on an unbalanced or non-IP bracketed host; one such field must not turn
    the route into a 500 and drop the rest of the batch. MUTATION: drop the ValueError guard
    around the parse - red."""
    _h, _app, browser, lines = sink
    r = _post(browser, [_report(blocked="https://[abc/x.js"), _report(page="//[x"),
                        _report(blocked="https://ok.example/y.js")])
    assert r.status_code == 204
    assert lines[0][1] == ('[CSP] reported by "alice": script-src-elem blocked - '
                           "on https://10.0.0.2:8443/")
    assert lines[-1][1].endswith("blocked https://ok.example/y.js on https://10.0.0.2:8443/")
