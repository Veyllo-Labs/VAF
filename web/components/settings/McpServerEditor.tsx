'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * McpServerEditor
 * ===============
 * Two-column modal to add or edit an MCP server in mcp_servers.json:
 *   - Left: a form (name, transport, command/url, permission, enabled) + a Test-connection button.
 *   - Right: a standard MCP config block ({ "mcpServers": { name: { command, args, env } } }, or
 *     { url, type, headers } for a remote server). Paste a block from anywhere and the form auto-fills;
 *     edit the form and the block regenerates. This is the de-facto format used by Claude Desktop /
 *     Cursor etc.
 * Secrets never come back from the server (vaf/core/mcp_secrets.py): env values arrive empty and an
 * empty value keeps the stored one, a remote server's token field starts empty with a "stored" note.
 * A remote server either takes one token for everybody or signs every account in on its own
 * (`auth: "oauth"`, vaf/core/mcp_oauth.py); then the form offers an optional hand-registered client
 * (ID, write-only secret) and shows the redirect address such a client needs (GET /api/mcp/sign-in).
 * It does NOT do WS itself — the parent SettingsModal drives onSave / onDelete / onTest. Overlay z-[80].
 */

import React, { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { AlertCircle, CheckCircle2, Loader2, Save, Trash2, Wifi, X } from 'lucide-react';
import { getApiBase } from '@/lib/utils';

export interface McpServerInfo {
  name: string;
  command: string;
  transport: string;            // "stdio" | "http" | "sse"
  url?: string;
  enabled: boolean;
  permission_level: string;     // "read" | "write" | "dangerous"
  env?: Record<string, string>;
  /** Remote servers: whether an access token is stored (the token itself never comes back). */
  token_set?: boolean;
  /** Sent on save only: a new access token, or clear_token to remove the stored one. */
  token?: string;
  clear_token?: boolean;
  /** "oauth": every account signs in on its own (a remote server only); "" otherwise. */
  auth?: string;
  /** A client registered at the service by hand; its secret is write-only like the token. */
  oauth_client_id?: string;
  oauth_client_secret_set?: boolean;
  oauth_client_secret?: string;
  clear_oauth_client_secret?: boolean;
  /** How many accounts have signed in (a server with a sign-in per account). */
  accounts_signed_in?: number;
  /** Discovery found no signed-in account to list the tools with. */
  sign_in_required?: boolean;
  connected?: boolean;
  tool_count?: number;
  error?: string | null;
}

export interface McpTestResult {
  connected: boolean;
  tool_count: number;
  tools?: string[];
  error?: string | null;
  /** The server answered, and wants every account to sign in: expected before the first sign-in. */
  sign_in_required?: boolean;
}

export interface McpServerEditorProps {
  server: McpServerInfo | null;   // null → create
  isSaving?: boolean;
  backendError?: string | null;
  onSave: (data: McpServerInfo) => void;
  onDelete?: (name: string) => void;
  onClose: () => void;
  onTest?: (cfg: McpTestConfig) => void;
  testResult?: McpTestResult | null;
  isTesting?: boolean;
}

export interface McpTestConfig {
  name?: string;
  command: string;
  transport: string;
  url: string;
  env: Record<string, string>;
  token?: string;
  auth?: string;
}

const REMOTE = ['http', 'sse'];

function splitCommand(cmd: string): { command: string; args: string[] } {
  const parts = (cmd || '').trim().split(/\s+/).filter(Boolean);
  return { command: parts[0] || '', args: parts.slice(1) };
}

function toStandardBlock(name: string, command: string, env: Record<string, string>, transport = 'stdio', url = ''): string {
  if (REMOTE.includes(transport)) {
    return JSON.stringify({ mcpServers: { [name || 'server']: { type: transport, url } } }, null, 2);
  }
  const { command: c, args } = splitCommand(command);
  const entry: Record<string, unknown> = { command: c, args };
  if (env && Object.keys(env).length) entry.env = env;
  return JSON.stringify({ mcpServers: { [name || 'server']: entry } }, null, 2);
}

/** A pasted remote entry names its transport as `type` (Claude, Cursor) or `transport`; one with
 *  only a url is Streamable HTTP. A bearer token in `headers.Authorization` fills the token field. */
function remoteTransport(cfg: any): string {
  const kind = String(cfg.transport || cfg.type || '').toLowerCase();
  if (kind === 'sse') return 'sse';
  if (kind === 'http' || kind === 'streamable-http' || kind === 'streamable_http' || kind === 'streamablehttp') return 'http';
  return cfg.url && !cfg.command ? 'http' : 'stdio';
}

function parseStandardBlock(text: string): { name?: string; command?: string; transport?: string; url?: string; env?: Record<string, string>; token?: string } | null {
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
  const auth = cfg.headers && typeof cfg.headers === 'object'
    ? String(Object.entries(cfg.headers).find(([k]) => k.toLowerCase() === 'authorization')?.[1] ?? '')
    : '';
  return {
    name,
    command,
    transport: remoteTransport(cfg),
    url: cfg.url || '',
    env: (cfg.env && typeof cfg.env === 'object') ? cfg.env : {},
    token: /^bearer\s+/i.test(auth) ? auth.replace(/^bearer\s+/i, '').trim() : undefined,
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
  // Starts empty: a stored token never comes back. Empty on save keeps the stored one.
  const [token, setToken] = useState('');
  const [clearToken, setClearToken] = useState(false);
  const tokenStored = Boolean(server?.token_set) && !clearToken;
  const [auth, setAuth] = useState(server?.auth === 'oauth' ? 'oauth' : '');
  const [clientId, setClientId] = useState(server?.oauth_client_id ?? '');
  // Write-only like the token: starts empty, empty on save keeps the stored secret.
  const [clientSecret, setClientSecret] = useState('');
  const [clearClientSecret, setClearClientSecret] = useState(false);
  const clientSecretStored = Boolean(server?.oauth_client_secret_set) && !clearClientSecret;
  const [redirectUri, setRedirectUri] = useState('');
  const [jsonText, setJsonText] = useState(() => toStandardBlock(server?.name ?? '', server?.command ?? '', server?.env ?? {}, server?.transport ?? 'stdio', server?.url ?? ''));
  const [jsonInvalid, setJsonInvalid] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const lastSource = useRef<'form' | 'json'>('form');

  const isStdio = transport === 'stdio';
  const perAccount = !isStdio && auth === 'oauth';

  // The redirect address a hand-registered client needs is this backend's, which only the
  // backend knows (it depends on the network mode): ask it once the form shows the field.
  useEffect(() => {
    if (!perAccount || redirectUri) return;
    let gone = false;
    fetch(`${getApiBase()}/api/mcp/sign-in`, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!gone && d?.redirect_uri) setRedirectUri(String(d.redirect_uri)); })
      .catch(() => { /* the field stays without the address */ });
    return () => { gone = true; };
  }, [perAccount, redirectUri]);

  // Form → JSON (skip while the change came from the JSON panel, to avoid clobbering the user's text).
  useEffect(() => {
    if (lastSource.current === 'form') setJsonText(toStandardBlock(name, command, env, transport, url));
  }, [name, command, env, transport, url]);

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
    if (parsed.token) { setToken(parsed.token); setClearToken(false); }
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
    onSave({
      name: name.trim(), command: command.trim(), transport, url: url.trim(), enabled, permission_level: permission, env,
      ...(isStdio ? { auth: '' } : perAccount
        ? {
          auth: 'oauth',
          oauth_client_id: clientId.trim(),
          oauth_client_secret: clientSecret.trim() || undefined,
          clear_oauth_client_secret: clearClientSecret && !clientSecret.trim(),
        }
        : { auth: '', token: token.trim() || undefined, clear_token: clearToken && !token.trim() }),
    });
  };

  const handleTest = () => {
    if (!validate()) return;
    onTest?.({ name: isEdit ? server!.name : undefined, command: command.trim(), transport, url: url.trim(), env,
      token: isStdio || perAccount ? undefined : (token.trim() || undefined), auth: perAccount ? 'oauth' : '' });
  };

  const inputCls = 'w-full px-4 h-11 bg-white border border-gray-200 rounded-xl text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-amber-400 focus:border-amber-500 transition-all';
  const labelCls = 'block text-xs font-semibold text-gray-600 mb-1.5';

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4 max-md:p-0" onClick={onClose}>
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
      <div className="relative bg-white w-full max-w-4xl aspect-[4/3] max-h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden max-md:max-w-none max-md:aspect-auto max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0" onClick={(e) => e.stopPropagation()}>
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
                <option value="http">{t('transportHttp')}</option>
                <option value="sse">{t('transportSse')}</option>
              </select>
            </div>

            {isStdio ? (
              <div>
                <label className={labelCls}>{t('command')}</label>
                <input type="text" value={command} onChange={(e) => F(() => setCommand(e.target.value))} placeholder="npx -y @modelcontextprotocol/server-filesystem /path" className={inputCls} />
              </div>
            ) : (
              <>
                <div>
                  <label className={labelCls}>{t('url')}</label>
                  <input type="text" value={url} onChange={(e) => F(() => setUrl(e.target.value))} placeholder="https://example.com/mcp" className={inputCls} />
                </div>
                <div>
                  <label className={labelCls}>{t('auth')}</label>
                  <select value={auth} onChange={(e) => setAuth(e.target.value)} className={inputCls}>
                    <option value="">{t('authNone')}</option>
                    <option value="oauth">{t('authOauth')}</option>
                  </select>
                  {perAccount && <p className="text-[11px] text-gray-400 mt-1">{t('authOauthHint')}</p>}
                </div>
                {perAccount ? (
                  <>
                    <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
                      <div>
                        <label className={labelCls}>{t('clientId')}</label>
                        <input type="text" value={clientId} autoComplete="off" onChange={(e) => setClientId(e.target.value)} className={inputCls} />
                      </div>
                      <div>
                        <label className={labelCls}>{t('clientSecret')}</label>
                        <input type="password" value={clientSecret} autoComplete="off" onChange={(e) => { setClientSecret(e.target.value); setClearClientSecret(false); }} placeholder={clientSecretStored ? t('tokenStoredPlaceholder') : ''} className={inputCls} />
                      </div>
                    </div>
                    <div className="text-[11px] text-gray-400 space-y-1">
                      {clientSecretStored && (
                        <p>
                          {t('clientSecretStoredHint')}
                          {!clientSecret && (
                            <button type="button" onClick={() => setClearClientSecret(true)} className="ml-2 underline text-gray-500 hover:text-gray-700">{t('clientSecretRemove')}</button>
                          )}
                        </p>
                      )}
                      <p>{t('clientHint')}</p>
                      {redirectUri && <p className="font-mono text-gray-600 break-all select-all">{redirectUri}</p>}
                    </div>
                  </>
                ) : (
                  <div>
                    <label className={labelCls}>{t('token')}</label>
                    <input type="password" value={token} autoComplete="off" onChange={(e) => { setToken(e.target.value); setClearToken(false); }} placeholder={tokenStored ? t('tokenStoredPlaceholder') : ''} className={inputCls} />
                    <p className="text-[11px] text-gray-400 mt-1">
                      {tokenStored ? t('tokenStoredHint') : t('tokenHint')}
                      {tokenStored && !token && (
                        <button type="button" onClick={() => setClearToken(true)} className="ml-2 underline text-gray-500 hover:text-gray-700">{t('tokenRemove')}</button>
                      )}
                    </p>
                  </div>
                )}
              </>
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
            {isStdio && Object.keys(env).length > 0 && <p className="text-[11px] text-gray-400 mt-1">{t('envHint')}</p>}
          </div>
        </div>

        {(localError || backendError) && (
          <div className="px-6 pb-3">
            <div className="flex items-start gap-2 p-3 bg-red-50 border border-red-100 rounded-xl text-sm text-red-600">
              <AlertCircle size={16} className="mt-0.5 shrink-0" />
              <span>{localError || backendError}</span>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="border-t border-gray-100 p-4 flex items-center justify-between gap-3 shrink-0">
          {/* Left: everything that acts ON this server. "Test connection" used to sit in a strip
              of its own above the footer, which cost a row of height for one button and left an
              empty band whenever there was nothing to report. */}
          <div className="flex items-center gap-2 min-w-0">
            {isEdit && onDelete && (
              <button onClick={() => onDelete(server!.name)} disabled={isSaving} className="flex items-center gap-2 px-4 h-10 text-red-600 hover:bg-red-50 rounded-xl text-sm font-medium transition-colors disabled:opacity-50">
                <Trash2 size={16} /> {t('remove')}
              </button>
            )}
            {onTest && (
              <button onClick={handleTest} disabled={isTesting} className="flex items-center gap-2 px-4 h-10 bg-amber-50 text-amber-700 hover:bg-amber-100 rounded-xl text-sm font-medium transition-colors disabled:opacity-50 dark:bg-white/5 dark:text-gray-200 dark:hover:bg-white/10">
                {isTesting ? <Loader2 size={16} className="animate-spin" /> : <Wifi size={16} />} {isTesting ? t('testing') : t('test')}
              </button>
            )}
            {!isTesting && testResult && (
              testResult.connected
                ? <span className="flex items-center gap-1.5 text-sm text-green-600 truncate"><CheckCircle2 size={16} className="shrink-0" /> {t('testOk', { count: testResult.tool_count })}</span>
                : testResult.sign_in_required
                  ? <span className="flex items-center gap-1.5 text-sm text-amber-700 truncate"><CheckCircle2 size={16} className="shrink-0" /> {t('testSignIn')}</span>
                  : <span className="flex items-center gap-1.5 text-sm text-red-600 truncate"><AlertCircle size={16} className="shrink-0" /> {testResult.error || t('testFail')}</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="px-4 h-10 text-gray-600 hover:bg-gray-100 rounded-xl text-sm font-medium transition-colors">{t('cancel')}</button>
            <button onClick={handleSave} disabled={isSaving} className="flex items-center gap-2 px-5 h-10 bg-amber-600 hover:bg-amber-700 text-white rounded-xl text-sm font-medium transition-colors disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none">
              {isSaving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} {t('save')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
