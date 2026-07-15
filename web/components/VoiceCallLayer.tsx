'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useCallback, useEffect, useRef } from 'react';
import { MicOff } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { AgentAvatar } from '@/components/AgentAvatar';
import { useVoiceCallStore } from '@/lib/voiceCallStore';
import { useLocaleStore } from '@/lib/localeStore';

// ── Live-call controller + agent window ──
// The voice-agent FIRST LAYER (user-approved design): hands-free mic loop
// (VAD auto-stop) -> WS voice_call_turn -> spoken reply; real work is
// delegated by the backend to the main agent, whose finished replies arrive
// as normal chat messages and are spoken during the call. This component
// owns all audio/WS logic and renders the agent WINDOW (top-left, canonical
// dot + status slot); the bar UI lives in VoiceCallBar and shares state via
// the voiceCallStore.

interface Props {
    ws: WebSocket | null;
    sessionId: string | null;
    onLocalMessage: (role: 'user' | 'assistant', content: string, kind?: string) => void;
}

const MAX_UTTER_MS = 20000;
const SILENCE_MS = 1500;
const SPEECH_THRESHOLD = 20;
const MIN_SPEECH_MS = 350;    // noise gate: a click is 1-2 voiced frames, real words accumulate
const UNMUTE_GRACE_MS = 400;  // swallow the click/pop of the unmute toggle itself

