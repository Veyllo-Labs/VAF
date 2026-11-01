// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';
//
// vLLM's share of the GPU memory (vllm_gpu_memory_utilization), under the vLLM model picker of
// Settings > AI & Model. Automatic by default; by hand a slider from 30 to 95 percent with the
// size in GB and whether the model fits at that value.
//
// - Every number comes from the server (local_engine.state()["vllm"]["memory"], built by
//   vllm_server.share_table): the fit per share, the desktop reserve, the automatic share now.
//   No fit formula lives here, so the slider can never disagree with the start.
// - The value reaches the server only through the engine switch (POST /api/system/local-engine,
//   gpu_share), never through Save changes: vLLM takes its share at launch, so a new one is
//   fitted and started once before it is written.
// - Not up to 100 percent: above 95 nothing is left for any other GPU context on the card.
// - The slider and its toggle are the failover slider and the temperature toggle of
//   SettingsModal.tsx; the value wraps under the slider on a phone (docs/web-ui/MOBILE_UI.md).

import React from 'react';
import { useFormatter, useTranslations } from 'next-intl';

/** One fit of the share table (vllm_server.share_table steps). */
export interface ShareStep {
    share: number;
    budget_mb: number;
    ok: boolean;
    max_model_len: number;
    need_mb: number;
}

/** local_engine.state()["vllm"]["memory"]. */
export interface ShareTable {
    total_mb: number | null;
    /** null while a local server runs (its memory is not free now) or when unknown. */
    free_mb: number | null;
    /** Whether this GPU drives a display; null when that cannot be read. */
    display: boolean | null;
    /** What an automatic share keeps of the card outside vLLM for the screen (0 without a
     *  display); room on the card, not memory that is free whatever else runs. */
    reserve_mb: number;
    /** The stored share, 0 = automatic. */
    configured: number;
    auto_share: number | null;
    auto_reserved: boolean;
    kv_dtype: string;
    range: [number, number];
    min_share: number | null;
    /** One fit per share of the range in 0.01 steps; empty before the model is downloaded. */
    steps: ShareStep[];
}

/** A share within the slider's range, in 0.01 steps. */
export function clampShare(value: number, range: [number, number]): number {
    return Math.round(Math.min(range[1], Math.max(range[0], value)) * 100) / 100;
}

