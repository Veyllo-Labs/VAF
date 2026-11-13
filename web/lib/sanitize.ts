// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import DOMPurify, { type Config, type UponSanitizeAttributeHookEvent } from 'dompurify';
import { ownImageSrc } from '@/lib/markdownImages';

/**
 * What survives of untrusted markup placed into the app's OWN document. Structure and text
 * only: no script, frames, forms or embedded media, and no style, class or id - in the app's
 * document those would let the markup draw its own interface over the real one (a Tailwind
 * class is enough for a full-screen overlay) or shadow the app's globals by id.
 */
const UNTRUSTED_FRAGMENT: Config = {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'option', 'img',
        'svg', 'math', 'iframe', 'object', 'embed', 'link', 'meta', 'base', 'video', 'audio'],
    // `background` on a table, row or cell is a picture the browser fetches (measured in
    // Chromium); DOMPurify allows the attribute by default.
    FORBID_ATTR: ['style', 'class', 'id', 'srcset', 'background'],
};

/**
 * Clean HTML a model wrote or a file carried before it goes into the app's own document
 * (dangerouslySetInnerHTML, a same-origin frame). A handler attribute there runs AS the
 * viewer, with the session. Fails closed: where DOMPurify cannot work (server rendering, no
 * DOM), nothing is returned rather than the input.
 */
export function sanitizeUntrustedHtml(html: string): string {
    if (!html) return '';
    if (!DOMPurify.isSupported) return '';
    return String(DOMPurify.sanitize(html, UNTRUSTED_FRAGMENT));
}

/**
 * A copy of the user's own document placed into the app's document to be rendered (the PDF
 * export, which html2canvas cannot render from the editor frame). Unlike a fragment, its
 * formatting has to survive: the editor writes alignment and highlights as inline style and
 * keeps images. What cannot survive is anything that reaches the app: id and class (the app's
 * CSS and globals) and style/link elements. The caller also gives the copy's container
 * `contain: layout paint`, so a position:fixed element in the file stays inside it instead of
 * drawing over the app (measured in Chromium).
 */
const DOCUMENT_COPY: Config = {
    FORBID_TAGS: ['style', 'link', 'base', 'meta'],
    FORBID_ATTR: ['class', 'id', 'background'],
};

/**
 * The copy of a document whose frame loads pictures only from this server's file lane (a
 * Markdown file, `web/lib/markdownImages.ts`): placed into the app's document, it would fetch
 * what its frame refused. So it keeps only what fetches nothing or fetches an own file.
 */
const OWN_SOURCES_COPY: Config = {
    FORBID_TAGS: ['style', 'link', 'base', 'meta', 'iframe', 'frame', 'frameset', 'object', 'embed',
        'applet', 'portal'],
    FORBID_ATTR: ['class', 'id', 'background'],
};

// Attributes whose value the browser fetches, whatever it is.
const FETCHING_ATTRS = new Set(['srcset', 'imagesrcset', 'poster', 'background', 'data', 'codebase',
    'lowsrc', 'dynsrc', 'longdesc', 'ping', 'action', 'formaction', 'manifest']);

/**
 * Whether an attribute may stay in an own-sources copy: a `src` only when it is the file lane
 * (`ownImageSrc`), a link target only on a link (it navigates on a click, it does not fetch),
 * inline style only without a URL or a CSS escape that could spell one, and no other
 * attribute that fetches. Pure, so it is tested without a DOM.
 */
export function keepsOwnSourcesOnly(tagName: string, attrName: string, value: string): boolean {
    const tag = tagName.toLowerCase();
    const attr = attrName.toLowerCase();
    if (FETCHING_ATTRS.has(attr)) return false;
    if (attr === 'src') return ownImageSrc(value) !== null;
    if (attr === 'href' || attr === 'xlink:href') return tag === 'a';
    if (attr === 'style') return !/url\s*\(|image-set|image\(|@import|\\/i.test(value);
    return true;
}

function dropForeignSource(node: Element, event: UponSanitizeAttributeHookEvent): void {
    if (!keepsOwnSourcesOnly(node.nodeName, event.attrName, event.attrValue)) event.keepAttr = false;
}

export function sanitizeDocumentCopy(html: string, options: { ownSourcesOnly?: boolean } = {}): string {
    if (!html) return '';
    if (!DOMPurify.isSupported) return '';
    if (!options.ownSourcesOnly) return String(DOMPurify.sanitize(html, DOCUMENT_COPY));
    // The hook is the shared instance's; it lives exactly as long as this one call.
    DOMPurify.addHook('uponSanitizeAttribute', dropForeignSource);
    try {
        return String(DOMPurify.sanitize(html, OWN_SOURCES_COPY));
    } finally {
        DOMPurify.removeHook('uponSanitizeAttribute', dropForeignSource);
    }
}
