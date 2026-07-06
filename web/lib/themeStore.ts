// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Zustand store for the UI theme (light / dark).
 *
 * Pure client-side, per-device UI preference — same pattern as the custom cursor
 * (cursorStore) and UI locale. The REAL driver of the theme is the `dark` class on
 * <html>: a parser-blocking inline script in app/layout.tsx stamps it from
 * localStorage BEFORE first paint (no light flash — globals.css has a global
 * transition-colors that would otherwise visibly fade on every load). This store
 * only mirrors that class for React consumers (the Settings switch) and performs
 * the live toggle + persistence.
 *
 * Storage: 'vaf_theme' = 'light' | 'dark'; absent = light (explicit default, so
 * every existing install keeps today's exact look; 'system' stays available as a
 * possible future third state).
 */

import { create } from 'zustand';

const STORAGE_KEY = 'vaf_theme';

export type UiTheme = 'light' | 'dark';

interface ThemeState {
  theme: UiTheme;
  /** Call once on mount: sync from the <html> class the pre-paint script stamped. */
  init: () => void;
  /** Switch the theme live and persist it. */
  setTheme: (theme: UiTheme) => void;
}

function readDom(): UiTheme {
  if (typeof document === 'undefined') return 'light'; // SSR default (html className="light")
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

export const useThemeStore = create<ThemeState>((set) => ({
  theme: readDom(),
  init: () => {
    set({ theme: readDom() });
  },
  setTheme: (theme: UiTheme) => {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // localStorage blocked / unavailable — the live toggle still works for this session
    }
    const cls = document.documentElement.classList;
    cls.remove('light', 'dark');
    cls.add(theme);
    set({ theme });
  },
}));
