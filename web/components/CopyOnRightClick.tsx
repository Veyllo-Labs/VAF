"use client";

import React, { useEffect, useRef, useState } from 'react';
import { Check } from 'lucide-react';

/**
 * Right-click on selected text copies it to the clipboard and shows a brief "Kopiert" toast
 * at the cursor. Does nothing when no text is selected, and leaves inputs/textareas/
 * contenteditable alone so their native menu (paste etc.) still works.
 */
export default function CopyOnRightClick() {
  const [toast, setToast] = useState<{ x: number; y: number; id: number } | null>(null);
  const hideRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const onContextMenu = (e: MouseEvent) => {
      const sel = typeof window !== 'undefined' ? window.getSelection() : null;
      const text = sel ? sel.toString() : '';
      if (!text.trim()) return; // nothing selected → keep native behavior

      const target = e.target as HTMLElement | null;
      if (target && target.closest('input, textarea, [contenteditable="true"]')) return;

      e.preventDefault();

      // Copy the already-active selection with execCommand. This is synchronous and does
      // NOT trigger a clipboard *permission* request — important inside the QtWebEngine
      // desktop window, where navigator.clipboard.writeText() fires a feature-permission
      // request that crashes pywebview's Qt handler. Fall back to the async API only if
      // execCommand is unavailable (e.g. a future plain-browser context).
      let copied = false;
      try { copied = document.execCommand('copy'); } catch { copied = false; }
      if (!copied && navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).catch(() => { /* give up silently */ });
      }

      setToast({ x: e.clientX, y: e.clientY, id: Date.now() });
      if (hideRef.current) clearTimeout(hideRef.current);
      hideRef.current = setTimeout(() => setToast(null), 1100);
    };

    document.addEventListener('contextmenu', onContextMenu);
    return () => {
      document.removeEventListener('contextmenu', onContextMenu);
      if (hideRef.current) clearTimeout(hideRef.current);
    };
  }, []);

  if (!toast) return null;

  return (
    <div
      key={toast.id}
      className="fixed z-[10000] pointer-events-none -translate-x-1/2 -translate-y-full"
      style={{ left: toast.x, top: toast.y - 8 }}
    >
      <div className="flex items-center gap-1.5 rounded-full bg-gray-900 px-2.5 py-1 text-xs font-medium text-white shadow-lg animate-in fade-in zoom-in duration-150">
        <Check size={13} className="text-emerald-400" />
        Kopiert
      </div>
    </div>
  );
}
