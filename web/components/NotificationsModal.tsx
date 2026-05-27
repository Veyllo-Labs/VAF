'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useTranslations } from 'next-intl';
import { X, RefreshCw, ChevronDown, ChevronRight, Activity, Search } from 'lucide-react';
import { cn } from '@/lib/utils';
import { getApiBase } from '@/lib/utils';

// ─── Types ────────────────────────────────────────────────────────────────────

export type NotificationItem = {
  id: string;
  kind: 'thinking' | 'automation' | 'channel_reply' | 'system';
  title: string;
  status: 'success' | 'skipped' | 'error';
  timestamp: string;
  summary?: string;
  sessionId?: string;
  channel?: string;
  task_name?: string;
  run_id?: string;
  action?: 'approve' | 'reject' | string;
  task_id?: string;
  handoff_id?: string;
  automation_action_result?: {
    ok?: boolean;
    operation?: string;
    task_id?: string;
    error?: string;
  };
};

type LogFile = {
  filename: string;
  domain: string;
  date: string;
  size_bytes: number;
  modified: number;
};

export interface NotificationsModalProps {
  isOpen: boolean;
  onClose: () => void;
  notifications: NotificationItem[];
  onFetchComplete?: (list: NotificationItem[]) => void;
}

// ─── Domain color dots ─────────────────────────────────────────────────────────

const DOMAIN_COLOR: Record<string, string> = {
  rag:              'bg-blue-500',
  memory:           'bg-purple-500',
  backend:          'bg-orange-500',
  prompt:           'bg-green-500',
  headless:         'bg-gray-400',
  attach:           'bg-yellow-500',
  tool_use:         'bg-cyan-500',
  webui:            'bg-pink-500',
  vaf_think:        'bg-indigo-500',
  telegram_reply:   'bg-sky-500',
  discord_reply:    'bg-violet-500',
  whatsapp_qr:      'bg-emerald-500',
  whatsapp_inbound: 'bg-teal-500',
  whatsapp_reply:   'bg-green-600',
};

// ─── Helpers ───────────────────────────────────────────────────────────────────

function formatRelativeTime(iso: string): string {
  try {
    const d = new Date(iso);
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return d.toLocaleDateString(undefined, { dateStyle: 'short' });
  } catch { return iso; }
}

