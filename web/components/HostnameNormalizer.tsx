'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { useEffect } from 'react';

/**
 * Redirect 127.0.0.1 -> localhost so cookies work consistently.
 * Browsers treat localhost and 127.0.0.1 as different origins; cookies set for one
 * are not sent to the other. Normalizing to localhost avoids auth loops after login.
 */
export default function HostnameNormalizer() {
    useEffect(() => {
        if (typeof window === 'undefined') return;
        const { hostname, port, protocol } = window.location;
        if (hostname === '127.0.0.1') {
            const portPart = port ? `:${port}` : '';
            window.location.replace(`${protocol}//localhost${portPart}${window.location.pathname}${window.location.search}`);
        }
    }, []);
    return null;
}
