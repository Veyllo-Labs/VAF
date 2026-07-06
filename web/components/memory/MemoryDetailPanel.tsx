'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Memory Detail Panel - View and edit memory content.
 * Also displays Tag details when a tag node is selected.
 *
 * Features:
 * - Display decrypted content
 * - Edit metadata and content
 * - Delete confirmation
 * - Tag management
 * - Tag node details (connected memories count, list)
 */

import React, { useState, useEffect, useMemo } from 'react';
import { useMemoryStore, Memory } from './stores/memoryStore';
import {
    X, Edit2, Trash2, Save, Tag, Calendar, Link2,
    ChevronDown, ChevronUp, FileText, AlertTriangle, Hash, CheckSquare, Square
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface MemoryDetailPanelProps {
    className?: string;
    onClose?: () => void;
    onToggleExpand?: (expanded: boolean) => void;
}

const TYPE_LABELS: Record<string, string> = {
    note: 'Notes', document: 'Documents', code: 'Code',
    conversation: 'Conversations', knowledge: 'Knowledge', document_index: 'Document Index',
};

// Tag Details Component
function TagDetailsView({
    tagNode,
    connectedMemories,
    onMemoryClick,
    onDeleteMemories,
    onRemoveTagFromMemories,
    isLoading,
}: {
    tagNode: { id: string; data: { label: string; tag: string; memoryCount: number } };
    connectedMemories: Array<{ id: string; label: string; type?: string }>;
    onMemoryClick: (memoryId: string) => void;
    onDeleteMemories: (ids: string[]) => Promise<void>;
    onRemoveTagFromMemories: (ids: string[], tag: string) => Promise<void>;
    isLoading: boolean;
}) {
    const [deleteMode, setDeleteMode] = useState(false);
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [isDoing, setIsDoing] = useState(false);

    // Group by type
    const grouped = useMemo(() => {
        const map = new Map<string, typeof connectedMemories>();
        for (const m of connectedMemories) {
            const t = m.type ?? 'note';
            if (!map.has(t)) map.set(t, []);
            map.get(t)!.push(m);
        }
        return map;
    }, [connectedMemories]);

    const toggleAll = () => {
        if (selected.size === connectedMemories.length) {
            setSelected(new Set());
        } else {
            setSelected(new Set(connectedMemories.map(m => m.id)));
        }
    };

    const toggle = (id: string) => {
        const next = new Set(selected);
        next.has(id) ? next.delete(id) : next.add(id);
        setSelected(next);
    };

    const handleConfirm = async () => {
        if (selected.size === 0) return;
        setIsDoing(true);
        const toDelete = [...selected];
        const toKeep = connectedMemories.filter(m => !selected.has(m.id)).map(m => m.id);
        await onDeleteMemories(toDelete);
        if (toKeep.length > 0) {
            await onRemoveTagFromMemories(toKeep, tagNode.data.tag);
        }
        setIsDoing(false);
        setDeleteMode(false);
        setSelected(new Set());
    };

    if (deleteMode) {
        return (
            <div className="space-y-3">
                <div className="flex items-center gap-2 pb-3 border-b border-red-200">
                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                    <p className="text-sm font-medium text-red-700">
                        Select memories to delete
                    </p>
                </div>
                <p className="text-xs text-gray-500">
                    Deselected memories are kept — their tag <span className="font-mono">#{tagNode.data.tag}</span> will be removed.
                </p>

                {/* Select all */}
                <button onClick={toggleAll} className="flex items-center gap-2 text-xs text-gray-600 hover:text-gray-900">
                    {selected.size === connectedMemories.length
                        ? <CheckSquare className="w-4 h-4 text-red-500" />
                        : <Square className="w-4 h-4" />}
                    Select all ({selected.size}/{connectedMemories.length})
                </button>

                {/* Grouped list */}
                <div className="max-h-[300px] overflow-y-auto space-y-3">
                    {[...grouped.entries()].map(([type, memories]) => (
                        <div key={type}>
                            <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1">
                                {TYPE_LABELS[type] ?? type}
                            </p>
                            {memories.map(m => (
                                <label key={m.id} className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-gray-50 cursor-pointer">
                                    {selected.has(m.id)
                                        ? <CheckSquare className="w-4 h-4 text-red-500 flex-shrink-0" />
                                        : <Square className="w-4 h-4 text-gray-300 flex-shrink-0" />}
                                    <input type="checkbox" className="sr-only" checked={selected.has(m.id)} onChange={() => toggle(m.id)} />
                                    <span className="text-sm text-gray-700 truncate">{m.label}</span>
                                </label>
                            ))}
                        </div>
                    ))}
                </div>

                {/* Actions */}
                <div className="flex gap-2 pt-2 border-t border-gray-200">
                    <button
                        onClick={handleConfirm}
                        disabled={selected.size === 0 || isDoing}
                        className="flex-1 px-3 py-2 text-xs font-medium bg-red-500 text-white rounded-lg hover:bg-red-600 disabled:opacity-40 transition-colors"
                    >
                        {isDoing ? 'Working...' : `Delete ${selected.size}`}
                    </button>
                    <button
                        onClick={() => { setDeleteMode(false); setSelected(new Set()); }}
                        className="px-3 py-2 text-xs font-medium bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
                    >
                        Cancel
                    </button>
                </div>
            </div>
        );
    }

    return (
        <div className="space-y-4">
            {/* Tag Info */}
            <div className="flex items-center justify-between pb-3 border-b border-gray-200">
                <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-purple-100 flex items-center justify-center">
                        <Hash className="w-5 h-5 text-purple-600" />
                    </div>
                    <div>
                        <h4 className="font-medium text-gray-900">{tagNode.data.label}</h4>
                        <p className="text-xs text-gray-500">Tag Master Node</p>
                    </div>
                </div>
                <button
                    onClick={() => { setDeleteMode(true); setSelected(new Set(connectedMemories.map(m => m.id))); }}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-red-600 border border-red-200 rounded-lg hover:bg-red-50 transition-colors"
                >
                    <Trash2 className="w-3.5 h-3.5" />
                    Delete tag
                </button>
            </div>

            {/* Stats */}
            <div className="grid grid-cols-2 gap-3">
                <div className="p-3 bg-purple-50 rounded-lg">
                    <div className="text-2xl font-bold text-purple-700">{tagNode.data.memoryCount || 0}</div>
                    <div className="text-xs text-purple-600">Connected Memories</div>
                </div>
                <div className="p-3 bg-gray-50 rounded-lg">
                    <div className="text-2xl font-bold text-gray-700">{connectedMemories.length}</div>
                    <div className="text-xs text-gray-600">Visible Connections</div>
                </div>
            </div>

            {/* Connected Memories List */}
            <div>
                <label className="block text-xs font-medium text-gray-500 mb-2">
                    <Link2 className="w-3 h-3 inline mr-1" />
                    Connected Memories
                </label>
                <div className="max-h-[200px] overflow-y-auto space-y-1">
                    {connectedMemories.length > 0 ? (
                        connectedMemories.map((memory) => (
                            <button
                                key={memory.id}
                                onClick={() => onMemoryClick(memory.id)}
                                className="w-full text-left px-3 py-2 rounded-lg hover:bg-gray-100 transition-colors flex items-center gap-2"
                            >
                                <FileText className="w-4 h-4 text-gray-400 flex-shrink-0" />
                                <span className="text-sm text-gray-700 truncate">{memory.label}</span>
                                {memory.type && (
                                    <span className="text-[10px] px-1.5 py-0.5 bg-gray-200 text-gray-500 rounded ml-auto">
                                        {TYPE_LABELS[memory.type] ?? memory.type}
                                    </span>
                                )}
                            </button>
                        ))
                    ) : (
                        <p className="text-xs text-gray-400 text-center py-4">No connected memories</p>
                    )}
                </div>
            </div>

            <div className="p-3 bg-purple-50 rounded-lg border border-purple-100">
                <p className="text-xs text-purple-700">
                    <span className="font-medium">Tip:</span> Drag from a memory node to this tag to add the tag to that memory.
                </p>
            </div>
        </div>
    );
}

export default function MemoryDetailPanel({ className, onClose, onToggleExpand }: MemoryDetailPanelProps) {
    const {
        selectedMemory,
        selectedNodeId,
        nodes,
        edges,
        selectMemory,
        setSelectedNodeId,
        updateMemory,
        deleteMemory,
        deleteByDocTag,
        removeTagFromMemory,
        isLoading,
        error
    } = useMemoryStore();

    const [isEditing, setIsEditing] = useState(false);
    const [editContent, setEditContent] = useState('');
    const [editTitle, setEditTitle] = useState('');
    const [editTags, setEditTags] = useState('');
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [expanded, setExpanded] = useState(true);

    // Determine if selected node is a tag (match API type and fallback by id prefix)
    const selectedNode = useMemo(() => {
        const found = nodes.find(n => n.id === selectedNodeId);
        if (found) return found;
        // Fallback: build minimal tag node from id so Tag Details still show (e.g. after refresh)
        if (selectedNodeId?.startsWith('tag-')) {
            const tag = selectedNodeId.replace(/^tag-/, '');
            return {
                id: selectedNodeId,
                type: 'tagNode',
                position: { x: 0, y: 0 },
                data: { label: `#${tag}`, tag, memoryCount: 0, isTagNode: true },
            } as typeof nodes[0];
        }
        return undefined;
    }, [nodes, selectedNodeId]);

    const isTagSelected = !!(selectedNode && (selectedNode.type === 'tagNode' || selectedNode.data?.isTagNode));

    // Get connected memories for tag node
    const connectedMemories = useMemo(() => {
        if (!isTagSelected || !selectedNodeId) return [];

        // Find all edges connected to this tag
        const connectedNodeIds = new Set<string>();
        edges.forEach(edge => {
            if (edge.source === selectedNodeId) {
                connectedNodeIds.add(edge.target);
            }
            if (edge.target === selectedNodeId) {
                connectedNodeIds.add(edge.source);
            }
        });

        // Get node info for connected memories
        return nodes
            .filter(n => n.type === 'memoryNode' && connectedNodeIds.has(n.id))
            .map(n => ({
                id: n.id,
                label: n.data.label,
                type: n.data.type,
            }));
    }, [isTagSelected, selectedNodeId, edges, nodes]);

    const toggleExpand = () => {
        const next = !expanded;
        setExpanded(next);
        onToggleExpand?.(next);
    };

    // Update local state when selected memory changes
    useEffect(() => {
        if (selectedMemory) {
            setEditContent(selectedMemory.content || '');
            setEditTitle(selectedMemory.metadata?.title || '');
            setEditTags(selectedMemory.metadata?.tags?.join(', ') || '');
            setIsEditing(false);
            setShowDeleteConfirm(false);
            setExpanded(true);
            onToggleExpand?.(true);
        }
    }, [selectedMemory]);

    // Handle click on connected memory from tag view
    const handleMemoryClick = (memoryId: string) => {
        setSelectedNodeId(memoryId);
        selectMemory(memoryId);
    };

    const handleSave = async () => {
        if (!selectedMemory) return;

        const tags = editTags.split(',').map(t => t.trim()).filter(Boolean);

        await updateMemory(
            selectedMemory.id,
            editContent !== selectedMemory.content ? editContent : undefined,
            { title: editTitle, tags }
        );

        setIsEditing(false);
    };

    const handleDelete = async () => {
        if (!selectedMemory) return;

        if (selectedMemory.metadata?.type === 'document_index') {
            const docTag = selectedMemory.metadata?.doc_tag;
            if (!docTag) return;
            const count = await deleteByDocTag(docTag, false);
            if (count > 0) onClose?.();
        } else {
            const success = await deleteMemory(selectedMemory.id, false);
            if (success) onClose?.();
        }
    };

    const handleClose = () => {
        selectMemory(null);
        setSelectedNodeId(null);
        onClose?.();
    };

    // Show tag details if a tag node is selected
    if (isTagSelected && selectedNode) {
        return (
            <div className={cn('bg-white rounded-xl border border-gray-200 overflow-hidden', className)}>
                {/* Header */}
                <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 bg-purple-50">
                    <div className="flex items-center gap-2">
                        <button
                            onClick={toggleExpand}
                            className="p-2 hover:bg-purple-100 rounded-lg transition-colors"
                            title={expanded ? 'Collapse' : 'Expand'}
                        >
                            {expanded ? (
                                <ChevronDown className="w-4 h-4 text-purple-600" />
                            ) : (
                                <ChevronUp className="w-4 h-4 text-purple-600" />
                            )}
                        </button>
                        <h3 className="font-medium text-purple-800">Tag Details</h3>
                    </div>
                    <button
                        onClick={handleClose}
                        className="p-2 hover:bg-purple-100 rounded-lg transition-colors text-purple-600"
                        title="Close"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {/* Content */}
                {expanded && (
                    <div className="flex-1 p-4 overflow-y-auto">
                        <TagDetailsView
                            tagNode={selectedNode as any}
                            connectedMemories={connectedMemories}
                            onMemoryClick={handleMemoryClick}
                            isLoading={isLoading}
                            onDeleteMemories={async (ids) => {
                                for (const id of ids) await deleteMemory(id, false);
                            }}
                            onRemoveTagFromMemories={async (ids, tag) => {
                                for (const id of ids) await removeTagFromMemory(id, tag);
                            }}
                        />
                    </div>
                )}
            </div>
        );
    }

    // Show placeholder if nothing selected
    if (!selectedMemory) {
        return (
            <div className={cn('bg-white rounded-xl border border-gray-200 p-6', className)}>
                <div className="text-center text-gray-500">
                    <FileText className="w-12 h-12 mx-auto mb-3 text-gray-300" />
                    <p className="text-sm">Select a memory to view details</p>
                    {error && (
                        <p className="mt-2 text-sm text-amber-600" title={error}>
                            {error}
                        </p>
                    )}
                </div>
            </div>
        );
    }

    // Show memory details
    return (
        <div className={cn('bg-white rounded-xl border border-gray-200 overflow-hidden', className)}>
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 bg-gray-50">
                <div className="flex items-center gap-2">
                    <button
                        onClick={toggleExpand}
                        className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
                        title={expanded ? 'Collapse' : 'Expand'}
                    >
                        {expanded ? (
                            <ChevronDown className="w-4 h-4 text-gray-500" />
                        ) : (
                            <ChevronUp className="w-4 h-4 text-gray-500" />
                        )}
                    </button>
                    <h3 className="font-medium text-gray-800">Memory Details</h3>
                </div>
                <div className="flex items-center gap-1">
                    {!isEditing ? (
                        <>
                            <button
                                onClick={() => setIsEditing(true)}
                                className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
                                title="Edit"
                            >
                                <Edit2 className="w-4 h-4" />
                            </button>
                            <button
                                onClick={() => setShowDeleteConfirm(true)}
                                className="p-2 hover:bg-red-100 rounded-lg transition-colors text-red-600"
                                title="Delete"
                            >
                                <Trash2 className="w-4 h-4" />
                            </button>
                        </>
                    ) : (
                        <button
                            onClick={handleSave}
                            disabled={isLoading}
                            className="p-2 hover:bg-green-100 rounded-lg transition-colors text-green-700"
                            title="Save"
                        >
                            <Save className="w-4 h-4" />
                        </button>
                    )}
                    <button
                        onClick={handleClose}
                        className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
                        title="Close"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>
            </div>

            {/* Delete confirmation */}
            {showDeleteConfirm && (
                <div className="px-4 py-3 bg-red-50 border-b border-red-200">
                    <div className="flex items-center gap-2 mb-2">
                        <AlertTriangle className="w-4 h-4 text-red-600" />
                        <span className="text-sm font-medium text-red-800">
                            {selectedMemory?.metadata?.type === 'document_index'
                                ? `Delete entire document «${selectedMemory.metadata.title}»? This removes all ${selectedMemory.metadata.page_count ?? '?'} pages from long-term memory.`
                                : 'Delete this memory?'
                            }
                        </span>
                    </div>
                    <div className="flex gap-2">
                        <button
                            onClick={handleDelete}
                            disabled={isLoading}
                            className="px-3 py-1 text-xs font-medium bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors disabled:opacity-50"
                        >
                            {isLoading ? 'Deleting...' : 'Delete'}
                        </button>
                        <button
                            onClick={() => setShowDeleteConfirm(false)}
                            className="px-3 py-1 text-xs font-medium bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
                        >
                            Cancel
                        </button>
                    </div>
                </div>
            )}

            {/* Content */}
            {expanded && (
                <div className="flex-1 p-4 space-y-4 overflow-y-auto">
                    {/* Error display */}
                    {error && (
                        <div className="p-2 bg-red-50 border border-red-200 rounded text-sm text-red-700">
                            {error}
                        </div>
                    )}

                    {/* IDs (Memory ID and User scope – for verifying RAG scope consistency) */}
                    <div className="grid grid-cols-1 gap-3 pb-3 border-b border-gray-200">
                        <div>
                            <label className="block text-xs font-medium text-gray-500 mb-1">
                                Memory ID
                            </label>
                            <p className="text-xs font-mono text-gray-700 break-all" title={selectedMemory.id}>
                                {selectedMemory.id}
                            </p>
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-gray-500 mb-1">
                                User scope ID
                            </label>
                            <p className="text-xs font-mono text-gray-700 break-all" title={selectedMemory.user_scope_id ?? '(none)'}>
                                {selectedMemory.user_scope_id && selectedMemory.user_scope_id !== ''
                                    ? selectedMemory.user_scope_id
                                    : '(none – global scope)'}
                            </p>
                        </div>
                    </div>

                    {/* Title */}
                    <div>
                        <label className="block text-xs font-medium text-gray-500 mb-1">
                            Title
                        </label>
                        {isEditing ? (
                            <input
                                type="text"
                                value={editTitle}
                                onChange={(e) => setEditTitle(e.target.value)}
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                        ) : (
                            <p className="text-sm text-gray-800 font-medium">
                                {selectedMemory.metadata?.title || 'Untitled'}
                            </p>
                        )}
                    </div>

                    {/* Tags */}
                    <div>
                        <label className="block text-xs font-medium text-gray-500 mb-1">
                            <Tag className="w-3 h-3 inline mr-1" />
                            Tags
                        </label>
                        {isEditing ? (
                            <input
                                type="text"
                                value={editTags}
                                onChange={(e) => setEditTags(e.target.value)}
                                placeholder="tag1, tag2, tag3"
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                        ) : (
                            <div className="flex flex-wrap gap-1">
                                {selectedMemory.metadata?.tags?.map((tag, idx) => (
                                    <span
                                        key={idx}
                                        className="inline-flex items-center px-2 py-0.5 rounded text-xs bg-gray-100 text-gray-700"
                                    >
                                        {tag}
                                    </span>
                                )) || (
                                    <span className="text-xs text-gray-400">No tags</span>
                                )}
                            </div>
                        )}
                    </div>

                    {/* Content */}
                    <div>
                        <label className="block text-xs font-medium text-gray-500 mb-1">
                            Content
                        </label>
                        {isEditing ? (
                            <textarea
                                value={editContent}
                                onChange={(e) => setEditContent(e.target.value)}
                                rows={10}
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                        ) : (
                            <div className="p-3 bg-gray-50 rounded-lg border border-gray-200 max-h-[300px] overflow-y-auto">
                                <pre className="text-xs text-gray-700 whitespace-pre-wrap font-mono">
                                    {selectedMemory.content || 'Content not available'}
                                </pre>
                            </div>
                        )}
                    </div>

                    {/* Metadata */}
                    <div className="grid grid-cols-2 gap-4 pt-3 border-t border-gray-200">
                        <div>
                            <label className="block text-xs font-medium text-gray-500 mb-1">
                                <Calendar className="w-3 h-3 inline mr-1" />
                                Created
                            </label>
                            <p className="text-xs text-gray-600">
                                {selectedMemory.created_at
                                    ? new Date(selectedMemory.created_at).toLocaleString()
                                    : 'Unknown'}
                            </p>
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-gray-500 mb-1">
                                <Calendar className="w-3 h-3 inline mr-1" />
                                Updated
                            </label>
                            <p className="text-xs text-gray-600">
                                {selectedMemory.updated_at
                                    ? new Date(selectedMemory.updated_at).toLocaleString()
                                    : 'Never'}
                            </p>
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-gray-500 mb-1">
                                <Link2 className="w-3 h-3 inline mr-1" />
                                Chunks
                            </label>
                            <p className="text-xs text-gray-600">
                                {selectedMemory.chunk_count} chunks
                            </p>
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-gray-500 mb-1">
                                Type
                            </label>
                            <p className="text-xs text-gray-600">
                                {selectedMemory.metadata?.type || 'note'}
                            </p>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
