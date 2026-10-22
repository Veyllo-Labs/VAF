// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * The script policy every page carries, in REPORT-ONLY mode for now: a browser runs everything
 * as before and reports what the policy would have blocked to /api/security/csp-report, which
 * writes it to the webui log. Enforcing it comes once those reports are measured clean.
 *
 * - script-src takes a fresh nonce per page (web/proxy.ts) and 'strict-dynamic', so scripts the
 *   framework loads with the nonce may load theirs. Next.js reads the nonce from this header on
 *   the request and puts it on its own scripts, which needs the page rendered per request
 *   (web/app/layout.tsx reads the headers). Development adds 'unsafe-eval', which React's
 *   debugging needs.
 * - style-src keeps 'unsafe-inline': React style attributes cannot carry a nonce, and a nonce in
 *   style-src would make a browser ignore 'unsafe-inline'.
 * - connect-src names the desktop window's socket and version poll, which go to the backend's
 *   own port on this machine (plain or TLS), not to the page's origin.
 * - No enforcing Content-Security-Policy is set here; the HTTPS proxy's `frame-ancestors 'none'`
 *   stays the enforcing one.
 */
export const CSP_REPORT_PATH = '/api/security/csp-report';

export function reportOnlyPolicy(nonce: string, development: boolean): string {
    const loopback = ['ws', 'wss', 'http', 'https']
        .flatMap(scheme => [`${scheme}://127.0.0.1:*`, `${scheme}://localhost:*`]);
    return [
        "default-src 'self'",
        `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ''}`,
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        `connect-src 'self' ${loopback.join(' ')}`,
        "media-src 'self' data: blob:",
        "worker-src 'self' blob:",
        "frame-src 'self' data: blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        `report-uri ${CSP_REPORT_PATH}`,
    ].join('; ');
}

/** A fresh, unguessable nonce for one page: a random UUID, base64. */
export function newNonce(): string {
    return btoa(crypto.randomUUID());
}
