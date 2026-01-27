import { create } from 'zustand';
import { Edge, Node } from 'reactflow';

export interface VAFStep {
  id: string;
  name: string;
  type: 'agent' | 'tool' | 'wait' | 'condition';
  status: 'idle' | 'running' | 'success' | 'failed' | 'skipped';
  progress?: number;
  result?: string;
  error?: string;
  inputs?: any;
  outputs?: any;
}

export interface VAFWorkflow {
  id: string;
  name: string;
  steps: VAFStep[];
  currentStepId: string | null;
  status: 'idle' | 'running' | 'paused' | 'completed' | 'failed';
}

interface WorkflowState {
  isOpen: boolean;
  workflow: VAFWorkflow | null;
  nodes: Node[];
  edges: Edge[];

  // Actions
  setIsOpen: (isOpen: boolean) => void;
  loadWorkflow: (workflow: VAFWorkflow) => void;
  updateStepStatus: (stepId: string, status: VAFStep['status'], progress?: number, result?: string) => void;
  openWorkflow: () => void;
  closeWorkflow: () => void;
  clearWorkflow: () => void;
}

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  isOpen: false,
  workflow: null,
  nodes: [],
  edges: [],

  setIsOpen: (isOpen) => set({ isOpen }),

  loadWorkflow: (workflow) => {
    // Transform steps to initial nodes/edges
    const nodes: Node[] = workflow.steps.map((step, index) => ({
      id: step.id,
      type: 'vafNode',
      position: { x: 0, y: index * 150 }, // Vertical layout
      data: { ...step, index },
    }));

    const edges: Edge[] = workflow.steps.slice(0, -1).map((step, index) => ({
      id: `e-${step.id}-${workflow.steps[index + 1].id}`,
      source: step.id,
      target: workflow.steps[index + 1].id,
      animated: true,
      style: { stroke: '#e2e8f0' },
    }));

    set({
      isOpen: true,
      workflow,
      nodes,
      edges
    });
  },

  updateStepStatus: (stepId, status, progress, result) => {
    const { workflow, nodes } = get();
    if (!workflow) return;

    // Update workflow steps
    const newSteps = workflow.steps.map(step =>
      step.id === stepId
        ? { ...step, status, progress, result }
        : step
    );

    // Update nodes data
    const newNodes = nodes.map(node =>
      node.id === stepId
        ? { ...node, data: { ...node.data, status, progress, result } }
        : node
    );

    // Update workflow status
    let wfStatus = workflow.status;
    if (status === 'running') wfStatus = 'running';
    if (status === 'failed') wfStatus = 'failed';
    // Check completion if this step succeeded
    const allDone = newSteps.every(s => s.status === 'success' || s.status === 'skipped');
    if (allDone) wfStatus = 'completed';

    set({
      workflow: { ...workflow, steps: newSteps, status: wfStatus },
      nodes: newNodes
    });
  },

  openWorkflow: () => set({ isOpen: true }),
  closeWorkflow: () => set({ isOpen: false }), // Keep data
  clearWorkflow: () => set({ isOpen: false, workflow: null, nodes: [], edges: [] }),
}));
