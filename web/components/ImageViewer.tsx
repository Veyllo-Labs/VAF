'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Image as ImageIcon, X, Download, ChevronDown, Highlighter, Eraser } from 'lucide-react';

const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico'];

export function isImageFile(filePath: string): boolean {
  const ext = filePath.split('.').pop()?.toLowerCase() ?? '';
  return IMAGE_EXTS.includes(ext);
}

/** A yellow-marked region of the image, ready to send to the vision model. */
export interface ImageMark {
  name: string;
  /** Full image with the yellow rectangle burned in (data: URL). */
  annotated: string;
  /** Zoomed crop of the marked region (data: URL). */
  crop: string;
}

interface NormRect { x: number; y: number; w: number; h: number }

interface ImageViewerProps {
  isOpen: boolean;
  filePath?: string;
  title?: string;
  /** Pre-resolved image source (URL or data: URL). Falls back to /api/file?path=<filePath>. */
  initialContent?: string;
  /** One-time vision description of the image (shown as searchable text). */
  description?: string;
  descriptionLoading?: boolean;
  /** Called when the user draws/clears a yellow marking. null = cleared. */
  onMark?: (mark: ImageMark | null) => void;
  /** Bump this number from the parent to clear the current marking (e.g. the chip's clear button). */
  clearMarkToken?: number;
  onClose: () => void;
}

