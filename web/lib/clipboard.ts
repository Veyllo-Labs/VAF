// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Put text on the clipboard from a button, on every host VAF runs in.
 *
 * Why this is shared rather than one more inline call: six sites wrote to the
 * clipboard by hand and four of them called `navigator.clipboard.writeText`
 * with no fallback at all. That API exists only in a SECURE context, so for a
 * user reaching VAF over plain HTTP on the LAN it is undefined: two of those
 * sites throw, two silently do nothing, and in both cases the code that was
 * copied never reaches the person who needs it.
 *
 * The fallback is a hidden textarea plus `document.execCommand('copy')`, which
 * has no secure-context requirement. It copies the SELECTION, so a button press
 * has to manufacture one first, which is the whole reason this cannot be a
 * one-liner at each call site.
 *
 * The desktop window (QtWebEngine) does support the async API: clipboard access
 * is enabled and the crashing permission slot replaced in
 * `vaf/core/desktop_window.py`. That fix is Qt-only, so the fallback stays
 * load-bearing for every other host.
 */
export async function copyText(text: string): Promise<boolean> {
  const value = String(text ?? '');
  if (!value) return false;
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Not available, denied, or an insecure context: fall through.
  }
  if (typeof document === 'undefined') return false;
  const area = document.createElement('textarea');
  area.value = value;
  // Off-screen rather than hidden: an element with display:none or
  // visibility:hidden cannot hold a selection, so the copy would be a no-op.
  // readOnly keeps the mobile keyboard down while it is briefly focused.
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  document.body.appendChild(area);
  const previous = document.getSelection()?.rangeCount ? document.getSelection()!.getRangeAt(0) : null;
  let copied = false;
  try {
    area.select();
    area.setSelectionRange(0, value.length);
    copied = document.execCommand('copy');
  } catch {
    copied = false;
  }
  document.body.removeChild(area);
  // Give the person their own selection back: copying a reply must not clear
  // the passage they had highlighted in it.
  if (previous) {
    const sel = document.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(previous);
  }
  return copied;
}
