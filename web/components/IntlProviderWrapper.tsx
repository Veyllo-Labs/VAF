'use client';

/**
 * Client-only wrapper that provides next-intl messages based on the
 * locale stored in the Zustand locale store.
 *
 * Wraps children with NextIntlClientProvider so that any component
 * inside can call `useTranslations('namespace')`.
 */

import React, { useEffect } from 'react';
import { NextIntlClientProvider } from 'next-intl';
import { useLocaleStore } from '@/lib/localeStore';

// Static imports – keeps bundle simple (one JSON per locale).
import deMessages from '@/messages/de.json';
import enMessages from '@/messages/en.json';

const messageMap: Record<string, typeof deMessages> = {
  de: deMessages,
  en: enMessages,
};

export default function IntlProviderWrapper({
  children,
}: {
  children: React.ReactNode;
}) {
  const locale = useLocaleStore((s) => s.locale);
  const init = useLocaleStore((s) => s.init);

  // Initialise once on mount (reads localStorage / browser lang).
  useEffect(() => {
    init();
  }, [init]);

  // Keep <html lang="…"> in sync.
  useEffect(() => {
    if (typeof document !== 'undefined') {
      document.documentElement.lang = locale;
    }
  }, [locale]);

  const messages = messageMap[locale] ?? messageMap.de;

  return (
    <NextIntlClientProvider locale={locale} messages={messages}>
      {children}
    </NextIntlClientProvider>
  );
}
