// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useState, type ChangeEvent, type ReactNode } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Copy, Download, ShieldAlert, ShieldCheck, Upload } from 'lucide-react';
import Skeleton, { SkeletonBar } from '@/components/ui/Skeleton';
import { copyText } from '@/lib/clipboard';

interface Facts {
    subject: string;
    issuer: string;
    names: string[];
    not_before: string;
    not_after: string;
    days_left: number;
    key_type: string;
    key_size: number | string | null;
    sha256: string;
    authority_subject: string;
    authority_sha256: string;
    self_signed: boolean;
    chain_length: number;
}

interface State {
    tls: boolean;
    source: 'own' | 'automatic' | 'none';
    own_configured: boolean;
    own_path: string | null;
    problem: { code: string; text: string } | null;
    certificate: Facts | null;
    authority: { subject: string; fingerprint: string } | null;
    local_ca: {
        path: string; fingerprint: string; not_after: string; restricted: boolean;
        // This machine's names a new authority leaves out of VAF's certificate.
        left_out_on_renewal?: string[];
    } | null;
    uncovered: string[];
    // <host>.local, which a new authority keeps (mDNS).
    mdns_name?: string | null;
    step_up_required: boolean;
}

interface ReportItem {
    code: string;
    text: string;
    params: Record<string, string | number>;
}

interface Report {
    ok: boolean;
    errors: ReportItem[];
    warnings: ReportItem[];
    facts: Partial<Facts>;
}

interface ReloadResult {
    ok: boolean;
    listeners: Record<string, string>;
    refused: string | null;
    source: string;
}

type Part = 'certificate' | 'key' | 'chain';
type Picked = Partial<Record<Part, { name: string; text: string }>>;

// The codes of vaf/network/ssl_utils.py REPORT_REASONS, each worded in the catalogue; an
// unknown code shows the server's English text.
const REASON_CODES = [
    'too_large', 'unreadable', 'missing', 'no_key', 'key_encrypted', 'key_mismatch', 'expired',
    'not_yet_valid', 'not_for_servers', 'weak_key', 'chain_broken', 'no_names', 'expires_soon',
    'names_missing', 'no_key_identifiers', 'no_issuer', 'authority_changes',
];
// One part of an upload, as the server holds it.
const PART_MAX_BYTES = 64 * 1024;
const PARTS: Part[] = ['certificate', 'key', 'chain'];
const BTN_SECONDARY = 'px-3 py-1.5 text-sm rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 disabled:opacity-50';
const BTN_PRIMARY = 'px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors disabled:opacity-50';

/** AB:CD:..., the way browsers and operating systems show a fingerprint. */
function colonFingerprint(hex: string): string {
    return (hex || '').toUpperCase().match(/.{1,2}/g)?.join(':') ?? '';
}

/** A fingerprint that wraps after a colon, never inside a pair: a person compares it pair by
 *  pair. <wbr> adds no character, so a selection copies the fingerprint as it is. */
function Fingerprint({ hex }: { hex: string }) {
    const pairs = colonFingerprint(hex).split(':');
    return (
        <span className="block text-xs font-mono text-gray-800 break-words">
            {pairs.map((pair, i) => (
                <span key={i}>{pair}{i < pairs.length - 1 && <>:<wbr /></>}</span>
            ))}
        </span>
    );
}

/** A fingerprint inside a sentence: the same break points, the sentence's own style. */
function inlineFingerprint(chunks: ReactNode) {
    const text = ([] as ReactNode[]).concat(chunks).join('');
    const pairs = text.split(':');
    return (
        <span className="font-mono break-words">
            {pairs.map((pair, i) => (
                <span key={i}>{pair}{i < pairs.length - 1 && <>:<wbr /></>}</span>
            ))}
        </span>
    );
}

/** A command inside a sentence. It wraps only between its words: a break at the hyphen of
 *  "-out" (the browser's default) gave a line a copy turns into a wrong command. */
function inlineCommand(chunks: ReactNode) {
    const words = ([] as ReactNode[]).concat(chunks).join('').split(' ');
    return (
        <code className="font-mono text-[0.95em] px-1 rounded bg-gray-100 text-gray-800">
            {words.map((word, i) => (
                <span key={i} className="whitespace-nowrap">{word}{i < words.length - 1 ? ' ' : ''}</span>
            ))}
        </code>
    );
}

