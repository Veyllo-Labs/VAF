'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useVoiceCallStore } from '@/lib/voiceCallStore';
import { voiceCallAudio } from '@/lib/voiceCallAudio';

// ── The call bar: overlays the chat input while a live call runs ──
// Fixed slots (info left, waveform absolutely centered, controls right), the
// approved red variant of the recording bar. The user-side waveform is a
// REAL level meter on the mic stream (voiceCallAudio); the gray AGENT bars
// remain animated (agent audio plays via an HTMLAudio element without an
// analyser); the speaker signal from the store decides which mode runs.

export function VoiceCallBar() {
    const t = useTranslations('voiceCall');
    const store = useVoiceCallStore();
    const waveRef = useRef<HTMLDivElement | null>(null);
    // Noise-gate marker: full meter scale for mapping gateLevel <-> x-position.
    const METER_MAX = 100;
    const [gateHover, setGateHover] = useState(false);
    const [gateDrag, setGateDrag] = useState(false);
    // Wave-strip rect for the PORTAL tooltip: the chat-input form clips
    // overflow, so the hint must render on document.body, positioned fixed.
    const [gateRect, setGateRect] = useState<DOMRect | null>(null);

    const dragTo = useCallback((clientX: number) => {
        const el = waveRef.current;
        if (!el) return;
        const r = el.getBoundingClientRect();
        const pct = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
        useVoiceCallStore.getState().setGateLevel(pct * METER_MAX);
    }, []);

    // Drag via WINDOW listeners: the slider is an inline flex sibling that
    // REMOUNTS when it moves between bars, so element-level pointer capture
    // would drop the drag after the first step.
    useEffect(() => {
        if (!gateDrag) return;
        const mv = (e: PointerEvent) => dragTo(e.clientX);
        const up = () => setGateDrag(false);
        window.addEventListener('pointermove', mv);
        window.addEventListener('pointerup', up);
        return () => {
            window.removeEventListener('pointermove', mv);
            window.removeEventListener('pointerup', up);
        };
    }, [gateDrag, dragTo]);

    // Waveform (user decision: REAL, like the recognition test): while the
    // user side is live, an AnalyserNode on the actual mic stream drives the
    // bars per frame - muting flattens them naturally (the disabled track
    // delivers true silence). Only while the AGENT speaks do the gray bars
    // fall back to the old animation (its audio plays via an HTMLAudio
    // element without an analyser). Transform-only updates (GPU rule).
    useEffect(() => {
        const el = waveRef.current;
        if (!el || !store.active) return;
        // PERF: ONE AudioContext + ONE rAF loop for the whole call. The loop
        // reads the store imperatively per frame and handles all modes
        // itself (agent = animated, user side = real meter, idle = flat) -
        // effect deps on store.speaker used to tear down / rebuild the
        // AudioContext on every speaker flip (jank on each transition).
        // Only the <i> bars are meter targets - the inline slider is a
        // sibling in the same row and must never be scaled.
        const bars = () => Array.from(el.getElementsByTagName('i')) as HTMLElement[];
        const flat = () => {
            for (const b of bars()) b.style.transform = 'scaleY(0.1)';
        };
        let raf: number | null = null;
        let retry: ReturnType<typeof setTimeout> | null = null;
        let ctx: AudioContext | null = null;
        let analyser: AnalyserNode | null = null;
        let buf: Uint8Array<ArrayBuffer> | null = null;
        let lastAgentFrame = 0;
        const tick = (now: number) => {
            const speaker = useVoiceCallStore.getState().speaker;
            const els = bars();
            const n = els.length;
            if (speaker === 'agent') {
                // Animated gray bars, throttled to the old 90ms cadence.
                if (now - lastAgentFrame > 90) {
                    lastAgentFrame = now;
                    for (let i = 0; i < n; i++) {
                        els[i].style.transform =
                            `scaleY(${(0.12 + Math.random() * 0.88).toFixed(2)})`;
                    }
                }
            } else if (analyser && buf) {
                analyser.getByteFrequencyData(buf);
                // VU fill: same level metric as the VAD (mean energy) fills
                // the strip left-to-right, so the threshold marker means
                // what it gates - excursions that stay left of the line are
                // exactly what the VAD ignores.
                const level = buf.reduce((a, b) => a + b, 0) / buf.length;
                const fill = Math.min(1, level / 100);
                for (let i = 0; i < n; i++) {
                    const lit = (i + 0.5) / n <= fill;
                    const v = buf[Math.floor((i / n) * buf.length)] / 255;
                    els[i].style.transform =
                        `scaleY(${(lit ? Math.max(0.35, v) : 0.1).toFixed(2)})`;
                }
            }
            raf = requestAnimationFrame(tick);
        };
        const start = () => {
            const stream = voiceCallAudio.stream;
            if (!stream) {
                // The stream arrives shortly AFTER call start (getUserMedia).
                flat();
                retry = setTimeout(start, 300);
                return;
            }
            try {
                ctx = new AudioContext();
                const src = ctx.createMediaStreamSource(stream);
                analyser = ctx.createAnalyser();
                analyser.fftSize = 64;
                src.connect(analyser);
                buf = new Uint8Array(analyser.frequencyBinCount);
            } catch { flat(); }
            raf = requestAnimationFrame(tick);
        };
        start();
        return () => {
            if (retry) clearTimeout(retry);
            if (raf !== null) cancelAnimationFrame(raf);
            try { ctx?.close(); } catch { /* noop */ }
            flat();
        };
    }, [store.active]);

    const mm = String(Math.floor(store.seconds / 60)).padStart(2, '0');
    const ss = String(store.seconds % 60).padStart(2, '0');

    return (
        // No own border: the chat-input FORM morphs its border/background to
        // the call colors (mockup mechanic "the line becomes the call bar");
        // this overlay only crossfades the content on top of it.
        <div className={`absolute inset-0 z-20 flex items-center justify-between gap-3 rounded-2xl bg-[#fdecec] dark:bg-[#2a1a1a] px-4 pr-2 ${store.closing ? 'animate-[voiceBarOut_0.25s_ease_forwards]' : 'animate-[voiceBarIn_0.25s_ease]'}`}>
            {/* Info (left) */}
            <span className="flex items-center gap-2 flex-none">
                <i className="w-2 h-2 rounded-full bg-red-600 animate-pulse" />
                <span className="text-[13px] font-semibold text-red-600 whitespace-nowrap">{t('liveCall')}</span>
                <span className="font-mono text-xs text-gray-500 dark:text-gray-400">{mm}:{ss}</span>
                {store.speaker === 'user' && (
                    <span className="text-[11px] font-semibold text-red-600 whitespace-nowrap">{t('youSpeak')}</span>
                )}
                {store.speaker === 'agent' && (
                    <span className="text-[11px] font-semibold text-gray-600 dark:text-gray-300 whitespace-nowrap">{t('agentSpeaks')}</span>
                )}
            </span>

            {/* Waveform + noise-gate marker: a VU meter; bars LEFT of the
                draggable line are the ignored zone (gray), bars RIGHT of it
                are recorded (red). Hovering shows the drag handle + hint. */}
            {/* Outer span only centers (its transform would make nested
                absolute anchoring unreliable). The inner ref span is a plain
                RELATIVE anchor whose width IS the bar cluster (original tight
                gap look, w = 24 bars + gaps) - so the marker's left-% lands
                exactly on the gray/red bar boundary, ON the waveform:
                the user's sketch  ▁▁▁▁<|>▂▅▇▃▁  */}
            <span className={`absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 h-6 flex items-center ${store.speaker === 'agent' ? 'opacity-70' : ''}`}>
            <span ref={waveRef}
                onMouseEnter={() => {
                    setGateHover(true);
                    setGateRect(waveRef.current?.getBoundingClientRect() ?? null);
                }}
                onMouseLeave={() => setGateHover(false)}
                className="relative flex items-center gap-[2.5px] h-full">
                {Array.from({ length: 24 }).map((_, i) => {
                    // Boundary bar index: everything before it is the gray
                    // ignored zone, from it on red. The slider is rendered
                    // INLINE right before this bar - a flex sibling in the
                    // SAME row as the bars, so it is structurally impossible
                    // for it to sit above or below the strip.
                    const k = Math.min(23, Math.max(1,
                        Math.round((store.gateLevel / METER_MAX) * 24)));
                    return (
                        <React.Fragment key={i}>
                            {i === k && store.speaker !== 'agent' && (
                                <span
                                    onPointerDown={(e) => {
                                        e.preventDefault();
                                        setGateRect(waveRef.current?.getBoundingClientRect() ?? null);
                                        setGateDrag(true);
                                        dragTo(e.clientX);
                                    }}
                                    className="group flex items-center h-7 cursor-ew-resize touch-none select-none"
                                    title={t('gateHint')}>
                                    {(gateHover || gateDrag) && (
                                        <ChevronLeft size={10} strokeWidth={3}
                                            className="shrink-0 -mr-[2px] text-gray-800 dark:text-white" />
                                    )}
                                    {/* Quiet by default (gray, no glow); lights up only
                                        while hovered or dragged (user decision). */}
                                    <span className={`shrink-0 w-[3px] h-7 rounded transition-all duration-150 ${gateDrag
                                        ? 'bg-gray-800 dark:bg-white shadow-[0_0_4px_rgba(0,0,0,0.35)] dark:shadow-[0_0_6px_rgba(255,255,255,0.7)]'
                                        : 'bg-gray-400 dark:bg-gray-500 group-hover:bg-gray-800 dark:group-hover:bg-white group-hover:shadow-[0_0_4px_rgba(0,0,0,0.35)] dark:group-hover:shadow-[0_0_6px_rgba(255,255,255,0.7)]'}`} />
                                    {(gateHover || gateDrag) && (
                                        <ChevronRight size={10} strokeWidth={3}
                                            className="shrink-0 -ml-[2px] text-gray-800 dark:text-white" />
                                    )}
                                </span>
                            )}
                            <i
                                className={`w-[3px] h-5 rounded origin-center ${store.speaker === 'agent'
                                    ? 'bg-gray-400 dark:bg-gray-500'
                                    : i < k
                                        ? 'bg-gray-400 dark:bg-gray-500'
                                        : 'bg-red-600'}`}
                                style={{ transform: 'scaleY(0.1)', transition: 'transform 80ms linear, background-color 0.2s' }} />
                        </React.Fragment>
                    );
                })}
                {/* Hover/drag explanation: PORTAL on document.body - the
                    chat-input form clips overflow, an in-flow tooltip above
                    the bar was invisible (live report). */}
                {(gateHover || gateDrag) && gateRect && typeof document !== 'undefined' && createPortal(
                    /* Below the bar, centered on the whole strip (user
                       decision) - portal, since the input form clips. */
                    <span
                        className="fixed z-[100] flex flex-col items-center gap-0.5 px-2.5 py-1.5 rounded-lg bg-white dark:bg-[#262626] border border-black/10 dark:border-white/10 shadow-lg whitespace-nowrap select-none pointer-events-none"
                        style={{
                            left: gateRect.left + gateRect.width / 2,
                            top: gateRect.bottom + 10,
                            transform: 'translate(-50%, 0)',
                        }}>
                        <span className="flex items-center gap-2 text-[10px] leading-none">
                            <span className="text-gray-500 dark:text-gray-400">{t('gateBelow')}</span>
                            <span className="text-gray-300 dark:text-gray-600">|</span>
                            <span className="text-red-600 font-medium">{t('gateAbove')}</span>
                        </span>
                        <span className="text-[10px] leading-tight text-gray-400 dark:text-gray-500">{t('gateHint')}</span>
                    </span>,
                    document.body
                )}
            </span>
            </span>

            {/* Controls (right) */}
            <span className="flex items-center gap-1.5 flex-none">
                <button onClick={store.toggleMute} title={t('mute')}
                    className={`w-9 h-9 rounded-full border flex items-center justify-center transition-colors ${store.muted
                        ? 'text-red-600 border-red-500/50 bg-red-100 dark:bg-red-500/15'
                        : 'text-gray-600 dark:text-gray-300 border-gray-200 dark:border-[#3a3a3a] bg-white dark:bg-[#262626] hover:bg-gray-100 dark:hover:bg-[#2f2f2f]'}`}>
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                        <rect x="9" y="3" width="6" height="11" rx="3" />
                        <path d="M5 11a7 7 0 0 0 14 0" />
                        <line x1="12" y1="18" x2="12" y2="21" />
                        {store.muted && <line x1="4" y1="4" x2="20" y2="20" />}
                    </svg>
                </button>
                <button onClick={store.requestHangup} title={t('hangUp')}
                    className="w-9 h-9 rounded-full bg-red-600 hover:bg-red-700 text-white flex items-center justify-center transition-colors">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" style={{ transform: 'rotate(135deg)' }}>
                        <path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z" />
                    </svg>
                </button>
            </span>
        </div>
    );
}
