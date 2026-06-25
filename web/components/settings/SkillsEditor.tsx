'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useCallback, useEffect } from 'react';
import { X, Trash2, AlertCircle, Loader2, Sparkles, Upload } from 'lucide-react';

// ─── Types ───────────────────────────────────────────────────────────────────

export interface SkillSaveData {
  skill_id: string;
  name: string;
  description: string;
  /** The Markdown instruction body (without the YAML frontmatter). */
  body: string;
  /** Admin override to install despite a HIGH-risk security-scan finding. */
  override?: boolean;
}

export interface SkillsEditorProps {
  /** null = create mode; string = edit mode (the skill ID) */
  skillId: string | null;
  initialData?: {
    name?: string;
    description?: string;
    /** Raw SKILL.md text (edit mode). Parsed into name/description/body below. */
    source?: string;
  };
  onSave: (data: SkillSaveData) => void;
  onDelete?: (id: string) => void;
  /** Upload a folder bundle as a .zip (create mode only). */
  onUploadZip?: (filename: string, base64: string, override: boolean) => void;
  onClose: () => void;
  isSaving?: boolean;
  backendError?: string | null;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Convert "My Cool Skill" → "my_cool_skill" (matches backend derive_skill_id). */
function nameToId(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/[\s-]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
  return slug || 'my_skill';
}

/** Light client-side SKILL.md parse: pull name/description out of the frontmatter,
 *  return the body. Robust enough for the simple frontmatter the backend writes. */
function parseSkillMd(src: string): { name: string; description: string; body: string } {
  const m = src.match(/^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*\r?\n?([\s\S]*)$/);
  if (!m) return { name: '', description: '', body: src };
  const fm = m[1];
  const body = (m[2] ?? '').replace(/^\n+/, '');
  const clean = (s?: string) => (s ?? '').trim().replace(/^["']|["']$/g, '');
  const nameM = fm.match(/^name:\s*(.+)$/m);
  const descM = fm.match(/^description:\s*(.+)$/m);
  return { name: clean(nameM?.[1]), description: clean(descM?.[1]), body };
}

const BODY_PLACEHOLDER = `# How to do the thing

1. First, ...
2. Then, ...

You can bundle helper files (scripts, references) by uploading a .zip with a
SKILL.md at its root; reference them from these instructions and the agent will
read them on demand.`;

// ─── Component ───────────────────────────────────────────────────────────────

export default function SkillsEditor({
  skillId,
  initialData,
  onSave,
  onDelete,
  onUploadZip,
  onClose,
  isSaving = false,
  backendError = null,
}: SkillsEditorProps) {
  const isEdit = skillId !== null;
  const parsed = initialData?.source ? parseSkillMd(initialData.source) : null;

  const [name, setName] = useState(parsed?.name ?? initialData?.name ?? '');
  const [sId, setSId] = useState(isEdit ? skillId : nameToId(initialData?.name ?? ''));
  const [idEdited, setIdEdited] = useState(false);
  const [description, setDescription] = useState(parsed?.description ?? initialData?.description ?? '');
  const [body, setBody] = useState(parsed?.body ?? '');
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [override, setOverride] = useState(false);
  const [lastUpload, setLastUpload] = useState<{ filename: string; base64: string } | null>(null);

  // Auto-generate ID from name (create mode only, until the user edits ID).
  useEffect(() => {
    if (!isEdit && !idEdited) setSId(nameToId(name));
  }, [name, isEdit, idEdited]);

  const handleSave = () => {
    setLocalError(null);
    const finalId = isEdit ? skillId! : sId;
    if (!name.trim()) {
      setLocalError('Skill name is required.');
      return;
    }
    if (!description.trim()) {
      setLocalError('Description is required — the router matches skills by it.');
      return;
    }
    if (!isEdit && !/^[a-z][a-z0-9_]*$/.test(finalId)) {
      setLocalError('Skill ID must be lowercase snake_case (e.g. my_skill).');
      return;
    }
    onSave({ skill_id: finalId, name: name.trim(), description: description.trim(), body, override });
  };

  const handleZip = useCallback((file: File) => {
    setLocalError(null);
    const reader = new FileReader();
    reader.onload = () => {
      const res = reader.result as string;
      const base64 = res.includes(',') ? res.split(',')[1] : res;
      setLastUpload({ filename: file.name, base64 });
      onUploadZip?.(file.name, base64, override);
    };
    reader.onerror = () => setLocalError('Could not read the .zip file.');
    reader.readAsDataURL(file);
  }, [onUploadZip, override]);

  // ─── render ─────────────────────────────────────────────────────────────────
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />

      <div
        className="relative bg-white w-full max-w-2xl max-h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div className="h-16 border-b border-gray-100 flex items-center justify-between px-6 shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-emerald-100 text-emerald-600 flex items-center justify-center">
              <Sparkles size={16} />
            </div>
            <h2 className="text-lg font-semibold text-gray-800">
              {isEdit ? `Edit "${name || skillId}"` : 'Create Skill'}
            </h2>
          </div>
          <button onClick={onClose} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
            <X size={20} />
          </button>
        </div>

        {/* ── Scrollable body ── */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">

          {/* Upload bundle (create only) */}
          {!isEdit && onUploadZip && (
            <div className="flex items-center justify-between gap-3 p-3 bg-emerald-50/60 border border-emerald-100 rounded-lg">
              <div className="text-xs text-gray-600">
                Have a SKILL.md folder bundle? Upload it as a <span className="font-mono">.zip</span> (with scripts/resources).
              </div>
              <label className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium bg-white border border-emerald-200 text-emerald-700 rounded-lg hover:bg-emerald-100 cursor-pointer transition-colors">
                <Upload size={13} />
                Upload .zip
                <input
                  type="file"
                  accept=".zip,application/zip"
                  className="hidden"
                  onChange={e => { const f = e.target.files?.[0]; if (f) handleZip(f); e.currentTarget.value = ''; }}
                />
              </label>
            </div>
          )}

          {/* Name + ID */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Name *</label>
              <input
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="My Skill"
                className="w-full h-10 px-3 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 transition-all"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">
                ID{isEdit && <span className="text-gray-400 font-normal ml-1">(read-only)</span>}
              </label>
              <input
                type="text"
                value={isEdit ? skillId! : sId}
                onChange={e => { if (!isEdit) { setIdEdited(true); setSId(e.target.value); } }}
                readOnly={isEdit}
                placeholder="my_skill"
                className={`w-full h-10 px-3 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 transition-all ${isEdit ? 'bg-gray-50 text-gray-400 cursor-default' : ''}`}
              />
            </div>
          </div>

          {/* Description */}
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              Description * <span className="text-gray-300 font-normal">— the router matches the skill by this</span>
            </label>
            <input
              type="text"
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="What the skill does and when to use it"
              className="w-full h-10 px-3 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 transition-all"
            />
          </div>

          {/* Instructions body */}
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              Instructions (Markdown) <span className="text-gray-300 font-normal">— loaded on demand when the skill is used</span>
            </label>
            <textarea
              value={body}
              onChange={e => setBody(e.target.value)}
              placeholder={BODY_PLACEHOLDER}
              rows={12}
              className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono resize-y focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 transition-all"
            />
          </div>

          {/* Error display */}
          {(localError || backendError) && (
            <div className="flex items-start gap-2 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
              <AlertCircle size={16} className="shrink-0 mt-0.5" />
              <span className="whitespace-pre-wrap">{localError || backendError}</span>
            </div>
          )}

          {/* Security-scan override (appears once a scan has blocked the skill) */}
          {backendError && (
            <div className="flex flex-col gap-2 p-3 bg-amber-50 border border-amber-200 rounded-lg">
              <label className="flex items-center gap-2 text-xs text-amber-800 cursor-pointer">
                <input type="checkbox" checked={override} onChange={e => setOverride(e.target.checked)} />
                Override the security scan and install anyway (admin — only if you trust this skill).
              </label>
              {lastUpload && override && (
                <button
                  type="button"
                  onClick={() => onUploadZip?.(lastUpload.filename, lastUpload.base64, true)}
                  className="self-start inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium bg-amber-600 text-white rounded-lg hover:bg-amber-700 transition-colors"
                >
                  <Upload size={13} />
                  Re-upload "{lastUpload.filename}" with override
                </button>
              )}
            </div>
          )}
        </div>

        {/* ── Footer ── */}
        <div className="h-16 border-t border-gray-100 flex items-center justify-between px-6 shrink-0 bg-gray-50/50">
          <div>
            {isEdit && onDelete && (
              showDeleteConfirm ? (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-red-600">Delete this skill?</span>
                  <button
                    onClick={() => { onDelete(skillId!); }}
                    className="px-3 py-1.5 text-xs font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors"
                  >
                    Delete
                  </button>
                  <button
                    onClick={() => setShowDeleteConfirm(false)}
                    className="px-3 py-1.5 text-xs font-medium text-gray-500 hover:bg-gray-100 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setShowDeleteConfirm(true)}
                  className="px-3 py-1.5 text-xs font-medium text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                >
                  Delete Skill
                </button>
              )
            )}
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={isSaving}
              className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
            >
              {isSaving && <Loader2 size={14} className="animate-spin" />}
              {isEdit ? 'Update Skill' : 'Create Skill'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
