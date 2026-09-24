'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// What the agent prepared and nobody has sent yet. A send the agent makes on the person's own
// chat turn is parked instead of delivered, and the turn ENDS there (vaf/core/outbound_hold.py):
// this card is that turn's answer. The person reads it, clicks into the text to change it, and
// sends or drops it. Send wakes the chat so the agent carries on; Discard is the end of it.
// Live incident behind the hold: a mail was asked for in the chat and was gone in the same
// turn, to a real external address.
//
// Each card sits UNDER THE TURN THAT WROTE IT, lined up with that turn's tool windows, not at
// the bottom of the conversation: the page finds the turn by the draft's ref in the tool
// result (`draftRefOf`). After the decision it stays there as one line, the record of what
// became of the draft - sent, discarded, or replaced by a newer version. Every string comes
// from the `outbox` catalogue block. A send that did not say it left keeps the draft waiting
// with its reason, because a bridge that is down must not consume the message.

import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Check, ChevronRight, Mail, MessageCircle, RefreshCw, Send, Trash2 } from 'lucide-react';
import { cn } from '@/lib/utils';

//: How long the send button stays locked after a draft appears, so the person reads it
//: before they can send it. Discard is never locked.
const SEND_DELAY_SECONDS = 3;

//: A text longer than this many lines (or characters) is folded until asked for, so a long
//: mail does not push the conversation off the screen. Clicking into it unfolds it anyway.
const FOLD_LINES = 8;
const FOLD_CHARS = 600;

const WAITING = new Set(['held', 'failed', 'ambiguous']);

// When each draft was first on screen, for the reading pause. Kept outside the card: the card
// can remount (it moves from the chat's last row into its turn once the tool result arrives,
// and a chat switch remounts everything), and a pause that restarted on every mount would lock
// the button again for a draft the person has been reading for a while.
const FIRST_SEEN = new Map<string, number>();

export type HeldSendRow = {
    kind: 'mail' | 'call';
    id: number;
    /** `mail:12` / `call:7`: the lane and the id together, because the two number independently. */
    ref: string;
    channel: string;
    tool: string;
    recipient: string;
    /** The name the person knows the recipient by (contact book, or a mail's display name). */
    recipient_name?: string;
    /** The other addresses a mail goes to. Shown because approving is approving what leaves:
     *  a card with the To line alone let a Bcc go out unseen. */
    cc?: string;
    bcc?: string;
    /** The names of the files that leave with it. */
    attachments?: string[];
    subject: string;
    preview: string;
    created_ts: number;
    decided_ts?: number;
    /** Waiting: 'held', 'failed' (the last attempt answered and the message did not leave),
     *  'ambiguous' (a worker died mid-send, it may have arrived). Decided: 'sent', 'sending',
     *  'discarded', 'replaced' (a newer draft to the same person took its place). */
    state?: string;
    /** Why the last attempt did not leave. Shown on the row, so nobody sends again blind. */
    error?: string;
    /** The person changed the words before sending. */
    edited?: boolean;
    replaced_by?: string;
};

export const isWaitingDraft = (row: HeldSendRow) => WAITING.has(row.state || 'held');

type T = ReturnType<typeof useTranslations>;

function addressee(row: HeldSendRow, t: T): string {
    return (row.recipient_name || '').trim() || row.recipient || t('noRecipient');
}

// One title per channel rather than a `{channel}` placeholder: the words around a channel name
// are grammar (a Korean particle follows the name's last sound, Thai spaces a Latin name but
// not a Thai one), and only a whole sentence per channel gets that right in every language.
function titleOf(row: HeldSendRow, t: T): string {
    const recipient = addressee(row, t);
    if (row.channel === 'mail') return t('titleMail', { recipient });
    if (row.channel === 'whatsapp') return t('titleWhatsapp', { recipient });
    return t('titleOther', { channel: row.channel, recipient });
}

type CardProps = {
    row: HeldSendRow;
    apiBase: string;
    /** Refetch the chat's drafts: the card never guesses the state it just caused. */
    onChanged: () => Promise<void> | void;
    /** The chat's own time format, for a decided draft's line. Seconds since the epoch. */
    formatTime: (ts: number) => string;
};

export function DraftCard(props: CardProps) {
    return isWaitingDraft(props.row) ? <WaitingDraft {...props} /> : <DecidedDraft {...props} />;
}

