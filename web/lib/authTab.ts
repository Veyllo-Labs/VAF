// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Open a sign-in page whose address the backend has to produce first.
 *
 * Why this is shared rather than one more inline call: four sign-ins (mail, calendar, cloud,
 * MCP services) fetched the authorization address and only THEN called `window.open`. A
 * browser lets a page open a tab only while the click that asked for it is fresh (the HTML
 * standard's transient activation, five seconds in Chromium, browser-defined elsewhere), and an
 * MCP service's start (metadata discovery, client registration) may take longer than that. Past it the tab is blocked, and
 * with `noopener` the call returns null in every case, so the page could not even tell.
 *
 * So the tab is taken DURING the click (`reserveAuthTab`, called before the first await) and
 * sent to the address once it arrives; `open` answers whether a tab really got it, and a
 * caller that hears false shows the address as a link. The reserved tab loses its `opener`
 * before it leaves this origin, which is what `noopener` did: the service's page cannot reach
 * back into VAF's.
 *
 * The desktop window is the exception: its shell (`vaf/core/desktop_window.py`, pywebview)
 * hands an external address to the system browser and has no popup blocker, while a blank
 * placeholder would open an empty system-browser window. There the address is opened once it
 * is known, as before.
 */
export interface AuthTab {
  /** Send the reserved tab (or a new one) to `url`; false when no tab could be opened. */
  open(url: string): boolean;
  /** The sign-in did not start: close the reserved tab again. */
  cancel(): void;
}

function inDesktopShell(): boolean {
  return typeof window !== 'undefined' && 'pywebview' in window;
}

export function reserveAuthTab(): AuthTab {
  let tab: Window | null = null;
  if (typeof window !== 'undefined' && !inDesktopShell()) {
    try {
      tab = window.open('about:blank', '_blank');
      if (tab) tab.opener = null;
    } catch {
      tab = null;
    }
  }
  return {
    open(url: string): boolean {
      if (typeof window === 'undefined' || !url) return false;
      if (tab && !tab.closed) {
        try {
          tab.location.replace(url);
          return true;
        } catch {
          /* fall through to a new tab */
        }
      }
      if (inDesktopShell()) {
        window.open(url, '_blank', 'noopener,noreferrer');
        return true;
      }
      // No reserved tab: the blocker took it at click time. One more try, WITHOUT noopener so
      // the answer says whether it opened; the opener is cut right after.
      try {
        const opened = window.open(url, '_blank');
        if (opened) {
          opened.opener = null;
          return true;
        }
      } catch {
        /* blocked */
      }
      return false;
    },
    cancel(): void {
      try {
        if (tab && !tab.closed) tab.close();
      } catch {
        /* already gone */
      }
      tab = null;
    },
  };
}