// The tags the catalogue marks commands and fingerprints with (t.rich).
const RICH = { code: inlineCommand, fp: inlineFingerprint };

/** The common name of a distinguished name ("O=VAF,CN=VAF Local Network CA"), else all of it. */
function commonName(dn: string): string {
    const match = /(?:^|,)CN=((?:\\,|[^,])+)/.exec(dn || '');
    return match ? match[1].replace(/\\,/g, ',') : dn;
}

/**
 * The certificate every device is served (vaf/network/ssl_utils.py, /api/network/certificate):
 * which one is in use, VAF's certificate authority with its fingerprint to compare, and a
 * certificate of the person's own, checked before it is used and served without a restart.
 * A signed-in admin gives the 2FA code for a change. The terminal half is `vaf server cert` /
 * `vaf server ca`.
 */
export default function CertificateSection({ sectionId }: { sectionId?: string }) {
    const t = useTranslations('settings.localNetwork.certificate');
    const tCommon = useTranslations('common');
    const locale = useLocale();
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [data, setData] = useState<State | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);
    // The route said this section is not the caller's (401/403); until any answer the section
    // stands with its title and a placeholder.
    const [hidden, setHidden] = useState(false);
    const [picked, setPicked] = useState<Picked>({});
    const [report, setReport] = useState<Report | null>(null);
    const [code, setCode] = useState('');
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<{ ok: boolean; text: ReactNode } | null>(null);
    const [confirming, setConfirming] = useState<'restrict' | 'reset' | null>(null);

    const load = useCallback(async () => {
        try {
            const res = await fetch(`${apiBase}/api/network/certificate`, { credentials: 'include' });
            // Not an admin: the section is not theirs, it stays hidden.
            if (res.status === 401 || res.status === 403) { setData(null); setLoadFailed(false); setHidden(true); return; }
            setHidden(false);
            if (!res.ok) { setLoadFailed(true); return; }
            setData(await res.json());
            setLoadFailed(false);
        } catch {
            setLoadFailed(true);
        }
    }, [apiBase]);

    useEffect(() => { void load(); }, [load]);

    const date = (iso: string | undefined) => {
        if (!iso) return '';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        // A date without a time (a report's "expired on") is a calendar day: read in UTC, it is
        // not shown a day early west of UTC.
        return d.toLocaleDateString(locale, /^\d{4}-\d{2}-\d{2}$/.test(iso) ? { timeZone: 'UTC' } : undefined);
    };

    const reason = (item: { code: string; text: string; params?: Record<string, string | number> }): ReactNode => {
        if (!REASON_CODES.includes(item.code)) return item.text;
        const params: Record<string, string | number> = { ...(item.params || {}) };
        if (typeof params.date === 'string') params.date = date(params.date);
        return t.rich(`reasons.${item.code}`, { ...params, ...RICH });
    };

    const copy = async (text: string) => {
        const ok = await copyText(text);
        setNote(ok ? { ok: true, text: t('copied') } : { ok: false, text: t('failed') });
    };

    const pick = async (part: Part, e: ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        e.currentTarget.value = '';
        setReport(null);
        setNote(null);
        if (!file) return;
        if (file.size > PART_MAX_BYTES) {
            setNote({ ok: false, text: t('tooLarge', { name: file.name }) });
            return;
        }
        const text = await file.text();
        setPicked(current => ({ ...current, [part]: { name: file.name, text } }));
    };

    const body = () => ({
        certificate: picked.certificate?.text ?? '',
        key: picked.key?.text ?? '',
        chain: picked.chain?.text ?? '',
    });

    const reloadNote = (reload: ReloadResult | null | undefined) => {
        if (!reload) return { ok: true, text: t('servedLater') };
        return reload.ok ? { ok: true, text: t('servedNow') } : { ok: false, text: t('restartNeeded') };
    };

    // The answer to a change: the second factor's refusals worded, everything else generic.
    const refusal = async (res: Response): Promise<ReactNode> => {
        const answer = await res.json().catch(() => ({}));
        if (res.status === 422 && answer?.detail?.code === 'refused') {
            setReport(answer.detail.report as Report);
            return t('refused');
        }
        if (res.status === 429) return t('codeThrottled');
        if (res.status === 400) return t.rich('codeUnavailable', RICH);
        if (res.status === 403 && data?.step_up_required) return t('codeWrong');
        return t('failed');
    };

    const check = async () => {
        setBusy(true);
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/network/certificate/check`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body()),
            });
            if (!res.ok) { setNote({ ok: false, text: t('failed') }); return; }
            setReport(await res.json());
        } catch {
            setNote({ ok: false, text: t('failed') });
        } finally {
            setBusy(false);
        }
    };

    // One request for each change: install (PUT), back to VAF's own (DELETE), new authority.
    const change = async (method: 'PUT' | 'DELETE' | 'POST', path: string, payload: object) => {
        setBusy(true);
        setNote(null);
        try {
            const res = await fetch(`${apiBase}${path}`, {
                method, credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...payload, code }),
            });
            if (!res.ok) { setNote({ ok: false, text: await refusal(res) }); return; }
            const answer = await res.json();
            setData(answer.state);
            setCode('');
            setConfirming(null);
            if (method === 'PUT') { setPicked({}); setReport(null); }
            const reload: ReloadResult | null | undefined = answer.result?.reload;
            if (path.endsWith('/authority')) {
                // An own certificate stays served through a renewal: nothing was reloaded.
                setNote(!reload ? { ok: true, text: t('renewedOwnStays') }
                    : reload.ok ? { ok: true, text: t('renewedNow') }
                    : { ok: false, text: t('restartNeeded') });
            } else {
                setNote(reloadNote(reload));
            }
        } catch {
            setNote({ ok: false, text: t('failed') });
        } finally {
            setBusy(false);
        }
    };

    if (!data) {
        if (hidden) return null;
        if (!loadFailed) return (
            <div id={sectionId} className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 scroll-mt-2">
                <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
                <p className="text-xs text-gray-600 mb-4">{t('intro')}</p>
                {/* The certificate in use, VAF's authority and the own-certificate controls, at
                    their loaded height with VAF's own certificate in use (measured). */}
                <Skeleton className="h-[770px] max-md:h-[1040px]">
                    <SkeletonBar className="h-2.5 w-28 mb-2" />
                    <SkeletonBar className="h-40 rounded-lg" />
                    <SkeletonBar className="h-2.5 w-36 mt-5 mb-2" />
                    <SkeletonBar className="h-56 rounded-lg" />
                    <SkeletonBar className="h-2.5 w-40 mt-5 mb-2" />
                    <SkeletonBar className="h-2.5 w-3/4 mb-3" />
                    <SkeletonBar className="h-40 rounded-lg" />
                </Skeleton>
            </div>
        );
        return (
            <div id={sectionId} className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 scroll-mt-2">
                <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
                <div className="flex items-center justify-between gap-3">
                    <p className="text-xs text-red-600">{t('loadFailed')}</p>
                    <button type="button" onClick={() => void load()} className={`${BTN_SECONDARY} shrink-0`}>
                        {t('retry')}
                    </button>
                </div>
            </div>
        );
    }

    const facts = data.certificate;
    const ca = data.local_ca;
    const codeReady = !data.step_up_required || /^\d{6}$/.test(code.trim());
    const canApply = !!report && report.ok && !!picked.certificate && !busy && codeReady;
    const daysTone = !facts ? '' : facts.days_left < 0 ? 'text-red-700' : facts.days_left < 30 ? 'text-amber-700' : 'text-gray-500';

    const codeField = data.step_up_required && (
        <label className="flex flex-col gap-1 text-xs text-gray-600">
            {t('code')}
            <input value={code} onChange={e => setCode(e.target.value.replace(/[^0-9]/g, '').slice(0, 6))}
                inputMode="numeric" autoComplete="one-time-code" placeholder={t('codePlaceholder')}
                className="w-40 max-w-full px-3 py-2 text-sm font-mono tracking-widest rounded-lg border border-gray-200 bg-white text-gray-900 focus:outline-none focus:ring-2 focus:ring-gray-300" />
        </label>
    );

    return (
        <div id={sectionId} className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 scroll-mt-2">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
            <p className="text-xs text-gray-600 mb-4">{t('intro')}</p>

            {data.problem && (
                <div className="mb-4 p-3 rounded-lg border border-red-200 bg-red-50" role="alert">
                    <p className="text-sm font-semibold text-red-700 mb-1">{t('problemTitle')}</p>
                    <p className="text-xs text-red-700 break-words">{reason(data.problem)}</p>
                    <p className="text-xs text-red-700 break-words">{t('problemText', { path: data.own_path ?? '' })}</p>
                </div>
            )}

            {/* The certificate in use */}
            <p className="text-xs font-semibold text-gray-700 mb-1">{t('inUse')}</p>
            <div className="flex flex-col gap-2 px-3 py-3 rounded-lg border border-gray-200 bg-white">
                {!data.tls ? (
                    <p className="text-sm text-gray-500">{t('tlsOff')}</p>
                ) : !facts ? (
                    <p className="text-sm text-gray-500">{t('notIssued')}</p>
                ) : (
                    <>
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
                                {data.source === 'own' ? t('sourceOwn') : t('sourceAutomatic')}
                            </span>
                            <span className="text-xs text-gray-500 min-w-0 break-words">
                                {t('issuedBy', { issuer: commonName(facts.issuer) })}
                            </span>
                        </div>
                        {facts.names.length > 0 && (
                            <div className="flex flex-wrap gap-1.5">
                                {facts.names.map(name => (
                                    <span key={name} className="text-[11px] font-mono px-1.5 py-0.5 rounded border border-gray-200 bg-gray-50 text-gray-700 break-words min-w-0">
                                        {name}
                                    </span>
                                ))}
                            </div>
                        )}
                        <p className={`text-xs ${daysTone}`}>
                            {facts.days_left < 0
                                ? t('expiredOn', { date: date(facts.not_after) })
                                : t('validUntil', { date: date(facts.not_after), days: facts.days_left })}
                        </p>
                        <div className="flex items-start gap-2">
                            <div className="flex-1 min-w-0">
                                <div className="text-[11px] text-gray-500">{t('fingerprint')}</div>
                                <Fingerprint hex={facts.sha256} />
                            </div>
                            <button type="button" onClick={() => void copy(colonFingerprint(facts.sha256))}
                                title={tCommon('copy')} aria-label={tCommon('copy')}
                                className="p-1.5 rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-100 shrink-0">
                                <Copy className="w-4 h-4" />
                            </button>
                        </div>
                    </>
                )}
            </div>
            {data.uncovered.length > 0 && (
                <p className="text-xs text-amber-700 mt-2 break-words">{t('uncovered', { names: data.uncovered.join(', ') })}</p>
            )}

            {/* VAF's own certificate authority */}
            {ca && (
                <>
                    <p className="text-xs font-semibold text-gray-700 mt-4 mb-1">{t('authorityTitle')}</p>
                    <div className="flex flex-col gap-2 px-3 py-3 rounded-lg border border-gray-200 bg-white">
                        <p className="text-xs text-gray-600">{t('authorityIntro')}</p>
                        <div className="flex items-start gap-2">
                            <div className="flex-1 min-w-0">
                                <div className="text-[11px] text-gray-500">{t('fingerprint')}</div>
                                <Fingerprint hex={ca.fingerprint} />
                            </div>
                            <button type="button" onClick={() => void copy(colonFingerprint(ca.fingerprint))}
                                title={tCommon('copy')} aria-label={tCommon('copy')}
                                className="p-1.5 rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-100 shrink-0">
                                <Copy className="w-4 h-4" />
                            </button>
                        </div>
                        <p className="text-xs text-gray-500">{t.rich('compare', RICH)}</p>
                        <div className={`flex items-center gap-1.5 text-xs ${ca.restricted ? 'text-green-700' : 'text-amber-700'}`}>
                            {ca.restricted ? <ShieldCheck className="w-4 h-4 shrink-0" /> : <ShieldAlert className="w-4 h-4 shrink-0" />}
                            <span>{ca.restricted ? t('restricted') : t('unrestricted')}</span>
                        </div>
                        <div className="flex flex-wrap items-center gap-2">
                            {/* The download serves the authority of the served chain, which is
                                this one only while VAF's own certificate is in use. */}
                            {data.source === 'automatic' && (
                                <a href={`${apiBase}/api/a2a/ca.pem`} download="vaf-authority.pem"
                                    className={`${BTN_SECONDARY} inline-flex items-center gap-1.5`}>
                                    <Download className="w-4 h-4" />{t('download')}
                                </a>
                            )}
                            {!ca.restricted && (
                                <button type="button" disabled={busy} onClick={() => { setConfirming('restrict'); setCode(''); setNote(null); }}
                                    className={BTN_SECONDARY}>
                                    {t('restrict')}
                                </button>
                            )}
                        </div>
                    </div>
                    {confirming === 'restrict' && (
                        <div className="mt-3 p-4 rounded-lg border border-amber-200 bg-amber-50" role="alertdialog" aria-label={t('restrictTitle')}>
                            <p className="text-sm font-semibold text-amber-800 mb-1">{t('restrictTitle')}</p>
                            <p className="text-xs text-amber-800 mb-3">{t('restrictText')}</p>
                            {(ca.left_out_on_renewal ?? []).length > 0 && (
                                <p className="text-xs text-amber-800 mb-3 break-words">
                                    {data.mdns_name && !(ca.left_out_on_renewal ?? []).includes(data.mdns_name)
                                        ? t('restrictLeavesOutMdns', { names: (ca.left_out_on_renewal ?? []).join(', '), mdns: data.mdns_name })
                                        : t('restrictLeavesOut', { names: (ca.left_out_on_renewal ?? []).join(', ') })}
                                </p>
                            )}
                            {codeField && <div className="mb-3">{codeField}</div>}
                            <div className="flex flex-wrap items-center justify-end gap-2">
                                <button type="button" onClick={() => setConfirming(null)} className={BTN_SECONDARY}>
                                    {tCommon('cancel')}
                                </button>
                                <button type="button" disabled={busy || !codeReady}
                                    onClick={() => void change('POST', '/api/network/certificate/authority', { confirm: true })}
                                    className="px-3 py-1.5 text-sm font-medium rounded-lg bg-amber-600 hover:bg-amber-700 text-white disabled:bg-gray-200 disabled:text-gray-500 disabled:hover:bg-gray-200">
                                    {t('restrictConfirm')}
                                </button>
                            </div>
                        </div>
                    )}
                </>
            )}

            {/* A certificate of the person's own */}
            <p className="text-xs font-semibold text-gray-700 mt-4 mb-1">{t('ownTitle')}</p>
            <p className="text-xs text-gray-600 mb-1">{t('ownIntro')}</p>
            <p className="text-xs text-amber-700 mb-3">{t.rich('keyHint', RICH)}</p>
            <div className="flex flex-col gap-2">
                {PARTS.map(part => (
                    <div key={part} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 rounded-lg border border-gray-200 bg-white">
                        <span className="flex flex-col flex-1 min-w-0 max-md:basis-full">
                            <span className="text-sm text-gray-700">{t(`parts.${part}`)}</span>
                            {part === 'chain' && <span className="text-xs text-gray-500">{t('parts.chainHint')}</span>}
                        </span>
                        <span className={`text-xs text-gray-500 min-w-0 break-all ${picked[part] ? 'font-mono' : ''}`}>{picked[part]?.name ?? t('noFile')}</span>
                        <label className={`${BTN_SECONDARY} inline-flex items-center gap-1.5 cursor-pointer shrink-0`}>
                            <Upload className="w-4 h-4" />{t('choose')}
                            <input type="file" accept=".pem,.crt,.cer,.key,.txt" className="hidden" onChange={e => void pick(part, e)} />
                        </label>
                    </div>
                ))}
            </div>
            <div className="flex flex-wrap items-center gap-2 mt-3">
                <button type="button" disabled={busy || !picked.certificate} onClick={() => void check()} className={BTN_SECONDARY}>
                    {t('check')}
                </button>
            </div>

            {report && (
                <div className="mt-3 flex flex-col gap-2 px-3 py-3 rounded-lg border border-gray-200 bg-white">
                    {report.ok && report.warnings.length === 0 && (
                        <p className="text-xs text-green-700">{t('reportOk')}</p>
                    )}
                    {report.errors.map(item => (
                        <p key={`e-${item.code}`} className="text-xs text-red-700 break-words">{reason(item)}</p>
                    ))}
                    {report.warnings.map(item => (
                        <p key={`w-${item.code}`} className="text-xs text-amber-700 break-words">{reason(item)}</p>
                    ))}
                    {report.facts?.sha256 && (
                        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs mt-1">
                            <dt className="text-gray-500">{t('factSubject')}</dt>
                            <dd className="text-gray-800 min-w-0 break-words">{commonName(report.facts.subject || '')}</dd>
                            <dt className="text-gray-500">{t('factIssuer')}</dt>
                            <dd className="text-gray-800 min-w-0 break-words">{commonName(report.facts.issuer || '')}</dd>
                            <dt className="text-gray-500">{t('factNames')}</dt>
                            <dd className="text-gray-800 min-w-0 break-words font-mono">{(report.facts.names || []).join(', ') || '-'}</dd>
                            <dt className="text-gray-500">{t('factValid')}</dt>
                            <dd className="text-gray-800">{date(report.facts.not_after)}</dd>
                            <dt className="text-gray-500">{t('factAuthority')}</dt>
                            <dd className="min-w-0"><Fingerprint hex={report.facts.authority_sha256 || ''} /></dd>
                        </dl>
                    )}
                    {report.ok && (
                        <div className="flex flex-col gap-2 mt-2 pt-2 border-t border-gray-100">
                            <p className="text-xs text-amber-700">{t('applyWarning')}</p>
                            {codeField}
                            <div className="flex flex-wrap items-center gap-2">
                                <button type="button" disabled={!canApply}
                                    onClick={() => void change('PUT', '/api/network/certificate', body())}
                                    className={BTN_PRIMARY}>
                                    {t('apply')}
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            )}

            {/* Back to VAF's own: also while a configured own certificate cannot be used */}
            {(data.source === 'own' || data.own_configured) && (
                <div className="mt-4 flex flex-col gap-2">
                    {confirming === 'reset' ? (
                        <div className="p-4 rounded-lg border border-amber-200 bg-amber-50" role="alertdialog" aria-label={t('reset')}>
                            <p className="text-xs text-amber-800 mb-3">{t('resetText')}</p>
                            {codeField && <div className="mb-3">{codeField}</div>}
                            <div className="flex flex-wrap items-center justify-end gap-2">
                                <button type="button" onClick={() => setConfirming(null)} className={BTN_SECONDARY}>
                                    {tCommon('cancel')}
                                </button>
                                <button type="button" disabled={busy || !codeReady}
                                    onClick={() => void change('DELETE', '/api/network/certificate', {})}
                                    className="px-3 py-1.5 text-sm font-medium rounded-lg bg-amber-600 hover:bg-amber-700 text-white disabled:bg-gray-200 disabled:text-gray-500 disabled:hover:bg-gray-200">
                                    {t('resetConfirm')}
                                </button>
                            </div>
                        </div>
                    ) : (
                        <div>
                            <button type="button" disabled={busy} onClick={() => { setConfirming('reset'); setCode(''); setNote(null); }}
                                className={BTN_SECONDARY}>
                                {t('reset')}
                            </button>
                        </div>
                    )}
                </div>
            )}

            {note && (
                <p className={`text-xs mt-3 ${note.ok ? 'text-gray-600' : 'text-red-600'}`} role="status">{note.text}</p>
            )}
            {loadFailed && (
                <div className="flex items-center justify-between gap-3 mt-2">
                    <p className="text-xs text-red-600">{t('loadFailed')}</p>
                    <button type="button" onClick={() => void load()} className={`${BTN_SECONDARY} shrink-0`}>
                        {t('retry')}
                    </button>
                </div>
            )}
        </div>
    );
}
