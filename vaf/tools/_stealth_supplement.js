/*
 * VAF stealth supplement — the ONLY init script we inject.
 *
 * Our browser is a real HEADED Chromium (under Xvfb) launched with
 * --disable-blink-features=AutomationControlled and a version-matched UA, so it
 * is already clean on the things classic stealth libs patch: navigator.webdriver
 * is natively false, navigator.platform is "Linux x86_64", window.chrome exists,
 * plugins.length is 5, and Object.getOwnPropertyNames(navigator) is empty.
 *
 * We therefore do NOT inject playwright-stealth (it spoofs platform to Win32 —
 * inconsistent with our Linux UA — and mangles navigator.userAgentData, both of
 * which detectors like rebrowser-bot-detector flag). This script only fixes the
 * one thing the container genuinely leaks — a software-WebGL "SwiftShader"
 * renderer — and adds subtle canvas/audio noise. It NEVER defines own properties
 * on `navigator` (that pollution is itself a tell), so it stays consistent with a
 * real Linux Chrome.
 *
 * Honest limitation: JS patching is weaker than a patched binary and a determined
 * anti-bot stack can still detect it; the robust wins live in the launch flags.
 */
(function () {
    'use strict';
    if (window.__vafStealthSupplement) return;
    window.__vafStealthSupplement = true;

    // Per-session seed + tiny deterministic PRNG (stable within a session).
    const SEED = (Math.floor(Math.random() * 0xffffffff)) >>> 0;
    function mulberry32(a) {
        return function () {
            a |= 0; a = (a + 0x6D2B79F5) | 0;
            let t = Math.imul(a ^ (a >>> 15), 1 | a);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }
    // Make a replacement report as native code (defeats fn.toString() checks).
    function maskNative(fn, name) {
        try {
            Object.defineProperty(fn, 'toString', {
                value: () => `function ${name || fn.name || ''}() { [native code] }`,
                configurable: true,
            });
        } catch (e) { /* ignore */ }
        return fn;
    }

    // ── WebGL UNMASKED vendor/renderer → realistic Linux/Mesa Intel ─────────
    // Software rendering (no GPU in the container) otherwise reports
    // "SwiftShader" — a classic headless/VM tell. Patched on the WebGL prototype
    // (does NOT touch navigator), so it stays consistent with the Linux UA.
    try {
        const WEBGL_VENDOR = 'Google Inc. (Intel)';
        const WEBGL_RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2), OpenGL 4.6 (Core Profile) Mesa 23.2.1)';
        const patchGL = (proto) => {
            if (!proto || !proto.getParameter) return;
            const getParameter = proto.getParameter;
            const patched = function (p) {
                if (p === 37445) return WEBGL_VENDOR;    // UNMASKED_VENDOR_WEBGL
                if (p === 37446) return WEBGL_RENDERER;  // UNMASKED_RENDERER_WEBGL
                return getParameter.apply(this, arguments);
            };
            maskNative(patched, 'getParameter');
            proto.getParameter = patched;
        };
        patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
        patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
    } catch (e) { /* ignore */ }

    // ── Canvas fingerprint noise (subtle, seeded, invisible) ────────────────
    try {
        const rng = mulberry32((SEED ^ 0xC0FFEE) >>> 0);
        const noisify = (data) => {
            for (let i = 0; i < data.length; i += 4) {
                data[i] = data[i] + ((rng() * 3) | 0) - 1;
                data[i + 1] = data[i + 1] + ((rng() * 3) | 0) - 1;
                data[i + 2] = data[i + 2] + ((rng() * 3) | 0) - 1;
            }
        };
        const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        const patchedGID = function () {
            const res = origGetImageData.apply(this, arguments);
            try { noisify(res.data); } catch (e) { /* ignore */ }
            return res;
        };
        maskNative(patchedGID, 'getImageData');
        CanvasRenderingContext2D.prototype.getImageData = patchedGID;

        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        const patchedTDU = function () {
            try {
                const ctx = this.getContext('2d');
                if (ctx && this.width > 0 && this.height > 0) {
                    const img = origGetImageData.call(ctx, 0, 0, this.width, this.height);
                    noisify(img.data);
                    ctx.putImageData(img, 0, 0);
                }
            } catch (e) { /* ignore — fall through to original */ }
            return origToDataURL.apply(this, arguments);
        };
        maskNative(patchedTDU, 'toDataURL');
        HTMLCanvasElement.prototype.toDataURL = patchedTDU;
    } catch (e) { /* ignore */ }

    // ── AudioContext fingerprint noise (subtle, seeded) ─────────────────────
    try {
        const rng = mulberry32((SEED ^ 0xA0D107) >>> 0);
        const AP = window.AnalyserNode && AnalyserNode.prototype;
        if (AP && AP.getFloatFrequencyData) {
            const orig = AP.getFloatFrequencyData;
            const patched = function (array) {
                orig.apply(this, arguments);
                try { for (let i = 0; i < array.length; i++) array[i] += (rng() - 0.5) * 1e-3; } catch (e) { /* ignore */ }
            };
            maskNative(patched, 'getFloatFrequencyData');
            AP.getFloatFrequencyData = patched;
        }
    } catch (e) { /* ignore */ }
})();
