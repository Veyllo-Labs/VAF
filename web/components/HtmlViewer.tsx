'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useCallback } from 'react';
import dynamic from 'next/dynamic';
import { useThemeStore } from '@/lib/themeStore';

// Monaco is heavy — load it only on client side
const MonacoEditor = dynamic(() => import('@monaco-editor/react'), { ssr: false });

export function isHtmlFile(filePath: string): boolean {
  const ext = filePath.split('.').pop()?.toLowerCase() ?? '';
  return ext === 'html' || ext === 'htm';
}

// ── Types ─────────────────────────────────────────────────────────────────────
interface HtmlViewerProps {
  isOpen: boolean;
  filePath?: string;
  title?: string;
  /** Pre-loaded HTML content — skips the server fetch */
  initialContent?: string;
  onClose: () => void;
}

// ── Component ─────────────────────────────────────────────────────────────────
export default function HtmlViewer({ isOpen, filePath, title, initialContent, onClose }: HtmlViewerProps) {
  const [mode, setMode] = useState<'preview' | 'source'>('preview');
  const [content, setContent] = useState(initialContent ?? '');
  const [loadError, setLoadError] = useState<string | null>(null);
  const fileName = title ?? filePath?.split('/').pop() ?? 'file.html';
  const theme = useThemeStore((s) => s.theme);

  // ── Fetch file from backend ─────────────────────────────────────────────────
  const fetchContent = useCallback(async () => {
    if (!filePath) return;
    try {
      const res = await fetch(`/api/file?path=${encodeURIComponent(filePath)}`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setContent(await res.text());
      setLoadError(null);
    } catch (err) {
      setLoadError(String(err));
    }
  }, [filePath]);

  useEffect(() => {
    if (!isOpen) return;
    setMode('preview');
    setLoadError(null);
    if (initialContent !== undefined) {
      setContent(initialContent);
    } else if (filePath) {
      fetchContent();
    }
  }, [isOpen, filePath, initialContent]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Download ────────────────────────────────────────────────────────────────
  const handleDownload = () => {
    const blob = new Blob([content], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (!isOpen) return null;

  return (
    <div className="flex flex-col h-full w-full bg-[#1e1e1e] text-white overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-2 bg-[#2d2d2d] border-b border-[#3e3e3e] shrink-0">
        {/* Badge */}
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-orange-600/30 text-orange-300 uppercase shrink-0">
          html
        </span>

        {/* Filename */}
        <span className="flex-1 text-sm font-medium text-gray-200 truncate" title={filePath ?? fileName}>
          {fileName}
        </span>

        {/* Preview / Source toggle */}
        <div className="flex rounded overflow-hidden border border-[#4e4e4e] shrink-0">
          <button
            onClick={() => setMode('preview')}
            className={`text-xs px-2.5 py-1 transition-colors ${
              mode === 'preview'
                ? 'bg-orange-600 text-white'
                : 'bg-[#3e3e3e] text-gray-400 hover:text-gray-200'
            }`}
          >
            Preview
          </button>
          <button
            onClick={() => setMode('source')}
            className={`text-xs px-2.5 py-1 transition-colors ${
              mode === 'source'
                ? 'bg-orange-600 text-white'
                : 'bg-[#3e3e3e] text-gray-400 hover:text-gray-200'
            }`}
          >
            Source
          </button>
        </div>

        {/* Download button */}
        <button
          onClick={handleDownload}
          disabled={!content}
          className="flex items-center gap-1 text-xs px-2 py-1 rounded bg-[#3e3e3e] hover:bg-[#4e4e4e] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
          title="Download HTML file"
        >
          <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          Download
        </button>

        {/* Close button */}
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-[#3e3e3e] text-gray-400 hover:text-gray-200 transition-colors shrink-0"
          title="Close"
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      {/* ── Content area ───────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-hidden">
        {loadError ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-gray-400">
            <svg className="w-8 h-8 text-red-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <p className="text-sm">{loadError}</p>
            <button onClick={fetchContent} className="text-xs px-3 py-1.5 rounded bg-[#3e3e3e] hover:bg-[#4e4e4e] transition-colors">
              Retry
            </button>
          </div>
        ) : mode === 'preview' ? (
          <iframe
            srcDoc={content || (theme === 'dark'
              ? '<html><body style="font-family:sans-serif;color:#8b93a7;background:#0f131c;padding:2rem;margin:0">Loading…</body></html>'
              : '<html><body style="font-family:sans-serif;color:#888;padding:2rem">Loading…</body></html>')}
            title={fileName}
            className="w-full h-full border-0 bg-white"
            sandbox="allow-same-origin allow-scripts allow-forms"
            allow="microphone 'none'; camera 'none'"
          />
        ) : (
          <MonacoEditor
            height="100%"
            language="html"
            value={content}
            theme="vs-dark"
            options={{
              readOnly: true,
              fontSize: 13,
              fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              wordWrap: 'on',
              lineNumbers: 'on',
              renderWhitespace: 'selection',
              tabSize: 2,
              automaticLayout: true,
              padding: { top: 8, bottom: 8 },
            }}
          />
        )}
      </div>

      {/* ── Footer ─────────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-1 bg-[#2d2d2d] border-t border-[#3e3e3e] text-[10px] text-gray-500 shrink-0">
        <span className="flex-1 truncate font-mono">{filePath ?? fileName}</span>
        {content && <span>{content.length.toLocaleString()} chars</span>}
      </div>
    </div>
  );
}
