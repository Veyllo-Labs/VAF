// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * What a refused API call shows: the server's own `detail` when it is a sentence, else the
 * caller's fallback in the person's language.
 *
 * The server's refusals are FastAPI's `{"detail": "..."}`. A 422 carries a LIST of
 * validation errors under the same key, which is no sentence for a person, so it gets the
 * fallback, as does a body that is no JSON at all. Server texts are shown as sent (they are
 * English): the convention the WhatsApp window already follows at every refusal it shows.
 */

/** null when the call succeeded (`ok`), otherwise the text to show. */
export function refusalText(ok: boolean, body: unknown, fallback: string): string | null {
    if (ok) return null;
    const detail = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined;
    return typeof detail === 'string' && detail.trim() ? detail : fallback;
}
