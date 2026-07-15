// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Non-reactive audio handles for the live call.
 *
 * The controller (VoiceCallLayer) owns the mic MediaStream; the call bar
 * (VoiceCallBar) drives its waveform from a real AnalyserNode on it - per
 * animation frame, so this must NOT live in the zustand store (a set() per
 * frame would re-render the world). Plain module singleton instead.
 */
export const voiceCallAudio: {
  stream: MediaStream | null;
  /** Analyser on the AGENT's playing audio (set per playback by the layer's
   *  eye-pulse pipeline) - lets the call bar show the agent's REAL level. */
  agentAnalyser: AnalyserNode | null;
} = { stream: null, agentAnalyser: null };
