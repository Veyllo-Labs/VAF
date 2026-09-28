// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import DOMPurify, { type Config } from 'dompurify';

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
    FORBID_ATTR: ['style', 'class', 'id', 'srcset'],
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
