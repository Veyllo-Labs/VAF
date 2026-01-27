import React, { useEffect, useCallback } from 'react';
import ReactFlow, { 
  Background, 
  Controls, 
  useNodesState, 
  useEdgesState,
  ConnectionLineType,
  MarkerType
} from 'reactflow';
import 'reactflow/dist/style.css';
import { useWorkflowStore } from './stores/workflowStore';
import VAFWorkflowNode from './VAFWorkflowNode';
import { X, Play, Pause, Square, Maximize2, Minimize2, Activity } from 'lucide-react';
import { cn } from '@/lib/utils';

const nodeTypes = {
  vafNode: VAFWorkflowNode,
};

const VAFWorkflowRuntime = () => {
  const { isOpen, nodes: initialNodes, edges: initialEdges, closeWorkflow, workflow } = useWorkflowStore();
  
  // Local state for React Flow (synced with store)
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  // Sync store -> local state
  useEffect(() => {
    setNodes(initialNodes);
    setEdges(initialEdges);
  }, [initialNodes, initialEdges, setNodes, setEdges]);

  // Auto-close on completion
  useEffect(() => {
    if (workflow?.status === 'completed' || workflow?.status === 'failed') {
      const timer = setTimeout(() => {
        closeWorkflow();
      }, 2500); // Auto-close after 2.5s
      return () => clearTimeout(timer);
    }
  }, [workflow?.status, closeWorkflow]);

  if (!isOpen) return null;

  return (
    <div className={cn(
      "fixed top-0 right-0 h-screen bg-white shadow-2xl transition-transform duration-500 z-40 flex flex-col border-l border-gray-200",
      isOpen ? "translate-x-0" : "translate-x-full",
      "w-full sm:w-[450px] md:w-[500px]" // Responsive width
    )}>
      {/* Header - Matches Main Header Height (h-16) */}
      <div className="h-16 border-b border-gray-200 flex items-center justify-between px-6 bg-white shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-indigo-50 flex items-center justify-center text-indigo-600 shrink-0">
            <Activity size={16} />
          </div>
          <div className="min-w-0">
            <h2 className="font-bold text-gray-900 text-sm truncate">Workflow Runtime</h2>
            <p className="text-xs text-gray-500 truncate max-w-[200px]">{workflow?.name || 'Untitled Workflow'}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* Controls - Minimal */}
          {workflow?.status === 'running' && (
             <span className="flex h-2 w-2 relative mr-2">
               <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-75"></span>
               <span className="relative inline-flex rounded-full h-2 w-2 bg-indigo-500"></span>
             </span>
          )}
        </div>
      </div>

      {/* Workflow Canvas */}
      <div className="flex-1 bg-gray-50 relative">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onInit={onInit}
          fitView
          attributionPosition="bottom-right"
          nodesDraggable={false} // Lock layout for vertical list feel
          nodesConnectable={false}
          zoomOnScroll={true}
          panOnDrag={true}
          minZoom={0.5}
          maxZoom={1.5}
          defaultEdgeOptions={{
            type: 'smoothstep',
            animated: true,
            style: { stroke: '#cbd5e1', strokeWidth: 2 },
            markerEnd: { type: MarkerType.ArrowClosed, color: '#cbd5e1' },
          }}
        >
          <Background color="#e2e8f0" gap={20} size={1} />
          <Controls 
            showInteractive={false} 
            className="!bg-white !shadow-sm !border !border-gray-200 !m-4 !rounded-lg overflow-hidden" 
          />
        </ReactFlow>
      </div>

      {/* Footer / Status */}
      <div className="p-4 border-t border-gray-200 bg-white text-xs text-gray-500 flex justify-between items-center shrink-0">
        <div className="flex items-center gap-2">
          <div className={cn("w-2 h-2 rounded-full",
            workflow?.status === 'running' ? "bg-indigo-500 animate-pulse" :
            workflow?.status === 'completed' ? "bg-green-500" :
            workflow?.status === 'failed' ? "bg-red-500" : "bg-gray-300"
          )} />
          <span className="font-medium">
            {workflow?.status?.toUpperCase() || 'IDLE'}
          </span>
        </div>
        <span className="font-mono text-gray-400">{nodes.length} STEPS</span>
      </div>
    </div>
  );
};

export default VAFWorkflowRuntime;
