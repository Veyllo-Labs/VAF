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
 */

export function restartNoticeResolved(names: string[], runningByAccount: Record<string, boolean> | null): boolean {
    if (!runningByAccount || typeof runningByAccount !== 'object') return false;
    // Own keys only: an account named like an Object method ("constructor") is a name here.
    return names.every(name => !Object.prototype.hasOwnProperty.call(runningByAccount, name)
        || runningByAccount[name] === true);
}
