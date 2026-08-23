// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useEffect, useRef } from 'react';

/**
 * One Escape press, one layer: the shared dismissal primitive for overlays.
 *
 * Why a registry instead of a listener per overlay. Overlays nest, and every
 * hand-rolled Escape handler in this app binds `keydown` on `window`.
 * `stopPropagation` is consulted between the NODES of a dispatch path, never
 * between two listeners on the same node, so a listener on `window` cannot stop
 * a sibling listener on `window`: one press reached all of them and dismissed
 * two or three layers at once, and the calls written to prevent that were inert.
 * A single listener over an ordered registry is the only shape that can decide
 * which layer the press belongs to.
 *
 * How it decides. The listener runs in the CAPTURE phase on `window`, the first
 * node of the path, and calls `stopImmediatePropagation()`, so nothing else sees
 * the press: no other window listener, none of React's delegated handlers (React
 * 18 attaches them to the app root, which is below `window`), and no `onKeyDown`
 * on a focused input. The topmost registered layer answers, and only it.
 *
 * `level` orders the layers, higher answers first. The convention is the
 * overlay's own Tailwind z-index number, so a `z-[100]` window registers at 100
 * and the ladder matches what the eye sees. Content that lives INSIDE one
 * overlay and covers it (a draft row, a context menu, a confirmation) counts up
 * from its parent's level in the order it covers the parent. Equal levels break
 * by registration order, latest first. An explicit level rather than plain
 * last-in-first-out: React runs child effects before parent effects, so a parent
 * that mounts with a nested layer already open would register the child first
 * and the order would follow mount timing instead of the screen.
 *
 * `onEscape: null` registers a layer that SWALLOWS the press and dismisses
 * nothing. That is the shape for a dialog whose Cancel is disabled while the
 * work it guards is in flight, and for a surface Escape must never abort. It
 * also keeps the layer underneath from answering in its place.
 *
 * Opting out is not registering. With an empty registry the listener is not
 * bound at all, so an overlay that has not been converted behaves exactly as it
 * did before this file existed.
 */
export type EscapeLayerOptions = {
  /** Register only while the overlay is really on screen. */
  active: boolean;
  /** Higher answers first. Convention: the overlay's own z-index number. */
  level: number;
  /** What one Escape does here. `null` swallows the press and dismisses nothing. */
  onEscape: (() => void) | null;
};

type Layer = { level: number; seq: number; read: () => (() => void) | null };

const layers: Layer[] = [];
let seq = 0;
let bound = false;

function topmost(): Layer | null {
  let best: Layer | null = null;
  for (const layer of layers) {
    if (!best || layer.level > best.level
      || (layer.level === best.level && layer.seq > best.seq)) best = layer;
  }
  return best;
}

function onKeyDown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return;
  // Escape ends an IME composition. That press belongs to the input.
  if (e.isComposing || e.keyCode === 229) return;
  const layer = topmost();
  if (!layer) return;
  // stopImmediatePropagation, not stopPropagation: the listeners this has to win
  // against are bound on `window` too, and stopPropagation does nothing between
  // two listeners on the same node.
  e.stopImmediatePropagation();
  e.preventDefault();
  layer.read()?.();
}

function bind() {
  if (bound || typeof window === 'undefined') return;
  window.addEventListener('keydown', onKeyDown, true);
  bound = true;
}

function unbindIfIdle() {
  if (!bound || layers.length > 0 || typeof window === 'undefined') return;
  window.removeEventListener('keydown', onKeyDown, true);
  bound = false;
}

export function useEscapeLayer({ active, level, onEscape }: EscapeLayerOptions): void {
  // The handler is read through a ref so the effect depends on `active` and
  // `level` alone. Call sites pass inline arrows and the chat page re-renders on
  // every streamed token, so keying the effect on the handler would tear the
  // registration down and rebuild it hundreds of times per reply.
  const latest = useRef(onEscape);
  latest.current = onEscape;

  useEffect(() => {
    if (!active) return;
    const layer: Layer = { level, seq: ++seq, read: () => latest.current };
    layers.push(layer);
    bind();
    return () => {
      const i = layers.indexOf(layer);
      if (i >= 0) layers.splice(i, 1);
      unbindIfIdle();
    };
  }, [active, level]);
}
