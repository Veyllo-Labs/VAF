/**
 * Zustand store for the active UI locale.
 *
 * Reads from localStorage on first access; writes back on every change.
 * Components subscribe via `useLocaleStore()`.
 */

import { create } from 'zustand';
import { defaultLocale, isSupportedLocale, resolveBrowserLocale } from './languages';

const STORAGE_KEY = 'ui_locale';

interface LocaleState {
  locale: string;
  /** Call once on app mount to read persisted / browser locale. */
  init: () => void;
  /** Switch the active locale and persist it. */
  setLocale: (code: string) => void;
}

function readPersistedLocale(): string {
  if (typeof window === 'undefined') return defaultLocale;
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && isSupportedLocale(stored)) return stored;
  } catch {
    // localStorage blocked / unavailable
  }
  return resolveBrowserLocale();
}

export const useLocaleStore = create<LocaleState>((set) => ({
  locale: defaultLocale, // will be overwritten by init()
  init: () => {
    const resolved = readPersistedLocale();
    set({ locale: resolved });
  },
  setLocale: (code: string) => {
    if (!isSupportedLocale(code)) return;
    try {
      localStorage.setItem(STORAGE_KEY, code);
    } catch {
      // ignore
    }
    set({ locale: code });
  },
}));
