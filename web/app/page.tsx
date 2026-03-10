'use client';

import React, { useCallback, useEffect, useMemo, useState, useRef, Fragment, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';
import {
    Send, Menu, Plus, MessageSquare, Brain, Bot, User, Trash2, Edit2, Paperclip,
    Activity, GitBranch, Workflow, CheckCircle2, ShieldAlert, Loader2,
    Settings, Mic, MicOff, Check, ChevronRight, Zap, Volume2, Square, Wrench, FileText, Calendar, Bell
} from 'lucide-react';
import { cn, getApiBase, getWsBase } from '@/lib/utils';
import { loadSessionCache, trimSessionCache, saveSessionCache } from '@/lib/sessionCache';
import SettingsModal, { type SettingsModalProps } from '@/components/SettingsModal';
import AutomationCalendarModal from '@/components/AutomationCalendarModal';
import CreateAutomationPopup, { type CreateAutomationPayload, type EditAutomationTask } from '@/components/CreateAutomationPopup';
import NotificationsModal, { type NotificationItem } from '@/components/NotificationsModal';
import SubAgentWindow from '@/components/SubAgentWindow';
import DocumentEditor from '@/components/DocumentEditor';
import DocumentViewer, { CHIP_BG_CLASSES, type InsertedSelectionRange } from '@/components/DocumentViewer';
import { ToolMessage } from '@/components/ToolMessage';
import VAFWorkflowRuntime from '@/components/workflows/VAFWorkflowRuntime';
import { useWorkflowStore } from '@/components/workflows/stores/workflowStore';
import { WorkflowChatElement } from '@/components/workflows/WorkflowChatElement';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Types
type Message = {
    role: 'user' | 'assistant' | 'system' | 'tool' | 'workflow';
    content: string; // For tools: this is the result
    timestamp: number;
    /** Attachments shown on user messages (name + mimeType only; data not stored) */
    files?: { name: string; mimeType: string }[];
    /** Document Viewer was open when this message was sent; list of document names (for indicator under bubble) */
    sidebarDocs?: string[];
    // Extra fields for tools
    toolId?: string;
    toolName?: string;
    toolStatus?: 'running' | 'completed' | 'error';
    toolArgs?: any;
    toolStartTime?: number;
    toolEndTime?: number;
    // Extra fields for workflows
    workflowId?: string;
    workflowName?: string;
    initialSteps?: number;
};

type Session = {
    id: string;
    title: string;
    messageCount?: number;
    /** Thinking-mode run shown in chat list with brain icon */
    source?: 'thinking';
};

/** Replace plain-text range [start, end] in HTML with newText; returns new HTML. */
function replaceTextInHtml(html: string, start: number, end: number, newText: string): string {
    const wrap = `<div id="__replaceRoot">${html}</div>`;
    const doc = new DOMParser().parseFromString(wrap, 'text/html');
    const root = doc.getElementById('__replaceRoot');
    if (!root) return html;
    const textNodes: { node: Text; nodeStart: number; nodeEnd: number }[] = [];
    let offset = 0;
    const walk = (node: Node) => {
        if (node.nodeType === Node.TEXT_NODE) {
            const len = (node.textContent || '').length;
            textNodes.push({ node: node as Text, nodeStart: offset, nodeEnd: offset + len });
            offset += len;
        } else {
            node.childNodes.forEach(walk);
        }
    };
    root.childNodes.forEach(walk);
    let first = true;
    for (const { node, nodeStart, nodeEnd } of textNodes) {
        const overlapStart = Math.max(0, start - nodeStart);
        const overlapEnd = Math.min(nodeEnd - nodeStart, end - nodeStart);
        if (overlapStart >= overlapEnd) continue;
        const overlapLen = overlapEnd - overlapStart;
        const replaceWith = first ? newText : '';
        first = false;
        node.replaceData(overlapStart, overlapLen, replaceWith);
    }
    return root.innerHTML;
}

/**
 * Convert audio Blob (webm/opus) to WAV format for better Whisper compatibility.
 * Uses Web Audio API to decode and re-encode as 16-bit PCM WAV.
 */
async function convertToWav(blob: Blob): Promise<Blob> {
    const arrayBuffer = await blob.arrayBuffer();
    const audioContext = new AudioContext({ sampleRate: 16000 });

    try {
        const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
        const numberOfChannels = 1; // Mono for STT
        const sampleRate = 16000;
        const length = audioBuffer.length;

        // Get audio data (convert to mono if stereo)
        const channelData = audioBuffer.getChannelData(0);

        // Create WAV file
        const wavBuffer = new ArrayBuffer(44 + length * 2);
        const view = new DataView(wavBuffer);

        // WAV header
        const writeString = (offset: number, str: string) => {
            for (let i = 0; i < str.length; i++) {
                view.setUint8(offset + i, str.charCodeAt(i));
            }
        };

        writeString(0, 'RIFF');
        view.setUint32(4, 36 + length * 2, true);
        writeString(8, 'WAVE');
        writeString(12, 'fmt ');
        view.setUint32(16, 16, true); // Subchunk1Size
        view.setUint16(20, 1, true); // AudioFormat (PCM)
        view.setUint16(22, numberOfChannels, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * numberOfChannels * 2, true); // ByteRate
        view.setUint16(32, numberOfChannels * 2, true); // BlockAlign
        view.setUint16(34, 16, true); // BitsPerSample
        writeString(36, 'data');
        view.setUint32(40, length * 2, true);

        // Write audio data as 16-bit PCM
        let offset = 44;
        for (let i = 0; i < length; i++) {
            const sample = Math.max(-1, Math.min(1, channelData[i]));
            view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7FFF, true);
            offset += 2;
        }

        return new Blob([wavBuffer], { type: 'audio/wav' });
    } finally {
        await audioContext.close();
    }
}

/** Same calendar day (local date)? */
function isSameDay(ts1: number, ts2: number): boolean {
    const d1 = new Date(ts1);
    const d2 = new Date(ts2);
    return d1.getFullYear() === d2.getFullYear() && d1.getMonth() === d2.getMonth() && d1.getDate() === d2.getDate();
}

function formatDayLabel(ts: number): string {
    return new Date(ts).toLocaleDateString('en-US', { day: 'numeric', month: 'long', year: 'numeric' });
}

/** Short message time (Messenger-style): today shows only time, yesterday/older shows date.
 * Uses user's time_format from Settings (24h or 12h) when provided. */
function formatMessageTime(ts: number, timeFormat?: '24h' | '12h'): string {
    const d = new Date(ts);
    const now = new Date();
    const today = now.getDate() === d.getDate() && now.getMonth() === d.getMonth() && now.getFullYear() === d.getFullYear();
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    const isYesterday = yesterday.getDate() === d.getDate() && yesterday.getMonth() === d.getMonth() && yesterday.getFullYear() === d.getFullYear();
    const hour12 = timeFormat === '24h' ? false : timeFormat === '12h' ? true : undefined;
    const timeOpts: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' };
    if (hour12 !== undefined) timeOpts.hour12 = hour12;
    const time = d.toLocaleTimeString('en-US', timeOpts);
    if (today) return time;
    if (isYesterday) return `Yesterday ${time}`;
    if (d.getFullYear() === now.getFullYear()) return `${d.toLocaleDateString('en-US', { weekday: 'short' })} ${time}`;
    return d.toLocaleDateString('en-US', { day: 'numeric', month: 'short' }) + ' ' + time;
}

/** Day separator in chat: date at top (end) and bottom (continuation). */
function DaySeparator({ endDate, startDate }: { endDate: number; startDate: number }) {
    const t = useTranslations('main');
    return (
        <div className="flex flex-col items-stretch py-4">
            <div className="text-right text-xs text-gray-400 pr-1" title={t('chatEndedOnThisDay')}>{formatDayLabel(endDate)}</div>
            <div className="border-t border-gray-200 my-1" aria-hidden />
            <div className="text-right text-xs text-gray-400 pr-1" title={t('continuedOnThisDay')}>{formatDayLabel(startDate)}</div>
        </div>
    );
}

/** Strip backend "--- FILE: name ---\n...\n----------------" blocks from message content for display. Returns cleaned text and parsed file names (for chips when msg.files is missing after reload). */
function stripAttachmentBlocks(content: string): { text: string; fileNames: string[] } {
    if (!content || !content.includes('--- FILE:')) return { text: content, fileNames: [] };
    const fileNames: string[] = [];
    const re = /\n\n--- FILE: ([^\n]+) ---\n[\s\S]*?\n----------------\n?/g;
    let match: RegExpExecArray | null;
    while ((match = re.exec(content)) !== null) fileNames.push(match[1].trim());
    const text = content.replace(re, '').trim();
    return { text, fileNames };
}

/** Strip OpenAI tool_calls JSON blocks from message content for display */
function stripToolCallsJSON(content: string): string {
    if (!content) return content;

    // Match JSON blocks: {"tool_calls": [...]} or malformed {"tool_calls": } or {"tool_calls":}
    // Handles empty, empty array, and populated array variants
    const toolCallsPattern = /\{"tool_calls":\s*(?:\[[\s\S]*?\]\s*)?\}/g;

    return content.replace(toolCallsPattern, '').trim();
}

/** Extract file paths from text (Windows and Unix paths with common extensions) */
function extractFilePaths(text: string): { path: string; start: number; end: number }[] {
    const results: { path: string; start: number; end: number }[] = [];
    // Match Windows paths (C:\...) and Unix paths (/...) with common file extensions
    const pathRegex = /(?:[A-Za-z]:\\[^\s<>"'`\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?|png|jpg|jpeg|gif|svg|mp[34]|wav))|(?:\/[^\s<>"'`\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?|png|jpg|jpeg|gif|svg|mp[34]|wav))/gi;
    let match;
    while ((match = pathRegex.exec(text)) !== null) {
        results.push({ path: match[0], start: match.index, end: match.index + match[0].length });
    }
    return results;
}

// Helper to parse and merge thinking blocks
// Returns: { thought, answer, isThinkingComplete }
const parseContent = (content: string): { thought: string | null; answer: string; isThinkingComplete: boolean } => {
    if (!content) return { thought: null, answer: "", isThinkingComplete: true };

    // Clean Rich markup tags and "resposta" prefix
    let clean = content.replace(/[\[][\/]?\w+\s*\w+[\]]/g, '').replace(/^resposta\s*/i, '');

    // Normalize different thinking tag formats to <think>
    clean = clean.replace(/<thinking>/gi, '<think>').replace(/<\/thinking>/gi, '</think>');

    // Merge consecutive thinking blocks
    let merged = clean.replace(/<\/think>\s*<think>/g, ' ');

    const openTag = "<think>";
    const closeTag = "</think>";
    const openIndex = merged.indexOf(openTag);

    // Method 1: Explicit <think> tags
    if (openIndex !== -1) {
        const closeIndex = merged.lastIndexOf(closeTag);
        if (closeIndex !== -1 && closeIndex > openIndex) {
            // Complete thinking block - has both open and close tags
            const thought = merged.substring(openIndex + openTag.length, closeIndex).trim();
            const answer = (merged.substring(0, openIndex) + merged.substring(closeIndex + closeTag.length)).trim();
            // Safeguard: content inside think tags may be a user-facing answer (API models sometimes misuse tags)
            const looksLikeUserAnswer = /\b(was ich über dich weiß|hier die Übersicht|Das sind die Infos|Name:\s*[A-Z]|Standort:)/i.test(thought);
            if (answer.length < 50 && looksLikeUserAnswer) {
                return { thought: null, answer: thought, isThinkingComplete: true };
            }
            return { thought, answer, isThinkingComplete: true };
        } else {
            // Incomplete thinking - has open tag but no close tag (still streaming)
            const thought = merged.substring(openIndex + openTag.length).trim();
            const answer = merged.substring(0, openIndex).trim();
            return { thought, answer, isThinkingComplete: false };
        }
    }

    // Method 2: Heuristic detection of thinking patterns (VQ-1 style, no tags)
    // Look for reasoning paragraphs at the start that end with a clear transition.
    // NOTE: Do NOT include "Okay," / "Okay I" - they often start the actual answer in DE/EN (e.g. "Okay, hier die Übersicht")
    const thinkingIndicators = [
        'First, I', 'I called', 'I need to', 'I should', 'I will',
        'Now, I', 'Now I', 'Let me', 'The user',
        'I\'ll check', 'I\'ll use', 'I\'ll need'
    ];

    // Check if content starts with thinking pattern
    const startsWithThinking = thinkingIndicators.some(ind =>
        merged.trimStart().toLowerCase().startsWith(ind.toLowerCase())
    );

    if (startsWithThinking) {
        // Find where actual answer starts (usually after double newline + formatted content)
        // Look for: **Header**, bullet lists, or German response start
        const answerPatterns = [
            /\n\n\*\*[A-ZÄÖÜ]/,           // **Bold Header**
            /\n\n[A-ZÄÖÜ][a-zäöüß]+\s+ist\s/, // "Google ist..."
            /\n\n- [A-ZÄÖÜ]/,              // Bullet list
            /\n\n\d+\.\s+/,                // Numbered list
            /\n\nHallo[,!]/i,              // German greeting
            /\n\nDie\s+/,                  // German article start
            /\n\nDas\s+/,
            /\n\nDer\s+/,
        ];

        let splitIndex = -1;
        for (const pattern of answerPatterns) {
            const match = merged.match(pattern);
            if (match && match.index !== undefined) {
                if (splitIndex === -1 || match.index < splitIndex) {
                    splitIndex = match.index;
                }
            }
        }

        if (splitIndex > 50) { // Ensure we have meaningful thinking content
            const thought = merged.substring(0, splitIndex).trim();
            const answer = merged.substring(splitIndex).trim();
            return { thought, answer, isThinkingComplete: true };
        }
    }

    return { thought: null, answer: merged, isThinkingComplete: true };
};

/** Detect thinking-mode system prompt so we can hide it in the Web UI when viewing a thinking session. */
function isThinkingModePrompt(content: string): boolean {
    if (!content || typeof content !== 'string') return false;
    const c = content.trim();
    if (c.length < 200) return false;
    return (
        (c.includes('You are the main agent in **Thinking Mode**') || c.includes('You are the main agent in Thinking Mode')) &&
        (c.includes('act on their behalf') || c.includes('That concludes this pass'))
    );
}

// Parse [WORKFLOW_ASYNC:taskId:workflowId] Workflow 'Name' ... from assistant text for card display
const WORKFLOW_ASYNC_REGEX = /\[WORKFLOW_ASYNC:([^:]+):([^\]]+)\]\s*Workflow\s+'([^']+)'[^\n]*(?:\n\n)?([\s\S]*)/;
function parseWorkflowAsync(answer: string): { taskId: string; workflowId: string; name: string; rest: string } | null {
    const m = (answer || '').trim().match(WORKFLOW_ASYNC_REGEX);
    if (!m) return null;
    return { taskId: m[1], workflowId: m[2], name: m[3], rest: m[4].trim() };
}

