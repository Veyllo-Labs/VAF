'use client';

/**
 * Memory Graph visualization using ReactFlow.
 * 
 * Features:
 * - Custom memory nodes with preview
 * - Edge thickness based on connection strength
 * - Node highlighting for RAG sources
 * - Interactive selection and navigation
 */

import React, { useCallback, useMemo } from 'react';
import ReactFlow, {
    Node,
    Edge,
    Background,
    Controls,
    MiniMap,
    useNodesState,
    useEdgesState,
    NodeTypes,
    EdgeTypes,
    NodeProps,
    Handle,
    Position,
    MarkerType,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { useMemoryStore, MemoryNode, MemoryEdge } from './stores/memoryStore';
import { FileText, Tag, Calendar, Link2, RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';

// Custom Memory Node Component
const MemoryNodeComponent = ({ data, selected }: NodeProps) => {
    const { selectMemory } = useMemoryStore();
    
    const handleClick = useCallback(() => {
        // ID is passed via the node, not data
    }, []);
    
    const typeColors: Record<string, string> = {
        note: 'border-blue-400 bg-blue-50',
        document: 'border-purple-400 bg-purple-50',
        code: 'border-green-400 bg-green-50',
        conversation: 'border-orange-400 bg-orange-50',
        default: 'border-gray-400 bg-gray-50'
    };
    
    const typeColor = typeColors[data.type] || typeColors.default;
    
    return (
        <div
            className={cn(
                'min-w-[200px] max-w-[280px] rounded-xl border-2 shadow-sm transition-all duration-200',
                typeColor,
                selected && 'ring-2 ring-gray-400 shadow-md',
                data.isHighlighted && 'ring-2 ring-yellow-500 shadow-lg scale-105 z-10'
            )}
        >
            {/* Connection handles */}
            <Handle
                type="target"
                position={Position.Top}
                className="w-3 h-3 bg-gray-400 border-2 border-white"
            />
            <Handle
                type="source"
                position={Position.Bottom}
                className="w-3 h-3 bg-gray-400 border-2 border-white"
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

// Custom Tag Master Node Component - Size scales with memory count
const TagNodeComponent = ({ data, selected }: NodeProps) => {
    // Dynamic size based on sizeScale (1.0 to 2.5)
    const sizeScale = data.sizeScale || 1.0;
    const baseSize = 80; // Base diameter in pixels
    const nodeSize = baseSize * sizeScale;
    const fontSize = Math.max(12, 14 * sizeScale);
    const iconSize = Math.max(16, 20 * sizeScale);

    return (
        <div
            className={cn(
                'rounded-full border-2 shadow-lg transition-all duration-300 flex items-center justify-center',
                'bg-gradient-to-br from-purple-500 via-purple-600 to-indigo-600 border-purple-300',
                'hover:shadow-2xl hover:border-purple-200',
                selected && 'ring-4 ring-purple-300/50 shadow-2xl border-purple-200'
            )}
            style={{
                width: nodeSize,
                height: nodeSize,
                minWidth: nodeSize,
                minHeight: nodeSize,
            }}
        >
            {/* Connection handles positioned around the circle */}
            <Handle
                type="target"
                position={Position.Top}
                className="w-2 h-2 bg-purple-300 border border-white opacity-0 hover:opacity-100"
            />
            <Handle
                type="target"
                position={Position.Right}
                className="w-2 h-2 bg-purple-300 border border-white opacity-0 hover:opacity-100"
            />
            <Handle
                type="target"
                position={Position.Bottom}
                className="w-2 h-2 bg-purple-300 border border-white opacity-0 hover:opacity-100"
            />
            <Handle
                type="target"
                position={Position.Left}
                className="w-2 h-2 bg-purple-300 border border-white opacity-0 hover:opacity-100"
            />

            {/* Tag content - centered */}
            <div className="flex flex-col items-center justify-center text-center p-2">
                <Tag style={{ width: iconSize, height: iconSize }} className="text-white/90 mb-1" />
                <span
                    className="font-bold text-white leading-tight"
                    style={{ fontSize: fontSize }}
                >
                    {data.label}
                </span>
                {data.memoryCount > 0 && (
                    <span
                        className="text-purple-200/80 mt-0.5"
                        style={{ fontSize: Math.max(9, fontSize * 0.7) }}
                    >
                        {data.memoryCount}
                    </span>
                )}
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
    } = useMemoryStore();
    
    // Convert store nodes to ReactFlow format
    const initialNodes: Node[] = useMemo(() => 
        storeNodes.map(node => ({
            id: node.id,
            type: node.type,
            position: node.position,
            data: node.data,
            selected: node.id === selectedNodeId,
        })),
        [storeNodes, selectedNodeId]
    );
    
    // Convert store edges to ReactFlow format (filter tag edges based on showTagConnections)
    const initialEdges: Edge[] = useMemo(() =>
        storeEdges
            .filter(edge => showTagConnections || edge.data.connectionType !== 'tag')
            .map(edge => {
                // Determine edge color based on connection type
                let strokeColor = '#9ca3af'; // default gray
                if (edge.data.connectionType === 'semantic') {
                    strokeColor = '#6b7280'; // darker gray for semantic
                } else if (edge.data.connectionType === 'tag') {
                    strokeColor = '#8b5cf6'; // purple for tag connections
                }

                return {
                    id: edge.id,
                    source: edge.source,
                    target: edge.target,
                    type: edge.type,
                    animated: edge.animated,
                    style: {
                        strokeWidth: edge.style.strokeWidth,
                        opacity: edge.style.opacity,
                        stroke: (edge.style as any).stroke || strokeColor,
                        strokeDasharray: edge.data.connectionType === 'tag' ? '5,5' : undefined,
                    },
                    markerEnd: edge.data.connectionType !== 'tag' ? {
                        type: MarkerType.ArrowClosed,
                        width: 15,
                        height: 15,
                    } : undefined,
                    label: edge.data.label || undefined,
                    labelStyle: {
                        fontSize: 10,
                        fill: edge.data.connectionType === 'tag' ? '#8b5cf6' : '#666',
                        fontWeight: edge.data.connectionType === 'tag' ? 500 : 400,
                    },
                };
            }),
        [storeEdges, showTagConnections]
    );
    
    const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
    
    // Update local state when store changes
    React.useEffect(() => {
        setNodes(initialNodes);
    }, [initialNodes, setNodes]);
    
    React.useEffect(() => {
        setEdges(initialEdges);
    }, [initialEdges, setEdges]);
    
    // Handle node selection
    const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
        setSelectedNodeId(node.id);
        selectMemory(node.id);
        onNodeSelect?.(node.id);
    }, [setSelectedNodeId, selectMemory, onNodeSelect]);
    
    // Handle background click (deselect)
    const onPaneClick = useCallback(() => {
        setSelectedNodeId(null);
        selectMemory(null);
        onNodeSelect?.(null);
    }, [setSelectedNodeId, selectMemory, onNodeSelect]);
    
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
        <div className={cn('rounded-xl overflow-hidden border border-gray-200', className)}>
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={onNodeClick}
                onPaneClick={onPaneClick}
                nodeTypes={nodeTypes}
                fitView
                fitViewOptions={{ padding: 0.2 }}
                minZoom={0.1}
                maxZoom={2}
                defaultEdgeOptions={{
                    type: 'smoothstep',
                }}
            >
                <Background color="#e5e7eb" gap={20} />
                <Controls className="bg-white rounded-lg shadow-lg" />
                <MiniMap
                    nodeColor={(node) => {
                        // Tag nodes are purple
                        if (node.data?.isTagNode) return '#8b5cf6';
                        if (node.data?.isHighlighted) return '#eab308';
                        if (node.selected) return '#374151';
                        return '#9ca3af';
                    }}
                    maskColor="rgba(0, 0, 0, 0.1)"
                    className="bg-white rounded-lg shadow-lg"
                />
            </ReactFlow>
        </div>
    );
}
