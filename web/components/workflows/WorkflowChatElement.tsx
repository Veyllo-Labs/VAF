// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import React, { useEffect, useState } from 'react';
import { useWorkflowStore } from './stores/workflowStore';
import { Activity, CheckCircle2, XCircle, Clock, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';

// How long without ANY signal about the run before this card admits it has not heard
// anything for a while. It only ever changes the LABEL; it never changes the run's state.
const STALE_AFTER_MS = 90_000;

interface WorkflowChatElementProps {
  workflowId: string;
  name: string;
  initialSteps?: number;
  forceStatus?: 'running' | 'completed' | 'failed';
}

export const WorkflowChatElement: React.FC<WorkflowChatElementProps> = ({ workflowId, name, initialSteps, forceStatus }) => {
  // NOTE: deliberately does NOT read isOpen. Whether the right panel is open says nothing
  // about the run, and treating it as a signal is what produced the false failure above.
  const { workflow, openWorkflow } = useWorkflowStore();
  const lastEventAt = useWorkflowStore(s => s.lastEventAt);

  const isMatch = workflow?.id === workflowId;
  const data = isMatch ? workflow : null;

  const steps = data?.steps || [];
  const currentStepIndex = steps.findIndex(s => s.status === 'running');
  const activeStep = steps[currentStepIndex];

  const status = (isMatch && data?.status) ? data.status : (forceStatus ?? 'completed');

  const total = steps.length || initialSteps || 1;
  const completed = steps.filter(s => s.status === 'success' || s.status === 'skipped').length;
  const progress = isMatch && steps.length > 0
    ? Math.round((completed / total) * 100)
    : (forceStatus === 'running' ? 0 : 100);

  // ── Staleness hint, never a verdict ────────────────────────────────────────
  // This used to be an "orphan timeout": while the right panel was CLOSED and this element
  // said 'running', it counted down 60 seconds and then wrote status='failed' into the store.
  // A closed panel says nothing whatsoever about the run - the user had simply put it away -
  // so a perfectly healthy workflow was declared dead while it was still streaming output.
  // That is the same mistake as the backend reporting a paused run as crashed: absence of a
  // signal treated as evidence of failure. Reported live on 2026-07-20, right after the panel
  // gained a close button and the case became easy to hit.
  //
  // It now measures the only thing that carries information - how long since we last heard
  // ANYTHING about this run - and it only says so. Whether the run is genuinely still alive
  // is answered by the backend (get_workflow_run_state), never guessed here.
  const isRunning = isMatch && status === 'running';
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!isRunning) return;
    const t = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(t);
  }, [isRunning]);

  const silentForMs = (isRunning && lastEventAt) ? Math.max(0, now - lastEventAt) : 0;
  const isStale = silentForMs > STALE_AFTER_MS;

  // ── Status label ────────────────────────────────────────────────────────────
  let statusLabel: React.ReactNode;
  if (isStale) {
    statusLabel = (
      <span className="text-amber-500">
        No update for {Math.round(silentForMs / 1000)}s, still running
      </span>
    );
  } else if (status === 'running' && activeStep) {
    statusLabel = `Step ${currentStepIndex + 1}: ${activeStep.name}`;
  } else if (status === 'running') {
    statusLabel = 'Running';
  } else if (status === 'paused') {
    statusLabel = 'Waiting for a background helper';
  } else if (status === 'completed') {
    statusLabel = 'Workflow completed';
  } else if (status === 'failed') {
    statusLabel = 'Workflow failed';
  } else if (status === 'ended') {
    statusLabel = 'Finished, see the message below';
  } else {
    statusLabel = 'Workflow finished';
  }

  return (
    <div
      onClick={(e) => {
        if (isMatch) {
          e.stopPropagation();
          openWorkflow();
        }
      }}
      className={cn(
        "flex items-center gap-3 p-3 rounded-xl border transition-all group bg-white shadow-sm max-w-sm my-2 select-none",
        isMatch ? "hover:border-indigo-300 hover:shadow-md border-gray-200 cursor-pointer" : "border-gray-100 opacity-80 cursor-default"
      )}
    >
      <div className={cn(
        "w-10 h-10 rounded-lg flex items-center justify-center shrink-0 transition-colors",
        isStale                   ? "bg-amber-50 text-amber-500" :
        status === 'running'      ? "bg-indigo-50 text-indigo-600" :
        status === 'paused'       ? "bg-amber-50 text-amber-500" :
        status === 'completed'    ? "bg-green-50 text-green-600" :
        status === 'failed'       ? "bg-red-50 text-red-600" :
        "bg-gray-100 text-gray-400"
      )}>
        {isStale                  ? <AlertTriangle size={20} className="animate-pulse" /> :
         status === 'running'     ? <Activity size={20} className="animate-pulse" /> :
         status === 'paused'      ? <Clock size={20} className="animate-pulse" /> :
         status === 'completed'   ? <CheckCircle2 size={20} /> :
         status === 'failed'      ? <XCircle size={20} /> :
         <Clock size={20} />}
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex justify-between items-center mb-1">
          <h4 className="font-semibold text-sm text-gray-900 truncate">{name}</h4>
          <span className="text-xs text-gray-500 font-mono">{progress}%</span>
        </div>

        <div className="h-1.5 w-full bg-gray-100 rounded-full overflow-hidden mb-1">
          <div
            className={cn("h-full rounded-full transition-all duration-500",
              status === 'completed' ? "bg-green-500" :
              status === 'failed'    ? "bg-red-500" :
              isStale                ? "bg-amber-400" :
              "bg-indigo-500"
            )}
            style={{ width: `${progress}%` }}
          />
        </div>

        <p className="text-[10px] text-gray-500 truncate">
          {statusLabel}
        </p>
      </div>
    </div>
  );
};
