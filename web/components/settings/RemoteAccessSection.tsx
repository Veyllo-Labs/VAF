// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { useTranslations } from 'next-intl';
import { Copy, Network, Shield, Trash2 } from 'lucide-react';
import { copyText } from '@/lib/clipboard';

interface Iface {
    name: string;
    ip: string;
    network: string;
    kind: 'lan' | 'vpn';
    admitted: boolean;
    url: string | null;
    in_certificate: boolean | null;
    single_address: boolean;
}

interface Refusal {
    value: string;
    reason: string;
}

interface State {
    enabled: boolean;
    access_port: number;
    interfaces: Iface[];
    allowed: string[];
    refused: Refusal[];
    vpn_only: boolean;
    vpn_networks: string[];
    mesh_vpn_network: string;
    mesh_vpn_admitted: boolean;
    your_address: string;
}

// The refusal codes of vaf/network/binding.py REFUSAL_REASONS, each worded in the catalogue.
const REFUSAL_CODES = ['not_private', 'everything', 'ipv6', 'loopback', 'invalid'];

/**
 * Remote access over a VPN (vaf/network/binding.py inbound_policy, /api/network/remote-access):
 * the LAN and VPN connections VAF detected with their access URL, and who is admitted - the local
 * networks, Tailscale/NetBird by switch, networks of the admin's own, or "VPN only". VAF runs no
 * VPN itself. A change that would shut out the address this page is open from is answered with
 * 409 first and saved only after the person confirms. The CLI half is `vaf server networks`.
 */