const normalizeDownloadHref = (rawHref: string): string => {
    if (!rawHref) return rawHref;
    const base = getApiBase();
    if (rawHref.startsWith('sandbox:/')) {
        const path = rawHref.replace(/^sandbox:\/*/, '');
        return `${base}/api/file?path=${encodeURIComponent(path)}`;
    }

    const looksLikeWindowsPath = /^[a-zA-Z]:[\\/]/.test(rawHref);
    const looksLikeUnixPath = rawHref.startsWith('/');
    if (looksLikeWindowsPath || looksLikeUnixPath) {
        return `${base}/api/file?path=${encodeURIComponent(rawHref)}`;
    }

    return rawHref;
};

const renderMarkdownLinks = (text: string): React.ReactNode[] => {
    const nodes: React.ReactNode[] = [];
    if (!text) return nodes;

    const linkRegex = /\[([^\]]+)\]\(([^)]+)\)/g;
    let lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = linkRegex.exec(text)) !== null) {
        if (match.index > lastIndex) {
            nodes.push(text.slice(lastIndex, match.index));
        }

        const label = match[1];
        const rawHref = match[2];
        const href = normalizeDownloadHref(rawHref);
        nodes.push(
            <a
                key={`link-${match.index}`}
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="text-gray-700 underline break-all hover:text-gray-900"
            >
                {label}
            </a>
        );

        lastIndex = match.index + match[0].length;
    }

    if (lastIndex < text.length) {
        nodes.push(text.slice(lastIndex));
    }

    return nodes.length > 0 ? nodes : [text];
};

/** Renders markdown in chat bubbles (headings, lists, bold, links, code). Links use normalizeDownloadHref for sandbox/local paths. User messages (dark): single line breaks are preserved as sent. */
const ChatMarkdown = ({ children, dark = false }: { children: string; dark?: boolean }) => {
    const text = dark ? children.replace(/\n/g, "  \n") : children;
    const linkClass = dark
        ? 'text-indigo-200 underline break-all hover:text-white'
        : 'text-gray-700 underline break-all hover:text-gray-900';
    return (
        <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
                a: ({ href, children: linkChildren }) => (
                    <a href={href ? normalizeDownloadHref(href) : href} target="_blank" rel="noopener noreferrer" className={linkClass}>
                        {linkChildren}
                    </a>
                ),
                p: ({ children: pChildren }) => <p className="mb-2 last:mb-0">{pChildren}</p>,
                strong: ({ children: sChildren }) => <strong className="font-semibold">{sChildren}</strong>,
                ul: ({ children: ulChildren }) => <ul className="list-disc list-inside mb-2 space-y-0.5">{ulChildren}</ul>,
                ol: ({ children: olChildren }) => <ol className="list-decimal list-inside mb-2 space-y-0.5">{olChildren}</ol>,
                li: ({ children: liChildren }) => <li className="ml-0">{liChildren}</li>,
                h1: ({ children: c }) => <h1 className="text-lg font-bold mt-3 mb-1 first:mt-0">{c}</h1>,
                h2: ({ children: c }) => <h2 className="text-base font-bold mt-3 mb-1 first:mt-0">{c}</h2>,
                h3: ({ children: c }) => <h3 className="text-sm font-bold mt-2 mb-1 first:mt-0">{c}</h3>,
                h4: ({ children: c }) => <h4 className="text-sm font-semibold mt-2 mb-0.5 first:mt-0">{c}</h4>,
                h5: ({ children: c }) => <h5 className="text-sm font-semibold mt-1 mb-0.5 first:mt-0">{c}</h5>,
                h6: ({ children: c }) => <h6 className="text-sm font-medium mt-1 first:mt-0">{c}</h6>,
                code: ({ className, children: codeChildren }) => {
                    const isBlock = className?.includes('language-');
                    if (isBlock) {
                        const preClass = dark ? 'bg-gray-700 text-gray-200 rounded p-2 text-xs overflow-x-auto my-2' : 'bg-gray-100 text-gray-800 rounded p-2 text-xs overflow-x-auto my-2';
                        return <pre className={preClass}><code>{codeChildren}</code></pre>;
                    }
                    const codeClass = dark ? 'bg-gray-700 text-gray-200 px-1 rounded text-xs' : 'bg-gray-100 text-gray-800 px-1 rounded text-xs';
                    return <code className={codeClass}>{codeChildren}</code>;
                },
            }}
        >
            {text}
        </ReactMarkdown>
    );
};

// Component: Thinking Accordion
// Open while incomplete, auto-close when complete
const ThinkingDetails = ({ thought, isComplete = true }: { thought: string; isComplete?: boolean }) => {
    const [isOpen, setIsOpen] = useState(!isComplete);
    const openedAtRef = useRef<number>(Date.now());
    const closeTimeoutRef = useRef<NodeJS.Timeout | null>(null);
    const scrollRef = useRef<HTMLDivElement | null>(null);
    const MIN_OPEN_MS = 800;
    const CLOSE_DELAY_MS = 400;

    // Auto-update when isComplete changes
    useEffect(() => {
        if (!isComplete) {
            if (closeTimeoutRef.current) {
                clearTimeout(closeTimeoutRef.current);
                closeTimeoutRef.current = null;
            }
            openedAtRef.current = Date.now();
            setIsOpen(true);
            return;
        }

        const elapsed = Date.now() - openedAtRef.current;
        const delay = Math.max(MIN_OPEN_MS - elapsed, 0) + CLOSE_DELAY_MS;
        if (closeTimeoutRef.current) {
            clearTimeout(closeTimeoutRef.current);
        }
        closeTimeoutRef.current = setTimeout(() => {
            setIsOpen(false);
            closeTimeoutRef.current = null;
        }, delay);
        return () => {
            if (closeTimeoutRef.current) {
                clearTimeout(closeTimeoutRef.current);
                closeTimeoutRef.current = null;
            }
        };
    }, [isComplete]);

    useEffect(() => {
        if (!isOpen || !scrollRef.current) return;
        scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }, [thought, isOpen]);

    if (!thought) return null;

    return (
        <div className="mb-3 rounded-xl border border-gray-200 bg-gray-50/50 overflow-hidden w-full max-w-[95%] shadow-sm">
            <button
                type="button"
                onClick={() => {
                    if (closeTimeoutRef.current) {
                        clearTimeout(closeTimeoutRef.current);
                        closeTimeoutRef.current = null;
                    }
                    const next = !isOpen;
                    if (next) {
                        openedAtRef.current = Date.now();
                    }
                    setIsOpen(next);
                }}
                className="w-full px-4 py-2.5 flex items-center justify-between text-[11px] uppercase tracking-wide font-semibold text-gray-500 hover:bg-gray-100 transition-colors"
            >
                <span className="flex items-center gap-2">
                    {!isComplete ? (
                        <Loader2 size={14} className="animate-spin text-gray-500" />
                    ) : (
                        <Activity size={14} />
                    )}
                    {!isComplete ? "Thinking..." : "Thinking Process"}
                </span>
                <ChevronRight size={14} className={cn("text-gray-400 transition-transform duration-200", isOpen && "rotate-90")} />
            </button>
            <div
                ref={scrollRef}
                className={cn(
                    "text-xs text-slate-600 font-mono leading-relaxed border-t border-gray-200 bg-white/50 overflow-y-auto transition-all duration-300 ease-out",
                    isOpen ? "max-h-[500px] opacity-100 px-4 py-3" : "max-h-0 opacity-0 px-0 py-0 border-t-transparent"
                )}
            >
                {thought}
            </div>
        </div>
    );
};

// Component: System Step Log
const SystemStep = ({ message, isLoading, onClick, useBotIcon = false }: { message: string, isLoading?: boolean, onClick?: () => void, useBotIcon?: boolean }) => {
    const isRouter = message.includes('Router');
    const isWorkflow = message.includes('Step') || message.includes('Workflow');
    const isSafety = message.includes('Safety');
    const isSubAgentResult = message.includes('Sub-Agent') && (message.includes('Output saved') || message.includes('completed'));

    // Extract clean text
    const cleanText = message.replace(/^(Router|Step \d+\/\d+|System|Agent|Info)\s*[:\|]?\s*/, '');
    const source = message.match(/^(Router|Step \d+\/\d+|System|Agent|Info)/)?.[0] || "System";

    // Ensure we don't show empty steps (fixes lag if empty router logs sent)
    if (!cleanText.trim()) return null;

    // Extract file paths for clickable links
    const filePaths = extractFilePaths(cleanText);

    // Render text with clickable file paths
    const renderTextWithLinks = (text: string) => {
        if (filePaths.length === 0) return text;

        const elements: (string | JSX.Element)[] = [];
        let lastIndex = 0;

        filePaths.forEach((fp, i) => {
            // Add text before this path
            if (fp.start > lastIndex) {
                elements.push(text.substring(lastIndex, fp.start));
            }
            // Add clickable link
            elements.push(
                <a
                    key={i}
                    href={`/api/file?path=${encodeURIComponent(fp.path)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="text-amber-600 hover:text-amber-700 underline decoration-dotted hover:decoration-solid font-medium"
                    title={`Open: ${fp.path}`}
                >
                    📄 {fp.path.split(/[/\\]/).pop()}
                </a>
            );
            lastIndex = fp.end;
        });

        // Add remaining text
        if (lastIndex < text.length) {
            elements.push(text.substring(lastIndex));
        }

        return <>{elements}</>;
    };

    // Use standard React state for animation to avoid build issues with framer-motion
    // eslint-disable-next-line react-hooks/rules-of-hooks
    const [isVisible, setIsVisible] = useState(false);
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useEffect(() => {
        const timer = setTimeout(() => setIsVisible(true), 50);
        return () => clearTimeout(timer);
    }, []);

    return (
        <div
            className={cn(
                "flex gap-4 w-full max-w-[85%] my-1 transition-all duration-500 ease-out",
                isVisible ? "opacity-100 translate-x-0" : "opacity-0 -translate-x-2",
                onClick ? "cursor-pointer" : "",
                isSubAgentResult && filePaths.length > 0 ? "bg-amber-50/50 rounded-lg p-2 -ml-2" : ""
            )}
            onClick={onClick}
            role={onClick ? "button" : undefined}
            tabIndex={onClick ? 0 : undefined}
            onKeyDown={onClick ? (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onClick();
                }
            } : undefined}
        >
            <div className="w-9 shrink-0 flex justify-center">
                {useBotIcon ? (
                    <div className={cn(
                        "w-9 h-9 rounded-xl bg-gray-900 flex items-center justify-center text-white shadow-sm shrink-0",
                        isLoading && "animate-pulse"
                    )}>
                        <Bot size={18} />
                    </div>
                ) : (
                    <div className="w-0.5 h-full bg-gray-100 relative">
                        <div className={cn(
                            "absolute top-1/2 -translate-y-1/2 left-1/2 -translate-x-1/2 w-5 h-5 rounded-full border bg-white flex items-center justify-center z-10",
                            isLoading ? "border-gray-300 text-gray-700 shadow-sm" :
                                isRouter ? "border-orange-200 text-orange-500" :
                                    isSafety ? "border-red-200 text-red-500" :
                                        isSubAgentResult && filePaths.length > 0 ? "border-amber-300 text-amber-600" :
                                            isWorkflow ? "border-gray-200 text-gray-500" : "border-gray-200 text-gray-400"
                        )}>
                            {isLoading ? <Loader2 size={10} className="animate-spin" /> :
                                isRouter ? <GitBranch size={10} /> :
                                    isSafety ? <ShieldAlert size={10} /> :
                                        isSubAgentResult && filePaths.length > 0 ? <FileText size={10} /> :
                                            isWorkflow ? <Workflow size={10} /> : <CheckCircle2 size={10} />}
                        </div>
                    </div>
                )}
            </div>
            <div className="flex-1 py-1">
                <div className={cn("text-xs text-gray-500 flex items-center gap-2 flex-wrap", onClick && "hover:text-gray-800")}>
                    <span className={cn("font-semibold uppercase tracking-wider text-[10px]", isLoading ? "text-gray-600" : "text-gray-400")}>{source}</span>
                    <span className={cn(isLoading ? "text-gray-900 font-medium" : "text-gray-600")}>{renderTextWithLinks(cleanText)}</span>
                </div>
            </div>
        </div>
    );
};

function VAFDashboardContent() {
    const router = useRouter();
    const searchParams = useSearchParams();
    const tAuth = useTranslations('auth');
    const tStatus = useTranslations('status');
    const tMain = useTranslations('main');
    const tNav = useTranslations('nav');
    const [authChecking, setAuthChecking] = useState(true);
    const [isAuthenticated, setIsAuthenticated] = useState(false);
    const [currentUser, setCurrentUser] = useState<any>(null);
    const [authError, setAuthError] = useState<string | null>(null);
    const [authRetryKey, setAuthRetryKey] = useState(0);

    useEffect(() => {
        setAuthError(null);
        const ac = new AbortController();
        const timeoutId = setTimeout(() => ac.abort(), 8000);
        fetch(`${getApiBase()}/api/auth/me`, { credentials: 'include', signal: ac.signal })
            .then(async (res) => {
                if (res.ok) {
                    const userData = await res.json();
                    setCurrentUser(userData);
                    setIsAuthenticated(true);
                } else {
                    router.replace('/login');
                }
            })
            .catch((err) => {
                if (err?.name === 'AbortError') {
                    setAuthError('timeout');
                } else {
                    setAuthError('error');
                }
            })
            .finally(() => {
                clearTimeout(timeoutId);
                setAuthChecking(false);
            });
        return () => {
            clearTimeout(timeoutId);
            ac.abort();
        };
    }, [router, authRetryKey]);

    // OAuth callback redirect: open Settings with Connections tab when URL has connections=1 or cloud_oauth/email_oauth
    const openedFromOAuthRef = useRef(false);

    const fetchUserTimeFormat = useCallback(() => {
        fetch(getApiBase() + '/api/user/persona')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                const tf = (data?.user_identity?.time_format || '').toString().toLowerCase();
                // "Default (24h)" is stored as empty string; empty and '24h' both mean 24h
                setUserTimeFormat(tf === '12h' ? '12h' : '24h');
            })
            .catch(() => setUserTimeFormat('24h'));
    }, []);

    useEffect(() => { fetchUserTimeFormat(); }, [fetchUserTimeFormat]);

    const handleSettingsClose = useCallback(() => {
        setSettingsInitialTab(null);
        setIsSettingsOpen(false);
        fetchUserTimeFormat(); // Refresh time format in case user changed it in Interface settings
        if (openedFromOAuthRef.current) {
            openedFromOAuthRef.current = false;
            router.replace('/', { scroll: false });
        }
    }, [router, fetchUserTimeFormat]);

    const [input, setInput] = useState('');
    const inputValueRef = useRef('');
    const [insertedSelections, setInsertedSelections] = useState<InsertedSelectionRange[]>([]);
    const [suggestion, setSuggestion] = useState('');
    const [messages, setMessages] = useState<Message[]>([]);
    const messagesRef = useRef<Message[]>([]); // Ref to access messages in WebSocket callback
    useEffect(() => { messagesRef.current = messages; }, [messages]);

    const [status, setStatus] = useState('connecting');
    const [modelLoaded, setModelLoaded] = useState<boolean | null>(null);
    const [modelProvider, setModelProvider] = useState<string | null>(null);
    const [sessions, setSessions] = useState<Session[]>([]);
    const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
    const currentSessionIdRef = useRef<string | null>(null);
    useEffect(() => { currentSessionIdRef.current = currentSessionId; }, [currentSessionId]);
    const pendingSendRef = useRef<{ text: string } | null>(null);
    const pendingSessionRequestRef = useRef(false);
    /** After sending a user message, first agent_message_update must append a new bubble (race-safe). */
    const expectNewAssistantRef = useRef(false);
    /** Timestamp (ms) when user last sent a message; within 1.5s we force-append first update (millisecond race). */
    const lastUserSendTimeRef = useRef(0);
    /** After a tool ended, next agent_message_update must append (tool card may not be in state yet). */
    const expectNewAssistantAfterToolRef = useRef(false);
    const sidebarListRef = useRef<HTMLDivElement>(null);
    const sidebarDocsSyncedForSessionRef = useRef<string | null>(null);
    type DocumentViewerDoc = { id: string; name: string; mimeType?: string; data?: string; content?: string; htmlContent?: string };

    const [ws, setWs] = useState<WebSocket | null>(null);
    const wsSocketRef = useRef<WebSocket | null>(null); // so cleanup can close when effect re-runs before async completes
    const [loading, setLoading] = useState(false);
    const [isGenerating, setIsGenerating] = useState(false);
    const [statusMessage, setStatusMessage] = useState(''); // RE-ADDED
    const [activeToolName, setActiveToolName] = useState(''); // Currently-running tool name for loading bubble

    const pendingCreateAutomationResolveRef = useRef<((r: { ok: boolean; error?: string }) => void) | null>(null);
    const pendingUpdateAutomationResolveRef = useRef<((r: { ok: boolean; error?: string }) => void) | null>(null);

    const refreshAutomations = useCallback(() => {
        ws?.send(JSON.stringify({ type: 'get_automations' }));
    }, [ws]);

    const createAutomationSubmit = useCallback((payload: CreateAutomationPayload & { task_id?: string }) => {
        return new Promise<{ ok: boolean; error?: string }>((resolve) => {
            if (!ws || ws.readyState !== WebSocket.OPEN) {
                resolve({ ok: false, error: 'Not connected' });
                return;
            }
            const taskId = payload.task_id;
            if (taskId) {
                pendingUpdateAutomationResolveRef.current = resolve;
                const { task_id, ...rest } = payload;
                ws.send(JSON.stringify({ type: 'update_automation', task_id, ...rest }));
            } else {
                pendingCreateAutomationResolveRef.current = resolve;
                ws.send(JSON.stringify({ type: 'create_automation', ...payload }));
            }
        });
    }, [ws]);

    const deleteAutomation = useCallback((taskId: string) => {
        setDeletingAutomationId(taskId);
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'delete_automation', task_id: taskId }));
        } else {
            setDeletingAutomationId(null);
        }
    }, [ws]);

    const onDeleteAutomationAnimationEnd = useCallback((taskId: string) => {
        setAutomations((prev) => prev.filter((a) => a.id !== taskId));
        setDeletingAutomationId(null);
    }, []);

    // Per-Session Animation State Tracking
    // Tracks which sessions are actively loading so we can restore animation state on session switch
    const sessionLoadingStates = useRef<Record<string, {
        loading: boolean;
        isGenerating: boolean;
        statusMessage: string;
        loadingMessageId: number | null;
    }>>({});
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editName, setEditName] = useState('');
    const [config, setConfig] = useState<any>({});
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    const [downloadModelStatus, setDownloadModelStatus] = useState<{
        status: 'idle' | 'downloading' | 'done' | 'error';
        message?: string;
        progress_pct?: number;
        bytes_done?: number;
        bytes_total?: number;
        speed_str?: string;
        repo_id?: string;
    }>({ status: 'idle' });
    const [modelPreviewData, setModelPreviewData] = useState<{ repo_id: string; card_content?: string; gguf_files: { filename: string; size_bytes: number }[]; error?: string } | null>(null);
    const [downloadToast, setDownloadToast] = useState<{ show: boolean; message: string; success: boolean }>({ show: false, message: '', success: false });
    const [apiModels, setApiModels] = useState<Record<string, string[]>>({});
    const [isSettingsOpen, setIsSettingsOpen] = useState(false);
    const [settingsInitialTab, setSettingsInitialTab] = useState<string | null>(null);
    /** User's preferred time format from Settings → Interface (24h | 12h). Used for message timestamps. */
    const [userTimeFormat, setUserTimeFormat] = useState<'24h' | '12h' | undefined>(undefined);
    const [isAutomationPopupOpen, setIsAutomationPopupOpen] = useState(false);
    // When automation calendar opens (footer), load notes and todos for the current user
    useEffect(() => {
        if (!isAutomationPopupOpen || !ws || ws.readyState !== WebSocket.OPEN) return;
        ws.send(JSON.stringify({ type: 'get_automation_notes' }));
        ws.send(JSON.stringify({ type: 'get_automation_todos' }));
    }, [isAutomationPopupOpen, ws]);
    const [editingAutomationFromCalendar, setEditingAutomationFromCalendar] = useState<EditAutomationTask | null>(null);
    const [showChangingModelOverlay, setShowChangingModelOverlay] = useState(false);
    type PendingContactReply = { replyId: string; source: string; contactName: string; preview: string; sessionId?: string };
    const [pendingContactReplies, setPendingContactReplies] = useState<PendingContactReply[]>([]);
    const [tools, setTools] = useState<Array<{ name: string; description: string; category: string }>>([]);
    const [workflows, setWorkflows] = useState<Array<{ id: string; name: string; description: string; steps: number }>>([]);
    const [trustedSources, setTrustedSources] = useState<{ categories: Array<{ id: string; name: string; description: string; sources: Array<{ name: string; url: string; domains: string[]; trust_score: number; is_custom: boolean }> }> }>({ categories: [] });
    const [trustedSourcesError, setTrustedSourcesError] = useState<string | null>(null);
    const [automations, setAutomations] = useState<Array<{ id: string; name: string; description: string; prompt?: string; frequency: string; time: string; weekday?: string | null; day?: number | null; enabled: boolean; next_run?: string }>>([]);
    const [deletingAutomationId, setDeletingAutomationId] = useState<string | null>(null);
    type AutomationNote = { id: string; title?: string | null; content: string; created_at: string };
    type AutomationTodo = { id: string; text: string; created_at: string; due_at?: string | null; done: boolean };
    const [automationNotes, setAutomationNotes] = useState<AutomationNote[]>([]);
    const [automationTodos, setAutomationTodos] = useState<AutomationTodo[]>([]);
    const [isNotificationsOpen, setIsNotificationsOpen] = useState(false);
    const [notifications, setNotifications] = useState<NotificationItem[]>([]);
    // const [activeTools, setActiveTools] = useState<ToolState[]>([]); // REPLACED BY INLINE MESSAGES

    const fileInputRef = useRef<HTMLInputElement>(null);
    const [isDragOver, setIsDragOver] = useState(false);

    // Stats state
    type TokenStats = {
        used: number;
        total: number;
        percent: number;
        api: boolean;
        input_tokens?: number;
        output_tokens?: number;
    };
    const [tokenStats, setTokenStats] = useState<TokenStats | null>(null);
    const [contextStats, setContextStats] = useState<any | null>(null); // New X-Ray Stats (Estimated)
    const [realContext, setRealContext] = useState<any | null>(null); // REAL Payload (The Truth)
    const [ragResults, setRagResults] = useState<any | null>(null); // RAG Results
    const [isContextModalOpen, setIsContextModalOpen] = useState(false);
    // xraySection state removed - Context Window modal now shows only overview diagram

    const contextBreakdown = useMemo(() => {
        if (!contextStats) return null;
        const totalCap = contextStats.max_tokens;
        const used = contextStats.tokens;
        const fallbackSystem = Math.round(used * 0.3);
        const fallbackHistory = Math.round(used * 0.5);
        const fallbackTools = Math.round(used * 0.2);

        const toFiniteNumber = (v: unknown): number | null =>
            (typeof v === 'number' && Number.isFinite(v) && v >= 0) ? v : null;

        let systemEst = toFiniteNumber(contextStats.system_tokens) ?? fallbackSystem;
        let historyEst = toFiniteNumber(contextStats.history_tokens) ?? fallbackHistory;
        let toolsEst = toFiniteNumber(contextStats.tools_tokens) ?? fallbackTools;

        // Guard against stale/mixed events where totals were refreshed via `stats`
        // but category breakdown is old or incomplete (e.g. tools/history still 0).
        const breakdownSum = systemEst + historyEst + toolsEst;
        if (used > 0) {
            if (breakdownSum <= 0) {
                systemEst = fallbackSystem;
                historyEst = fallbackHistory;
                toolsEst = fallbackTools;
            } else if (breakdownSum < used * 0.85) {
                // Preserve known parts and attribute missing portion to conversation,
                // which is the most dynamic part during active chats.
                historyEst += (used - breakdownSum);
            } else if (breakdownSum > used * 1.15) {
                // Keep bars numerically consistent with the used total.
                const scale = used / breakdownSum;
                systemEst *= scale;
                historyEst *= scale;
                toolsEst *= scale;
            }
        }
        const pctOfUsedSystem = used ? (systemEst / used) * 100 : 0;
        const pctOfUsedTools = used ? (toolsEst / used) * 100 : 0;
        const pctOfUsedHistory = used ? (historyEst / used) * 100 : 0;
        const pctOfCapSystem = totalCap ? (systemEst / totalCap) * 100 : 0;
        const pctOfCapTools = totalCap ? (toolsEst / totalCap) * 100 : 0;
        const pctOfCapHistory = totalCap ? (historyEst / totalCap) * 100 : 0;
        return { totalCap, used, systemEst, historyEst, toolsEst, pctOfUsedSystem, pctOfUsedTools, pctOfUsedHistory, pctOfCapSystem, pctOfCapTools, pctOfCapHistory };
    }, [contextStats]);

    // Sub-Agent Window State
    const [subAgentState, setSubAgentState] = useState<{
        isOpen: boolean;
        agentName: string;
        status: string;
        presence: 'online' | 'idle' | 'error';
        currentFile: string;
        codeContent: string;
        artifactFile: string;
        artifactCode: string;
        artifactStatus: string;
        consoleLines: string[];
        steps: any[];
    }>({
        isOpen: false,
        agentName: "Sub-Agent",
        status: "Idle",
        presence: "idle",
        currentFile: "",
        codeContent: "",
        artifactFile: "",
        artifactCode: "",
        artifactStatus: "Idle",
        consoleLines: [],
        steps: []
    });

    // Document Editor: one state entry per session (like Viewer); includes content so unsaved edits survive chat switch.
    const [sessionEditorState, setSessionEditorState] = useState<Record<string, { isOpen: boolean; filePath: string; title: string; content?: string }>>({});
    const defaultEditorState = useMemo(() => ({ isOpen: false as const, filePath: '', title: 'Document' }), []);
    const documentEditorState = currentSessionId
        ? (sessionEditorState[currentSessionId] ?? defaultEditorState)
        : defaultEditorState;
    const setDocumentEditorState = useCallback((
        valueOrUpdater: { isOpen: boolean; filePath: string; title: string; content?: string } | ((prev: { isOpen: boolean; filePath: string; title: string; content?: string }) => { isOpen: boolean; filePath: string; title: string; content?: string })
    ) => {
        if (!currentSessionId) return;
        setSessionEditorState(prev => {
            const current = prev[currentSessionId] ?? defaultEditorState;
            const next = typeof valueOrUpdater === 'function' ? valueOrUpdater(current) : valueOrUpdater;
            return { ...prev, [currentSessionId]: next };
        });
    }, [currentSessionId, defaultEditorState]);
    const setDocumentEditorStateForSession = useCallback((sessionId: string, valueOrUpdater: { isOpen: boolean; filePath: string; title: string; content?: string } | ((prev: { isOpen: boolean; filePath: string; title: string; content?: string }) => { isOpen: boolean; filePath: string; title: string; content?: string })) => {
        setSessionEditorState(prev => {
            const current = prev[sessionId] ?? defaultEditorState;
            const next = typeof valueOrUpdater === 'function' ? valueOrUpdater(current) : valueOrUpdater;
            return { ...prev, [sessionId]: next };
        });
    }, [defaultEditorState]);

    // Document Viewer: one state entry per session so switching chats never overwrites or loses data.
    const [sessionViewerState, setSessionViewerState] = useState<Record<string, { isOpen: boolean; documents: DocumentViewerDoc[] }>>({});
    const defaultViewerState = useMemo(() => ({ isOpen: false as const, documents: [] as DocumentViewerDoc[] }), []);
    const documentViewerState = currentSessionId
        ? (sessionViewerState[currentSessionId] ?? defaultViewerState)
        : defaultViewerState;
    const setDocumentViewerState = useCallback((
        valueOrUpdater: { isOpen: boolean; documents: DocumentViewerDoc[] } | ((prev: { isOpen: boolean; documents: DocumentViewerDoc[] }) => { isOpen: boolean; documents: DocumentViewerDoc[] })
    ) => {
        if (!currentSessionId) return;
        setSessionViewerState(prev => {
            const current = prev[currentSessionId] ?? defaultViewerState;
            const next = typeof valueOrUpdater === 'function' ? valueOrUpdater(current) : valueOrUpdater;
            return { ...prev, [currentSessionId]: next };
        });
    }, [currentSessionId, defaultViewerState]);
    const setDocumentViewerStateForSession = useCallback((sessionId: string, valueOrUpdater: { isOpen: boolean; documents: DocumentViewerDoc[] } | ((prev: { isOpen: boolean; documents: DocumentViewerDoc[] }) => { isOpen: boolean; documents: DocumentViewerDoc[] })) => {
        setSessionViewerState(prev => {
            const current = prev[sessionId] ?? defaultViewerState;
            const next = typeof valueOrUpdater === 'function' ? valueOrUpdater(current) : valueOrUpdater;
            return { ...prev, [sessionId]: next };
        });
    }, [defaultViewerState]);

    // Suggestion State
    const [suggestionList, setSuggestionList] = useState<any[]>([]);
    const [suggestionType, setSuggestionType] = useState<'tool' | 'workflow' | null>(null);
    const [selectedSuggestionIndex, setSelectedSuggestionIndex] = useState(0);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const suggestionListRef = useRef<HTMLDivElement>(null);

    // Scroll Sync for Suggestions
    useEffect(() => {
        if (suggestionListRef.current && suggestionList.length > 0) {
            const activeItem = suggestionListRef.current.children[selectedSuggestionIndex] as HTMLElement;
            if (activeItem) {
                const container = suggestionListRef.current;
                const itemTop = activeItem.offsetTop;
                const itemBottom = itemTop + activeItem.offsetHeight;
                const containerTop = container.scrollTop;
                const containerBottom = containerTop + container.offsetHeight;

                if (itemTop < containerTop) {
                    container.scrollTop = itemTop;
                } else if (itemBottom > containerBottom) {
                    container.scrollTop = itemBottom - container.offsetHeight;
                }
            }
        }
    }, [selectedSuggestionIndex, suggestionList.length]);

    const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        const val = e.target.value;
        setInput(val);

        // Simple trigger logic: Check last word
        const words = val.split(' ');
        const lastWord = words[words.length - 1];

        if (lastWord.startsWith('/')) {
            const query = lastWord.slice(1).toLowerCase();
            const commands = [
                { name: 'clear', description: tNav('clearConversation') },
                { name: 'help', description: tNav('help') },
                { name: 'settings', description: tNav('settings') },
                { name: 'stop', description: tNav('stopSpeaking') },
                { name: 'new', description: tNav('newSession') },
                { name: 'load', description: tNav('loadSession') },
            ];

            // Ensure tools are loaded if list is empty
            if (tools.length === 0 && ws) {
                ws.send(JSON.stringify({ type: 'get_tools' }));
            }

            // Merge Tools + Commands (Tools First!)
            const allOptions = [
                ...tools.map(t => ({ name: t.name, description: t.description })),
                ...commands
            ];

            const filtered = allOptions
                .filter(c => c.name.toLowerCase().includes(query))
                .slice(0, 15); // Increased limit

            setSuggestionList(filtered);
            setSuggestionType('tool');
            setSelectedSuggestionIndex(0);
        } else if (lastWord.startsWith('@')) {
            const query = lastWord.slice(1).toLowerCase();
            // Workflows are loaded in `workflows` state
            const filtered = workflows
                .filter(w =>
                    (w.name && w.name.toLowerCase().includes(query)) ||
                    (w.id && w.id.toLowerCase().includes(query))
                )
                .slice(0, 10); // Predictive limit
            setSuggestionList(filtered);
            setSuggestionType('workflow');
            setSelectedSuggestionIndex(0);
        } else {
            setSuggestionList([]);
            setSuggestionType(null);
            setSelectedSuggestionIndex(0);
        }
    };

    const handleSuggestionClick = (item: any) => {
        const words = input.split(' ');
        words.pop(); // Remove partial
        const prefix = suggestionType === 'tool' ? '/' : '@';
        // Use ID for workflows if available, else name
        const value = suggestionType === 'workflow' ? (item.id || item.name) : item.name;
        const newValue = [...words, prefix + value].join(' ') + ' ';
        setInput(newValue);
        setSuggestionList([]);
        setSuggestionType(null);
        setSelectedSuggestionIndex(0);
        inputRef.current?.focus();
    };

    // Workflow Store
    const { workflow: activeWorkflow, isOpen: workflowPanelOpen, loadWorkflow, updateStepStatus, appendWorkflowLine, clearWorkflow } = useWorkflowStore();
    // Check if a workflow is actively running
    const isWorkflowRunning = activeWorkflow?.status === 'running';

    // Ref for WebSocket access (to avoid stale closure)
    const isWorkflowRunningRef = useRef(isWorkflowRunning);
    useEffect(() => { isWorkflowRunningRef.current = isWorkflowRunning; }, [isWorkflowRunning]);

    // TTS State
    const [playingMessageId, setPlayingMessageId] = useState<number | null>(null);
    const [loadingMessageId, setLoadingMessageId] = useState<number | null>(null);

    // Refs for WebSocket access
    const loadingMessageIdRef = useRef<number | null>(null);
    useEffect(() => { loadingMessageIdRef.current = loadingMessageId; }, [loadingMessageId]);

    const handleSpeak = (index: number, text: string) => {
        if (playingMessageId === index) {
            handleStopSpeech();
            return;
        }

        // Stop any current speech
        if (playingMessageId !== null) {
            ws?.send(JSON.stringify({ type: 'stop_speech' }));
        }

        // Only show TTS loading animation when TTS is enabled (avoids endless loading when TTS is off)
        if (config.speech_tts_enabled) {
            setLoadingMessageId(index);
        }

        // Send speak command immediately. 
        // We wait for 'tts_state' event (status='playing') to switch to playing state.
        ws?.send(JSON.stringify({ type: 'speak', text }));
    };

    const handleStopSpeech = () => {
        // Stop frontend audio
        if (currentAudioRef.current) {
            currentAudioRef.current.pause();
            currentAudioRef.current = null;
        }
        setPlayingMessageId(null);
        setLoadingMessageId(null);
        ws?.send(JSON.stringify({ type: 'stop_speech' }));
    };

    const [isRecording, setIsRecording] = useState(false);
    const [isProcessingAudio, setIsProcessingAudio] = useState(false);
    const [sttEnabled, setSttEnabled] = useState(false); // Track STT status
    const [memoryLearning, setMemoryLearning] = useState<{ active: boolean; message: string } | null>(null);
    const memoryLearningTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [volume, setVolume] = useState(0);
    const mediaRecorderRef = useRef<MediaRecorder | null>(null);
    const audioChunksRef = useRef<Blob[]>([]);
    const mediaStreamRef = useRef<MediaStream | null>(null);
    const audioContextRef = useRef<AudioContext | null>(null);
    const analyserRef = useRef<AnalyserNode | null>(null);
    const silenceStartRef = useRef<number | null>(null);
    const hasSpokenRef = useRef(false);
    const animationFrameRef = useRef<number | null>(null);
    const currentAudioRef = useRef<HTMLAudioElement | null>(null);
    const pendingSttSendRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const scrollRef = useRef<HTMLDivElement>(null);
    const containerRef = useRef<HTMLDivElement>(null);

    const artifactDirtyRef = useRef(false);
    const artifactLastEditRef = useRef(0);
    const artifactSendTimeoutRef = useRef<NodeJS.Timeout | null>(null);
    const subAgentStepsRef = useRef<Array<{ id: string; status: string; title?: string; description?: string }>>([]);
    const subAgentLogSetRef = useRef<Set<string>>(new Set());
    const subAgentAutoCloseRef = useRef<NodeJS.Timeout | null>(null);
    const subAgentManualOpenRef = useRef(false);
    const subAgentUserClosedRef = useRef(false);  // User explicitly closed panel - don't auto-reopen
    const subAgentOutputSetRef = useRef<Set<string>>(new Set());
    const [showSubAgentPanel, setShowSubAgentPanel] = useState(true);
    const subAgentUnmountRef = useRef<NodeJS.Timeout | null>(null);

    const preserveChatScroll = (update: () => void) => {
        const container = containerRef.current;
        if (!container) {
            update();
            return;
        }
        const prevScrollTop = container.scrollTop;
        const prevScrollHeight = container.scrollHeight;
        const wasAtBottom = prevScrollTop + container.clientHeight >= prevScrollHeight - 8;
        update();
        requestAnimationFrame(() => {
            const nextContainer = containerRef.current;
            if (!nextContainer) return;
            if (wasAtBottom) {
                nextContainer.scrollTop = nextContainer.scrollHeight;
                return;
            }
            const nextScrollHeight = nextContainer.scrollHeight;
            const scrollDelta = nextScrollHeight - prevScrollHeight;
            nextContainer.scrollTop = prevScrollTop + scrollDelta;
        });
    };

    const appendSubAgentLine = (line: string) => {
        if (!line) return;
        if (subAgentLogSetRef.current.has(line)) return;
        subAgentLogSetRef.current.add(line);
        const lineLower = line.toLowerCase();
        const isFailure = lineLower.includes('failed') || lineLower.includes('timeout') || lineLower.includes('error');
        setSubAgentState(prev => ({
            ...prev,
            consoleLines: [...prev.consoleLines, line].slice(-500),
            ...(isFailure ? { status: line.trim().slice(0, 120) } : {})
        }));
    };

    const appendSubAgentBlock = (block: string, keyHint?: string) => {
        if (!block) return;
        const key = `${keyHint || ''}:${block.length}:${block.slice(0, 200)}`;
        if (subAgentOutputSetRef.current.has(key)) return;
        subAgentOutputSetRef.current.add(key);
        setSubAgentState(prev => ({
            ...prev,
            consoleLines: [...prev.consoleLines, block].slice(-500)
        }));
    };

    const openSubAgentWindow = (manual: boolean) => {
        // Don't open Sub-Agent window when a workflow is running - output goes to workflow terminal
        if (isWorkflowRunningRef.current && !manual) {
            return;
        }
        if (manual) {
            subAgentManualOpenRef.current = true;
            subAgentUserClosedRef.current = false;  // User opened - clear "user closed" flag
            if (subAgentAutoCloseRef.current) {
                clearTimeout(subAgentAutoCloseRef.current);
                subAgentAutoCloseRef.current = null;
            }
        } else {
            subAgentManualOpenRef.current = false;
        }
        preserveChatScroll(() => {
            setSubAgentState(prev => ({ ...prev, isOpen: true }));
        });
    };

    const closeSubAgentWindow = (manual: boolean) => {
        if (manual) {
            subAgentManualOpenRef.current = false;
            subAgentUserClosedRef.current = true;  // User explicitly closed - don't auto-reopen
        }
        preserveChatScroll(() => {
            setSubAgentState(prev => ({ ...prev, isOpen: false }));
        });
    };

    // Sub-Agent Window: hide when a workflow is running to avoid overlay
    const subAgentStateRef = useRef(subAgentState);
    useEffect(() => { subAgentStateRef.current = subAgentState; }, [subAgentState]);
    useEffect(() => {
        if (!isWorkflowRunning) return;
        if (!subAgentStateRef.current.isOpen) return;
        if (subAgentAutoCloseRef.current) {
            clearTimeout(subAgentAutoCloseRef.current);
            subAgentAutoCloseRef.current = null;
        }
        preserveChatScroll(() => setSubAgentState(prev => ({ ...prev, isOpen: false })));
    }, [isWorkflowRunning]);

    // Cache State
    const sessionCache = useRef<Record<string, Message[]>>({});
    const cacheSaveTimeout = useRef<NodeJS.Timeout | null>(null);
    const sessionsRef = useRef<Session[]>([]);
    useEffect(() => {
        sessionsRef.current = sessions;
    }, [sessions]);

    // Load Cache on Mount
    useEffect(() => {
        sessionCache.current = loadSessionCache();
    }, []);

    // Save Cache on Update (Debounced)
    useEffect(() => {
        if (!currentSessionId) return;

        // Update in-memory cache immediately
        sessionCache.current[currentSessionId] = messages;

        // Debounce save to disk
        if (cacheSaveTimeout.current) clearTimeout(cacheSaveTimeout.current);
        cacheSaveTimeout.current = setTimeout(() => {
            const sessionIdsInOrder = sessionsRef.current.map((s) => s.id);
            const trimmed = trimSessionCache(sessionCache.current, {
                currentSessionId,
                sessionIdsInOrder,
            });
            sessionCache.current = trimmed;
            saveSessionCache(sessionCache.current, {
                currentSessionId,
                sessionIdsInOrder,
            });
        }, 1000);
    }, [messages, currentSessionId]);

    const handleSessionSwitch = (id: string) => {
        if (currentSessionId === id) return;

        // 1. Save current session state before switching (messages + animation). Document Viewer is already keyed by session in state.
        if (currentSessionId) {
            sessionCache.current[currentSessionId] = messages;
            sessionLoadingStates.current[currentSessionId] = {
                loading,
                isGenerating,
                statusMessage,
                loadingMessageId
            };
        }

        // 2. Close Sub-Agent panel – it belongs to the previous session. Viewer/Editor state is per-session and will show correctly for the new session.
        subAgentUserClosedRef.current = false;  // Reset for new session
        setSubAgentState(prev => ({ ...prev, isOpen: false }));

        // 3. Optimistic Switch (viewer state is derived from sessionViewerState[id] automatically)
        setCurrentSessionId(id);
        expectNewAssistantRef.current = false;
        lastUserSendTimeRef.current = 0;
        expectNewAssistantAfterToolRef.current = false;
        const cached = sessionCache.current[id] || [];
        setMessages(cached);

        // 4. Restore animation state for target session (or default to idle)
        const targetState = sessionLoadingStates.current[id];
        if (targetState) {
            setLoading(targetState.loading);
            setIsGenerating(targetState.isGenerating);
            setStatusMessage(targetState.statusMessage);
            setLoadingMessageId(targetState.loadingMessageId);
        } else {
            // No saved state = assume idle
            setLoading(false);
            setIsGenerating(false);
            setStatusMessage('');
            setLoadingMessageId(null);
        }

        // 5. Request sync
        ws?.send(JSON.stringify({ type: 'load_session', id }));
    };

    // Gewählten Chat in der Sidebar sichtbar halten (nicht nach oben springen)
    useEffect(() => {
        if (!currentSessionId || !sidebarListRef.current) return;
        const el = sidebarListRef.current.querySelector(`[data-session-id="${currentSessionId}"]`);
        if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }, [currentSessionId]);

    const [reconnectAttempt, setReconnectAttempt] = useState(0);
    useEffect(() => {
        if (typeof window === 'undefined') return;
        let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
        let cancelled = false;
        (async () => {
            let base: string;
            const port = window.location.port || '';
            // When accessed via reverse proxy (nginx/integrated proxy) on 80/443/8443, use same-origin so /ws is proxied
            if (port === '80' || port === '443' || port === '8443' || port === '') {
                base = '';
            } else {
                try {
                    const r = await fetch(`${getApiBase() || ''}/api/network/ws-config`, { credentials: 'include' });
                    if (!r.ok) throw new Error('');
                    const { useWss, port: backendPort } = await r.json();
                    const protocol = useWss ? 'wss' : 'ws';
                    base = `${protocol}://${window.location.hostname}:${backendPort}`;
                } catch {
                    base = getWsBase();
                }
            }
            if (cancelled) return;
            let wsUrl = (base ? base + '/ws' : '/ws');
            const token = sessionStorage.getItem('vaf_token');
            if (token) {
                wsUrl += (wsUrl.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
            }
            const socket = new WebSocket(wsUrl);
            wsSocketRef.current = socket;
            socket.onopen = () => {
            setStatus('connected');
            socket.send(JSON.stringify({ type: 'get_sessions' }));
            socket.send(JSON.stringify({ type: 'get_config' }));
            socket.send(JSON.stringify({ type: 'get_models' }));
            socket.send(JSON.stringify({ type: 'get_workflows' })); // Fetch workflows for autocomplete
            socket.send(JSON.stringify({ type: 'get_tools' }));     // Fetch tools for reference
        };
        socket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);

                // CRITICAL: Filter by session to prevent cross-contamination!
                const activeSessionId = currentSessionIdRef.current;

                // Only filter if both IDs are present and they don't match
                // If data.sessionId is missing, it's a global update -> Allow
                // If activeSessionId is missing, we are in initial state -> Allow
                if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) {
                    // Exception: history_update, session_list, contact_reply_pending (show for any session)
                    if (data.type !== 'session_list' && data.type !== 'history_update' && data.type !== 'contact_reply_pending') {
                        console.log(`🔍 [FILTER] Rejecting ${data.type}: backend=${data.sessionId}, frontend=${activeSessionId}`);
                        return;
                    }
                }

                if (data.type === 'new_log') {
                    const src = data.entry.source || "";
                    const rawMsg = data.entry.message || "";
                    const msgLower = rawMsg.toLowerCase();
                    const srcLower = src.toLowerCase();
                    const isSubAgentLog =
                        msgLower.includes('sub-agent') ||
                        msgLower.includes('subagent') ||
                        srcLower.includes('sub-agent') ||
                        srcLower.includes('subagent');
                    if (isSubAgentLog) {
                        const timeStamp = new Date().toISOString().slice(11, 19);
                        appendSubAgentLine(`[${timeStamp}] ${rawMsg}`);
                        // Only open if not already open - avoids duplicate open from tool_update + domain_log
                        if (!subAgentStateRef.current.isOpen) {
                            openSubAgentWindow(false);
                        }
                    } else if (subAgentState.isOpen && (src === 'System' || src === 'Info') && rawMsg) {
                        const timeStamp = new Date().toISOString().slice(11, 19);
                        appendSubAgentLine(`[${timeStamp}] ${src}: ${rawMsg}`);
                    }

                    // ACTIVE TOOLS HANDLING via tool_update
                    // Legacy code removed

                    // Skip "Agent Thinking..." as requested
                    if (src === 'Agent' && rawMsg.toLowerCase().includes('thinking')) {
                        return;
                    }

                    if (src.includes('Step') || src.includes('Router') || src.includes('System') || src.includes('Agent') || src.includes('Info')) {
                        const cleanMsg = rawMsg.replace(/^\|\s*/, '');

                        // Strip ALL dots, ellipsis, and whitespace from start/end
                        // Also remove "Thinking" if it stands alone or with dots
                        let displayMsg = cleanMsg.replace(/^[\.\u2026\s]+|[\.\u2026\s]+$/g, '');

                        // If message is just "Thinking", ignore it (UI handles this via loading state)
                        if (displayMsg.toLowerCase() === 'thinking') displayMsg = '';

                        // ROBUST FILTER: If removing all non-alphanumeric chars results in empty string, ignore it.
                        // This catches "...", ". . .", "___", etc.
                        if (displayMsg.replace(/[\W_]/g, '').length === 0) return;

                        // Do not set statusMessage here: we add this as a system step below; showing it again as ghost loader causes duplicate (prominent + faded).
                        // Insert system step before the next assistant (so it appears above the reply), not at the end – avoids "system messages below LLM answer" when logs arrive after the answer.
                        setMessages(prev => {
                            const newContent = `${src}: ${cleanMsg}`;
                            const last = prev[prev.length - 1];
                            if (last && last.role === 'system' && last.content === newContent) return prev;
                            const lastUserIdx = prev.map((m, i) => ({ role: m.role, i })).filter(({ role }) => role === 'user').pop()?.i ?? -1;
                            const insertAt = prev.findIndex((m, i) => i > lastUserIdx && m.role === 'assistant');
                            const idx = insertAt === -1 ? prev.length : insertAt;
                            const next = [...prev];
                            next.splice(idx, 0, { role: 'system', content: newContent, timestamp: Date.now() });
                            return next;
                        });
                    }
                }
                else if (data.type === 'tool_update') {
                    if (data.sessionId && currentSessionId && data.sessionId !== currentSessionId) return;

                    const { subType, toolId, name, data: eventData, timestamp } = data;
                    const toolName = String(name || '').toLowerCase();
                    const isSubAgentTool = /(?:^|[^a-z])(librarian|research|document|coding)_agent(?:$|[^a-z])/.test(toolName);

                    // Track active tool for loading bubble
                    if (subType === 'start') {
                        setActiveToolName(String(name || '').replace(/_/g, ' '));
                    } else if (subType === 'end' || subType === 'error') {
                        setActiveToolName('');
                    }

                    if (subType === 'start' && isSubAgentTool) {
                        subAgentUserClosedRef.current = false;  // New task - user wants to see it
                        openSubAgentWindow(false);
                        const title = String(name || 'Sub-Agent').replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
                        setSubAgentState(prev => ({
                            ...prev,
                            status: 'Running...',
                            presence: 'online',
                            steps: [
                                ...prev.steps.filter((s: { id: string }) => s.id !== toolId),
                                { id: toolId, title, status: 'running', actions: [] as Array<{ type: string; details: string }> }
                            ]
                        }));
                    }
                    if (subType === 'end' || subType === 'error') {
                        if (isSubAgentTool) {
                            const isAsyncMarker = eventData != null && String(eventData).includes('[SUBAGENT_ASYNC:');
                            if (!isAsyncMarker) {
                                setSubAgentState(prev => ({
                                    ...prev,
                                    status: subType === 'error' ? 'Failed' : 'Completed',
                                    presence: 'idle',
                                    steps: prev.steps.map((s: { id: string; status: string }) =>
                                        s.id === toolId ? { ...s, status: 'completed' as const } : s
                                    )
                                }));
                                if (eventData) {
                                    const blockTitle = String(name || 'Sub-Agent').replace(/_/g, ' ');
                                    appendSubAgentBlock(`### ${blockTitle}\n${eventData}`, toolId);
                                }
                            }
                        }
                    }
                    if (subAgentState.isOpen) {
                        const timeStamp = new Date().toISOString().slice(11, 19);
                        const statusLabel = subType === 'start' ? 'Start' : subType === 'end' ? 'End' : 'Error';
                        const payload = eventData ? ` - ${eventData}` : '';
                        appendSubAgentLine(`[${timeStamp}] ${statusLabel}: ${name}${payload}`);
                    }

                    setMessages(prev => {
                        // Check if tool message exists
                        const existingIdx = prev.findIndex(m => m.toolId === toolId);

                        if (subType === 'start') {
                            if (existingIdx !== -1) return prev; // Duplicate start
                            return [...prev, {
                                role: 'tool',
                                content: '', // Result empty at start
                                timestamp: Date.now(),
                                toolId: toolId,
                                toolName: name,
                                toolArgs: eventData, // Arguments passed in data
                                toolStatus: 'running',
                                toolStartTime: Date.now()
                            }];
                        }
                        else if (subType === 'end' || subType === 'error') {
                            if (existingIdx === -1) return prev; // Tool not found (maybe page reload?)
                            expectNewAssistantAfterToolRef.current = true;
                            const newMessages = [...prev];
                            newMessages[existingIdx] = {
                                ...newMessages[existingIdx],
                                toolStatus: subType === 'error' ? 'error' : 'completed',
                                content: eventData, // Result passed in data
                                toolEndTime: Date.now()
                            };
                            return newMessages;
                        }
                        return prev;
                    });
                    // Clear status message when tool runs
                    setStatusMessage('');
                }
                else if (data.type === 'stats') {
                    // Update stats if session matches OR if it's a global update (no sessionId)
                    if (data.sessionId && currentSessionId && data.sessionId !== currentSessionId) return;
                    setTokenStats(data.stats);
                    // Also drive the context bar (X / Y Tokens) from stats so it's never stuck at 0
                    // when context_status is not received (e.g. headless only sends stats)
                    const s = data.stats;
                    if (s && typeof s.used === 'number' && typeof s.total === 'number') {
                        const percent = s.percent != null ? s.percent : (s.total ? Math.round((s.used / s.total) * 1000) / 10 : 0);
                        setContextStats((prev: any) => ({
                            ...(prev || {}),
                            tokens: s.used,
                            max_tokens: s.total,
                            percent,
                            message_count: prev?.message_count ?? 0,
                            ...(s.input_tokens != null && { input_tokens: s.input_tokens }),
                            ...(s.output_tokens != null && { output_tokens: s.output_tokens }),
                        }));
                    }
                }
                else if (data.type === 'contact_reply_pending') {
                    setPendingContactReplies(prev => [...prev, {
                        replyId: data.replyId,
                        source: data.source || 'whatsapp',
                        contactName: data.contactName || '',
                        preview: data.preview || '',
                        sessionId: data.sessionId,
                    }]);
                }
                else if (data.type === 'contact_reply_result' && data.replyId) {
                    setPendingContactReplies(prev => prev.filter(p => p.replyId !== data.replyId));
                }
                else if (data.type === 'context_status') {
                    setContextStats(data.stats);
                }
                else if (data.type === 'real_context_payload') {
                    setRealContext(data);
                }
                else if (data.type === 'rag_results') {
                    setRagResults(data);
                }
                else if (data.type === 'agent_message_update') {
                    // CRITICAL: Only update if this message belongs to the current session!
                    // If user switched chats while bot was typing, ignore this update.
                    const activeSessionId = currentSessionIdRef.current;
                    if (!activeSessionId && data.sessionId) {
                        setCurrentSessionId(data.sessionId);
                        ws?.send(JSON.stringify({ type: 'load_session', id: data.sessionId }));
                    } else if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) {
                        // Update per-session state even if not the active session
                        // So when user switches back, animations are correct
                        if (data.sessionId) {
                            const previousState = sessionLoadingStates.current[data.sessionId];
                            sessionLoadingStates.current[data.sessionId] = {
                                loading: false,
                                isGenerating: previousState?.isGenerating ?? true,
                                statusMessage: '',
                                loadingMessageId: null
                            };
                        }
                        return;
                    }

                    setLoading(false);
                    setIsGenerating(true);
                    setStatusMessage(''); // Clear status when answer starts
                    setActiveToolName(''); // Clear active tool when answer starts

                    // Update per-session loading state
                    if (activeSessionId) {
                        sessionLoadingStates.current[activeSessionId] = {
                            loading: false,
                            isGenerating: true,
                            statusMessage: '',
                            loadingMessageId: null
                        };
                    }
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        const expectNew = expectNewAssistantRef.current;
                        if (expectNew) expectNewAssistantRef.current = false;
                        const expectAfterTool = expectNewAssistantAfterToolRef.current;
                        if (expectAfterTool) expectNewAssistantAfterToolRef.current = false;
                        // Within 1.5s of user send: force append once (handles ms-level race before state has user msg)
                        const t = lastUserSendTimeRef.current;
                        const withinUserSendWindow = t && (Date.now() - t < 1500);
                        if (withinUserSendWindow) lastUserSendTimeRef.current = 0;
                        const forceAppend = expectNew || expectAfterTool || withinUserSendWindow;
                        if (last && last.role === 'assistant' && !forceAppend) {
                            const newMsgs = [...prev];
                            newMsgs[newMsgs.length - 1] = { ...last, content: data.content };
                            return newMsgs;
                        }
                        // New turn (user just sent), after tool end, or within user-send window. Append a new assistant bubble.
                        // Backend sends full accumulated content; show only the delta after the previous
                        // assistant text so the user sees a separate answer bubble (e.g. after tool use).
                        let content = data.content ?? '';
                        const lastAssistantIdx = prev.map((m, i) => ({ m, i })).filter(({ m }) => m.role === 'assistant').pop()?.i;
                        if (lastAssistantIdx !== undefined) {
                            const prevContent = String(prev[lastAssistantIdx].content ?? '').trim();
                            const newTrimmed = String(content).trim();
                            if (prevContent.length > 0 && newTrimmed.length >= prevContent.length && newTrimmed.startsWith(prevContent)) {
                                const delta = newTrimmed.slice(prevContent.length).replace(/^\s*\n+/, '').trim();
                                if (delta.length > 0) content = delta;
                            }
                        }
                        return [...prev, { role: 'assistant', content, timestamp: Date.now() }];
                    });
                }
                else if (data.type === 'clear_last_assistant') {
                    // Remove faulty assistant message so only the retry response is shown (empty-response retry).
                    // We must remove the last *assistant* message, not the last message: the "Empty response..."
                    // system log is often appended before this event, so the last message can be system.
                    const activeSessionId = currentSessionIdRef.current;
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    setMessages(prev => {
                        const lastAssistantIdx = prev.map((m, i) => ({ m, i })).filter(({ m }) => m.role === 'assistant').pop()?.i;
                        if (lastAssistantIdx === undefined) return prev;
                        return prev.slice(0, lastAssistantIdx).concat(prev.slice(lastAssistantIdx + 1));
                    });
                }
                else if (data.type === 'tts_audio') {
                    console.log('[TTS] Received tts_audio, audio length:', data.audio?.length);
                    // Stop any current audio
                    if (currentAudioRef.current) {
                        currentAudioRef.current.pause();
                    }

                    // Play new audio
                    const audioSrc = `data:audio/wav;base64,${data.audio}`;
                    console.log('[TTS] Creating Audio element with data URL length:', audioSrc.length);
                    const audio = new Audio(audioSrc);
                    currentAudioRef.current = audio;

                    audio.onplay = () => {
                        console.log('[TTS] Audio started playing');
                        // Transition from loading to playing
                        if (loadingMessageIdRef.current !== null) {
                            setPlayingMessageId(loadingMessageIdRef.current);
                            setLoadingMessageId(null);
                        }
                    };

                    audio.onended = () => {
                        console.log('[TTS] Audio ended');
                        setPlayingMessageId(null);
                        currentAudioRef.current = null;
                    };

                    audio.onerror = (e) => {
                        console.error("[TTS] Audio playback error", e);
                        setPlayingMessageId(null);
                        setLoadingMessageId(null);
                        currentAudioRef.current = null;
                    };

                    audio.play().then(() => {
                        console.log('[TTS] play() promise resolved');
                    }).catch(e => {
                        console.error("[TTS] Autoplay failed", e);
                        setPlayingMessageId(null);
                        setLoadingMessageId(null);
                    });
                }
                else if (data.type === 'tts_state') {
                    if (data.status === 'loading') {
                        // Only show TTS loading when TTS is enabled (server may send loading from other code paths)
                        if (config.speech_tts_enabled) {
                            let targetIndex = -1;
                            if (loadingMessageIdRef.current !== null) {
                                targetIndex = loadingMessageIdRef.current;
                            } else {
                                const currentMessages = messagesRef.current;
                                for (let i = currentMessages.length - 1; i >= 0; i--) {
                                    if (currentMessages[i].role === 'assistant') {
                                        targetIndex = i;
                                        break;
                                    }
                                }
                            }
                            if (targetIndex !== -1) {
                                setLoadingMessageId(targetIndex);
                            }
                        }
                    }
                    else if (data.status === 'playing') {
                        // Find target message
                        let targetIndex = -1;

                        // Use Ref to get current loading ID (avoids closure staleness)
                        if (loadingMessageIdRef.current !== null) {
                            targetIndex = loadingMessageIdRef.current;
                        } else {
                            // Auto-TTS: Assume last assistant message
                            const currentMessages = messagesRef.current;
                            for (let i = currentMessages.length - 1; i >= 0; i--) {
                                if (currentMessages[i].role === 'assistant') {
                                    targetIndex = i;
                                    break;
                                }
                            }
                        }

                        if (targetIndex !== -1) {
                            setPlayingMessageId(targetIndex);
                            setLoadingMessageId(null);
                        }
                    } else if (data.status === 'stopped') {
                        setPlayingMessageId(null);
                        setLoadingMessageId(null);
                    }
                }
                else if (data.type === 'message_complete') {
                    if (data.sessionId) {
                        sessionLoadingStates.current[data.sessionId] = {
                            loading: false,
                            isGenerating: false,
                            statusMessage: '',
                            loadingMessageId: null
                        };
                    }
                    const activeSessionId = currentSessionIdRef.current;
                    if (!data.sessionId || data.sessionId === activeSessionId) {
                        setIsGenerating(false);
                    }
                    // Completion sound: play when model has finished (Web UI only)
                    // Use same-origin relative URL so it works through HTTPS proxy (no mixed content)
                    try {
                        const soundUrl = '/sounds/tts01.mp3';
                        const audio = new Audio(soundUrl);
                        audio.volume = 0.6;
                        audio.play().catch(() => { /* ignore autoplay policy / user mute */ });
                    } catch {
                        // ignore if Audio or play fails
                    }
                    // Auto-TTS: Speak the response if enabled
                    if (config.tts_auto_speak && config.speech_tts_enabled && data.content) {
                        // Don't auto-speak if already playing/loading
                        if (playingMessageId === null && loadingMessageId === null) {
                            ws?.send(JSON.stringify({
                                type: 'speak',
                                text: data.content
                            }));
                        }
                    }
                }
                else if (data.type === 'session_list') {
                    setSessions(data.sessions);

                    // Only auto-create if we have NO sessions and NO active session selected
                    if (data.sessions.length === 0 && !activeSessionId) {
                        ws?.send(JSON.stringify({ type: 'new_session' }));
                        return;
                    }

                    // Auto-select latest if none selected (initial load)
                    // or if the current session no longer exists in the list.
                    if (data.sessions.length > 0) {
                        const sessionIds = new Set(data.sessions.map((s: Session) => s.id));
                        if (!activeSessionId || !sessionIds.has(activeSessionId)) {
                            setCurrentSessionId(data.sessions[0].id);
                            ws?.send(JSON.stringify({ type: 'load_session', id: data.sessions[0].id }));
                        }
                    }
                }
                else if (data.type === 'workflow_start') {
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;

                    loadWorkflow({
                        id: data.workflowId || 'wf-' + Date.now(),
                        name: data.name || 'Workflow',
                        steps: data.steps || [],
                        currentStepId: null,
                        status: 'running'
                    });

                    // NOTE: We do NOT add a chat message here anymore!
                    // The workflow is already shown in:
                    // 1. The Runtime Panel on the right side
                    // 2. The inline WorkflowChatElement parsed from [WORKFLOW_ASYNC:...] text
                    // Adding a message here caused duplicate workflow elements in the chat.
                }
                else if (data.type === 'workflow_update') {
                    if (data.sessionId && currentSessionId && data.sessionId !== currentSessionId) return;
                    updateStepStatus(data.stepId, data.status, data.progress, data.result);
                }
                else if (data.type === 'workflow_output_stream') {
                    if (data.sessionId && currentSessionId && data.sessionId !== currentSessionId) return;
                    const line = typeof data.line === 'string' ? data.line : '';
                    appendWorkflowLine(line);
                }
                else if (data.type === 'document_ready') {
                    // Store editor state for the session that received the document (per-session like Viewer).
                    const sid = data.sessionId || currentSessionId;
                    if (sid) {
                        setDocumentEditorStateForSession(sid, {
                            isOpen: true,
                            filePath: data.filePath || '',
                            title: data.title || 'Document',
                            content: undefined, // new document: load from server; clears any previous doc content
                        });
                        // Only switch UI if this event is for the currently visible chat
                        if (sid === currentSessionId) {
                            setShowSubAgentPanel(true);
                            setDocumentViewerStateForSession(sid, (prev) => ({ ...prev, isOpen: false }));
                        }
                    }
                }
                else if (data.type === 'editor_apply_edit') {
                    const sid = data.sessionId || currentSessionId;
                    const start = typeof data.start === 'number' ? data.start : undefined;
                    const end = typeof data.end === 'number' ? data.end : undefined;
                    const selectionIndex = typeof data.selectionIndex === 'number' ? data.selectionIndex : 0;
                    const newText = typeof data.newText === 'string' ? data.newText : '';
                    if (!sid || start === undefined || end === undefined) return;
                    setSessionEditorState(prev => {
                        const cur = prev[sid];
                        if (!cur?.content) return prev;
                        const nextContent = replaceTextInHtml(cur.content, start, end, newText);
                        return { ...prev, [sid]: { ...cur, content: nextContent } };
                    });
                    setInsertedSelections(prev => prev.filter((_, i) => i !== selectionIndex));
                }
                else if (data.type === 'sidebar_documents_set') {
                    const contents = (data.contents || []) as Array<{ name: string; content: string; data?: string; mimeType?: string; htmlContent?: string }>;
                    const sid = data.sessionId || activeSessionId;
                    const updater = (prev: { isOpen: boolean; documents: DocumentViewerDoc[] }) => {
                        if (contents.length === 0) return { ...prev, documents: [] };
                        return {
                            ...prev,
                            isOpen: true,
                            documents: contents.map((c, i) => ({
                                ...(prev.documents[i] || {}),
                                id: (prev.documents.length === contents.length && prev.documents[i]?.name === c.name && prev.documents[i]?.id)
                                    ? prev.documents[i].id
                                    : `doc-${i}-${crypto.randomUUID().slice(0, 8)}`,
                                name: c.name,
                                content: c.content ?? prev.documents[i]?.content ?? '',
                                ...(c.data != null && { data: c.data }),
                                ...(c.mimeType != null && { mimeType: c.mimeType }),
                                ...(typeof c.htmlContent === 'string' && c.htmlContent.length > 0 && { htmlContent: c.htmlContent }),
                            })),
                        };
                    };
                    if (sid) setDocumentViewerStateForSession(sid, updater);
                    else setDocumentViewerState(updater);
                    if (contents.length > 0) setShowSubAgentPanel(true);
                }
                else if (data.type === 'subagent_update') {
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    const statusText = String(data.status || '').trim();
                    const modelLabel = data.model ? `• ${String(data.model)}` : '';
                    const statusLine = `${statusText}${modelLabel ? ` ${modelLabel}` : ''}`.trim();
                    const newSteps = data.steps || [];
                    const prevSteps = subAgentStepsRef.current;
                    const prevMap = new Map(prevSteps.map(step => [step.id, step.status]));
                    const statusLines: string[] = [];

                    newSteps.forEach((step: any) => {
                        const prevStatus = prevMap.get(step.id);
                        if (!prevStatus || prevStatus !== step.status) {
                            const label = step.status === 'completed'
                                ? 'Completed'
                                : step.status === 'running'
                                    ? 'Running'
                                    : 'Pending';
                            const detail = step.description ? ` - ${step.description}` : '';
                            statusLines.push(`${label}: ${step.title}${detail}`);
                        }
                    });

                    if (statusLines.length > 0) {
                        const timeStamp = new Date().toISOString().slice(11, 19);
                        statusLines.forEach(line => appendSubAgentLine(`[${timeStamp}] ${line}`));
                    }

                    subAgentStepsRef.current = newSteps;
                    const presence = data.presence || subAgentState.presence;
                    const statusLower = String(data.status || '').toLowerCase();
                    const isRunning = presence === 'online' || statusLower.includes('running') || statusLower.includes('pending');
                    // Don't force-reopen if user explicitly closed the panel
                    const shouldOpen = isRunning && !subAgentUserClosedRef.current;
                    setSubAgentState(prev => ({
                        ...prev,
                        isOpen: shouldOpen ? true : prev.isOpen,
                        agentName: data.agentName || prev.agentName,
                        status: statusLine || prev.status,
                        presence: presence,
                        currentFile: data.file || prev.currentFile,
                        codeContent: data.code || prev.codeContent,
                        steps: data.steps || prev.steps,
                        artifactFile: artifactDirtyRef.current ? prev.artifactFile : (data.file || prev.artifactFile),
                        artifactCode: artifactDirtyRef.current ? prev.artifactCode : (data.code || prev.artifactCode),
                        artifactStatus: artifactDirtyRef.current ? prev.artifactStatus : (data.code || data.file ? 'Synced' : prev.artifactStatus)
                    }));
                }
                else if (data.type === 'artifact_update') {
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    setSubAgentState(prev => {
                        const incomingFile = data.file ?? prev.artifactFile;
                        const incomingCode = data.code ?? prev.artifactCode;
                        if (artifactDirtyRef.current) {
                            if (incomingCode === prev.artifactCode) {
                                artifactDirtyRef.current = false;
                                return {
                                    ...prev,
                                    artifactFile: incomingFile,
                                    artifactStatus: 'Saved'
                                };
                            }
                            return prev;
                        }
                        return {
                            ...prev,
                            artifactFile: incomingFile,
                            artifactCode: incomingCode,
                            artifactStatus: 'Saved'
                        };
                    });
                }
                else if (data.type === 'subagent_output') {
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    if (data.output) {
                        const prefix = data.agentType ? `### ${data.agentType.replace(/_/g, ' ')}` : '### Sub-Agent Output';
                        // If workflow is running, send to workflow terminal instead of sub-agent window
                        if (isWorkflowRunningRef.current) {
                            appendWorkflowLine(`${prefix}\n${data.output}`);
                        } else {
                            appendSubAgentBlock(`${prefix}\n${data.output}`, data.taskId);
                        }
                    }
                }
                else if (data.type === 'subagent_output_stream') {
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    const line = typeof data.line === 'string' ? data.line : '';
                    if (line) {
                        const timeStamp = new Date().toISOString().slice(11, 19);
                        const formattedLine = `[${timeStamp}] ${line}`;
                        // If workflow is running, send to workflow terminal instead of sub-agent window
                        if (isWorkflowRunningRef.current) {
                            appendWorkflowLine(formattedLine);
                        } else {
                            appendSubAgentLine(formattedLine);
                            setSubAgentState(prev => ({ ...prev, isOpen: true }));
                        }
                    }
                }
                else if (data.type === 'history_update') {
                    setCurrentSessionId(data.sessionId);
                    // Do not clear Document Viewer documents here – they are kept per session in sessionViewerState and would be lost on second switch-back
                    sidebarDocsSyncedForSessionRef.current = null;

                    // Restore active state
                    const isActive = !!data.isActive;
                    const status = data.currentStatus && isActive ? `Agent: ${data.currentStatus}` : '';
                    setLoading(isActive);
                    setIsGenerating(isActive);
                    setStatusMessage(status);

                    // Update per-session loading state tracking
                    sessionLoadingStates.current[data.sessionId] = {
                        loading: isActive,
                        isGenerating: isActive,
                        statusMessage: status,
                        loadingMessageId: isActive ? (data.messages?.length || 0) : null
                    };

                    // Parse server messages and preserve server order (index) so sort is stable
                    const serverMsgs: Array<Message & { _order: number }> = data.messages
                        .filter((m: any) => m.role !== 'system') // Hide raw system prompts from server (we have better local logs)
                        .map((m: any, idx: number) => ({
                            role: m.role,
                            content: m.content,
                            timestamp: m.timestamp ? new Date(m.timestamp).getTime() : Date.now(),
                            _order: idx,
                            toolId: m.toolId,
                            toolName: m.toolName,
                            toolStatus: m.toolStatus as Message['toolStatus'] | undefined
                        }));

                    // Best practice for reload/network stability:
                    // When no generation is active, treat backend history as source of truth.
                    // This avoids expensive cache/orphan merge work on large chats and prevents
                    // stale cached fragments (e.g. partial thinking chunks) from reordering UI.
                    if (!isActive) {
                        const finalServerMsgs = serverMsgs.map(({ _order, ...msg }) => msg) as Message[];
                        setMessages(finalServerMsgs);

                        // If a chat was queued before we had a session, send it now.
                        if (pendingSendRef.current && data.sessionId) {
                            const pending = pendingSendRef.current;
                            pendingSendRef.current = null;
                            pendingSessionRequestRef.current = false;
                            ws?.send(JSON.stringify({
                                type: 'chat',
                                content: pending.text,
                                sessionId: data.sessionId
                            }));
                        }
                        return;
                    }

                    // MERGE STRATEGY: UNION with Server Priority
                    // 1. Hydrate Server Messages with Cache details (e.g. Tool args/status)
                    // 2. Inject Cached Messages that are missing from Server (e.g. System logs, Pending/Streaming Assistant response)

                    const cachedMsgs = sessionCache.current[data.sessionId] || [];

                    // Normalize content for comparison (strip <think> blocks so server "answer only" matches cache "think + answer")
                    const normContent = (s: string) => (s ?? '')
                        .replace(/<think>[\s\S]*?<\/think>/gi, '')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .slice(0, 400);

                    const hydratedServerMsgs = serverMsgs.map((srvMsg: Message & { _order?: number }) => {
                        if (srvMsg.role === 'tool') {
                            const match = cachedMsgs.find(cm =>
                                cm.role === 'tool' &&
                                cm.content === srvMsg.content
                            );
                            if (match) {
                                return { ...srvMsg, ...match, _order: srvMsg._order };
                            }
                        }
                        // Assistant: prefer cache version if it contains <think> so Thinking appears above answer after reload
                        if (srvMsg.role === 'assistant') {
                            const srvNorm = normContent(String(srvMsg.content ?? ''));
                            const withThink = cachedMsgs.find(cm =>
                                cm.role === 'assistant' &&
                                (normContent(String(cm.content ?? '')) === srvNorm || Math.abs((cm.timestamp ?? 0) - (srvMsg.timestamp ?? 0)) < 2000) &&
                                /<think>[\s\S]*?<\/think>/i.test(String(cm.content ?? ''))
                            );
                            if (withThink) {
                                return { ...srvMsg, content: withThink.content, _order: srvMsg._order };
                            }
                        }
                        return srvMsg;
                    });

                    // Find Orphans: Messages in Cache but NOT in Server
                    const orphans = cachedMsgs.filter(cMsg => {
                        // Best practice on reload: trust server history for assistant/user turns.
                        // Cached assistant "orphans" (especially thinking-only stream fragments)
                        // can appear out of turn after restart and should not be re-injected.
                        if (cMsg.role === 'assistant' || cMsg.role === 'user') return false;
                        if (cMsg.role === 'system') return true;

                        const existsInServer = hydratedServerMsgs.some((sMsg: Message) => {
                            if (sMsg.role !== cMsg.role) return false;
                            if (sMsg.content === cMsg.content) return true;
                            if (Math.abs(sMsg.timestamp - cMsg.timestamp) < 2000) return true;
                            return false;
                        });

                        return !existsInServer;
                    }).map((m, idx) => ({ ...m, _order: 100000 + idx })); // So orphans sort after server when timestamps tie

                    // Sort by _order only so server order is strict (fixes user message appearing below assistant when backend timestamps differ or are equal)
                    let finalMsgs = [...hydratedServerMsgs, ...orphans].sort((a, b) =>
                        ((a as any)._order ?? 0) - ((b as any)._order ?? 0)
                    );
                    // Strip _order before setting state
                    finalMsgs = finalMsgs.map(({ _order, ...msg }) => msg) as Message[];

                    // Fix order after reload: cached orphans (system + tool) are appended at end. For each assistant that has system/tool after it, move that run before the assistant. Process from end so indices stay valid. Sort each moved block: system before tool.
                    const assistantIndices = finalMsgs.map((m, i) => ({ role: m.role, i })).filter(({ role }) => role === 'assistant').map(({ i }) => i);
                    for (let a = assistantIndices.length - 1; a >= 0; a--) {
                        const assistantIdx = assistantIndices[a];
                        if (assistantIdx + 1 >= finalMsgs.length) continue;
                        let artifactEnd = assistantIdx + 1;
                        while (artifactEnd < finalMsgs.length && (finalMsgs[artifactEnd].role === 'system' || finalMsgs[artifactEnd].role === 'tool')) artifactEnd++;
                        const runLength = artifactEnd - (assistantIdx + 1);
                        if (runLength <= 0) continue;
                        const turnArtifacts = finalMsgs.splice(assistantIdx + 1, runLength);
                        const systemFirst = [...turnArtifacts.filter((m) => m.role === 'system'), ...turnArtifacts.filter((m) => m.role === 'tool')];
                        finalMsgs.splice(assistantIdx, 0, ...systemFirst);
                        // Update indices for assistants we haven't processed yet (they shifted right)
                        for (let j = 0; j < a; j++) {
                            if (assistantIndices[j] >= assistantIdx) assistantIndices[j] += systemFirst.length;
                        }
                    }

                    // Deduplicate: when switching back to a session, cache + server can both contribute
                    // the same messages (server content cleaned, timestamps differ), causing duplicates
                    const norm = (s: string) => (s ?? '')
                        .replace(/<think>[\s\S]*?<\/think>/gi, '')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .slice(0, 400);
                    finalMsgs = finalMsgs.filter((msg, i) => {
                        const n = norm(String(msg.content ?? ''));
                        const firstIdx = finalMsgs.findIndex(m => m.role === msg.role && norm(String(m.content ?? '')) === n);
                        return firstIdx === i;
                    });

                    // NOTE: Do NOT sort by timestamp here. The _order-based sort + reorder
                    // logic above already produces correct chronological ordering. A timestamp
                    // sort destroys that order for network clients where client-side timestamps
                    // (Date.now() on the browser) differ from server-side timestamps — causing
                    // system/tool messages to appear ABOVE the user prompt instead of below it.

                    // Merge thinking-only assistant orphans into adjacent answer assistant messages.
                    // During streaming, delta extraction after tool use can split one model response
                    // into two assistant messages (thinking-only + answer-only). On history reload,
                    // cached orphans can survive and appear below the answer. Use parseContent() so
                    // we catch complete and incomplete thinking blocks consistently.
                    for (let i = finalMsgs.length - 1; i >= 0; i--) {
                        const msg = finalMsgs[i];
                        if (msg.role !== 'assistant') continue;
                        const content = String(msg.content ?? '');
                        const parsedCurrent = parseContent(content);
                        const hasThinking = !!parsedCurrent.thought && parsedCurrent.thought.trim().length > 0;
                        const hasVisibleAnswer = !!parsedCurrent.answer && parsedCurrent.answer.trim().length > 0;
                        if (!hasThinking || hasVisibleAnswer) continue; // not thinking-only

                        // Look backward first (thinking orphan sorted after answer)
                        let merged = false;
                        for (let j = i - 1; j >= 0; j--) {
                            if (finalMsgs[j].role === 'user') break;
                            if (finalMsgs[j].role === 'assistant') {
                                const tc = String(finalMsgs[j].content ?? '');
                                const parsedTarget = parseContent(tc);
                                if (parsedTarget.answer && parsedTarget.answer.trim().length > 0) {
                                    finalMsgs[j] = { ...finalMsgs[j], content: content + '\n\n' + tc };
                                    finalMsgs.splice(i, 1);
                                    merged = true;
                                    break;
                                }
                            }
                        }
                        if (merged) continue;
                        // Look forward (thinking before answer)
                        for (let j = i + 1; j < finalMsgs.length; j++) {
                            if (finalMsgs[j].role === 'user') break;
                            if (finalMsgs[j].role === 'assistant') {
                                const tc = String(finalMsgs[j].content ?? '');
                                const parsedTarget = parseContent(tc);
                                if (parsedTarget.answer && parsedTarget.answer.trim().length > 0) {
                                    finalMsgs[j] = { ...finalMsgs[j], content: content + '\n\n' + tc };
                                    finalMsgs.splice(i, 1);
                                    break;
                                }
                            }
                        }
                    }

                    // Final safety net: within each turn (between user messages), enforce
                    // role order: system → tool → assistant. This catches any edge cases
                    // where the reorder/merge/dedup pipeline left messages out of order
                    // (e.g. cache+server timestamp mismatches on network clients).
                    {
                        const roleWeight = (r: string) => r === 'system' ? 0 : r === 'tool' ? 1 : 2;
                        let turnStart = 0;
                        for (let i = 0; i <= finalMsgs.length; i++) {
                            if (i === finalMsgs.length || finalMsgs[i].role === 'user') {
                                // Sort the turn segment [turnStart, i) by role, stable
                                if (i - turnStart > 1) {
                                    const segment = finalMsgs.slice(turnStart, i)
                                        .map((m, idx) => ({ m, idx }))
                                        .sort((a, b) => {
                                            const wa = roleWeight(a.m.role);
                                            const wb = roleWeight(b.m.role);
                                            if (wa !== wb) return wa - wb;
                                            return a.idx - b.idx; // stable: preserve relative order within same role
                                        })
                                        .map(({ m }) => m);
                                    finalMsgs.splice(turnStart, i - turnStart, ...segment);
                                }
                                turnStart = i + 1; // next segment starts after user message
                            }
                        }
                    }

                    setMessages(finalMsgs);

                    // If a chat was queued before we had a session, send it now.
                    if (pendingSendRef.current && data.sessionId) {
                        const pending = pendingSendRef.current;
                        pendingSendRef.current = null;
                        pendingSessionRequestRef.current = false;
                        ws?.send(JSON.stringify({
                            type: 'chat',
                            content: pending.text,
                            sessionId: data.sessionId
                        }));
                    }
                }
                else if (data.type === 'context_checkpoint') {
                    // Backend intentionally compressed history — clear local session cache so
                    // old messages are not re-injected as "orphans" on the next history_update.
                    const sid = data.session_id || currentSessionIdRef.current;
                    if (sid) {
                        sessionCache.current[sid] = [];
                    }
                }
                else if (data.type === 'config_update') {
                    setConfig(data.config);
                }
                else if (data.type === 'config_saved') {
                    ws?.send(JSON.stringify({ type: 'get_config' }));
                    // Provider or other critical change: show overlay and reload after 5s
                    if (data.requires_refresh) {
                        setShowChangingModelOverlay(true);
                        setTimeout(() => {
                            setShowChangingModelOverlay(false);
                            window.location.reload();
                        }, 5000);
                    }
                }
                else if (data.type === 'models_list') {
                    setAvailableModels(data.models || []);
                }
                else if (data.type === 'model_preview') {
                    if (data.error && !data.gguf_files?.length) {
                        setModelPreviewData({ repo_id: data.repo_id || '', gguf_files: [], error: data.error });
                    } else {
                        setModelPreviewData({
                            repo_id: data.repo_id || '',
                            card_content: data.card_content,
                            gguf_files: data.gguf_files || [],
                            error: data.error
                        });
                    }
                }
                else if (data.type === 'model_download_progress') {
                    setDownloadModelStatus(prev => prev.status === 'downloading' ? {
                        ...prev,
                        progress_pct: data.progress_pct,
                        bytes_done: data.bytes_done,
                        bytes_total: data.bytes_total,
                        speed_str: data.speed_str
                    } : prev);
                }
                else if (data.type === 'model_download_done') {
                    setAvailableModels(data.models || []);
                    setDownloadModelStatus(data.success ? { status: 'done' } : { status: 'error', message: data.error || 'Download failed' });
                    setDownloadToast({ show: true, message: data.success ? 'Model downloaded.' : (data.error || 'Download failed'), success: data.success });
                    setTimeout(() => setDownloadToast(prev => ({ ...prev, show: false })), 4000);
                    if (data.success) setTimeout(() => setDownloadModelStatus({ status: 'idle' }), 3000);
                    if (!data.success) setTimeout(() => setDownloadModelStatus({ status: 'idle' }), 5000);
                }
                else if (data.type === 'api_models' || data.type === 'api_models_list') {
                    setApiModels(prev => ({ ...prev, [data.provider]: data.models }));
                }
                else if (data.type === 'tools_list') {
                    setTools(data.tools || []);
                }
                else if (data.type === 'workflows_list') {
                    console.log('[Workflows]', data.workflows);
                    setWorkflows(data.workflows || []);
                }
                else if (data.type === 'trusted_sources_list') {
                    setTrustedSources({ categories: data.categories || [] });
                    setTrustedSourcesError(null);
                }
                else if (data.type === 'trusted_source_updated') {
                    if (data.ok) {
                        setTrustedSources({ categories: data.categories || [] });
                        setTrustedSourcesError(null);
                    } else {
                        setTrustedSourcesError(data.error || 'Error');
                    }
                }
                else if (data.type === 'config') {
                    setConfig(data.config);
                    setSttEnabled(data.config.stt_enabled === true);
                    // Extract initial available models if present in config (legacy method)
                    if (data.config.llm_available_models) {
                        setAvailableModels(data.config.llm_available_models);
                    }
                }
                else if (data.type === 'automations_list') {
                    setAutomations(data.automations || []);
                }
                else if (data.type === 'create_automation_result') {
                    const resolve = pendingCreateAutomationResolveRef.current;
                    pendingCreateAutomationResolveRef.current = null;
                    resolve?.({ ok: data.ok === true, error: data.error });
                }
                else if (data.type === 'update_automation_result') {
                    const resolve = pendingUpdateAutomationResolveRef.current;
                    pendingUpdateAutomationResolveRef.current = null;
                    resolve?.({ ok: data.ok === true, error: data.error });
                    if (data.ok === true) ws?.send(JSON.stringify({ type: 'get_automations' }));
                }
                else if (data.type === 'delete_automation_result') {
                    if (data.ok === true) {
                        ws?.send(JSON.stringify({ type: 'get_automations' }));
                    } else {
                        setDeletingAutomationId(null);
                    }
                }
                else if (data.type === 'notification' && data.notification) {
                    setNotifications(prev => [data.notification, ...prev]);
                }
                else if (data.type === 'notifications_list' && Array.isArray(data.notifications)) {
                    setNotifications(data.notifications);
                }
                else if (data.type === 'automation_notes_list') {
                    setAutomationNotes(Array.isArray(data.notes) ? data.notes : []);
                }
                else if (data.type === 'automation_todos_list') {
                    setAutomationTodos(Array.isArray(data.todos) ? data.todos : []);
                }
                else if (data.type === 'create_automation_note_result' && data.ok === true) {
                    if (data.note && typeof data.note === 'object' && data.note.id) {
                        setAutomationNotes(prev => [...prev, data.note]);
                    } else {
                        ws?.send(JSON.stringify({ type: 'get_automation_notes' }));
                    }
                }
                else if (data.type === 'create_automation_todo_result' && data.ok === true) {
                    if (data.todo && typeof data.todo === 'object' && data.todo.id) {
                        setAutomationTodos(prev => [...prev, data.todo]);
                    } else {
                        ws?.send(JSON.stringify({ type: 'get_automation_todos' }));
                    }
                }
                else if (data.type === 'update_automation_todo_result' && data.ok === true) {
                    ws?.send(JSON.stringify({ type: 'get_automation_todos' }));
                }
                else if (data.type === 'delete_automation_note_result' && data.ok === true) {
                    if (data.id) {
                        setAutomationNotes(prev => prev.filter(n => n.id !== data.id));
                    } else {
                        ws?.send(JSON.stringify({ type: 'get_automation_notes' }));
                    }
                }
                else if (data.type === 'delete_automation_todo_result' && data.ok === true) {
                    if (data.id) {
                        setAutomationTodos(prev => prev.filter(t => t.id !== data.id));
                    } else {
                        ws?.send(JSON.stringify({ type: 'get_automation_todos' }));
                    }
                }
                else if (data.type === 'model_state') {
                    if (typeof data.loaded === 'boolean') {
                        setModelLoaded(data.loaded);
                    }
                    if (typeof data.provider === 'string') {
                        setModelProvider(data.provider);
                    }
                }
                else if (data.type === 'stt_result') {
                    // STT transcription result: append to existing input so multiple voice segments accumulate
                    const text = (data.text || '').trim();
                    if (text) {
                        const prev = inputValueRef.current || '';
                        const newValue = prev ? prev.trimEnd() + ' ' + text : text;
                        setInput(newValue);
                        setIsProcessingAudio(false);

                        // Reset timer so we only send after user stops speaking (last segment + 0.5s)
                        if (pendingSttSendRef.current) clearTimeout(pendingSttSendRef.current);
                        pendingSttSendRef.current = setTimeout(() => {
                            pendingSttSendRef.current = null;
                            sendMessage(undefined, newValue);
                        }, 500);
                    } else {
                        setIsProcessingAudio(false);
                    }
                }
                else if (data.type === 'stt_error') {
                    console.error('STT Error:', data.error);
                    alert(`Voice Error: ${data.error}`);
                    setIsProcessingAudio(false);
                }
                else if (data.type === 'autosuggest_result') {
                    setSuggestion(data.suggestion || '');
                }
                else if (data.type === 'generation_stopped') {
                    // Update per-session loading state
                    if (data.sessionId) {
                        sessionLoadingStates.current[data.sessionId] = {
                            loading: false,
                            isGenerating: false,
                            statusMessage: '',
                            loadingMessageId: null
                        };
                    }
                    // Only update UI if this is the active session
                    const activeSessionId = currentSessionIdRef.current;
                    if (!data.sessionId || data.sessionId === activeSessionId) {
                        setLoading(false);
                        setIsGenerating(false);
                        setLoadingMessageId(null);
                        // Clear workflow runtime so stop button hides (workflow was stopped)
                        clearWorkflow();
                    }
                }
                else if (data.type === 'memory_learning') {
                    // Memory Learning status updates
                    if (data.status === 'started') {
                        if (memoryLearningTimeoutRef.current) clearTimeout(memoryLearningTimeoutRef.current);
                        setMemoryLearning({ active: true, message: data.message || 'Memory Learning in progress...' });
                        // If backend never sends completed/error (e.g. message dropped, server loop missing), auto-dismiss after 90s so UI is not stuck
                        memoryLearningTimeoutRef.current = setTimeout(() => {
                            memoryLearningTimeoutRef.current = null;
                            setMemoryLearning((prev) => prev?.active ? { active: false, message: 'Memory Learning finished.' } : prev);
                            setTimeout(() => setMemoryLearning(null), 4000);
                        }, 90000);
                    } else if (data.status === 'completed' || data.status === 'error') {
                        if (memoryLearningTimeoutRef.current) {
                            clearTimeout(memoryLearningTimeoutRef.current);
                            memoryLearningTimeoutRef.current = null;
                        }
                        setMemoryLearning({ active: false, message: data.message || 'Memory Learning complete!' });
                        setTimeout(() => setMemoryLearning(null), 4000);
                    }
                }
            } catch (e) { console.error(e); }
        };
        socket.onclose = () => {
            setStatus('disconnected');
            setWs(null);
            reconnectTimeout = setTimeout(() => {
                setStatus('connecting');
                setReconnectAttempt((a) => a + 1);
            }, 3000);
        };
        socket.onerror = () => setStatus('disconnected');
        if (!cancelled) setWs(socket);
        })();
        return () => {
            cancelled = true;
            if (reconnectTimeout) clearTimeout(reconnectTimeout);
            if (memoryLearningTimeoutRef.current) clearTimeout(memoryLearningTimeoutRef.current);
            memoryLearningTimeoutRef.current = null;
            wsSocketRef.current?.close();
            wsSocketRef.current = null;
            setWs(null);
        };
    }, [reconnectAttempt]);

    // Open Settings with Connections tab when returning from OAuth callback; refresh config to show new account
    useEffect(() => {
        if (!isAuthenticated || authChecking) return;
        const conn = searchParams.get('connections');
        const cloudOauth = searchParams.get('cloud_oauth');
        const emailOauth = searchParams.get('email_oauth');
        if (conn === '1' || cloudOauth || emailOauth) {
            openedFromOAuthRef.current = true;
            setIsSettingsOpen(true);
            setSettingsInitialTab('connections');
            if (ws?.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'get_config' }));
            }
        }
    }, [isAuthenticated, authChecking, searchParams, ws]);

    useEffect(() => {
        if (ws && status === 'connected' && input.length >= 2) {
            const timeoutId = setTimeout(() => {
                ws.send(JSON.stringify({ type: 'get_autosuggest', text: input }));
            }, 300);
            return () => clearTimeout(timeoutId);
        } else {
            setSuggestion('');
        }
    }, [input, ws, status]);

    useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollIntoView({ behavior: 'smooth' });
    }, [messages, loading]);

    // Keep inputValueRef in sync so WebSocket handlers see latest input (e.g. STT append)
    useEffect(() => {
        inputValueRef.current = input;
    }, [input]);

    // Clear pending STT auto-send on unmount
    useEffect(() => {
        return () => {
            if (pendingSttSendRef.current) {
                clearTimeout(pendingSttSendRef.current);
                pendingSttSendRef.current = null;
            }
        };
    }, []);

    // Sync sttEnabled state with config changes
    useEffect(() => {
        setSttEnabled(config.stt_enabled === true);

        const isApi = config.provider && config.provider !== 'local';
        const nCtx = config.n_ctx ?? 8192;
        const max_tokens = isApi && nCtx <= 16384 ? 128000 : nCtx;

        // Initialize context stats if empty (so bar is always visible)
        // Use provider-aware max_tokens: API mode (e.g. OpenAI) uses 128k, local uses n_ctx
        if (!contextStats) {
            setContextStats({
                tokens: 0,
                max_tokens,
                percent: 0,
                message_count: 0
            });
        } else if (contextStats.max_tokens !== max_tokens) {
            // Provider or n_ctx changed: update max_tokens so display matches backend
            setContextStats((prev: { tokens: number; max_tokens: number; percent: number; message_count?: number }) => ({
                ...prev,
                max_tokens,
                percent: prev.tokens && max_tokens ? Math.round((prev.tokens / max_tokens) * 1000) / 10 : prev.percent,
            }));
        }
    }, [config, contextStats]);

    // ESC: close context modal
    useEffect(() => {
        if (!isContextModalOpen) return;
        const onKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                setIsContextModalOpen(false);
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [isContextModalOpen]);

    const stopGeneration = () => {
        if (!ws || !currentSessionId) return;
        ws.send(JSON.stringify({
            type: 'stop_generation',
            sessionId: currentSessionId
        }));
        setLoading(false);
        setIsGenerating(false);

        // Update per-session loading state
        sessionLoadingStates.current[currentSessionId] = {
            loading: false,
            isGenerating: false,
            statusMessage: '',
            loadingMessageId: null
        };
    };

    const sendMessage = async (e?: React.FormEvent, overrideText?: string) => {
        e?.preventDefault();
        const combined = [input, ...insertedSelections.map((s) => s.text)].filter(Boolean).join('\n\n');
        const textToSend = overrideText ?? combined;
        if (!textToSend.trim() || !ws) return;

        setMessages(prev => [...prev, {
            role: 'user',
            content: textToSend,
            timestamp: Date.now(),
            sidebarDocs: documentViewerState.documents.length > 0
                ? documentViewerState.documents.map(d => d.name)
                : undefined,
        }]);
        expectNewAssistantRef.current = true;
        lastUserSendTimeRef.current = Date.now();
        setLoading(true);
        setIsGenerating(true);

        if (currentSessionId) {
            sessionLoadingStates.current[currentSessionId] = {
                loading: true,
                isGenerating: true,
                statusMessage: '',
                loadingMessageId: null
            };
        }

        if (!currentSessionId) {
            pendingSendRef.current = { text: textToSend };
            if (!pendingSessionRequestRef.current) {
                pendingSessionRequestRef.current = true;
                ws.send(JSON.stringify({ type: 'new_session' }));
            }
            setInput('');
            setSuggestion('');
            return;
        }

        const sidebarPayload = documentViewerState.documents.filter(d => d.data).length > 0
            ? documentViewerState.documents.filter(d => d.data).map(d => ({ name: d.name, data: d.data, mimeType: d.mimeType || '' }))
            : undefined;
        const editorDoc =
            documentEditorState.isOpen && documentEditorState.content
                ? (() => {
                    const div = document.createElement('div');
                    div.innerHTML = documentEditorState.content;
                    return { name: documentEditorState.title || 'Document', content: (div.textContent || div.innerText || '').trim() };
                })()
                : undefined;
        const editorSelectionsPayload =
            insertedSelections
                .filter((s) => s.documentId === 'editor')
                .map((s) => ({ start: s.start, end: s.end, text: s.text }));
        ws.send(JSON.stringify({
            type: 'chat',
            content: textToSend,
            sessionId: currentSessionId,
            ...(sidebarPayload && sidebarPayload.length > 0 ? { sidebarDocuments: sidebarPayload } : {}),
            ...(editorDoc && editorDoc.content !== '' ? { editorDocument: editorDoc } : {}),
            ...(editorSelectionsPayload.length > 0 ? { editorSelections: editorSelectionsPayload } : {}),
        }));
        setInput('');
        setSuggestion('');
    };

    const fileToBase64 = (file: File): Promise<string> => {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.readAsDataURL(file);
            reader.onload = () => resolve(reader.result as string);
            reader.onerror = error => reject(error);
        });
    };

    const ACCEPT_ATTACHMENTS = '.pdf,.docx,.xlsx,.pptx,.txt,.md,.json,.csv';
    const acceptedExtensions = useMemo(() => new Set(ACCEPT_ATTACHMENTS.split(',').map(ext => ext.trim().toLowerCase())), []);

    const addFilesAsAttachments = useCallback(async (newFiles: File[]) => {
        const filtered = newFiles.filter(f => {
            const ext = '.' + (f.name.split('.').pop() ?? '').toLowerCase();
            return acceptedExtensions.has(ext);
        });
        if (filtered.length === 0) return;
        const base64List = await Promise.all(filtered.map(f => fileToBase64(f)));
        const newDocs = filtered.map((f, i) => ({
            id: crypto.randomUUID(),
            name: f.name,
            mimeType: f.type,
            data: base64List[i],
        }));
        setDocumentViewerState(prev => {
            const newList = [...prev.documents, ...newDocs];
            if (ws && currentSessionId) {
                ws.send(JSON.stringify({
                    type: 'set_sidebar_documents',
                    sessionId: currentSessionId,
                    documents: newList.map(d => ({ name: d.name, data: d.data, mimeType: d.mimeType })),
                }));
                sidebarDocsSyncedForSessionRef.current = currentSessionId;
            }
            return { ...prev, documents: newList, isOpen: true };
        });
        setShowSubAgentPanel(true);
    }, [ws, currentSessionId, acceptedExtensions]);

    const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const newFiles = e.target.files ? Array.from(e.target.files) : [];
        e.target.value = '';
        if (newFiles.length === 0) return;
        await addFilesAsAttachments(newFiles);
    };

    const handleDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        setIsDragOver(false);
        const files = e.dataTransfer.files ? Array.from(e.dataTransfer.files) : [];
        if (files.length > 0) addFilesAsAttachments(files);
    }, [addFilesAsAttachments]);

    const handleDragOver = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.dataTransfer.types.includes('Files')) setIsDragOver(true);
    }, []);

    const handleDragLeave = useCallback((e: React.DragEvent) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setIsDragOver(false);
    }, []);


    const handleDocumentViewerAddFiles = async (files: File[]) => {
        if (!ws || !currentSessionId) return;
        const base64List = await Promise.all(files.map(f => fileToBase64(f)));
        const newDocs = files.map((f, i) => ({
            id: crypto.randomUUID(),
            name: f.name,
            mimeType: f.type,
            data: base64List[i],
        }));
        setDocumentViewerState(prev => {
            const newList = [...prev.documents, ...newDocs];
            ws.send(JSON.stringify({
                type: 'set_sidebar_documents',
                sessionId: currentSessionId,
                documents: newList.map(d => ({ name: d.name, data: d.data, mimeType: d.mimeType })),
            }));
            sidebarDocsSyncedForSessionRef.current = currentSessionId;
            return { ...prev, documents: newList };
        });
    };

    const handleDocumentViewerRemove = (id: string) => {
        setDocumentViewerState(prev => {
            const newList = prev.documents.filter(d => d.id !== id);
            const withData = newList.filter(d => d.data);
            if (ws && currentSessionId) {
                ws.send(JSON.stringify({
                    type: 'set_sidebar_documents',
                    sessionId: currentSessionId,
                    documents: withData.map(d => ({ name: d.name, data: d.data, mimeType: d.mimeType })),
                }));
            }
            return { ...prev, documents: newList };
        });
    };

    const handleDocumentViewerClose = () => {
        if (ws && currentSessionId) {
            ws.send(JSON.stringify({ type: 'set_sidebar_documents', sessionId: currentSessionId, documents: [] }));
        }
        sidebarDocsSyncedForSessionRef.current = null;
        setDocumentViewerState({ isOpen: false, documents: [] });
    };

    // When we get a session after user already added docs (no session before), sync sidebar to backend
    useEffect(() => {
        if (!currentSessionId || !ws || sidebarDocsSyncedForSessionRef.current === currentSessionId) return;
        if (ws.readyState !== WebSocket.OPEN) return; // avoid "Still in CONNECTING state" when adding attachment early
        const withData = documentViewerState.documents.filter(d => d.data);
        if (withData.length === 0) return;
        ws.send(JSON.stringify({
            type: 'set_sidebar_documents',
            sessionId: currentSessionId,
            documents: withData.map(d => ({ name: d.name, data: d.data, mimeType: d.mimeType })),
        }));
        sidebarDocsSyncedForSessionRef.current = currentSessionId;
    }, [currentSessionId, ws, documentViewerState.documents]);

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        // Enter = send, Shift+Enter = new line
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const hasContent = input.trim() || insertedSelections.length > 0;
            if (hasContent && !isGenerating) {
                sendMessage(undefined);
            }
            return;
        }

        // Handle suggestion popup navigation
        if (suggestionList.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setSelectedSuggestionIndex(prev =>
                    prev < suggestionList.length - 1 ? prev + 1 : 0
                );
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                setSelectedSuggestionIndex(prev =>
                    prev > 0 ? prev - 1 : suggestionList.length - 1
                );
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                if (suggestionList.length > 0) {
                    e.preventDefault();
                    handleSuggestionClick(suggestionList[selectedSuggestionIndex]);
                    return;
                }
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                setSuggestionList([]);
                setSuggestionType(null);
                setSelectedSuggestionIndex(0);
                return;
            }
        }

        // Tab completion for inline suggestions
        if (e.key === 'Tab' && suggestion) {
            e.preventDefault();
            setInput(input + suggestion);
            setSuggestion('');
        }
    };

    // Auto-grow textarea: 1 line min, 10 lines max, then scrollbar
    const resizeInput = useCallback(() => {
        const el = inputRef.current;
        if (!el) return;
        el.style.height = 'auto';
        const lineHeight = 20;
        const maxHeight = lineHeight * 10;
        el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
    }, []);
    useEffect(() => {
        resizeInput();
    }, [input, resizeInput]);

    const getMicErrorMessage = (error: unknown) => {
        if (!(error instanceof DOMException)) {
            return 'Could not access microphone. Please try again.';
        }

        switch (error.name) {
            case 'NotAllowedError':
                return 'Microphone permission denied. Please allow access in your browser settings.';
            case 'NotFoundError':
                return 'No microphone found. Please connect a microphone and try again.';
            case 'NotReadableError':
                return 'Microphone is busy or unavailable. Close other apps using it, refresh this page, then try again.';
            case 'OverconstrainedError':
                return 'Microphone constraints could not be satisfied. Try a different device.';
            case 'SecurityError':
                return 'Microphone access blocked by browser security settings.';
            default:
                return `Could not access microphone (${error.name}). Please try again.`;
        }
    };

    const startRecording = async () => {
        if (!sttEnabled) {
            const confirmEnable = confirm("Voice Input is currently disabled. Would you like to open Settings to enable it?");
            if (confirmEnable) {
                setSettingsInitialTab(null);
                setIsSettingsOpen(true);
            }
            return;
        }

        if (!navigator.mediaDevices?.getUserMedia) {
            alert('Microphone access is not supported by this browser.');
            return;
        }

        if (!window.isSecureContext) {
            alert('Microphone access requires a secure context (HTTPS or localhost).');
            return;
        }

        try {
            // Release any previous mic/recorder so the device is free (reduces NotReadableError)
            releaseMic();
            await new Promise((r) => setTimeout(r, 400));

            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            mediaStreamRef.current = stream;

            // Audio Context for VAD (Voice Activity Detection)
            const audioContext = new AudioContext();
            audioContextRef.current = audioContext;
            const analyser = audioContext.createAnalyser();
            analyser.fftSize = 256;
            analyserRef.current = analyser;
            const source = audioContext.createMediaStreamSource(stream);
            source.connect(analyser);

            // Try to use audio/wav or audio/ogg for better compatibility with Whisper
            // Chrome default is webm/opus which some Whisper containers struggle with
            let mimeType = 'audio/webm';
            if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
                mimeType = 'audio/webm;codecs=opus';
            } else if (MediaRecorder.isTypeSupported('audio/ogg;codecs=opus')) {
                mimeType = 'audio/ogg;codecs=opus';
            }
            console.log('[STT] Using MediaRecorder mimeType:', mimeType);

            const mediaRecorder = new MediaRecorder(stream, { mimeType });
            mediaRecorderRef.current = mediaRecorder;
            audioChunksRef.current = [];

            mediaRecorder.ondataavailable = (event) => {
                if (event.data.size > 0) {
                    audioChunksRef.current.push(event.data);
                }
            };

            mediaRecorder.onstop = async () => {
                const audioBlob = new Blob(audioChunksRef.current, { type: mimeType });

                try {
                    // Convert webm/opus to WAV for better Whisper compatibility
                    console.log('[STT] Converting audio to WAV...');
                    const wavBlob = await convertToWav(audioBlob);
                    console.log('[STT] WAV conversion complete, size:', wavBlob.size);

                    // Convert to base64
                    const reader = new FileReader();
                    reader.readAsDataURL(wavBlob);
                    reader.onloadend = () => {
                        const base64Audio = reader.result as string;
                        // Send to backend
                        ws?.send(JSON.stringify({
                            type: 'process_audio',
                            audio: base64Audio.split(',')[1], // Remove data:audio/wav;base64, prefix
                            format: 'wav'
                        }));
                        setIsProcessingAudio(true);
                    };
                } catch (conversionError) {
                    console.warn('[STT] WAV conversion failed, sending original format:', conversionError);
                    // Fallback: send original format
                    const reader = new FileReader();
                    reader.readAsDataURL(audioBlob);
                    reader.onloadend = () => {
                        const base64Audio = reader.result as string;
                        ws?.send(JSON.stringify({
                            type: 'process_audio',
                            audio: base64Audio.split(',')[1]
                        }));
                        setIsProcessingAudio(true);
                    };
                }

                // Stop all tracks
                stream.getTracks().forEach(track => track.stop());
                if (mediaStreamRef.current === stream) {
                    mediaStreamRef.current = null;
                }

                // Cleanup AudioContext
                if (audioContextRef.current) {
                    audioContextRef.current.close().catch(console.error);
                    audioContextRef.current = null;
                }
                if (animationFrameRef.current) {
                    cancelAnimationFrame(animationFrameRef.current);
                    animationFrameRef.current = null;
                }
                setVolume(0);
            };

            // VAD Logic setup
            hasSpokenRef.current = false;
            silenceStartRef.current = null;
            const dataArray = new Uint8Array(analyser.frequencyBinCount);

            const detectSilence = () => {
                if (!analyserRef.current) return;
                analyserRef.current.getByteFrequencyData(dataArray);

                // Calculate average volume
                let sum = 0;
                for (let i = 0; i < dataArray.length; i++) {
                    sum += dataArray[i];
                }
                const average = sum / dataArray.length;
                setVolume(average); // Update UI

                // Thresholds (adjustable)
                const SPEECH_THRESHOLD = 20;
                const SILENCE_DURATION = 1500; // 1.5 seconds

                if (average > SPEECH_THRESHOLD) {
                    hasSpokenRef.current = true;
                    silenceStartRef.current = null; // Reset silence timer
                } else {
                    if (hasSpokenRef.current) {
                        if (silenceStartRef.current === null) {
                            silenceStartRef.current = Date.now();
                        } else if (Date.now() - silenceStartRef.current > SILENCE_DURATION) {
                            // Auto-Stop
                            stopRecording();
                            return; // Exit loop
                        }
                    }
                }

                animationFrameRef.current = requestAnimationFrame(detectSilence);
            };

            mediaRecorder.start();
            detectSilence(); // Start VAD loop
            setIsRecording(true);
        } catch (error) {
            // Use console.warn instead of console.error to avoid triggering Next.js error overlay
            // This is an expected user-facing error (mic busy, permission denied, etc.)
            console.warn('[STT] Microphone access failed:', error instanceof Error ? error.message : error);
            alert(getMicErrorMessage(error));
        }
    };

    const releaseMic = useCallback(() => {
        if (animationFrameRef.current) {
            cancelAnimationFrame(animationFrameRef.current);
            animationFrameRef.current = null;
        }
        if (audioContextRef.current) {
            audioContextRef.current.close().catch(() => { });
            audioContextRef.current = null;
        }
        if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'recording') {
            try {
                mediaRecorderRef.current.stop();
            } catch {
                // ignore
            }
            mediaRecorderRef.current = null;
        } else {
            mediaRecorderRef.current = null;
        }
        if (mediaStreamRef.current) {
            mediaStreamRef.current.getTracks().forEach((t) => t.stop());
            mediaStreamRef.current = null;
        }
        setVolume(0);
        setIsRecording(false);
    }, []);

    const stopRecording = () => {
        if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'recording') {
            mediaRecorderRef.current.stop();
            setIsRecording(false);
        } else {
            releaseMic();
        }
    };

    useEffect(() => {
        return () => {
            releaseMic();
        };
    }, [releaseMic]);

    const startEditing = (s: Session) => {
        setEditingId(s.id);
        setEditName(s.title.replace(".json", ""));
    };

    const submitRename = () => {
        if (editingId && editName.trim()) {
            // Optimistic update to prevent flickering
            setSessions(prev => prev.map(s =>
                s.id === editingId ? { ...s, title: editName.trim() } : s
            ));

            ws?.send(JSON.stringify({ type: 'rename_session', id: editingId, newName: editName.trim() }));
            setEditingId(null);
        } else {
            setEditingId(null);
        }
    };

    const handleSaveConfig = (newConfig: any) => {
        const providerChanged = newConfig.provider !== config?.provider;
        if (providerChanged) {
            setShowChangingModelOverlay(true);
            setTimeout(() => {
                setShowChangingModelOverlay(false);
                window.location.reload();
            }, 5000);
        }
        ws?.send(JSON.stringify({ type: 'save_config', config: newConfig }));
        setConfig(newConfig);
    };

    const fetchApiModels = (provider: string, apiKey: string) => {
        ws?.send(JSON.stringify({ type: 'get_api_models', provider, api_key: apiKey }));
    };

    const refreshLocalModels = () => {
        ws?.send(JSON.stringify({ type: 'get_models' }));
    };

    const requestModelPreview = (repoId: string) => {
        const trimmed = repoId.trim();
        if (!trimmed) return;
        ws?.send(JSON.stringify({ type: 'get_model_preview', repo_id: trimmed }));
    };

    const downloadModel = (repoId: string, filename?: string) => {
        const trimmed = repoId.trim();
        if (!trimmed) return;
        setDownloadModelStatus({ status: 'downloading', repo_id: trimmed });
        ws?.send(JSON.stringify({ type: 'download_model', repo_id: trimmed, ...(filename ? { filename } : {}) }));
    };

    const cancelModelDownload = () => {
        ws?.send(JSON.stringify({ type: 'cancel_model_download' }));
    };

    const subAgentStatusLower = subAgentState.status.toLowerCase();
    const hasRunningSubAgentStep = subAgentState.steps.some(step => step.status === 'running');
    const isSubAgentRunning = subAgentState.presence === 'online' || hasRunningSubAgentStep || subAgentStatusLower.includes('running') || subAgentStatusLower.includes('pending');
    const hasCompletedOrDoneStep = subAgentState.steps.some(
        (step: { status?: string }) =>
            step.status === 'completed' || step.status === 'failed' || step.status === 'timeout'
    );
    // Allow close when subagent finished, failed, or timed out (so user can always close on error)
    const subAgentCanClose = !hasRunningSubAgentStep && (
        subAgentStatusLower.includes('completed') ||
        subAgentStatusLower.includes('done') ||
        subAgentStatusLower.includes('failed') ||
        subAgentStatusLower.includes('timeout') ||
        subAgentStatusLower.includes('error') ||
        (subAgentStatusLower.includes('idle') && hasCompletedOrDoneStep)
    );
    const subAgentHasContent = Boolean(
        subAgentState.steps.length ||
        subAgentState.artifactCode ||
        subAgentState.codeContent ||
        subAgentState.artifactFile ||
        subAgentState.currentFile
    );

    const providerName = modelProvider || config?.provider || 'local';
    const isLocalProvider = providerName === 'local';
    const isConnected = status === 'connected';
    const showIdleState = isConnected && isLocalProvider && modelLoaded === false;
    const connectionLabel = isConnected ? (showIdleState ? tStatus('idle') : tStatus('connected')) : tStatus('disconnected');

    const handleArtifactChange = (nextValue: string) => {
        const nextFile = subAgentState.artifactFile || subAgentState.currentFile;
        artifactDirtyRef.current = true;
        artifactLastEditRef.current = Date.now();
        setSubAgentState(prev => ({
            ...prev,
            artifactCode: nextValue,
            artifactFile: nextFile,
            artifactStatus: 'Editing'
        }));

        if (artifactSendTimeoutRef.current) {
            clearTimeout(artifactSendTimeoutRef.current);
        }

        artifactSendTimeoutRef.current = setTimeout(() => {
            const sessionId = currentSessionIdRef.current;
            if (!sessionId || !ws) return;
            setSubAgentState(prev => ({ ...prev, artifactStatus: 'Saving' }));
            ws.send(JSON.stringify({
                type: 'artifact_edit',
                sessionId,
                file: nextFile,
                code: nextValue,
                source: 'web'
            }));
        }, 500);
    };

    useEffect(() => {
        if (!subAgentState.isOpen) return;
        if (subAgentManualOpenRef.current) return;
        if (!subAgentCanClose) {
            if (subAgentAutoCloseRef.current) {
                clearTimeout(subAgentAutoCloseRef.current);
                subAgentAutoCloseRef.current = null;
            }
            return;
        }

        if (subAgentAutoCloseRef.current) {
            clearTimeout(subAgentAutoCloseRef.current);
        }

        subAgentAutoCloseRef.current = setTimeout(() => {
            setSubAgentState(prev => ({ ...prev, isOpen: false }));
            subAgentAutoCloseRef.current = null;
        }, 3000);

        return () => {
            if (subAgentAutoCloseRef.current) {
                clearTimeout(subAgentAutoCloseRef.current);
                subAgentAutoCloseRef.current = null;
            }
        };
    }, [subAgentCanClose, subAgentState.isOpen]);

    useEffect(() => {
        if (subAgentState.isOpen && !showSubAgentPanel) {
            setShowSubAgentPanel(true);
        }
    }, [subAgentState.isOpen, showSubAgentPanel]);

    const chatWidthClass = subAgentState.isOpen ? 'max-w-3xl' : 'max-w-4xl';
    const messagesAreaWidthClass = subAgentState.isOpen ? 'max-w-5xl' : 'max-w-6xl';

    if (authChecking) {
        return (
            <main className="h-screen flex flex-col items-center justify-center bg-gray-50">
                <div className="w-10 h-10 border-2 border-gray-300 border-t-gray-900 rounded-full animate-spin" />
                <p className="mt-4 text-sm text-gray-500">{tAuth('checkingSession')}</p>
            </main>
        );
    }
    if (authError) {
        return (
            <main className="h-screen flex flex-col items-center justify-center bg-gray-50 px-4">
                <p className="text-sm text-gray-600 text-center max-w-md">{tAuth('serverUnreachable')}</p>
                <button
                    type="button"
                    onClick={() => { setAuthError(null); setAuthChecking(true); setAuthRetryKey((k) => k + 1); }}
                    className="mt-4 px-4 py-2 bg-gray-900 text-white text-sm font-medium rounded-lg hover:bg-gray-800"
                >
                    {tAuth('retry')}
                </button>
            </main>
        );
    }
    if (!isAuthenticated) {
        return (
            <main className="h-screen flex flex-col items-center justify-center bg-gray-50">
                <p className="text-sm text-gray-500">{tAuth('redirectingToLogin')}</p>
            </main>
        );
    }

    return (
        <main
            className="h-screen flex flex-col bg-gray-50 text-gray-900 font-sans overflow-hidden relative"
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
        >
            {isDragOver && (
                <div className="fixed inset-0 z-[100] flex items-center justify-center bg-blue-100/95 pointer-events-none">
                    <span className="text-lg font-medium text-blue-700">{tMain('dropFilesHere')}</span>
                </div>
            )}
            {pendingContactReplies.length > 0 && (
                <div className="shrink-0 bg-amber-50 border-b border-amber-200 px-4 py-3 flex items-center gap-4 flex-wrap">
                    {pendingContactReplies.slice(0, 3).map((p) => {
                        const channel = p.source === 'telegram' ? 'Telegram' : 'WhatsApp';
                        return (
                            <div key={p.replyId} className="flex items-center gap-3 bg-white rounded-lg border border-amber-200 p-3 shadow-sm min-w-0 max-w-2xl">
                                <div className="min-w-0 flex-1">
                                    <p className="text-sm font-medium text-amber-900">{tMain('contactReplyTitle')}</p>
                                    <p className="text-xs text-amber-800 mt-0.5">
                                        {tMain('contactReplyTo', { name: p.contactName || p.source, channel })}
                                    </p>
                                    <p className="text-xs text-gray-600 mt-1 truncate" title={p.preview}>{p.preview}</p>
                                </div>
                                <div className="flex gap-2 shrink-0">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setPendingContactReplies(prev => prev.filter(x => x.replyId !== p.replyId));
                                            ws?.send(JSON.stringify({ type: 'contact_reply_decision', replyId: p.replyId, decision: 'approve' }));
                                        }}
                                        className="px-3 py-1.5 text-sm font-medium rounded-md bg-green-600 text-white hover:bg-green-700"
                                    >
                                        {tMain('contactReplyApprove')}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setPendingContactReplies(prev => prev.filter(x => x.replyId !== p.replyId));
                                            ws?.send(JSON.stringify({ type: 'contact_reply_decision', replyId: p.replyId, decision: 'reject' }));
                                        }}
                                        className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-200 text-gray-800 hover:bg-gray-300"
                                    >
                                        {tMain('contactReplyReject')}
                                    </button>
                                </div>
                            </div>
                        );
                    })}
                    {pendingContactReplies.length > 3 && (
                        <span className="text-sm text-amber-800">+{pendingContactReplies.length - 3} more</span>
                    )}
                </div>
            )}
            <div className="flex-1 flex min-h-0 overflow-hidden">
                <aside className="group flex flex-col min-h-0 h-full bg-white border-r border-gray-200 w-16 hover:w-72 transition-all duration-300 z-20 shadow-lg overflow-hidden">

                    {/* App Header / Logo */}
                    <div className="h-16 flex items-center px-4 gap-3 shrink-0">
                        <div className="w-[38px] h-[38px] rounded-lg overflow-hidden shrink-0 -ml-[5.5px]">
                            <img src="/logo.png" alt="VAF" className="w-full h-full object-cover" />
                        </div>
                        <span className="font-bold text-gray-800 whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity delay-100 duration-300 overflow-hidden">{tMain('veylloAgenticFramework')}</span>
                    </div>

                    {/* Session-Liste: äußere Box overflow-hidden = feste Höhe, innere Box scrollt */}
                    <div className="flex-1 min-h-0 relative overflow-hidden">
                        <div
                            ref={sidebarListRef}
                            className="absolute inset-0 overflow-y-auto overflow-x-hidden p-2 pt-0 space-y-1 scrollbar-hide"
                            style={{ WebkitOverflowScrolling: 'touch' }}
                        >
                            {/* New Chat Button */}
                            <div
                                onClick={() => ws?.send(JSON.stringify({ type: 'new_session' }))}
                                className="flex items-center gap-3 p-2 pl-3 rounded-lg cursor-pointer hover:bg-gray-100 text-gray-600 hover:text-gray-900 transition-colors"
                            >
                                <Plus size={16} className="shrink-0" />
                                <span className="text-sm font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-200">New Chat</span>
                            </div>

                            {sessions.map(s => (
                                <div key={s.id} data-session-id={s.id} onClick={() => handleSessionSwitch(s.id)}
                                    className={cn("flex items-center gap-3 p-2 pl-3 rounded-lg cursor-pointer group/item relative", currentSessionId === s.id ? 'bg-transparent' : 'hover:bg-gray-100')}>

                                    {/* Active Indicator (Dot) */}
                                    {currentSessionId === s.id && (
                                        <div className="absolute left-1 my-auto w-1 h-1 bg-black rounded-full" />
                                    )}

                                    {(s as Session).source === 'thinking' ? (
                                        <span title="Thinking mode">
                                            <Brain size={16} className={cn("shrink-0", currentSessionId === s.id ? "text-gray-900" : "text-gray-400")} />
                                        </span>
                                    ) : (
                                        <MessageSquare size={16} className={cn("shrink-0", currentSessionId === s.id ? "text-gray-900" : "text-gray-400")} />
                                    )}

                                    <div className="flex-1 flex justify-between items-center opacity-0 group-hover:opacity-100 transition-opacity min-w-0 pr-1">
                                        {editingId === s.id ? (
                                            <input
                                                autoFocus
                                                className="w-full text-xs border-b border-gray-500 focus:outline-none bg-transparent"
                                                value={editName}
                                                onChange={e => setEditName(e.target.value)}
                                                onKeyDown={e => {
                                                    if (e.key === 'Enter') submitRename();
                                                    if (e.key === 'Escape') setEditingId(null);
                                                }}
                                                onBlur={submitRename}
                                                onClick={e => e.stopPropagation()}
                                            />
                                        ) : (
                                            <span className={cn("truncate text-sm transition-colors", currentSessionId === s.id ? "font-medium text-gray-900" : "text-gray-600")}>
                                                {s.title.replace(".json", "")}
                                            </span>
                                        )}

                                        {/* Action Icons (Hover Only) */}
                                        <div className="flex items-center gap-1.5 opacity-0 group-hover/item:opacity-100 transition-opacity">
                                            {!editingId && (
                                                <>
                                                    <Edit2 size={12} className="text-gray-400 hover:text-gray-900" onClick={(e) => { e.stopPropagation(); startEditing(s); }} />
                                                    <Trash2 size={12} className="text-gray-400 hover:text-red-600" onClick={(e) => {
                                                        e.stopPropagation();
                                                        const isThinking = (s as { source?: string }).source === 'thinking';
                                                        ws?.send(JSON.stringify({ type: isThinking ? 'hide_session' : 'delete_session', id: s.id }));
                                                        if (currentSessionId === s.id) {
                                                            const remaining = sessions.filter(sess => sess.id !== s.id);
                                                            const empty = remaining.find(sess => (sess.messageCount || 0) === 0);
                                                            if (empty) {
                                                                handleSessionSwitch(empty.id);
                                                            } else if (remaining.length > 0) {
                                                                handleSessionSwitch(remaining[0].id);
                                                            } else {
                                                                setTimeout(() => {
                                                                    ws?.send(JSON.stringify({ type: 'new_session' }));
                                                                }, 100);
                                                            }
                                                        }
                                                    }} />
                                                </>
                                            )}
                                        </div>
                                    </div>
                                </div>
                            ))}
                            <div className="h-28 shrink-0" aria-hidden />
                        </div>
                        {/* Nebel: weißer Fade (statt grau), letzter sichtbarer Chat „verschwindet“ */}
                        <div
                            className="absolute bottom-0 left-0 right-0 h-28 pointer-events-none"
                            style={{
                                zIndex: 50,
                                background: 'linear-gradient(to top, #ffffff 0%, rgba(255,255,255,0.92) 35%, rgba(255,255,255,0.5) 65%, transparent 100%)',
                            }}
                        />
                    </div>

                    {/* Status Footer: Automation, Notifications, Settings (Connection-Indikator im Main-Bereich links) */}
                    <div className="shrink-0 p-3 mt-auto mb-2 flex flex-col gap-1 w-full overflow-hidden">

                        <div
                            onClick={() => {
                                setIsAutomationPopupOpen(true);
                                ws?.send(JSON.stringify({ type: 'get_automations' }));
                                // If calendar is connected, ensure "Daily calendar check" exists so it appears in the list
                                fetch(`${getApiBase()}/api/calendar/ensure-daily-check-automation`, { method: 'POST', credentials: 'include' })
                                    .then((r) => r.json())
                                    .then((data) => { if (data?.ok && ws?.readyState === WebSocket.OPEN) ws?.send(JSON.stringify({ type: 'get_automations' })); })
                                    .catch(() => { });
                            }}
                            className="flex items-center gap-3 p-2 rounded-xl cursor-pointer hover:bg-gray-100 text-gray-500 hover:text-gray-900 group/automation transition-all justify-start"
                            title="Automation"
                        >
                            <div className="w-6 flex justify-center shrink-0">
                                <Calendar size={20} />
                            </div>
                            <span className="max-w-0 group-hover:max-w-xs overflow-hidden opacity-0 group-hover:opacity-100 transition-all duration-300 font-medium whitespace-nowrap text-sm">Automation</span>
                        </div>

                        <div
                            onClick={() => setIsNotificationsOpen(true)}
                            className="flex items-center gap-3 p-2 rounded-xl cursor-pointer hover:bg-gray-100 text-gray-500 hover:text-gray-900 group/notifications transition-all justify-start"
                            title={tNav('notifications')}
                        >
                            <div className="w-6 flex justify-center shrink-0">
                                <Bell size={20} />
                            </div>
                            <span className="max-w-0 group-hover:max-w-xs overflow-hidden opacity-0 group-hover:opacity-100 transition-all duration-300 font-medium whitespace-nowrap text-sm">{tNav('notifications')}</span>
                        </div>

                        <div
                            onClick={() => {
                                setSettingsInitialTab(null);
                                setIsSettingsOpen(true);
                                // Fetch tools, workflows, and automations when opening settings
                                ws?.send(JSON.stringify({ type: 'get_tools' }));
                                ws?.send(JSON.stringify({ type: 'get_workflows' }));
                                ws?.send(JSON.stringify({ type: 'get_trusted_sources' }));
                                ws?.send(JSON.stringify({ type: 'get_automations' }));
                                // If calendar is connected, ensure "Daily calendar check" automation exists, then refresh list
                                fetch(`${getApiBase()}/api/calendar/ensure-daily-check-automation`, { method: 'POST', credentials: 'include' })
                                    .then((r) => r.json())
                                    .then((data) => { if (data?.ok && ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'get_automations' })); })
                                    .catch(() => { });
                            }}
                            className="flex items-center gap-3 p-2 rounded-xl cursor-pointer hover:bg-gray-100 text-gray-500 hover:text-gray-900 group/settings transition-all justify-start"
                            title={tNav('settings')}
                        >
                            <div className="w-6 flex justify-center shrink-0">
                                <Settings size={20} />
                            </div>
                            <span className="max-w-0 group-hover:max-w-xs overflow-hidden opacity-0 group-hover:opacity-100 transition-all duration-300 font-medium whitespace-nowrap text-sm">Settings</span>
                        </div>
                    </div>
                </aside>

                <div
                    className={cn(
                        "flex-1 flex overflow-hidden pr-4 transition-all duration-300 ease-out",
                        subAgentState.isOpen ? "gap-4" : "gap-0"
                    )}
                >
                    <div className="flex-1 flex flex-col relative bg-white overflow-hidden">
                        <div className="flex-1 overflow-y-auto p-6" ref={containerRef}>
                            <div className={cn(messagesAreaWidthClass, "mx-auto space-y-2 pb-32")}>
                                {/* Sub-Agent banner removed; reopen via tool cards or system log */}
                                {/* Empty state welcome is shown in the centered input block below */}
                                {(() => {
                                    const isThinkingSession = (sessions.find(s => s.id === currentSessionId) as Session | undefined)?.source === 'thinking';
                                    const filteredMessages = messages
                                        .filter(m => !m.content.includes('__CMD__'))
                                        .filter(m => {
                                            if (!isThinkingSession) return true;
                                            if ((m.role === 'system' || m.role === 'user') && isThinkingModePrompt(String(m.content ?? ''))) return false;
                                            return true;
                                        });
                                    return filteredMessages.map((msg, i) => {
                                        const trueIndex = messages.indexOf(msg);
                                        const prevMsg = i > 0 ? filteredMessages[i - 1] : null;
                                        const showDaySeparator = prevMsg !== null && !isSameDay(prevMsg.timestamp, msg.timestamp);
                                        return (
                                            <Fragment key={trueIndex}>
                                                {showDaySeparator && <DaySeparator endDate={prevMsg.timestamp} startDate={msg.timestamp} />}
                                                {/* Message content */}
                                                {(() => {
                                                    // Skip duplicate consecutive assistant messages (same visible answer) to avoid duplicated thinking + error text
                                                    if (msg.role === 'assistant' && prevMsg?.role === 'assistant') {
                                                        const prevAnswer = parseContent(prevMsg.content).answer.trim();
                                                        const currAnswer = parseContent(msg.content).answer.trim();
                                                        if (prevAnswer && currAnswer && prevAnswer === currAnswer) return null;
                                                    }
                                                    // Skip duplicate consecutive system messages (e.g. same "Context usage..." twice)
                                                    if (msg.role === 'system' && prevMsg?.role === 'system' && String(msg.content ?? '') === String(prevMsg.content ?? '')) return null;

                                                    // Render System Steps (Timeline Style)
                                                    if (msg.role === 'system') {
                                                        const isLast = i === filteredMessages.length - 1;
                                                        const isSubAgentMessage = msg.content.toLowerCase().includes('sub-agent');
                                                        const prevWasSystem = i > 0 && filteredMessages[i - 1].role === 'system';
                                                        return (
                                                            <div key={`system-${trueIndex}`} className={cn("flex justify-center", prevWasSystem ? "pt-0" : "pt-4")}>
                                                                <SystemStep
                                                                    message={msg.content}
                                                                    isLoading={loading && isLast}
                                                                    useBotIcon={loading && isLast}
                                                                    onClick={isSubAgentMessage ? () => openSubAgentWindow(true) : undefined}
                                                                />
                                                            </div>
                                                        );
                                                    }

                                                    // API empty error: show as system log (no bot bubble) for consistency
                                                    const apiEmptyErrorText = 'API returned empty responses repeatedly. Please try again.';
                                                    const isApiEmptyError = msg.role === 'assistant' && msg.content && (
                                                        msg.content.includes(apiEmptyErrorText) ||
                                                        msg.content.replace(/^\[Error\]\s*/i, '').trim() === apiEmptyErrorText
                                                    );
                                                    if (isApiEmptyError) {
                                                        const prevWasSystemApiErr = i > 0 && filteredMessages[i - 1].role === 'system';
                                                        return (
                                                            <div key={`system-${trueIndex}`} className={cn("flex justify-center", prevWasSystemApiErr ? "pt-0" : "pt-4")}>
                                                                <SystemStep message={`System: ${apiEmptyErrorText}`} />
                                                            </div>
                                                        );
                                                    }

                                                    // Render Tool Messages (same width as ThinkingDetails / assistant bubble content)
                                                    if (msg.role === 'tool') {
                                                        const toolLower = (msg.toolName || '').toLowerCase();
                                                        const isSubAgentTool = /(?:^|[^a-z])(librarian|research|document|coding)_agent(?:$|[^a-z])/.test(toolLower);
                                                        const prevWasSystem = i > 0 && filteredMessages[i - 1].role === 'system';
                                                        return (
                                                            <div className={cn("flex justify-center", prevWasSystem ? "pt-0" : "pt-4")}>
                                                                <div className="w-full max-w-[85%] flex gap-4">
                                                                    <div className="w-9 shrink-0" aria-hidden />
                                                                    <div className="flex-1 min-w-0">
                                                                        <div className="max-w-[95%]">
                                                                            <ToolMessage
                                                                                key={`tool-${trueIndex}`}
                                                                                id={msg.toolId || `tool-${trueIndex}`}
                                                                                name={msg.toolName || 'Unknown Tool'}
                                                                                status={msg.toolStatus || 'completed'}
                                                                                result={msg.content}
                                                                                args={msg.toolArgs}
                                                                                startTime={msg.toolStartTime}
                                                                                endTime={msg.toolEndTime}
                                                                                onToggleScroll={preserveChatScroll}
                                                                                onToggle={isSubAgentTool ? (nextExpanded) => {
                                                                                    if (nextExpanded) {
                                                                                        openSubAgentWindow(true);
                                                                                    } else {
                                                                                        closeSubAgentWindow(true);
                                                                                    }
                                                                                } : undefined}
                                                                            />
                                                                        </div>
                                                                    </div>
                                                                </div>
                                                            </div>
                                                        );
                                                    }

                                                    // Render Workflow Messages
                                                    if (msg.role === 'workflow') {
                                                        return (
                                                            <div key={`workflow-${trueIndex}`} className="flex justify-center gap-4 pt-4">
                                                                <div className="flex gap-4 max-w-[85%] w-full items-start">
                                                                    <div className="w-9 h-9 rounded-xl bg-gray-900 flex items-center justify-center text-white shadow-sm shrink-0"><Bot size={18} /></div>
                                                                    <WorkflowChatElement
                                                                        workflowId={msg.workflowId || ""}
                                                                        name={msg.workflowName || "Workflow"}
                                                                        initialSteps={msg.initialSteps}
                                                                    />
                                                                </div>
                                                            </div>
                                                        );
                                                    }

                                                    const { thought, answer, isThinkingComplete } = parseContent(msg.content);
                                                    const isBot = msg.role === 'assistant';
                                                    const isLastMessage = i === filteredMessages.length - 1;
                                                    // Simple: thinking is done when the </think> tag is found (isThinkingComplete)
                                                    // For non-last messages, always treat as complete
                                                    const thinkingDone = !isLastMessage || isThinkingComplete;
                                                    // For user messages: don't show attachment content in bubble (strip --- FILE: ... --- blocks); keep chips from msg.files or parsed from content after reload
                                                    const attachmentStripped = !isBot ? stripAttachmentBlocks(msg.content) : null;
                                                    const displayAnswer = !isBot && attachmentStripped ? attachmentStripped.text : answer;
                                                    const displayFiles = !isBot && (msg.files?.length ? msg.files : (attachmentStripped?.fileNames.length ? attachmentStripped.fileNames.map(name => ({ name, mimeType: '' })) : undefined));

                                                    // Filter out tool_calls JSON from bot answers
                                                    const cleanAnswer = isBot ? stripToolCallsJSON(answer) : answer;
                                                    // Add top margin if following a system step
                                                    const prevWasSystem = i > 0 && filteredMessages[i - 1].role === 'system';
                                                    // Only show the speech bubble when there is visible content (avoid empty bubbles)
                                                    const hasBubbleContent = !isBot
                                                        ? !!(displayAnswer || (displayFiles && displayFiles.length > 0))
                                                        : !!((cleanAnswer && cleanAnswer.trim() !== '') || parseWorkflowAsync(answer));
                                                    // When there is nothing to show (no bubble, no thinking), don't render the row at all (no avatar, no timestamp, no empty space)
                                                    const hasVisibleContent = isBot
                                                        ? (hasBubbleContent || !!(thought && thought.trim() !== ''))
                                                        : hasBubbleContent;
                                                    if (!hasVisibleContent) return null;

                                                    const bubbleContent = (
                                                        <>
                                                            {isBot && thought && <ThinkingDetails thought={thought} isComplete={thinkingDone} />}

                                                            {/* Show answer bubble only when there is content (never show empty speech bubble) */}
                                                            {hasBubbleContent && (
                                                                <div className="flex flex-col gap-3 w-full">
                                                                    {isBot && parseWorkflowAsync(answer) ? (() => {
                                                                        const wf = parseWorkflowAsync(answer)!;
                                                                        return (
                                                                            <>
                                                                                <WorkflowChatElement
                                                                                    workflowId={wf.workflowId}
                                                                                    name={wf.name}
                                                                                    initialSteps={4}
                                                                                />
                                                                                {wf.rest ? (
                                                                                    <div className="relative group flex items-end">
                                                                                        <div className="px-5 py-3 rounded-2xl shadow-sm text-[15px] leading-relaxed bg-white text-gray-800 rounded-tl-none border border-transparent">
                                                                                            <div className="chat-markdown"><ChatMarkdown>{wf.rest}</ChatMarkdown></div>
                                                                                        </div>
                                                                                        <button
                                                                                            onClick={(e) => {
                                                                                                e.stopPropagation();
                                                                                                if (playingMessageId === trueIndex) handleStopSpeech();
                                                                                                else handleSpeak(trueIndex, wf.rest);
                                                                                            }}
                                                                                            className="ml-2 mb-1 p-1.5 rounded-full hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-all opacity-40 hover:opacity-100 data-[active=true]:opacity-100 shrink-0"
                                                                                            data-active={playingMessageId === trueIndex || loadingMessageId === trueIndex}
                                                                                            title={playingMessageId === trueIndex ? "Stop Speaking" : "Read Aloud"}
                                                                                        >
                                                                                            {loadingMessageId === trueIndex ? <Loader2 size={14} className="animate-spin" /> : playingMessageId === trueIndex ? (
                                                                                                <div className="relative"><Volume2 size={14} className="text-gray-600" /><span className="absolute -inset-1 rounded-full bg-gray-400/20 animate-ping" /></div>
                                                                                            ) : <Volume2 size={14} />}
                                                                                        </button>
                                                                                    </div>
                                                                                ) : null}
                                                                            </>
                                                                        );
                                                                    })() : (
                                                                        <div className="relative group flex items-end">
                                                                            <div className={cn("px-5 py-3 rounded-2xl shadow-sm text-[15px] leading-relaxed",
                                                                                isBot ? "bg-white text-gray-800 rounded-tl-none border border-transparent" : "bg-gray-800 text-white rounded-tr-none")}>
                                                                                <div className="chat-markdown"><ChatMarkdown dark={!isBot}>{isBot ? cleanAnswer : displayAnswer}</ChatMarkdown></div>
                                                                            </div>
                                                                            {isBot && (
                                                                                <button
                                                                                    onClick={(e) => {
                                                                                        e.stopPropagation();
                                                                                        if (playingMessageId === trueIndex) handleStopSpeech();
                                                                                        else handleSpeak(trueIndex, cleanAnswer);
                                                                                    }}
                                                                                    className="ml-2 mb-1 p-1.5 rounded-full hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-all opacity-40 hover:opacity-100 data-[active=true]:opacity-100 shrink-0"
                                                                                    data-active={playingMessageId === trueIndex || loadingMessageId === trueIndex}
                                                                                    title={playingMessageId === trueIndex ? "Stop Speaking" : "Read Aloud"}
                                                                                >
                                                                                    {loadingMessageId === trueIndex ? (
                                                                                        <Loader2 size={14} className="animate-spin" />
                                                                                    ) : playingMessageId === trueIndex ? (
                                                                                        <div className="relative">
                                                                                            <Volume2 size={14} className="text-gray-600" />
                                                                                            <span className="absolute -inset-1 rounded-full bg-gray-400/20 animate-ping" />
                                                                                        </div>
                                                                                    ) : (
                                                                                        <Volume2 size={14} />
                                                                                    )}
                                                                                </button>
                                                                            )}
                                                                        </div>
                                                                    )}
                                                                    {/* User message: show attachment chips below the bubble (from msg.files or parsed from content after reload) */}
                                                                    {!isBot && displayFiles && displayFiles.length > 0 && (
                                                                        <div className="flex gap-2 flex-wrap mt-1 justify-end">
                                                                            {displayFiles.map((f, idx) => (
                                                                                <div key={idx} className="flex items-center gap-1.5 bg-gray-100 rounded-lg px-2.5 py-1 text-xs text-gray-600">
                                                                                    <Paperclip size={12} className="shrink-0 text-gray-400" />
                                                                                    <span className="truncate max-w-[140px]">{f.name}</span>
                                                                                </div>
                                                                            ))}
                                                                        </div>
                                                                    )}
                                                                    {/* User message: indicator that Document Viewer had attachments when this was sent (only when viewer is closed) */}
                                                                    {!isBot && !documentViewerState.isOpen && msg.sidebarDocs && msg.sidebarDocs.length > 0 && (
                                                                        <div className="flex gap-1.5 flex-wrap mt-1 justify-end items-center">
                                                                            <span className="text-[10px] text-gray-400">{tMain('attachmentsShort')}:</span>
                                                                            {msg.sidebarDocs.slice(0, 3).map((name, idx) => (
                                                                                <span
                                                                                    key={idx}
                                                                                    className="inline-flex items-center gap-1 bg-gray-100/80 rounded px-2 py-0.5 text-[10px] text-gray-500 truncate max-w-[120px]"
                                                                                    title={name}
                                                                                >
                                                                                    {name.length > 10 ? `${name.slice(0, 10)}…` : name}
                                                                                </span>
                                                                            ))}
                                                                            {msg.sidebarDocs.length > 3 && (
                                                                                <span className="text-[10px] text-gray-400">+{msg.sidebarDocs.length - 3}</span>
                                                                            )}
                                                                        </div>
                                                                    )}
                                                                </div>
                                                            )}

                                                            {/* Unauffällige Uhrzeit unter der Blase (Messenger-Stil) */}
                                                            {(msg.role === 'user' || msg.role === 'assistant') && (
                                                                <div className={cn("w-full mt-0.5", isBot ? "text-left" : "text-right")}>
                                                                    <span className="text-[10px] text-gray-400" title={new Date(msg.timestamp).toLocaleString('en-US', userTimeFormat ? { hour12: userTimeFormat === '12h' } : undefined)}>{formatMessageTime(msg.timestamp, userTimeFormat)}</span>
                                                                </div>
                                                            )}
                                                            {/* Show status steps below the active message if streaming */}
                                                            {loading && isBot && i === filteredMessages.length - 1 && statusMessage && /[a-zA-Z0-9]/.test(statusMessage) && (
                                                                <span className="text-[10px] text-gray-400 mt-1 ml-1 animate-in fade-in">{statusMessage}</span>
                                                            )}
                                                        </>
                                                    );

                                                    return (
                                                        <div key={`bubble-${trueIndex}`} className={cn("flex gap-4 pt-4", isBot ? "justify-center" : "justify-end", prevWasSystem ? "pt-2" : "pt-4")}>
                                                            {isBot ? (
                                                                <div className="w-full max-w-[85%] flex gap-4">
                                                                    <div className="w-9 h-9 rounded-xl bg-gray-900 flex items-center justify-center text-white shadow-sm shrink-0"><Bot size={18} /></div>
                                                                    <div className="flex flex-col flex-1 min-w-0 shrink-0 items-start w-full">
                                                                        {bubbleContent}
                                                                    </div>
                                                                </div>
                                                            ) : (
                                                                <>
                                                                    <div className={cn("flex flex-col", "max-w-[72%] items-end shrink-0")}>
                                                                        {bubbleContent}
                                                                    </div>
                                                                    <div className="w-9 h-9 rounded-xl bg-white border border-gray-200 flex items-center justify-center text-gray-500 shadow-sm shrink-0"><User size={18} /></div>
                                                                </>
                                                            )}
                                                        </div>
                                                    );
                                                })()}
                                            </Fragment>
                                        );
                                    });
                                })()}

                                {loading && messages.length > 0 && !(
                                    // Hide loading bubble when a tool is actively running (tool card shows spinner)
                                    (messages[messages.length - 1].role === 'tool' &&
                                    messages[messages.length - 1].toolStatus === 'running') ||
                                    // Or when last message is a system step (RAG, Router, Final tools, etc.) – no redundant avatar + dots row
                                    messages[messages.length - 1].role === 'system'
                                ) && (
                                    <div className="flex gap-4 justify-center pt-4">
                                        <div className="w-full max-w-[85%] flex gap-4">
                                            <div className="w-9 h-9 rounded-xl bg-gray-200 flex items-center justify-center text-gray-400 shrink-0"><Bot size={18} /></div>
                                            <div className="flex flex-col gap-1">
                                                {activeToolName ? (
                                                    // Tool just finished / between tools: show tool name with spinner
                                                    <div className="bg-gray-100 px-4 py-2 rounded-2xl rounded-tl-none flex items-center gap-2 text-xs text-gray-500">
                                                        <Loader2 size={12} className="animate-spin shrink-0" />
                                                        <span className="capitalize">{activeToolName}</span>
                                                    </div>
                                                ) : (
                                                    // Thinking / between calls: bouncing dots
                                                    <div className="bg-gray-100 px-4 py-2 rounded-2xl rounded-tl-none w-fit flex gap-1 animate-pulse">
                                                        <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce"></span>
                                                        <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce delay-75"></span>
                                                        <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce delay-150"></span>
                                                    </div>
                                                )}
                                                {statusMessage && /[a-zA-Z0-9]/.test(statusMessage) && <span className="text-[10px] text-gray-400 ml-2">{statusMessage}</span>}
                                            </div>
                                        </div>
                                    </div>
                                )}

                                {/* Active Tools Panel Removed (Now Inline) */}

                                <div ref={scrollRef} />
                            </div>
                        </div>

                        <div
                            className={cn(
                                "absolute left-0 right-0 w-full z-40 transition-all duration-500 ease-out",
                                messages.length === 0
                                    ? "top-1/2 -translate-y-1/2 bottom-auto"
                                    : "top-auto bottom-0 translate-y-0"
                            )}
                        >
                            <div className="bg-gradient-to-t from-white via-white to-transparent pt-10 pb-8 px-6">
                                {messages.length === 0 && (
                                    <div className={cn(chatWidthClass, "mx-auto mb-4 text-center")}>
                                        <Bot size={40} className="text-gray-300 mx-auto mb-3" />
                                        <h2 className="text-xl font-bold text-gray-800">{tMain('howCanIHelp')}</h2>
                                        <p className="text-gray-400 mt-1 text-sm">{tMain('startConversationOrWorkflow')}</p>
                                    </div>
                                )}
                                {/* Suggestions Popup - Fixed centered, with arrow key navigation */}
                                {suggestionList.length > 0 && (
                                    <div
                                        className="fixed left-1/2 -translate-x-1/2 w-80 bg-white rounded-xl shadow-2xl border border-gray-200 overflow-hidden z-[9999]"
                                        style={{ bottom: '120px' }}
                                    >
                                        <div className="px-3 py-2 bg-gray-50 border-b border-gray-100 text-[10px] font-bold text-gray-400 uppercase tracking-wider flex justify-between">
                                            <span>{suggestionType === 'tool' ? tMain('tools') : tMain('workflows')}</span>
                                            <span className="text-gray-300">{tMain('navigateSelect')}</span>
                                        </div>
                                        <div className="max-h-64 overflow-y-auto" ref={suggestionListRef}>
                                            {suggestionList.map((item, idx) => (
                                                <div
                                                    key={idx}
                                                    className={cn(
                                                        "px-4 py-3 cursor-pointer flex items-center gap-3 transition-colors border-b border-gray-50 last:border-0",
                                                        idx === selectedSuggestionIndex
                                                            ? "bg-gray-900 text-white"
                                                            : "hover:bg-gray-100 text-gray-700"
                                                    )}
                                                    onClick={() => handleSuggestionClick(item)}
                                                    onMouseEnter={() => setSelectedSuggestionIndex(idx)}
                                                >
                                                    <div className={cn(
                                                        "w-8 h-8 rounded-lg flex items-center justify-center shrink-0 transition-all duration-200",
                                                        suggestionType === 'tool' ? "bg-orange-100 text-orange-600" : "bg-gray-100 text-gray-600",
                                                        idx === selectedSuggestionIndex && (
                                                            suggestionType === 'tool'
                                                                ? "shadow-[0_0_12px_rgba(249,115,22,0.5)] scale-105"
                                                                : "shadow-[0_0_12px_rgba(59,130,246,0.5)] scale-105"
                                                        )
                                                    )}>
                                                        {suggestionType === 'tool' ? <Wrench size={16} /> : <Workflow size={16} />}
                                                    </div>
                                                    <div className="flex flex-col min-w-0">
                                                        <span className="text-sm font-medium truncate">{item.name || item.id}</span>
                                                        {item.description && <span className={cn("text-xs truncate", idx === selectedSuggestionIndex ? "text-gray-400" : "text-gray-400")}>{item.description}</span>}
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {/* Memory Learning Banner */}
                                {memoryLearning && (
                                    <div className={cn(chatWidthClass, "mx-auto mb-2")}>
                                        <div className={cn(
                                            "flex items-center gap-2 px-4 py-2 rounded-xl border shadow-sm transition-all animate-in fade-in slide-in-from-bottom-2",
                                            memoryLearning.active
                                                ? "bg-violet-50 border-violet-300 text-violet-700"
                                                : "bg-green-50 border-green-300 text-green-700"
                                        )}>
                                            {memoryLearning.active ? (
                                                <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
                                            ) : (
                                                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                                                </svg>
                                            )}
                                            <span className="text-sm font-medium">{memoryLearning.message}</span>
                                        </div>
                                    </div>
                                )}

                                {/* Model download progress (visible when downloading, e.g. after closing Settings) */}
                                {downloadModelStatus?.status === 'downloading' && (
                                    <div className={cn(chatWidthClass, "mx-auto mb-2")}>
                                        <div className="flex items-center gap-3 px-4 py-2.5 rounded-xl border border-gray-200 bg-white shadow-sm">
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center justify-between text-xs text-gray-600 mb-1">
                                                    <span className="truncate">{downloadModelStatus.repo_id ? `Downloading ${downloadModelStatus.repo_id}` : 'Downloading model…'}</span>
                                                    {downloadModelStatus.speed_str && <span className="shrink-0 ml-2">{downloadModelStatus.speed_str}</span>}
                                                </div>
                                                <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                                                    <div
                                                        className="h-full bg-gray-900 rounded-full transition-[width] duration-300"
                                                        style={{ width: `${downloadModelStatus.progress_pct ?? 0}%` }}
                                                    />
                                                </div>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={cancelModelDownload}
                                                className="text-xs font-medium text-red-600 hover:text-red-700 shrink-0"
                                            >
                                                Cancel
                                            </button>
                                        </div>
                                    </div>
                                )}

                                {/* Token Stats (Clickable) + RAG Badge */}
                                <div className={cn(chatWidthClass, "mx-auto mb-1 flex justify-end items-baseline gap-2 min-h-[16px]")}>
                                    {ragResults?.sources?.length > 0 && (
                                        <div className="group relative inline-flex items-center pt-3">
                                            <span
                                                className="text-[10px] font-mono text-gray-400 opacity-80 px-2 py-0.5 rounded cursor-help border border-gray-200 bg-transparent leading-none group-hover:text-violet-600 group-hover:opacity-100 group-hover:bg-violet-50 group-hover:border-violet-200 transition-all"
                                                title="RAG snippets passed to model this turn"
                                            >
                                                {tMain('ragHits', { count: ragResults.sources.length })}
                                            </span>
                                            <div className="hidden group-hover:block absolute right-0 bottom-full mb-0 pb-2 z-[80] w-80 max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-xl p-3 text-left">
                                                <div className="text-xs font-semibold text-gray-700 mb-2">{tMain('ragSnippetsThisTurn')}</div>
                                                <div className="space-y-2">
                                                    {ragResults.sources.slice(0, 10).map((s: { text?: string; full_text?: string; score?: number }, i: number) => (
                                                        <div key={i} className="text-xs text-gray-600 bg-gray-50 p-2 rounded border border-gray-100">
                                                            {s.score !== undefined && <span className="text-violet-600 font-mono">{(s.score * 100).toFixed(0)}%</span>}
                                                            <div className="mt-1 line-clamp-3">{(s.full_text ?? s.text ?? '').slice(0, 300)}{((s.full_text ?? s.text ?? '').length > 300 ? '…' : '')}</div>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        </div>
                                    )}
                                    {contextStats && (
                                        <span
                                            className="text-[10px] sm:text-xs font-mono text-gray-400 opacity-80 select-none cursor-pointer hover:text-black hover:opacity-100 transition-all leading-none"
                                            onClick={() => setIsContextModalOpen(true)}
                                        >
                                            {tMain('tokens')}:
                                            <span className="mx-1 tracking-tighter">
                                                {"●".repeat(Math.min(10, Math.max(0, Math.round(contextStats.percent / 10))))}
                                                {"○".repeat(Math.max(0, 10 - Math.min(10, Math.max(0, Math.round(contextStats.percent / 10)))))}
                                            </span>
                                            {Math.round(contextStats.percent)}% ({contextStats.tokens.toLocaleString()}/{contextStats.max_tokens.toLocaleString()})
                                        </span>
                                    )}
                                </div>

                                {/* Stop button left of message box — show when chat is loading, a workflow is running, or a sub-agent is active */}
                                <div className={cn(chatWidthClass, "mx-auto flex items-center gap-2")}>
                                    <div className="w-9 shrink-0 flex items-center justify-center">
                                        {(isGenerating || isWorkflowRunning || isSubAgentRunning) && (
                                            <button
                                                type="button"
                                                onClick={stopGeneration}
                                                title="Stop"
                                                className="p-2 rounded-full bg-red-500 text-white text-sm font-medium hover:bg-red-600 transition-all shadow-md flex items-center justify-center animate-in fade-in slide-in-from-bottom-2"
                                            >
                                                <Square size={12} fill="currentColor" />
                                            </button>
                                        )}
                                    </div>
                                    <form
                                        onSubmit={sendMessage}
                                        className="flex-1 min-w-0 flex items-end bg-white rounded-2xl border border-gray-200 shadow-xl focus-within:border-gray-400 transition-all overflow-hidden"
                                    >
                                        <input
                                            type="file"
                                            ref={fileInputRef}
                                            onChange={handleFileSelect}
                                            className="hidden"
                                            multiple
                                            accept={ACCEPT_ATTACHMENTS}
                                        />
                                        <button
                                            type="button"
                                            onClick={() => fileInputRef.current?.click()}
                                            className={cn(
                                                "p-4 transition-colors",
                                                documentViewerState.isOpen ? "text-blue-600" : "text-gray-400 hover:text-gray-900"
                                            )}
                                            title={tMain('attachmentsDocumentViewer')}
                                        >
                                            <Paperclip size={20} />
                                        </button>
                                        <div className="flex-1 relative flex flex-col min-w-0">
                                            {insertedSelections.length > 0 && (
                                                <div className="flex flex-wrap items-center gap-1.5 px-2 pt-2 pb-1 border-b border-gray-100">
                                                    {insertedSelections.map((s, i) => (
                                                        <button
                                                            key={i}
                                                            type="button"
                                                            onClick={() => setInsertedSelections(prev => prev.filter((_, idx) => idx !== i))}
                                                            className={cn(
                                                                'max-w-[200px] truncate text-xs rounded px-2 py-0.5 transition-colors',
                                                                CHIP_BG_CLASSES[i % CHIP_BG_CLASSES.length],
                                                                'hover:bg-red-500 hover:text-white'
                                                            )}
                                                            title={`${s.text}\nKlicken zum Entfernen`}
                                                        >
                                                            &quot;{s.text.slice(0, 30)}{s.text.length > 30 ? '…' : ''}&quot;
                                                        </button>
                                                    ))}
                                                </div>
                                            )}
                                            <div className="relative flex items-end flex-1 min-h-0">
                                                <div className="absolute inset-0 py-4 px-1 pointer-events-none text-sm text-gray-400 whitespace-pre overflow-hidden">
                                                    <span className="text-transparent">{input}</span>
                                                    {suggestion}
                                                </div>
                                                <textarea
                                                    ref={inputRef}
                                                    rows={1}
                                                    value={input}
                                                    onChange={handleInputChange}
                                                    onKeyDown={handleKeyDown}
                                                    placeholder={input ? "" : "Ask anything..."}
                                                    className="w-full min-h-[2.5rem] max-h-[12.5rem] py-4 px-1 bg-transparent border-none focus:ring-0 focus:outline-none text-sm relative z-10 resize-none overflow-y-auto"
                                                    disabled={isGenerating}
                                                />
                                            </div>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={isRecording ? stopRecording : startRecording}
                                            disabled={isProcessingAudio || isGenerating}
                                            className={cn(
                                                "m-2 p-2 rounded-xl transition-all shadow-sm",
                                                isRecording ? "bg-red-500 text-white" :
                                                    isProcessingAudio ? "bg-gray-300 text-gray-500" :
                                                        "bg-gray-900 text-white hover:bg-black disabled:bg-gray-200"
                                            )}
                                            style={{
                                                boxShadow: isRecording ? `0 0 0 ${Math.min(volume / 5, 15)}px rgba(239, 68, 68, 0.4)` : 'none',
                                                transition: 'box-shadow 0.05s ease-out'
                                            }}
                                            title={isRecording ? tMain('stopRecording') : isProcessingAudio ? tMain('processing') : tMain('voiceInput')}
                                        >
                                            {isProcessingAudio ? (
                                                <Loader2 size={18} className="mx-2 animate-spin" />
                                            ) : isRecording ? (
                                                <MicOff size={18} className="mx-2" />
                                            ) : (
                                                <Mic size={18} className="mx-2" />
                                            )}
                                        </button>
                                    </form>
                                </div>
                            </div>
                        </div>
                    </div>
                    {/* Right Panel: DocumentViewer, DocumentEditor, or SubAgentWindow (dock mode) */}
                    {showSubAgentPanel && (
                        <div
                            className={cn(
                                "hidden lg:flex h-full items-stretch overflow-hidden transition-all duration-300 ease-out",
                                (subAgentState.isOpen || documentEditorState.isOpen || documentViewerState.isOpen)
                                    ? "w-[58%] min-w-[704px] max-w-[1000px] opacity-100"
                                    : "w-0 min-w-0 max-w-0 opacity-0 pointer-events-none"
                            )}
                            aria-hidden={!subAgentState.isOpen && !documentEditorState.isOpen && !documentViewerState.isOpen}
                        >
                            {documentViewerState.isOpen ? (
                                <DocumentViewer
                                    isOpen={documentViewerState.isOpen}
                                    onClose={handleDocumentViewerClose}
                                    title={tMain('documentViewerTitle')}
                                    mode="dock"
                                    documents={documentViewerState.documents}
                                    onAddFiles={handleDocumentViewerAddFiles}
                                    onRemoveDocument={handleDocumentViewerRemove}
                                    onInsertSelection={(text, range) => setInsertedSelections(prev => [...prev, { text, ...range }])}
                                    insertedSelectionsCount={insertedSelections.length}
                                    insertedSelections={insertedSelections}
                                />
                            ) : documentEditorState.isOpen ? (
                                <DocumentEditor
                                    isOpen={documentEditorState.isOpen}
                                    onClose={() => setDocumentEditorState(prev => ({ ...prev, isOpen: false }))}
                                    filePath={documentEditorState.filePath}
                                    title={documentEditorState.title}
                                    initialContent={documentEditorState.content ?? ''}
                                    onContentChange={(content) => setDocumentEditorState(prev => ({ ...prev, content }))}
                                    onInsertSelection={(text, range) => setInsertedSelections(prev => [...prev, { text, ...range }])}
                                    insertedSelectionsCount={insertedSelections.length}
                                    insertedSelections={insertedSelections}
                                    mode="dock"
                                />
                            ) : (
                                <SubAgentWindow
                                    isOpen={subAgentState.isOpen}
                                    mode="dock"
                                    onClose={() => {
                                        subAgentManualOpenRef.current = false;
                                        setSubAgentState(prev => ({ ...prev, isOpen: false }));
                                    }}
                                    canClose={true}
                                    agentName={subAgentState.agentName}
                                    status={subAgentState.status}
                                    presence={subAgentState.presence}
                                    currentFile={subAgentState.currentFile}
                                    codeContent={subAgentState.codeContent}
                                    artifactFile={subAgentState.artifactFile || subAgentState.currentFile}
                                    artifactCode={subAgentState.artifactCode || subAgentState.codeContent}
                                    artifactStatus={subAgentState.artifactStatus}
                                    onArtifactChange={handleArtifactChange}
                                    consoleLines={subAgentState.consoleLines}
                                    steps={subAgentState.steps}
                                />
                            )}
                        </div>
                    )}
                </div>
            </div>
            {/* Active Tools Panel Moved Inline */}
            <VAFWorkflowRuntime />

            {/* Context Window Modal - Clean & Professional */}
            {isContextModalOpen && contextStats && (
                <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-[100] flex items-center justify-center p-4 animate-in fade-in duration-200">
                    <div className="bg-white w-full max-w-4xl rounded-2xl shadow-2xl overflow-hidden flex flex-col animate-in zoom-in-95 duration-200 border border-gray-200">
                        {/* Header */}
                        <div className="shrink-0 px-8 py-6 border-b border-gray-100 bg-gray-50/80">
                            <div className="flex justify-between items-start">
                                <div>
                                    <div className="flex items-center gap-4">
                                        <h3 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
                                            <Activity className="text-gray-800" />
                                            Context Window
                                        </h3>
                                        <div className="flex items-center gap-2 px-3 py-1 bg-white rounded-lg border border-gray-200 shadow-sm self-center">
                                            <span className="text-xs font-mono font-bold text-gray-700">{contextStats.tokens.toLocaleString()} / {contextStats.max_tokens.toLocaleString()} {tMain('tokens')}</span>
                                            <span className="text-[10px] font-bold text-violet-700 bg-violet-100 px-1.5 py-0.5 rounded">{contextStats.percent}%</span>
                                            <span className="w-px h-3 bg-gray-200 mx-1"></span>
                                            <span className="text-xs font-medium text-gray-500">{contextStats.message_count} messages</span>
                                        </div>
                                        {/* Memory Learning Badge */}
                                        {contextStats.user_turn_count !== undefined && (
                                            <div className="flex items-center gap-2 px-3 py-1 bg-violet-50 rounded-lg border border-violet-200 shadow-sm self-center" title="Memory Learning: After every 15 messages, VAF analyzes the conversation and stores important facts to long-term memory">
                                                <span className="text-xs font-medium text-violet-700">{tMain('memoryLearning')}:</span>
                                                <div className="flex items-center gap-1">
                                                    <div className="h-1.5 w-16 bg-violet-200 rounded-full overflow-hidden">
                                                        <div
                                                            className="h-full bg-violet-500 transition-all duration-300"
                                                            style={{ width: `${((contextStats.user_turn_count % (contextStats.compaction_interval || 15)) / (contextStats.compaction_interval || 15)) * 100}%` }}
                                                        />
                                                    </div>
                                                    <span className="text-xs font-mono font-bold text-violet-600">
                                                        {contextStats.user_turn_count % (contextStats.compaction_interval || 15)}/{contextStats.compaction_interval || 15}
                                                    </span>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                    <p className="text-sm text-gray-500 mt-1">
                                        How your context window is being used
                                    </p>
                                </div>
                                <button
                                    onClick={() => setIsContextModalOpen(false)}
                                    className="p-2 hover:bg-gray-200 rounded-full transition-colors text-gray-400 hover:text-gray-700"
                                >
                                    <span className="sr-only">Close</span>
                                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                                </button>
                            </div>
                        </div>

                        {/* Diagram area with legend */}
                        <div className="flex px-8 py-6 flex-1 min-h-0 gap-6">
                            {/* Legend - Left side with token count and percentage */}
                            <div className="shrink-0 w-52 flex flex-col justify-center gap-3 text-sm">
                                <div className="flex items-start gap-2">
                                    <div className="w-3 h-3 rounded-sm bg-gray-800 mt-1 shrink-0"></div>
                                    <div className="min-w-0">
                                        <div className="font-semibold text-gray-700">System Prompt</div>
                                        <div className="text-xs text-gray-500">Instructions, persona, rules</div>
                                        {contextBreakdown && (
                                        <div className="text-xs font-mono text-gray-600 mt-0.5">{Math.round(contextBreakdown.systemEst).toLocaleString()} {tMain('tokens')} · {contextBreakdown.pctOfCapSystem.toFixed(1)}%</div>
                                        )}
                                    </div>
                                </div>
                                <div className="flex items-start gap-2">
                                    <div className="w-3 h-3 rounded-sm bg-violet-400 mt-1 shrink-0" title="Lilac"></div>
                                    <div className="min-w-0">
                                        <div className="font-semibold text-gray-700">Tool Schemas</div>
                                        <div className="text-xs text-gray-500">Available functions & params</div>
                                        {contextBreakdown && (
                                        <div className="text-xs font-mono text-gray-600 mt-0.5">{Math.round(contextBreakdown.toolsEst).toLocaleString()} {tMain('tokens')} · {contextBreakdown.pctOfCapTools.toFixed(1)}%</div>
                                        )}
                                    </div>
                                </div>
                                <div className="flex items-start gap-2">
                                    <div className="w-3 h-3 rounded-sm bg-violet-600 mt-1 shrink-0" title="Violet"></div>
                                    <div className="min-w-0">
                                        <div className="font-semibold text-gray-700">{tMain('conversation')}</div>
                                        <div className="text-xs text-gray-500">Chat history & tool results</div>
                                        {contextBreakdown && (
                                        <div className="text-xs font-mono text-gray-600 mt-0.5">{Math.round(contextBreakdown.historyEst).toLocaleString()} {tMain('tokens')} · {contextBreakdown.pctOfCapHistory.toFixed(1)}%</div>
                                        )}
                                    </div>
                                </div>
                                <div className="border-t border-gray-200 my-2"></div>
                                <div className="flex items-start gap-2">
                                    <div className="w-3 h-3 rounded-sm bg-gradient-to-b from-gray-700 to-violet-600 mt-1 shrink-0"></div>
                                    <div>
                                        <div className="font-semibold text-gray-700">Used</div>
                                        <div className="text-xs text-gray-500">{tMain('totalContextConsumed')}</div>
                                    </div>
                                </div>
                                <div className="flex items-start gap-2">
                                    <div className="w-3 h-3 rounded-sm bg-gray-200 mt-1 shrink-0"></div>
                                    <div>
                                        <div className="font-semibold text-gray-400">Free</div>
                                        <div className="text-xs text-gray-400">{tMain('remainingCapacity')}</div>
                                    </div>
                                </div>
                                {/* Memory Learning Info */}
                                {contextStats.user_turn_count !== undefined && (
                                    <>
                                        <div className="border-t border-violet-200 my-2"></div>
                                        <div className="flex items-start gap-2">
                                            <div className="w-3 h-3 rounded-sm bg-violet-600 mt-1 shrink-0"></div>
                                            <div>
                                                <div className="font-semibold text-violet-700">{tMain('memoryLearning')}</div>
                                                <div className="text-xs text-gray-500">
                                                    Every {contextStats.compaction_interval || 15} messages, VAF analyzes the chat and stores important facts to long-term memory.
                                                </div>
                                                <div className="text-xs text-violet-600 font-mono mt-1">
                                                    Next save in {(contextStats.compaction_interval || 15) - (contextStats.user_turn_count % (contextStats.compaction_interval || 15))} messages
                                                </div>
                                            </div>
                                        </div>
                                    </>
                                )}
                            </div>

                            {/* Diagram - Right side (uses contextBreakdown) */}
                            <div className="flex-1 flex flex-col min-h-0">
                                {contextBreakdown && (() => {
                                    const { totalCap, used, systemEst, toolsEst, historyEst } = contextBreakdown;
                                    const freeEst = totalCap - used;

                                    // 2. Layout Configuration
                                    const w = 800;
                                    const h = 500;
                                    const pad = 20;
                                    const nodeW = 20;
                                    const leftX = pad;
                                    const rightX = w - pad - nodeW;
                                    const gap = 30;

                                    // 3. Scale Factor (map tokens to pixels)
                                    const totalAvailableH = h - (pad * 2);
                                    const scale = totalAvailableH / totalCap;

                                    // 4. Calculate Node Heights & Positions
                                    const hSystem = Math.max(2, systemEst * scale);
                                    const hTools = Math.max(2, toolsEst * scale);
                                    const hHistory = Math.max(2, historyEst * scale);

                                    // Center the source group vertically
                                    const totalLeftH = hSystem + hTools + hHistory + (2 * gap);
                                    let currentY = (h - totalLeftH) / 2;

                                    const ySystem = currentY;
                                    currentY += hSystem + gap;
                                    const yTools = currentY;  // Renamed from yRag
                                    currentY += hTools + gap;
                                    const yHistory = currentY;

                                    // Right Nodes (Target) - Stacked without gaps (it's one memory block)
                                    const hUsed = used * scale;
                                    const hFree = freeEst * scale;
                                    const totalRightH = hUsed + hFree;
                                    const yRightStart = (h - totalRightH) / 2;

                                    const yUsed = yRightStart;
                                    const yFree = yUsed + hUsed;

                                    // Target Offsets for Flow (where the ribbon lands on the right bar)
                                    // They stack up exactly on the right side
                                    const yTargetSystem = yUsed;
                                    const yTargetRag = yTargetSystem + hSystem; // Simplified: usually calculated by exact proportion
                                    // Recalculate target heights based on exact left heights to ensure perfect alignment
                                    // Actually, 'used' on right might slightly differ from sum(left) due to estimates.
                                    // For visual coherence, we force the Right Used Bar to match the sum of inputs visually here.

                                    // Better approach for flow:
                                    // Map Left Height -> Target Height directly

                                    // 5. Path Generator (Bezier Ribbon) - Non-interactive
                                    const makeRibbon = (yLeft: number, hLeft: number, yRight: number, color: string) => {
                                        const c1x = leftX + nodeW + 150;
                                        const c2x = rightX - 150;

                                        const p1 = `M ${leftX + nodeW} ${yLeft}`;
                                        const c1 = `C ${c1x} ${yLeft}, ${c2x} ${yRight}, ${rightX} ${yRight}`;
                                        const l1 = `L ${rightX} ${yRight + hLeft}`;
                                        const c2 = `C ${c2x} ${yRight + hLeft}, ${c1x} ${yLeft + hLeft}, ${leftX + nodeW} ${yLeft + hLeft}`;
                                        const z = `Z`;

                                        return (
                                            <path
                                                d={`${p1} ${c1} ${l1} ${c2} ${z}`}
                                                fill={color}
                                                opacity={0.35}
                                                className="transition-all duration-300"
                                            />
                                        );
                                    };

                                    // Node renderer - Non-interactive
                                    const makeNode = (x: number, y: number, w: number, h: number, color: string, label: string, sub: string) => {
                                        return (
                                            <g>
                                                <rect x={x} y={y} width={w} height={h} fill={color} rx="4" />
                                                {h > 15 && (
                                                    <>
                                                        <text x={x + 25} y={y + (h / 2) + 4} className="text-[11px] font-bold fill-gray-700 uppercase">{label}</text>
                                                        <text x={x + 25} y={y + (h / 2) + 18} className="text-[10px] fill-gray-500">{sub}</text>
                                                    </>
                                                )}
                                            </g>
                                        );
                                    };

                                    return (
                                        <div className="w-full bg-gray-50 rounded-xl border border-gray-200 overflow-hidden relative flex-1 min-h-[300px] flex flex-col">
                                            <svg viewBox={`0 0 ${w} ${h}`} className="w-full flex-1 select-none">
                                                <defs>
                                                    <linearGradient id="gradUsed" x1="0%" y1="0%" x2="0%" y2="100%">
                                                        <stop offset="0%" stopColor="#1f2937" />
                                                        <stop offset="50%" stopColor="#a78bfa" />
                                                        <stop offset="100%" stopColor="#7c3aed" />
                                                    </linearGradient>
                                                </defs>

                                                {/* --- RIBBONS (Flows): Gold, Lilac, Violet --- */}
                                                <g>
                                                    {makeRibbon(ySystem, hSystem, yUsed, "#1f2937")}
                                                    {makeRibbon(yTools, hTools, yUsed + hSystem, "#a78bfa")}
                                                    {makeRibbon(yHistory, hHistory, yUsed + hSystem + hTools, "#7c3aed")}
                                                </g>

                                                {/* --- LEFT: Source Components --- */}
                                                {makeNode(leftX, ySystem, nodeW, hSystem, "#1f2937", "System Prompt", `${Math.round(systemEst).toLocaleString()} tokens`)}
                                                {makeNode(leftX, yTools, nodeW, hTools, "#a78bfa", "Tool Schemas", `${Math.round(toolsEst).toLocaleString()} tokens`)}
                                                {makeNode(leftX, yHistory, nodeW, hHistory, "#7c3aed", tMain('conversation'), `${Math.round(historyEst).toLocaleString()} ${tMain('tokens')}`)}

                                                {/* --- RIGHT: Context Usage --- */}
                                                <g>
                                                    <rect x={rightX} y={yUsed} width={nodeW} height={hUsed} fill="url(#gradUsed)" rx="4" />
                                                    <rect x={rightX} y={yFree} width={nodeW} height={hFree} fill="#e5e7eb" rx="4" />

                                                    <text x={rightX - 10} y={yUsed + (hUsed / 2)} textAnchor="end" className="text-[12px] font-bold fill-gray-700">Used</text>
                                                    <text x={rightX - 10} y={yUsed + (hUsed / 2) + 16} textAnchor="end" className="text-[11px] fill-gray-500">{used.toLocaleString()} tokens ({contextStats.percent}%)</text>

                                                    <text x={rightX - 10} y={yFree + (hFree / 2)} textAnchor="end" className="text-[12px] font-bold fill-gray-400">Free</text>
                                                    <text x={rightX - 10} y={yFree + (hFree / 2) + 16} textAnchor="end" className="text-[11px] fill-gray-400">{Math.round(freeEst).toLocaleString()} tokens</text>
                                                </g>
                                            </svg>
                                        </div>
                                    );
                                })()}
                            </div>
                        </div>

                    </div>
                </div>
            )}

            {/* Model download toast (success / error) */}
            {downloadToast.show && (
                <div className="fixed bottom-8 left-1/2 -translate-x-1/2 z-[85] animate-in fade-in slide-in-from-bottom-4 duration-300">
                    <div className={cn(
                        "px-4 py-3 rounded-xl shadow-lg border text-sm font-medium",
                        downloadToast.success ? "bg-green-50 border-green-200 text-green-800" : "bg-red-50 border-red-200 text-red-800"
                    )}>
                        {downloadToast.message}
                    </div>
                </div>
            )}

            {/* Changing model overlay (API ↔ Local): show ~5s then reload */}
            {showChangingModelOverlay && (
                <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
                    <div className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-wait" />
                    <div className="relative bg-white rounded-2xl shadow-2xl p-8 flex flex-col items-center gap-4 animate-in fade-in zoom-in-95 duration-300">
                        <div className="relative">
                            <div className="w-16 h-16 border-4 border-gray-100 border-t-gray-900 rounded-full animate-spin" />
                            <div className="absolute inset-0 flex items-center justify-center">
                                <Loader2 size={24} className="text-gray-900" />
                            </div>
                        </div>
                        <div className="text-center">
                            <h3 className="text-lg font-bold text-gray-900">Changing model</h3>
                            <p className="text-sm text-gray-500 mt-1">Switching provider and updating model…</p>
                        </div>
                    </div>
                </div>
            )}

            <SettingsModal
                isOpen={isSettingsOpen}
                onClose={handleSettingsClose}
                config={config}
                onSave={handleSaveConfig}
                availableModels={availableModels}
                apiModels={apiModels}
                onFetchApiModels={fetchApiModels}
                onRefreshLocalModels={refreshLocalModels}
                onRequestModelPreview={requestModelPreview}
                onConfirmModelDownload={downloadModel}
                onCloseModelPreview={() => setModelPreviewData(null)}
                modelPreviewData={modelPreviewData}
                downloadModelStatus={downloadModelStatus}
                onCancelModelDownload={cancelModelDownload}
                tools={tools}
                onRefreshTools={() => ws?.send(JSON.stringify({ type: 'get_tools' }))}
                workflows={workflows}
                trustedSources={trustedSources}
                onAddTrustedSource={(categoryId, name, url) => ws?.send(JSON.stringify({ type: 'add_trusted_source', category_id: categoryId, name, url }))}
                onRemoveTrustedSource={(domain, is_custom) => ws?.send(JSON.stringify({ type: 'remove_trusted_source', domain, is_custom }))}
                onDeleteTrustedCategory={(categoryId) => ws?.send(JSON.stringify({ type: 'delete_trusted_category', category_id: categoryId }))}
                onRequestTrustedSources={() => { setTrustedSourcesError(null); ws?.send(JSON.stringify({ type: 'get_trusted_sources' })); }}
                onCreateTrustedCategory={(name) => ws?.send(JSON.stringify({ type: 'create_trusted_category', name }))}
                trustedSourcesError={trustedSourcesError}
                automations={automations}
                currentUser={currentUser}
                onLogout={() => {
                    setSettingsInitialTab(null);
                    setIsSettingsOpen(false);
                    router.replace('/login');
                }}
                apiBase={getApiBase()}
                initialTab={settingsInitialTab ?? undefined}
                onRefreshConfig={() => ws?.send(JSON.stringify({ type: 'get_config' }))}
                connectionLabel={connectionLabel}
                isConnected={isConnected}
                showIdleState={showIdleState}
                onReconnect={() => { if (!isConnected) { setStatus('connecting'); setReconnectAttempt((a) => a + 1); } }}
                onCreateAutomationSubmit={createAutomationSubmit as SettingsModalProps['onCreateAutomationSubmit']}
                onAutomationCreated={refreshAutomations}
                onDeleteAutomation={deleteAutomation}
                deletingAutomationId={deletingAutomationId}
                onDeleteAutomationAnimationEnd={onDeleteAutomationAnimationEnd}
                automationNotes={automationNotes}
                automationTodos={automationTodos}
                onSendPlannerMessage={(msg) => { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); }}
                userTimeFormat={userTimeFormat}
                onOpenAutomationCalendar={() => { if (ws?.readyState === WebSocket.OPEN) { ws.send(JSON.stringify({ type: 'get_automation_notes' })); ws.send(JSON.stringify({ type: 'get_automation_todos' })); } }}
            />
            <AutomationCalendarModal
                isOpen={isAutomationPopupOpen}
                onClose={() => setIsAutomationPopupOpen(false)}
                currentUser={currentUser}
                automations={automations}
                automationNotes={automationNotes}
                automationTodos={automationTodos}
                onSendPlannerMessage={(msg) => { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); }}
                userTimeFormat={userTimeFormat}
                onSubmitCreateAutomation={createAutomationSubmit}
                onAutomationCreated={refreshAutomations}
                onEditAutomation={(auto) => setEditingAutomationFromCalendar({
                    id: auto.id,
                    name: auto.name,
                    prompt: auto.prompt ?? auto.description ?? '',
                    frequency: auto.frequency,
                    time: auto.time,
                    weekday: auto.weekday ?? undefined,
                    day: auto.day ?? undefined,
                })}
            />
            <NotificationsModal
                isOpen={isNotificationsOpen}
                onClose={() => setIsNotificationsOpen(false)}
                notifications={notifications}
                onFetchComplete={setNotifications}
            />
            {editingAutomationFromCalendar && (
                <CreateAutomationPopup
                    isOpen={true}
                    onClose={() => setEditingAutomationFromCalendar(null)}
                    initialDate={new Date()}
                    initialHour={(() => { const p = (editingAutomationFromCalendar.time || '06:00').split(':'); return Math.max(0, Math.min(23, parseInt(p[0], 10) || 0)); })()}
                    initialMinute={(() => { const p = (editingAutomationFromCalendar.time || '06:00').split(':'); return Math.max(0, Math.min(59, parseInt(p[1], 10) || 0)); })()}
                    editTask={editingAutomationFromCalendar}
                    onCreated={() => { setEditingAutomationFromCalendar(null); refreshAutomations(); }}
                    onSubmit={createAutomationSubmit}
                    onDelete={(taskId) => { deleteAutomation(taskId); setEditingAutomationFromCalendar(null); refreshAutomations(); }}
                />
            )}
        </main>
    );
}

export default function VAFDashboard() {
    return (
        <Suspense fallback={<div className="min-h-screen flex items-center justify-center bg-gray-50"><Loader2 className="w-8 h-8 animate-spin text-gray-400" /></div>}>
            <VAFDashboardContent />
        </Suspense>
    );
}
