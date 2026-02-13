'use client';

import { Suspense, useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

function SettingsRedirectInner() {
    const router = useRouter();
    const searchParams = useSearchParams();

    useEffect(() => {
        const query = searchParams.toString();
        const target = query ? `/?${query}` : '/';
        router.replace(target);
    }, [router, searchParams]);

    return (
        <div className="flex min-h-screen items-center justify-center bg-gray-50">
            <p className="text-sm text-gray-500">Redirecting to settings…</p>
        </div>
    );
}

/**
 * Redirect /settings?* to /?* (main page with same query params).
 * OAuth callbacks (cloud, email) redirect to /settings?connections=1&...;
 * this route fixes the 404 by forwarding to the main page.
 */
export default function SettingsRedirectPage() {
    return (
        <Suspense fallback={
            <div className="flex min-h-screen items-center justify-center bg-gray-50">
                <p className="text-sm text-gray-500">Redirecting…</p>
            </div>
        }>
            <SettingsRedirectInner />
        </Suspense>
    );
}
