'use client';

import React, { useState, useEffect, useRef, useCallback } from 'react';
import dynamic from 'next/dynamic';

// Monaco is heavy — load it only on client side
const MonacoEditor = dynamic(() => import('@monaco-editor/react'), { ssr: false });

// ── Language detection ────────────────────────────────────────────────────────
const EXT_TO_LANG: Record<string, string> = {
  py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', tsx: 'typescript', jsx: 'javascript',
  html: 'html', htm: 'html', css: 'css', scss: 'scss', sass: 'scss',
  json: 'json', jsonc: 'json', yaml: 'yaml', yml: 'yaml',
  md: 'markdown', mdx: 'markdown',
  sh: 'shell', bash: 'shell', zsh: 'shell', fish: 'shell',
  sql: 'sql', xml: 'xml', svg: 'xml',
  go: 'go', rs: 'rust', java: 'java',
  cpp: 'cpp', cc: 'cpp', cxx: 'cpp', c: 'c', h: 'c',
  php: 'php', rb: 'ruby', lua: 'lua', r: 'r',
  toml: 'ini', ini: 'ini', cfg: 'ini', env: 'ini',
  tf: 'hcl', dockerfile: 'dockerfile', txt: 'plaintext', csv: 'plaintext',
};

function detectLanguage(filePath: string): string {
  const name = filePath.split('/').pop()?.toLowerCase() ?? '';
  if (name === 'dockerfile') return 'dockerfile';
  const ext = name.split('.').pop() ?? '';
  return EXT_TO_LANG[ext] ?? 'plaintext';
}

// ── Code-file extensions that should open in this viewer ─────────────────────
const CODE_EXTENSIONS = new Set(Object.keys(EXT_TO_LANG));
CODE_EXTENSIONS.add('dockerfile');
// txt and csv are data/text files → route to DocumentViewer, not CodeViewer
CODE_EXTENSIONS.delete('txt');
CODE_EXTENSIONS.delete('csv');

export function isCodeFile(filePath: string): boolean {
  const name = filePath.split('/').pop()?.toLowerCase() ?? '';
  if (name === 'dockerfile') return true;
  const ext = name.split('.').pop() ?? '';
  return CODE_EXTENSIONS.has(ext);
}

// ── Types ─────────────────────────────────────────────────────────────────────
export interface CodeViewerProps {
  isOpen: boolean;
  filePath: string;
  title?: string;
  /** Pre-loaded content — skips the server fetch (used for browser-attached files without a real path) */
  initialContent?: string;
  /** When true, polls the file every 2 s and shows a LIVE badge */
  liveRefresh?: boolean;
  onClose: () => void;
  /** Called whenever the displayed content changes (used to give the agent context) */
  onContentLoad?: (content: string) => void;
}

