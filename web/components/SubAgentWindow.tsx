'use client';

import React, { useMemo, useRef, useEffect, useState } from 'react';
import { X, Terminal, FileCode, CheckCircle2, Circle, Loader2, Globe, Folder, GitBranch, Moon } from 'lucide-react';
import { cn } from '@/lib/utils';

/** Live project state streamed by the coding agent (`coder_state` event). */
export type CoderViewState = {
    fileTree: Array<{ name: string; size: number; status: string }>;
    git: { branch: string; dirty: number; commits: Array<{ sha: string; when: string; msg: string }> };
    tasks: Array<{ title: string; status: string }>;
    loop: number;
    taskProgress: string;
    linterOk: boolean;
    projectName: string;
    projectPath: string;
};

export type SubAgentWindowProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    mode?: 'overlay' | 'dock';
    agentName: string;
    status: string;
    presence?: 'online' | 'idle' | 'error';  // Direct presence from backend
    currentFile: string;
    codeContent: string;
    artifactFile?: string;
    artifactCode?: string;
    artifactStatus?: string;
    onArtifactChange?: (nextValue: string) => void;
    consoleLines?: string[];
    steps: Array<{
        id: string;
        title: string;
        description?: string;
        status: 'pending' | 'running' | 'completed';
        actions: Array<{ type: string; details: string }>;
    }>;
    browserFrame?: string;   // base64 JPEG screenshot from browser_agent
    browserUrl?: string;     // current page URL
    coder?: CoderViewState | null;  // enables the VS-Code view (coding agent only)
    [key: string]: any;
};

const formatSize = (bytes: number) =>
    bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;

const fileBadgeTone = (status: string) =>
    status === 'W' ? 'animate-pulse text-blue-600'
        : status === 'M' ? 'text-amber-500'
            : status === 'A' ? 'text-emerald-600'
                : 'text-transparent';

/** Tiny per-line syntax highlighter (tags, strings, comments, keywords) — no external lib. */
const TOKEN_RE = /(<!--.*?(?:-->|$)|\/\/.*$|"[^"]*"|'[^']*'|`[^`]*`|<\/?[a-zA-Z][^>\s]*|\b(?:const|let|var|function|return|if|else|for|while|class|import|export|new|async|await|def|self)\b)/g;

function highlightLine(line: string, dark: boolean): React.ReactNode[] {
    const nodes: React.ReactNode[] = [];
    let last = 0;
    let key = 0;
    let m: RegExpExecArray | null;
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(line)) !== null) {
        if (m.index > last) nodes.push(line.slice(last, m.index));
        const tok = m[0];
        const cls = tok.startsWith('<!--') || tok.startsWith('//')
            ? (dark ? 'italic text-gray-500' : 'italic text-gray-400')
            : tok.startsWith('"') || tok.startsWith("'") || tok.startsWith('`')
                ? (dark ? 'text-emerald-300' : 'text-emerald-600')
                : tok.startsWith('<')
                    ? (dark ? 'text-blue-300' : 'text-blue-600')
                    : (dark ? 'text-pink-300' : 'text-pink-600');
        nodes.push(<span key={key++} className={cls}>{tok}</span>);
        last = m.index + tok.length;
        if (m.index === TOKEN_RE.lastIndex) TOKEN_RE.lastIndex++;
    }
    if (last < line.length) nodes.push(line.slice(last));
    return nodes;
}

/**
 * Typewriter animation for console lines — strictly sequential.
 *
 * state:
 *   'done'    — already animated, show full text immediately
 *   'active'  — currently typing (only one at a time)
 *   'pending' — waiting in queue, invisible until its turn
 *
 * When the active line finishes typing it calls onDone() → parent advances
 * animatingIdx → next line becomes 'active'.
 */
function AnimatedConsoleLine({ text, state, onDone, onType }: {
    text: string;
    state: 'done' | 'active' | 'pending';
    onDone: () => void;
    onType?: () => void;
}) {
    const [typedLen, setTypedLen] = useState(() => state === 'done' ? text.length : 0);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const progressRef = useRef(state === 'done' ? text.length : 0);
    const onDoneRef = useRef(onDone);
    onDoneRef.current = onDone;
    const onTypeRef = useRef(onType);
    onTypeRef.current = onType;

    useEffect(() => {
        if (state === 'done') { progressRef.current = text.length; setTypedLen(text.length); return; }
        if (state === 'pending') { progressRef.current = 0; setTypedLen(0); return; }
        // state === 'active'. Streaming redraws GROW the text of the line that is
        // currently typing — continue from the previous progress instead of
        // restarting at 0, otherwise fast-growing lines never finish and the
        // animation queue (and with it the console scroll) stalls.
        let i = Math.min(progressRef.current, text.length);
        setTypedLen(i);
        // Long lines type in bigger steps so the queue keeps up.
        const step = Math.max(1, Math.ceil(text.length / 150));
        const tick = () => {
            i = Math.min(i + step, text.length);
            progressRef.current = i;
            setTypedLen(i);
            // Typing grows the container height without a consoleLines change —
            // let the parent keep the scroll pinned to the bottom.
            onTypeRef.current?.();
            if (i < text.length) {
                timerRef.current = setTimeout(tick, 7 + Math.random() * 5);
            } else {
                onDoneRef.current();
            }
        };
        timerRef.current = setTimeout(tick, 10);
        return () => { if (timerRef.current) clearTimeout(timerRef.current); };
    }, [state, text]);

    return (
        <div className="break-all whitespace-pre-wrap leading-5">
            {text.slice(0, typedLen)}
            {state === 'active' && typedLen < text.length && (
                <span className="inline-block w-[1px] h-[0.85em] bg-gray-400 align-middle ml-[1px] animate-pulse" />
            )}
        </div>
    );
}

