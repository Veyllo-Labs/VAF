// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// Every draft ONE chat produced, in every state, for the cards in that conversation
// (`GET /api/outbox?session_id=&settled=true`, vaf/core/outbound_hold.chat_drafts). A decided
// draft stays listed: its card turns into the one-line record of what happened to it, under
// the turn that wrote it. Fetched on the `outbound_held` / `inbox_changed` signals (the
// `version` the page bumps) and on a chat change, never on a timer.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { HeldSendRow } from './HeldSendCard';

export function useChatDrafts(apiBase: string, sessionId: string, version: number) {
    const [rows, setRows] = useState<HeldSendRow[]>([]);
    // The chat on screen, readable from inside an in-flight fetch: switching chats while a
    // listing is still on the wire is the ordinary case, and the late answer would otherwise
    // put the previous chat's drafts under the new chat's turns.
    const sessionRef = useRef(sessionId);

    const load = useCallback(async () => {
        // Only this conversation's drafts. A message the agent is writing in one chat must not
        // appear in another; the chat it belongs to carries the red dot in the sidebar.
        if (!sessionId) { setRows([]); return; }
        const asked = sessionId;
        try {
            const res = await fetch(`${apiBase}/api/outbox?session_id=${encodeURIComponent(asked)}&settled=true`,
                { credentials: 'include' });
            if (!res.ok) return;
            const data = await res.json().catch(() => ({}));
            if (sessionRef.current !== asked) return;
            setRows(Array.isArray(data.rows) ? data.rows : []);
        } catch { /* a listing that cannot be fetched shows nothing, never an error banner */ }
    }, [apiBase, sessionId]);

    useEffect(() => { sessionRef.current = sessionId; setRows([]); }, [sessionId]);
    useEffect(() => { void load(); }, [load, version]);

    const byRef = useMemo(() => new Map(rows.map(r => [r.ref, r])), [rows]);
    return { rows, byRef, reload: load };
}
