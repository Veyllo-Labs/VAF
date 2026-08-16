'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle, Boxes, Check, ExternalLink, Loader2, RefreshCw, Server, Wrench, X, XCircle,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Background, Controls, Position, useEdgesState, useNodesState } from 'reactflow';
import 'reactflow/dist/style.css';

import { getApiBase } from '@/lib/utils';
import { compareVersions, formatVersion } from '@/lib/version';
import { useThemeStore } from '@/lib/themeStore';

const ReactFlow = lazy(() => import('reactflow').then((mod) => ({ default: mod.default })));

// ─── Types (tolerant: the backend may grow fields, an old one may lack them) ──

export interface ServiceRow {
    name: string;
    service_key: string;
    required: boolean;
    exists: boolean | null;
    running: boolean;
    health?: string | null;
    configured_port?: number | null;
    port_mismatch?: boolean;
    probe_ok?: boolean | null;
    state?: string;
    reason?: string;
}

interface ServicesSnapshot {
    docker: { available: boolean; reason?: string; detail?: string };
    stack_root?: string | null;
    services: ServiceRow[];
}

interface RepairStep {
    step: string;
    action?: string;
    ok: boolean;
    message?: string;
}

/** Where the update side of the dialog is. The waiting state is the interesting
 *  one: the server it is talking to is gone on purpose while it holds. */
type UpdatePhase =
    | { kind: 'idle' }
    | { kind: 'checking' }
    | { kind: 'upToDate' }
    | { kind: 'available'; latest: string; releaseUrl?: string }
    | { kind: 'confirm'; latest: string; releaseUrl?: string }
    | { kind: 'applying'; latest: string }
    | { kind: 'waiting'; from: string; to: string; startedAt: number }
    | { kind: 'done'; to: string }
    | { kind: 'timeout'; from: string; to: string };

export interface UpdateRepairModalProps {
    currentUser?: { role?: string } | null;
    onClose: () => void;
}

// Status colors are deliberately identical in both themes: they carry meaning,
// not styling (docs/web-ui/DARKMODE.md protects semantic status colors).
const COLOR = {
    ok: '#22c55e',
    degraded: '#f59e0b',
    down: '#ef4444',
    idle: '#9ca3af',
};

type Health = 'ok' | 'degraded' | 'down' | 'idle';

/** One service's traffic light. A container that RUNS but does not answer is
 *  amber, not green: "running" was never the question. */
export function healthOf(svc: ServiceRow): Health {
    if (svc.state === 'unknown') return 'idle';
    if (svc.exists === false || svc.exists === null) return svc.required ? 'down' : 'idle';
    if (!svc.running) return svc.required ? 'down' : 'idle';
    if (svc.port_mismatch) return 'degraded';
    if (svc.probe_ok === false) return svc.required ? 'down' : 'degraded';
    if (svc.health && !['healthy', 'none', ''].includes(svc.health)) return 'degraded';
    return 'ok';
}

const SESSION_PENDING = 'vaf_update_pending';
const SESSION_DONE = 'vaf_update_done';
const WAIT_TIMEOUT_MS = 10 * 60 * 1000;
/** Consecutive unanswered repair polls before the dialog stops waiting (about 30s). */
const REPAIR_POLL_MISS_LIMIT = 20;

function readSession(key: string): any {
    try {
        const raw = sessionStorage.getItem(key);
        return raw ? JSON.parse(raw) : null;
    } catch {
        return null;
    }
}

function writeSession(key: string, value: any): void {
    try {
        sessionStorage.setItem(key, JSON.stringify(value));
    } catch {
        /* private mode: the resume convenience is lost, nothing else */
    }
}

function clearSession(key: string): void {
    try {
        sessionStorage.removeItem(key);
    } catch {
        /* nothing to clean up */
    }
}

function mmss(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
}

