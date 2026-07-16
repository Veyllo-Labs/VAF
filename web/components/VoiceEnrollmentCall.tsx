'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { toWav16k } from '@/lib/wav';
import { AgentAvatar, type AvatarMode } from '@/components/AgentAvatar';
import { useLocaleStore } from '@/lib/localeStore';

// ── Voice-profile enrollment as a LIVE CALL ──
// The first use case of the call surface: the agent asks casual questions,
// the user answers hands-free (VAD auto-stop), each answer is one enrollment
// round (WS speaker_enroll_round). The guide overlay (progress/confidence/
// rounds) is enrollment-specific; everything else is the generic call UI.
// No TTS during the call on purpose: agent speech would bleed into the mic.
//
// Anti-spoofing contract (backend-enforced, mirrored in the UI copy): the
// profile only changes through this explicit enrollment, never from live
// conversations; inconsistent rounds are rejected and repeated.

interface Props {
    open: boolean;
    ws: WebSocket | null;
    displayName: string;
    onClose: (saved: boolean) => void;
}

type Phase = 'connect' | 'intro' | 'ask' | 'record' | 'process' | 'roundResult' | 'done' | 'saving';

const TARGET_SECONDS = 25;
const MAX_ROUNDS = 15;
const MAX_ANSWER_MS = 15000;
const SILENCE_MS = 1500;
const SPEECH_THRESHOLD = 20;

