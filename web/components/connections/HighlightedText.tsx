'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React from 'react';

/** Renders text with all case-insensitive occurrences of query wrapped in <mark>. */
export default function HighlightedText({ text, query }: { text: string; query: string }) {
    if (!query) return <>{text}</>;
    const lower = text.toLowerCase();
    const q = query.toLowerCase();
    const parts: React.ReactNode[] = [];
    let pos = 0;
    for (let idx = lower.indexOf(q); idx !== -1; idx = lower.indexOf(q, pos)) {
        if (idx > pos) parts.push(text.slice(pos, idx));
        parts.push(
            <mark key={idx} className="bg-amber-200 text-gray-900 rounded-[2px]">
                {text.slice(idx, idx + q.length)}
            </mark>
        );
        pos = idx + q.length;
    }
    parts.push(text.slice(pos));
    return <>{parts}</>;
}
