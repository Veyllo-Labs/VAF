import React from 'react';
import { useWorkflowStore } from './stores/workflowStore';
import { Activity, CheckCircle2, XCircle, Clock } from 'lucide-react';
import { cn } from '@/lib/utils';

interface WorkflowChatElementProps {
  workflowId: string;
  name: string;
  initialSteps?: number;
  /** When set (e.g. from WORKFLOW_ASYNC text), show this state instead of store; avoids card jumping to 100% before updates */
  forceStatus?: 'running' | 'completed' | 'failed';
}

export const WorkflowChatElement: React.FC<WorkflowChatElementProps> = ({ workflowId, name, initialSteps, forceStatus }) => {
  const { workflow, openWorkflow } = useWorkflowStore();
  
  // Check if this component corresponds to the active/latest workflow in store
  const isMatch = workflow?.id === workflowId;
  const data = isMatch ? workflow : null;
  
  const status = forceStatus ?? (data?.status || 'completed');
  const steps = data?.steps || [];
  const currentStepIndex = steps.findIndex(s => s.status === 'running');
  const activeStep = steps[currentStepIndex];
  
  // Progress calculation: when forceStatus is running, show 0% until store has updates
  const total = steps.length || initialSteps || 1;
  const completed = steps.filter(s => s.status === 'success' || s.status === 'skipped').length;
  const progress = forceStatus === 'running'
    ? 0
    : (isMatch ? Math.round((completed / total) * 100) : 100);

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
        status === 'running' ? "bg-indigo-50 text-indigo-600" :
        status === 'completed' ? "bg-green-50 text-green-600" :
        status === 'failed' ? "bg-red-50 text-red-600" :
        "bg-gray-100 text-gray-400"
      )}>
        {status === 'running' ? <Activity size={20} className="animate-pulse" /> :
         status === 'completed' ? <CheckCircle2 size={20} /> :
         status === 'failed' ? <XCircle size={20} /> :
         <Clock size={20} />
        }
      </div>
      
      <div className="flex-1 min-w-0">
        <div className="flex justify-between items-center mb-1">
          <h4 className="font-semibold text-sm text-gray-900 truncate">{name}</h4>
          <span className="text-xs text-gray-500 font-mono">{progress}%</span>
        </div>
        
        {/* Progress Bar */}
        <div className="h-1.5 w-full bg-gray-100 rounded-full overflow-hidden mb-1">
          <div 
            className={cn("h-full rounded-full transition-all duration-500", 
              status === 'completed' ? "bg-green-500" : 
              status === 'failed' ? "bg-red-500" : 
              "bg-indigo-500"
            )}
            style={{ width: `${progress}%` }}
          />
        </div>
        
        <p className="text-[10px] text-gray-500 truncate">
          {status === 'running' && activeStep ? `Step ${currentStepIndex + 1}: ${activeStep.name}` : 
           status === 'completed' ? "Workflow completed" : 
           status === 'failed' ? "Workflow failed" : "Workflow finished"}
        </p>
      </div>
    </div>
  );
};
