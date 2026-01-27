import React, { memo } from 'react';
import { Handle, Position } from 'reactflow';
import { Check, Loader2, AlertCircle, Clock, Play } from 'lucide-react';
import { cn } from '@/lib/utils';
import { VAFStep } from './stores/workflowStore';

const VAFWorkflowNode = ({ data }: { data: VAFStep & { index: number } }) => {
  const isRunning = data.status === 'running';
  const isSuccess = data.status === 'success';
  const isFailed = data.status === 'failed';
  const isIdle = data.status === 'idle';

  return (
    <div className={cn(
      "w-72 bg-white rounded-xl border transition-all duration-300 shadow-sm relative overflow-hidden group",
      isRunning ? "border-indigo-500 shadow-md ring-1 ring-indigo-500/20" : 
      isSuccess ? "border-gray-200" : // Clean look for success
      isFailed ? "border-red-300 bg-red-50/10" :
      "border-gray-200 opacity-80"
    )}>
      {/* Handles for flow connection */}
      <Handle type="target" position={Position.Top} className="!bg-gray-300 !w-2 !h-2 !-top-1 opacity-0 group-hover:opacity-100 transition-opacity" />
      
      {/* Progress Bar Background for Running state */}
      {isRunning && (
        <div className="absolute top-0 left-0 h-0.5 bg-indigo-500 animate-progress w-full opacity-100" />
      )}

      <div className="p-3 flex items-start gap-3">
        {/* Status Icon */}
        <div className={cn(
          "w-7 h-7 rounded-lg flex items-center justify-center shrink-0 transition-colors mt-0.5",
          isRunning ? "bg-indigo-50 text-indigo-600" :
          isSuccess ? "bg-green-50 text-green-600" :
          isFailed ? "bg-red-50 text-red-600" :
          "bg-gray-100 text-gray-400"
        )}>
          {isRunning ? <Loader2 size={14} className="animate-spin" /> :
           isSuccess ? <Check size={14} /> :
           isFailed ? <AlertCircle size={14} /> :
           <div className="text-[10px] font-bold font-mono">{data.index + 1}</div>
          }
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex justify-between items-start mb-0.5">
            <h3 className={cn("font-medium text-sm leading-snug truncate pr-2", 
              isRunning ? "text-indigo-700" : "text-gray-900"
            )}>
              {data.name}
            </h3>
          </div>
          
          <div className="flex items-center gap-2">
            <span className={cn("uppercase tracking-wider font-bold text-[9px]",
               data.type === 'agent' ? "text-purple-600" :
               data.type === 'tool' ? "text-blue-600" :
               "text-gray-500"
            )}>
              {data.type}
            </span>
            
            {isRunning && data.progress !== undefined && (
               <span className="text-[9px] font-mono text-indigo-500">
                 {data.progress}%
               </span>
            )}
          </div>

          {/* Result Snippet (if any) */}
          {data.result && !isRunning && (
            <div className="mt-2 text-[10px] bg-gray-50 border border-gray-100 px-2 py-1.5 rounded-lg text-gray-600 font-mono break-all line-clamp-2 leading-relaxed">
              {data.result}
            </div>
          )}
          
          {data.error && (
            <div className="mt-2 text-[10px] bg-red-50 border border-red-100 px-2 py-1.5 rounded-lg text-red-600 leading-relaxed">
              {data.error}
            </div>
          )}
        </div>
      </div>

      <Handle type="source" position={Position.Bottom} className="!bg-gray-300 !w-2 !h-2 !-bottom-1 opacity-0 group-hover:opacity-100 transition-opacity" />
    </div>
  );
};

export default memo(VAFWorkflowNode);
