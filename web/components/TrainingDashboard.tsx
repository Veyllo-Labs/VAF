'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
    X, Activity, Zap, AlertTriangle, CheckCircle2, Clock, Eye, ShieldAlert, ListChecks, Loader2,
    Gavel, XCircle,
} from 'lucide-react';
import { AgentAvatar, type AvatarMode } from '@/components/AgentAvatar';

// Whare Wananga training dashboard.
// Big panel (Memory-Graph sized) that shows, per tool: status, error rate, predictions,
// and the three baskets of knowledge (Aronui / Tuatea / Tuarua). Reads the stored
// tool_knowledge record. Live training metrics (duration, error-rate-over-attempts graph)
// populate once the predict-then-verify runner exists; until then those areas are
// placeholders.

interface ToolKnowledge {
    tool?: string;
    status?: string;
    confidence?: number;
    uses?: number;
    success?: number;
    fail?: number;
    side_effect_class?: string;
    aronui?: { when_to_use?: string; output_shape?: string; notes?: string[] };
    tuatea?: { pitfalls?: Array<{ text?: string; source?: string; seen?: number }> };
    tuarua?: { procedure?: string[]; verification?: string[] };
    predict_records?: Array<{ intent?: string; predicted?: string; actual?: string; match?: boolean }>;
    updated_at?: string;
}

const STATE_LABEL: Record<string, string> = {
    learned: 'Learned', learning: 'Learning', stale: 'Stale', unlearned: 'Not learned',
};

// Stage helpers are defined at module scope (NOT inside the component): an inline component
// gets a new identity on every poll, which would remount its subtree — making the ToolMessage
// bubble replay its entrance animation (appear/disappear) every 1.5s instead of mounting once.
function StageCol({ label, sub, footer, children }: { label?: React.ReactNode; sub?: string; footer?: React.ReactNode; children: React.ReactNode }) {
    // The label is positioned ABSOLUTELY below the visual so it does not add to the column's
    // height. That way a row of `items-center` aligns the VISUALS (a short avatar box vs the tall
    // tool card) on one shared centre line, instead of the avatar floating up because its label
    // made its column taller.
    return (
        <div className="relative flex flex-col items-center">
            <div className="flex items-center justify-center">{children}</div>
            {(label || sub || footer) && (
                <div className="absolute top-full mt-2 left-1/2 -translate-x-1/2 w-max text-center">
                    {label ? <div className="text-xs font-bold text-gray-800 truncate max-w-[170px]">{label}</div> : null}
                    {sub ? <div className="text-[10px] text-gray-400">{sub}</div> : null}
                    {footer}
                </div>
            )}
        </div>
    );
}

// Glyphs that "flow" as data — geometric shapes + digits, varied over the run via a seed.
const FLOW_GLYPHS = ['▲', '■', '●', '◆', '7', '3', '5', '0', '9', '2', '△', '◇'];
const flowGlyph = (n: number) => FLOW_GLYPHS[((n % FLOW_GLYPHS.length) + FLOW_GLYPHS.length) % FLOW_GLYPHS.length];

// Data-flow link between two actors. Forward (left -> right): grey shapes/digits while the step
// is active (the agent feeding input to the tool, the tool feeding the judge). Return
// (right -> left): coloured shapes for the latest result — green = pass, red = fail. A faint
// static line when idle. `seed` rotates the glyphs as the run progresses.
function FlowLink({ on, back, seed = 0 }: { on: boolean; back?: 'pass' | 'fail' | null; seed?: number }) {
    const backColor = back === 'pass' ? 'text-emerald-500' : 'text-rose-500';
    return (
        <div className="relative h-8 w-16 shrink-0 self-center overflow-hidden" aria-hidden>
            <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 h-px bg-gray-200" />
            {on && [0, 1, 2].map((i) => (
                <span
                    key={`f${i}`}
                    className="absolute top-0.5 text-[10px] leading-none font-bold text-gray-400 select-none"
                    style={{ left: 0, animation: `wwFlow 1.3s linear ${i * 0.42}s infinite` }}
                >
                    {flowGlyph(seed + i * 4)}
                </span>
            ))}
            {on && back && [0, 1].map((i) => (
                <span
                    key={`b${i}`}
                    className={`absolute bottom-0.5 text-[10px] leading-none font-bold select-none ${backColor}`}
                    style={{ right: 0, animation: `wwFlowBack 1.3s linear ${i * 0.5}s infinite` }}
                >
                    {flowGlyph(seed + 5 + i * 3)}
                </span>
            ))}
        </div>
    );
}

