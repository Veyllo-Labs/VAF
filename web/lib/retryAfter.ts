// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * How long a held sign-in has to wait, from the server's `Retry-After`, as a phrase in the
 * reader's language for a `{when}` placeholder: "in 15 minutes", "in 2 hours". A second
 * factor held after repeated wrong codes waits up to a day, and "1440 minutes" says nothing.
 * `Intl.RelativeTimeFormat` brings the plural and the word order of every locale, so the
 * catalogues carry one sentence each instead of a plural table.
 */

/** Seconds from a response's `Retry-After` header; the window's default (15 minutes) when
 * it is missing or unreadable. */
export function retryAfterSeconds(res: Response): number {
    const value = Number(res.headers.get('Retry-After'));
    return Number.isFinite(value) && value > 0 ? value : 15 * 60;
}

/** "in 15 minutes" below an hour, "in 2 hours" above, in `locale`. */
export function retryWhen(seconds: number, locale: string): string {
    const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'always' });
    if (seconds < 3600) return rtf.format(Math.max(1, Math.ceil(seconds / 60)), 'minute');
    return rtf.format(Math.ceil(seconds / 3600), 'hour');
}
