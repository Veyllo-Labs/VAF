'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { Fragment, useMemo, useRef, useEffect, useLayoutEffect, useState } from 'react';
import { X, Terminal, FileCode, CheckCircle2, Circle, Loader2, Globe, Folder, FolderOpen, GitBranch, Moon, Printer, Search, Pencil, HardDrive, Cloud, Lock, FileText, Image as ImageIcon, Film, Archive, ChevronLeft, ChevronRight, RotateCw } from 'lucide-react';
import { cn } from '@/lib/utils';

/** Live research state streamed by the research agent (`research_state` event). */
export type ResearchViewState = {
    topic: string;
    stage: string;
    sections: Array<{ title: string; status: string; words: number; targetWords: number }>;
    sectionsHtml: string[];
    sources: Array<{ url: string; title: string; domain: string }>;
    wordsTarget: number;
    loop: number;
};

/** Live document state streamed by the document agent (`document_state` event). */
export type DocumentViewState = {
    title: string;
    format: string;       // docx | pdf | md | txt
    docType: string;      // contract | report | letter | …
    stage: string;
    sections: Array<{ title: string; status: string; words: number; targetWords: number }>;
    sectionsHtml: string[];
    placeholders: Array<{ name: string; value: string; source: string }>;  // source: memory|chat|auto|open
    wordsTarget: number;
    savePath: string;
    loop: number;
};

/** Live read-only state streamed by the librarian agent (`librarian_state` event).
 *  The librarian only reads — file system, folder sizes, storage and Google Drive. */
export type LibrarianViewState = {
    root: string;
    stage: string;            // scanning | done
    readOnly: boolean;
    totalSize: number;        // bytes
    totalFiles: number;
    totalFolders: number;
    entries: Array<{ name: string; type: string; sizeBytes: number; items?: number; gd?: boolean }>;
    topFolders: Array<{ name: string; sizeBytes: number }>;
    drives: Array<{ name: string; kind: 'disk' | 'home' | 'cloud'; usedBytes: number; totalBytes: number; connected?: boolean }>;
    search: { query: string; hits?: number | null } | null;
    activity: Array<{ cls: string; text: string }>;
    // Set when the agent is browsing/searching a concrete folder → the explorer opens it
    // and lists its files; null → the disk-usage overview is shown.
    currentFolder: {
        path: string;
        name: string;
        fileCount: number;
        folderCount: number;
        totalSize: number;
        types: Array<{ type: string; count: number }>;
        entries: Array<{ name: string; type: string; isDir: boolean; sizeBytes: number; items?: number; modified: string; match?: boolean }>;
    } | null;
};

/** Live project state streamed by the coding agent (`coder_state` event). */
export type CoderViewState = {
    fileTree: Array<{ name: string; size: number; status: string }>;
    git: { branch: string; dirty: number; commits: Array<{ sha: string; when: string; msg: string }> };
    /** Unified git diff per changed file (vs the run's start), for the red/green Diff view. */
    diffs?: Record<string, string>;
    tasks: Array<{ title: string; status: string }>;
    loop: number;
    taskProgress: string;
    linterOk: boolean;
    projectName: string;
    projectPath: string;
};

/** Live state streamed by the browser agent (`browser_state` event) for the browser
 *  window's dock; the screenshot itself arrives separately as browserFrame/browserUrl. */
export type BrowserViewState = {
    task: string;
    url: string;
    status: string;          // running | done
    step: number;
    maxSteps: number;
    vision: string;          // aus | auto | aktiv
    actions: Array<{ verb: string; text: string; status: string }>;  // verb: nav|click|type|read|scroll
    history: string[];       // visited URLs, in order
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
    research?: ResearchViewState | null;  // enables the paper view (research agent only)
    document?: DocumentViewState | null;  // enables the document view (document agent only)
    librarian?: LibrarianViewState | null;  // enables the read-only explorer view (librarian agent only)
    browser?: BrowserViewState | null;  // enables the live browser window (browser agent only)
    // Kind known the moment the main agent CALLS the sub-agent (from the tool name) -> the matching custom
    // view renders IMMEDIATELY in a loading shell, instead of waiting for streamed data.
    agentKind?: 'coder' | 'research' | 'document' | 'librarian' | 'browser' | null;
    [key: string]: any;
};

// Empty-but-valid shells so a custom view can render a loading state the instant the kind is known, before
// any <x>_state has streamed. Each contains exactly the fields its view reads (coder.git is fully populated
// because the VS-Code view reads coder.git.commits/.branch/.dirty non-optionally; librarian starts in
// overview/"Scanning" mode with currentFolder null).
const EMPTY_CODER: CoderViewState = {
    fileTree: [], git: { branch: '', dirty: 0, commits: [] }, tasks: [],
    loop: 0, taskProgress: '', linterOk: true, projectName: '', projectPath: '',
};
const EMPTY_RESEARCH: ResearchViewState = {
    topic: '', stage: '', sections: [], sectionsHtml: [], sources: [], wordsTarget: 0, loop: 0,
};
const EMPTY_DOCUMENT: DocumentViewState = {
    title: '', format: 'docx', docType: 'report', stage: '', sections: [], sectionsHtml: [],
    placeholders: [], wordsTarget: 0, savePath: '', loop: 0,
};
const EMPTY_LIBRARIAN: LibrarianViewState = {
    root: '~', stage: '', readOnly: true, totalSize: 0, totalFiles: 0, totalFolders: 0,
    entries: [], topFolders: [], drives: [], search: null, activity: [], currentFolder: null,
};
const EMPTY_BROWSER: BrowserViewState = {
    task: '', url: '', status: 'running', step: 0, maxSteps: 0, vision: 'auto', actions: [], history: [],
};

const SUBAGENT_KIND_LABEL: Record<string, string> = {
    coder: 'Coder', research: 'Research Agent', document: 'Document Agent',
    librarian: 'Librarian', browser: 'Browser Agent',
};

/** Slim header strip shown while a custom view has opened (kind known) but no data has streamed yet. */
function StartingBanner({ label }: { label: string }) {
    return (
        <div className="flex items-center gap-2 border-b border-gray-100 bg-gray-50/70 px-3 py-1.5 text-[11.5px] text-gray-500">
            <Loader2 size={12} className="animate-spin text-gray-400" />
            <span>Starting {label} — waiting for the agent…</span>
        </div>
    );
}

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

// ── DIN A4 paper preview ──────────────────────────────────────────────────────
// The report flows through a CSS multi-column container whose column size is
// exactly the A4 content box (210mm − 2×20mm margins). The browser's own layout
// engine places the breaks (line-accurate, inline markup preserved); every
// column is then shown as one fixed 210×297mm sheet. Printing reuses the same
// sheet markup and CSS via `@page size: A4; margin: 0`, so the preview and the
// printout are identical by construction.
const MM_PX = 96 / 25.4;                                   // CSS: 1mm = 96/25.4 px, device-independent
const A4_PAGE_W = 210 * MM_PX;
const A4_PAGE_H = 297 * MM_PX;
const A4_CONTENT_W = Math.floor((210 - 2 * 20) * MM_PX);   // content box at 20mm margins
const A4_CONTENT_H = Math.floor((297 - 2 * 20) * MM_PX);
const A4_COL_GAP = 48;
const A4_FLOW_STEP = A4_CONTENT_W + A4_COL_GAP;

const A4_PAGE_CSS = `
.vaf-a4-page { width: 210mm; height: 297mm; padding: 20mm; box-sizing: border-box; background: #ffffff; overflow: hidden; }
.vaf-a4-clip { width: ${A4_CONTENT_W}px; height: ${A4_CONTENT_H}px; overflow: hidden; }
.vaf-a4-flow { width: ${A4_CONTENT_W}px; height: ${A4_CONTENT_H}px; column-width: ${A4_CONTENT_W}px; column-gap: ${A4_COL_GAP}px; column-fill: auto; }
`;

// Typography is fully self-contained (explicit font stack, no app classes) so the
// print document — which loads none of the app CSS — renders the same glyphs and
// line breaks as the preview.
const RESEARCH_PAPER_CSS = `
.vaf-paper { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #34353f; overflow-wrap: anywhere; }
.vaf-paper .rpt-label { font-size: 10px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: #7c3aed; }
.vaf-paper h1 { font-family: Georgia, 'Times New Roman', serif; font-size: 26px; line-height: 1.25; color: #111827; font-weight: 400; margin: 8px 0 0; }
.vaf-paper .rpt-meta { font-size: 11.5px; color: #9ca3af; border-bottom: 1px solid #f3f4f6; padding-bottom: 16px; margin: 8px 0 24px; }
.vaf-paper h2 { font-family: Georgia, 'Times New Roman', serif; font-size: 17px; color: #1d1e26; margin: 24px 0 8px; break-after: avoid-column; }
.vaf-paper p { font-size: 13.5px; line-height: 1.75; color: #34353f; margin: 0 0 10px; }
.vaf-paper ul, .vaf-paper ol { font-size: 13.5px; line-height: 1.7; color: #34353f; margin: 0 0 10px 20px; padding: 0; }
.vaf-paper li { break-inside: avoid-column; }
.vaf-paper a { color: #7c3aed; text-decoration: none; }
.vaf-paper a:hover { text-decoration: underline; }
.vaf-paper em { color: #6b7280; }
.vaf-paper img, .vaf-paper table { max-width: 100%; }
.vaf-typing-caret { display: inline-block; width: 2px; height: 1em; background: #8b5cf6; vertical-align: middle; margin-left: 1px; animation: vafCaretBlink 1s step-end infinite; }
@keyframes vafCaretBlink { 50% { opacity: 0; } }
.vaf-paper .cite { font-size: 10px; color: #7c3aed; background: #f3e8ff; border-radius: 4px; padding: 0 4px; margin-left: 2px; vertical-align: super; font-family: Consolas, monospace; }
.vaf-paper .pending-note { margin-top: 22px; padding: 13px 16px; border: 1px dashed #e5e7eb; border-radius: 9px; font-size: 12px; color: #9ca3af; }
.vaf-paper .searching-note { display: flex; gap: 9px; align-items: center; }
.vaf-paper .searching-note .spin { display: inline-block; animation: vafSpin 1s linear infinite; color: #7c3aed; }
@keyframes vafSpin { to { transform: rotate(360deg); } }
`;

const escapeHtml = (s: string) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const buildResearchHeaderHtml = (topic: string, metaLine: string) =>
    `<div class="rpt-label">Research Report</div><h1>${escapeHtml(topic)}</h1><div class="rpt-meta">${escapeHtml(metaLine)}</div>`;