const actionTone = (type: string) => {
    const normalized = type.toLowerCase();
    if (normalized === 'exec') return 'bg-gray-900 text-white';
    if (normalized === 'read') return 'bg-blue-100 text-blue-700';
    if (normalized === 'write') return 'bg-emerald-100 text-emerald-700';
    if (normalized === 'think') return 'bg-purple-100 text-purple-700';
    return 'bg-gray-100 text-gray-600';
};

export default function SubAgentWindow({
    isOpen,
    onClose,
    canClose = true,
    mode = 'overlay',
    agentName,
    status,
    presence,
    currentFile,
    codeContent,
    artifactFile,
    artifactCode,
    artifactStatus,
    onArtifactChange,
    consoleLines = [],
    steps,
    browserFrame,
    browserUrl,
    coder,
}: SubAgentWindowProps) {
    const displayFile = artifactFile ?? currentFile;
    const displayCode = artifactCode ?? codeContent;
    const displayStatus = status;
    const artifactStateLabel = artifactStatus ?? '';

    // Use presence from backend if available, otherwise infer from status text
    const statusLower = (status || '').toLowerCase();
    const hasRunningStep = steps.some(step => step.status === 'running');
    const inferredPresence = presence
        ? presence  // Use backend presence directly
        : statusLower.includes('error') || statusLower.includes('fail') || statusLower.includes('timeout')
            ? 'error'
            : hasRunningStep || statusLower.includes('online') || statusLower.includes('running')
                ? 'online'
                : 'idle';
    const presenceLabel = inferredPresence === 'online' ? 'Running' : inferredPresence === 'error' ? 'Error' : 'Idle';
    const presenceTone = inferredPresence === 'online'
        ? 'bg-emerald-500'
        : inferredPresence === 'error'
            ? 'bg-red-500'
            : 'bg-gray-400';
    const hasWorkflow = false;
    const codeLines = useMemo(() => (displayCode ? displayCode.split('\n') : []), [displayCode]);

    // Sequential console animation queue — only one line types at a time.
    // Initialise to current line count so existing lines on mount show instantly.
    const [animatingIdx, setAnimatingIdx] = useState(() => consoleLines.length);
    const advanceAnim = () => setAnimatingIdx(i => i + 1);

    // Smart auto-scroll: stick to bottom; pause when user scrolls up; resume when near bottom again.
    const consoleScrollRef = useRef<HTMLDivElement>(null);
    const userScrolledUpRef = useRef(false);

    const scrollConsoleToBottom = () => {
        if (consoleScrollRef.current) {
            consoleScrollRef.current.scrollTop = consoleScrollRef.current.scrollHeight;
        }
    };

    // Auto-scroll on new lines (unless user scrolled up)
    useEffect(() => {
        if (!userScrolledUpRef.current) {
            scrollConsoleToBottom();
        }
    }, [consoleLines]);

    // When a new screenshot arrives the image height may change, causing a layout shift that
    // fires a scroll event and falsely marks userScrolledUpRef=true. Reset on every new frame.
    useEffect(() => {
        userScrolledUpRef.current = false;
        scrollConsoleToBottom();
    }, [browserFrame]);

    const handleConsoleScroll = (e: React.UIEvent<HTMLDivElement>) => {
        const el = e.currentTarget;
        const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        userScrolledUpRef.current = distFromBottom > 48;
    };

    // Keep the console pinned to the bottom while lines type out — the
    // typewriter grows the content height without changing consoleLines.
    const keepConsolePinned = () => {
        if (!userScrolledUpRef.current) scrollConsoleToBottom();
    };
    useEffect(() => {
        keepConsolePinned();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [animatingIdx]);

    // ── VS-Code view (coding agent only) ──────────────────────────────────
    const hasCoderData = !!(coder && coder.fileTree && coder.fileTree.length > 0);
    const [editorDark, setEditorDark] = useState(false);

    // Bottom panel tabs (Console / Linter / Telemetry)
    const [activeConsoleTab, setActiveConsoleTab] = useState<'console' | 'linter' | 'telemetry'>('console');

    // Explorer file viewing: click a file -> load its content as a read-only
    // tab; clicking the live file (or the same tab again) returns to the
    // live stream view.
    const [openedFile, setOpenedFile] = useState<{ name: string; content: string } | null>(null);
    const editorScrollRef = useRef<HTMLDivElement>(null);
    const openedFileRef = useRef(openedFile);
    openedFileRef.current = openedFile;
    useEffect(() => {
        // Follow the live code stream like the typing cursor in an editor —
        // but never while the user is reading an explorer-opened file.
        if (!openedFileRef.current && editorScrollRef.current) {
            editorScrollRef.current.scrollTop = editorScrollRef.current.scrollHeight;
        }
    }, [codeContent]);
    useEffect(() => {
        // Jump to the top when switching to a freshly opened file.
        if (openedFile && editorScrollRef.current) {
            editorScrollRef.current.scrollTop = 0;
        }
    }, [openedFile]);
    const openFileFromExplorer = async (name: string) => {
        if (!coder?.projectPath) return;
        const liveName = (currentFile || '').split('/').pop() || '';
        if (name === liveName || openedFile?.name === name) {
            setOpenedFile(null);
            return;
        }
        try {
            const res = await fetch(`/api/file?path=${encodeURIComponent(`${coder.projectPath}/${name}`)}`);
            if (!res.ok) return;
            const text = await res.text();
            setOpenedFile({ name, content: text.slice(0, 120000) });
        } catch {
            /* file not readable - keep current view */
        }
    };
    // A new coder run starts -> drop the stale opened file
    useEffect(() => {
        if (!coder) setOpenedFile(null);
    }, [coder]);

    if (!isOpen && mode === 'overlay') return null;

    if (mode === 'dock' && hasCoderData && coder) {
        // ── VS-Code style view for the coding agent ───────────────────────
        // Left: header / file tabs / live editor / console. Right sidebar:
        // Explorer, Tasks, Source Control. Bottom: status bar.
        const isLive = inferredPresence === 'online';
        const activeName = (displayFile || '').split('/').pop() || '';
        const touched = coder.fileTree.filter(f => f.status);
        let fileTabs = (activeName && !touched.some(f => f.name === activeName)
            ? [{ name: activeName, size: 0, status: 'W' }, ...touched]
            : touched
        ).slice(0, 4);
        if (openedFile && !fileTabs.some(f => f.name === openedFile.name)) {
            fileTabs = [...fileTabs.slice(0, 3), { name: openedFile.name, size: 0, status: '' }];
        }
        const headSha = coder.git.commits[0]?.sha || '';
        // Editor shows either the live stream or an explorer-opened file
        const viewingName = openedFile?.name ?? activeName;
        const editorLines = openedFile ? openedFile.content.split('\n') : codeLines;
        const editorIsLive = !openedFile && isLive;

        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full flex-col">
                    <div className="flex min-h-0 flex-1">
                        {/* ── Left column: header, tabs, editor, console ── */}
                        <div className="flex min-w-0 flex-1 flex-col bg-[#F9FAFB]">
                            <div className="flex h-12 flex-none items-center justify-between border-b border-gray-200 bg-white px-4">
                                <div className="flex items-center gap-3">
                                    <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-700">
                                        <Terminal size={14} />
                                    </div>
                                    <div>
                                        <div className="text-xs font-semibold text-gray-900">{agentName}</div>
                                        <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                            <span className={cn("h-1.5 w-1.5 rounded-full", presenceTone)} />
                                            {displayStatus ? (
                                                <span className="text-gray-500">{displayStatus}</span>
                                            ) : (
                                                <span className="uppercase">{presenceLabel}</span>
                                            )}
                                        </div>
                                    </div>
                                </div>
                                <div className="flex items-center gap-2">
                                    <button
                                        onClick={() => setEditorDark(d => !d)}
                                        className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                        aria-label="Toggle editor theme"
                                        title="Editor-Theme umschalten"
                                    >
                                        <Moon size={13} />
                                    </button>
                                    <button
                                        onClick={onClose}
                                        className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                        aria-label="Close"
                                    >
                                        <X size={14} />
                                    </button>
                                </div>
                            </div>

                            {/* File tabs */}
                            <div className="flex h-8 flex-none items-end gap-1 border-b border-gray-100 bg-white px-2">
                                {fileTabs.map(tab => (
                                    <button
                                        key={tab.name}
                                        onClick={() => openFileFromExplorer(tab.name)}
                                        className={cn(
                                            'flex items-center gap-1.5 rounded-t-lg border border-b-0 px-3 py-1.5 font-mono text-[11px]',
                                            tab.name === viewingName
                                                ? (editorDark ? 'border-gray-200 bg-[#1e1e2e] font-semibold text-gray-200' : 'border-gray-100 bg-white font-semibold text-gray-900 shadow-sm')
                                                : 'border-transparent text-gray-400 hover:text-gray-600'
                                        )}
                                    >
                                        <span className="max-w-[130px] truncate">{tab.name}</span>
                                        {tab.status === 'W' && tab.name === activeName && isLive && (
                                            <span className="animate-pulse rounded bg-red-500 px-1 py-px text-[7px] font-extrabold tracking-wider text-white">LIVE</span>
                                        )}
                                        {tab.status === 'M' && <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />}
                                        {tab.status === 'A' && <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />}
                                    </button>
                                ))}
                                {fileTabs.length === 0 && (
                                    <span className="px-3 py-1.5 text-[11px] text-gray-300">No files yet</span>
                                )}
                            </div>

                            {/* Breadcrumb */}
                            <div className={cn(
                                "flex h-6 flex-none items-center gap-1.5 border-b px-4 text-[10px]",
                                editorDark ? "border-[#26263a] bg-[#1e1e2e] text-gray-500" : "border-gray-100 bg-white text-gray-400"
                            )}>
                                <span className="truncate">{coder.projectName}</span>
                                <span>&rsaquo;</span>
                                <span className={cn("font-semibold", editorDark ? "text-gray-300" : "text-gray-600")}>{viewingName || 'No active file'}</span>
                                {openedFile && (
                                    <span className="ml-1 rounded bg-gray-100 px-1.5 py-px text-[8px] font-semibold uppercase tracking-wide text-gray-400">Read</span>
                                )}
                            </div>

                            {/* Editor */}
                            <div
                                ref={editorScrollRef}
                                className={cn(
                                    "min-h-0 flex-1 overflow-auto py-2 font-mono text-[11.5px] leading-[1.6]",
                                    editorDark ? "bg-[#1e1e2e] text-gray-200" : "bg-white text-gray-800"
                                )}
                            >
                                {editorLines.length > 0 ? editorLines.map((line, i) => {
                                    const isCursorLine = editorIsLive && i === editorLines.length - 1;
                                    return (
                                        <div key={i} className={cn('flex', isCursorLine && (editorDark ? 'bg-[#26263a]' : 'bg-gray-50'))}>
                                            <span className={cn(
                                                'w-10 flex-none select-none pr-3 text-right text-[10px]',
                                                editorDark ? 'text-gray-600' : 'text-gray-300'
                                            )}>{i + 1}</span>
                                            <span className="min-w-0 flex-1 whitespace-pre pr-4">
                                                {highlightLine(line, editorDark)}
                                                {isCursorLine && (
                                                    <span className="ml-px inline-block h-[0.95em] w-[2px] animate-pulse bg-blue-500 align-middle" />
                                                )}
                                            </span>
                                        </div>
                                    );
                                }) : (
                                    <div className={cn("flex items-center gap-2 px-4 py-2 text-xs", editorDark ? "text-gray-600" : "text-gray-300")}>
                                        <Loader2 size={13} className="animate-spin opacity-60" />
                                        Waiting for code…
                                    </div>
                                )}
                            </div>

                            {/* Bottom panel: Console / Linter / Telemetry */}
                            <div className="flex h-[150px] flex-none flex-col border-t border-gray-200 bg-white">
                                <div className="flex h-7 flex-none items-center gap-4 border-b border-gray-100 px-4">
                                    {(['console', 'linter', 'telemetry'] as const).map(tab => (
                                        <button
                                            key={tab}
                                            onClick={() => setActiveConsoleTab(tab)}
                                            className={cn(
                                                'py-1 text-[9px] font-bold uppercase tracking-widest',
                                                activeConsoleTab === tab
                                                    ? 'border-b-2 border-blue-500 text-gray-600'
                                                    : 'text-gray-300 hover:text-gray-500'
                                            )}
                                        >
                                            {tab}
                                        </button>
                                    ))}
                                </div>
                                {activeConsoleTab === 'console' && (
                                    <div
                                        ref={consoleScrollRef}
                                        onScroll={handleConsoleScroll}
                                        className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4 py-2 font-mono text-[11px] text-gray-900"
                                    >
                                        {consoleLines.length > 0 ? (
                                            <div className="space-y-0.5">
                                                {consoleLines.map((line, index) => (
                                                    <AnimatedConsoleLine
                                                        key={`${index}-${line.slice(0, 20)}`}
                                                        text={line}
                                                        state={index < animatingIdx ? 'done' : index === animatingIdx ? 'active' : 'pending'}
                                                        onDone={advanceAnim}
                                                        onType={keepConsolePinned}
                                                    />
                                                ))}
                                            </div>
                                        ) : (
                                            <div className="flex items-center gap-2 text-gray-300">
                                                <Loader2 size={13} className="animate-spin opacity-50" />
                                                <span className="text-xs">Waiting for output…</span>
                                            </div>
                                        )}
                                    </div>
                                )}
                                {activeConsoleTab === 'linter' && (
                                    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-2 font-mono text-[11px]">
                                        <div className={cn('mb-1 flex items-center gap-2', coder.linterOk ? 'text-emerald-600' : 'text-red-600')}>
                                            <span className={cn('h-1.5 w-1.5 rounded-full', coder.linterOk ? 'bg-emerald-500' : 'bg-red-500')} />
                                            {coder.linterOk ? 'No active linter errors' : 'Linter errors active'}
                                        </div>
                                        {consoleLines.filter(l => /lint/i.test(l)).map((line, i) => (
                                            <div key={i} className="whitespace-pre-wrap break-all leading-5 text-gray-700">{line}</div>
                                        ))}
                                        {consoleLines.filter(l => /lint/i.test(l)).length === 0 && (
                                            <div className="text-gray-300">No linter output yet.</div>
                                        )}
                                    </div>
                                )}
                                {activeConsoleTab === 'telemetry' && (
                                    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-2 font-mono text-[11px] text-gray-700">
                                        {([
                                            ['Loop', String(coder.loop)],
                                            ['Tasks', coder.taskProgress || '–'],
                                            ['Files', `${coder.fileTree.length} (${coder.fileTree.filter(f => f.status === 'A').length} added, ${coder.fileTree.filter(f => f.status === 'M').length} modified)`],
                                            ['Commits', `${coder.git.commits.length}${headSha ? ` (head ${headSha})` : ''}`],
                                            ['Working tree', coder.git.dirty > 0 ? `${coder.git.dirty} uncommitted change(s)` : 'clean'],
                                            ['Project', coder.projectPath],
                                        ] as const).map(([k, v]) => (
                                            <div key={k} className="flex gap-2 leading-5">
                                                <span className="w-24 flex-none text-gray-400">{k}</span>
                                                <span className="min-w-0 flex-1 break-all">{v}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>

                        {/* ── Right sidebar: Explorer / Tasks / Source Control ── */}
                        <div className="flex w-[35%] min-w-[240px] max-w-[340px] flex-none flex-col border-l border-gray-200 bg-white">
                            {/* Explorer */}
                            <div className="flex min-h-0 flex-[1.2] flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">
                                    Explorer
                                    <span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">
                                        {coder.fileTree.length} files
                                    </span>
                                </div>
                                <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
                                    <div className="px-2 pb-1 text-[9px] text-gray-300">{coder.projectName}/</div>
                                    {coder.fileTree.map(f => (
                                        <button
                                            key={f.name}
                                            onClick={() => openFileFromExplorer(f.name)}
                                            className={cn(
                                                'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left',
                                                f.name === viewingName ? 'bg-blue-50' : 'hover:bg-gray-50'
                                            )}
                                        >
                                            <FileCode size={11} className="flex-none text-gray-400" />
                                            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-gray-700">{f.name}</span>
                                            <span className="flex-none text-[9px] text-gray-300">{formatSize(f.size)}</span>
                                            <span className={cn('w-3 flex-none text-center text-[9px] font-extrabold', fileBadgeTone(f.status))}>
                                                {f.status || '·'}
                                            </span>
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Tasks */}
                            <div className="flex min-h-0 flex-1 flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">
                                    Tasks
                                    {coder.taskProgress && (
                                        <span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">
                                            {coder.taskProgress}
                                        </span>
                                    )}
                                </div>
                                <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2.5 pb-2">
                                    {/* Real task plan streamed by the coder (coder_state.tasks);
                                        the generic heartbeat steps are only the fallback. */}
                                    {(coder.tasks.length > 0
                                        ? coder.tasks.map((t, i) => ({ id: `t${i}`, title: t.title, description: undefined as string | undefined, status: t.status }))
                                        : steps
                                    ).map(step => (
                                        <div
                                            key={step.id}
                                            className={cn(
                                                'flex items-start gap-2 rounded-lg border px-2.5 py-1.5',
                                                step.status === 'running' && 'border-blue-200 bg-white ring-1 ring-blue-50',
                                                step.status === 'completed' && 'border-gray-100 bg-gray-50',
                                                step.status === 'failed' && 'border-red-100 bg-red-50/40',
                                                (step.status === 'pending' || step.status === 'skipped') && 'border-gray-100 bg-white'
                                            )}
                                        >
                                            <span className={cn(
                                                'mt-px flex h-4 w-4 flex-none items-center justify-center rounded-full',
                                                step.status === 'running' && 'bg-blue-100 text-blue-600',
                                                step.status === 'completed' && 'bg-emerald-100 text-emerald-600',
                                                step.status === 'failed' && 'bg-red-100 text-red-600',
                                                (step.status === 'pending' || step.status === 'skipped') && 'bg-gray-100 text-gray-400'
                                            )}>
                                                {step.status === 'running' && <Loader2 size={9} className="animate-spin" />}
                                                {step.status === 'completed' && <CheckCircle2 size={9} />}
                                                {step.status === 'failed' && <X size={9} />}
                                                {(step.status === 'pending' || step.status === 'skipped') && <Circle size={7} />}
                                            </span>
                                            <span className="min-w-0 flex-1">
                                                <span className="block text-[11px] font-semibold leading-tight text-gray-800">{step.title}</span>
                                                {step.description && (
                                                    <span className="block truncate text-[9.5px] text-gray-400">{step.description}</span>
                                                )}
                                            </span>
                                        </div>
                                    ))}
                                    {coder.tasks.length === 0 && steps.length === 0 && (
                                        <div className="px-2 text-[10px] text-gray-300">Planning…</div>
                                    )}
                                </div>
                            </div>

                            {/* Source control */}
                            <div className="flex min-h-0 flex-1 flex-col">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">
                                    Source Control
                                    <span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">
                                        {coder.git.commits.length} commits
                                    </span>
                                </div>
                                <div className="min-h-0 flex-1 overflow-y-auto px-2.5 pb-2.5">
                                    <div className="flex items-center gap-2 px-1.5 pb-1.5 text-[10.5px] text-gray-500">
                                        <GitBranch size={10} className="flex-none" />
                                        <span>{coder.git.branch || 'no branch'}</span>
                                        <span className={cn(
                                            'rounded-md px-1.5 py-px text-[8.5px] font-semibold',
                                            coder.git.dirty > 0 ? 'bg-amber-50 text-amber-700' : 'bg-emerald-50 text-emerald-700'
                                        )}>
                                            {coder.git.dirty > 0 ? `${coder.git.dirty} changes` : 'clean'}
                                        </span>
                                    </div>
                                    {coder.git.commits.map((c, i) => (
                                        <div key={c.sha} className="flex items-baseline gap-2 rounded-md px-1.5 py-1 hover:bg-gray-50">
                                            <span className="flex-none font-mono text-[9.5px] text-blue-600">{c.sha}</span>
                                            <span className={cn('min-w-0 flex-1 truncate text-[10.5px] text-gray-700', i === 0 && 'font-semibold')}>{c.msg}</span>
                                            <span className="flex-none text-[8.5px] text-gray-300">{c.when}</span>
                                        </div>
                                    ))}
                                    <div className="mx-1 mt-2 rounded-lg border border-dashed border-gray-200 px-2.5 py-1.5 text-[9px] leading-relaxed text-gray-400">
                                        Rollback: einfach im Chat sagen — &quot;rollback auf &lt;id&gt;&quot;
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* ── Status bar ── */}
                    <div className="flex h-6 flex-none items-center bg-[#1f2335] text-[10px] text-[#c8d0e8]">
                        <div className="flex h-full items-center bg-blue-600 px-3 font-bold text-white">VAF</div>
                        <div className="flex h-full items-center gap-1 px-2.5">
                            <GitBranch size={9} />
                            {coder.git.branch || '–'}
                        </div>
                        {headSha && <div className="flex h-full items-center px-2.5 font-mono">@ {headSha}</div>}
                        <div className="flex h-full items-center gap-1.5 px-2.5">
                            <span className={cn('h-1.5 w-1.5 rounded-full', coder.linterOk ? 'bg-emerald-400' : 'bg-red-400')} />
                            Linter: {coder.linterOk ? 'passed' : 'errors'}
                        </div>
                        {coder.taskProgress && <div className="flex h-full items-center px-2.5">{coder.taskProgress}</div>}
                        <div className="ml-auto flex h-full items-center px-2.5">Loop {coder.loop}</div>
                        <div className="flex h-full items-center gap-1.5 px-3">
                            <span className={cn('h-1.5 w-1.5 rounded-full', presenceTone)} />
                            {presenceLabel}
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    if (mode === 'dock') {
        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full">
                {hasWorkflow && (
                    <div className="flex w-[36%] min-w-[280px] flex-col border-r border-gray-200 bg-white">
                        <div className="flex h-12 items-center justify-between border-b border-gray-100 px-4">
                            <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Workflow</span>
                        </div>

                        <div className="relative flex-1 overflow-y-auto px-4 py-5">
                            <div className="absolute bottom-5 left-5 top-5 w-px bg-gray-200" />
                            <div className="space-y-4">
                                {steps.map((step) => (
                                    <div key={step.id} className="relative pl-7">
                                        <div
                                            className={cn(
                                                'absolute left-[2px] top-2 flex h-5 w-5 items-center justify-center rounded-full border border-white shadow-sm',
                                                step.status === 'running' && 'bg-blue-100 text-blue-600',
                                                step.status === 'completed' && 'bg-emerald-100 text-emerald-600',
                                                step.status === 'pending' && 'bg-gray-100 text-gray-400'
                                            )}
                                        >
                                            {step.status === 'running' && <Loader2 size={12} className="animate-spin" />}
                                            {step.status === 'completed' && <CheckCircle2 size={12} />}
                                            {step.status === 'pending' && <Circle size={10} />}
                                        </div>

                                        <div
                                            className={cn(
                                                'rounded-xl border px-3 py-2.5 transition',
                                                step.status === 'running' && 'border-blue-200 bg-white ring-1 ring-blue-50',
                                                step.status === 'completed' && 'border-gray-100 bg-gray-50',
                                                step.status === 'pending' && 'border-gray-100 bg-white'
                                            )}
                                        >
                                            <div className="flex flex-col gap-1">
                                                <div className="text-[13px] font-semibold text-gray-900">{step.title}</div>
                                                {step.description && (
                                                    <div className="text-[11px] text-gray-500">{step.description}</div>
                                                )}
                                            </div>

                                            {step.actions.length > 0 && (
                                                <div className="mt-2 flex flex-wrap items-center gap-2">
                                                    {step.actions.map((action, index) => (
                                                        <div key={index} className="flex items-center gap-2 text-xs">
                                                            <span
                                                                className={cn(
                                                                    'rounded-[4px] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                                                                    actionTone(action.type)
                                                                )}
                                                            >
                                                                {action.type}
                                                            </span>
                                                            <span className="max-w-[190px] truncate font-mono text-gray-600">
                                                                {action.details}
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                )}

                <div className={cn("flex flex-1 flex-col bg-[#F9FAFB]", !hasWorkflow && "rounded-l-2xl")}>
                    <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4">
                        <div className="flex items-center gap-3">
                            <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-700">
                                <Terminal size={14} />
                            </div>
                            <div>
                                <div className="text-xs font-semibold text-gray-900">{agentName}</div>
                                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 rounded-full", presenceTone)} />
                                    {displayStatus ? (
                                        <span className="text-gray-500">{displayStatus}</span>
                                    ) : (
                                        <span className="uppercase">{presenceLabel}</span>
                                    )}
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center gap-2">
                            {artifactStateLabel && (
                                <span className="text-[10px] uppercase tracking-wide text-gray-400">
                                    {artifactStateLabel}
                                </span>
                            )}
                            <button
                                onClick={onClose}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Close"
                            >
                                <X size={14} />
                            </button>
                        </div>
                    </div>

                    <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500">
                        <span className="rounded-md bg-gray-100 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500">
                            {onArtifactChange ? 'Edit' : 'Read'}
                        </span>
                        <span className="truncate font-mono text-[11px]">{displayFile || 'No active file'}</span>
                    </div>

                    <div className="flex flex-1 flex-col overflow-hidden">
                        {/* Browser live viewport — edge-to-edge, no wrapper border */}
                        {browserFrame && (
                            <div className="flex-none border-b border-gray-100">
                                {/* URL bar */}
                                <div className="flex h-7 items-center gap-2 bg-gray-50 px-3">
                                    <Globe size={10} className="shrink-0 text-gray-400" />
                                    <span className="flex-1 truncate font-mono text-[10px] text-gray-500">
                                        {browserUrl || 'Loading…'}
                                    </span>
                                    <div className="flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-[8px] font-semibold uppercase tracking-wide text-red-500">
                                        <span className="h-1 w-1 animate-pulse rounded-full bg-red-500" />
                                        Live
                                    </div>
                                </div>
                                {/* Screenshot — full width, proportional height, no crop */}
                                <img
                                    src={`data:image/jpeg;base64,${browserFrame}`}
                                    alt="Browser live view"
                                    className="block w-full"
                                    draggable={false}
                                    onLoad={() => { if (!userScrolledUpRef.current) scrollConsoleToBottom(); }}
                                />
                            </div>
                        )}

                        {/* Console — fills all remaining space, flat (no extra border box) */}
                        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
                            <div className="flex h-7 items-center border-b border-gray-100 bg-gray-50/50 px-3 text-[10px] text-gray-400">
                                <div className="flex-1 truncate text-center font-mono">
                                    Console
                                </div>
                            </div>
                            <div
                                ref={consoleScrollRef}
                                onScroll={handleConsoleScroll}
                                className="flex-1 overflow-y-auto overflow-x-hidden bg-white px-4 py-4 font-mono text-xs text-gray-900"
                            >
                                {consoleLines.length > 0 ? (
                                    <div className="space-y-0.5">
                                        {consoleLines.map((line, index) => (
                                            <AnimatedConsoleLine
                                                key={`${index}-${line.slice(0, 20)}`}
                                                text={line}
                                                state={index < animatingIdx ? 'done' : index === animatingIdx ? 'active' : 'pending'}
                                                onDone={advanceAnim}
                                                    onType={keepConsolePinned}
                                            />
                                        ))}
                                    </div>
                                ) : (
                                    <div className="flex items-center gap-2 text-gray-300">
                                        <Loader2 size={14} className="animate-spin opacity-50" />
                                        <span className="text-xs">Waiting for output…</span>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            </div>
        );
    }

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl">
                <div className="flex w-[35%] min-w-[320px] flex-col border-r border-gray-200 bg-white">
                    <div className="flex h-14 items-center justify-between border-b border-gray-100 px-5">
                        <span className="text-sm font-semibold text-gray-700">Workflow</span>
                    </div>

                    <div className="relative flex-1 overflow-y-auto px-5 py-6">
                        <div className="absolute bottom-6 left-7 top-6 w-px bg-gray-200" />
                        <div className="space-y-5">
                            {steps.map((step) => (
                                <div key={step.id} className="relative pl-8">
                                    <div
                                        className={cn(
                                            'absolute left-[6px] top-2 flex h-6 w-6 items-center justify-center rounded-full border border-white shadow-sm',
                                            step.status === 'running' && 'bg-blue-100 text-blue-600',
                                            step.status === 'completed' && 'bg-emerald-100 text-emerald-600',
                                            step.status === 'pending' && 'bg-gray-100 text-gray-400'
                                        )}
                                    >
                                        {step.status === 'running' && <Loader2 size={14} className="animate-spin" />}
                                        {step.status === 'completed' && <CheckCircle2 size={14} />}
                                        {step.status === 'pending' && <Circle size={12} />}
                                    </div>

                                    <div
                                        className={cn(
                                            'rounded-xl border px-4 py-3 shadow-sm transition',
                                            step.status === 'running' && 'border-blue-200 bg-white ring-1 ring-blue-100',
                                            step.status === 'completed' && 'border-gray-100 bg-gray-50',
                                            step.status === 'pending' && 'border-gray-100 bg-white'
                                        )}
                                    >
                                        <div className="flex flex-col gap-1">
                                            <div className="text-sm font-semibold text-gray-900">{step.title}</div>
                                            {step.description && (
                                                <div className="text-xs text-gray-500">{step.description}</div>
                                            )}
                                        </div>

                                        {step.actions.length > 0 && (
                                            <div className="mt-3 flex flex-wrap items-center gap-2">
                                                {step.actions.map((action, index) => (
                                                    <div key={index} className="flex items-center gap-2 text-xs">
                                                        <span
                                                            className={cn(
                                                                'rounded-[4px] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                                                                actionTone(action.type)
                                                            )}
                                                        >
                                                            {action.type}
                                                        </span>
                                                        <span className="max-w-[190px] truncate font-mono text-gray-600">
                                                            {action.details}
                                                        </span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>

                <div className="flex flex-1 flex-col bg-[#F9FAFB]">
                    <div className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-6">
                        <div className="flex items-center gap-3">
                            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-black text-white shadow-sm">
                                <Terminal size={18} />
                            </div>
                            <div>
                                <div className="text-sm font-semibold text-gray-900">{agentName}</div>
                                <div className="flex items-center gap-2 text-xs text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 rounded-full", presenceTone)} />
                                    {status ? (
                                        <span className="text-gray-500">{status}</span>
                                    ) : (
                                        <span className="uppercase">{presenceLabel}</span>
                                    )}
                                </div>
                            </div>
                        </div>
                        <button
                            onClick={onClose}
                            className="rounded-full p-2 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                            aria-label="Close"
                        >
                            <X size={16} />
                        </button>
                    </div>

                    <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-6 text-xs text-gray-500">
                        <FileCode size={12} />
                        <span className="truncate font-mono">{currentFile || 'No active file'}</span>
                    </div>

                    <div className="flex flex-1 flex-col overflow-hidden p-6 gap-4">
                        {/* Browser live viewport — natural aspect ratio, no bars */}
                        {browserFrame && (
                            <div className="flex-none overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                                {/* URL bar */}
                                <div className="flex h-8 items-center gap-2 border-b border-gray-100 bg-gray-50 px-4">
                                    <Globe size={12} className="shrink-0 text-gray-400" />
                                    <span className="flex-1 truncate font-mono text-xs text-gray-500">
                                        {browserUrl || 'Loading…'}
                                    </span>
                                    <div className="flex items-center gap-1.5 rounded-full bg-red-50 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-red-500">
                                        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-red-500" />
                                        Live
                                    </div>
                                </div>
                                {/* Screenshot — full width, proportional height, no crop */}
                                <img
                                    src={`data:image/jpeg;base64,${browserFrame}`}
                                    alt="Browser live view"
                                    className="block w-full"
                                    draggable={false}
                                    onLoad={() => { if (!userScrolledUpRef.current) scrollConsoleToBottom(); }}
                                />
                            </div>
                        )}

                        {/* Code / Console — fills remaining space */}
                        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                            <div className="flex h-9 items-center border-b border-gray-100 bg-gray-50 px-4 text-xs font-medium text-gray-600">
                                {currentFile ? currentFile.split('/').pop() : 'Console'}
                            </div>
                            <div
                                ref={consoleScrollRef}
                                onScroll={handleConsoleScroll}
                                className="flex-1 overflow-y-auto overflow-x-hidden"
                            >
                                {codeContent ? (
                                    <div className="flex text-sm leading-6 text-gray-800">
                                        <div className="select-none border-r bg-gray-50/70 px-4 py-4 text-right font-mono text-xs text-gray-400">
                                            {codeLines.map((_, index) => (
                                                <div key={`line-${index}`}>{index + 1}</div>
                                            ))}
                                        </div>
                                        <pre className="flex-1 whitespace-pre px-4 py-4 font-mono">
                                            {codeContent}
                                        </pre>
                                    </div>
                                ) : consoleLines && consoleLines.length > 0 ? (
                                    <div className="space-y-0.5 px-4 py-4 font-mono text-xs text-gray-900">
                                        {consoleLines.map((line, index) => (
                                            <AnimatedConsoleLine
                                                key={`${index}-${line.slice(0, 20)}`}
                                                text={line}
                                                state={index < animatingIdx ? 'done' : index === animatingIdx ? 'active' : 'pending'}
                                                onDone={advanceAnim}
                                                    onType={keepConsolePinned}
                                            />
                                        ))}
                                    </div>
                                ) : (
                                    <div className="flex h-full flex-col items-center justify-center gap-2 text-gray-300">
                                        <Loader2 size={28} className="animate-spin opacity-50" />
                                        <span className="text-xs">Waiting for output…</span>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}