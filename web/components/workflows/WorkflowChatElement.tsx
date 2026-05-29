import React, { useEffect, useState, useRef } from 'react';
import { useWorkflowStore } from './stores/workflowStore';
import type { VAFWorkflow } from './stores/workflowStore';
import { Activity, CheckCircle2, XCircle, Clock, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';

const ORPHAN_TIMEOUT = 60; // seconds before declaring "no output received"

interface WorkflowChatElementProps {
  workflowId: string;
  name: string;
  initialSteps?: number;
  forceStatus?: 'running' | 'completed' | 'failed';
}

export const WorkflowChatElement: React.FC<WorkflowChatElementProps> = ({ workflowId, name, initialSteps, forceStatus }) => {
  const { workflow, isOpen, openWorkflow } = useWorkflowStore();

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

  // ── Orphan timeout ─────────────────────────────────────────────────────────
  // If the right panel closed but this element is still 'running', count down.
  // After ORPHAN_TIMEOUT seconds with no update → force 'failed'.
  const [countdown, setCountdown] = useState<number | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isOrphaned = isMatch && status === 'running' && !isOpen;

  useEffect(() => {
    if (isOrphaned) {
      setCountdown(ORPHAN_TIMEOUT);
      intervalRef.current = setInterval(() => {
        setCountdown(prev => {
          if (prev === null) return null;
          if (prev <= 1) {
            clearInterval(intervalRef.current!);
            intervalRef.current = null;
            // Force-fail the workflow in the store
            useWorkflowStore.setState(s => ({
              workflow: s.workflow
                ? { ...s.workflow, status: 'failed' as VAFWorkflow['status'] }
                : null
            }));
            return null;
          }
          return prev - 1;
        });
      }, 1000);
    } else {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      setCountdown(null);
    }
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [isOrphaned]);

  // ── Status label ────────────────────────────────────────────────────────────
  let statusLabel: React.ReactNode;
  if (countdown !== null) {
    statusLabel = (
      <span className="text-amber-500">
        Waiting for output — timeout in {countdown}s
      </span>
    );
  } else if (status === 'running' && activeStep) {
    statusLabel = `Step ${currentStepIndex + 1}: ${activeStep.name}`;
  } else if (status === 'completed') {
    statusLabel = 'Workflow completed';
  } else if (status === 'failed') {
    statusLabel = countdown === null && isOrphaned ? 'Failed to generate output' : 'Workflow failed';
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
        countdown !== null        ? "bg-amber-50 text-amber-500" :
        status === 'running'      ? "bg-indigo-50 text-indigo-600" :
        status === 'completed'    ? "bg-green-50 text-green-600" :
        status === 'failed'       ? "bg-red-50 text-red-600" :
        "bg-gray-100 text-gray-400"
      )}>
        {countdown !== null       ? <AlertTriangle size={20} className="animate-pulse" /> :
         status === 'running'     ? <Activity size={20} className="animate-pulse" /> :
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
              countdown !== null     ? "bg-amber-400" :
              status === 'completed' ? "bg-green-500" :
              status === 'failed'    ? "bg-red-500" :
              "bg-indigo-500"
            )}
            style={{ width: countdown !== null ? `${Math.round((1 - countdown / ORPHAN_TIMEOUT) * 100)}%` : `${progress}%` }}
          />
        </div>

        <p className="text-[10px] text-gray-500 truncate">
          {statusLabel}
        </p>
      </div>
    </div>
  );
};
