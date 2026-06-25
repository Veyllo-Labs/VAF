'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

/**
 * Redirect /settings?* to /?* (main page with same query params).
 * OAuth callbacks (cloud, email) redirect to /settings?connections=1&...; this route forwards
 * to the main page so they don't 404.
 *
 * Intentionally simple: it does NOT use `useSearchParams()` (which suspends during the static
 * prerender and, combined with the other hooks here, caused a server/client hook-count mismatch
 * -> React #310 "Rendered more hooks than during the previous render"). The query string is read
 * client-side from `window.location.search` in an effect instead, so the hook order is identical
 * on server and client.
 */
export default function SettingsRedirectPage() {
    const router = useRouter();

    useEffect(() => {
        const query = typeof window !== 'undefined' ? window.location.search : '';
        router.replace(query ? `/${query}` : '/');
    }, [router]);

    return (
        <div className="flex min-h-screen items-center justify-center bg-gray-50">
            <p className="text-sm text-gray-500">Redirecting…</p>
        </div>
    );
}
