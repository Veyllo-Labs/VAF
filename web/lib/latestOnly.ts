// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Tells the latest of overlapping calls from an older one.
 *
 * A switch flipped on and then off at once sends two requests, and their answers can arrive
 * in either order. Applied as they arrive, the older answer can be the last word and show
 * a state the server no longer has. Each call takes a token with `begin()`; its answer is
 * applied only while `isLatest(token)` holds, so the newest call decides what is shown.
 */

export interface LatestGate {
    begin(): number;
    isLatest(token: number): boolean;
}

export function latestGate(): LatestGate {
    let current = 0;
    return {
        begin: () => ++current,
        isLatest: (token: number) => token === current,
    };
}
