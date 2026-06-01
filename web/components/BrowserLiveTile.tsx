"use client";

import React from 'react';
import { Globe, X } from 'lucide-react';

/**
 * Live browser view tiled next to another right-docked window (the Workflow Runtime). When a
 * browser sub-agent runs inside a workflow there are two visual windows; instead of overlapping,
 * this docks the browser feed immediately to the LEFT of the runtime window (rightOffset = its
 * width). Shows only while a frame is available.
 */
interface BrowserLiveTileProps {
  frame?: string;
  url?: string;
  agentName?: string;
  /** px to inset from the right edge — set to the width of the window it tiles beside. */
  rightOffset?: number;
  onClose?: () => void;
}

export default function BrowserLiveTile({
  frame,
  url,
  agentName = 'Browser Agent',
  rightOffset = 0,
  onClose,
}: BrowserLiveTileProps) {
  if (!frame) return null;

  return (
    <div
      className="fixed top-0 z-40 hidden xl:flex h-screen w-[460px] max-w-[40vw] flex-col border-l border-gray-200 bg-white shadow-2xl"
      style={{ right: rightOffset }}
    >
      {/* Header — matches the Workflow Runtime header height (h-16) for a clean tiled look */}
      <div className="h-16 shrink-0 border-b border-gray-200 flex items-center justify-between px-4 bg-white">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-sky-50 flex items-center justify-center text-sky-600 shrink-0">
            <Globe size={16} />
          </div>
          <div className="min-w-0">
            <h2 className="font-bold text-gray-900 text-sm truncate">{agentName}</h2>
            <div className="flex items-center gap-1.5 text-xs text-gray-500">
              <span className="flex h-2 w-2 relative">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
              </span>
              <span>Live</span>
            </div>
          </div>
        </div>
        {onClose && (
          <button
            onClick={onClose}
            className="rounded-full p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        )}
      </div>

      {/* URL bar */}
      <div className="shrink-0 px-3 py-1.5 border-b border-gray-100 bg-gray-50">
        <div className="truncate font-mono text-[11px] text-gray-500">{url || 'Loading…'}</div>
      </div>

      {/* Live frame */}
      <div className="flex-1 min-h-0 overflow-auto bg-[#1e1e1e] flex items-start justify-center">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={`data:image/jpeg;base64,${frame}`}
          alt="Browser live view"
          className="w-full h-auto object-contain"
        />
      </div>
    </div>
  );
}
