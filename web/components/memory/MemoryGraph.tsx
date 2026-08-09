'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Memory Graph on Sigma.js (WebGL) + graphology ForceAtlas2.
 *
 * The previous ReactFlow renderer rebuilt hundreds of DOM nodes on every
 * selection and hid everything beyond a 100-node window. This renderer draws
 * the WHOLE store (limit=0) the way large-graph tools do (Gephi/Obsidian
 * pattern): force-directed layout, node size by degree, labels appear on
 * zoom, selection and hover restyle via reducers - the graph itself is never
 * rebuilt for a click.
 *
 * Deliberate boundaries: no drag-persisted positions (the force layout owns
 * placement, recomputed per load with seeded randomness), and no draw-an-edge
 * tag linking (the Link Tags modal covers that path).
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Graph from 'graphology';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import type Sigma from 'sigma';
import { useMemoryStore } from './stores/memoryStore';
import { FileText, Maximize2, RefreshCw, ZoomIn, ZoomOut } from 'lucide-react';
import { cn } from '@/lib/utils';

const TYPE_COLORS: Record<string, string> = {
    note: '#60a5fa',
    conversation: '#fb923c',
    memory_flush: '#fb923c',
    document: '#c084fc',
    code: '#4ade80',
    knowledge: '#14b8a6',
    document_index: '#f59e0b',
};
const DEFAULT_COLOR = '#9ca3af';
const TAG_COLOR = '#8b5cf6';
const FADED_COLOR = '#d1d5db';
const HIGHLIGHT_COLOR = '#f97316';

const LEGEND: Array<{ type: string; label: string; color: string }> = [
    { type: 'note', label: 'Note', color: TYPE_COLORS.note },
    { type: 'conversation', label: 'Conversation', color: TYPE_COLORS.conversation },
    { type: 'document', label: 'Document', color: TYPE_COLORS.document },
    { type: 'code', label: 'Code', color: TYPE_COLORS.code },
    { type: 'knowledge', label: 'Knowledge', color: TYPE_COLORS.knowledge },
    { type: 'other', label: 'Other', color: DEFAULT_COLOR },
];
const KNOWN_TYPES = new Set(Object.keys(TYPE_COLORS));

// Deterministic per-node start positions: the layout is reproducible across
// loads without persisting anything.
function seededXY(id: string): { x: number; y: number } {
    let h = 2166136261;
    for (let i = 0; i < id.length; i++) {
        h ^= id.charCodeAt(i);
        h = Math.imul(h, 16777619);
    }
    const a = ((h >>> 0) % 10000) / 10000;
    const b = (Math.imul(h, 48271) >>> 0) % 10000 / 10000;
    return { x: (a - 0.5) * 100, y: (b - 0.5) * 100 };
}

function legendKey(memType: string | undefined): string {
    if (!memType) return 'other';
    if (memType === 'memory_flush') return 'conversation';
    if (memType === 'document_index') return 'document';
    return KNOWN_TYPES.has(memType) ? memType : 'other';
}

interface MemoryGraphProps {
    className?: string;
    onNodeSelect?: (nodeId: string | null) => void;
    showTagConnections?: boolean;
}

