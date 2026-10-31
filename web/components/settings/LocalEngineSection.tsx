// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Loader2 } from 'lucide-react';
import ConfirmDialog from '@/components/ui/ConfirmDialog';
import Skeleton, { SkeletonBar } from '@/components/ui/Skeleton';
import EngineChoice, { type EngineId, type EngineOption } from './EngineChoice';
import { Input, Select } from './SettingsFields';

/** The fit of a switch (vaf/core/vllm_server.py FitResult, as data). */
interface FitData {
    ok: boolean;
    code: string;
    max_model_len: number;
    kv_dtype: string;
    need_mb: number;
    budget_mb: number;
    weights_mb: number;
    kv_mb: number;
    free_mb: number;
    total_mb: number;
    reason: string;
}

/** What local_engine.apply did (EngineSwitch.to_dict). */
interface SwitchResult {
    status: 'done' | 'needs_install' | 'refused' | 'failed';
    code: string;
    text: string;
    detail: string;
    engine: string;
    model: string;
    fit: FitData | null;
    stopped_llama: boolean;
    note: string;
    params: Record<string, string>;
}

interface EngineJob {
    running: boolean;
    phase: string | null;
    step: number;
    steps: number;
    result: SwitchResult | null;
    error: string | null;
}

/** GET /api/system/local-engine: local_engine.state() plus the switch job. */
export interface EngineState {
    engine: EngineId;
    profile: 'local' | 'api';
    support: { level: 'supported' | 'later' | 'unsupported'; code: string; params: Record<string, string>; reason: string };
    vllm: {
        pin: string;
        installed_version: string | null;
        needs_install: boolean;
        download_gb_at_least: number;
        model_setting: string;
        model: string;
        auto_model: string;
        curated: { repo: string; weights_gb: number }[];
        preview: string;
        model_downloaded: boolean;
        parsers: { tool: string | null; reasoning: string };
        parser_refusal: { code: string; text: string } | null;
        server: 'running' | 'loading' | 'idle';
    };
    llama: { model: string };
    conflicts: { code: string; text: string }[];
    job?: EngineJob;
}

export interface DownloadStatus {
    status: 'idle' | 'downloading' | 'done' | 'error';
    progress_pct?: number;
    bytes_done?: number;
    bytes_total?: number;
    speed_str?: string;
}

const OTHER = '__other__';
const REPO_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/;
const INSTALL_LOG = 'logs/vllm_install.log';
// Polls without an answer before the section stops waiting (the profile switch's limit).
const POLL_MISS_LIMIT = 20;

/**
 * The engine of the local model (vaf/core/local_engine.py, /api/system/local-engine), at the top
 * of the "Local model" section: llama.cpp or vLLM, the model picker of the chosen engine below it.
 *
 * The engine and its model change through the switch route only, never with Save changes: a
 * switch installs, downloads, stops a server and checks the fit, and a save sends the whole page
 * (both save lanes drop the keys). The switch is a job like the profile switch: POST returns at
 * once, the section follows `job` on GET. Under llama.cpp the caller's GGUF picker (`children`)
 * is shown; under vLLM the Hugging Face model picker. Admin only, like the tab it sits on.
 *
 * `onChanged` receives the engine and the vLLM model now set; `currentEngine` is what the caller
 * shows, and a loaded engine that differs (a switch made in a terminal) is reported once.
 */