export function VoiceCallLayer({ ws, sessionId, onLocalMessage }: Props) {
    const t = useTranslations('voiceCall');
    const locale = useLocaleStore((s) => s.locale);
    const store = useVoiceCallStore();
    const active = store.active;

    const stopFlag = useRef(false);
    const streamRef = useRef<MediaStream | null>(null);
    const recorderRef = useRef<MediaRecorder | null>(null);
    const timeouts = useRef<ReturnType<typeof setTimeout>[]>([]);
    const agentAudioRef = useRef<HTMLAudioElement | null>(null);
    const agentCtxRef = useRef<AudioContext | null>(null);
    const agentRafRef = useRef<number>(0);
    const eyePulseRef = useRef<HTMLSpanElement | null>(null);
    const pendingTts = useRef<((b64: string | null) => void) | null>(null);
    const mutedRef = useRef(false);
    mutedRef.current = store.muted;
    const graceUntilRef = useRef(0);
    const discardNextRef = useRef(false);
    // While a delegated result is being synthesized/spoken the listen loop is
    // HELD: the mic must not run while the agent reads the result, or the next
    // turn would transcribe the agent's own voice.
    const holdLoopRef = useRef(false);

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
        try { agentAudioRef.current?.pause(); } catch { /* noop */ }
        agentAudioRef.current = null;
        cancelAnimationFrame(agentRafRef.current);
        if (eyePulseRef.current) eyePulseRef.current.style.transform = 'scale(1)';
        try { agentCtxRef.current?.close(); } catch { /* noop */ }
        agentCtxRef.current = null;
        pendingTts.current = null;
    }, []);

    const endCall = useCallback(() => {
        // Two-phase teardown: audio/WS stop IMMEDIATELY, the visuals play a
        // short exit animation (closing phase) before the layer unmounts -
        // window and ring animate out, and because `active` stays true until
        // stop(), the chat avatars fade back in only AFTER the window is gone.
        if (useVoiceCallStore.getState().closing) return;
        playEarcon('end');
        try { ws?.send(JSON.stringify({ type: 'voice_call_end' })); } catch { /* noop */ }
        cleanup();
        useVoiceCallStore.getState().set({ closing: true });
        setTimeout(() => useVoiceCallStore.getState().stop(), 380);
    }, [ws, cleanup]);

    // Bar -> controller: hangup intent
    useEffect(() => {
        if (active && store.hangupRequested) endCall();
    }, [active, store.hangupRequested, endCall]);

    // Phone-ring while the model loads ("dialing"): a soft 425 Hz tone every
    // 2.5 s until the lane comes alive - then the greeting takes over. Audio
    // only, no animated visuals (GPU rule).
    useEffect(() => {
        if (!active || !store.loadingModel) return;
        playEarcon('dial');
        const iv = setInterval(() => playEarcon('dial'), 2500);
        return () => clearInterval(iv);
    }, [active, store.loadingModel]);

    // Mute is REAL mute: the track goes silent, so the recorder cannot keep
    // capturing the room while muted. Toggling also invalidates the in-flight
    // utterance - buffered audio from the muted period must never ride into
    // the next turn - and unmuting opens a short grace window that swallows
    // the click/pop of the toggle itself.
    useEffect(() => {
        if (!active) return;
        streamRef.current?.getAudioTracks().forEach(tr => { tr.enabled = !store.muted; });
        if (!store.muted) graceUntilRef.current = performance.now() + UNMUTE_GRACE_MS;
        const rec = recorderRef.current;
        if (rec && rec.state === 'recording') {
            discardNextRef.current = true;
            try { rec.stop(); } catch { /* noop */ }
        }
    }, [active, store.muted]);

    // Call timer
    useEffect(() => {
        if (!active) return;
        const id = setInterval(() => useVoiceCallStore.getState().tick(), 1000);
        return () => clearInterval(id);
    }, [active]);

    // Play a base64 WAV with the eye following the amplitude; resolves via onDone.
    const playAgentAudio = useCallback((b64: string, onDone: () => void) => {
        try {
            const bytes = atob(b64);
            const arr = new Uint8Array(bytes.length);
            for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
            const url = URL.createObjectURL(new Blob([arr], { type: 'audio/wav' }));
            const audio = new Audio(url);
            agentAudioRef.current = audio;
            let stopPulse = () => { /* below */ };
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
            } catch { /* analyser optional */ }
            const done = () => {
                stopPulse();
                URL.revokeObjectURL(url);
                if (!stopFlag.current) onDone();
            };
            audio.onplay = () => useVoiceCallStore.getState().set({
                speaker: 'agent', agentMode: 'talking', statusKey: 'speaking',
            });
            audio.onended = done;
            audio.onerror = done;
            audio.play().catch(done);
        } catch {
            onDone();
        }
    }, []);

    // Hands-free listen loop: one utterance -> one voice_call_turn.
    const listenLoop = useCallback(() => {
        if (stopFlag.current || !streamRef.current) return;
        const st = useVoiceCallStore.getState();
        st.set({ speaker: null, agentMode: 'listening', statusKey: 'listening' });

        const stream = streamRef.current;
        const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus' : 'audio/ogg;codecs=opus';
        const recorder = new MediaRecorder(stream, { mimeType: mime });
        recorderRef.current = recorder;
        const chunks: BlobPart[] = [];
        recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };

        const ctx = new AudioContext();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        const buf = new Uint8Array(analyser.frequencyBinCount);
        let heardSpeech = false;
        let voicedMs = 0;
        let lastTick = 0;
        let silenceStart = 0;
        let rafId = 0;
        const tick = () => {
            if (stopFlag.current) { stop(); return; }
            analyser.getByteFrequencyData(buf);
            const now = performance.now();
            const dt = lastTick ? now - lastTick : 0;
            lastTick = now;
            const gated = mutedRef.current || now < graceUntilRef.current;
            const level = gated ? 0 : buf.reduce((a, b) => a + b, 0) / buf.length;
            useVoiceCallStore.getState().set({
                speaker: level > SPEECH_THRESHOLD ? 'user'
                    : useVoiceCallStore.getState().speaker === 'user' && heardSpeech ? 'user' : null,
            });
            if (level > SPEECH_THRESHOLD) { heardSpeech = true; voicedMs += dt; silenceStart = 0; }
            else if (heardSpeech) {
                if (!silenceStart) silenceStart = now;
                else if (now - silenceStart > SILENCE_MS) { stop(); return; }
            }
            rafId = requestAnimationFrame(tick);
        };
        const maxId = setTimeout(() => stop(), MAX_UTTER_MS);
        timeouts.current.push(maxId);
        const stop = () => {
            cancelAnimationFrame(rafId);
            clearTimeout(maxId);
            try { ctx.close(); } catch { /* noop */ }
            if (recorder.state !== 'inactive') recorder.stop();
        };

        recorder.onstop = async () => {
            if (stopFlag.current) return;
            // Mute toggled mid-utterance (or a result is about to be spoken):
            // the buffer is invalid, start fresh - unless the loop is held for
            // result playback, then playAgentAudio's onDone restarts it.
            if (discardNextRef.current) {
                discardNextRef.current = false;
                if (!holdLoopRef.current) later(listenLoop, 300);
                return;
            }
            // Noise gate: a click/pop arms heardSpeech for a frame or two but
            // never accumulates real voiced time - discard, keep listening.
            if (!heardSpeech || voicedMs < MIN_SPEECH_MS) { later(listenLoop, 400); return; }
            // Deaf (no live LLM) or temporarily mute (local time-sharing
            // while the main agent holds the one model): don't send turns
            // that can only fail/stall - the muted-mic state explains why.
            const stNow = useVoiceCallStore.getState();
            if (!stNow.voiceReady || (stNow.exclusive && stNow.mainTask)) {
                later(listenLoop, 600); return;
            }
            // Utterance accepted for processing: give the user an audible "heard you"
            playEarcon('accept');
            useVoiceCallStore.getState().set({ speaker: null, agentMode: 'thinking', statusKey: 'thinking' });
            try {
                const blob = new Blob(chunks, { type: mime });
                const wav = await toWav16k(blob);
                const b64 = await blobToBase64(wav);
                const st2 = useVoiceCallStore.getState();
                ws?.send(JSON.stringify({
                    type: 'voice_call_turn', audio: b64, format: 'wav', sessionId,
                    main_busy: !!st2.mainTask, pending_task: st2.mainTask || '',
                }));
                // reply handled in the WS effect below
            } catch {
                later(listenLoop, 600);
            }
        };
        recorder.start();
        rafId = requestAnimationFrame(tick);
    }, [ws, sessionId, later]);

    // WS replies for the call
    useEffect(() => {
        if (!active || !ws) return;
        const handler = (ev: MessageEvent) => {
            let data: any;
            try { data = JSON.parse(ev.data); } catch { return; }

            if (data.type === 'voice_call_reply') {
                // Voice small talk stays OUT of the chat (user decision): the
                // conversation lives in audio. Only a DELEGATION appears, on
                // the right side, as the voice agent's message to the main
                // agent - that is what actually entered the session.
                if (data.delegated) {
                    useVoiceCallStore.getState().set({ mainTask: data.delegated });
                    // Bare task text: the red-bordered bubble + "voice agent"
                    // tag next to the timestamp identify the sender - no
                    // prefix inside the bubble needed.
                    onLocalMessage('user', data.delegated, 'voice_delegation');
                }
                if (data.audio) {
                    // The mic may be LIVE when unsolicited audio arrives (the
                    // greeting lands mid-listen): hold + discard so the agent
                    // never transcribes its own voice. No-op for normal turns
                    // (recorder is already stopped there).
                    holdLoopRef.current = true;
                    const recNow = recorderRef.current;
                    if (recNow && recNow.state === 'recording') {
                        discardNextRef.current = true;
                        try { recNow.stop(); } catch { /* noop */ }
                    }
                    playAgentAudio(data.audio, () => { holdLoopRef.current = false; later(listenLoop, 300); });
                } else {
                    later(listenLoop, 600);
                }
            }
            else if (data.type === 'voice_call_started') {
                // ok:false = no live LLM for the voice lane: the agent is
                // DEAF - show the muted-mic state instead of silently eating
                // the user's words. reason 'model_loading' = local model is
                // being loaded right now (the backend kicked the lazy load):
                // show a loading state and heal via the model_state push.
                // exclusive = local time-sharing: the voice agent goes
                // temporarily mute while the main agent holds the one model
                // (mainTask set).
                useVoiceCallStore.getState().set({
                    voiceReady: data.ok !== false,
                    exclusive: data.exclusive === true,
                    loadingModel: data.ok === false && data.reason === 'model_loading',
                });
            }
            else if (data.type === 'model_state') {
                // Self-heal for the "call started before the local model was
                // loaded" case: once the model reports loaded, re-send
                // voice_call_start - the fresh voice_call_started {ok:true}
                // flips the store and the backend greets, so the call comes
                // alive without user action.
                const stM = useVoiceCallStore.getState();
                if (data.loaded === true && stM.active && !stM.voiceReady) {
                    try {
                        ws.send(JSON.stringify({
                            type: 'voice_call_start', ui_lang: locale, sessionId }));
                    } catch { /* noop */ }
                }
            }
            else if (data.type === 'voice_call_error') {
                // no_speech and friends: just keep listening
                later(listenLoop, 500);
            }
            else if (data.type === 'speaker_enroll_tts') {
                pendingTts.current?.(data.audio || null);
            }
            else if (data.type === 'message_complete' && useVoiceCallStore.getState().mainTask) {
                // The delegated main-agent task finished. Anchor: the SAME
                // event that plays the completion chime in page.tsx. Hardened
                // after a silent live failure:
                // - only THIS session's completion counts (another session's
                //   task/automation must neither clear mainTask nor be spoken),
                // - [ASYNC_ACK] is not the result (the drain delivers it later),
                // - empty content must not swallow the pending task,
                // - think blocks are never spoken,
                // - the mic is held while the result plays (the agent must not
                //   transcribe its own voice as a user turn),
                // - a TTS failure gets a short spoken notice instead of silence.
                if (data.sessionId && sessionId && data.sessionId !== sessionId) return;
                const raw = String(data.content || '');
                if (!raw.trim() || raw.startsWith('[ASYNC_ACK]')) return;
                const text = sanitizeForSpeech(raw).slice(0, 1200);
                // Sentinel-only content (the model parroted a context block):
                // it IS this session's completion - announce briefly, never
                // read markup aloud, never swallow silently.
                const spoken = text || t('resultReady');
                useVoiceCallStore.getState().set({ mainTask: '' });
                holdLoopRef.current = true;
                const rec = recorderRef.current;
                if (rec && rec.state === 'recording') {
                    discardNextRef.current = true;
                    try { rec.stop(); } catch { /* noop */ }
                }
                const resume = () => { holdLoopRef.current = false; later(listenLoop, 300); };
                let retried = false;
                pendingTts.current = (b64) => {
                    if (b64) {
                        pendingTts.current = null;
                        // The loop may have resumed listening while TTS was
                        // synthesizing - stop and discard before speaking.
                        holdLoopRef.current = true;
                        const rec2 = recorderRef.current;
                        if (rec2 && rec2.state === 'recording') {
                            discardNextRef.current = true;
                            try { rec2.stop(); } catch { /* noop */ }
                        }
                        try { agentAudioRef.current?.pause(); } catch { /* noop */ }
                        playAgentAudio(b64, resume);
                    } else if (!retried) {
                        retried = true;
                        try { ws.send(JSON.stringify({ type: 'voice_call_speak', text: t('resultReady') })); }
                        catch { pendingTts.current = null; resume(); }
                    } else {
                        pendingTts.current = null;
                        resume();
                    }
                };
                try { ws.send(JSON.stringify({ type: 'voice_call_speak', text: spoken })); }
                catch { pendingTts.current = null; resume(); }
            }
        };
        ws.addEventListener('message', handler);
        return () => ws.removeEventListener('message', handler);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [active, ws, listenLoop, playAgentAudio]);

    // Call start / teardown
    useEffect(() => {
        if (!active) return;
        stopFlag.current = false;
        (async () => {
            try {
                streamRef.current = await navigator.mediaDevices.getUserMedia({ audio: true });
                streamRef.current.getAudioTracks().forEach(
                    tr => { tr.enabled = !useVoiceCallStore.getState().muted; });
            } catch {
                endCall();
                return;
            }
            ws?.send(JSON.stringify({ type: 'voice_call_start', ui_lang: locale, sessionId }));
            playEarcon('start');
            later(listenLoop, 600);
        })();
        return () => cleanup();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [active]);

    if (!active) return null;

    const mm = String(Math.floor(store.seconds / 60)).padStart(2, '0');
    const ss = String(store.seconds % 60).padStart(2, '0');

    return (<>
        {/* In-call indicator: red inset ring around the whole surface with an
            INWARD glow. z-[45]: the input dock is z-40 with an opaque bottom
            gradient that swallowed the ring's bottom edge; modals (z-50) stay
            above. pointer-events-none, static shadow (only opacity animates -
            GPU-safe). Fades in on call start, out during the closing phase. */}
        <div className={`fixed inset-2 rounded-2xl border-[1.5px] border-red-500/40 shadow-[inset_0_0_48px_10px_rgba(220,38,38,0.16)] pointer-events-none z-[45] transition-opacity duration-300 animate-[voiceFadeIn_0.4s_ease] ${store.closing ? 'opacity-0' : 'opacity-100'}`} />
        {/* Agent window: equal 16px gap to the top edge and the collapsed rail
            (w-16); z-10 keeps it BELOW the session sidebar (z-20), so the
            expanding list slides over it by design. Animates in on call start
            and back out (reverse path) while the call is closing. */}
        <div className={`fixed top-4 left-20 z-10 w-[210px] min-h-[300px] flex flex-col items-center rounded-2xl border border-black/10 dark:border-white/10 bg-white/90 dark:bg-[#1f1f1f]/90 backdrop-blur-lg shadow-2xl px-4 pt-3.5 pb-3.5 ${store.closing ? 'animate-[voiceWindowOut_0.35s_ease_forwards]' : 'animate-[voiceWindowIn_0.35s_ease]'}`}>
            <div className="self-start inline-flex items-center gap-1.5 text-[11px] font-bold font-mono text-red-600">
                <i className="w-1.5 h-1.5 rounded-full bg-red-600 animate-pulse" />
                {t('liveCall')}&nbsp;{mm}:{ss}
            </div>
            {/* deaf = no model at all; muteBusy = local time-sharing, the
                main agent currently holds the one model (temporary). */}
            <div className="my-auto py-6 relative">
                <div style={{ transform: 'scale(3)' }}>
                    {/* Canonical-avatar mode mapping (approved mockup states):
                        listening = slow organic morph+breathe -> 'waiting',
                        thinking  = fast morph+breathe        -> 'working',
                        talking   = agentAvatarTalk           -> 'talking'.
                        The literal 'listening'/'thinking' avatar modes are
                        ear/gaze EMOTES and look wrong as call states. */}
                    <AgentAvatar
                        mode={(!store.voiceReady || (store.exclusive && store.mainTask)) ? 'waiting'
                            : store.agentMode === 'talking' ? 'talking'
                            : store.agentMode === 'thinking' ? 'working' : 'waiting'}
                        dim={!store.voiceReady || (store.exclusive && !!store.mainTask)}
                        eyePulseRef={eyePulseRef} />
                </div>
                {((!store.voiceReady && !store.loadingModel) || (store.exclusive && store.mainTask)) && (
                    <span className="absolute left-1/2 -translate-x-1/2 -bottom-4 flex h-7 w-7 items-center justify-center rounded-full bg-red-600 text-white shadow-md">
                        <MicOff size={15} strokeWidth={2.5} />
                    </span>
                )}
            </div>
            <div className="w-full mt-auto border-t border-black/10 dark:border-white/10 pt-2.5 flex flex-col gap-1">
                <div className="flex items-center gap-2 text-xs font-medium text-gray-700 dark:text-gray-300">
                    <i className={`w-[7px] h-[7px] rounded-full flex-none ${
                        !store.voiceReady ? (store.loadingModel ? 'bg-amber-500 animate-pulse' : 'bg-red-500')
                        : (store.exclusive && store.mainTask) ? 'bg-red-500'
                        : store.statusKey === 'listening' ? 'bg-green-500 animate-pulse'
                        : store.statusKey === 'speaking' ? 'bg-gray-100 animate-pulse'
                        : store.statusKey === 'thinking' ? 'bg-amber-500 animate-pulse'
                        : 'bg-gray-400'}`} />
                    <span>{!store.voiceReady
                        ? (store.loadingModel ? t('status_loading_model') : t('status_deaf'))
                        : (store.exclusive && store.mainTask) ? t('status_deaf_busy')
                        : t('status_' + store.statusKey)}</span>
                </div>
                {store.mainTask && (
                    <div className="flex items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
                        <span className="flex-none scale-[0.45] -m-2.5"><AgentAvatar mode="working" /></span>
                        <span className="truncate">{t('mainWorking')}</span>
                    </div>
                )}
            </div>
        </div>
    </>);
}

// ── earcons: tiny synthesized audio cues (no asset files) ──
// start = gentle ascending two-tone, end = descending twin, accept = one soft
// blip the moment an utterance is accepted for processing ("I heard you").
// A short-lived AudioContext per cue; calls originate from a user gesture
// (call button) so autoplay policy is satisfied.
function playEarcon(kind: 'start' | 'end' | 'accept' | 'dial') {
    try {
        const ctx = new AudioContext();
        const gain = ctx.createGain();
        gain.connect(ctx.destination);
        const tones: Array<[number, number, number]> =
            kind === 'start' ? [[440, 0, 0.10], [660, 0.10, 0.12]]
            : kind === 'end' ? [[660, 0, 0.10], [440, 0.10, 0.12]]
            : kind === 'dial' ? [[425, 0, 0.45]]  // phone ringing tone while the model loads
            : [[880, 0, 0.06]];
        const vol = kind === 'accept' ? 0.10 : kind === 'dial' ? 0.08 : 0.16;
        const t0 = ctx.currentTime;
        const total = tones[tones.length - 1][1] + tones[tones.length - 1][2];
        for (const [freq, off, dur] of tones) {
            const osc = ctx.createOscillator();
            osc.type = 'sine';
            osc.frequency.value = freq;
            osc.connect(gain);
            osc.start(t0 + off);
            osc.stop(t0 + off + dur);
        }
        gain.gain.setValueAtTime(0, t0);
        gain.gain.linearRampToValueAtTime(vol, t0 + 0.015);
        gain.gain.setValueAtTime(vol, t0 + Math.max(0.02, total - 0.05));
        gain.gain.linearRampToValueAtTime(0, t0 + total);
        setTimeout(() => { try { ctx.close(); } catch { /* noop */ } }, total * 1000 + 200);
    } catch { /* audio cues are optional */ }
}

// Strip everything that must never reach TTS: think blocks, sentinel tokens,
// and "[Context: ...]" squash/context blocks with their tool list lines (the
// model sometimes parrots those - a live call once read one aloud verbatim).
function sanitizeForSpeech(raw: string): string {
    const noThink = raw.replace(/<think>[\s\S]*?<\/think>/g, '');
    const lines = noThink.split('\n').filter(l => {
        const s = l.trim();
        if (/^\[(Context|ASYNC_ACK|SYSTEM_LOG_ONLY|Generation stopped)/i.test(s)) return false;
        if (/^-\s*\w+\s*\(.*\)\s*$/.test(s)) return false;              // "- mail_inbox({...})"
        if (/^-\s*\w+.*(→|->)\s*(OK|FAILED)/.test(s)) return false;     // "- find_mail → OK: ..."
        return true;
    });
    return lines.join('\n').trim();
}

// ── audio helpers (same 16 kHz mono WAV contract as the chat mic) ──

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
