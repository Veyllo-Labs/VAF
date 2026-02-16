/**
 * Supported UI locales – single source of truth.
 *
 * Adding a language:
 *   1. Add an entry here.
 *   2. Create web/messages/<code>.json (copy de.json as template).
 *   3. Register the import in IntlProviderWrapper.tsx.
 */

export interface Language {
  code: string;
  name: string;
  flag: string;
}

export const languages: Language[] = [
  { code: 'de', name: 'Deutsch', flag: '🇩🇪' },
  { code: 'en', name: 'English', flag: '🇬🇧' },
];

/** All supported locale codes, derived from the array above. */
export const localeCodes = languages.map((l) => l.code);

/** Default locale when nothing is stored and browser language is unsupported. */
export const defaultLocale = 'de';

/** Check whether a string is a supported locale code. */
export function isSupportedLocale(code: string): boolean {
  return localeCodes.includes(code);
}

/**
 * Derive a supported locale from the browser's language.
 * navigator.language may be "de-DE" or "en-US"; we normalise to the
 * base tag first, then try the full tag if the base didn't match.
 */
export function resolveBrowserLocale(): string {
  if (typeof navigator === 'undefined') return defaultLocale;

  const raw = navigator.language || '';
  const base = raw.split('-')[0].toLowerCase();

  if (isSupportedLocale(base)) return base;
  if (isSupportedLocale(raw.toLowerCase())) return raw.toLowerCase();

  return defaultLocale;
}
