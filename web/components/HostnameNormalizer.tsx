'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { useEffect } from 'react';

/**
 * Redirect 127.0.0.1 -> localhost so cookies work consistently.
 * Browsers treat localhost and 127.0.0.1 as different origins; cookies set for one
 * are not sent to the other. Normalizing to localhost avoids auth loops after login.
 * Rendered by the root layout, so on every page it also drops the token copy an older
 * VAF kept in the page's storage.
 */
export default function HostnameNormalizer() {
    useEffect(() => {
        if (typeof window === 'undefined') return;
        // An older VAF kept a copy of the access token here, where any script in the page could
        // read it. Nothing reads it any more: the HttpOnly cookie is the only credential.
        try { localStorage.removeItem('vaf_token'); } catch { /* storage blocked */ }
        const { hostname, port, protocol } = window.location;
        if (hostname === '127.0.0.1') {
            const portPart = port ? `:${port}` : '';
            window.location.replace(`${protocol}//localhost${portPart}${window.location.pathname}${window.location.search}`);
        }
    }, []);
    return null;
}
