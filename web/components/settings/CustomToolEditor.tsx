'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * CustomToolEditor
 * ================
 * Modal panel that lets an admin create or edit a custom Python tool.
 *
 * Modes:
 *   - Create (toolName === null):  blank/template code, editable name field.
 *   - Edit   (toolName !== null):  pre-filled code from backend, name is read-only.
 *
 * The component is intentionally self-contained:
 *   - It renders inside a full-screen overlay (z-[80]) so it stacks above the
 *     Tools modal (z-[60]) and the CodeViewer (z-[70]).
 *   - It does NOT handle WS communication itself — it calls onSave() /
 *     onDelete() callbacks and lets the parent (SettingsModal) drive the WS.
 *
 * Permissions flow:
 *   - Only admins ever see this component (SettingsModal only renders it when
 *     currentUser?.role === "admin").
 *   - The "share with" user picker receives the full user list from the parent;
 *     the parent fetches it via the get_custom_tool_users WS message.
 */

import dynamic from 'next/dynamic';
import React, { useEffect, useRef, useState } from 'react';
import { AlertCircle, ChevronDown, ChevronUp, Loader2, Save, Trash2, Users, X } from 'lucide-react';

// Monaco is heavy — load it client-side only (same pattern as CodeViewer.tsx)
const MonacoEditor = dynamic(() => import('@monaco-editor/react'), { ssr: false });

// ─── Types ───────────────────────────────────────────────────────────────────

export interface CustomToolUser {
  id: string;
  username: string;
  user_scope_id: string;
  role: string;
}

export interface CustomToolEditorProps {
  /** null → create mode; string → edit mode for this tool name */
  toolName: string | null;
  /** Pre-filled code (edit mode) or undefined (create mode → template used) */
  initialCode?: string;
  /** Current shared_with value (edit mode) */
  initialSharedWith?: string[];
  /** Full list of non-admin users for the share picker */
  users: CustomToolUser[];
  /** Called when the admin clicks Save */
  onSave: (params: {
    name: string;
    code: string;
    sharedWith: string[];
  }) => void;
  /** Called when the admin confirms deletion (edit mode only) */
  onDelete?: (name: string) => void;
  /** Close without saving */
  onClose: () => void;
  /** Whether the backend is processing a previous save/delete */
  isSaving?: boolean;
  /** Error message from the backend (e.g. "no BaseTool subclass found") */
  backendError?: string | null;
}

// ─── Default template injected in create mode ────────────────────────────────

const TOOL_TEMPLATE = `from vaf.tools.base import BaseTool


class MyCustomTool(BaseTool):
    name        = "my_custom_tool"   # snake_case, must match the file name
    description = "Describe what this tool does for the agent."

    # Declarative contract — set all three on every tool
    permission_level  = "read"   # "read" | "write" | "dangerous" | "system"
    side_effect_class = "none"   # "none" | "reversible" | "irreversible"
    channel_restrictions = ()    # e.g. ("telegram", "whatsapp") to block chat channels

    # Optional: 1–3 concrete examples shown to the agent (provider-agnostic)
    input_examples = [
        {"input": "example value"},
    ]

    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The input text to process.",
            }
        },
        "required": ["input"],
    }

    def run(self, **kwargs) -> str:
        input_text = kwargs.get("input", "")
        # TODO: implement your tool logic here
        return f"Processed: {input_text}"
`;

// ─── Name validation (mirrors backend: ^[a-z][a-z0-9_]*$) ───────────────────

