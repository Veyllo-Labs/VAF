'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React from 'react';
import { useIsMobile } from '@/hooks/useIsMobile';

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
    | 'warning' | 'permission' | 'exclaim'
    // Activity · Multi-Agent & Learning
    | 'plan'
    // Activity · Lifecycle (used on the 2FA / login screen)
    | 'waking'
    // Activity · Status (a barrier the agent bumps into)
    | 'blocked'
    // Activity · Multi-Agent — the agent hands a token to a sub-agent (used while a sub-agent runs)
    | 'delegate'
    // Activity · Tool scenes (agent + the running tool's prop, the whole showcase scene scaled to 36px)
    | 'searching' | 'executing' | 'browsing' | 'writing' | 'downloading' | 'uploading' | 'remembering'
    // Away scenes — the agent passes the time while the user is away (one per nudge, rotating).
    // GPU-safe (transform/opacity only): nap / coffee / stars / groove. TV + newspaper excluded.
    | 'away_nap' | 'away_coffee' | 'away_stars' | 'away_groove';

// eye (dot) animation per mode (matches agent-character-emotions.html). idle / activity handled
// separately; activity drives the eye via E_ACT.
const ANIM: Partial<Record<AvatarMode, string>> = {
    waiting: 'agentAvatarMorph 5.5s ease-in-out infinite, agentAvatarBreathe 4.0s ease-in-out infinite',
    thinking: 'ponderGaze 4.2s ease-in-out infinite',   // Pondering: eye looks UP and holds (no morph)
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
    thinking: 'ponderBody 4.2s ease-in-out infinite',   // Pondering: the square floats gently while the eye gazes up
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
    exclaim: 'bAsk 2.6s ease-in-out infinite',   // same asking pose as permission, but a "!" glyph
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
    exclaim: 'eAsk 1.6s ease-in-out infinite',
    plan: 'ePlan 3.5s ease-in-out infinite',
    waking: 'eWake 1.4s ease-out',
    blocked: 'eBlocked 2.6s ease-in-out infinite',
    delegate: 'eHandoff 2.6s ease-in-out infinite',
};
const isActivity = (m: AvatarMode) => m === 'learn' || m === 'success' || m === 'error' || m === 'write' || m === 'warning' || m === 'permission' || m === 'exclaim' || m === 'plan' || m === 'waking' || m === 'blocked' || m === 'delegate';

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

