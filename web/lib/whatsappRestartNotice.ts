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
 * Only a map from a read that STARTED after the restart answered counts: `dataLoad` is the
 * start number of the read whose data is shown, `restartLoad` the number of reads started when
 * the answer arrived. A read already under way during the restart, or the map kept from before
 * it when the read after it failed, can lack an account the restart was meant for, or still
 * show it running from before, and would read as resolved.
 */

export function restartNoticeResolved(names: string[], runningByAccount: Record<string, boolean> | null,
                                      dataLoad: number, restartLoad: number): boolean {
    if (!(dataLoad > restartLoad)) return false;
    if (!runningByAccount || typeof runningByAccount !== 'object') return false;
    // Own keys only: an account named like an Object method ("constructor") is a name here.
    return names.every(name => !Object.prototype.hasOwnProperty.call(runningByAccount, name)
        || runningByAccount[name] === true);
}
