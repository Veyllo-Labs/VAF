// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// One yes/no question, asked before something that cannot be taken back cheaply.
//
// Why this is a primitive and not one more hand-rolled overlay: there was no
// shared confirmation anywhere (web/components/ui/ held card.tsx and nothing
// else), while the destructive-dialog card class string is repeated verbatim in
// five places and four more sites fall back to the browser's own
// window.confirm(), which blocks, ignores the theme and cannot be translated.
// This component is what those converge on. Callers own the wording, nothing
// else.
//
// The house dark-mode fold decides the button classes and they are not
// interchangeable with the usual pair (see docs/web-ui/DARKMODE.md):
//   - `bg-gray-900` and `text-white` are NOT folded, so the confirm button stays
//     black with white text in both themes without a `dark:` override.
//   - `bg-white` IS folded (to #202020), so the cancel button needs the literal
//     `dark:bg-[#e6e6e6]`, and its label needs `dark:text-[#181818]` because
//     `dark:text-gray-900` renders LIGHT.
// The pair is deliberately inverted against the usual primary/secondary: here the
// SAFE answer carries the emphasis, because the confirmed action costs real work.

'use client';

import React, { useEffect, useRef } from 'react';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';

export interface ConfirmDialogProps {
    open: boolean;
    title: string;
    /** Body copy. A string renders as one paragraph; pass nodes for several. */
    body: React.ReactNode;
    confirmLabel: string;
    cancelLabel: string;
    onConfirm: () => void;
    onCancel: () => void;
    /** Stacking context. Must sit above whatever opened it. */
    zIndexClass?: string;
    /** Escape level. Follows the stacking, so it answers before what it covers. */
    escapeLevel?: number;
}

export default function ConfirmDialog({
    open, title, body, confirmLabel, cancelLabel, onConfirm, onCancel,
    zIndexClass = 'z-[80]', escapeLevel = 80,
}: ConfirmDialogProps) {
    const cancelRef = useRef<HTMLButtonElement>(null);
    // The handler is read through a ref so the listener below can depend on
    // `open` alone. Callers pass an inline arrow for onCancel, so its identity
    // changes on every parent render - and the parent here re-renders on a
    // two-second poll. Keying the effects on it would re-focus the button, and
    // tear down and rebuild the listener, twice a minute for as long as the
    // dialog is up.
    const latestCancel = useRef(onCancel);
    latestCancel.current = onCancel;

    // Focus the SAFE answer, once per opening: Enter on a dialog nobody has
    // touched must not be the expensive answer.
    useEffect(() => {
        if (open) cancelRef.current?.focus();
    }, [open]);

    // Escape is a layer in the shared registry, above the modal that opened this
    // dialog, so one press answers the question and the modal underneath cannot
    // close out from under it. Its own listener could not do that any more once
    // the registry existed: both would be capture-phase listeners on window, and
    // the one bound first wins a press the other never sees.
    useEscapeLayer({ active: open, level: escapeLevel, onEscape: () => latestCancel.current() });

    if (!open) return null;

    return (
        <div className={`fixed inset-0 ${zIndexClass} flex items-center justify-center p-4`}
            onClick={onCancel}>
            <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
            <div
                role="dialog"
                aria-modal="true"
                aria-label={title}
                onClick={(e) => e.stopPropagation()}
                className="relative bg-white dark:bg-[#181818] rounded-2xl shadow-2xl w-full max-w-md border border-gray-200 dark:border-[#2a2a2a] p-6 animate-in fade-in zoom-in-95 duration-150"
            >
                <h3 className="text-base font-semibold text-gray-900 mb-3">{title}</h3>
                <div className="text-sm text-gray-600 space-y-3 mb-6 leading-relaxed">
                    {typeof body === 'string' ? <p>{body}</p> : body}
                </div>
                <div className="flex gap-3">
                    <button
                        ref={cancelRef}
                        type="button"
                        onClick={onCancel}
                        className="flex-1 py-2.5 rounded-xl font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 border border-gray-200 dark:bg-[#e6e6e6] dark:hover:bg-white dark:text-[#181818] dark:border-transparent transition-colors"
                    >
                        {cancelLabel}
                    </button>
                    <button
                        type="button"
                        onClick={onConfirm}
                        // No dark: override on purpose - neither utility folds, so this
                        // stays black with white text in both themes. The hairline is
                        // what keeps it off the #181818 card.
                        className="flex-1 py-2.5 rounded-xl font-medium bg-gray-900 hover:bg-black text-white border border-transparent dark:border-[#3a3a3a] transition-colors"
                    >
                        {confirmLabel}
                    </button>
                </div>
            </div>
        </div>
    );
}
