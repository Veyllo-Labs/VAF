// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Which account a chat socket belongs to, decided from the socket itself.
 *
 * The server's first frame on every connect is `auth_state` with `accountId`, the account its
 * ownership checks use (vaf/core/web_server.py). A page remembers the account of its FIRST
 * connect. A later connect (a reconnect after a restart or a network drop) with the same
 * account goes on as before. With another account, or without an account after one that had
 * one, the page reloads before it applies any other frame: everything it shows and keeps
 * belongs to the first account, and a reload is the one way to drop all of it at once instead
 * of clearing state by hand, piece by piece. Both answers come from the same source (the
 * socket), so a reload cannot loop: after it, the new account is the first one.
 */

export type ConnectVerdict = 'adopt' | 'same' | 'reload';

/** `first`: the account of this page load's first connect, `undefined` before it. */
export function connectAccount(first: string | null | undefined,
                               incoming: unknown): ConnectVerdict {
    const id = typeof incoming === 'string' && incoming ? incoming : null;
    if (first === undefined) return 'adopt';
    return id === first ? 'same' : 'reload';
}
