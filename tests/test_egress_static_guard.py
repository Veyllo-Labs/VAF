# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""No raw HTTP call with a URL that someone else chose, in the code that fetches for the agent.

Nine fetch paths dialled whatever a model, a web page or a stored setting handed them, from
the VAF process, where a tokenless request from 127.0.0.1 is the owner. Each one was
converted onto vaf.network.egress; this guard keeps the next one from being written raw.

What it scans: the agent's tools (vaf/tools/**), the WebDAV client and the MCP lane - the
places where a URL arrives from outside. A raw requests/httpx/urllib/urllib3 call there is
allowed only when its URL is a literal (or an f-string whose host is literal), or when it
is listed in FIXED_URL with the reason its destination is VAF's own choice (a search
provider's API, the configured model endpoint, VAF's own backend). A client OBJECT
(requests.Session, httpx.Client, a urllib3 pool) is never assumed safe: its later calls
are invisible here, so it needs a FIXED_URL entry or egress_session(). An entry that no
longer matches anything fails too, so the list cannot rot.

NAMED BOUNDARY: the rest of vaf/ talks to fixed services (model providers, messenger
APIs, OAuth endpoints) and is not scanned; a URL taken from outside there would be a new
fetch path and belongs here.
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCANNED = sorted([*REPO.glob("vaf/tools/**/*.py"),
                  REPO / "vaf/cloud/nextcloud.py",
                  REPO / "vaf/core/mcp_remote.py",
                  REPO / "vaf/core/mcp_oauth.py",
                  REPO / "vaf/core/mcp_registry.py"])

_CALLS = {
    "requests": {"get", "post", "put", "delete", "head", "patch", "request", "Session", "session"},
    "httpx": {"get", "post", "put", "delete", "head", "patch", "request", "stream", "Client", "AsyncClient"},
    "urllib.request": {"urlopen", "Request"},
    "urllib3": {"PoolManager", "HTTPConnectionPool", "HTTPSConnectionPool", "ProxyManager", "request"},
}
_CLIENT_OBJECTS = {"Session", "session", "Client", "AsyncClient", "PoolManager",
                   "HTTPConnectionPool", "HTTPSConnectionPool", "ProxyManager"}
_LITERAL_HOST = re.compile(r"^https?://[^/{}\s]+(?:[/:?#]|$)")

# (file, enclosing function, call, URL argument as written) -> why it is VAF's own choice
FIXED_URL = {
    ("vaf/tools/search.py", "_search_google", "requests.get", "url"):
        "www.google.com, built from a literal base",
    ("vaf/tools/search.py", "_search_brave_api", "requests.get", "url"):
        "api.search.brave.com, built from a literal base",
    ("vaf/tools/search.py", "_search_google_cse", "requests.get", "url"):
        "www.googleapis.com, a literal",
    ("vaf/tools/search.py", "_search_duckduckgo", "requests.Session", ""):
        "a session used only for the DuckDuckGo HTML endpoint",
    ("vaf/tools/coder.py", "CodingAgentTool.run._llm_verify_call", "requests.post", "_llm_chat_url"):
        "the configured model endpoint (admin configuration)",
    ("vaf/tools/coder.py", "CodingAgentTool.run", "requests.get", "_llm_models_url"):
        "the configured model endpoint (admin configuration)",
    ("vaf/tools/coder.py", "CodingAgentTool.run", "requests.post", "_llm_chat_url"):
        "the configured model endpoint (admin configuration)",
}

