'use client';

import React, { useState, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { X, ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { getApiBase } from '@/lib/utils';

export type NotificationItem = {
  id: string;
  kind: 'thinking' | 'automation' | 'channel_reply';
  title: string;
  status: 'success' | 'skipped' | 'error';
  timestamp: string;
  summary?: string;
  sessionId?: string;
  channel?: string;
  task_name?: string;
  run_id?: string;
};

function formatRelativeTime(iso: string): string {
  try {
    const date = new Date(iso);
    const now = new Date();
    const sec = Math.floor((now.getTime() - date.getTime()) / 1000);
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    if (sec < 604800) return `${Math.floor(sec / 86400)}d ago`;
    return date.toLocaleDateString(undefined, { dateStyle: 'short' });
  } catch {
    return iso;
  }
}

export interface NotificationsModalProps {
  isOpen: boolean;
  onClose: () => void;
  notifications: NotificationItem[];
  onFetchComplete?: (list: NotificationItem[]) => void;
}

export default function NotificationsModal({
  isOpen,
  onClose,
  notifications,
  onFetchComplete,
}: NotificationsModalProps) {
  const t = useTranslations('notifications');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (expandedId) setExpandedId(null);
        else onClose();
        e.preventDefault();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, expandedId, onClose]);

  useEffect(() => {
    if (!isOpen) return;
    setLoading(true);
    fetch(`${getApiBase()}/api/notifications?limit=50`, { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : { notifications: [] }))
      .then((data) => {
        const list = Array.isArray(data?.notifications) ? data.notifications : [];
        onFetchComplete?.(list);
      })
      .catch(() => onFetchComplete?.([]))
      .finally(() => setLoading(false));
  }, [isOpen, onFetchComplete]);

  if (!isOpen) return null;

  const list = [...notifications].sort(
    (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
  );

  const statusLabel = (status: string) => {
    if (status === 'success') return t('statusSuccess');
    if (status === 'skipped') return t('statusSkipped');
    return t('statusError');
  };

  const statusColor = (status: string) => {
    if (status === 'success') return 'text-green-600 bg-green-50';
    if (status === 'skipped') return 'text-gray-500 bg-gray-100';
    return 'text-red-600 bg-red-50';
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
      <div
        className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="shrink-0 px-4 py-3 border-b border-gray-200 flex items-center justify-between">
          <h2 className="text-lg font-bold text-gray-900 truncate">
            {t('title')} {list.length > 0 && `(${list.length})`}
          </h2>
          <button
            onClick={onClose}
            className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
            title={t('close')}
          >
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-auto p-4">
          {loading && list.length === 0 ? (
            <p className="text-sm text-gray-500">Loading…</p>
          ) : list.length === 0 ? (
            <p className="text-sm text-gray-500">{t('empty')}</p>
          ) : (
            <ul className="space-y-1">
              {list.map((item) => (
                <li key={item.id} className="rounded-xl border border-gray-200 bg-gray-50/80 overflow-hidden">
                  <div
                    className="flex items-center gap-3 p-3 cursor-pointer hover:bg-gray-100/80 transition-colors"
                    onClick={() => setExpandedId(expandedId === item.id ? null : item.id)}
                  >
                    <span
                      className={cn(
                        'shrink-0 text-xs font-medium uppercase px-2 py-0.5 rounded',
                        statusColor(item.status)
                      )}
                    >
                      {statusLabel(item.status)}
                    </span>
                    <span className="flex-1 min-w-0 font-medium text-gray-900 truncate">{item.title}</span>
                    <span className="shrink-0 text-xs text-gray-500">{formatRelativeTime(item.timestamp)}</span>
                    <span className="shrink-0 text-gray-500" aria-hidden>
                      {expandedId === item.id ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                    </span>
                  </div>
                  {expandedId === item.id && (item.summary || item.channel) && (
                    <div className="px-3 pb-3 pt-0 border-t border-gray-100">
                      <div className="mt-2 text-sm text-gray-700 whitespace-pre-wrap bg-white rounded-lg p-3 border border-gray-100">
                        {item.channel && (
                          <p className="text-xs text-gray-500 mb-1">Channel: {item.channel}</p>
                        )}
                        {item.summary}
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
