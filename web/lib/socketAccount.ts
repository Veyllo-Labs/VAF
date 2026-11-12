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
 *
 * One more case: the page load's first socket closed before its `auth_state` arrived, so
 * there is no account to compare a later socket with, and the account may have changed in
 * between. Such a later socket reloads the page once; a flag in sessionStorage (it survives
 * the reload) keeps that from repeating, and every adopted account clears it. Once a page
 * has an account to compare with, the flag holds back no reload for a change of account. It
 * leaves one window: in the page load right after such a reload, if the first socket again
 * closes before its greeting, a later socket's account is taken without a comparison. When
 * the flag cannot be written (storage blocked), there is no reload for a missing baseline at
 * all (a reload without the flag could repeat forever); a reload for a KNOWN other account
 * never depends on it.
 */

export type ConnectVerdict = 'adopt' | 'same' | 'reload';

export type ConnectContext = {
    /** This is the first socket this page load opened. */
    firstSocket: boolean;
    /** This tab already reloaded once because a socket came without a baseline. */
    reloadedForMissingBaseline: boolean;
};

/** The sessionStorage key of that one reload. */
export const MISSING_BASELINE_RELOAD_KEY = 'vaf_socket_baseline_reload';

/** Write the one-reload flag; false when storage refuses it (then the page must not reload). */
export function markMissingBaselineReload(storage: { setItem: (k: string, v: string) => void } | null | undefined): boolean {
    try {
        if (!storage) return false;
        storage.setItem(MISSING_BASELINE_RELOAD_KEY, '1');
        return true;
    } catch {
        return false;
    }
}

/** `first`: the account of this page load's first `auth_state`, `undefined` before one came. */
export function connectAccount(first: string | null | undefined, incoming: unknown,
                               ctx: ConnectContext = { firstSocket: true, reloadedForMissingBaseline: false }): ConnectVerdict {
    const id = typeof incoming === 'string' && incoming ? incoming : null;
    if (first === undefined) {
        return (ctx.firstSocket || ctx.reloadedForMissingBaseline) ? 'adopt' : 'reload';
    }
    return id === first ? 'same' : 'reload';
}
