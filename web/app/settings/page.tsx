'use client';

import { Suspense, useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';

function SettingsRedirectInner() {
    const router = useRouter();
    const searchParams = useSearchParams();
    const t = useTranslations('settings');

    useEffect(() => {
        const query = searchParams.toString();
        const target = query ? `/?${query}` : '/';
        router.replace(target);
    }, [router, searchParams]);

    return (
        <div className="flex min-h-screen items-center justify-center bg-gray-50">
            <p className="text-sm text-gray-500">{t('redirectingToSettings')}</p>
        </div>
    );
}

/**
 * Redirect /settings?* to /?* (main page with same query params).
 * OAuth callbacks (cloud, email) redirect to /settings?connections=1&...;
 * this route fixes the 404 by forwarding to the main page.
 */
export default function SettingsRedirectPage() {
    const t = useTranslations('settings');
    return (
        <Suspense fallback={
            <div className="flex min-h-screen items-center justify-center bg-gray-50">
                <p className="text-sm text-gray-500">{t('redirecting')}</p>
            </div>
        }>
            <SettingsRedirectInner />
        </Suspense>
    );
}
