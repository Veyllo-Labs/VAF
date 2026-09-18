'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// What the agent prepared and nobody has sent yet. A send the agent makes on the person's
// own chat turn is parked instead of delivered (vaf/core/outbound_hold.py), and this is where
// the person decides: read it, send it, or drop it. Live incident: a mail was asked for in the
// chat and was gone in the same turn, to a real external address.
//
// It sits IN the conversation, as the last row under the answer that produced it, because it
// is the agent's own output waiting for a word: it belongs to that one answer, in that one
// chat, and a banner over the header would read as a system alert about the whole app. It
// refetches on the `outbound_held` event and on `inbox_changed` rather than on a timer. Every
// string comes from the `outbox` catalogue block; nothing here is hardcoded copy. A send that
// did not say it left keeps the draft in the list with its reason, because a bridge that is
// down must not consume the message.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Mail, MessageCircle, Send, Trash2 } from 'lucide-react';

//: How long the send button stays locked after a draft appears, so the person reads it
//: before they can send it. Discard is never locked.
const SEND_DELAY_SECONDS = 3;

export type HeldSendRow = {
    kind: 'mail' | 'call';
    id: number;
    channel: string;
    tool: string;
    recipient: string;
    subject: string;
    preview: string;
    created_ts: number;
    /** 'held' while it waits, 'failed' when the last attempt answered and the message did not
     *  leave, 'ambiguous' when a worker died mid-send and nobody knows whether it arrived. */
    state?: string;
    /** Why the last attempt did not leave. Shown on the row, so nobody sends again blind. */
    error?: string;
};

