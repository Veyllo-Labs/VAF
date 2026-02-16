"use client";

import { create } from "zustand";
import { defaultLocale, resolveBrowserLocale, isSupportedLocale, type LocaleCode } from "./languages";

const STORAGE_KEY = "ui_locale";

function getStoredLocale(): LocaleCode | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && isSupportedLocale(stored)) return stored as LocaleCode;
  } catch {
    // ignore
  }
  return null;
}

interface LocaleState {
  locale: LocaleCode;
  /** Call once on app mount to init from localStorage or browser */
  init: () => void;
  setLocale: (code: LocaleCode) => void;
}

export const useLocaleStore = create<LocaleState>((set) => ({
  locale: defaultLocale,
  init: () => {
    const stored = getStoredLocale();
    if (stored) {
      set({ locale: stored });
      return;
    }
    set({ locale: resolveBrowserLocale() });
  },
  setLocale: (code: LocaleCode) => {
    try {
      if (typeof window !== "undefined") localStorage.setItem(STORAGE_KEY, code);
    } catch {
      // ignore
    }
    set({ locale: code });
  },
}));
