// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Zustand store for the custom-cursor preference (VAF's custom dot cursor vs. the system mouse).
 *
 * Pure client-side, per-device UI preference - same pattern as the UI locale (localeStore):
 * default ON, read from localStorage on mount (init), persisted on change, applied live.
 * CustomCursor subscribes to render-or-not + toggle the body class; SettingsModal flips it.
 */

import { create } from 'zustand';

const STORAGE_KEY = 'vaf_custom_cursor';

interface CursorState {
  enabled: boolean;
  /** Call once on app mount to read the persisted preference (avoids SSR hydration mismatch). */
  init: () => void;
  /** Toggle the custom cursor and persist it. */
  setEnabled: (on: boolean) => void;
}

function readPersisted(): boolean {
  if (typeof window === 'undefined') return true; // SSR: default ON (matches initial state)
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'off') return false;
    if (stored === 'on') return true;
  } catch {
    // localStorage blocked / unavailable
  }
  return true; // unset => default ON
}

export const useCursorStore = create<CursorState>((set) => ({
  enabled: true, // overwritten by init() after mount
  init: () => {
    set({ enabled: readPersisted() });
  },
  setEnabled: (on: boolean) => {
    try {
      localStorage.setItem(STORAGE_KEY, on ? 'on' : 'off');
    } catch {
      // ignore
    }
    set({ enabled: on });
  },
}));
