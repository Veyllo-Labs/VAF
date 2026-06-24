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
// (docs/web-ui/AgentAvatar.md): the whole figure collapses to a soft neutral point, the mode is swapped,
// then it blooms into the new state — so any state flows into any other instead of snapping.
export type AvatarMode =
    | 'idle' | 'waiting' | 'thinking' | 'talking'
    | 'surprised' | 'curious' | 'confused' | 'idea'
    | 'happy' | 'excited' | 'sad' | 'sleepy'
    | 'nod' | 'shake' | 'listening' | 'search'
    | 'celebrate' | 'working'
    // Activity states (body+eye+icon model) — used for the Whare Wananga learn phase:
    | 'learn' | 'success' | 'error'
    // Activity · Tool & Action (body+eye+compact prop)
    | 'write'
    // Activity · Status & Outcome (icon above the head)
    | 'warning' | 'permission'
    // Activity · Multi-Agent & Learning
    | 'plan'
    // Activity · Lifecycle (used on the 2FA / login screen)
    | 'waking'
    // Activity · Status (a barrier the agent bumps into)
    | 'blocked'
    // Activity · Multi-Agent — the agent hands a token to a sub-agent (used while a sub-agent runs)
    | 'delegate'
    // Activity · Tool scenes (agent + the running tool's prop, the whole showcase scene scaled to 36px)
    | 'searching' | 'executing' | 'browsing' | 'writing' | 'downloading' | 'uploading' | 'remembering';

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
    write: 'bWrite 1.6s ease-in-out infinite',
    success: 'bSuccess 2.4s cubic-bezier(.3,.7,.3,1) infinite',
    error: 'bError 2.2s ease-in-out infinite',
    warning: 'bWarn 2s ease-in-out infinite',
    permission: 'bAsk 2.6s ease-in-out infinite',
    plan: 'bPlan 3s ease-in-out infinite',
    waking: 'bWake 1.4s ease-out',          // plays ONCE then holds at rest; caller then switches to 'waiting'
    blocked: 'bBlocked 2.6s ease-in-out infinite',
    delegate: 'bHandoff 2.6s ease-in-out infinite',
};
const E_ACT: Partial<Record<AvatarMode, string>> = {
    learn: 'eLearn 3s ease-in-out infinite',
    write: 'eWrite 1.8s ease-in-out infinite',
    success: 'eSuccess 2.4s ease-in-out infinite',
    error: 'eError 2.2s ease-in-out infinite',
    warning: 'eWarn 2s ease-in-out infinite',
    permission: 'eAsk 1.6s ease-in-out infinite',
    plan: 'ePlan 3.5s ease-in-out infinite',
    waking: 'eWake 1.4s ease-out',
    blocked: 'eBlocked 2.6s ease-in-out infinite',
    delegate: 'eHandoff 2.6s ease-in-out infinite',
};
const isActivity = (m: AvatarMode) => m === 'learn' || m === 'success' || m === 'error' || m === 'write' || m === 'warning' || m === 'permission' || m === 'plan' || m === 'waking' || m === 'blocked' || m === 'delegate';

