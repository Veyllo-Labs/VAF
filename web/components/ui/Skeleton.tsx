// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';
//
// The house placeholder for a region waiting for its FIRST data: grey bars in the shape and
// height of what is coming, so nothing below moves when it arrives. A site chooses sizes and
// layout only; the motion, the delay, the colour and what a screen reader hears live here and
// in globals.css (.vaf-skel, .vaf-skel-bar), so two placeholders in the product cannot drift
// apart in pace or shade.
//
// - The box takes its space from the first frame and becomes visible after 180 ms: a load that
//   finishes sooner causes neither a jump nor a flash.
// - The bars pulse in opacity only (skelPulse), and hold still under reduced motion.
// - One status line for screen readers (`label`, else common.loading); the shapes are hidden.
// - A refetch with data on screen keeps the data: a site shows this only while it has none.

import type { CSSProperties, ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';

export default function Skeleton({ label, className, style, children }: {
    /** What a screen reader hears; defaults to the shared "Loading…". */
    label?: string;
    className?: string;
    style?: CSSProperties;
    children?: ReactNode;
}) {
    const t = useTranslations('common');
    return (
        <div role="status" className={cn('vaf-skel overflow-hidden', className)} style={style}>
            <span className="sr-only">{label ?? t('loading')}</span>
            {/* Fills the root, so a placeholder with a set height can lay out a full frame. */}
            <div aria-hidden="true" className="h-full">{children}</div>
        </div>
    );
}

/** One grey bar. Height, width and radius come from the site; the shade folds to the dark
 *  theme by itself (bg-gray-200 is a folded utility, docs/web-ui/DARKMODE.md). */
export function SkeletonBar({ className, style }: { className?: string; style?: CSSProperties }) {
    return <div className={cn('vaf-skel-bar rounded bg-gray-200', className)} style={style} />;
}

/** A width: a number is a percentage of the row, a string any CSS length. */
type Width = number | string;
const cssWidth = (w: Width) => (typeof w === 'number' ? `${w}%` : w);

/**
 * `count` rows of fixed height (`rowClassName` sets height, gap and padding). Each row holds an
 * optional leading bar (`lead`, an icon or avatar) and one bar per width of its `widths` entry
 * (inside a box of `textClassName` when given: the pulse animates the bars' own opacity, so a
 * row whose text fades in and out with its container, like the collapsed sidebar, fades the box;
 * an empty `widths` draws the leading bar alone);
 * the entries are cycled, never random, because a placeholder that reshuffles on every render
 * flickers. Each row's pulse starts `stagger` seconds after the one above, so the column reads
 * as one gentle wave instead of a blink.
 */
export function SkeletonRows({ count, widths, rowClassName, barClassName, textClassName, lead, stagger = 0.08 }: {
    count: number;
    widths: (Width | Width[])[];
    rowClassName?: string;
    barClassName?: string;
    textClassName?: string;
    lead?: string;
    stagger?: number;
}) {
    return (
        <>
            {Array.from({ length: count }).map((_, i) => {
                const entry = widths.length ? widths[i % widths.length] : [];
                const delay = { animationDelay: `${(i * stagger).toFixed(2)}s` };
                const bars = (Array.isArray(entry) ? entry : [entry]).map((w, j) => (
                    <SkeletonBar key={j} className={cn('h-2.5', barClassName)}
                        style={{ ...delay, width: cssWidth(w) }} />
                ));
                return (
                    <div key={i} className={cn('flex items-center gap-3', rowClassName)}>
                        {lead && <SkeletonBar className={cn('shrink-0', lead)} style={delay} />}
                        {textClassName ? <div className={cn('flex-1 min-w-0 flex items-center gap-3', textClassName)}>{bars}</div> : bars}
                    </div>
                );
            })}
        </>
    );
}
