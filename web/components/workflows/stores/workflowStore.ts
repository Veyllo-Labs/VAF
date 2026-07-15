// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
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
  consoleLines: string[];

  // Actions
  setIsOpen: (isOpen: boolean) => void;
  loadWorkflow: (workflow: VAFWorkflow) => void;
  updateStepStatus: (stepId: string, status: VAFStep['status'], progress?: number, result?: string) => void;
  appendWorkflowLine: (line: string) => void;
  openWorkflow: () => void;
  closeWorkflow: () => void;
  clearWorkflow: () => void;
}

export const useWorkflowStore = create<WorkflowState>()(
  persist(
    (set, get) => ({
  isOpen: false,
  workflow: null,
  nodes: [],
  edges: [],
  consoleLines: [],

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
      edges,
      consoleLines: []
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

  appendWorkflowLine: (line) => {
    if (!line && line !== '') return;
    // Hard cap PER ENTRY: a single sub-agent output block arrives as one
    // "line" and can be tens of KB - 400 of those ballooned the DOM until
    // the page lagged (live report). 500 chars each, tail dropped; the
    // full output still lives in the logs/session, the terminal is a
    // ticker, not an archive.
    const entry = line.length > 500
      ? line.slice(0, 500) + ` [... +${(line.length - 500).toLocaleString()} chars]`
      : line;
    set(state => {
      const next = [...state.consoleLines, entry];
      const capped = next.length > 400 ? next.slice(-400) : next;
      return { consoleLines: capped };
    });
  },

  openWorkflow: () => set({ isOpen: true }),
  closeWorkflow: () => set({ isOpen: false }), // Keep data
  clearWorkflow: () => set({ isOpen: false, workflow: null, nodes: [], edges: [], consoleLines: [] }),
    }),
    {
      name: 'vaf-workflow-state',
      storage: createJSONStorage(() => sessionStorage),
      // Persist everything except actions (functions can't be serialised)
      partialize: (state) => ({
        isOpen: state.isOpen,
        workflow: state.workflow,
        nodes: state.nodes,
        edges: state.edges,
        consoleLines: state.consoleLines,
      }),
    }
  )
);
