'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { useState, useEffect, useRef, useCallback, useMemo, memo, type CSSProperties, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import {
  X, RefreshCw, ChevronDown, ChevronRight, Activity, Search,
  GitBranch, ShieldCheck, ShieldAlert, Clock, CheckCircle2,
  XCircle, Loader2, Link2, Zap, Sparkles, MessageSquare, Info, GraduationCap, Menu,
  LayoutGrid, Bot, ShieldQuestion,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useThemeStore } from '@/lib/themeStore';
import { getApiBase } from '@/lib/utils';
import { useIsMobile } from '@/hooks/useIsMobile';
import ReactFlow, {
  Background, Controls, MiniMap,
  ReactFlowProvider,
  useNodesState, useEdgesState,
  Handle, Position, MarkerType,
  useReactFlow,
  type Node, type Edge,
} from 'reactflow';
import 'reactflow/dist/style.css';

// ─── Types ────────────────────────────────────────────────────────────────────

export type NotificationItem = {
  id: string;
  kind: 'thinking' | 'automation' | 'channel_reply' | 'system';
  title: string;
  status: 'success' | 'skipped' | 'error';
  timestamp: string;
  summary?: string;
  sessionId?: string;
  channel?: string;
  task_name?: string;
  run_id?: string;
  action?: 'approve' | 'reject' | string;
  task_id?: string;
  handoff_id?: string;
  automation_action_result?: {
    ok?: boolean;
    operation?: string;
    task_id?: string;
    error?: string;
  };
};

type LogFile = {
  filename: string;
  domain: string;
  date: string;
  size_bytes: number;
  modified: number;
};

type TimelineEvent = {
  ts: string;
  type: 'tool_start' | 'tool_end' | 'subagent_start' | 'thinking_run' | 'ww_train_start';
  tool?: string;
  call_id?: string;
  session?: string;
  scope?: string;
  args?: string;
  status?: 'ok' | 'error' | 'running';
  duration_s?: number;
  result?: string;
  mode?: string;
  source?: string;
  confirmed?: boolean;
  challenge_passed?: boolean;
  confidence?: number;
  task_id?: string;
  agent_type?: string;
  run_id?: string;
  ended_at?: string;
  prev_hash?: string;
  hash?: string;
};

type SandboxStatus = {
  state: 'ok' | 'warn';
  reason: 'container_running' | 'ephemeral_on_demand' | 'docker_unavailable';
  container_running: boolean;
  hardening?: {
    cap_drop_all: boolean;
    no_new_privileges: boolean;
    memory_bytes: number;
    nano_cpus: number;
    networks: string[];
    isolated_network: boolean;
  };
};

type DockerIsolation = {
  state: 'ok' | 'warn';
  any_lan_exposed: boolean;
  sandbox_off_internal: boolean | null;
  containers: {
    name: string;
    running: boolean;
    networks: string[];
    ports: { port: string; host_ip: string; host_port: string }[];
    lan_exposed: boolean;
  }[];
};

type FirewallStatus = {
  state: 'ok';
  reason: 'lan_enabled' | 'lan_disabled';
  lan_enabled: boolean;
  os_rules_enabled: boolean;
  blocked_today: number;
  failed_logins_today: number;
  docker?: DockerIsolation | null;
};

type SecurityEvent = {
  ts: string;
  kind: string;
  channel?: string;
  ip?: string;
  username?: string;
  path?: string;
  detail?: string;
};

type ChannelsStatus = {
  state: 'ok' | 'warn';
  any_permissive: boolean;
  rejected_today: number;
  channels: {
    name: string;
    enabled: boolean;
    mode: string;
    contact_fallback: boolean;
    paired: number;
    last_ts: number | null;
    rejected_today: number;
  }[];
};

type IsolationMetrics = {
  scope_count: number;
  scopes: { scope: string; username?: string; memories: number; chunks: number }[];
  db_size_bytes: number | null;
  rag_probe_ms: number | null;
  user_folders: { name: string; folders: number; size_bytes: number; truncated: boolean }[];
  workspace_count: number;
  total_size_bytes: number;
  truncated?: boolean;
};

type GuardrailsStatus = {
  state: 'ok';
  plan_gate: boolean;
  confirmation_gate: boolean;
  proactive_reply_gate: boolean;
  ask_first_drain_gate: boolean;
  channel_tools_unrestricted: boolean;
  tools: {
    total: number; read: number; write: number; dangerous: number; system: number;
    admin_only: number; channel_restricted: number;
  };
  trust: { trusted_dirs: string[]; allow_always_tools: string[] };
};

type SkillsStatus = {
  state: 'ok' | 'warn' | 'critical';
  total: number;
  counts: Record<string, number>;
  worst: string;
  quarantined_total: number;
  acknowledged_total: number;
  skills: { id: string; level: string; score: number; quarantined: boolean; acknowledged: boolean }[];
  blocked_today: number;
  overrides_today: number;
  alerts_today: number;
  last_rescan: { ts?: string; scanned?: number; changed?: number; alerts?: number; worst?: string } | null;
};

type ThinkingUserStatus = {
  username: string;
  scope: string;
  running: boolean;
  run_started_ts: number | null;
  waiting: { question: string; since_ts: number | null; channel: string; nudged: boolean; escalated: boolean; username: string } | null;
  minutes_since_last_run: number | null;
  last_run: { ended_at: string | null; duration_s: number | null; tools: string[] } | null;
  requests: { id: string; question: string; status: string; needs_reconfirm: boolean; created_at: string; updated_at: string }[];
};
type ThinkingStatus = { enabled: boolean; users: ThinkingUserStatus[] };

type SupervisorUnit = {
  task_id: string; agent_type: string; session_id: string | null; status: string;
  task: string; runtime_s: number | null; heartbeat_age_s: number | null; stale: boolean; username?: string;
};
type SupervisorStatus = { units: SupervisorUnit[]; liveness_timeout_s?: number; count?: number; error?: string };

type SkillScanDetail = {
  id: string;
  level: string;
  score: number;
  quarantined: boolean;
  acknowledged: boolean;
  findings: { category: string; severity: string; message: string; file: string; line: number; snippet: string }[];
};

type SecurityOverview = {
  sandbox?: SandboxStatus;
  firewall?: FirewallStatus;
  isolation?: IsolationMetrics | null;
  channels?: ChannelsStatus;
  guardrails?: GuardrailsStatus;
  skills?: SkillsStatus;
  security_latest_ts?: string | null;
};

type MemoryHealth = {
  status: string;
  db_connected: boolean;
  memory_enabled?: boolean;
};

type MailMessage = {
  from: string;
  subject: string;
  date: string;
  message_date_iso?: string | null;
  suspicious_for_agent?: boolean;
  suspicious_reasons?: string[];
  suspicious_score?: number;
};

export interface NotificationsModalProps {
  isOpen: boolean;
  onClose: () => void;
  notifications: NotificationItem[];
  onFetchComplete?: (list: NotificationItem[]) => void;
  userTimeFormat?: '24h' | '12h';
  /** Called when the admin views the security log; carries the newest event ts
   *  so the parent can clear the sidebar notification dot in sync. */
  onSecuritySeen?: (ts: string) => void;
}

// ─── Domain colors ─────────────────────────────────────────────────────────────

const DOMAIN_COLOR: Record<string, string> = {
  security:         'bg-red-500',
  rag:              'bg-blue-500',
  memory:           'bg-purple-500',
  backend:          'bg-orange-500',
  prompt:           'bg-green-500',
  headless:         'bg-gray-400',
  attach:           'bg-yellow-500',
  tool_use:         'bg-cyan-500',
  webui:            'bg-pink-500',
  vaf_think:        'bg-indigo-500',
  telegram_reply:   'bg-sky-500',
  discord_reply:    'bg-violet-500',
  whatsapp_qr:      'bg-emerald-500',
  whatsapp_inbound: 'bg-teal-500',
  whatsapp_reply:   'bg-green-600',
};

// ─── Tool category (vertical list) ────────────────────────────────────────────

function toolColor(tool: string): { bg: string; text: string; dot: string } {
  const t = tool.toLowerCase();
  if (/search|web|fetch|browse/.test(t)) return { bg: 'bg-blue-50',   text: 'text-blue-700',   dot: 'bg-blue-500' };
  if (/memory|remember|recall/.test(t))  return { bg: 'bg-purple-50', text: 'text-purple-700', dot: 'bg-purple-500' };
  if (/read|write|edit|file|glob|grep/.test(t)) return { bg: 'bg-green-50', text: 'text-green-700', dot: 'bg-green-500' };
  if (/bash|exec|run|code|python/.test(t)) return { bg: 'bg-orange-50', text: 'text-orange-700', dot: 'bg-orange-500' };
  if (/rag|embed|chunk|document/.test(t)) return { bg: 'bg-cyan-50',  text: 'text-cyan-700',   dot: 'bg-cyan-500' };
  if (/calendar|email|mail|slack|telegram|discord|whatsapp/.test(t)) return { bg: 'bg-pink-50', text: 'text-pink-700', dot: 'bg-pink-500' };
  return { bg: 'bg-gray-100', text: 'text-gray-700', dot: 'bg-gray-400' };
}

// ─── Activity kind meta ────────────────────────────────────────────────────────

type KindMeta = { bg: string; text: string; border: string; Icon: LucideIcon };
const KIND_META: Record<string, KindMeta> = {
  thinking:      { bg: 'bg-indigo-50', text: 'text-indigo-700', border: 'border-indigo-200', Icon: Sparkles },
  automation:    { bg: 'bg-amber-50',  text: 'text-amber-700',  border: 'border-amber-200',  Icon: Zap },
  channel_reply: { bg: 'bg-sky-50',    text: 'text-sky-700',    border: 'border-sky-200',    Icon: MessageSquare },
  system:        { bg: 'bg-slate-50',  text: 'text-slate-600',  border: 'border-slate-200',  Icon: Info },
};
const KIND_META_DEFAULT: KindMeta = { bg: 'bg-gray-50', text: 'text-gray-600', border: 'border-gray-200', Icon: Activity };

// ─── Horizontal timeline lanes ─────────────────────────────────────────────────

const TL_LANES = [
  { key: 'web',    label: 'Web / Search', color: '#3b82f6' },
  { key: 'file',   label: 'Files',        color: '#22c55e' },
  { key: 'memory', label: 'Memory',       color: '#a855f7' },
  { key: 'code',   label: 'Code / Bash',  color: '#f97316' },
  { key: 'comms',  label: 'Messages',     color: '#ec4899' },
  { key: 'agent',  label: 'Sub-Agents',   color: '#6366f1' },
  { key: 'learn',  label: 'Tool Learning', color: '#14b8a6' },
  { key: 'other',  label: 'Other',        color: '#9ca3af' },
] as const;