function WaitingDraft({ row, apiBase, onChanged }: CardProps) {
    const t = useTranslations('outbox');
    const isMail = row.kind === 'mail';
    // A send interrupted mid-flight may have arrived: it can only be dropped, so it is not
    // editable either - new words on it would be words nobody can send.
    const editable = row.state !== 'ambiguous';
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<string | null>(null);
    const [editing, setEditing] = useState<null | 'body' | 'subject'>(null);
    const [body, setBody] = useState(row.preview || '');
    const [subject, setSubject] = useState(row.subject || '');
    const [unfolded, setUnfolded] = useState(false);
    // What the store holds, as far as this card knows. An edit is saved when the person leaves
    // the text (or presses Send) and only when it differs from this.
    const saved = useRef({ body: row.preview || '', subject: row.subject || '' });
    const current = useRef({ body, subject });
    current.current = { body, subject };
    const saving = useRef<Promise<boolean> | null>(null);
    const editRef = useRef<HTMLDivElement | null>(null);
    const areaRef = useRef<HTMLTextAreaElement | null>(null);
    const subjectRef = useRef<HTMLInputElement | null>(null);

    // The store's words whenever the person is not in the middle of changing them.
    useEffect(() => {
        saved.current = { body: row.preview || '', subject: row.subject || '' };
        if (!editing) { setBody(row.preview || ''); setSubject(row.subject || ''); }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [row.preview, row.subject]);

    // The reading pause: the send button is dead for the first seconds the draft is on screen,
    // and says how long. Not an undo after the click: the pause sits BEFORE the decision.
    const [seenAt] = useState(() => {
        const at = FIRST_SEEN.get(row.ref) ?? Date.now();
        FIRST_SEEN.set(row.ref, at);
        return at;
    });
    const [now, setNow] = useState(() => Date.now());
    const unlockAt = seenAt + SEND_DELAY_SECONDS * 1000;
    const locked = Math.max(0, Math.ceil((unlockAt - now) / 1000));
    // A quarter-second tick while the pause runs, and none after: the interval stops itself,
    // so a card sitting on screen carries no timer and re-renders nothing.
    useEffect(() => {
        if (Date.now() >= unlockAt) return;
        const tick = setInterval(() => {
            const at = Date.now();
            setNow(at);
            if (at >= unlockAt) clearInterval(tick);
        }, 250);
        return () => clearInterval(tick);
    }, [unlockAt]);

    // The text field is as tall as its text. The border is added back: under border-box a
    // height of scrollHeight alone is two pixels short and the field scrolls at one line.
    useLayoutEffect(() => {
        const el = areaRef.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
    }, [body, editing]);

    useEffect(() => {
        if (editing === 'body') areaRef.current?.focus();
        if (editing === 'subject') subjectRef.current?.focus();
    }, [editing]);

    const save = async (): Promise<boolean> => {
        if (saving.current) return saving.current;
        const next = current.current;
        const patch: { body?: string; subject?: string } = {};
        if (next.body !== saved.current.body) patch.body = next.body;
        if (isMail && next.subject !== saved.current.subject) patch.subject = next.subject;
        if (patch.body === undefined && patch.subject === undefined) return true;
        if (patch.body !== undefined && !patch.body.trim()) { setNote(t('emptyText')); return false; }
        const run = (async () => {
            try {
                const res = await fetch(`${apiBase}/api/outbox/${row.kind}/${row.id}`, {
                    method: 'PATCH', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
                });
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    setNote(t('failed', { error: String(data.detail || res.status) }));
                    return false;
                }
                saved.current = { ...saved.current, ...patch };
                setNote(null);
                await onChanged();
                return true;
            } catch {
                setNote(t('failed', { error: '' }));
                return false;
            } finally {
                saving.current = null;
            }
        })();
        saving.current = run;
        return run;
    };

    // Leaving the text is saving it. Moving between the subject and the text is not leaving.
    const onEditBlur = (e: React.FocusEvent) => {
        const to = e.relatedTarget as Node | null;
        if (to && editRef.current?.contains(to)) return;
        setEditing(null);
        void save();
    };

    const revert = () => {
        setBody(saved.current.body);
        setSubject(saved.current.subject);
        setNote(null);
        setEditing(null);
    };

    const act = async (action: 'send' | 'discard') => {
        if (busy) return;
        setBusy(true);
        setNote(null);
        try {
            if (action === 'send') {
                // Whatever the person typed is what leaves: an edit still on its way, or still
                // in the field because they went straight for the button, is saved first.
                if (saving.current && !(await saving.current)) return;
                if (!(await save())) return;
            }
            const url = `${apiBase}/api/outbox/${row.kind}/${row.id}${action === 'send' ? '/send' : ''}`;
            const res = await fetch(url, { method: action === 'send' ? 'POST' : 'DELETE', credentials: 'include' });
            const data = await res.json().catch(() => ({}));
            if (!(res.ok && (action === 'discard' || data.ok))) {
                setNote(t('failed', { error: String(data.error || data.detail || res.status) }));
            } else {
                setEditing(null);
            }
            await onChanged();
        } catch {
            setNote(t('failed', { error: '' }));
        } finally {
            setBusy(false);
        }
    };

    const named = !!(row.recipient_name || '').trim() && row.recipient_name !== row.recipient;
    const text = editing ? body : (row.preview || '');
    const foldable = !editing && (text.split('\n').length > FOLD_LINES || text.length > FOLD_CHARS);
    const Icon = row.channel === 'mail' ? Mail : MessageCircle;
    const dirty = body !== saved.current.body || (isMail && subject !== saved.current.subject);

    return (
        <div className="relative max-w-2xl min-w-0 rounded-2xl border border-gray-200 dark:border-[#2e2e2e] bg-gray-50 dark:bg-[#1f1f1f] px-4 py-3 flex flex-col gap-2">
            {/* The rim that says "read me", while the send button is still locked. Its own
                element with a fixed shadow, breathing in OPACITY only: animating the card's
                own border or shadow would repaint the card on every frame (the repaint rule
                in globals.css, written after a measured GPU leak). */}
            {locked > 0 && (
                <span aria-hidden="true"
                    className="vaf-draft-rim pointer-events-none absolute inset-0 rounded-2xl" />
            )}
            <div className="flex items-start gap-2 text-gray-800 dark:text-gray-200">
                <Icon className="w-4 h-4 shrink-0 mt-0.5" />
                <div className="min-w-0">
                    <p className="text-sm font-medium break-words">{titleOf(row, t)}</p>
                    <p className="text-xs text-gray-500 dark:text-[#9a9a9a] break-words">
                        {named ? `${row.recipient} · ` : ''}{t('notSent')}{row.edited ? ` · ${t('edited')}` : ''}
                    </p>
                </div>
            </div>
            {/* Every address and every file, because what the person approves is what
                leaves; a line is shown only when there is something on it. */}
            {row.cc ? <p className="text-xs text-gray-500 dark:text-[#9a9a9a] break-words">{t('cc', { recipients: row.cc })}</p> : null}
            {row.bcc ? <p className="text-xs text-gray-500 dark:text-[#9a9a9a] break-words">{t('bcc', { recipients: row.bcc })}</p> : null}
            {row.attachments && row.attachments.length > 0 ? (
                <p className="text-xs text-gray-500 dark:text-[#9a9a9a] break-words">{t('attachments', { names: row.attachments.join(', ') })}</p>
            ) : null}
            {/* The words themselves: click into them to change them, the way one edits a
                message before it goes. The subject and the text are one editing area, so
                moving between them does not save half an edit. */}
            <div ref={editRef} onBlur={editing ? onEditBlur : undefined} className="flex flex-col gap-1.5">
                {isMail && (editing ? (
                    <input ref={subjectRef} value={subject} onChange={e => setSubject(e.target.value)}
                        onFocus={() => setEditing(prev => prev ?? 'subject')}
                        onKeyDown={e => { if (e.key === 'Escape') { e.preventDefault(); revert(); } }}
                        placeholder={t('subjectPlaceholder')} aria-label={t('subjectPlaceholder')}
                        className="w-full rounded-md border border-gray-300 dark:border-[#3a3a3a] bg-white dark:bg-[#181818] px-2 py-1 text-sm font-medium text-gray-800 dark:text-gray-200 focus:outline-none focus:border-gray-400 dark:focus:border-[#555]" />
                ) : (row.subject ? (
                    <button type="button" disabled={!editable} onClick={() => setEditing('subject')} title={editable ? t('editHint') : undefined}
                        className="text-left text-sm font-medium text-gray-800 dark:text-gray-200 break-words cursor-text disabled:cursor-default">
                        {row.subject}
                    </button>
                ) : null))}
                {editing ? (
                    <textarea ref={areaRef} value={body} rows={1} onChange={e => setBody(e.target.value)}
                        onFocus={() => setEditing(prev => prev ?? 'body')}
                        onKeyDown={e => { if (e.key === 'Escape') { e.preventDefault(); revert(); } }}
                        aria-label={t('editHint')}
                        className="w-full resize-none overflow-hidden rounded-md border border-gray-300 dark:border-[#3a3a3a] bg-white dark:bg-[#181818] px-2 py-1.5 text-sm leading-relaxed text-gray-800 dark:text-gray-200 focus:outline-none focus:border-gray-400 dark:focus:border-[#555]" />
                ) : (
                    <p role={editable ? 'button' : undefined} tabIndex={editable ? 0 : undefined}
                        onClick={editable ? () => { setUnfolded(true); setEditing('body'); } : undefined}
                        onKeyDown={editable ? e => { if (e.key === 'Enter') { e.preventDefault(); setUnfolded(true); setEditing('body'); } } : undefined}
                        title={editable ? t('editHint') : undefined}
                        className={cn('text-sm leading-relaxed text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words rounded-md',
                            editable && 'cursor-text hover:bg-gray-100 dark:hover:bg-[#262626]',
                            foldable && !unfolded && 'line-clamp-[8]')}>
                        {text}
                    </p>
                )}
                {foldable && (
                    <button type="button" onClick={() => setUnfolded(u => !u)}
                        className="self-start text-xs text-gray-500 hover:text-gray-700 dark:text-[#9a9a9a] dark:hover:text-gray-300">
                        {unfolded ? t('showLess') : t('showMore')}
                    </button>
                )}
            </div>
            {/* A draft whose last attempt did not leave says so, with the reason: a retry with
                no explanation is a person guessing. Not while the note below already says it
                for this card: the note is set on the failed click and the reload brings the
                row back as failed, so both would show at once. */}
            {row.state === 'failed' && !note && (
                <p className="text-xs text-red-600 dark:text-red-400">{t('failed', { error: row.error || '' })}</p>
            )}
            {/* Interrupted mid-send: it may have arrived. The person gets the reason and the
                Discard button, and no Send, because a second click could be a second delivery
                and nothing here can tell. */}
            {row.state === 'ambiguous' && (
                <p className="text-xs text-amber-700 dark:text-[#e0b866]">{t('ambiguous')}</p>
            )}
            <div className="flex flex-wrap items-center gap-2 pt-1">
                {/* The house's own primary button (bg-gray-900 light, #e6e6e6 dark): the light
                    tone is spelled out, because the bare white token under a dark variant folds
                    to the DARK surface colour and would hide the label (a guard test pins that
                    repo-wide). While the reading pause runs the button is dead and says why;
                    nothing is ever sent by the clock. */}
                {row.state !== 'ambiguous' && (
                    <button type="button" disabled={busy || locked > 0} onMouseDown={e => e.preventDefault()}
                        onClick={() => act('send')}
                        className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-900 text-white hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none disabled:opacity-50 inline-flex items-center gap-1.5 min-w-[7.5rem] justify-center">
                        {locked > 0
                            ? <>{t('countdown', { seconds: locked })}</>
                            : <><Send className="w-3.5 h-3.5" />{t('send')}</>}
                    </button>
                )}
                <button type="button" disabled={busy} onMouseDown={e => e.preventDefault()} onClick={() => act('discard')}
                    className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-200 dark:bg-[#2e2e2e] text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-[#3a3a3a] disabled:opacity-50 inline-flex items-center gap-1.5">
                    <Trash2 className="w-3.5 h-3.5" />{t('discard')}
                </button>
                {editing && dirty && (
                    <button type="button" onMouseDown={e => e.preventDefault()} onClick={revert}
                        className="px-2 py-1.5 text-xs text-gray-500 hover:text-gray-700 dark:text-[#9a9a9a] dark:hover:text-gray-300">
                        {t('revert')}
                    </button>
                )}
            </div>
            {note && <span className="text-xs text-red-600 dark:text-red-400">{note}</span>}
        </div>
    );
}

function DecidedDraft({ row, formatTime }: CardProps) {
    const t = useTranslations('outbox');
    const tCommon = useTranslations('common');
    const [open, setOpen] = useState(false);
    const state = row.state === 'sent' ? t('sent') : row.state === 'sending' ? t('sending')
        : row.state === 'replaced' ? t('replaced') : t('discarded');
    // "Gesendet: WhatsApp an Anna Berg · 14:32". The separator is the catalogue's, because
    // Chinese writes a full-width colon where German and English write ": ".
    const when = row.state === 'sent' && row.decided_ts ? ` · ${formatTime(row.decided_ts)}` : '';
    const label = `${state}${tCommon('labelSeparator')}${titleOf(row, t)}${when}`;
    const Icon = row.state === 'sent' ? Check : row.state === 'sending' ? Send : row.state === 'replaced' ? RefreshCw : Trash2;
    return (
        <div className="max-w-2xl min-w-0 rounded-xl border border-gray-200 dark:border-[#2e2e2e] bg-gray-50 dark:bg-[#1f1f1f]">
            {/* One line, the record of what became of the draft; the words it held open under
                it, read-only, because this is what the agent wrote (and, for a sent draft,
                what left). */}
            <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
                className="w-full flex items-center gap-2 px-3 py-2 text-left text-xs text-gray-500 hover:text-gray-700 dark:text-[#9a9a9a] dark:hover:text-gray-300">
                <Icon className="w-3.5 h-3.5 shrink-0" />
                <span className="min-w-0 truncate">{label}{row.edited ? ` · ${t('edited')}` : ''}</span>
                <ChevronRight className={cn('w-3.5 h-3.5 ml-auto shrink-0 transition-transform', open && 'rotate-90')} />
            </button>
            {open && (
                <div className="px-3 pb-3 flex flex-col gap-1">
                    {row.subject ? <p className="text-xs font-medium text-gray-700 dark:text-gray-300 break-words">{row.subject}</p> : null}
                    <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words">{row.preview}</p>
                </div>
            )}
        </div>
    );
}

type ListProps = Omit<CardProps, 'row'> & { rows: HeldSendRow[] };

/**
 * The drafts of ONE turn, lined up with that turn's tool windows. Under the answer of a turn
 * with an actions rail they are indented by the rail (`pl-[26px]`, the offset
 * TurnActionsTimeline gives every tool window), which is the case the card used to miss by
 * exactly that much; under a tool row of its own (`indent={false}`) the tool window already
 * sits in the answer column and the card starts where it does. On a phone the rail's answer
 * drops to full width, and so do the cards.
 */
export function TurnDrafts({ rows, indent = true, ...rest }: ListProps & { indent?: boolean }) {
    if (!rows.length) return null;
    return (
        <div className={cn('flex flex-col gap-3 pt-3', indent && 'pl-[26px] max-md:pl-0')}>
            {rows.map(r => <DraftCard key={r.ref} row={r} {...rest} />)}
        </div>
    );
}

/**
 * Waiting drafts with no turn on screen to sit under - the tool result has not arrived yet,
 * or the turn is no longer in the loaded history. They take the chat's last row, in the bot
 * row's own geometry (the row, the 85% block, the avatar gutter as a spacer) plus the rail
 * offset, so they land exactly where the card in a turn would. Nothing waiting, nothing
 * rendered: not even the row wrapper, or every conversation would end in an empty row.
 */
export function UnplacedDrafts({ rows, ...rest }: ListProps) {
    const t = useTranslations('outbox');
    if (!rows.length) return null;
    return (
        <div className="flex gap-4 pt-4 vaf-msg-row">
            <div className="w-full max-w-[85%] max-md:max-w-full flex gap-4 max-md:gap-2">
                <div className="w-9 shrink-0 max-md:hidden" aria-hidden="true" />
                <div className="flex flex-col gap-3 flex-1 min-w-0 pl-[26px] max-md:pl-0">
                    {rows.slice(0, 3).map(r => <DraftCard key={r.ref} row={r} {...rest} />)}
                    {rows.length > 3 && (
                        <span className="text-xs text-gray-500 dark:text-[#9a9a9a]">{t('more', { count: rows.length - 3 })}</span>
                    )}
                </div>
            </div>
        </div>
    );
}

export default UnplacedDrafts;
