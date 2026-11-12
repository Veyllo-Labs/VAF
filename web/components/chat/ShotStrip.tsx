'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Camera, ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { edgeMask, FADE_PX, type FileChip } from '@/components/chat/workingShots';

// How far from either edge a picture the keys move to stays: past the faded edge and the
// 28px arrow button over it, so the focused picture and its ring are never under either.
// No scroll snapping on the row: a snap point pulled the keyboard's scrollIntoView back
// half a picture, which left the focused one cut at the edge.
const KEEP_CLEAR_PX = FADE_PX + 12;

function prefersReducedMotion(): boolean {
    try {
        return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch {
        return false;
    }
}

const fileUrl = (path: string) => `/api/file?path=${encodeURIComponent(path)}`;

interface Props {
    shots: FileChip[];
    onOpen: (path: string, name: string) => void;
    /** A caption above the row (the turn's strip); the strip inside a step has none. */
    label?: string;
}

/**
 * One row of working screenshots that scrolls sideways (components/chat/workingShots.ts
 * decides which pictures). A click, Enter or Space opens the picture large, through the
 * same call the image attachment uses. One tab stop per row: the arrow keys, Home and End
 * move between the pictures. The vertical wheel is left to the chat; shift+wheel, a
 * trackpad and a finger scroll the row natively.
 */
export function ShotStrip({ shots, onOpen, label }: Props) {
    const t = useTranslations('main');
    const scrollerRef = useRef<HTMLDivElement | null>(null);
    const thumbRefs = useRef<(HTMLButtonElement | null)[]>([]);
    const frameRef = useRef<number | null>(null);
    const [edges, setEdges] = useState({ atStart: true, atEnd: true });
    const [active, setActive] = useState(0);

    const measure = useCallback(() => {
        frameRef.current = null;
        const el = scrollerRef.current;
        if (!el) return;
        const atStart = el.scrollLeft <= 1;
        const atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 1;
        setEdges(prev => (prev.atStart === atStart && prev.atEnd === atEnd ? prev : { atStart, atEnd }));
    }, []);

    // One measurement per frame at most, and only when something moved: no timer, no loop.
    const scheduleMeasure = useCallback(() => {
        if (frameRef.current !== null) return;
        frameRef.current = window.requestAnimationFrame(measure);
    }, [measure]);

    useEffect(() => {
        const el = scrollerRef.current;
        if (!el) return;
        measure();
        let ro: ResizeObserver | null = null;
        if (typeof ResizeObserver !== 'undefined') {
            ro = new ResizeObserver(scheduleMeasure);
            ro.observe(el);
        }
        return () => {
            ro?.disconnect();
            if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
            frameRef.current = null;
        };
    }, [measure, scheduleMeasure, shots.length]);

    const count = shots.length;
    const current = Math.min(active, Math.max(count - 1, 0));

    const focusAt = (index: number) => {
        const next = Math.max(0, Math.min(count - 1, index));
        setActive(next);
        const btn = thumbRefs.current[next];
        if (!btn) return;
        btn.focus({ preventScroll: true });
        btn.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    };

    const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
        let target: number | null = null;
        if (e.key === 'ArrowRight') target = index + 1;
        else if (e.key === 'ArrowLeft') target = index - 1;
        else if (e.key === 'Home') target = 0;
        else if (e.key === 'End') target = count - 1;
        if (target === null) return;
        e.preventDefault();
        focusAt(target);
    };

    const page = (direction: 1 | -1) => {
        const el = scrollerRef.current;
        if (!el) return;
        el.scrollBy({ left: direction * 0.8 * el.clientWidth, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    };

    if (count === 0) return null;
    const mask = edgeMask(edges.atStart, edges.atEnd);
    const arrowClass = 'max-md:hidden absolute top-1/2 -translate-y-1/2 z-10 flex h-7 w-7 items-center justify-center rounded-full border border-gray-200 bg-white/90 text-gray-600 shadow-sm hover:bg-white hover:text-gray-900';

    return (
        <div className="w-full min-w-0" role="group" aria-label={label || t('workShotsLabel', { count })}>
            {label && (
                <div className="mb-1 flex items-center gap-1.5 px-1 text-[11px] text-gray-500">
                    <Camera size={12} className="shrink-0" aria-hidden="true" />
                    <span>{label}</span>
                </div>
            )}
            <div className="relative">
                <div
                    ref={scrollerRef}
                    onScroll={scheduleMeasure}
                    className="flex gap-2 overflow-x-auto overscroll-x-contain py-1 px-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
                    style={{ scrollPaddingInline: KEEP_CLEAR_PX, ...(mask ? { WebkitMaskImage: mask, maskImage: mask } : {}) }}
                >
                    {shots.map((s, i) => (
                        <button
                            key={s.path}
                            ref={el => { thumbRefs.current[i] = el; }}
                            type="button"
                            tabIndex={i === current ? 0 : -1}
                            onClick={() => { setActive(i); onOpen(s.path, s.name); }}
                            onKeyDown={e => onKeyDown(e, i)}
                            onFocus={() => setActive(i)}
                            aria-label={t('workShotOpen', { index: i + 1, count })}
                            className="shrink-0 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                        >
                            {/* Smaller below md: at a phone's width three full pictures filled the
                                row exactly and the fourth did not show at all, so nothing said
                                there was more. Now a part of the next one always shows. */}
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                                src={fileUrl(s.path)}
                                alt=""
                                loading="lazy"
                                decoding="async"
                                width={112}
                                height={70}
                                draggable={false}
                                className="block h-[70px] w-[112px] max-md:h-[60px] max-md:w-[96px] rounded-lg border border-gray-200 bg-gray-50 object-cover object-top"
                            />
                        </button>
                    ))}
                </div>
                {!edges.atStart && (
                    <button type="button" tabIndex={-1} aria-hidden="true" title={t('workShotsEarlier')}
                        onClick={() => page(-1)} className={`${arrowClass} left-1`}>
                        <ChevronLeft size={16} />
                    </button>
                )}
                {!edges.atEnd && (
                    <button type="button" tabIndex={-1} aria-hidden="true" title={t('workShotsLater')}
                        onClick={() => page(1)} className={`${arrowClass} right-1`}>
                        <ChevronRight size={16} />
                    </button>
                )}
            </div>
        </div>
    );
}
