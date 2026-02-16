"use client";

import { useEffect } from "react";
import { NextIntlClientProvider } from "next-intl";
import { useLocaleStore } from "@/lib/localeStore";
import { defaultLocale, type LocaleCode } from "@/lib/languages";
import deMessages from "@/messages/de.json";
import enMessages from "@/messages/en.json";

const messagesMap: Record<LocaleCode, typeof deMessages> = {
  de: deMessages,
  en: enMessages,
};

function getMessages(locale: LocaleCode): typeof deMessages {
  return messagesMap[locale] ?? messagesMap[defaultLocale];
}

export default function IntlProviderWrapper({
  children,
}: {
  children: React.ReactNode;
}) {
  const locale = useLocaleStore((s) => s.locale);
  const init = useLocaleStore((s) => s.init);

  useEffect(() => {
    init();
  }, [init]);

  useEffect(() => {
    if (typeof document !== "undefined") {
      document.documentElement.lang = locale;
    }
  }, [locale]);

  const messages = getMessages(locale);

  return (
    <NextIntlClientProvider locale={locale} messages={messages}>
      {children}
    </NextIntlClientProvider>
  );
}