export default function LocalEngineSection({ children, downloadModelStatus, onChanged, currentEngine }: {
    children?: React.ReactNode;
    downloadModelStatus?: DownloadStatus;
    onChanged?: (now: { engine: EngineId; vllm_model: string }) => void;
    currentEngine?: string;
}) {
    const t = useTranslations('localEngine');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [data, setData] = useState<EngineState | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);
    const [pending, setPending] = useState<EngineId | null>(null);
    const [modelChoice, setModelChoice] = useState<string>('auto');
    const [otherRepo, setOtherRepo] = useState('');
    const [preview, setPreview] = useState<EngineState['vllm'] | null>(null);
    const [confirming, setConfirming] = useState<'install' | 'remove' | null>(null);
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<{ lines: string[]; tone: 'ok' | 'error' } | null>(null);
    const closed = useRef(false);
    const following = useRef(false);
    const onChangedRef = useRef(onChanged);
    useEffect(() => { onChangedRef.current = onChanged; }, [onChanged]);
    useEffect(() => {
        closed.current = false;
        return () => { closed.current = true; };
    }, []);

    /** A coded text from the catalogue, the server's English sentence when the code is new. */
    const coded = useCallback((ns: string, code: string, params: Record<string, string | number> = {}, fallback = '') => {
        const key = `${ns}.${code}`;
        return code && t.has(key) ? t(key, params) : (fallback || code);
    }, [t]);

    const load = useCallback(async (quiet = false) => {
        try {
            const res = await fetch(`${apiBase}/api/system/local-engine`, { credentials: 'include' });
            if (!res.ok) { if (!quiet) setLoadFailed(true); return null; }
            const state: EngineState = await res.json();
            setData(state);
            setLoadFailed(false);
            return state;
        } catch {
            if (!quiet) setLoadFailed(true);
            return null;
        }
    }, [apiBase]);

    useEffect(() => {
        void load();
        const reload = () => { if (document.visibilityState === 'visible' && !following.current) void load(true); };
        window.addEventListener('focus', reload);
        document.addEventListener('visibilitychange', reload);
        return () => {
            window.removeEventListener('focus', reload);
            document.removeEventListener('visibilitychange', reload);
        };
    }, [load]);

    // The model picker starts on the stored setting whenever a new state arrives.
    const setting = data?.vllm.model_setting || 'auto';
    useEffect(() => { setModelChoice(setting); setOtherRepo(''); }, [setting]);

    const curated = useMemo(() => data?.vllm.curated ?? [], [data]);
    const gbOf = useCallback((repo: string) => curated.find((c) => c.repo === repo)?.weights_gb, [curated]);
    const chosenRepo = modelChoice === OTHER ? otherRepo.trim() : modelChoice;
    const repoValid = chosenRepo === 'auto' || REPO_ID.test(chosenRepo);
    const runningRepo = chosenRepo === 'auto' ? (data?.vllm.auto_model || '') : chosenRepo;

    // The parsers and the download state of the repo in the picker: the loaded state covers the
    // stored setting; another repo is asked for (no network on the server, the cache only).
    useEffect(() => {
        if (!data) return;
        if (chosenRepo === setting || !repoValid) { setPreview(null); return; }
        let cancelled = false;
        const timer = window.setTimeout(async () => {
            try {
                const res = await fetch(`${apiBase}/api/system/local-engine?model=${encodeURIComponent(chosenRepo)}`, { credentials: 'include' });
                if (!res.ok || cancelled) return;
                const state: EngineState = await res.json();
                if (!cancelled) setPreview(state.vllm);
            } catch { /* the line falls back to the stored model's */ }
        }, 400);
        return () => { cancelled = true; window.clearTimeout(timer); };
    }, [apiBase, chosenRepo, repoValid, setting, data]);

    const resultLines = useCallback((state: EngineState): { lines: string[]; tone: 'ok' | 'error' } => {
        const job = state.job;
        if (job?.error) return { lines: [job.error], tone: 'error' };
        const r = job?.result;
        if (!r) return { lines: [t('failed')], tone: 'error' };
        const params = { model: r.model, ...r.params };
        const ns = r.code && t.has(`support.${r.code}`) ? 'support' : 'result';
        const lines = [coded(ns, r.code, params, r.text)];
        const fit = r.fit;
        if (fit && fit.code) {
            lines.push(coded('fit', fit.code, {
                need: fit.need_mb, budget: fit.budget_mb, ctx: fit.max_model_len, kv: fit.kv_dtype,
                weights: fit.weights_mb, kvMb: fit.kv_mb, total: fit.total_mb, free: fit.free_mb,
            }, fit.reason));
        }
        if (r.code === 'no_fit' && r.stopped_llama) lines.push(t('stoppedLlama'));
        if (r.code === 'install_failed' || r.code === 'cuda_probe_failed') lines.push(t('logHint', { path: INSTALL_LOG }));
        return { lines, tone: r.status === 'done' ? 'ok' : 'error' };
    }, [coded, t]);

    const follow = useCallback(async () => {
        if (following.current) return;
        following.current = true;
        setBusy(true);
        let misses = 0;
        try {
            for (;;) {
                await new Promise((r) => setTimeout(r, 1500));
                if (closed.current) return;
                const state = await load(true);
                if (!state) {
                    misses += 1;
                    if (misses >= POLL_MISS_LIMIT) { setNote({ lines: [t('failed')], tone: 'error' }); return; }
                    continue;
                }
                misses = 0;
                if (!state.job?.running) {
                    setNote(resultLines(state));
                    setPending(null);
                    onChangedRef.current?.({ engine: state.engine, vllm_model: state.vllm.model_setting });
                    return;
                }
            }
        } finally {
            following.current = false;
            if (!closed.current) setBusy(false);
        }
    }, [load, resultLines, t]);

    // A switch started elsewhere (another tab, a reopened Settings) is followed, not offered again.
    useEffect(() => {
        if (data?.job?.running) void follow();
    }, [data?.job?.running, follow]);

    // A switch made in a terminal: the caller still shows the old engine. Once per loaded state.
    const reportedFor = useRef<EngineState | null>(null);
    useEffect(() => {
        if (!data || data.job?.running || !currentEngine || reportedFor.current === data) return;
        if ((currentEngine === 'vllm' ? 'vllm' : 'llama') !== data.engine) {
            reportedFor.current = data;
            onChangedRef.current?.({ engine: data.engine, vllm_model: data.vllm.model_setting });
        }
    }, [data, currentEngine]);

    const start = async (body: { engine: EngineId; model?: string; install?: boolean }) => {
        setConfirming(null);
        setNote(null);
        setBusy(true);
        let started = false;
        try {
            const res = await fetch(`${apiBase}/api/system/local-engine`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (res.ok) {
                started = true;
            } else {
                const answer = await res.json().catch(() => ({}));
                const code = String(answer.code || '');
                const ns = code && t.has(`support.${code}`) ? 'support' : 'result';
                setNote({ lines: [coded(ns, code, { model: runningRepo, ...(answer.params || {}) }, String(answer.detail || t('failed')))], tone: 'error' });
            }
        } catch {
            setNote({ lines: [t('failed')], tone: 'error' });
        }
        if (started) await follow();
        else { setBusy(false); void load(true); }
    };

    const remove = async () => {
        setConfirming(null);
        setNote(null);
        setBusy(true);
        try {
            const res = await fetch(`${apiBase}/api/system/local-engine/vllm`, { method: 'DELETE', credentials: 'include' });
            const answer = await res.json().catch(() => ({}));
            if (res.ok) {
                setNote({
                    lines: [answer.kept_model
                        ? t('removedModel', { model: answer.kept_model, gb: (Number(answer.kept_bytes || 0) / 1024 ** 3).toFixed(1) })
                        : t('removedNoModel')],
                    tone: 'ok',
                });
            } else {
                setNote({ lines: [coded('result', String(answer.code || ''), {}, String(answer.detail || t('failed')))], tone: 'error' });
            }
        } catch {
            setNote({ lines: [t('failed')], tone: 'error' });
        }
        setBusy(false);
        void load(true);
    };

    if (!data) {
        return (
            <div className="mb-5">
                <div className="text-sm font-medium text-gray-700 ml-1 mb-1.5">{t('label')}</div>
                {loadFailed ? (
                    <div className="flex items-center gap-3">
                        <p className="text-sm text-red-600">{t('loadFailed')}</p>
                        <button type="button" onClick={() => void load()}
                            className="px-2 py-1 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100">
                            {t('retry')}
                        </button>
                    </div>
                ) : (
                    // The two engine cards, side by side on a desktop, stacked on a phone.
                    <Skeleton className="h-[92px] max-md:h-[196px]">
                        <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1 h-full">
                            {[0, 1].map((i) => (
                                <div key={i} className="rounded-xl border-2 border-gray-100 px-4 py-3 flex flex-col gap-2">
                                    <SkeletonBar className="h-3 w-20" />
                                    <SkeletonBar className="h-2.5 w-11/12" />
                                    <SkeletonBar className="h-2.5 w-2/3" />
                                </div>
                            ))}
                        </div>
                    </Skeleton>
                )}
            </div>
        );
    }

    const lean = data.profile === 'api';
    const supported = data.support.level === 'supported';
    const jobRunning = busy || !!data.job?.running;
    const shown: EngineId = pending ?? data.engine;
    const vllmNote = lean ? t('profileApi')
        : coded('support', data.support.code, data.support.params, data.support.reason);
    const options: EngineOption[] = [
        { id: 'llama', name: t('llamaName'), line: t('llamaLine'), disabled: lean },
        {
            id: 'vllm', name: t('vllmName'), line: t('vllmLine'), disabled: lean || !supported,
            note: vllmNote,
            noteTone: lean || !supported ? 'amber' : 'muted',
            noteWord: lean ? undefined : data.support.level === 'later' ? t('levelLater') : t('levelUnsupported'),
        },
    ];
    const vllmInfo = preview ?? data.vllm;
    const needsInstall = data.vllm.needs_install;
    const sizeGb = gbOf(runningRepo);
    // A free repo's size is not known before its download.
    const sizeLine = sizeGb
        ? t('sizeLine', { pin: data.vllm.pin, gb: data.vllm.download_gb_at_least.toFixed(1), modelGb: sizeGb.toFixed(2) })
        : t('sizeLineNoSize', { pin: data.vllm.pin, gb: data.vllm.download_gb_at_least.toFixed(1) });
    const autoGb = gbOf(data.vllm.auto_model);
    const modelOptions = [
        { value: 'auto', label: autoGb ? t('modelAuto', { repo: data.vllm.auto_model, gb: autoGb.toFixed(2) }) : t('modelAutoNoSize', { repo: data.vllm.auto_model }) },
        ...curated.map((c) => ({ value: c.repo, label: t('modelCurated', { repo: c.repo, gb: c.weights_gb.toFixed(2) }) })),
        ...(setting !== 'auto' && !gbOf(setting) ? [{ value: setting, label: setting }] : []),
        { value: OTHER, label: t('modelOther') },
    ];
    const showVllmPicker = !lean && (shown === 'vllm');
    const modelChanged = chosenRepo !== setting;
    const refusal = vllmInfo.parser_refusal;

    const progress = jobRunning && data.job?.running ? data.job : null;
    const downloading = progress?.phase === 'download_model' && downloadModelStatus?.status === 'downloading';

    const primary = 'px-3 py-1.5 text-xs font-medium rounded-md bg-gray-900 text-white hover:bg-black disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5]';
    const secondary = 'px-3 py-1.5 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100 disabled:opacity-50';

    const vllmAction = () => {
        if (!repoValid || refusal) return;
        if (needsInstall) { setConfirming('install'); return; }
        void start({ engine: 'vllm', model: chosenRepo });
    };

    return (
        <div className="mb-5" data-local-engine={data.engine}>
            <div className="text-sm font-medium text-gray-700 ml-1 mb-1.5">{t('label')}</div>
            <EngineChoice
                label={t('label')}
                value={shown}
                options={options}
                onChange={(engine) => {
                    if (jobRunning) return;
                    setNote(null);
                    setPending(engine === data.engine ? null : engine);
                }}
            />
            {!lean && <p className="text-xs text-gray-500 mt-2 ml-1">{t('appliesNow')}</p>}

            {showVllmPicker && (
                <div className="mt-4 space-y-2">
                    <Select
                        label={t('model')}
                        value={modelChoice}
                        onChange={(v: string) => setModelChoice(v)}
                        options={modelOptions}
                        disabled={jobRunning}
                    />
                    {modelChoice === OTHER && (
                        <div>
                            <Input
                                label={t('modelOtherLabel')}
                                value={otherRepo}
                                onChange={(v: string) => setOtherRepo(v)}
                                placeholder={t('modelOtherPlaceholder')}
                                disabled={jobRunning}
                            />
                            <p className="text-xs text-gray-500 mt-1 ml-1">{t('modelOtherHint')}</p>
                            {otherRepo.trim() !== '' && !repoValid && (
                                <p className="text-xs text-amber-700 mt-1 ml-1">{t('modelInvalid')}</p>
                            )}
                        </div>
                    )}
                    {repoValid && (refusal ? (
                        <p className="text-xs text-amber-700 ml-1">{coded('result', refusal.code, { model: runningRepo }, refusal.text)}</p>
                    ) : vllmInfo.parsers.tool ? (
                        <p className="text-xs text-gray-500 ml-1">
                            {vllmInfo.parsers.reasoning
                                ? t('parsers', { tool: vllmInfo.parsers.tool, reasoning: vllmInfo.parsers.reasoning })
                                : t('parsersNoReasoning', { tool: vllmInfo.parsers.tool })}
                            {' '}{vllmInfo.model_downloaded ? t('downloaded') : t('notDownloaded')}
                        </p>
                    ) : null)}
                </div>
            )}

            {/* Switching to vLLM from llama.cpp: what it costs, then the button. */}
            {!lean && pending === 'vllm' && !progress && (
                <div className="mt-3 space-y-1">
                    {needsInstall && (
                        <p className="text-xs text-gray-700 ml-1">{sizeLine}</p>
                    )}
                    <p className="text-xs text-gray-500 ml-1">{t('fitNote')}</p>
                    <p className="text-xs text-gray-500 ml-1">{t('restartNote')}</p>
                    <div className="flex flex-wrap items-center gap-2 pt-2">
                        <button type="button" className={primary} disabled={jobRunning || !repoValid || !!refusal} onClick={vllmAction}>
                            {needsInstall ? t('installAndSwitch') : t('switchToVllm')}
                        </button>
                        <button type="button" className={secondary} disabled={jobRunning} onClick={() => setPending(null)}>{t('cancel')}</button>
                    </div>
                </div>
            )}

            {/* Under vLLM: its server, a changed model, the way back. */}
            {!lean && data.engine === 'vllm' && pending === null && !progress && (
                <div className="mt-3 space-y-1">
                    {data.vllm.installed_version && (
                        <p className="text-xs text-gray-600 ml-1">{t(`server.${data.vllm.server}`, { version: data.vllm.installed_version })}</p>
                    )}
                    {data.conflicts.map((c) => (
                        <p key={c.code} className="text-xs text-amber-700 ml-1">{coded('conflict', c.code, {}, c.text)}</p>
                    ))}
                    <div className="flex flex-wrap items-center gap-2 pt-2">
                        {(modelChanged || needsInstall) && (
                            <button type="button" className={primary} disabled={jobRunning || !repoValid || !!refusal} onClick={vllmAction}>
                                {needsInstall ? t('installAndSwitch') : t('useModel')}
                            </button>
                        )}
                        <button type="button" className={secondary} disabled={jobRunning} onClick={() => void start({ engine: 'llama' })}>
                            {t('switchToLlama')}
                        </button>
                    </div>
                </div>
            )}

            {!lean && data.engine === 'vllm' && pending === 'llama' && !progress && (
                <div className="mt-3 space-y-1">
                    <p className="text-xs text-gray-500 ml-1">{t('restartNote')}</p>
                    <div className="flex flex-wrap items-center gap-2 pt-2">
                        <button type="button" className={primary} disabled={jobRunning} onClick={() => void start({ engine: 'llama' })}>{t('switchToLlama')}</button>
                        <button type="button" className={secondary} disabled={jobRunning} onClick={() => setPending(null)}>{t('cancel')}</button>
                    </div>
                </div>
            )}

            {progress && (
                <div className="mt-4 space-y-2" aria-live="polite">
                    <div className="flex items-center justify-between text-xs text-gray-700">
                        <span className="flex items-center gap-2 min-w-0">
                            <Loader2 size={14} className="animate-spin shrink-0" />
                            <span className="truncate">{progress.phase ? coded('phase', progress.phase, {}, progress.phase) : t('stepOf', { step: 0, steps: progress.steps || 0 })}</span>
                        </span>
                        {progress.steps > 0 && <span className="shrink-0 ml-2 text-gray-500">{t('stepOf', { step: Math.min(progress.step + 1, progress.steps), steps: progress.steps })}</span>}
                    </div>
                    <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                        <div className="h-full bg-gray-900 dark:bg-[#ececec] rounded-full transition-[width] duration-300"
                            style={{ width: `${progress.steps ? Math.round((progress.step / progress.steps) * 100) : 0}%` }} />
                    </div>
                    {downloading && (
                        <div className="flex items-center justify-between text-xs text-gray-500">
                            <span>
                                {downloadModelStatus?.bytes_done != null && downloadModelStatus?.bytes_total
                                    ? `${(downloadModelStatus.bytes_done / (1024 * 1024)).toFixed(0)} MB / ${(downloadModelStatus.bytes_total / (1024 * 1024)).toFixed(0)} MB`
                                    : downloadModelStatus?.progress_pct != null ? `${Math.round(downloadModelStatus.progress_pct)}%` : ''}
                            </span>
                            {downloadModelStatus?.speed_str && <span>{downloadModelStatus.speed_str}</span>}
                        </div>
                    )}
                </div>
            )}

            {note && (
                <div className="mt-3 space-y-0.5">
                    {note.lines.map((line, i) => (
                        <p key={i} className={`text-xs ml-1 ${note.tone === 'error' ? 'text-red-600' : 'text-emerald-700'}`}>{line}</p>
                    ))}
                </div>
            )}

            {/* Under llama.cpp: the GGUF picker of the caller, and the way to remove vLLM. */}
            {!lean && shown === 'llama' && data.engine === 'llama' && (
                <div className="mt-4">
                    {children}
                    {data.vllm.installed_version && !progress && (
                        <button type="button" className={`${secondary} mt-3`} disabled={jobRunning} onClick={() => setConfirming('remove')}>
                            {t('removeVllm')}
                        </button>
                    )}
                </div>
            )}

            <ConfirmDialog
                open={confirming === 'install'}
                title={t('installConfirmTitle')}
                body={<>
                    <p>{sizeLine}</p>
                    <p>{t('fitNote')}</p>
                    <p>{t('restartNote')}</p>
                </>}
                confirmLabel={t('installConfirm')}
                cancelLabel={t('cancel')}
                onConfirm={() => void start({ engine: 'vllm', model: chosenRepo, install: true })}
                onCancel={() => setConfirming(null)}
            />
            <ConfirmDialog
                open={confirming === 'remove'}
                destructive
                title={t('removeConfirmTitle')}
                body={t('removeConfirmBody')}
                confirmLabel={t('removeConfirm')}
                cancelLabel={t('cancel')}
                onConfirm={() => void remove()}
                onCancel={() => setConfirming(null)}
            />
        </div>
    );
}
