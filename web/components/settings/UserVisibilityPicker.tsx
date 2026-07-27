// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Who may see a skill or a custom tool - the `shared_with` picker, shared by both editors.
 *
 * The value is the manifest's own three-state shape, unchanged:
 *   ['*']        everyone
 *   []           admin only
 *   [scope, ...] those users (plus admin)
 *
 * The three states are mutually exclusive by construction here, because they are mutually
 * exclusive in the data: offering "everyone" and a per-user list at the same time would leave
 * somebody having to guess later which one wins.
 *
 * The per-user list lives in a POPUP rather than inline. Inline it grows with the number of
 * accounts, and past a couple of dozen the picks a user already made scroll out of sight - so
 * the popup gets a search field, sorts the selected to the top, keeps a fixed height and shows
 * a count. It edits a DRAFT: Cancel (and Escape, and a click on the backdrop) really discards,
 * which matters when somebody has just clicked through eighty names.
 *
 * Layering: the popup sits at z-[95]. Both editors that host it render at z-[80]; 85 and 90 are
 * taken (training dashboard, one settings overlay) and 100 is the top-level wizard tier.
 */
'use client';

import { ChevronRight, Search, Users, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

export interface VisibilityUser {
  id: string;
  username: string;
  user_scope_id: string;
  role: string;
}

export interface UserVisibilityPickerProps {
  /** Current `shared_with`. */
  value: string[];
  onChange: (next: string[]) => void;
  /** Non-admin accounts offered in the popup. Empty is a normal state (single-user install). */
  users?: VisibilityUser[];
  /** Tailwind accent class, so each editor keeps its own colour in LIGHT mode. Dark mode is a
   *  neutral gray theme with no brand accent on active states (docs/web-ui/DARKMODE.md), so the
   *  dark half is the same light neutral the toggles use. */
  accent?: string;
  /** Wording for the "admin only" row - the editors phrase ownership differently. */
  adminOnlyLabel?: string;
}

export default function UserVisibilityPicker({
  value,
  onChange,
  users = [],
  accent = 'accent-emerald-600 dark:accent-[#d9d9d9]',
  adminOnlyLabel = 'Only me (admin)',
}: UserVisibilityPickerProps) {
  const isAllUsers = value.includes('*');
  const isAdminOnly = value.length === 0;
  const namedCount = value.filter(v => v !== '*').length;

  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string[]>([]);
  const [query, setQuery] = useState('');

  // Escape closes without committing - same as Cancel.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const hit = q ? users.filter(u => u.username.toLowerCase().includes(q)) : users;
    // Selected first: otherwise a handful of picks disappear into a long list.
    const picked = hit.filter(u => draft.includes(u.user_scope_id));
    const rest = hit.filter(u => !draft.includes(u.user_scope_id));
    return { picked, rest, total: hit.length };
  }, [users, query, draft]);

  function openPicker() {
    if (isAllUsers) return;
    setDraft(value.filter(v => v !== '*'));
    setQuery('');
    setOpen(true);
  }

  function commit() {
    onChange(draft);
    setOpen(false);
  }

  function toggleDraft(scopeId: string, checked: boolean) {
    setDraft(prev => (checked ? [...prev.filter(id => id !== scopeId), scopeId]
                              : prev.filter(id => id !== scopeId)));
  }

  const row = (u: VisibilityUser) => (
    <label
      key={u.user_scope_id}
      className={`flex items-center gap-2 px-2 py-1.5 rounded-md cursor-pointer select-none ${
        draft.includes(u.user_scope_id) ? 'bg-emerald-500/10' : 'hover:bg-black/5 dark:hover:bg-white/5'
      }`}
    >
      <input
        type="checkbox"
        checked={draft.includes(u.user_scope_id)}
        onChange={e => toggleDraft(u.user_scope_id, e.target.checked)}
        className={accent}
      />
      <span className="text-sm truncate">{u.username}</span>
    </label>
  );

  return (
    <>
      <div className="space-y-2">
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={isAllUsers}
            onChange={e => onChange(e.target.checked ? ['*'] : [])}
            className={accent}
          />
          <span className="text-sm">Everyone</span>
        </label>

        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={isAdminOnly}
            onChange={e => { if (e.target.checked) onChange([]); }}
            className={accent}
          />
          <span className="text-sm">{adminOnlyLabel}</span>
        </label>

        <button
          type="button"
          onClick={openPicker}
          disabled={isAllUsers}
          title={isAllUsers ? 'Turn off "Everyone" to pick individual users' : undefined}
          className="w-full flex items-center justify-between gap-2 px-3 py-2 rounded-md border border-black/10 dark:border-white/10 text-sm hover:bg-black/5 dark:hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          <span className="flex items-center gap-2">
            <Users size={14} /> Specific users
          </span>
          <span className="flex items-center gap-1 text-xs opacity-70">
            {namedCount > 0 ? `${namedCount} selected` : 'none selected'}
            <ChevronRight size={13} />
          </span>
        </button>
      </div>

      {open && (
        <div
          className="fixed inset-0 z-[95] flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm"
          onClick={e => { if (e.target === e.currentTarget) setOpen(false); }}
        >
          <div className="w-[26rem] max-w-full rounded-xl border border-black/10 dark:border-white/10 bg-white dark:bg-[#252526] shadow-2xl flex flex-col max-h-[80vh]">
            <div className="flex items-center justify-between px-4 py-3 border-b border-black/10 dark:border-white/10">
              <h3 className="text-sm font-semibold">Who may see this?</h3>
              <div className="flex items-center gap-3">
                <span className="text-xs opacity-70">{draft.length} of {users.length}</span>
                <button type="button" onClick={() => setOpen(false)} className="opacity-60 hover:opacity-100">
                  <X size={16} />
                </button>
              </div>
            </div>

            <div className="p-4 pb-2">
              <div className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 opacity-50" />
                <input
                  autoFocus
                  value={query}
                  onChange={e => setQuery(e.target.value)}
                  placeholder="Search users..."
                  className="w-full h-9 pl-9 pr-3 rounded-md border border-black/10 dark:border-white/10 bg-transparent text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
                />
              </div>
            </div>

            <div className="px-4 pb-2 overflow-y-auto">
              {shown.picked.length > 0 && (
                <>
                  <p className="text-[10px] uppercase tracking-wider opacity-50 mt-1 mb-1">Selected</p>
                  {shown.picked.map(row)}
                </>
              )}
              {shown.rest.length > 0 && (
                <>
                  {shown.picked.length > 0 && (
                    <p className="text-[10px] uppercase tracking-wider opacity-50 mt-3 mb-1">Others</p>
                  )}
                  {shown.rest.map(row)}
                </>
              )}
              {shown.total === 0 && (
                <p className="text-xs opacity-60 py-4 text-center">
                  {users.length === 0 ? 'No other users yet.' : 'No match.'}
                </p>
              )}
            </div>

            <div className="flex items-center justify-between px-4 py-3 border-t border-black/10 dark:border-white/10">
              <button
                type="button"
                onClick={() => setDraft([])}
                disabled={draft.length === 0}
                className="text-xs opacity-60 hover:opacity-100 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                Clear selection
              </button>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="px-3 py-1.5 rounded-md border border-black/10 dark:border-white/10 text-sm hover:bg-black/5 dark:hover:bg-white/5"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={commit}
                  className="px-3 py-1.5 rounded-md bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium"
                >
                  Done
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
