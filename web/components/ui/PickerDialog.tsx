'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Centered, searchable single-select dialog - the shared picker primitive.
 *
 * Why a primitive: the language control and the timezone control both need a
 * list that stays usable as it grows, and the closest existing components do
 * not fit - ConfirmDialog is a yes/no shell without a list, and
 * UserVisibilityPicker is a multi-select DRAFT editor (Cancel discards) whose
 * contract would break under instant single-select. This dialog reuses the
 * ConfirmDialog shell conventions (backdrop, panel fold, escape layer,
 * z-ladder) around a search field and an option list: picking applies
 * immediately and closes.
 *
 * Search folds case and strips diacritics ("turkce" finds "Türkçe") and also
 * matches the item's value, so IANA zone ids hit on their city name. Arrow
 * keys move, Enter picks, Escape closes via the shared escape registry.
 *
 * Colors use the light Tailwind palette; the .dark variable fold in
 * globals.css derives the dark theme. The panel carries the same explicit
 * dark overrides as ConfirmDialog, because a dialog sits on the page tone
 * (#181818), not on the elevated card tone the fold gives bg-white.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';

export interface PickerItem {
    value: string;
    /** Primary label (endonym, zone id, ...). */
    label: string;
    /** Secondary gray label (English exonym, ...). */
    sublabel?: string;
    /** Leading emoji (a flag). A string, so rows stay text-aligned. */
    emoji?: string;
}

export interface PickerDialogProps {
    open: boolean;
    /** Accessible name; also the search placeholder unless one is given. */
    title: string;
    searchPlaceholder?: string;
    emptyText: string;
    items: PickerItem[];
    value: string;
    onSelect: (value: string) => void;
    onClose: () => void;
    /** Stacking context. Must sit above whatever opened it. */
    zIndexClass?: string;
    /** Escape level. Follows the stacking, so it answers before what it covers. */
    escapeLevel?: number;
}

/** Case-fold and strip combining marks so a diacritic-free query still matches. */
const fold = (s: string) => s.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');

export default function PickerDialog({
    open, title, searchPlaceholder, emptyText, items, value, onSelect, onClose,
    zIndexClass = 'z-[80]', escapeLevel = 80,
}: PickerDialogProps) {
    const [query, setQuery] = useState('');
    const [highlight, setHighlight] = useState(0);
    const inputRef = useRef<HTMLInputElement>(null);
    const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

    const filtered = useMemo(() => {
        const q = fold(query.trim());
        if (!q) return items;
        return items.filter(
            (it) =>
                fold(it.label).includes(q)
                || (it.sublabel !== undefined && fold(it.sublabel).includes(q))
                || it.value.toLowerCase().includes(q)
        );
    }, [items, query]);

    // Read through refs so the open-effect depends on `open` alone: callers may
    // rebuild `items` every render (the settings modal re-renders on a poll),
    // and re-running the effect would wipe the query mid-typing.
    const latestItems = useRef(items);
    latestItems.current = items;
    const latestValue = useRef(value);
    latestValue.current = value;

    // Opening clears the search and highlights the active entry; the focus jump
    // waits a frame so the input exists before it is focused.
    useEffect(() => {
        if (!open) return;
        setQuery('');
        const idx = latestItems.current.findIndex((it) => it.value === latestValue.current);
        setHighlight(idx >= 0 ? idx : 0);
        const raf = requestAnimationFrame(() => inputRef.current?.focus());
        return () => cancelAnimationFrame(raf);
    }, [open]);

    useEffect(() => {
        itemRefs.current[highlight]?.scrollIntoView({ block: 'nearest' });
    }, [highlight]);

    const latestClose = useRef(onClose);
    latestClose.current = onClose;
    useEscapeLayer({ active: open, level: escapeLevel, onEscape: () => latestClose.current() });

    if (!open) return null;

    const pick = (v: string) => {
        onSelect(v);
        onClose();
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            if (!filtered.length) return;
            const step = e.key === 'ArrowDown' ? 1 : -1;
            setHighlight((h) => (h + step + filtered.length) % filtered.length);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            const hit = filtered[highlight];
            if (hit) pick(hit.value);
        }
    };

    return (
        <div className={`fixed inset-0 ${zIndexClass} flex items-center justify-center p-4`} onClick={onClose}>
            <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
            <div
                role="dialog"
                aria-modal="true"
                aria-label={title}
                onClick={(e) => e.stopPropagation()}
                onKeyDown={onKeyDown}
                className="relative bg-white dark:bg-[#181818] rounded-2xl shadow-2xl w-full max-w-sm border border-gray-200 dark:border-[#2a2a2a] overflow-hidden animate-in fade-in zoom-in-95 duration-150"
            >
                <div className="p-3 pb-2">
                    <input
                        ref={inputRef}
                        value={query}
                        onChange={(e) => {
                            setQuery(e.target.value);
                            setHighlight(0);
                        }}
                        placeholder={searchPlaceholder ?? title}
                        className="w-full px-3 py-2 bg-white text-gray-900 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent placeholder:text-gray-400"
                    />
                </div>
                <ul role="listbox" aria-label={title} className="max-h-[50vh] overflow-y-auto vaf-scroll py-1">
                    {filtered.map((it, i) => (
                        <li key={it.value} role="option" aria-selected={it.value === value}>
                            <button
                                ref={(el) => { itemRefs.current[i] = el; }}
                                type="button"
                                onClick={() => pick(it.value)}
                                onMouseEnter={() => setHighlight(i)}
                                tabIndex={-1}
                                className={cn(
                                    'w-full flex items-center gap-2 px-4 py-2.5 text-left text-sm',
                                    it.value === value ? 'bg-blue-50' : i === highlight ? 'bg-gray-100' : ''
                                )}
                            >
                                {it.emoji !== undefined && <span className="shrink-0">{it.emoji}</span>}
                                <span className="font-medium text-gray-900 truncate">{it.label}</span>
                                {it.sublabel !== undefined && <span className="text-xs text-gray-500 truncate">{it.sublabel}</span>}
                                {it.value === value && <Check size={16} className="ml-auto text-blue-600 shrink-0" />}
                            </button>
                        </li>
                    ))}
                    {filtered.length === 0 && (
                        <li className="px-4 py-6 text-center text-sm text-gray-500">{emptyText}</li>
                    )}
                </ul>
            </div>
        </div>
    );
}

export interface PickerSelectProps {
    label: string;
    value: string;
    onChange: (value: string) => void;
    options: { value: string; label: string }[];
    emptyText: string;
}

/**
 * Drop-in sibling of the settings `Select` (same label and trigger styling)
 * whose options open in a PickerDialog instead of the native dropdown.
 */
export function PickerSelect({ label, value, onChange, options, emptyText }: PickerSelectProps) {
    const [open, setOpen] = useState(false);
    const current = options.find((o) => o.value === value);
    return (
        <div className="flex flex-col gap-1.5 w-full">
            <label className="text-sm font-medium text-gray-700 ml-1">{label}</label>
            <button
                type="button"
                onClick={() => setOpen(true)}
                aria-haspopup="dialog"
                aria-expanded={open}
                className="relative w-full h-10 px-4 pr-10 bg-white border border-gray-200 rounded-lg text-sm text-left text-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
            >
                <span className="block truncate">{current ? current.label : value}</span>
                <span className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-gray-400">
                    <ChevronDown size={16} />
                </span>
            </button>
            <PickerDialog
                open={open}
                title={label}
                emptyText={emptyText}
                items={options}
                value={value}
                onSelect={onChange}
                onClose={() => setOpen(false)}
            />
        </div>
    );
}
