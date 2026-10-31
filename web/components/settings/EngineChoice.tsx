// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';
//
// The local engine as a choice of two cards: llama.cpp or vLLM, each with its name and one
// line, and under an option the line about this machine (the verdict). One component for Settings
// > AI & Model and the first-run wizard, so the two never word or draw the choice differently.
//
// - A radiogroup of two radios (aria-checked), keyboard arrows move and choose like a native
//   radio group, and only the checked card is in the tab order.
// - A disabled option stays readable and keeps its reason visible (aria-describedby points at
//   it): a greyed card without a reason is a dead end. Only the name and line are dimmed.
// - The cards are the wizard's theme cards (login/page.tsx) in a smaller size, so the dark
//   theme folds them the same way (docs/web-ui/DARKMODE.md).
// - Two columns on a desktop, one on a phone (docs/web-ui/MOBILE_UI.md).

import React, { useRef } from 'react';
import { Check } from 'lucide-react';
import { cn } from '@/lib/utils';

export type EngineId = 'llama' | 'vllm';

export interface EngineOption {
    id: EngineId;
    name: string;
    line: string;
    disabled?: boolean;
    /** The line under the option: this machine's verdict, or why the option is disabled. */
    note?: string;
    /** `amber` for a verdict that keeps the option from running here; muted otherwise. */
    noteTone?: 'muted' | 'amber';
    /** A short word before an amber note ("Not yet", "Not available"). */
    noteWord?: string;
}

export default function EngineChoice({ label, value, options, onChange, idPrefix = 'engine' }: {
    label: string;
    value: EngineId;
    options: EngineOption[];
    onChange: (engine: EngineId) => void;
    idPrefix?: string;
}) {
    const refs = useRef<Record<string, HTMLButtonElement | null>>({});
    const enabled = options.filter((o) => !o.disabled);

    const move = (from: EngineId, step: number) => {
        if (!enabled.length) return;
        const at = enabled.findIndex((o) => o.id === from);
        const next = enabled[(at + step + enabled.length) % enabled.length];
        refs.current[next.id]?.focus();
        if (next.id !== value) onChange(next.id);
    };

    return (
        <div role="radiogroup" aria-label={label} className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
            {options.map((opt) => {
                const checked = opt.id === value;
                const noteId = opt.note ? `${idPrefix}-${opt.id}-note` : undefined;
                const amber = opt.noteTone === 'amber';
                return (
                    <button
                        key={opt.id}
                        ref={(el) => { refs.current[opt.id] = el; }}
                        type="button"
                        role="radio"
                        aria-checked={checked}
                        aria-disabled={opt.disabled || undefined}
                        aria-describedby={noteId}
                        data-engine={opt.id}
                        tabIndex={checked || (!enabled.some((o) => o.id === value) && opt === enabled[0]) ? 0 : -1}
                        onClick={() => { if (!opt.disabled && !checked) onChange(opt.id); }}
                        onKeyDown={(e) => {
                            if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); move(opt.id, 1); }
                            else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); move(opt.id, -1); }
                        }}
                        className={cn(
                            'relative flex flex-col items-start gap-1 px-4 py-3 rounded-xl border-2 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400',
                            checked
                                ? 'border-gray-900 bg-gray-50 dark:border-[#e6e6e6]'
                                : 'border-gray-200',
                            !checked && !opt.disabled && 'hover:bg-gray-50',
                            opt.disabled && 'cursor-not-allowed',
                        )}
                    >
                        {checked && <Check className="w-4 h-4 text-gray-900 absolute top-2.5 right-2.5" aria-hidden="true" />}
                        <span className={cn('text-sm font-semibold text-gray-900 pr-6', opt.disabled && 'opacity-50')}>{opt.name}</span>
                        <span className={cn('text-xs text-gray-600 leading-snug', opt.disabled && 'opacity-50')}>{opt.line}</span>
                        {opt.note && (
                            <span id={noteId} className={cn('text-xs leading-snug mt-1', amber ? 'text-amber-700' : 'text-gray-500')}>
                                {amber && opt.noteWord && <span className="font-medium">{opt.noteWord}: </span>}
                                {opt.note}
                            </span>
                        )}
                    </button>
                );
            })}
        </div>
    );
}
