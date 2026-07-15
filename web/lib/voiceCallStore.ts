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
  /** bar -> controller intents */
  hangupRequested: boolean;

  start: () => void;
  stop: () => void;
  tick: () => void;
  set: (patch: Partial<VoiceCallState>) => void;
  requestHangup: () => void;
  toggleMute: () => void;
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
}));
