'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * UI-language control (Settings -> Interface): a trigger button styled like
 * the other settings inputs that opens the shared PickerDialog over the
 * locales from lib/languages.ts. Each row carries the endonym plus the
 * English exonym, so every entry stays readable regardless of the active
 * locale; searching, keyboard handling and theming live in the dialog.
 */

import React, { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { ChevronDown } from 'lucide-react';
import PickerDialog from '@/components/ui/PickerDialog';
import { languages } from '@/lib/languages';

interface LanguagePickerProps {
    /** Active locale code. */
    value: string;
    onChange: (code: string) => void;
}

export default function LanguagePicker({ value, onChange }: LanguagePickerProps) {
    const tInterface = useTranslations('settings.interface');
    const [open, setOpen] = useState(false);
    const current = languages.find((l) => l.code === value) ?? languages[0];
    const items = useMemo(
        () => languages.map((l) => ({ value: l.code, label: l.name, sublabel: l.englishName, emoji: l.flag })),
        []
    );
    return (
        <>
            <button
                type="button"
                onClick={() => setOpen(true)}
                aria-haspopup="dialog"
                aria-expanded={open}
                className="w-full flex items-center justify-between px-3 py-2 bg-white text-gray-900 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            >
                <span>{current.flag} {current.name}</span>
                <ChevronDown size={16} className="text-gray-400" />
            </button>
            <PickerDialog
                open={open}
                title={tInterface('language')}
                emptyText={tInterface('languageNoResults')}
                items={items}
                value={value}
                onSelect={onChange}
                onClose={() => setOpen(false)}
            />
        </>
    );
}
