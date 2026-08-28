// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Supported UI locales - single source of truth.
 *
 * Adding a language:
 *   1. Add an entry here.
 *   2. Create web/messages/<code>.json (copy de.json as template).
 *   3. Register the import in IntlProviderWrapper.tsx.
 */

export interface Language {
  code: string;
  /** Endonym: the language's own name for itself, the primary label everywhere. */
  name: string;
  /** English exonym, the secondary label in the language picker so every entry stays readable regardless of the active locale. */
  englishName: string;
  flag: string;
}

export const languages: Language[] = [
  { code: 'de', name: 'Deutsch', englishName: 'German', flag: '🇩🇪' },
  { code: 'en', name: 'English', englishName: 'English', flag: '🇺🇸' },
  { code: 'tr', name: 'Türkçe', englishName: 'Turkish', flag: '🇹🇷' },
  { code: 'zh', name: '简体中文', englishName: 'Simplified Chinese', flag: '🇨🇳' },
  { code: 'ja', name: '日本語', englishName: 'Japanese', flag: '🇯🇵' },
  { code: 'ko', name: '한국어', englishName: 'Korean', flag: '🇰🇷' },
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
