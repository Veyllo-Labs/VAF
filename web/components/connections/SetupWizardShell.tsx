'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// The frame of a connection's setup window: the overlay, the 672 px panel (a full-screen sheet
// below md), a header with the icon, title and per-step subtitle, the numbered steps, the
// scrolling body and a footer with Back on the left and the step's action on the right.
//
// The class strings are the Discord, Telegram, Cloud and GitHub windows' own, copied verbatim
// so a test can hold them against DiscordSetupWizard.tsx: those four still carry the frame by
// hand, and this is the one place they can move to. Three things are added over those copies:
// the dialog semantics (role, aria-modal, Escape and a click beside the panel all close); a
// height cap with a scrolling body on a desktop, since a step taller than the screen would
// otherwise push the footer out of the panel (the cap is the screen less 1rem above and below,
// not 90vh: on a 793 px window 90vh left 40 px unused while the QR step's body scrolled); and a
// ring around the current step, dark in the light theme and white in the dark one, because the
// completed steps share its black and white and the current one must read as current without a
// colour of its own. The ring's gap takes the panel's own surface colour (--sfc-white), so it
// shows no light halo on the dark panel. The ring reaches 4px past the circle, so the row keeps
// 4px below it (pb-1): without them a scrolled body painted its cards over the ring's lower
// edge. The windows that still draw their own stepper get the ring when they move onto this
// frame.
//
// The disabled Back is `text-gray-300`, which the dark theme does not fold (the text ramp
// folds 400 to 900), so on the dark panel it would read as an active button; it gets a dark
// tone of its own.
//
// Completed steps are drawn black and white like the current one, not green: a window that
// says success and failure by its text only (the Veyllo App's) must not get its colour from
// the frame.

import React from 'react';
import { useTranslations } from 'next-intl';
import { Check, ChevronLeft, ChevronRight, Loader2, X } from 'lucide-react';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';
import { cn } from '@/lib/utils';

export interface SetupWizardShellProps {
    /** The header's title; also the dialog's accessible name. */
    title: string;
    /** The line under the title, usually the current step's name. */
    subtitle: string;
    /** Drawn white inside the header's icon box. */
    icon: React.ReactNode;
    /** How many numbered steps the header draws. */
    steps: number;
    /** The current step, counted from 0. */
    current: number;
    /** The X, Escape and a click beside the panel. */
    onClose: () => void;
    /** Back; without it Back is shown disabled. */
    onBack?: () => void;
    /** The footer's right side, usually a WizardNextButton. */
    action: React.ReactNode;
    children: React.ReactNode;
}

/** Rendered only while the window is open: mounting it registers the Escape layer. */
export default function SetupWizardShell({ title, subtitle, icon, steps, current, onClose, onBack, action, children }: SetupWizardShellProps) {
    const tCommon = useTranslations('common');
    useEscapeLayer({ active: true, level: 60, onEscape: onClose });

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm max-md:p-0" onClick={onClose}>
            <div role="dialog" aria-modal="true" aria-label={title} onClick={e => e.stopPropagation()}
                className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 max-md:max-w-none max-md:mx-0 max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:flex max-md:flex-col max-h-[calc(100dvh-2rem)] flex flex-col max-md:max-h-none">
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0 shrink-0 gap-3">
                    <div className="flex items-center gap-3 min-w-0">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 dark:bg-[#2e2e2e] flex items-center justify-center shrink-0">
                            {icon}
                        </div>
                        <div className="min-w-0">
                            <h2 className="text-xl font-bold text-gray-900">{title}</h2>
                            <p className="text-sm text-gray-500">{subtitle}</p>
                        </div>
                    </div>
                    <button type="button" onClick={onClose} aria-label={tCommon('close')}
                        className="p-2 hover:bg-gray-200 rounded-lg transition-colors shrink-0">
                        <X className="w-5 h-5 text-gray-500" aria-hidden="true" />
                    </button>
                </div>

                <div className="px-6 pt-4 pb-1 shrink-0">
                    <p className="sr-only">{tCommon('stepOf', { step: current + 1, steps })}</p>
                    <div className="flex items-center gap-2" aria-hidden="true">
                        {Array.from({ length: steps }, (_, idx) => (
                            <React.Fragment key={idx}>
                                <div className={cn(
                                    'w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-all shrink-0',
                                    idx < current ? 'bg-gray-900 text-white dark:bg-[#e6e6e6] dark:text-[#181818]' :
                                    idx === current ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white ring-2 ring-gray-900 dark:ring-white ring-offset-2 ring-offset-[rgb(var(--sfc-white))]' :
                                    'bg-gray-200 text-gray-500'
                                )}>
                                    {idx < current ? <Check className="w-4 h-4" /> : idx + 1}
                                </div>
                                {idx < steps - 1 && (
                                    <div className={cn(
                                        'flex-1 h-1 rounded-full transition-all',
                                        idx < current ? 'bg-gray-900 dark:bg-[#e6e6e6]' : 'bg-gray-200'
                                    )} />
                                )}
                            </React.Fragment>
                        ))}
                    </div>
                </div>

                <div className="p-6 min-h-[350px] max-md:min-h-0 max-md:flex-1 max-md:overflow-y-auto max-md:p-4 flex-1 overflow-y-auto">
                    {children}
                </div>

                <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0 shrink-0 gap-3">
                    <button
                        type="button"
                        onClick={onBack}
                        disabled={!onBack}
                        className={cn(
                            'flex items-center gap-2 px-4 py-2 rounded-lg transition-colors',
                            !onBack ? 'text-gray-300 dark:text-[#4a4a4a] cursor-not-allowed' : 'text-gray-600 hover:bg-gray-200'
                        )}
                    >
                        <ChevronLeft className="w-4 h-4" aria-hidden="true" />
                        {tCommon('back')}
                    </button>
                    {action}
                </div>
            </div>
        </div>
    );
}

/** The footer's primary button, in the Discord window's Next look. `chevron` is for a plain
 *  Next; an action that does something else (Show QR code, New code, Done) goes without. */
export function WizardNextButton({ children, onClick, disabled = false, type = 'button', form, busy = false, chevron = true }: {
    children: React.ReactNode;
    onClick?: () => void;
    disabled?: boolean;
    type?: 'button' | 'submit';
    /** The id of a form elsewhere in the window that this button submits. */
    form?: string;
    busy?: boolean;
    chevron?: boolean;
}) {
    return (
        <button
            type={type}
            form={form}
            onClick={onClick}
            disabled={disabled}
            className={cn(
                'flex items-center gap-2 px-6 py-2 rounded-lg font-medium transition-colors',
                !disabled
                    ? 'bg-gray-900 hover:bg-gray-800 text-white dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none'
                    : 'bg-gray-200 text-gray-400 cursor-not-allowed'
            )}
        >
            {busy && <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" />}
            {children}
            {chevron && <ChevronRight className="w-4 h-4" aria-hidden="true" />}
        </button>
    );
}
