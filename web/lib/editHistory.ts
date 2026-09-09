// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * A bounded undo/redo history over immutable snapshots.
 *
 * The native DOCX editor edits a plain document model, not a DOM the browser could undo
 * for us, so the history has to be ours: every edit records the model it replaces, undo
 * and redo walk between the recorded snapshots. The functions are pure and the history
 * value is never mutated, so a React functional state update can carry the model and its
 * history in one object without a ref that a re-run could double-record.
 *
 * A snapshot is stored by reference. Callers must therefore treat a model as immutable
 * once it has been in state (the editor clones before it mutates), or the history would
 * hold the same object under two labels.
 */

/** How many steps are kept. Older ones fall off the far end. */
export const HISTORY_LIMIT = 200;

/**
 * Edits with the same coalesce key that arrive within this window form ONE step, so a
 * burst of typing into a field that writes through on every keystroke (a table cell, the
 * font name) is taken back as a whole and not letter by letter. The window is measured
 * from the previous keystroke, so it stays open while typing continues.
 */
export const COALESCE_WINDOW_MS = 1000;

type HistoryStep<T> = {
  /** The snapshot the edit replaced: what undo restores. */
  before: T;
  /** Groups consecutive edits of one field; null for an edit that stands alone. */
  key: string | null;
  /** When the last edit of this step happened (ms). */
  at: number;
};

export type EditHistory<T> = {
  readonly past: readonly HistoryStep<T>[];
  readonly future: readonly T[];
};

export function createEditHistory<T>(): EditHistory<T> {
  return { past: [], future: [] };
}

export function canUndo<T>(history: EditHistory<T>): boolean {
  return history.past.length > 0;
}

export function canRedo<T>(history: EditHistory<T>): boolean {
  return history.future.length > 0;
}

/**
 * Records that `before` is being replaced by a new snapshot. Returns the history to keep
 * next to the new snapshot. Any redo branch is dropped: after a fresh edit there is
 * nothing to redo, as in every editor.
 */
export function recordStep<T>(
  history: EditHistory<T>,
  before: T,
  coalesceKey?: string,
  now: number = Date.now(),
  limit: number = HISTORY_LIMIT,
): EditHistory<T> {
  const last = history.past[history.past.length - 1];
  if (coalesceKey && last && last.key === coalesceKey && now - last.at <= COALESCE_WINDOW_MS) {
    // Same field, still typing: the step keeps the snapshot from before the burst began
    // and only its clock moves, so the burst stays one step for as long as it continues.
    const past = history.past.slice(0, -1).concat({ ...last, at: now });
    return { past, future: [] };
  }
  const past = history.past.concat({ before, key: coalesceKey ?? null, at: now });
  return { past: past.length > limit ? past.slice(past.length - limit) : past, future: [] };
}

/**
 * Steps back. `current` is the snapshot in use, which becomes the redo target. Returns
 * null when there is nothing to undo.
 */
export function undoStep<T>(history: EditHistory<T>, current: T): { history: EditHistory<T>; value: T } | null {
  const last = history.past[history.past.length - 1];
  if (!last) return null;
  return {
    value: last.before,
    history: { past: history.past.slice(0, -1), future: [current, ...history.future] },
  };
}

/**
 * Steps forward again. The step re-entered into the past carries no coalesce key: a redone
 * burst is closed, later typing must not merge into it.
 */
export function redoStep<T>(history: EditHistory<T>, current: T, now: number = Date.now()): { history: EditHistory<T>; value: T } | null {
  const [next, ...rest] = history.future;
  if (next === undefined) return null;
  return {
    value: next,
    history: { past: history.past.concat({ before: current, key: null, at: now }), future: rest },
  };
}

/**
 * The keyboard convention shared by the editors: Ctrl+Z (Cmd+Z) undoes, Ctrl+Y and
 * Ctrl+Shift+Z (Cmd+Shift+Z) redo. Alt combinations are left to the browser. `key` is
 * the produced character, so the swapped Z and Y of a QWERTZ layout are handled by the
 * browser and arrive here as the user reads them on the key.
 */
export function historyShortcut(e: { key: string; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean }): 'undo' | 'redo' | null {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return null;
  const key = e.key.toLowerCase();
  if (key === 'z') return e.shiftKey ? 'redo' : 'undo';
  if (key === 'y' && !e.shiftKey) return 'redo';
  return null;
}
