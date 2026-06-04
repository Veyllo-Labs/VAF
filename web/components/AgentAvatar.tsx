'use client';

import React from 'react';
import { cn } from '@/lib/utils';

// ── Agent Avatar: morphing white dot — idle / waiting / thinking / talking ──
// The agent's visual identity is a living white dot (never an icon). Keyframes
// (agentAvatarMorph / Breathe / Talk / IdleFloat) live in globals.css so they are available
// on first paint. Shared by the chat header and the Whare Wananga training stage so the dot
// language stays in one place.
export type AvatarMode = 'idle' | 'waiting' | 'thinking' | 'talking';

export function AgentAvatar({ mode = 'idle', dim = false, invert = false }: { mode?: AvatarMode; dim?: boolean; invert?: boolean }) {
    // Keyframes live in globals.css — no runtime injection needed
    const active = mode !== 'idle';
    // `invert` = the judge's negative of the agent: what is light on the agent becomes dark
    // here and vice-versa (dark dot on a light container, dark glow). Same animation language.
    const dotColor = invert ? '#111827' : '#ffffff';
    const glow = invert ? '0 0 10px 3px rgba(17,24,39,0.35)' : '0 0 10px 3px rgba(255,255,255,0.35)';
    const containerClass = dim ? 'bg-gray-200' : invert ? 'bg-gray-100' : 'bg-gray-900';
    const animation = mode === 'talking'
        ? 'agentAvatarTalk 0.75s ease-in-out infinite'
        : mode === 'thinking'
            ? 'agentAvatarMorph 1.0s ease-in-out infinite, agentAvatarBreathe 0.7s ease-in-out infinite'
            : mode === 'waiting'
                ? 'agentAvatarMorph 5.5s ease-in-out infinite, agentAvatarBreathe 4.0s ease-in-out infinite'
                : 'none';
    const size = mode === 'talking' ? 15 : 14;

    const SPRING = 'cubic-bezier(0.34, 1.56, 0.64, 1)';
    const EASE_IN = 'cubic-bezier(0.4, 0, 1, 1)';

    // Shy repulsion: dot drifts away from cursor when hovered (non-dim only)
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
        const strength = 7;
        setRepulse({
            x: -(dx / dist) * Math.min(strength, dist * 0.85),
            y: -(dy / dist) * Math.min(strength, dist * 0.85),
        });
    };
    const handleMouseEnter = () => { if (!dim) setIsHovering(true); };
    const handleMouseLeave = () => { setIsHovering(false); setRepulse({ x: 0, y: 0 }); };

    return (
        <div className={cn("w-9 h-9 rounded-xl flex items-center justify-center shrink-0", containerClass)}
             data-agent-avatar
             style={{ position: 'relative', overflow: 'hidden' }}
             onMouseMove={handleMouseMove}
             onMouseEnter={handleMouseEnter}
             onMouseLeave={handleMouseLeave}>
            {/* Repulsion wrapper — all dots translate together away from cursor.
                Using a zero-size anchor at center so children can use negative margins for centering.
                This layer's transform is separate from any child animation transforms. */}
            <span style={{
                position: 'absolute',
                width: 0, height: 0,
                top: '50%', left: '50%',
                transform: `translate(${repulse.x}px, ${repulse.y}px)`,
                transition: isHovering
                    ? 'transform 0.1s ease-out'
                    : `transform 0.6s ${SPRING}`,
                pointerEvents: 'none',
            }}>
                {/* Background aura blob — soft static halo behind idle dot.
                    NOTE: the `animation` was removed. A continuously-morphing element with
                    filter:blur() forces a fresh GPU blur texture EVERY frame; under
                    QtWebEngine's in-process GPU those textures piled up and leaked the
                    renderer (~40 MB/s). Static blur is rasterized once and cached. */}
                {!active && !dim && (
                    <span style={{
                        position: 'absolute',
                        display: 'block',
                        width: 20, height: 20,
                        top: -10, left: -10,
                        borderRadius: '50%',
                        backgroundColor: dotColor,
                        opacity: 0.13,
                        filter: 'blur(2.5px)',
                    }} />
                )}
                {/* idle dot — white on dark bg, gray on light bg — fades out when active.
                    Animation drives transform; repulsion is on the parent wrapper. */}
                <span style={{
                    position: 'absolute',
                    display: 'block',
                    width: 14, height: 14,
                    top: -7, left: -7,
                    borderRadius: '50%',
                    backgroundColor: dim ? '#b0b0b0' : dotColor,
                    opacity: active ? 0 : 1,
                    transform: active ? 'scale(0.5)' : undefined,
                    transition: active
                        ? `opacity 0.25s ${EASE_IN}`
                        : `opacity 0.35s ease`,
                    animation: (!active && !dim) ? 'agentAvatarIdleFloat 15s ease-in-out infinite 0.4s' : 'none',
                }} />
                {/* active dot: outer handles spring/fade, inner runs morph animation.
                    Splitting layers avoids inline-transform vs keyframe-transform conflict. */}
                <span style={{
                    position: 'absolute',
                    display: 'block',
                    width: size, height: size,
                    top: -(size / 2), left: -(size / 2),
                    opacity: active ? 1 : 0,
                    transform: active ? 'scale(1)' : 'scale(0.3)',
                    transition: active
                        ? `opacity 0.32s ease, transform 0.38s ${SPRING}`
                        : `opacity 0.55s ease, transform 0.55s ease`,
                }}>
                    <span style={{
                        display: 'block',
                        width: '100%', height: '100%',
                        borderRadius: '50%',
                        backgroundColor: dotColor,
                        boxShadow: glow,
                        animation,
                    }} />
                </span>
            </span>
        </div>
    );
}