// ── Component ─────────────────────────────────────────────────────────────────
export default function CodeViewer({ isOpen, filePath, title, initialContent, liveRefresh = false, onClose, onContentLoad }: CodeViewerProps) {
  const [content, setContent] = useState('');
  const [isDirty, setIsDirty] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<Date | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [lastFetched, setLastFetched] = useState<Date | null>(null);
  const editorRef = useRef<unknown>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const language = detectLanguage(filePath);
  const fileName = filePath.split('/').pop() ?? filePath;

  // ── Fetch file from backend ─────────────────────────────────────────────────
  const fetchContent = useCallback(async (silent = false) => {
    if (!filePath) return;
    try {
      const res = await fetch(`/api/file?path=${encodeURIComponent(filePath)}`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const text = await res.text();
      if (!silent || !isDirty) {
        setContent(text);
        setLastFetched(new Date());
        setLoadError(null);
        onContentLoad?.(text);
      }
    } catch (err) {
      if (!silent) setLoadError(String(err));
    }
  }, [filePath, isDirty, onContentLoad]);

  // Initial load + live polling
  useEffect(() => {
    if (!isOpen) return;
    setIsDirty(false);
    setSavedAt(null);
    setLoadError(null);
    if (initialContent !== undefined) {
      // Content supplied directly (e.g. browser-attached file without server path)
      setContent(initialContent);
      setLastFetched(new Date());
      onContentLoad?.(initialContent);
    } else if (filePath) {
      fetchContent();
    }
  }, [isOpen, filePath, initialContent]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (isOpen && liveRefresh) {
      pollRef.current = setInterval(() => fetchContent(true), 2000);
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [isOpen, liveRefresh, fetchContent]);

  // ── Save ────────────────────────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!filePath || !isDirty) return;
    setIsSaving(true);
    try {
      const res = await fetch('/api/file/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: filePath, content }),
      });
      if (!res.ok) throw new Error(await res.text());
      setIsDirty(false);
      setSavedAt(new Date());
    } catch (err) {
      alert(`Save failed: ${err}`);
    } finally {
      setIsSaving(false);
    }
  }, [filePath, content, isDirty]);

  // Ctrl+S
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's' && isOpen) {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, handleSave]);

  if (!isOpen) return null;

  return (
    <div className="flex flex-col h-full w-full bg-[#1e1e1e] text-white overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-2 bg-[#2d2d2d] border-b border-[#3e3e3e] shrink-0">
        {/* Language icon / badge */}
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[#3e3e3e] text-gray-400 uppercase shrink-0">
          {language}
        </span>

        {/* Filename */}
        <span className="flex-1 text-sm font-medium text-gray-200 truncate" title={filePath}>
          {title ?? fileName}
        </span>

        {/* LIVE badge */}
        {liveRefresh && (
          <span className="flex items-center gap-1 text-[10px] font-medium text-emerald-400 shrink-0">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            LIVE
          </span>
        )}

        {/* Saved indicator */}
        {savedAt && !isDirty && (
          <span className="text-[10px] text-emerald-400 shrink-0">Saved</span>
        )}

        {/* Save button */}
        <button
          onClick={handleSave}
          disabled={!isDirty || isSaving}
          className="flex items-center gap-1 text-xs px-2 py-1 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
          title="Save (Ctrl+S)"
        >
          {isSaving ? (
            <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" opacity=".25"/><path d="M12 2a10 10 0 0 1 10 10" /></svg>
          ) : (
            <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          )}
          Save
        </button>

        {/* Download button */}
        <button
          onClick={() => {
            const blob = new Blob([content], { type: 'text/plain' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = fileName;
            a.click();
            URL.revokeObjectURL(url);
          }}
          disabled={!content}
          className="flex items-center gap-1 text-xs px-2 py-1 rounded bg-[#3e3e3e] hover:bg-[#4e4e4e] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
          title="Download file"
        >
          <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Download
        </button>

        {/* Refresh button */}
        <button
          onClick={() => fetchContent()}
          className="p-1 rounded hover:bg-[#3e3e3e] text-gray-400 hover:text-gray-200 transition-colors shrink-0"
          title="Reload from disk"
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        </button>

        {/* Close button */}
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-[#3e3e3e] text-gray-400 hover:text-gray-200 transition-colors shrink-0"
          title="Close"
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>

      {/* ── Editor area ────────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-hidden">
        {loadError ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-gray-400">
            <svg className="w-8 h-8 text-red-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            <p className="text-sm">{loadError}</p>
            <button onClick={() => fetchContent()} className="text-xs px-3 py-1.5 rounded bg-[#3e3e3e] hover:bg-[#4e4e4e] transition-colors">Retry</button>
          </div>
        ) : (
          <MonacoEditor
            height="100%"
            language={language}
            value={content}
            theme="vs-dark"
            onChange={(val) => {
              setContent(val ?? '');
              setIsDirty(true);
              setSavedAt(null);
            }}
            onMount={(editor) => { editorRef.current = editor; }}
            options={{
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
        <span className="flex-1 truncate font-mono">{filePath}</span>
        {lastFetched && (
          <span>Updated {lastFetched.toLocaleTimeString()}</span>
        )}
        {isDirty && <span className="text-yellow-400">● Unsaved changes</span>}
      </div>
    </div>
  );
}
