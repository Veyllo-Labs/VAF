// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * When an admin's "Restart WhatsApp" notice may go away by itself.
 *
 * The notice names the accounts the restart could not start because their previous process
 * had not ended. The dashboard sends an admin `running_by_account`, `{name: running}` over
 * every account that is linked and switched on (null for everybody else). The notice has done
 * its job once every named account runs again, or is no longer in the map at all: switched
 * off or unlinked meanwhile, so there is nothing left to start.
 *
 * Only a map from a load that succeeded after the restart counts (`dataLoad` greater than
 * `restartLoad`, the window's count of successful loads then): when the load after the
 * restart fails, the window keeps the map from before it, where an account the restart was
 * meant for can be missing and would read as resolved.
 */

export function restartNoticeResolved(names: string[], runningByAccount: Record<string, boolean> | null,
                                      dataLoad: number, restartLoad: number): boolean {
    if (!(dataLoad > restartLoad)) return false;
    if (!runningByAccount || typeof runningByAccount !== 'object') return false;
    // Own keys only: an account named like an Object method ("constructor") is a name here.
    return names.every(name => !Object.prototype.hasOwnProperty.call(runningByAccount, name)
        || runningByAccount[name] === true);
}