export function HeldSendCard({ apiBase, version, sessionId }: { apiBase: string; version: number; sessionId: string }) {
    const t = useTranslations('outbox');
    const [rows, setRows] = useState<HeldSendRow[]>([]);
    // Keyed by LANE and id: the two lanes number independently (mail op ids, parked-call
    // rowids), so a bare id would disable a mail draft's buttons while a call with the same
    // number is being sent.
    const [busy, setBusy] = useState<string | null>(null);
    const [note, setNote] = useState('');
    // The send button is dead for the first seconds a draft is on screen, and says how long.
    // Not an undo after the click: the point is that nobody fires off a message they have not
    // read, so the pause sits BEFORE the decision. Discard stays available the whole time -
    // throwing away something unread costs nothing.
    const [now, setNow] = useState(() => 0);
    const seenAtRef = useRef<Map<string, number>>(new Map());
    const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

    useEffect(() => () => { if (tickRef.current) clearInterval(tickRef.current); }, []);

    const load = useCallback(async () => {
        // Only this conversation's drafts. A message the agent is writing in one chat must not
        // appear in another, and switching chats mid-draft is the ordinary case: the chat it
        // belongs to carries the red dot in the sidebar until the person goes back to it.
        if (!sessionId) { setRows([]); return; }
        try {
            const res = await fetch(`${apiBase}/api/outbox?session_id=${encodeURIComponent(sessionId)}`,
                { credentials: 'include' });
            if (!res.ok) return;
            const data = await res.json().catch(() => ({}));
            setRows(Array.isArray(data.rows) ? data.rows : []);
        } catch { /* a listing that cannot be fetched shows nothing, never an error banner */ }
    }, [apiBase, sessionId]);

    useEffect(() => { setRows([]); setNote(''); }, [sessionId]);
    useEffect(() => { void load(); }, [load, version]);

    // A quarter-second tick, and only while a row is still locked: the moment the last
    // reading pause has run out the interval stops itself, so a card sitting on screen carries
    // no timer. Without that stop it kept re-rendering the chat four times a second for as
    // long as a draft was visible.
    useEffect(() => {
        const stamp = Date.now();
        rows.forEach(r => {
            const key = `${r.kind}-${r.id}`;
            if (!seenAtRef.current.has(key)) seenAtRef.current.set(key, stamp);
        });
        setNow(Date.now());
        if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
        if (!rows.length) return;
        const stopAt = Math.max(...rows.map(r => (seenAtRef.current.get(`${r.kind}-${r.id}`) ?? stamp)
            + SEND_DELAY_SECONDS * 1000));
        if (stopAt <= stamp) return;
        tickRef.current = setInterval(() => {
            const t = Date.now();
            setNow(t);
            if (t >= stopAt && tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
        }, 250);
        return () => { if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; } };
    }, [rows]);

    const lockedFor = (row: HeldSendRow) => {
        const seen = seenAtRef.current.get(`${row.kind}-${row.id}`);
        if (!seen) return SEND_DELAY_SECONDS;
        return Math.max(0, Math.ceil((seen + SEND_DELAY_SECONDS * 1000 - (now || Date.now())) / 1000));
    };

    const act = async (row: HeldSendRow, action: 'send' | 'discard') => {
        setBusy(`${row.kind}-${row.id}`); setNote('');
        try {
            const url = `${apiBase}/api/outbox/${row.kind}/${row.id}${action === 'send' ? '/send' : ''}`;
            const res = await fetch(url, {
                method: action === 'send' ? 'POST' : 'DELETE', credentials: 'include',
            });
            const data = res.ok ? await res.json().catch(() => ({})) : {};
            if (res.ok && (action === 'discard' || data.ok)) {
                setRows(prev => prev.filter(r => !(r.id === row.id && r.kind === row.kind)));
            } else {
                setNote(t('failed', { error: String(data.error || res.status) }));
                await load();
            }
        } catch {
            setNote(t('failed', { error: '' }));
        } finally {
            setBusy(null);
        }
    };

    // Nothing waiting, nothing rendered: not even the row wrapper, or every conversation would
    // end in an empty padded row.
    if (!rows.length) return null;

    return (
        /* The bot row's own geometry, copied rather than approximated: the row, the 85% block,
           the avatar gutter (w-9 plus the row gap) as an empty spacer, then the content. The row
           centers its child, so a card without the block starts left of the whole column, and one
           without the spacer starts under the avatar instead of under the text. */
        <div className="flex gap-4 pt-4 vaf-msg-row">
            <div className="w-full max-w-[85%] max-md:max-w-full flex gap-4 max-md:gap-2">
                <div className="w-9 shrink-0" aria-hidden="true" />
                <div className="flex flex-col gap-3 flex-1 min-w-0">
            {rows.slice(0, 3).map((r) => {
              const locked = lockedFor(r);
              return (
                <div key={`${r.kind}-${r.id}`}
                    className="relative max-w-2xl min-w-0 rounded-2xl border border-gray-200 dark:border-[#2e2e2e] bg-gray-50 dark:bg-[#1f1f1f] px-4 py-3 flex flex-col gap-2">
                    {/* The rim that says "read me", while the send button is still locked. Its own
                        element with a fixed shadow, breathing in OPACITY only: animating the card's
                        own border or shadow would repaint the card on every frame (the repaint rule
                        in globals.css, written after a measured GPU leak). */}
                    {locked > 0 && (
                        <span aria-hidden="true"
                            className="vaf-draft-rim pointer-events-none absolute inset-0 rounded-2xl" />
                    )}
                    <div className="flex items-center gap-2 text-gray-800 dark:text-gray-200">
                        {r.channel === 'mail' ? <Mail className="w-4 h-4 shrink-0" /> : <MessageCircle className="w-4 h-4 shrink-0" />}
                        <span className="text-sm font-medium">{t('title')}</span>
                    </div>
                    <p className="text-xs text-gray-500 dark:text-[#9a9a9a]">
                        {t('to', { recipient: r.recipient || t('noRecipient') })}
                        {r.subject ? ` - ${r.subject}` : ''}
                    </p>
                    <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words">{r.preview}</p>
                    {/* A draft whose last attempt did not leave says so on the row, with the
                        reason: the person decides whether to try again or drop it, and a retry
                        with no explanation is a person guessing. */}
                    {r.state === 'failed' && (
                        <p className="text-xs text-red-600 dark:text-red-400">{t('failed', { error: r.error || '' })}</p>
                    )}
                    {/* Interrupted mid-send: it may have arrived. The person gets the reason and
                        the Discard button, and no Send, because a second click could be a second
                        delivery and nothing here can tell. */}
                    {r.state === 'ambiguous' && (
                        <p className="text-xs text-amber-700 dark:text-[#e0b866]">{t('ambiguous')}</p>
                    )}
                    <div className="flex gap-2 pt-1">
                        {/* The house's own primary button (bg-gray-900 light, #e6e6e6 dark): the
                            light tone is spelled out, because the bare white token under a dark
                            variant folds to the DARK surface colour and would hide the label (a
                            guard test pins that repo-wide). While the reading pause runs the
                            button is dead and says why; nothing is ever sent by the clock. */}
                        {r.state !== 'ambiguous' && (
                        <button type="button" disabled={busy === `${r.kind}-${r.id}` || locked > 0}
                            onClick={() => act(r, 'send')}
                            className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-900 text-white hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none disabled:opacity-50 inline-flex items-center gap-1.5 min-w-[7.5rem] justify-center">
                            {locked > 0
                                ? <>{t('countdown', { seconds: locked })}</>
                                : <><Send className="w-3.5 h-3.5" />{t('send')}</>}
                        </button>
                        )}
                        <button type="button" disabled={busy === `${r.kind}-${r.id}`} onClick={() => act(r, 'discard')}
                            className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-200 dark:bg-[#2e2e2e] text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-[#3a3a3a] disabled:opacity-50 inline-flex items-center gap-1.5">
                            <Trash2 className="w-3.5 h-3.5" />{t('discard')}
                        </button>
                    </div>
                    {note && <span className="text-xs text-red-600 dark:text-red-400">{note}</span>}
                </div>
              );
            })}
            {rows.length > 3 && (
                <span className="text-xs text-gray-500 dark:text-[#9a9a9a]">{t('more', { count: rows.length - 3 })}</span>
            )}
                </div>
            </div>
        </div>
    );
}

export default HeldSendCard;
