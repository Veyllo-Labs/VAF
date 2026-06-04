'use client';

import React from 'react';

// ── Agent Avatar: the living dot, as a character ──
// Keyframes (agentAvatar* + emo* + body* + wink + activity b*/e*/i*) live in globals.css.
// Shared by the chat header (basic modes) and the Whare Wananga training stage (full emotion +
// activity range, plus `invert` for the judge: dark dot on a light square).
//
// PERFORMANCE — READ BEFORE ADDING/EDITING AN ANIMATION (see the ANTI-LEAK RULE in globals.css):
// This avatar renders on EVERY message and runs in QtWebEngine's in-process GPU. Any animation
// that runs `infinite` (idle wink/float, the stage avatars) MUST animate only `transform` /
// `opacity`. NEVER animate `clip-path`, `box-shadow`, `border-radius`, `filter`, `background`,
// `width`/`height` on a continuous animation — each repaints every frame and leaks GPU tiles into
// the renderer (we crashed at 6 GB once: the idle `wink` used clip-path, several `emo*` animated
// box-shadow). The dot's glow is therefore a STATIC box-shadow (`glow` below); the blob-morph
// (border-radius) is allowed only in the chat's short-lived thinking/talking, and the long-running
// stage avatars pass `lite` to drop it. Confirm with the leak_diag logger (VAF_LEAK_DIAG=1).
//
// One unified figure, faithful to docs/animations/agent_avatar: a dark square (the BODY) with
// the white dot (the EYE) NESTED inside it, so when the body jumps/shakes the eye moves with it.
// Emotion modes keep the body subtle and the dot expressive; activity modes (learn/success/error)
// let the body carry the motion + an icon overlay. State changes use the UNIVERSAL MORPH
// (docs/AgentAvatar.md): the whole figure collapses to a soft neutral point, the mode is swapped,
// then it blooms into the new state — so any state flows into any other instead of snapping.
export type AvatarMode =
    | 'idle' | 'waiting' | 'thinking' | 'talking'
    | 'surprised' | 'curious' | 'confused' | 'idea'
    | 'happy' | 'excited' | 'sad' | 'sleepy'
    | 'nod' | 'shake' | 'listening' | 'search'
    | 'celebrate' | 'working'
    // Activity states (body+eye+icon model) — used for the Whare Wananga learn phase:
    | 'learn' | 'success' | 'error';

// eye (dot) animation per mode (matches agent-character-emotions.html). idle / activity handled
// separately; activity drives the eye via E_ACT.
const ANIM: Partial<Record<AvatarMode, string>> = {
    waiting: 'agentAvatarMorph 5.5s ease-in-out infinite, agentAvatarBreathe 4.0s ease-in-out infinite',
    thinking: 'agentAvatarMorph 1.0s ease-in-out infinite, agentAvatarBreathe 0.7s ease-in-out infinite',
    talking: 'agentAvatarTalk 0.75s ease-in-out infinite',
    surprised: 'emoSurprised 2.4s cubic-bezier(.34,1.56,.64,1) infinite',
    curious: 'emoCurious 4.0s ease-in-out infinite',
    confused: 'emoConfused 2.8s ease-in-out infinite, agentAvatarMorph 2.2s ease-in-out infinite',
    idea: 'emoIdea 2.8s cubic-bezier(.5,0,.2,1) infinite',
    happy: 'emoHappy 1.9s cubic-bezier(.3,.7,.3,1) infinite',
    excited: 'emoExcited 0.9s ease-in-out infinite',
    sad: 'emoSad 5.5s ease-in-out infinite',
    sleepy: 'emoSleepy 4.8s ease-in-out infinite',
    nod: 'emoNod 1.7s ease-in-out infinite',
    shake: 'emoShake 1.4s ease-in-out infinite',
    listening: 'emoListening 2.2s ease-in-out infinite',
    search: 'emoSearch 3.4s ease-in-out infinite',
    celebrate: 'emoCelebrate 2.4s cubic-bezier(.3,.7,.3,1) infinite',
    working: 'agentAvatarMorph 1.0s ease-in-out infinite, agentAvatarBreathe 0.8s ease-in-out infinite',
};