export function VoiceEnrollmentCall({ open, ws, displayName, onClose }: Props) {
    const t = useTranslations('settings.voiceEnroll');
    // Authoritative UI locale (the same store that feeds NextIntlClientProvider).
    const locale = useLocaleStore((s) => s.locale);
    // Spoken lines arrive from the server in the USER'S language (config
    // "language" first, UI locale as fallback); the visual UI labels stay in
    // the UI locale via next-intl. IMPORTANT: all spoken text is read from
    // linesRef AT CALL TIME - never captured in render-time closures, so a
    // late speaker_enroll_started can never leave stale-language questions.
    const linesRef = useRef<any>(null);
    const [phase, setPhase] = useState<Phase>('connect');
    const [avatarMode, setAvatarMode] = useState<AvatarMode>('idle');
    const [caption, setCaption] = useState('');
    const [chip, setChip] = useState<{ text: string; warn: boolean } | null>(null);
    const [netSeconds, setNetSeconds] = useState(0);
    const [confidence, setConfidence] = useState('-');
    const [round, setRound] = useState(0);
    const [timer, setTimer] = useState(0);
    const [recording, setRecording] = useState(false);

    const questionIdx = useRef(0);
    const timeouts = useRef<ReturnType<typeof setTimeout>[]>([]);
    const streamRef = useRef<MediaStream | null>(null);
    const recorderRef = useRef<MediaRecorder | null>(null);
    const audioCtxRef = useRef<AudioContext | null>(null);
    const stopFlag = useRef(false);
    // Agent voice: resolver for the pending speaker_enroll_tts reply + the
    // currently playing clip (so hang-up cuts the agent off immediately).
    const pendingTts = useRef<((b64: string | null) => void) | null>(null);
    const agentAudioRef = useRef<HTMLAudioElement | null>(null);
    // Voice-reactive visuals: ONLY the agent's EYE (the morphing dot) follows
    // the TTS amplitude - the body stays still; the user's waveform bars
    // follow the mic analyser. Both are driven per-frame via style.transform
    // (GPU-safe, no keyframes).
    const eyePulseRef = useRef<HTMLSpanElement | null>(null);
    const agentCtxRef = useRef<AudioContext | null>(null);
    const agentRafRef = useRef<number>(0);
    const waveRef = useRef<HTMLDivElement | null>(null);

    // Spoken-text accessors: resolved at CALL TIME from the server lines,
    // with the UI-locale i18n strings as fallback.
    const line = useCallback((key: string, fallbackKey: string) =>
        (linesRef.current && linesRef.current[key]) || t(fallbackKey as any), [t]);
    const questionAt = useCallback((idx: number) => {
        const qs: string[] = linesRef.current?.questions?.length
            ? linesRef.current.questions
            : [t('q1'), t('q2'), t('q3'), t('q4'), t('q5'), t('q6')];
        return qs[idx % qs.length];
    }, [t]);

    const later = useCallback((fn: () => void, ms: number) => {
        const id = setTimeout(fn, ms);
        timeouts.current.push(id);
        return id;
    }, []);

    const cleanup = useCallback(() => {
        stopFlag.current = true;
        timeouts.current.forEach(clearTimeout);
        timeouts.current = [];
        try { recorderRef.current?.state !== 'inactive' && recorderRef.current?.stop(); } catch { /* noop */ }
        recorderRef.current = null;
        streamRef.current?.getTracks().forEach(tr => tr.stop());
        streamRef.current = null;
        try { audioCtxRef.current?.close(); } catch { /* noop */ }
        audioCtxRef.current = null;
        try { agentAudioRef.current?.pause(); } catch { /* noop */ }
        agentAudioRef.current = null;
        pendingTts.current?.(null);
        pendingTts.current = null;
        cancelAnimationFrame(agentRafRef.current);
        if (eyePulseRef.current) eyePulseRef.current.style.transform = 'scale(1)';
        try { agentCtxRef.current?.close(); } catch { /* noop */ }
        agentCtxRef.current = null;
    }, []);

    // Speak a line with the agent's real voice (WS speaker_enroll_speak),
    // show it as caption, and continue when playback ends. Falls back to a
    // reading-time estimate when TTS is unavailable. Recording only ever
    // starts AFTER playback finished, so the agent's voice never bleeds
    // into the enrollment audio.
    const speakAndThen = useCallback((text: string, then: () => void) => {
        if (stopFlag.current) return;
        setCaption(text);
        // Talking animation ONLY while audio actually plays (set in the
        // playback handlers below); silent caption fallback keeps the eye calm.
        const fallbackMs = Math.min(8000, Math.max(2400, text.length * 55));
        let settled = false;
        const proceed = () => {
            if (settled || stopFlag.current) return;
            settled = true;
            later(then, 350);
        };
        const fallbackId = setTimeout(() => {
            if (pendingTts.current) { pendingTts.current = null; proceed(); }
        }, 8000);
        timeouts.current.push(fallbackId);

        pendingTts.current = (b64) => {
            clearTimeout(fallbackId);
            pendingTts.current = null;
            if (stopFlag.current) return;
            if (!b64) { later(proceed, fallbackMs); return; }
            try {
                const bytes = atob(b64);
                const arr = new Uint8Array(bytes.length);
                for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
                const url = URL.createObjectURL(new Blob([arr], { type: 'audio/wav' }));
                const audio = new Audio(url);
                agentAudioRef.current = audio;
                // Amplitude-reactive EYE: route playback through an analyser
                // and let only the morphing dot follow the voice level.
                let stopPulse = () => { /* set below */ };
                try {
                    const ctx = agentCtxRef.current ?? new AudioContext();
                    agentCtxRef.current = ctx;
                    ctx.resume().catch(() => { /* noop */ });
                    const src = ctx.createMediaElementSource(audio);
                    const an = ctx.createAnalyser();
                    an.fftSize = 256;
                    src.connect(an);
                    an.connect(ctx.destination);
                    const data = new Uint8Array(an.frequencyBinCount);
                    // Subtle breathing on top of the canonical talk morph: the
                    // keyframes already reach ~1.38x, so the amplitude only adds
                    // a gentle envelope (max ~1.15x, fast attack / slow decay).
                    let smooth = 0;
                    const pulse = () => {
                        an.getByteFrequencyData(data);
                        const lvl = data.reduce((a, b) => a + b, 0) / data.length / 255;
                        smooth = lvl > smooth ? smooth * 0.6 + lvl * 0.4 : smooth * 0.88 + lvl * 0.12;
                        if (eyePulseRef.current) {
                            eyePulseRef.current.style.transform = `scale(${(1 + smooth * 0.15).toFixed(3)})`;
                        }
                        agentRafRef.current = requestAnimationFrame(pulse);
                    };
                    agentRafRef.current = requestAnimationFrame(pulse);
                    stopPulse = () => {
                        cancelAnimationFrame(agentRafRef.current);
                        if (eyePulseRef.current) eyePulseRef.current.style.transform = 'scale(1)';
                    };
                } catch { /* analyser optional; audio still plays */ }
                const stopTalking = () => { stopPulse(); setAvatarMode('idle'); };
                audio.onplay = () => setAvatarMode('talking');
                audio.onended = () => { stopTalking(); URL.revokeObjectURL(url); proceed(); };
                audio.onerror = () => { stopTalking(); URL.revokeObjectURL(url); later(proceed, fallbackMs); };
                audio.play().catch(() => { stopTalking(); URL.revokeObjectURL(url); later(proceed, fallbackMs); });
            } catch {
                later(proceed, fallbackMs);
            }
        };
        try {
            ws?.send(JSON.stringify({ type: 'speaker_enroll_speak', text }));
        } catch {
            clearTimeout(fallbackId);
            pendingTts.current = null;
            later(proceed, fallbackMs);
        }
    }, [ws, later]);

    const abort = useCallback(() => {
        try { ws?.send(JSON.stringify({ type: 'speaker_enroll_abort' })); } catch { /* noop */ }
        cleanup();
        onClose(false);
    }, [ws, cleanup, onClose]);

    // Call timer
    useEffect(() => {
        if (!open) return;
        const id = setInterval(() => setTimer(x => x + 1), 1000);
        return () => clearInterval(id);
    }, [open]);

    // WS replies for this call (addEventListener keeps page.tsx's onmessage intact)
    useEffect(() => {
        if (!open || !ws) return;
        const handler = (ev: MessageEvent) => {
            let data: any;
            try { data = JSON.parse(ev.data); } catch { return; }
            if (data.type === 'speaker_enroll_tts') {
                pendingTts.current?.(data.audio || null);
            }
            else if (data.type === 'speaker_enroll_started') {
                if (data.lines) linesRef.current = data.lines;
            }
            else if (data.type === 'speaker_enroll_round_result') {
                setNetSeconds(data.net_seconds ?? 0);
                setConfidence(data.confidence ?? '-');
                setRound(data.rounds ?? 0);
                if (data.ok) {
                    setChip({ text: t('gained', { s: String(data.gained_seconds ?? 0) }), warn: false });
                    if (data.done) {
                        setPhase('done');
                        setAvatarMode('happy');
                        speakAndThen(line('done', 'doneCaption'), () => {
                            setPhase('saving');
                            ws.send(JSON.stringify({ type: 'speaker_enroll_finalize', display_name: displayName }));
                        });
                    } else {
                        later(() => nextQuestion(), 1400);
                    }
                } else {
                    const key = data.quality === 'inconsistent_voice' ? 'qInconsistent'
                        : data.quality === 'no_speech' ? 'qNoSpeech'
                        : data.quality === 'too_short' ? 'qTooShort'
                        : data.quality === 'engine_unavailable' ? 'qEngine' : 'qError';
                    const lineKey = data.quality === 'inconsistent_voice' ? 'q_inconsistent'
                        : data.quality === 'no_speech' ? 'q_no_speech'
                        : data.quality === 'too_short' ? 'q_too_short'
                        : data.quality === 'engine_unavailable' ? 'q_engine' : 'q_error';
                    setChip({ text: t(key + 'Chip'), warn: true });
                    speakAndThen(line(lineKey, key), () => askAgain());
                }
            } else if (data.type === 'speaker_profile' && phase === 'saving') {
                cleanup();
                onClose(!!data.saved);
            }
        };
        ws.addEventListener('message', handler);
        return () => ws.removeEventListener('message', handler);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open, ws, phase, displayName]);

    // Call start: mic permission, WS session, intro
    useEffect(() => {
        if (!open) return;
        stopFlag.current = false;
        setPhase('connect'); setAvatarMode('idle'); setCaption(''); setChip(null);
        setNetSeconds(0); setConfidence('-'); setRound(0); setTimer(0);
        questionIdx.current = 0;

        (async () => {
            try {
                streamRef.current = await navigator.mediaDevices.getUserMedia({ audio: true });
            } catch {
                setCaption(t('micDenied'));
                later(() => abort(), 3200);
                return;
            }
            ws?.send(JSON.stringify({ type: 'speaker_enroll_start', ui_lang: locale }));
            setPhase('intro');
            // Tiny grace period so speaker_enroll_started (with the spoken
            // lines in the user's language) arrives before the intro starts.
            later(() => {
                speakAndThen(line('intro1', 'intro1'), () => {
                    speakAndThen(line('intro2', 'intro2'), () => nextQuestion());
                });
            }, 450);
        })();

        return () => cleanup();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open]);

    const nextQuestion = useCallback(() => {
        if (stopFlag.current) return;
        const q = questionAt(questionIdx.current);
        questionIdx.current += 1;
        setChip(null);
        setPhase('ask');
        speakAndThen(q, () => startRecording());
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [questionAt, speakAndThen]);

    const askAgain = useCallback(() => {
        if (stopFlag.current) return;
        questionIdx.current = Math.max(0, questionIdx.current - 1);
        nextQuestion();
    }, [nextQuestion]);

    // One answer = one round: record with VAD auto-stop, convert to 16k WAV, send.
    const startRecording = useCallback(() => {
        if (stopFlag.current || !streamRef.current) return;
        setPhase('record');
        setAvatarMode('listening');
        setRecording(true);

        const stream = streamRef.current;
        const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus' : 'audio/ogg;codecs=opus';
        const recorder = new MediaRecorder(stream, { mimeType: mime });
        recorderRef.current = recorder;
        const chunks: BlobPart[] = [];
        recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };

        // VAD: stop after SILENCE_MS below threshold once speech was heard
        const ctx = new AudioContext();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        const buf = new Uint8Array(analyser.frequencyBinCount);
        let heardSpeech = false;
        let silenceStart = 0;
        let rafId = 0;
        const tick = () => {
            analyser.getByteFrequencyData(buf);
            const level = buf.reduce((a, b) => a + b, 0) / buf.length;
            // Live waveform: the bars follow the real mic spectrum, not a
            // canned animation (transform-only, driven per frame).
            const bars = waveRef.current?.children;
            if (bars) {
                const step = Math.floor(buf.length / bars.length) || 1;
                for (let i = 0; i < bars.length; i++) {
                    const v = Math.min(1, (buf[i * step] || 0) / 160);
                    (bars[i] as HTMLElement).style.transform = `scaleY(${(0.15 + v * 0.85).toFixed(3)})`;
                }
            }
            const now = performance.now();
            if (level > SPEECH_THRESHOLD) { heardSpeech = true; silenceStart = 0; }
            else if (heardSpeech) {
                if (!silenceStart) silenceStart = now;
                else if (now - silenceStart > SILENCE_MS) { stop(); return; }
            }
            rafId = requestAnimationFrame(tick);
        };
        const maxId = setTimeout(() => stop(), MAX_ANSWER_MS);
        timeouts.current.push(maxId);

        const stop = () => {
            cancelAnimationFrame(rafId);
            clearTimeout(maxId);
            try { ctx.close(); } catch { /* noop */ }
            if (recorder.state !== 'inactive') recorder.stop();
        };

        recorder.onstop = async () => {
            setRecording(false);
            if (stopFlag.current) return;
            setPhase('process');
            setAvatarMode('thinking');
            try {
                const blob = new Blob(chunks, { type: mime });
                const wav = await toWav16k(blob);
                const b64 = await blobToBase64(wav);
                ws?.send(JSON.stringify({ type: 'speaker_enroll_round', audio: b64, format: 'wav' }));
            } catch {
                setChip({ text: t('qErrorChip'), warn: true });
                later(() => askAgain(), 2400);
            }
        };
        recorder.start();
        rafId = requestAnimationFrame(tick);
    }, [ws, later, askAgain, t]);

    if (!open) return null;

    const mm = String(Math.floor(timer / 60)).padStart(2, '0');
    const ss = String(timer % 60).padStart(2, '0');
    const pct = Math.min(100, (netSeconds / TARGET_SECONDS) * 100);
    const confPct = netSeconds >= TARGET_SECONDS ? 100 : netSeconds >= 16 ? 75 : netSeconds >= 8 ? 45 : 15;

    return (
        <div className="fixed inset-0 z-[80] bg-gradient-to-b from-gray-100 via-gray-50 to-gray-200 dark:from-[#232323] dark:via-[#171717] dark:to-[#101010]">
            {/* Top: mode pill + timer */}
            <div className="absolute top-0 left-1/2 -translate-x-1/2 w-full max-w-[720px] flex justify-between items-center px-6 pt-4 text-sm text-gray-500 dark:text-gray-400">
                <span className="inline-flex items-center gap-2 bg-white/75 dark:bg-[#1e1e1e]/80 border border-black/10 dark:border-white/10 backdrop-blur rounded-full px-3 py-1 text-xs font-semibold text-gray-900 dark:text-gray-100">
                    <i className="w-[7px] h-[7px] rounded-full bg-red-600 animate-pulse" />
                    {t('mode')}
                </span>
                <span className="font-mono">{mm}:{ss}</span>
            </div>

            {/* Center: the agent, exactly centered */}
            <div className="absolute left-1/2 top-[44%] -translate-x-1/2 -translate-y-1/2 flex flex-col items-center gap-5">
                <div className="w-[144px] h-[144px] flex items-center justify-center">
                    <div style={{ transform: 'scale(4)' }}>
                        <AgentAvatar mode={avatarMode} eyePulseRef={eyePulseRef} />
                    </div>
                </div>
                <div className="text-sm text-gray-500 dark:text-gray-400 min-h-[20px]">
                    {phase === 'connect' ? t('connecting')
                        : phase === 'record' ? t('listening')
                        : phase === 'process' || phase === 'saving' ? t('processing')
                        : t('speaking')}
                </div>
            </div>

            {/* Captions + waveform + chip: fixed zone below center */}
            <div className="absolute left-1/2 top-[63%] -translate-x-1/2 w-[min(640px,calc(100%-48px))] text-center flex flex-col items-center gap-3">
                <div className="text-lg md:text-xl font-medium leading-relaxed text-gray-900 dark:text-gray-100 min-h-[62px]">
                    {caption}
                </div>
                {recording && (
                    <div ref={waveRef} className="flex justify-center items-center gap-[3px] h-[34px]">
                        {Array.from({ length: 22 }).map((_, i) => (
                            <i key={i}
                               className="w-1 h-8 rounded bg-amber-600 origin-center"
                               style={{ transform: 'scaleY(0.15)', transition: 'transform 60ms linear' }} />
                        ))}
                    </div>
                )}
                {chip && (
                    <span className={`rounded-full px-4 py-1.5 text-xs border ${chip.warn
                        ? 'bg-yellow-100 text-yellow-700 border-yellow-300 dark:bg-amber-500/10 dark:text-amber-400 dark:border-amber-500/40'
                        : 'bg-green-100 text-green-700 border-green-300 dark:bg-emerald-500/10 dark:text-emerald-400 dark:border-emerald-500/40'}`}>
                        {chip.text}
                    </span>
                )}
            </div>

            {/* Guide overlay: enrollment-specific, not part of the generic call UI */}
            <div className="absolute left-1/2 bottom-[128px] -translate-x-1/2 w-[min(560px,calc(100%-48px))] bg-white/75 dark:bg-[#1e1e1e]/80 border border-black/10 dark:border-white/10 backdrop-blur rounded-2xl px-5 py-3 grid grid-cols-[1fr_1fr_auto] max-md:grid-cols-1 gap-4 items-center">
                <div>
                    <div className="flex justify-between text-[11px] text-gray-500 dark:text-gray-400 mb-1">
                        <span>{t('progressSpeech')}</span><span>{Math.round(netSeconds)} / {TARGET_SECONDS} s</span>
                    </div>
                    <div className="h-1.5 rounded bg-gray-200 dark:bg-[#333] overflow-hidden">
                        <div className="h-full rounded bg-amber-600 transition-all" style={{ width: `${pct}%` }} />
                    </div>
                </div>
                <div>
                    <div className="flex justify-between text-[11px] text-gray-500 dark:text-gray-400 mb-1">
                        <span>{t('progressConfidence')}</span><span>{confidence}</span>
                    </div>
                    <div className="h-1.5 rounded bg-gray-200 dark:bg-[#333] overflow-hidden">
                        <div className="h-full rounded bg-green-600 dark:bg-emerald-500 transition-all" style={{ width: `${confPct}%` }} />
                    </div>
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                    {t('round', { n: String(round), max: String(MAX_ROUNDS) })}
                </div>
            </div>

            {/* Controls */}
            <div className="absolute left-1/2 bottom-7 -translate-x-1/2 flex items-center">
                <div className="text-center">
                    <button onClick={abort} title={t('hangUp')}
                        className="w-[62px] h-[62px] rounded-full bg-red-600 hover:bg-red-700 text-white flex items-center justify-center shadow-lg transition-transform hover:scale-105">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" style={{ transform: 'rotate(135deg)' }}>
                            <path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z"/>
                        </svg>
                    </button>
                    <div className="text-[11px] text-gray-500 dark:text-gray-400 mt-1.5">{t('hangUp')}</div>
                </div>
            </div>
        </div>
    );
}

// ── audio helpers (self-contained; 16 kHz mono 16-bit WAV like the chat mic) ──


function blobToBase64(blob: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onloadend = () => resolve(String(r.result).split(',')[1] || '');
        r.onerror = reject;
        r.readAsDataURL(blob);
    });
}
