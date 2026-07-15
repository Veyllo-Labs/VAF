'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Loader2, Mic, ThumbsDown, ThumbsUp } from 'lucide-react';

// ── Recognition test (Settings > Voice) ──
// Ported from the approved speaker-diff mockup: threshold slider with the
// three-band explanation, plus a LIVE test - a red level meter shows the
// recording is hot, the system says who it detected, and the verdict flow
// mirrors the confirmation card: "Was that right?" -> personalized yes/no
// (thumbs) -> on "no" an OPTIONAL name that stores the voice as a named
// third-party profile. Calibration and naming never touch the owner profile.

const RECORD_MS = 4000;
const WAVE_BARS = 14;

interface TestResult {
    label: string;
    score: number;
    name?: string;
    display_name?: string;
    net_seconds?: number;
}

interface Stats {
    n?: number;
    n_owner?: number;
    n_other?: number;
    owner_avg?: number;
    other_avg?: number;
    suggested_threshold?: number;
}

export function SpeakerTest({ apiBase, threshold, band, isAdmin, onThresholdChange }: {
    apiBase: string;
    threshold: number;
    band: number;
    isAdmin: boolean;
    onThresholdChange: (v: number) => void;
}) {
    const t = useTranslations('settings.voice');
    const [phase, setPhase] = useState<'idle' | 'recording' | 'analyzing'>('idle');
    const [result, setResult] = useState<TestResult | null>(null);
    const [error, setError] = useState<string>('');
    const [stats, setStats] = useState<Stats | null>(null);
    const [verdictPhase, setVerdictPhase] = useState<'ask' | 'name' | 'done'>('ask');
    const [savedName, setSavedName] = useState<string>('');
    const [nameInput, setNameInput] = useState('');
    const [countdown, setCountdown] = useState(0);
    const busyRef = useRef(false);
    const audioB64Ref = useRef<string>('');
    const waveRef = useRef<HTMLDivElement | null>(null);

    const lower = Math.max(0, threshold - band);

    const runTest = async () => {
        if (busyRef.current) return;
        busyRef.current = true;
        setError(''); setResult(null); setStats(null);
        setVerdictPhase('ask'); setSavedName(''); setNameInput('');
        let ctx: AudioContext | null = null;
        let rafId = 0;
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus' : 'audio/ogg;codecs=opus';
            const rec = new MediaRecorder(stream, { mimeType: mime });
            const chunks: BlobPart[] = [];
            rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
            const done = new Promise<void>((resolve) => { rec.onstop = () => resolve(); });
            rec.start();
            setPhase('recording');

            // Live level meter: the user must SEE the mic is hot (same red
            // wave language as the call bar; transform-only updates).
            try {
                ctx = new AudioContext();
                const src = ctx.createMediaStreamSource(stream);
                const analyser = ctx.createAnalyser();
                analyser.fftSize = 64;
                src.connect(analyser);
                const buf = new Uint8Array(analyser.frequencyBinCount);
                const tick = () => {
                    analyser.getByteFrequencyData(buf);
                    const el = waveRef.current;
                    if (el) {
                        for (let i = 0; i < el.children.length; i++) {
                            const v = buf[Math.floor((i / el.children.length) * buf.length)] / 255;
                            (el.children[i] as HTMLElement).style.transform =
                                `scaleY(${Math.max(0.12, v).toFixed(2)})`;
                        }
                    }
                    rafId = requestAnimationFrame(tick);
                };
                rafId = requestAnimationFrame(tick);
            } catch { /* meter is optional */ }

            for (let s = Math.round(RECORD_MS / 1000); s > 0; s--) {
                setCountdown(s);
                await new Promise(r => setTimeout(r, 1000));
            }
            rec.stop();
            await done;
            stream.getTracks().forEach(tr => tr.stop());
            setPhase('analyzing');
            const wav = await toWav16k(new Blob(chunks, { type: mime }));
            const b64 = await blobToBase64(wav);
            audioB64Ref.current = b64;
            const resp = await fetch(`${apiBase}/api/speaker/test`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ audio: b64 }),
            });
            const data = await resp.json();
            if (!data.ok) { setError(t(`testError_${data.error || 'no_speech'}`)); }
            else setResult(data);
        } catch {
            setError(t('testError_mic'));
        } finally {
            cancelAnimationFrame(rafId);
            try { ctx?.close(); } catch { /* noop */ }
            setPhase('idle'); setCountdown(0); busyRef.current = false;
        }
    };

    const sendVerdict = async (verdict: 'correct' | 'wrong', name?: string) => {
        if (!result) return;
        setVerdictPhase('done');
        try {
            const resp = await fetch(`${apiBase}/api/speaker/feedback`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    score: result.score, label: result.label, verdict,
                    ...(name ? { name, audio: audioB64Ref.current } : {}),
                }),
            });
            const data = await resp.json();
            if (data.ok) {
                setStats(data.stats || null);
                if (data.saved_profile) setSavedName(data.saved_profile);
            }
        } catch { /* calibration is best-effort */ }
    };

    const ownerName = result?.display_name || '';
    const resultText = !result ? '' :
        result.label === 'self' ? t('testResult_self', { name: ownerName })
        : result.label === 'named' ? t('testResult_named', { name: result.name || '' })
        : result.label === 'other' ? t('testResult_other')
        : t('testResult_unsure');
    const yesText = result?.label === 'self'
        ? t('testYesName', { name: ownerName }) : t('testYes');
    const noText = result?.label === 'self'
        ? t('testNoName', { name: ownerName }) : t('testNo');

    return (
        <div className="mt-5 pt-4 border-t border-gray-100 dark:border-white/10">
            <p className="text-sm font-medium text-gray-800 dark:text-gray-200">{t('testTitle')}</p>
            <p className="text-xs text-gray-500 mt-1 max-w-xl">{t('testIntro')}</p>

            {isAdmin && (
                <div className="mt-3">
                    <div className="flex items-center gap-3">
                        <label className="text-xs text-gray-500 w-20">{t('testThreshold')}</label>
                        <input type="range" min={0.3} max={0.8} step={0.01} value={threshold}
                            onChange={(e) => onThresholdChange(parseFloat(e.target.value))}
                            className="flex-1 max-w-[260px] accent-amber-500" />
                        <span className="font-mono text-sm font-semibold text-amber-600">{threshold.toFixed(2)}</span>
                    </div>
                    <p className="text-[11px] text-gray-400 mt-1">
                        {t('testBandInfo', { t: threshold.toFixed(2), low: lower.toFixed(2) })}
                    </p>
                </div>
            )}

            <div className="mt-3 flex items-center gap-3 flex-wrap">
                <button type="button" onClick={runTest} disabled={phase !== 'idle'}
                    className={`inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg transition-colors disabled:opacity-80 ${
                        phase === 'recording'
                            ? 'bg-[#fdecec] dark:bg-[#2a1a1a] border border-red-500/60 text-red-600'
                            : 'bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818]'}`}>
                    {phase === 'recording' ? <><Mic size={14} className="animate-pulse" />{t('testRecording', { s: String(countdown) })}</>
                        : phase === 'analyzing' ? <><Loader2 size={14} className="animate-spin" />{t('testAnalyzing')}</>
                        : <><Mic size={14} />{t('testRecord')}</>}
                </button>
                {/* Live level: red bars driven by the real mic amplitude */}
                {phase === 'recording' && (
                    <div ref={waveRef} className="flex items-center gap-[2.5px] h-6" aria-hidden>
                        {Array.from({ length: WAVE_BARS }, (_, i) => (
                            <i key={i} className="w-[3px] h-5 rounded-[2px] bg-red-500 origin-center"
                                style={{ transform: 'scaleY(0.12)', transition: 'transform 80ms linear' }} />
                        ))}
                    </div>
                )}
                {error && <span className="text-xs text-red-500">{error}</span>}
            </div>

            {result && (
                <div className="mt-3 rounded-lg border border-gray-200 dark:border-white/10 p-3 max-w-xl">
                    <div className="flex items-center justify-between gap-3 flex-wrap">
                        {/* Unknown voice reads RED (result text + bar) - blue looked like a
                            neutral/positive state and confused the verdict step */}
                        <span className={`text-sm font-semibold ${result.label === 'other' ? 'text-red-500' : 'text-gray-800 dark:text-gray-200'}`}>{resultText}</span>
                        <span className="font-mono text-xs text-gray-500">Score {result.score.toFixed(2)}</span>
                    </div>
                    {/* score meter with threshold + band markers */}
                    <div className="relative h-2 mt-2 rounded bg-gray-100 dark:bg-white/10 overflow-visible">
                        <div className={`absolute inset-y-0 left-0 rounded ${result.label === 'self' ? 'bg-amber-500' : result.label === 'unsure' ? 'bg-gray-400' : result.label === 'named' ? 'bg-sky-500' : 'bg-red-500'}`}
                            style={{ width: `${Math.min(100, Math.max(2, result.score * 100))}%` }} />
                        <i className="absolute -top-1 -bottom-1 w-0.5 bg-amber-600" style={{ left: `${threshold * 100}%` }} />
                        <i className="absolute -top-1 -bottom-1 w-0.5 bg-gray-400/70" style={{ left: `${lower * 100}%` }} />
                    </div>

                    {verdictPhase === 'ask' && (
                        <div className="mt-3">
                            <p className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('testVerdictAsk')}</p>
                            <div className="flex items-center gap-2 mt-2 flex-wrap">
                                {/* Neutral buttons - the verdict color lives in the thumb */}
                                <button type="button" onClick={() => sendVerdict('correct')}
                                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-[#232323] text-gray-800 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-[#2c2c2c]">
                                    <ThumbsUp size={13} className="text-green-600" />{yesText}
                                </button>
                                <button type="button" onClick={() => setVerdictPhase('name')}
                                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-[#232323] text-gray-800 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-[#2c2c2c]">
                                    <ThumbsDown size={13} className="text-red-500" />{noText}
                                </button>
                            </div>
                        </div>
                    )}

                    {verdictPhase === 'name' && (
                        <div className="mt-3">
                            <p className="text-sm text-gray-700 dark:text-gray-300">{t('testWhoWas')}</p>
                            <div className="flex items-center gap-2 mt-2 flex-wrap">
                                <input type="text" autoFocus maxLength={32} value={nameInput}
                                    placeholder={t('testWhoWasPlaceholder')}
                                    onChange={(e) => setNameInput(e.target.value)}
                                    className="w-40 px-2 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-[#181818] text-gray-800 dark:text-gray-200" />
                                <button type="button" disabled={!nameInput.trim()}
                                    onClick={() => sendVerdict('wrong', nameInput.trim())}
                                    className="px-3 py-1.5 text-sm font-medium rounded-md bg-sky-600 text-white hover:bg-sky-700 disabled:opacity-50">
                                    {t('testSaveName')}
                                </button>
                                <button type="button" onClick={() => sendVerdict('wrong')}
                                    className="px-3 py-1.5 text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">
                                    {t('testSkipName')}
                                </button>
                            </div>
                            <p className="text-[11px] text-gray-400 mt-1.5">{t('testWhoWasHint')}</p>
                        </div>
                    )}

                    {verdictPhase === 'done' && (
                        <p className="text-xs text-gray-400 mt-2">
                            {savedName ? t('testSavedName', { name: savedName }) : t('testVerdictThanks')}
                        </p>
                    )}

                    {stats && (stats.n_owner || 0) + (stats.n_other || 0) > 0 && (
                        <div className="mt-2 text-[11px] text-gray-500">
                            {t('testStats', {
                                own: stats.owner_avg != null ? stats.owner_avg.toFixed(2) : '-',
                                other: stats.other_avg != null ? stats.other_avg.toFixed(2) : '-',
                                n: String(stats.n || 0),
                            })}
                            {stats.suggested_threshold != null && isAdmin && (
                                <span className="ml-2">
                                    {t('testSuggestion', { t: stats.suggested_threshold.toFixed(2) })}
                                    <button type="button"
                                        onClick={() => onThresholdChange(stats.suggested_threshold as number)}
                                        className="ml-1.5 px-2 py-0.5 rounded bg-amber-500/15 text-amber-600 font-medium hover:bg-amber-500/25">
                                        {t('testSuggestionApply')}
                                    </button>
                                </span>
                            )}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

// Same 16 kHz mono WAV contract as the chat mic / enrollment call.
async function toWav16k(blob: Blob): Promise<Blob> {
    const arrayBuffer = await blob.arrayBuffer();
    const ctx = new AudioContext({ sampleRate: 16000 });
    const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
    const samples = audioBuffer.getChannelData(0);
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
        pcm[i] = Math.max(-32768, Math.min(32767, samples[i] * 32768));
    }
    try { ctx.close(); } catch { /* noop */ }
    const header = new ArrayBuffer(44);
    const v = new DataView(header);
    const writeStr = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    writeStr(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); writeStr(8, 'WAVE');
    writeStr(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    writeStr(36, 'data'); v.setUint32(40, pcm.length * 2, true);
    return new Blob([header, pcm.buffer], { type: 'audio/wav' });
}

function blobToBase64(blob: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onloadend = () => resolve(String(r.result).split(',')[1] || '');
        r.onerror = reject;
        r.readAsDataURL(blob);
    });
}
