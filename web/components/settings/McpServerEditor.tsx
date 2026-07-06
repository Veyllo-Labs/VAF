'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * McpServerEditor
 * ===============
 * Two-column modal to add or edit an MCP server in mcp_servers.json:
 *   - Left: a form (name, transport, command/url, permission, enabled) + a Test-connection button.
 *   - Right: a standard MCP config block ({ "mcpServers": { name: { command, args, env } } }). Paste a
 *     block from anywhere and the form auto-fills; edit the form and the block regenerates. This is the
 *     de-facto format used by Claude Desktop / Cursor etc.
 * It does NOT do WS itself — the parent SettingsModal drives onSave / onDelete / onTest. Overlay z-[80].
 */

import React, { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { AlertCircle, CheckCircle2, Loader2, Save, Trash2, Wifi, X } from 'lucide-react';

export interface McpServerInfo {
  name: string;
  command: string;
  transport: string;            // "stdio" | "http" | "sse"
  url?: string;
  enabled: boolean;
  permission_level: string;     // "read" | "write" | "dangerous"
  env?: Record<string, string>;
  connected?: boolean;
  tool_count?: number;
  error?: string | null;
}

export interface McpServerEditorProps {
  server: McpServerInfo | null;   // null → create
  isSaving?: boolean;
  backendError?: string | null;
  onSave: (data: McpServerInfo) => void;
  onDelete?: (name: string) => void;
  onClose: () => void;
  onTest?: (cfg: { command: string; transport: string; url: string; env: Record<string, string> }) => void;
  testResult?: { connected: boolean; tool_count: number; tools?: string[]; error?: string | null } | null;
  isTesting?: boolean;
}

function splitCommand(cmd: string): { command: string; args: string[] } {
  const parts = (cmd || '').trim().split(/\s+/).filter(Boolean);
  return { command: parts[0] || '', args: parts.slice(1) };
}

function toStandardBlock(name: string, command: string, env: Record<string, string>): string {
  const { command: c, args } = splitCommand(command);
  const entry: Record<string, unknown> = { command: c, args };
  if (env && Object.keys(env).length) entry.env = env;
  return JSON.stringify({ mcpServers: { [name || 'server']: entry } }, null, 2);
}

function parseStandardBlock(text: string): { name?: string; command?: string; transport?: string; url?: string; env?: Record<string, string> } | null {
  let obj: any;
  try { obj = JSON.parse(text); } catch { return null; }
  if (!obj || typeof obj !== 'object') return null;
  let name: string | undefined;
  let cfg: any;
  if (obj.mcpServers && typeof obj.mcpServers === 'object') {
    name = Object.keys(obj.mcpServers)[0];
    cfg = obj.mcpServers[name as string];
  } else if (obj.servers && typeof obj.servers === 'object') {
    name = Object.keys(obj.servers)[0];
    cfg = obj.servers[name as string];
  } else {
    cfg = obj;
  }
  if (!cfg || typeof cfg !== 'object') return null;
  const command = [cfg.command, ...(Array.isArray(cfg.args) ? cfg.args : [])].filter(Boolean).join(' ');
  return {
    name,
    command,
    transport: cfg.transport || 'stdio',
    url: cfg.url || '',
    env: (cfg.env && typeof cfg.env === 'object') ? cfg.env : {},
  };
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
  const [env, setEnv] = useState<Record<string, string>>(server?.env ?? {});
  const [jsonText, setJsonText] = useState(() => toStandardBlock(server?.name ?? '', server?.command ?? '', server?.env ?? {}));
  const [jsonInvalid, setJsonInvalid] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const lastSource = useRef<'form' | 'json'>('form');

  const isStdio = transport === 'stdio';

  // Form → JSON (skip while the change came from the JSON panel, to avoid clobbering the user's text).
  useEffect(() => {
    if (lastSource.current === 'form') setJsonText(toStandardBlock(name, command, env));
  }, [name, command, env]);

  const onJsonChange = (text: string) => {
    lastSource.current = 'json';
    setJsonText(text);
    const parsed = parseStandardBlock(text);
    if (!parsed) { setJsonInvalid(true); return; }
    setJsonInvalid(false);
    if (!isEdit && parsed.name !== undefined) setName(parsed.name || '');
    if (parsed.command !== undefined) setCommand(parsed.command || '');
    if (parsed.transport) setTransport(parsed.transport);
    if (parsed.url !== undefined) setUrl(parsed.url || '');
    if (parsed.env) setEnv(parsed.env);
  };

  const F = (fn: () => void) => { lastSource.current = 'form'; fn(); };

  const validate = (): boolean => {
    setLocalError(null);
    if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(name.trim())) { setLocalError(t('errName')); return false; }
    if (isStdio && !command.trim()) { setLocalError(t('errCommand')); return false; }
    if (!isStdio && !url.trim()) { setLocalError(t('errUrl')); return false; }
    return true;
  };

  const handleSave = () => {
    if (!validate()) return;
    onSave({ name: name.trim(), command: command.trim(), transport, url: url.trim(), enabled, permission_level: permission, env });
  };

  const handleTest = () => {
    if (!validate()) return;
    onTest?.({ command: command.trim(), transport, url: url.trim(), env });
  };

  const inputCls = 'w-full px-4 h-11 bg-white border border-gray-200 rounded-xl text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-amber-400 focus:border-amber-500 transition-all';
  const labelCls = 'block text-xs font-semibold text-gray-600 mb-1.5';

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4 max-md:p-0" onClick={onClose}>
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
      <div className="relative bg-white w-full max-w-4xl aspect-[3/2] max-h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden max-md:max-w-none max-md:aspect-auto max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="h-16 border-b border-gray-100 flex items-center justify-between px-6 shrink-0">
          <h2 className="text-lg font-bold text-gray-800">{isEdit ? t('editTitle') : t('addTitle')}</h2>
          <button onClick={onClose} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
            <X size={20} />
          </button>
        </div>

        {/* Body: form (left) + JSON block (right) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 p-6 overflow-y-auto flex-1 min-h-0">
          {/* Left: form */}
          <div className="space-y-4">
            <div>
              <label className={labelCls}>{t('name')}</label>
              <input type="text" value={name} disabled={isEdit} onChange={(e) => F(() => setName(e.target.value))} placeholder="filesystem" className={`${inputCls} ${isEdit ? 'bg-gray-50 text-gray-500' : ''}`} />
              <p className="text-[11px] text-gray-400 mt-1">{t('nameHint')}</p>
            </div>

            <div>
              <label className={labelCls}>{t('transport')}</label>
              <select value={transport} onChange={(e) => F(() => setTransport(e.target.value))} className={inputCls}>
                <option value="stdio">{t('transportStdio')}</option>
                <option value="http">http</option>
                <option value="sse">sse</option>
              </select>
            </div>

            {isStdio ? (
              <div>
                <label className={labelCls}>{t('command')}</label>
                <input type="text" value={command} onChange={(e) => F(() => setCommand(e.target.value))} placeholder="npx -y @modelcontextprotocol/server-filesystem /path" className={inputCls} />
              </div>
            ) : (
              <div>
                <label className={labelCls}>{t('url')}</label>
                <input type="text" value={url} onChange={(e) => F(() => setUrl(e.target.value))} placeholder="http://127.0.0.1:8000" className={inputCls} />
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
          </div>

          {/* Right: standard config block */}
          <div className="flex flex-col">
            <label className={labelCls}>{t('pasteTitle')}</label>
            <textarea
              value={jsonText}
              onChange={(e) => onJsonChange(e.target.value)}
              spellCheck={false}
              className={`flex-1 min-h-[220px] w-full px-3 py-2 font-mono text-xs bg-gray-900 text-gray-100 rounded-xl border ${jsonInvalid ? 'border-red-400' : 'border-gray-700'} focus:outline-none focus:ring-2 focus:ring-amber-400`}
            />
            <p className="text-[11px] text-gray-400 mt-1">{jsonInvalid ? t('jsonInvalid') : t('pasteHint')}</p>
          </div>
        </div>

        {/* Test connection + errors */}
        <div className="px-6 pb-2 space-y-3">
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
            <button onClick={handleSave} disabled={isSaving} className="flex items-center gap-2 px-5 h-10 bg-amber-600 hover:bg-amber-700 text-white rounded-xl text-sm font-medium transition-colors disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none">
              {isSaving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} {t('save')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
