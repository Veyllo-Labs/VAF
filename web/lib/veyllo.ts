// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import { isSupportedLocale } from './languages';

/**
 * Where a person gets a Veyllo API key: the dashboard on veyllo.app, in their language.
 *
 * veyllo.app serves every UI locale of VAF under `/<code>/` and English without a prefix
 * (`/en/dashboard` redirects to `/dashboard`); a visitor who is not signed in lands on the
 * login of that language and comes back to the dashboard. One address for the first-run
 * wizard and Settings, which each linked to the start page before.
 *
 * Only a UI locale has a prefix there. Anything else (a browser tag such as `en-US`, an
 * empty string) would name a page that does not exist, so it gets the English address.
 */
export function veylloDashboardUrl(locale: string): string {
    return locale !== 'en' && isSupportedLocale(locale)
        ? `https://veyllo.app/${locale}/dashboard`
        : 'https://veyllo.app/dashboard';
}
