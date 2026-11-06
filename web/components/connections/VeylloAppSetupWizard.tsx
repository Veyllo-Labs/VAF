'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// The Veyllo App setup window: pairing a phone with the signed-in account
// (vaf/api/app_link_routes.py, vaf/core/app_link/pairing.py).
//
// The flow follows the server's pairing states. The account opens its pairing window with its
// OWN 2FA code (POST /pairing-window); the window then issues a QR (POST /pairings) for the way
// chosen on the switch, Veyllo Relay or Local, and toggling the way issues a new QR without a
// second code while the window is open (the server withdraws the earlier one). The phone scans
// it, and the account confirms that the fingerprint shown here is the one on the phone
// (POST /pairings/current/confirm). Nothing is paired before that confirmation.
//
// The QR arrives as a PNG drawn on this machine (`qr_png`): what it carries is the pairing's
// one-time value, so it is never handed to a renderer elsewhere and never stored; closing the
// window drops it from state. Closing also withdraws a live QR and refuses a phone that waits
// for the confirmation, so no pairing nobody watches stays confirmable.
//
// The state is polled every 2 s while the window is open, the tab is visible and the pairing
// is still running, as the WhatsApp and Telegram pairings do: the state lives in the web
// server's process, and the shortest deadline (120 s for the confirmation) is sixty polls long.
//
// The 2FA field is the Account tab's (SessionsSection) copied verbatim rather than shared:
// what the three code fields in the app have in common is two attributes, and a component
// for those would be an adapter, not a primitive.
//
// Icons here are black and white only: success and failure are said by the text.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { ChevronRight, ExternalLink, Loader2, Smartphone, X } from 'lucide-react';
import ConfirmDialog from '@/components/ui/ConfirmDialog';
import Skeleton from '@/components/ui/Skeleton';
import { Switch, switchWord } from '@/components/ui/Switch';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';
import { retryAfterSeconds, retryWhen } from '@/lib/retryAfter';
import { cn } from '@/lib/utils';

const api = (path: string) => `/api/app-link${path}`;

/** Veyllo's privacy policy, which names what the app's agent does with VAF's answers. Only
 *  linked: VAF never fetches it. */
const PRIVACY_URL = 'https://veyllo.app/legal/privacy';

/** Withdraw the account's live code on the server (its K and the relay claim). */
const withdrawCurrent = () => {
    void fetch(api('/pairings/current'), { method: 'DELETE', credentials: 'include', keepalive: true }).catch(() => {});
};

/** Every state GET /api/app-link/pairings/current can answer (vaf/core/app_link/pairing.py),
 *  and the view it is shown in. The key set is held against the server by a test. */
const PAIRING_VIEWS = {
    none: 'explain',
    issued: 'qr',
    claimed: 'qr',
    awaiting_confirm: 'confirm',
    confirming: 'confirm',
    paired: 'paired',
    denied: 'ended',
    expired: 'ended',
    used_elsewhere: 'ended',
    failed: 'ended',
} as const;
type PairingState = keyof typeof PAIRING_VIEWS;
type View = typeof PAIRING_VIEWS[PairingState];

/** The states in which the server can still move the pairing on; only these are polled. */
const RUNNING: ReadonlySet<string> = new Set(['issued', 'claimed', 'awaiting_confirm', 'confirming']);

/** A refusal of the window or of a QR, by its `code`: a sentence of this window's own, or the
 *  Account tab's wording for the same 2FA answer. */
const REFUSAL_KEYS: Record<string, { ns: 'own' | 'account'; key: string }> = {
    window_closed: { ns: 'own', key: 'refusals.window_closed' },
    relay_off: { ns: 'own', key: 'refusals.relay_off' },
    no_direct_path: { ns: 'own', key: 'refusals.no_direct_path' },
    bad_mode: { ns: 'own', key: 'refusals.bad_mode' },
    twofa_not_set_up: { ns: 'own', key: 'refusals.twofa_not_set_up' },
    twofa_unreadable: { ns: 'account', key: 'errTwofaUnreadable' },
    code_spent: { ns: 'account', key: 'errCodeSpent' },
    wrong_code: { ns: 'account', key: 'errWrongCode' },
};

/** The relay problems a waiting pairing can carry (`relay_error`, vaf/core/app_link/relay.py). */
const RELAY_ERRORS = ['pairing_used', 'expired', 'secret_rejected', 'relay_unavailable',
    'instance_mismatch', 'secret_not_stored'] as const;

/** The connector's states (relay.STATES) that need the admin: they get a second sentence. */
const RELAY_STATE_HELP = ['secret_rejected', 'secret_not_stored', 'unavailable', 'replaced', 'revoked'];

type Way = 'relay' | 'local';

interface DeviceRow {
    id: string;
    model: string;
    platform: string;
    app_version: string;
    created_at: number | null;
    last_seen_at: number | null;
    last_transport: string | null;
    connected: boolean;
    relay: boolean;
    fingerprint: string;
    username?: string;
}

interface LinkStatus {
    channel_enabled: boolean;
    relay: { enabled: boolean; state: string; instance: boolean; connected_since: number | null; last_error: string };
    direct: { available: boolean; addresses: string[] };
    my_devices: DeviceRow[];
}