// Tool-activity scenes: the whole showcase scene scaled 0.75 (agent stays 36px) + the tool's prop.
// `l`/`t` = wrapper offset (showcase agent position × 0.75, negated) so the agent sits at the avatar's
// left; `w`/`h` size the avatar area so the prop has room (the rest overflows visibly, like delegate).
const TOOL_SCENES: Record<string, { cls: string; l: number; t: number; w: number; h: number }> = {
    searching: { cls: 'search', l: -18, t: -43.5, w: 108, h: 36 },
    executing: { cls: 'execute', l: -22.5, t: -43.5, w: 110, h: 36 },
    browsing: { cls: 'browse', l: -19.5, t: -42, w: 110, h: 36 },
    writing: { cls: 'write', l: -18, t: -43.5, w: 114, h: 36 },
    downloading: { cls: 'download', l: -57, t: -55.5, w: 44, h: 36 },
    uploading: { cls: 'upload', l: -57, t: -55.5, w: 44, h: 36 },
    remembering: { cls: 'remembering', l: -57, t: -58.5, w: 46, h: 36 },
};
const toolProps = (m: AvatarMode): React.ReactNode => {
    switch (m) {
        case 'searching': return (<><span className="pt a dark" /><span className="pt b" /><span className="pt c" /><span className="pt d dark" /><span className="pt e" /><div className="lens"><span className="glint" /></div></>);
        case 'executing': return (<><div className="term"><span className="prompt" /><span className="cur" /></div><div className="spinner" /></>);
        case 'browsing': return (<div className="globe"><i className="glon" /></div>);
        case 'writing': return (<div className="editor"><span className="tline" /><span className="caret" /></div>);
        case 'downloading':
        case 'uploading': return (<><span className="arrow" /><span className="pkt p1" /><span className="pkt p2" /><span className="pkt p3" /></>);
        case 'remembering': return (<><span className="mem m1" /><span className="mem m2" /><span className="mem m3" /><span className="mem m4" /><span className="mem m5" /></>);
        default: return null;
    }
};

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

