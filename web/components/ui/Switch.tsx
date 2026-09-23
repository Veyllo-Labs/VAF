// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The house's two-position switch: a track, a knob, and nothing else. It started as a local
// component of the Front Office window and moved here when the contact book and the WhatsApp
// window needed the same control, because a second hand-drawn track is how two switches in one
// product end up a pixel and a shade apart.
//
// It carries one pair of faces, not two. It used to take a `tone`, because the channel windows
// were dark whatever the theme was and a switch wearing its light face inside one would have
// read as "on". Those windows follow the theme now, so the second face had no caller left.

import React from 'react';
import { cn } from '@/lib/utils';

const TRACK = 'relative w-11 h-6 rounded-full transition-colors shrink-0 disabled:opacity-60';
const KNOB = 'absolute top-1 w-4 h-4 rounded-full shadow transition-transform';

/** The class for one of the two words that flank a switch: the one the knob points at
 *  carries the weight, the other steps back. It lives with the control because both windows
 *  that show the pair need the same two greys. A word on each side rather than one after the
 *  switch: a single trailing word reads as what pressing it would DO, so "No" beside a switch
 *  that is already off said the opposite of the truth. */
export function switchWord(active: boolean): string {
    return active ? 'font-medium text-gray-900 dark:text-[#e8e8e8]' : 'text-gray-400 dark:text-[#6a6a6a]';
}

export function Switch({ on, disabled, label, onClick, className }: {
    on: boolean;
    disabled?: boolean;
    /** Read out instead of the track, which has no text of its own. */
    label: string;
    onClick: () => void;
    className?: string;
}) {
    return (
        <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label={label}
            disabled={disabled}
            onClick={onClick}
            className={cn(TRACK, className,
                on ? 'bg-gray-800 dark:bg-[#d9d9d9]' : 'bg-gray-300 dark:bg-[#333333]',
                disabled && 'opacity-60')}
        >
            <div className={cn(KNOB, on ? 'translate-x-6' : 'translate-x-1',
                on ? 'bg-white dark:bg-[#1a1a1a]' : 'bg-white dark:bg-[#e8e8e8]')} />
        </button>
    );
}

export default Switch;