// Pull the main string argument (the code/query/etc.) out of the probe's args JSON for display.
function mainInput(argsJson?: string): string {
    if (!argsJson) return '';
    try {
        const o = JSON.parse(argsJson) as Record<string, unknown>;
        for (const k of ['code', 'query', 'q', 'text', 'content', 'prompt', 'input', 'command', 'path', 'url']) {
            const v = o[k];
            if (typeof v === 'string' && v.trim()) return v.trim();
        }
        for (const v of Object.values(o)) if (typeof v === 'string' && v.trim()) return v.trim();
    } catch { /* not JSON */ }
    return argsJson;
}

function fmtDur(s?: number | null): string {
    if (s == null || !isFinite(s) || s < 0) return '—';
    if (s < 60) return `${Math.round(s)}s`;
    return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

// Per-emotion animation loop length (ms), so a flashed reaction can play at least one full cycle.
const CYCLE_MS: Partial<Record<AvatarMode, number>> = {
    surprised: 2400, curious: 4000, confused: 2800, idea: 2800,
    happy: 1900, excited: 900, sad: 4000, sleepy: 4800,
    nod: 1700, shake: 1400, listening: 2200, search: 3400,
    celebrate: 2400, success: 2400, error: 2200,
};
// A reaction is held for at least 2s AND at least one full cycle -> it never looks cut off.
const reactionHold = (m: AvatarMode): number => Math.max(2000, CYCLE_MS[m] ?? 2000);

// Flash an avatar reaction that is guaranteed to finish: the current reaction is held for
// reactionHold(mode); a reaction requested while one is playing is queued (latest wins) and
// shown afterwards, instead of cutting the running animation short.
function useReactionFlash(): readonly [AvatarMode | null, (m: AvatarMode) => void] {
    const [react, setReact] = useState<AvatarMode | null>(null);
    const holdUntil = useRef(0);
    const queued = useRef<AvatarMode | null>(null);
    const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const showRef = useRef<(m: AvatarMode) => void>(() => {});
    showRef.current = (mode: AvatarMode) => {
        setReact(mode);
        const dur = reactionHold(mode);
        holdUntil.current = Date.now() + dur;
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => {
            const q = queued.current;
            queued.current = null;
            if (q) showRef.current(q); else setReact(null);
        }, dur);
    };
    const fire = useCallback((mode: AvatarMode) => {
        if (Date.now() >= holdUntil.current) showRef.current(mode);
        else queued.current = mode;   // a reaction is playing -> show this one after it finishes
    }, []);
    useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
    return [react, fire] as const;
}

