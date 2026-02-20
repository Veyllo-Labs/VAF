'use client';

/**
 * Memory Graph visualization using ReactFlow.
 *
 * Features:
 * - Custom memory nodes with preview
 * - Edge thickness based on connection strength
 * - Node highlighting for RAG sources
 * - Interactive selection and navigation
 * - Position persistence (localStorage)
 * - Connected node highlighting with fade effect
 * - Collision detection for node positioning
 */

import React, { useCallback, useMemo, useState, useEffect, useRef } from 'react';
import ReactFlow, {
    Node,
    Edge,
    Background,
    Controls,
    MiniMap,
    useNodesState,
    useEdgesState,
    NodeTypes,
    NodeProps,
    Handle,
    Position,
    MarkerType,
    Connection,
    ConnectionLineType,
    getSmoothStepPath,
    NodeChange,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { useMemoryStore, MemoryNode, MemoryEdge } from './stores/memoryStore';
import { FileText, Tag, Calendar, Link2, RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';

// Constants for collision detection
const NODE_WIDTH = 240;
const NODE_HEIGHT = 120;
const TAG_NODE_WIDTH = 150;
const TAG_NODE_HEIGHT = 60;
const COLLISION_PADDING = 20;

// localStorage key for positions
const POSITIONS_STORAGE_KEY = 'vaf-memory-graph-positions';

// Helper: Load saved positions from localStorage
function loadSavedPositions(): Record<string, { x: number; y: number }> {
    if (typeof window === 'undefined') return {};
    try {
        const saved = localStorage.getItem(POSITIONS_STORAGE_KEY);
        return saved ? JSON.parse(saved) : {};
    } catch {
        return {};
    }
}

// Helper: Save positions to localStorage
function savePositions(positions: Record<string, { x: number; y: number }>) {
    if (typeof window === 'undefined') return;
    try {
        localStorage.setItem(POSITIONS_STORAGE_KEY, JSON.stringify(positions));
    } catch {
        // Ignore storage errors
    }
}

// Helper: Check if two rectangles overlap
function rectsOverlap(
    r1: { x: number; y: number; width: number; height: number },
    r2: { x: number; y: number; width: number; height: number }
): boolean {
    return !(
        r1.x + r1.width + COLLISION_PADDING < r2.x ||
        r2.x + r2.width + COLLISION_PADDING < r1.x ||
        r1.y + r1.height + COLLISION_PADDING < r2.y ||
        r2.y + r2.height + COLLISION_PADDING < r1.y
    );
}

// Helper: Apply collision detection to prevent overlapping
function applyCollisionDetection(nodes: Node[]): Node[] {
    const result = [...nodes];
    const maxIterations = 50;

    for (let iter = 0; iter < maxIterations; iter++) {
        let hasCollision = false;

        for (let i = 0; i < result.length; i++) {
            const nodeA = result[i];
            const widthA = nodeA.type === 'tagNode' ? TAG_NODE_WIDTH : NODE_WIDTH;
            const heightA = nodeA.type === 'tagNode' ? TAG_NODE_HEIGHT : NODE_HEIGHT;

            for (let j = i + 1; j < result.length; j++) {
                const nodeB = result[j];
                const widthB = nodeB.type === 'tagNode' ? TAG_NODE_WIDTH : NODE_WIDTH;
                const heightB = nodeB.type === 'tagNode' ? TAG_NODE_HEIGHT : NODE_HEIGHT;

                const rectA = { x: nodeA.position.x, y: nodeA.position.y, width: widthA, height: heightA };
                const rectB = { x: nodeB.position.x, y: nodeB.position.y, width: widthB, height: heightB };

                if (rectsOverlap(rectA, rectB)) {
                    hasCollision = true;

                    // Calculate push direction
                    const centerAX = rectA.x + rectA.width / 2;
                    const centerAY = rectA.y + rectA.height / 2;
                    const centerBX = rectB.x + rectB.width / 2;
                    const centerBY = rectB.y + rectB.height / 2;

                    const dx = centerBX - centerAX;
                    const dy = centerBY - centerAY;
                    const dist = Math.sqrt(dx * dx + dy * dy) || 1;

                    // Push apart
                    const pushForce = 30;
                    const pushX = (dx / dist) * pushForce;
                    const pushY = (dy / dist) * pushForce;

                    result[i] = {
                        ...result[i],
                        position: {
                            x: result[i].position.x - pushX,
                            y: result[i].position.y - pushY
                        }
                    };
                    result[j] = {
                        ...result[j],
                        position: {
                            x: result[j].position.x + pushX,
                            y: result[j].position.y + pushY
                        }
                    };
                }
            }
        }

        if (!hasCollision) break;
    }

    return result;
}

// Type-to-stroke-color map (matches node border colors)
const TYPE_STROKE_COLORS: Record<string, string> = {
    note: '#60a5fa',
    conversation: '#fb923c',
    memory_flush: '#fb923c',
    document: '#c084fc',
    code: '#4ade80',
};
const DEFAULT_STROKE = '#9ca3af';

// Custom ConnectionLine: stroke color matches source memory type (orange memory → orange line)
function MemoryConnectionLine(props: {
    fromNode?: { data?: { type?: string; isTagNode?: boolean } };
    fromX: number;
    fromY: number;
    toX: number;
    toY: number;
    fromPosition: Position;
    toPosition: Position;
}) {
    const { fromNode, fromX, fromY, toX, toY, fromPosition, toPosition } = props;
    const memoryType = fromNode?.data?.isTagNode ? null : (fromNode?.data?.type ?? 'default');
    const stroke = memoryType ? (TYPE_STROKE_COLORS[memoryType] ?? DEFAULT_STROKE) : '#8b5cf6';
    const [path] = getSmoothStepPath({ sourceX: fromX, sourceY: fromY, targetX: toX, targetY: toY, sourcePosition: fromPosition, targetPosition: toPosition });
    return (
        <g>
            <path fill="none" stroke={stroke} strokeWidth={2} d={path} />
            <circle cx={toX} cy={toY} fill="#fff" r={3} stroke={stroke} strokeWidth={2} />
        </g>
    );
}

// Custom Memory Node Component
const MemoryNodeComponent = ({ data, selected }: NodeProps) => {
    const typeColors: Record<string, string> = {
        note: 'border-blue-400 bg-blue-50',           // memory_save, auto_capture
        document: 'border-purple-400 bg-purple-50',
        code: 'border-green-400 bg-green-50',
        conversation: 'border-orange-400 bg-orange-50',  // compaction (15 msgs)
        memory_flush: 'border-orange-400 bg-orange-50',  // legacy compaction – same as conversation
        default: 'border-gray-400 bg-gray-50'
    };

    const typeColor = typeColors[data.type] || typeColors.default;
    const isFaded = data.isFaded;

    return (
        <div
            className={cn(
                'min-w-[200px] max-w-[280px] rounded-xl border-2 shadow-sm transition-all duration-300',
                typeColor,
                selected && 'ring-2 ring-gray-400 shadow-md',
                data.isHighlighted && 'ring-2 ring-yellow-500 shadow-lg scale-105 z-10'
            )}
            style={{ opacity: isFaded ? 0.4 : 1 }}
        >
            {/* Top/Bottom: memory ↔ tag (add tag to memory) */}
            <Handle
                type="target"
                position={Position.Top}
                id="top"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="source"
                position={Position.Top}
                id="top-src"
                className="w-2 h-2 bg-purple-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="target"
                position={Position.Bottom}
                id="bottom"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="source"
                position={Position.Bottom}
                id="bottom-src"
                className="w-2 h-2 bg-purple-400 border border-white opacity-30 hover:opacity-100"
            />

            {/* Header */}
            <div className="px-3 py-2 border-b border-gray-200 bg-white/50 rounded-t-xl">
                <div className="flex items-center gap-2">
                    <FileText className="w-4 h-4 text-gray-500 flex-shrink-0" />
                    <span className="font-medium text-sm text-gray-800 truncate">
                        {data.label}
                    </span>
                </div>

                {/* Relevance badge */}
                {data.relevance > 0 && (
                    <div className="mt-1">
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800">
                            {Math.round(data.relevance * 100)}% match
                        </span>
                    </div>
                )}
            </div>

            {/* Body */}
            <div className="px-3 py-2">
                {/* Preview text */}
                {data.preview && (
                    <p className="text-xs text-gray-600 line-clamp-2 mb-2">
                        {data.preview}
                    </p>
                )}

                {/* Tags */}
                {data.tags && data.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mb-2">
                        {data.tags.slice(0, 3).map((tag: string, idx: number) => (
                            <span
                                key={idx}
                                className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] bg-gray-200 text-gray-700"
                            >
                                <Tag className="w-2.5 h-2.5" />
                                {tag}
                            </span>
                        ))}
                        {data.tags.length > 3 && (
                            <span className="text-[10px] text-gray-500">
                                +{data.tags.length - 3}
                            </span>
                        )}
                    </div>
                )}

                {/* Footer meta */}
                <div className="flex items-center justify-between text-[10px] text-gray-500">
                    <div className="flex items-center gap-1">
                        <Link2 className="w-3 h-3" />
                        <span>{data.chunkCount} chunks</span>
                    </div>
                    {data.createdAt && (
                        <div className="flex items-center gap-1">
                            <Calendar className="w-3 h-3" />
                            <span>{new Date(data.createdAt).toLocaleDateString()}</span>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

// Custom Tag Master Node Component - Rectangular style matching popup design
const TagNodeComponent = ({ data, selected }: NodeProps) => {
    const isFaded = data.isFaded;

    return (
        <div
            className={cn(
                'min-w-[120px] max-w-[180px] rounded-xl border-2 shadow-sm transition-all duration-300',
                'border-gray-300 bg-gray-50',
                selected && 'ring-2 ring-purple-400 shadow-md',
                data.isHighlighted && 'ring-2 ring-purple-500 shadow-lg scale-105 z-10'
            )}
            style={{ opacity: isFaded ? 0.4 : 1 }}
        >
            {/* All 4 sides: memory ↔ tag (add tag to memory) – für übersichtlichere Verbindungen */}
            <Handle
                type="target"
                position={Position.Top}
                id="top"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="source"
                position={Position.Top}
                id="top-src"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="target"
                position={Position.Bottom}
                id="bottom"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="source"
                position={Position.Bottom}
                id="bottom-src"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="target"
                position={Position.Left}
                id="left"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="source"
                position={Position.Left}
                id="left-src"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="target"
                position={Position.Right}
                id="right"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />
            <Handle
                type="source"
                position={Position.Right}
                id="right-src"
                className="w-2 h-2 bg-gray-400 border border-white opacity-30 hover:opacity-100"
            />

            {/* Tag content - matching popup style */}
            <div className="px-3 py-2">
                <div className="flex items-center gap-2">
                    <Tag className="w-4 h-4 text-purple-500 flex-shrink-0" />
                    <span className="font-medium text-sm text-gray-800 truncate">
                        {data.label}
                    </span>
                </div>
                <div className="text-[10px] text-gray-500 mt-1">
                    {data.memoryCount || 0} memories
                </div>
            </div>
        </div>
    );
};

// Node types for ReactFlow
const nodeTypes: NodeTypes = {
    memoryNode: MemoryNodeComponent,
    tagNode: TagNodeComponent,
};

// Props for MemoryGraph component
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
        addTagToMemory,
    } = useMemoryStore();

    // Toast state for connection feedback
    const [connectionToast, setConnectionToast] = useState<{ show: boolean; message: string; type: 'success' | 'error' }>({
        show: false,
        message: '',
        type: 'success'
    });

    // Track saved positions
    const savedPositionsRef = useRef<Record<string, { x: number; y: number }>>(loadSavedPositions());

    // Get connected node IDs for highlighting
    const connectedNodeIds = useMemo(() => {
        if (!selectedNodeId) return new Set<string>();

        const connected = new Set<string>();
        connected.add(selectedNodeId);

        // Find all edges connected to selected node
        storeEdges.forEach(edge => {
            if (edge.source === selectedNodeId) {
                connected.add(edge.target);
            }
            if (edge.target === selectedNodeId) {
                connected.add(edge.source);
            }
        });

        // If a tag is selected, also include all memories connected to that tag
        const selectedNode = storeNodes.find(n => n.id === selectedNodeId);
        const isTagNode = selectedNode?.type === 'tagNode' || selectedNode?.data?.isTagNode;

        if (isTagNode) {
            // Tag connections - edges go from memory (source) to tag (target)
            storeEdges.forEach(edge => {
                if (edge.target === selectedNodeId) {
                    connected.add(edge.source); // Add the memory
                }
                if (edge.source === selectedNodeId) {
                    connected.add(edge.target); // Add the memory
                }
            });
        }

        // If a memory is selected, include its connected tags and other memories via those tags
        if (!isTagNode && selectedNode) {
            // Get connected tags
            const connectedTags = new Set<string>();
            storeEdges.forEach(edge => {
                if (edge.source === selectedNodeId && edge.target.startsWith('tag-')) {
                    connectedTags.add(edge.target);
                    connected.add(edge.target);
                }
                if (edge.target === selectedNodeId && edge.source.startsWith('tag-')) {
                    connectedTags.add(edge.source);
                    connected.add(edge.source);
                }
            });

            // Get all memories connected to those tags
            connectedTags.forEach(tagId => {
                storeEdges.forEach(edge => {
                    if (edge.target === tagId) {
                        connected.add(edge.source);
                    }
                    if (edge.source === tagId) {
                        connected.add(edge.target);
                    }
                });
            });
        }

        return connected;
    }, [selectedNodeId, storeEdges, storeNodes]);

    // Convert store nodes to ReactFlow format with saved positions and collision detection
    const initialNodes: Node[] = useMemo(() => {
        let nodes = storeNodes.map(node => {
            // Use saved position if available
            const savedPos = savedPositionsRef.current[node.id];
            const position = savedPos || node.position;

            // Determine if this node should be faded (unconnected) or highlighted (connected)
            const isFaded = selectedNodeId !== null && !connectedNodeIds.has(node.id);
            const isHighlightedFromSelection = selectedNodeId !== null && connectedNodeIds.has(node.id);
            const isHighlighted = isHighlightedFromSelection || (selectedNodeId === null && !!node.data.isHighlighted);

            return {
                id: node.id,
                type: node.type,
                position,
                data: {
                    ...node.data,
                    isFaded,
                    isHighlighted,
                },
                selected: node.id === selectedNodeId,
            };
        });

        // Apply collision detection only if no saved positions exist
        const hasSavedPositions = Object.keys(savedPositionsRef.current).length > 0;
        if (!hasSavedPositions && nodes.length > 0) {
            nodes = applyCollisionDetection(nodes) as typeof nodes;
        }

        return nodes;
    }, [storeNodes, selectedNodeId, connectedNodeIds]);

    // Convert store edges to ReactFlow format. Nur Memory↔Tag; keine Memory↔Memory.
    const initialEdges: Edge[] = useMemo(() =>
        storeEdges
            .filter(edge => edge.data.connectionType === 'tag')
            .filter(edge => showTagConnections)
            .map(edge => {
                // Edge color matches source memory's type (for tag & semantic: source is memory)
                const sourceNode = storeNodes.find(n => n.id === edge.source);
                const memoryType = sourceNode?.data?.type ?? 'default';
                const strokeColor = TYPE_STROKE_COLORS[memoryType] ?? DEFAULT_STROKE;

                // Fade edges that are not connected to selected node
                const isFaded = selectedNodeId !== null &&
                    !connectedNodeIds.has(edge.source) &&
                    !connectedNodeIds.has(edge.target);

                const isTagEdge = edge.data.connectionType === 'tag';

                return {
                    id: edge.id,
                    source: edge.source,
                    target: edge.target,
                    type: edge.type,
                    animated: edge.animated,
                    style: {
                        strokeWidth: edge.style.strokeWidth,
                        opacity: isFaded ? 0.15 : edge.style.opacity,
                        stroke: strokeColor,
                        strokeDasharray: isTagEdge ? '5,5' : undefined,
                    },
                    markerEnd: !isTagEdge ? {
                        type: MarkerType.ArrowClosed,
                        width: 15,
                        height: 15,
                    } : undefined,
                    label: edge.data.label || undefined,
                    labelStyle: {
                        fontSize: 10,
                        fill: strokeColor,
                        fontWeight: isTagEdge ? 500 : 400,
                    },
                };
            }),
        [storeEdges, storeNodes, showTagConnections, selectedNodeId, connectedNodeIds]
    );

    const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

    // Update local state when store changes or selection changes
    useEffect(() => {
        setNodes(currentNodes =>
            currentNodes.map(node => {
                const isFaded = selectedNodeId !== null && !connectedNodeIds.has(node.id);
                const isHighlightedFromSelection = selectedNodeId !== null && connectedNodeIds.has(node.id);
                const ragHighlight = storeNodes.find(n => n.id === node.id)?.data?.isHighlighted ?? false;
                const isHighlighted = isHighlightedFromSelection || (selectedNodeId === null && ragHighlight);
                return {
                    ...node,
                    data: {
                        ...node.data,
                        isFaded,
                        isHighlighted,
                    },
                    selected: node.id === selectedNodeId,
                };
            })
        );
    }, [selectedNodeId, connectedNodeIds, storeNodes, setNodes]);

    // Update when store nodes change (new data from backend)
    useEffect(() => {
        setNodes(initialNodes);
    }, [storeNodes, setNodes]);

    useEffect(() => {
        setEdges(initialEdges);
    }, [initialEdges, setEdges]);

    // Handle node position changes and save to localStorage
    const handleNodesChange = useCallback((changes: NodeChange[]) => {
        onNodesChange(changes);

        // Save positions when nodes are dragged
        changes.forEach(change => {
            if (change.type === 'position' && change.position && change.dragging === false) {
                savedPositionsRef.current[change.id] = change.position;
                savePositions(savedPositionsRef.current);
            }
        });
    }, [onNodesChange]);

    // Handle node selection
    const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
        setSelectedNodeId(node.id);
        // Only select memory if it's a memory node, not a tag node
        if (node.type === 'memoryNode') {
            selectMemory(node.id);
        } else {
            // For tag nodes, set selected but don't try to fetch memory details
            selectMemory(null);
        }
        onNodeSelect?.(node.id);
    }, [setSelectedNodeId, selectMemory, onNodeSelect]);

    // Handle background click (deselect)
    const onPaneClick = useCallback(() => {
        setSelectedNodeId(null);
        selectMemory(null);
        onNodeSelect?.(null);
    }, [setSelectedNodeId, selectMemory, onNodeSelect]);

    // Nur memory ↔ tag. Must be before early returns (Rules of Hooks).
    const isValidConnection = useCallback(
        (conn: Connection) => {
            if (!conn.source || !conn.target) return false;
            const src = storeNodes.find((n) => n.id === conn.source);
            const tgt = storeNodes.find((n) => n.id === conn.target);
            if (!src || !tgt) return false;
            const srcIsTag = src.type === "tagNode" || src.data?.isTagNode;
            const tgtIsTag = tgt.type === "tagNode" || tgt.data?.isTagNode;
            if (srcIsTag === tgtIsTag) return false;
            const memTagHandles = ["top", "top-src", "bottom", "bottom-src"];
            const tagAllHandles = ["top", "top-src", "bottom", "bottom-src", "left", "left-src", "right", "right-src"];
            const srcHandle = conn.sourceHandle ?? "";
            const tgtHandle = conn.targetHandle ?? "";
            if (srcIsTag) {
                return tagAllHandles.includes(srcHandle) && memTagHandles.includes(tgtHandle);
            }
            return memTagHandles.includes(srcHandle) && tagAllHandles.includes(tgtHandle);
        },
        [storeNodes]
    );

    // Handle connection between memory and tag node (drag to connect)
    const onConnect = useCallback(async (connection: Connection) => {
        const { source, target } = connection;
        if (!source || !target) return;

        // Find source and target nodes to determine types
        const sourceNode = storeNodes.find(n => n.id === source);
        const targetNode = storeNodes.find(n => n.id === target);

        if (!sourceNode || !targetNode) {
            return;
        }

        // Memory-to-tag: add tag to memory
        const isMemoryToTag = sourceNode.type === 'memoryNode' && targetNode.type === 'tagNode';
        const isTagToMemory = sourceNode.type === 'tagNode' && targetNode.type === 'memoryNode';
        if (isMemoryToTag || isTagToMemory) {
            const memoryId = isMemoryToTag ? source : target;
            const tagNode = isMemoryToTag ? targetNode : sourceNode;
            const tag = tagNode.data.tag;

            if (tag) {
                const success = await addTagToMemory(memoryId, tag);
                setConnectionToast({
                    show: true,
                    message: success ? `Added tag #${tag} to memory` : 'Failed to add tag',
                    type: success ? 'success' : 'error'
                });
                setTimeout(() => setConnectionToast(prev => ({ ...prev, show: false })), 2000);
            }
        }
    }, [storeNodes, addTagToMemory]);

    if (isLoading && nodes.length === 0) {
        return (
            <div className={cn('flex items-center justify-center bg-gray-50 rounded-xl', className)}>
                <div className="text-center">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-400 mx-auto mb-2" />
                    <p className="text-sm text-gray-500">Loading memory graph...</p>
                </div>
            </div>
        );
    }

    if (nodes.length === 0) {
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
                                className="inline-flex items-center gap-2 px-4 py-2 bg-gray-900 hover:bg-gray-800 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors"
                            >
                                <RefreshCw className={cn('w-4 h-4', isLoading && 'animate-spin')} />
                                Refresh
                            </button>
                        </>
                    ) : (
                        <>
                            <h3 className="text-lg font-medium text-gray-700 mb-1">No memories yet</h3>
                            <p className="text-sm text-gray-500">
                                Create your first memory to see the graph
                            </p>
                        </>
                    )}
                </div>
            </div>
        );
    }

    return (
        <div className={cn('rounded-xl overflow-hidden border border-gray-200 relative', className)}>
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={handleNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={onNodeClick}
                onPaneClick={onPaneClick}
                onConnect={onConnect}
                isValidConnection={isValidConnection}
                nodeTypes={nodeTypes}
                fitView
                fitViewOptions={{ padding: 0.2 }}
                minZoom={0.1}
                maxZoom={2}
                connectionLineComponent={MemoryConnectionLine}
                connectionLineType={ConnectionLineType.SmoothStep}
                defaultEdgeOptions={{
                    type: 'smoothstep',
                }}
            >
                <Background color="#e5e7eb" gap={20} />
                <Controls className="bg-white rounded-lg shadow-lg" />
                <MiniMap
                    nodeColor={(node) => {
                        // Tag nodes purple; highlighted tag = brighter purple, highlighted memory = yellow
                        if (node.data?.isTagNode && node.data?.isHighlighted) return '#a78bfa';
                        if (node.data?.isTagNode) return '#8b5cf6';
                        if (node.data?.isHighlighted) return '#eab308';
                        if (node.selected) return '#374151';
                        return '#9ca3af';
                    }}
                    maskColor="rgba(0, 0, 0, 0.1)"
                    className="bg-white rounded-lg shadow-lg"
                />
            </ReactFlow>

            {/* Connection Toast Notification */}
            {connectionToast.show && (
                <div className={cn(
                    'absolute bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 rounded-lg shadow-lg',
                    'text-sm font-medium transition-all duration-300 z-50',
                    connectionToast.type === 'success'
                        ? 'bg-green-500 text-white'
                        : 'bg-red-500 text-white'
                )}>
                    {connectionToast.message}
                </div>
            )}

            {/* Connection Hint + Legend */}
            <div className="absolute top-2 left-2 flex flex-col gap-2 z-10">
                <div className="px-3 py-1.5 bg-white/90 rounded-lg shadow text-xs text-gray-500">
                    <span className="font-medium text-purple-600">Tip:</span> Memory ↔ Tag. Tag: alle 4 Seiten für Verbindungen.
                </div>
                <div className="px-3 py-2 bg-white/90 rounded-lg shadow text-xs text-gray-500 max-w-[280px]">
                    <div className="font-medium text-gray-600 mb-1.5">Legend</div>
                    <div className="grid grid-cols-1 gap-1 text-[11px]">
                        <div className="flex items-center gap-2">
                            <span className="w-3 h-3 rounded border border-blue-400 bg-blue-50 flex-shrink-0" />
                            <span>Note – manually saved (memory_save)</span>
                        </div>
                        <div className="flex items-center gap-2">
                            <span className="w-3 h-3 rounded border border-orange-400 bg-orange-50 flex-shrink-0" />
                            <span>Conversation – from chat (15 msg compaction)</span>
                        </div>
                        <div className="flex items-center gap-2">
                            <span className="w-3 h-3 rounded border border-purple-400 bg-purple-50 flex-shrink-0" />
                            <span>Document</span>
                        </div>
                        <div className="flex items-center gap-2">
                            <span className="w-3 h-3 rounded border border-green-400 bg-green-50 flex-shrink-0" />
                            <span>Code</span>
                        </div>
                        <div className="flex items-center gap-2">
                            <span className="w-3 h-3 rounded border border-gray-400 bg-gray-50 flex-shrink-0" />
                            <span>Other</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
