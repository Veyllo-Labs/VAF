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
  // 'ended' is a NEUTRAL terminal state: the run is over but this panel never learned
  // how it turned out (its events were lost while the socket was down). It must never be
  // guessed into 'completed' or 'failed' - the real outcome is in the chat message.
  status: 'idle' | 'running' | 'paused' | 'completed' | 'failed' | 'ended';
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
  reconcileWorkflow: (verdict: 'running' | 'paused' | 'ended') => void;
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

    // Update workflow status.
    // A workflow that has already reached a TERMINAL state must not be resurrected by a late
    // frame: after a reconnect the backend's verdict is the truth, and a stale event still in
    // flight would otherwise flip a finished panel back to "running" forever.
    // 'paused' is deliberately NOT terminal - the CLI resume path advances it.
    let wfStatus = workflow.status;
    const isTerminal = wfStatus === 'completed' || wfStatus === 'failed' || wfStatus === 'ended';
    if (!isTerminal) {
      if (status === 'running') wfStatus = 'running';
      if (status === 'failed') wfStatus = 'failed';
      // Check completion if this step succeeded. Guarded by isTerminal above, so it can never
      // rewrite the neutral 'ended' into a manufactured success.
      const allDone = newSteps.every(s => s.status === 'success' || s.status === 'skipped');
      if (allDone) wfStatus = 'completed';
    }

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

  // The backend's answer to "is this run actually still going?". Used after a reconnect or
  // when the tab becomes visible again, because workflow events are fire and forget: if the
  // socket dies mid-run, every later event is delivered to nobody and the panel sits on the
  // last state it happened to receive (live incident 2026-07-20: "step 1 running, 50%").
  reconcileWorkflow: (verdict) => set(state => {
    const wf = state.workflow;
    if (!wf) return {};
    if (wf.status === 'completed' || wf.status === 'failed' || wf.status === 'ended') return {};
    if (verdict === 'running') return {};                       // nothing to correct
    if (verdict === 'paused') return { workflow: { ...wf, status: 'paused' } };
    // 'ended': the run is over. Do NOT invent an outcome - leave the steps as they are and
    // mark the panel neutrally, so it can be closed and the chat message carries the result.
    return { workflow: { ...wf, status: 'ended' } };
  }),

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
