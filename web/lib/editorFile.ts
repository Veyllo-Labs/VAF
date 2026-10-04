// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * What an editor knows about the file it opened, and the one way it saves.
 *
 * The save routes write only while the file is at the revision the editor loaded
 * (vaf/core/file_revision.py): a change the agent, another tab or another program
 * made meanwhile is never overwritten silently - the answer is 409 and the draft
 * stays. An office file a save would lose content of is never overwritten either:
 * the server saves ONE copy, `<name> (bearbeitet)<ext>`, and says so, and the editor
 * works on in that copy. The three load routes say all of it in headers, because the
 * body is the file itself (or its HTML, or its model).
 */

export type EditorFileInfo = {
    /** The revision a save must name; null for a file that does not exist yet. */
    revision: string | null;
    /** What saving would lose (reason codes, `editorFile.loss_*`); empty when nothing. */
    loss: string[];
    /** The edit copy a lossy file already has, if any. */
    editCopy: string | null;
};

export type SaveConflict = {
    /** `conflict`: the file changed on disk. `copy_exists`: the edit copy is already there. */
    code: 'conflict' | 'copy_exists';
    path: string;
    currentRevision: string | null;
};

export type SaveOutcome =
    | { ok: true; path: string; revision: string; redirected: boolean }
    | { ok: false; conflict?: SaveConflict; error?: string };

export const EMPTY_FILE_INFO: EditorFileInfo = { revision: null, loss: [], editCopy: null };

export function fileInfoFromResponse(res: Response): EditorFileInfo {
    let loss: string[] = [];
    try {
        const raw = res.headers.get('X-VAF-Loss');
        const parsed = raw ? JSON.parse(raw) : [];
        if (Array.isArray(parsed)) loss = parsed.map(String);
    } catch { /* no report reads as nothing to lose; the server still refuses the overwrite */ }
    const copy = res.headers.get('X-VAF-Edit-Copy');
    let editCopy: string | null = null;
    if (copy) {
        try { editCopy = decodeURIComponent(copy); } catch { editCopy = copy; }
    }
    return { revision: res.headers.get('X-VAF-Revision'), loss, editCopy };
}

/** POST one save. `base_revision` is the revision the draft was edited from. */
export async function saveEditorFile(endpoint: string, body: Record<string, unknown>): Promise<SaveOutcome> {
    let res: Response;
    try {
        res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
    const payload = await res.json().catch(() => ({}));
    if (res.status === 409 && payload?.detail && typeof payload.detail === 'object') {
        const d = payload.detail;
        return {
            ok: false,
            conflict: {
                code: d.code === 'copy_exists' ? 'copy_exists' : 'conflict',
                path: String(d.path || ''),
                currentRevision: d.current_revision ? String(d.current_revision) : null,
            },
        };
    }
    if (!res.ok) {
        const detail = typeof payload?.detail === 'string' ? payload.detail : res.statusText;
        return { ok: false, error: detail || 'Save failed' };
    }
    return {
        ok: true,
        path: String(payload.path || ''),
        revision: String(payload.revision || ''),
        redirected: Boolean(payload.redirected),
    };
}
