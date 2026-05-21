'use client';

import React, { useState, useCallback, useEffect, useMemo } from 'react';
import { X, Plus, Trash2, AlertCircle, Loader2, GitBranch } from 'lucide-react';

// ─── Types ───────────────────────────────────────────────────────────────────

export interface WorkflowStep {
  /** Unique key for React — not sent to backend */
  _id: string;
  /** Prompt / instruction for this step. Supports {variable} placeholders. */
  input: string;
  /** Tool the agent should use (e.g. "coding_agent", "web_search") */
  tool: string;
  /** Short human-readable description of what the step does */
  description: string;
}

export interface WorkflowSaveData {
  workflow_id: string;
  name: string;
  description: string;
  triggers: string[];
  steps: Array<{
    input: string;
    tool: string;
    description: string;
    output: string;
  }>;
}

export interface WorkflowCreatorProps {
  /** null = create mode; string = edit mode (the workflow ID) */
  workflowId: string | null;
  initialData?: {
    name?: string;
    description?: string;
    triggers?: string[];
    steps?: Array<{ input?: string; tool?: string; description?: string }>;
  };
  /** Full tool list from the agent — used to populate the tool picker */
  availableTools: Array<{ name: string; description?: string }>;
  onSave: (data: WorkflowSaveData) => void;
  onDelete?: (id: string) => void;
  onClose: () => void;
  isSaving?: boolean;
  backendError?: string | null;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Convert "My Cool Workflow" → "my_cool_workflow" */
function nameToId(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, '')
    .trim()
    .replace(/\s+/g, '_');
  return slug || 'my_workflow';
}

let _uid = 0;
function uid(): string { return `s${++_uid}_${Date.now()}`; }

function emptyStep(): WorkflowStep {
  return { _id: uid(), input: '', tool: 'coding_agent', description: '' };
}

/** Tools always shown first in the picker regardless of what the backend provides */
const PINNED_TOOLS = [
  'coding_agent',
  'research_agent',
  'web_search',
  'python_sandbox',
  'read_file',
  'write_file',
  'list_files',
];

// ─── Component ───────────────────────────────────────────────────────────────