function tlCategory(ev: TimelineEvent): string {
  if (ev.type === 'ww_train_start') return 'learn';
  if (ev.type === 'subagent_start' || ev.type === 'thinking_run') return 'agent';
  const t = (ev.tool ?? '').toLowerCase();
  if (/search|web|fetch|browse|http|url/.test(t)) return 'web';
  if (/read|write|edit|file|glob|grep|path|notebook/.test(t)) return 'file';
  if (/memory|remember|recall|rag|embed|chunk|learn/.test(t)) return 'memory';
  if (/bash|exec|run|code|python|script/.test(t)) return 'code';
  if (/calendar|email|mail|slack|telegram|discord|whatsapp/.test(t)) return 'comms';
  return 'other';
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function formatRelativeTime(iso: string): string {
  try {
    const d = new Date(iso);
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return d.toLocaleDateString(undefined, { dateStyle: 'short' });
  } catch { return iso; }
}

function formatTime(iso: string, hour12?: boolean): string {
  try {
    const opts: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit', second: '2-digit' };
    if (hour12 !== undefined) opts.hour12 = hour12;
    return new Date(iso).toLocaleTimeString(undefined, opts);
  } catch { return iso; }
}


function parseLogLine(line: string): { ts: string; rest: string } {
  const m = line.match(/^(\d{4}-\d{2}-\d{2}T[\d:.]+)\s+([\s\S]*)$/);
  return m ? { ts: m[1].replace('T', ' '), rest: m[2] } : { ts: '', rest: line };
}

// ─── Chain visualization ───────────────────────────────────────────────────────

function ChainVisualization({ events, chainOk }: { events: TimelineEvent[]; chainOk: boolean }) {
  const sample = events.slice(0, 4);
  const blocks: Array<{ label: string; hash: string; ok: boolean }> = sample.length >= 2
    ? sample.map((ev, i) => ({
        label: i === 0 ? 'GENESIS' : (ev.tool ?? ev.type ?? '?').slice(0, 12),
        hash: (ev.hash ?? '').slice(0, 8) || '????????',
        ok: true,
      }))
    : [
        { label: 'GENESIS',    hash: 'a3f2c8d1', ok: true },
        { label: 'tool_start', hash: 'b7c14e90', ok: true },
        { label: 'tool_end',   hash: 'e2d9f30a', ok: true },
      ];
  if (!chainOk && blocks.length > 0) blocks[blocks.length - 1].ok = false;

  return (
    <div className="flex items-stretch gap-0 overflow-x-auto pb-1">
      {blocks.map((b, i) => (
        <div key={i} className="flex items-center gap-0 shrink-0">
          <div className={cn(
            'rounded-xl border px-2.5 py-2 min-w-[72px] text-center transition-colors',
            !b.ok ? 'bg-red-50 border-red-300' : i === blocks.length - 1 ? 'bg-gray-900 border-gray-700 text-white' : 'bg-gray-50 border-gray-200'
          )}>
            <p className={cn('text-[9px] uppercase tracking-wider font-semibold truncate',
              !b.ok ? 'text-red-500' : i === blocks.length - 1 ? 'text-gray-300' : 'text-gray-400')}>
              {b.label}
            </p>
            <p className={cn('text-[10px] font-mono mt-0.5',
              !b.ok ? 'text-red-600' : i === blocks.length - 1 ? 'text-yellow-300' : 'text-gray-600')}>
              {b.hash}…
            </p>
            {!b.ok && <p className="text-[9px] text-red-500 mt-0.5 font-semibold">BROKEN</p>}
          </div>
          {i < blocks.length - 1 && (
            <div className="flex items-center px-1 shrink-0">
              <div className={cn('h-px w-4', !b.ok || !blocks[i+1].ok ? 'bg-red-300' : 'bg-gray-300')} />
              <div className={cn('w-0 h-0 border-y-[3px] border-y-transparent border-l-[5px]',
                !b.ok || !blocks[i+1].ok ? 'border-l-red-300' : 'border-l-gray-300')} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ─── Process node — animated card with tool info ─────────────────────────────

const PROCESS_NODE_W = 210;

// Theme-aware color tokens for the Logs/observation window. The tailwind palette
// swap cannot reach this component — its timeline/canvas draw with raw inline hex —
// so the theme is resolved explicitly. LIGHT values are byte-identical to the
// pre-dark-mode literals; category hues (lane colors, error red, running amber,
// success green) are intentionally shared across both themes.
function tlColors(dark: boolean) {
  return dark
    ? {
        bg: '#202020', bgLabel: '#1c1c1c', bgRuler: '#262626', bgAlt: '#1e1e1e',
        panel: '#181818', border: '#2f2f2f', borderLt: '#262626', borderFaint: '#242424',
        tickMaj: '#6b6b6b', tickMin: '#383838', tsColor: '#60a5fa',
        dayLine: '#555555', dayText: '#b0b0b0', nowFill: '#ef444426',
        flowBg: '#262626', node: '#262626', cursorBar: '#d0d0d0',
        badgeBg: '#333333', badgeText: '#ececec', barTrack: '#333333', barTrackSel: '#484848',
        errBg: '#2a1414', warnBg: '#2a2410', errChrome: '#2a1416', liveChrome: '#2a1416',
        textStrong: '#ececec', textMid: '#b0b0b0', textDim: '#8a8a8a', textMuted: '#8a8a8a',
        textFaint: '#6b6b6b', textArg: '#9a9a9a', textResult: '#9a9a9a', textLabel: '#8a8a8a',
      }
    : {
        bg: '#ffffff', bgLabel: '#f9fafb', bgRuler: '#f3f4f6', bgAlt: '#fafafa',
        panel: '#f8fafc', border: '#e5e7eb', borderLt: '#f3f4f6', borderFaint: '#f1f5f9',
        tickMaj: '#9ca3af', tickMin: '#d1d5db', tsColor: '#3b82f6',
        dayLine: '#9ca3af', dayText: '#374151', nowFill: '#ef444418',
        flowBg: '#edf2f7', node: '#ffffff', cursorBar: '#1e293b',
        badgeBg: '#1e293b', badgeText: '#ffffff', barTrack: '#f1f5f9', barTrackSel: '#334155',
        errBg: '#fff5f5', warnBg: '#fffbeb', errChrome: '#fff1f2', liveChrome: '#fef2f2',
        textStrong: '#111827', textMid: '#374151', textDim: '#94a3b8', textMuted: '#9ca3af',
        textFaint: '#d1d5db', textArg: '#64748b', textResult: '#475569', textLabel: '#6b7280',
      };
}



const ProcessNode = memo(({ data }: { data: {
  tool?: string; evType: string; status: string; duration_s?: number;
  color: string; selected: boolean; args?: string; session?: string;
}}) => {
  const C = tlColors(useThemeStore((st) => st.theme) === 'dark');
  const isErr = data.status === 'error';
  const isRun = data.status === 'running';
  const accent = isErr ? '#ef4444' : isRun ? '#f59e0b' : data.color;

  return (
    <div style={{
      width: PROCESS_NODE_W,
      background: data.selected ? accent + '0d' : C.node,
      borderRadius: 10,
      border: `1.5px solid ${data.selected ? accent : accent + '35'}`,
      borderLeft: `4px solid ${accent}`,
      boxShadow: isRun
        ? `0 0 0 3px ${accent}25, 0 4px 16px ${accent}20`
        : data.selected
        ? `0 0 0 4px ${accent}40, 0 4px 16px ${accent}25`
        : `0 2px 8px rgba(0,0,0,0.07)`,
      cursor: 'pointer',
      overflow: 'hidden',
      animation: isRun ? 'pNodeGlow 2s ease-in-out infinite' : undefined,
      transition: 'box-shadow 0.2s',
    }}>
      <Handle type="target" position={Position.Left}  style={{ opacity: 0, width: 8, height: 8, top: '50%', transform: 'translateY(-50%)' }} />
      <Handle type="source" position={Position.Right} style={{ opacity: 0, width: 8, height: 8, top: '50%', transform: 'translateY(-50%)' }} />

      {/* Running progress sweep */}
      {isRun && (
        <div style={{ height: 2, background: `linear-gradient(90deg,transparent,${accent},transparent)`, animation: 'pNodeSweep 1.4s linear infinite' }} />
      )}

      <div style={{ padding: '8px 10px' }}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <div style={{ width: 7, height: 7, borderRadius: '50%', background: accent, flexShrink: 0,
            animation: isRun ? 'pNodePulse 1.2s ease-in-out infinite' : undefined }} />
          <span style={{ fontWeight: 700, fontSize: 11, color: C.textStrong,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
            {data.tool ?? data.evType}
          </span>
          <span style={{ fontSize: 12, flexShrink: 0, lineHeight: 1 }}>
            {isErr  && <span style={{ color: '#ef4444' }}>✗</span>}
            {isRun  && <span style={{ color: '#f59e0b', display:'inline-block', animation:'pNodeSpin 1s linear infinite' }}>↻</span>}
            {!isErr && !isRun && <span style={{ color: '#22c55e' }}>✓</span>}
          </span>
        </div>

        {/* Duration + session */}
        <div style={{ display: 'flex', gap: 8, marginTop: 3, alignItems: 'center' }}>
          {data.duration_s != null && (
            <span style={{ fontSize: 9, fontFamily: 'monospace', color: C.textMuted }}>
              {data.duration_s < 1 ? `${Math.round(data.duration_s * 1000)}ms` : `${data.duration_s.toFixed(2)}s`}
            </span>
          )}
          {data.session && (
            <span style={{ fontSize: 8, fontFamily: 'monospace', color: C.textFaint,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
              {data.session.slice(0, 12)}
            </span>
          )}
        </div>

        {/* Duration bar */}
        {data.duration_s != null && (
          <div style={{ marginTop: 5, height: 2, borderRadius: 1, background: data.selected ? C.barTrackSel : C.barTrack, overflow: 'hidden' }}>
            <div style={{ height: '100%', borderRadius: 1, background: accent,
              width: `${Math.min((data.duration_s / 60) * 100, 100)}%`,
              animation: 'pNodeBar 0.5s ease-out' }} />
          </div>
        )}

        {/* Args snippet */}
        {data.args && (
          <div style={{ marginTop: 4, fontSize: 9, color: C.textLabel,
            fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            <span style={{ color: C.textFaint }}>→ </span>
            {data.args.slice(0, 30)}
          </div>
        )}
      </div>
    </div>
  );
});
ProcessNode.displayName = 'ProcessNode';

const PROCESS_NODE_TYPES = { process: ProcessNode };

// Inject keyframes once
if (typeof document !== 'undefined' && !document.getElementById('vaf-pn-anim')) {
  const s = document.createElement('style');
  s.id = 'vaf-pn-anim';
  s.textContent = `
    @keyframes pNodeGlow  {0%,100%{box-shadow:0 0 0 3px #f59e0b25,0 4px 16px #f59e0b20}50%{box-shadow:0 0 0 7px #f59e0b35,0 6px 22px #f59e0b30}}
    @keyframes pNodePulse {0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.65)}}
    @keyframes pNodeSweep {0%{margin-left:-100%}100%{margin-left:100%}}
    @keyframes pNodeSpin  {to{transform:rotate(360deg)}}
    @keyframes pNodeBar   {from{width:0}}
  `;
  document.head.appendChild(s);
}

// ─── EventGraph: ReactFlow canvas that syncs scroll with the timeline below ───
// Uses ReactFlowProvider so useReactFlow works; scroll sync is bidirectional.

function EventGraphContent({
  nodes, edges, onNodeClick, onDeselect,
}: {
  nodes: Node[]; edges: Edge[];
  onNodeClick: (id: string) => void;
  onDeselect: () => void;
}) {
  const C = tlColors(useThemeStore((st) => st.theme) === 'dark');
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(nodes);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState(edges);

  useEffect(() => { setRfNodes(nodes); }, [nodes, setRfNodes]);
  useEffect(() => { setRfEdges(edges); }, [edges, setRfEdges]);

  return (
    <ReactFlow
      nodes={rfNodes} edges={rfEdges}
      onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
      nodeTypes={PROCESS_NODE_TYPES}
      nodesDraggable={false} nodesConnectable={false} elementsSelectable={true}
      fitView fitViewOptions={{ padding: 0.3, maxZoom: 2 }}
      minZoom={0.1} maxZoom={5}
      onNodeClick={(_, n) => onNodeClick(n.id)}
      onPaneClick={onDeselect}
      proOptions={{ hideAttribution: true }}
    >
      <Background color={C.flowBg} gap={32} />
      <Controls showInteractive={false} style={{ bottom: 8, right: 8, left: 'auto', top: 'auto' }} />
    </ReactFlow>
  );
}

function EventGraph(props: {
  nodes: Node[]; edges: Edge[];
  onNodeClick: (id: string) => void;
  onDeselect: () => void;
}) {
  return (
    <ReactFlowProvider>
      <EventGraphContent {...props} />
    </ReactFlowProvider>
  );
}

// ─── Horizontal observation timeline ──────────────────────────────────────────

const LANE_H      = 40;
const RULER_H     = 28;
const LABEL_W     = 90;
const MIN_EV_W    = 6;
const MIN_SPAN_MS = 5 * 60 * 1000;
const MIN_ZOOM    = 1.0;
const MAX_ZOOM    = 20;

function HorizontalTimeline({ events, date, hour12, i18n }: {
  events: TimelineEvent[]; date: string; hour12?: boolean;
  i18n?: { activityTitle: string; clickHint: string; canvasHint: string };
}) {
  const C = tlColors(useThemeStore((st) => st.theme) === 'dark');
  const scrollRef      = useRef<HTMLDivElement>(null);
  const [hovered,      setHovered]      = useState<{ ev: TimelineEvent; mx: number; my: number } | null>(null);
  const [containerW,      setContainerW]      = useState(800);
  const [playheadX,       setPlayheadX]       = useState<number | null>(null);
  const [zoom,            setZoom]            = useState(1);
  const [autoScroll,      setAutoScroll]      = useState(true);
  const [selectedCallId,  setSelectedCallId]  = useState<string | null>(null);
  const [detailEvent,     setDetailEvent]     = useState<TimelineEvent | null>(null);
  const [detailLogs,      setDetailLogs]      = useState<Array<{file:string;ts:string;line:string}>>([]);
  const [loadingDetailLogs, setLoadingDetailLogs] = useState(false);
  const [cursorTs,        setCursorTs]        = useState<number | null>(null);
  const isMobile = useIsMobile();
  // hour12 comes from parent (page.tsx fetches it from /api/user/persona)
  const isHour12 = hour12 ?? false;

  // Measure scroll-canvas width.
  // The ResizeObserver callback is deferred to the next animation frame so it never synchronously
  // triggers another layout pass — that synchronous re-entry is what spams the console with
  // "ResizeObserver loop completed with undelivered notifications" (very visible while hovering the
  // timeline, where rapid re-renders churn layout). We also skip sub-pixel-only changes so an
  // unchanged width never re-renders.
  useEffect(() => {
    let raf = 0;
    const measure = () => {
      if (!scrollRef.current) return;
      const w = scrollRef.current.getBoundingClientRect().width || 800;
      setContainerW(prev => (Math.abs(prev - w) > 0.5 ? w : prev));
    };
    measure();
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    });
    if (scrollRef.current) ro.observe(scrollRef.current);
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, []);

  // Auto-scroll to right (newest) when content changes — only if user hasn't scrolled away
  useEffect(() => {
    if (autoScroll && scrollRef.current)
      scrollRef.current.scrollLeft = scrollRef.current.scrollWidth;
  }, [events, containerW, zoom, autoScroll]);

  // Live-mode: external signal from the Live button sets cursorTs to now
  useEffect(() => {
    const handler = (e: Event) => {
      const ts = (e as CustomEvent<{ ts: number }>).detail?.ts ?? Date.now();
      setCursorTs(ts);
      setAutoScroll(true);
      if (scrollRef.current) scrollRef.current.scrollLeft = scrollRef.current.scrollWidth;
    };
    window.addEventListener('vaf-tl-live', handler);
    return () => window.removeEventListener('vaf-tl-live', handler);
  }, []);

  // Fetch real log lines from server when a canvas node is selected
  useEffect(() => {
    if (!detailEvent) { setDetailLogs([]); return; }
    setLoadingDetailLogs(true);
    setDetailLogs([]);
    const params = new URLSearchParams({ ts: detailEvent.ts, window_s: '30', type: detailEvent.type });
    if (detailEvent.call_id) params.set('call_id', detailEvent.call_id);
    if (detailEvent.run_id)  params.set('run_id',  detailEvent.run_id);
    if (detailEvent.tool)    params.set('tool',    detailEvent.tool);
    if (detailEvent.session) params.set('session', detailEvent.session);
    fetch(`${getApiBase()}/api/logs/timeline/context?${params}`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.lines) setDetailLogs(data.lines); })
      .catch(() => {})
      .finally(() => setLoadingDetailLogs(false));
  }, [detailEvent]);

  // Non-passive wheel handler: plain wheel → horizontal scroll, Ctrl+wheel → zoom
  // Must use native addEventListener with passive:false — React wheel events are passive
  // and cannot call preventDefault(), so Ctrl+scroll would also scroll instead of zoom.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) {
        const factor = e.deltaY > 0 ? 0.8 : 1.25;
        setZoom(z => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * factor)));
      } else {
        el.scrollLeft += e.deltaY + e.deltaX;
      }
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  const zoomIn  = () => setZoom(z => Math.min(MAX_ZOOM, z * 1.5));
  const zoomOut = () => setZoom(z => Math.max(MIN_ZOOM, z * 0.67));

  // Events whose timeline bar is touched by the cursor line.
  // Bar starts at ev.ts and extends for ev.duration_s seconds — same as how
  // the timeline renders bars (toX(evTs) with width = duration * pxPerMs).
  // Point events (no duration) use a ±15s threshold.
  const activeAtCursor = useMemo(() => {
    if (cursorTs === null || events.length === 0) return [];
    return events.filter(ev => {
      const startTs = new Date(ev.ts).getTime();
      if (ev.duration_s && ev.duration_s > 0) {
        return startTs <= cursorTs && cursorTs <= startTs + ev.duration_s * 1000;
      }
      return Math.abs(startTs - cursorTs) <= 15_000;
    });
  }, [cursorTs, events]);

  // Time range
  const now = Date.now();
  const today = new Date().toISOString().slice(0, 10);
  const isToday = date === today;
  const tsArr = events.map(e => new Date(e.ts).getTime()).filter(n => isFinite(n));
  const rawT0 = tsArr.length ? Math.min(...tsArr) : now - MIN_SPAN_MS;
  const rawT1 = tsArr.length ? Math.max(...tsArr) : now;
  const t0 = rawT0 - 30_000;
  const t1 = isToday ? Math.max(rawT1 + 60_000, now + 8 * 60_000) : rawT1 + 60_000;
  const spanMs = Math.max(t1 - t0, MIN_SPAN_MS);

  // Zoom: zoom=1 → fills container; higher = more detail
  const basePxPerMs = containerW / spanMs;
  const pxPerMs = Math.max(basePxPerMs * zoom, 0.00015);
  const totalW = Math.ceil(spanMs * pxPerMs);

  // Group by lane
  const byLane = new Map<string, TimelineEvent[]>();
  TL_LANES.forEach(l => byLane.set(l.key, []));
  events.forEach(ev => byLane.get(tlCategory(ev))?.push(ev));
  const activeLanes = TL_LANES.filter(l => (byLane.get(l.key)?.length ?? 0) > 0);

  // Ruler ticks
  const tickMs = spanMs / zoom < 2 * 60_000   ? 30_000 :
                 spanMs / zoom < 10 * 60_000  ? 60_000 :
                 spanMs / zoom < 60 * 60_000  ? 5 * 60_000 :
                 spanMs / zoom < 6 * 3_600_000 ? 15 * 60_000 : 3_600_000;
  const ticks: number[] = [];
  for (let t = Math.ceil(t0 / tickMs) * tickMs; t <= t1; t += tickMs) ticks.push(t);

  // Day boundaries
  const dayBounds: number[] = [];
  const d0 = new Date(t0); d0.setHours(0, 0, 0, 0); d0.setDate(d0.getDate() + 1);
  for (let db = d0.getTime(); db < t1; db += 86_400_000) dayBounds.push(db);

  const toX = (ts: number) => Math.round((ts - t0) * pxPerMs);

  // ── Event graph nodes / edges — built from activeAtCursor only ──────────────
  // Canvas is EMPTY when no cursor; shows only events active at cursor time.
  const { graphNodes, graphEdges } = useMemo(() => {
    if (cursorTs === null || activeAtCursor.length === 0)
      return { graphNodes: [], graphEdges: [] };

    const laneIndexMap  = new Map<string, number>(activeLanes.map((l, i) => [l.key as string, i]));
    const NODE_GAP      = 16;   // gap between nodes in the same lane
    const LANE_H_CANVAS = 110;  // row height (node ~75px + breathing room)

    // Sort chronologically — left = oldest, right = newest
    const sorted = [...activeAtCursor].sort(
      (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime()
    );

    // Per-lane counter: how many nodes placed so far in each lane
    const laneCount = new Map<number, number>();

    const nodes: Node[] = sorted.map((ev, i) => {
      const cat    = tlCategory(ev);
      const laneI  = laneIndexMap.get(cat) ?? activeLanes.length;
      const callId = ev.call_id ?? `ev-${i}`;
      const idx    = laneCount.get(laneI) ?? 0;
      laneCount.set(laneI, idx + 1);

      return {
        id: callId,
        type: 'process',
        position: { x: idx * (PROCESS_NODE_W + NODE_GAP), y: laneI * LANE_H_CANVAS },
        data: {
          tool:       ev.tool,
          evType:     ev.type,
          status:     ev.status ?? 'ok',
          duration_s: ev.duration_s,
          color:      TL_LANES.find(l => l.key === cat)?.color ?? '#9ca3af',
          selected:   callId === selectedCallId,
          args:       ev.args,
          session:    ev.session,
        },
      };
    });

    // No edges — chronological left-to-right layout makes connections obvious
    const edges: Edge[] = [];

    return { graphNodes: nodes, graphEdges: edges };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeAtCursor, selectedCallId, activeLanes.length, t0, pxPerMs, cursorTs]);

  // Playhead timestamp
  const playheadTs = playheadX !== null
    ? new Date(t0 + playheadX / pxPerMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: isHour12 })
    : null;

  // ── Color tokens ──
  const BG        = C.bg;
  const BG_LABEL  = C.bgLabel;
  const BG_RULER  = C.bgRuler;
  const BORDER    = C.border;
  const BORDER_LT = C.borderLt;
  const TICK_MAJ  = C.tickMaj;
  const TICK_MIN  = C.tickMin;
  const TS_COLOR  = C.tsColor;
  const DAY_LINE  = C.dayLine;
  const DAY_TEXT  = C.dayText;
  const NOW_LINE  = '#ef4444';
  const NOW_FILL  = C.nowFill;

  if (events.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-white">
        <p className="text-sm whitespace-pre-line text-center text-gray-400">
          {'No events yet.\nEnable Debug Logs and run the agent.'}
        </p>
      </div>
    );
  }

  return (
    // Outer: flex-col — top = EventGraph, bottom = timeline ruler+lanes
    <div className="flex-1 flex flex-col min-h-0" style={{ background: BG }}>

      {/* ── TOP: 2:3 split — left=Activity panel, right=ReactFlow canvas. On mobile the two
            stack vertically (Activity over Flow) so neither gets crushed in a side-by-side. ── */}
      <div style={{ display: 'flex', flexDirection: isMobile ? 'column' : 'row', flex: 1, minHeight: 0, borderBottom: `1px solid ${BORDER}` }}>

        {/* ── LEFT (2): Activity / detail panel ── */}
        <div style={{ flex: 2, minWidth: 0, minHeight: isMobile ? 160 : 0, display: 'flex', flexDirection: 'column', borderRight: isMobile ? 'none' : `1px solid ${BORDER}`, borderBottom: isMobile ? `1px solid ${BORDER}` : 'none', background: C.bg, position: 'relative' }}>
          {/* Header */}
          <div style={{ height: 36, flexShrink: 0, display: 'flex', alignItems: 'center', gap: 8, padding: '0 12px', borderBottom: `1px solid ${BORDER}`, background: BG_LABEL }}>
            <div style={{ width: 7, height: 7, borderRadius: '50%', background: cursorTs !== null ? '#6366f1' : C.tickMin }} />
            <span style={{ fontSize: 10, fontWeight: 700, color: C.textMid, letterSpacing: 0.3 }}>
              {cursorTs !== null
                ? new Date(cursorTs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: isHour12 })
                : (i18n?.activityTitle ?? 'Activity')}
            </span>
            {cursorTs !== null && (
              <span style={{ fontSize: 9, color: C.textDim, marginLeft: 'auto' }}>
                {activeAtCursor.length} event{activeAtCursor.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>

          {/* ── Detail popup (window in window) — appears when a canvas node is clicked ── */}
          {detailEvent && (() => {
            const cat   = tlCategory(detailEvent);
            const color = TL_LANES.find(l => l.key === cat)?.color ?? '#9ca3af';
            const isErr = detailEvent.status === 'error';
            const accent = isErr ? '#ef4444' : color;
            return (
              <div style={{
                position: 'absolute', inset: 8, zIndex: 20,
                background: C.bg,
                border: `1.5px solid ${accent}50`,
                borderLeft: `4px solid ${accent}`,
                borderRadius: 10,
                boxShadow: `0 8px 32px rgba(0,0,0,0.18), 0 0 0 4px ${accent}15`,
                display: 'flex', flexDirection: 'column',
                overflow: 'hidden',
              }}>
                {/* Header */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', background: accent + '0d', borderBottom: `1px solid ${accent}20`, flexShrink: 0 }}>
                  <div style={{ width: 8, height: 8, borderRadius: '50%', background: accent }} />
                  <span style={{ fontWeight: 700, fontSize: 12, color: C.textStrong, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {detailEvent.tool ?? detailEvent.type}
                  </span>
                  <span style={{ fontSize: 9, fontFamily: 'monospace', color: C.textDim, flexShrink: 0 }}>
                    {formatTime(detailEvent.ts, isHour12)}
                  </span>
                  <button
                    onClick={() => { setDetailEvent(null); setSelectedCallId(null); }}
                    style={{ width: 20, height: 20, borderRadius: 5, border: '1.5px solid #fca5a5', background: C.errChrome, color: '#ef4444', cursor: 'pointer', fontSize: 12, fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, lineHeight: 1 }}
                    title="Close"
                  >✕</button>
                </div>
                {/* Meta row */}
                <div style={{ display: 'flex', gap: 12, padding: '6px 10px', borderBottom: `1px solid ${C.borderFaint}`, flexShrink: 0 }}>
                  {detailEvent.duration_s != null && (
                    <span style={{ fontSize: 9, fontFamily: 'monospace', color: C.textArg }}>
                      ⏱ {detailEvent.duration_s < 1 ? `${Math.round(detailEvent.duration_s * 1000)}ms` : `${detailEvent.duration_s.toFixed(2)}s`}
                    </span>
                  )}
                  <span style={{ fontSize: 9, fontWeight: 600, color: isErr ? '#ef4444' : '#22c55e' }}>
                    {detailEvent.status ?? 'ok'}
                  </span>
                  {detailEvent.session && (
                    <span style={{ fontSize: 9, fontFamily: 'monospace', color: C.textFaint, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                      {detailEvent.session}
                    </span>
                  )}
                </div>
                {/* Scrollable log content — white background */}
                <div style={{ flex: 1, overflowY: 'auto', background: C.bg, borderTop: `1px solid ${C.borderFaint}` }}>
                  {loadingDetailLogs ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 10px', color: C.textDim, fontSize: 10, fontFamily: 'monospace' }}>
                      <Loader2 size={12} className="animate-spin" />loading logs…
                    </div>
                  ) : detailLogs.length === 0 ? (
                    <div style={{ padding: '12px 10px', color: C.textDim, fontSize: 10, fontFamily: 'monospace', textAlign: 'center' }}>
                      No log lines found within ±60s
                    </div>
                  ) : (
                    <div>
                      {detailLogs.map((entry, i) => {
                        const isTimeline = entry.file.startsWith('timeline_');
                        const isErr  = /error/i.test(entry.line);
                        const isWarn = /warn/i.test(entry.line);
                        const lineColor = isErr ? '#ef4444' : isWarn ? '#f59e0b' : isTimeline ? '#3b82f6' : C.textMid;
                        const bgColor   = isErr ? C.errBg : isWarn ? C.warnBg : i % 2 === 0 ? C.bg : C.bgLabel;
                        return (
                          <div key={i} style={{ display: 'flex', gap: 8, padding: (entry as any).block ? '6px 10px' : '2px 10px', fontFamily: 'monospace', fontSize: 11, lineHeight: 1.6, borderBottom: `1px solid ${C.borderLt}`, background: bgColor }}>
                            <span style={{ color: C.textDim, flexShrink: 0, minWidth: 80, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600, fontSize: 10 }}>
                              {entry.file.replace(/_\d{4}-\d{2}-\d{2}/, '').replace(/\.log$|\.jsonl$/, '')}
                            </span>
                            <span style={{ color: lineColor, wordBreak: 'break-word', flex: 1, whiteSpace: (entry as any).block ? 'pre-wrap' : 'normal' }}>
                              {entry.line}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            );
          })()}

          {/* Content */}
          {cursorTs === null ? (
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 6, padding: 16 }}>
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke={C.textFaint} strokeWidth="1.5">
                <line x1="12" y1="2" x2="12" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/>
                <circle cx="12" cy="12" r="3" fill={C.textFaint}/>
              </svg>
              <span style={{ fontSize: 10, color: C.textMuted, fontFamily: 'monospace', textAlign: 'center' }}>
                {(i18n?.clickHint ?? 'Click on the timeline\nto inspect that moment').split('\n').map((l, i) => <span key={i}>{l}{i === 0 && <br/>}</span>)}
              </span>
            </div>
          ) : (
            <div style={{ flex: 1, overflowY: 'auto' }}>
              {activeAtCursor.map((ev, i) => {
                const cat   = tlCategory(ev);
                const color = TL_LANES.find(l => l.key === cat)?.color ?? '#9ca3af';
                const isErr = ev.status === 'error';
                return (
                  <div key={i} style={{ display: 'flex', gap: 10, padding: '7px 12px', borderBottom: `1px solid ${C.panel}`, alignItems: 'flex-start' }}>
                    <div style={{ width: 3, borderRadius: 2, background: isErr ? '#ef4444' : color, flexShrink: 0, alignSelf: 'stretch', minHeight: 16 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                        <span style={{ fontSize: 12, fontWeight: 700, color: C.textStrong }}>{ev.tool ?? ev.type}</span>
                        {ev.duration_s != null && (
                          <span style={{ fontSize: 11, fontFamily: 'monospace', color: C.textDim }}>
                            {ev.duration_s < 1 ? `${Math.round(ev.duration_s * 1000)}ms` : `${ev.duration_s.toFixed(2)}s`}
                          </span>
                        )}
                        <span style={{ fontSize: 11, color: isErr ? '#ef4444' : '#22c55e', fontWeight: 600 }}>{ev.status ?? 'ok'}</span>
                      </div>
                      {ev.args && (
                        <div style={{ fontSize: 11, color: C.textArg, fontFamily: 'monospace', marginTop: 3, wordBreak: 'break-all', lineHeight: 1.5, maxHeight: 56, overflow: 'hidden' }}>
                          <span style={{ color: C.textDim }}>→ </span>{ev.args.slice(0, 200)}
                        </div>
                      )}
                      {ev.result && (
                        <div style={{ fontSize: 11, color: C.textResult, fontFamily: 'monospace', marginTop: 2, wordBreak: 'break-all', lineHeight: 1.5, maxHeight: 56, overflow: 'hidden' }}>
                          <span style={{ color: C.textDim }}>← </span>{ev.result.slice(0, 200)}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* ── RIGHT (3): ReactFlow canvas ── */}
        <div style={{ flex: 3, minWidth: 0, minHeight: isMobile ? 200 : 0, position: 'relative', background: C.panel }}>
          {cursorTs === null ? (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <span style={{ fontSize: 10, color: C.textFaint, fontFamily: 'monospace' }}>{i18n?.canvasHint ?? 'Flow'}</span>
            </div>
          ) : (
            <EventGraph
              key={`${cursorTs}-${activeLanes.length}`}
              nodes={graphNodes}
              edges={graphEdges}
              onNodeClick={id => {
                const isNew = id !== selectedCallId;
                setSelectedCallId(isNew ? id : null);
                setDetailEvent(isNew
                  ? (activeAtCursor.find(e => (e.call_id ?? `ev-${activeAtCursor.indexOf(e)}`) === id) ?? null)
                  : null);
              }}
              onDeselect={() => { setSelectedCallId(null); setDetailEvent(null); }}
            />
          )}
        </div>
      </div>

      {/* ── Timeline row: labels + scrollable canvas ── */}
      <div style={{ display: 'flex', flexShrink: 0, borderTop: `1px solid ${BORDER}` }}>

        {/* Lane labels — bottom of container, lanes stack upward */}
        <div style={{ width: LABEL_W, background: BG_LABEL, borderRight: `1px solid ${BORDER}`, display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
          {/* Ruler spacer */}
          <div style={{ height: RULER_H, background: BG_RULER, borderBottom: `1px solid ${BORDER}`, flexShrink: 0 }} />
          {activeLanes.map(lane => (
            <div key={lane.key} style={{ height: LANE_H, borderBottom: `1px solid ${BORDER_LT}`, display: 'flex', alignItems: 'center', paddingLeft: 10, gap: 6, flexShrink: 0 }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: lane.color, flexShrink: 0 }} />
              <span style={{ fontSize: 10, color: C.textLabel, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {lane.label}
              </span>
            </div>
          ))}
        </div>

        {/* Scrollable canvas */}
        <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>

          {/* Zoom buttons — sticky top-right, outside scroll area */}
          <div style={{ position: 'absolute', top: 4, right: 6, zIndex: 30, display: 'flex', alignItems: 'center', gap: 2, pointerEvents: 'auto' }}>
            <button onClick={zoomOut} title="Zoom out (Ctrl+scroll)" style={{ width: 20, height: 20, borderRadius: 4, border: `1px solid ${C.tickMin}`, background: C.bgLabel, color: C.textMid, fontSize: 14, lineHeight: 1, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 600 }}>−</button>
            <span style={{ fontSize: 9, fontFamily: 'monospace', color: C.textLabel, minWidth: 26, textAlign: 'center', userSelect: 'none' }}>
              {zoom < 1 ? `${(zoom * 100).toFixed(0)}%` : `${zoom.toFixed(1)}×`}
            </span>
            <button onClick={zoomIn}  title="Zoom in (Ctrl+scroll)"  style={{ width: 20, height: 20, borderRadius: 4, border: `1px solid ${C.tickMin}`, background: C.bgLabel, color: C.textMid, fontSize: 14, lineHeight: 1, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 600 }}>+</button>
            {!autoScroll && (
              <button
                onClick={() => {
                  setAutoScroll(true);
                  setCursorTs(Date.now()); // jump cursor to live position
                  if (scrollRef.current) scrollRef.current.scrollLeft = scrollRef.current.scrollWidth;
                }}
                title="Jump to now"
                style={{ height: 20, padding: '0 5px', borderRadius: 4, border: '1px solid #fca5a5', background: C.liveChrome, color: '#ef4444', fontSize: 9, fontFamily: 'monospace', cursor: 'pointer', whiteSpace: 'nowrap' }}
              >▶ live</button>
            )}
          </div>

          <div
            ref={scrollRef}
            style={{ width: '100%', height: '100%', overflowX: 'auto', overflowY: 'hidden', position: 'relative', cursor: 'crosshair' }}
            onMouseMove={e => {
              if (!scrollRef.current) return;
              const rect = scrollRef.current.getBoundingClientRect();
              setPlayheadX(e.clientX - rect.left + scrollRef.current.scrollLeft);
            }}
            onMouseLeave={() => setPlayheadX(null)}
            onScroll={() => {
              if (!scrollRef.current) return;
              const { scrollLeft, scrollWidth, clientWidth } = scrollRef.current;
              setAutoScroll(scrollLeft + clientWidth >= scrollWidth - 4);
            }}
            onClick={e => {
              if (!scrollRef.current) return;
              const rect = scrollRef.current.getBoundingClientRect();
              const clickX = e.clientX - rect.left + scrollRef.current.scrollLeft;
              const ts = t0 + clickX / pxPerMs;
              setCursorTs(prev => prev === ts ? null : ts);
            }}
          >
            <div style={{ width: totalW, position: 'relative', boxSizing: 'content-box' }}>

              {/* Ruler */}
              <div style={{ height: RULER_H, background: BG_RULER, borderBottom: `1px solid ${BORDER}`, position: 'relative', overflow: 'hidden' }}>
                {ticks.map(tick => {
                  const x = toX(tick);
                  const isHourMark = tick % 3_600_000 === 0;
                  return (
                    <div key={tick} style={{ position: 'absolute', left: x, top: 0, bottom: 0 }}>
                      <div style={{ width: 1, height: isHourMark ? 14 : 7, background: isHourMark ? TICK_MAJ : TICK_MIN, position: 'absolute', bottom: 0 }} />
                      <span style={{ position: 'absolute', top: 4, left: 3, fontSize: 9, color: TS_COLOR, whiteSpace: 'nowrap', fontFamily: 'monospace', fontWeight: 600 }}>
                        {new Date(tick).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: isHour12 })}
                      </span>
                    </div>
                  );
                })}
                {dayBounds.map(db => (
                  <div key={db} style={{ position: 'absolute', left: toX(db), top: 0, bottom: 0, width: 1, background: DAY_LINE }}>
                    <span style={{ position: 'absolute', top: 4, left: 3, fontSize: 9, color: DAY_TEXT, fontFamily: 'monospace', fontWeight: 700, whiteSpace: 'nowrap' }}>
                      {new Date(db).toLocaleDateString([], { month: 'short', day: 'numeric' })}
                    </span>
                  </div>
                ))}
                {isToday && <div style={{ position: 'absolute', left: toX(now), top: 0, bottom: 0, width: 3, background: NOW_LINE }} />}
                {/* Cursor marker in ruler — thick black line with time badge */}
                {cursorTs !== null && (
                  <div style={{ position: 'absolute', left: toX(cursorTs), top: 0, bottom: 0, pointerEvents: 'none', zIndex: 25 }}>
                    <div style={{ position: 'absolute', top: 0, bottom: 0, left: -1, width: 3, background: C.cursorBar }} />
                    <div style={{ position: 'absolute', bottom: -1, left: -5, width: 0, height: 0, borderLeft: '5px solid transparent', borderRight: '5px solid transparent', borderTop: `8px solid ${C.cursorBar}` }} />
                    <span style={{ position: 'absolute', top: 3, left: 6, fontSize: 9, color: C.badgeText, fontFamily: 'monospace', fontWeight: 700, whiteSpace: 'nowrap', background: C.badgeBg, padding: '1px 4px', borderRadius: 3 }}>
                      {new Date(cursorTs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: isHour12 })}
                    </span>
                  </div>
                )}
                {/* Playhead in ruler */}
                {playheadX !== null && playheadTs && (
                  <div style={{ position: 'absolute', left: playheadX, top: 0, bottom: 0, pointerEvents: 'none', zIndex: 20 }}>
                    <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: 1, borderLeft: `1px dashed ${C.textMid}`, opacity: 0.5 }} />
                    <div style={{ position: 'absolute', bottom: -1, left: -4, width: 0, height: 0, borderLeft: '4px solid transparent', borderRight: '4px solid transparent', borderTop: `6px solid ${C.textMid}` }} />
                    <span style={{ position: 'absolute', top: 3, left: 5, fontSize: 9, color: C.textStrong, fontFamily: 'monospace', fontWeight: 700, whiteSpace: 'nowrap', background: C.bg, padding: '1px 3px', borderRadius: 3, border: `1px solid ${C.border}`, boxShadow: '0 1px 3px rgba(0,0,0,0.08)' }}>
                      {playheadTs}
                    </span>
                  </div>
                )}
              </div>

              {/* Lane rows */}
              {activeLanes.map((lane, li) => (
                <div key={lane.key} style={{ height: LANE_H, position: 'relative', borderBottom: `1px solid ${BORDER_LT}`, overflow: 'hidden', background: li % 2 === 0 ? BG : C.bgAlt }}>
                  {ticks.map(tick => <div key={tick} style={{ position: 'absolute', left: toX(tick), top: 0, bottom: 0, width: 1, background: BORDER_LT }} />)}
                  {dayBounds.map(db => <div key={db} style={{ position: 'absolute', left: toX(db), top: 0, bottom: 0, width: 1, background: BORDER }} />)}
                  {isToday && <div style={{ position: 'absolute', left: toX(now), top: 0, bottom: 0, width: 1, background: NOW_FILL }} />}
                  {playheadX !== null && <div style={{ position: 'absolute', left: playheadX, top: 0, bottom: 0, width: 1, borderLeft: `1px dashed ${C.textMid}66`, pointerEvents: 'none', zIndex: 5 }} />}
                  {/* Thick cursor line — marks the clicked position */}
                  {cursorTs !== null && <div style={{ position: 'absolute', left: toX(cursorTs) - 1, top: 0, bottom: 0, width: 3, background: C.cursorBar, opacity: 0.85, pointerEvents: 'none', zIndex: 10 }} />}
                  {/* Events */}
                  {(byLane.get(lane.key) ?? []).map((ev, i) => {
                    const evTs  = new Date(ev.ts).getTime();
                    const x     = toX(evTs);
                    const dur   = ev.duration_s ?? 0.3;
                    const w     = Math.max(dur * 1000 * pxPerMs, MIN_EV_W);
                    const isErr = ev.status === 'error';
                    const isRun = ev.status === 'running';
                    const col   = isErr ? '#ef4444' : isRun ? '#f59e0b' : lane.color;
                    const isSelected = detailEvent === ev || (ev.call_id != null && ev.call_id === selectedCallId);
                    return (
                      <div
                        key={i}
                        style={{
                          position: 'absolute', left: x, width: w, top: 8, height: LANE_H - 16,
                          background: `linear-gradient(90deg,${col},${col}99)`,
                          borderRadius: 3, borderLeft: `2px solid ${col}`, cursor: 'pointer',
                          boxShadow: isSelected ? `0 0 0 2px ${col}, 0 2px 8px ${col}60` : `0 1px 4px ${col}40`,
                          outline: isSelected ? `2px solid ${col}` : 'none',
                          outlineOffset: 1,
                          opacity: (selectedCallId || detailEvent) && !isSelected ? 0.45 : 1,
                          transition: 'opacity 0.15s, box-shadow 0.15s',
                        }}
                        onMouseEnter={e => setHovered({ ev, mx: e.clientX, my: e.clientY })}
                        onMouseLeave={() => setHovered(null)}
                        onMouseMove={e => setHovered(p => p ? { ...p, mx: e.clientX, my: e.clientY } : null)}
                        onClick={e => {
                          // Don't let the click bubble to the scroll-container handler, which sets
                          // cursorTs from the raw click pixel (t0 + clickX/pxPerMs). For short bars
                          // (clamped to MIN_EV_W=6px) that pixel maps to a time far outside the
                          // event's real duration window, so the event would be excluded from
                          // activeAtCursor and the right-panel node graph would render empty.
                          e.stopPropagation();
                          // Clicking a timeline bar always surfaces it in the detail/log panel above
                          // (mirrors the flow-canvas node click), and highlights it here — even for
                          // events without a call_id. Re-clicking the same bar clears it.
                          const isSame = detailEvent === ev;
                          setDetailEvent(isSame ? null : ev);
                          setSelectedCallId(isSame ? null : (ev.call_id ?? null));
                          // Anchor the cursor INSIDE this event's own time window (its center) so
                          // activeAtCursor always includes it, regardless of how thin the rendered
                          // bar was. This drives the right-panel canvas. Re-clicking clears it.
                          if (isSame) {
                            setCursorTs(null);
                          } else {
                            const startTs = new Date(ev.ts).getTime();
                            setCursorTs(startTs + ((ev.duration_s ?? 0) * 1000) / 2);
                          }
                        }}
                      />
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Event tooltip */}
      {hovered && (
        <div style={{ position: 'fixed', left: hovered.mx + 14, top: hovered.my - 68, zIndex: 300, pointerEvents: 'none', maxWidth: 260 }} className="rounded-xl px-3 py-2 shadow-2xl border border-gray-200 bg-white">
          <p className="text-xs font-semibold text-gray-900 truncate">{hovered.ev.tool ?? hovered.ev.type}</p>
          <p className="text-[10px] font-mono mt-0.5 text-blue-500">{formatTime(hovered.ev.ts, isHour12)}</p>
          {typeof hovered.ev.duration_s === 'number' && (
            <p className="text-[10px] text-gray-500 mt-0.5">
              {hovered.ev.duration_s < 1 ? `${Math.round(hovered.ev.duration_s * 1000)}ms` : `${hovered.ev.duration_s.toFixed(2)}s`}
            </p>
          )}
          {hovered.ev.status && (
            <p className={cn('text-[10px] font-medium mt-0.5', hovered.ev.status === 'ok' ? 'text-green-600' : hovered.ev.status === 'error' ? 'text-red-500' : 'text-amber-500')}>
              {hovered.ev.status}
            </p>
          )}
          {hovered.ev.session && <p className="text-[10px] font-mono mt-0.5 text-gray-400 truncate">session: {hovered.ev.session.slice(0, 12)}…</p>}
          {hovered.ev.scope   && <p className="text-[10px] font-mono mt-0.5 text-gray-400 truncate">user: {hovered.ev.scope.slice(0, 12)}…</p>}
        </div>
      )}
    </div>
  );
}

// ─── Overview pane - antivirus-style protection dashboard ─────────────────────
// Renders the complete Overview layout (hero, module grid, chain + skills panels,
// background-agent panel, activity strip, supervised units, security posture).
// Every metric deliberately renders the explicit no-data state instead of a
// placeholder value; the status endpoints are wired step by step afterwards.

type OvColors = ReturnType<typeof tlColors>;

function OvSectionHeading({ C, title, hint }: { C: OvColors; title: string; hint?: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, margin: '22px 2px 10px' }}>
      <h3 style={{ fontSize: 13, fontWeight: 700, color: C.textStrong, margin: 0, letterSpacing: '0.01em' }}>{title}</h3>
      {hint && <span style={{ fontSize: 11, color: C.textFaint }}>{hint}</span>}
    </div>
  );
}

function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !isFinite(n)) return '–';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

function OvNoData({ C, label }: { C: OvColors; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, color: C.textDim }}>
      <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'transparent', border: `1.5px solid ${C.textFaint}`, flexShrink: 0 }} />
      {label}
    </span>
  );
}

function OverviewPane({ chainOk, events, totalRaw, dates, date, today, onDateChange, security, memHealth, mail, thinking, supervisor, securityLogFile, onOpenLogFile, onRefreshSecurity }: {
  chainOk: boolean | null;
  events: TimelineEvent[];
  totalRaw: number | null;
  dates: string[];
  date: string;
  today: string;
  onDateChange: (d: string) => void;
  security: SecurityOverview | null;
  memHealth: MemoryHealth | null;
  mail: MailMessage[] | null;
  thinking: ThinkingStatus | null;
  supervisor: SupervisorStatus | null;
  securityLogFile?: string;
  onOpenLogFile: (filename: string) => void;
  onRefreshSecurity: () => void;
}) {
  const t = useTranslations('notifications');
  const dark = useThemeStore((st) => st.theme) === 'dark';
  const C = tlColors(dark);
  const noData = t('ovNoData');

  // ── Audit-chain state (wired live; the first READY signal of the dashboard) ──
  // Honesty floor: chain_ok defaults true on an empty/missing file, so "no events"
  // must render as not-measured (grey), never as verified-green.
  const hasChainData = chainOk !== null && (totalRaw ?? 0) > 0;
  const heroState: 'ok' | 'broken' | 'nodata' = chainOk === false ? 'broken' : hasChainData ? 'ok' : 'nodata';
  const green = dark ? '#4ade80' : '#15803d';
  const red = dark ? '#f87171' : '#b91c1c';
  const lastEv = events.length > 0 ? events[events.length - 1] : null;
  const tailHashes = events.filter(e => e.hash).slice(-4);
  const isToday = date === today;
  const heroChrome = heroState === 'ok'
    ? { border: 'rgba(34,197,94,.28)', bg: 'linear-gradient(135deg, rgba(34,197,94,.11), rgba(34,197,94,.02))', emblemBg: 'rgba(34,197,94,.14)', emblemBorder: 'rgba(34,197,94,.4)', color: green }
    : heroState === 'broken'
      ? { border: 'rgba(239,68,68,.34)', bg: 'linear-gradient(135deg, rgba(239,68,68,.13), rgba(239,68,68,.02))', emblemBg: 'rgba(239,68,68,.14)', emblemBorder: 'rgba(239,68,68,.45)', color: red }
      : { border: C.border, bg: `linear-gradient(135deg, ${C.bgRuler}, transparent)`, emblemBg: C.bgRuler, emblemBorder: C.border, color: C.textDim };
  const HeroIcon = heroState === 'ok' ? ShieldCheck : heroState === 'broken' ? ShieldAlert : ShieldQuestion;
  // Overall protection status = worst-of roll-up over every module, four states:
  //   critical (red) > attention (amber) > ok (green), with a nodata (grey) floor
  //   so absent data never reads as safe. Today only the audit chain is wired, so
  //   the only live critical input is a tampered chain; amber inputs (medium
  //   findings, RLS pending cutover, a stale supervised unit, channel_tools_
  //   unrestricted, a medium-risk skill, ...) join this array as they are wired.
  // Sandbox module (live via /api/security/overview): ok = container-enforced
  // (running or ephemeral-on-demand), warn = docker down -> execution BLOCKED
  // (fail-closed, so attention rather than critical), undefined = not measured.
  const sandbox = security?.sandbox;
  // Firewall / LAN perimeter (live): blocks happening = the perimeter working
  // (stays green with a count); lan disabled = remote surface closed entirely.
  const firewall = security?.firewall;
  const fwDeflected = firewall ? firewall.blocked_today + firewall.failed_logins_today : 0;
  // User isolation (live via the existing scope-aware memory endpoints):
  //   green = enforced (scope-filtered stats reachable), amber = memory DB down
  //   (the known real failure: silent empty memory), neutral = memory disabled.
  const isoState: 'ok' | 'db_down' | 'mem_off' | 'nodata' =
    !memHealth ? 'nodata'
      : memHealth.memory_enabled === false ? 'mem_off'
        : memHealth.db_connected === false ? 'db_down'
          : 'ok';
  // Admin aggregate metrics (RAG latency, per-scope store sizes, workspaces).
  const isolation = security?.isolation ?? null;

  // ── Module detail popup ──────────────────────────────────────────────────────
  const [detail, setDetail] = useState<string | null>(null);
  // Blocked/rejected attempts for the firewall popup, fetched lazily on open.
  const [secEvents, setSecEvents] = useState<SecurityEvent[] | null>(null);
  useEffect(() => {
    if (detail !== 'firewall' && detail !== 'channels') return;
    fetch(`${getApiBase()}/api/security/events?limit=100`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setSecEvents(Array.isArray(d?.events) ? d.events : []))
      .catch(() => setSecEvents([]));
  }, [detail]);
  const evKindLabel = (kind: string): string => {
    switch (kind) {
      case 'ip_blocked': return t('ovEvIpBlocked');
      case 'unauthenticated_blocked': return t('ovEvUnauth');
      case 'token_rejected': return t('ovEvToken');
      case 'login_failed': return t('ovEvLogin');
      case 'twofa_failed': return t('ovEv2fa');
      case 'ws_rejected': return t('ovEvWs');
      case 'channel_rejected': return t('ovEvChannel');
      default: return kind;
    }
  };
  const channels = security?.channels;
  const guardrails = security?.guardrails;
  const skills = security?.skills;

  // ── Skill resolution actions (delete / acknowledge-2FA / restore-2FA / isolate) ─
  type SkillAction = 'delete' | 'acknowledge' | 'restore' | 'isolate';
  const NEEDS_2FA: Record<SkillAction, boolean> = { delete: false, isolate: false, acknowledge: true, restore: true };
  const [qCode, setQCode] = useState('');
  const [qBusy, setQBusy] = useState(false);
  const [qError, setQError] = useState<string | null>(null);
  const [qPending, setQPending] = useState<SkillAction | null>(null); // action awaiting a 2FA code
  // Live re-scan findings for the skill detail popup (the "why").
  const [skillScan, setSkillScan] = useState<SkillScanDetail | null>(null);
  const runSkillAction = async (id: string, action: SkillAction) => {
    setQBusy(true);
    setQError(null);
    try {
      const res = await fetch(`${getApiBase()}/api/security/skills/${encodeURIComponent(id)}/${action}`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(NEEDS_2FA[action] ? { code: qCode } : {}),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        setQError(String(d?.detail ?? `HTTP ${res.status}`));
      } else {
        setQCode('');
        setQPending(null);
        if (action === 'delete') setDetail(null);
        onRefreshSecurity();
        setSkillScan(null); // force the popup to re-fetch the (now changed) state
      }
    } catch {
      setQError('network error');
    }
    setQBusy(false);
  };
  const detailSkillId = detail?.startsWith('skill:') ? detail.slice(6) : null;
  useEffect(() => {
    if (!detailSkillId) { setSkillScan(null); return; }
    setSkillScan(null); setQPending(null); setQCode(''); setQError(null);
    fetch(`${getApiBase()}/api/security/skills/${encodeURIComponent(detailSkillId)}/scan`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setSkillScan(d && typeof d === 'object' ? d : null))
      .catch(() => setSkillScan(null));
  }, [detailSkillId]);
  // Phishing shield: flags happening = the shield working (green with count).
  // No synced mail = not scanning anything -> neutral grey, never false-green.
  const mailFlagged = mail?.filter(m => m.suspicious_for_agent) ?? null;
  const phReason = (code: string): string => {
    switch (code) {
      case 'provider_spam_category': return t('ovPhRProvider');
      case 'punycode_domain': return t('ovPhRPunycode');
      case 'social_engineering_language': return t('ovPhRSocial');
      case 'exec_impersonation_free_mail': return t('ovPhRImperson');
      case 'phishing_pattern': return t('ovPhRPattern');
      default: return code;
    }
  };
  // Worst-of roll-up, driven by NAMED reasons so the hero states the actual
  // cause (a HIGH skill and a tampered chain both go red, but must not both
  // say "chain tampered"). Reasons are ordered most-severe first.
  const quarantinedSkills = skills?.quarantined_total ?? 0;
  const criticalReasons: string[] = [
    chainOk === false ? t('ovRcChain') : '',
    quarantinedSkills > 0 ? t('ovRcSkillQuarantined', { n: quarantinedSkills }) : '',
    (skills?.state === 'critical' && quarantinedSkills === 0) ? t('ovRcSkillHigh') : '',
  ].filter(Boolean);
  const amberReasons: string[] = [
    sandbox?.state === 'warn' ? t('ovRaSandbox') : '',
    isoState === 'db_down' ? t('ovRaDb') : '',
    firewall?.docker?.state === 'warn' ? t('ovRaDocker') : '',
    channels?.state === 'warn' ? t('ovRaChannel') : '',
    skills?.state === 'warn' ? t('ovRaSkillMedium') : '',
  ].filter(Boolean);
  const overallState: 'critical' | 'attention' | 'ok' | 'nodata' =
    criticalReasons.length > 0 ? 'critical'
      : !hasChainData ? 'nodata'
        : amberReasons.length > 0 ? 'attention'
          : 'ok';
  const amber = dark ? '#fbbf24' : '#b45309';
  const overall = overallState === 'ok'
    ? { rgb: '34,197,94', shield: 'rgba(74,222,128,.5)', color: green, Icon: ShieldCheck, border: 'rgba(34,197,94,.28)', head: t('ovHeroOk'), sub: `${t('ovHeroOkSub')} · ${totalRaw} ${t('ovEventsSecured')}`, pulse: false }
    : overallState === 'attention'
      ? { rgb: '245,158,11', shield: 'rgba(251,191,36,.5)', color: amber, Icon: ShieldAlert, border: 'rgba(245,158,11,.30)', head: t('ovHeroAttention'), sub: amberReasons.join(' · '), pulse: false }
      : overallState === 'critical'
        ? { rgb: '239,68,68', shield: 'rgba(248,113,113,.5)', color: red, Icon: ShieldAlert, border: 'rgba(239,68,68,.34)', head: t('ovHeroCritical'), sub: criticalReasons.join(' · '), pulse: true }
        : { rgb: '150,150,150', shield: 'rgba(138,138,138,.3)', color: C.textMid, Icon: ShieldQuestion, border: C.border, head: t('ovHeroNoData'), sub: t('ovHeroNoDataSub'), pulse: false };
  const OverallIcon = overall.Icon;

  const strip: CSSProperties = { background: C.panel, border: `1px solid ${C.borderLt}`, borderRadius: 12 };
  const panelH: CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 };
  const panelT: CSSProperties = { fontSize: 12.5, fontWeight: 700, color: C.textStrong };

  return (
    <div className="flex-1 overflow-auto" style={{ background: C.bg, padding: '16px 18px 26px' }}>

      {/* ── Top layout (owner-decided 2026-07-22): ONE big protection panel left
           (hero shield + the eight modules as a status list with traffic-light
           dots: green ok, amber attention, red alert, grey not measured), and
           the audit-chain + skills panels STACKED in the right column. ── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))', gap: 12, alignItems: 'stretch' }}>
      <section style={{
        position: 'relative', overflow: 'hidden', minHeight: 300,
        border: `1px solid ${overall.border}`, borderRadius: 14, padding: '18px 20px',
        display: 'flex', alignItems: 'center', gap: 22, flexWrap: 'wrap',
        background: `linear-gradient(100deg, rgba(${overall.rgb},.15) 0%, rgba(${overall.rgb},.05) 34%, rgba(${overall.rgb},.01) 60%, transparent 100%)`,
      }}>
        {/* Big shield-in-disc motif filling the left, bleeding off the edge as a
            half-circle, with the gradient (above) running rightward. State-tinted. */}
        {/* Disc positioned BY ITS CENTRE (translate -50%,-50%); the shield is
            nested + flex-centred, so shield centre == disc centre always
            (owner: circle centre must equal shield centre). The soft radial
            fade IS the "mega blur" - no filter/blur (banned by the anti-leak
            rules). height 116% overflows top/bottom so the glow softly touches
            both panel edges. */}
        <div aria-hidden style={{ position: 'absolute', top: '50%', left: 90, height: '116%', aspectRatio: '1 / 1', transform: 'translate(-50%, -50%)', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 0, pointerEvents: 'none', background: `radial-gradient(circle, rgba(${overall.rgb},.22) 0%, rgba(${overall.rgb},.16) 30%, rgba(${overall.rgb},.07) 52%, rgba(${overall.rgb},.02) 72%, transparent 90%)` }}>
          <div style={{ color: overall.shield }}><OverallIcon size={210} strokeWidth={1.4} /></div>
        </div>
        <div style={{ position: 'relative', zIndex: 1, flex: '1 1 220px', minWidth: 200, paddingLeft: 160 }}>
          <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.16em', color: C.textDim, fontWeight: 600, marginBottom: 3 }}>{t('ovEyebrow')}</div>
          <div style={{ fontSize: 24, fontWeight: 700, lineHeight: 1.1, letterSpacing: '-0.01em', color: overallState === 'nodata' ? C.textMid : overall.color, ...(overall.pulse ? { animation: 'pulse 1.6s ease-in-out infinite' } : {}) }}>{overall.head}</div>
          <div style={{ fontSize: 12.5, color: C.textDim, marginTop: 6 }}>{overall.sub}</div>
        </div>
        {/* Module status list. Only the audit chain is wired; every other module
            stays grey (not measured) until its data point lands. */}
        <div style={{ position: 'relative', zIndex: 1, flex: '1 1 280px', minWidth: 260, display: 'flex', flexDirection: 'column' }}>
          {([
            {
              key: 'audit',
              name: t('ovCardAudit'),
              dot: heroState === 'broken' ? '#ef4444' : heroState === 'ok' ? '#22c55e' : C.textFaint,
              status: heroState === 'broken' ? t('ovAuditBroken') : heroState === 'ok' ? t('ovAuditOk') : noData,
              statusColor: heroState === 'broken' ? red : heroState === 'ok' ? green : C.textDim,
            },
            {
              key: 'sandbox',
              name: t('ovCardSandbox'),
              dot: sandbox ? (sandbox.state === 'warn' ? '#f59e0b' : '#22c55e') : C.textFaint,
              status: !sandbox ? noData
                : sandbox.state === 'warn' ? t('ovSandboxBlocked')
                  : sandbox.reason === 'container_running' ? t('ovSandboxEnforced')
                    : t('ovSandboxOnDemand'),
              statusColor: !sandbox ? C.textDim : sandbox.state === 'warn' ? amber : green,
            },
            {
              key: 'firewall',
              name: t('ovCardFirewall'),
              dot: !firewall ? C.textFaint : firewall.docker?.state === 'warn' ? '#f59e0b' : '#22c55e',
              status: !firewall ? noData
                : firewall.docker?.state === 'warn' ? t('ovFwDockerExposed')
                  : firewall.reason === 'lan_disabled' ? t('ovFwLanOff')
                    : t('ovFwActive', { n: fwDeflected }),
              statusColor: !firewall ? C.textDim : firewall.docker?.state === 'warn' ? amber : green,
            },
            {
              key: 'isolation',
              name: t('ovCardIsolation'),
              dot: isoState === 'ok' ? '#22c55e' : isoState === 'db_down' ? '#f59e0b' : C.textFaint,
              status: isoState === 'ok' ? t('ovIsoEnforced')
                : isoState === 'db_down' ? t('ovIsoDbDown')
                  : isoState === 'mem_off' ? t('ovIsoMemOff') : noData,
              statusColor: isoState === 'ok' ? green : isoState === 'db_down' ? amber : C.textDim,
            },
            {
              key: 'channels',
              name: t('ovCardChannels'),
              dot: !channels ? C.textFaint : channels.state === 'warn' ? '#f59e0b' : '#22c55e',
              status: !channels ? noData
                : channels.state === 'warn' ? t('ovChPermissive')
                  : t('ovChLocked', { n: channels.rejected_today }),
              statusColor: !channels ? C.textDim : channels.state === 'warn' ? amber : green,
            },
            {
              key: 'phishing',
              name: t('ovCardPhishing'),
              dot: mail === null || mail.length === 0 ? C.textFaint : '#22c55e',
              status: mail === null ? noData
                : mail.length === 0 ? t('ovPhNoMail')
                  : t('ovPhActive', { n: mailFlagged?.length ?? 0 }),
              statusColor: mail === null || mail.length === 0 ? C.textDim : green,
            },
            {
              key: 'guardrails',
              name: t('ovCardGuardrails'),
              dot: guardrails ? '#22c55e' : C.textFaint,
              status: !guardrails ? noData
                : guardrails.tools.total > 0 ? t('ovGrActive', { n: guardrails.tools.total })
                  : t('ovGrActiveNoCount'),
              statusColor: !guardrails ? C.textDim : green,
            },
          ]).map((m, mi, arr) => (
            <div
              key={m.key}
              role="button"
              tabIndex={0}
              onClick={() => setDetail(m.key)}
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') setDetail(m.key); }}
              className="group"
              style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '6px 6px', margin: '0 -6px', borderRadius: 7, cursor: 'pointer', borderBottom: mi < arr.length - 1 ? `1px solid ${C.borderFaint}` : 'none' }}
              onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.background = dark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.04)'; }}
              onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.background = 'transparent'; }}
            >
              <span style={{ width: 9, height: 9, borderRadius: '50%', background: m.dot, flexShrink: 0, ...(m.dot === '#ef4444' ? { animation: 'pulse 1.6s ease-in-out infinite' } : {}) }} />
              <span style={{ flex: 1, fontSize: 12, color: C.textStrong, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.name}</span>
              <span style={{ fontSize: 11, color: m.statusColor, textAlign: 'right', flexShrink: 0 }}>{m.status}</span>
              <ChevronRight size={12} style={{ color: C.textFaint, flexShrink: 0 }} />
            </div>
          ))}
        </div>
      </section>

      {/* ── Chain + skills panels, stacked right column ── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ ...strip, padding: 16 }}>
          <div style={panelH}>
            <span style={panelT}>{t('ovChainTitle')}</span>
            {isToday && hasChainData && (
              <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 10, color: C.textFaint }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', background: '#22c55e' }} />{t('ovLiveTag')}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 12 }}>
            <div style={{ width: 58, height: 58, borderRadius: '50%', flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: heroChrome.emblemBg, border: `1px solid ${heroChrome.emblemBorder}`, color: heroChrome.color }}>
              <HeroIcon size={28} strokeWidth={1.8} />
            </div>
            <div>
              <div style={{ fontSize: 14, fontWeight: 700, color: heroState === 'nodata' ? C.textMid : heroChrome.color }}>
                {heroState === 'ok' ? t('ovChainVerified') : heroState === 'broken' ? t('ovChainBroken') : noData}
              </div>
              {hasChainData && lastEv && (
                <div style={{ fontSize: 12, color: C.textMid, marginTop: 2 }}>
                  {totalRaw?.toLocaleString('de-DE')} {t('ovEventsSecured')} · {t('ovLast')}{' '}
                  <span style={{ fontFamily: 'monospace' }}>
                    {new Date(lastEv.ts).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                    {lastEv.tool ? ` · ${lastEv.tool}` : lastEv.type === 'thinking_run' ? ' · thinking' : ''}
                  </span>
                </div>
              )}
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 10, fontFamily: 'monospace', padding: '2px 8px', borderRadius: 6, background: C.bgRuler, border: `1px solid ${C.borderLt}`, color: C.textMid, marginTop: 6 }}>
                <Link2 size={10} />{t('ovChainAnchor')}
              </span>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 3, flexWrap: 'wrap', margin: '8px 0 12px' }} title={t('ovChainTailTitle')}>
            <span style={{ fontFamily: 'monospace', fontSize: 9, color: C.textMid, background: C.bgRuler, border: `1px solid ${C.border}`, borderRadius: 4, padding: '4px 6px' }}>GENESIS</span>
            <span style={{ color: C.textFaint, fontSize: 11 }}>→</span>
            <span style={{ color: C.textFaint, fontSize: 11, letterSpacing: 2 }}>…</span>
            {tailHashes.length > 0 ? tailHashes.map((ev, i) => {
              const isNewest = i === tailHashes.length - 1;
              const col = heroState === 'broken' ? red : green;
              return (
                <span key={ev.hash} style={{ display: 'contents' }}>
                  <span style={{ color: C.textFaint, fontSize: 11 }}>→</span>
                  <span style={{
                    fontFamily: 'monospace', fontSize: 9, borderRadius: 4, padding: '4px 6px',
                    color: heroState === 'broken' ? red : green,
                    background: heroState === 'broken' ? 'rgba(239,68,68,.08)' : 'rgba(34,197,94,.08)',
                    border: `1px solid ${heroState === 'broken' ? 'rgba(239,68,68,.3)' : 'rgba(34,197,94,.3)'}`,
                    ...(isNewest && isToday ? { animation: 'pulse 1.8s ease-in-out infinite' } : {}),
                  }}>
                    {(ev.hash ?? '').slice(0, 4)}{isNewest && isToday ? ` · ${t('ovNew')}` : ''}
                  </span>
                </span>
              );
            }) : [0, 1, 2].map(i => (
              <span key={i} style={{ display: 'contents' }}>
                <span style={{ color: C.textFaint, fontSize: 11 }}>→</span>
                <span style={{ fontFamily: 'monospace', fontSize: 9, color: C.textFaint, background: C.bgRuler, border: `1px dashed ${C.borderLt}`, borderRadius: 4, padding: '4px 6px' }}>????</span>
              </span>
            ))}
          </div>
          <div style={{ fontSize: 11.5, color: C.textDim, lineHeight: 1.5 }}>{t('ovChainExplain')}</div>
          <select
            value={date || today}
            onChange={e => onDateChange(e.target.value)}
            style={{ marginTop: 12, fontSize: 11, background: C.bgLabel, border: `1px solid ${C.border}`, borderRadius: 8, color: C.textMid, padding: '5px 9px', fontFamily: 'monospace' }}
          >
            {dates.length === 0 && <option value={today}>{today} ({t('today')})</option>}
            {dates.map(d => (
              <option key={d} value={d}>{d === today ? `${d} (${t('today')})` : d}</option>
            ))}
          </select>
        </div>

        <div style={{ ...strip, padding: 16 }}>
          <div style={panelH}>
            <span style={panelT}>{t('ovSkillsTitle')}</span>
            {skills && skills.quarantined_total > 0 && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 10.5, fontWeight: 700, color: dark ? '#f87171' : '#b91c1c', background: 'rgba(239,68,68,.12)', border: '1px solid rgba(239,68,68,.3)', borderRadius: 999, padding: '1px 8px' }}>
                <ShieldAlert size={11} />{t('ovSkIsolatedN', { n: skills.quarantined_total })}
              </span>
            )}
            {skills?.last_rescan?.ts && (
              <span style={{ marginLeft: 'auto', fontSize: 10, color: C.textFaint, fontFamily: 'monospace' }}>
                {t('ovSkLastScan')} {String(skills.last_rescan.ts).slice(5, 16).replace('T', ' ')}
              </span>
            )}
          </div>
          {!skills ? <OvNoData C={C} label={noData} /> : (
            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
              <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
                <svg width="118" height="118" viewBox="0 0 118 118" aria-hidden="true">
                  <circle cx="59" cy="59" r="42" fill="none" stroke={C.barTrack} strokeWidth="15" />
                  {skills.total > 0 && (() => {
                    const CIRC = 2 * Math.PI * 42;
                    const order: Array<[string, string]> = [['clean', '#22c55e'], ['low', '#3b82f6'], ['medium', '#f59e0b'], ['high', '#ef4444']];
                    let offset = 0;
                    return (
                      <g transform="rotate(-90 59 59)">
                        {order.map(([lvl, col]) => {
                          const frac = (skills.counts[lvl] ?? 0) / skills.total;
                          if (frac <= 0) return null;
                          const seg = (
                            <circle key={lvl} cx="59" cy="59" r="42" fill="none" stroke={col} strokeWidth="15"
                              strokeDasharray={`${frac * CIRC} ${CIRC}`} strokeDashoffset={-offset} />
                          );
                          offset += frac * CIRC;
                          return seg;
                        })}
                      </g>
                    );
                  })()}
                  <text x="59" y="55" textAnchor="middle" fill={skills.total > 0 ? C.textStrong : C.textFaint} fontFamily="monospace" fontSize="24" fontWeight="600">{skills.total}</text>
                  <text x="59" y="72" textAnchor="middle" fill={C.textDim} fontSize="10">{t('ovSkills')}</text>
                </svg>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 11, color: C.textMid }}>
                  {([['#22c55e', t('ovLvlClean'), 'clean'], ['#3b82f6', t('ovLvlLow'), 'low'], ['#f59e0b', t('ovLvlMedium'), 'medium'], ['#ef4444', t('ovLvlHigh'), 'high']] as const).map(([col, lbl, key]) => (
                    <span key={key} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ width: 9, height: 9, borderRadius: 2, background: col, opacity: 0.7 }} />{lbl} · {skills.counts[key] ?? 0}
                    </span>
                  ))}
                </div>
              </div>
              <div style={{ flex: '1 1 220px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <span style={{ fontFamily: 'monospace', fontSize: 26, fontWeight: 600, color: skills.blocked_today > 0 ? (dark ? '#f87171' : '#b91c1c') : C.textStrong }}>{skills.blocked_today}</span>
                  <span style={{ fontSize: 11, color: C.textDim }}>{t('ovSkBlockedToday')}</span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 11, color: C.textMid }}>
                  {skills.overrides_today > 0 && <span style={{ color: amber }}>{skills.overrides_today} {t('ovSkOverrides')}</span>}
                  {skills.alerts_today > 0 && <span style={{ color: amber }}>{skills.alerts_today} {t('ovSkAlerts')}</span>}
                </div>
                <div style={{ borderTop: `1px solid ${C.borderLt}` }}>
                  {skills.skills.slice(0, 6).map(s => (
                    <div
                      key={s.id}
                      role="button"
                      tabIndex={0}
                      onClick={() => setDetail(`skill:${s.id}`)}
                      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') setDetail(`skill:${s.id}`); }}
                      style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 6px', margin: '0 -6px', borderRadius: 6, cursor: 'pointer', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 11.5 }}
                      onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.background = dark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.04)'; }}
                      onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.background = 'transparent'; }}
                    >
                      <span style={{ flex: 1, fontFamily: 'monospace', color: C.textMid, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.id}</span>
                      {s.acknowledged && !s.quarantined && (
                        <span style={{ fontSize: 9.5, color: C.textDim }}>{t('ovSkAckShort')}</span>
                      )}
                      {s.quarantined && (
                        <span style={{ fontSize: 9.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em', padding: '1px 7px', borderRadius: 999, color: dark ? '#f87171' : '#b91c1c', background: 'rgba(239,68,68,.14)', border: '1px solid rgba(239,68,68,.35)', animation: 'pulse 1.8s ease-in-out infinite' }}>
                          {t('ovSkQuarantine')}
                        </span>
                      )}
                      <span style={{
                        fontSize: 9.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em', padding: '1px 7px', borderRadius: 999,
                        color: s.level === 'high' ? (dark ? '#f87171' : '#b91c1c') : s.level === 'medium' ? amber : s.level === 'low' ? '#60a5fa' : green,
                        background: s.level === 'high' ? 'rgba(239,68,68,.12)' : s.level === 'medium' ? 'rgba(245,158,11,.10)' : s.level === 'low' ? 'rgba(59,130,246,.12)' : 'rgba(34,197,94,.10)',
                      }}>
                        {s.level === 'clean' ? t('ovLvlClean') : s.level === 'low' ? t('ovLvlLow') : s.level === 'medium' ? t('ovLvlMedium') : t('ovLvlHigh')}
                      </span>
                      <ChevronRight size={12} style={{ color: C.textFaint, flexShrink: 0 }} />
                    </div>
                  ))}
                  {skills.skills.length > 6 && (
                    <div style={{ fontSize: 10.5, color: C.textFaint, padding: '4px 0 0' }}>{t('ovMore', { n: skills.skills.length - 6 })}</div>
                  )}
                  {skills.total === 0 && (
                    <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('ovSkNone')}</div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
      </div>

      {/* ── Background agent (admin view: per-user, cross-scope) ── */}
      <OvSectionHeading C={C} title={t('ovBgAgent')} hint={t('ovBgAgentHint')} />
      <div style={{ ...strip, padding: 16 }}>
        <div style={{ ...panelH, marginBottom: 14 }}>
          <span style={panelT}>{t('ovBgState')}</span>
          {thinking && !thinking.enabled && (
            <span style={{ fontSize: 9.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em', padding: '1px 7px', borderRadius: 999, color: C.textDim, background: C.bgRuler, border: `1px solid ${C.borderLt}` }}>{t('ovBgDisabled')}</span>
          )}
        </div>
        {(() => {
          if (!thinking) {
            return (
              <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                <Bot size={16} style={{ color: C.textDim, flexShrink: 0 }} />
                <span style={{ fontSize: 13, fontWeight: 600, color: C.textMid }}>{noData}</span>
              </div>
            );
          }
          const ago = (mins: number): string => {
            if (mins < 60) return t('ovAgoMin', { n: Math.max(1, Math.round(mins)) });
            if (mins < 48 * 60) return t('ovAgoH', { n: Math.round(mins / 60) });
            return t('ovAgoD', { n: Math.round(mins / 1440) });
          };
          // Users with any signal first (active/waiting/ran recently); cap the list.
          const users = thinking.users.slice(0, 6);
          const allAsked = thinking.users
            .flatMap(u => u.requests.map(r => ({ ...r, username: u.username })))
            .sort((a, b) => ((a.created_at ?? '') < (b.created_at ?? '') ? 1 : -1))
            .slice(0, 6);
          const stBadge = (status: string, reconfirm: boolean): { label: string; color: string; bg: string } => {
            if (reconfirm) return { label: t('ovBgStReconfirm'), color: amber, bg: 'rgba(245,158,11,.10)' };
            switch (status) {
              case 'asked': return { label: t('ovBgStAsked'), color: amber, bg: 'rgba(245,158,11,.10)' };
              case 'replied': return { label: t('ovBgStReplied'), color: '#60a5fa', bg: 'rgba(59,130,246,.12)' };
              case 'done': case 'confirmed': return { label: t('ovBgStDone'), color: green, bg: 'rgba(34,197,94,.10)' };
              case 'declined': return { label: t('ovBgStDeclined'), color: C.textDim, bg: C.bgRuler };
              default: return { label: status, color: C.textDim, bg: C.bgRuler };
            }
          };
          return (
            <div style={{ display: 'flex', gap: 22, flexWrap: 'wrap' }}>
              <div style={{ flex: '1 1 300px', minWidth: 0 }}>
                {users.length === 0 ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                    <Bot size={16} style={{ color: C.textDim, flexShrink: 0 }} />
                    <span style={{ fontSize: 13, fontWeight: 600, color: C.textMid }}>{t('ovBgNever')}</span>
                  </div>
                ) : users.map(u => (
                  <div key={u.scope || '__admin__'} style={{ padding: '6px 0', borderBottom: `1px solid ${C.borderFaint}` }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                      <span style={{
                        width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
                        background: u.running ? '#f59e0b' : u.waiting ? '#60a5fa' : u.minutes_since_last_run !== null ? '#22c55e' : C.textFaint,
                        animation: u.running || u.waiting ? 'pulse 1.8s ease-in-out infinite' : undefined,
                      }} />
                      <span style={{ fontWeight: 700, color: C.textStrong }}>{u.username}</span>
                      <span style={{ flex: 1, color: C.textDim, fontSize: 11.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {u.running ? t('ovBgRunning')
                          : u.waiting ? t('ovBgWaiting', { channel: u.waiting.channel })
                          : u.minutes_since_last_run !== null ? t('ovBgLastRun', { ago: ago(u.minutes_since_last_run) })
                          : t('ovBgNever')}
                      </span>
                      {u.waiting?.nudged && (
                        <span style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase', padding: '1px 6px', borderRadius: 999, color: '#60a5fa', background: 'rgba(59,130,246,.12)', flexShrink: 0 }}>{t('ovBgNudged')}</span>
                      )}
                    </div>
                    {u.waiting && (
                      <div style={{ background: C.bgAlt, borderLeft: '3px solid #60a5fa', borderRadius: '0 8px 8px 0', padding: '8px 11px', fontSize: 12, color: C.textMid, lineHeight: 1.45, marginTop: 6 }}>
                        {u.waiting.question}
                      </div>
                    )}
                    {!u.waiting && u.last_run && u.last_run.tools.length > 0 && (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 5, flexWrap: 'wrap', marginTop: 5, paddingLeft: 16 }}>
                        {u.last_run.tools.slice(0, 5).map(tool => (
                          <span key={tool} style={{ fontSize: 9.5, fontFamily: 'monospace', padding: '1px 6px', borderRadius: 5, color: C.textDim, background: C.bgRuler, border: `1px solid ${C.borderLt}` }}>{tool}</span>
                        ))}
                        {u.last_run.tools.length > 5 && <span style={{ fontSize: 9.5, color: C.textFaint }}>+{u.last_run.tools.length - 5}</span>}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <div style={{ flex: '1 1 260px', minWidth: 0 }}>
                <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.12em', color: C.textFaint, fontWeight: 600, marginBottom: 7 }}>{t('ovBgAsked')}</div>
                <div style={{ borderTop: `1px solid ${C.borderLt}`, paddingTop: 4 }}>
                  {allAsked.length === 0 ? (
                    <OvNoData C={C} label={noData} />
                  ) : allAsked.map(r => {
                    const b = stBadge(r.status, r.needs_reconfirm);
                    return (
                      <div key={r.id + r.created_at} style={{ padding: '5px 0', borderBottom: `1px solid ${C.borderFaint}` }}>
                        <div style={{ fontSize: 11.5, color: C.textMid, lineHeight: 1.4, overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>{r.question}</div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
                          <span style={{ fontSize: 9.5, fontWeight: 700, padding: '1px 7px', borderRadius: 999, color: b.color, background: b.bg }}>{b.label}</span>
                          <span style={{ fontSize: 10, color: C.textFaint }}>{r.username}</span>
                          <span style={{ flex: 1 }} />
                          <span style={{ fontSize: 9.5, fontFamily: 'monospace', color: C.textFaint }}>{(r.created_at ?? '').slice(5, 16).replace('T', ' ')}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          );
        })()}
      </div>

      {/* ── Recent supervised activity ──
          FE-only derivation from the already-loaded audit-chain events of the
          selected day (no extra request): the newest supervised actions, i.e.
          exactly what the hash chain vouches for. */}
      <OvSectionHeading C={C} title={t('ovActivityTitle')} hint={t('ovActivityHint')} />
      <div style={{ ...strip, padding: '12px 14px' }}>
        {(() => {
          const rows = [...events]
            .filter(e => e.tool || e.type === 'thinking_run' || e.type === 'subagent_start' || e.type === 'ww_train_start')
            .sort((a, b) => (a.ts < b.ts ? 1 : -1))
            .slice(0, 8);
          if (rows.length === 0) return <OvNoData C={C} label={noData} />;
          const kindLabel = (e: TimelineEvent): string =>
            e.tool ?? (e.type === 'thinking_run' ? t('ovActThinking') : e.type === 'subagent_start' ? t('ovActSubagent') : t('ovActTraining'));
          // Per-user attribution (admin view): scope uid8 -> username via the
          // isolation block, same mapping the audit breakdown uses.
          const userFor = (e: TimelineEvent): string | null => {
            const uid8 = (e.scope ?? '').slice(0, 8);
            if (!uid8) return null;
            return isolation?.scopes.find(s => s.scope === uid8)?.username ?? uid8;
          };
          return (
            <div>
              {rows.map((e, i) => (
                <div key={`${e.ts}-${i}`} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '5px 0', borderBottom: i < rows.length - 1 ? `1px solid ${C.borderFaint}` : 'none', fontSize: 11.5 }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: e.status === 'error' ? '#ef4444' : e.status === 'running' ? '#f59e0b' : '#22c55e' }} />
                  <span style={{ fontFamily: 'monospace', color: C.textStrong, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{kindLabel(e)}</span>
                  {!e.tool && (
                    <span style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', padding: '1px 6px', borderRadius: 999, color: '#a5b4fc', background: 'rgba(99,102,241,.12)', flexShrink: 0 }}>
                      {e.type === 'thinking_run' ? t('ovActThinkingBadge') : e.type === 'subagent_start' ? t('ovActSubagentBadge') : t('ovActTrainingBadge')}
                    </span>
                  )}
                  <span style={{ flex: 1 }} />
                  {userFor(e) && (
                    <span style={{ fontSize: 10, color: C.textDim, flexShrink: 0 }}>{userFor(e)}</span>
                  )}
                  {typeof e.duration_s === 'number' && e.duration_s > 0 && (
                    <span style={{ fontSize: 10, fontFamily: 'monospace', color: C.textFaint, flexShrink: 0 }}>{e.duration_s >= 10 ? Math.round(e.duration_s) : e.duration_s.toFixed(1)}s</span>
                  )}
                  <span style={{ fontSize: 10, fontFamily: 'monospace', color: C.textDim, flexShrink: 0 }}>{e.ts.slice(11, 19)}</span>
                </div>
              ))}
            </div>
          );
        })()}
      </div>

      {/* ── Supervised units (admin: all sessions, attributed per user) ── */}
      <OvSectionHeading C={C} title={t('ovUnitsTitle')} hint={t('ovUnitsHint')} />
      <div style={{ ...strip, padding: '12px 14px' }}>
        {!supervisor ? (
          <OvNoData C={C} label={noData} />
        ) : supervisor.units.length === 0 ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textDim }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#22c55e', flexShrink: 0 }} />
            {t('ovUnitsNone')}
          </div>
        ) : (
          supervisor.units.map((u, i) => (
            <div key={u.task_id} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '6px 0', borderBottom: i < supervisor.units.length - 1 ? `1px solid ${C.borderFaint}` : 'none', fontSize: 11.5 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: u.stale ? '#ef4444' : '#f59e0b', animation: 'pulse 1.8s ease-in-out infinite' }} />
              <span style={{ fontFamily: 'monospace', fontWeight: 700, color: C.textStrong, flexShrink: 0 }}>{u.agent_type}</span>
              {u.username && <span style={{ fontSize: 10, color: C.textDim, flexShrink: 0 }}>{u.username}</span>}
              <span style={{ flex: 1, color: C.textDim, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{u.task}</span>
              {u.stale && (
                <span style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase', padding: '1px 6px', borderRadius: 999, color: dark ? '#f87171' : '#b91c1c', background: 'rgba(239,68,68,.12)', flexShrink: 0 }}>{t('ovUnitsStale')}</span>
              )}
              {typeof u.runtime_s === 'number' && (
                <span style={{ fontSize: 10, fontFamily: 'monospace', color: C.textFaint, flexShrink: 0 }}>{u.runtime_s >= 90 ? `${Math.round(u.runtime_s / 60)}m` : `${Math.round(u.runtime_s)}s`}</span>
              )}
            </div>
          ))
        )}
      </div>

      {/* Security posture section removed by owner decision 2026-07-23: a
          misconfiguration must surface as a warning in the module list above
          (single source of attention), not as a second static checklist. */}

      {/* ── Module detail popup (centered, click-outside closes) ── */}
      {detail !== null && (() => {
        const titles: Record<string, string> = {
          audit: t('ovCardAudit'), sandbox: t('ovCardSandbox'), firewall: t('ovCardFirewall'),
          isolation: t('ovCardIsolation'), channels: t('ovCardChannels'),
          phishing: t('ovCardPhishing'), guardrails: t('ovCardGuardrails'),
        };
        const descs: Record<string, string> = {
          audit: t('ovDescAudit'), sandbox: t('ovDescSandbox'), firewall: t('ovDescFirewall'),
          isolation: t('ovDescIsolation'), channels: t('ovDescChannels'),
          phishing: t('ovDescPhishing'), guardrails: t('ovDescGuardrails'),
        };
        const factRow = (label: string, value: ReactNode, okDot?: boolean | null) => (
          <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 12 }}>
            {okDot !== undefined && (
              <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: okDot === null ? C.textFaint : okDot ? '#22c55e' : '#f59e0b' }} />
            )}
            <span style={{ flex: 1, color: C.textMid }}>{label}</span>
            <span style={{ color: C.textStrong, fontFamily: 'monospace', fontSize: 11 }}>{value}</span>
          </div>
        );
        return (
          <div
            style={{ position: 'fixed', inset: 0, zIndex: 70, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20 }}
            onClick={() => setDetail(null)}
          >
            <div
              onClick={e => e.stopPropagation()}
              style={{ background: C.panel, border: `1px solid ${C.border}`, borderRadius: 14, width: 'min(560px, 94vw)', maxHeight: '82vh', display: 'flex', flexDirection: 'column', overflow: 'hidden', boxShadow: '0 24px 60px rgba(0,0,0,.5)' }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '13px 16px', borderBottom: `1px solid ${C.borderLt}` }}>
                <span style={{ fontSize: 13.5, fontWeight: 700, color: C.textStrong, flex: 1, fontFamily: detailSkillId ? 'monospace' : undefined }}>{detailSkillId ?? titles[detail] ?? detail}</span>
                <button type="button" onClick={() => setDetail(null)} title={t('close')}
                  style={{ background: 'transparent', border: 'none', color: C.textDim, cursor: 'pointer', padding: 4, display: 'flex' }}>
                  <X size={16} />
                </button>
              </div>
              <div style={{ padding: '14px 16px', overflow: 'auto' }}>
                <div style={{ fontSize: 11.5, color: C.textDim, lineHeight: 1.5, marginBottom: 12 }}>{detailSkillId ? t('ovSkDetailDesc') : (descs[detail] ?? '')}</div>

                {/* ── Skill detail: WHY it was flagged + resolution actions ── */}
                {detailSkillId && (() => {
                  const sc = skillScan;
                  const level = sc?.level ?? skills?.skills.find(s => s.id === detailSkillId)?.level ?? 'clean';
                  const catLabel = (c: string): string => {
                    switch (c) {
                      case 'prompt_injection': return t('ovSkCatInjection');
                      case 'data_exfiltration': return t('ovSkCatExfil');
                      case 'remote_code_exec': return t('ovSkCatRce');
                      case 'credential_access': return t('ovSkCatCreds');
                      case 'hardcoded_secret': return t('ovSkCatSecret');
                      case 'obfuscation': return t('ovSkCatObfus');
                      case 'dangerous_code': return t('ovSkCatDangerous');
                      case 'destructive': return t('ovSkCatDestructive');
                      case 'network': return t('ovSkCatNetwork');
                      case 'covert_action': return t('ovSkCatCovert');
                      case 'system_prompt_leak': return t('ovSkCatPromptLeak');
                      default: return c;
                    }
                  };
                  const canDelete = level !== 'clean';
                  const canAck = level === 'medium' && !sc?.acknowledged;
                  const canRestore = !!sc?.quarantined;
                  const canIsolate = level === 'high' && !sc?.quarantined;
                  const twoFaAction = qPending === 'acknowledge' || qPending === 'restore';
                  return (
                    <div>
                      {factRow(t('ovSkLevel'), (level === 'clean' ? t('ovLvlClean') : level === 'low' ? t('ovLvlLow') : level === 'medium' ? t('ovLvlMedium') : t('ovLvlHigh')), level === 'high' ? false : level === 'clean' ? true : null)}
                      {sc && factRow(t('ovSkScore'), String(sc.score))}
                      {sc?.quarantined && factRow(t('ovSkState'), t('ovSkQuarantine'), false)}
                      {sc?.acknowledged && factRow(t('ovSkState'), t('ovSkAckShort'), true)}

                      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovSkWhy')}</div>
                      {sc === null ? (
                        <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('loading')}</div>
                      ) : sc.findings.length === 0 ? (
                        <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('ovSkNoFindings')}</div>
                      ) : (
                        sc.findings.map((f, i) => (
                          <div key={i} style={{ padding: '7px 0', borderBottom: `1px solid ${C.borderFaint}` }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                              <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: f.severity === 'high' ? '#ef4444' : f.severity === 'medium' ? '#f59e0b' : '#60a5fa' }} />
                              <span style={{ flex: 1, fontSize: 12, color: C.textStrong }}>{f.message}</span>
                              <span style={{ fontSize: 9.5, color: C.textFaint, fontFamily: 'monospace' }}>{f.file}:{f.line}</span>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3, paddingLeft: 16 }}>
                              <span style={{ fontSize: 9.5, padding: '1px 6px', borderRadius: 5, color: C.textMid, background: C.bgRuler, border: `1px solid ${C.borderLt}` }}>{catLabel(f.category)}</span>
                              {f.snippet && <span style={{ fontSize: 10, fontFamily: 'monospace', color: C.textDim, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.snippet}</span>}
                            </div>
                          </div>
                        ))
                      )}

                      {/* actions */}
                      {(canDelete || canAck || canRestore || canIsolate) && (
                        <div style={{ marginTop: 14, borderTop: `1px solid ${C.borderLt}`, paddingTop: 12 }}>
                          {twoFaAction ? (
                            <div>
                              <div style={{ fontSize: 11.5, color: C.textMid, marginBottom: 6 }}>
                                {qPending === 'acknowledge' ? t('ovSkAckPrompt') : t('ovSkRestorePrompt')}
                              </div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                                <input value={qCode} onChange={e => setQCode(e.target.value)} placeholder={t('ovSkCodePlaceholder')} inputMode="numeric" maxLength={8}
                                  style={{ width: 120, fontSize: 12, fontFamily: 'monospace', padding: '5px 9px', borderRadius: 6, border: `1px solid ${C.border}`, background: C.bgLabel, color: C.textStrong, outline: 'none' }} />
                                <button type="button" disabled={qBusy || qCode.trim().length < 6} onClick={() => runSkillAction(detailSkillId, qPending!)}
                                  style={{ fontSize: 11.5, fontWeight: 600, padding: '5px 12px', borderRadius: 6, border: `1px solid ${qPending === 'restore' ? 'rgba(34,197,94,.35)' : 'rgba(245,158,11,.35)'}`, background: qPending === 'restore' ? 'rgba(34,197,94,.10)' : 'rgba(245,158,11,.10)', color: qPending === 'restore' ? green : amber, cursor: 'pointer', opacity: qBusy || qCode.trim().length < 6 ? 0.5 : 1 }}>
                                  {t('ovSkConfirm2fa')}
                                </button>
                                <button type="button" disabled={qBusy} onClick={() => { setQPending(null); setQCode(''); setQError(null); }}
                                  style={{ fontSize: 11.5, padding: '5px 12px', borderRadius: 6, border: `1px solid ${C.border}`, background: C.bgLabel, color: C.textMid, cursor: 'pointer' }}>
                                  {t('ovSkCancel')}
                                </button>
                              </div>
                            </div>
                          ) : (
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                              {canDelete && (
                                <button type="button" disabled={qBusy} onClick={() => runSkillAction(detailSkillId, 'delete')}
                                  style={{ fontSize: 11.5, fontWeight: 600, padding: '5px 12px', borderRadius: 6, border: '1px solid rgba(239,68,68,.35)', background: 'rgba(239,68,68,.08)', color: dark ? '#f87171' : '#b91c1c', cursor: 'pointer' }}>
                                  {t('ovSkDelete')}
                                </button>
                              )}
                              {canAck && (
                                <button type="button" onClick={() => { setQPending('acknowledge'); setQCode(''); setQError(null); }}
                                  style={{ fontSize: 11.5, fontWeight: 600, padding: '5px 12px', borderRadius: 6, border: `1px solid ${C.border}`, background: 'transparent', color: C.textStrong, cursor: 'pointer' }}>
                                  {t('ovSkAcknowledge')}
                                </button>
                              )}
                              {canRestore && (
                                <button type="button" onClick={() => { setQPending('restore'); setQCode(''); setQError(null); }}
                                  style={{ fontSize: 11.5, fontWeight: 600, padding: '5px 12px', borderRadius: 6, border: `1px solid ${C.border}`, background: 'transparent', color: C.textMid, cursor: 'pointer' }}>
                                  {t('ovSkFalsePositive')}
                                </button>
                              )}
                              {canIsolate && (
                                <button type="button" disabled={qBusy} onClick={() => runSkillAction(detailSkillId, 'isolate')}
                                  style={{ fontSize: 11.5, fontWeight: 600, padding: '5px 12px', borderRadius: 6, border: `1px solid ${C.border}`, background: 'transparent', color: amber, cursor: 'pointer' }}>
                                  {t('ovSkIsolate')}
                                </button>
                              )}
                            </div>
                          )}
                          {qError && <div style={{ fontSize: 11, color: dark ? '#f87171' : '#b91c1c', marginTop: 8 }}>{qError}</div>}
                        </div>
                      )}
                    </div>
                  );
                })()}

                {detail === 'audit' && (() => {
                  // Per-user breakdown of the day's chained events. The timeline is ONE
                  // shared chain for ALL users - tampering with any user's events breaks
                  // chain_ok for the admin. This section makes that coverage visible.
                  const byScope = new Map<string, number>();
                  for (const ev of events) {
                    const key = (ev.scope ?? '').slice(0, 8) || '__local__';
                    byScope.set(key, (byScope.get(key) ?? 0) + 1);
                  }
                  const nameFor = (uid8: string): string => {
                    const hit = isolation?.scopes.find(s => s.scope === uid8);
                    return hit?.username ?? uid8;
                  };
                  const rows = [...byScope.entries()].sort((a, b) => b[1] - a[1]);
                  return (
                    <div>
                      {factRow(t('ovChainTitle'), heroState === 'broken' ? t('ovChainBroken') : heroState === 'ok' ? t('ovChainVerified') : noData, heroState === 'ok' ? true : heroState === 'broken' ? false : null)}
                      {factRow(t('ovEventsSecured'), hasChainData ? String(totalRaw) : '–')}
                      {lastEv && factRow(t('ovLast'), `${new Date(lastEv.ts).toLocaleTimeString('de-DE')} · ${lastEv.tool ?? lastEv.type}`)}
                      {factRow('GENESIS', t('ovChainAnchor'), true)}
                      {rows.length > 0 && (
                        <>
                          <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovAuditPerUser')}</div>
                          {rows.map(([uid8, count]) => (
                            <div key={uid8} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 4px 16px', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 11 }}>
                              <span style={{ flex: 1, color: uid8 === '__local__' ? C.textDim : C.textStrong, fontWeight: uid8 === '__local__' ? 400 : 600 }}>
                                {uid8 === '__local__' ? t('ovAuditNoScope') : nameFor(uid8)}
                              </span>
                              <span style={{ color: C.textMid, fontFamily: 'monospace', fontSize: 10.5 }}>{count} {t('ovAuditEventsUnit')}</span>
                            </div>
                          ))}
                          <div style={{ fontSize: 10.5, color: C.textFaint, lineHeight: 1.5, marginTop: 8 }}>{t('ovAuditSharedChain')}</div>
                        </>
                      )}
                    </div>
                  );
                })()}

                {detail === 'sandbox' && (
                  sandbox ? (
                    <div>
                      {factRow(t('ovCardSandbox'), sandbox.state === 'warn' ? t('ovSandboxBlocked') : sandbox.reason === 'container_running' ? t('ovSandboxEnforced') : t('ovSandboxOnDemand'), sandbox.state !== 'warn')}
                      {sandbox.hardening && (
                        <>
                          <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovHardening')}</div>
                          {factRow(t('ovCapDrop'), sandbox.hardening.cap_drop_all ? 'ALL' : '–', sandbox.hardening.cap_drop_all)}
                          {factRow('no-new-privileges', sandbox.hardening.no_new_privileges ? t('ovOn') : t('ovOff'), sandbox.hardening.no_new_privileges)}
                          {factRow(t('ovRamLimit'), sandbox.hardening.memory_bytes ? `${Math.round(sandbox.hardening.memory_bytes / 1048576)} MB` : '–')}
                          {factRow(t('ovCpuLimit'), sandbox.hardening.nano_cpus ? `${(sandbox.hardening.nano_cpus / 1e9).toFixed(1)} CPU` : '–')}
                          {factRow(t('ovIsolatedNet'), sandbox.hardening.networks.join(', ') || '–', sandbox.hardening.isolated_network)}
                        </>
                      )}
                    </div>
                  ) : <OvNoData C={C} label={noData} />
                )}

                {detail === 'firewall' && (
                  firewall ? (
                    <div>
                      {factRow(t('ovFwLan'), firewall.lan_enabled ? t('ovOn') : t('ovOff'), true)}
                      {factRow(t('ovFwOsRules'), firewall.os_rules_enabled ? t('ovOn') : t('ovOff'), firewall.os_rules_enabled)}
                      {factRow(t('ovFwBlockedCount'), String(firewall.blocked_today))}
                      {factRow(t('ovFwFailedLogins'), String(firewall.failed_logins_today))}
                      {firewall.docker && (
                        <>
                          <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovFwDocker')}</div>
                          {firewall.docker.sandbox_off_internal !== null &&
                            factRow(t('ovFwSandboxIso'), firewall.docker.sandbox_off_internal ? t('ovOn') : t('ovOff'), firewall.docker.sandbox_off_internal)}
                          {firewall.docker.containers.map(c => {
                            const portsLabel = c.ports.length === 0
                              ? t('ovFwNoPorts')
                              : c.ports.map(p => `${p.host_ip || '0.0.0.0'}:${p.host_port}`).join(', ');
                            return (
                              <div key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 4px 16px', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 11, opacity: c.running ? 1 : 0.55 }}>
                                <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: !c.running ? C.textFaint : c.lan_exposed ? '#f59e0b' : '#22c55e' }} />
                                <span style={{ flex: 1, color: C.textStrong, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                  {c.name}
                                  <span style={{ color: C.textFaint, fontSize: 10, marginLeft: 6 }}>{c.networks.join(', ')}</span>
                                </span>
                                <span style={{ color: c.lan_exposed ? amber : C.textMid, fontFamily: 'monospace', fontSize: 10 }}>{portsLabel}</span>
                              </div>
                            );
                          })}
                        </>
                      )}
                      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovFwEventsTitle')}</div>
                      {secEvents === null ? (
                        <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('loading')}</div>
                      ) : secEvents.length === 0 ? (
                        <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('ovNoEventsToday')}</div>
                      ) : (
                        [...secEvents].reverse().map((ev, i) => (
                          <div key={`${ev.ts}-${i}`} style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '5px 0', borderBottom: `1px solid ${C.borderFaint}` }}>
                            <span style={{ fontFamily: 'monospace', fontSize: 10, color: C.textFaint, flexShrink: 0 }}>{(ev.ts || '').slice(11, 19)}</span>
                            <span style={{ fontSize: 11.5, color: C.textStrong, fontWeight: 600, flexShrink: 0 }}>{evKindLabel(ev.kind)}</span>
                            <span style={{ fontFamily: 'monospace', fontSize: 10.5, color: C.textMid, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {[ev.ip, ev.username, ev.path, ev.detail].filter(Boolean).join(' · ')}
                            </span>
                          </div>
                        ))
                      )}
                      <button
                        type="button"
                        disabled={!securityLogFile}
                        onClick={() => { if (securityLogFile) { setDetail(null); onOpenLogFile(securityLogFile); } }}
                        title={securityLogFile ? undefined : t('ovNoEventsToday')}
                        style={{ marginTop: 14, fontSize: 11.5, fontWeight: 600, padding: '7px 12px', borderRadius: 8, border: `1px solid ${C.border}`, background: C.bgLabel, color: securityLogFile ? C.textStrong : C.textFaint, cursor: securityLogFile ? 'pointer' : 'not-allowed', display: 'inline-flex', alignItems: 'center', gap: 6 }}
                      >
                        <GitBranch size={12} />{t('ovShowLogHistory')}
                      </button>
                    </div>
                  ) : <OvNoData C={C} label={noData} />
                )}

                {detail === 'isolation' && (
                  memHealth ? (
                    <div>
                      {factRow(t('ovIsoScopeFilter'), t('ovOn'), true)}
                      {firewall && factRow(t('ovIsoMode'), firewall.lan_enabled ? t('ovIsoModeServer') : t('ovIsoModeSingle'), true)}
                      {factRow(t('ovIsoDb'), memHealth.db_connected ? t('ovOn') : t('ovOff'), memHealth.db_connected)}
                      {isoState === 'mem_off' && factRow(t('ovCardIsolation'), t('ovIsoMemOff'), null)}
                      {isolation && (
                        <>
                          <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovIsoAdmin')}</div>
                          {factRow(t('ovIsoRagLatency'), isolation.rag_probe_ms !== null ? `${isolation.rag_probe_ms} ms` : t('ovIsoNoChunks'), isolation.rag_probe_ms === null ? null : isolation.rag_probe_ms < 150)}
                          {factRow(t('ovIsoDbSize'), fmtBytes(isolation.db_size_bytes))}
                          {factRow(t('ovIsoScopeCount'), String(isolation.scope_count))}
                          {isolation.scopes.slice(0, 8).map(s => (
                            <div key={s.scope} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 4px 16px', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 11 }}>
                              <span style={{ flex: 1, color: C.textStrong, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {s.username ?? s.scope}
                                {s.username && <span style={{ color: C.textFaint, fontFamily: 'monospace', fontWeight: 400, fontSize: 10, marginLeft: 6 }}>{s.scope}</span>}
                              </span>
                              <span style={{ color: C.textMid, fontFamily: 'monospace', fontSize: 10.5 }}>{s.memories} / {s.chunks} {t('ovIsoEntriesChunks')}</span>
                            </div>
                          ))}
                          {isolation.scopes.length > 8 && (
                            <div style={{ fontSize: 10.5, color: C.textFaint, padding: '4px 0 0 16px' }}>{t('ovMore', { n: isolation.scopes.length - 8 })}</div>
                          )}
                          {factRow(t('ovIsoWorkspaces'), `${isolation.workspace_count} · ${fmtBytes(isolation.total_size_bytes)}${isolation.truncated ? '+' : ''}`)}
                          {(isolation.user_folders ?? []).slice(0, 8).map(u => (
                            <div key={u.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 4px 16px', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 11 }}>
                              <span style={{ flex: 1, color: u.name === '__unassigned__' ? C.textDim : C.textStrong, fontWeight: u.name === '__unassigned__' ? 400 : 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {u.name === '__unassigned__' ? t('ovIsoUnassigned') : u.name}
                              </span>
                              <span style={{ color: C.textMid, fontFamily: 'monospace', fontSize: 10.5 }}>
                                {u.folders} {t('ovIsoFoldersUnit')} · {fmtBytes(u.size_bytes)}{u.truncated ? '+' : ''}
                              </span>
                            </div>
                          ))}
                          {(isolation.user_folders ?? []).length > 8 && (
                            <div style={{ fontSize: 10.5, color: C.textFaint, padding: '4px 0 0 16px' }}>{t('ovMore', { n: (isolation.user_folders ?? []).length - 8 })}</div>
                          )}
                        </>
                      )}
                    </div>
                  ) : <OvNoData C={C} label={noData} />
                )}

                {detail === 'channels' && (
                  channels ? (
                    <div>
                      {channels.channels.map(ch => {
                        const label = ch.name === 'telegram' ? 'Telegram' : ch.name === 'whatsapp' ? 'WhatsApp' : ch.name === 'discord' ? 'Discord' : ch.name;
                        const parts = ch.enabled
                          ? [
                              ch.mode,
                              `${ch.paired} ${t('ovChPaired')}`,
                              ch.last_ts ? `${t('ovChLastUsed')} ${new Date(ch.last_ts * 1000).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}` : null,
                              ch.rejected_today > 0 ? `${ch.rejected_today} ${t('ovChRejectedUnit')}` : null,
                            ].filter(Boolean).join(' · ')
                          : t('ovOff');
                        const okDot = !ch.enabled ? null : ch.mode === 'permissive' ? false : true;
                        return factRow(label, parts, okDot);
                      })}
                      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovChRejectedSenders')}</div>
                      {secEvents === null ? (
                        <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('loading')}</div>
                      ) : (() => {
                        const chEvents = secEvents.filter(ev => ev.kind === 'channel_rejected');
                        return chEvents.length === 0 ? (
                          <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('ovChNoRejected')}</div>
                        ) : (
                          [...chEvents].reverse().map((ev, i) => (
                            <div key={`${ev.ts}-${i}`} style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '5px 0', borderBottom: `1px solid ${C.borderFaint}` }}>
                              <span style={{ fontFamily: 'monospace', fontSize: 10, color: C.textFaint, flexShrink: 0 }}>{(ev.ts || '').slice(11, 19)}</span>
                              <span style={{ fontSize: 11.5, color: C.textStrong, fontWeight: 600, flexShrink: 0 }}>{ev.channel ?? '?'}</span>
                              <span style={{ fontFamily: 'monospace', fontSize: 10.5, color: C.textMid, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {[ev.username, ev.detail].filter(Boolean).join(' · ')}
                              </span>
                            </div>
                          ))
                        );
                      })()}
                      <button
                        type="button"
                        disabled={!securityLogFile}
                        onClick={() => { if (securityLogFile) { setDetail(null); onOpenLogFile(securityLogFile); } }}
                        style={{ marginTop: 14, fontSize: 11.5, fontWeight: 600, padding: '7px 12px', borderRadius: 8, border: `1px solid ${C.border}`, background: C.bgLabel, color: securityLogFile ? C.textStrong : C.textFaint, cursor: securityLogFile ? 'pointer' : 'not-allowed', display: 'inline-flex', alignItems: 'center', gap: 6 }}
                      >
                        <GitBranch size={12} />{t('ovShowLogHistory')}
                      </button>
                    </div>
                  ) : <OvNoData C={C} label={noData} />
                )}

                {detail === 'phishing' && (
                  mail === null ? <OvNoData C={C} label={noData} /> : (
                    <div>
                      {factRow(t('ovPhScanned'), String(mail.length))}
                      {factRow(t('ovPhFlagged'), String(mailFlagged?.length ?? 0), (mailFlagged?.length ?? 0) >= 0 ? true : null)}
                      <div style={{ fontSize: 10.5, color: C.textFaint, lineHeight: 1.5, margin: '8px 0' }}>{t('ovPhHiddenNote')}</div>
                      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovPhFlaggedTitle')}</div>
                      {(mailFlagged?.length ?? 0) === 0 ? (
                        <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('ovPhNone')}</div>
                      ) : (
                        (mailFlagged ?? []).map((m, i) => (
                          <div key={`${m.date}-${i}`} style={{ padding: '7px 0', borderBottom: `1px solid ${C.borderFaint}` }}>
                            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                              <span style={{ fontFamily: 'monospace', fontSize: 10, color: C.textFaint, flexShrink: 0 }}>
                                {(m.message_date_iso || m.date || '').slice(0, 16).replace('T', ' ')}
                              </span>
                              <span style={{ fontSize: 11, color: C.textMid, fontFamily: 'monospace', flexShrink: 0, maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.from}</span>
                              <span style={{ fontSize: 11.5, color: C.textStrong, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.subject || '(–)'}</span>
                            </div>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4, paddingLeft: 2 }}>
                              {(m.suspicious_reasons ?? []).map(rc => (
                                <span key={rc} style={{ fontSize: 9.5, padding: '1px 7px', borderRadius: 6, color: dark ? '#fbbf24' : '#b45309', background: 'rgba(245,158,11,.10)', border: '1px solid rgba(245,158,11,.25)' }}>
                                  {phReason(rc)}
                                </span>
                              ))}
                              {typeof m.suspicious_score === 'number' && (
                                <span style={{ fontSize: 9.5, padding: '1px 7px', borderRadius: 6, color: C.textDim, background: C.bgRuler, border: `1px solid ${C.borderLt}` }}>
                                  Score {m.suspicious_score}
                                </span>
                              )}
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  )
                )}

                {detail === 'guardrails' && (
                  guardrails ? (
                    <div>
                      {factRow(t('ovGatePlan'), guardrails.plan_gate ? t('ovOn') : t('ovOff'), guardrails.plan_gate)}
                      {factRow(t('ovGateConfirm'), t('ovOn'), true)}
                      {factRow(t('ovGrProactive'), guardrails.proactive_reply_gate ? t('ovOn') : t('ovOff'), guardrails.proactive_reply_gate)}
                      {factRow(t('ovGrAskFirst'), guardrails.ask_first_drain_gate ? t('ovOn') : t('ovOff'), guardrails.ask_first_drain_gate)}
                      {factRow(t('ovGrChannelRestr'), guardrails.channel_tools_unrestricted ? t('ovGrLoosened') : t('ovGrStrict'), !guardrails.channel_tools_unrestricted)}
                      {guardrails.tools.total > 0 && (
                        <>
                          <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovGrInventory')}</div>
                          {factRow(t('ovGrTotal'), String(guardrails.tools.total))}
                          {factRow('read', String(guardrails.tools.read))}
                          {factRow('write', String(guardrails.tools.write))}
                          {factRow('dangerous', String(guardrails.tools.dangerous))}
                          {factRow('system', String(guardrails.tools.system))}
                          {factRow(t('ovGrAdminOnly'), String(guardrails.tools.admin_only))}
                          {factRow(t('ovGrChannelBlocked'), String(guardrails.tools.channel_restricted))}
                        </>
                      )}
                      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', color: C.textFaint, fontWeight: 600, margin: '12px 0 4px' }}>{t('ovGrStanding')}</div>
                      {guardrails.trust.trusted_dirs.length === 0 && guardrails.trust.allow_always_tools.length === 0 ? (
                        <div style={{ fontSize: 11, color: C.textDim, padding: '6px 0' }}>{t('ovGrNoStanding')}</div>
                      ) : (
                        <>
                          {guardrails.trust.allow_always_tools.map(tool => (
                            <div key={`t-${tool}`} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 4px 16px', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 11 }}>
                              <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#f59e0b', flexShrink: 0 }} />
                              <span style={{ flex: 1, color: C.textStrong, fontFamily: 'monospace' }}>{tool}</span>
                              <span style={{ color: amber, fontSize: 10.5 }}>{t('ovGrAllowAlways')}</span>
                            </div>
                          ))}
                          {guardrails.trust.trusted_dirs.map(d => (
                            <div key={`d-${d}`} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 4px 16px', borderBottom: `1px solid ${C.borderFaint}`, fontSize: 11 }}>
                              <span style={{ width: 8, height: 8, borderRadius: '50%', background: C.textFaint, flexShrink: 0 }} />
                              <span style={{ flex: 1, color: C.textMid, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d}</span>
                              <span style={{ color: C.textDim, fontSize: 10.5 }}>{t('ovGrTrustedDir')}</span>
                            </div>
                          ))}
                        </>
                      )}
                      <div style={{ fontSize: 10.5, color: C.textFaint, lineHeight: 1.5, marginTop: 8 }}>{t('ovGrStandingNote')}</div>
                    </div>
                  ) : <OvNoData C={C} label={noData} />
                )}
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}

// ─── Main component ────────────────────────────────────────────────────────────

export default function NotificationsModal({
  isOpen,
  onClose,
  notifications,
  onFetchComplete,
  userTimeFormat,
  onSecuritySeen,
}: NotificationsModalProps) {
  const t = useTranslations('notifications');

  // 'overview' = protection dashboard, 'timeline' = horizontal, 'tooluse' = vertical
  // tool list, 'activity', or filename
  const [selectedSource, setSelectedSource] = useState<string>('overview');
  const [logsExpanded, setLogsExpanded]     = useState(false);

  // Log files
  const [logFiles, setLogFiles]             = useState<LogFile[]>([]);
  const [loadingFiles, setLoadingFiles]     = useState(false);
  const [logLines, setLogLines]             = useState<string[]>([]);
  const [totalLines, setTotalLines]         = useState(0);
  const [loadingContent, setLoadingContent] = useState(false);

  // Timeline (shared by both horizontal + vertical views)
  const [timelineEvents, setTimelineEvents]   = useState<TimelineEvent[]>([]);
  const [timelineDates, setTimelineDates]     = useState<string[]>([]);
  const [timelineDate, setTimelineDate]       = useState<string>('');
  const [timelineChainOk, setTimelineChainOk] = useState<boolean | null>(null);
  const [timelineTotalRaw, setTimelineTotalRaw] = useState<number | null>(null);
  // Aggregated protection-module status (GET /api/security/overview, admin-gated).
  // null = not fetched / unavailable (403, backend down) -> rows stay grey.
  const [securityOverview, setSecurityOverview] = useState<SecurityOverview | null>(null);
  const [thinkingStatus, setThinkingStatus] = useState<ThinkingStatus | null>(null);
  const [supervisorStatus, setSupervisorStatus] = useState<SupervisorStatus | null>(null);
  // User-isolation health input: the EXISTING endpoint is reused on purpose -
  // isolation-critical logic must never be reimplemented in a second place.
  // (Admin aggregate metrics come from /api/security/overview's isolation block.)
  const [memoryHealth, setMemoryHealth] = useState<MemoryHealth | null>(null);
  // Phishing-shield input: the synced-mail endpoint already carries the
  // suspicious_* annotations per message (local store read, no IMAP roundtrip).
  const [mailMessages, setMailMessages] = useState<MailMessage[] | null>(null);
  const [loadingTimeline, setLoadingTimeline] = useState(false);
  // mobile: the left nav is an off-canvas drawer opened from a hamburger in the header;
  // picking a section closes it again.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  useEffect(() => { setSidebarOpen(false); }, [selectedSource]);
  const [expandedCallId, setExpandedCallId]   = useState<string | null>(null);
  const [showChainInfo, setShowChainInfo]     = useState(false);

  // Common
  const [searchQuery, setSearchQuery] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [autoScroll, setAutoScroll]   = useState(true);
  const [isLiveMode,  setIsLiveMode]  = useState(false);
  const liveCursorRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [expandedId, setExpandedId]   = useState<string | null>(null);

  // Flash animation tracking
  const [flashItems, setFlashItems]         = useState<Set<string>>(new Set());
  const prevNotifIdsRef    = useRef<Set<string>>(new Set());
  const prevTimelineIdsRef = useRef<Set<string>>(new Set());

  const contentRef     = useRef<HTMLDivElement>(null);
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isTimelineView = selectedSource === 'timeline' || selectedSource === 'tooluse';
  const isOverview     = selectedSource === 'overview';
  const isFileView     = selectedSource !== 'activity' && !isOverview && !isTimelineView;
  // Count of security events recorded today (blocked access, rejections, skill
  // blocks/overrides/alerts). Drives the pulsing attention marker on the Log
  // Files section so the admin is nudged to open the security log when
  // something happened. Recomputed from the aggregator, no extra request.
  const securityAlertCount = (() => {
    const s = securityOverview;
    if (!s) return 0;
    const fw = s.firewall ? s.firewall.blocked_today + s.firewall.failed_logins_today : 0;
    const ch = s.channels ? s.channels.rejected_today : 0;
    const sk = s.skills ? s.skills.blocked_today + s.skills.overrides_today + s.skills.alerts_today : 0;
    return fw + ch + sk;
  })();
  const securityLogFilename = logFiles.find(f => f.domain === 'security')?.filename;
  // UNREAD gate: the badge/pulse only shows while there is a security event
  // NEWER than the last time the admin opened the security log (shared marker
  // with the sidebar dot). Clicking the security log clears it, and a fresh
  // event re-lights it - not a permanent count.
  const securityLatestTs = securityOverview?.security_latest_ts ?? null;
  const [securitySeenTs, setSecuritySeenTs] = useState<string | null>(
    () => (typeof window !== 'undefined' ? localStorage.getItem('vaf_logs_seen_ts') : null)
  );
  const securityUnread = securityAlertCount > 0 && !!securityLatestTs && (!securitySeenTs || securityLatestTs > securitySeenTs);
  // Opening the security log marks everything up to the newest event as seen
  // (clears this badge AND, via onSecuritySeen, the sidebar dot).
  useEffect(() => {
    if (!securityLogFilename || selectedSource !== securityLogFilename || !securityLatestTs) return;
    if (securitySeenTs && securityLatestTs <= securitySeenTs) return;
    setSecuritySeenTs(securityLatestTs);
    try { localStorage.setItem('vaf_logs_seen_ts', securityLatestTs); } catch { /* private mode */ }
    onSecuritySeen?.(securityLatestTs);
  }, [selectedSource, securityLogFilename, securityLatestTs, securitySeenTs, onSecuritySeen]);

  // ── Fetchers ─────────────────────────────────────────────────────────────────

  const fetchFiles = useCallback(() => {
    setLoadingFiles(true);
    fetch(`${getApiBase()}/api/logs`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { files: [] })
      .then(d => setLogFiles(Array.isArray(d?.files) ? d.files : []))
      .catch(() => setLogFiles([]))
      .finally(() => setLoadingFiles(false));
  }, []);

  const fetchContent = useCallback((filename: string) => {
    setLoadingContent(true);
    fetch(`${getApiBase()}/api/logs/${encodeURIComponent(filename)}?tail=500`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { lines: [], total_lines: 0 })
      .then(d => {
        setLogLines(Array.isArray(d?.lines) ? d.lines : []);
        setTotalLines(d?.total_lines ?? 0);
      })
      .catch(() => setLogLines([]))
      .finally(() => setLoadingContent(false));
  }, []);

  const fetchActivity = useCallback(() => {
    fetch(`${getApiBase()}/api/notifications?limit=50`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { notifications: [] })
      .then(d => onFetchComplete?.(Array.isArray(d?.notifications) ? d.notifications : []))
      .catch(() => onFetchComplete?.([]));
  }, [onFetchComplete]);

  const fetchTimelineDates = useCallback(() => {
    fetch(`${getApiBase()}/api/logs/timeline/dates`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { dates: [] })
      .then(d => {
        const dates: string[] = Array.isArray(d?.dates) ? d.dates : [];
        setTimelineDates(dates);
        const today = new Date().toISOString().slice(0, 10);
        const best = dates.includes(today) ? today : (dates[0] ?? today);
        setTimelineDate(prev => prev || best);
      })
      .catch(() => setTimelineDates([]));
  }, []);

  const fetchTimeline = useCallback((date: string) => {
    setLoadingTimeline(true);
    fetch(`${getApiBase()}/api/logs/timeline/events?date=${encodeURIComponent(date)}&merge=true`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { events: [], chain_ok: null, total_raw: null })
      .then(d => {
        setTimelineEvents(Array.isArray(d?.events) ? d.events : []);
        setTimelineChainOk(d?.chain_ok ?? null);
        setTimelineTotalRaw(typeof d?.total_raw === 'number' ? d.total_raw : null);
      })
      .catch(() => { setTimelineEvents([]); setTimelineChainOk(null); setTimelineTotalRaw(null); })
      .finally(() => setLoadingTimeline(false));
  }, []);

  // ── Effects ──────────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!isOpen) return;
    fetchFiles();
    fetchActivity();
    fetchTimelineDates();
  }, [isOpen, fetchFiles, fetchActivity, fetchTimelineDates]);

  useEffect(() => {
    if (!isOpen || !(isTimelineView || isOverview) || !timelineDate) return;
    fetchTimeline(timelineDate);
  }, [isOpen, isTimelineView, isOverview, timelineDate, fetchTimeline]);

  // Overview: keep the audit chain live - always show the NEWEST end of today's
  // chain (5 s cadence, matching the Live toggle's interval). Past dates are
  // static, so polling only runs while today is selected.
  useEffect(() => {
    if (!isOpen || !isOverview || !timelineDate) return;
    if (timelineDate !== new Date().toISOString().slice(0, 10)) return;
    const iv = setInterval(() => fetchTimeline(timelineDate), 5000);
    return () => clearInterval(iv);
  }, [isOpen, isOverview, timelineDate, fetchTimeline]);

  // Overview: protection-module status. Fetched on open + a slow 30 s cadence
  // (docker inspect etc. change rarely; no need for the 5 s chain rhythm).
  // Failures (403 for non-admins, backend down) leave it null -> grey rows.
  // Exposed as a callback so pane actions (e.g. quarantine resolution) can
  // refresh immediately after a mutation.
  const loadSecurity = useCallback(() => {
    fetch(`${getApiBase()}/api/security/overview`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setSecurityOverview(d && typeof d === 'object' ? d : null))
      .catch(() => setSecurityOverview(null));
    fetch(`${getApiBase()}/api/thinking/status`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setThinkingStatus(d && Array.isArray(d.users) ? d : null))
      .catch(() => setThinkingStatus(null));
    fetch(`${getApiBase()}/api/supervisor/status`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setSupervisorStatus(d && Array.isArray(d.units) ? d : null))
      .catch(() => setSupervisorStatus(null));
    fetch(`${getApiBase()}/api/memory/health`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setMemoryHealth(d && typeof d === 'object' ? d : null))
      .catch(() => setMemoryHealth(null));
    fetch(`${getApiBase()}/api/email/messages?limit=100`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setMailMessages(Array.isArray(d?.messages) ? d.messages : null))
      .catch(() => setMailMessages(null));
  }, []);
  useEffect(() => {
    if (!isOpen || !isOverview) return;
    loadSecurity();
    const iv = setInterval(loadSecurity, 30000);
    return () => clearInterval(iv);
  }, [isOpen, isOverview, loadSecurity]);

  useEffect(() => {
    if (!isOpen || !isFileView) return;
    fetchContent(selectedSource);
    setSearchQuery('');
  }, [isOpen, selectedSource, isFileView, fetchContent]);

  useEffect(() => {
    if (autoScroll && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight;
    }
  }, [logLines, autoScroll]);

  useEffect(() => {
    if (autoRefreshRef.current) { clearInterval(autoRefreshRef.current); autoRefreshRef.current = null; }
    if (autoRefresh && isOpen) {
      if (isTimelineView) {
        autoRefreshRef.current = setInterval(() => timelineDate && fetchTimeline(timelineDate), 5000);
      } else if (isFileView) {
        autoRefreshRef.current = setInterval(() => fetchContent(selectedSource), 5000);
      }
    }
    return () => { if (autoRefreshRef.current) clearInterval(autoRefreshRef.current); };
  }, [autoRefresh, isOpen, selectedSource, isTimelineView, isFileView, timelineDate, fetchTimeline, fetchContent]);

  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') { onClose(); e.preventDefault(); } };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isOpen, onClose]);


  // Flash new notification items (activity feed)
  useEffect(() => {
    const currentIds = new Set(notifications.map(n => n.id));
    if (prevNotifIdsRef.current.size > 0) {
      const newIds = [...currentIds].filter(id => !prevNotifIdsRef.current.has(id));
      if (newIds.length > 0) {
        setFlashItems(prev => new Set([...prev, ...newIds]));
        setTimeout(() => setFlashItems(prev => {
          const next = new Set(prev);
          newIds.forEach(id => next.delete(id));
          return next;
        }), 1400);
      }
    }
    prevNotifIdsRef.current = currentIds;
  }, [notifications]);

  // Flash new timeline items (tool use list)
  useEffect(() => {
    const currentIds = new Set(timelineEvents.map((ev, i) =>
      ev.call_id ?? ev.task_id ?? ev.run_id ?? `tl-${i}`));
    if (prevTimelineIdsRef.current.size > 0) {
      const newIds = [...currentIds].filter(id => !prevTimelineIdsRef.current.has(id));
      if (newIds.length > 0) {
        setFlashItems(prev => new Set([...prev, ...newIds]));
        setTimeout(() => setFlashItems(prev => {
          const next = new Set(prev);
          newIds.forEach(id => next.delete(id));
          return next;
        }), 1400);
      }
    }
    prevTimelineIdsRef.current = currentIds;
  }, [timelineEvents]);

  if (!isOpen) return null;

  // ── Derived ──────────────────────────────────────────────────────────────────

  const domainLatest = logFiles.reduce<Record<string, LogFile>>((acc, f) => {
    if (!acc[f.domain] || f.modified > acc[f.domain].modified) acc[f.domain] = f;
    return acc;
  }, {});
  const sortedDomains = Object.keys(domainLatest).sort();

  const filteredLines = searchQuery.trim()
    ? logLines.filter(l => l.toLowerCase().includes(searchQuery.toLowerCase()))
    : logLines;

  const sortedNotifications = [...notifications].sort(
    (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
  );

  function highlight(text: string) {
    if (!searchQuery.trim()) return <>{text}</>;
    const q = searchQuery.toLowerCase();
    const idx = text.toLowerCase().indexOf(q);
    if (idx === -1) return <>{text}</>;
    return (
      <>
        {text.slice(0, idx)}
        <mark className="bg-yellow-300/80 text-gray-900 rounded-sm">{text.slice(idx, idx + searchQuery.length)}</mark>
        {text.slice(idx + searchQuery.length)}
      </>
    );
  }

  const today = new Date().toISOString().slice(0, 10);

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 max-md:p-0" onClick={onClose}>
      <style>{`
        @keyframes flash-new {
          0%   { box-shadow: none; }
          25%  { box-shadow: 0 0 0 2px rgba(96,165,250,0.75); }
          50%  { box-shadow: none; }
          75%  { box-shadow: 0 0 0 2px rgba(96,165,250,0.75); }
          100% { box-shadow: none; }
        }
        .flash-new { animation: flash-new 1.4s ease-out; }
      `}</style>
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
      <div
        className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div className="shrink-0 px-4 py-3 border-b border-gray-200 flex items-center gap-2.5 max-md:gap-1.5 max-md:px-3 bg-white">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="hidden max-md:inline-flex items-center justify-center -ml-1 h-8 w-8 rounded-lg text-gray-500 hover:bg-gray-100 shrink-0"
            aria-label="Sections"
          >
            <Menu size={18} />
          </button>
          <h2 className="text-base font-bold text-gray-900 shrink-0">{t('title')}</h2>

          {isFileView && (
            <>
              <span className="text-gray-300 shrink-0">/</span>
              <span className="text-sm text-gray-500 font-mono truncate min-w-0">{selectedSource}</span>
              {totalLines > 500 && (
                <span className="text-[11px] text-gray-400 shrink-0 ml-1">
                  {t('linesOf', { tail: Math.min(500, filteredLines.length), total: totalLines })}
                </span>
              )}
            </>
          )}

          {/* Chain badge */}
          {isTimelineView && timelineChainOk !== null && (
            <div className="relative shrink-0">
              <button
                type="button"
                onClick={() => setShowChainInfo(v => !v)}
                className={cn(
                  'flex items-center gap-1 text-[11px] font-medium px-2 py-0.5 rounded-full border transition-opacity hover:opacity-80',
                  timelineChainOk
                    ? 'bg-green-50 text-green-700 border-green-200'
                    : 'bg-red-50 text-red-600 border-red-200 animate-pulse'
                )}
              >
                {timelineChainOk
                  ? <><ShieldCheck size={11} /><span className="max-md:hidden">{t('chainOk')}</span></>
                  : <><ShieldAlert size={11} /><span className="max-md:hidden">{t('chainFailed')}</span></>}
              </button>

              {showChainInfo && (
                <div
                  className="absolute top-8 left-0 z-50 w-80 bg-white rounded-2xl shadow-2xl border border-gray-200 p-4 animate-in fade-in zoom-in-95 duration-150"
                  onClick={e => e.stopPropagation()}
                >
                  <button type="button" onClick={() => setShowChainInfo(false)}
                    className="absolute top-3 right-3 p-1 hover:bg-gray-100 rounded-lg text-gray-400">
                    <X size={13} />
                  </button>
                  <p className="text-xs font-semibold text-gray-700 mb-3 flex items-center gap-1.5">
                    <Link2 size={12} className="text-gray-500" />
                    How the hash chain works
                  </p>
                  <ChainVisualization events={timelineEvents} chainOk={timelineChainOk} />
                  <p className="mt-3 text-[11px] text-gray-500 leading-relaxed">
                    Every tool call is saved with a <strong className="text-gray-700">SHA-256 fingerprint</strong> that includes the previous event&apos;s fingerprint. Modifying or deleting any event <strong className="text-gray-700">instantly breaks</strong> all following hashes.
                  </p>
                  {!timelineChainOk && (
                    <p className="mt-2 text-[11px] text-red-600 font-medium bg-red-50 rounded-lg px-2.5 py-1.5 border border-red-200">
                      Chain is broken — one or more events may have been tampered with or deleted.
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="flex-1 min-w-0" />

          {/* Date selector — timeline views */}
          {isTimelineView && (
            <select
              value={timelineDate}
              onChange={e => setTimelineDate(e.target.value)}
              className="text-xs border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-gray-400 bg-gray-50 shrink-0 max-md:max-w-[120px] max-md:px-1.5"
            >
              {timelineDates.length === 0 && <option value={today}>{t('today')}</option>}
              {timelineDates.map(d => (
                <option key={d} value={d}>{d === today ? `${d} (${t('today')})` : d}</option>
              ))}
            </select>
          )}

          {/* Search — file view */}
          {isFileView && (
            <div className="relative shrink-0">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              <input
                type="text"
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                placeholder={t('searchPlaceholder')}
                className="pl-7 pr-3 py-1.5 text-xs border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-400 bg-gray-50 w-44"
              />
            </div>
          )}

          {/* Live toggle */}
          {selectedSource !== 'activity' && !isOverview && (
            <button
              type="button"
              onClick={() => {
                const next = !autoRefresh;
                setAutoRefresh(next);
                // When going live: also enable live cursor mode for timeline/canvas/activity
                if (next && isTimelineView) {
                  setIsLiveMode(true);
                  if (liveCursorRef.current) clearInterval(liveCursorRef.current);
                  // Immediately jump cursor to now
                  // (cursorTs is in HorizontalTimeline state — signal via a custom event)
                  window.dispatchEvent(new CustomEvent('vaf-tl-live', { detail: { ts: Date.now() } }));
                  liveCursorRef.current = setInterval(() => {
                    window.dispatchEvent(new CustomEvent('vaf-tl-live', { detail: { ts: Date.now() } }));
                  }, 3000);
                } else {
                  setIsLiveMode(false);
                  if (liveCursorRef.current) { clearInterval(liveCursorRef.current); liveCursorRef.current = null; }
                }
              }}
              title={t('autoRefreshTitle')}
              className={cn(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors shrink-0 max-md:px-2',
                autoRefresh ? 'bg-green-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              )}
            >
              <span className={cn('w-1.5 h-1.5 rounded-full shrink-0', autoRefresh ? 'bg-white animate-pulse' : 'bg-gray-400')} />
              <span className="max-md:hidden">{t('autoRefresh')}</span>
            </button>
          )}

          {/* Manual refresh */}
          {selectedSource !== 'activity' && !isOverview && (
            <button
              type="button"
              onClick={() => {
                if (isTimelineView) { timelineDate && fetchTimeline(timelineDate); }
                else { fetchContent(selectedSource); }
              }}
              disabled={loadingContent || loadingTimeline}
              title={t('refresh')}
              className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700 shrink-0 disabled:opacity-50 max-md:hidden"
            >
              <RefreshCw size={15} className={(loadingContent || loadingTimeline) ? 'animate-spin' : ''} />
            </button>
          )}

          <button onClick={onClose} title={t('close')}
            className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700 shrink-0">
            <X size={18} />
          </button>
        </div>

        {/* ── Body ── */}
        <div className="flex-1 flex min-h-0">

          {/* mobile: scrim behind the off-canvas drawer */}
          {sidebarOpen && (
            <div className="fixed inset-0 z-40 bg-black/40 md:hidden" onClick={() => setSidebarOpen(false)} />
          )}

          {/* ── Sidebar (off-canvas drawer on mobile) ── */}
          <div className={cn(
            "w-44 shrink-0 border-r border-gray-100 flex flex-col min-h-0 bg-gray-50/60",
            "max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-50 max-md:w-64 max-md:shadow-2xl max-md:transition-transform max-md:duration-300",
            sidebarOpen ? "max-md:translate-x-0" : "max-md:-translate-x-full"
          )}>
            <div className="flex-1 overflow-auto py-2 px-1.5 space-y-0.5">

              {/* Overview section */}
              <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold px-2 pt-1 pb-0.5">
                {t('ovSection')}
              </p>
              <button
                type="button"
                onClick={() => setSelectedSource('overview')}
                className={cn(
                  'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                  selectedSource === 'overview' ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'text-gray-700 hover:bg-gray-200'
                )}
              >
                <LayoutGrid size={13} className="shrink-0" />
                <span className="flex-1 truncate">{t('ovLabel')}</span>
              </button>

              {/* Timeline section */}
              <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold px-2 pt-3 pb-0.5">
                {t('sectionTimeline')}
              </p>
              <button
                type="button"
                onClick={() => setSelectedSource('timeline')}
                className={cn(
                  'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                  selectedSource === 'timeline' ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'text-gray-700 hover:bg-gray-200'
                )}
              >
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none" className="shrink-0">
                  <rect x="1" y="6" width="12" height="2" rx="1" fill="currentColor" opacity="0.4"/>
                  <circle cx="3"  cy="7" r="2" fill="currentColor"/>
                  <circle cx="7"  cy="7" r="2" fill="currentColor" opacity="0.7"/>
                  <circle cx="11" cy="7" r="2" fill="currentColor" opacity="0.5"/>
                </svg>
                <span className="flex-1 truncate">{t('timelineLabel')}</span>
              </button>

              {/* Tool Use section */}
              <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold px-2 pt-3 pb-0.5">
                {t('sectionToolUse')}
              </p>
              <button
                type="button"
                onClick={() => setSelectedSource('tooluse')}
                className={cn(
                  'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                  selectedSource === 'tooluse' ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'text-gray-700 hover:bg-gray-200'
                )}
              >
                <GitBranch size={13} className="shrink-0" />
                <span className="flex-1 truncate">{t('toolUseLabel')}</span>
                {timelineEvents.length > 0 && (
                  <span className={cn('text-[10px] font-bold px-1.5 py-0.5 rounded-full shrink-0',
                    selectedSource === 'tooluse' ? 'bg-white/20' : 'bg-gray-200 text-gray-500')}>
                    {timelineEvents.length}
                  </span>
                )}
              </button>

              {/* Activity section */}
              <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold px-2 pt-3 pb-0.5">
                {t('sectionActivity')}
              </p>
              <button
                type="button"
                onClick={() => setSelectedSource('activity')}
                className={cn(
                  'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                  selectedSource === 'activity' ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'text-gray-700 hover:bg-gray-200'
                )}
              >
                <Activity size={13} className="shrink-0" />
                <span className="flex-1 truncate">{t('activityLabel')}</span>
                {notifications.length > 0 && (
                  <span className={cn('text-[10px] font-bold px-1.5 py-0.5 rounded-full shrink-0',
                    selectedSource === 'activity' ? 'bg-white/20' : 'bg-gray-200 text-gray-500')}>
                    {notifications.length}
                  </span>
                )}
              </button>

              {/* Log Files — collapsible. A pulsing count nudges the admin to the
                  security log when access attempts / skill events were recorded. */}
              <button
                type="button"
                onClick={() => {
                  const next = !logsExpanded;
                  setLogsExpanded(next);
                  // expanding via the unread badge jumps straight to the security log
                  if (next && securityUnread && securityLogFilename) setSelectedSource(securityLogFilename);
                }}
                className="w-full flex items-center gap-1.5 px-2 pt-3 pb-0.5 text-left"
                title={securityUnread ? t('ovLogAlert', { n: securityAlertCount }) : undefined}
              >
                <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold flex-1">
                  {t('sectionFiles')}
                </p>
                {securityUnread && (
                  <span
                    className="text-[9px] font-bold px-1.5 py-0.5 rounded-full shrink-0 animate-pulse"
                    style={{ color: '#fff', background: '#ef4444' }}
                  >
                    {securityAlertCount}
                  </span>
                )}
                {logsExpanded
                  ? <ChevronDown size={10} className="text-gray-400 shrink-0" />
                  : <ChevronRight size={10} className="text-gray-400 shrink-0" />}
              </button>

              {logsExpanded && (
                loadingFiles ? (
                  <p className="text-xs text-gray-400 px-2.5 py-1">{t('loading')}</p>
                ) : sortedDomains.length === 0 ? (
                  <p className="text-xs text-gray-400 px-2.5 py-1 whitespace-pre-line">{t('noFiles')}</p>
                ) : (
                  sortedDomains.map(domain => {
                    const file = domainLatest[domain];
                    const isSelected = selectedSource === file.filename;
                    return (
                      <button
                        key={domain}
                        type="button"
                        onClick={() => setSelectedSource(file.filename)}
                        className={cn(
                          'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                          isSelected ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'text-gray-700 hover:bg-gray-200'
                        )}
                      >
                        <span className={cn('w-2 h-2 rounded-full shrink-0', DOMAIN_COLOR[domain] ?? 'bg-gray-400',
                          domain === 'security' && securityUnread && !isSelected && 'animate-pulse')} />
                        <span className="flex-1 truncate font-mono text-xs">{domain}</span>
                        {domain === 'security' && securityUnread ? (
                          <span className={cn('text-[9px] font-bold px-1.5 py-0.5 rounded-full shrink-0', isSelected ? 'bg-white/20' : 'bg-red-500 text-white')}>
                            {securityAlertCount}
                          </span>
                        ) : file.date && (
                          <span className={cn('text-[9px] shrink-0', isSelected ? 'text-white/50' : 'text-gray-400')}>
                            {file.date.slice(5)}
                          </span>
                        )}
                      </button>
                    );
                  })
                )
              )}
            </div>

            {/* Auto-scroll — file view */}
            {isFileView && (
              <div className="shrink-0 px-3 py-2.5 border-t border-gray-200">
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={autoScroll}
                    onChange={e => setAutoScroll(e.target.checked)}
                    className="rounded border-gray-300 text-gray-900 focus:ring-gray-400 w-3.5 h-3.5"
                  />
                  <span className="text-xs text-gray-500">{t('autoScroll')}</span>
                </label>
              </div>
            )}
          </div>

          {/* ── Content ── */}
          <div className="flex-1 min-w-0 flex flex-col min-h-0">

            {/* OVERVIEW - protection dashboard */}
            {isOverview ? (
              <OverviewPane
                chainOk={timelineChainOk}
                events={timelineEvents}
                totalRaw={timelineTotalRaw}
                dates={timelineDates}
                date={timelineDate}
                today={today}
                onDateChange={setTimelineDate}
                security={securityOverview}
                memHealth={memoryHealth}
                mail={mailMessages}
                thinking={thinkingStatus}
                supervisor={supervisorStatus}
                securityLogFile={domainLatest['security']?.filename}
                onOpenLogFile={(f) => setSelectedSource(f)}
                onRefreshSecurity={loadSecurity}
              />

            /* HORIZONTAL TIMELINE */
            ) : selectedSource === 'timeline' ? (
              loadingTimeline && timelineEvents.length === 0 ? (
                <div className="flex-1 flex items-center justify-center" style={{ background: '#0d1117' }}>
                  <div className="flex items-center gap-2 text-sm" style={{ color: '#8b949e' }}>
                    <Loader2 size={14} className="animate-spin" />
                    {t('loading')}
                  </div>
                </div>
              ) : (
                <HorizontalTimeline
                  events={timelineEvents}
                  date={timelineDate}
                  hour12={userTimeFormat === '12h'}
                  i18n={{
                    activityTitle: t('activityPanelTitle'),
                    clickHint: t('activityClickHint'),
                    canvasHint: t('canvasHint'),
                  }}
                />
              )

            /* VERTICAL TOOL USE */
            ) : selectedSource === 'tooluse' ? (
              <div className="flex-1 overflow-auto p-4">
                {loadingTimeline && timelineEvents.length === 0 ? (
                  <div className="flex items-center gap-2 text-sm text-gray-400">
                    <Loader2 size={14} className="animate-spin" />
                    {t('loading')}
                  </div>
                ) : timelineEvents.length === 0 ? (
                  <p className="text-sm text-gray-400 whitespace-pre-line">{t('toolUseEmpty')}</p>
                ) : (
                  <div className="relative">
                    <div className="absolute left-[19px] top-2 bottom-2 w-px bg-gray-200" />
                    <ul className="space-y-1">
                      {[...timelineEvents].reverse().map((ev, i) => {
                        const isSubagent    = ev.type === 'subagent_start';
                        const isThinkingRun = ev.type === 'thinking_run';
                        const isWwTrain     = ev.type === 'ww_train_start';
                        const colors     = toolColor(ev.tool ?? '');
                        const callKey    = ev.call_id ?? ev.task_id ?? ev.run_id ?? `tl-${i}`;
                        const isExpanded = expandedCallId === callKey;
                        const isFlashing = flashItems.has(callKey);
                        return (
                          <li key={callKey} className="relative flex gap-3 pl-1">
                            <div className="relative z-10 mt-2 shrink-0">
                              {isSubagent ? (
                                <div className="w-9 h-9 rounded-full bg-indigo-100 border-2 border-indigo-300 flex items-center justify-center">
                                  <GitBranch size={14} className="text-indigo-600" />
                                </div>
                              ) : isThinkingRun ? (
                                <div className="w-9 h-9 rounded-full bg-violet-100 border-2 border-violet-300 flex items-center justify-center">
                                  <Sparkles size={14} className="text-violet-600" />
                                </div>
                              ) : isWwTrain ? (
                                <div className="w-9 h-9 rounded-full bg-teal-100 border-2 border-teal-300 flex items-center justify-center">
                                  {ev.status === 'running'
                                    ? <Loader2 size={14} className="text-teal-600 animate-spin" />
                                    : <GraduationCap size={14} className="text-teal-600" />}
                                </div>
                              ) : (
                                <div className={cn('w-9 h-9 rounded-full border-2 flex items-center justify-center',
                                  ev.status === 'error'   ? 'bg-red-50 border-red-300' :
                                  ev.status === 'running' ? 'bg-yellow-50 border-yellow-300' :
                                  `${colors.bg} border-gray-200`)}>
                                  {ev.status === 'ok'      && <CheckCircle2 size={14} className="text-green-600" />}
                                  {ev.status === 'error'   && <XCircle size={14} className="text-red-500" />}
                                  {ev.status === 'running' && <Loader2 size={14} className="text-yellow-500 animate-spin" />}
                                  {!ev.status && <span className={cn('w-2 h-2 rounded-full', colors.dot)} />}
                                </div>
                              )}
                            </div>
                            <div className="flex-1 min-w-0 mb-2">
                              <div
                                className={cn('rounded-xl border px-3 py-2 cursor-pointer hover:shadow-sm transition-shadow',
                                  isSubagent    ? 'bg-indigo-50/70 border-indigo-200' :
                                  isThinkingRun ? 'bg-violet-50/70 border-violet-200' :
                                  isWwTrain     ? 'bg-teal-50/70 border-teal-200' :
                                  'bg-white border-gray-200',
                                  isFlashing && 'flash-new')}
                                onClick={() => setExpandedCallId(isExpanded ? null : callKey)}
                              >
                                <div className="flex items-center gap-2 min-w-0">
                                  <span className={cn('text-xs font-semibold px-2 py-0.5 rounded-full shrink-0',
                                    isSubagent    ? 'bg-indigo-200 text-indigo-800' :
                                    isThinkingRun ? 'bg-violet-200 text-violet-800' :
                                    isWwTrain     ? 'bg-teal-200 text-teal-800' :
                                    `${colors.bg} ${colors.text}`)}>
                                    {isSubagent ? t('subagentLabel') : isThinkingRun ? 'thinking' : isWwTrain ? `learn: ${ev.tool ?? '?'}` : ev.tool ?? '?'}
                                  </span>
                                  {isWwTrain && ev.status !== 'running' && ev.result && (
                                    <span className={cn('text-[10px] font-medium shrink-0',
                                      ev.result === 'confirmed' ? 'text-green-600' : 'text-amber-600')}>
                                      {ev.result}
                                      {typeof ev.confidence === 'number' ? ` · ${Math.round(ev.confidence * 100)}%` : ''}
                                      {ev.mode ? ` · ${ev.mode}` : ''}
                                    </span>
                                  )}
                                  {isSubagent && ev.agent_type && (
                                    <span className="text-xs text-indigo-600 font-mono truncate">{ev.agent_type}</span>
                                  )}
                                  {isThinkingRun && ev.scope && (
                                    <span className="text-xs text-violet-600 font-mono truncate">{ev.scope}</span>
                                  )}
                                  {typeof ev.duration_s === 'number' && (
                                    <span className="flex items-center gap-0.5 text-[10px] text-gray-400 shrink-0">
                                      <Clock size={9} />
                                      {ev.duration_s < 1 ? `${Math.round(ev.duration_s * 1000)}ms` : `${ev.duration_s.toFixed(1)}s`}
                                    </span>
                                  )}
                                  {ev.status === 'running' && (
                                    <span className="text-[10px] text-yellow-600 font-medium shrink-0">{t('timelineRunning')}</span>
                                  )}
                                  <div className="flex-1" />
                                  <span className="text-[10px] text-gray-400 shrink-0 tabular-nums">{formatTime(ev.ts)}</span>
                                  {isExpanded
                                    ? <ChevronDown size={12} className="text-gray-400 shrink-0" />
                                    : <ChevronRight size={12} className="text-gray-400 shrink-0" />}
                                </div>
                                {ev.session && (
                                  <p className="text-[10px] text-gray-400 mt-0.5 font-mono truncate">
                                    session:{ev.session.slice(0, 12)}…
                                  </p>
                                )}
                              </div>
                              {isExpanded && (
                                <div className="mt-1 rounded-xl border border-gray-100 bg-gray-50 overflow-hidden text-xs font-mono">
                                  {ev.args && (
                                    <div className="px-3 py-2 border-b border-gray-100">
                                      <p className="text-[10px] uppercase text-gray-400 font-sans mb-1">Args</p>
                                      <pre className="text-gray-700 whitespace-pre-wrap break-all max-h-40 overflow-auto">{ev.args}</pre>
                                    </div>
                                  )}
                                  {ev.result && (
                                    <div className="px-3 py-2">
                                      <p className="text-[10px] uppercase text-gray-400 font-sans mb-1">Result</p>
                                      <pre className="text-gray-700 whitespace-pre-wrap break-all max-h-40 overflow-auto">{ev.result}</pre>
                                    </div>
                                  )}
                                  {ev.task_id && (
                                    <div className="px-3 py-1.5 border-t border-gray-100">
                                      <span className="text-[10px] text-gray-400">task_id: {ev.task_id}</span>
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}
              </div>

            /* ACTIVITY */
            ) : selectedSource === 'activity' ? (
              <div className="flex-1 overflow-auto p-4">
                {sortedNotifications.length === 0 ? (
                  <p className="text-sm text-gray-400">{t('empty')}</p>
                ) : (
                  <div className="relative">
                    <div className="absolute left-[19px] top-2 bottom-2 w-px bg-gray-200" />
                    <ul className="space-y-1">
                      {sortedNotifications.map(item => {
                        const meta = KIND_META[item.kind] ?? KIND_META_DEFAULT;
                        const { Icon } = meta;
                        const isExpanded = expandedId === item.id;
                        const isOk  = item.status === 'success';
                        const isErr = item.status === 'error';
                        const isFlashing = flashItems.has(item.id);
                        return (
                          <li key={item.id} className="relative flex gap-3 pl-1">
                            <div className="relative z-10 mt-2 shrink-0">
                              <div className={cn('w-9 h-9 rounded-full border-2 flex items-center justify-center',
                                isErr ? 'bg-red-50 border-red-300' : `${meta.bg} ${meta.border}`)}>
                                {isOk  && <CheckCircle2 size={14} className="text-green-600" />}
                                {isErr && <XCircle size={14} className="text-red-500" />}
                                {!isOk && !isErr && <Icon size={14} className={meta.text} />}
                              </div>
                            </div>
                            <div className="flex-1 min-w-0 mb-2">
                              <div
                                className={cn('rounded-xl border px-3 py-2 cursor-pointer hover:shadow-sm transition-shadow bg-white',
                                  isErr ? 'border-red-200' : 'border-gray-200',
                                  isFlashing && 'flash-new')}
                                onClick={() => setExpandedId(isExpanded ? null : item.id)}
                              >
                                <div className="flex items-center gap-2 min-w-0">
                                  <span className={cn('text-xs font-semibold px-2 py-0.5 rounded-full shrink-0', meta.bg, meta.text)}>
                                    {item.kind}
                                  </span>
                                  <p className="flex-1 font-medium text-gray-900 truncate text-sm min-w-0">{item.title}</p>
                                  <span className="shrink-0 text-[10px] text-gray-400 tabular-nums">{formatRelativeTime(item.timestamp)}</span>
                                  {isExpanded
                                    ? <ChevronDown size={12} className="text-gray-400 shrink-0" />
                                    : <ChevronRight size={12} className="text-gray-400 shrink-0" />}
                                </div>
                                {item.summary && !isExpanded && (
                                  <p className="text-xs text-gray-500 truncate mt-0.5">
                                    {item.summary.split('\n').find(Boolean)?.trim()}
                                  </p>
                                )}
                              </div>
                              {isExpanded && item.summary && (
                                <div className="mt-1 rounded-xl border border-gray-100 bg-gray-50 px-3 py-2.5">
                                  <pre className="text-xs text-gray-700 whitespace-pre-wrap font-mono overflow-auto max-h-52">
                                    {item.summary}
                                  </pre>
                                </div>
                              )}
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}
              </div>

            /* TERMINAL LOG VIEWER */
            ) : (
              <div ref={contentRef} className="flex-1 overflow-auto bg-[#0d1117] font-mono text-xs leading-5 select-text">
                {loadingContent && logLines.length === 0 ? (
                  <p className="text-gray-500 p-4">{t('loading')}</p>
                ) : filteredLines.length === 0 ? (
                  <p className="text-gray-500 p-4">{searchQuery ? t('noResults') : t('emptyFile')}</p>
                ) : (
                  filteredLines.map((line, i) => {
                    const { ts, rest } = parseLogLine(line);
                    const isContinuation = line.startsWith('    ') && !ts;
                    return (
                      <div key={i} className={cn('flex gap-0 hover:bg-white/[0.03]', isContinuation && 'opacity-70')}>
                        <span className="text-[#3d444d] select-none text-right pr-3 pl-4 py-px w-12 shrink-0 tabular-nums">{i + 1}</span>
                        {ts
                          ? <span className="text-[#539bf5] pr-3 py-px whitespace-nowrap shrink-0">{ts}</span>
                          : <span className="w-[168px] shrink-0" />}
                        <span className="text-[#adbac7] py-px pr-4 break-all">{highlight(rest || line)}</span>
                      </div>
                    );
                  })
                )}
                <div className="h-6" />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