export default function UpdateRepairModal({ currentUser, onClose }: UpdateRepairModalProps) {
    const tM = useTranslations('modals.updateRepair');
    const tCommon = useTranslations('common');
    const isDark = useThemeStore((st: any) => st.theme) === 'dark';
    const isAdmin = currentUser?.role === 'admin';

    const [version, setVersion] = useState<string | null>(null);
    const [checkedAt, setCheckedAt] = useState<string | null>(null);
    const [canApply, setCanApply] = useState(true);
    const [applyReason, setApplyReason] = useState('');
    const [unfinished, setUnfinished] = useState(false);
    const [phase, setPhase] = useState<UpdatePhase>({ kind: 'idle' });
    const [updateError, setUpdateError] = useState<string | null>(null);
    const [justUpdatedTo, setJustUpdatedTo] = useState<string | null>(null);
    const [elapsed, setElapsed] = useState(0);

    const [services, setServices] = useState<ServicesSnapshot | null>(null);
    const [servicesError, setServicesError] = useState<string | null>(null);
    const [repairBusy, setRepairBusy] = useState(false);
    const [repairSteps, setRepairSteps] = useState<RepairStep[] | null>(null);
    const [repairError, setRepairError] = useState<string | null>(null);
    const [repairOk, setRepairOk] = useState<boolean | null>(null);

    const inFlight = useRef(false);
    // Set when the dialog goes away, so the repair poll below stops instead of
    // running on against a component nobody is looking at any more.
    const closed = useRef(false);
    const phaseRef = useRef<UpdatePhase>(phase);
    phaseRef.current = phase;

    useEffect(() => {
        closed.current = false;
        return () => { closed.current = true; };
    }, []);

    const api = (path: string) => `${getApiBase()}${path}`;

    // ─── services ────────────────────────────────────────────────────────────

    const loadServices = useCallback(async () => {
        if (inFlight.current) return;
        inFlight.current = true;
        try {
            const res = await fetch(api('/api/system/services'), { credentials: 'include' });
            if (!res.ok) {
                const d = await res.json().catch(() => null);
                setServicesError(String(d?.detail ?? `HTTP ${res.status}`));
            } else {
                setServices(await res.json());
                setServicesError(null);
            }
        } catch (e: any) {
            setServicesError(String(e?.message ?? e));
        } finally {
            inFlight.current = false;
        }
    }, []);

    const pollRepair = useCallback(async () => {
        // The run outlives any single request, so its state is fetched rather
        // than awaited: a container engine start can take minutes. Two exits
        // besides success, because without them the Repair button would stay
        // disabled for the life of the page: the dialog closing, and a server
        // that has stopped answering entirely.
        let misses = 0;
        for (;;) {
            await new Promise((r) => setTimeout(r, 1500));
            if (closed.current) return;
            let state: any = null;
            try {
                const res = await fetch(api('/api/system/services/repair'), { credentials: 'include' });
                if (res.ok) state = await res.json();
            } catch {
                /* the repair may restart docker under us; keep watching */
            }
            if (!state) {
                misses += 1;
                if (misses >= REPAIR_POLL_MISS_LIMIT) {
                    setRepairError('lost contact with the server while repairing');
                    return;
                }
                continue;
            }
            misses = 0;
            setRepairSteps(state.steps ?? []);
            if (!state.running) {
                if (state.error) setRepairError(String(state.error));
                setRepairOk(Boolean(state.result?.ok));
                return;
            }
        }
    }, []);

    const runRepair = useCallback(async () => {
        setRepairBusy(true);
        setRepairError(null);
        setRepairSteps([]);
        setRepairOk(null);
        try {
            const res = await fetch(api('/api/system/services/repair'), {
                method: 'POST',
                credentials: 'include',
            });
            if (!res.ok) {
                const d = await res.json().catch(() => null);
                setRepairError(String(d?.detail ?? `HTTP ${res.status}`));
                return;
            }
            await pollRepair();
        } catch (e: any) {
            setRepairError(String(e?.message ?? e));
        } finally {
            setRepairBusy(false);
            loadServices();
        }
    }, [loadServices, pollRepair]);

    // ─── update ──────────────────────────────────────────────────────────────

    const loadUpdateState = useCallback(async () => {
        try {
            const res = await fetch(api('/api/system/update'), { credentials: 'include' });
            if (!res.ok) return;
            const data = await res.json();
            setVersion(data.current ?? null);
            setCheckedAt(data.cache?.checked_at ?? null);
            setCanApply(Boolean(data.can_apply));
            setApplyReason(String(data.reason ?? ''));
            setUnfinished(Boolean(data.last_update));
        } catch {
            /* the dialog still works without it; the version card just stays empty */
        }
    }, []);

    const checkForUpdates = useCallback(async () => {
        setPhase({ kind: 'checking' });
        setUpdateError(null);
        try {
            const res = await fetch(api('/api/system/update/check'), {
                method: 'POST',
                credentials: 'include',
            });
            const data = await res.json().catch(() => null);
            if (!res.ok) {
                setUpdateError(tM('checkFailed', { message: String(data?.detail ?? `HTTP ${res.status}`) }));
                setPhase({ kind: 'idle' });
                return;
            }
            if (data?.checked_at) setCheckedAt(String(data.checked_at));
            if (typeof data?.can_apply === 'boolean') setCanApply(data.can_apply);
            if (data?.reason) setApplyReason(String(data.reason));
            if (!data?.latest) {
                setUpdateError(tM('checkFailed', { message: String(data?.message ?? '') }));
                setPhase({ kind: 'idle' });
                return;
            }
            setPhase(data.relevant
                ? { kind: 'available', latest: String(data.latest), releaseUrl: data.release_url }
                : { kind: 'upToDate' });
        } catch (e: any) {
            setUpdateError(tM('checkFailed', { message: String(e?.message ?? e) }));
            setPhase({ kind: 'idle' });
        }
    }, []);

    const applyUpdate = useCallback(async (latest: string) => {
        const from = version ?? '';
        setUpdateError(null);
        setPhase({ kind: 'applying', latest });
        // Written BEFORE the request: the server may stop before it answers,
        // and this is what lets the waiting screen come back after a reload.
        writeSession(SESSION_PENDING, { from, to: latest, startedAt: Date.now() });
        try {
            const res = await fetch(api('/api/system/update/apply'), {
                method: 'POST',
                credentials: 'include',
            });
            if (!res.ok) {
                const d = await res.json().catch(() => null);
                clearSession(SESSION_PENDING);
                setUpdateError(tM('applyFailed', { message: String(d?.detail ?? `HTTP ${res.status}`) }));
                setPhase({ kind: 'available', latest });
                return;
            }
            setPhase({ kind: 'waiting', from, to: latest, startedAt: Date.now() });
        } catch {
            // A network-level failure here is ambiguous and the safe reading is
            // "it started": the update stops this very server, so the answer can
            // legitimately never arrive. Waiting shows the truth either way.
            setPhase({ kind: 'waiting', from, to: latest, startedAt: Date.now() });
        }
    }, [version]);

    // ─── lifecycle ───────────────────────────────────────────────────────────

    useEffect(() => {
        let cancelled = false;
        (async () => {
            const done = readSession(SESSION_DONE);
            if (done?.to) {
                setJustUpdatedTo(String(done.to));
                clearSession(SESSION_DONE);
            }
            let current: string | null = null;
            try {
                const res = await fetch(api('/api/version'), { credentials: 'include' });
                if (res.ok) current = String((await res.json()).version ?? '');
            } catch {
                /* the poll below reports it if the server is really gone */
            }
            if (cancelled) return;
            if (current) setVersion(current);
            await loadUpdateState();
            if (cancelled) return;

            // An update was running when this dialog was last closed: pick the
            // waiting screen back up instead of showing a stale idle state.
            const pending = readSession(SESSION_PENDING);
            if (pending?.to) {
                const from = String(pending.from ?? '');
                if (current && from && compareVersions(current, from) > 0) {
                    clearSession(SESSION_PENDING);
                    setPhase({ kind: 'done', to: current });
                } else {
                    setPhase({
                        kind: 'waiting',
                        from,
                        to: String(pending.to),
                        startedAt: Number(pending.startedAt) || Date.now(),
                    });
                }
            }
        })();
        return () => { cancelled = true; };
    }, [loadUpdateState]);

    useEffect(() => {
        loadServices();
        const id = setInterval(() => {
            const kind = phaseRef.current.kind;
            if (kind === 'applying' || kind === 'waiting' || kind === 'done' || kind === 'timeout') return;
            if (repairBusy) return;
            loadServices();
        }, 10000);
        return () => clearInterval(id);
    }, [loadServices, repairBusy]);

    useEffect(() => {
        if (phase.kind !== 'waiting') return;
        const { from, to, startedAt } = phase;
        let stopped = false;
        const tick = async () => {
            if (stopped) return;
            setElapsed(Math.floor((Date.now() - startedAt) / 1000));
            if (Date.now() - startedAt > WAIT_TIMEOUT_MS) {
                // Drop the resume record with the phase. Kept, it would replay a
                // long-expired startedAt on every reopen and every reload, so the
                // dialog would show this timeout screen - and hide the repair half
                // behind its overlay - for the rest of the browser session.
                clearSession(SESSION_PENDING);
                setPhase({ kind: 'timeout', from, to });
                return;
            }
            const controller = new AbortController();
            const abort = setTimeout(() => controller.abort(), 2000);
            try {
                const res = await fetch(api('/api/version'), {
                    credentials: 'include',
                    cache: 'no-store',
                    signal: controller.signal,
                });
                if (res.ok) {
                    const now = String((await res.json()).version ?? '');
                    // Any version other than the one we left is the new one: a
                    // rollback lands back on `from` and keeps us waiting, which
                    // the timeout screen then explains.
                    if (now && from && compareVersions(now, from) > 0) {
                        clearSession(SESSION_PENDING);
                        writeSession(SESSION_DONE, { to: now });
                        setPhase({ kind: 'done', to: now });
                        return;
                    }
                }
            } catch {
                /* expected while the server is down */
            } finally {
                clearTimeout(abort);
            }
        };
        const id = setInterval(tick, 2500);
        tick();
        return () => { stopped = true; clearInterval(id); };
    }, [phase]);

    useEffect(() => {
        if (phase.kind !== 'done') return;
        const id = setTimeout(() => window.location.reload(), 3000);
        return () => clearTimeout(id);
    }, [phase]);

    // ─── the node graph ──────────────────────────────────────────────────────

    const [nodes, setNodes, onNodesChange] = useNodesState([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState([]);

    // Memoised because it feeds the node-graph effect's dependency list. A bare
    // `services?.services ?? []` hands that effect a new array identity on every
    // render, and its empty branch sets fresh [] node/edge state, which renders
    // again: an unbounded loop for as long as `services` is null, which is
    // exactly the state while the server is restarting during an update.
    const rows = useMemo(() => services?.services ?? [], [services]);
    const issues = useMemo(() => rows.filter((s) => healthOf(s) !== 'ok'), [rows]);

    const statusLabel = useCallback((svc: ServiceRow): string => {
        const h = healthOf(svc);
        if (svc.state === 'unknown') return tM('statusUnknown');
        if (svc.exists === false) return tM('statusAbsent');
        if (!svc.running) return tM('statusDown');
        if (svc.port_mismatch) return tM('portMismatch');
        if (h === 'degraded') return tM('statusDegraded');
        if (h === 'down') return tM('statusDegraded');
        return tM('statusOk');
    }, [tM]);

    useEffect(() => {
        if (!rows.length) {
            setNodes([]);
            setEdges([]);
            return;
        }
        const ROW = 96;
        const height = (rows.length - 1) * ROW;
        const hub = {
            id: 'vaf',
            type: 'input',
            position: { x: 0, y: height / 2 },
            sourcePosition: Position.Right,
            draggable: false,
            style: { border: 'none', background: 'transparent', width: 'auto' },
            data: {
                label: (
                    <div className="px-4 py-3 rounded-xl bg-white border border-gray-200 shadow-sm flex items-center gap-3">
                        <div className="w-9 h-9 rounded-lg bg-gray-900 dark:bg-[#2e2e2e] text-white flex items-center justify-center">
                            <Server size={16} />
                        </div>
                        <div className="text-left">
                            <div className="text-sm font-semibold text-gray-800">{tM('hubNode')}</div>
                            <div className="text-[10px] text-gray-500">{formatVersion(version).display}</div>
                        </div>
                    </div>
                ),
            },
        };
        const serviceNodes = rows.map((svc, i) => {
            const color = COLOR[healthOf(svc)];
            return {
                id: `svc-${svc.service_key}`,
                type: 'output',
                position: { x: 380, y: i * ROW },
                targetPosition: Position.Left,
                draggable: false,
                style: { border: 'none', background: 'transparent', width: 'auto' },
                data: {
                    label: (
                        <div
                            className="px-3 py-2 rounded-xl bg-white border border-gray-200 shadow-sm min-w-[210px] text-left"
                            style={{ borderLeft: `4px solid ${color}` }}
                        >
                            <div className="flex items-center gap-2">
                                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: color }} />
                                <span className="text-sm font-medium text-gray-800 truncate">{svc.name}</span>
                            </div>
                            <div className="text-[10px] text-gray-500 mt-0.5">
                                {statusLabel(svc)}
                                {svc.configured_port ? <span className="font-mono"> :{svc.configured_port}</span> : null}
                            </div>
                        </div>
                    ),
                },
            };
        });
        setNodes([hub, ...serviceNodes] as any);
        setEdges(rows.map((svc) => {
            const h = healthOf(svc);
            return {
                id: `e-${svc.service_key}`,
                source: 'vaf',
                target: `svc-${svc.service_key}`,
                animated: repairBusy,
                style: {
                    stroke: COLOR[h],
                    strokeWidth: 1.5,
                    opacity: 0.6,
                    ...(h === 'ok' ? {} : { strokeDasharray: '4 4' }),
                },
            };
        }) as any);
    }, [rows, repairBusy, version, statusLabel, tM, setNodes, setEdges]);

    // ─── render ──────────────────────────────────────────────────────────────

    const overlayActive = ['applying', 'waiting', 'done', 'timeout'].includes(phase.kind);
    const versionInfo = formatVersion(version);

    return (
        <div className="fixed inset-0 z-[80] flex items-center justify-center p-4 max-md:p-0" onClick={onClose}>
            <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
            <div
                className="relative bg-white w-full max-w-[90vw] h-[70vh] min-h-[480px] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:max-h-none max-md:min-h-0 max-md:rounded-none max-md:border-0"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="h-20 border-b border-gray-100 flex items-center justify-between px-8 shrink-0 bg-white z-10 max-md:h-auto max-md:px-4 max-md:py-3">
                    <div className="flex items-center gap-4 min-w-0">
                        <div className="w-12 h-12 rounded-xl bg-gray-100 text-gray-700 flex items-center justify-center shadow-sm shrink-0 max-md:w-10 max-md:h-10 max-md:shadow-none">
                            <Wrench size={24} />
                        </div>
                        <div className="min-w-0">
                            <h2 className="text-2xl font-bold text-gray-800 truncate max-md:text-lg">{tM('title')}</h2>
                            <p className="text-sm text-gray-500 truncate max-md:text-xs">{tM('subtitle')}</p>
                        </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                        <button
                            onClick={() => loadServices()}
                            disabled={repairBusy || overlayActive}
                            className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors disabled:opacity-40 max-md:hidden"
                            title={tCommon('refresh')}
                        >
                            <RefreshCw size={18} />
                        </button>
                        <button
                            onClick={onClose}
                            className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors"
                        >
                            <X size={24} />
                        </button>
                    </div>
                </div>

                {!isAdmin ? (
                    <div className="flex-1 flex items-center justify-center text-sm text-gray-500">
                        {tM('adminOnly')}
                    </div>
                ) : (
                    <div className="flex-1 flex overflow-hidden relative max-md:flex-col max-md:overflow-y-auto">
                        {/* ── LEFT: version and update ── */}
                        <div className="w-[340px] shrink-0 border-r border-gray-100 flex flex-col p-6 gap-4 overflow-y-auto max-md:w-full max-md:border-r-0 max-md:border-b max-md:p-4">
                            {justUpdatedTo && (
                                <div className="flex items-start gap-2 p-3 rounded-lg bg-green-50 border border-green-100 text-xs text-green-700">
                                    <Check size={14} className="mt-0.5 shrink-0" />
                                    <span>{tM('updateFinished', { version: formatVersion(justUpdatedTo).display || justUpdatedTo })}</span>
                                </div>
                            )}

                            <div className="p-4 rounded-xl bg-gray-50/70 border border-gray-100">
                                <div className="text-xs text-gray-500">{tM('installedVersion')}</div>
                                <div className="flex items-baseline gap-2 mt-1">
                                    <span className="text-2xl font-bold text-gray-800">{versionInfo.display || '...'}</span>
                                    {versionInfo.channel && (
                                        <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-gray-200 text-gray-600">
                                            {versionInfo.channel}
                                        </span>
                                    )}
                                </div>
                                {version && <div className="text-xs text-gray-400 font-mono mt-1">{version}</div>}
                            </div>

                            <div className="text-xs text-gray-500">
                                {checkedAt
                                    ? tM('lastChecked', { time: new Date(checkedAt).toLocaleString() })
                                    : tM('neverChecked')}
                            </div>

                            {unfinished && (
                                <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-50 border border-amber-100 text-xs text-amber-700">
                                    <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                                    <span>{tM('unfinishedUpdate')}</span>
                                </div>
                            )}

                            {phase.kind === 'upToDate' && (
                                <div className="flex items-center gap-2 text-sm text-green-600">
                                    <Check size={16} /> {tM('upToDate')}
                                </div>
                            )}

                            {(phase.kind === 'available' || phase.kind === 'confirm') && (
                                <div className="p-4 rounded-xl border border-gray-200 bg-white space-y-3">
                                    <div className="text-sm font-semibold text-gray-800">{tM('updateAvailable')}</div>
                                    <div className="text-sm text-gray-600">
                                        {tM('newVersion', { version: formatVersion(phase.latest).display || phase.latest })}
                                    </div>
                                    {phase.releaseUrl && (
                                        <a
                                            href={phase.releaseUrl}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
                                        >
                                            {tM('releaseNotes')} <ExternalLink size={11} />
                                        </a>
                                    )}
                                    {canApply ? (
                                        <div className="flex items-start gap-2 p-2.5 rounded-lg bg-amber-50 border border-amber-100 text-xs text-amber-700">
                                            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                                            <span>{tM('restartNotice')}</span>
                                        </div>
                                    ) : (
                                        <div className="text-xs text-gray-500">
                                            {tM('cannotUpdateHere')}
                                            {applyReason && <span className="block mt-1 text-gray-400">{applyReason}</span>}
                                        </div>
                                    )}
                                </div>
                            )}

                            {updateError && <div className="text-xs text-red-600">{updateError}</div>}

                            <div className="mt-auto pt-2 flex flex-col gap-2">
                                <button
                                    onClick={checkForUpdates}
                                    disabled={phase.kind === 'checking' || overlayActive}
                                    className="h-10 px-4 rounded-lg border border-gray-200 bg-white hover:bg-gray-50 text-sm font-medium text-gray-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
                                >
                                    {phase.kind === 'checking' ? (
                                        <><Loader2 size={14} className="animate-spin" /> {tM('checking')}</>
                                    ) : (
                                        <><RefreshCw size={14} /> {tM('checkForUpdates')}</>
                                    )}
                                </button>

                                {canApply && phase.kind === 'available' && (
                                    <button
                                        onClick={() => setPhase({ kind: 'confirm', latest: phase.latest, releaseUrl: phase.releaseUrl })}
                                        className="h-10 px-4 rounded-lg bg-gray-900 text-white hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none text-sm font-medium transition-colors"
                                    >
                                        {tM('updateNow')}
                                    </button>
                                )}
                                {canApply && phase.kind === 'confirm' && (
                                    <>
                                        <button
                                            onClick={() => applyUpdate(phase.latest)}
                                            className="h-10 px-4 rounded-lg bg-gray-900 text-white hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none text-sm font-medium transition-colors"
                                        >
                                            {tM('confirmUpdate')}
                                        </button>
                                        <button
                                            onClick={() => setPhase({ kind: 'available', latest: phase.latest, releaseUrl: phase.releaseUrl })}
                                            className="text-xs text-gray-500 hover:text-gray-700"
                                        >
                                            {tCommon('cancel')}
                                        </button>
                                    </>
                                )}
                            </div>
                        </div>

                        {/* ── RIGHT: the services as a node graph ── */}
                        <div className="flex-1 flex flex-col overflow-hidden bg-gray-50 max-md:min-h-[60vh] max-md:shrink-0">
                            <div className="flex-1 relative overflow-hidden">
                                {servicesError ? (
                                    <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-8 text-center">
                                        <XCircle size={28} className="text-red-400" />
                                        <p className="text-sm text-red-600">{tM('loadFailed', { message: servicesError })}</p>
                                        <button
                                            onClick={() => loadServices()}
                                            className="h-9 px-4 rounded-lg border border-gray-200 bg-white hover:bg-gray-50 text-sm text-gray-700"
                                        >
                                            {tM('retry')}
                                        </button>
                                    </div>
                                ) : services && !services.docker.available ? (
                                    <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-8 text-center">
                                        <Boxes size={32} className="text-gray-400" />
                                        <p className="text-sm text-gray-500 max-w-sm">{tM('dockerUnavailable')}</p>
                                        {services.docker.detail && (
                                            <p className="text-xs text-gray-400 font-mono max-w-md truncate">{services.docker.detail}</p>
                                        )}
                                    </div>
                                ) : (
                                    <Suspense fallback={<div className="absolute inset-0 flex items-center justify-center"><Loader2 size={20} className="animate-spin text-gray-400" /></div>}>
                                        <ReactFlow
                                            className="vaf-netmap"
                                            nodes={nodes}
                                            edges={edges}
                                            onNodesChange={onNodesChange}
                                            onEdgesChange={onEdgesChange}
                                            fitView
                                            fitViewOptions={{ padding: 0.2 }}
                                            minZoom={0.05}
                                            maxZoom={4}
                                            nodesDraggable={false}
                                            nodesConnectable={false}
                                            elementsSelectable={false}
                                            proOptions={{ hideAttribution: true }}
                                        >
                                            <Background color={isDark ? '#2f2f2f' : '#e5e7eb'} gap={20} />
                                            <Controls
                                                position="bottom-right"
                                                showInteractive={false}
                                                className="bg-white border border-gray-200 shadow-sm text-gray-500 rounded-lg overflow-hidden"
                                            />
                                        </ReactFlow>
                                    </Suspense>
                                )}
                            </div>

                            {/* Issues + repair */}
                            <div className="shrink-0 border-t border-gray-100 bg-white p-4 max-h-[45%] overflow-y-auto">
                                <div className="flex items-center justify-between gap-4">
                                    <span className="text-xs font-bold text-gray-500 uppercase tracking-wide">
                                        {issues.length ? `${tM('issues')} (${issues.length})` : tM('services')}
                                    </span>
                                    <button
                                        onClick={runRepair}
                                        disabled={repairBusy || overlayActive}
                                        className={`h-9 px-4 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 disabled:opacity-50 ${
                                            issues.length
                                                ? 'bg-amber-500 hover:bg-amber-600 text-white'
                                                : 'border border-gray-200 bg-white hover:bg-gray-50 text-gray-700'
                                        }`}
                                    >
                                        {repairBusy ? (
                                            <><Loader2 size={14} className="animate-spin" /> {tM('repairRunning')}</>
                                        ) : (
                                            <><Wrench size={14} /> {tM('repair')}</>
                                        )}
                                    </button>
                                </div>

                                <div className="mt-3 space-y-1.5">
                                    {issues.length === 0 && rows.length > 0 && (
                                        <div className="flex items-center gap-2 text-xs text-green-600">
                                            <Check size={13} /> {tM('noIssues')}
                                        </div>
                                    )}
                                    {issues.map((svc) => (
                                        <div key={svc.service_key} className="flex items-start gap-2 text-xs">
                                            <span
                                                className="w-2 h-2 rounded-full mt-1 shrink-0"
                                                style={{ background: COLOR[healthOf(svc)] }}
                                            />
                                            <span className="font-medium text-gray-700 shrink-0">{svc.name}</span>
                                            <span className="text-gray-500">{svc.reason || statusLabel(svc)}</span>
                                        </div>
                                    ))}
                                </div>

                                {repairSteps && repairSteps.length > 0 && (
                                    <div className="mt-4 pt-3 border-t border-gray-100">
                                        <div className="text-xs font-bold text-gray-500 uppercase tracking-wide mb-2">
                                            {tM('repairReport')}
                                        </div>
                                        <div className="space-y-1.5">
                                            {repairSteps.map((step, i) => (
                                                <div key={`${step.step}-${i}`} className="flex items-start gap-2 text-xs">
                                                    {step.ok
                                                        ? <Check size={13} className="text-green-600 mt-0.5 shrink-0" />
                                                        : <XCircle size={13} className="text-red-500 mt-0.5 shrink-0" />}
                                                    <span className="font-medium text-gray-700 shrink-0">{step.step}</span>
                                                    <span className="text-gray-500">{step.message}</span>
                                                </div>
                                            ))}
                                        </div>
                                        {repairOk !== null && !repairBusy && (
                                            <div className={`mt-2 text-xs ${repairOk ? 'text-green-600' : 'text-amber-600'}`}>
                                                {repairOk ? tM('repairDone') : tM('repairIncomplete')}
                                            </div>
                                        )}
                                    </div>
                                )}
                                {repairError && (
                                    <div className="mt-2 text-xs text-red-600">{tM('repairFailed', { message: repairError })}</div>
                                )}
                            </div>
                        </div>

                        {/* ── the update overlay: the server is away on purpose ── */}
                        {overlayActive && (
                            <div className="absolute inset-0 z-20 bg-white/95 backdrop-blur-sm flex flex-col items-center justify-center gap-3 p-8 text-center">
                                {phase.kind === 'applying' && (
                                    <>
                                        <Loader2 size={28} className="animate-spin text-gray-400" />
                                        <p className="text-sm text-gray-600">{tM('applying')}</p>
                                    </>
                                )}
                                {phase.kind === 'waiting' && (
                                    <>
                                        <Loader2 size={28} className="animate-spin text-gray-400" />
                                        <h3 className="text-lg font-semibold text-gray-800">
                                            {tM('waitingTitle', {
                                                from: formatVersion(phase.from).display || phase.from,
                                                to: formatVersion(phase.to).display || phase.to,
                                            })}
                                        </h3>
                                        <p className="text-sm text-gray-600 max-w-md">{tM('waitingBody')}</p>
                                        <p className="text-xs text-gray-400 font-mono">{tM('waitingElapsed', { time: mmss(elapsed) })}</p>
                                        <p className="text-xs text-gray-400">{tM('waitingKeepOpen')}</p>
                                    </>
                                )}
                                {phase.kind === 'done' && (
                                    <>
                                        <Check size={32} className="text-green-500" />
                                        <h3 className="text-lg font-semibold text-gray-800">{tM('doneTitle')}</h3>
                                        <p className="text-sm text-gray-600">
                                            {tM('doneBody', { version: formatVersion(phase.to).display || phase.to })}
                                        </p>
                                        <button
                                            onClick={() => window.location.reload()}
                                            className="h-10 px-5 rounded-lg bg-gray-900 text-white hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none text-sm font-medium"
                                        >
                                            {tM('reloadNow')}
                                        </button>
                                    </>
                                )}
                                {phase.kind === 'timeout' && (
                                    <>
                                        <AlertTriangle size={30} className="text-amber-500" />
                                        <h3 className="text-lg font-semibold text-gray-800">{tM('timeoutTitle')}</h3>
                                        <p className="text-sm text-gray-600 max-w-md">{tM('timeoutBody')}</p>
                                        <div className="flex items-center gap-2">
                                            <button
                                                onClick={() => setPhase({ kind: 'waiting', from: phase.from, to: phase.to, startedAt: Date.now() })}
                                                className="h-10 px-4 rounded-lg border border-gray-200 bg-white hover:bg-gray-50 text-sm text-gray-700"
                                            >
                                                {tM('keepWaiting')}
                                            </button>
                                            <button
                                                onClick={() => window.location.reload()}
                                                className="h-10 px-4 rounded-lg bg-gray-900 text-white hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none text-sm font-medium"
                                            >
                                                {tM('reloadNow')}
                                            </button>
                                        </div>
                                    </>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