// Away scenes: the full 200×160 away-stage (agent-away-scenes.html) scaled 0.75 so the 48px agent
// renders ~36px. `l`/`t` offset the scaled scene so the agent sits at the avatar's LEFT (derived from
// the source agent position × 0.75, negated); `w` lets the props lean RIGHT into the gutter like
// TOOL_SCENES. Scene motion is CSS-class-driven (`.asc.<cls> .bd/.ey/...` in globals.css), so these
// modes need NO ANIM/BODY_ANIM entries (they bypass the persistent figure). All keyframes are
// transform/opacity only → leak-safe.
// `w` here ONLY drives the leftward lean (leanLeft = w − 36) so the scene's RIGHT-leaning props don't
// push the chat content. Away props are small and mostly ABOVE the agent (z's, stars, notes) or a small
// cup, so they barely reach right — keep `w` ≈ the prop's real rightmost extent, NOT the full stage width,
// or the agent drifts way too far left (the props aren't that big).
const AWAY_SCENES: Record<string, { cls: string; l: number; t: number; w: number; h: number }> = {
    away_nap: { cls: 'nap', l: -57, t: -52.5, w: 48, h: 36 },
    away_coffee: { cls: 'coffee', l: -40.5, t: -43.5, w: 54, h: 36 },
    away_stars: { cls: 'stars', l: -58.5, t: -63, w: 56, h: 54 },
    away_groove: { cls: 'music', l: -57, t: -48, w: 56, h: 36 },
};
// Scene modes (tool + away) render a SEPARATE scaled DOM (.tsc/.asc) instead of the persistent body, so
// a swap to/from one can't morph in place — without help it would POP. We cross-dissolve those transitions.
const isSceneMode = (m: AvatarMode): boolean => !!TOOL_SCENES[m] || !!AWAY_SCENES[m];
const awayProps = (m: AvatarMode): React.ReactNode => {
    switch (m) {
        case 'away_nap': return (<><div className="pillow" /><span className="z z1">z</span><span className="z z2">z</span><span className="z z3">z</span></>);
        case 'away_coffee': return (<><div className="cup"><span className="handle" /></div><span className="steam s1" /><span className="steam s2" /><span className="steam s3" /></>);
        case 'away_stars': return (<><span className="star st1" /><span className="star st2" /><span className="star st3" /><span className="star st4" /><span className="star st5" /><span className="star st6" /><span className="shoot" /></>);
        case 'away_groove': return (<><span className="note n1">&#9834;</span><span className="note n2">&#9835;</span><span className="note n3">&#9833;</span></>);
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
    thinking: 'ponderGaze 4.2s ease-in-out infinite',   // gaze-up is transform-only → already leak-safe for the stage
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
    const isMobile = useIsMobile();  // mobile: reserve the scene width as the avatar's footprint instead of the desktop leftward lean
    React.useEffect(() => {
        if (mode === shown) return;
        setSettling(true);                            // 1) drop animation -> ease to neutral
        const t = setTimeout(() => {
            setShown(mode);                           // 2) swap mode (and props) at rest
            setSettling(false);                       // 3) start the new animation from neutral
        }, 200);
        return () => clearTimeout(t);                 // debounce: a new change resets the settle
    }, [mode, shown]);

    // The "living" Working satellite: while Working is shown, a rAF loop eases its angular speed toward a
    // new random target every ~0.6–2s and occasionally (~40%) flips direction, so it speeds up / slows
    // down / reverses organically (the second satellite stays a calm counter-clockwise anchor). Pure
    // transform:rotate per frame = compositor-only → leak-safe. The loop runs ONLY in Working.
    const livingOrbitRef = React.useRef<HTMLSpanElement | null>(null);
    React.useEffect(() => {
        if (shown !== 'working' || dim) return;
        const el = livingOrbitRef.current;
        if (!el) return;
        const rng = (a: number, b: number) => a + Math.random() * (b - a);
        let angle = rng(0, 360);
        let speed = (Math.random() < 0.5 ? -1 : 1) * rng(110, 470);   // deg/s; sign = direction
        let target = speed;
        let nextRoll = 0;
        let last = performance.now();
        let raf = 0;
        const reroll = (now: number) => {
            let s = Math.sign(target) || 1;
            if (Math.random() < 0.4) s = -s;                          // ~40% chance to reverse
            target = s * rng(110, 470);
            nextRoll = now + rng(600, 2000);                          // re-target every 0.6–2.0s
        };
        const frame = (now: number) => {
            const dt = Math.min(0.05, (now - last) / 1000); last = now;
            if (now >= nextRoll) reroll(now);
            speed += (target - speed) * Math.min(1, dt * 3);          // ease toward target (no snapping)
            angle += speed * dt;
            el.style.transform = `rotate(${angle}deg)`;
            raf = requestAnimationFrame(frame);
        };
        reroll(performance.now());
        raf = requestAnimationFrame(frame);
        return () => cancelAnimationFrame(raf);
    }, [shown, dim]);

    // Pondering glyph stream: while the agent is thinking, shapes/numbers/letters pop in above the head and
    // float away (like racing thoughts). Short-lived nodes with transform/opacity animations (+ static
    // shapes) = leak-safe. Skipped in `lite` (the long-running stage) to avoid minutes of DOM churn.
    const thinkStreamRef = React.useRef<HTMLDivElement | null>(null);
    React.useEffect(() => {
        if (shown !== 'thinking' || dim || lite) return;
        const stream = thinkStreamRef.current;
        if (!stream) return;
        const rnd = (a: number, b: number) => a + Math.random() * (b - a);
        const pick = <T,>(a: T[]): T => a[Math.floor(Math.random() * a.length)];
        const DIGITS = '0123456789'.split('');
        const LETTERS = 'ABCDEFGHJKLMNPQRTXYZ'.split('');
        const SHAPES = ['circle', 'ring', 'square', 'diamond', 'tri'];
        const timer = setInterval(() => {
            const kind = pick(['shape', 'num', 'letter']);
            const el = document.createElement('span');
            el.className = 'tgly';
            const dur = rnd(0.95, 1.6);
            el.style.setProperty('--x', rnd(-9, 9).toFixed(1) + 'px');
            el.style.setProperty('--r', (rnd(-45, 45) | 0) + 'deg');
            el.style.animation = `ponderPop ${dur.toFixed(2)}s cubic-bezier(.35,.7,.3,1) forwards`;
            if (kind === 'shape') {
                const s = pick(SHAPES); const sz = rnd(6, 9);
                el.classList.add(s);
                if (s !== 'tri') { el.style.width = sz.toFixed(1) + 'px'; el.style.height = sz.toFixed(1) + 'px'; }
                if (s === 'diamond') el.style.setProperty('--r', '45deg');
            } else {
                el.textContent = kind === 'num' ? pick(DIGITS) : pick(LETTERS);
                el.style.fontSize = rnd(8, 11).toFixed(1) + 'px';
            }
            stream.appendChild(el);
            setTimeout(() => el.remove(), dur * 1000 + 80);
        }, 200);
        return () => { clearInterval(timer); stream.innerHTML = ''; };
    }, [shown, dim, lite]);

    const active = shown !== 'idle';
    const act = isActivity(shown);
    // DESKTOP only: the wide tool/away scenes (web_search magnifier, browse globe, away stages …) extend far
    // to the right. On the narrow mobile column they would push/clip the whole content row, so we suppress
    // them there and fall back to the plain 36px avatar dot (see the !toolScene body branch below).
    const toolScene = isMobile ? undefined : TOOL_SCENES[shown];
    const awayScene = isMobile ? undefined : AWAY_SCENES[shown];
    // A tool scene (or delegate) extends a prop to the agent's RIGHT. Reserving that as layout WIDTH would
    // shove the whole content column (timeline / bubble) rightward every time a tool runs. Instead keep the
    // avatar's layout footprint at the normal 36px and let the scene lean LEFT into the empty gutter: a
    // negative margin-left pulls the scene (and the agent with it) left, an equal margin-right restores the
    // margin-box so nothing to the RIGHT moves. Margins transition (not per-frame) → leak-safe.
    // Only when the scene is actually drawn: a DIMMED bubble (non-latest) renders just the plain 36px figure,
    // so it must NOT lean — otherwise the dim dot gets shoved ~74-94px left, detached from its bubble.
    const sceneWidth = dim ? 0 : (toolScene ? toolScene.w : awayScene ? awayScene.w : (!isMobile && shown === 'delegate' ? 88 : 0));
    const leanLeft = sceneWidth ? sceneWidth - 36 : 0;
    // Cross-dissolve transitions that involve a scene mode (e.g. thinking → web search): during the settle
    // window the whole content fades OUT, the DOM swaps while invisible, then the new scene/body fades IN —
    // so the separate .tsc/.asc DOM no longer pops. Normal↔normal keeps its in-place morph (no fade here).
    const sceneFade = settling && (isSceneMode(shown) || isSceneMode(mode));

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
            <div style={{ position: 'absolute', inset: 0, opacity: sceneFade ? 0 : 1, transition: 'opacity 0.18s ease' }}>
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
                {/* working — TWO orbiting satellites. Both are WHITE (like the showcase original, not the
                    dark overlay): the orbit radius (16px) is smaller than the body half-height (18px), so a
                    satellite rides the dark body's edge and stays visible on the light surface. The TOP one
                    is "living" (rAF-driven: random direction + tempo, see the effect above); the BOTTOM one
                    (180° offset) is a calm fixed counter-clockwise anchor. */}
                {shown === 'working' && !dim && (
                    <>
                        <span ref={livingOrbitRef} style={{
                            position: 'absolute', left: '50%', top: '50%', width: 32, height: 32, marginLeft: -16, marginTop: -16, zIndex: 2,
                        }}>
                            <span style={{
                                position: 'absolute', top: -3, left: '50%', marginLeft: -3, width: 6, height: 6,
                                borderRadius: '50%', backgroundColor: '#fff', boxShadow: '0 0 6px 2px rgba(255,255,255,0.5)',
                            }} />
                        </span>
                        <span style={{
                            position: 'absolute', left: '50%', top: '50%', width: 32, height: 32, marginLeft: -16, marginTop: -16, zIndex: 2,
                            animation: 'emoOrbit 2.4s linear infinite reverse',
                        }}>
                            <span style={{
                                position: 'absolute', bottom: -3, left: '50%', marginLeft: -3, width: 6, height: 6,
                                borderRadius: '50%', backgroundColor: '#fff', boxShadow: '0 0 6px 2px rgba(255,255,255,0.5)',
                            }} />
                        </span>
                    </>
                )}
                {/* thinking (Pondering) — the glyph stream rises above the head (eye gazes up via ponderGaze).
                    Glyphs are spawned into this container by the effect above; static container, leak-safe. */}
                {shown === 'thinking' && !dim && !lite && (
                    <div ref={thinkStreamRef} className="tgly-stream" />
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
                {/* permission — a "?" above the head; exclaim — the SAME asking pose with a "!" instead.
                    Both lean/ask via bAsk + iPulse the glyph. */}
                {(shown === 'permission' || shown === 'exclaim') && !dim && (
                    <span style={{
                        position: 'absolute', left: '50%', bottom: '100%', width: 14, marginLeft: -7, marginBottom: 2, textAlign: 'center',
                        lineHeight: 1, fontSize: 17, fontWeight: 800, color: overlay, zIndex: 2, transformOrigin: 'center bottom',
                        animation: 'iPulse 1.4s ease-in-out infinite',
                    }}>{shown === 'exclaim' ? '!' : '?'}</span>
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
                {shown === 'delegate' && !dim && !isMobile && (
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

                {/* away scene — the agent passes the time (nap / coffee / stars / groove) while the user
                    is away. The full away-stage scaled so the agent stays ~36px; props lean right.
                    Rendered INSTEAD of the plain body, exactly like a tool scene. */}
                {awayScene && !dim && (
                    <div className={`asc ${awayScene.cls}`} style={{ left: awayScene.l, top: awayScene.t }}>
                        {/* eye is NESTED in the body (like the showcase .agent>.body>.eye) so it tracks the
                            body's motion. The coffee `bodySip` lurches the body ~22px toward the cup; with a
                            flat .bd+.ey the eye stayed put and detached (broken). Nesting keeps them together. */}
                        <div className="ag"><div className="bd"><div className="ey" /></div></div>
                        {awayProps(shown)}
                    </div>
                )}

                {/* AGENT STAGE — the persistent figure (body+eye). For `blocked` the agent keeps its FIXED
                    size and only GLIDES left (transform + transition only = leak-safe) to free room for the
                    barrier on the right, like the showcase play: the agent is never shrunk, swapped or faded.
                    For `delegate` the stage is a 36px box on the LEFT (the root is wider); else it fills the root. */}
                {!toolScene && (!awayScene || dim) && (
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
