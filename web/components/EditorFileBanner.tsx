// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useTranslations } from 'next-intl';
import { AlertTriangle } from 'lucide-react';
import type { EditorFileInfo, SaveConflict } from '@/lib/editorFile';

/**
 * The one notice every editor shows about its file (web/lib/editorFile.ts): a save
 * that would lose content goes to a copy, the file changed on disk, the agent changed
 * it while the draft was open, the agent opened another file while this draft has unsaved
 * changes, or it could not be loaded. Each states what happened and offers what the person
 * can do; the draft is never thrown away without a click.
 */
export default function EditorFileBanner({
    info,
    conflict,
    externalChange = false,
    loadFailed = false,
    pendingFile = null,
    onOpenPending,
    onKeepDraft,
    onReload,
    onSaveAsCopy,
    onOverwrite,
    onOpenCopy,
    onDismiss,
}: {
    info: EditorFileInfo | null;
    conflict: SaveConflict | null;
    externalChange?: boolean;
    loadFailed?: boolean;
    /** Another file the agent opened while this draft has unsaved changes. */
    pendingFile?: string | null;
    onOpenPending?: () => void;
    onKeepDraft?: () => void;
    onReload?: () => void;
    onSaveAsCopy?: () => void;
    onOverwrite?: () => void;
    onOpenCopy?: (path: string) => void;
    onDismiss?: () => void;
}) {
    const t = useTranslations('editorFile');
    const btn = 'px-2 py-0.5 rounded border text-[11px] font-medium transition-colors';
    const quiet = `${btn} border-amber-300 bg-white text-amber-900 hover:bg-amber-100`;
    const loud = `${btn} border-amber-600 bg-amber-600 text-white hover:bg-amber-700`;
    const reasons = (info?.loss || []).map(code => t.has(`loss_${code}`) ? t(`loss_${code}`) : code).join(t('separator'));
    const fileName = (path: string) => path.split(/[\\/]/).pop() || path;

    let title = '';
    let body = '';
    const actions: Array<{ label: string; onClick: () => void; primary?: boolean }> = [];
    if (loadFailed) {
        title = t('loadFailedTitle');
        body = t('loadFailedBody');
        if (onReload) actions.push({ label: t('reload'), onClick: onReload });
    } else if (conflict?.code === 'copy_exists') {
        title = t('copyExistsTitle');
        body = t('copyExistsBody', { copy: fileName(conflict.path) });
        if (onOpenCopy) actions.push({ label: t('openCopy'), onClick: () => onOpenCopy(conflict.path), primary: true });
    } else if (conflict) {
        title = t('conflictTitle');
        body = t('conflictBody');
        if (onReload) actions.push({ label: t('reload'), onClick: onReload });
        if (onSaveAsCopy) actions.push({ label: t('saveAsCopy'), onClick: onSaveAsCopy, primary: true });
        if (onOverwrite) actions.push({ label: t('overwrite'), onClick: onOverwrite });
    } else if (externalChange) {
        title = t('externalTitle');
        body = t('externalBody');
        if (onReload) actions.push({ label: t('reload'), onClick: onReload });
        if (onDismiss) actions.push({ label: t('keepMine'), onClick: onDismiss, primary: true });
    } else if (pendingFile) {
        title = t('pendingTitle', { file: fileName(pendingFile) });
        body = t('pendingBody');
        if (onKeepDraft) actions.push({ label: t('keepMine'), onClick: onKeepDraft, primary: true });
        if (onOpenPending) actions.push({ label: t('openPending'), onClick: onOpenPending });
    } else if (info && info.loss.length > 0) {
        title = t('lossTitle');
        body = info.editCopy
            ? t('lossBodyCopyExists', { reasons, copy: fileName(info.editCopy) })
            : t('lossBody', { reasons });
        if (info.editCopy && onOpenCopy) {
            const copy = info.editCopy;
            actions.push({ label: t('openCopy'), onClick: () => onOpenCopy(copy), primary: true });
        }
    } else {
        return null;
    }
    return (
        <div role="status" className="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-3 py-2 text-amber-900">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <div className="min-w-0 flex-1">
                <p className="text-xs font-semibold">{title}</p>
                <p className="text-[11px] leading-snug">{body}</p>
                {actions.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {actions.map(a => (
                            <button key={a.label} type="button" onClick={a.onClick} className={a.primary ? loud : quiet}>
                                {a.label}
                            </button>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
