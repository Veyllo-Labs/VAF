// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';
//
// The two form fields of the Settings window, shared by the sections that live in their own file
// (LocalEngineSection) and SettingsModal itself, so a field drawn in a section cannot drift from
// the ones beside it.

import React from 'react';
import { useTranslations } from 'next-intl';
import { ChevronRight, ExternalLink } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface InputProps {
    label: string;
    value: any;
    onChange: (value: string) => void;
    type?: string;
    placeholder?: string;
    disabled?: boolean;
    link?: string;
    // Rendered in the LABEL row, after the provider link. For state that belongs to the
    // field but must not cost it a row of its own - nine API-key fields each carrying a
    // status line below the box turns the panel into a list of three-line blocks.
    labelExtra?: React.ReactNode;
}

export const Input = ({ label, value, onChange, type = "text", placeholder, disabled, link, labelExtra }: InputProps) => (
    <div className="flex flex-col gap-1.5 w-full">
        <div className="flex items-center gap-1.5 ml-1 max-md:flex-wrap">
            <label className="text-sm font-medium text-gray-700">{label}</label>
            {link && (
                <a
                    href={link}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-gray-100 hover:bg-indigo-100 text-gray-400 hover:text-indigo-600 transition-colors"
                    title={`Get API key → ${link}`}
                >
                    <ExternalLink size={9} />
                </a>
            )}
            {labelExtra}
        </div>
        <input
            type={type}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            disabled={disabled}
            className="px-4 h-10 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all placeholder:text-gray-400 disabled:opacity-60 disabled:cursor-not-allowed"
        />
    </div>
);

export interface SelectProps {
    label: string;
    value: any;
    onChange: (value: string) => void;
    options: { value: string; label: string; disabled?: boolean }[];
    disabled?: boolean;
}

export const Select = ({ label, value, onChange, options, disabled }: SelectProps) => {
    const tCommon = useTranslations('common');
    const uniqueOptions = options.filter((option, index) => {
        return options.findIndex((candidate) => candidate.value === option.value) === index;
    });

    return (
        <div className="flex flex-col gap-1.5 w-full">
            <label className="text-sm font-medium text-gray-700 ml-1">{label}</label>
            <div className="relative">
                <select
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    disabled={disabled}
                    // A half-width select cuts a long option; the full text stays one hover away.
                    title={uniqueOptions.find(o => o.value === value)?.label || value || undefined}
                    className={cn("w-full h-10 appearance-none px-4 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all text-gray-700 pr-10", disabled && "cursor-not-allowed text-gray-400")}
                >
                    {/* Default option if current value is not in options (e.g. custom input previously saved) */}
                    {!uniqueOptions.some(o => o.value === value) && value && (
                        <option value={value}>{tCommon('currentOption', { value })}</option>
                    )}
                    {uniqueOptions.map((o) => (
                        <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>
                    ))}
                </select>
                <div className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-gray-400">
                    <ChevronRight size={16} className="rotate-90" />
                </div>
            </div>
        </div>
    );
};
