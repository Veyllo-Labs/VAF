'use client';

import React from 'react';
import { cn } from '@/lib/utils';

// ── Agent Avatar: the living dot, now with character emotions ──
// Keyframes (agentAvatar* + emo*) live in globals.css so they are available on first paint.
// Shared by the chat header (basic modes) and the Whare Wananga training stage (full emotion
// range, plus `invert` for the judge: dark dot on a light container). The dot language stays in
// one place.
export type AvatarMode =
    | 'idle' | 'waiting' | 'thinking' | 'talking'
    | 'surprised' | 'curious' | 'confused' | 'idea'
    | 'happy' | 'excited' | 'sad' | 'sleepy'
    | 'nod' | 'shake' | 'listening' | 'search'
    | 'celebrate' | 'working';

const SPRING = 'cubic-bezier(0.34, 1.56, 0.64, 1)';
const EASE_IN = 'cubic-bezier(0.4, 0, 1, 1)';

// mode -> dot animation (matches agent-character-emotions.html exactly)
const ANIM: Record<AvatarMode, string> = {
    idle: 'none',
    waiting: 'agentAvatarMorph 5.5s ease-in-out infinite, agentAvatarBreathe 4.0s ease-in-out infinite',
    thinking: 'agentAvatarMorph 1.0s ease-in-out infinite, agentAvatarBreathe 0.7s ease-in-out infinite',
    talking: 'agentAvatarTalk 0.75s ease-in-out infinite',
    surprised: 'emoSurprised 2.4s cubic-bezier(.34,1.56,.64,1) infinite',
    curious: 'emoCurious 4.0s ease-in-out infinite',
    confused: 'emoConfused 2.8s ease-in-out infinite, agentAvatarMorph 2.2s ease-in-out infinite',
    idea: 'emoIdea 2.8s cubic-bezier(.5,0,.2,1) infinite',
    happy: 'emoHappy 1.9s cubic-bezier(.3,.7,.3,1) infinite',
    excited: 'emoExcited 0.9s ease-in-out infinite',
    sad: 'emoSad 4.0s ease-in-out infinite',
    sleepy: 'emoSleepy 4.8s ease-in-out infinite',
    nod: 'emoNod 1.7s ease-in-out infinite',
    shake: 'emoShake 1.4s ease-in-out infinite',
    listening: 'emoListening 2.2s ease-in-out infinite',
    search: 'emoSearch 3.4s ease-in-out infinite',
    celebrate: 'emoCelebrate 2.4s cubic-bezier(.3,.7,.3,1) infinite',
    working: 'agentAvatarMorph 1.0s ease-in-out infinite, agentAvatarBreathe 0.8s ease-in-out infinite',
};

// states whose squash/stretch should be grounded at the bottom
const ORIGIN_BOTTOM = new Set<AvatarMode>(['curious', 'idea', 'happy', 'sad', 'sleepy', 'celebrate']);