export default function WorkflowCreator({
  workflowId,
  initialData,
  availableTools,
  onSave,
  onDelete,
  onClose,
  isSaving = false,
  backendError = null,
}: WorkflowCreatorProps) {
  const isEdit = workflowId !== null;

  // ── form state ──────────────────────────────────────────────────────────────
  const [name, setName]               = useState(initialData?.name ?? '');
  const [wfId, setWfId]               = useState(isEdit ? workflowId : nameToId(initialData?.name ?? ''));
  const [idEdited, setIdEdited]       = useState(false);   // did user manually change ID?
  const [description, setDescription] = useState(initialData?.description ?? '');
  const [triggers, setTriggers]       = useState<string[]>(initialData?.triggers ?? []);
  const [triggerInput, setTriggerInput] = useState('');
  const [steps, setSteps] = useState<WorkflowStep[]>(() => {
    const initial = initialData?.steps;
    if (initial && initial.length > 0) {
      return initial.map(s => ({
        _id:         uid(),
        input:       s.input ?? '',
        tool:        s.tool  ?? 'coding_agent',
        description: s.description ?? '',
      }));
    }
    return [emptyStep()];
  });
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  // Auto-generate ID from name (create mode only, until user edits ID manually)
  useEffect(() => {
    if (!isEdit && !idEdited) setWfId(nameToId(name));
  }, [name, isEdit, idEdited]);

  // ── tool list ────────────────────────────────────────────────────────────────
  const toolNames = useMemo(() => {
    const backendNames = availableTools.map(t => t.name).filter(Boolean);
    const all = new Set([...PINNED_TOOLS, ...backendNames]);
    return Array.from(all).sort((a, b) => {
      // Pinned tools first
      const ai = PINNED_TOOLS.indexOf(a);
      const bi = PINNED_TOOLS.indexOf(b);
      if (ai !== -1 && bi !== -1) return ai - bi;
      if (ai !== -1) return -1;
      if (bi !== -1) return 1;
      return a.localeCompare(b);
    });
  }, [availableTools]);

  // ── triggers ─────────────────────────────────────────────────────────────────
  const addTrigger = useCallback(() => {
    const t = triggerInput.trim();
    if (t && !triggers.includes(t)) setTriggers(prev => [...prev, t]);
    setTriggerInput('');
  }, [triggerInput, triggers]);

  // ── steps ────────────────────────────────────────────────────────────────────
  const addStepAfter = (idx: number) => {
    setSteps(prev => {
      const next = [...prev];
      next.splice(idx + 1, 0, emptyStep());
      return next;
    });
  };

  const removeStep = (id: string) => {
    setSteps(prev => prev.length === 1 ? prev : prev.filter(s => s._id !== id));
  };

  const updateStep = (id: string, field: keyof WorkflowStep, value: string) => {
    setSteps(prev => prev.map(s => s._id === id ? { ...s, [field]: value } : s));
  };

  // ── save ─────────────────────────────────────────────────────────────────────
  const handleSave = () => {
    setLocalError(null);
    const finalId = isEdit ? workflowId! : wfId;

    if (!name.trim()) {
      setLocalError('Workflow name is required.');
      return;
    }
    if (!isEdit && !/^[a-z][a-z0-9_]*$/.test(finalId)) {
      setLocalError('Workflow ID must be lowercase snake_case (e.g. my_workflow).');
      return;
    }
    const emptyPrompt = steps.findIndex(s => !s.input.trim());
    if (emptyPrompt !== -1) {
      setLocalError(`Step ${emptyPrompt + 1} must have a prompt.`);
      return;
    }

    onSave({
      workflow_id: finalId,
      name:        name.trim(),
      description: description.trim(),
      triggers,
      steps: steps.map((s, i) => ({
        input:       s.input,
        tool:        s.tool,
        description: s.description.trim() || `Step ${i + 1}`,
        output:      `step_${i + 1}_output`,
      })),
    });
  };

  // ─── render ─────────────────────────────────────────────────────────────────
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />

      <div
        className="relative bg-white w-full max-w-2xl max-h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div className="h-16 border-b border-gray-100 flex items-center justify-between px-6 shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-purple-100 text-purple-600 flex items-center justify-center">
              <GitBranch size={16} />
            </div>
            <h2 className="text-lg font-semibold text-gray-800">
              {isEdit ? `Edit "${initialData?.name || workflowId}"` : 'Create Workflow'}
            </h2>
          </div>
          <button onClick={onClose} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
            <X size={20} />
          </button>
        </div>

        {/* ── Scrollable body ── */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">

          {/* Name + ID */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Name *</label>
              <input
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="My Workflow"
                className="w-full h-10 px-3 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">
                ID{isEdit && <span className="text-gray-400 font-normal ml-1">(read-only)</span>}
              </label>
              <input
                type="text"
                value={isEdit ? workflowId! : wfId}
                onChange={e => { if (!isEdit) { setIdEdited(true); setWfId(e.target.value); } }}
                readOnly={isEdit}
                placeholder="my_workflow"
                className={`w-full h-10 px-3 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all ${isEdit ? 'bg-gray-50 text-gray-400 cursor-default' : ''}`}
              />
            </div>
          </div>

          {/* Description */}
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Description</label>
            <input
              type="text"
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="What does this workflow do?"
              className="w-full h-10 px-3 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all"
            />
          </div>

          {/* Trigger phrases */}
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Trigger Phrases</label>
            {triggers.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {triggers.map(t => (
                  <span key={t} className="inline-flex items-center gap-1 px-2.5 py-1 bg-purple-50 text-purple-700 text-xs font-medium rounded-full">
                    {t}
                    <button onClick={() => setTriggers(prev => prev.filter(x => x !== t))} className="text-purple-300 hover:text-purple-700 transition-colors">
                      <X size={10} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="flex gap-2">
              <input
                type="text"
                value={triggerInput}
                onChange={e => setTriggerInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTrigger(); } }}
                placeholder="Type phrase and press Enter"
                className="flex-1 h-9 px-3 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all"
              />
              <button onClick={addTrigger} className="px-3 py-1.5 text-xs font-medium bg-purple-50 text-purple-700 rounded-lg hover:bg-purple-100 transition-colors">
                Add
              </button>
            </div>
          </div>

          {/* Steps chain */}
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-3">Steps</label>

            {steps.map((step, idx) => (
              <React.Fragment key={step._id}>
                {/* Step card */}
                <div className="border border-gray-200 rounded-xl bg-white shadow-sm overflow-hidden">
                  {/* Step header bar */}
                  <div className="flex items-center justify-between px-4 py-2 bg-gray-50 border-b border-gray-100">
                    <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-widest">
                      Step {idx + 1}
                    </span>
                    <button
                      onClick={() => removeStep(step._id)}
                      disabled={steps.length === 1}
                      title="Remove step"
                      className="p-1 text-gray-300 hover:text-red-500 disabled:opacity-20 disabled:cursor-not-allowed transition-colors"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>

                  {/* Step body */}
                  <div className="p-4 space-y-3">
                    {/* Prompt textarea */}
                    <div>
                      <label className="block text-[11px] font-medium text-gray-400 mb-1">
                        Prompt&nbsp;<span className="text-gray-300 font-normal">— use {'{variable}'} for placeholders</span>
                      </label>
                      <textarea
                        value={step.input}
                        onChange={e => updateStep(step._id, 'input', e.target.value)}
                        placeholder={`What should the agent do in step ${idx + 1}?`}
                        rows={3}
                        className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm resize-none focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all"
                      />
                    </div>

                    {/* Tool + Description */}
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="block text-[11px] font-medium text-gray-400 mb-1">Tool</label>
                        <select
                          value={step.tool}
                          onChange={e => updateStep(step._id, 'tool', e.target.value)}
                          className="w-full h-8 px-2 border border-gray-200 rounded-lg text-sm bg-white focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all"
                        >
                          {toolNames.map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className="block text-[11px] font-medium text-gray-400 mb-1">Description <span className="font-normal text-gray-300">(optional)</span></label>
                        <input
                          type="text"
                          value={step.description}
                          onChange={e => updateStep(step._id, 'description', e.target.value)}
                          placeholder="Brief step description…"
                          className="w-full h-8 px-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all"
                        />
                      </div>
                    </div>
                  </div>
                </div>

                {/* Add step connector */}
                <div className="flex justify-center py-3">
                  <button
                    onClick={() => addStepAfter(idx)}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-400 hover:text-purple-600 hover:bg-purple-50 rounded-full border border-dashed border-gray-200 hover:border-purple-300 transition-all"
                  >
                    <Plus size={12} />
                    Add Step
                  </button>
                </div>
              </React.Fragment>
            ))}
          </div>

          {/* Error display */}
          {(localError || backendError) && (
            <div className="flex items-start gap-2 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
              <AlertCircle size={16} className="shrink-0 mt-0.5" />
              <span>{localError || backendError}</span>
            </div>
          )}
        </div>

        {/* ── Footer ── */}
        <div className="h-16 border-t border-gray-100 flex items-center justify-between px-6 shrink-0 bg-gray-50/50">
          {/* Left: delete */}
          <div>
            {isEdit && onDelete && (
              showDeleteConfirm ? (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-red-600">Delete this workflow?</span>
                  <button
                    onClick={() => { onDelete(workflowId!); }}
                    className="px-3 py-1.5 text-xs font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors"
                  >
                    Delete
                  </button>
                  <button
                    onClick={() => setShowDeleteConfirm(false)}
                    className="px-3 py-1.5 text-xs font-medium text-gray-500 hover:bg-gray-100 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setShowDeleteConfirm(true)}
                  className="px-3 py-1.5 text-xs font-medium text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                >
                  Delete Workflow
                </button>
              )
            )}
          </div>

          {/* Right: cancel + save */}
          <div className="flex items-center gap-3">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={isSaving}
              className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
            >
              {isSaving && <Loader2 size={14} className="animate-spin" />}
              {isEdit ? 'Update Workflow' : 'Create Workflow'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
