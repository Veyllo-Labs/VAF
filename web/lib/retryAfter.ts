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
    const phrase = seconds < 3600
        ? rtf.format(Math.max(1, Math.ceil(seconds / 60)), 'minute')
        : rtf.format(Math.ceil(seconds / 3600), 'hour');
    // CLDR puts a space between the number and the counter in Japanese ("15 minutes later"
    // comes out as digits, space, kanji); Japanese sets them together, as the catalogue's own
    // style does. Korean keeps its space (it is correct there), Chinese has none.
    return locale.toLowerCase().startsWith('ja')
        ? phrase.replace(/(\d)\s+(?=[\u3040-\u30ff\u4e00-\u9fff])/g, '$1')
        : phrase;
}