// Dedicated viewer for image attachments. Mirrors the DocumentViewer's docked-card chrome but
// renders ONLY a single image (never RAG-indexed). It also lets the user draw a yellow marking
// to ask the model about a specific region: the marking is burned into a copy of the image plus
// a zoomed crop, which the vision model answers about (see imageViewer / markedRegion flow).
export default function ImageViewer({
  isOpen, filePath, title, initialContent, description, descriptionLoading, onMark, clearMarkToken, onClose,
}: ImageViewerProps) {
  const [descOpen, setDescOpen] = useState(false);
  const [markMode, setMarkMode] = useState(false);
  const [rect, setRect] = useState<NormRect | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const startRef = useRef<{ x: number; y: number } | null>(null);
  const rectRef = useRef<NormRect | null>(null);

  const fileName = title ?? filePath?.split('/').pop() ?? 'image';
  const src = initialContent || (filePath ? `/api/file?path=${encodeURIComponent(filePath)}` : '');

  const clearMark = useCallback(() => {
    setRect(null);
    rectRef.current = null;
    onMark?.(null);
  }, [onMark]);

  // New image → reset the marking, INCLUDING the parent's (onMark(null)) so a region drawn on
  // the previous image can never be sent (wrong-image vision + billing) or leave a ghost chip.
  // onMark is a stable setState setter, so it won't re-run this effect spuriously.
  useEffect(() => {
    setRect(null);
    rectRef.current = null;
    setMarkMode(false);
    onMark?.(null);
  }, [filePath, onMark]);

  // Parent asked to clear (chip's clear button).
  useEffect(() => {
    if (clearMarkToken === undefined) return;
    setRect(null);
    rectRef.current = null;
    setMarkMode(false);
  }, [clearMarkToken]);

  const _norm = (e: React.MouseEvent) => {
    const b = boxRef.current?.getBoundingClientRect();
    if (!b || !b.width || !b.height) return { x: 0, y: 0 };
    return {
      x: Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)),
      y: Math.min(1, Math.max(0, (e.clientY - b.top) / b.height)),
    };
  };

  const onDown = (e: React.MouseEvent) => {
    if (!markMode) return;
    e.preventDefault();
    startRef.current = _norm(e);
    const r = { ...startRef.current, w: 0, h: 0 };
    rectRef.current = r;
    setRect(r);
  };
  const onMove = (e: React.MouseEvent) => {
    if (!markMode || !startRef.current) return;
    const p = _norm(e);
    const s = startRef.current;
    const r = { x: Math.min(s.x, p.x), y: Math.min(s.y, p.y), w: Math.abs(p.x - s.x), h: Math.abs(p.y - s.y) };
    rectRef.current = r;
    setRect(r);
  };
  const onUp = () => {
    if (!markMode || !startRef.current) return;
    startRef.current = null;
    const r = rectRef.current;
    if (!r || r.w < 0.02 || r.h < 0.02) {
      clearMark();
      return;
    }
    const mark = buildMark(r);
    if (mark) onMark?.(mark);
  };

  // Burn the yellow rectangle into a full-res copy + produce a zoomed crop of the region.
  const buildMark = (r: NormRect): ImageMark | null => {
    const img = imgRef.current;
    if (!img || !img.naturalWidth || !img.naturalHeight) return null;
    try {
      const NW = img.naturalWidth, NH = img.naturalHeight;
      const rx = r.x * NW, ry = r.y * NH, rw = r.w * NW, rh = r.h * NH;

      // 1. Annotated full image (yellow box burned in).
      const c1 = document.createElement('canvas');
      c1.width = NW; c1.height = NH;
      const x1 = c1.getContext('2d');
      if (!x1) return null;
      x1.drawImage(img, 0, 0);
      x1.lineWidth = Math.max(3, Math.round(NW * 0.004));
      x1.strokeStyle = '#FFD400';
      x1.strokeRect(rx, ry, rw, rh);
      const annotated = c1.toDataURL('image/png');

      // 2. Zoomed crop of the region (+ padding), upscaled so small areas stay legible.
      const pad = 0.06;
      const cx = Math.max(0, r.x - pad) * NW;
      const cy = Math.max(0, r.y - pad) * NH;
      const cw = Math.min(1, r.x + r.w + pad) * NW - cx;
      const ch = Math.min(1, r.y + r.h + pad) * NH - cy;
      const scale = Math.min(3, Math.max(1, 640 / Math.max(1, Math.max(cw, ch))));
      const c2 = document.createElement('canvas');
      c2.width = Math.max(1, Math.round(cw * scale));
      c2.height = Math.max(1, Math.round(ch * scale));
      const x2 = c2.getContext('2d');
      if (!x2) return null;
      x2.drawImage(img, cx, cy, cw, ch, 0, 0, c2.width, c2.height);
      const crop = c2.toDataURL('image/png');

      return { name: fileName, annotated, crop };
    } catch {
      return null;  // tainted canvas / decode error — fail soft
    }
  };

  if (!isOpen) return null;

  return (
    <div className="relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA]">
      <div className="flex h-full w-full flex-col">
        {/* Header — mirrors the DocumentViewer's h-12 chrome */}
        <div className="flex h-12 items-center justify-between gap-3 border-b border-gray-200 bg-white px-4 shrink-0">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-emerald-600">
              <ImageIcon size={14} />
            </div>
            <div className="min-w-0">
              <div className="text-xs font-semibold text-gray-900">Image viewer</div>
              <div className="flex flex-wrap items-center gap-2 text-[10px] text-gray-500">
                <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500">
                  Image
                </span>
                <span className="truncate font-mono text-[11px] text-gray-600" title={filePath ?? fileName}>
                  {fileName}
                </span>
              </div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              onClick={() => setMarkMode(m => !m)}
              className={`rounded-full p-1.5 transition ${markMode ? 'bg-yellow-100 text-yellow-700' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600'}`}
              title={markMode ? 'Exit marking mode' : 'Mark a region and ask about it'}
            >
              <Highlighter size={15} />
            </button>
            {rect && (
              <button
                onClick={clearMark}
                className="rounded-full p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                title="Clear marking"
              >
                <Eraser size={15} />
              </button>
            )}
            <button
              onClick={handleDownload}
              disabled={!src}
              className="rounded-full p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600 disabled:cursor-not-allowed disabled:opacity-40"
              title="Download image"
            >
              <Download size={15} />
            </button>
            <button
              onClick={onClose}
              className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
              title="Close"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {markMode && (
          <div className="shrink-0 bg-yellow-50 px-4 py-1 text-[11px] text-yellow-800">
            Drag a rectangle over the area you want to know more about, then ask about it in the chat.
          </div>
        )}

        {/* Body — the image (with the marking overlay) on a neutral backdrop */}
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-[#EEF0F3] p-4">
          {src ? (
            <div ref={boxRef} className="relative inline-block max-h-full max-w-full leading-none">
              <img
                ref={imgRef}
                src={src}
                alt={fileName}
                className="block max-h-full max-w-full rounded object-contain shadow-sm"
                draggable={false}
              />
              {(markMode || rect) && (
                <div
                  className={`absolute inset-0 ${markMode ? 'cursor-crosshair' : 'pointer-events-none'}`}
                  onMouseDown={onDown}
                  onMouseMove={onMove}
                  onMouseUp={onUp}
                  onMouseLeave={onUp}
                >
                  {rect && rect.w > 0 && rect.h > 0 && (
                    <div
                      className="absolute border-2 border-yellow-400 bg-yellow-300/20"
                      style={{ left: `${rect.x * 100}%`, top: `${rect.y * 100}%`, width: `${rect.w * 100}%`, height: `${rect.h * 100}%` }}
                    />
                  )}
                </div>
              )}
            </div>
          ) : (
            <span className="text-sm text-gray-400">No image</span>
          )}
        </div>

        {/* Vision description — collapsible (display-only). It still goes to the agent every turn
            while this viewer is open (see imageViewerContext), independent of this toggle. */}
        {(descriptionLoading || description) && (
          <div className="shrink-0 border-t border-gray-200 bg-white">
            <button
              type="button"
              onClick={() => setDescOpen(o => !o)}
              className="flex w-full items-center gap-1.5 px-4 py-2 text-left text-[10px] font-semibold uppercase tracking-wide text-gray-500 transition hover:bg-gray-50"
              title={descOpen ? 'Collapse description' : 'Show description'}
            >
              <ChevronDown size={12} className={`shrink-0 transition-transform ${descOpen ? '' : '-rotate-90'}`} />
              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
              Vision description
              {descriptionLoading && <span className="ml-1 font-normal normal-case text-gray-400">· generating…</span>}
              {!descOpen && !descriptionLoading && description && (
                <span className="ml-auto max-w-[55%] truncate font-normal normal-case text-gray-400">{description}</span>
              )}
            </button>
            {descOpen && (
              <div className="max-h-48 select-text overflow-auto whitespace-pre-wrap px-4 pb-3 pt-0 text-xs leading-relaxed text-gray-700">
                {descriptionLoading ? 'Generating description…' : description}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );

  function handleDownload() {
    if (!src) return;
    const a = document.createElement('a');
    a.href = src;
    a.download = fileName;
    a.rel = 'noopener';
    a.click();
  }
}