interface RelayAdmin {
    enabled: boolean;
    state: string;
    instance: boolean;
    secret: boolean;
    connected_since: number | null;
    last_error: string;
    members: number;
    devices_on_relay: number;
}

interface Pairing {
    state: PairingState;
    mode?: Way;
    device?: { model: string; platform: string; app_version: string; fingerprint: string };
    relay_error?: string;
    device_id?: string;
}

interface VeylloAppSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    /** A device was paired: the Connections card and the config refresh. */
    onComplete: () => void;
    isAdmin: boolean;
    /** Show Settings, Account (the device list); the window closes itself first. */
    onOpenAccount: () => void;
}

const card = 'rounded-xl border border-gray-200 bg-gray-50 p-4';
const primary = 'px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors disabled:opacity-50 dark:disabled:opacity-100 dark:disabled:bg-[#3a3a3a] dark:disabled:text-[#a3a3a3]';
const secondary = 'px-4 py-2 text-sm font-medium rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 transition-colors disabled:opacity-50';
const field = 'w-full px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white focus:outline-none focus:ring-2 focus:ring-gray-400';
const label = 'block text-xs font-semibold text-gray-700 mb-1';
const linkButton = 'text-xs font-medium text-red-600 hover:text-red-700 hover:underline disabled:opacity-50 shrink-0';

/** The two ways as one control: a sliding thumb under two halves (radiogroup). The thumb
 *  starts at the inline start, so in a right-to-left document "Veyllo Relay" sits on the right
 *  and the thumb travels the other way (globals.css, `.vaf-way-thumb`). */
function WaySwitch({ value, onChange, disabled, label: groupLabel, relayLabel, localLabel }: {
    value: Way;
    onChange: (way: Way) => void;
    disabled: boolean;
    label: string;
    relayLabel: string;
    localLabel: string;
}) {
    const refs = useRef<Record<Way, HTMLButtonElement | null>>({ relay: null, local: null });
    const ways: Way[] = ['relay', 'local'];
    const pick = (way: Way) => {
        refs.current[way]?.focus();
        if (!disabled && way !== value) onChange(way);
    };
    const onKey = (e: React.KeyboardEvent) => {
        const rtl = typeof document !== 'undefined'
            && getComputedStyle(e.currentTarget as Element).direction === 'rtl';
        // Left and Right follow the screen, which mirrors in a right-to-left document; Up and
        // Down move to the other half; Home is the first way, End the last.
        const target: Record<string, Way> = {
            ArrowRight: rtl ? 'relay' : 'local',
            ArrowLeft: rtl ? 'local' : 'relay',
            ArrowUp: value === 'relay' ? 'local' : 'relay',
            ArrowDown: value === 'relay' ? 'local' : 'relay',
            Home: 'relay',
            End: 'local',
        };
        const next = target[e.key];
        if (!next) return;
        e.preventDefault();
        pick(next);
    };
    return (
        <div role="radiogroup" aria-label={groupLabel} aria-disabled={disabled || undefined}
            className="relative grid grid-cols-2 rounded-full bg-gray-100 border border-gray-200 p-1">
            <span aria-hidden="true" data-way={value}
                className="vaf-way-thumb absolute inset-y-1 start-1 w-[calc(50%-4px)] rounded-full bg-gray-900 dark:bg-[#e6e6e6]" />
            {ways.map(way => {
                const checked = way === value;
                return (
                    <button key={way} type="button" role="radio" aria-checked={checked}
                        ref={el => { refs.current[way] = el; }}
                        tabIndex={checked ? 0 : -1}
                        onClick={() => pick(way)}
                        onKeyDown={onKey}
                        className={cn('relative z-10 rounded-full px-3 py-2 text-sm font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400',
                            checked ? 'text-white dark:text-[#181818]' : 'text-gray-500 hover:text-gray-700',
                            disabled && 'cursor-not-allowed')}>
                        {way === 'relay' ? relayLabel : localLabel}
                    </button>
                );
            })}
        </div>
    );
}