export default function RemoteAccessSection({ sectionId }: { sectionId?: string }) {
    const t = useTranslations('settings.localNetwork.remoteAccess');
    const tCommon = useTranslations('common');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [data, setData] = useState<State | null>(null);
    const [draft, setDraft] = useState('');
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
    // `draft` is set when the confirmation belongs to an entry typed into the add field, so
    // the field is emptied once the confirmed save went through.
    const [lockout, setLockout] = useState<{ address: string; allowed: string[]; vpnOnly: boolean; draft?: string } | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);

    const load = useCallback(async () => {
        try {
            const res = await fetch(`${apiBase}/api/network/remote-access`, { credentials: 'include' });
            // Not an admin: the section is not theirs, it stays hidden.
            if (res.status === 401 || res.status === 403) { setData(null); setLoadFailed(false); return; }
            // Anything else keeps the last state and says so, instead of the section vanishing.
            if (!res.ok) { setLoadFailed(true); return; }
            setData(await res.json());
            setLoadFailed(false);
        } catch {
            setLoadFailed(true);
        }
    }, [apiBase]);

    useEffect(() => { void load(); }, [load]);

    const refusalText = (r: Refusal) =>
        REFUSAL_CODES.includes(r.reason) ? t(`refused.${r.reason}`, { value: r.value }) : r.value;

    const save = async (allowed: string[], vpnOnly: boolean, confirm = false, draftEntry?: string): Promise<boolean> => {
        setBusy(true);
        setNote(null);
        // A new edit replaces a confirmation still on screen: "Save anyway" must only ever
        // save the change it was shown for, never an older one after this edit failed.
        if (!confirm) setLockout(null);
        try {
            const res = await fetch(`${apiBase}/api/network/remote-access`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                // The state this change was made on: the server refuses the save when the
                // stored settings moved on since (another admin, the CLI), instead of letting
                // this page's older list bring a removed network back.
                body: JSON.stringify({ allowed, vpn_only: vpnOnly, confirm,
                                       base_allowed: data?.allowed ?? null, base_vpn_only: data?.vpn_only ?? null }),
            });
            const body = await res.json().catch(() => ({}));
            if (res.status === 409 && body?.detail?.code === 'stale' && body.detail.state) {
                setData(body.detail.state);
                setLockout(null);
                setNote({ ok: false, text: t('changedMeanwhile') });
                return false;
            }
            if (res.status === 409 && body?.detail?.code === 'lockout') {
                setLockout({ address: String(body.detail.address || ''), allowed, vpnOnly, draft: draftEntry });
                return false;
            }
            if (res.status === 422 && Array.isArray(body?.detail?.refused)) {
                setNote({ ok: false, text: (body.detail.refused as Refusal[]).map(refusalText).join(' ') });
                return false;
            }
            if (!res.ok) { setNote({ ok: false, text: t('failed') }); return false; }
            setData(body);
            setLockout(null);
            setNote({ ok: true, text: t('saved') });
            return true;
        } catch {
            setNote({ ok: false, text: t('failed') });
            return false;
        } finally {
            setBusy(false);
        }
    };

    const copyUrl = async (url: string) => {
        const ok = await copyText(url);
        setNote(ok ? { ok: true, text: t('copied') } : { ok: false, text: t('failed') });
    };

    if (!data) {
        if (!loadFailed) return null;
        return (
            <div id={sectionId} className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 scroll-mt-2">
                <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
                <div className="flex items-center justify-between gap-3">
                    <p className="text-xs text-red-600">{t('loadFailed')}</p>
                    <button type="button" onClick={() => void load()}
                        className="px-3 py-1.5 text-sm rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 shrink-0">
                        {t('retry')}
                    </button>
                </div>
            </div>
        );
    }
    const mesh = data.mesh_vpn_network;
    const own = data.allowed.filter(n => n !== mesh);
    const meshWaiting = !data.mesh_vpn_admitted
        && data.interfaces.some(i => i.kind === 'vpn' && i.network === mesh && !i.admitted);
    const nobodyGetsIn = data.vpn_only && data.vpn_networks.length === 0 && data.allowed.length === 0;

    const toggleMesh = () => {
        const next = data.mesh_vpn_admitted ? data.allowed.filter(n => n !== mesh) : [...data.allowed, mesh];
        void save(next, data.vpn_only);
    };
    const addOwn = (e: FormEvent) => {
        e.preventDefault();
        if (busy) return;
        const entry = draft.trim();
        if (!entry) return;
        void save([...data.allowed, entry], data.vpn_only, false, entry).then(saved => { if (saved) setDraft(''); });
    };

    return (
        <div id={sectionId} className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 scroll-mt-2">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
            <p className="text-xs text-gray-600 mb-4">{t('intro')}</p>

            <p className="text-xs font-semibold text-gray-700 mb-1">{t('connections')}</p>
            {data.interfaces.length === 0 ? (
                <p className="text-sm text-gray-500 mb-4">{t('none')}</p>
            ) : (
                <ul className="flex flex-col gap-2 mb-2">
                    {data.interfaces.map(i => (
                        <li key={`${i.name}-${i.ip}`} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white">
                            {i.kind === 'vpn'
                                ? <Shield className="w-4 h-4 text-gray-500 shrink-0" />
                                : <Network className="w-4 h-4 text-gray-500 shrink-0" />}
                            <span className="text-sm font-mono text-gray-800 shrink-0 max-w-[9rem] truncate" title={i.name}>{i.name}</span>
                            <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 shrink-0">
                                {i.kind === 'vpn' ? t('kindVpn') : t('kindLan')}
                            </span>
                            <div className="flex-1 min-w-0">
                                {i.url ? (
                                    <div className="text-xs font-mono text-green-700 truncate">{i.url}</div>
                                ) : (
                                    <div className="text-xs text-gray-500 truncate">
                                        <span className="font-mono">{i.ip}</span>{tCommon('labelSeparator')}{t('notAdmitted')}
                                    </div>
                                )}
                                {i.admitted && i.in_certificate === false && (
                                    <div className="text-[11px] text-amber-700">{t('notInCertificate')}</div>
                                )}
                                {i.kind === 'vpn' && i.single_address && (
                                    <div className="text-[11px] text-amber-700">{t('singleAddress')}</div>
                                )}
                            </div>
                            {i.url && (
                                <button type="button" onClick={() => void copyUrl(i.url as string)} title={tCommon('copy')} aria-label={tCommon('copy')}
                                    className="p-1.5 rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-100 shrink-0">
                                    <Copy className="w-4 h-4" />
                                </button>
                            )}
                        </li>
                    ))}
                </ul>
            )}
            {meshWaiting && <p className="text-xs text-amber-700 mb-2">{t('meshDetected')}</p>}

            <p className="text-xs font-semibold text-gray-700 mt-4 mb-2">{t('admitted')}</p>
            <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white">
                    <span className={`text-sm ${data.vpn_only ? 'text-gray-400 line-through' : 'text-gray-800'}`}>{t('localNetworks')}</span>
                    <span className="text-xs text-gray-500 text-right">
                        {data.vpn_only ? t('localNetworksOff') : '10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16'}
                    </span>
                </div>
                <ToggleRow label={t('mesh')} description={t('meshDesc')} checked={data.mesh_vpn_admitted}
                    disabled={busy} onChange={toggleMesh} />
                <ToggleRow label={t('vpnOnly')} description={t('vpnOnlyDesc')} checked={data.vpn_only}
                    disabled={busy} onChange={() => void save(data.allowed, !data.vpn_only)} />
                {nobodyGetsIn && <p className="text-xs text-amber-700">{t('vpnOnlyNoVpn')}</p>}
            </div>

            <p className="text-xs font-semibold text-gray-700 mt-4 mb-1">{t('own')}</p>
            {own.length === 0 ? (
                <p className="text-sm text-gray-500 mb-2">{t('ownNone')}</p>
            ) : (
                <ul className="flex flex-col gap-2 mb-2">
                    {own.map(n => (
                        <li key={n} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white">
                            <span className="text-sm font-mono text-gray-800 flex-1 min-w-0 truncate">{n}</span>
                            <button type="button" disabled={busy} title={t('remove')} aria-label={t('remove')}
                                onClick={() => void save(data.allowed.filter(x => x !== n), data.vpn_only)}
                                className="p-1.5 rounded-md text-gray-500 hover:text-red-600 hover:bg-gray-100 disabled:opacity-50">
                                <Trash2 className="w-4 h-4" />
                            </button>
                        </li>
                    ))}
                </ul>
            )}
            <form onSubmit={addOwn} className="flex items-center gap-2">
                <input value={draft} onChange={e => setDraft(e.target.value)} placeholder={t('addPlaceholder')}
                    className="flex-1 min-w-0 px-3 py-2 text-sm font-mono rounded-lg border border-gray-200 bg-white text-gray-900 focus:outline-none focus:ring-2 focus:ring-gray-300" />
                <button type="submit" disabled={busy || !draft.trim()}
                    className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors disabled:opacity-50">
                    {t('add')}
                </button>
            </form>

            {data.refused.length > 0 && (
                <p className="text-xs text-red-600 mt-2">{data.refused.map(refusalText).join(' ')}</p>
            )}
            {note && (
                <p className={`text-xs mt-2 ${note.ok ? 'text-gray-600' : 'text-red-600'}`}>{note.text}</p>
            )}
            {loadFailed && (
                <div className="flex items-center justify-between gap-3 mt-2">
                    <p className="text-xs text-red-600">{t('loadFailed')}</p>
                    <button type="button" onClick={() => void load()}
                        className="px-3 py-1.5 text-sm rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 shrink-0">
                        {t('retry')}
                    </button>
                </div>
            )}

            {lockout && (
                <div className="mt-4 p-4 rounded-lg border border-amber-200 bg-amber-50" role="alertdialog">
                    <p className="text-sm font-semibold text-amber-800 mb-1">{t('lockoutTitle')}</p>
                    <p className="text-xs text-amber-800 mb-3">{t('lockoutText', { address: lockout.address })}</p>
                    <div className="flex items-center justify-end gap-2">
                        <button type="button" onClick={() => setLockout(null)}
                            className="px-3 py-1.5 text-sm rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50">
                            {tCommon('cancel')}
                        </button>
                        <button type="button" disabled={busy}
                            onClick={() => {
                                const typed = lockout.draft;
                                void save(lockout.allowed, lockout.vpnOnly, true).then(saved => {
                                    if (saved && typed !== undefined) setDraft(current => (current.trim() === typed ? '' : current));
                                });
                            }}
                            className="px-3 py-1.5 text-sm font-medium rounded-lg bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-50">
                            {t('lockoutConfirm')}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}

function ToggleRow({ label, description, checked, disabled, onChange }: {
    label: string; description: string; checked: boolean; disabled?: boolean; onChange: () => void;
}) {
    return (
        <div className="flex items-start justify-between gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white">
            <div className="flex flex-col gap-0.5 min-w-0">
                <span className="text-sm font-medium text-gray-700">{label}</span>
                <span className="text-xs text-gray-400">{description}</span>
            </div>
            <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={onChange}
                className={`w-11 h-6 rounded-full transition-colors relative shrink-0 disabled:opacity-50 ${checked ? 'bg-gray-800 dark:bg-[#d9d9d9]' : 'bg-gray-200 dark:bg-[#333333]'}`}>
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow-sm transition-transform duration-200 ${checked ? 'translate-x-5 dark:bg-[#1a1a1a]' : 'translate-x-0 dark:bg-[#e8e8e8]'}`} />
            </button>
        </div>
    );
}