export default function TrainingDashboard({ toolName, onClose, onStateChange }: { toolName: string; onClose: () => void; onStateChange?: (tool: string, state: string) => void }) {
    const [loading, setLoading] = useState(true);
    const [state, setState] = useState<string>('unlearned');
    const [rec, setRec] = useState<ToolKnowledge | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [job, setJob] = useState<any>(null);
    // Transient emotional reactions — each held until it has played a full cycle (>= 2s),
    // queued (latest wins) so rapid live events never cut an animation short.
    const [agentReact, fireAgent] = useReactionFlash();
    const [judgeReact, fireJudge] = useReactionFlash();

    const loadRecord = useCallback(async (): Promise<string> => {
        let st = 'unlearned';
        try {
            const res = await fetch(`/api/whare_wananga/tool_knowledge/${encodeURIComponent(toolName)}`);
            const data = await res.json().catch(() => ({}));
            st = data.state || 'unlearned';
            setState(st);
            setRec(data.record || null);
            if (!data.ok && data.error) setError(String(data.error));
        } catch (e) {
            setError(String(e));
        } finally {
            setLoading(false);
        }
        return st;
    }, [toolName]);

    useEffect(() => { setLoading(true); loadRecord(); }, [loadRecord]);

    // Poll live training status while a job runs; refresh the record once it finishes.
    useEffect(() => {
        let alive = true;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const poll = async () => {
            try {
                const res = await fetch(`/api/whare_wananga/training_status/${encodeURIComponent(toolName)}`);
                const data = await res.json().catch(() => ({}));
                if (!alive) return;
                const st = data.status;
                setJob(st);
                if (st && (st.state === 'done' || st.state === 'error' || st.state === 'skipped')) {
                    const newState = await loadRecord();
                    onStateChange?.(toolName, newState);
                    return;  // finished -> stop polling
                }
                // While running, refresh the stored record too so the three baskets appear as
                // soon as the runner distils them (after the initial learning phase) and update
                // after each refinement round.
                if (st && st.state === 'running') await loadRecord();
            } catch { /* ignore transient poll errors */ }
            if (alive) timer = setTimeout(poll, 1500);
        };
        poll();
        return () => { alive = false; if (timer) clearTimeout(timer); };
    }, [toolName, loadRecord]);

    // Live metrics: while a job runs, read from the job's events (the record is only saved at
    // the end of training); otherwise read from the stored record.
    const running = job?.state === 'running';
    const liveEvents = (job?.events ?? []) as Array<{ i?: number; match?: boolean; phase?: string; predicted_outcome?: string; actual_outcome?: string; verdict?: string; reason?: string; intent?: string; actual?: string }>;
    // Cumulative counters come from the job (full counts, not the capped event list).
    const attemptsCount = running ? (job?.attempt ?? liveEvents.length) : ((rec?.success ?? 0) + (rec?.fail ?? 0));
    const failCount = running ? (job?.fails ?? liveEvents.filter((e) => e.actual_outcome === 'error').length) : (rec?.fail ?? 0);
    const predTotal = running ? (job?.attempt ?? liveEvents.length) : (rec?.predict_records ?? []).length;
    const hitsCount = running ? (job?.hits ?? liveEvents.filter((e) => e.match).length) : (rec?.predict_records ?? []).filter((p) => p.match).length;
    const errorRate = attemptsCount > 0 ? Math.round((failCount / attemptsCount) * 100) : null;
    const confidencePct = running
        ? (predTotal ? Math.round((hitsCount / predTotal) * 100) : 0)
        : (rec ? Math.round((rec.confidence ?? 0) * 100) : null);
    const inValidation = job?.phase === 'validate';
    const inChallenge = job?.phase === 'challenge';
    const inPrep = job?.phase === 'prep';
    const phaseLabel = inPrep
        ? `Preparing prerequisites${job?.prereqs?.length ? ` (${job.prereqs.join(', ')})` : ''}`
        : inChallenge
            ? `Challenge — judge poses the test${job?.challenge ? ` (${job.challenge.round_pass ?? 0}/${job.challenge.need ?? 3} passed, ${job.challenge.total_fails ?? 0}/${job.challenge.max_fails ?? 10} fails)` : ''}`
            : inValidation
                ? `Validating — round ${job?.round ?? 1}${job?.max_rounds ? `/${job.max_rounds}` : ''}`
                : 'Learning';
    const statusLabel = running ? phaseLabel : (STATE_LABEL[state] ?? state);
    const empty = <span className="text-gray-400 italic">— empty —</span>;

    // Training stage: agent -> tool -> judge. The judge appears in the validation phase AND the
    // challenge phase (both judge-graded); during plain learning it's just agent -> tool.
    const lastEvent = liveEvents[liveEvents.length - 1];
    const judgeEvents = liveEvents.filter((e) => e.phase === 'validate' || e.phase === 'challenge');
    const lastJudge = judgeEvents[judgeEvents.length - 1];
    const judgeActive = inValidation || inChallenge;
    const showJudge = judgeActive || judgeEvents.length > 0;
    const judgeVerdict = lastJudge?.verdict;   // 'pass' | 'fail' | undefined
    const judgeReason = lastJudge?.reason;

    // Fire the transient reactions on the relevant live signals.
    const lastIdx = lastEvent?.i;
    const lastMatch = lastEvent?.match;
    const lastPhase = lastEvent?.phase;
    useEffect(() => {
        if (!running || lastIdx == null) return;
        // Learn phase uses the activity states (success / error); validate & challenge use the
        // lighter emotion beats (nod / confused).
        fireAgent(lastPhase === 'learn'
            ? (lastMatch ? 'success' : 'error')
            : (lastMatch ? 'nod' : 'confused'));
    }, [lastIdx, lastMatch, lastPhase, running, fireAgent]);

    const distils = job?.distils ?? 0;
    useEffect(() => {
        if (!running || !distils) return;
        fireAgent('idea');                                    // consolidated the document -> AHA
    }, [distils, running, fireAgent]);

    const lastJudgeIdx = lastJudge?.i;
    const lastJudgeVerdict = lastJudge?.verdict;
    useEffect(() => {
        if (!running || lastJudgeIdx == null) return;
        fireJudge(lastJudgeVerdict === 'pass' ? 'nod' : 'shake');  // approve / reject
    }, [lastJudgeIdx, lastJudgeVerdict, running, fireJudge]);

    // Avatar states:
    //   Agent  — Stage 1 (learn): the "learn" activity state (knowledge orbs), with success/error
    //            activity beats per probe; Stage 2 (validate): thinking + nod/confused; Stage 3
    //            (challenge): listening + nod/confused; idea on a re-distil; celebrate when
    //            mastered, sad on draft/halt, idle at rest.
    //   Judge (inverted) — thinking while grading, talking while posing a challenge; nod/shake
    //            per verdict; idle when resting.
    let agentMode: AvatarMode;
    if (running) {
        agentMode = agentReact ?? (inChallenge ? 'listening' : inValidation ? 'thinking' : 'learn');
    } else if (job?.state === 'done') {
        agentMode = job.halted ? 'sad' : job.challenge_passed ? 'celebrate' : job.confirmed ? 'idle' : 'sad';
    } else if (job?.state === 'error') {
        agentMode = 'sad';
    } else {
        agentMode = 'idle';
    }
    let judgeMode: AvatarMode;
    if (running && judgeActive) {
        judgeMode = judgeReact ?? (inChallenge ? 'talking' : 'thinking');
    } else if (job?.state === 'done' && showJudge) {
        judgeMode = job.challenge_passed ? 'nod' : 'idle';
    } else {
        judgeMode = 'idle';
    }

    // Tool bubble (same component as the chat, just smaller): driven by the latest probe.
    const toolStatus: 'running' | 'completed' | 'error' =
        running ? 'running' : (lastEvent?.actual_outcome === 'error' ? 'error' : 'completed');
    const toolArgs = lastEvent?.intent;
    const toolResult = lastEvent?.actual;

    // Duration: live (now - started_at) while running, total (ended_at - started_at) when done.
    const durationS = (typeof job?.started_at === 'number')
        ? ((typeof job?.ended_at === 'number' ? job.ended_at : Date.now() / 1000) - job.started_at)
        : null;

    const Metric = ({ icon, label, value, hint }: { icon: React.ReactNode; label: string; value: React.ReactNode; hint?: string }) => (
        <div className="rounded-xl border border-gray-200 bg-gray-50/60 px-4 py-3">
            <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide font-semibold text-gray-500">{icon}{label}</div>
            <div className="mt-1 text-2xl font-bold text-gray-900">{value}</div>
            {hint && <div className="text-[11px] text-gray-400 mt-0.5">{hint}</div>}
        </div>
    );

    const Facet = ({ icon, title, subtitle, children }: { icon: React.ReactNode; title: string; subtitle: string; children: React.ReactNode }) => (
        <div className="rounded-xl border border-gray-200 bg-white overflow-hidden flex flex-col">
            <div className="px-4 py-2.5 border-b border-gray-100 flex items-center gap-2">
                {icon}
                <div>
                    <div className="text-sm font-bold text-gray-900">{title}</div>
                    <div className="text-[11px] text-gray-400">{subtitle}</div>
                </div>
            </div>
            <div className="p-4 text-sm text-gray-700 flex-1 overflow-auto">{children}</div>
        </div>
    );

    return (
        <div className="fixed inset-0 z-[85] flex items-center justify-center p-4" onClick={onClose}>
            <div className="absolute inset-0 bg-black/50 backdrop-blur-md" />
            <div
                className="relative bg-white w-full max-w-[90vw] h-[90vh] rounded-2xl shadow-2xl flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-200"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="h-14 border-b border-gray-200 flex items-center justify-between px-6 shrink-0">
                    <div className="flex items-center gap-3">
                        <Activity size={18} className="text-amber-600" />
                        <span className="font-semibold text-gray-900">Whare Wananga &mdash; Training</span>
                        <span className="font-mono text-sm text-gray-500">{toolName}</span>
                        <span className="px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider rounded-md bg-gray-100 text-gray-500">
                            {STATE_LABEL[state] ?? state}
                        </span>
                    </div>
                    <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-900 rounded-md hover:bg-gray-100 transition-colors">
                        <X size={18} />
                    </button>
                </div>

                {/* Body */}
                <div className="flex-1 overflow-auto p-6 space-y-6">
                    {job?.state === 'running' ? (
                        <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800 flex items-center gap-2">
                            <Loader2 size={16} className="animate-spin shrink-0" />
                            <span>
                                {phaseLabel} · {job.attempt ?? 0} probes · {job.hits ?? 0} correct
                                {job.validate ? ` · last batch ${job.validate.hits}/${job.validate.n}` : ''}
                            </span>
                        </div>
                    ) : job?.state === 'done' && job.declared ? (
                        <div className={`rounded-xl border px-4 py-3 text-sm flex items-center gap-2 ${job.confirmed ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-amber-200 bg-amber-50 text-amber-800'}`}>
                            {job.confirmed ? <CheckCircle2 size={16} className="shrink-0" /> : <AlertTriangle size={16} className="shrink-0" />}
                            <span>
                                {job.confirmed
                                    ? 'Learned from the tool’s declaration — this tool mutates session state and has no rejectable inputs, so it was not probed; the baskets come from its description + schema.'
                                    : 'Could not distil usable knowledge from the tool’s declaration (and it is not safe to probe).'}
                            </span>
                        </div>
                    ) : job?.state === 'done' ? (
                        <div className={`rounded-xl border px-4 py-3 text-sm flex items-center gap-2 ${job.confirmed && job.challenge_passed && !job.halted ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-amber-200 bg-amber-50 text-amber-800'}`}>
                            {job.confirmed && job.challenge_passed && !job.halted ? <CheckCircle2 size={16} className="shrink-0" /> : <AlertTriangle size={16} className="shrink-0" />}
                            <span>
                                {job.halted
                                    ? 'Training halted — an invalid probe was unexpectedly accepted (possible side effect); see Tuatea.'
                                    : (job.confirmed && job.challenge_passed)
                                        ? `Mastered — confirmed 9/9 and passed the judge's challenge.`
                                        : job.confirmed
                                            ? `Learned — confirmed 9/9, but did not pass the judge's challenge (${job.challenge_fails ?? job.challenge?.total_fails ?? 0} fails). Stays confirmed.`
                                            : `Stopped after ${job.rounds ?? 0} rounds — not fully validated (status ${job.status ?? state}).`}
                                {' '}Confidence {Math.round((((job.confidence ?? rec?.confidence) ?? 0) as number) * 100)}%.
                            </span>
                        </div>
                    ) : job?.state === 'skipped' ? (
                        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 flex items-center gap-2">
                            <AlertTriangle size={16} className="shrink-0" />
                            <span>Skipped: {job.reason || 'not eligible for training'}.</span>
                        </div>
                    ) : job?.state === 'error' ? (
                        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800 flex items-center gap-2">
                            <XCircle size={16} className="shrink-0" />
                            <span>Stopped — {job.error || 'training failed'}</span>
                        </div>
                    ) : (
                        <div className="rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-600 flex items-center gap-2">
                            <Activity size={16} className="shrink-0" />
                            <span>Showing the stored tool_knowledge record. Use &quot;Train tool now&quot; to run a fresh pass.</span>
                        </div>
                    )}

                    {/* Metrics */}
                    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
                        <Metric icon={<Activity size={13} />} label="Status" value={statusLabel} />
                        <Metric icon={<CheckCircle2 size={13} />} label="Confidence" value={confidencePct === null ? '—' : `${confidencePct}%`} />
                        <Metric icon={<Clock size={13} />} label="Duration" value={fmtDur(durationS)} hint={running ? 'running' : (durationS != null ? 'total' : '')} />
                        <Metric icon={<Zap size={13} />} label="Uses" value={attemptsCount} />
                        <Metric icon={<AlertTriangle size={13} />} label="Tool error rate" value={errorRate === null ? '—' : `${errorRate}%`} hint={attemptsCount ? `${failCount}/${attemptsCount} tool errors` : 'no runs yet'} />
                        <Metric icon={<CheckCircle2 size={13} />} label="Predictions" value={predTotal ? `${hitsCount}/${predTotal}` : '—'} hint="correct / all probes" />
                    </div>

                    {/* Error-rate / progress graph (placeholder until the runner streams attempts) */}
                    <div className="rounded-xl border border-gray-200 bg-gray-50/40 p-4">
                        <div className="text-[11px] uppercase tracking-wide font-semibold text-gray-500 mb-2 flex items-center gap-1.5">
                            <Activity size={13} /> Predict-then-verify attempts
                        </div>
                        {(() => {
                            const live = (job?.events ?? []) as Array<{ i?: number; match?: boolean; predicted_outcome?: string; actual_outcome?: string }>;
                            const fromRec = (rec?.predict_records ?? []).map((p, i) => ({
                                i: i + 1, match: p.match,
                                predicted_outcome: String(p.predicted ?? '').split(':')[0],
                                actual_outcome: String(p.actual ?? '').split(':')[0],
                            }));
                            // While running, stream the live events; once done, render the FULL
                            // stored record (the live events are capped, so using them after the
                            // run made the badge count disagree with the "Predictions" metric).
                            const items = running ? (live.length ? live : fromRec) : (fromRec.length ? fromRec : live);
                            if (!items.length) {
                                return (
                                    <div className="h-24 flex items-center justify-center text-sm text-gray-400 border border-dashed border-gray-300 rounded-lg">
                                        {loading || job?.state === 'running' ? 'Probing…' : 'No attempts yet — use "Train tool now".'}
                                    </div>
                                );
                            }
                            return (
                                <div className="flex flex-col gap-1.5">
                                    <div className="flex gap-1.5 flex-wrap">
                                        {items.map((it, i) => (
                                            <div
                                                key={i}
                                                title={`#${it.i ?? i + 1}: predicted ${it.predicted_outcome} / actual ${it.actual_outcome}`}
                                                className={`w-7 h-7 rounded-md flex items-center justify-center text-[11px] font-bold ${it.match ? 'bg-emerald-100 text-emerald-700' : 'bg-rose-100 text-rose-700'}`}
                                            >
                                                {it.match ? '✓' : '✗'}
                                            </div>
                                        ))}
                                    </div>
                                    <div className="text-[11px] text-gray-400">green = prediction matched reality · red = surprise</div>
                                </div>
                            );
                        })()}
                    </div>

                    {/* Three baskets (Nga Kete) */}
                    <div>
                        <div className="text-[11px] uppercase tracking-wide font-semibold text-gray-500 mb-2">
                            What is learned — Nga Kete (the three baskets)
                        </div>
                        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 min-h-[200px]">
                            <Facet icon={<Eye size={16} className="text-blue-500" />} title="Aronui" subtitle="What it returns / when to use it">
                                {rec?.aronui?.when_to_use ? <div className="mb-2"><b>When:</b> {rec.aronui.when_to_use}</div> : null}
                                {rec?.aronui?.output_shape ? <div className="mb-2"><b>Output:</b> {rec.aronui.output_shape}</div> : null}
                                {(rec?.aronui?.notes?.length ?? 0) > 0
                                    ? <ul className="list-disc list-inside">{rec!.aronui!.notes!.map((n, i) => <li key={i}>{n}</li>)}</ul>
                                    : null}
                                {!rec?.aronui?.when_to_use && !rec?.aronui?.output_shape && !(rec?.aronui?.notes?.length) ? empty : null}
                            </Facet>
                            <Facet icon={<ShieldAlert size={16} className="text-rose-500" />} title="Tuatea" subtitle="Dangers / pitfalls">
                                {(rec?.tuatea?.pitfalls?.length ?? 0) > 0
                                    ? <ul className="list-disc list-inside space-y-1">{rec!.tuatea!.pitfalls!.map((p, i) => <li key={i}>{p.text}{p.seen ? <span className="text-gray-400"> ({p.seen}x)</span> : null}</li>)}</ul>
                                    : empty}
                            </Facet>
                            <Facet icon={<ListChecks size={16} className="text-emerald-500" />} title="Tuarua" subtitle="The correct ritual">
                                {(rec?.tuarua?.procedure?.length ?? 0) > 0
                                    ? <ol className="list-decimal list-inside space-y-1">{rec!.tuarua!.procedure!.map((s, i) => <li key={i}>{s}</li>)}</ol>
                                    : empty}
                                {(rec?.tuarua?.verification?.length ?? 0) > 0
                                    ? <div className="mt-3"><div className="text-[11px] uppercase tracking-wide font-semibold text-gray-400">Verification</div><ul className="list-disc list-inside">{rec!.tuarua!.verification!.map((v, i) => <li key={i}>{v}</li>)}</ul></div>
                                    : null}
                            </Facet>
                        </div>
                    </div>

                    {/* Training stage — agent learns the tool; in the final test a judge grades
                        each call. Agent (left) -> Tool (middle) -> Judge (right, validation only). */}
                    <div className="rounded-xl border border-gray-200 bg-gradient-to-b from-gray-50 to-white p-5">
                        <div className="text-[11px] uppercase tracking-wide font-semibold text-gray-500 mb-4 flex items-center gap-1.5">
                            <Activity size={13} /> Training stage
                            {running && <span className="text-gray-400 normal-case font-normal">· {phaseLabel}</span>}
                            {showJudge && <span className="ml-auto text-gray-400 normal-case font-normal flex items-center gap-1"><Gavel size={11} /> final test — the judge decides pass / fail</span>}
                        </div>
                        {/* items-center aligns the VISUALS on one centre line. The avatar boxes stay
                            short (their labels are positioned absolutely in StageCol, so they don't
                            add height) -> avatar centres line up with the tool card centre + flow. */}
                        <div className="flex items-center justify-center gap-2 md:gap-4">
                            {/* Agent — the learner (living white dot). waiting at rest, talking while it calls tools. */}
                            <StageCol label="Agent" sub={running ? 'calling tool' : 'learning'}>
                                <div className="h-20 w-20 flex items-center justify-center">
                                    <div style={{ transform: 'scale(2.1)' }}><AgentAvatar mode={agentMode} lite /></div>
                                </div>
                            </StageCol>

                            <FlowLink on={running} seed={job?.attempt ?? 0} back={running && lastEvent ? (lastEvent.match ? 'pass' : 'fail') : null} />

                            {/* Tool under test — a FIXED-size card (styled like the chat tool bubble:
                                status dot, name, output). Always the same size; only the content
                                updates and the output area scrolls, so it never jumps per call. */}
                            <StageCol>
                                <div className="w-[260px] h-[184px] rounded-lg border border-gray-200 bg-white shadow-sm overflow-hidden flex flex-col">
                                    <div className="flex items-center gap-2 p-2.5 border-b border-gray-100 shrink-0">
                                        <div className="relative flex h-7 w-7 items-center justify-center rounded-full border bg-gray-100 shrink-0">
                                            {toolStatus === 'running' ? (
                                                <span className="animate-pulse" style={{ width: 10, height: 10, backgroundColor: '#111827', borderRadius: '50%', boxShadow: '0 0 6px 2px rgba(0,0,0,0.25)' }} />
                                            ) : toolStatus === 'error' ? (
                                                <XCircle size={16} className="text-rose-500" />
                                            ) : (
                                                <CheckCircle2 size={16} className="text-emerald-500" />
                                            )}
                                        </div>
                                        <div className="min-w-0">
                                            <div className="text-xs font-semibold text-gray-800 truncate">{toolName}</div>
                                            <div className="text-[10px] text-gray-400">{toolStatus === 'running' ? 'Running…' : toolStatus === 'error' ? 'Failed' : 'Completed'}</div>
                                        </div>
                                    </div>
                                    <div className="flex-1 overflow-y-auto p-2 space-y-1.5 font-mono text-[10px] leading-relaxed">
                                        {toolArgs ? (
                                            <div className="rounded border border-gray-200 bg-gray-50 px-1.5 py-1 text-gray-700 break-all">{mainInput(toolArgs)}</div>
                                        ) : null}
                                        {toolResult ? (
                                            <div className="rounded border border-gray-200 bg-gray-50 px-1.5 py-1 text-gray-600 whitespace-pre-wrap break-all">{toolResult}</div>
                                        ) : (!toolArgs ? <div className="text-gray-300 italic">idle</div> : null)}
                                    </div>
                                </div>
                            </StageCol>

                            {/* Judge — only in the validation (final-test) phase. Inverted avatar:
                                thinking while awaiting, talking while it judges. */}
                            {showJudge && (
                                <>
                                    <FlowLink on={running && judgeActive} seed={(job?.attempt ?? 0) + 2} back={running && judgeActive && lastJudge ? (lastJudge.verdict === 'pass' ? 'pass' : 'fail') : null} />
                                    <StageCol
                                        label="Judge"
                                        footer={
                                            <div className="mt-0.5 flex items-center justify-center gap-1">
                                                {judgeVerdict === 'pass' ? (
                                                    <span className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-emerald-600"><CheckCircle2 size={12} /> pass</span>
                                                ) : judgeVerdict === 'fail' ? (
                                                    <span className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-rose-600"><XCircle size={12} /> fail</span>
                                                ) : (
                                                    <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400"><Gavel size={11} /> {inValidation ? 'judging…' : 'pass / fail'}</span>
                                                )}
                                            </div>
                                        }
                                    >
                                        <div className="h-20 w-20 flex items-center justify-center">
                                            <div style={{ transform: 'scale(2.1)' }}><AgentAvatar mode={judgeMode} invert lite /></div>
                                        </div>
                                    </StageCol>
                                </>
                            )}
                        </div>
                        {showJudge && judgeReason && (
                            <div className="mt-3 text-center text-[11px] text-gray-500 italic max-w-xl mx-auto">“{judgeReason}”</div>
                        )}
                        {job?.challenge ? (
                            <div className="mt-4 text-center text-[11px] text-gray-500">
                                Judge challenge: <span className="font-bold text-gray-800">{job.challenge.round_pass ?? 0}/{job.challenge.need ?? 3}</span> passed
                                {' · '}{job.challenge.total_fails ?? 0}/{job.challenge.max_fails ?? 10} fails
                                {job.challenge.passed ? ' — mastered.' : ''}
                            </div>
                        ) : job?.validate ? (
                            <div className="mt-4 text-center text-[11px] text-gray-500">
                                Last validation batch: <span className="font-bold text-gray-800">{job.validate.hits}/{job.validate.n}</span> predicted correctly
                                {job.validate.hits === job.validate.n ? ' — tool confirmed.' : ' — refining and retrying.'}
                            </div>
                        ) : null}
                    </div>

                    {error && <div className="text-sm text-rose-600">Error: {error}</div>}
                </div>
            </div>
        </div>
    );
}