export default function VeylloAppSetupWizard({ isOpen, onClose, onComplete, isAdmin, onOpenAccount }: VeylloAppSetupWizardProps) {
    const t = useTranslations('settings.veylloApp');
    const tAccount = useTranslations('account');
    const tCommon = useTranslations('common');
    const locale = useLocale();

    const [status, setStatus] = useState<LinkStatus | null>(null);
    const [statusFailed, setStatusFailed] = useState(false);
    const [pairing, setPairing] = useState<Pairing>({ state: 'none' });
    // The view is the pairing's, except that a QR state found on opening has no QR to show
    // (the server never sends it twice): that starts at the explanation.
    const [view, setView] = useState<View>('explain');
    const [way, setWay] = useState<Way>('relay');
    const [qrPng, setQrPng] = useState<string | null>(null);
    const [deadline, setDeadline] = useState<number | null>(null);
    const [now, setNow] = useState(() => Date.now());
    const [issueRefusal, setIssueRefusal] = useState<string | null>(null);
    const [code, setCode] = useState('');
    const [codeNote, setCodeNote] = useState<string | null>(null);
    const [busy, setBusy] = useState<string | null>(null);

    const pairingRef = useRef<Pairing>({ state: 'none' });
    pairingRef.current = pairing;
    const pollSeq = useRef(0);
    // The newest issue() call: a stale answer of the newest one is a code nobody shows.
    const issueSeq = useRef(0);

    const relayAllowed = status?.relay.enabled !== false;
    const directAvailable = !!status?.direct.available;
    const impossible = !!status && !relayAllowed && !directAvailable;

    const refusalText = useCallback((data: { code?: string; detail?: string }, res?: Response): string => {
        const known = REFUSAL_KEYS[String(data.code || '')];
        if (known?.key === 'refusals.no_direct_path' && isAdmin) return t('refusalNoDirectAdmin');
        if (known) return known.ns === 'account' ? tAccount(known.key) : t(known.key);
        if (res?.status === 429) return tCommon('tooManyAttempts', { when: retryWhen(retryAfterSeconds(res), locale) });
        if (res?.status === 404) return t('errNoAccount');
        return String(data.detail || t('failedGeneric'));
    }, [isAdmin, locale, t, tAccount, tCommon]);

    const loadStatus = useCallback(async (): Promise<LinkStatus | null> => {
        try {
            const res = await fetch(api('/status'), { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error(String(res.status));
            const data = await res.json() as LinkStatus;
            setStatus(data);
            setStatusFailed(false);
            return data;
        } catch {
            setStatusFailed(true);
            return null;
        }
    }, []);

    /** Apply one answer of the server about the pairing. Only the newest answer writes. */
    const applyPairing = useCallback((next: Pairing, seq: number) => {
        if (seq !== pollSeq.current) return;
        setPairing(next);
        const target = PAIRING_VIEWS[next.state as PairingState] ?? 'ended';
        setView(prev => {
            if (target === 'qr' && prev !== 'qr') return prev;
            if (next.state === 'none' && prev === 'ended') return prev;
            // Answered elsewhere (another tab) or withdrawn: say so at the explanation.
            if (next.state === 'none' && prev !== 'explain') setCodeNote(t('pairingStates.none'));
            return target;
        });
        if (next.state === 'paired') {
            setQrPng(null);
            void loadStatus();
            onComplete();
        }
        if (!RUNNING.has(next.state)) setQrPng(null);
    }, [loadStatus, onComplete, t]);

    const issue = useCallback(async (mode: Way) => {
        const seq = ++pollSeq.current;
        issueSeq.current = seq;
        setBusy('issue');
        setIssueRefusal(null);
        setQrPng(null);
        setWay(mode);
        try {
            const res = await fetch(api('/pairings'), {
                method: 'POST', credentials: 'include', cache: 'no-store',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode }),
            });
            const data = await res.json().catch(() => ({}));
            if (seq !== pollSeq.current) {
                // Overtaken while the server was issuing (the window closed, or the code ran
                // out): the code it just issued is on nobody's screen, so it is withdrawn. Not
                // when a newer issue() started, which replaces it on the server and may be the
                // account's current code by now.
                if (res.ok && issueSeq.current === seq) withdrawCurrent();
                return;
            }
            if (!res.ok) {
                // A refused QR leaves the account's earlier one live on the server (a refusal
                // withdraws nothing): withdraw it, since it is no longer on screen.
                withdrawCurrent();
                if (data.code === 'window_closed') {
                    setPairing({ state: 'none' });
                    setView('explain');
                    setCodeNote(t('refusals.window_closed'));
                    return;
                }
                setPairing({ state: 'none' });
                setView('qr');
                setIssueRefusal(refusalText(data, res));
                return;
            }
            setQrPng(String(data.qr_png || ''));
            // The time left from the server's own clock, counted from this answer.
            setDeadline(Date.now() + (Number(data.expires_at) - Number(data.server_time)) * 1000);
            setNow(Date.now());
            setPairing({ state: 'issued', mode: data.mode === 'local' ? 'local' : 'relay' });
            setView('qr');
        } catch {
            if (seq !== pollSeq.current) return;
            setView('qr');
            setIssueRefusal(t('failedGeneric'));
        } finally {
            setBusy(null);
        }
    }, [refusalText, t]);

    const openWindow = async () => {
        setCodeNote(null);
        setBusy('code');
        try {
            const res = await fetch(api('/pairing-window'), {
                method: 'POST', credentials: 'include', cache: 'no-store',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code.trim() }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setCodeNote(refusalText(data, res));
                return;
            }
            setCode('');
            setBusy(null);
            await issue(relayAllowed ? 'relay' : 'local');
        } catch {
            setCodeNote(t('failedGeneric'));
        } finally {
            setBusy(null);
        }
    };

    const answer = async (accept: boolean) => {
        const seq = ++pollSeq.current;
        setPairing(prev => ({ ...prev, state: 'confirming' }));
        try {
            const res = await fetch(api('/pairings/current/confirm'), {
                method: 'POST', credentials: 'include', cache: 'no-store',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ accept }),
            });
            const data = await res.json().catch(() => ({}));
            const state = res.ok && data.state in PAIRING_VIEWS ? data.state as PairingState : 'failed';
            const current = state === 'paired'
                ? await fetch(api('/pairings/current'), { credentials: 'include', cache: 'no-store' })
                    .then(r => r.json()).catch(() => ({ state }))
                : { state };
            applyPairing({ ...current, state }, seq);
        } catch {
            applyPairing({ state: 'failed' }, seq);
        }
    };

    /** Leaving the window leaves nothing behind: a live QR is withdrawn, a phone that waits for
     *  the confirmation is refused. Runs once per pairing (the ref is cleared first). */
    const leave = useCallback(() => {
        const state = pairingRef.current.state;
        pairingRef.current = { state: 'none' };
        ++pollSeq.current;
        if (state === 'issued' || state === 'claimed') {
            withdrawCurrent();
        } else if (state === 'awaiting_confirm') {
            void fetch(api('/pairings/current/confirm'), {
                method: 'POST', credentials: 'include', keepalive: true,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ accept: false }),
            }).catch(() => {});
        }
        setPairing({ state: 'none' });
        setView('explain');
        setQrPng(null);
        setDeadline(null);
        setIssueRefusal(null);
        setCode('');
        setCodeNote(null);
    }, []);

    const close = () => {
        leave();
        onClose();
    };

    // Opening: the account's link state and its pairing. A phone that waits for the
    // confirmation is shown at once; everything else starts at the explanation.
    useEffect(() => {
        if (!isOpen) return;
        let cancelled = false;
        const seq = ++pollSeq.current;
        void (async () => {
            const loaded = await loadStatus();
            if (cancelled) return;
            if (loaded) setWay(loaded.relay.enabled ? 'relay' : 'local');
            try {
                const res = await fetch(api('/pairings/current'), { credentials: 'include', cache: 'no-store' });
                const data = await res.json() as Pairing;
                if (cancelled || seq !== pollSeq.current || !res.ok) return;
                if (data.state === 'awaiting_confirm' || data.state === 'confirming') {
                    setPairing(data);
                    setView('confirm');
                }
            } catch { /* the explanation stays */ }
        })();
        return () => { cancelled = true; };
    }, [isOpen, loadStatus]);

    // Closed from outside (Settings closing, the component going away): the same as the X.
    useEffect(() => {
        if (!isOpen) return;
        return () => leave();
    }, [isOpen, leave]);

    // The poll: open, visible, and a pairing the server can still move on.
    useEffect(() => {
        if (!isOpen || !RUNNING.has(pairing.state)) return;
        const timer = setInterval(async () => {
            if (document.visibilityState !== 'visible') return;
            const seq = pollSeq.current;
            try {
                const res = await fetch(api('/pairings/current'), { credentials: 'include', cache: 'no-store' });
                if (!res.ok) return;
                applyPairing(await res.json() as Pairing, seq);
            } catch { /* the next poll asks again */ }
        }, 2000);
        return () => clearInterval(timer);
    }, [isOpen, pairing.state, applyPairing]);

    // The countdown under the QR. At zero the code has run out, also without a poll.
    useEffect(() => {
        if (!isOpen || deadline === null || view !== 'qr') return;
        const timer = setInterval(() => {
            const at = Date.now();
            setNow(at);
            if (at >= deadline && (pairingRef.current.state === 'issued' || pairingRef.current.state === 'claimed')) {
                ++pollSeq.current;
                setPairing({ state: 'expired' });
                setView('ended');
                setQrPng(null);
            }
        }, 1000);
        return () => clearInterval(timer);
    }, [isOpen, deadline, view]);

    useEscapeLayer({ active: isOpen, level: 60, onEscape: close });

    if (!isOpen) return null;

    const left = deadline === null ? 0 : Math.max(0, Math.ceil((deadline - now) / 1000));
    const countdown = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
    const devices = status?.my_devices ?? [];
    const pairedModel = devices.find(d => d.id === pairing.device_id)?.model || t('phoneFallback');
    const switchLocked = busy !== null || pairing.state === 'awaiting_confirm' || pairing.state === 'confirming';
    const relayError = pairing.relay_error && (RELAY_ERRORS as readonly string[]).includes(pairing.relay_error)
        ? t(`relayErrors.${pairing.relay_error}`) : null;

    const explain = (
        <div className="space-y-4">
            <div className={card}>
                <h4 className="text-sm font-semibold text-gray-900 mb-2">{t('canDoTitle')}</h4>
                <ul className="list-disc ps-5 space-y-1 text-sm text-gray-700">
                    <li>{t('canChat')}</li>
                    <li>{t('canReceive')}</li>
                    <li>{t('canTasks')}</li>
                </ul>
                <p className="text-sm text-gray-700 mt-2">{t('appName')}</p>
                <p className="text-sm text-gray-700 mt-2">{t('agentUsesVaf')}</p>
                <a href={PRIVACY_URL} target="_blank" rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 mt-1 text-sm font-medium text-gray-900 underline underline-offset-2 hover:no-underline">
                    {t('privacyLink')} <ExternalLink className="w-3.5 h-3.5 text-gray-500" aria-hidden="true" />
                </a>
            </div>
            <div className={card}>
                <h4 className="text-sm font-semibold text-gray-900 mb-2">{t('seesTitle')}</h4>
                <ul className="list-disc ps-5 space-y-1 text-sm text-gray-700">
                    <li>{t('seesRelay')}</li>
                    <li>{t('seesDirect')}</li>
                </ul>
                <p className="text-sm text-gray-700 mt-2">{t('costSentence')}</p>
            </div>
            {devices.length > 0 && (
                <div className="flex items-center justify-between gap-3 text-sm text-gray-700 max-md:flex-col max-md:items-start">
                    <span>{t('haveDevices', { count: devices.length })}</span>
                    <button type="button" onClick={() => { close(); onOpenAccount(); }} className={cn(secondary, 'shrink-0 whitespace-nowrap')}>
                        {t('openAccount')}
                    </button>
                </div>
            )}
            {statusFailed && !status ? (
                <div className="flex items-center gap-3">
                    <p className="text-sm text-red-600 flex-1">{t('statusFailed')}</p>
                    <button type="button" onClick={() => void loadStatus()}
                        className="text-xs font-medium text-gray-700 hover:text-gray-900 hover:underline">
                        {tAccount('retry')}
                    </button>
                </div>
            ) : !status ? (
                <Skeleton className="h-[86px]" />
            ) : impossible ? (
                <div className="space-y-1">
                    <p className="text-sm text-gray-900 font-medium">{t('impossible')}</p>
                    {isAdmin && <p className="text-sm text-gray-600">{t('impossibleAdmin')}</p>}
                </div>
            ) : (
                <form className="space-y-2" onSubmit={e => { e.preventDefault(); void openWindow(); }}>
                    <p className="text-xs text-gray-600">{t('codeHint')}</p>
                    <div className="flex items-end gap-3 max-md:flex-col max-md:items-stretch">
                        <div>
                            <label className={label} htmlFor="veyllo-app-code">{tAccount('code')}</label>
                            <input id="veyllo-app-code" value={code} onChange={e => setCode(e.target.value)} inputMode="numeric"
                                autoComplete="one-time-code" placeholder="123456" className={`${field} font-mono w-32 max-md:w-full`} />
                        </div>
                        <button type="submit" disabled={busy !== null || !code.trim()} className={cn(primary, 'inline-flex items-center justify-center gap-2')}>
                            {busy !== null && <Loader2 className="w-4 h-4 animate-spin" />}
                            {busy !== null ? t('opening') : t('showQr')}
                        </button>
                    </div>
                    {codeNote && <p className="text-xs text-red-600">{codeNote}</p>}
                </form>
            )}
        </div>
    );

    const qr = (
        <div className="space-y-4">
            <div key={way} className="vaf-way-enter flex flex-col items-center gap-2">
                {issueRefusal ? (
                    <div className="w-64 max-w-full min-h-[16rem] rounded-xl border border-gray-200 bg-gray-50 p-4 flex items-center">
                        <p className="text-sm text-gray-700 text-center">{issueRefusal}</p>
                    </div>
                ) : qrPng ? (
                    <img src={'data:image/png;base64,' + qrPng} alt={t('qrAlt')}
                        className="w-64 h-64 rounded-xl border border-gray-200 bg-white [image-rendering:pixelated]" />
                ) : (
                    <Skeleton className="w-64 h-64 rounded-xl" />
                )}
                {!issueRefusal && (
                    <p className="text-sm text-gray-600 tabular-nums" aria-live="off">
                        {qrPng ? t('validFor', { time: countdown }) : ' '}
                    </p>
                )}
            </div>
            {relayAllowed ? (
                <WaySwitch value={way} disabled={switchLocked} onChange={mode => void issue(mode)}
                    label={t('wayLabel')} relayLabel={t('wayRelay')} localLabel={t('wayLocal')} />
            ) : (
                <p className="text-sm text-gray-700 text-center">{t('relayOffByAdmin')}</p>
            )}
            <p key={`explain-${way}`} className="vaf-way-enter text-sm text-gray-600 text-center">
                {way === 'relay' ? t('explainRelay') : t('explainLocal')}
            </p>
            {!issueRefusal && qrPng && (
                <div className="text-center space-y-1" aria-live="polite">
                    <p className="text-sm font-medium text-gray-900">
                        {pairing.state === 'claimed' ? t('pairingStates.claimed') : t('pairingStates.issued')}
                    </p>
                    {relayError && <p className="text-sm text-red-600">{relayError}</p>}
                </div>
            )}
        </div>
    );

    const confirmView = !pairing.device ? (
        <div className={cn(card, 'flex items-center gap-2')}>
            <Loader2 className="w-4 h-4 text-gray-900 animate-spin" aria-hidden="true" />
            <p className="text-sm text-gray-900">{t('pairingStates.confirming')}</p>
        </div>
    ) : (
        <div className="space-y-4">
            <div className={cn(card, 'bg-white')}>
                <h4 className="text-base font-semibold text-gray-900 mb-3">{t('pairingStates.awaiting_confirm')}</h4>
                <div className="flex items-start gap-3">
                    <Smartphone className="w-5 h-5 text-gray-900 shrink-0 mt-0.5" aria-hidden="true" />
                    <div className="min-w-0">
                        <p className="text-sm font-medium text-gray-900 break-words">{pairing.device.model}</p>
                        <p className="text-xs text-gray-500">{t('deviceMeta', { platform: pairing.device.platform, version: pairing.device.app_version })}</p>
                    </div>
                </div>
                <p className="text-xs font-semibold text-gray-700 mt-4 mb-1">{t('fingerprintLabel')}</p>
                <p className="font-mono text-2xl max-md:text-xl tracking-wider text-gray-900 break-words" dir="ltr">{pairing.device.fingerprint}</p>
                <p className="text-sm text-gray-600 mt-3">{t('confirmHint')}</p>
            </div>
            <div className="flex gap-3 max-md:flex-col">
                <button type="button" disabled={pairing.state === 'confirming'} onClick={() => void answer(true)}
                    className={cn(primary, 'flex-1 inline-flex items-center justify-center gap-2')}>
                    {pairing.state === 'confirming' && <Loader2 className="w-4 h-4 animate-spin" />}
                    {pairing.state === 'confirming' ? t('pairingStates.confirming') : t('confirmYes')}
                </button>
                <button type="button" disabled={pairing.state === 'confirming'} onClick={() => void answer(false)}
                    className={cn(secondary, 'flex-1')}>
                    {t('confirmNo')}
                </button>
            </div>
        </div>
    );

    const endText = pairing.state === 'denied' || pairing.state === 'expired'
        || pairing.state === 'used_elsewhere' || pairing.state === 'failed'
        ? t(`pairingStates.${pairing.state}`) : t('pairingStates.failed');

    const ended = (
        <div className="space-y-4">
            <div className={card}>
                <p className="text-sm font-medium text-gray-900">{endText}</p>
                {relayError && pairing.relay_error !== 'pairing_used' && pairing.relay_error !== 'expired' && (
                    <p className="text-sm text-gray-600 mt-1">{relayError}</p>
                )}
            </div>
            <div className="flex gap-3 max-md:flex-col">
                <button type="button" disabled={busy !== null} onClick={() => void issue(relayAllowed ? way : 'local')}
                    className={cn(primary, 'flex-1')}>
                    {t('newCode')}
                </button>
                <button type="button" onClick={close} className={cn(secondary, 'flex-1')}>{tCommon('close')}</button>
            </div>
        </div>
    );

    const paired = (
        <div className="space-y-4">
            <div className={card}>
                <div className="flex items-start gap-3">
                    <Smartphone className="w-5 h-5 text-gray-900 shrink-0 mt-0.5" aria-hidden="true" />
                    <p className="text-sm font-medium text-gray-900">{t('pairingStates.paired', { model: pairedModel })}</p>
                </div>
                <p className="text-sm text-gray-600 mt-3">{t('manageInAccount')}</p>
            </div>
            <div className="flex gap-3 max-md:flex-col">
                <button type="button" onClick={close} className={cn(primary, 'flex-1')}>{t('done')}</button>
                <button type="button" onClick={() => { close(); onOpenAccount(); }} className={cn(secondary, 'flex-1')}>
                    {t('openAccount')}
                </button>
            </div>
        </div>
    );

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4 max-md:p-0" onClick={close}>
            <div role="dialog" aria-modal="true" aria-label={t('title')} onClick={e => e.stopPropagation()}
                className="bg-white rounded-2xl shadow-xl max-w-md w-full max-h-[90vh] flex flex-col overflow-hidden max-md:max-w-none max-md:mx-0 max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0">
                <div className="flex items-center justify-between px-6 pt-6 pb-4 max-md:p-4 max-md:shrink-0">
                    <h3 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                        <Smartphone className="w-5 h-5 text-gray-900" aria-hidden="true" />
                        {t('title')}
                    </h3>
                    <button type="button" onClick={close} aria-label={tCommon('close')} className="p-1 hover:bg-gray-100 rounded-lg">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>
                <div className="px-6 pb-6 overflow-y-auto min-h-0 max-md:flex-1 max-md:p-4 max-md:pt-0">
                    {view === 'explain' && explain}
                    {view === 'qr' && qr}
                    {view === 'confirm' && confirmView}
                    {view === 'ended' && ended}
                    {view === 'paired' && paired}
                    {isAdmin && view === 'explain' && <RelayAdminSection onChanged={() => void loadStatus()} />}
                </div>
            </div>
        </div>
    );
}

