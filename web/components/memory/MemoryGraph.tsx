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
import { FileText, Tag, Calendar, Link2 } from 'lucide-react';
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
                selected && 'ring-2 ring-indigo-500 shadow-md',
                data.isHighlighted && 'ring-2 ring-yellow-400 shadow-lg scale-105 z-10'
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

// Node types for ReactFlow
const nodeTypes: NodeTypes = {
    memoryNode: MemoryNodeComponent,
};

// Props for MemoryGraph component
interface MemoryGraphProps {
    className?: string;
    onNodeSelect?: (nodeId: string | null) => void;
}

export default function MemoryGraph({ className, onNodeSelect }: MemoryGraphProps) {
    const { 
        nodes: storeNodes, 
        edges: storeEdges, 
        selectedNodeId,
        setSelectedNodeId,
        selectMemory,
        isLoading 
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
    
    // Convert store edges to ReactFlow format
    const initialEdges: Edge[] = useMemo(() =>
        storeEdges.map(edge => ({
            id: edge.id,
            source: edge.source,
            target: edge.target,
            type: edge.type,
            animated: edge.animated,
            style: {
                strokeWidth: edge.style.strokeWidth,
                opacity: edge.style.opacity,
                stroke: edge.data.connectionType === 'semantic' ? '#6366f1' : '#9ca3af',
            },
            markerEnd: {
                type: MarkerType.ArrowClosed,
                width: 15,
                height: 15,
            },
            label: edge.data.label || undefined,
            labelStyle: { fontSize: 10, fill: '#666' },
        })),
        [storeEdges]
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
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-600 mx-auto mb-2" />
                    <p className="text-sm text-gray-500">Loading memory graph...</p>
                </div>
            </div>
        );
    }
    
    if (nodes.length === 0) {
        return (
            <div className={cn('flex items-center justify-center bg-gray-50 rounded-xl', className)}>
                <div className="text-center p-8">
                    <FileText className="w-12 h-12 text-gray-300 mx-auto mb-3" />
                    <h3 className="text-lg font-medium text-gray-700 mb-1">No memories yet</h3>
                    <p className="text-sm text-gray-500">
                        Create your first memory to see the graph
                    </p>
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
                        if (node.data?.isHighlighted) return '#fbbf24';
                        if (node.selected) return '#6366f1';
                        return '#9ca3af';
                    }}
                    maskColor="rgba(0, 0, 0, 0.1)"
                    className="bg-white rounded-lg shadow-lg"
                />
            </ReactFlow>
        </div>
    );
}