export default function GpuShareSlider({ memory, value, onChange, disabled, mode }: {
    memory: ShareTable;
    /** 0 = automatic, else the share by hand. */
    value: number;
    onChange: (value: number) => void;
    disabled?: boolean;
    /** `switch`: it applies with a switch from llama.cpp; `running`: vLLM is the engine. */
    mode: 'switch' | 'running';
}) {
    const t = useTranslations('localEngine.share');
    const format = useFormatter();
    const pct = (share: number) => format.number(share, { style: 'percent', maximumFractionDigits: 0 });
    const gb = (mib: number) => format.number((mib || 0) / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    const total = memory.total_mb || 0;
    const [low, high] = memory.range;
    const automatic = value === 0;
    const stored = memory.configured || 0;
    const storedOutside = stored > 0 && (stored < low - 1e-9 || stored > high + 1e-9);
    // Whole percent on both sides: the table's shares and the slider's value are 0.01 steps.
    const step = memory.steps.find((s) => Math.round(s.share * 100) === Math.round(value * 100));
    const left = step ? Math.max(total - step.budget_mb, 0) : Math.max(total - Math.floor(value * total), 0);
    const leavesLow = memory.display !== false && memory.reserve_mb > 0 && left < memory.reserve_mb;
    const muted = 'text-xs text-gray-500 ml-1';
    const amber = 'text-xs text-amber-700 ml-1';

    const reserveLine = memory.display === false ? t('noDisplay')
        : memory.display ? t('reserve', { reserve: gb(memory.reserve_mb) })
            : t('reserveUnknown', { reserve: gb(memory.reserve_mb) });

    const toggle = () => {
        if (disabled) return;
        if (!automatic) { onChange(0); return; }
        // From automatic to the slider: start where the automatic share is now.
        onChange(clampShare(memory.auto_share ?? (stored || 0.7), memory.range));
    };

    return (
        <div className="space-y-1.5 pt-1" role="group" aria-label={t('label')} data-gpu-share>
            <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium text-gray-700 ml-1">{t('label')}</span>
                <div className="flex items-center gap-2 shrink-0">
                    <span className="text-xs text-gray-500">{t('auto')}</span>
                    <button
                        type="button"
                        aria-pressed={automatic}
                        aria-label={t('auto')}
                        disabled={disabled}
                        onClick={toggle}
                        className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 ${
                            automatic ? 'bg-gray-800 dark:bg-[#d9d9d9]' : 'bg-gray-300 dark:bg-[#333333]'
                        }`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                                automatic ? 'translate-x-4 dark:bg-[#1a1a1a]' : 'translate-x-1 dark:bg-[#e8e8e8]'
                            }`}
                        />
                    </button>
                </div>
            </div>

            {automatic ? (
                <p className={muted}>
                    {memory.auto_share != null
                        ? t('autoNow', { pct: pct(memory.auto_share), gb: gb(Math.floor(memory.auto_share * total)), total: gb(total) })
                        : t('autoLater')}
                </p>
            ) : (
                <>
                    <div className="flex items-center gap-x-3 gap-y-1 flex-wrap">
                        <input
                            type="range" min={30} max={95} step={1}
                            value={Math.round(value * 100)}
                            disabled={disabled}
                            onChange={(e) => onChange(parseInt(e.target.value, 10) / 100)}
                            aria-label={t('label')}
                            aria-valuetext={`${pct(value)}, ${gb(Math.floor(value * total))} GB`}
                            className="flex-1 min-w-[10rem] accent-gray-900 dark:accent-[#d9d9d9]"
                        />
                        <span className="shrink-0 whitespace-nowrap">
                            <span className="text-xs font-semibold text-gray-800">{pct(value)}</span>
                            <span className="text-xs text-gray-500 ml-2">{t('of', { gb: gb(Math.floor(value * total)), total: gb(total) })}</span>
                        </span>
                    </div>
                    {storedOutside && (
                        <p className={amber}>{t('outOfRange', { pct: pct(stored), min: pct(low), max: pct(high) })}</p>
                    )}
                    {/* Two lines kept for the status: the fits line wraps on a phone where the
                        too-small one does not, and the text below must not jump while the slider
                        moves across the share where the model starts to fit. */}
                    <div className="min-h-[2rem] space-y-1.5" data-share-status>
                        {!step ? (
                            <p className={muted}>{t('afterDownload')}</p>
                        ) : step.ok ? (
                            <p className={muted}>{t('fits', { ctx: step.max_model_len, need: gb(step.need_mb), budget: gb(step.budget_mb) })}</p>
                        ) : memory.min_share != null ? (
                            <p className={amber}>{t('tooSmall', { need: gb(step.need_mb), ctx: step.max_model_len, min: pct(memory.min_share) })}</p>
                        ) : (
                            <p className={amber}>{t('tooSmallAll', { max: pct(high) })}</p>
                        )}
                        {step && memory.free_mb != null && step.budget_mb > memory.free_mb && (
                            <p className={amber}>{t('aboveFree', { free: gb(memory.free_mb) })}</p>
                        )}
                    </div>
                    <p className={leavesLow ? amber : muted}>
                        {leavesLow ? t('leavesLow', { left: gb(left), reserve: gb(memory.reserve_mb) }) : t('leaves', { left: gb(left) })}
                    </p>
                    <p className={muted}>{t('whyMax', { max: pct(high) })}</p>
                </>
            )}
            {automatic && <p className={muted}>{reserveLine}</p>}
            {mode === 'switch' && <p className={muted}>{t('withSwitch')}</p>}
        </div>
    );
}