/** The relay for every account, and every account's devices: admins only (the routes answer
 *  403 to anybody else). Collapsed until opened, so the pairing stays the window's subject. */
function RelayAdminSection({ onChanged }: { onChanged: () => void }) {
    const t = useTranslations('settings.veylloApp');
    const tAccount = useTranslations('account');
    const tCommon = useTranslations('common');
    const locale = useLocale();
    const [open, setOpen] = useState(false);
    const [relay, setRelay] = useState<RelayAdmin | null>(null);
    const [rows, setRows] = useState<DeviceRow[] | null>(null);
    const [failed, setFailed] = useState(false);
    const [busy, setBusy] = useState<string | null>(null);
    const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
    const [asking, setAsking] = useState<'rotate' | { remove: DeviceRow } | null>(null);
    const [dissolving, setDissolving] = useState(false);
    const [code, setCode] = useState('');
    const [codeNote, setCodeNote] = useState<string | null>(null);

    const load = useCallback(async () => {
        try {
            const [r, d] = await Promise.all([
                fetch(api('/relay'), { credentials: 'include', cache: 'no-store' }),
                fetch(api('/devices?all=1'), { credentials: 'include', cache: 'no-store' }),
            ]);
            if (!r.ok || !d.ok) throw new Error(String(r.status));
            setRelay(await r.json() as RelayAdmin);
            const data = await d.json();
            setRows(Array.isArray(data.devices) ? data.devices : []);
            setFailed(false);
        } catch {
            setFailed(true);
        }
    }, []);

    useEffect(() => { if (open) void load(); }, [open, load]);

    const format = (sec: number | null) => {
        if (!sec) return '-';
        try {
            return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' })
                .format(new Date(sec * 1000)).replace(/\s/g, ' ');
        } catch {
            return String(sec);
        }
    };

    const failureText = async (res: Response) => {
        const data = await res.json().catch(() => ({}));
        if (res.status === 502 && ['not_set_up', 'unreachable', 'refused'].includes(String(data.reason))) {
            return t(`failureReasons.${data.reason}`);
        }
        return String(data.detail || t('failedGeneric'));
    };

    const switchRelay = async () => {
        if (!relay) return;
        setBusy('switch');
        setNote(null);
        try {
            const res = await fetch(api('/relay'), {
                method: 'PUT', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: !relay.enabled }),
            });
            if (!res.ok) setNote({ ok: false, text: await failureText(res) });
            else setRelay(await res.json() as RelayAdmin);
        } catch {
            setNote({ ok: false, text: t('failedGeneric') });
        } finally {
            setBusy(null);
            onChanged();
        }
    };

    const rotate = async () => {
        setAsking(null);
        setBusy('rotate');
        setNote(null);
        try {
            const res = await fetch(api('/relay/rotate'), { method: 'POST', credentials: 'include' });
            if (!res.ok) setNote({ ok: false, text: await failureText(res) });
            else {
                setRelay(await res.json() as RelayAdmin);
                setNote({ ok: true, text: t('rotated') });
            }
        } catch {
            setNote({ ok: false, text: t('failedGeneric') });
        } finally {
            setBusy(null);
        }
    };

    const dissolve = async () => {
        setBusy('dissolve');
        setCodeNote(null);
        try {
            const res = await fetch(api('/relay/dissolve'), {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code.trim() }),
            });
            if (!res.ok) {
                if (res.status === 502) setCodeNote(await failureText(res));
                else {
                    const data = await res.json().catch(() => ({}));
                    const known = REFUSAL_KEYS[String(data.code || '')];
                    setCodeNote(known ? (known.ns === 'account' ? tAccount(known.key) : t(known.key))
                        : res.status === 429 ? tCommon('tooManyAttempts', { when: retryWhen(retryAfterSeconds(res), locale) })
                            : String(data.detail || t('failedGeneric')));
                }
                return;
            }
            setRelay(await res.json() as RelayAdmin);
            setDissolving(false);
            setCode('');
            setNote({ ok: true, text: t('dissolved') });
            void load();
        } catch {
            setCodeNote(t('failedGeneric'));
        } finally {
            setBusy(null);
            onChanged();
        }
    };

    const remove = async (row: DeviceRow) => {
        setAsking(null);
        setBusy(row.id);
        setNote(null);
        try {
            const res = await fetch(api(`/devices/${encodeURIComponent(row.id)}`), { method: 'DELETE', credentials: 'include' });
            // 404: it went meanwhile; the reload shows the truth.
            if (!res.ok && res.status !== 404) setNote({ ok: false, text: tAccount('actionFailed') });
        } catch {
            setNote({ ok: false, text: tAccount('actionFailed') });
        } finally {
            setBusy(null);
            void load();
            onChanged();
        }
    };

    const badge = 'text-[11px] font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 border border-gray-200';
    const meta = 'text-xs text-gray-500 mt-0.5 break-words';

    return (
        <div className="mt-6 pt-4 border-t border-gray-200">
            <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
                className="w-full flex items-center justify-between gap-3 text-start">
                <span className="text-sm font-semibold text-gray-900">{t('adminTitle')}</span>
                <ChevronRight className={cn('w-4 h-4 text-gray-500 transition-transform', open && 'rotate-90')} aria-hidden="true" />
            </button>
            {open && (
                <div className="mt-3 space-y-4">
                    {failed && !relay ? (
                        <div className="flex items-center gap-3">
                            <p className="text-sm text-red-600 flex-1">{t('adminFailed')}</p>
                            <button type="button" onClick={() => void load()}
                                className="text-xs font-medium text-gray-700 hover:text-gray-900 hover:underline">
                                {tAccount('retry')}
                            </button>
                        </div>
                    ) : !relay ? (
                        <Skeleton className="h-[120px]" />
                    ) : (
                        <>
                            <div className={card}>
                                <div className="flex items-center justify-between gap-3">
                                    <span className="text-sm font-medium text-gray-900">{t('wayRelay')}</span>
                                    <div className="flex items-center gap-2 text-sm">
                                        <span className={switchWord(!relay.enabled)}>{t('off')}</span>
                                        <Switch on={relay.enabled} disabled={busy !== null} label={t('relaySwitchLabel')} onClick={() => void switchRelay()} />
                                        <span className={switchWord(relay.enabled)}>{t('on')}</span>
                                    </div>
                                </div>
                                <p className="text-sm text-gray-700 mt-2">
                                    {relay.state === 'connected' && relay.connected_since
                                        ? t('relayConnectedSince', { when: format(relay.connected_since) })
                                        : t(`relayStates.${relay.state}`)}
                                </p>
                                {RELAY_STATE_HELP.includes(relay.state) && (
                                    <p className="text-sm text-gray-600 mt-1">{t(`relayStateHelp.${relay.state}`)}</p>
                                )}
                                <p className="text-xs text-gray-500 mt-2">
                                    {relay.instance ? t('counts', { members: relay.members, onRelay: relay.devices_on_relay }) : t('noInstance')}
                                </p>
                                <div className="flex flex-wrap gap-2 mt-3">
                                    <button type="button" disabled={busy !== null || !relay.instance} onClick={() => setAsking('rotate')} className={cn(secondary, 'text-xs px-3 py-1.5')}>
                                        {t('rotate')}
                                    </button>
                                    {!dissolving && (
                                        <button type="button" disabled={busy !== null || !relay.instance} onClick={() => setDissolving(true)}
                                            className="px-3 py-1.5 text-xs font-medium rounded-lg border border-red-300 text-red-700 hover:bg-red-50 transition-colors disabled:opacity-50">
                                            {t('dissolve')}
                                        </button>
                                    )}
                                </div>
                                {note && <p className={`text-xs mt-3 ${note.ok ? 'text-gray-600' : 'text-red-600'}`}>{note.text}</p>}
                            </div>
                            {dissolving && (
                                <form className="rounded-xl border border-red-300 p-4 space-y-3" onSubmit={e => { e.preventDefault(); void dissolve(); }}>
                                    <p className="text-sm font-semibold text-gray-900">{t('dissolve')}</p>
                                    <p className="text-sm text-gray-700">{t('dissolveBody')}</p>
                                    <div className="flex items-end gap-3 max-md:flex-col max-md:items-stretch">
                                        <div>
                                            <label className={label} htmlFor="veyllo-relay-code">{tAccount('code')}</label>
                                            <input id="veyllo-relay-code" value={code} onChange={e => setCode(e.target.value)} inputMode="numeric"
                                                autoComplete="one-time-code" placeholder="123456" className={`${field} font-mono w-32 max-md:w-full`} />
                                        </div>
                                        <button type="submit" disabled={busy !== null || !code.trim()}
                                            className="px-4 py-2 text-sm font-medium rounded-lg bg-red-600 hover:bg-red-700 text-white transition-colors disabled:opacity-50">
                                            {t('dissolveConfirm')}
                                        </button>
                                        <button type="button" onClick={() => { setDissolving(false); setCode(''); setCodeNote(null); }} className={secondary}>
                                            {tCommon('cancel')}
                                        </button>
                                    </div>
                                    {codeNote && <p className="text-xs text-red-600">{codeNote}</p>}
                                </form>
                            )}
                            <div>
                                <h4 className="text-sm font-semibold text-gray-900 mb-2">{t('allDevicesTitle')}</h4>
                                {rows && rows.length === 0 ? (
                                    <p className="text-sm text-gray-500">{t('allDevicesNone')}</p>
                                ) : (
                                    <ul className="flex flex-col gap-2">
                                        {(rows ?? []).map(row => (
                                            <li key={row.id} className="flex items-start gap-3 px-3 py-2.5 rounded-lg border border-gray-200 bg-white max-md:flex-wrap">
                                                <Smartphone className="w-4 h-4 text-gray-500 shrink-0 mt-0.5" aria-hidden="true" />
                                                <div className="flex-1 min-w-0">
                                                    <div className="flex items-center gap-2 flex-wrap">
                                                        <span className="text-sm font-medium text-gray-800 break-words">{row.model || t('phoneFallback')}</span>
                                                        <span className={badge}>{row.username}</span>
                                                        <span className={badge}>{row.last_transport === 'relay' ? tAccount('wayRelay') : tAccount('wayDirect')}</span>
                                                        {row.connected && <span className={badge}>{tAccount('connectedNow')}</span>}
                                                    </div>
                                                    <p className={meta}>{tAccount('appLastConnected', { when: format(row.last_seen_at) })}</p>
                                                    <p className={meta}>
                                                        {tAccount('appPaired', { when: format(row.created_at) })}
                                                        {/* Below md the fingerprint takes a line of its own, so no dot ends a line. */}
                                                        <span className="max-md:hidden">{' · '}</span>
                                                        <span className="font-mono whitespace-nowrap max-md:block">{tAccount('appFingerprint', { fingerprint: row.fingerprint })}</span>
                                                    </p>
                                                </div>
                                                <button type="button" disabled={busy !== null} onClick={() => setAsking({ remove: row })}
                                                    className={`${linkButton} max-md:basis-full max-md:text-left max-md:pl-7`}>
                                                    {tAccount('removeApp')}
                                                </button>
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </div>
                        </>
                    )}
                </div>
            )}
            <ConfirmDialog
                open={asking === 'rotate'}
                title={t('rotateTitle')}
                body={t('rotateBody')}
                confirmLabel={t('rotateConfirm')}
                cancelLabel={tCommon('cancel')}
                onConfirm={() => void rotate()}
                onCancel={() => setAsking(null)}
            />
            <ConfirmDialog
                open={typeof asking === 'object' && asking !== null}
                title={tAccount('removeAppTitle')}
                body={typeof asking === 'object' && asking !== null
                    ? tAccount('removeAppConfirm', { model: asking.remove.model || t('phoneFallback') }) : ''}
                confirmLabel={tAccount('removeApp')}
                cancelLabel={tCommon('cancel')}
                destructive
                onConfirm={() => { if (typeof asking === 'object' && asking !== null) void remove(asking.remove); }}
                onCancel={() => setAsking(null)}
            />
        </div>
    );
}
