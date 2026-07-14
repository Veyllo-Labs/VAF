'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useEffect, useRef } from 'react';
import { useTranslations } from 'next-intl';
import { useVoiceCallStore } from '@/lib/voiceCallStore';

// ── The call bar: overlays the chat input while a live call runs ──
// Fixed slots (info left, waveform absolutely centered, controls right), the
// approved red variant of the recording bar. The waveform is decorative in
// v1 (random amplitude while someone speaks, flat line in silence); the real
// speaker signal drives WHO is animated via the store.

export function VoiceCallBar() {
    const t = useTranslations('voiceCall');
    const store = useVoiceCallStore();
    const waveRef = useRef<HTMLDivElement | null>(null);

    // Animate bars while someone speaks; flat centered line otherwise.
    useEffect(() => {
        const el = waveRef.current;
        if (!el) return;
        let id: ReturnType<typeof setInterval> | null = null;
        if (store.speaker) {
            id = setInterval(() => {
                for (let i = 0; i < el.children.length; i++) {
                    (el.children[i] as HTMLElement).style.transform =
                        `scaleY(${(0.12 + Math.random() * 0.88).toFixed(2)})`;
                }
            }, 90);
        } else {
            for (let i = 0; i < el.children.length; i++) {
                (el.children[i] as HTMLElement).style.transform = 'scaleY(0.1)';
            }
        }
        return () => { if (id) clearInterval(id); };
    }, [store.speaker]);

    const mm = String(Math.floor(store.seconds / 60)).padStart(2, '0');
    const ss = String(store.seconds % 60).padStart(2, '0');

    return (
        <div className={`absolute inset-0 z-20 flex items-center justify-between gap-3 rounded-2xl border border-red-500/60 bg-[#fdecec] dark:bg-[#2a1a1a] px-4 pr-2 ${store.closing ? 'animate-[voiceBarOut_0.3s_ease_forwards]' : 'animate-[voiceBarIn_0.3s_ease]'}`}>
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

            {/* Waveform: absolutely centered in the bar */}
            <span ref={waveRef}
                className={`absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 flex items-center gap-[2.5px] h-6 w-[min(240px,32%)] justify-center ${store.speaker === 'agent' ? 'opacity-70' : ''}`}>
                {Array.from({ length: 24 }).map((_, i) => (
                    <i key={i}
                        className={`w-[3px] h-5 rounded origin-center ${store.speaker === 'agent' ? 'bg-gray-400 dark:bg-gray-500' : 'bg-red-600'}`}
                        style={{ transform: 'scaleY(0.1)', transition: 'transform 80ms linear, background-color 0.2s' }} />
                ))}
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
