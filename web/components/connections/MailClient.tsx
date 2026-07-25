'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// In-app window for the v2 mail client. It reuses the exact modal chrome of the
// other connection dashboards (Calendar/Cloud): a centered,
// windowed 95vw x 90vh rounded panel over a dimmed backdrop, full-screen on
// mobile. The three-pane UI itself lives in MailClientView (app/mail/page.tsx),
// which also backs the standalone /mail route.

import React from 'react';
import { MailClientView } from '@/app/mail/page';

export function MailClient({ isOpen, onClose }: {
    isOpen: boolean;
    onClose: () => void;
}) {
    if (!isOpen) return null;
    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4 max-md:p-0 bg-black/50"
            onClick={onClose}
        >
            <div
                className="relative bg-[#181818] w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-[#2e2e2e] flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0"
                onClick={e => e.stopPropagation()}
            >
                <MailClientView onClose={onClose} />
            </div>
        </div>
    );
}