export function AgentAvatar({ mode = 'idle', dim = false, invert = false, lite = false, tint }: { mode?: AvatarMode; dim?: boolean; invert?: boolean; lite?: boolean; tint?: { body?: string; dot?: string } }) {
    // Settle-to-neutral transition (docs/web-ui/AgentAvatar.md "Same-position switches"): the agent stays
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
    const toolScene = TOOL_SCENES[shown];   // non-null while a tool runs → render the scaled scene instead of the plain body
    // A tool scene (or delegate) extends a prop to the agent's RIGHT. Reserving that as layout WIDTH would
    // shove the whole content column (timeline / bubble) rightward every time a tool runs. Instead keep the
    // avatar's layout footprint at the normal 36px and let the scene lean LEFT into the empty gutter: a
    // negative margin-left pulls the scene (and the agent with it) left, an equal margin-right restores the
    // margin-box so nothing to the RIGHT moves. Margins transition (not per-frame) → leak-safe.
    const sceneWidth = toolScene ? toolScene.w : (shown === 'delegate' ? 88 : 0);
    const leanLeft = sceneWidth ? sceneWidth - 36 : 0;

    const dotColor = invert ? '#111827' : '#ffffff';
    const glow = invert ? '0 0 10px 3px rgba(17,24,39,0.35)' : '0 0 10px 3px rgba(255,255,255,0.35)';
    const ringColor = invert ? 'rgba(17,24,39,0.85)' : 'rgba(255,255,255,0.85)';
    // The app surface is light (no dark mode), so overlay glyphs that sit OVER it — orbs, rings,
    // halo, check, bang, satellite — must be DARK to be visible (like the showcase's var(--ink)
    // on light). The eye stays white on the dark body, so it keeps dotColor/glow.
    const overlay = '#2a3142';
    const overlayGlow = '0 0 4px 1px rgba(30,36,52,0.35)';
    const overlayRing = 'rgba(30,36,52,0.6)';
    const bodyColor = tint?.body ?? (dim ? '#e5e7eb' : invert ? '#f3f4f6' : '#111827');
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
        <div className="w-9 h-9 rounded-xl shrink-0" data-agent-avatar style={{ position: 'relative', width: leanLeft ? 36 : undefined, marginLeft: leanLeft ? -leanLeft : undefined, marginRight: leanLeft ? leanLeft : undefined, transition: 'margin 0.25s ease' }}>
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
                {/* write — a short line types out (scaleX, not width = leak-safe) + a blinking caret.
                    On the dark body, so it uses the white dot colour (not the dark overlay ink). */}
                {shown === 'write' && !dim && (
                    <>
                        <span style={{
                            position: 'absolute', left: 10, bottom: 4, width: 12, height: 2,
                            borderRadius: 1, backgroundColor: dotColor, transformOrigin: 'left center', opacity: 0.92, zIndex: 2,
                            animation: 'iType 2.2s steps(10, end) infinite',
                        }} />
                        <span style={{
                            position: 'absolute', left: 22, bottom: 3, width: 2, height: 6,
                            borderRadius: 0.5, backgroundColor: dotColor, zIndex: 2,
                            animation: 'caretBlink 0.9s step-end infinite',
                        }} />
                    </>
                )}
                {/* warning — a pulsing caution triangle above the head (dark overlay: over the light panel) */}
                {shown === 'warning' && !dim && (
                    <svg viewBox="0 0 26 24" style={{
                        position: 'absolute', left: '50%', bottom: '100%', width: 15, height: 14, marginLeft: -7.5, marginBottom: 2,
                        overflow: 'visible', color: overlay, zIndex: 2, transformOrigin: 'center bottom',
                        animation: 'iPulse 1.4s ease-in-out infinite',
                    }}>
                        <path d="M13 2 L24.5 22 L1.5 22 Z" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinejoin="round" />
                        <rect x="11.8" y="9" width="2.4" height="6" rx="1.2" fill="currentColor" />
                        <circle cx="13" cy="18.6" r="1.3" fill="currentColor" />
                    </svg>
                )}
                {/* permission — a "?" above the head; the body leans/asks (bAsk) */}
                {shown === 'permission' && !dim && (
                    <span style={{
                        position: 'absolute', left: '50%', bottom: '100%', width: 14, marginLeft: -7, marginBottom: 2, textAlign: 'center',
                        lineHeight: 1, fontSize: 17, fontWeight: 800, color: overlay, zIndex: 2, transformOrigin: 'center bottom',
                        animation: 'iPulse 1.4s ease-in-out infinite',
                    }}>?</span>
                )}

                {/* blocked — a striped road-block barrier appears to the RIGHT, SEPARATE from the agent
                    (the agent glides left + bumps toward it; see the stage wrapper below). Static
                    gradient/border + a one-shot transform/opacity intro = leak-safe. The legs sit at the
                    bar's BOTTOM edge (top past the border-box) so they don't poke through the striped panel. */}
                {shown === 'blocked' && !dim && (
                    <span style={{
                        position: 'absolute', right: -12, top: '50%', width: 24, height: 8, marginTop: -9, zIndex: 2,
                        border: `1.6px solid ${overlay}`, borderRadius: 2,
                        background: `repeating-linear-gradient(45deg, ${overlay} 0 3px, transparent 3px 6px)`,
                        animation: 'blkBarIn 0.3s ease both',
                    }}>
                        <span style={{ position: 'absolute', top: 9.6, left: 1.5, width: 1.7, height: 9, borderRadius: '0 0 1px 1px', backgroundColor: overlay, transform: 'rotate(14deg)', transformOrigin: 'top center' }} />
                        <span style={{ position: 'absolute', top: 9.6, right: 1.5, width: 1.7, height: 9, borderRadius: '0 0 1px 1px', backgroundColor: overlay, transform: 'rotate(-14deg)', transformOrigin: 'top center' }} />
                    </span>
                )}

                {/* delegate — the avatar area is WIDER in this mode (root width above): the main agent stays
                    full size on the LEFT, a sub-agent (peer) spawns in on the RIGHT with a real gap, and a
                    token is handed across (arc) main -> sub. transform/opacity + static shadows = leak-safe. */}
                {shown === 'delegate' && !dim && (
                    <>
                        {/* token flows main -> sub in an arc (after the peer has spawned) */}
                        <span style={{
                            position: 'absolute', left: 36, top: '50%', width: 6, height: 6, marginTop: -3, zIndex: 2,
                            borderRadius: '50%', backgroundColor: overlay, boxShadow: overlayGlow, opacity: 0,
                            animation: 'iToken 2.6s ease-in-out infinite 0.6s',
                        }} />
                        {/* sub-agent peer — a smaller second agent; its eye pulses when it receives the token */}
                        <div style={{
                            position: 'absolute', left: 66, top: '50%', width: 18, height: 18, marginTop: -9, zIndex: 3,
                            transformOrigin: 'center', animation: 'iSpawnIn 0.6s ease-out both',
                        }}>
                            <div style={{
                                position: 'absolute', inset: 0, borderRadius: 6, backgroundColor: '#111827',
                                boxShadow: '0 3px 8px rgba(0,0,0,0.45), inset 0 0 0 1px rgba(255,255,255,0.05)',
                            }}>
                                <span style={{
                                    position: 'absolute', left: '50%', top: '50%', width: 8, height: 8, marginLeft: -4, marginTop: -4,
                                    borderRadius: '50%', backgroundColor: '#fff', boxShadow: '0 0 5px 1px rgba(255,255,255,0.4)',
                                    animation: 'iPeerRecv 2.6s ease-in-out infinite',
                                }} />
                            </div>
                        </div>
                    </>
                )}

                {/* tool-activity scene — the agent + the running tool's prop (the whole showcase scene scaled
                    so the agent stays 36px). Rendered INSTEAD of the plain body while a tool runs. */}
                {toolScene && !dim && (
                    <div className={`tsc ${toolScene.cls}`} style={{ left: toolScene.l, top: toolScene.t }}>
                        <div className="ag"><div className="bd" /><div className="ey" /></div>
                        {toolProps(shown)}
                    </div>
                )}

                {/* AGENT STAGE — the persistent figure (body+eye). For `blocked` the agent keeps its FIXED
                    size and only GLIDES left (transform + transition only = leak-safe) to free room for the
                    barrier on the right, like the showcase play: the agent is never shrunk, swapped or faded.
                    For `delegate` the stage is a 36px box on the LEFT (the root is wider); else it fills the root. */}
                {!toolScene && (
                <div style={{
                    position: 'absolute',
                    ...(shown === 'delegate' ? { left: 0, top: 0, width: 36, height: 36 } : { inset: 0 }),
                    transform: shown === 'blocked' ? 'translateX(-15px)' : 'none',
                    transition: 'transform 0.3s ease',
                }}>
                    {/* BODY — the square; carries the motion. EYE nested inside so it moves with it.
                        While settling the animation is dropped and `transition: transform` eases both
                        back to their rest pose, so the next animation starts from neutral. */}
                    <div style={{
                        position: 'absolute', inset: 0, borderRadius: 12, backgroundColor: bodyColor,
                        boxShadow: (shown === 'blocked' || shown === 'delegate')
                            ? '0 5px 14px rgba(0,0,0,0.45), inset 0 0 0 1px rgba(255,255,255,0.05)'
                            : (lightBody ? '0 1px 4px rgba(0,0,0,0.08)' : 'none'),
                        transformOrigin: act ? 'center' : 'center bottom',
                        animation: settling ? 'none' : bodyAnimation,
                        transition: 'transform 0.2s ease',
                    }}>
                        <span style={{
                            position: 'absolute', left: '50%', top: '50%', width: eyeSize, height: eyeSize,
                            marginLeft: -(eyeSize / 2), marginTop: -(eyeSize / 2), borderRadius: '50%',
                            backgroundColor: tint?.dot ?? (dim ? '#b0b0b0' : dotColor),
                            boxShadow: (active && !dim) ? glow : 'none',
                            transformOrigin: ORIGIN_BOTTOM.has(shown) ? 'center bottom' : 'center',
                            animation: settling ? 'none' : eyeAnimation,
                            transition: 'transform 0.2s ease',
                        }} />
                    </div>
                </div>
                )}
            </div>
        </div>
    );
}
