// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// PCM tap for the server-side voice endpointer: downsamples the mic to 16 kHz mono
// int16 and posts ~100 ms frames to the main thread, which base64s them into
// voice_call_chunk messages. Observation only - the utterance that becomes the turn
// is still recorded and sent whole by the MediaRecorder path; this stream just lets
// the server decide WHEN it ends. Lives in public/ because AudioWorklet modules are
// loaded by URL (ctx.audioWorklet.addModule), not bundled.
class PcmTap extends AudioWorkletProcessor {
    constructor() {
        super();
        this.ratio = sampleRate / 16000;   // worklet-global sampleRate = context rate
        this.acc = 0;                       // fractional read position for decimation
        this.out = new Int16Array(1600);    // 100 ms at 16 kHz
        this.fill = 0;
    }
    process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (!ch) return true;
        // Nearest-sample decimation: fine for a VAD/endpoint feature (the judged
        // audio is prosody-level, not ASR input - the real turn WAV is made
        // elsewhere with a proper resampler).
        for (let i = 0; i < ch.length; i++) {
            this.acc += 1;
            if (this.acc >= this.ratio) {
                this.acc -= this.ratio;
                const s = Math.max(-1, Math.min(1, ch[i]));
                this.out[this.fill++] = (s * 32767) | 0;
                if (this.fill === this.out.length) {
                    // Transfer a copy; the buffer is reused immediately.
                    this.port.postMessage(this.out.slice(0).buffer, []);
                    this.fill = 0;
                }
            }
        }
        return true;
    }
}
registerProcessor('vaf-pcm-tap', PcmTap);