export function AgentAvatar({ mode = 'idle', dim = false, invert = false }: { mode?: AvatarMode; dim?: boolean; invert?: boolean }) {
    const active = mode !== 'idle';
    const animation = ANIM[mode] ?? 'none';
    const size = mode === 'talking' ? 15 : 14;

    // `invert` = the judge's negative of the agent: dark dot on a light container, dark glow.
    const dotColor = invert ? '#111827' : '#ffffff';
    const glow = invert ? '0 0 10px 3px rgba(17,24,39,0.35)' : '0 0 10px 3px rgba(255,255,255,0.35)';
    const ringColor = invert ? 'rgba(17,24,39,0.85)' : 'rgba(255,255,255,0.85)';
    const containerClass = dim ? 'bg-gray-200' : invert ? 'bg-gray-100' : 'bg-gray-900';

    // Shy repulsion (chat flourish): the dot drifts away from the cursor.
    const [repulse, setRepulse] = React.useState({ x: 0, y: 0 });
    const [isHovering, setIsHovering] = React.useState(false);
    const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
        if (dim) return;
        const rect = e.currentTarget.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const dx = e.clientX - cx;
        const dy = e.clientY - cy;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        setRepulse({ x: -(dx / dist) * Math.min(7, dist * 0.85), y: -(dy / dist) * Math.min(7, dist * 0.85) });
    };
    const handleMouseEnter = () => { if (!dim) setIsHovering(true); };
    const handleMouseLeave = () => { setIsHovering(false); setRepulse({ x: 0, y: 0 }); };

    return (
        <div className={cn('w-9 h-9 rounded-xl flex items-center justify-center shrink-0', containerClass)}
             data-agent-avatar
             style={{ position: 'relative', overflow: 'hidden' }}
             onMouseMove={handleMouseMove}
             onMouseEnter={handleMouseEnter}
             onMouseLeave={handleMouseLeave}>
            {/* Repulsion wrapper — zero-size anchor at centre; children centre via negative offsets. */}
            <span style={{
                position: 'absolute', width: 0, height: 0, top: '50%', left: '50%',
                transform: `translate(${repulse.x}px, ${repulse.y}px)`,
                transition: isHovering ? 'transform 0.1s ease-out' : `transform 0.6s ${SPRING}`,
                pointerEvents: 'none',
            }}>
                {/* idle aura — soft static halo behind the idle dot */}
                {!active && !dim && (
                    <span style={{
                        position: 'absolute', display: 'block', width: 20, height: 20, top: -10, left: -10,
                        borderRadius: '50%', backgroundColor: dotColor, opacity: 0.13, filter: 'blur(2.5px)',
                    }} />
                )}
                {/* celebrate — expanding success rings */}
                {mode === 'celebrate' && !dim && [0, 1].map((i) => (
                    <span key={`ring${i}`} style={{
                        position: 'absolute', display: 'block', width: 18, height: 18, top: -9, left: -9,
                        borderRadius: '50%', border: `2px solid ${ringColor}`, opacity: 0,
                        animation: `emoRing 2.4s ease-out ${i * 0.28}s infinite`,
                    }} />
                ))}
                {/* working — orbiting satellite (background work) */}
                {mode === 'working' && !dim && (
                    <span style={{
                        position: 'absolute', display: 'block', width: 32, height: 32, top: -16, left: -16,
                        animation: 'emoOrbit 1.6s linear infinite',
                    }}>
                        <span style={{
                            position: 'absolute', top: -3, left: '50%', marginLeft: -3, width: 6, height: 6,
                            borderRadius: '50%', backgroundColor: dotColor, boxShadow: glow,
                        }} />
                    </span>
                )}
                {/* idle dot — fades out when active */}
                <span style={{
                    position: 'absolute', display: 'block', width: 14, height: 14, top: -7, left: -7,
                    borderRadius: '50%', backgroundColor: dim ? '#b0b0b0' : dotColor,
                    opacity: active ? 0 : 1, transform: active ? 'scale(0.5)' : undefined,
                    transition: active ? `opacity 0.25s ${EASE_IN}` : 'opacity 0.35s ease',
                    animation: (!active && !dim) ? 'agentAvatarIdleFloat 15s ease-in-out infinite 0.4s' : 'none',
                }} />
                {/* active dot — outer handles spring/fade, inner runs the (emotion) animation */}
                <span style={{
                    position: 'absolute', display: 'block', width: size, height: size, top: -(size / 2), left: -(size / 2),
                    opacity: active ? 1 : 0, transform: active ? 'scale(1)' : 'scale(0.3)',
                    transition: active ? `opacity 0.32s ease, transform 0.38s ${SPRING}` : 'opacity 0.55s ease, transform 0.55s ease',
                }}>
                    <span style={{
                        display: 'block', width: '100%', height: '100%', borderRadius: '50%',
                        backgroundColor: dotColor, boxShadow: glow,
                        transformOrigin: ORIGIN_BOTTOM.has(mode) ? 'center bottom' : 'center',
                        animation: dim ? 'none' : animation,
                    }} />
                </span>
            </span>
        </div>
    );
}
