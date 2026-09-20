// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The house's two-position switch: a track, a knob, and nothing else. It started as a local
// component of the Front Office window and moved here when the contact book and the WhatsApp
// window needed the same control, because a second hand-drawn track is how two switches in one
// product end up a pixel and a shade apart.
//
// `tone` exists because the surfaces differ: the settings pages follow the theme (light tokens
// with dark variants), while the channel windows are dark whatever the theme is, so a switch
// wearing its light face inside one would read as "on".

import React from 'react';
import { cn } from '@/lib/utils';

const TRACK = 'relative w-11 h-6 rounded-full transition-colors shrink-0 disabled:opacity-60';
const KNOB = 'absolute top-1 w-4 h-4 rounded-full shadow transition-transform';

/** The class for one of the two words that flank a switch: the one the knob points at
 *  carries the weight, the other steps back. It lives with the control because both windows
 *  that show the pair need the same two greys, and `tone` picks the same palette the track
 *  does. A word on each side rather than one after the switch: a single trailing word reads
 *  as what pressing it would DO, so "No" beside a switch that is already off said the
 *  opposite of the truth. */
export function switchWord(active: boolean, tone: 'auto' | 'dark' = 'auto'): string {
    if (tone === 'dark') return active ? 'font-medium text-[#e8e8e8]' : 'text-[#6a6a6a]';
    return active ? 'font-medium text-gray-900 dark:text-[#e8e8e8]' : 'text-gray-400 dark:text-[#6a6a6a]';
}

export function Switch({ on, disabled, label, onClick, tone = 'auto', className }: {
    on: boolean;
    disabled?: boolean;
    /** Read out instead of the track, which has no text of its own. */
    label: string;
    onClick: () => void;
    /** 'auto' follows the theme; 'dark' is for the always-dark channel windows. */
    tone?: 'auto' | 'dark';
    className?: string;
}) {
    const dark = tone === 'dark';
    return (
        <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label={label}
            disabled={disabled}
            onClick={onClick}
            className={cn(TRACK, className,
                dark
                    ? (on ? 'bg-[#d9d9d9]' : 'bg-[#333333]')
                    : (on ? 'bg-gray-800 dark:bg-[#d9d9d9]' : 'bg-gray-300 dark:bg-[#333333]'),
                disabled && 'opacity-60')}
        >
            <div className={cn(KNOB, on ? 'translate-x-6' : 'translate-x-1',
                dark
                    ? (on ? 'bg-[#1a1a1a]' : 'bg-[#e8e8e8]')
                    : (on ? 'bg-white dark:bg-[#1a1a1a]' : 'bg-white dark:bg-[#e8e8e8]'))} />
        </button>
    );
}

export default Switch;
