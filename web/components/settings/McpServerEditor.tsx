'use client';

/**
 * McpServerEditor
 * ===============
 * Modal form to add or edit an MCP server in mcp_servers.json. Mirrors CustomToolEditor's
 * overlay/callback contract (it does NOT do WS itself — the parent SettingsModal drives the WS via
 * onSave / onDelete / onTest). Renders in a full-screen overlay (z-[80]).
 */

import React, { useState } from 'react';
import { useTranslations } from 'next-intl';
import { AlertCircle, CheckCircle2, Loader2, Save, Trash2, Wifi, X } from 'lucide-react';

export interface McpServerInfo {
  name: string;
  command: string;
  transport: string;            // "stdio" | "http" | "sse"
  url?: string;
  enabled: boolean;
  permission_level: string;     // "read" | "write" | "dangerous"
  connected?: boolean;
  tool_count?: number;
  error?: string | null;
}

export interface McpServerEditorProps {
  /** null → create mode; otherwise edit mode for this server */
  server: McpServerInfo | null;
  isSaving?: boolean;
  backendError?: string | null;
  onSave: (data: McpServerInfo) => void;
  onDelete?: (name: string) => void;
  onClose: () => void;
  /** Probe the current form values without saving */
  onTest?: (cfg: { command: string; transport: string; url: string }) => void;
  testResult?: { connected: boolean; tool_count: number; tools?: string[]; error?: string | null } | null;
  isTesting?: boolean;
}

export default function McpServerEditor({ server, isSaving = false, backendError = null, onSave, onDelete, onClose, onTest, testResult = null, isTesting = false }: McpServerEditorProps) {
  const t = useTranslations('modals.mcp.editor');
  const isEdit = server !== null;
  const [name, setName] = useState(server?.name ?? '');
  const [command, setCommand] = useState(server?.command ?? '');
  const [transport, setTransport] = useState(server?.transport ?? 'stdio');
  const [url, setUrl] = useState(server?.url ?? '');
  const [permission, setPermission] = useState(server?.permission_level ?? 'write');
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [localError, setLocalError] = useState<string | null>(null);

  const isStdio = transport === 'stdio';

  const validate = (): boolean => {
    setLocalError(null);
    if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(name.trim())) { setLocalError(t('errName')); return false; }
    if (isStdio && !command.trim()) { setLocalError(t('errCommand')); return false; }
    if (!isStdio && !url.trim()) { setLocalError(t('errUrl')); return false; }
    return true;
  };

  const handleSave = () => {
    if (!validate()) return;
    onSave({ name: name.trim(), command: command.trim(), transport, url: url.trim(), enabled, permission_level: permission });
  };

  const handleTest = () => {
    if (!validate()) return;
    onTest?.({ command: command.trim(), transport, url: url.trim() });
  };

  const inputCls = 'w-full px-4 h-11 bg-white border border-gray-200 rounded-xl text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-amber-400 focus:border-amber-500 transition-all';
  const labelCls = 'block text-xs font-semibold text-gray-600 mb-1.5';

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4" onClick={onClose}>
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
      <div className="relative bg-white w-full max-w-lg rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="h-16 border-b border-gray-100 flex items-center justify-between px-6 shrink-0">
          <h2 className="text-lg font-bold text-gray-800">{isEdit ? t('editTitle') : t('addTitle')}</h2>
          <button onClick={onClose} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
            <X size={20} />
          </button>
        </div>

        {/* Body */}
        <div className="p-6 space-y-4 overflow-y-auto max-h-[70vh]">
          <div>
            <label className={labelCls}>{t('name')}</label>
            <input type="text" value={name} disabled={isEdit} onChange={(e) => setName(e.target.value)} placeholder="filesystem" className={`${inputCls} ${isEdit ? 'bg-gray-50 text-gray-500' : ''}`} />
            <p className="text-[11px] text-gray-400 mt-1">{t('nameHint')}</p>
          </div>

          <div>
            <label className={labelCls}>{t('transport')}</label>
            <select value={transport} onChange={(e) => setTransport(e.target.value)} className={inputCls}>
              <option value="stdio">{t('transportStdio')}</option>
              <option value="http">http</option>
              <option value="sse">sse</option>
            </select>
          </div>

          {isStdio ? (
            <div>
              <label className={labelCls}>{t('command')}</label>
              <input type="text" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx -y @modelcontextprotocol/server-filesystem /path" className={inputCls} />
            </div>
          ) : (
            <div>
              <label className={labelCls}>{t('url')}</label>
              <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://127.0.0.1:8000" className={inputCls} />
            </div>
          )}

          <div>
            <label className={labelCls}>{t('permission')}</label>
            <select value={permission} onChange={(e) => setPermission(e.target.value)} className={inputCls}>
              <option value="read">{t('permRead')}</option>
              <option value="write">{t('permWrite')}</option>
              <option value="dangerous">{t('permDangerous')}</option>
            </select>
            <p className="text-[11px] text-gray-400 mt-1">{t('permHint')}</p>
          </div>

          <label className="flex items-center justify-between p-3 bg-gray-50 rounded-xl border border-gray-100 cursor-pointer">
            <span className="text-sm font-medium text-gray-700">{t('enabled')}</span>
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 accent-amber-600" />
          </label>

          {/* Test connection */}
          {onTest && (
            <div className="flex items-center gap-3">
              <button onClick={handleTest} disabled={isTesting} className="flex items-center gap-2 px-4 h-10 bg-amber-50 text-amber-700 hover:bg-amber-100 rounded-xl text-sm font-medium transition-colors disabled:opacity-50">
                {isTesting ? <Loader2 size={16} className="animate-spin" /> : <Wifi size={16} />} {isTesting ? t('testing') : t('test')}
              </button>
              {!isTesting && testResult && (
                testResult.connected
                  ? <span className="flex items-center gap-1.5 text-sm text-green-600"><CheckCircle2 size={16} /> {t('testOk', { count: testResult.tool_count })}</span>
                  : <span className="flex items-center gap-1.5 text-sm text-red-600"><AlertCircle size={16} /> {testResult.error || t('testFail')}</span>
              )}
            </div>
          )}

          {(localError || backendError) && (
            <div className="flex items-start gap-2 p-3 bg-red-50 border border-red-100 rounded-xl text-sm text-red-600">
              <AlertCircle size={16} className="mt-0.5 shrink-0" />
              <span>{localError || backendError}</span>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="border-t border-gray-100 p-4 flex items-center justify-between shrink-0">
          {isEdit && onDelete ? (
            <button onClick={() => onDelete(server!.name)} disabled={isSaving} className="flex items-center gap-2 px-4 h-10 text-red-600 hover:bg-red-50 rounded-xl text-sm font-medium transition-colors disabled:opacity-50">
              <Trash2 size={16} /> {t('remove')}
            </button>
          ) : <span />}
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="px-4 h-10 text-gray-600 hover:bg-gray-100 rounded-xl text-sm font-medium transition-colors">{t('cancel')}</button>
            <button onClick={handleSave} disabled={isSaving} className="flex items-center gap-2 px-5 h-10 bg-amber-600 hover:bg-amber-700 text-white rounded-xl text-sm font-medium transition-colors disabled:opacity-50">
              {isSaving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} {t('save')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
