// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * The password rule the pages check before they send anything. The server checks it again,
 * and only the server knows the list of common passwords (`vaf/auth/user_admin.py`
 * `validate_password`); a page words its refusal `password_common` from the catalogue.
 * tests/test_password_policy_sync.py fails when this number and `MIN_PASSWORD_LEN` differ.
 */
export const MIN_PASSWORD_LEN = 12;
