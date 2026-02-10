'use client';

import React, { useState, useRef } from 'react';
import { X, FileText, Plus, Trash2 } from 'lucide-react';
import { cn } from '@/lib/utils';

export type DocumentViewerDocument = {
    id: string;
    name: string;
    mimeType?: string;
    content?: string;
};

export type DocumentViewerProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    title?: string;
    mode?: 'overlay' | 'dock';
    documents: DocumentViewerDocument[];
    onAddFiles: (files: File[]) => void;
    onRemoveDocument: (id: string) => void;
};

const FILE_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.txt,.md,.json,.csv';

export default function DocumentViewer({
    isOpen,
    onClose,
    canClose = true,
    title = 'Document Viewer',
    mode = 'dock',
    documents,
    onAddFiles,
    onRemoveDocument,
}: DocumentViewerProps) {
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const hasDocuments = documents.length > 0;
    const selectedDoc = documents.find((d) => d.id === selectedId) ?? documents[0];
    const displayContent = selectedDoc?.content ?? '';
    const isImage = selectedDoc?.mimeType?.startsWith('image/');

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = e.target.files ? Array.from(e.target.files) : [];
        if (files.length) {
            onAddFiles(files);
        }
        e.target.value = '';
    };

    if (!isOpen && mode === 'overlay') return null;

    // Dock mode - same layout/size as DocumentEditor dock
    if (mode === 'dock') {
        return (
            <div
                className={cn(
                    'relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out',
                    isOpen ? 'translate-x-0 opacity-100' : 'translate-x-8 opacity-0 pointer-events-none'
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full">
                    {/* Left: document list (same width as DocumentEditor steps panel) */}
                    <div className="flex w-[36%] min-w-[280px] flex-col border-r border-gray-200 bg-white">
                        <div className="flex h-12 items-center justify-between border-b border-gray-100 px-4">
                            <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                Dokumentliste
                            </span>
                        </div>
                        <div className="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-2">
                            <button
                                type="button"
                                onClick={() => fileInputRef.current?.click()}
                                className="flex items-center gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2.5 text-left text-[13px] font-medium text-gray-700 hover:border-blue-200 hover:bg-blue-50/50 transition"
                            >
                                <Plus size={14} />
                                Dokument hinzufügen
                            </button>
                            <input
                                ref={fileInputRef}
                                type="file"
                                multiple
                                accept={FILE_ACCEPT}
                                className="hidden"
                                onChange={handleFileChange}
                            />
                            {documents.map((doc) => (
                                <div
                                    key={doc.id}
                                    className={cn(
                                        'flex items-center gap-2 rounded-xl border px-3 py-2.5 transition cursor-pointer',
                                        selectedId === doc.id
                                            ? 'border-blue-200 bg-blue-50/50 ring-1 ring-blue-50'
                                            : 'border-gray-100 bg-gray-50 hover:border-gray-200'
                                    )}
                                    onClick={() => setSelectedId(doc.id)}
                                >
                                    <FileText size={14} className="shrink-0 text-gray-500" />
                                    <span className="flex-1 truncate text-[13px] font-medium text-gray-900" title={doc.name}>
                                        {doc.name}
                                    </span>
                                    <button
                                        type="button"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            onRemoveDocument(doc.id);
                                            if (selectedId === doc.id) {
                                                const next = documents.find((d) => d.id !== doc.id);
                                                setSelectedId(next?.id ?? null);
                                            }
                                        }}
                                        className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600 transition"
                                        aria-label={`Remove ${doc.name}`}
                                    >
                                        <Trash2 size={12} />
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* Right: content (same layout as DocumentEditor main panel) */}
                    <div className="flex flex-1 flex-col bg-[#F9FAFB] rounded-l-2xl">
                        <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4">
                            <div className="flex items-center gap-3">
                                <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600">
                                    <FileText size={14} />
                                </div>
                                <div>
                                    <div className="text-xs font-semibold text-gray-900">{title}</div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                        <span className="h-1.5 w-1.5 rounded-full bg-gray-400" />
                                        <span className="uppercase">Ready</span>
                                    </div>
                                </div>
                            </div>
                            <button
                                onClick={onClose}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Close"
                            >
                                <X size={14} />
                            </button>
                        </div>
                        <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500">
                            <span className="rounded-md bg-gray-100 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500">
                                Anhänge
                            </span>
                            <span className="truncate font-mono text-[11px]">
                                {selectedDoc?.name ?? 'Kein Dokument ausgewählt'}
                            </span>
                        </div>
                        <div className="flex-1 overflow-hidden p-4">
                            <div className="flex h-full flex-col overflow-hidden rounded-xl border border-gray-200 bg-white">
                                <div className="flex h-8 items-center border-b border-gray-100 bg-gray-50 px-3 text-[10px] text-gray-400">
                                    <div className="flex-1 truncate text-center font-mono">
                                        {selectedDoc?.name ?? '—'}
                                    </div>
                                </div>
                                <div className="flex-1 overflow-auto bg-white p-4">
                                    {!hasDocuments ? (
                                        <div className="flex flex-col items-center justify-center h-full gap-2 text-gray-400 text-sm">
                                            <FileText size={32} className="opacity-50" />
                                            <p>Keine Dokumente. Klicke auf &quot;Dokument hinzufügen&quot;, um Anhänge zu öffnen.</p>
                                            <p className="text-xs">Der Assistent kann dann auf deren Inhalt antworten.</p>
                                        </div>
                                    ) : !selectedDoc ? (
                                        <div className="text-gray-400 text-sm">Dokument auswählen.</div>
                                    ) : isImage && selectedDoc.content?.startsWith('data:') ? (
                                        <img src={selectedDoc.content} alt={selectedDoc.name} className="max-w-full h-auto" />
                                    ) : (
                                        <pre className="whitespace-pre-wrap break-words font-sans text-sm text-gray-800">
                                            {displayContent || '(Kein Textinhalt)'}
                                        </pre>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    // Overlay mode
    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl">
                <div className="flex w-[35%] min-w-[320px] flex-col border-r border-gray-200 bg-white">
                    <div className="flex h-14 items-center justify-between border-b border-gray-100 px-5">
                        <span className="text-sm font-semibold text-gray-700">Dokumentliste</span>
                    </div>
                    <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-3">
                        <button
                            type="button"
                            onClick={() => fileInputRef.current?.click()}
                            className="flex items-center gap-2 rounded-xl border border-gray-200 bg-white px-4 py-3 text-left text-sm font-medium text-gray-700 hover:border-blue-200 hover:bg-blue-50/50 transition"
                        >
                            <Plus size={16} />
                            Dokument hinzufügen
                        </button>
                        <input
                            ref={fileInputRef}
                            type="file"
                            multiple
                            accept={FILE_ACCEPT}
                            className="hidden"
                            onChange={handleFileChange}
                        />
                        {documents.map((doc) => (
                            <div
                                key={doc.id}
                                className={cn(
                                    'flex items-center gap-2 rounded-xl border px-4 py-3 transition cursor-pointer',
                                    selectedId === doc.id ? 'border-blue-200 bg-blue-50/50' : 'border-gray-100 bg-gray-50'
                                )}
                                onClick={() => setSelectedId(doc.id)}
                            >
                                <FileText size={16} className="shrink-0 text-gray-500" />
                                <span className="flex-1 truncate text-sm font-medium text-gray-900">{doc.name}</span>
                                <button
                                    type="button"
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        onRemoveDocument(doc.id);
                                        if (selectedId === doc.id) {
                                            const next = documents.find((d) => d.id !== doc.id);
                                            setSelectedId(next?.id ?? null);
                                        }
                                    }}
                                    className="rounded p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-600"
                                    aria-label={`Remove ${doc.name}`}
                                >
                                    <Trash2 size={14} />
                                </button>
                            </div>
                        ))}
                    </div>
                </div>
                <div className="flex flex-1 flex-col bg-[#F9FAFB]">
                    <div className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-6">
                        <div className="flex items-center gap-3">
                            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500 text-white shadow-sm">
                                <FileText size={18} />
                            </div>
                            <div>
                                <div className="text-sm font-semibold text-gray-900">{title}</div>
                                <div className="text-xs text-gray-500">Ready</div>
                            </div>
                        </div>
                        <button
                            onClick={onClose}
                            className="rounded-full p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                            aria-label="Close"
                        >
                            <X size={16} />
                        </button>
                    </div>
                    <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-6 text-xs text-gray-500">
                        <span className="truncate font-mono">{selectedDoc?.name ?? '—'}</span>
                    </div>
                    <div className="flex-1 overflow-hidden p-6">
                        <div className="flex h-full flex-col overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                            <div className="flex-1 overflow-auto p-4">
                                {!hasDocuments ? (
                                    <div className="flex flex-col items-center justify-center h-full gap-2 text-gray-400 text-sm">
                                        <FileText size={40} className="opacity-50" />
                                        <p>Keine Dokumente. Dokument hinzufügen, um Anhänge zu öffnen.</p>
                                    </div>
                                ) : selectedDoc ? (
                                    isImage && selectedDoc.content?.startsWith('data:') ? (
                                        <img src={selectedDoc.content} alt={selectedDoc.name} className="max-w-full h-auto" />
                                    ) : (
                                        <pre className="whitespace-pre-wrap break-words font-sans text-sm text-gray-800">
                                            {displayContent || '(Kein Textinhalt)'}
                                        </pre>
                                    )
                                ) : null}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