export default function MemoryGraph({ className, onNodeSelect, showTagConnections = true }: MemoryGraphProps) {
    const {
        nodes: storeNodes,
        edges: storeEdges,
        selectedNodeId,
        setSelectedNodeId,
        selectMemory,
        isLoading,
        stats,
        fetchGraph,
    } = useMemoryStore();

    const containerRef = useRef<HTMLDivElement | null>(null);
    const sigmaRef = useRef<Sigma | null>(null);
    const graphRef = useRef<Graph | null>(null);

    // Reducer inputs live in refs: changing them restyles via refresh() and
    // never rebuilds graph or renderer (the ReactFlow version rebuilt every
    // node object per click - the click latency the rewrite removes).
    const selectedRef = useRef<string | null>(null);
    const neighborsRef = useRef<Set<string>>(new Set());
    const hoveredRef = useRef<string | null>(null);
    const hoverNeighborsRef = useRef<Set<string>>(new Set());
    const showTagEdgesRef = useRef<boolean>(showTagConnections);
    const hiddenTypesRef = useRef<Set<string>>(new Set());

    const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());

    // ── Build graph + renderer when the DATA changes ────────────────────────
    useEffect(() => {
        if (!containerRef.current || storeNodes.length === 0) return;
        let cancelled = false;

        // Theme-aware palette, resolved when the renderer is (re)built: the
        // default gray labels were unreadable on the dark background.
        const isDark = typeof document !== 'undefined'
            && document.documentElement.classList.contains('dark');
        const faded = isDark ? '#3f3f46' : FADED_COLOR;
        const labelCol = isDark ? '#d4d4d8' : '#4b5563';
        const edgeTag = isDark ? 'rgba(167,139,250,0.25)' : 'rgba(139,92,246,0.16)';
        const edgeSem = isDark ? 'rgba(161,161,170,0.30)' : 'rgba(107,114,128,0.28)';
        const edgeTagHot = isDark ? 'rgba(196,181,253,0.75)' : 'rgba(139,92,246,0.55)';
        const edgeSemHot = isDark ? 'rgba(212,212,216,0.80)' : 'rgba(75,85,99,0.7)';
        const edgeDim = isDark ? 'rgba(63,63,70,0.15)' : 'rgba(209,213,219,0.12)';

        const graph = new Graph();
        for (const n of storeNodes) {
            const isTag = n.type === 'tagNode' || n.data.isTagNode;
            const { x, y } = seededXY(n.id);
            graph.addNode(n.id, {
                x, y,
                label: isTag ? n.data.label : (n.data.label || 'Untitled').slice(0, 60),
                isTag,
                memType: n.data.type || 'note',
                docTag: (n.data as { docTag?: string }).docTag || '',
                isHighlighted: !!n.data.isHighlighted,
                memoryCount: n.data.memoryCount || 0,
                color: isTag ? TAG_COLOR : (TYPE_COLORS[n.data.type || ''] || DEFAULT_COLOR),
                size: 3,
            });
        }
        for (const e of storeEdges) {
            if (!graph.hasNode(e.source) || !graph.hasNode(e.target)) continue;
            if (graph.hasEdge(e.source, e.target)) continue;
            graph.addEdge(e.source, e.target, {
                kind: e.data?.connectionType || 'semantic',
                weight: e.data?.strength || 0.5,
            });
        }
        // Node size from structure: degree for memories, membership for tags.
        graph.forEachNode((id, attrs) => {
            const deg = graph.degree(id);
            graph.setNodeAttribute(id, 'size', attrs.isTag
                ? Math.min(14, 4 + Math.sqrt(attrs.memoryCount || deg) * 1.6)
                : Math.min(12, 3 + Math.sqrt(deg) * 1.2));
        });

        const settings = forceAtlas2.inferSettings(graph);
        forceAtlas2.assign(graph, {
            iterations: Math.min(500, 120 + graph.order),
            settings: { ...settings, scalingRatio: 8, slowDown: 5 },
        });
        graphRef.current = graph;

        (async () => {
            // Sigma touches window/WebGL - import stays out of the SSR pass.
            const { default: SigmaCtor } = await import('sigma');
            if (cancelled || !containerRef.current) return;
            sigmaRef.current?.kill();
            const renderer = new SigmaCtor(graph, containerRef.current, {
                renderLabels: true,
                labelRenderedSizeThreshold: 7,
                labelFont: 'Inter, system-ui, sans-serif',
                labelSize: 12,
                labelWeight: '500',
                labelColor: { color: labelCol },
                defaultEdgeType: 'line',
                minCameraRatio: 0.03,
                maxCameraRatio: 6,
                nodeReducer: (id, attrs) => {
                    const res: Record<string, unknown> = { ...attrs };
                    if (!attrs.isTag && hiddenTypesRef.current.has(legendKey(attrs.memType as string))) {
                        res.hidden = true;
                        return res;
                    }
                    const sel = selectedRef.current;
                    const hov = hoveredRef.current;
                    if (attrs.isHighlighted) {
                        res.color = HIGHLIGHT_COLOR;
                        res.size = (attrs.size as number) + 2;
                    }
                    if (sel) {
                        if (id === sel) {
                            res.size = (res.size as number) + 3;
                            res.zIndex = 2;
                        } else if (!neighborsRef.current.has(id)) {
                            res.color = faded;
                            res.label = '';
                        }
                    } else if (hov) {
                        if (id === hov) {
                            res.size = (res.size as number) + 2;
                            res.zIndex = 2;
                        } else if (!hoverNeighborsRef.current.has(id)) {
                            res.color = faded;
                        }
                    }
                    return res;
                },
                edgeReducer: (id, attrs) => {
                    const res: Record<string, unknown> = { ...attrs };
                    const g = graphRef.current;
                    if (attrs.kind === 'tag' && !showTagEdgesRef.current) {
                        res.hidden = true;
                        return res;
                    }
                    res.color = attrs.kind === 'tag' ? edgeTag : edgeSem;
                    res.size = attrs.kind === 'tag' ? 0.6 : 1;
                    const focus = selectedRef.current || hoveredRef.current;
                    if (focus && g) {
                        const [s, t] = g.extremities(id);
                        if (s === focus || t === focus) {
                            res.color = attrs.kind === 'tag' ? edgeTagHot : edgeSemHot;
                            res.size = (res.size as number) + 0.6;
                            res.zIndex = 1;
                        } else {
                            res.color = edgeDim;
                        }
                    }
                    return res;
                },
            });

            renderer.on('clickNode', ({ node }) => {
                selectedRef.current = node;
                neighborsRef.current = new Set(graph.neighbors(node));
                setSelectedNodeId(node);
                if (!graph.getNodeAttribute(node, 'isTag')) {
                    selectMemory(node);
                }
                onNodeSelect?.(node);
                renderer.refresh();
            });
            renderer.on('clickStage', () => {
                selectedRef.current = null;
                neighborsRef.current = new Set();
                setSelectedNodeId(null);
                selectMemory(null);
                onNodeSelect?.(null);
                renderer.refresh();
            });
            renderer.on('enterNode', ({ node }) => {
                hoveredRef.current = node;
                hoverNeighborsRef.current = new Set(graph.neighbors(node));
                renderer.refresh();
            });
            renderer.on('leaveNode', () => {
                hoveredRef.current = null;
                hoverNeighborsRef.current = new Set();
                renderer.refresh();
            });

            sigmaRef.current = renderer;
        })();

        return () => {
            cancelled = true;
            sigmaRef.current?.kill();
            sigmaRef.current = null;
            graphRef.current = null;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [storeNodes, storeEdges]);

    // ── Restyle-only inputs ────────────────────────────────────────────────
    useEffect(() => {
        showTagEdgesRef.current = showTagConnections;
        sigmaRef.current?.refresh();
    }, [showTagConnections]);

    useEffect(() => {
        hiddenTypesRef.current = hiddenTypes;
        sigmaRef.current?.refresh();
    }, [hiddenTypes]);

    useEffect(() => {
        selectedRef.current = selectedNodeId;
        const g = graphRef.current;
        neighborsRef.current = selectedNodeId && g?.hasNode(selectedNodeId)
            ? new Set(g.neighbors(selectedNodeId))
            : new Set();
        sigmaRef.current?.refresh();
    }, [selectedNodeId]);

    const toggleType = useCallback((type: string) => {
        setHiddenTypes((prev) => {
            const next = new Set(prev);
            if (next.has(type)) next.delete(type);
            else next.add(type);
            return next;
        });
    }, []);

    const zoom = useCallback((dir: 'in' | 'out' | 'fit') => {
        const cam = sigmaRef.current?.getCamera();
        if (!cam) return;
        if (dir === 'fit') cam.animatedReset({ duration: 300 });
        else if (dir === 'in') cam.animatedZoom({ duration: 200 });
        else cam.animatedUnzoom({ duration: 200 });
    }, []);

    if (isLoading && storeNodes.length === 0) {
        return (
            <div className={cn('flex items-center justify-center bg-gray-50 rounded-xl', className)}>
                <div className="text-center">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-400 mx-auto mb-2" />
                    <p className="text-sm text-gray-500">Loading memory graph...</p>
                </div>
            </div>
        );
    }

    if (storeNodes.length === 0) {
        const graphFailed = (stats?.memories ?? 0) > 0;
        return (
            <div className={cn('flex items-center justify-center bg-gray-50 rounded-xl', className)}>
                <div className="text-center p-8">
                    <FileText className="w-12 h-12 text-gray-300 mx-auto mb-3" />
                    {graphFailed ? (
                        <>
                            <h3 className="text-lg font-medium text-gray-700 mb-1">Graph couldn&apos;t load memories</h3>
                            <p className="text-sm text-gray-500 mb-4">
                                Check the connection and try Refresh.
                            </p>
                            <button
                                type="button"
                                onClick={() => fetchGraph()}
                                disabled={isLoading}
                                className="inline-flex items-center gap-2 px-4 py-2 bg-gray-900 hover:bg-gray-800 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                            >
                                <RefreshCw className={cn('w-4 h-4', isLoading && 'animate-spin')} />
                                Retry
                            </button>
                        </>
                    ) : (
                        <>
                            <h3 className="text-lg font-medium text-gray-700 mb-1">No memories yet</h3>
                            <p className="text-sm text-gray-500">
                                Memories appear here as you chat or add them manually.
                            </p>
                        </>
                    )}
                </div>
            </div>
        );
    }

    return (
        <div className={cn('relative bg-gray-50 rounded-xl overflow-hidden', className)}>
            <div ref={containerRef} className="absolute inset-0" />

            {/* Zoom controls */}
            <div className="absolute bottom-3 left-3 z-10 flex flex-col gap-1 bg-white/90 border border-gray-200 rounded-lg p-1 shadow-sm">
                <button type="button" onClick={() => zoom('in')} title="Zoom in"
                        className="p-1.5 rounded hover:bg-gray-100 text-gray-600">
                    <ZoomIn className="w-4 h-4" />
                </button>
                <button type="button" onClick={() => zoom('out')} title="Zoom out"
                        className="p-1.5 rounded hover:bg-gray-100 text-gray-600">
                    <ZoomOut className="w-4 h-4" />
                </button>
                <button type="button" onClick={() => zoom('fit')} title="Fit view"
                        className="p-1.5 rounded hover:bg-gray-100 text-gray-600">
                    <Maximize2 className="w-4 h-4" />
                </button>
            </div>

            {/* Legend - click a type to hide/show it */}
            <div className="absolute top-3 left-3 z-10 bg-white/90 border border-gray-200 rounded-lg px-3 py-2 shadow-sm">
                <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-1.5">Legend</p>
                <div className="space-y-1">
                    {LEGEND.map((item) => (
                        <button
                            key={item.type}
                            type="button"
                            onClick={() => toggleType(item.type)}
                            title={hiddenTypes.has(item.type) ? `Show ${item.label}` : `Hide ${item.label}`}
                            className={cn(
                                'flex items-center gap-2 text-xs text-gray-600 w-full text-left rounded px-1 py-0.5 hover:bg-gray-100',
                                hiddenTypes.has(item.type) && 'opacity-35 line-through'
                            )}
                        >
                            <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: item.color }} />
                            {item.label}
                        </button>
                    ))}
                    <div className="flex items-center gap-2 text-xs text-gray-600 px-1 py-0.5">
                        <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: TAG_COLOR }} />
                        Tag
                    </div>
                </div>
            </div>

            {/* Node count - the honest scale of what is on screen */}
            <div className="absolute bottom-3 right-3 z-10 bg-white/90 border border-gray-200 rounded-lg px-2.5 py-1 shadow-sm">
                <p className="text-[11px] text-gray-500">
                    {storeNodes.filter((n) => n.type !== 'tagNode' && !n.data.isTagNode).length} memories
                </p>
            </div>
        </div>
    );
}
