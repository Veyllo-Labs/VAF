// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Shared mic-blob -> mono 16-bit PCM WAV converter (chat mic, live call,
 * enrollment, recognition test - one implementation, Rule 2).
 *
 * The AudioContext is asked for 16 kHz, but the header writes the buffer's
 * ACTUAL sample rate: older WebKit builds ignore the sampleRate option and
 * decode at 48 kHz - a hardcoded 16000 header would then lie about the
 * payload (wrong pitch/duration downstream). Every backend consumer
 * resamples by header rate (speaker_id.wav_bytes_to_samples, the STT
 * stack), so an honest header is always correct.
 */
export async function toWav16k(blob: Blob): Promise<Blob> {
    const arrayBuffer = await blob.arrayBuffer();
    const ctx = new AudioContext({ sampleRate: 16000 });
    try {
        const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
        const rate = Math.round(audioBuffer.sampleRate) || 16000;
        const samples = audioBuffer.getChannelData(0);
        const pcm = new Int16Array(samples.length);
        for (let i = 0; i < samples.length; i++) {
            pcm[i] = Math.max(-32768, Math.min(32767, samples[i] * 32768));
        }
        const header = new ArrayBuffer(44);
        const v = new DataView(header);
        const writeStr = (o: number, s: string) => {
            for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i));
        };
        writeStr(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); writeStr(8, 'WAVE');
        writeStr(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
        v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
        writeStr(36, 'data'); v.setUint32(40, pcm.length * 2, true);
        return new Blob([header, pcm.buffer], { type: 'audio/wav' });
    } finally {
        try { ctx.close(); } catch { /* noop */ }
    }
}
