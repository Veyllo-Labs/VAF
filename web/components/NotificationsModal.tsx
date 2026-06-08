'use client';

import { useState, useEffect, useRef, useCallback, useMemo, memo } from 'react';
import { useTranslations } from 'next-intl';
import {
  X, RefreshCw, ChevronDown, ChevronRight, Activity, Search,
  GitBranch, ShieldCheck, ShieldAlert, Clock, CheckCircle2,
  XCircle, Loader2, Link2, Zap, Sparkles, MessageSquare, Info, GraduationCap,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { getApiBase } from '@/lib/utils';
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

export interface NotificationsModalProps {
  isOpen: boolean;
  onClose: () => void;
  notifications: NotificationItem[];
  onFetchComplete?: (list: NotificationItem[]) => void;
  userTimeFormat?: '24h' | '12h';
}

// ─── Domain colors ─────────────────────────────────────────────────────────────

const DOMAIN_COLOR: Record<string, string> = {
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

const ProcessNode = memo(({ data }: { data: {
  tool?: string; evType: string; status: string; duration_s?: number;
  color: string; selected: boolean; args?: string; session?: string;
}}) => {
  const isErr = data.status === 'error';
  const isRun = data.status === 'running';
  const accent = isErr ? '#ef4444' : isRun ? '#f59e0b' : data.color;

  return (
    <div style={{
      width: PROCESS_NODE_W,
      background: data.selected ? accent + '0d' : '#ffffff',
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
          <span style={{ fontWeight: 700, fontSize: 11, color: '#111827',
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
            <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#9ca3af' }}>
              {data.duration_s < 1 ? `${Math.round(data.duration_s * 1000)}ms` : `${data.duration_s.toFixed(2)}s`}
            </span>
          )}
          {data.session && (
            <span style={{ fontSize: 8, fontFamily: 'monospace', color: '#d1d5db',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
              {data.session.slice(0, 12)}
            </span>
          )}
        </div>

        {/* Duration bar */}
        {data.duration_s != null && (
          <div style={{ marginTop: 5, height: 2, borderRadius: 1, background: data.selected ? '#334155' : '#f1f5f9', overflow: 'hidden' }}>
            <div style={{ height: '100%', borderRadius: 1, background: accent,
              width: `${Math.min((data.duration_s / 60) * 100, 100)}%`,
              animation: 'pNodeBar 0.5s ease-out' }} />
          </div>
        )}

        {/* Args snippet */}
        {data.args && (
          <div style={{ marginTop: 4, fontSize: 9, color: '#6b7280',
            fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            <span style={{ color: '#d1d5db' }}>→ </span>
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
      <Background color="#edf2f7" gap={32} />
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
  // hour12 comes from parent (page.tsx fetches it from /api/user/persona)
  const isHour12 = hour12 ?? false;

  // Measure scroll-canvas width
  useEffect(() => {
    const measure = () => {
      if (scrollRef.current) setContainerW(scrollRef.current.getBoundingClientRect().width || 800);
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (scrollRef.current) ro.observe(scrollRef.current);
    return () => ro.disconnect();
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
  const BG        = '#ffffff';
  const BG_LABEL  = '#f9fafb';
  const BG_RULER  = '#f3f4f6';
  const BORDER    = '#e5e7eb';
  const BORDER_LT = '#f3f4f6';
  const TICK_MAJ  = '#9ca3af';
  const TICK_MIN  = '#d1d5db';
  const TS_COLOR  = '#3b82f6';
  const DAY_LINE  = '#9ca3af';
  const DAY_TEXT  = '#374151';
  const NOW_LINE  = '#ef4444';
  const NOW_FILL  = '#ef444418';

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

      {/* ── TOP: 2:3 split — left=Activity panel, right=ReactFlow canvas ── */}
      <div style={{ display: 'flex', flex: 1, minHeight: 0, borderBottom: `1px solid ${BORDER}` }}>

        {/* ── LEFT (2): Activity / detail panel ── */}
        <div style={{ flex: 2, minWidth: 0, display: 'flex', flexDirection: 'column', borderRight: `1px solid ${BORDER}`, background: '#fff', position: 'relative' }}>
          {/* Header */}
          <div style={{ height: 36, flexShrink: 0, display: 'flex', alignItems: 'center', gap: 8, padding: '0 12px', borderBottom: `1px solid ${BORDER}`, background: BG_LABEL }}>
            <div style={{ width: 7, height: 7, borderRadius: '50%', background: cursorTs !== null ? '#6366f1' : '#d1d5db' }} />
            <span style={{ fontSize: 10, fontWeight: 700, color: '#374151', letterSpacing: 0.3 }}>
              {cursorTs !== null
                ? new Date(cursorTs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: isHour12 })
                : (i18n?.activityTitle ?? 'Activity')}
            </span>
            {cursorTs !== null && (
              <span style={{ fontSize: 9, color: '#94a3b8', marginLeft: 'auto' }}>
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
                background: '#ffffff',
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
                  <span style={{ fontWeight: 700, fontSize: 12, color: '#111827', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {detailEvent.tool ?? detailEvent.type}
                  </span>
                  <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#94a3b8', flexShrink: 0 }}>
                    {formatTime(detailEvent.ts, isHour12)}
                  </span>
                  <button
                    onClick={() => { setDetailEvent(null); setSelectedCallId(null); }}
                    style={{ width: 20, height: 20, borderRadius: 5, border: '1.5px solid #fca5a5', background: '#fff1f2', color: '#ef4444', cursor: 'pointer', fontSize: 12, fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, lineHeight: 1 }}
                    title="Close"
                  >✕</button>
                </div>
                {/* Meta row */}
                <div style={{ display: 'flex', gap: 12, padding: '6px 10px', borderBottom: `1px solid #f1f5f9`, flexShrink: 0 }}>
                  {detailEvent.duration_s != null && (
                    <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#64748b' }}>
                      ⏱ {detailEvent.duration_s < 1 ? `${Math.round(detailEvent.duration_s * 1000)}ms` : `${detailEvent.duration_s.toFixed(2)}s`}
                    </span>
                  )}
                  <span style={{ fontSize: 9, fontWeight: 600, color: isErr ? '#ef4444' : '#22c55e' }}>
                    {detailEvent.status ?? 'ok'}
                  </span>
                  {detailEvent.session && (
                    <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#d1d5db', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                      {detailEvent.session}
                    </span>
                  )}
                </div>
                {/* Scrollable log content — white background */}
                <div style={{ flex: 1, overflowY: 'auto', background: '#ffffff', borderTop: '1px solid #f1f5f9' }}>
                  {loadingDetailLogs ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 10px', color: '#94a3b8', fontSize: 10, fontFamily: 'monospace' }}>
                      <Loader2 size={12} className="animate-spin" />loading logs…
                    </div>
                  ) : detailLogs.length === 0 ? (
                    <div style={{ padding: '12px 10px', color: '#94a3b8', fontSize: 10, fontFamily: 'monospace', textAlign: 'center' }}>
                      No log lines found within ±60s
                    </div>
                  ) : (
                    <div>
                      {detailLogs.map((entry, i) => {
                        const isTimeline = entry.file.startsWith('timeline_');
                        const isErr  = /error/i.test(entry.line);
                        const isWarn = /warn/i.test(entry.line);
                        const lineColor = isErr ? '#ef4444' : isWarn ? '#f59e0b' : isTimeline ? '#3b82f6' : '#374151';
                        const bgColor   = isErr ? '#fff5f5' : isWarn ? '#fffbeb' : i % 2 === 0 ? '#ffffff' : '#f9fafb';
                        return (
                          <div key={i} style={{ display: 'flex', gap: 8, padding: (entry as any).block ? '6px 10px' : '2px 10px', fontFamily: 'monospace', fontSize: 11, lineHeight: 1.6, borderBottom: '1px solid #f3f4f6', background: bgColor }}>
                            <span style={{ color: '#94a3b8', flexShrink: 0, minWidth: 80, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600, fontSize: 10 }}>
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
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#d1d5db" strokeWidth="1.5">
                <line x1="12" y1="2" x2="12" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/>
                <circle cx="12" cy="12" r="3" fill="#d1d5db"/>
              </svg>
              <span style={{ fontSize: 10, color: '#9ca3af', fontFamily: 'monospace', textAlign: 'center' }}>
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
                  <div key={i} style={{ display: 'flex', gap: 10, padding: '7px 12px', borderBottom: `1px solid #f8fafc`, alignItems: 'flex-start' }}>
                    <div style={{ width: 3, borderRadius: 2, background: isErr ? '#ef4444' : color, flexShrink: 0, alignSelf: 'stretch', minHeight: 16 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                        <span style={{ fontSize: 12, fontWeight: 700, color: '#111827' }}>{ev.tool ?? ev.type}</span>
                        {ev.duration_s != null && (
                          <span style={{ fontSize: 11, fontFamily: 'monospace', color: '#94a3b8' }}>
                            {ev.duration_s < 1 ? `${Math.round(ev.duration_s * 1000)}ms` : `${ev.duration_s.toFixed(2)}s`}
                          </span>
                        )}
                        <span style={{ fontSize: 11, color: isErr ? '#ef4444' : '#22c55e', fontWeight: 600 }}>{ev.status ?? 'ok'}</span>
                      </div>
                      {ev.args && (
                        <div style={{ fontSize: 11, color: '#64748b', fontFamily: 'monospace', marginTop: 3, wordBreak: 'break-all', lineHeight: 1.5, maxHeight: 56, overflow: 'hidden' }}>
                          <span style={{ color: '#94a3b8' }}>→ </span>{ev.args.slice(0, 200)}
                        </div>
                      )}
                      {ev.result && (
                        <div style={{ fontSize: 11, color: '#475569', fontFamily: 'monospace', marginTop: 2, wordBreak: 'break-all', lineHeight: 1.5, maxHeight: 56, overflow: 'hidden' }}>
                          <span style={{ color: '#94a3b8' }}>← </span>{ev.result.slice(0, 200)}
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
        <div style={{ flex: 3, minWidth: 0, position: 'relative', background: '#f8fafc' }}>
          {cursorTs === null ? (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <span style={{ fontSize: 10, color: '#d1d5db', fontFamily: 'monospace' }}>{i18n?.canvasHint ?? 'Flow'}</span>
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
              <span style={{ fontSize: 10, color: '#6b7280', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {lane.label}
              </span>
            </div>
          ))}
        </div>

        {/* Scrollable canvas */}
        <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>

          {/* Zoom buttons — sticky top-right, outside scroll area */}
          <div style={{ position: 'absolute', top: 4, right: 6, zIndex: 30, display: 'flex', alignItems: 'center', gap: 2, pointerEvents: 'auto' }}>
            <button onClick={zoomOut} title="Zoom out (Ctrl+scroll)" style={{ width: 20, height: 20, borderRadius: 4, border: '1px solid #d1d5db', background: '#f9fafb', color: '#374151', fontSize: 14, lineHeight: 1, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 600 }}>−</button>
            <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#6b7280', minWidth: 26, textAlign: 'center', userSelect: 'none' }}>
              {zoom < 1 ? `${(zoom * 100).toFixed(0)}%` : `${zoom.toFixed(1)}×`}
            </span>
            <button onClick={zoomIn}  title="Zoom in (Ctrl+scroll)"  style={{ width: 20, height: 20, borderRadius: 4, border: '1px solid #d1d5db', background: '#f9fafb', color: '#374151', fontSize: 14, lineHeight: 1, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 600 }}>+</button>
            {!autoScroll && (
              <button
                onClick={() => {
                  setAutoScroll(true);
                  setCursorTs(Date.now()); // jump cursor to live position
                  if (scrollRef.current) scrollRef.current.scrollLeft = scrollRef.current.scrollWidth;
                }}
                title="Jump to now"
                style={{ height: 20, padding: '0 5px', borderRadius: 4, border: '1px solid #fca5a5', background: '#fef2f2', color: '#ef4444', fontSize: 9, fontFamily: 'monospace', cursor: 'pointer', whiteSpace: 'nowrap' }}
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
                    <div style={{ position: 'absolute', top: 0, bottom: 0, left: -1, width: 3, background: '#1e293b' }} />
                    <div style={{ position: 'absolute', bottom: -1, left: -5, width: 0, height: 0, borderLeft: '5px solid transparent', borderRight: '5px solid transparent', borderTop: '8px solid #1e293b' }} />
                    <span style={{ position: 'absolute', top: 3, left: 6, fontSize: 9, color: '#fff', fontFamily: 'monospace', fontWeight: 700, whiteSpace: 'nowrap', background: '#1e293b', padding: '1px 4px', borderRadius: 3 }}>
                      {new Date(cursorTs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: isHour12 })}
                    </span>
                  </div>
                )}
                {/* Playhead in ruler */}
                {playheadX !== null && playheadTs && (
                  <div style={{ position: 'absolute', left: playheadX, top: 0, bottom: 0, pointerEvents: 'none', zIndex: 20 }}>
                    <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: 1, borderLeft: '1px dashed #374151', opacity: 0.5 }} />
                    <div style={{ position: 'absolute', bottom: -1, left: -4, width: 0, height: 0, borderLeft: '4px solid transparent', borderRight: '4px solid transparent', borderTop: '6px solid #374151' }} />
                    <span style={{ position: 'absolute', top: 3, left: 5, fontSize: 9, color: '#1f2937', fontFamily: 'monospace', fontWeight: 700, whiteSpace: 'nowrap', background: 'white', padding: '1px 3px', borderRadius: 3, border: '1px solid #e5e7eb', boxShadow: '0 1px 3px rgba(0,0,0,0.08)' }}>
                      {playheadTs}
                    </span>
                  </div>
                )}
              </div>

              {/* Lane rows */}
              {activeLanes.map((lane, li) => (
                <div key={lane.key} style={{ height: LANE_H, position: 'relative', borderBottom: `1px solid ${BORDER_LT}`, overflow: 'hidden', background: li % 2 === 0 ? BG : '#fafafa' }}>
                  {ticks.map(tick => <div key={tick} style={{ position: 'absolute', left: toX(tick), top: 0, bottom: 0, width: 1, background: BORDER_LT }} />)}
                  {dayBounds.map(db => <div key={db} style={{ position: 'absolute', left: toX(db), top: 0, bottom: 0, width: 1, background: BORDER }} />)}
                  {isToday && <div style={{ position: 'absolute', left: toX(now), top: 0, bottom: 0, width: 1, background: NOW_FILL }} />}
                  {playheadX !== null && <div style={{ position: 'absolute', left: playheadX, top: 0, bottom: 0, width: 1, borderLeft: '1px dashed #37415166', pointerEvents: 'none', zIndex: 5 }} />}
                  {/* Thick cursor line — marks the clicked position */}
                  {cursorTs !== null && <div style={{ position: 'absolute', left: toX(cursorTs) - 1, top: 0, bottom: 0, width: 3, background: '#1e293b', opacity: 0.85, pointerEvents: 'none', zIndex: 10 }} />}
                  {/* Events */}
                  {(byLane.get(lane.key) ?? []).map((ev, i) => {
                    const evTs  = new Date(ev.ts).getTime();
                    const x     = toX(evTs);
                    const dur   = ev.duration_s ?? 0.3;
                    const w     = Math.max(dur * 1000 * pxPerMs, MIN_EV_W);
                    const isErr = ev.status === 'error';
                    const isRun = ev.status === 'running';
                    const col   = isErr ? '#ef4444' : isRun ? '#f59e0b' : lane.color;
                    const isSelected = ev.call_id != null && ev.call_id === selectedCallId;
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
                          opacity: selectedCallId && !isSelected ? 0.45 : 1,
                          transition: 'opacity 0.15s, box-shadow 0.15s',
                        }}
                        onMouseEnter={e => setHovered({ ev, mx: e.clientX, my: e.clientY })}
                        onMouseLeave={() => setHovered(null)}
                        onMouseMove={e => setHovered(p => p ? { ...p, mx: e.clientX, my: e.clientY } : null)}
                        onClick={() => setSelectedCallId(id => id === ev.call_id ? null : (ev.call_id ?? null))}
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

// ─── Main component ────────────────────────────────────────────────────────────

export default function NotificationsModal({
  isOpen,
  onClose,
  notifications,
  onFetchComplete,
  userTimeFormat,
}: NotificationsModalProps) {
  const t = useTranslations('notifications');

  // 'timeline' = horizontal, 'tooluse' = vertical tool list, 'activity', or filename
  const [selectedSource, setSelectedSource] = useState<string>('timeline');
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
  const [loadingTimeline, setLoadingTimeline] = useState(false);
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
  const isFileView     = selectedSource !== 'activity' && !isTimelineView;

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
      .then(r => r.ok ? r.json() : { events: [], chain_ok: true })
      .then(d => {
        setTimelineEvents(Array.isArray(d?.events) ? d.events : []);
        setTimelineChainOk(d?.chain_ok ?? null);
      })
      .catch(() => { setTimelineEvents([]); setTimelineChainOk(null); })
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
    if (!isOpen || !isTimelineView || !timelineDate) return;
    fetchTimeline(timelineDate);
  }, [isOpen, isTimelineView, timelineDate, fetchTimeline]);

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
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={onClose}>
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
        className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div className="shrink-0 px-4 py-3 border-b border-gray-200 flex items-center gap-2.5 bg-white">
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
                  ? <><ShieldCheck size={11} />{t('chainOk')}</>
                  : <><ShieldAlert size={11} />{t('chainFailed')}</>}
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
              className="text-xs border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-gray-400 bg-gray-50 shrink-0"
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
          {selectedSource !== 'activity' && (
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
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors shrink-0',
                autoRefresh ? 'bg-green-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              )}
            >
              <span className={cn('w-1.5 h-1.5 rounded-full shrink-0', autoRefresh ? 'bg-white animate-pulse' : 'bg-gray-400')} />
              {t('autoRefresh')}
            </button>
          )}

          {/* Manual refresh */}
          {selectedSource !== 'activity' && (
            <button
              type="button"
              onClick={() => {
                if (isTimelineView) { timelineDate && fetchTimeline(timelineDate); }
                else { fetchContent(selectedSource); }
              }}
              disabled={loadingContent || loadingTimeline}
              title={t('refresh')}
              className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700 shrink-0 disabled:opacity-50"
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

          {/* ── Sidebar ── */}
          <div className="w-44 shrink-0 border-r border-gray-100 flex flex-col min-h-0 bg-gray-50/60">
            <div className="flex-1 overflow-auto py-2 px-1.5 space-y-0.5">

              {/* Timeline section */}
              <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold px-2 pt-1 pb-0.5">
                {t('sectionTimeline')}
              </p>
              <button
                type="button"
                onClick={() => setSelectedSource('timeline')}
                className={cn(
                  'w-full flex items-center gap-2 px-2.5 py-1.5 text-sm rounded-lg transition-colors text-left',
                  selectedSource === 'timeline' ? 'bg-gray-900 text-white' : 'text-gray-700 hover:bg-gray-200'
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
                  selectedSource === 'tooluse' ? 'bg-gray-900 text-white' : 'text-gray-700 hover:bg-gray-200'
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
                  selectedSource === 'activity' ? 'bg-gray-900 text-white' : 'text-gray-700 hover:bg-gray-200'
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

              {/* Log Files — collapsible */}
              <button
                type="button"
                onClick={() => setLogsExpanded(v => !v)}
                className="w-full flex items-center gap-1.5 px-2 pt-3 pb-0.5 text-left"
              >
                <p className="text-[9px] uppercase tracking-widest text-gray-400 font-semibold flex-1">
                  {t('sectionFiles')}
                </p>
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
                          isSelected ? 'bg-gray-900 text-white' : 'text-gray-700 hover:bg-gray-200'
                        )}
                      >
                        <span className={cn('w-2 h-2 rounded-full shrink-0', DOMAIN_COLOR[domain] ?? 'bg-gray-400')} />
                        <span className="flex-1 truncate font-mono text-xs">{domain}</span>
                        {file.date && (
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

            {/* HORIZONTAL TIMELINE */}
            {selectedSource === 'timeline' ? (
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