// Document-agent paper: teal accent + placeholder chips (instead of citations).
const DOCUMENT_PAPER_CSS = `
.vaf-paper .doc-label { font-size: 10px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: #0f766e; }
.vaf-paper .ph { font-size: 11.5px; color: #0f766e; background: #ccfbf1; border: 1px solid #99f6e4; border-radius: 4px; padding: 0 4px; font-family: Consolas, monospace; }
.vaf-paper.vaf-doc .vaf-typing-caret { background: #0d9488; }
.vaf-paper.vaf-doc .searching-note .spin { color: #0d9488; }
`;

const buildDocHeaderHtml = (title: string, docType: string, metaLine: string) =>
    `<div class="doc-label">${escapeHtml(docType)}</div><h1>${escapeHtml(title)}</h1><div class="rpt-meta">${escapeHtml(metaLine)}</div>`;

/** Highlights `{{PLACEHOLDER}}` tokens as teal chips (text nodes only, never tags). */
const decoratePlaceholders = (html: string) =>
    html.split(/(<[^>]+>)/).map(part =>
        part.startsWith('<') ? part : part.replace(/\{\{([A-ZÄÖÜ0-9_]+)\}\}/g, '<span class="ph">{{$1}}</span>')
    ).join('');

/** Renders [n] citation markers as the mockup's superscript chips (text nodes only,
 *  never inside tags/attributes). The numbers reference the global source list. */
const decorateCitations = (html: string) =>
    html.split(/(<[^>]+>)/).map(part =>
        part.startsWith('<') ? part : part.replace(/\[(\d{1,3})\]/g, '<span class="cite">[$1]</span>')
    ).join('');

/**
 * Print the report exactly as previewed: a hidden same-origin iframe gets the
 * identical sheet markup and CSS, re-runs the same column measurement, and
 * prints each sheet as one A4 page. The iframe isolates the print from the
 * app shell (whose overflow containers would clip every page after the first).
 */
function printResearchReport(topic: string, metaLine: string, sectionsHtml: string[],
    opts?: { headerHtml?: string; decorate?: (h: string) => string; extraCss?: string }) {
    const decorate = opts?.decorate ?? decorateCitations;
    const header = opts?.headerHtml ?? buildResearchHeaderHtml(topic, metaLine);
    const fullHtml = header + decorate(sectionsHtml.join(''));
    const doc = `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(topic)}</title><style>
@page { size: A4; margin: 0; }
html, body { margin: 0; padding: 0; background: #ffffff; }
${A4_PAGE_CSS}
${RESEARCH_PAPER_CSS}
${opts?.extraCss || ''}
.vaf-a4-page { page-break-after: always; }
.vaf-a4-page:last-child { page-break-after: auto; }
</style></head><body>
<div id="flow" class="vaf-a4-flow vaf-paper">${fullHtml}</div>
<script>(function () {
    var flow = document.getElementById('flow');
    var step = ${A4_FLOW_STEP};
    var n = Math.max(1, Math.round((flow.scrollWidth + ${A4_COL_GAP}) / step));
    for (var i = 0; i < n; i++) {
        var page = document.createElement('div'); page.className = 'vaf-a4-page';
        var clip = document.createElement('div'); clip.className = 'vaf-a4-clip';
        var copy = flow.cloneNode(true); copy.removeAttribute('id');
        copy.style.transform = 'translateX(' + (-i * step) + 'px)';
        clip.appendChild(copy); page.appendChild(clip); document.body.appendChild(page);
    }
    flow.remove();
    requestAnimationFrame(function () { setTimeout(function () { window.print(); }, 60); });
})();</` + `script></body></html>`;

    const iframe = document.createElement('iframe');
    iframe.setAttribute('aria-hidden', 'true');
    iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;';
    document.body.appendChild(iframe);
    const cleanup = () => { try { iframe.remove(); } catch { /* already gone */ } };
    iframe.addEventListener('load', () => {
        iframe.contentWindow?.addEventListener('afterprint', () => setTimeout(cleanup, 100));
    });
    setTimeout(cleanup, 120000); // fallback if afterprint never fires
    iframe.srcdoc = doc;
}

/**
 * Live A4 view of the growing report. The newest section types in as plain
 * text first (cursor caret), then swaps to its rendered HTML — same feel as
 * before, but inside real paginated sheets. Only the measuring flow and the
 * last sheet re-render per typing tick; finished sheets reuse a frozen copy
 * (with `column-fill: auto`, appended content never reflows earlier columns).
 */
