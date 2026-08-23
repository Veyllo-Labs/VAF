// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/** What the desktop window's native Save dialog answers. */
type SaveResult = { ok?: boolean; path?: string; cancelled?: boolean; error?: string };

type DesktopApi = { save_text_as?: (content: string, name: string) => Promise<SaveResult> };

function desktopApi(): DesktopApi | undefined {
  if (typeof window === 'undefined') return undefined;
  return (window as unknown as { pywebview?: { api?: DesktopApi } }).pywebview?.api;
}

/**
 * Write text to a file the user picks, on every host VAF runs in.
 *
 * In the desktop window the host's own Save dialog is the only good answer:
 * QtWebEngine's download path is brittle there (a parentless dialog can open
 * behind the window), which is why `save_text_as` exists in
 * `vaf/core/desktop_window.py`. In a browser the anchor is the only answer.
 *
 * Three details that are not decoration:
 * - The bridge's RESULT decides, not its presence. A bridge that answers
 *   `{ok: false, error}` has saved nothing, and branching on the function name
 *   alone leaves the person with no file and no message.
 * - A CANCELLED dialog is an answer, not a failure. Falling back to the browser
 *   download there would write the file the person just declined to write.
 * - The anchor never carries `target="_blank"`. The desktop window intercepts
 *   such clicks on localhost URLs and navigates the whole app to them.
 *
 * Returns true when the file was written or handed to the browser, false when
 * the person cancelled or nothing could be saved.
 */
export async function downloadText(content: string, filename: string, mime = 'text/markdown'): Promise<boolean> {
  const text = String(content ?? '');
  const name = filename || 'download.txt';

  const api = desktopApi();
  if (api?.save_text_as) {
    try {
      const res = await api.save_text_as(text, name);
      if (res?.ok) return true;
      if (res?.cancelled) return false;
      // Anything else is a broken bridge: fall through to the browser path.
    } catch {
      // Bridge unreachable: fall through.
    }
  }

  if (typeof document === 'undefined') return false;
  const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoked on the next tick: revoking synchronously can race the browser's own
  // read of the blob and produce an empty file.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return true;
}
