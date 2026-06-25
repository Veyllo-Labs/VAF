// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Built-in OAuth client IDs used by the backend when user hasn't configured their own.
 * Shown as empty in the UI so users aren't confused — they only see their own value if they enter one.
 */
export const BUILTIN_GOOGLE_CLIENT_ID = "827949283932-0l83lmf1ip671vqta9d6m9k2fa4gii42.apps.googleusercontent.com";

/** Return empty string if value is the built-in default (for display purposes). */
export function displayOAuthValue(value: string | null | undefined, builtin: string): string {
    const v = (value ?? "").trim();
    return v === builtin ? "" : v;
}