function A4ResearchPaper({ topic, metaLine, sectionsHtml, noticeHtml, onGrow, headerHtml, decorate = decorateCitations, paperClass = '' }: {
    topic: string;
    metaLine: string;
    sectionsHtml: string[];
    /** Live status box (mockup's .pending-note) rendered IN the page flow after the
     *  last text — never part of the print document. */
    noticeHtml?: string;
    onGrow?: () => void;
    /** Header HTML; defaults to the research report header. The document view passes
     *  its own (title + doc type). */
    headerHtml?: string;
    /** Text-node decorator: citations (research) or placeholder chips (document). */
    decorate?: (html: string) => string;
    /** Extra class on the flow (e.g. `vaf-doc` for the teal caret). */
    paperClass?: string;
}) {
    const measureRef = useRef<HTMLDivElement>(null);
    const wrapRef = useRef<HTMLDivElement>(null);
    const [pageCount, setPageCount] = useState(1);
    const [scale, setScale] = useState(1);
    const [, bumpRender] = useState(0);
    const frozenRef = useRef('');
    const lastStableRef = useRef('');
    const pageCountRef = useRef(1);
    const onGrowRef = useRef(onGrow); onGrowRef.current = onGrow;

    // ── typewriter for the newest section ──
    const [typedLen, setTypedLen] = useState(0);
    const [animUpTo, setAnimUpTo] = useState(0);   // sections fully revealed
    useEffect(() => {
        // New research run -> retype from the first arriving section again
        setAnimUpTo(0); setTypedLen(0);
        frozenRef.current = ''; lastStableRef.current = ''; pageCountRef.current = 1;
        setPageCount(1);
    }, [topic]);

    const lastIdx = sectionsHtml.length - 1;
    const animatingIdx = lastIdx >= 0 && lastIdx >= animUpTo ? lastIdx : -1;
    const animatingHtml = animatingIdx >= 0 ? sectionsHtml[animatingIdx] : '';
    const heading = useMemo(() => {
        const m = animatingHtml.match(/<h2[^>]*>([\s\S]*?)<\/h2>/i);
        return m ? m[1].replace(/<[^>]+>/g, '').trim() : '';
    }, [animatingHtml]);
    const bodyText = useMemo(
        () => animatingHtml.replace(/<h2[\s\S]*?<\/h2>/i, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(),
        [animatingHtml]
    );

    useEffect(() => {
        if (animatingIdx < 0) return;
        if (!bodyText) { setAnimUpTo(animatingIdx + 1); return; }
        let i = 0;
        let timer: ReturnType<typeof setTimeout>;
        // Scale step so even long sections finish in a few seconds
        const step = Math.max(2, Math.ceil(bodyText.length / 300));
        const tick = () => {
            i = Math.min(i + step, bodyText.length);
            setTypedLen(i);
            if (i < bodyText.length) {
                timer = setTimeout(tick, 16 + Math.random() * 14);
            } else {
                setAnimUpTo(animatingIdx + 1);
            }
        };
        setTypedLen(0);
        timer = setTimeout(tick, 40);
        return () => clearTimeout(timer);
    }, [animatingIdx, bodyText]);

    // ── flow html: stable prefix + live typing tail ──
    const headerHtmlResolved = headerHtml ?? buildResearchHeaderHtml(topic, metaLine);
    const stableHtml = headerHtmlResolved + decorate(
        sectionsHtml.slice(0, animatingIdx < 0 ? sectionsHtml.length : animatingIdx).join('')
    );
    const typingHtml = animatingIdx >= 0
        ? `${heading ? `<h2>${escapeHtml(heading)}</h2>` : ''}<p>${escapeHtml(bodyText.slice(0, typedLen))}<span class="vaf-typing-caret"></span></p>`
        : '';
    const liveHtml = stableHtml + typingHtml + (noticeHtml || '');

    // ── pagination: page count from the hidden measuring flow ──
    useLayoutEffect(() => {
        const el = measureRef.current;
        if (!el) return;
        const count = Math.max(1, Math.round((el.scrollWidth + A4_COL_GAP) / A4_FLOW_STEP));
        if (count !== pageCountRef.current || stableHtml !== lastStableRef.current) {
            // Refresh the frozen copy whenever a sheet fills up or a section
            // swaps from typed text to final HTML — earlier sheets then show
            // complete columns again.
            frozenRef.current = liveHtml;
            lastStableRef.current = stableHtml;
            if (count !== pageCountRef.current) {
                pageCountRef.current = count;
                setPageCount(count);
            } else {
                bumpRender(v => v + 1);
            }
        }
        onGrowRef.current?.();
    }, [liveHtml, stableHtml]);

    // ── fit-width scaling (sheets keep true A4 geometry, like a PDF viewer) ──
    useEffect(() => {
        const el = wrapRef.current;
        if (!el) return;
        const update = () => setScale(Math.min(1, el.clientWidth / A4_PAGE_W));
        update();
        const ro = new ResizeObserver(update);
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    return (
        <div ref={wrapRef} className="w-full">
            <div
                ref={measureRef}
                aria-hidden
                className={`vaf-a4-flow vaf-paper ${paperClass}`}
                style={{ position: 'absolute', visibility: 'hidden', pointerEvents: 'none', left: -99999, top: 0 }}
                dangerouslySetInnerHTML={{ __html: liveHtml }}
            />
            {Array.from({ length: pageCount }, (_, i) => {
                const html = i === pageCount - 1 ? liveHtml : (frozenRef.current || liveHtml);
                return (
                    <Fragment key={i}>
                        <div className="mx-auto" style={{ width: A4_PAGE_W * scale, height: A4_PAGE_H * scale }}>
                            <div
                                className="vaf-a4-page rounded-[2px] shadow-[0_2px_14px_rgba(0,0,0,0.12)]"
                                style={{ transform: `scale(${scale})`, transformOrigin: 'top left' }}
                            >
                                <div className="vaf-a4-clip">
                                    <div
                                        className={`vaf-a4-flow vaf-paper ${paperClass}`}
                                        style={{ transform: `translateX(-${i * A4_FLOW_STEP}px)` }}
                                        dangerouslySetInnerHTML={{ __html: html }}
                                    />
                                </div>
                            </div>
                        </div>
                        <div className="mx-auto mb-4 mt-1.5 text-center text-[9.5px] text-gray-400">
                            Page {i + 1} of {pageCount} · A4
                        </div>
                    </Fragment>
                );
            })}
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

/** Render a unified git diff with Cursor-style red/green line coloring. */
function DiffLines({ diff }: { diff: string }) {
    return (
        <div className="font-mono text-[11px] leading-[1.35]">
            {diff.split('\n').map((ln, i) => {
                let cls = 'text-gray-500';       // context line
                if (ln.startsWith('+') && !ln.startsWith('+++')) cls = 'bg-emerald-50 text-emerald-700';
                else if (ln.startsWith('-') && !ln.startsWith('---')) cls = 'bg-red-50 text-red-700';
                else if (ln.startsWith('@@')) cls = 'text-violet-500';
                else if (ln.startsWith('diff ') || ln.startsWith('index ') || ln.startsWith('+++') || ln.startsWith('---')) cls = 'text-gray-300';
                return (
                    <div key={i} className={cn('whitespace-pre-wrap break-all px-3', cls)}>{ln || ' '}</div>
                );
            })}
        </div>
    );
}

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
    research,
    document,
    librarian,
    browser,
    agentKind = null,
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

    // Smart auto-scroll: stick to bottom; pause when user scrolls up; resume when near bottom again.
    const consoleScrollRef = useRef<HTMLDivElement>(null);
    // Follow the tail by default (like a terminal / tail -f). Unpin only when the user
    // deliberately scrolls up; re-pin when they return to the very bottom.
    const stickToBottomRef = useRef(true);
    // Our own scroll-to-bottom fires a scroll event too — flag it so handleConsoleScroll
    // does not mistake it for the user scrolling up (that false positive is what froze the
    // follow: the console would stop tracking new output after a brief pause).
    const programmaticScrollRef = useRef(false);

    const scrollConsoleToBottom = () => {
        const el = consoleScrollRef.current;
        if (!el) return;
        programmaticScrollRef.current = true;
        el.scrollTop = el.scrollHeight;
        setTimeout(() => { programmaticScrollRef.current = false; }, 60);
    };

    // Follow new console lines while pinned. rAF so scrollHeight is read AFTER the new
    // lines are laid out (otherwise we scroll to a stale, too-short height and lag behind).
    useEffect(() => {
        if (stickToBottomRef.current) requestAnimationFrame(scrollConsoleToBottom);
    }, [consoleLines]);

    // A new screenshot changes the image height (layout shift) — re-pin and follow.
    useEffect(() => {
        stickToBottomRef.current = true;
        requestAnimationFrame(scrollConsoleToBottom);
    }, [browserFrame]);

    const handleConsoleScroll = (e: React.UIEvent<HTMLDivElement>) => {
        if (programmaticScrollRef.current) return;   // our own scroll, not the user
        const el = e.currentTarget;
        const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        // Unpin the instant the user scrolls up; re-pin only once they are back at the
        // very bottom (small threshold = "all the way down").
        stickToBottomRef.current = distFromBottom <= 24;
    };

    // ── VS-Code view (coding agent only) ──────────────────────────────────
    const [editorDark, setEditorDark] = useState(false);

    // Bottom panel tabs (Console / Linter / Telemetry)
    const [activeConsoleTab, setActiveConsoleTab] = useState<'console' | 'linter' | 'telemetry' | 'diff'>('console');

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

    // ── Research view (research agent only) ───────────────────────────────
    const researchViewerRef = useRef<HTMLDivElement>(null);
    const keepResearchViewerPinned = () => {
        if (researchViewerRef.current) {
            researchViewerRef.current.scrollTop = researchViewerRef.current.scrollHeight;
        }
    };
    useEffect(() => {
        keepResearchViewerPinned();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [research?.sectionsHtml?.length]);

    // ── Document view (document agent only) ───────────────────────────────
    // Show the document window as soon as the FIRST state arrives — even during the
    // (slow) planning phase when no sections exist yet — so the user sees the custom
    // window with a "Planning…" placeholder instead of the generic startup console.
    const documentViewerRef = useRef<HTMLDivElement>(null);
    const keepDocumentViewerPinned = () => {
        if (documentViewerRef.current) {
            documentViewerRef.current.scrollTop = documentViewerRef.current.scrollHeight;
        }
    };
    useEffect(() => {
        keepDocumentViewerPinned();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [document?.sectionsHtml?.length]);

    // Follow the work: scroll the Sektionen list to the section being written and the
    // Activity feed to the newest line whenever the document state advances.
    const documentOutlineRef = useRef<HTMLDivElement>(null);
    const documentActivityRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        documentOutlineRef.current?.querySelector('[data-doc-active="1"]')?.scrollIntoView({ block: 'nearest' });
        if (documentActivityRef.current) documentActivityRef.current.scrollTop = documentActivityRef.current.scrollHeight;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [document?.sections, document?.stage, document?.savePath]);

    // Librarian read-only explorer view (file system / storage / Google Drive)
    const librarianViewerRef = useRef<HTMLDivElement>(null);
    const librarianActivityRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (librarianActivityRef.current) librarianActivityRef.current.scrollTop = librarianActivityRef.current.scrollHeight;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [librarian?.activity?.length, librarian?.entries?.length, librarian?.stage]);

    // Browser window: auto-scroll the action plan and the activity log to the newest entry
    const browserActionsRef = useRef<HTMLDivElement>(null);
    const browserActivityRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (browserActionsRef.current) browserActionsRef.current.scrollTop = browserActionsRef.current.scrollHeight;
    }, [browser?.actions?.length, browser?.step]);
    useEffect(() => {
        if (browserActivityRef.current) browserActivityRef.current.scrollTop = browserActivityRef.current.scrollHeight;
    }, [consoleLines.length]);

    // Custom views render IMMEDIATELY once the kind is known (from the tool CALL), in a loading shell, then
    // fill as <x>_state streams. <x>V = a guaranteed-valid object for the (large) view bodies; <x>Loading =
    // the kind is active but its data has not arrived yet -> show the "Starting…" banner.
    const coderV = coder ?? EMPTY_CODER;
    const researchV = research ?? EMPTY_RESEARCH;
    const documentV = document ?? EMPTY_DOCUMENT;
    const librarianV = librarian ?? EMPTY_LIBRARIAN;
    const browserV = browser ?? EMPTY_BROWSER;
    const coderLoading = !coder;
    const researchLoading = !research;
    const documentLoading = !document;
    const librarianLoading = !librarian;
    const browserLoading = !browser && !browserFrame;

    // When the window is closed the outer dock panel is already w-0/opacity-0/invisible, so render
    // nothing rather than building the full (un-virtualized) research/document report DOM and arming its
    // typewriter + ResizeObserver. The old `&& mode === 'overlay'` made this guard dead code: the only
    // caller uses mode="dock", so the heavy view was still built while invisible and then thrashed for
    // minutes when revealed (e.g. closing the editor after a research run re-mounts this view).
    if (!isOpen) return null;

    if (mode === 'dock' && agentKind === 'document') {
        // ── Document view for the document agent: A4 paper growing section by
        // section, placeholders pre-filled from memory/chat, status bar. ──
        const document = documentV;  // guaranteed-valid shell while data streams (loading shown via banner)
        const doneCount = document.sections.filter(s => s.status === 'done').length;
        const wordsTotal = document.sections.reduce((sum, s) => sum + (s.words || 0), 0);
        const isLive = inferredPresence === 'online';
        const fmt = (document.format || 'docx').toUpperCase();
        const docTypeLabel = (document.docType || 'document').replace(/^\w/, c => c.toUpperCase());
        const docMetaLine = `${doneCount}/${document.sections.length} sections · ${wordsTotal.toLocaleString()} words · ${document.placeholders.length} placeholders`;
        const docHeaderHtml = buildDocHeaderHtml(document.title || 'Document', docTypeLabel, docMetaLine);
        const filledCount = document.placeholders.filter(p => p.source !== 'open').length;
        const writingSec = document.sections.find(s => s.status === 'writing');
        const docNoticeHtml = !isLive ? '' : writingSec
            ? `<div class="pending-note"><div class="searching-note"><span class="spin">&#9998;</span>Writing &quot;${escapeHtml(writingSec.title)}&quot;…</div></div>`
            : (document.sectionsHtml.length === 0
                ? '<div class="pending-note"><div class="searching-note"><span class="spin">&#9998;</span>Planning sections…</div></div>'
                : '');
        const phChip = (source: string): { cls: string; label: string } => ({
            memory: { cls: 'bg-teal-100 text-teal-700', label: 'Memory' },
            chat: { cls: 'bg-blue-100 text-blue-700', label: 'Chat' },
            auto: { cls: 'bg-amber-100 text-amber-700', label: 'Auto' },
        }[source] ?? { cls: 'bg-gray-100 text-gray-400', label: 'offen' });
        const N = document.sections.length;
        // Activity feed derived from the live document state, so it advances with each
        // section (the raw sub-agent stdout goes quiet during long LLM generation).
        const docActivity: string[] = [];
        if (N > 0) docActivity.push(`Plan: ${docTypeLabel} · ${N} sections · ${fmt}`);
        document.sections.forEach((s, i) => {
            if (s.status === 'done') docActivity.push(`[${i + 1}/${N}] ${s.title}: done — ${s.words} words`);
            else if (s.status === 'writing') docActivity.push(`[${i + 1}/${N}] ${s.title}: writing…`);
        });
        if (N === 0 && document.stage) docActivity.push(`${document.stage}…`);
        if (document.savePath) docActivity.push(`Saved: ${document.savePath.split(/[\\/]/).pop()}`);

        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <style>{A4_PAGE_CSS + RESEARCH_PAPER_CSS + DOCUMENT_PAPER_CSS}</style>
                <div className="flex h-full w-full flex-col">
                    {documentLoading && <StartingBanner label="Document Agent" />}
                    {/* Header */}
                    <div className="flex h-12 flex-none items-center justify-between border-b border-gray-200 bg-white px-4">
                        <div className="flex min-w-0 items-center gap-3">
                            <div className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-gray-200 bg-white text-gray-700">
                                <FileCode size={14} />
                            </div>
                            <div className="min-w-0">
                                <div className="text-xs font-semibold text-gray-900">{agentName && agentName !== 'Sub-Agent' ? agentName : 'Document Agent'}</div>
                                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 flex-none rounded-full", presenceTone)} />
                                    <span className="truncate">{document.title || displayStatus}</span>
                                </div>
                            </div>
                        </div>
                        <div className="flex flex-none items-center gap-2">
                            <span className="rounded-md bg-teal-50 px-2 py-1 text-[9px] font-extrabold tracking-wider text-teal-700">{fmt}</span>
                            <span className="flex items-center gap-1.5 rounded-full bg-teal-50 px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider text-teal-700">
                                {isLive && (/writ/i.test(document.stage)
                                    ? <Pencil size={9} className="animate-spin" />
                                    : <Loader2 size={9} className="animate-spin" />)}
                                {document.stage}
                            </span>
                            <button
                                onClick={() => printResearchReport(document.title, docMetaLine, document.sectionsHtml, { headerHtml: docHeaderHtml, decorate: decoratePlaceholders, extraCss: DOCUMENT_PAPER_CSS })}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Print document (A4)"
                                title="Print document (A4)"
                            >
                                <Printer size={14} />
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

                    <div className="flex min-h-0 flex-1">
                        {/* ── Paper document viewer ── */}
                        <div ref={documentViewerRef} className="min-w-0 flex-1 overflow-y-auto bg-[#e9eaee] px-7 py-6">
                            <A4ResearchPaper
                                topic={document.title}
                                metaLine={docMetaLine}
                                sectionsHtml={document.sectionsHtml}
                                noticeHtml={docNoticeHtml}
                                onGrow={keepDocumentViewerPinned}
                                headerHtml={docHeaderHtml}
                                decorate={decoratePlaceholders}
                                paperClass="vaf-doc"
                            />
                        </div>

                        {/* ── Sidebar: Sections / Placeholders / Activity ── */}
                        <div className="flex w-[33%] min-w-[280px] max-w-[380px] flex-none flex-col border-l border-gray-200 bg-white">
                            {/* Sections */}
                            <div className="flex min-h-0 flex-1 flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">
                                    Sektionen
                                    <span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">
                                        {doneCount}/{document.sections.length} fertig
                                    </span>
                                </div>
                                <div ref={documentOutlineRef} className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2.5 pb-2">
                                    {document.sections.map((s, i) => (
                                        <div
                                            key={i}
                                            data-doc-active={s.status === 'writing' ? '1' : undefined}
                                            className={cn(
                                                'flex items-start gap-2 rounded-lg border px-2.5 py-1.5',
                                                s.status === 'writing' && 'border-teal-200 bg-white ring-1 ring-teal-50',
                                                s.status === 'done' && 'border-gray-100 bg-gray-50',
                                                s.status === 'planned' && 'border-gray-100 bg-white'
                                            )}
                                        >
                                            <span className={cn(
                                                'mt-px flex h-4 w-4 flex-none items-center justify-center rounded-full text-[8px] font-bold',
                                                s.status === 'done' && 'bg-emerald-100 text-emerald-600',
                                                s.status === 'writing' && 'bg-teal-100 text-teal-600',
                                                s.status === 'planned' && 'bg-gray-100 text-gray-400'
                                            )}>
                                                {s.status === 'done' && <CheckCircle2 size={9} />}
                                                {s.status === 'writing' && <Pencil size={9} className="animate-spin" />}
                                                {s.status === 'planned' && (i + 1)}
                                            </span>
                                            <span className="min-w-0 flex-1">
                                                <span className="block text-[11px] font-semibold leading-tight text-gray-800">{i + 1}. {s.title}</span>
                                                <span className="mt-0.5 flex items-center gap-1.5 text-[9px] text-gray-400">
                                                    <span className="inline-block h-[3px] w-16 overflow-hidden rounded-full bg-gray-100">
                                                        <span
                                                            className="block h-full rounded-full bg-teal-400 transition-all"
                                                            style={{ width: `${Math.min(100, (s.words / Math.max(1, s.targetWords)) * 100)}%` }}
                                                        />
                                                    </span>
                                                    {s.status === 'done' ? `${s.words} Wörter`
                                                        : s.status === 'writing' ? 'schreibt…'
                                                        : 'geplant'}
                                                </span>
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>

                            {/* Placeholders */}
                            <div className="flex min-h-0 flex-[1.5] flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">
                                    Platzhalter
                                    <span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">
                                        {filledCount}/{document.placeholders.length} gefüllt
                                    </span>
                                </div>
                                <div className="min-h-0 flex-1 overflow-y-auto px-2.5 pb-2">
                                    {document.placeholders.length === 0 && (
                                        <div className="px-2 py-3 text-[10px] text-gray-300">Noch keine Platzhalter.</div>
                                    )}
                                    {document.placeholders.map((p, i) => {
                                        const chip = phChip(p.source);
                                        const open = p.source === 'open';
                                        return (
                                            <div
                                                key={`${p.name}-${i}`}
                                                className={cn('mb-1 flex items-baseline gap-2 rounded-lg border px-2 py-1.5',
                                                    open ? 'border-gray-100' : 'border-teal-100 bg-teal-50/40')}
                                            >
                                                <span className="flex-none font-mono text-[10px] text-teal-700">{`{{${p.name}}}`}</span>
                                                <span className={cn('min-w-0 flex-1 truncate text-[11px]', open ? 'italic text-gray-400' : 'text-gray-800')}>
                                                    {open ? 'im Chat ergänzen' : p.value}
                                                </span>
                                                <span className={cn('flex-none rounded px-1.5 py-px text-[8.5px] font-bold uppercase tracking-wide', chip.cls)}>{chip.label}</span>
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>

                            {/* Activity — derived from the live document state so it keeps
                                advancing even while the sub-agent stdout is quiet. */}
                            <div className="flex min-h-0 flex-[0.7] flex-col">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Activity</div>
                                <div
                                    ref={documentActivityRef}
                                    className="min-h-0 flex-1 overflow-y-auto px-3.5 pb-2 font-mono text-[10px] leading-relaxed text-gray-500"
                                >
                                    {docActivity.map((line, index) => (
                                        <div key={`${index}-${line.slice(0, 24)}`} className="break-all">{line}</div>
                                    ))}
                                    {docActivity.length === 0 && (
                                        <div className="text-gray-300">Waiting for output…</div>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* ── Status bar ── */}
                    <div className="flex h-6 flex-none items-center bg-[#1f2335] text-[10px] text-[#c8d0e8]">
                        <div className="flex h-full items-center bg-teal-600 px-3 font-bold text-white">VAF</div>
                        <div className="flex h-full items-center px-2.5">Format: {fmt}</div>
                        <div className="flex h-full items-center px-2.5">Section {Math.min(doneCount + (writingSec ? 1 : 0), document.sections.length)}/{document.sections.length}</div>
                        <div className="flex h-full items-center gap-1.5 px-2.5">
                            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                            {wordsTotal.toLocaleString()} Wörter
                        </div>
                        <div className="flex h-full items-center px-2.5">{filledCount}/{document.placeholders.length} gefüllt</div>
                        {document.savePath && (
                            <div className="ml-auto flex h-full max-w-[40%] items-center truncate px-2.5 font-mono text-[9px] text-[#8b93b0]" title={document.savePath}>{document.savePath}</div>
                        )}
                        <div className={cn("flex h-full items-center gap-1.5 px-3", !document.savePath && "ml-auto")}>
                            <span className={cn('h-1.5 w-1.5 rounded-full', presenceTone)} />
                            {presenceLabel}
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    if (mode === 'dock' && agentKind === 'research') {
        const research = researchV;  // guaranteed-valid shell while data streams (loading shown via banner)
        const doneCount = research.sections.filter(s => s.status === 'done').length;
        const wordsTotal = research.sections.reduce((sum, s) => sum + (s.words || 0), 0);
        const isLive = inferredPresence === 'online';
        const nextSection = research.sections.find(s => s.status === 'searching' || s.status === 'planned');
        const metaLine = `${wordsTotal.toLocaleString()}${research.wordsTarget ? ` / ${research.wordsTarget.toLocaleString()}` : ''} words · ${research.sources.length} sources`;
        // Live status box ON the sheet (mockup's .pending-note), one at a time:
        // a concrete next section beats the generic planning state.
        const researchNoticeHtml = !isLive ? '' : nextSection
            ? (nextSection.status === 'searching'
                ? `<div class="pending-note"><div class="searching-note"><span class="spin">&#9906;</span>Searching sources for &quot;${escapeHtml(nextSection.title)}&quot;…</div></div>`
                : `<div class="pending-note">Planned next: &quot;${escapeHtml(nextSection.title)}&quot;</div>`)
            : (research.sectionsHtml.length === 0
                ? '<div class="pending-note"><div class="searching-note"><span class="spin">&#9906;</span>Planning and searching…</div></div>'
                : '');

        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                {/* Shared sheet + typography CSS — the print document uses the identical rules */}
                <style>{A4_PAGE_CSS + RESEARCH_PAPER_CSS}</style>
                <div className="flex h-full w-full flex-col">
                    {researchLoading && <StartingBanner label="Research Agent" />}
                    {/* Header */}
                    <div className="flex h-12 flex-none items-center justify-between border-b border-gray-200 bg-white px-4">
                        <div className="flex min-w-0 items-center gap-3">
                            <div className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-gray-200 bg-white text-gray-700">
                                <Globe size={14} />
                            </div>
                            <div className="min-w-0">
                                <div className="text-xs font-semibold text-gray-900">{agentName || 'Research Agent'}</div>
                                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 flex-none rounded-full", presenceTone)} />
                                    <span className="truncate">{research.topic || displayStatus}</span>
                                </div>
                            </div>
                        </div>
                        <div className="flex flex-none items-center gap-2">
                            <span className="flex items-center gap-1.5 rounded-full bg-violet-50 px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider text-violet-700">
                                {isLive && (/search/i.test(research.stage)
                                    ? <Search size={9} className="animate-spin" />
                                    : /summar|writ/i.test(research.stage)
                                        ? <Pencil size={9} className="animate-spin" />
                                        : <Loader2 size={9} className="animate-spin" />)}
                                {research.stage}
                            </span>
                            <button
                                onClick={() => printResearchReport(research.topic, metaLine, research.sectionsHtml)}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Print report (A4)"
                                title="Print report (A4)"
                            >
                                <Printer size={14} />
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

                    <div className="flex min-h-0 flex-1">
                        {/* ── Paper document viewer: true A4 sheets with automatic page breaks ── */}
                        <div ref={researchViewerRef} className="min-w-0 flex-1 overflow-y-auto bg-[#e9eaee] px-7 py-6">
                            <A4ResearchPaper
                                topic={research.topic}
                                metaLine={metaLine}
                                sectionsHtml={research.sectionsHtml}
                                noticeHtml={researchNoticeHtml}
                                onGrow={keepResearchViewerPinned}
                            />
                        </div>

                        {/* ── Sidebar: Outline / Sources / Activity ── */}
                        <div className="flex w-[33%] min-w-[280px] max-w-[380px] flex-none flex-col border-l border-gray-200 bg-white">
                            {/* Outline */}
                            <div className="flex min-h-0 flex-[1.25] flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">
                                    Outline
                                    <span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">
                                        {doneCount}/{research.sections.length} done
                                    </span>
                                </div>
                                <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2.5 pb-2">
                                    {research.sections.map((s, i) => (
                                        <div
                                            key={i}
                                            className={cn(
                                                'flex items-start gap-2 rounded-lg border px-2.5 py-1.5',
                                                s.status === 'writing' && 'border-violet-200 bg-white ring-1 ring-violet-50',
                                                s.status === 'searching' && 'border-blue-100 bg-white',
                                                s.status === 'done' && 'border-gray-100 bg-gray-50',
                                                s.status === 'error' && 'border-red-100 bg-red-50/40',
                                                s.status === 'planned' && 'border-gray-100 bg-white'
                                            )}
                                        >
                                            <span className={cn(
                                                'mt-px flex h-4 w-4 flex-none items-center justify-center rounded-full text-[8px] font-bold',
                                                s.status === 'done' && 'bg-emerald-100 text-emerald-600',
                                                s.status === 'writing' && 'bg-violet-100 text-violet-600',
                                                s.status === 'searching' && 'bg-blue-100 text-blue-600',
                                                s.status === 'error' && 'bg-red-100 text-red-600',
                                                s.status === 'planned' && 'bg-gray-100 text-gray-400'
                                            )}>
                                                {s.status === 'done' && <CheckCircle2 size={9} />}
                                                {s.status === 'searching' && <Search size={9} className="animate-spin" />}
                                                {s.status === 'writing' && <Pencil size={9} className="animate-spin" />}
                                                {s.status === 'error' && <X size={9} />}
                                                {s.status === 'planned' && (i + 1)}
                                            </span>
                                            <span className="min-w-0 flex-1">
                                                <span className="block text-[11px] font-semibold leading-tight text-gray-800">{i + 1}. {s.title}</span>
                                                <span className="mt-0.5 flex items-center gap-1.5 text-[9px] text-gray-400">
                                                    <span className="inline-block h-[3px] w-16 overflow-hidden rounded-full bg-gray-100">
                                                        <span
                                                            className="block h-full rounded-full bg-violet-400 transition-all"
                                                            style={{ width: `${Math.min(100, (s.words / Math.max(1, s.targetWords)) * 100)}%` }}
                                                        />
                                                    </span>
                                                    {s.status === 'done' ? `${s.words} words`
                                                        : s.status === 'writing' ? 'writing…'
                                                        : s.status === 'searching' ? 'searching…'
                                                        : s.status === 'error' ? 'no results'
                                                        : 'planned'}
                                                </span>
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>

                            {/* Sources */}
                            <div className="flex min-h-0 flex-[1.15] flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">
                                    Sources
                                    <span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">
                                        {research.sources.length}
                                    </span>
                                </div>
                                <div className="min-h-0 flex-1 overflow-y-auto px-2.5 pb-2">
                                    {research.sources.map((s, i) => (
                                        <a
                                            key={s.url || i}
                                            href={s.url}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="flex items-baseline gap-2 rounded-md px-1.5 py-1 hover:bg-gray-50"
                                        >
                                            <span className="flex-none rounded bg-violet-50 px-1 font-mono text-[8.5px] text-violet-600">[{i + 1}]</span>
                                            <span className="min-w-0 flex-1">
                                                <span className="block truncate text-[10.5px] text-gray-700">{s.title}</span>
                                                <span className="block truncate font-mono text-[8.5px] text-gray-300">{s.domain}</span>
                                            </span>
                                        </a>
                                    ))}
                                    {research.sources.length === 0 && (
                                        <div className="px-2 py-3 text-[10px] text-gray-300">No sources yet.</div>
                                    )}
                                </div>
                            </div>

                            {/* Activity (console lines) */}
                            <div className="flex min-h-0 flex-1 flex-col">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Activity</div>
                                <div
                                    ref={consoleScrollRef}
                                    onScroll={handleConsoleScroll}
                                    className="min-h-0 flex-1 overflow-y-auto px-3.5 pb-2 font-mono text-[10px] leading-relaxed text-gray-500"
                                >
                                    {consoleLines.slice(-80).map((line, index) => (
                                        <div key={`${index}-${line.slice(0, 20)}`} className="break-all">{line}</div>
                                    ))}
                                    {consoleLines.length === 0 && (
                                        <div className="text-gray-300">Waiting for output…</div>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* ── Status bar ── */}
                    <div className="flex h-6 flex-none items-center bg-[#1f2335] text-[10px] text-[#c8d0e8]">
                        <div className="flex h-full items-center bg-violet-600 px-3 font-bold text-white">VAF</div>
                        <div className="flex h-full items-center px-2.5">Stage: {research.stage}</div>
                        <div className="flex h-full items-center px-2.5">Sections {doneCount}/{research.sections.length}</div>
                        <div className="flex h-full items-center gap-1.5 px-2.5">
                            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                            {wordsTotal.toLocaleString()}{research.wordsTarget ? ` / ${research.wordsTarget.toLocaleString()}` : ''} words
                        </div>
                        <div className="flex h-full items-center px-2.5">{research.sources.length} sources</div>
                        <div className="ml-auto flex h-full items-center px-2.5">Loop {research.loop}</div>
                        <div className="flex h-full items-center gap-1.5 px-3">
                            <span className={cn('h-1.5 w-1.5 rounded-full', presenceTone)} />
                            {presenceLabel}
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    if (mode === 'dock' && agentKind === 'librarian' && librarian?.currentFolder) {
        // ── Folder-browse view: the librarian opened a concrete folder; list its files
        // like a file manager and highlight search matches (read-only). Renders only with
        // real folder data; the overview shell below covers the loading state. ──
        const lib = librarian;
        const cf = lib.currentFolder!;
        const fmtBytes = (b: number): string => {
            if (!b || b < 0) return '0 B';
            const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let n = b;
            while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
            return `${i <= 1 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
        };
        const isLive = inferredPresence === 'online';
        const query = lib.search?.query || null;
        const hitCount = cf.entries.filter(e => e.match).length;
        const matches = cf.entries.filter(e => e.match);
        const TYPE_STYLE: Record<string, { bg: string; fg: string; bar: string }> = {
            folder: { bg: 'bg-orange-50', fg: 'text-orange-700', bar: 'bg-orange-500' },
            pdf: { bg: 'bg-red-50', fg: 'text-red-600', bar: 'bg-red-500' },
            image: { bg: 'bg-blue-50', fg: 'text-blue-600', bar: 'bg-blue-500' },
            video: { bg: 'bg-violet-50', fg: 'text-violet-600', bar: 'bg-violet-500' },
            doc: { bg: 'bg-teal-50', fg: 'text-teal-600', bar: 'bg-teal-500' },
            data: { bg: 'bg-emerald-50', fg: 'text-emerald-600', bar: 'bg-emerald-500' },
            arch: { bg: 'bg-slate-100', fg: 'text-slate-500', bar: 'bg-slate-400' },
            exe: { bg: 'bg-slate-100', fg: 'text-slate-600', bar: 'bg-slate-500' },
            txt: { bg: 'bg-gray-100', fg: 'text-gray-500', bar: 'bg-gray-400' },
        };
        const TYPE_LABEL: Record<string, string> = { folder: 'Ordner', pdf: 'PDF', image: 'Bilder', video: 'Video', doc: 'Docs', data: 'Daten', arch: 'Archiv', exe: 'Apps', txt: 'Text' };
        const iconFor = (t: string, isDir: boolean) => isDir ? Folder : t === 'image' ? ImageIcon : t === 'video' ? Film : t === 'arch' ? Archive : FileText;
        const styleFor = (t: string) => TYPE_STYLE[t] ?? TYPE_STYLE.txt;
        const crumbs = cf.path.split(/[\\/]/).filter(Boolean);
        const maxType = Math.max(1, ...cf.types.map(t => t.count));
        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full flex-col">
                    {/* Header */}
                    <div className="flex h-12 flex-none items-center justify-between border-b border-gray-200 bg-white px-4">
                        <div className="flex min-w-0 items-center gap-3">
                            <div className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-gray-200 bg-white text-orange-600">
                                <FolderOpen size={14} />
                            </div>
                            <div className="min-w-0">
                                <div className="text-xs font-semibold text-gray-900">{agentName && agentName !== 'Sub-Agent' ? agentName : 'Librarian Agent'}</div>
                                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 flex-none rounded-full", presenceTone)} />
                                    <span className="truncate">{query ? <>Durchsucht: <span className="font-mono">{cf.name}</span> nach <span className="font-mono">{query}</span></> : <>Öffnet: <span className="font-mono">{cf.name}</span></>}</span>
                                </div>
                            </div>
                        </div>
                        <div className="flex flex-none items-center gap-2">
                            <span className="flex items-center gap-1.5 rounded-md bg-orange-50 px-2 py-1 text-[9px] font-extrabold tracking-wider text-orange-700"><Lock size={9} /> READ-ONLY</span>
                            <span className="flex items-center gap-1.5 rounded-full bg-orange-50 px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider text-orange-700">
                                {isLive && lib.stage !== 'done' && <Loader2 size={9} className="animate-spin" />}
                                {lib.stage === 'done' ? 'Fertig' : (query ? 'Searching' : 'Liest')}{query ? ` · ${hitCount} Treffer` : ''}
                            </span>
                            <button onClick={onClose} className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600" aria-label="Close"><X size={14} /></button>
                        </div>
                    </div>

                    <div className="flex min-h-0 flex-1">
                        {/* ── Explorer: opened folder, files listed ── */}
                        <div className="flex min-w-0 flex-1 flex-col bg-[#e9eaee]">
                            {/* toolbar: back + breadcrumb + search */}
                            <div className="flex h-10 flex-none items-center gap-2 border-b border-gray-200 bg-[#f7f8fa] px-3.5">
                                <span className="flex h-6 w-6 flex-none items-center justify-center rounded-md border border-gray-200 bg-white text-gray-400" title="Übersicht"><ChevronLeft size={13} /></span>
                                <div className="flex min-w-0 items-center gap-1 text-[11.5px] text-gray-500">
                                    <span className="rounded px-1.5 py-0.5">~</span>
                                    {crumbs.slice(-3).map((c, i, arr) => (
                                        <span key={i} className="flex items-center gap-1">
                                            <span className="text-gray-300">/</span>
                                            {i === arr.length - 1
                                                ? <span className="flex items-center gap-1 rounded bg-orange-50 px-1.5 py-0.5 font-bold text-orange-700"><Folder size={10} />{c}</span>
                                                : <span className="truncate px-1 py-0.5">{c}</span>}
                                        </span>
                                    ))}
                                </div>
                                {query && (
                                    <span className="ml-auto flex items-center gap-1.5 rounded-md border border-gray-200 bg-white px-2 py-1 text-[11px] text-gray-700">
                                        <Search size={11} className="text-gray-400" />
                                        <span className="font-mono text-orange-700">{query}</span>
                                        <span className="rounded-full bg-orange-50 px-1.5 text-[10px] font-bold text-orange-700">{hitCount}</span>
                                    </span>
                                )}
                            </div>
                            {/* folder banner */}
                            <div className="flex flex-none items-center gap-2.5 px-4 pb-2 pt-2.5">
                                <div className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-orange-50 text-orange-700"><FolderOpen size={15} /></div>
                                <div className="min-w-0">
                                    <div className="text-[14px] font-bold text-gray-900">{cf.name}</div>
                                    <div className="text-[10.5px] text-gray-400">{cf.fileCount.toLocaleString('de-DE')} Dateien · {cf.folderCount} Unterordner · {fmtBytes(cf.totalSize)}</div>
                                </div>
                                <div className="ml-auto flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-orange-700">
                                    {lib.stage === 'done' ? <CheckCircle2 size={11} className="text-emerald-500" /> : <Loader2 size={11} className="animate-spin" />}
                                    {lib.stage === 'done' ? 'gelesen' : 'liest Ordner'}
                                </div>
                            </div>
                            {/* file listing */}
                            <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
                                <div className="sticky top-0 flex items-center gap-3 border-b border-gray-200 bg-[#e9eaee] px-3 pb-1.5 pt-1 text-[9px] font-bold uppercase tracking-wider text-gray-400">
                                    <span className="flex-1">Name</span><span className="w-[74px] text-right">Größe</span><span className="w-[88px] text-right">Geändert</span>
                                </div>
                                {cf.entries.map((e, i) => {
                                    const Icon = iconFor(e.type, e.isDir);
                                    const ts = styleFor(e.isDir ? 'folder' : e.type);
                                    return (
                                        <div key={`${e.name}-${i}`} className={cn("flex items-center gap-3 border-b border-[#e0e1e6] px-3 py-1.5", e.match && "bg-orange-50/70 shadow-[inset_3px_0_0_#ea580c]")}>
                                            <span className={cn("flex h-5 w-5 flex-none items-center justify-center rounded-md", ts.bg, ts.fg)}><Icon size={12} /></span>
                                            <span className={cn("min-w-0 flex-1 truncate text-[12.5px] text-gray-800", e.isDir && "font-semibold")}>{e.name}</span>
                                            {e.match && <span className="flex-none rounded bg-orange-100 px-1.5 py-px text-[8px] font-extrabold uppercase tracking-wide text-orange-700">Treffer</span>}
                                            <span className="w-[74px] flex-none text-right text-[11px] tabular-nums text-gray-600">{e.isDir ? (e.items != null ? `${e.items} El.` : '—') : fmtBytes(e.sizeBytes)}</span>
                                            <span className="w-[88px] flex-none text-right text-[10.5px] tabular-nums text-gray-400">{e.modified}</span>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>

                        {/* ── Sidebar: current folder / matches / activity ── */}
                        <div className="flex w-[33%] min-w-[300px] max-w-[380px] flex-none flex-col border-l border-gray-200 bg-white">
                            <div className="flex flex-none flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Aktueller Ordner</div>
                                <div className="px-3.5 pb-3 pt-0.5">
                                    <div className="mb-2 break-all font-mono text-[11px] text-orange-700">{cf.path}</div>
                                    <div className="mb-2.5 flex gap-4">
                                        <div><div className="text-[16px] font-bold text-gray-900">{cf.fileCount.toLocaleString('de-DE')}</div><div className="text-[9px] text-gray-400">Dateien</div></div>
                                        <div><div className="text-[16px] font-bold text-gray-900">{cf.folderCount}</div><div className="text-[9px] text-gray-400">Unterordner</div></div>
                                        <div><div className="text-[16px] font-bold text-gray-900">{fmtBytes(cf.totalSize)}</div><div className="text-[9px] text-gray-400">Größe</div></div>
                                    </div>
                                    <div className="flex flex-col gap-1.5">
                                        {cf.types.map((t, i) => (
                                            <div key={i} className="flex items-center gap-2 text-[11px]">
                                                <span className="w-12 flex-none text-gray-500">{TYPE_LABEL[t.type] ?? t.type}</span>
                                                <span className="h-[5px] flex-1 overflow-hidden rounded-full bg-gray-100"><span className={cn("block h-full rounded-full", styleFor(t.type).bar)} style={{ width: `${Math.round(t.count / maxType * 100)}%` }} /></span>
                                                <span className="w-7 flex-none text-right text-gray-400">{t.count}</span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                            <div className="flex min-h-0 flex-1 flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Treffer{query && <span className="ml-1.5 font-mono lowercase tracking-normal text-orange-700">{query}</span>}<span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">{matches.length}</span></div>
                                <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-3 pb-3 pt-0.5">
                                    {matches.length === 0
                                        ? <div className="px-1 py-1 text-[9.5px] text-gray-400">{query ? 'Noch keine Treffer in diesem Ordner.' : 'Keine aktive Suche.'}</div>
                                        : matches.map((m, i) => {
                                            const Icon = iconFor(m.type, false); const ts = styleFor(m.type);
                                            return (
                                                <div key={i} className="flex items-center gap-2 rounded-lg border border-orange-100 bg-orange-50/60 px-2.5 py-1.5">
                                                    <span className={cn("flex h-4 w-4 flex-none items-center justify-center rounded", ts.bg, ts.fg)}><Icon size={10} /></span>
                                                    <span className="min-w-0 flex-1 truncate text-[11px] text-gray-800">{m.name}</span>
                                                    <span className="flex-none text-[10px] tabular-nums text-gray-400">{fmtBytes(m.sizeBytes)}</span>
                                                </div>
                                            );
                                        })}
                                </div>
                            </div>
                            <div className="flex h-[28%] min-h-[110px] flex-none flex-col">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Activity</div>
                                <div ref={librarianActivityRef} className="min-h-0 flex-1 overflow-y-auto px-3.5 pb-3 font-mono text-[10px] leading-relaxed text-gray-500">
                                    {lib.activity.map((a, i) => (
                                        <div key={i}><span className={cn(a.cls === 'ok' && 'text-emerald-600', a.cls === 'info' && 'text-blue-600', a.cls === 'warn' && 'text-amber-600', a.cls === 'scan' && 'text-orange-700')}>{a.text}</span></div>
                                    ))}
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* Statusbar */}
                    <div className="flex h-6 flex-none items-center bg-[#1f2335] text-[10.5px] text-[#c8d0e8]">
                        <div className="flex h-full items-center gap-1.5 bg-orange-600 px-2.5 font-bold text-white">VAF</div>
                        <div className="flex h-full items-center gap-1.5 bg-[#3a2f25] px-2.5 font-bold tracking-wide text-[#fcd9b6]"><Lock size={9} /> NUR LESEN</div>
                        <div className="flex h-full items-center gap-1.5 px-2.5 font-mono">{cf.path}</div>
                        <div className="hidden h-full items-center gap-1.5 px-2.5 sm:flex">{cf.fileCount.toLocaleString('de-DE')} Dateien</div>
                        {query && <div className="flex h-full items-center gap-1.5 bg-[#2a2f45] px-2.5 font-semibold text-[#fcd9b6]">{hitCount} Treffer</div>}
                    </div>
                </div>
            </div>
        );
    }

    if (mode === 'dock' && agentKind === 'librarian') {
        // ── Read-only explorer view for the librarian agent: filesystem map as a
        // disk-usage listing, storage/Google-Drive gauges, biggest folders, activity. ──
        const lib = librarianV;  // guaranteed-valid shell while data streams (loading shown via banner)
        const fmtBytes = (b: number): string => {
            if (!b || b < 0) return '0 B';
            const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let n = b;
            while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
            return `${i <= 1 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
        };
        const isLive = inferredPresence === 'online';
        const maxEntry = Math.max(1, ...lib.entries.map(e => e.sizeBytes || 0));
        const maxTop = Math.max(1, ...lib.topFolders.map(f => f.sizeBytes || 0));
        const hasCloud = lib.drives.some(d => d.kind === 'cloud');
        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full flex-col">
                    {librarianLoading && <StartingBanner label="Librarian" />}
                    {/* Header */}
                    <div className="flex h-12 flex-none items-center justify-between border-b border-gray-200 bg-white px-4">
                        <div className="flex min-w-0 items-center gap-3">
                            <div className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-gray-200 bg-white text-orange-600">
                                <FolderOpen size={14} />
                            </div>
                            <div className="min-w-0">
                                <div className="text-xs font-semibold text-gray-900">{agentName && agentName !== 'Sub-Agent' ? agentName : 'Librarian Agent'}</div>
                                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 flex-none rounded-full", presenceTone)} />
                                    <span className="truncate">Analysiert: Speicher &amp; Dateien · <span className="font-mono">{lib.root || '~'}</span></span>
                                </div>
                            </div>
                        </div>
                        <div className="flex flex-none items-center gap-2">
                            <span className="flex items-center gap-1.5 rounded-md bg-orange-50 px-2 py-1 text-[9px] font-extrabold tracking-wider text-orange-700"><Lock size={9} /> READ-ONLY</span>
                            <span className="flex items-center gap-1.5 rounded-full bg-orange-50 px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider text-orange-700">
                                {isLive && lib.stage !== 'done' && <Loader2 size={9} className="animate-spin" />}
                                {lib.stage === 'done' ? 'Fertig' : 'Scanning'}{lib.totalFiles ? ` · ${lib.totalFiles.toLocaleString('de-DE')} Dateien` : ''}
                            </span>
                            <button onClick={onClose} className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600" aria-label="Close"><X size={14} /></button>
                        </div>
                    </div>

                    <div className="flex min-h-0 flex-1">
                        {/* ── Explorer (filesystem map / disk usage) ── */}
                        <div className="flex min-w-0 flex-1 flex-col bg-[#e9eaee]">
                            <div className="flex h-9 flex-none items-center gap-2 border-b border-gray-200 bg-[#f7f8fa] px-4">
                                <Folder size={12} className="text-gray-400" />
                                <span className="font-mono text-[11px] text-gray-600">{lib.root || '~'}</span>
                                {lib.search?.query && (
                                    <span className="ml-auto flex items-center gap-1.5 rounded-md border border-gray-200 bg-white px-2 py-1 text-[11px] text-gray-700">
                                        <Search size={11} className="text-gray-400" />
                                        <span className="font-mono text-orange-700">{lib.search.query}</span>
                                        {typeof lib.search.hits === 'number' && <span className="text-[10px] text-gray-400">{lib.search.hits} Treffer</span>}
                                    </span>
                                )}
                            </div>
                            <div ref={librarianViewerRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-3.5">
                                <div className="mb-2.5 flex items-baseline gap-2.5">
                                    <div className="text-[22px] font-bold text-gray-900">{fmtBytes(lib.totalSize)}</div>
                                    <div className="text-[11px] text-gray-400">{lib.totalFiles.toLocaleString('de-DE')} Dateien · {lib.totalFolders.toLocaleString('de-DE')} Ordner gescannt</div>
                                    <div className="ml-auto flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-orange-700">
                                        {lib.stage === 'done' ? <CheckCircle2 size={11} className="text-emerald-500" /> : <Loader2 size={11} className="animate-spin" />}
                                        {lib.stage === 'done' ? 'Map bereit' : 'Map wird aufgebaut'}
                                    </div>
                                </div>
                                <div className="flex flex-col gap-[3px]">
                                    {lib.entries.map((e, i) => {
                                        const big = (e.sizeBytes || 0) >= maxEntry * 0.5;
                                        return (
                                            <div key={`${e.name}-${i}`} className={cn("flex items-center gap-2.5 rounded-lg border bg-white px-2.5 py-1.5", big ? "border-orange-200 ring-1 ring-orange-50" : "border-gray-100")}>
                                                <span className="flex h-5 w-5 flex-none items-center justify-center rounded-md bg-orange-50 text-orange-700"><Folder size={12} /></span>
                                                <span className="truncate text-[12.5px] font-semibold text-gray-800">{e.name}</span>
                                                {e.gd && <span className="flex-none rounded bg-blue-50 px-1.5 py-px text-[8px] font-extrabold uppercase tracking-wide text-blue-600">Drive</span>}
                                                {typeof e.items === 'number' && <span className="flex-none text-[10px] text-gray-400">{e.items.toLocaleString('de-DE')} Dateien</span>}
                                                <span className="flex-1" />
                                                <span className="h-[5px] w-40 flex-none overflow-hidden rounded-full bg-gray-100"><span className="block h-full rounded-full bg-orange-500" style={{ width: `${Math.round((e.sizeBytes || 0) / maxEntry * 100)}%` }} /></span>
                                                <span className="w-16 flex-none text-right text-[11.5px] font-bold tabular-nums text-gray-700">{fmtBytes(e.sizeBytes)}</span>
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>
                        </div>

                        {/* ── Sidebar: drives / biggest folders / activity ── */}
                        <div className="flex w-[34%] min-w-[300px] max-w-[400px] flex-none flex-col border-l border-gray-200 bg-white">
                            <div className="flex flex-none flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Datenträger &amp; Cloud<span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">{lib.drives.length} Quellen</span></div>
                                <div className="flex flex-col gap-2.5 px-3.5 pb-3 pt-1">
                                    {lib.drives.map((d, i) => {
                                        const pct = d.totalBytes > 0 ? Math.round(d.usedBytes / d.totalBytes * 100) : 0;
                                        const tone = pct >= 90 ? 'bg-red-500' : pct >= 75 ? 'bg-amber-500' : 'bg-emerald-500';
                                        const Icon = d.kind === 'cloud' ? Cloud : d.kind === 'home' ? Folder : HardDrive;
                                        const cloudNoQuota = d.kind === 'cloud' && d.totalBytes === 0;
                                        return (
                                            <div key={`${d.name}-${i}`}>
                                                <div className="mb-1 flex items-baseline gap-2">
                                                    <span className="flex items-center gap-1.5 text-[11.5px] font-semibold text-gray-800"><Icon size={12} className={d.kind === 'cloud' ? 'text-blue-500' : 'text-gray-500'} />{d.name}</span>
                                                    {cloudNoQuota
                                                        ? <span className="ml-auto text-[10px] text-emerald-600">verbunden</span>
                                                        : <span className="ml-auto text-[10px] text-gray-400">{fmtBytes(Math.max(0, d.totalBytes - d.usedBytes))} frei</span>}
                                                </div>
                                                {!cloudNoQuota && (
                                                    <>
                                                        <span className="block h-[7px] overflow-hidden rounded-full bg-gray-100"><span className={cn("block h-full rounded-full", tone)} style={{ width: `${pct}%` }} /></span>
                                                        <div className="mt-1 text-[10.5px] tabular-nums text-gray-500">{fmtBytes(d.usedBytes)} / {fmtBytes(d.totalBytes)} · {pct}% belegt</div>
                                                    </>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>
                            <div className="flex min-h-0 flex-1 flex-col border-b border-gray-100">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Größte Ordner<span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">Top {lib.topFolders.length}</span></div>
                                <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-3 pb-3 pt-0.5">
                                    {lib.topFolders.map((f, i) => (
                                        <div key={`${f.name}-${i}`} className="flex items-center gap-2.5 rounded-lg border border-gray-100 px-2.5 py-1.5">
                                            <span className="w-4 flex-none text-center text-[10px] font-bold text-gray-400">{i + 1}</span>
                                            <span className="min-w-0 flex-1 truncate text-[11.5px] text-gray-700">{f.name}</span>
                                            <span className="h-1 w-14 flex-none overflow-hidden rounded bg-gray-100"><span className="block h-full bg-orange-500" style={{ width: `${Math.round((f.sizeBytes || 0) / maxTop * 100)}%` }} /></span>
                                            <span className="w-14 flex-none text-right text-[10.5px] font-bold tabular-nums text-gray-700">{fmtBytes(f.sizeBytes)}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                            <div className="flex h-[32%] min-h-[120px] flex-none flex-col">
                                <div className="flex h-8 flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Activity</div>
                                <div ref={librarianActivityRef} className="min-h-0 flex-1 overflow-y-auto px-3.5 pb-3 font-mono text-[10px] leading-relaxed text-gray-500">
                                    {lib.activity.map((a, i) => (
                                        <div key={i}>
                                            <span className={cn(a.cls === 'ok' && 'text-emerald-600', a.cls === 'info' && 'text-blue-600', a.cls === 'warn' && 'text-amber-600', a.cls === 'scan' && 'text-orange-700')}>{a.text}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* Statusbar */}
                    <div className="flex h-6 flex-none items-center bg-[#1f2335] text-[10.5px] text-[#c8d0e8]">
                        <div className="flex h-full items-center gap-1.5 bg-orange-600 px-2.5 font-bold text-white">VAF</div>
                        <div className="flex h-full items-center gap-1.5 bg-[#3a2f25] px-2.5 font-bold tracking-wide text-[#fcd9b6]"><Lock size={9} /> NUR LESEN</div>
                        <div className="hidden h-full items-center gap-1.5 px-2.5 sm:flex">{lib.totalFiles.toLocaleString('de-DE')} Dateien · {lib.totalFolders.toLocaleString('de-DE')} Ordner</div>
                        <div className="flex h-full items-center gap-1.5 px-2.5"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> {fmtBytes(lib.totalSize)}</div>
                        <div className="hidden h-full items-center gap-1.5 px-2.5 md:flex">Lokal{hasCloud ? ' · Google Drive' : ''}</div>
                        <div className="ml-auto flex h-full items-center gap-1.5 px-2.5 font-mono">{lib.root || '~'} (lesend)</div>
                    </div>
                </div>
            </div>
        );
    }

    if (mode === 'dock' && agentKind === 'coder') {
        // ── VS-Code style view for the coding agent ───────────────────────
        // Left: header / file tabs / live editor / console. Right sidebar:
        // Explorer, Tasks, Source Control. Bottom: status bar.
        const coder = coderV;  // guaranteed-valid shell while data streams (loading shown via banner)
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
        // No live code stream (e.g. the agent is doing edit_file, which does not stream new
        // content) → show the edited file's red/green diff in the editor, jumped to the changed
        // hunks, instead of a blank "Waiting for code…". Prefer the file being edited.
        const diffMap = coder.diffs || {};
        const activeDiff = (!openedFile && editorLines.length === 0) ? (() => {
            const keys = Object.keys(diffMap);
            if (keys.length === 0) return null;
            const curName = (displayFile || '').split('/').pop() || '';
            if (curName && diffMap[curName]) return { name: curName, diff: diffMap[curName] };
            const w = coder.fileTree.find(f => (f.status === 'W' || f.status === 'M') && diffMap[f.name]);
            if (w) return { name: w.name, diff: diffMap[w.name] };
            return { name: keys[0], diff: diffMap[keys[0]] };
        })() : null;

        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full flex-col">
                    {coderLoading && <StartingBanner label="Coder" />}
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
                                }) : activeDiff ? (
                                    <div className="bg-white text-gray-800">
                                        <div className="sticky top-0 z-10 flex items-center gap-1.5 border-b border-gray-100 bg-white/95 px-3 py-1 text-[10px] font-bold text-gray-500">
                                            <Pencil size={11} className="text-violet-500" /> Editing {activeDiff.name} — live changes
                                        </div>
                                        <DiffLines diff={activeDiff.diff} />
                                    </div>
                                ) : (
                                    <div className={cn("flex items-center gap-2 px-4 py-2 text-xs", editorDark ? "text-gray-600" : "text-gray-300")}>
                                        <Loader2 size={13} className="animate-spin opacity-60" />
                                        Waiting for code…
                                    </div>
                                )}
                            </div>

                            {/* Bottom panel: Console / Linter / Telemetry */}
                            <div className="flex h-[150px] flex-none flex-col border-t border-gray-200 bg-white">
                                <div className="flex h-7 flex-none items-center gap-4 border-b border-gray-100 px-4">
                                    {(['console', 'linter', 'telemetry', 'diff'] as const).map(tab => {
                                        const diffCount = Object.keys(coder.diffs || {}).length;
                                        return (
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
                                            {tab === 'diff' && diffCount > 0 ? `diff (${diffCount})` : tab}
                                        </button>
                                        );
                                    })}
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
                                                    <div
                                                        key={`${index}-${line.slice(0, 20)}`}
                                                        className="break-all whitespace-pre-wrap leading-5"
                                                    >
                                                        {line}
                                                    </div>
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
                                {activeConsoleTab === 'diff' && (
                                    <div className="min-h-0 flex-1 overflow-y-auto py-1 text-[11px]">
                                        {Object.keys(coder.diffs || {}).length > 0 ? (
                                            Object.entries(coder.diffs || {}).map(([name, diff]) => (
                                                <div key={name} className="mb-1.5">
                                                    <div className="sticky top-0 z-10 border-b border-gray-100 bg-white px-3 py-1 font-mono text-[10px] font-bold text-gray-500">{name}</div>
                                                    <DiffLines diff={diff} />
                                                </div>
                                            ))
                                        ) : (
                                            <div className="px-4 py-2 text-gray-300">No changes yet — edits will show here as a red/green diff.</div>
                                        )}
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

    // Browser agent window: full-width live browser (chrome + widescreen screenshot) with a
    // bottom dock (task / action plan / visited URLs / activity). The numbered element boxes
    // come from the screenshot itself (browser-use highlights them).
    if (mode === 'dock' && agentKind === 'browser') {
        const b = browserV;  // guaranteed-valid shell while data streams (screenshot/banner cover loading)
        const isLive = inferredPresence === 'online';
        const verbLabel: Record<string, string> = { nav: 'Navigate', click: 'Click', type: 'Type', read: 'Read', scroll: 'Scroll' };
        const verbStyle: Record<string, string> = { nav: 'bg-sky-50 text-sky-700', click: 'bg-amber-50 text-amber-700', type: 'bg-indigo-50 text-indigo-700', read: 'bg-teal-50 text-teal-700', scroll: 'bg-gray-100 text-gray-500' };
        const activeAction = b?.actions?.find(a => a.status === 'active') || (b?.actions?.length ? b.actions[b.actions.length - 1] : null);
        const url = browserUrl || b?.url || '';
        const stepTxt = b && b.maxSteps ? `Schritt ${b.step}/${b.maxSteps}` : (b?.step ? `Schritt ${b.step}` : '');
        const shortUrl = (u: string) => (u || 'about:blank').replace(/^https?:\/\//, '');
        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full flex-col">
                    {browserLoading && <StartingBanner label="Browser Agent" />}
                    {/* Header */}
                    <div className="flex h-12 flex-none items-center justify-between border-b border-gray-200 bg-white px-4">
                        <div className="flex min-w-0 items-center gap-3">
                            <div className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-gray-200 bg-white text-sky-600"><Globe size={14} /></div>
                            <div className="min-w-0">
                                <div className="text-xs font-semibold text-gray-900">{agentName && agentName !== 'Sub-Agent' ? agentName : 'Browser Agent'}</div>
                                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 flex-none rounded-full", presenceTone)} />
                                    <span className="truncate">{b?.task ? `Surft autonom: ${b.task}` : (displayStatus || 'Browser')}</span>
                                </div>
                            </div>
                        </div>
                        <div className="flex flex-none items-center gap-2">
                            {isLive && <span className="flex items-center gap-1.5 rounded-md bg-red-50 px-2 py-1 text-[9px] font-extrabold uppercase tracking-wider text-red-500"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-red-500" /> LIVE</span>}
                            <span className="flex items-center gap-1.5 rounded-full bg-sky-50 px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider text-sky-700">
                                {isLive && b?.status !== 'done' && <Loader2 size={9} className="animate-spin" />}
                                {stepTxt || 'Browser'}{activeAction ? ` · ${verbLabel[activeAction.verb] ?? activeAction.verb}` : ''}
                            </span>
                            <button onClick={onClose} className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600" aria-label="Close"><X size={14} /></button>
                        </div>
                    </div>

                    <div className="flex min-h-0 flex-1 flex-col">
                        {/* Browser: chrome + widescreen screenshot (letterboxed) */}
                        <div className="flex min-h-0 flex-1 flex-col bg-[#1e2430]">
                            <div className="flex h-10 flex-none items-center gap-2 border-b border-black/40 bg-[#2a313f] px-3">
                                <div className="flex gap-0.5">
                                    <span className="flex h-6 w-6 items-center justify-center rounded-md text-[#9aa4b5] hover:bg-[#384151]"><ChevronLeft size={14} /></span>
                                    <span className="flex h-6 w-6 items-center justify-center rounded-md text-[#9aa4b5] opacity-40"><ChevronRight size={14} /></span>
                                    <span className="flex h-6 w-6 items-center justify-center rounded-md text-[#9aa4b5] hover:bg-[#384151]"><RotateCw size={12} /></span>
                                </div>
                                <div className="flex h-7 min-w-0 flex-1 items-center gap-2 rounded-lg border border-black/40 bg-[#1b202b] px-3">
                                    <Lock size={10} className="flex-none text-emerald-400" />
                                    <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-[#c7cedb]">{url || 'about:blank'}</span>
                                    {isLive && b?.status !== 'done' && <Loader2 size={11} className="flex-none animate-spin text-sky-400" />}
                                </div>
                                <span className="flex-none rounded bg-[#384151] px-2 py-1 text-[9px] text-[#9aa4b5]">1 Tab</span>
                            </div>
                            <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden p-3">
                                {browserFrame
                                    ? <img src={`data:image/jpeg;base64,${browserFrame}`} alt="Browser live view" draggable={false} className="max-h-full max-w-full rounded-lg shadow-[0_4px_24px_rgba(0,0,0,0.35)]" />
                                    : <div className="flex items-center gap-2 text-[12px] text-[#9aa4b5]"><Loader2 size={14} className="animate-spin" /> Browser startet…</div>}
                            </div>
                        </div>

                        {/* Bottom dock: Aufgabe · Aktionen · Verlauf · Activity */}
                        <div className="flex h-[266px] flex-none border-t border-gray-200 bg-white">
                            <div className="flex min-w-0 flex-col border-r border-gray-100" style={{ flex: 1.15 }}>
                                <div className="flex h-[30px] flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Aufgabe</div>
                                <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
                                    <div className="rounded-lg border border-sky-100 bg-sky-50 px-3 py-2.5 text-[11.5px] leading-relaxed text-sky-900"><span className="mb-1 block text-[8.5px] font-extrabold uppercase tracking-wider text-sky-600">Ziel</span>{b?.task || '—'}</div>
                                </div>
                            </div>
                            <div className="flex min-w-0 flex-col border-r border-gray-100" style={{ flex: 1.75 }}>
                                <div className="flex h-[30px] flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Aktionen<span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">{b?.maxSteps ? `${b.step}/${b.maxSteps}` : (b?.actions?.length ?? 0)}</span></div>
                                <div ref={browserActionsRef} className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-2.5 pb-3">
                                    {(b?.actions ?? []).length === 0
                                        ? <div className="px-1 py-1 text-[9.5px] text-gray-400">Wartet auf den ersten Schritt…</div>
                                        : b!.actions.map((a, i) => (
                                            <div key={i} className={cn("flex items-start gap-2 rounded-lg border px-2 py-1.5", a.status === 'active' ? "border-sky-200 bg-sky-50/60" : "border-gray-100")}>
                                                <span className={cn("mt-px flex h-4 w-4 flex-none items-center justify-center rounded-full", a.status === 'active' ? "bg-sky-100 text-sky-700" : "bg-emerald-50 text-emerald-600")}>{a.status === 'active' ? <Loader2 size={9} className="animate-spin" /> : <CheckCircle2 size={10} />}</span>
                                                <div className="min-w-0 flex-1">
                                                    <span className={cn("mr-1.5 rounded px-1.5 py-px text-[8px] font-extrabold uppercase tracking-wide", verbStyle[a.verb] ?? 'bg-gray-100 text-gray-500')}>{verbLabel[a.verb] ?? a.verb}</span>
                                                    <span className="text-[11px] text-gray-700">{a.text}</span>
                                                </div>
                                            </div>
                                        ))}
                                </div>
                            </div>
                            <div className="flex min-w-0 flex-col border-r border-gray-100" style={{ flex: 1.1 }}>
                                <div className="flex h-[30px] flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Verlauf<span className="ml-auto rounded-full bg-gray-100 px-2 py-px text-[8px] font-semibold text-gray-400">{b?.history?.length ?? 0}</span></div>
                                <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2.5 pb-3">
                                    {(b?.history ?? []).length === 0
                                        ? <div className="px-1 py-1 text-[9.5px] text-gray-400">—</div>
                                        : b!.history.map((h, i) => (
                                            <div key={i} className={cn("flex items-center gap-2 rounded px-2 py-1 text-[11px]", i === b!.history.length - 1 && "bg-sky-50")}>
                                                <Globe size={11} className="flex-none text-gray-400" />
                                                <span className={cn("min-w-0 flex-1 truncate font-mono", i === b!.history.length - 1 ? "text-sky-700" : "text-gray-500")}>{shortUrl(h)}</span>
                                            </div>
                                        ))}
                                </div>
                            </div>
                            <div className="flex min-w-0 flex-col" style={{ flex: 1.15 }}>
                                <div className="flex h-[30px] flex-none items-center px-3.5 text-[9px] font-bold uppercase tracking-widest text-gray-400">Activity</div>
                                <div ref={browserActivityRef} className="min-h-0 flex-1 overflow-y-auto px-3.5 pb-3 font-mono text-[10px] leading-relaxed text-gray-500">
                                    {consoleLines.slice(-40).map((line, i) => <div key={i} className="whitespace-pre-wrap break-words">{line}</div>)}
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* Statusbar */}
                    <div className="flex h-6 flex-none items-center bg-[#1f2335] text-[10.5px] text-[#c8d0e8]">
                        <div className="flex h-full items-center gap-1.5 bg-sky-600 px-2.5 font-bold text-white">VAF</div>
                        <div className="flex h-full items-center gap-1.5 px-2.5 font-mono">{shortUrl(url).slice(0, 48)}</div>
                        {stepTxt && <div className="hidden h-full items-center gap-1.5 px-2.5 sm:flex">{stepTxt}</div>}
                        {activeAction && <div className="flex h-full items-center gap-1.5 px-2.5"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> {verbLabel[activeAction.verb] ?? activeAction.verb}</div>}
                        <div className="hidden h-full items-center gap-1.5 bg-[#2a2f45] px-2.5 text-[#bfe3ff] md:flex">Vision: {b?.vision ?? 'auto'}</div>
                        <div className="ml-auto flex h-full items-center gap-1.5 px-2.5">{b?.status === 'done' ? 'fertig' : 'läuft'}</div>
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
                                    onLoad={() => { if (stickToBottomRef.current) scrollConsoleToBottom(); }}
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
                                            <div
                                                key={`${index}-${line.slice(0, 20)}`}
                                                className="break-all whitespace-pre-wrap leading-5"
                                            >
                                                {line}
                                            </div>
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
                                    onLoad={() => { if (stickToBottomRef.current) scrollConsoleToBottom(); }}
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
                                            <div
                                                key={`${index}-${line.slice(0, 20)}`}
                                                className="break-all whitespace-pre-wrap leading-5"
                                            >
                                                {line}
                                            </div>
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