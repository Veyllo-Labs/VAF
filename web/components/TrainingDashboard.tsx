'use client';

import React, { useCallback, useEffect, useState } from 'react';
import {
    X, Activity, Zap, AlertTriangle, CheckCircle2, Clock, Eye, ShieldAlert, ListChecks, Loader2,
} from 'lucide-react';

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

export default function TrainingDashboard({ toolName, onClose, onStateChange }: { toolName: string; onClose: () => void; onStateChange?: (tool: string, state: string) => void }) {
    const [loading, setLoading] = useState(true);
    const [state, setState] = useState<string>('unlearned');
    const [rec, setRec] = useState<ToolKnowledge | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [job, setJob] = useState<any>(null);

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
            } catch { /* ignore transient poll errors */ }
            if (alive) timer = setTimeout(poll, 1500);
        };
        poll();
        return () => { alive = false; if (timer) clearTimeout(timer); };
    }, [toolName, loadRecord]);

    const success = rec?.success ?? 0;
    const fail = rec?.fail ?? 0;
    const totalRuns = success + fail;
    const errorRate = totalRuns > 0 ? Math.round((fail / totalRuns) * 100) : null;
    const predicts = rec?.predict_records ?? [];
    const predHits = predicts.filter((p) => p.match).length;
    const empty = <span className="text-gray-400 italic">— empty —</span>;

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
                            <span>Training… attempt {job.attempt ?? 0}/{job.max_attempts ?? '?'} · correct predictions {job.hits ?? 0}</span>
                        </div>
                    ) : job?.state === 'done' ? (
                        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 flex items-center gap-2">
                            <CheckCircle2 size={16} className="shrink-0" />
                            <span>Training complete — status: {job.status ?? state}, confidence {Math.round((((job.confidence ?? rec?.confidence) ?? 0) as number) * 100)}%.</span>
                        </div>
                    ) : job?.state === 'skipped' ? (
                        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 flex items-center gap-2">
                            <AlertTriangle size={16} className="shrink-0" />
                            <span>Skipped: {job.reason || 'not eligible for training'}.</span>
                        </div>
                    ) : (
                        <div className="rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-600 flex items-center gap-2">
                            <Activity size={16} className="shrink-0" />
                            <span>Showing the stored tool_knowledge record. Use &quot;Train tool now&quot; to run a fresh pass.</span>
                        </div>
                    )}

                    {/* Metrics */}
                    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
                        <Metric icon={<Activity size={13} />} label="Status" value={STATE_LABEL[state] ?? state} />
                        <Metric icon={<CheckCircle2 size={13} />} label="Confidence" value={rec ? `${Math.round((rec.confidence ?? 0) * 100)}%` : '—'} />
                        <Metric icon={<Clock size={13} />} label="Duration" value="—" hint="live during a run" />
                        <Metric icon={<Zap size={13} />} label="Uses" value={rec?.uses ?? 0} />
                        <Metric icon={<AlertTriangle size={13} />} label="Error rate" value={errorRate === null ? '—' : `${errorRate}%`} hint={totalRuns ? `${fail}/${totalRuns} failed` : 'no runs yet'} />
                        <Metric icon={<CheckCircle2 size={13} />} label="Predictions" value={predicts.length ? `${predHits}/${predicts.length}` : '—'} hint="correct / total" />
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
                            const items = live.length ? live : fromRec;
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

                    {error && <div className="text-sm text-rose-600">Error: {error}</div>}
                </div>
            </div>
        </div>
    );
}
