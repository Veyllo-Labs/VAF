'use client';

/**
 * Memory Detail Panel - View and edit memory content.
 * 
 * Features:
 * - Display decrypted content
 * - Edit metadata and content
 * - Delete confirmation
 * - Tag management
 */

import React, { useState, useEffect } from 'react';
import { useMemoryStore, Memory } from './stores/memoryStore';
import { 
    X, Edit2, Trash2, Save, Tag, Calendar, Link2, 
    ChevronDown, ChevronUp, FileText, AlertTriangle 
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface MemoryDetailPanelProps {
    className?: string;
    onClose?: () => void;
    onToggleExpand?: (expanded: boolean) => void;
}

export default function MemoryDetailPanel({ className, onClose, onToggleExpand }: MemoryDetailPanelProps) {
    const { 
        selectedMemory, 
        selectMemory,
        updateMemory, 
        deleteMemory,
        isLoading,
        error 
    } = useMemoryStore();
    
    const [isEditing, setIsEditing] = useState(false);
    const [editContent, setEditContent] = useState('');
    const [editTitle, setEditTitle] = useState('');
    const [editTags, setEditTags] = useState('');
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [expanded, setExpanded] = useState(true);

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
        
        const success = await deleteMemory(selectedMemory.id, false);
        if (success) {
            onClose?.();
        }
    };
    
    const handleClose = () => {
        selectMemory(null);
        onClose?.();
    };
    
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
                            <ChevronDown className="w-4 h-4 text-gray-50" />
                        ) : (
                            <ChevronUp className="w-4 h-4 text-gray-50" />
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
                            Delete this memory?
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