// the square's subtle reaction per emotion (base states keep it still)
const BODY_ANIM: Partial<Record<AvatarMode, string>> = {
    surprised: 'bodySurprised 2.4s cubic-bezier(.34,1.56,.64,1) infinite',
    curious: 'bodyCurious 4.0s ease-in-out infinite',
    confused: 'bodyConfused 2.8s ease-in-out infinite',
    idea: 'bodyIdea 2.8s cubic-bezier(.5,0,.2,1) infinite',
    happy: 'bodyHappy 1.9s cubic-bezier(.3,.7,.3,1) infinite',
    excited: 'bodyExcited 0.9s ease-in-out infinite',
    sad: 'bodySad 5.5s ease-in-out infinite',
    sleepy: 'bodySleepy 4.8s ease-in-out infinite',
    nod: 'bodyNod 1.7s ease-in-out infinite',
    shake: 'bodyShake 1.4s ease-in-out infinite',
    listening: 'bodyListen 2.2s ease-in-out infinite',
    search: 'bodySearch 3.4s ease-in-out infinite',
    celebrate: 'bodyCelebrate 2.4s cubic-bezier(.3,.7,.3,1) infinite',
    working: 'bodyWorking 1.6s ease-in-out infinite',
};

// Activity states: the square (body) carries strong motion (B_ACT); the eye animates with E_ACT.
const B_ACT: Partial<Record<AvatarMode, string>> = {
    learn: 'bLearn 3s ease-in-out infinite',
    success: 'bSuccess 2.4s cubic-bezier(.3,.7,.3,1) infinite',
    error: 'bError 2.2s ease-in-out infinite',
};
const E_ACT: Partial<Record<AvatarMode, string>> = {
    learn: 'eLearn 3s ease-in-out infinite',
    success: 'eSuccess 2.4s ease-in-out infinite',
    error: 'eError 2.2s ease-in-out infinite',
};
const isActivity = (m: AvatarMode) => m === 'learn' || m === 'success' || m === 'error';

// states whose squash/stretch should be grounded at the bottom
const ORIGIN_BOTTOM = new Set<AvatarMode>(['curious', 'idea', 'happy', 'sad', 'sleepy', 'celebrate']);

// `lite` (used by the long-running Whare Wananga training stage): swap the modes that animate
// border-radius (the blob-morph) for transform-only variants. Animating border-radius forces a
// per-frame REPAINT, and QtWebEngine's in-process GPU leaks tiles when an avatar repaints
// continuously for minutes (which the stage does). Compositor-only (transform/opacity) is safe.
const LITE: Partial<Record<AvatarMode, string>> = {
    waiting: 'agentAvatarBreathe 4.0s ease-in-out infinite',
    thinking: 'agentAvatarBreathe 0.7s ease-in-out infinite',
    working: 'agentAvatarBreathe 0.8s ease-in-out infinite',
    talking: 'agentAvatarBreathe 0.5s ease-in-out infinite',
    confused: 'emoConfused 2.8s ease-in-out infinite',   // emoConfused is transform-only; drop the morph half
};

