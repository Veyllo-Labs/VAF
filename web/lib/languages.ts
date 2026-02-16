/**
 * Single source of truth for supported UI languages.
 * Used by: language switcher (Settings), optional generate-locales script.
 */

export const languages = [
  { code: "de", name: "Deutsch", flag: "🇩🇪" },
  { code: "en", name: "English", flag: "🇺🇸" },
] as const;

export type LocaleCode = (typeof languages)[number]["code"];
export const localeCodes = languages.map((l) => l.code);
export const defaultLocale: LocaleCode = "de";

/** Check if a string is a supported locale (e.g. from navigator) */
export function isSupportedLocale(lang: string): lang is LocaleCode {
  return localeCodes.includes(lang as LocaleCode);
}

/** Resolve browser language to supported locale (e.g. "de-DE" -> "de") */
export function resolveBrowserLocale(): LocaleCode {
  if (typeof navigator === "undefined") return defaultLocale;
  const raw = navigator.language || (navigator as { userLanguage?: string }).userLanguage || "";
  const base = raw.split("-")[0].toLowerCase();
  return isSupportedLocale(base) ? base : defaultLocale;
}
