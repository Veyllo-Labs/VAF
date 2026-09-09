// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The undo and redo shortcuts as the person's keyboard names them: Command on a Mac,
// the Control key (by its localized name, "Strg" on a German keyboard) elsewhere. A
// label that says Ctrl+Y on a Mac names a key combination that does nothing there.
// Read in an effect, so the server render and the first client render agree.

import { useEffect, useState } from 'react';

export interface EditShortcuts { undo: string; redo: string }

export function editShortcuts(ctrlLabel: string, isMac: boolean): EditShortcuts {
    return isMac ? { undo: '\u2318Z', redo: '\u21e7\u2318Z' } : { undo: `${ctrlLabel}+Z`, redo: `${ctrlLabel}+Y` };
}

export function useEditShortcuts(ctrlLabel: string): EditShortcuts {
    const [isMac, setIsMac] = useState(false);
    useEffect(() => {
        const nav = typeof navigator !== 'undefined' ? navigator : null;
        setIsMac(!!nav && /Mac|iPhone|iPad|iPod/.test(nav.platform || nav.userAgent || ''));
    }, []);
    return editShortcuts(ctrlLabel, isMac);
}