export function AgentAvatar({ mode = 'idle', dim = false, invert = false, lite = false }: { mode?: AvatarMode; dim?: boolean; invert?: boolean; lite?: boolean }) {
    // Settle-to-neutral transition (docs/AgentAvatar.md "Same-position switches"): the agent stays
    // persistent and in one piece. On a mode change we briefly DROP the animation so the body+eye
    // ease back to their rest pose (via `transition: transform`), then start the new mode's
    // animation from rest -- its 0% is neutral, so it eases in instead of snapping. No collapse,
    // no cross-fade, no slideshow. `shown` lags `mode` by the settle duration.
    const [shown, setShown] = React.useState<AvatarMode>(mode);
    const [settling, setSettling] = React.useState(false);
    React.useEffect(() => {
        if (mode === shown) return;
        setSettling(true);                            // 1) drop animation -> ease to neutral
        const t = setTimeout(() => {
            setShown(mode);                           // 2) swap mode (and props) at rest
            setSettling(false);                       // 3) start the new animation from neutral
        }, 200);
        return () => clearTimeout(t);                 // debounce: a new change resets the settle
    }, [mode, shown]);

    const active = shown !== 'idle';
    const act = isActivity(shown);

    const dotColor = invert ? '#111827' : '#ffffff';
    const glow = invert ? '0 0 10px 3px rgba(17,24,39,0.35)' : '0 0 10px 3px rgba(255,255,255,0.35)';
    const ringColor = invert ? 'rgba(17,24,39,0.85)' : 'rgba(255,255,255,0.85)';
    // The app surface is light (no dark mode), so overlay glyphs that sit OVER it — orbs, rings,
    // halo, check, bang, satellite — must be DARK to be visible (like the showcase's var(--ink)
    // on light). The eye stays white on the dark body, so it keeps dotColor/glow.
    const overlay = '#2a3142';
    const overlayGlow = '0 0 4px 1px rgba(30,36,52,0.35)';
    const overlayRing = 'rgba(30,36,52,0.6)';
    const bodyColor = dim ? '#e5e7eb' : invert ? '#f3f4f6' : '#111827';
    // A light square (judge `invert`, or `dim` archive) is invisible on a light background — give
    // it a subtle LIFT (soft drop shadow only, no hard outline) so it stays delineated in light mode.
    const lightBody = dim || invert;

    const bodyAnimation = dim ? 'none' : (act ? (B_ACT[shown] ?? 'none') : (BODY_ANIM[shown] ?? 'none'));
    const eyeAnimation = dim ? 'none'
        : shown === 'idle' ? 'agentAvatarIdleFloat 15s ease-in-out infinite 0.4s, wink 9s ease-in-out infinite'
            : act ? (E_ACT[shown] ?? 'none')
                : lite ? (LITE[shown] ?? ANIM[shown] ?? 'none')   // stage: no continuous border-radius repaint
                    : (ANIM[shown] ?? 'none');
    const eyeSize = dim ? 14 : shown === 'talking' ? 15 : 14;

    return (
        <div className="w-9 h-9 rounded-xl shrink-0" data-agent-avatar style={{ position: 'relative' }}>
            {/* The agent is PERSISTENT — never destroyed, hidden, scaled or faded on a state
                change. Only the running animation (body/eye) and the surrounding props swap, so the
                figure stays in one piece. */}
            <div style={{ position: 'absolute', inset: 0 }}>
                {/* idle aura — soft static halo */}
                {!active && !dim && (
                    <span style={{
                        position: 'absolute', left: '50%', top: '50%', width: 20, height: 20, marginLeft: -10, marginTop: -10,
                        borderRadius: '50%', backgroundColor: dotColor, opacity: 0.13, filter: 'blur(2.5px)',
                    }} />
                )}

                {/* ── overlays (independent of the body's motion) ── */}
                {/* celebrate — energy rings (fire on the landing) */}
                {shown === 'celebrate' && !dim && [0, 1].map((i) => (
                    <span key={`ring${i}`} style={{
                        position: 'absolute', left: '50%', top: '50%', width: 18, height: 18, marginLeft: -9, marginTop: -9,
                        borderRadius: '50%', border: `2px solid ${overlayRing}`, opacity: 0, zIndex: 2,
                        animation: `emoRing 2.4s ease-out ${i * 0.15}s infinite`,
                    }} />
                ))}
                {/* working — orbiting satellite */}
                {shown === 'working' && !dim && (
                    <span style={{
                        position: 'absolute', left: '50%', top: '50%', width: 32, height: 32, marginLeft: -16, marginTop: -16, zIndex: 2,
                        animation: 'emoOrbit 1.6s linear infinite',
                    }}>
                        <span style={{
                            position: 'absolute', top: -3, left: '50%', marginLeft: -3, width: 6, height: 6,
                            borderRadius: '50%', backgroundColor: overlay, boxShadow: overlayGlow,
                        }} />
                    </span>
                )}
                {/* learn — progress halo + knowledge orbs absorbed into the eye */}
                {shown === 'learn' && !dim && (
                    <>
                        <span style={{
                            position: 'absolute', left: '50%', top: '50%', width: 46, height: 46, marginLeft: -23, marginTop: -23,
                            borderRadius: '50%', border: '2px solid transparent', borderTopColor: overlay,
                            opacity: 0.5, zIndex: 2, animation: 'wwSpin 1.1s linear infinite',
                        }} />
                        {([['-22px', '-13px'], ['22px', '-14px'], ['-19px', '15px'], ['23px', '12px']] as const).map(([dx, dy], i) => (
                            <span key={`orb${i}`} style={{
                                position: 'absolute', left: '50%', top: '50%', width: 5, height: 5, marginLeft: -2.5, marginTop: -2.5,
                                borderRadius: '50%', backgroundColor: overlay, boxShadow: overlayGlow, opacity: 0, zIndex: 2,
                                ['--dx' as string]: dx, ['--dy' as string]: dy,
                                animation: `iAbsorb 1.8s ease-in ${i * 0.45}s infinite`,
                            } as React.CSSProperties} />
                        ))}
                    </>
                )}
                {/* success — expanding ring + check mark popping above the head */}
                {shown === 'success' && !dim && (
                    <>
                        <span style={{
                            position: 'absolute', left: '50%', top: '50%', width: 36, height: 36, marginLeft: -18, marginTop: -18,
                            borderRadius: '50%', border: `2px solid ${overlayRing}`, opacity: 0, zIndex: 2,
                            animation: 'ringExpand 2.4s ease-out infinite',
                        }} />
                        <span style={{
                            position: 'absolute', left: '50%', bottom: '100%', width: 16, marginLeft: -8, marginBottom: 2, textAlign: 'center',
                            lineHeight: 1, fontSize: 18, fontWeight: 800, color: overlay, opacity: 0, zIndex: 2,
                            transformOrigin: 'center', animation: 'iCheck 2.4s ease-out infinite',
                        }}>✓</span>
                    </>
                )}
                {/* error — exclamation flash above the head */}
                {shown === 'error' && !dim && (
                    <span style={{
                        position: 'absolute', left: '50%', bottom: '100%', width: 16, marginLeft: -8, marginBottom: 2, textAlign: 'center',
                        lineHeight: 1, fontSize: 18, fontWeight: 800, color: overlay, opacity: 0, zIndex: 2,
                        transformOrigin: 'center', animation: 'iBang 2.2s ease-in-out infinite',
                    }}>!</span>
                )}

                {/* BODY — the square; carries the motion. EYE nested inside so it moves with it.
                    While settling the animation is dropped and `transition: transform` eases both
                    back to their rest pose, so the next animation starts from neutral. */}
                <div style={{
                    position: 'absolute', inset: 0, borderRadius: 12, backgroundColor: bodyColor,
                    boxShadow: lightBody ? '0 1px 4px rgba(0,0,0,0.08)' : 'none',
                    transformOrigin: act ? 'center' : 'center bottom',
                    animation: settling ? 'none' : bodyAnimation,
                    transition: 'transform 0.2s ease',
                }}>
                    <span style={{
                        position: 'absolute', left: '50%', top: '50%', width: eyeSize, height: eyeSize,
                        marginLeft: -(eyeSize / 2), marginTop: -(eyeSize / 2), borderRadius: '50%',
                        backgroundColor: dim ? '#b0b0b0' : dotColor,
                        boxShadow: (active && !dim) ? glow : 'none',
                        transformOrigin: ORIGIN_BOTTOM.has(shown) ? 'center bottom' : 'center',
                        animation: settling ? 'none' : eyeAnimation,
                        transition: 'transform 0.2s ease',
                    }} />
                </div>
            </div>
        </div>
    );
}