# Fetch paths converted onto vaf.network.egress later in this round. EMPTY when done.
PENDING = {
    ("vaf/tools/webfetch.py", "WebFetchTool.run", "requests.get", "url"),
    ("vaf/tools/download_file.py", "DownloadFileTool.run", "requests.get", "url"),
    ("vaf/tools/github_tools.py", "GitHubGetFileTool.run", "urllib.request.urlopen", "download_url"),
    ("vaf/tools/github_tools.py", "GitHubGetFileStructureTool.run", "urllib.request.urlopen", "download_url"),
    ("vaf/tools/search.py", "WebSearchTool.run.fetch_text", "requests.get", "url"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool._format_search_results.fetch_text", "requests.get", "url"),
    ("vaf/tools/coder.py", "CodingAgentTool.run", "requests.get", "url"),
    ("vaf/tools/coder.py", "CodingAgentTool.run.fetch_summary", "requests.get", "url"),
    ("vaf/core/mcp_oauth.py", "_revoke", "httpx.post", "endpoint"),
    ("vaf/cloud/nextcloud.py", "NextcloudProvider._propfind", "requests.request", "url"),
    ("vaf/cloud/nextcloud.py", "NextcloudProvider.ensure_sync_folder", "requests.request", "url"),
    ("vaf/cloud/nextcloud.py", "NextcloudProvider.upload_file", "requests.put", "url"),
    ("vaf/cloud/nextcloud.py", "NextcloudProvider._ensure_parents", "requests.request", "url"),
    ("vaf/cloud/nextcloud.py", "NextcloudProvider.download_file", "requests.get", "url"),
    ("vaf/cloud/nextcloud.py", "NextcloudProvider.delete_file", "requests.delete", "url"),
}


def _aliases(tree):
    """Local name -> (module, member or None) for the HTTP modules this guard knows."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in _CALLS:
                    out[a.asname or a.name.split(".")[0]] = (a.name, None)
        elif isinstance(node, ast.ImportFrom) and node.module in _CALLS:
            for a in node.names:
                if a.name in _CALLS[node.module]:
                    out[a.asname or a.name] = (node.module, a.name)
    return out


def _qualname(node, parents):
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"


def _call_name(fn, aliases):
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        module, member = aliases.get(fn.value.id, (None, "x"))
        if module and member is None and fn.attr in _CALLS[module]:
            return f"{module}.{fn.attr}"
    if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Attribute)
            and isinstance(fn.value.value, ast.Name) and fn.value.value.id == "urllib"
            and fn.value.attr == "request" and fn.attr in _CALLS["urllib.request"]):
        return f"urllib.request.{fn.attr}"
    if isinstance(fn, ast.Name) and fn.id in aliases and aliases[fn.id][1]:
        return f"{aliases[fn.id][0]}.{aliases[fn.id][1]}"
    return None


def _url_argument(node, call):
    """The URL argument: requests.request(method, url) takes it second."""
    position = 1 if call.endswith(".request") else 0
    for kw in node.keywords:
        if kw.arg == "url":
            return kw.value
    return node.args[position] if len(node.args) > position else None


def _is_literal_destination(arg):
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return True
    if isinstance(arg, ast.JoinedStr) and arg.values and isinstance(arg.values[0], ast.Constant):
        return bool(_LITERAL_HOST.match(str(arg.values[0].value)))
    return False


def _open_sites():
    sites = set()
    for path in SCANNED:
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = _call_name(node.func, aliases)
            if not call:
                continue
            if call.rsplit(".", 1)[1] in _CLIENT_OBJECTS:
                sites.add((rel, _qualname(node, parents), call, ""))
                continue
            arg = _url_argument(node, call)
            if arg is None or _is_literal_destination(arg):
                continue
            sites.add((rel, _qualname(node, parents), call, ast.unparse(arg)))
    return sites


def test_no_fetch_path_dials_a_url_it_did_not_choose():
    """MUTATION: write `requests.get(url)` with a model's URL into any tool, and this names it."""
    unexplained = sorted(_open_sites() - set(FIXED_URL) - PENDING)
    assert unexplained == [], (
        "fetch through vaf.network.egress.egress_session(), or - if the destination is VAF's "
        f"own choice - add the site to FIXED_URL with the reason: {unexplained}")


def test_the_lists_name_only_sites_that_still_exist():
    """An entry for code that moved or was converted would silently allow its next occupant."""
    sites = _open_sites()
    stale = sorted((set(FIXED_URL) | PENDING) - sites)
    assert stale == [], f"remove these entries, they no longer match a raw call: {stale}"


def test_the_guard_sees_a_raw_fetch():
    """The scanner itself, on a synthetic tool: a guard that finds nothing proves nothing."""
    tree = ast.parse("import requests as rq\nfrom urllib.request import urlopen\n"
                     "def run(url):\n    rq.get(url)\n    urlopen(url)\n"
                     "    rq.get('https://api.example.com/x')\n    rq.request('GET', url)\n")
    aliases = _aliases(tree)
    found = [(_call_name(n.func, aliases), ast.unparse(_url_argument(n, _call_name(n.func, aliases))))
             for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n.func, aliases)]
    assert ("requests.get", "url") in found and ("urllib.request.urlopen", "url") in found
    assert ("requests.request", "url") in found
    literal = [a for c, a in found if a.startswith("'https://")]
    assert literal and _is_literal_destination(ast.parse(literal[0]).body[0].value)
