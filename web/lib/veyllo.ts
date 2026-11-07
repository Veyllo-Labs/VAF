// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import { isSupportedLocale } from './languages';

/**
 * A page of veyllo.app in the person's language.
 *
 * veyllo.app serves every UI locale of VAF under `/<code>/` and English without a prefix
 * (`/en/dashboard` redirects to `/dashboard`). Only a UI locale has a prefix there. Anything
 * else (a browser tag such as `en-US`, an empty string) would name a page that does not
 * exist, so it gets the English address.
 */
export function veylloPageUrl(locale: string, page: string): string {
    return locale !== 'en' && isSupportedLocale(locale)
        ? `https://veyllo.app/${locale}/${page}`
        : `https://veyllo.app/${page}`;
}

/**
 * Where a person gets a Veyllo API key: the dashboard. A visitor who is not signed in lands
 * on the login of that language and comes back to the dashboard. One address for the
 * first-run wizard and Settings, which each linked to the start page before.
 */
export function veylloDashboardUrl(locale: string): string {
    return veylloPageUrl(locale, 'dashboard');
}

/** The Veyllo Relay's prices, which the Veyllo App window links instead of quoting amounts
 *  that would go stale in an installed VAF. */
export function veylloRelayPricesUrl(locale: string): string {
    return veylloPageUrl(locale, 'relay#prices');
}