/** Split "2026-05-27T14:23:45.123" into { ts, rest } for coloured display. */
function parseLogLine(line: string): { ts: string; rest: string } {
  const m = line.match(/^(\d{4}-\d{2}-\d{2}T[\d:.]+)\s+([\s\S]*)$/);
  return m ? { ts: m[1].replace('T', ' '), rest: m[2] } : { ts: '', rest: line };
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function NotificationsModal({
  isOpen,
  onClose,
  notifications,
  onFetchComplete,
}: NotificationsModalProps) {
  const t = useTranslations('notifications');

  const [selectedSource, setSelectedSource] = useState<'activity' | string>('activity');
  const [logFiles, setLogFiles]             = useState<LogFile[]>([]);
  const [loadingFiles, setLoadingFiles]     = useState(false);
  const [logLines, setLogLines]             = useState<string[]>([]);
  const [totalLines, setTotalLines]         = useState(0);
  const [loadingContent, setLoadingContent] = useState(false);
  const [searchQuery, setSearchQuery]       = useState('');
  const [autoRefresh, setAutoRefresh]       = useState(false);
  const [autoScroll, setAutoScroll]         = useState(true);
  const [expandedId, setExpandedId]         = useState<string | null>(null);

  const contentRef      = useRef<HTMLDivElement>(null);
  const autoRefreshRef  = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Data fetchers ────────────────────────────────────────────────────────────

  const fetchFiles = useCallback(() => {
    setLoadingFiles(true);
    fetch(`${getApiBase()}/api/logs`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { files: [] })
      .then(d => setLogFiles(Array.isArray(d?.files) ? d.files : []))
      .catch(() => setLogFiles([]))
      .finally(() => setLoadingFiles(false));
  }, []);

  const fetchContent = useCallback((filename: string) => {
    setLoadingContent(true);
    fetch(`${getApiBase()}/api/logs/${encodeURIComponent(filename)}?tail=500`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { lines: [], total_lines: 0 })
      .then(d => {
        setLogLines(Array.isArray(d?.lines) ? d.lines : []);
        setTotalLines(d?.total_lines ?? 0);
      })
      .catch(() => setLogLines([]))
      .finally(() => setLoadingContent(false));
  }, []);

  const fetchActivity = useCallback(() => {
    fetch(`${getApiBase()}/api/notifications?limit=50`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { notifications: [] })
      .then(d => onFetchComplete?.(Array.isArray(d?.notifications) ? d.notifications : []))
      .catch(() => onFetchComplete?.([]));
  }, [onFetchComplete]);

  // ── Effects ──────────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!isOpen) return;
    fetchFiles();
    fetchActivity();
  }, [isOpen, fetchFiles, fetchActivity]);

  useEffect(() => {
    if (!isOpen || selectedSource === 'activity') return;
    fetchContent(selectedSource);
    setSearchQuery('');
  }, [isOpen, selectedSource, fetchContent]);

  // Auto-scroll terminal to bottom
  useEffect(() => {
    if (autoScroll && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight;
    }
  }, [logLines, autoScroll]);

  // Auto-refresh interval
  useEffect(() => {
    if (autoRefreshRef.current) { clearInterval(autoRefreshRef.current); autoRefreshRef.current = null; }
    if (autoRefresh && isOpen && selectedSource !== 'activity') {
      autoRefreshRef.current = setInterval(() => fetchContent(selectedSource), 5000);
    }
    return () => { if (autoRefreshRef.current) clearInterval(autoRefreshRef.current); };
  }, [autoRefresh, isOpen, selectedSource, fetchContent]);

  // ESC key
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') { onClose(); e.preventDefault(); } };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  // ── Derived ──────────────────────────────────────────────────────────────────

  // Group files by domain, keep only the most recent per domain
  const domainLatest = logFiles.reduce<Record<string, LogFile>>((acc, f) => {
    if (!acc[f.domain] || f.modified > acc[f.domain].modified) acc[f.domain] = f;
    return acc;
  }, {});
  const sortedDomains = Object.keys(domainLatest).sort();

  const filteredLines = searchQuery.trim()
    ? logLines.filter(l => l.toLowerCase().includes(searchQuery.toLowerCase()))
    : logLines;

  const sortedNotifications = [...notifications].sort(
    (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
  );

  function highlight(text: string) {
    if (!searchQuery.trim()) return <>{text}</>;
    const q = searchQuery.toLowerCase();
    const idx = text.toLowerCase().indexOf(q);
    if (idx === -1) return <>{text}</>;
    return (
      <>
        {text.slice(0, idx)}
        <mark className="bg-yellow-300/80 text-gray-900 rounded-sm">{text.slice(idx, idx + searchQuery.length)}</mark>
        {text.slice(idx + searchQuery.length)}
      </>
    );
  }

  const statusBadge = (status: string) => {
    if (status === 'success') return 'bg-green-50 text-green-700 border-green-200';
    if (status === 'skipped') return 'bg-gray-100 text-gray-500 border-gray-200';
    return 'bg-red-50 text-red-600 border-red-200';
  };

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
      <div
        className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div className="shrink-0 px-4 py-3 border-b border-gray-200 flex items-center gap-2.5 bg-white">
          <h2 className="text-base font-bold text-gray-900 shrink-0">{t('title')}</h2>

          {selectedSource !== 'activity' && (
            <>
              <span className="text-gray-300 shrink-0">/</span>
              <span className="text-sm text-gray-500 font-mono truncate min-w-0">{selectedSource}</span>
              {totalLines > 500 && (
                <span className="text-[11px] text-gray-400 shrink-0 ml-1">
                  {t('linesOf', { tail: Math.min(500, filteredLines.length), total: totalLines })}
                </span>
              )}
            </>
          )}

          <div className="flex-1 min-w-0" />

          {/* Search — only for file view */}
          {selectedSource !== 'activity' && (
            <div className="relative shrink-0">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              <input
                type="text"
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                placeholder={t('searchPlaceholder')}
                className="pl-7 pr-3 py-1.5 text-xs border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-400 bg-gray-50 w-44"
              />
            </div>
          )}

          {/* Live toggle */}
          {selectedSource !== 'activity' && (
            <button
              type="button"
              onClick={() => setAutoRefresh(v => !v)}
              title={t('autoRefreshTitle')}
              className={cn(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors shrink-0',
                autoRefresh ? 'bg-green-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              )}
            >
              <span className={cn('w-1.5 h-1.5 rounded-full shrink-0', autoRefresh ? 'bg-white animate-pulse' : 'bg-gray-400')} />
              {t('autoRefresh')}
            </button>
          )}

          {/* Manual refresh */}
          {selectedSource !== 'activity' && (
            <button
              type="button"
              onClick={() => fetchContent(selectedSource)}
              disabled={loadingContent}
              title={t('refresh')}
              className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700 shrink-0 disabled:opacity-50"
            >
              <RefreshCw size={15} className={loadingContent ? 'animate-spin' : ''} />
            </button>
          )}

          <button
            onClick={onClose}
            title={t('close')}
            className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700 shrink-0"
          >
            <X size={18} />
          </button>
        </div>

        {/* ── Body ── */}
        <div className="flex-1 flex min-h-0">

          {/* ── Left sidebar ── */}
          <div className="w-44 shrink-0 border-r border-gray-100 flex flex-col min-h-0 bg-gray-50/60">
            <div className="flex-1 overflow-auto py-2 px-1.5 space-y-0.5">

              {/* Activity */}
              <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold px-2 pt-1 pb-0.5">
                {t('sectionActivity')}
              </p>
              <button
                type="button"
                onClick={() => setSelectedSource('activity')}
                className={cn(
                  'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                  selectedSource === 'activity'
                    ? 'bg-gray-900 text-white'
                    : 'text-gray-700 hover:bg-gray-200'
                )}
              >
                <Activity size={13} className="shrink-0" />
                <span className="flex-1 truncate">{t('activityLabel')}</span>
                {notifications.length > 0 && (
                  <span className={cn(
                    'text-[10px] font-bold px-1.5 py-0.5 rounded-full shrink-0',
                    selectedSource === 'activity' ? 'bg-white/20' : 'bg-gray-200 text-gray-500'
                  )}>
                    {notifications.length}
                  </span>
                )}
              </button>

              {/* Log files */}
              <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold px-2 pt-3 pb-0.5">
                {t('sectionFiles')}
              </p>

              {loadingFiles ? (
                <p className="text-xs text-gray-400 px-2.5 py-1">{t('loading')}</p>
              ) : sortedDomains.length === 0 ? (
                <p className="text-xs text-gray-400 px-2.5 py-1 whitespace-pre-line">{t('noFiles')}</p>
              ) : (
                sortedDomains.map(domain => {
                  const file = domainLatest[domain];
                  const isSelected = selectedSource === file.filename;
                  return (
                    <button
                      key={domain}
                      type="button"
                      onClick={() => setSelectedSource(file.filename)}
                      className={cn(
                        'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                        isSelected ? 'bg-gray-900 text-white' : 'text-gray-700 hover:bg-gray-200'
                      )}
                    >
                      <span className={cn('w-2 h-2 rounded-full shrink-0', DOMAIN_COLOR[domain] ?? 'bg-gray-400')} />
                      <span className="flex-1 truncate font-mono text-xs">{domain}</span>
                      {file.date && (
                        <span className={cn(
                          'text-[9px] shrink-0',
                          isSelected ? 'text-white/50' : 'text-gray-400'
                        )}>
                          {file.date.slice(5)} {/* MM-DD */}
                        </span>
                      )}
                    </button>
                  );
                })
              )}
            </div>

            {/* Auto-scroll checkbox */}
            {selectedSource !== 'activity' && (
              <div className="shrink-0 px-3 py-2.5 border-t border-gray-200">
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={autoScroll}
                    onChange={e => setAutoScroll(e.target.checked)}
                    className="rounded border-gray-300 text-gray-900 focus:ring-gray-400 w-3.5 h-3.5"
                  />
                  <span className="text-xs text-gray-500">{t('autoScroll')}</span>
                </label>
              </div>
            )}
          </div>

          {/* ── Content ── */}
          <div className="flex-1 min-w-0 flex flex-col min-h-0">
            {selectedSource === 'activity' ? (
              /* Activity list */
              <div className="flex-1 overflow-auto p-4">
                {sortedNotifications.length === 0 ? (
                  <p className="text-sm text-gray-400">{t('empty')}</p>
                ) : (
                  <ul className="space-y-1.5">
                    {sortedNotifications.map(item => (
                      <li key={item.id} className="rounded-xl border border-gray-200 bg-white overflow-hidden">
                        <div
                          className="flex items-center gap-3 px-3 py-2.5 cursor-pointer hover:bg-gray-50 transition-colors"
                          onClick={() => setExpandedId(expandedId === item.id ? null : item.id)}
                        >
                          <span className={cn('shrink-0 text-xs font-medium px-2 py-0.5 rounded border', statusBadge(item.status))}>
                            {item.status === 'success' ? t('statusSuccess') : item.status === 'skipped' ? t('statusSkipped') : t('statusError')}
                          </span>
                          <span className="shrink-0 text-[10px] font-bold uppercase tracking-tight text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">
                            {item.kind}
                          </span>
                          <div className="flex-1 min-w-0">
                            <p className="font-medium text-gray-900 truncate text-sm">{item.title}</p>
                            {item.summary && (
                              <p className="text-xs text-gray-500 truncate mt-0.5">
                                {item.summary.split('\n').find(Boolean)?.trim()}
                              </p>
                            )}
                          </div>
                          <span className="shrink-0 text-xs text-gray-400">{formatRelativeTime(item.timestamp)}</span>
                          {expandedId === item.id
                            ? <ChevronDown size={14} className="shrink-0 text-gray-400" />
                            : <ChevronRight size={14} className="shrink-0 text-gray-400" />}
                        </div>
                        {expandedId === item.id && item.summary && (
                          <div className="border-t border-gray-100 px-3 pb-3">
                            <pre className="mt-2 text-xs text-gray-700 bg-gray-50 rounded-lg p-3 border border-gray-100 whitespace-pre-wrap font-mono overflow-auto max-h-52">
                              {item.summary}
                            </pre>
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              /* Terminal log viewer */
              <div
                ref={contentRef}
                className="flex-1 overflow-auto bg-[#0d1117] font-mono text-xs leading-5 select-text"
              >
                {loadingContent && logLines.length === 0 ? (
                  <p className="text-gray-500 p-4">{t('loading')}</p>
                ) : filteredLines.length === 0 ? (
                  <p className="text-gray-500 p-4">{searchQuery ? t('noResults') : t('emptyFile')}</p>
                ) : (
                  filteredLines.map((line, i) => {
                    const { ts, rest } = parseLogLine(line);
                    const isContinuation = line.startsWith('    ') && !ts;
                    return (
                      <div
                        key={i}
                        className={cn(
                          'flex gap-0 hover:bg-white/[0.03] px-0',
                          isContinuation && 'opacity-70'
                        )}
                      >
                        {/* Line number */}
                        <span className="text-[#3d444d] select-none text-right pr-3 pl-4 py-px w-12 shrink-0 tabular-nums">
                          {i + 1}
                        </span>
                        {/* Timestamp */}
                        {ts ? (
                          <span className="text-[#539bf5] pr-3 py-px whitespace-nowrap shrink-0">
                            {ts}
                          </span>
                        ) : (
                          <span className="w-[168px] shrink-0" />
                        )}
                        {/* Message */}
                        <span className="text-[#adbac7] py-px pr-4 break-all">
                          {highlight(rest || line)}
                        </span>
                      </div>
                    );
                  })
                )}
                {/* Padding at bottom so last line isn't flush against edge */}
                <div className="h-6" />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