function isValidToolName(name: string): boolean {
  return /^[a-z][a-z0-9_]*$/.test(name);
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function CustomToolEditor({
  toolName,
  initialCode,
  initialSharedWith,
  users,
  onSave,
  onDelete,
  onClose,
  isSaving = false,
  backendError = null,
}: CustomToolEditorProps) {
  const isEditMode = toolName !== null;

  // ── Local state ──────────────────────────────────────────────────────────
  const [name, setName]             = useState(toolName ?? '');
  const [code, setCode]             = useState(initialCode ?? TOOL_TEMPLATE);
  const [sharedWith, setSharedWith] = useState<string[]>(
    initialSharedWith ?? ['*']
  );
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showSharePanel, setShowSharePanel]       = useState(false);
  const nameInputRef = useRef<HTMLInputElement>(null);

  // Focus the name field on mount in create mode
  useEffect(() => {
    if (!isEditMode) nameInputRef.current?.focus();
  }, [isEditMode]);

  // ── Sharing helpers ──────────────────────────────────────────────────────
  const isAllUsers    = sharedWith.includes('*');
  const isAdminOnly   = sharedWith.length === 0;

  function toggleAllUsers(checked: boolean) {
    setSharedWith(checked ? ['*'] : []);
  }

  function toggleUser(scopeId: string, checked: boolean) {
    if (isAllUsers) return; // "all users" overrides individual selection
    setSharedWith(prev =>
      checked ? [...prev.filter(id => id !== '*'), scopeId]
              : prev.filter(id => id !== scopeId)
    );
  }

  // ── Derived validation ───────────────────────────────────────────────────
  const nameError =
    name.length > 0 && !isValidToolName(name)
      ? 'Must be lowercase snake_case (e.g. my_tool)'
      : null;
  const canSave = isValidToolName(name) && code.trim().length > 0 && !isSaving;

  // ── Save handler ─────────────────────────────────────────────────────────
  function handleSave() {
    if (!canSave) return;
    onSave({ name, code, sharedWith });
  }

  // ── UI ───────────────────────────────────────────────────────────────────
  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center p-4 max-md:p-0"
      onClick={onClose}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />

      {/* Panel */}
      <div
        className="relative bg-[#1e1e1e] w-full max-w-5xl h-[90vh] rounded-2xl shadow-2xl flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ─────────────────────────────────────────────────────── */}
        <div className="h-16 border-b border-white/10 flex items-center justify-between px-6 shrink-0">
          <h2 className="text-lg font-bold text-white">
            {isEditMode ? `Edit Tool: ${toolName}` : 'Create Custom Tool'}
          </h2>
          <button
            onClick={onClose}
            className="p-2 text-gray-400 hover:text-white rounded-full hover:bg-white/10 transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* ── Body (name + editor + sidebar) ─────────────────────────────── */}
        <div className="flex flex-1 overflow-hidden">
          {/* Left: name input + Monaco editor */}
          <div className="flex flex-1 flex-col overflow-hidden">
            {/* Tool name (create mode only — in edit mode the name is fixed) */}
            {!isEditMode && (
              <div className="px-4 pt-3 pb-2 border-b border-white/10 shrink-0">
                <label className="block text-xs text-gray-400 mb-1">
                  Tool name&nbsp;
                  <span className="text-gray-500">(snake_case, e.g. my_tool)</span>
                </label>
                <input
                  ref={nameInputRef}
                  type="text"
                  value={name}
                  onChange={e => setName(e.target.value.toLowerCase().replace(/\s+/g, '_'))}
                  placeholder="my_custom_tool"
                  className={`w-full bg-[#2d2d2d] text-white text-sm px-3 py-2 rounded-lg border
                    focus:outline-none focus:ring-1 transition-colors font-mono
                    ${nameError
                      ? 'border-red-500 focus:ring-red-500'
                      : 'border-white/20 focus:ring-blue-500'}`}
                />
                {nameError && (
                  <p className="mt-1 text-xs text-red-400 flex items-center gap-1">
                    <AlertCircle size={12} /> {nameError}
                  </p>
                )}
              </div>
            )}

            {/* Monaco code editor */}
            <div className="flex-1 overflow-hidden">
              <MonacoEditor
                height="100%"
                language="python"
                theme="vs-dark"
                value={code}
                onChange={v => setCode(v ?? '')}
                options={{
                  fontSize: 13,
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  wordWrap: 'on',
                  tabSize: 4,
                  insertSpaces: true,
                  lineNumbers: 'on',
                  renderLineHighlight: 'line',
                  automaticLayout: true,
                }}
              />
            </div>
          </div>

          {/* Right sidebar: sharing controls */}
          <div className="w-64 border-l border-white/10 flex flex-col shrink-0 overflow-y-auto bg-[#252526]">
            {/* Share panel toggle */}
            <button
              className="flex items-center justify-between px-4 py-3 text-sm font-medium text-gray-300 hover:text-white hover:bg-white/5 transition-colors border-b border-white/10"
              onClick={() => setShowSharePanel(p => !p)}
            >
              <span className="flex items-center gap-2">
                <Users size={15} /> Visibility
              </span>
              {showSharePanel ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
            </button>

            {showSharePanel && (
              <div className="px-4 py-3 space-y-3">
                {/* All users toggle */}
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={isAllUsers}
                    onChange={e => toggleAllUsers(e.target.checked)}
                    className="accent-blue-500"
                  />
                  <span className="text-sm text-gray-300">All users</span>
                </label>

                {/* Admin-only shortcut */}
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={isAdminOnly}
                    onChange={e => {
                      if (e.target.checked) setSharedWith([]);
                    }}
                    className="accent-blue-500"
                  />
                  <span className="text-sm text-gray-300">Admin only</span>
                </label>

                {/* Per-user checkboxes (only shown when not "all users") */}
                {!isAllUsers && users.length > 0 && (
                  <>
                    <p className="text-[11px] text-gray-500 uppercase tracking-wider pt-1">
                      Specific users
                    </p>
                    {users.map(u => (
                      <label
                        key={u.user_scope_id}
                        className="flex items-center gap-2 cursor-pointer select-none"
                      >
                        <input
                          type="checkbox"
                          checked={sharedWith.includes(u.user_scope_id)}
                          onChange={e => toggleUser(u.user_scope_id, e.target.checked)}
                          className="accent-blue-500"
                        />
                        <span className="text-sm text-gray-300 truncate">{u.username}</span>
                      </label>
                    ))}
                  </>
                )}

                {!isAllUsers && users.length === 0 && (
                  <p className="text-xs text-gray-500">No other users found.</p>
                )}
              </div>
            )}

            {/* Spacer */}
            <div className="flex-1" />

            {/* Delete button (edit mode only) */}
            {isEditMode && onDelete && (
              <div className="px-4 pb-3 pt-2 border-t border-white/10">
                {showDeleteConfirm ? (
                  <div className="space-y-2">
                    <p className="text-xs text-red-400">
                      Delete <strong>{toolName}</strong>? This cannot be undone.
                    </p>
                    <div className="flex gap-2">
                      <button
                        onClick={() => onDelete(toolName!)}
                        disabled={isSaving}
                        className="flex-1 py-1.5 text-xs rounded-lg bg-red-600 hover:bg-red-700 text-white disabled:opacity-50 transition-colors"
                      >
                        {isSaving ? 'Deleting…' : 'Confirm'}
                      </button>
                      <button
                        onClick={() => setShowDeleteConfirm(false)}
                        className="flex-1 py-1.5 text-xs rounded-lg bg-white/10 hover:bg-white/20 text-gray-300 transition-colors"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    onClick={() => setShowDeleteConfirm(true)}
                    className="w-full flex items-center justify-center gap-1.5 py-1.5 text-xs rounded-lg text-red-400 hover:text-red-300 hover:bg-red-500/10 transition-colors"
                  >
                    <Trash2 size={13} /> Delete tool
                  </button>
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Footer ─────────────────────────────────────────────────────── */}
        <div className="h-14 border-t border-white/10 flex items-center justify-between px-6 shrink-0 bg-[#1e1e1e]">
          {/* Backend error message */}
          <div className="flex-1 mr-4">
            {backendError && (
              <p className="text-xs text-red-400 flex items-center gap-1">
                <AlertCircle size={12} />
                {backendError}
              </p>
            )}
          </div>

          {/* Action buttons */}
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-400 hover:text-white hover:bg-white/10 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={!canSave}
              className="flex items-center gap-2 px-5 py-2 text-sm font-medium rounded-lg bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {isSaving
                ? <><Loader2 size={14} className="animate-spin" /> Saving…</>
                : <><Save size={14} /> {isEditMode ? 'Save changes' : 'Create tool'}</>
              }
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
