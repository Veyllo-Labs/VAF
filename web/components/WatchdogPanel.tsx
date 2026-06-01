"use client";

import React, { useEffect, useRef, useState } from 'react';
import { getApiBase, cn } from '@/lib/utils';
import { Activity, Skull, Loader2 } from 'lucide-react';

/**
 * Live watchdog over the bounded, killable sub-agent execution. Polls the supervisor status
 * endpoint and shows what is running right now — agent type, runtime, heartbeat freshness —
 * with a per-unit kill button. Renders nothing when nothing is running.
 */
interface Unit {
  task_id: string;
  agent_type: string;
  session_id?: string | null;
  status: string;
  task?: string;
  runtime_s?: number | null;
  heartbeat_age_s?: number | null;
  stale?: boolean;
}

const POLL_MS = 2000;

function fmtDuration(s?: number | null): string {
  if (s == null) return '—';
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}m ${sec}s`;
}

/**
 * @param excludeAgentTypes agent types already shown inline in a running tool bubble — hidden
 *   here to avoid duplication. The panel then only surfaces units without an inline bubble
 *   (e.g. workflow steps / orphans).
 */
export default function WatchdogPanel({ excludeAgentTypes = [] }: { excludeAgentTypes?: string[] }) {
  const [units, setUnits] = useState<Unit[]>([]);
  const [killing, setKilling] = useState<Record<string, boolean>>({});
  const [collapsed, setCollapsed] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const poll = async () => {
    try {
      const r = await fetch(`${getApiBase()}/api/supervisor/status`, { credentials: 'include' });
      if (!r.ok) return;
      const d = await r.json();
      setUnits(Array.isArray(d.units) ? d.units : []);
    } catch {
      /* transient — keep last state */
    }
  };

  useEffect(() => {
    poll();
    timer.current = setInterval(poll, POLL_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  const kill = async (taskId: string) => {
    setKilling((k) => ({ ...k, [taskId]: true }));
    try {
      await fetch(`${getApiBase()}/api/supervisor/cancel`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId }),
      });
    } catch {
      /* ignore — the next poll reflects reality */
    }
    setUnits((u) => u.filter((x) => x.task_id !== taskId)); // optimistic
    setTimeout(poll, 300);
    setKilling((k) => {
      const n = { ...k };
      delete n[taskId];
      return n;
    });
  };

  const excluded = new Set(excludeAgentTypes.map((t) => t.toLowerCase()));
  const shown = units.filter((u) => !excluded.has((u.agent_type || '').toLowerCase()));
  if (shown.length === 0) return null;

  return (
    <div className="fixed bottom-4 left-4 z-[9990] w-80 max-w-[90vw] pointer-events-auto">
      <div className="rounded-xl border border-gray-200 bg-white/95 backdrop-blur shadow-lg overflow-hidden">
        <button
          onClick={() => setCollapsed((c) => !c)}
          className="w-full flex items-center gap-2 px-3 py-2 bg-gray-900 text-white text-xs font-semibold"
        >
          <Activity size={14} className="animate-pulse text-emerald-400" />
          <span>Active agents</span>
          <span className="ml-auto rounded-full bg-white/15 px-2 py-0.5 text-[11px]">{shown.length}</span>
          <span className="text-white/50">{collapsed ? '▸' : '▾'}</span>
        </button>

        {!collapsed && (
          <div className="max-h-72 overflow-y-auto divide-y divide-gray-100">
            {shown.map((u) => {
              const fresh = u.heartbeat_age_s != null && u.heartbeat_age_s < 10;
              const dot = u.stale ? 'bg-red-500' : fresh ? 'bg-emerald-500' : 'bg-amber-400';
              return (
                <div key={u.task_id} className="flex items-start gap-2 px-3 py-2">
                  <span className={cn('mt-1 h-2 w-2 rounded-full shrink-0', dot, !u.stale && 'animate-pulse')} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold text-gray-900 truncate">
                        {u.agent_type.replace(/_/g, ' ')}
                      </span>
                      <span className="text-[10px] text-gray-400 font-mono shrink-0">{fmtDuration(u.runtime_s)}</span>
                    </div>
                    {u.task ? <p className="text-[10px] text-gray-500 truncate">{u.task}</p> : null}
                    <p className="text-[10px]">
                      {u.stale ? (
                        <span className="text-red-500">no heartbeat for {fmtDuration(u.heartbeat_age_s)} — likely stuck</span>
                      ) : (
                        <span className="text-gray-400">heartbeat {fmtDuration(u.heartbeat_age_s)} ago</span>
                      )}
                    </p>
                  </div>
                  <button
                    onClick={() => kill(u.task_id)}
                    disabled={!!killing[u.task_id]}
                    title="Kill this agent"
                    className="shrink-0 p-1.5 rounded-lg text-red-500 hover:bg-red-50 disabled:opacity-40 transition-colors"
                  >
                    {killing[u.task_id] ? <Loader2 size={14} className="animate-spin" /> : <Skull size={14} />}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
