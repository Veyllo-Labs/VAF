// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Zustand store for the live voice call.
 *
 * Shared between the call BAR (rendered inside the chat input container) and
 * the CONTROLLER + agent window (rendered at page root): the controller owns
 * the audio/WS logic and writes state; the bar renders it and raises the
 * mute/hangup intents.
 */

import { create } from 'zustand';

export type VoiceSpeaker = 'user' | 'agent' | null;
export type VoiceAgentMode = 'idle' | 'listening' | 'thinking' | 'talking';

interface VoiceCallState {
  active: boolean;
  /** true while the end-of-call exit animation plays (active stays true) */
  closing: boolean;
  /** false when the backend reported no live LLM for the voice lane: the
   *  agent is DEAF - the window shows a muted-mic state and no turns are
   *  sent. */
  voiceReady: boolean;
  /** true in local mode: ONE model time-shared with the main agent - while
   *  a delegated task runs (mainTask set) the voice agent is temporarily
   *  mute and turns pause until the result arrives. */
  exclusive: boolean;
  /** true while the local model is still loading (voice_call_started with
   *  reason "model_loading"): the call heals itself - the controller re-sends
   *  voice_call_start once the model_state push reports loaded. */
  loadingModel: boolean;
  seconds: number;
  speaker: VoiceSpeaker;
  agentMode: VoiceAgentMode;
  /** i18n key suffix for the status slot (listening/thinking/speaking/connecting) */
  statusKey: string;
  /** running main-agent delegation, shown as substatus ('' = none) */
  mainTask: string;
  muted: boolean;
  /** Noise-gate level (mean-frequency scale 0..255-ish): audio below it is
   *  IGNORED by the VAD (not recorded); the call-bar threshold marker sets
   *  it and it persists in localStorage. */
  gateLevel: number;
  /** bar -> controller intents */
  hangupRequested: boolean;

  start: () => void;
  stop: () => void;
  tick: () => void;
  set: (patch: Partial<VoiceCallState>) => void;
  requestHangup: () => void;
  toggleMute: () => void;
  setGateLevel: (v: number) => void;
}

const GATE_DEFAULT = 20;   // the old fixed SPEECH_THRESHOLD
const GATE_MIN = 4;        // never fully open (clicks would pass)
const GATE_MAX = 80;       // never gate real speech away entirely

function loadGate(): number {
  try {
    const v = parseFloat(window.localStorage.getItem('vaf_voice_gate') || '');
    if (Number.isFinite(v)) return Math.min(GATE_MAX, Math.max(GATE_MIN, v));
  } catch { /* SSR / storage off */ }
  return GATE_DEFAULT;
}

export const useVoiceCallStore = create<VoiceCallState>((set) => ({
  active: false,
  closing: false,
  voiceReady: true,
  exclusive: false,
  loadingModel: false,
  seconds: 0,
  speaker: null,
  agentMode: 'idle',
  statusKey: 'connecting',
  mainTask: '',
  muted: false,
  gateLevel: loadGate(),
  hangupRequested: false,

  start: () => set({
    active: true, closing: false, voiceReady: true, exclusive: false,
    loadingModel: false, seconds: 0, speaker: null, agentMode: 'idle',
    statusKey: 'connecting', mainTask: '', muted: false, hangupRequested: false,
  }),
  stop: () => set({ active: false, closing: false, speaker: null, mainTask: '', hangupRequested: false }),
  tick: () => set((s) => ({ seconds: s.seconds + 1 })),
  set: (patch) => set(patch as VoiceCallState),
  requestHangup: () => set({ hangupRequested: true }),
  toggleMute: () => set((s) => ({ muted: !s.muted })),
  setGateLevel: (v: number) => {
    const clamped = Math.min(GATE_MAX, Math.max(GATE_MIN, v));
    try { window.localStorage.setItem('vaf_voice_gate', String(clamped)); } catch { /* noop */ }
    set({ gateLevel: clamped });
  },
}));
