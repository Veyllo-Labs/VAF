// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useEffect, useState } from 'react';

/**
 * Single shared mobile/touch breakpoint primitive. Aligned with Tailwind's `md`
 * (768px) so JS state and `md:` / `max-md:` CSS always agree.
 *
 * SSR-safe: returns `false` on the server and the first client paint, then syncs
 * to the real viewport in an effect — so it never triggers a hydration mismatch.
 * Components should render the desktop layout by default and only branch to the
 * mobile layout once this is `true`.
 */
export function useIsMobile(query = '(max-width: 767px)'): boolean {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia(query);
    const onChange = () => setIsMobile(mql.matches);
    onChange();
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [query]);

  return isMobile;
}
