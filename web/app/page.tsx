'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useCallback, useEffect, useMemo, useState, useRef, Fragment, Suspense } from 'react';
import { createPortal } from 'react-dom';
import { useRouter, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { toWav16k } from '@/lib/wav';
import {
    Send, Menu, Plus, MessageSquare, Brain, Bot, ChevronLeft, User, Trash2, Edit2, Paperclip,
    Activity, GitBranch, Workflow, CheckCircle2, ShieldAlert, Loader2,
    Settings, Mic, MicOff, Check, ChevronRight, Zap, Volume2, Square, Wrench, FileText, Calendar, ScrollText, AlarmClock,
    Folder, Download, Upload, RefreshCw, ArrowLeft
} from 'lucide-react';
import { cn, getApiBase, getWsBase } from '@/lib/utils';
import { type NativeDocxDocument, flattenNativeDocxText, replaceTextInNativeDocx } from '@/lib/docxNative';
import { loadSessionCache, trimSessionCache, saveSessionCache } from '@/lib/sessionCache';
import SettingsModal, { type SettingsModalProps } from '@/components/SettingsModal';
import { AgentAvatar, type AvatarMode } from '@/components/AgentAvatar';
import { VoiceEnrollmentCall } from '@/components/VoiceEnrollmentCall';
import { VoiceCallLayer } from '@/components/VoiceCallLayer';
import { VoiceCallBar } from '@/components/VoiceCallBar';
import { useVoiceCallStore } from '@/lib/voiceCallStore';
import { TurnActionsTimeline, type TimelineAction } from '@/components/TurnActionsTimeline';
import AutomationCalendarModal from '@/components/AutomationCalendarModal';
import CreateAutomationPopup, { type CreateAutomationPayload, type EditAutomationTask } from '@/components/CreateAutomationPopup';
import NotificationsModal, { type NotificationItem } from '@/components/NotificationsModal';
import AnnouncementModal from '@/components/AnnouncementModal';
import { formatVersion } from '@/lib/version';
import { decideAnnouncement, acknowledgedVersion, latestEntry, type AnnouncementKind } from '@/lib/changelog';
import SubAgentWindow from '@/components/SubAgentWindow';
import DocumentEditor from '@/components/DocumentEditor';
import DocumentViewer, { CHIP_BG_CLASSES, type InsertedSelectionRange } from '@/components/DocumentViewer';
import CodeViewer, { isCodeFile, isTextFile } from '@/components/CodeViewer';
import HtmlViewer, { isHtmlFile } from '@/components/HtmlViewer';
import ImageViewer, { isImageFile, type ImageMark } from '@/components/ImageViewer';
import { ToolMessage } from '@/components/ToolMessage';
import VAFWorkflowRuntime from '@/components/workflows/VAFWorkflowRuntime';
import CopyOnRightClick from '@/components/CopyOnRightClick';
import BrowserLiveTile from '@/components/BrowserLiveTile';
import type { VAFWorkflow } from '@/components/workflows/stores/workflowStore';
import { useWorkflowStore } from '@/components/workflows/stores/workflowStore';
import { WorkflowChatElement } from '@/components/workflows/WorkflowChatElement';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Types
type Message = {
    role: 'user' | 'assistant' | 'system' | 'tool' | 'workflow';
    content: string; // For tools: this is the result
    timestamp: number;
    /** System-activity / wake-up kind (e.g. "timer") — rendered in the left-side wake area, not a normal bubble. */
    kind?: string;
    /** Attachments shown on user messages (name + mimeType only; data not stored) */
    files?: { name: string; mimeType: string }[];
    /** Inline images attached to user message — stored as data URIs for display */
    images?: { url: string; name: string }[];
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
    /** Files created by a coding agent — shown as blue download chips inside this bubble */
    downloadFiles?: { path: string; name: string }[];
    /** Code Viewer was open when sent — shown as chip; content is NOT stored here */
    codeViewerFile?: { name: string; path: string; ext: string; lineCount: number };
};

type Session = {
    id: string;
    title: string;
    messageCount?: number;
    /** Thinking-mode run shown in chat list with brain icon */
    source?: 'thinking';
};

type SessionEditorDocumentState = {
    isOpen: boolean;
    filePath: string;
    title: string;
    content?: string;
    docxModel?: NativeDocxDocument | null;
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
const parseThinkBlocks = (content: string): { thought: string | null; answer: string; isThinkingComplete: boolean } => {
    if (!content) return { thought: null, answer: "", isThinkingComplete: true };

    // Clean Rich markup tags and "resposta" prefix
    let clean = content.replace(/[\[][\/]?\w+\s*\w+[\]]/g, '').replace(/^resposta\s*/i, '');

    // Normalize different thinking tag formats to <think>
    clean = clean.replace(/<thinking>/gi, '<think>').replace(/<\/thinking>/gi, '</think>');

    // Merge consecutive thinking blocks, and collapse doubled/nested tags. Weak local models
    // (e.g. Gemma) sometimes emit <think><think>... or repeated close tags; normalizing here keeps the
    // Thinking panel clean and stops a literal tag leaking into the answer. No-op for well-formed output.
    let merged = clean
        .replace(/<\/think>\s*<think>/gi, ' ')                // adjacent close->open: one block
        .replace(/<think>(?:\s*<think>)+/gi, '<think>')       // collapse doubled open tags
        .replace(/<\/think>(?:\s*<\/think>)+/gi, '</think>'); // collapse doubled close tags

    const openTag = "<think>";
    const closeTag = "</think>";
    const openIndex = merged.indexOf(openTag);

    // Method 1: Explicit <think> tags — ONLY when the reasoning LEADS the message.
    // If real answer text precedes the first <think>, the tag is something the agent
    // *mentioned* in its answer (e.g. explaining the tag format) — not a reasoning block.
    // In that case we must NOT pull it into the Thinking panel; leave it in the answer.
    const leadsMessage = openIndex !== -1 && merged.substring(0, openIndex).trim() === "";
    if (openIndex !== -1 && leadsMessage) {
        // Normally use the FIRST close after the open, so a later <think>...</think> the agent
        // mentions inside its answer is not swallowed into the Thinking panel. But weak models
        // sometimes quote a </think> mid-reasoning (e.g. quoting an earlier reply): then a STRAY
        // </think> (no matching open) remains in the answer region -> use the LAST </think> instead.
        let closeIndex = merged.indexOf(closeTag, openIndex + openTag.length);
        if (closeIndex !== -1) {
            const after = merged.substring(closeIndex + closeTag.length);
            const opens = (after.match(/<think>/gi) || []).length;
            const closes = (after.match(/<\/think>/gi) || []).length;
            if (closes > opens) closeIndex = merged.lastIndexOf(closeTag);  // stray close -> real close is the last
        }
        if (closeIndex !== -1) {
            // Complete leading thinking block
            const thought = merged.substring(openIndex + openTag.length, closeIndex).trim();
            const answer = (merged.substring(0, openIndex) + merged.substring(closeIndex + closeTag.length)).trim();
            // Safeguard: content inside think tags may be a user-facing answer (API models sometimes misuse tags)
            const looksLikeUserAnswer = /\b(was ich über dich weiß|hier die Übersicht|Das sind die Infos|Name:\s*[A-Z]|Standort:)/i.test(thought);
            if (answer.length < 50 && looksLikeUserAnswer) {
                return { thought: null, answer: thought, isThinkingComplete: true };
            }
            return { thought, answer, isThinkingComplete: true };
        } else {
            // Incomplete thinking - leading open tag, close not streamed yet
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

// Wraps parseThinkBlocks and additionally extracts an <Action>...</Action> block.
// Returns: { thought, answer, action, isThinkingComplete }
const parseContent = (content: string): { thought: string | null; answer: string; action: string | null; isThinkingComplete: boolean; isActionComplete: boolean } => {
    const base = parseThinkBlocks(content);
    let answer = base.answer;
    let isThinkingComplete = base.isThinkingComplete;
    let isActionComplete = true;
    const thoughts: string[] = base.thought ? [base.thought] : [];
    const actions: string[] = [];

    // Pull EVERY complete <think>...</think> block out of the answer (the model may emit a
    // second one after it already started answering). Prose around them stays as the answer —
    // raw tags must NEVER appear in the answer bubble. Non-greedy => no swallowing.
    answer = answer.replace(/<think>([\s\S]*?)<\/think>/gi, (_m, inner) => {
        const t = String(inner).trim(); if (t) thoughts.push(t); return '';
    });
    // Pull EVERY complete <Action>...</Action> block, wherever it appears.
    answer = answer.replace(/<action>([\s\S]*?)<\/action>/gi, (_m, inner) => {
        const a = String(inner).trim(); if (a) actions.push(a); return '';
    });
    // Trailing unterminated tag (still streaming): take the remainder as that block.
    const tOpen = answer.search(/<think>/i);
    if (tOpen !== -1) {
        const t = answer.substring(tOpen).replace(/<think>/i, '').trim();
        if (t) thoughts.push(t);
        answer = answer.substring(0, tOpen);
        isThinkingComplete = false;
    }
    const aOpen = answer.search(/<action>/i);
    if (aOpen !== -1) {
        const a = answer.substring(aOpen).replace(/<action>/i, '').trim();
        if (a) actions.push(a);
        answer = answer.substring(0, aOpen);
        isActionComplete = false;
    }

    return {
        thought: thoughts.length ? thoughts.join('\n\n') : null,
        answer: answer.trim(),
        action: actions.length ? actions.join('\n\n') : null,
        isThinkingComplete,
        isActionComplete,
    };
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

// Sub-agent custom-view kind, derived from the tool name the moment the main agent CALLS the sub-agent —
// so the matching custom window opens immediately (in a loading state) instead of waiting for streamed data.
// Single source of truth for both detection and the immediate open.
type SubAgentKind = 'coder' | 'research' | 'document' | 'librarian' | 'browser';
const SUBAGENT_KIND_BY_TOOL: Array<[RegExp, SubAgentKind]> = [
    [/coding_agent/i, 'coder'],
    [/research_agent/i, 'research'],
    [/document_agent/i, 'document'],
    [/librarian_agent/i, 'librarian'],
    [/browser_agent/i, 'browser'],
];
function subAgentKindFromName(toolName: string): SubAgentKind | null {
    const n = String(toolName || '');
    for (const [re, kind] of SUBAGENT_KIND_BY_TOOL) if (re.test(n)) return kind;
    return null;
}

// Parse [WORKFLOW_ASYNC:taskId:workflowId] Workflow 'Name' ... from assistant text for card display
// Hard cap on the in-memory `messages` array. During Live-Mode the agent streams
// System/Step/Router/Tool log entries continuously; without a cap `messages` grows
// unbounded over a multi-hour session and leaks the JS heap (RAM climbs to several GB).
// The chat only ever renders the last MSG_TURNS user-turns, and the full history lives
// in backend session storage, so trimming the tail is invisible to the user.
const MAX_LIVE_MESSAGES = 1500;
// Minimum time the running-tool avatar animation stays on screen, so fast tools (e.g. memory search)
// don't flash by before the animation is readable.
const TOOL_ANIM_MIN_MS = 1000;

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
// Open while incomplete, auto-close when complete.
// Measured think durations live in a module-level cache keyed per assistant message, so the value
// survives the inline→timeline remount of the first thinking block (else it would lose its time).
const thinkDurationCache = new Map<string | number, number>();
const ThinkingDetails = ({ thought, isComplete = true, durationKey }: { thought: string; isComplete?: boolean; durationKey?: string | number }) => {
    const [isOpen, setIsOpen] = useState(!isComplete);
    const openedAtRef = useRef<number>(Date.now());
    const closeTimeoutRef = useRef<NodeJS.Timeout | null>(null);
    const scrollRef = useRef<HTMLDivElement | null>(null);
    const MIN_OPEN_MS = 800;
    const CLOSE_DELAY_MS = 400;
    // Measured think duration: captured once when streaming finishes (only when we saw it live,
    // so history-loaded turns — mounted already complete — show no bogus time).
    const startRef = useRef<number>(Date.now());
    const wasLiveRef = useRef<boolean>(!isComplete);
    const [durationSec, setDurationSec] = useState<number | null>(
        () => (durationKey != null ? thinkDurationCache.get(durationKey) ?? null : null)
    );

    // Auto-update when isComplete changes
    useEffect(() => {
        if (!isComplete) {
            if (closeTimeoutRef.current) {
                clearTimeout(closeTimeoutRef.current);
                closeTimeoutRef.current = null;
            }
            openedAtRef.current = Date.now();
            wasLiveRef.current = true;
            setIsOpen(true);
            return;
        }

        // streaming just finished — record how long the thinking ran (and cache it so the value
        // survives the remount when this block moves from inline into the grouped timeline)
        setDurationSec(prev => {
            if (prev !== null) return prev;
            if (wasLiveRef.current) {
                const sec = (Date.now() - startRef.current) / 1000;
                if (durationKey != null) thinkDurationCache.set(durationKey, sec);
                return sec;
            }
            return durationKey != null ? thinkDurationCache.get(durationKey) ?? null : null;
        });

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
        <div className={cn(
            "relative w-full max-w-[95%] overflow-hidden rounded-[13px] border bg-gradient-to-b from-[#fcfcfd] to-[#f8fafc] dark:from-[#1e1e1e] dark:to-[#1e1e1e] transition-colors",
            !isComplete ? "border-[#ede9fe] dark:border-[#2f2f2f]" : "border-gray-200"
        )}>
            {!isComplete && <span className="chat-shimmer-overlay" aria-hidden />}
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
                className="relative flex w-full items-center gap-2 px-3 py-2 text-[12px] font-semibold text-[#3b3f4a] dark:text-gray-300 transition-colors hover:bg-black/[0.03] dark:hover:bg-white/[0.04]"
            >
                {!isComplete ? (
                    <>
                        <span>Thinking</span>
                        <span className="chat-think-dots" style={{ color: '#7c3aed' }} aria-hidden><span /><span /><span /></span>
                    </>
                ) : (
                    <>
                        <span>Thinking Process</span>
                        {durationSec !== null && (
                            <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] font-semibold text-violet-600">{durationSec.toFixed(1)}s</span>
                        )}
                    </>
                )}
                <ChevronRight size={13} className={cn("ml-auto text-gray-400 transition-transform duration-300", isOpen && "rotate-90")} />
            </button>
            <div
                ref={scrollRef}
                className={cn(
                    "overflow-y-auto font-mono text-[12.5px] leading-relaxed text-slate-500 transition-all duration-300 ease-out",
                    isOpen ? "max-h-[400px] px-3.5 pb-3 pt-0 opacity-100" : "max-h-0 px-0 py-0 opacity-0"
                )}
            >
                {thought}
            </div>
        </div>
    );
};

// Component: Action Accordion — same collapse behaviour as ThinkingDetails, amber accent.
// Open while streaming, auto-collapses when the stream completes.
const ActionDetails = ({ action, isComplete = true }: { action: string; isComplete?: boolean }) => {
    const [isOpen, setIsOpen] = useState(!isComplete);
    const openedAtRef = useRef<number>(Date.now());
    const closeTimeoutRef = useRef<NodeJS.Timeout | null>(null);
    const MIN_OPEN_MS = 800;
    const CLOSE_DELAY_MS = 400;

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
        if (closeTimeoutRef.current) clearTimeout(closeTimeoutRef.current);
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

    if (!action) return null;

    return (
        <div className="mb-3 rounded-xl border border-amber-200 bg-amber-50/40 overflow-hidden w-full max-w-[95%] shadow-sm">
            <button
                type="button"
                onClick={() => {
                    if (closeTimeoutRef.current) {
                        clearTimeout(closeTimeoutRef.current);
                        closeTimeoutRef.current = null;
                    }
                    const next = !isOpen;
                    if (next) openedAtRef.current = Date.now();
                    setIsOpen(next);
                }}
                className="w-full px-4 py-2.5 flex items-center justify-between text-[11px] uppercase tracking-wide font-semibold text-amber-600 hover:bg-amber-100/50 transition-colors"
            >
                <span className="flex items-center gap-2">
                    {!isComplete ? (
                        <Loader2 size={14} className="animate-spin text-amber-600" />
                    ) : (
                        <Zap size={14} />
                    )}
                    {!isComplete ? "Action..." : "Action"}
                </span>
                <ChevronRight size={14} className={cn("text-amber-400 transition-transform duration-200", isOpen && "rotate-90")} />
            </button>
            <div
                className={cn(
                    "text-xs text-slate-700 font-mono leading-relaxed border-t border-amber-200 bg-white/50 overflow-y-auto transition-all duration-300 ease-out whitespace-pre-wrap",
                    isOpen ? "max-h-[500px] opacity-100 px-4 py-3" : "max-h-0 opacity-0 px-0 py-0 border-t-transparent"
                )}
            >
                {action}
            </div>
        </div>
    );
};

// AgentAvatar (the living white dot) now lives in @/components/AgentAvatar so the chat header
// and the Whare Wananga training stage share one source of truth for the agent's identity.

// Animates a title string: types text character by character after a short delay.
function TypingTitle({ text }: { text: string }) {
    const [displayed, setDisplayed] = React.useState('');

    React.useEffect(() => {
        let alive = true;
        setDisplayed('');

        let i = 0;
        const typeNext = () => {
            if (!alive) return;
            i++;
            setDisplayed(text.slice(0, i));
            if (i < text.length) setTimeout(typeNext, 38 + Math.random() * 22);
        };
        const t = setTimeout(typeNext, 50);

        return () => { alive = false; clearTimeout(t); };
    }, [text]);

    return <>{displayed}</>;
}

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
                    <AgentAvatar mode={isLoading ? 'plan' : 'idle'} />
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

// Map a running tool's raw name → the avatar's tool-scene animation (null = no special scene).
// Sub-agent tools are handled separately (isSubAgentRunning → 'delegate'), so they're not listed here.
function toolAvatarMode(rawName: string): AvatarMode | null {
    const n = (rawName || '').toLowerCase();
    if (n === 'browser_agent') return 'browsing';                                                   // globe
    if (['web_search', 'webfetch', 'web_fetch', 'read_file', 'find_files', 'github_get_file', 'github_search_files', 'github_get_file_structure'].includes(n)) return 'searching';   // magnifier over a document
    if (['bash', 'python_exec', 'python_sandbox'].includes(n)) return 'executing';                  // terminal + spinner
    if (['write_file', 'document_writer', 'document_editor', 'move_file', 'github_update_file', 'replace_editor_text', 'replace_editor_selection'].includes(n)) return 'writing';   // editor types
    if (['memory_save', 'memory_search', 'add_memory', 'update_codex'].includes(n)) return 'remembering';   // recall dots
    if (n.startsWith('send_') || n === 'whatsapp_call') return 'uploading';                         // packets rise out
    return null;
}

// Away scenes the avatar plays on a NUDGE ("are you there?") bubble. Rotated per nudge but STABLE per
// bubble (derived from the message timestamp), so a re-render never reshuffles the scene. A live nudge's
// timestamp is Date.now(), so successive nudges land on different scenes.
const AWAY_MODES: AvatarMode[] = ['away_nap', 'away_coffee', 'away_stars', 'away_groove'];
const awayModeFor = (ts: number): AvatarMode => AWAY_MODES[Math.abs(Math.floor(ts || 0)) % AWAY_MODES.length];

// One ▢ step box. The active box pops bigger + opaque and is RANDOMLY black-filled or just a crisp
// outline; idle boxes are small, faint, hollow. Monochrome (no colours); fill toggles per tick.
const PlanBox = ({ active, filled }: { active: boolean; filled: boolean }) => (
    <span
        className="planbox"
        style={{
            // Monochrome (no colours): active = bigger & fully opaque, RANDOMLY black-filled OR just a
            // crisp bigger outline; idle = small, faint, hollow. transform/opacity = compositor and the
            // fill toggles per sequence tick (event-driven, not per frame), so it stays leak-safe.
            backgroundColor: active && filled ? 'var(--planbox-ink)' : 'transparent',
            transform: active ? 'scale(1.28)' : 'scale(0.82)',
            opacity: active ? 1 : 0.4,
        }}
    />
);

// The live setup/plan indicator — ONE stable element for the whole setup/routing phase, rendered
// OUTSIDE the message map (which remounts per step). Because it stays mounted for the phase, the
// `plan` animation + the ▢–▢–▢ step boxes loop CONTINUOUSLY; only `message` updates. The full step
// trace lives in the logs/terminal, so the chat stays clean.
const SetupLine = ({ message }: { message: string }) => {
    const cleanText = message.replace(/^(Router|Step \d+\/\d+|System|Agent|Info)\s*[:\|]?\s*/, '');
    const source = message.match(/^(Router|Step \d+\/\d+|System|Agent|Info)/)?.[0] || 'System';
    // The ▢ sequence is driven HERE (one stable element → no remount, animation never restarts per
    // step): every ~800ms the next box becomes "active" (bigger + opaque), the others idle (small,
    // faint). The active box is RANDOMLY black-filled or just a bigger outline (~45% hollow), toggled
    // per tick (event-driven, not per frame) — monochrome, no colours.
    const [active, setActive] = useState(0);
    const [filled, setFilled] = useState(true);
    useEffect(() => {
        const t = setInterval(() => { setActive(a => (a + 1) % 3); setFilled(Math.random() >= 0.45); }, 800);
        return () => clearInterval(t);
    }, []);
    return (
        <div className="flex gap-4 items-center w-full">
            <div className="w-9 shrink-0 flex justify-center">
                <AgentAvatar mode="working" />
            </div>
            <div className="flex items-center gap-3 min-w-0 flex-1">
                <span className="flex items-center gap-[3px] text-[#2a3142] shrink-0" aria-hidden="true">
                    <PlanBox active={active === 0} filled={filled} />
                    <span className="planlnk" />
                    <PlanBox active={active === 1} filled={filled} />
                    <span className="planlnk" />
                    <PlanBox active={active === 2} filled={filled} />
                </span>
                <div className="text-xs flex items-center gap-2 min-w-0">
                    <span className="font-semibold uppercase tracking-wider text-[10px] text-gray-600 shrink-0">{source}</span>
                    <span className="text-gray-900 font-medium truncate">{cleanText}</span>
                </div>
            </div>
        </div>
    );
};

// Chat-history loading indicator. The agent "works in the background" (Working: morphing eye + a white
// orbiting satellite) while the step-boxes cycle to its right — same box sequence as SetupLine (one
// stable element so the loop never restarts), but for the load phase instead of the setup phase.
const ChatLoadingLine = () => {
    const [active, setActive] = useState(0);
    const [filled, setFilled] = useState(true);
    useEffect(() => {
        const t = setInterval(() => { setActive(a => (a + 1) % 3); setFilled(Math.random() >= 0.45); }, 800);
        return () => clearInterval(t);
    }, []);
    return (
        <div className="flex gap-4 items-center">
            <div className="w-9 shrink-0 flex justify-center">
                <AgentAvatar mode="working" />
            </div>
            <div className="flex items-center gap-3 min-w-0">
                <span className="flex items-center gap-[3px] text-[#2a3142] shrink-0" aria-hidden="true">
                    <PlanBox active={active === 0} filled={filled} />
                    <span className="planlnk" />
                    <PlanBox active={active === 1} filled={filled} />
                    <span className="planlnk" />
                    <PlanBox active={active === 2} filled={filled} />
                </span>
                <span className="text-sm text-gray-400">Chat wird geladen…</span>
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
    // Voice-profile enrollment (live call)
    const [voiceCallOpen, setVoiceCallOpen] = useState(false);
    const [speakerProfile, setSpeakerProfile] = useState<any>(null);
    // First call without a voice profile: OFFER enrollment (never force it) -
    // the voice-gated delegation guard only works with a profile. A skip is
    // remembered per browser so the offer never nags.
    const [showEnrollOffer, setShowEnrollOffer] = useState(false);
    const enrollThenCallRef = useRef(false);
    // Live voice call (voice-agent first layer)
    const voiceCallActive = useVoiceCallStore((s) => s.active);
    const voiceCallClosing = useVoiceCallStore((s) => s.closing);
    const [authError, setAuthError] = useState<string | null>(null);
    const [authRetryKey, setAuthRetryKey] = useState(0);

    // If network TLS/proxy mode is active, avoid staying on :3000.
    // Redirect to the HTTPS access port so auth/ws/session are on the correct origin.
    useEffect(() => {
        if (typeof window === 'undefined') return;
        if (window.location.port !== '3000') return;
        const ac = new AbortController();
        fetch(`${getApiBase()}/api/network/ws-config`, { signal: ac.signal, cache: 'no-store' })
            .then((r) => (r.ok ? r.json() : null))
            .then((cfg) => {
                const useWss = !!cfg?.useWss;
                const targetPort = String(cfg?.port || '');
                if (!useWss || !targetPort || targetPort === '3000') return;
                const targetUrl = `https://${window.location.hostname}:${targetPort}${window.location.pathname}${window.location.search}${window.location.hash}`;
                window.location.replace(targetUrl);
            })
            .catch(() => {});
        return () => ac.abort();
    }, []);

    useEffect(() => {
        setAuthError(null);
        const ac = new AbortController();
        const timeoutId = setTimeout(() => ac.abort(), 8000);
        const authHeaders: Record<string, string> = {
            'Cache-Control': 'no-cache',
        };
        if (typeof window !== 'undefined') {
            const token = localStorage.getItem('vaf_token');
            if (token) authHeaders.Authorization = `Bearer ${token}`;
        }
        fetch(`${getApiBase()}/api/auth/me`, {
            credentials: 'include',
            signal: ac.signal,
            cache: 'no-store',
            headers: authHeaders,
        })
            .then(async (res) => {
                if (res.ok) {
                    const userData = await res.json();
                    setCurrentUser(userData);
                    setIsAuthenticated(true);
                } else {
                    if (typeof window !== 'undefined') {
                        // Avoid split-brain auth state (stale token in storage, invalid on backend)
                        localStorage.removeItem('vaf_token');
                        // Hard navigation: router.replace('/login') can fail to leave the dashboard
                        // shell when using the integrated HTTPS proxy (e.g. https://localhost:8443).
                        window.location.replace(`${window.location.origin}/login`);
                    } else {
                        router.replace('/login');
                    }
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

    // Chain-alert: silently poll today's timeline hash chain for admins; show red dot if broken
    useEffect(() => {
        if (currentUser?.role !== 'admin') return;
        const check = () => {
            const today = new Date().toISOString().slice(0, 10);
            fetch(`${getApiBase()}/api/logs/timeline/events?date=${today}&merge=false`, { credentials: 'include' })
                .then(r => r.ok ? r.json() : null)
                .then(d => { if (d && d.total_raw > 0) setChainAlert(d.chain_ok === false); })
                .catch(() => {});
        };
        check();
        const id = setInterval(check, 5 * 60 * 1000); // every 5 min
        return () => clearInterval(id);
    }, [currentUser]);

    // OAuth callback redirect: open Settings with Connections tab when URL has connections=1 or cloud_oauth/email_oauth
    const openedFromOAuthRef = useRef(false);

    const fetchUserTimeFormat = useCallback(() => {
        fetch(getApiBase() + '/api/user/persona')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                const tf = (data?.user_identity?.time_format || '').toString().toLowerCase();
                // "Default (24h)" is stored as empty string; empty and '24h' both mean 24h
                setUserTimeFormat(tf === '12h' ? '12h' : '24h');
                const name = data?.user_identity?.name || data?.identity?.name || null;
                setUserName(name || null);
                setLastSeenVersion(data?.user_identity?.last_seen_announcement_version ?? null);
                setPersonaLoaded(true);
            })
            .catch(() => setUserTimeFormat('24h'));
    }, []);

    useEffect(() => { fetchUserTimeFormat(); }, [fetchUserTimeFormat]);

    // App version — single source of truth: backend /api/version -> vaf/version.py. Drives the badge.
    useEffect(() => {
        fetch(getApiBase() + '/api/version', { credentials: 'include' })
            .then(res => res.ok ? res.json() : null)
            .then(data => { if (data?.version) setRawVersion(String(data.version)); })
            .catch(() => {});
    }, []);

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

    // User dismissed the tiled browser live view for the current run (reset on next workflow start).
    const [browserTileClosed, setBrowserTileClosed] = useState(false);

    const [status, setStatus] = useState('connecting');
    const [modelLoaded, setModelLoaded] = useState<boolean | null>(null);
    const [modelProvider, setModelProvider] = useState<string | null>(null);
    const [sessions, setSessions] = useState<Session[]>([]);
    const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
    const [unreadSessions, setUnreadSessions] = useState<Set<string>>(new Set());
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
    /** Set true when user clicks Stop; cleared by generation_stopped. Gates late agent_message_update events from re-setting isGenerating=true. */
    const isStoppingGenerationRef = useRef(false);
    const sidebarListRef = useRef<HTMLDivElement>(null);
    const sidebarDocsSyncedForSessionRef = useRef<string | null>(null);
    type DocumentViewerDoc = { id: string; name: string; mimeType?: string; data?: string; content?: string; htmlContent?: string };

    const [ws, setWs] = useState<WebSocket | null>(null);
    const wsSocketRef = useRef<WebSocket | null>(null); // so cleanup can close when effect re-runs before async completes
    const [loading, setLoading] = useState(false);
    const [isGenerating, setIsGenerating] = useState(false);
    const [isStoppingGeneration, setIsStoppingGeneration] = useState(false);
    const [stopHovered, setStopHovered] = useState(false);
    const [stopPulsing, setStopPulsing] = useState(false);
    const stopPulseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const stopBtnRef = useRef<HTMLButtonElement>(null);
    const [stopBtnPos, setStopBtnPos] = useState<{ x: number; y: number } | null>(null);
    const [createdFiles, setCreatedFiles] = useState<{ path: string; name: string; sessionId: string }[]>([]);
    const [statusMessage, setStatusMessage] = useState(''); // RE-ADDED
    const [activeToolName, setActiveToolName] = useState(''); // Currently-running tool name for loading bubble
    const [activeToolMode, setActiveToolMode] = useState<AvatarMode | null>(null); // avatar animation for the running tool (web_search→searching, …)
    // Some tools finish almost instantly (memory search hits a local pgvector in <100ms) — the avatar
    // animation would flash by unseen. Hold the running-tool mode for at least TOOL_ANIM_MIN_MS so the
    // animation is always legible; a NEW tool start cancels the pending release and takes over at once.
    const toolModeStartRef = useRef<number>(0);
    const toolModeClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
    const [historyLoading, setHistoryLoading] = useState(false);  // chat history is being fetched (load_session) and nothing is cached to show meanwhile
    const syncedSessions = useRef<Set<string>>(new Set());        // sessions whose history snapshot already arrived → re-entering an (empty) one shows no spinner flash
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
    /** User's display name from user_identity.json (e.g. "Mert"). Used for personalised welcome greeting. */
    const [userName, setUserName] = useState<string | null>(null);
    /** Randomly selected welcome greeting — refreshed each time the chat is empty (new chat). */
    const [welcomeText, setWelcomeText] = useState('');
    const isEmpty = messages.length === 0;
    useEffect(() => {
        if (!isEmpty) return;
        const raw = tMain.raw('welcomeGreetings') as string[];
        const pool = userName
            ? raw
            : raw.filter(g => !g.includes('{name}'));
        const picked = pool[Math.floor(Math.random() * pool.length)] ?? tMain('howCanIHelp');
        setWelcomeText(picked.replace('{name}', userName ?? ''));
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isEmpty, userName]);

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
    // Speaker confirmation ("was that your voice?"): web fallback card when no
    // main messenger is configured. nameOpen/name are local UI state for the
    // "no, that's <name>" answer.
    type PendingSpeakerConfirm = { confirmId: string; question: string; audioPath: string; score?: number; nameOpen?: boolean; name?: string };
    const [pendingSpeakerConfirms, setPendingSpeakerConfirms] = useState<PendingSpeakerConfirm[]>([]);
    const [tools, setTools] = useState<Array<{
        name: string;
        description: string;
        category: string;
        is_custom?: boolean;
        can_manage?: boolean;
        shared_with?: string[];
        created_by?: string;
        updated_at?: string;
        learned_state?: string;
        requires_config?: boolean;
        configured?: boolean;
    }>>([]);
    // Custom tool management state (admin only)
    const [customToolUsers, setCustomToolUsers]             = useState<Array<{ id: string; username: string; user_scope_id: string; role: string }>>([]);
    const [isCustomToolSaving, setIsCustomToolSaving]       = useState(false);
    const [customToolBackendError, setCustomToolBackendError] = useState<string | null>(null);
    const [mcpServers, setMcpServers] = useState<Array<{ name: string; command: string; transport: string; url?: string; enabled: boolean; permission_level: string; env?: Record<string, string>; connected?: boolean; tool_count?: number; error?: string | null }>>([]);
    const [isMcpSaving, setIsMcpSaving] = useState(false);
    const [mcpBackendError, setMcpBackendError] = useState<string | null>(null);
    const [isMcpTesting, setIsMcpTesting] = useState(false);
    const [mcpTestResult, setMcpTestResult] = useState<{ connected: boolean; tool_count: number; tools?: string[]; error?: string | null } | null>(null);
    const [workflows, setWorkflows] = useState<Array<{ id: string; name: string; description: string; steps: number; is_custom?: boolean }>>([]);
    const [isWorkflowSaving, setIsWorkflowSaving]       = useState(false);
    const [workflowBackendError, setWorkflowBackendError] = useState<string | null>(null);
    const [skills, setSkills] = useState<Array<{ id: string; name: string; description: string; valid?: boolean; error?: string | null; shared_with?: string[]; created_by?: string; can_manage?: boolean; source?: string; scan?: { score?: number; level?: string; count?: number } | null }>>([]);
    const [isSkillSaving, setIsSkillSaving]             = useState(false);
    const [skillBackendError, setSkillBackendError]     = useState<string | null>(null);
    const [skillSavedTick, setSkillSavedTick]           = useState(0);
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
    // Announcement modal: first-run Open-Alpha notice, reused as a per-version "what's new".
    const [announcement, setAnnouncement] = useState<AnnouncementKind>(null);
    const [rawVersion, setRawVersion] = useState<string | null>(null);
    const [lastSeenVersion, setLastSeenVersion] = useState<string | null>(null);
    const [personaLoaded, setPersonaLoaded] = useState(false);
    const [gateRequest, setGateRequest] = useState<{ tool: string; cwd: string; reason: string; args_preview: string } | null>(null);
    // The main agent avatar briefly FLASHES a tool's outcome (success / error); a pending risky-tool
    // confirmation (gateRequest) shows `permission`. The flash auto-clears after ~one cycle so we
    // never leave an infinite reaction running on the avatar.
    const [agentReaction, setAgentReaction] = useState<AvatarMode | null>(null);
    const agentReactionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const fireAgentReaction = useCallback((m: AvatarMode, durationMs = 2200) => {
        setAgentReaction(m);
        if (agentReactionTimer.current) clearTimeout(agentReactionTimer.current);
        agentReactionTimer.current = setTimeout(() => setAgentReaction(null), durationMs);
    }, []);
    useEffect(() => () => { if (agentReactionTimer.current) clearTimeout(agentReactionTimer.current); }, []);
    const [chainAlert, setChainAlert] = useState(false);
    // const [activeTools, setActiveTools] = useState<ToolState[]>([]); // REPLACED BY INLINE MESSAGES

    const fileInputRef = useRef<HTMLInputElement>(null);
    const [isDragOver, setIsDragOver] = useState(false);
    /** Images staged in the input bar, cleared after send */
    const [attachedImages, setAttachedImages] = useState<{ id: string; url: string; name: string }[]>([]);

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
    // Mobile RAG-snippets popover: hover doesn't exist on touch, so a tap toggles it (works on iOS too).
    const [ragSnipOpen, setRagSnipOpen] = useState(false);
    const ragGroupRef = useRef<HTMLDivElement | null>(null);
    // Close the RAG popover on any tap or scroll OUTSIDE it. A fixed scrim can't be used here: the popover
    // lives inside the composer wrapper which carries a transform, so a `fixed` child is clipped to that
    // wrapper instead of the viewport. A document-level listener is transform-proof.
    useEffect(() => {
        if (!ragSnipOpen) return;
        const onDown = (e: Event) => {
            if (!ragGroupRef.current?.contains(e.target as Node)) setRagSnipOpen(false);
        };
        const onScroll = () => setRagSnipOpen(false);
        document.addEventListener('pointerdown', onDown, true);
        document.addEventListener('scroll', onScroll, true);
        return () => {
            document.removeEventListener('pointerdown', onDown, true);
            document.removeEventListener('scroll', onScroll, true);
        };
    }, [ragSnipOpen]);

    // Session workspace window: file browser over the chat's own folder
    // (VAF_Projects/<uid>/<session_id>) with download/upload + drag and drop
    const [workspaceInfo, setWorkspaceInfo] = useState<{
        path: string;
        name: string;
        displayName?: string;
        label?: string | null;
        subpath: string;
        dirs: Array<{ name: string; items: number }>;
        files: Array<{ name: string; size: number; modified: string }>;
    } | null>(null);
    const [isWorkspaceModalOpen, setIsWorkspaceModalOpen] = useState(false);
    const [workspaceUploading, setWorkspaceUploading] = useState(false);
    const workspaceFileInputRef = useRef<HTMLInputElement>(null);
    // Central Data Explorer: 'index' lists ALL of this user's workspaces (live + orphaned); 'folder' is the
    // per-chat view. Default 'folder' so opening from a chat keeps today's behavior.
    const [workspaceView, setWorkspaceView] = useState<'index' | 'folder'>('folder');
    const [allWorkspaces, setAllWorkspaces] = useState<Array<{
        sessionId: string; displayName: string; label: string | null; liveTitle: string | null;
        orphan: boolean; fileCount: number; folderCount: number; updated: string;
    }>>([]);

    const workspaceSubpathRef = useRef('');
    // The workspace currently OPEN in the viewer. It may be an orphan or another chat's
    // workspace — NOT necessarily the active chat. Sid-less operations (drill into a subfolder,
    // Back, Refresh, upload, delete) must target THIS, not currentSessionId; otherwise opening a
    // subfolder of an orphan jumped to the active chat's workspace.
    const viewedWorkspaceSidRef = useRef<string | null>(null);
    const refreshWorkspace = useCallback(async (sid?: string | null, subpath?: string, _retry: number = 0) => {
        const id = sid ?? viewedWorkspaceSidRef.current ?? currentSessionId;
        if (!id) { setWorkspaceInfo(null); return; }
        viewedWorkspaceSidRef.current = id;   // remember it so sid-less navigation stays put
        const sub = subpath ?? workspaceSubpathRef.current;
        try {
            const res = await fetch(
                `${getApiBase()}/api/session/workspace?sessionId=${encodeURIComponent(id)}&subpath=${encodeURIComponent(sub)}`,
                { credentials: 'include' }
            );
            if (!res.ok) {
                // Folder may have been deleted - fall back to the root
                if (sub) { workspaceSubpathRef.current = ''; refreshWorkspace(id, ''); return; }
                // Transient server error (e.g. while the renderer was stalled): retry a few times so the
                // workspace button recovers instead of staying hidden for good. A 4xx (e.g. 403) is
                // definitive and not retried.
                if (res.status >= 500 && _retry < 3) setTimeout(() => refreshWorkspace(id, '', _retry + 1), 1500);
                return;
            }
            const data = await res.json();
            workspaceSubpathRef.current = data?.subpath ?? '';
            setWorkspaceInfo(data?.path ? data : null);
        } catch {
            // Backend unreachable / the renderer stalled mid-request. The session-change effect already
            // cleared workspaceInfo, so without a retry the button would vanish permanently after a UI
            // freeze. Retry a few times before giving up.
            if (_retry < 3) setTimeout(() => refreshWorkspace(id, sub, _retry + 1), 1500);
        }
    }, [currentSessionId]);

    // Load workspace info whenever the active chat changes
    useEffect(() => {
        setWorkspaceInfo(null);
        workspaceSubpathRef.current = '';
        if (currentSessionId) refreshWorkspace(currentSessionId, '');
    }, [currentSessionId, refreshWorkspace]);

    const uploadWorkspaceFiles = useCallback(async (files: File[]) => {
        const wsSid = viewedWorkspaceSidRef.current ?? currentSessionId;
        if (!wsSid || !workspaceInfo?.path) return;
        setWorkspaceUploading(true);
        try {
            for (const file of files.slice(0, 10)) {
                if (file.size > 25 * 1024 * 1024) continue;
                const base64 = await new Promise<string>((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
                    reader.onerror = reject;
                    reader.readAsDataURL(file);
                });
                await fetch(`${getApiBase()}/api/session/workspace/upload`, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        sessionId: wsSid,
                        filename: file.name,
                        content_base64: base64,
                        subpath: workspaceSubpathRef.current,
                    }),
                });
            }
            await refreshWorkspace();
        } catch { /* upload failed - list stays unchanged */ }
        finally { setWorkspaceUploading(false); }
    }, [currentSessionId, workspaceInfo?.path, refreshWorkspace]);
    const [workspaceDragOver, setWorkspaceDragOver] = useState(false);

    // Explorer-style navigation: history stack for the Back button, Up = parent folder
    const [workspaceNavHist, setWorkspaceNavHist] = useState<string[]>([]);
    const navigateWorkspace = useCallback((sub: string) => {
        setWorkspaceNavHist(prev => [...prev, workspaceSubpathRef.current]);
        refreshWorkspace(undefined, sub);
    }, [refreshWorkspace]);
    const workspaceGoBack = useCallback(() => {
        setWorkspaceNavHist(prev => {
            if (prev.length === 0) return prev;
            const next = [...prev];
            const target = next.pop() as string;
            refreshWorkspace(undefined, target);
            return next;
        });
    }, [refreshWorkspace]);
    // Central index: list all of the user's workspaces (server scopes to the authenticated user).
    const refreshAllWorkspaces = useCallback(async () => {
        try {
            const res = await fetch(`${getApiBase()}/api/workspaces`, { credentials: 'include' });
            if (!res.ok) return;
            const data = await res.json();
            setAllWorkspaces(Array.isArray(data?.workspaces) ? data.workspaces : []);
        } catch { /* backend unreachable - keep current */ }
    }, []);
    // Drill into any workspace from the index (incl. orphans) WITHOUT switching the active chat.
    const openWorkspace = useCallback((sid: string) => {
        workspaceSubpathRef.current = '';
        setWorkspaceNavHist([]);
        setWorkspaceView('folder');
        refreshWorkspace(sid, '');
    }, [refreshWorkspace]);
    const renameWorkspace = useCallback(async (sid: string, current: string) => {
        const label = window.prompt('Workspace name', current || '');
        if (label == null) return;
        try {
            await fetch(`${getApiBase()}/api/workspaces/rename`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sessionId: sid, label: label.trim() }),
            });
            await refreshAllWorkspaces();
        } catch { /* keep current */ }
    }, [refreshAllWorkspaces]);
    // Delete with confirmation dialog (target held in state while the dialog is open)
    const [workspaceDeleteTarget, setWorkspaceDeleteTarget] = useState<{ name: string; isDir: boolean; items?: number; kind?: 'workspace'; sessionId?: string } | null>(null);
    const [workspaceDeleting, setWorkspaceDeleting] = useState(false);
    const deleteWorkspaceEntry = useCallback(async () => {
        if (!workspaceDeleteTarget) return;
        setWorkspaceDeleting(true);
        try {
            if (workspaceDeleteTarget.kind === 'workspace' && workspaceDeleteTarget.sessionId) {
                // Delete a WHOLE workspace folder from the central index (orphans + manual cleanup).
                await fetch(`${getApiBase()}/api/workspaces/delete`, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sessionId: workspaceDeleteTarget.sessionId }),
                });
                await refreshAllWorkspaces();
            } else if (viewedWorkspaceSidRef.current ?? currentSessionId) {
                await fetch(`${getApiBase()}/api/session/workspace/delete`, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        sessionId: viewedWorkspaceSidRef.current ?? currentSessionId,
                        name: workspaceDeleteTarget.name,
                        subpath: workspaceSubpathRef.current,
                    }),
                });
                await refreshWorkspace();
            }
        } catch { /* delete failed - list stays unchanged */ }
        finally {
            setWorkspaceDeleting(false);
            setWorkspaceDeleteTarget(null);
        }
    }, [currentSessionId, workspaceDeleteTarget, refreshWorkspace, refreshAllWorkspaces]);
    // xraySection state removed - Context Window modal now shows only overview diagram

    // Agent Brain: working memory, plan, tasks, intent, team state
    type BrainTask = { text: string; status: 'pending' | 'done' };
    type BrainAgent = { task_id: string; agent_type: string; status: string; task: string; question: string; result: string };
    type BrainData = { intent: string; notes: string[]; plan: string[]; tasks: BrainTask[]; agents: BrainAgent[] };
    const [brainData, setBrainData] = useState<BrainData | null>(null);

    // Memoize filtered messages so typing in the input box doesn't re-process
    // all messages on every keystroke (parseContent + ReactMarkdown are expensive).
    // Recomputes only when messages or currentSessionId/sessions change.
    const filteredMessages = useMemo(() => {
        const isThinkingSession = (sessions.find(s => s.id === currentSessionId) as Session | undefined)?.source === 'thinking';
        return messages
            .filter(m => !String(m?.content ?? '').includes('__CMD__'))
            .filter(m => {
                if (!isThinkingSession) return true;
                if ((m.role === 'system' || m.role === 'user') && isThinkingModePrompt(String(m.content ?? ''))) return false;
                return true;
            });
    }, [messages, currentSessionId, sessions]);

    // Virtual window: show last N user-prompt pairs (user msg + all responses/tools until next user).
    // User can load earlier pairs by clicking the banner at the top.
    const MSG_TURNS = 8; // number of user prompts to keep visible
    const [msgOffset, setMsgOffset] = useState(0); // extra turns to reveal (added when clicking banner)
    const [expandedMsgs, setExpandedMsgs] = useState<Set<number>>(new Set());
    const toggleMsgExpanded = (idx: number) => setExpandedMsgs(prev => {
        const next = new Set(prev);
        next.has(idx) ? next.delete(idx) : next.add(idx);
        return next;
    });

    // Past long bot answers collapse to a preview once a NEWER user message exists (keeps the chat
    // scannable). This is derived at RENDER time from the message's position + length — NOT a Set of
    // array indices. The old index-based approach broke whenever a message was removed
    // (clear_last_assistant, dedup): the indices shifted onto the wrong bubble, so tiny replies
    // collapsed, long ones stayed open, and the streaming reply collapsed mid-stream. We only persist
    // the user's manual EXPAND choices, keyed by the stable message timestamp.
    const [expandedBotMsgs, setExpandedBotMsgs] = useState<Set<number>>(new Set());
    // Manual expand/collapse overrides for the per-turn actions-timeline, keyed by the assistant
    // message timestamp (stable, mirrors expandedBotMsgs). No entry → use the natural state
    // (expanded while the turn runs, collapsed once it answers / for history).
    const [timelineExpand, setTimelineExpand] = useState<Map<number, boolean>>(new Map());
    const BOT_COLLAPSE_THRESHOLD = 800; // chars — shorter replies stay fully visible
    const BOT_COLLAPSED_PREVIEW = 300;  // chars shown when collapsed
    // Reset offset when session changes so we always start at the bottom.
    useEffect(() => { setMsgOffset(0); }, [currentSessionId]);
    const visibleMessages = useMemo(() => {
        // Find indices of all user messages in filteredMessages
        const userIdxs = filteredMessages
            .map((m, i) => (m.role === 'user' ? i : -1))
            .filter(i => i >= 0);
        const totalTurns = userIdxs.length;
        const visibleTurns = MSG_TURNS + msgOffset;
        if (totalTurns <= visibleTurns) return filteredMessages; // everything fits
        // Cut from the (totalTurns - visibleTurns)-th user message onward
        const cutTurnIdx = totalTurns - visibleTurns;
        const start = userIdxs[cutTurnIdx];
        return filteredMessages.slice(start);
    }, [filteredMessages, msgOffset]);
    const MSG_PAGE_SIZE = MSG_TURNS; // alias used by navigator click handler
    const hiddenCount = filteredMessages.length - visibleMessages.length;

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
        agentKind: SubAgentKind | null;   // known at the tool CALL -> open the matching custom window at once
        status: string;
        presence: 'online' | 'idle' | 'error';
        currentFile: string;
        codeContent: string;
        artifactFile: string;
        artifactCode: string;
        artifactStatus: string;
        consoleLines: string[];
        steps: any[];
        browserFrame: string;
        browserUrl: string;
        // Coder VS-Code view: streamed by the coding agent as `coder_state`
        coder: {
            fileTree: Array<{ name: string; size: number; status: string }>;
            git: { branch: string; dirty: number; commits: Array<{ sha: string; when: string; msg: string }> };
            tasks: Array<{ title: string; status: string }>;
            loop: number;
            taskProgress: string;
            linterOk: boolean;
            projectName: string;
            projectPath: string;
            diffs?: Record<string, string>;
            activity?: string;
        } | null;
        // Research view: streamed by the research agent as `research_state`
        research: {
            topic: string;
            stage: string;
            sections: Array<{ title: string; status: string; words: number; targetWords: number }>;
            sectionsHtml: string[];
            sources: Array<{ url: string; title: string; domain: string }>;
            wordsTarget: number;
            loop: number;
        } | null;
        // Document view: streamed by the document agent as `document_state`
        document: {
            title: string;
            format: string;
            docType: string;
            stage: string;
            sections: Array<{ title: string; status: string; words: number; targetWords: number }>;
            sectionsHtml: string[];
            placeholders: Array<{ name: string; value: string; source: string }>;
            wordsTarget: number;
            savePath: string;
            loop: number;
        } | null;
        // Librarian read-only explorer view: streamed by the librarian agent as `librarian_state`
        librarian: {
            root: string;
            stage: string;
            readOnly: boolean;
            totalSize: number;
            totalFiles: number;
            totalFolders: number;
            entries: Array<{ name: string; type: string; sizeBytes: number; items?: number; gd?: boolean }>;
            topFolders: Array<{ name: string; sizeBytes: number }>;
            drives: Array<{ name: string; kind: 'disk' | 'home' | 'cloud'; usedBytes: number; totalBytes: number; connected?: boolean }>;
            search: { query: string; hits?: number | null } | null;
            activity: Array<{ cls: string; text: string }>;
            currentFolder: {
                path: string;
                name: string;
                fileCount: number;
                folderCount: number;
                totalSize: number;
                types: Array<{ type: string; count: number }>;
                entries: Array<{ name: string; type: string; isDir: boolean; sizeBytes: number; items?: number; modified: string; match?: boolean }>;
            } | null;
        } | null;
        // Browser agent live window: streamed as `browser_state` (+ the browserFrame screenshot)
        browser: {
            task: string;
            url: string;
            status: string;
            step: number;
            maxSteps: number;
            vision: string;
            actions: Array<{ verb: string; text: string; status: string }>;
            history: string[];
        } | null;
    }>({
        isOpen: false,
        agentName: "Sub-Agent",
        agentKind: null,
        status: "Idle",
        presence: "idle",
        currentFile: "",
        codeContent: "",
        artifactFile: "",
        artifactCode: "",
        artifactStatus: "Idle",
        consoleLines: [],
        steps: [],
        browserFrame: "",
        browserUrl: "",
        coder: null,
        research: null,
        document: null,
        librarian: null,
        browser: null,
    });

    // Document Editor: one state entry per session (like Viewer); includes content so unsaved edits survive chat switch.
    const [sessionEditorState, setSessionEditorState] = useState<Record<string, SessionEditorDocumentState>>({});
    const defaultEditorState = useMemo(
        () => ({ isOpen: false as const, filePath: '', title: 'Document', docxModel: null as NativeDocxDocument | null }),
        []
    );
    const documentEditorState = currentSessionId
        ? (sessionEditorState[currentSessionId] ?? defaultEditorState)
        : defaultEditorState;
    const setDocumentEditorState = useCallback((
        valueOrUpdater: SessionEditorDocumentState | ((prev: SessionEditorDocumentState) => SessionEditorDocumentState)
    ) => {
        if (!currentSessionId) return;
        setSessionEditorState(prev => {
            const current = prev[currentSessionId] ?? defaultEditorState;
            const next = typeof valueOrUpdater === 'function' ? valueOrUpdater(current) : valueOrUpdater;
            return { ...prev, [currentSessionId]: next };
        });
    }, [currentSessionId, defaultEditorState]);
    const setDocumentEditorStateForSession = useCallback((sessionId: string, valueOrUpdater: SessionEditorDocumentState | ((prev: SessionEditorDocumentState) => SessionEditorDocumentState)) => {
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
    // Attachment indexing status per session (for the Document Viewer header indicator + banner).
    const [attachmentIndexStatus, setAttachmentIndexStatus] = useState<Record<string, 'indexing' | 'ready' | 'error'>>({});
    const [attachmentIndexCount, setAttachmentIndexCount] = useState<Record<string, number>>({});
    const activeAttachmentIndexStatus = currentSessionId ? attachmentIndexStatus[currentSessionId] : undefined;
    const activeAttachmentIndexCount = currentSessionId ? attachmentIndexCount[currentSessionId] : undefined;
    // True while the LLM is indexing attached documents — blocks prompting + closing the
    // Document Viewer, and surfaces the stop button so the user can cancel.
    const isIndexing = activeAttachmentIndexStatus === 'indexing';
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

    // Code Viewer state (VS Code-like editor panel for .py/.js/.ts etc.)
    const [codeViewerState, setCodeViewerState] = useState<{ isOpen: boolean; filePath: string; title?: string; initialContent?: string; liveRefresh?: boolean; loadedContent?: string }>({
        isOpen: false, filePath: '',
    });

    // HTML Viewer state (native iframe preview for .html/.htm files)
    const [htmlViewerState, setHtmlViewerState] = useState<{ isOpen: boolean; filePath: string; title?: string; initialContent?: string }>({
        isOpen: false, filePath: '',
    });

    // Image Viewer state (dedicated viewer for image files — NOT the DocumentViewer,
    // and never synced as a sidebar document, so images are not RAG-indexed as text).
    // `description` holds the one-time vision description (shown in the viewer AND sent to
    // the agent each turn while the viewer is open, via imageViewerContext).
    const [imageViewerState, setImageViewerState] = useState<{ isOpen: boolean; filePath: string; title?: string; src?: string; description?: string; descLoading?: boolean }>({
        isOpen: false, filePath: '',
    });
    // Yellow-marked region the user drew in the Image Viewer to ask about. While set (and the
    // viewer is open) it rides every chat message as `markedRegion` so the backend runs vision
    // on that region for this question. Cleared via the chip ✕ (bumps markClearToken to reset
    // the viewer's drawing) or when the viewer closes.
    const [imageMark, setImageMark] = useState<ImageMark | null>(null);
    const [markClearToken, setMarkClearToken] = useState(0);
    const clearImageMark = useCallback(() => { setImageMark(null); setMarkClearToken(t => t + 1); }, []);

    // Open an image in the dedicated Image Viewer and (once) fetch its vision description so it
    // shows in the viewer AND stays in the agent's context while the viewer is open.
    const openImageInViewer = useCallback((path: string, name: string, src?: string) => {
        setImageViewerState({ isOpen: true, filePath: path, title: name, src, description: '', descLoading: true });
        setShowSubAgentPanel(true);
        if (!currentSessionId) { setImageViewerState(prev => ({ ...prev, descLoading: false })); return; }
        fetch(`${getApiBase()}/api/image/describe`, {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sessionId: currentSessionId, path }),
        })
            .then(r => r.ok ? r.json() : { description: '' })
            .then(d => setImageViewerState(prev => prev.filePath === path ? { ...prev, description: d?.description || '', descLoading: false } : prev))
            .catch(() => setImageViewerState(prev => prev.filePath === path ? { ...prev, descLoading: false } : prev));
    }, [currentSessionId]);

    // ── Workspace file actions (download + open in viewer) ──────────────────
    const workspaceFileAbsPath = useCallback((name: string) => {
        if (!workspaceInfo?.path) return '';
        const sub = workspaceInfo.subpath ? `/${workspaceInfo.subpath}` : '';
        return `${workspaceInfo.path}${sub}/${name}`;
    }, [workspaceInfo?.path, workspaceInfo?.subpath]);

    // Click a workspace file -> open it in the right panel AND make it visible to
    // the agent (synced as a sidebar document, exactly like attaching a file).
    // Documents (PDF, Office, Markdown, HTML, images, text) open in the
    // DocumentViewer; genuine code files open in the CodeViewer (whose content
    // already reaches the agent via the codeViewerFile chip on send).
    const openWorkspaceFile = useCallback(async (name: string) => {
        const full = workspaceFileAbsPath(name);
        if (!full) return;
        const ext = (name.split('.').pop() || '').toLowerCase();
        // Images open in the dedicated Image Viewer — never the DocumentViewer — and are
        // NOT synced as sidebar documents, so opening one never RAG-indexes it as text.
        if (isImageFile(name)) {
            openImageInViewer(full, name, `${getApiBase()}/api/file?path=${encodeURIComponent(full)}`);
            setIsWorkspaceModalOpen(false);
            return;
        }
        // Binary/office documents that need the DocumentViewer's rich rendering.
        // Text formats (.md/.txt/...) open in the CodeViewer instead (with a
        // Markdown preview toggle there), so every path opens them consistently.
        const docExts = new Set([
            'pdf', 'docx', 'xlsx', 'pptx', 'rtf', 'csv',
        ]);
        // HTML → HtmlViewer (rendered Preview/Source toggle), like the file-chip flow — not raw code.
        if (isHtmlFile(name)) {
            setHtmlViewerState({ isOpen: true, filePath: full, title: name });
            setShowSubAgentPanel(true);
            setIsWorkspaceModalOpen(false);
            return;
        }
        if (!docExts.has(ext) && (isCodeFile(name) || isTextFile(name))) {
            setCodeViewerState({ isOpen: true, filePath: full, title: name });
            setShowSubAgentPanel(true);
            setIsWorkspaceModalOpen(false);
            return;
        }
        try {
            const res = await fetch(`${getApiBase()}/api/file?path=${encodeURIComponent(full)}`);
            if (!res.ok) return;
            const blob = await res.blob();
            const mimeType = blob.type || 'application/octet-stream';
            const isImage = /^(png|jpe?g|gif|webp|svg|bmp|ico)$/.test(ext);
            const isBinaryDoc = /^(pdf|docx|xlsx|pptx)$/.test(ext);
            // The backend extracts the agent-visible text from `data` (base64), so
            // every opened doc carries `data`. The DocumentViewer additionally
            // renders binary docs from `data`, images/markdown/html from `content`.
            const dataUri: string = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(String(reader.result));
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            });
            let entry: DocumentViewerDoc;
            if (isBinaryDoc) {
                entry = { id: crypto.randomUUID(), name, mimeType, data: dataUri };
            } else if (isImage) {
                entry = { id: crypto.randomUUID(), name, mimeType, content: dataUri, data: dataUri };
            } else {
                entry = { id: crypto.randomUUID(), name, mimeType, content: await blob.text(), data: dataUri };
            }
            setDocumentViewerState(prev => {
                const newList = [...prev.documents.filter(d => d.name !== name), entry];
                // Sync to the backend so the agent sees the opened file's content
                // (same path as attaching/drag-drop). Without this it only knows
                // the filename and guesses at the path.
                if (ws && currentSessionId) {
                    ws.send(JSON.stringify({
                        type: 'set_sidebar_documents',
                        sessionId: currentSessionId,
                        documents: newList.filter(d => d.data).map(d => ({ name: d.name, data: d.data, mimeType: d.mimeType })),
                    }));
                    sidebarDocsSyncedForSessionRef.current = currentSessionId;
                }
                return { ...prev, isOpen: true, documents: newList };
            });
            setShowSubAgentPanel(true);
            setIsWorkspaceModalOpen(false);
        } catch { /* network/permission error - keep the workspace open */ }
    }, [workspaceFileAbsPath, setDocumentViewerState, ws, currentSessionId, openImageInViewer]);

    // Suggestion State
    const [suggestionList, setSuggestionList] = useState<any[]>([]);
    const [suggestionType, setSuggestionType] = useState<'tool' | 'workflow' | null>(null);
    const [selectedSuggestionIndex, setSelectedSuggestionIndex] = useState(0);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const ghostRef = useRef<HTMLDivElement>(null);   // inline-autocomplete mirror; kept scroll-synced with the textarea
    const suggestionListRef = useRef<HTMLDivElement>(null);

    // Re-focus input whenever the agent finishes generating
    useEffect(() => {
        if (!isGenerating) inputRef.current?.focus();
    }, [isGenerating]);

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
            // Load MCP server list + connection status (for the Advanced-tab "N connected" count)
            if (ws) {
                ws.send(JSON.stringify({ type: 'get_mcp_servers' }));
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
    // Holds completion-chime Audio elements until they finish, so the GC can't collect
    // a locally-scoped Audio mid-playback (which cut the sound off).
    const completionSoundsRef = useRef<Set<HTMLAudioElement>>(new Set());
    const pendingSttSendRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const scrollRef = useRef<HTMLDivElement>(null);
    const containerRef = useRef<HTMLDivElement>(null);
    const sessionScrollPositions = useRef<Record<string, { scrollTop: number; wasAtBottom: boolean }>>({});
    const isAtBottomRef = useRef(true);
    const pendingScrollRestore = useRef<{ scrollTop: number; wasAtBottom: boolean } | 'bottom' | null>(null);

    const artifactDirtyRef = useRef(false);
    const artifactLastEditRef = useRef(0);
    const artifactSendTimeoutRef = useRef<NodeJS.Timeout | null>(null);
    const subAgentStepsRef = useRef<Array<{ id: string; status: string; title?: string; description?: string }>>([]);
    const lastSubAgentStatusRef = useRef<string>('');
    const subAgentLogSetRef = useRef<Set<string>>(new Set());
    const subAgentAutoCloseRef = useRef<NodeJS.Timeout | null>(null);
    const subAgentManualOpenRef = useRef(false);
    const subAgentUserClosedRef = useRef(false);  // User explicitly closed panel - don't auto-reopen
    // Current task has a custom view (coder/research/browser): keep the window
    // closed until the first custom data arrives — never flash the generic window.
    const subAgentCustomViewRef = useRef(false);
    const subAgentOutputSetRef = useRef<Set<string>>(new Set());
    const [showSubAgentPanel, setShowSubAgentPanel] = useState(true);
    const [subAgentSheetOpen, setSubAgentSheetOpen] = useState(false);  // mobile: dock opened as a full-screen sheet
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
        // Dedup by content only (strip leading [HH:MM:SS] timestamp) so the same
        // message from repeated Rich TUI re-renders doesn't appear multiple times.
        const contentKey = line.replace(/^\[\d{2}:\d{2}:\d{2}\] /, '');
        if (subAgentLogSetRef.current.has(contentKey)) return;
        subAgentLogSetRef.current.add(contentKey);
        const lineLower = line.toLowerCase();
        const isFailure = lineLower.includes('failed') || lineLower.includes('timeout') || lineLower.includes('error');
        setSubAgentState(prev => {
            const lines = [...prev.consoleLines];
            // Streaming TUI redraws emit progressively longer versions of the
            // same line ("ent", "ent navigation.", "ent navigation. The …").
            // Replace the previous fragment in place instead of stacking them.
            const last = lines[lines.length - 1];
            if (last) {
                const lastKey = last.replace(/^\[\d{2}:\d{2}:\d{2}\] /, '');
                if (lastKey.length >= 4 && contentKey.startsWith(lastKey)) {
                    lines[lines.length - 1] = line;
                    return {
                        ...prev,
                        consoleLines: lines.slice(-500),
                        ...(isFailure ? { status: line.trim().slice(0, 120) } : {})
                    };
                }
                if (contentKey.length >= 4 && lastKey.startsWith(contentKey)) {
                    return prev; // shorter fragment of what is already shown
                }
            }
            lines.push(line);
            return {
                ...prev,
                consoleLines: lines.slice(-500),
                ...(isFailure ? { status: line.trim().slice(0, 120) } : {})
            };
        });
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
            const container = containerRef.current;
            if (container) {
                sessionScrollPositions.current[currentSessionId] = {
                    scrollTop: container.scrollTop,
                    wasAtBottom: isAtBottomRef.current,
                };
            }
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
        setUnreadSessions(prev => { const next = new Set(prev); next.delete(id); return next; });
        expectNewAssistantRef.current = false;
        lastUserSendTimeRef.current = 0;
        expectNewAssistantAfterToolRef.current = false;
        const cached = sessionCache.current[id] || [];
        setMessages(cached);
        // Per-turn ephemeral state belongs to the previous session, not this one - clear it so a
        // stale RAG-snippets badge / context X-ray cannot linger across a switch (defense-in-depth
        // for user isolation; the backend now scopes these pushes to the owner). Repopulates on the
        // next turn of this session.
        setRagResults(null);
        setRealContext(null);
        pendingScrollRestore.current = sessionScrollPositions.current[id] ?? 'bottom';

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

        // 5. Request sync — show a loading spinner only when there's nothing cached AND we have not
        //    already synced this session (a new/empty chat has nothing to load → no spinner flash).
        setHistoryLoading(cached.length === 0 && !syncedSessions.current.has(id));
        ws?.send(JSON.stringify({ type: 'load_session', id }));
    };

    // Keep the selected chat visible in the sidebar (don't jump to the top)
    useEffect(() => {
        if (!currentSessionId || !sidebarListRef.current) return;
        const el = sidebarListRef.current.querySelector(`[data-session-id="${currentSessionId}"]`);
        if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }, [currentSessionId]);

    const [reconnectAttempt, setReconnectAttempt] = useState(0);
    const [drawerOpen, setDrawerOpen] = useState(false);  // mobile sidebar drawer (max-md only; desktop keeps the hover-rail)
    useEffect(() => {
        if (typeof window === 'undefined') return;
        // Only open the socket once authenticated — opening with an absent/expired token makes the
        // backend reject the handshake, which (with reconnects) would otherwise hammer /ws.
        if (authChecking || !isAuthenticated) return;
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
            const token = localStorage.getItem('vaf_token');
            if (token) {
                wsUrl += (wsUrl.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
            }
            const socket = new WebSocket(wsUrl);
            wsSocketRef.current = socket;
            let opened = false;
            socket.onopen = () => {
            opened = true;
            setStatus('connected');
            socket.send(JSON.stringify({ type: 'get_sessions' }));
            socket.send(JSON.stringify({ type: 'get_config' }));
            socket.send(JSON.stringify({ type: 'get_models' }));
            socket.send(JSON.stringify({ type: 'get_workflows' })); // Fetch workflows for autocomplete
            socket.send(JSON.stringify({ type: 'get_skills' }));    // Fetch skills (second routing tier)
            socket.send(JSON.stringify({ type: 'get_tools' }));     // Fetch tools for reference
            socket.send(JSON.stringify({ type: 'speaker_profile_get' })); // Voice profile (call button offers enrollment without one)
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
                    // Exception: events that intentionally target other sessions must pass
                    // through so the UI can react (show an unread badge, append proactive
                    // automation results, etc.) instead of being silently dropped.
                    const crossSessionAllowed = (
                        data.type === 'session_list' ||
                        data.type === 'history_update' ||
                        data.type === 'contact_reply_pending' ||
                        data.type === 'session_unread' ||
                        data.type === 'agent_message_append'
                    );
                    if (!crossSessionAllowed) {
                        console.log(`🔍 [FILTER] Rejecting ${data.type}: backend=${data.sessionId}, frontend=${activeSessionId}`);
                        return;
                    }
                }

                if (data.type === 'new_log') {
                    const entry = data.entry || {};
                    const src = String(entry.source || "");
                    const rawMsg = String(entry.message || "");
                    const msgLower = rawMsg.toLowerCase();
                    const srcLower = src.toLowerCase();
                    const isSubAgentLog =
                        msgLower.includes('sub-agent') ||
                        msgLower.includes('subagent') ||
                        srcLower.includes('sub-agent') ||
                        srcLower.includes('subagent');
                    if (isSubAgentLog) {
                        const timeStamp = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                        appendSubAgentLine(`[${timeStamp}] ${rawMsg}`);
                        // Only open if not already open - avoids duplicate open from tool_update + domain_log.
                        // Custom-view tasks wait for their custom data instead of the generic window.
                        if (!subAgentStateRef.current.isOpen && !subAgentCustomViewRef.current) {
                            openSubAgentWindow(false);
                        }
                    } else if (subAgentState.isOpen && (src === 'System' || src === 'Info') && rawMsg) {
                        // Suppress repetitive generic runner lines; detailed progress comes via subagent_update.
                        if (/^running:\s*research agent\b/i.test(rawMsg.trim())) return;
                        const timeStamp = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
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
                            return next.length > MAX_LIVE_MESSAGES ? next.slice(-MAX_LIVE_MESSAGES) : next;
                        });
                    }
                }
                else if (data.type === 'tool_update') {
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;

                    const { subType, toolId, name, data: eventData, timestamp } = data;
                    const toolName = String(name || '').toLowerCase();
                    const isSubAgentTool = /(?:^|[^a-z])(librarian|research|document|coding|browser)_agent(?:$|[^a-z])/.test(toolName);

                    // Track active tool for loading bubble + the avatar's tool animation. The mode is held
                    // for at least TOOL_ANIM_MIN_MS so a fast tool's animation stays readable (see ref above).
                    if (subType === 'start') {
                        if (toolModeClearTimerRef.current) { clearTimeout(toolModeClearTimerRef.current); toolModeClearTimerRef.current = null; }
                        toolModeStartRef.current = Date.now();
                        setActiveToolName(String(name || '').replace(/_/g, ' '));
                        setActiveToolMode(toolAvatarMode(String(name || '')));
                    } else if (subType === 'end' || subType === 'error') {
                        if (toolModeClearTimerRef.current) { clearTimeout(toolModeClearTimerRef.current); toolModeClearTimerRef.current = null; }
                        const release = () => { setActiveToolName(''); setActiveToolMode(null); toolModeClearTimerRef.current = null; };
                        const elapsed = Date.now() - toolModeStartRef.current;
                        if (elapsed >= TOOL_ANIM_MIN_MS) release();
                        else toolModeClearTimerRef.current = setTimeout(release, TOOL_ANIM_MIN_MS - elapsed);
                    }

                    if (subType === 'start' && isSubAgentTool) {
                        lastSubAgentStatusRef.current = '';
                        subAgentUserClosedRef.current = false;  // New task - user wants to see it
                        // The main agent CALLS the sub-agent by name -> we know the kind right now and open
                        // the matching custom window immediately (it renders a loading shell until data
                        // streams), instead of showing the generic console first.
                        const startKind = subAgentKindFromName(String(name || ''));
                        subAgentCustomViewRef.current = startKind !== null;  // now correct for all 5 kinds
                        openSubAgentWindow(false);  // no-op during a workflow (workflow guard inside)
                        const title = String(name || 'Sub-Agent').replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
                        setSubAgentState(prev => ({
                            ...prev,
                            agentKind: startKind,
                            status: 'Running...',
                            presence: 'online',
                            // Clear ALL stale per-view state on each new task start — a re-run (or a
                            // different kind) must never show the previous run's data through the new gate.
                            browserFrame: '',
                            browserUrl: '',
                            consoleLines: [],
                            coder: null,
                            research: null,
                            document: null,
                            librarian: null,
                            browser: null,
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
                        const timeStamp = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                        const statusLabel = subType === 'start' ? 'Start' : subType === 'end' ? 'End' : 'Error';
                        const payload = eventData ? ` - ${eventData}` : '';
                        appendSubAgentLine(`[${timeStamp}] ${statusLabel}: ${name}${payload}`);
                    }

                    setMessages(prev => {
                        // Check if tool message exists
                        const existingIdx = prev.findIndex(m => m.toolId === toolId);

                        if (subType === 'start') {
                            if (existingIdx !== -1) return prev; // Duplicate start
                            const appended: Message[] = [...prev, {
                                role: 'tool',
                                content: '', // Result empty at start
                                timestamp: Date.now(),
                                toolId: toolId,
                                toolName: name,
                                toolArgs: eventData, // Arguments passed in data
                                toolStatus: 'running',
                                toolStartTime: Date.now()
                            }];
                            return appended.length > MAX_LIVE_MESSAGES ? appended.slice(-MAX_LIVE_MESSAGES) : appended;
                        }
                        else if (subType === 'end' || subType === 'error') {
                            let resolvedIdx = existingIdx;
                            if (resolvedIdx === -1) {
                                // toolId was lost on history_update rebuild — fall back to last
                                // running tool message with the same name
                                const fallback = prev
                                    .map((m, i) => ({ m, i }))
                                    .filter(({ m }) => m.role === 'tool' && m.toolName === name && m.toolStatus === 'running')
                                    .pop();
                                resolvedIdx = fallback?.i ?? -1;
                            }
                            if (resolvedIdx === -1) return prev; // truly not found
                            expectNewAssistantAfterToolRef.current = true;
                            const newMessages = [...prev];
                            newMessages[resolvedIdx] = {
                                ...newMessages[resolvedIdx],
                                toolId: toolId, // restore toolId in case it was lost
                                toolStatus: subType === 'error' ? 'error' : 'completed',
                                content: eventData,
                                toolEndTime: Date.now()
                            };
                            return newMessages;
                        }
                        return prev;
                    });

                    // Flash the main agent avatar with the tool's outcome. A plan-gate / policy block
                    // arrives as the tool RESULT ([PLAN REQUIRED] / Security Error) -> `blocked` (bumps a wall).
                    if (subType === 'end' || subType === 'error') {
                        const res = String(eventData ?? '');
                        if (res.includes('[PLAN REQUIRED]') || res.startsWith('Security Error:')) {
                            fireAgentReaction('blocked');
                        } else {
                            fireAgentReaction(subType === 'error' ? 'error' : 'success');
                        }
                    }

                    // When execute_workflow finishes, force-close any workflow panel still in
                    // 'running' state — workflow_done may have been lost in the WebSocket queue
                    if ((subType === 'end' || subType === 'error') && name === 'execute_workflow') {
                        const wfStore = useWorkflowStore.getState();
                        if (wfStore.workflow && wfStore.workflow.status === 'running') {
                            const finalStatus = subType === 'error' ? 'failed' : 'completed';
                            wfStore.workflow.steps.forEach((step: { id: string; status: string }) => {
                                if (step.status === 'idle' || step.status === 'running') {
                                    updateStepStatus(step.id, finalStatus as 'failed' | 'success', 100, undefined);
                                }
                            });
                            useWorkflowStore.setState(s => ({
                                workflow: s.workflow ? { ...s.workflow, status: finalStatus as VAFWorkflow['status'] } : null
                            }));
                        }
                    }

                    // Clear status message when tool runs
                    setStatusMessage('');

                    // Drive agent-cursor animation
                    if (toolName === 'learn_attached_knowledge') {
                        if (subType === 'start') {
                            window.dispatchEvent(new CustomEvent('agent-cursor', { detail: { phase: 'start' } }));
                        } else if (subType === 'end' || subType === 'error') {
                            window.dispatchEvent(new CustomEvent('agent-cursor', { detail: { phase: 'end' } }));
                        }
                    } else if (subType === 'start') {
                        if (toolName === 'create_workflow') {
                            let args: Record<string, unknown> = {};
                            try { args = JSON.parse(eventData || '{}'); } catch { /* empty */ }
                            window.dispatchEvent(new CustomEvent('agent-cursor', {
                                detail: { phase: 'tool-sequence', tool: toolName, args }
                            }));
                        }
                    }
                }
                else if (data.type === 'cursor_animation') {
                    window.dispatchEvent(new CustomEvent('agent-cursor', { detail: data }));
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
                else if (data.type === 'speaker_confirm_pending') {
                    // Every field forwarded explicitly (unforwarded = silently dropped)
                    setPendingSpeakerConfirms(prev => [...prev.filter(p => p.confirmId !== data.confirmId), {
                        confirmId: data.confirmId,
                        question: data.question || '',
                        audioPath: data.audioPath || '',
                        score: data.score,
                    }]);
                }
                else if (data.type === 'speaker_confirm_result' && data.confirmId) {
                    setPendingSpeakerConfirms(prev => prev.filter(p => p.confirmId !== data.confirmId));
                }
                else if (data.type === 'gate_required') {
                    setGateRequest({ tool: data.tool, cwd: data.cwd || '', reason: data.reason || '', args_preview: data.args_preview || '' });
                }
                else if (data.type === 'gate_decision') {
                    setGateRequest(null);
                }
                else if (data.type === 'context_status') {
                    // Merge into existing contextStats so partial updates (e.g. load_session
                    // sending only user_turn_count) don't wipe token counts set by 'stats'.
                    setContextStats((prev: any) => ({ ...(prev || {}), ...data.stats }));
                }
                else if (data.type === 'real_context_payload') {
                    setRealContext(data);
                }
                else if (data.type === 'rag_results') {
                    setRagResults(data);
                }
                else if (data.type === 'attachment_indexing' || data.type === 'attachment_indexed' || data.type === 'attachment_index_error' || data.type === 'attachment_index_cancelled') {
                    // Attachment lane indexing status -> Document Viewer header indicator + banner.
                    const sid: string | undefined = data.sessionId;
                    if (sid) {
                        if (data.type === 'attachment_index_cancelled') {
                            // User cancelled via the stop button — drop the status entirely so the
                            // banner disappears and prompting/closing unblocks immediately.
                            setAttachmentIndexStatus(prev => {
                                const next = { ...prev }; delete next[sid]; return next;
                            });
                            setAttachmentIndexCount(prev => {
                                const next = { ...prev }; delete next[sid]; return next;
                            });
                        } else {
                            const status: 'indexing' | 'ready' | 'error' =
                                data.type === 'attachment_indexing' ? 'indexing'
                                    : data.type === 'attachment_indexed' ? 'ready' : 'error';
                            setAttachmentIndexStatus(prev => ({ ...prev, [sid]: status }));
                            if (status === 'indexing' && typeof data.count === 'number') {
                                setAttachmentIndexCount(prev => ({ ...prev, [sid]: data.count }));
                            }
                            // Auto-clear the transient 'ready'/'error' so the header returns to neutral.
                            if (status !== 'indexing') {
                                setTimeout(() => {
                                    setAttachmentIndexStatus(prev => {
                                        if (prev[sid] !== status) return prev;
                                        const next = { ...prev };
                                        delete next[sid];
                                        return next;
                                    });
                                }, status === 'ready' ? 4000 : 8000);
                            }
                        }
                    }
                }
                else if (data.type === 'agent_message_update') {
                    // CRITICAL: Only update if this message belongs to the current session!
                    // If user switched chats while bot was typing, ignore this update.
                    const activeSessionId = currentSessionIdRef.current;
                    if (!activeSessionId && data.sessionId) {
                        setCurrentSessionId(data.sessionId);
                        wsSocketRef.current?.send(JSON.stringify({ type: 'load_session', id: data.sessionId }));
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

                    // Same rules as inside setMessages: detect streaming updates to the existing assistant bubble.
                    const prevMsgs = messagesRef.current;
                    const lastMsg = prevMsgs[prevMsgs.length - 1];
                    const expectNew = expectNewAssistantRef.current;
                    const expectAfterTool = expectNewAssistantAfterToolRef.current;
                    const tSend = lastUserSendTimeRef.current;
                    const withinUserSendWindow = tSend && (Date.now() - tSend < 1500);
                    const forceAppend = expectNew || expectAfterTool || withinUserSendWindow;
                    const inPlaceAssistantStream = !!(lastMsg && lastMsg.role === 'assistant' && !forceAppend);

                    setLoading(false);
                    // Only mark "generating" when starting a new assistant bubble. Re-applying full text
                    // after stream (or out-of-order vs message_complete) was leaving Stop stuck on.
                    // Also skip if user just clicked Stop — late-queued WS events must not re-arm isGenerating.
                    if (!inPlaceAssistantStream && !isStoppingGenerationRef.current) {
                        setIsGenerating(true);
                        setStatusMessage('');
                        setActiveToolName('');
                        if (activeSessionId) {
                            sessionLoadingStates.current[activeSessionId] = {
                                loading: false,
                                isGenerating: true,
                                statusMessage: '',
                                loadingMessageId: null
                            };
                        }
                    }

                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        const expectNewInner = expectNewAssistantRef.current;
                        if (expectNewInner) expectNewAssistantRef.current = false;
                        const expectAfterToolInner = expectNewAssistantAfterToolRef.current;
                        if (expectAfterToolInner) expectNewAssistantAfterToolRef.current = false;
                        // Within 1.5s of user send: force append once (handles ms-level race before state has user msg)
                        const t = lastUserSendTimeRef.current;
                        const withinUserSendWindowInner = t && (Date.now() - t < 1500);
                        if (withinUserSendWindowInner) lastUserSendTimeRef.current = 0;
                        const forceAppendInner = expectNewInner || expectAfterToolInner || withinUserSendWindowInner;
                        if (last && last.role === 'assistant' && !forceAppendInner) {
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
                else if (data.type === 'agent_message_append') {
                    // Proactive, COMPLETE standalone message (e.g. an automation result).
                    // Always append it as its own new bubble — never stream/merge in-place.
                    const activeSessionId = currentSessionIdRef.current;
                    const content = String(data.content ?? '');
                    if (content.trim().length === 0) return;

                    // No active session yet: adopt the target session and load it from disk
                    // (the message was already persisted server-side, so it will appear).
                    if (!activeSessionId && data.sessionId) {
                        setCurrentSessionId(data.sessionId);
                        wsSocketRef.current?.send(JSON.stringify({ type: 'load_session', id: data.sessionId }));
                        return;
                    }
                    // Targets a different session: surface an unread badge, don't inject here.
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) {
                        setUnreadSessions(prev => new Set(prev).add(data.sessionId));
                        return;
                    }
                    // Active session: append a fresh bubble. Do NOT touch isGenerating —
                    // this is a finished message, not a live turn.
                    setLoading(false);
                    setMessages(prev => [...prev, { role: data.role || 'assistant', content, timestamp: Date.now(), kind: data.kind }]);
                }
                else if (data.type === 'clear_last_assistant') {
                    // Remove faulty assistant message so only the retry response is shown (empty-response retry).
                    // We must remove the last *assistant* message, not the last message: the "Empty response..."
                    // system log is often appended before this event, so the last message can be system.
                    const activeSessionId = currentSessionIdRef.current;
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    setMessages(prev => {
                        const lastAssistantEntry = prev.map((m, i) => ({ m, i })).filter(({ m }) => m.role === 'assistant').pop();
                        if (!lastAssistantEntry) return prev;
                        const { m: lastAssMsg, i: lastAssistantIdx } = lastAssistantEntry;
                        // Guard: do NOT clear a completed response from a previous turn.
                        // If the agent fails before streaming any text in the current turn,
                        // there is no streaming bubble to clear — without this guard, repeated
                        // retries would erase previous-turn assistant messages one by one.
                        // lastUserSendTimeRef is non-zero only while waiting for the first
                        // streaming chunk (reset to 0 once streaming starts, so mid-stream
                        // retries are always allowed through).
                        const lastSendTime = lastUserSendTimeRef.current;
                        if (lastSendTime && (lastAssMsg.timestamp ?? 0) < lastSendTime - 500) return prev;
                        return prev.slice(0, lastAssistantIdx).concat(prev.slice(lastAssistantIdx + 1));
                    });
                }
                else if (data.type === 'tts_audio') {
                    console.log('[TTS] Received tts_audio, audio length:', data.audio?.length);
                    // Stop any current audio
                    if (currentAudioRef.current) {
                        currentAudioRef.current.pause();
                    }

                    // Play new audio. Decode base64 into a Blob object URL instead of a
                    // data: URI - cloud TTS answers can be 10+ MB WAV and giant data
                    // URIs are slow/fragile as Audio sources.
                    const byteString = atob(data.audio);
                    const byteArray = new Uint8Array(byteString.length);
                    for (let i = 0; i < byteString.length; i++) {
                        byteArray[i] = byteString.charCodeAt(i);
                    }
                    const audioBlob = new Blob([byteArray], { type: 'audio/wav' });
                    const audioSrc = URL.createObjectURL(audioBlob);
                    console.log('[TTS] Creating Audio element from blob, bytes:', byteArray.length);
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
                        URL.revokeObjectURL(audioSrc);
                    };

                    audio.onerror = (e) => {
                        console.error("[TTS] Audio playback error", e);
                        setPlayingMessageId(null);
                        setLoadingMessageId(null);
                        currentAudioRef.current = null;
                        URL.revokeObjectURL(audioSrc);
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
                        setIsStoppingGeneration(false);
                        // Final-answer punctuation reaction: a trailing '?' replays the "permission" asking
                        // animation, a trailing '!' the "exclaim" twin — each for 3s — to punctuate the close.
                        try {
                            const finalContent = String(data.content ?? '')
                                || (messagesRef.current.filter(m => m.role === 'assistant').pop()?.content ?? '');
                            const lastCh = parseThinkBlocks(finalContent).answer.trim().slice(-1);
                            if (lastCh === '?') fireAgentReaction('permission', 3000);
                            else if (lastCh === '!') fireAgentReaction('exclaim', 3000);
                        } catch { /* cosmetic only */ }
                        // Attach any pending file_created chips to the last assistant message bubble
                        setCreatedFiles(pending => {
                            if (pending.length > 0) {
                                setMessages(prev => {
                                    const lastAssistantIdx = prev.map((m, i) => ({ m, i })).filter(({ m }) => m.role === 'assistant').pop()?.i;
                                    if (lastAssistantIdx === undefined) return prev;
                                    const newMsgs = [...prev];
                                    const existing = newMsgs[lastAssistantIdx].downloadFiles || [];
                                    const toAdd = pending.filter(f => !existing.some(e => e.path === f.path)).map(f => ({ path: f.path, name: f.name }));
                                    if (toAdd.length === 0) return prev;
                                    newMsgs[lastAssistantIdx] = { ...newMsgs[lastAssistantIdx], downloadFiles: [...existing, ...toAdd] };
                                    return newMsgs;
                                });
                            }
                            return []; // Clear floating chips
                        });
                    }
                    // Completion sound: play when model has finished (Web UI only)
                    // Use same-origin relative URL so it works through HTTPS proxy (no mixed content)
                    try {
                        const soundUrl = '/sounds/tts01.mp3';
                        const audio = new Audio(soundUrl);
                        audio.volume = 0.6;
                        // Retain a reference until playback ends, otherwise the GC can
                        // collect this locally-scoped Audio mid-play and cut the sound off.
                        completionSoundsRef.current.add(audio);
                        const release = () => completionSoundsRef.current.delete(audio);
                        audio.addEventListener('ended', release);
                        audio.addEventListener('error', release);
                        audio.play().catch(() => { release(); /* autoplay policy / user mute */ });
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
                else if (data.type === 'session_unread') {
                    const sid = data.sessionId as string;
                    if (sid && sid !== currentSessionIdRef.current) {
                        setUnreadSessions(prev => new Set(prev).add(sid));
                    }
                }
                else if (data.type === 'session_list') {
                    setSessions(data.sessions);

                    // Only auto-create if we have NO sessions and NO active session selected
                    if (data.sessions.length === 0 && !activeSessionId) {
                        wsSocketRef.current?.send(JSON.stringify({ type: 'new_session' }));
                        return;
                    }

                    // Auto-select latest if none selected (initial load)
                    // or if the current session no longer exists in the list.
                    // NOTE: send via wsSocketRef — the `ws` STATE captured by this
                    // onmessage closure is still null on the first connect, which
                    // silently dropped the initial load_session (chat stayed empty
                    // until the user switched away and back).
                    if (data.sessions.length > 0) {
                        const sessionIds = new Set(data.sessions.map((s: Session) => s.id));
                        if (!activeSessionId || !sessionIds.has(activeSessionId)) {
                            setCurrentSessionId(data.sessions[0].id);
                            wsSocketRef.current?.send(JSON.stringify({ type: 'load_session', id: data.sessions[0].id }));
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

                    // Fresh run: drop any stale browser frame and re-arm the tiled live view.
                    setBrowserTileClosed(false);
                    setSubAgentState(prev => ({ ...prev, browserFrame: '', browserUrl: '' }));

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
                else if (data.type === 'workflow_done') {
                    if (data.sessionId && currentSessionId && data.sessionId !== currentSessionId) return;
                    // Mark all pending steps as failed/done so the store triggers auto-close
                    const wfStore = useWorkflowStore.getState();
                    if (wfStore.workflow) {
                        const finalStatus = data.success ? 'success' : 'failed';
                        wfStore.workflow.steps.forEach(step => {
                            if (step.status === 'idle' || step.status === 'running') {
                                updateStepStatus(step.id, finalStatus, 100, undefined);
                            }
                        });
                        // Force workflow status update so auto-close fires
                        useWorkflowStore.setState(s => ({
                            workflow: s.workflow ? { ...s.workflow, status: data.success ? 'completed' : 'failed' } : null
                        }));
                    }
                    // Browser is done with the workflow → remove the tiled live view.
                    setSubAgentState(prev => ({ ...prev, browserFrame: '', browserUrl: '' }));
                }
                else if (data.type === 'workflow_output_stream') {
                    if (data.sessionId && currentSessionId && data.sessionId !== currentSessionId) return;
                    const line = typeof data.line === 'string' ? data.line : '';
                    appendWorkflowLine(line);
                }
                else if (data.type === 'document_ready') {
                    // Read the session from the ref, not the closure: onmessage is bound once
                    // per socket, so the captured currentSessionId is stale (null on first
                    // connect) and the strict equality below would silently drop the event.
                    const activeSid = currentSessionIdRef.current;
                    const sid = data.sessionId || activeSid;
                    const fp = data.filePath || '';
                    // Office/HTML documents open in the DocumentEditor; text formats
                    // (.md/.txt/...) open in the CodeViewer (with a Markdown preview
                    // toggle) so every path routes them the same way.
                    const docExts = ['html', 'htm', 'docx', 'xlsx', 'pptx', 'rtf'];
                    const ext = (fp.split('/').pop() || '').split('.').pop()?.toLowerCase() || '';
                    const isDocumentFile = docExts.includes(ext);
                    if (sid) {
                        if (!isDocumentFile && (isCodeFile(fp) || isTextFile(fp))) {
                            // Code files → CodeViewer (not DocumentEditor)
                            if (sid === activeSid) {
                                setCodeViewerState({ isOpen: true, filePath: fp, title: data.title || fp.split('/').pop() || 'Code' });
                                setShowSubAgentPanel(true);
                                setDocumentViewerStateForSession(sid, (prev) => ({ ...prev, isOpen: false }));
                                setDocumentEditorStateForSession(sid, (prev) => ({ ...prev, isOpen: false }));
                            }
                        } else {
                            // Document files → DocumentEditor as before
                            setDocumentEditorStateForSession(sid, {
                                isOpen: true,
                                filePath: fp,
                                title: data.title || 'Document',
                                content: undefined,
                                docxModel: null,
                            });
                            if (sid === activeSid) {
                                setShowSubAgentPanel(true);
                                setDocumentViewerStateForSession(sid, (prev) => ({ ...prev, isOpen: false }));
                            }
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
                        if (!cur) return prev;
                        if (cur.docxModel) {
                            const nextModel = replaceTextInNativeDocx(cur.docxModel, start, end, newText);
                            return {
                                ...prev,
                                [sid]: {
                                    ...cur,
                                    docxModel: nextModel,
                                    content: flattenNativeDocxText(nextModel),
                                },
                            };
                        }
                        if (!cur.content) return prev;
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
                            documents: contents.map((c, i) => {
                                const prevDoc = prev.documents[i];
                                // Keep locally-loaded clean content (e.g. a workspace-opened
                                // markdown file) for DISPLAY: the backend's extracted text is
                                // LLM-formatted ("## File: <tmpname>" + a ``` code fence) and
                                // would render as a code block. Normal attachments have no
                                // local content, so they fall back to the backend extraction.
                                const localContent = prevDoc?.name === c.name
                                    && typeof prevDoc?.content === 'string' && prevDoc.content.length > 0
                                    ? prevDoc.content : null;
                                return {
                                    ...(prevDoc || {}),
                                    id: (prev.documents.length === contents.length && prevDoc?.name === c.name && prevDoc?.id)
                                        ? prevDoc.id
                                        : `doc-${i}-${crypto.randomUUID().slice(0, 8)}`,
                                    name: c.name,
                                    content: localContent ?? c.content ?? prevDoc?.content ?? '',
                                    ...(c.data != null && { data: c.data }),
                                    ...(c.mimeType != null && { mimeType: c.mimeType }),
                                    ...(typeof c.htmlContent === 'string' && c.htmlContent.length > 0 && { htmlContent: c.htmlContent }),
                                };
                            }),
                        };
                    };
                    if (sid) setDocumentViewerStateForSession(sid, updater);
                    else setDocumentViewerState(updater);
                    if (contents.length > 0) setShowSubAgentPanel(true);
                }
                else if (data.type === 'sidebar_documents_restored') {
                    // Server sends previously-saved sidebar documents when a session is loaded
                    // (e.g. after page refresh). Only restore if the sidebar is currently empty
                    // so we don't overwrite a freshly-attached document.
                    const contents = (data.contents || []) as Array<{ name: string; content: string; mimeType?: string }>;
                    const sid = data.sessionId || activeSessionId;
                    if (contents.length > 0) {
                        const updater = (prev: { isOpen: boolean; documents: DocumentViewerDoc[] }) => {
                            if (prev.documents.length > 0) return prev; // already has docs, don't overwrite
                            return {
                                isOpen: true,
                                documents: contents.map((c, i) => ({
                                    id: `doc-${i}-${crypto.randomUUID().slice(0, 8)}`,
                                    name: c.name,
                                    content: c.content ?? '',
                                    ...(c.mimeType != null && { mimeType: c.mimeType }),
                                })),
                            };
                        };
                        if (sid) setDocumentViewerStateForSession(sid, updater);
                        else setDocumentViewerState(updater);
                        setShowSubAgentPanel(true);
                    }
                }
                else if (data.type === 'subagent_update') {
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    const statusText = String(data.status || '').trim();
                    const modelLabel = data.model ? `• ${String(data.model)}` : '';
                    const statusLine = `${statusText}${modelLabel ? ` ${modelLabel}` : ''}`.trim();
                    const incomingSteps = Array.isArray(data.steps) ? data.steps.filter((s: unknown) => s && typeof s === 'object') : [];
                    const prevSteps = Array.isArray(subAgentStepsRef.current) ? subAgentStepsRef.current : [];
                    // Keep previous step list if this update carries only status text.
                    // Otherwise, status-only updates clear steps and cause repeated generic "Running ..." entries.
                    let newSteps: any[] = incomingSteps.length > 0 ? incomingSteps : [...prevSteps];
                    // When agent signals idle/completed, mark any leftover "running" steps as completed
                    // so the stop button hides and the UI reflects the finished state.
                    const incomingPresence = String(data.presence || '').trim().toLowerCase();
                    if (incomingPresence === 'idle' || incomingPresence === 'error') {
                        const hasRunning = newSteps.some((s: any) => s?.status === 'running');
                        if (hasRunning) {
                            newSteps = newSteps.map((s: any) =>
                                s?.status === 'running'
                                    ? { ...s, status: incomingPresence === 'error' ? 'failed' : 'completed' }
                                    : s
                            );
                        }
                    }
                    newSteps = newSteps.filter((s: any) => s && typeof s === 'object');
                    const prevMap = new Map(
                        prevSteps.map((step: { id?: string; status?: string }) => [step.id ?? '', step.status])
                    );
                    const statusLines: string[] = [];

                    if (incomingSteps.length > 0) {
                        newSteps.forEach((step: any) => {
                            if (!step || typeof step !== 'object') return;
                            const sid = step.id ?? '';
                            const prevStatus = prevMap.get(sid);
                            if (!prevStatus || prevStatus !== step.status) {
                                const label = step.status === 'completed'
                                    ? 'Completed'
                                    : step.status === 'running'
                                        ? 'Running'
                                        : 'Pending';
                                const detail = step.description ? ` - ${step.description}` : '';
                                const stitle = step.title != null ? String(step.title) : 'Step';
                                statusLines.push(`${label}: ${stitle}${detail}`);
                            }
                        });
                    }

                    if (statusLines.length > 0) {
                        const timeStamp = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                        statusLines.forEach(line => appendSubAgentLine(`[${timeStamp}] ${line}`));
                    }

                    subAgentStepsRef.current = newSteps;
                    const statusLower = String(data.status || '').toLowerCase();
                    const isGenericHeartbeatStatus = statusLower.startsWith('running sub-agent tasks');
                    const isDetailedStatus = !!statusText && !isGenericHeartbeatStatus;
                    if (isDetailedStatus && statusText !== lastSubAgentStatusRef.current) {
                        const timeStamp = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                        appendSubAgentLine(`[${timeStamp}] ${statusText}`);
                        lastSubAgentStatusRef.current = statusText;
                    }
                    setSubAgentState(prev => {
                    const nextPresence: 'online' | 'idle' | 'error' =
                        incomingPresence === 'online' || incomingPresence === 'idle' || incomingPresence === 'error'
                            ? incomingPresence
                            : prev.presence;
                    // Sub-agent is busy only when presence says online or status clearly says so — not when idle/error.
                    const isRunning =
                        nextPresence === 'online' ||
                        (nextPresence !== 'idle' &&
                            nextPresence !== 'error' &&
                            (statusLower.includes('running') || statusLower.includes('pending')));
                    // A custom kind can open as soon as it is known (the custom view renders a loading shell
                    // without data) — START already opened it; this keeps a status tick consistent. (The old
                    // data gate also missed document + the browser object.)
                    const shouldOpen = isRunning && !subAgentUserClosedRef.current;
                    return {
                        ...prev,
                        isOpen: shouldOpen ? true : prev.isOpen,
                        agentName: data.agentName || prev.agentName,
                        // Keep detailed progress text visible; don't overwrite it every second with generic heartbeat status.
                        status: isGenericHeartbeatStatus && prev.status ? prev.status : (statusLine || prev.status),
                        presence: nextPresence,
                        currentFile: data.file || prev.currentFile,
                        codeContent: data.code || prev.codeContent,
                        steps: newSteps,
                        artifactFile: artifactDirtyRef.current ? prev.artifactFile : (data.file || prev.artifactFile),
                        artifactCode: artifactDirtyRef.current ? prev.artifactCode : (data.code || prev.artifactCode),
                        artifactStatus: artifactDirtyRef.current ? prev.artifactStatus : (data.code || data.file ? 'Synced' : prev.artifactStatus)
                    };
                    });
                }
                else if (data.type === 'coder_state') {
                    // Live project state from the coding agent: file tree, git,
                    // loop/task progress. Powers the VS-Code view in SubAgentWindow.
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    // First custom data -> now the window may open (in its custom look)
                    if (!subAgentUserClosedRef.current) openSubAgentWindow(false);
                    setSubAgentState(prev => ({
                        ...prev,
                        coder: {
                            fileTree: Array.isArray(data.fileTree) ? data.fileTree : (prev.coder?.fileTree ?? []),
                            git: data.git ?? prev.coder?.git ?? { branch: '', dirty: 0, commits: [] },
                            tasks: Array.isArray(data.tasks) ? data.tasks : (prev.coder?.tasks ?? []),
                            loop: typeof data.loop === 'number' ? data.loop : (prev.coder?.loop ?? 0),
                            taskProgress: data.taskProgress ?? prev.coder?.taskProgress ?? '',
                            linterOk: typeof data.linterOk === 'boolean' ? data.linterOk : (prev.coder?.linterOk ?? true),
                            projectName: data.projectName ?? prev.coder?.projectName ?? '',
                            projectPath: data.projectPath ?? prev.coder?.projectPath ?? '',
                            // Per-file red/green diff for the editor's live "what is being edited" view.
                            // Was dropped here (built field-by-field), so the diff never reached the window.
                            diffs: (data.diffs && typeof data.diffs === 'object') ? data.diffs : (prev.coder?.diffs ?? {}),
                            // Live current action ("Loop 26", "Checking quality...") for the header's
                            // liveness signal in file-less phases (docs/verify). Forward it too, else
                            // the field-by-field rebuild drops it (same trap as diffs above).
                            activity: typeof data.activity === 'string' ? data.activity : (prev.coder?.activity ?? ''),
                        },
                    }));
                }
                else if (data.type === 'research_state') {
                    // Live research state: outline, sources, finished section html.
                    // Powers the paper-style research view in SubAgentWindow.
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    // First custom data -> now the window may open (in its custom look)
                    if (!subAgentUserClosedRef.current) openSubAgentWindow(false);
                    setSubAgentState(prev => ({
                        ...prev,
                        research: {
                            topic: data.topic ?? prev.research?.topic ?? '',
                            stage: data.stage ?? prev.research?.stage ?? '',
                            sections: Array.isArray(data.sections) ? data.sections : (prev.research?.sections ?? []),
                            sectionsHtml: Array.isArray(data.sectionsHtml) ? data.sectionsHtml : (prev.research?.sectionsHtml ?? []),
                            sources: Array.isArray(data.sources) ? data.sources : (prev.research?.sources ?? []),
                            wordsTarget: typeof data.wordsTarget === 'number' ? data.wordsTarget : (prev.research?.wordsTarget ?? 0),
                            loop: typeof data.loop === 'number' ? data.loop : (prev.research?.loop ?? 0),
                        },
                    }));
                }
                else if (data.type === 'document_state') {
                    // Live document state: sections, growing section html, placeholders.
                    // Powers the paper-style document view in SubAgentWindow.
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    if (!subAgentUserClosedRef.current) openSubAgentWindow(false);
                    setSubAgentState(prev => ({
                        ...prev,
                        document: {
                            title: data.title ?? prev.document?.title ?? '',
                            format: data.format ?? prev.document?.format ?? 'docx',
                            docType: data.docType ?? prev.document?.docType ?? 'report',
                            stage: data.stage ?? prev.document?.stage ?? '',
                            sections: Array.isArray(data.sections) ? data.sections : (prev.document?.sections ?? []),
                            sectionsHtml: Array.isArray(data.sectionsHtml) ? data.sectionsHtml : (prev.document?.sectionsHtml ?? []),
                            placeholders: Array.isArray(data.placeholders) ? data.placeholders : (prev.document?.placeholders ?? []),
                            wordsTarget: typeof data.wordsTarget === 'number' ? data.wordsTarget : (prev.document?.wordsTarget ?? 0),
                            savePath: data.savePath ?? prev.document?.savePath ?? '',
                            loop: typeof data.loop === 'number' ? data.loop : (prev.document?.loop ?? 0),
                        },
                    }));
                }
                else if (data.type === 'librarian_state') {
                    // Live read-only state from the librarian agent: filesystem map,
                    // folder sizes, storage/drives, Google Drive, optional search.
                    // Powers the explorer view in SubAgentWindow.
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    if (!subAgentUserClosedRef.current) openSubAgentWindow(false);
                    setSubAgentState(prev => ({
                        ...prev,
                        librarian: {
                            root: data.root ?? prev.librarian?.root ?? '~',
                            stage: data.stage ?? prev.librarian?.stage ?? '',
                            readOnly: typeof data.readOnly === 'boolean' ? data.readOnly : (prev.librarian?.readOnly ?? true),
                            totalSize: typeof data.totalSize === 'number' ? data.totalSize : (prev.librarian?.totalSize ?? 0),
                            totalFiles: typeof data.totalFiles === 'number' ? data.totalFiles : (prev.librarian?.totalFiles ?? 0),
                            totalFolders: typeof data.totalFolders === 'number' ? data.totalFolders : (prev.librarian?.totalFolders ?? 0),
                            entries: Array.isArray(data.entries) ? data.entries : (prev.librarian?.entries ?? []),
                            topFolders: Array.isArray(data.topFolders) ? data.topFolders : (prev.librarian?.topFolders ?? []),
                            drives: Array.isArray(data.drives) ? data.drives : (prev.librarian?.drives ?? []),
                            search: data.search ?? prev.librarian?.search ?? null,
                            activity: Array.isArray(data.activity) ? data.activity : (prev.librarian?.activity ?? []),
                            currentFolder: data.currentFolder ?? prev.librarian?.currentFolder ?? null,
                        },
                    }));
                }
                else if (data.type === 'browser_state') {
                    // Live structured state from the browser agent: task, step, action plan,
                    // visited URLs, vision. Powers the dock of the browser window (the
                    // screenshot arrives separately as browser_frame_update).
                    if (data.sessionId && activeSessionId && data.sessionId !== activeSessionId) return;
                    if (!subAgentUserClosedRef.current) openSubAgentWindow(false);
                    setSubAgentState(prev => ({
                        ...prev,
                        browser: {
                            task: data.task ?? prev.browser?.task ?? '',
                            url: data.url ?? prev.browser?.url ?? '',
                            status: data.status ?? prev.browser?.status ?? 'running',
                            step: typeof data.step === 'number' ? data.step : (prev.browser?.step ?? 0),
                            maxSteps: typeof data.maxSteps === 'number' ? data.maxSteps : (prev.browser?.maxSteps ?? 0),
                            vision: data.vision ?? prev.browser?.vision ?? 'auto',
                            actions: Array.isArray(data.actions) ? data.actions : (prev.browser?.actions ?? []),
                            history: Array.isArray(data.history) ? data.history : (prev.browser?.history ?? []),
                        },
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
                        const timeStamp = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
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
                    const prevSessionId = currentSessionIdRef.current;
                    setCurrentSessionId(data.sessionId);
                    // Do not clear Document Viewer documents here – they are kept per session in sessionViewerState and would be lost on second switch-back
                    sidebarDocsSyncedForSessionRef.current = null;

                    // Restore active state
                    const isActive = !!data.isActive;
                    // Race guard: a local turn can already be in-flight while backend history
                    // still reports isActive=false (e.g. reconnect/load snapshot during stream).
                    // In that case, avoid replacing local chat state with an incomplete snapshot.
                    const prevLocalState = sessionLoadingStates.current[data.sessionId];
                    const hadLocalGenerating = !!prevLocalState?.isGenerating;
                    const effectiveActive = isActive || hadLocalGenerating;
                    const status = data.currentStatus && isActive ? `Agent: ${data.currentStatus}` : '';
                    setLoading(effectiveActive);
                    setIsGenerating(effectiveActive);
                    setStatusMessage(status);
                    syncedSessions.current.add(data.sessionId);
                    setHistoryLoading(false);  // history snapshot arrived (active OR idle/empty) → stop the load spinner

                    // Update per-session loading state tracking
                    sessionLoadingStates.current[data.sessionId] = {
                        loading: effectiveActive,
                        isGenerating: effectiveActive,
                        statusMessage: status,
                        loadingMessageId: effectiveActive ? (data.messages?.length || 0) : null
                    };

                    // Parse server messages and preserve server order (index) so sort is stable
                    const serverMsgs: Array<Message & { _order: number }> = data.messages
                        .filter((m: any) => m.role !== 'system') // Hide raw system prompts from server (we have better local logs)
                        .map((m: any, idx: number) => ({
                            role: m.role,
                            content: m.content,
                            timestamp: m.timestamp ? new Date(m.timestamp).getTime() : Date.now(),
                            kind: m.kind,   // proactive bubble tag -> re-plays the avatar animation on reload/chat-switch
                            _order: idx,
                            toolId: m.toolId,
                            toolName: m.toolName,
                            toolStatus: m.toolStatus as Message['toolStatus'] | undefined
                        }));

                    // Best practice for reload/network stability:
                    // When no generation is active, treat backend history as source of truth.
                    // This avoids expensive cache/orphan merge work on large chats and prevents
                    // stale cached fragments (e.g. partial thinking chunks) from reordering UI.
                    if (!isActive && !hadLocalGenerating) {
                        const finalServerMsgs = serverMsgs.map(({ _order, ...msg }) => msg) as Message[];
                        // Guard: don't wipe existing messages if server sends an empty history
                        // during reconnect for the SAME session (transient backend restore).
                        // But always apply when switching to a different session (e.g. new_session
                        // sends messages:[] intentionally to clear the chat).
                        const sessionSwitched = data.sessionId !== prevSessionId;
                        if (sessionSwitched || finalServerMsgs.length > 0 || messagesRef.current.length === 0) {
                            setMessages(finalServerMsgs);
                            if (sessionSwitched) setAttachedImages([]);
                        }

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

                    // Normalize content for comparison (strip <think> and <Action> blocks so a server
                    // "answer only" version matches the cache "think + action + answer" version)
                    const normContent = (s: string) => (s ?? '')
                        .replace(/<think>[\s\S]*?<\/think>/gi, '')
                        .replace(/<Action>[\s\S]*?<\/Action>/gi, '')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .slice(0, 400);

                    const hydratedServerMsgs = serverMsgs.map((srvMsg: Message & { _order?: number }) => {
                        if (srvMsg.role === 'tool') {
                            // 1) Exact content match (tool already completed on client too)
                            const exactMatch = cachedMsgs.find(cm =>
                                cm.role === 'tool' &&
                                cm.content === srvMsg.content
                            );
                            if (exactMatch) {
                                return { ...srvMsg, ...exactMatch, _order: srvMsg._order };
                            }
                            // 2) Running→completed match: client has content='' (still running),
                            //    server already has the result. Preserve toolId/toolArgs from
                            //    client but take the completed content from server.
                            const runningMatch = cachedMsgs.find(cm =>
                                cm.role === 'tool' &&
                                cm.toolStatus === 'running' &&
                                (cm.content === '' || cm.content == null) &&
                                cm.toolName === srvMsg.toolName
                            );
                            if (runningMatch) {
                                return {
                                    ...srvMsg,
                                    toolId: runningMatch.toolId,
                                    toolArgs: runningMatch.toolArgs,
                                    toolStartTime: runningMatch.toolStartTime,
                                    toolStatus: 'completed' as const,
                                    _order: srvMsg._order
                                };
                            }
                        }
                        // Assistant: prefer cache version if it contains <think> OR <Action> tags
                        // so the Thinking / Action panels survive reload (server may store answer-only).
                        if (srvMsg.role === 'assistant') {
                            const srvNorm = normContent(String(srvMsg.content ?? ''));
                            const withThink = cachedMsgs.find(cm =>
                                cm.role === 'assistant' &&
                                (normContent(String(cm.content ?? '')) === srvNorm || Math.abs((cm.timestamp ?? 0) - (srvMsg.timestamp ?? 0)) < 2000) &&
                                (/<think>[\s\S]*?<\/think>/i.test(String(cm.content ?? '')) || /<Action>[\s\S]*?<\/Action>/i.test(String(cm.content ?? '')))
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
                        .replace(/<Action>[\s\S]*?<\/Action>/gi, '')
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
                    // Show the banner for ANY download in progress -- including tray/auto (first-run)
                    // downloads, not only ones started from the WebUI (which pre-set 'downloading').
                    setDownloadModelStatus(prev => ({
                        status: 'downloading',
                        repo_id: data.repo_id ?? prev.repo_id,
                        progress_pct: data.progress_pct,
                        bytes_done: data.bytes_done,
                        bytes_total: data.bytes_total,
                        speed_str: data.speed_str
                    }));
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
                // ── Custom tool WS responses ──────────────────────────────────
                else if (data.type === 'custom_tool_created' || data.type === 'custom_tool_updated' || data.type === 'custom_tool_deleted' || data.type === 'custom_tool_permissions_updated') {
                    // Operation succeeded — clear saving state and error
                    setIsCustomToolSaving(false);
                    setCustomToolBackendError(null);
                    // Backend also broadcasts a tools_list refresh (handled by the tools_list branch above)
                }
                else if (data.type === 'custom_tool_error') {
                    // Backend rejected the operation (e.g. no BaseTool subclass found, invalid name)
                    setIsCustomToolSaving(false);
                    setCustomToolBackendError(data.error || 'Unknown error');
                }
                else if (data.type === 'mcp_servers') {
                    // Configured MCP servers + live connection status
                    setMcpServers(data.servers || []);
                }
                else if (data.type === 'mcp_server_saved' || data.type === 'mcp_server_deleted') {
                    // Save/delete succeeded. The reply carries the refreshed list, so the UI updates
                    // immediately without a separate get_mcp_servers round-trip.
                    setIsMcpSaving(false);
                    setMcpBackendError(null);
                    if (Array.isArray(data.servers)) {
                        setMcpServers(data.servers);
                    } else {
                        ws?.send(JSON.stringify({ type: 'get_mcp_servers' }));
                    }
                    // A freshly-added server may still be downloading (npx) on first run, so its first
                    // status is "offline". Refresh again shortly so it turns green without a manual click.
                    if (data.type === 'mcp_server_saved') {
                        setTimeout(() => { try { ws?.send(JSON.stringify({ type: 'get_mcp_servers' })); } catch { /* ws closed */ } }, 8000);
                    }
                }
                else if (data.type === 'mcp_server_error') {
                    setIsMcpSaving(false);
                    setMcpBackendError(data.error || 'Unknown error');
                }
                else if (data.type === 'mcp_server_test_result') {
                    setIsMcpTesting(false);
                    setMcpTestResult({ connected: !!data.connected, tool_count: data.tool_count || 0, tools: data.tools || [], error: data.error ?? null });
                }
                else if (data.type === 'custom_tool_users') {
                    // List of non-admin users for the share picker
                    setCustomToolUsers(data.users || []);
                }
                else if (data.type === 'custom_tool_source') {
                    // Source code response — the SettingsModal stores this in its
                    // customToolEditor state via a ref.  We surface it here so the
                    // parent can pass it down. For now we fire an event via a custom
                    // DOM event so the modal can pick it up without prop drilling.
                    window.dispatchEvent(new CustomEvent('vaf:custom_tool_source', {
                        detail: { name: data.name, source: data.source }
                    }));
                }
                // ─────────────────────────────────────────────────────────────
                else if (data.type === 'workflows_list') {
                    console.log('[Workflows]', data.workflows);
                    setWorkflows(data.workflows || []);
                }
                else if (data.type === 'workflow_created' || data.type === 'workflow_updated' || data.type === 'workflow_deleted') {
                    setIsWorkflowSaving(false);
                    setWorkflowBackendError(null);
                    // workflows_list refresh is sent by the backend immediately after
                }
                else if (data.type === 'workflow_error') {
                    setIsWorkflowSaving(false);
                    setWorkflowBackendError(data.error || 'Unknown error');
                }
                else if (data.type === 'skills_list') {
                    setSkills(data.skills || []);
                }
                else if (data.type === 'skill_created' || data.type === 'skill_updated' || data.type === 'skill_deleted') {
                    setIsSkillSaving(false);
                    setSkillBackendError(null);
                    setSkillSavedTick(t => t + 1);  // closes the editor; skills_list broadcast refreshes the grid
                }
                else if (data.type === 'skill_permissions_updated') {
                    setIsSkillSaving(false);
                    setSkillBackendError(null);
                }
                else if (data.type === 'skill_error') {
                    setIsSkillSaving(false);
                    setSkillBackendError(data.error || 'Unknown error');
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
                else if (data.type === 'speaker_profile') {
                    setSpeakerProfile(data.profile || null);
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
                    setNotifications(prev => [data.notification, ...prev].slice(0, 200));
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
                        // Keep isStoppingGenerationRef true for 3s after stop is acknowledged.
                        // The backend confirms stop immediately but the agent loop may still
                        // be running and send late agent_message_update events that would
                        // re-arm isGenerating and bring the stop button back.
                        // The ref guards those late events (line: !isStoppingGenerationRef.current).
                        setTimeout(() => { isStoppingGenerationRef.current = false; }, 3000);
                        setLoading(false);
                        setIsGenerating(false);
                        setIsStoppingGeneration(false);
                        setLoadingMessageId(null);
                        // Clear workflow runtime so stop button hides (workflow was stopped)
                        clearWorkflow();
                    }
                }
                else if (data.type === 'file_created') {
                    const sid = data.sessionId || currentSessionIdRef.current || '';
                    const activeSessionId = currentSessionIdRef.current;
                    if (!data.sessionId || data.sessionId === activeSessionId) {
                        // Keep the workspace chip/window in sync with new files — but only while the
                        // viewer is showing the active chat's workspace, so a background file event
                        // never yanks the view away from another workspace the user is browsing.
                        if (viewedWorkspaceSidRef.current == null || viewedWorkspaceSidRef.current === activeSessionId) {
                            refreshWorkspace(activeSessionId);
                        }
                        const newChip = { path: data.filePath, name: data.title || data.filePath.split('/').pop() || 'file', sessionId: sid };
                        setCreatedFiles(prev => {
                            // Avoid duplicates
                            if (prev.some(f => f.path === data.filePath)) return prev;
                            return [...prev, newChip];
                        });
                        // Also attach immediately to the last assistant message to avoid race
                        // with message_complete clearing the pending list before this chip arrives.
                        setMessages(prev => {
                            const lastAssistantIdx = prev.map((m, i) => ({ m, i })).filter(({ m }) => m.role === 'assistant').pop()?.i;
                            if (lastAssistantIdx === undefined) return prev;
                            const existing = prev[lastAssistantIdx].downloadFiles || [];
                            if (existing.some(f => f.path === data.filePath)) return prev;
                            const updated = [...prev];
                            updated[lastAssistantIdx] = { ...updated[lastAssistantIdx], downloadFiles: [...existing, { path: data.filePath, name: newChip.name }] };
                            return updated;
                        });
                    }
                }
                else if (data.type === 'browser_frame_update') {
                    // Live browser screenshot from browser_agent. The frame is always stored; how it
                    // is shown depends on context:
                    //  • inside a workflow → the BrowserLiveTile docks left of the Workflow Runtime
                    //    window (tiled, no overlap), so we do NOT open the dock here.
                    //  • standalone → surface the SubAgent dock window as before.
                    const inWorkflow = isWorkflowRunningRef.current;
                    setSubAgentState(prev => ({
                        ...prev,
                        browserFrame: data.frame || '',
                        browserUrl: data.url || '',
                        agentName: prev.agentName || 'Browser Agent',
                        presence: prev.presence === 'error' ? prev.presence : 'online',
                        isOpen: (!inWorkflow && !subAgentUserClosedRef.current) ? true : prev.isOpen,
                    }));
                    if (!inWorkflow && !subAgentUserClosedRef.current) setShowSubAgentPanel(true);
                }
                else if (data.type === 'browser_step_update' && data.line) {
                    // browser-use agent step log line → SubAgent console
                    appendSubAgentLine(String(data.line));
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
        const scheduleReconnect = () => {
            if (cancelled) return;
            // Exponential backoff with jitter, capped at 30s — no more fixed-interval hammering.
            const delay = Math.min(1000 * 2 ** Math.min(reconnectAttempt, 5), 30000);
            const jitter = Math.floor(Math.random() * 0.3 * delay);
            reconnectTimeout = setTimeout(() => {
                setStatus('connecting');
                setReconnectAttempt((a) => a + 1);
            }, delay + jitter);
        };
        socket.onclose = () => {
            setStatus('disconnected');
            setWs(null);
            if (cancelled) return;
            // A socket that closed WITHOUT ever opening = a rejected handshake (expired/invalid token,
            // or the server briefly down). Re-check auth before reconnecting so an expired session
            // routes cleanly to /login instead of hammering /ws.
            if (!opened) {
                const t = localStorage.getItem('vaf_token');
                fetch(`${getApiBase() || ''}/api/auth/me`, {
                    credentials: 'include',
                    cache: 'no-store',
                    headers: t ? { Authorization: `Bearer ${t}` } : {},
                })
                    .then((res) => {
                        if (cancelled) return;
                        if (res.status === 401 || res.status === 403) {
                            localStorage.removeItem('vaf_token');
                            setIsAuthenticated(false);
                            window.location.replace(`${window.location.origin}/login`);
                            return;
                        }
                        scheduleReconnect();
                    })
                    .catch(() => scheduleReconnect());
                return;
            }
            scheduleReconnect();
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
    }, [reconnectAttempt, isAuthenticated, authChecking]);

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

    // Download a generated file safely. In the desktop window the QtWebEngine blob
    // (<a download>) path is brittle/blocked, so use the native Save-As bridge
    // (same one the workspace panel uses); in a real browser, fetch -> blob ->
    // download. Either way a failure shows a toast instead of navigating the whole
    // window to raw JSON with no way back. Never render created-file chips as raw
    // <a> navigations.
    const notifyDownloadError = useCallback((name: string, code?: number | string) => {
        setDownloadToast({ show: true, message: `Could not open "${name}"${code ? ` (${code})` : ''}`, success: false });
        setTimeout(() => setDownloadToast(prev => ({ ...prev, show: false })), 4000);
    }, []);

    const safeDownloadFile = useCallback(async (path: string, name: string) => {
        const api = (window as unknown as { pywebview?: { api?: { save_file_as?: (p: string) => Promise<{ ok?: boolean; error?: string; cancelled?: boolean }> } } }).pywebview?.api;
        if (api?.save_file_as) {
            try {
                const r = await api.save_file_as(path);
                if (r && r.ok === false && !r.cancelled) notifyDownloadError(name, r.error);
            } catch {
                notifyDownloadError(name);
            }
            return;
        }
        try {
            const res = await fetch(`${getApiBase()}/api/file?path=${encodeURIComponent(path)}`, { credentials: 'include' });
            if (!res.ok) { notifyDownloadError(name, res.status); return; }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = name;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch {
            notifyDownloadError(name);
        }
    }, [notifyDownloadError]);

    const handleAnnouncementClose = useCallback(() => {
        const acked = acknowledgedVersion(rawVersion);
        setLastSeenVersion(acked || null);  // local update so the decision effect won't re-fire
        setAnnouncement(null);
        if (acked) {
            fetch(getApiBase() + '/api/user/user-identity', {
                method: 'PUT', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ last_seen_announcement_version: acked }),
            }).catch(() => {});
        }
    }, [rawVersion]);

    // First-run Open-Alpha notice (or a per-version "what's new"). Gated on persona AND version both
    // loaded so it never flashes; the per-user last_seen_announcement_version drives show-once.
    useEffect(() => {
        if (!isAuthenticated || authChecking || !personaLoaded || !rawVersion || announcement) return;
        const kind = decideAnnouncement(lastSeenVersion, rawVersion);
        if (kind) setAnnouncement(kind);
    }, [isAuthenticated, authChecking, personaLoaded, rawVersion, lastSeenVersion, announcement]);

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
        if (pendingScrollRestore.current !== null) {
            const restore = pendingScrollRestore.current;
            pendingScrollRestore.current = null;
            requestAnimationFrame(() => {
                const container = containerRef.current;
                if (!container) return;
                if (restore === 'bottom' || restore.wasAtBottom) {
                    container.scrollTop = container.scrollHeight;
                    isAtBottomRef.current = true;
                } else {
                    container.scrollTop = restore.scrollTop;
                    isAtBottomRef.current = false;
                }
            });
        } else if (isAtBottomRef.current) {
            scrollRef.current?.scrollIntoView({ behavior: 'smooth' });
        }
    }, [messages, loading]);

    // Track whether the user is scrolled to the bottom
    useEffect(() => {
        const container = containerRef.current;
        if (!container) return;
        const handleScroll = () => {
            const { scrollTop, scrollHeight, clientHeight } = container;
            isAtBottomRef.current = scrollTop + clientHeight >= scrollHeight - 50;
        };
        container.addEventListener('scroll', handleScroll, { passive: true });
        return () => container.removeEventListener('scroll', handleScroll);
    }, []);

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

        // Initialize context stats placeholder only — the backend sends the real
        // max_tokens (model-specific context window) via the 'stats' event.
        // Never override once we have backend stats; that would reset DeepSeek's
        // 64k / Claude's 200k back to the local n_ctx value.
        if (!contextStats) {
            const isApi = config.provider && config.provider !== 'local';
            const nCtx = config.n_ctx ?? 8192;
            // Reasonable placeholder: API providers are always at least 128k
            const max_tokens = isApi ? 128000 : nCtx;
            setContextStats({
                tokens: 0,
                max_tokens,
                percent: 0,
                message_count: 0
            });
        }
    }, [config]); // eslint-disable-line react-hooks/exhaustive-deps

    // Fetch brain data when the context modal opens
    useEffect(() => {
        if (!isContextModalOpen) return;
        fetch(`${getApiBase()}/api/agent/brain`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : null)
            .then(d => d && !d.error ? setBrainData(d) : setBrainData(null))
            .catch(() => setBrainData(null));
    }, [isContextModalOpen]);

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
        // Unblock attachment indexing locally right away (the backend cancels the task
        // server-side too) so prompting + closing the Document Viewer recover instantly.
        if (isIndexing) {
            const sid = currentSessionId;
            setAttachmentIndexStatus(prev => { const next = { ...prev }; delete next[sid]; return next; });
            setAttachmentIndexCount(prev => { const next = { ...prev }; delete next[sid]; return next; });
        }
        // Stop scope (chat-while-subagent-runs): while a reply is streaming, a press stops
        // ONLY the generation — the backend keeps active sub-agents alive (scoped stop).
        // When nothing is streaming but a sub-agent runs, the press can only mean the
        // sub-agent → that IS the explicit stop-sub-agent action (scope 'all').
        const stopScope = (!isGenerating && isSubAgentRunning) ? 'all' : undefined;
        if (isStoppingGenerationRef.current) {
            // Already stopping a generation; still send the stop so any in-flight indexing
            // for this session is cancelled, then bail out of the pulse/animation setup.
            ws.send(JSON.stringify({ type: 'stop_generation', sessionId: currentSessionId, ...(stopScope ? { scope: stopScope } : {}) }));
            return;
        }
        isStoppingGenerationRef.current = true;
        setIsStoppingGeneration(true);
        // Capture button position for portal ripples before state change
        if (stopBtnRef.current) {
            const r = stopBtnRef.current.getBoundingClientRect();
            setStopBtnPos({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
        }
        // Ripple pulses for minimum 3 full cycles (3 × 1.1s ≈ 3.3s) regardless of how fast the backend stops
        setStopPulsing(true);
        if (stopPulseTimerRef.current) clearTimeout(stopPulseTimerRef.current);
        stopPulseTimerRef.current = setTimeout(() => { setStopPulsing(false); setStopBtnPos(null); }, 3500);
        ws.send(JSON.stringify({
            type: 'stop_generation',
            sessionId: currentSessionId,
            ...(stopScope ? { scope: stopScope } : {})
        }));
    };

    const sendMessage = async (e?: React.FormEvent, overrideText?: string) => {
        e?.preventDefault();
        const combined = [input, ...insertedSelections.map((s) => s.text)].filter(Boolean).join('\n\n');
        const textToSend = overrideText ?? combined;
        const imagesToSend = [...attachedImages];
        if (!textToSend.trim() || !ws) return;
        // Block prompting while attachments are still being indexed for retrieval.
        if (isIndexing) return;

        // Compute code viewer chip metadata upfront (no content stored in message state)
        const _cvChip = codeViewerState.isOpen && codeViewerState.loadedContent ? {
            name: codeViewerState.filePath.split('/').pop() || codeViewerState.title || 'File',
            path: codeViewerState.filePath,
            ext: (codeViewerState.filePath.split('.').pop() || '').toLowerCase(),
            lineCount: codeViewerState.loadedContent.split('\n').length,
        } : undefined;

        setMessages(prev => [...prev, {
            role: 'user',
            content: textToSend,
            timestamp: Date.now(),
            sidebarDocs: documentViewerState.documents.length > 0
                ? documentViewerState.documents.map(d => d.name)
                : undefined,
            codeViewerFile: _cvChip,
            images: imagesToSend.length > 0 ? imagesToSend.map(({ url, name }) => ({ url, name })) : undefined,
        }]);
        expectNewAssistantRef.current = true;
        lastUserSendTimeRef.current = Date.now();
        setCreatedFiles([]);
        setIsStoppingGeneration(false);
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

        // Only send name+mimeType (no base64) — backend already has content from set_sidebar_documents.
        // Sending base64 on every message caused double-processing of large PDFs and WS message bloat.
        const sidebarPayload = documentViewerState.documents.length > 0
            ? documentViewerState.documents.map(d => ({ name: d.name, mimeType: d.mimeType || '' }))
            : undefined;
        const editorDoc =
            documentEditorState.isOpen && (documentEditorState.docxModel || documentEditorState.content)
                ? (() => {
                    if (documentEditorState.docxModel) {
                        return {
                            name: documentEditorState.title || 'Document',
                            content: flattenNativeDocxText(documentEditorState.docxModel),
                        };
                    }
                    const div = document.createElement('div');
                    div.innerHTML = documentEditorState.content || '';
                    return { name: documentEditorState.title || 'Document', content: (div.textContent || div.innerText || '').trim() };
                })()
                : undefined;
        const editorSelectionsPayload =
            insertedSelections
                .filter((s) => s.documentId === 'editor')
                .map((s) => ({ start: s.start, end: s.end, text: s.text }));
        // Pass code viewer file to backend (stored in runtime_state, injected per-turn, NOT stored in message history)
        const codeViewerFile = _cvChip && codeViewerState.loadedContent ? {
            name: _cvChip.name,
            path: _cvChip.path,
            content: codeViewerState.loadedContent.slice(0, 30000),
        } : undefined;
        // While the Image Viewer is open, send its vision description so the backend keeps it in
        // the agent's context for this turn (stored in runtime_state, injected per-turn, NOT in history).
        const imageViewerCtx = imageViewerState.isOpen && imageViewerState.description ? {
            name: imageViewerState.title || imageViewerState.filePath.split('/').pop() || 'image',
            path: imageViewerState.filePath,
            description: imageViewerState.description.slice(0, 8000),
        } : undefined;
        // Yellow-marked region (annotated full image + zoomed crop) — sent while a marking is
        // active so the backend runs vision on that region for THIS question.
        const markedRegion = imageViewerState.isOpen && imageMark ? {
            name: imageMark.name,
            annotated: imageMark.annotated,
            crop: imageMark.crop,
        } : undefined;
        ws.send(JSON.stringify({
            type: 'chat',
            content: textToSend,
            sessionId: currentSessionId,
            ...(sidebarPayload && sidebarPayload.length > 0 ? { sidebarDocuments: sidebarPayload } : {}),
            ...(editorDoc && editorDoc.content !== '' ? { editorDocument: editorDoc } : {}),
            ...(editorSelectionsPayload.length > 0 ? { editorSelections: editorSelectionsPayload } : {}),
            ...(codeViewerFile ? { codeViewerFile } : {}),
            ...(imageViewerCtx ? { imageViewerContext: imageViewerCtx } : {}),
            ...(markedRegion ? { markedRegion } : {}),
            // Vision: send images as file objects so web_server can route them to the vision pipeline
            ...(imagesToSend.length > 0 ? {
                files: imagesToSend.map(img => ({
                    name: img.name,
                    data: img.url,  // data URI — web_server strips the prefix
                    mimeType: img.url.split(';')[0].replace('data:', '') || 'image/jpeg',
                }))
            } : {}),
        }));
        setAttachedImages([]);
        // One-shot marking: the region vision ran for this question — clear it so unrelated
        // follow-up messages don't keep re-billing a region analysis (re-mark to ask again).
        if (markedRegion) clearImageMark();
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

    const ACCEPT_ATTACHMENTS = 'image/*,.pdf,.docx,.xlsx,.pptx,.txt,.md,.json,.csv,.py,.js,.ts,.tsx,.jsx,.html,.htm,.css,.scss,.yaml,.yml,.sh,.sql,.xml,.go,.rs,.java,.cpp,.c,.rb,.php';
    const acceptedExtensions = useMemo(() => new Set(ACCEPT_ATTACHMENTS.split(',').map(ext => ext.trim().toLowerCase())), []);

    /** Stage image files as previews in the input bar (sent alongside next message). */
    const addImagesAsAttachments = useCallback(async (newFiles: File[]) => {
        const imgFiles = newFiles.filter(f => f.type.startsWith('image/'));
        if (imgFiles.length === 0) return;
        const entries = await Promise.all(imgFiles.map(async (f) => ({
            id: crypto.randomUUID(),
            url: await fileToBase64(f),  // data URI – used both for preview and sending
            name: f.name,
        })));
        setAttachedImages(prev => [...prev, ...entries]);
    }, []);

    const addFilesAsAttachments = useCallback(async (newFiles: File[]) => {
        // Route image files to the vision pipeline instead of document viewer
        const imageFiles = newFiles.filter(f => f.type.startsWith('image/'));
        if (imageFiles.length > 0) addImagesAsAttachments(imageFiles);

        const nonImageFiles = newFiles.filter(f => !f.type.startsWith('image/'));
        const filtered = nonImageFiles.filter(f => {
            const ext = '.' + (f.name.split('.').pop() ?? '').toLowerCase();
            return acceptedExtensions.has(ext);
        });
        if (filtered.length === 0) return;

        // Split into code files and document files
        const codeFiles = filtered.filter(f => isCodeFile(f.name));
        const docFiles = filtered.filter(f => !isCodeFile(f.name));

        // Code files → open in CodeViewer (read the content from the File object)
        if (codeFiles.length > 0) {
            const first = codeFiles[0]; // show the first one; user can attach more one at a time
            const text = await first.text();
            // HTML → HtmlViewer (rendered preview); other code → CodeViewer.
            if (isHtmlFile(first.name)) {
                setHtmlViewerState({ isOpen: true, filePath: first.name, title: first.name, initialContent: text });
            } else {
                setCodeViewerState({ isOpen: true, filePath: first.name, title: first.name, initialContent: text } as typeof codeViewerState);
            }
            setShowSubAgentPanel(true);
            // Also send as context attachment if there's content (so the agent can see the code)
        }

        // Document files → existing DocumentViewer logic
        if (docFiles.length > 0) {
            const base64List = await Promise.all(docFiles.map(f => fileToBase64(f)));
            const newDocs = docFiles.map((f, i) => ({
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
        }
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
        // While the LLM is indexing the attached documents, closing would clear them
        // mid-flight; keep the viewer open until indexing finishes or the user hits stop.
        if (isIndexing) return;
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
            // Chat-while-subagent-runs: Enter must send while a sub-agent is active even
            // if isGenerating lingers (same unlock as the textarea/send-button gates).
            if (hasContent && !(isGenerating && !isSubAgentRunning) && !isIndexing) {
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
                    const wavBlob = await toWav16k(audioBlob);
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
    const subPresence = subAgentState.presence;
    const subAgentTerminal = subPresence === 'idle' || subPresence === 'error';
    const hasActiveSubAgentStep = subAgentState.steps.some(
        (step) => step.status === 'running' || step.status === 'pending'
    );
    const allSubAgentStepsFinished =
        subAgentState.steps.length > 0 &&
        subAgentState.steps.every((step: { status?: string }) =>
            ['completed', 'failed', 'timeout'].includes(String(step.status || ''))
        );
    // Idle/error from backend wins over status text (avoids false "running" from unrelated substrings).
    const isSubAgentRunning =
        !subAgentTerminal &&
        !allSubAgentStepsFinished &&
        (subPresence === 'online' ||
            hasActiveSubAgentStep ||
            (subAgentState.steps.length === 0 &&
                (subAgentStatusLower.includes('running') || subAgentStatusLower.includes('pending'))));
    const hasCompletedOrDoneStep = subAgentState.steps.some(
        (step: { status?: string }) =>
            step.status === 'completed' || step.status === 'failed' || step.status === 'timeout'
    );
    // Allow close when subagent finished, failed, or timed out (so user can always close on error)
    const subAgentCanClose = !hasActiveSubAgentStep && (
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

    // Mobile: the right dock (sub-agent + document/code/browser viewers) is hidden <lg. A mini-bar opens it
    // as a full-screen sheet on demand; reset the sheet once every dock panel has closed, so the next one
    // doesn't auto-reopen full-screen.
    const anyDockPanelOpen = subAgentState.isOpen || documentEditorState.isOpen || documentViewerState.isOpen || codeViewerState.isOpen || htmlViewerState.isOpen || imageViewerState.isOpen;
    useEffect(() => {
        if (!anyDockPanelOpen && subAgentSheetOpen) setSubAgentSheetOpen(false);
    }, [anyDockPanelOpen, subAgentSheetOpen]);

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
                    className="mt-4 px-4 py-2 bg-gray-900 text-white text-sm font-medium rounded-lg hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
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
            {/* Mobile top bar — hamburger opens the sidebar drawer (desktop keeps the hover-rail) */}
            <div className="md:hidden shrink-0 h-14 flex items-center gap-2 px-3 border-b border-gray-200 bg-white z-30">
                <button type="button" onClick={() => setDrawerOpen(true)} aria-label="Menu" className="p-2.5 -ml-1 rounded-lg text-gray-700 hover:bg-gray-100 active:bg-gray-200 touch-target">
                    <Menu size={22} />
                </button>
            </div>
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
            {pendingSpeakerConfirms.length > 0 && (
                /* Centered floating card (user decision: mid-chat, not a
                   top-left banner). pointer-events-none wrapper keeps the
                   chat usable behind it - answering is optional (the
                   messenger fallback may already have it). */
                <div className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 px-4 pointer-events-none">
                    {pendingSpeakerConfirms.map((p) => (
                        <div key={p.confirmId} className="pointer-events-auto flex flex-col gap-2.5 bg-white dark:bg-[#1f1f1f] rounded-2xl border border-black/10 dark:border-white/10 p-5 shadow-2xl w-full max-w-md">
                            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{tMain('speakerConfirmTitle')}</p>
                            <p className="text-xs text-gray-600 dark:text-gray-400">{p.question}</p>
                            {p.audioPath && (
                                <audio controls preload="none" className="h-8 w-64 max-w-full"
                                    src={`${getApiBase()}/api/file?path=${encodeURIComponent(p.audioPath)}`} />
                            )}
                            <div className="flex items-center gap-2 flex-wrap">
                                <button type="button"
                                    onClick={() => {
                                        setPendingSpeakerConfirms(prev => prev.filter(x => x.confirmId !== p.confirmId));
                                        ws?.send(JSON.stringify({ type: 'speaker_confirm_reply', confirmId: p.confirmId, answer: 'yes' }));
                                    }}
                                    className="px-3 py-1.5 text-sm font-medium rounded-md bg-green-600 text-white hover:bg-green-700">
                                    {tMain('speakerConfirmYes')}
                                </button>
                                <button type="button"
                                    onClick={() => {
                                        setPendingSpeakerConfirms(prev => prev.filter(x => x.confirmId !== p.confirmId));
                                        ws?.send(JSON.stringify({ type: 'speaker_confirm_reply', confirmId: p.confirmId, answer: 'no' }));
                                    }}
                                    className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-200 dark:bg-gray-700 text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-600">
                                    {tMain('speakerConfirmNo')}
                                </button>
                                {!p.nameOpen ? (
                                    <button type="button"
                                        onClick={() => setPendingSpeakerConfirms(prev => prev.map(x =>
                                            x.confirmId === p.confirmId ? { ...x, nameOpen: true } : x))}
                                        className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-200 dark:bg-gray-700 text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-600">
                                        {tMain('speakerConfirmNamed')}
                                    </button>
                                ) : (
                                    <span className="flex items-center gap-1.5">
                                        <input type="text" autoFocus maxLength={32}
                                            placeholder={tMain('speakerConfirmNamePlaceholder')}
                                            value={p.name || ''}
                                            onChange={(e) => setPendingSpeakerConfirms(prev => prev.map(x =>
                                                x.confirmId === p.confirmId ? { ...x, name: e.target.value } : x))}
                                            className="w-32 px-2 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-[#181818] text-gray-800 dark:text-gray-200" />
                                        <button type="button" disabled={!(p.name || '').trim()}
                                            onClick={() => {
                                                setPendingSpeakerConfirms(prev => prev.filter(x => x.confirmId !== p.confirmId));
                                                ws?.send(JSON.stringify({
                                                    type: 'speaker_confirm_reply', confirmId: p.confirmId,
                                                    answer: 'no', name: (p.name || '').trim(),
                                                }));
                                            }}
                                            className="px-3 py-1.5 text-sm font-medium rounded-md bg-gray-800 dark:bg-amber-600 text-white hover:bg-gray-700 dark:hover:bg-amber-500 disabled:opacity-50">
                                            {tMain('speakerConfirmSave')}
                                        </button>
                                    </span>
                                )}
                            </div>
                            <p className="text-[11px] text-gray-400 dark:text-gray-500">{tMain('speakerConfirmHint')}</p>
                        </div>
                    ))}
                </div>
            )}
            {/* Mobile drawer scrim — tap outside to dismiss (desktop never renders it) */}
            {drawerOpen && (
                <div className="md:hidden fixed inset-0 z-[45] bg-black/40" onClick={() => setDrawerOpen(false)} aria-hidden />
            )}
            <div className="flex-1 flex min-h-0 overflow-hidden">
                <aside
                    data-editing={editingId ? 'true' : undefined}
                    className={cn(
                    "group flex flex-col min-h-0 h-full bg-white border-r border-gray-200 transition-[width,transform] duration-300 shadow-lg dark:shadow-none overflow-hidden",
                    // While renaming a chat, pin the sidebar open (it only expands on hover otherwise)
                    // so the rename input never collapses out from under the user. The data-editing
                    // attribute reveals the labels via group-data-[editing=true]:opacity-100.
                    editingId ? "md:w-72 md:z-20" : "md:w-16 md:hover:w-72 md:z-20",
                    "max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:w-72 max-md:z-50 max-md:shadow-2xl",
                    drawerOpen ? "max-md:translate-x-0" : "max-md:-translate-x-full"
                )}>

                    {/* App Header / Logo */}
                    <div className="h-16 flex items-center px-4 gap-3 shrink-0">
                        <div className="w-[38px] h-[38px] rounded-lg overflow-hidden shrink-0 -ml-[5.5px]">
                            <img src="/logo.png" alt="VAF" className="w-full h-full object-cover" />
                        </div>
                        <span className="font-bold text-gray-800 whitespace-nowrap opacity-0 group-hover:opacity-100 group-data-[editing=true]:opacity-100 max-md:opacity-100 transition-opacity delay-100 duration-300 overflow-hidden">{tMain('veylloAgenticFramework')}</span>
                    </div>

                    {/* Session list: outer box overflow-hidden = fixed height, inner box scrolls */}
                    <div className="flex-1 min-h-0 relative overflow-hidden">
                        <div
                            ref={sidebarListRef}
                            className="absolute inset-0 overflow-y-auto overflow-x-hidden p-2 pt-0 space-y-1 scrollbar-hide"
                            style={{ WebkitOverflowScrolling: 'touch' }}
                        >
                            {/* New Chat Button */}
                            <div
                                onClick={() => { setDrawerOpen(false); ws?.send(JSON.stringify({ type: 'new_session' })); }}
                                className="flex items-center gap-3 p-2 pl-3.5 max-md:py-3 rounded-lg cursor-pointer hover:bg-gray-100 text-gray-600 hover:text-gray-900 transition-colors"
                            >
                                <Plus size={16} className="shrink-0" />
                                <span className="text-sm font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 group-data-[editing=true]:opacity-100 max-md:opacity-100 transition-opacity duration-200">New Chat</span>
                            </div>

                            {sessions.map(s => (
                                <div key={s.id} data-session-id={s.id} onClick={() => { setDrawerOpen(false); handleSessionSwitch(s.id); }}
                                    className={cn("flex items-center gap-3 p-2 pl-3.5 max-md:min-h-[44px] rounded-lg cursor-pointer group/item relative", currentSessionId === s.id ? 'bg-transparent' : 'hover:bg-gray-100')}>

                                    {/* Active Indicator (Dot) — only while the sidebar is expanded, so the
                                        collapsed rail keeps the active bubble aligned with the inactive ones
                                        (the active state is still conveyed by the darker icon colour). */}
                                    {currentSessionId === s.id && (
                                        <div className="absolute left-1 top-1/2 -translate-y-1/2 w-1 h-1 bg-black rounded-full opacity-0 group-hover:opacity-100 group-data-[editing=true]:opacity-100 max-md:opacity-100 transition-opacity" />
                                    )}

                                    {/* Unread message indicator — shown when agent sent a message while session was not open */}
                                    {unreadSessions.has(s.id) && currentSessionId !== s.id && (
                                        <div className="absolute right-2 top-1/2 -translate-y-1/2 w-2 h-2 bg-red-500 rounded-full animate-pulse" />
                                    )}

                                    {(s as Session).source === 'thinking' ? (
                                        <span title="Thinking mode">
                                            <Brain size={16} className={cn("shrink-0", currentSessionId === s.id ? "text-gray-900" : "text-gray-400")} />
                                        </span>
                                    ) : (
                                        <MessageSquare size={16} className={cn("shrink-0", currentSessionId === s.id ? "text-gray-900" : "text-gray-400")} />
                                    )}

                                    <div className="flex-1 flex justify-between items-center opacity-0 group-hover:opacity-100 group-data-[editing=true]:opacity-100 max-md:opacity-100 transition-opacity min-w-0 pr-1">
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
                                        <div className="flex items-center gap-1.5 opacity-0 group-hover/item:opacity-100 max-md:opacity-100 transition-opacity">
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
                                                                    wsSocketRef.current?.send(JSON.stringify({ type: 'new_session' }));
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
                        {/* Fade: white fade (instead of gray), so the last visible chat fades out */}
                        <div
                            className="absolute bottom-0 left-0 right-0 h-28 pointer-events-none"
                            style={{
                                zIndex: 50,
                                background: 'linear-gradient(to top, rgb(var(--chat-fog)) 0%, rgb(var(--chat-fog) / 0.92) 35%, rgb(var(--chat-fog) / 0.5) 65%, transparent 100%)',
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
                            className="flex items-center gap-3 p-2 rounded-xl cursor-pointer hover:bg-gray-100 text-gray-500 hover:text-gray-900 group/automation transition-colors justify-start"
                            title="Automation"
                        >
                            <div className="w-6 flex justify-center shrink-0">
                                <Calendar size={20} />
                            </div>
                            <span className="overflow-hidden opacity-0 group-hover:opacity-100 group-data-[editing=true]:opacity-100 max-md:opacity-100 transition-opacity duration-200 font-medium whitespace-nowrap text-sm">Automation</span>
                        </div>

                        {currentUser?.role === 'admin' && (
                        <div
                            onClick={() => setIsNotificationsOpen(true)}
                            className="flex items-center gap-3 p-2 rounded-xl cursor-pointer hover:bg-gray-100 text-gray-500 hover:text-gray-900 group/notifications transition-colors justify-start"
                            title={tNav('notifications')}
                        >
                            <div className="w-6 flex justify-center shrink-0 relative">
                                <ScrollText size={20} />
                                {chainAlert && (
                                    <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 bg-red-500 rounded-full border-2 border-white animate-pulse" />
                                )}
                            </div>
                            <span className="overflow-hidden opacity-0 group-hover:opacity-100 group-data-[editing=true]:opacity-100 max-md:opacity-100 transition-opacity duration-200 font-medium whitespace-nowrap text-sm">{tNav('notifications')}</span>
                        </div>
                        )}

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
                            className="flex items-center gap-3 p-2 rounded-xl cursor-pointer hover:bg-gray-100 text-gray-500 hover:text-gray-900 group/settings transition-colors justify-start"
                            title={tNav('settings')}
                            data-agent-hint="nav-settings"
                        >
                            <div className="w-6 flex justify-center shrink-0">
                                <Settings size={20} />
                            </div>
                            <span className="overflow-hidden opacity-0 group-hover:opacity-100 group-data-[editing=true]:opacity-100 max-md:opacity-100 transition-opacity duration-200 font-medium whitespace-nowrap text-sm">Settings</span>
                        </div>
                    </div>
                </aside>

                <div
                    className={cn(
                        "flex-1 flex overflow-hidden pr-4 transition-all duration-300 ease-out",
                        (subAgentState.isOpen || codeViewerState.isOpen || documentEditorState.isOpen || documentViewerState.isOpen) ? "gap-4" : "gap-0"
                    )}
                >
                    <div className="flex-1 flex flex-col relative bg-white overflow-hidden">
                        {/* ── Prompt Navigator (right rail, DeepSeek-style) ── */}
                        {(() => {
                            const userPrompts = messages.filter(m => m.role === 'user' && String(m.content ?? '').trim());
                            if (userPrompts.length === 0) return null;
                            const MAX_DASHES = 9;
                            const displayed = userPrompts.slice(-MAX_DASHES);
                            return (
                                // Outer wrapper is the hover target — wide enough to bridge dashes + popup gap
                                <div className="group/promptnav absolute right-0 top-1/2 -translate-y-1/2 z-20 flex flex-row-reverse items-center max-md:hidden">
                                    {/* Dots — always visible, right edge */}
                                    <div className="flex flex-col gap-3 items-end px-2 py-3">
                                        {displayed.map((_, i) => (
                                            <div
                                                key={i}
                                                className="w-[5px] h-[5px] rounded-full bg-gray-300 group-hover/promptnav:bg-gray-500 transition-colors duration-150"
                                            />
                                        ))}
                                    </div>
                                    {/* Popup — appears on hover; flex-col-reverse keeps scroll anchored to bottom (newest last) */}
                                    <div className="opacity-0 pointer-events-none group-hover/promptnav:opacity-100 group-hover/promptnav:pointer-events-auto transition-opacity duration-150 mr-2">
                                        <div className="w-64 max-h-72 overflow-y-auto bg-white border border-gray-200 rounded-2xl shadow-xl flex flex-col-reverse">
                                            {[...userPrompts].reverse().map((m, i) => {
                                                const realIdx = userPrompts.length - 1 - i;
                                                // Find the index of this message in filteredMessages
                                                const msgFiltIdx = filteredMessages.indexOf(m);
                                                return (
                                                    <button
                                                        key={realIdx}
                                                        type="button"
                                                        onClick={() => {
                                                            // Ensure the target message is in the visible window.
                                                            // visibleMessages = filteredMessages.slice(start) where
                                                            // start = max(0, filteredMessages.length - MSG_PAGE_SIZE - msgOffset)
                                                            const start = Math.max(0, filteredMessages.length - MSG_PAGE_SIZE - msgOffset);
                                                            if (msgFiltIdx >= 0 && msgFiltIdx < start) {
                                                                // Message is outside the visible window — expand offset so it becomes visible.
                                                                const neededOffset = filteredMessages.length - MSG_PAGE_SIZE - msgFiltIdx;
                                                                setMsgOffset(Math.max(0, neededOffset));
                                                                // Scroll after React re-renders with the new offset.
                                                                setTimeout(() => {
                                                                    const trueIdx = messages.indexOf(m);
                                                                    const el = containerRef.current?.querySelector(`[data-msg-idx="${trueIdx}"]`);
                                                                    el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                                                }, 80);
                                                            } else {
                                                                const trueIdx = messages.indexOf(m);
                                                                const el = containerRef.current?.querySelector(`[data-msg-idx="${trueIdx}"]`);
                                                                el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                                            }
                                                        }}
                                                        className="text-left px-4 py-3 text-xs text-gray-600 hover:bg-gray-50 hover:text-gray-900 truncate shrink-0 border-t border-gray-100 first:border-0 transition-colors duration-100"
                                                        title={String(m.content ?? '')}
                                                    >
                                                        {String(m.content ?? '')}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    </div>
                                </div>
                            );
                        })()}
                        <div className={cn("flex-1 overflow-y-auto p-6 max-md:p-3", voiceCallActive && "voice-call-hide-avatars")} ref={containerRef}>
                            <div className={cn(messagesAreaWidthClass, "mx-auto space-y-2 pb-32")}>
                                {/* Reconnecting banner — shown when WebSocket is disconnected or reconnecting */}
                                {!isConnected && messages.length > 0 && (
                                    <div className="sticky top-0 z-10 flex items-center justify-center gap-2 py-2 px-4 mb-2 rounded-lg bg-yellow-500/10 border border-yellow-500/30 text-yellow-400 text-sm">
                                        <svg className="animate-spin h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/></svg>
                                        <span>Verbindung wird wiederhergestellt…</span>
                                    </div>
                                )}
                                {/* Chat history is being fetched and nothing is cached yet → the working
                                    avatar + step-boxes (Option A) instead of a blank area */}
                                {historyLoading && messages.length === 0 && (
                                    <div className="min-h-[55vh] flex items-center justify-center">
                                        <ChatLoadingLine />
                                    </div>
                                )}
                                {/* Sub-Agent banner removed; reopen via tool cards or system log */}
                                {/* Empty state welcome is shown in the centered input block below */}

                                {/* Load earlier messages banner */}
                                {hiddenCount > 0 && (
                                    <button
                                        onClick={() => setMsgOffset(o => o + MSG_TURNS)}
                                        className="w-full py-2 px-4 mb-2 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-xs text-gray-400 hover:text-gray-200 transition-colors"
                                    >
                                        ↑ {hiddenCount} earlier message{hiddenCount !== 1 ? 's' : ''} — click to load more
                                    </button>
                                )}

                                {(() => {
                                    // Index of the most recent assistant message — only that one gets the avatar
                                    const lastBotTrueIndex = (() => {
                                        for (let k = messages.length - 1; k >= 0; k--) {
                                            if (messages[k].role === 'assistant') return k;
                                        }
                                        return -1;
                                    })();
                                    // Group each turn's tool rows under that turn's assistant, so they render as ONE
                                    // collapsible actions-timeline (thinking first, then the tools) instead of loose
                                    // rows. A "turn" is the span between user messages and is AGENTIC: it can hold
                                    // several assistant messages (think → tool → think → tool → … → answer). All of a
                                    // turn's thinking blocks + tools become ONE timeline, anchored on the turn's FIRST
                                    // assistant (stable while the turn streams, so nothing remounts), with the FINAL
                                    // answer rendered below. SAFE FALLBACK: a turn is grouped only if it has ≥1 tool and
                                    // no non-final assistant carries visible answer text (else we'd hide content) — any
                                    // other turn keeps today's per-row rendering.
                                    type TurnTl = { actions: { kind: 'think' | 'tool' | 'say'; msg: typeof visibleMessages[number] }[]; answerMsg: typeof visibleMessages[number] };
                                    const turnTimeline = new Map<number, TurnTl>(); // key: visibleMessages idx of the turn's first assistant (anchor)
                                    const consumedVmIdx = new Set<number>();        // tools + non-anchor assistants shown inside a timeline
                                    (() => {
                                        const vm = visibleMessages;
                                        const ans = (m: typeof vm[number]) => stripToolCallsJSON(parseContent(m.content).answer).trim();
                                        const flush = (s: number, e: number) => {
                                            const assistants: number[] = [];
                                            let toolCount = 0;
                                            for (let k = s; k < e; k++) {
                                                if (vm[k].role === 'assistant') assistants.push(k);
                                                else if (vm[k].role === 'tool') toolCount++;
                                            }
                                            if (toolCount < 1 || assistants.length < 1) return;
                                            const anchor = assistants[0];
                                            const answerIdx = assistants[assistants.length - 1];
                                            // Intermediate assistant text — a conversational line BETWEEN tool calls (e.g.
                                            // "Lass mich nochmal genauer nachschauen.") — is rendered as a first-class 'say'
                                            // rail step, NOT a reason to abandon grouping. Only the FINAL assistant's answer
                                            // (answerIdx) becomes the answer bubble below; every earlier assistant's visible
                                            // text becomes a 'say' step in order, alongside its 'think' if present.
                                            const actions: TurnTl['actions'] = [];
                                            for (let k = s; k < e; k++) {
                                                if (vm[k].role === 'assistant') {
                                                    if ((parseContent(vm[k].content).thought ?? '').trim() !== '') actions.push({ kind: 'think', msg: vm[k] });
                                                    if (k !== answerIdx && ans(vm[k]) !== '') actions.push({ kind: 'say', msg: vm[k] });
                                                } else if (vm[k].role === 'tool') {
                                                    actions.push({ kind: 'tool', msg: vm[k] });
                                                }
                                            }
                                            turnTimeline.set(anchor, { actions, answerMsg: vm[answerIdx] });
                                            for (let k = s; k < e; k++) {
                                                if (k !== anchor && (vm[k].role === 'tool' || vm[k].role === 'assistant')) consumedVmIdx.add(k);
                                            }
                                        };
                                        let segStart = 0;
                                        for (let k = 0; k < vm.length; k++) {
                                            if (vm[k].role === 'user' && k > segStart) { flush(segStart, k); segStart = k; }
                                        }
                                        flush(segStart, vm.length);
                                    })();
                                    return visibleMessages.map((msg, i) => {
                                        const trueIndex = messages.indexOf(msg);
                                        const prevMsg = i > 0 ? visibleMessages[i - 1] : null;
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
                                                    // Wake / system-activity message (e.g. a fired timer): render like an AGENT
                                                    // chat row — a timer-icon avatar (in the agent-avatar slot) + a speech bubble,
                                                    // but with an amber border (the same accent as the Action tag). Not a plain
                                                    // user/bot bubble. Sent as role="user" so it still creates a bubble boundary;
                                                    // matched here by kind, or by the "⏰ Timer fired" prefix when reloaded.
                                                    const _isWake = msg.kind === 'timer' || String(msg.content ?? '').startsWith('⏰ Timer fired');
                                                    if (_isWake) {
                                                        // Show only the user's note, not the internal "Act on it…" framing.
                                                        const _noteMatch = String(msg.content ?? '').match(/your note:\s*"([\s\S]*?)"/);
                                                        const _wakeText = (_noteMatch ? _noteMatch[1] : String(msg.content ?? '').replace(/^⏰\s*/, '')).trim();
                                                        // Two states: ACTIVE (E) while the agent is still handling the timer — real (dark) agent
                                                        // avatar + amber clock BADGE + amber bubble ("look here"); DONE (J) once it has replied —
                                                        // neutral dim avatar, neutral bubble, amber only in the "TIMER" label (quietly marked,
                                                        // like a normal agent message).
                                                        const _afterWake = messages.slice(trueIndex + 1);
                                                        const _wakeDone = _afterWake.some(m => m.role === 'user')
                                                            || (_afterWake.some(m => m.role === 'assistant' && String(m.content ?? '').trim().length > 0) && !isGenerating);
                                                        return (
                                                            <div className="flex gap-4 pt-4 justify-center">
                                                                <div className="w-full max-w-[85%] flex gap-4 items-start">
                                                                    {/* Avatar: the real agent avatar. ACTIVE → dark + amber clock badge; DONE → dim + no badge. */}
                                                                    <div className="relative shrink-0">
                                                                        <AgentAvatar mode="idle" dim={_wakeDone} />
                                                                        {!_wakeDone && (
                                                                            <span className="absolute -right-1.5 -bottom-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full border-2 border-white bg-amber-500 text-white shadow-sm">
                                                                                <AlarmClock className="h-2.5 w-2.5" />
                                                                            </span>
                                                                        )}
                                                                    </div>
                                                                    {/* Bubble + timestamp. ACTIVE → amber bubble; DONE → neutral bubble (amber stays only in the TIMER label). */}
                                                                    <div className="flex flex-col items-start min-w-0">
                                                                        <div className={cn(
                                                                            "rounded-2xl rounded-tl-none border px-5 py-3 text-[15px] leading-relaxed shadow-sm",
                                                                            _wakeDone ? "border-gray-200 bg-gray-50 text-gray-700" : "border-amber-300 bg-amber-50 text-amber-900"
                                                                        )}>
                                                                            <div className="mb-1 flex items-center gap-1.5 text-amber-500">
                                                                                <AlarmClock className="h-3.5 w-3.5" />
                                                                                <span className="text-[11px] font-semibold uppercase tracking-wide">Timer</span>
                                                                            </div>
                                                                            <div className="chat-markdown"><ChatMarkdown>{_wakeText}</ChatMarkdown></div>
                                                                        </div>
                                                                        <div className="w-full mt-0.5 text-left">
                                                                            <span className="text-[10px] text-gray-400" title={new Date(msg.timestamp).toLocaleString('en-US', userTimeFormat ? { hour12: userTimeFormat === '12h' } : undefined)}>{formatMessageTime(msg.timestamp, userTimeFormat)}</span>
                                                                        </div>
                                                                    </div>
                                                                </div>
                                                            </div>
                                                        );
                                                    }
                                                    // System/Router/Step messages are NOT rendered as rows. The live setup/plan
                                                    // indicator (SetupLine, below the list) shows the current step in ONE stable,
                                                    // continuously-animating line; the full trace stays in the logs/terminal.
                                                    if (msg.role === 'system') return null;

                                                    // Consumed into its turn's grouped actions-timeline (a tool, or a non-anchor
                                                    // assistant whose thinking is shown in the rail) → don't render the loose row.
                                                    if (consumedVmIdx.has(i)) return null;

                                                    // API empty error: show as system log (no bot bubble) for consistency
                                                    const apiEmptyErrorText = 'API returned empty responses repeatedly. Please try again.';
                                                    const isApiEmptyError = msg.role === 'assistant' && msg.content && (
                                                        msg.content.includes(apiEmptyErrorText) ||
                                                        msg.content.replace(/^\[Error\]\s*/i, '').trim() === apiEmptyErrorText
                                                    );
                                                    if (isApiEmptyError) {
                                                        const prevWasSystemApiErr = i > 0 && visibleMessages[i - 1].role === 'system';
                                                        return (
                                                            <div key={`system-${trueIndex}`} className={cn("flex justify-center", prevWasSystemApiErr ? "pt-0" : "pt-4")}>
                                                                <SystemStep message={`System: ${apiEmptyErrorText}`} />
                                                            </div>
                                                        );
                                                    }

                                                    // Render Tool Messages (same width as ThinkingDetails / assistant bubble content)
                                                    if (msg.role === 'tool') {
                                                        const toolLower = (msg.toolName || '').toLowerCase();
                                                        const isSubAgentTool = /(?:^|[^a-z])(librarian|research|document|coding|browser)_agent(?:$|[^a-z])/.test(toolLower);
                                                        const prevWasSystem = i > 0 && visibleMessages[i - 1].role === 'system';
                                                        return (
                                                            <div className={cn("flex justify-center animate-in fade-in slide-in-from-bottom-2 duration-300", prevWasSystem ? "pt-0" : "pt-4")}>
                                                                <div className="w-full max-w-[85%] flex gap-4">
                                                                    <div className="w-9 shrink-0" aria-hidden />
                                                                    <div className="flex-1 min-w-0">
                                                                        <div className={cn("max-w-[95%] rounded-lg transition-[outline] duration-150", stopHovered && msg.toolStatus === 'running' ? "outline outline-2 outline-red-400/60" : "")}>
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
                                                                <div className={cn(
                                                                    "flex gap-4 max-w-[85%] w-full items-start rounded-xl transition-all duration-300",
                                                                    stopHovered && isWorkflowRunning && trueIndex === messages.length - 1
                                                                        ? "outline outline-2 outline-red-400/60 shadow-[0_0_12px_4px_rgba(239,68,68,0.15)]"
                                                                        : ""
                                                                )}>
                                                                    <AgentAvatar />
                                                                    <WorkflowChatElement
                                                                        workflowId={msg.workflowId || ""}
                                                                        name={msg.workflowName || "Workflow"}
                                                                        initialSteps={msg.initialSteps}
                                                                    />
                                                                </div>
                                                            </div>
                                                        );
                                                    }

                                                    const isBot = msg.role === 'assistant';
                                                    // Agentic turns group into ONE timeline anchored on the turn's FIRST assistant; the
                                                    // visible ANSWER + Action then come from the turn's FINAL assistant (answerMsg), while
                                                    // each step's thinking is shown per-dot in the rail. Non-grouped reads from itself.
                                                    const turnTl = isBot ? turnTimeline.get(i) : undefined;
                                                    const hasTimeline = !!turnTl;
                                                    const answerMsg = turnTl ? turnTl.answerMsg : msg;
                                                    const parsedSelf = parseContent(msg.content);
                                                    const parsedAns = hasTimeline ? parseContent(answerMsg.content) : parsedSelf;
                                                    const thought = parsedSelf.thought;
                                                    const answer = parsedAns.answer;
                                                    const action = parsedAns.action;
                                                    const isThinkingComplete = parsedSelf.isThinkingComplete;
                                                    const isActionComplete = parsedAns.isActionComplete;
                                                    // Use trueIndex (position in full messages array) — visibleMessages may be a subset.
                                                    // For a grouped turn the answer streams into answerMsg (the turn's last assistant).
                                                    const answerTrueIndex = hasTimeline ? messages.indexOf(answerMsg) : trueIndex;
                                                    const isLastMessage = answerTrueIndex === messages.length - 1;
                                                    // Simple: thinking is done when the </think> tag is found (isThinkingComplete)
                                                    // For non-last messages, always treat as complete
                                                    const thinkingDone = trueIndex !== messages.length - 1 || isThinkingComplete;
                                                    const actionDone = !isLastMessage || isActionComplete;
                                                    // For user messages: don't show attachment content in bubble (strip --- FILE: ... --- blocks); keep chips from msg.files or parsed from content after reload
                                                    const attachmentStripped = !isBot ? stripAttachmentBlocks(msg.content) : null;
                                                    const displayAnswer = !isBot && attachmentStripped ? attachmentStripped.text : answer;
                                                    const displayFiles = !isBot && (msg.files?.length ? msg.files : (attachmentStripped?.fileNames.length ? attachmentStripped.fileNames.map(name => ({ name, mimeType: '' })) : undefined));

                                                    // Filter out tool_calls JSON from bot answers
                                                    const cleanAnswer = isBot ? stripToolCallsJSON(answer) : answer;
                                                    // Add top margin if following a system step
                                                    const prevWasSystem = i > 0 && visibleMessages[i - 1].role === 'system';
                                                    // Only show the speech bubble when there is visible content (avoid empty bubbles)
                                                    const hasBubbleContent = !isBot
                                                        ? !!(displayAnswer || (displayFiles && displayFiles.length > 0))
                                                        : !!((cleanAnswer && cleanAnswer.trim() !== '') || parseWorkflowAsync(answer));
                                                    // When there is nothing to show (no bubble, no thinking), don't render the row at all (no avatar, no timestamp, no empty space)
                                                    const hasVisibleContent = isBot
                                                        // grouped anchor always renders (it owns the turn's timeline); otherwise keep visible
                                                        // when there's a think tag at all (even after reload when thought may be empty)
                                                        ? (hasTimeline || hasBubbleContent || !!(thought && thought.trim() !== '') || !!(action && action.trim() !== '') || msg.content.includes('<think>'))
                                                        : hasBubbleContent;
                                                    if (!hasVisibleContent) return null;

                                                    // ── Per-turn actions-timeline (anchor = first assistant). Build the ordered rail items
                                                    //    (each step's thinking + each tool) + the collapse state. hasTimeline / answerMsg are
                                                    //    computed above; non-grouped messages keep the plain inline rendering.
                                                    const isLatestBot = isBot && (hasTimeline ? answerTrueIndex === lastBotTrueIndex : trueIndex === lastBotTrueIndex);
                                                    const liveThinking = hasTimeline
                                                        ? (answerMsg.content.includes('<think>') && !parsedAns.isThinkingComplete)
                                                        : (msg.content.includes('<think>') && !isThinkingComplete);
                                                    // Per-bubble override for proactive background messages: a thinking-run message
                                                    // plays `idea` ("found the solution"); a nudge ("are you there?") plays a rotating
                                                    // away scene (stable per bubble via its timestamp). Applies whether or not the row is
                                                    // latest — it dims via botAvatarDim like any other past bubble.
                                                    const kindAvatar: AvatarMode | null =
                                                        msg.kind === 'thinking' ? 'idea'
                                                            : msg.kind === 'nudge' ? awayModeFor(msg.timestamp)
                                                                : null;
                                                    const botAvatarMode: AvatarMode = kindAvatar ?? ((isLatestBot && !loading)
                                                        // delegate has TOP priority: while a sub-agent runs it stays stable, so tool-outcome
                                                        // flashes (agentReaction) can't interrupt it and make the sub-agent re-spawn mid-run.
                                                        // a tool with its own scene wins (incl. browser_agent → globe); other sub-agents → delegate.
                                                        ? (activeToolMode ? activeToolMode
                                                            : isSubAgentRunning ? 'delegate'
                                                            : agentReaction ? agentReaction
                                                            : gateRequest ? 'permission'
                                                            : !isGenerating ? 'idle'
                                                            : liveThinking ? 'thinking'
                                                            : 'talking')
                                                        : 'idle');
                                                    const botAvatarDim = !(isLatestBot && !loading);
                                                    // Expanded while the latest turn is still running; collapses only when generation
                                                    // ENDS (so an intermediate answer line mid-turn no longer folds the rail while the
                                                    // agent is still working) and for all history — a manual click overrides either way.
                                                    // A FINISHED turn must stay collapsed while a NEW prompt is pending, so it does not
                                                    // briefly re-expand on send: detect a `user` message that comes AFTER the latest bot
                                                    // turn (the just-sent prompt). During a live run no user message follows the latest
                                                    // bot output, so the running turn — and its thinking block, which lives inside this
                                                    // timeline — stay expanded for the whole run. We scan for a user message past
                                                    // lastBotTrueIndex rather than checking the literal tail role: on send a status/system
                                                    // message can be appended AFTER the user prompt, so the tail is not reliably `user`.
                                                    const userPromptPending = messages.some((m, i) => i > lastBotTrueIndex && m.role === 'user');
                                                    const timelineNaturalExpanded = isLatestBot && isGenerating && !userPromptPending;
                                                    const tlManual = timelineExpand.get(msg.timestamp);
                                                    const timelineExpanded = tlManual !== undefined ? tlManual : timelineNaturalExpanded;
                                                    const timelineActions: TimelineAction[] = hasTimeline ? turnTl!.actions.map((act) => {
                                                        const m = act.msg;
                                                        const mIdx = messages.indexOf(m);
                                                        if (act.kind === 'think') {
                                                            const pc = parseContent(m.content);
                                                            const tdone = mIdx !== messages.length - 1 || pc.isThinkingComplete;
                                                            return {
                                                                key: `tl-think-${mIdx}`,
                                                                kind: 'think' as const,
                                                                state: (tdone ? 'done' : 'pending') as TimelineAction['state'],
                                                                node: <ThinkingDetails thought={pc.thought ?? ''} isComplete={tdone} durationKey={m.timestamp} />,
                                                            };
                                                        }
                                                        if (act.kind === 'say') {
                                                            // Intermediate spoken line (between tool calls). Reuse the bot markdown
                                                            // renderer in a lighter card than the final answer bubble so it reads as
                                                            // an in-flight aside, not the turn's conclusion. Always 'done' (already
                                                            // emitted) so it never steals the avatar's active dot from a running tool.
                                                            const sayText = stripToolCallsJSON(parseContent(m.content).answer);
                                                            return {
                                                                key: `tl-say-${mIdx}`,
                                                                kind: 'say' as const,
                                                                state: 'done' as TimelineAction['state'],
                                                                node: (
                                                                    <div className="max-w-[95%] rounded-2xl rounded-tl-none border border-gray-200 bg-white px-4 py-2 text-[14px] leading-relaxed text-gray-700 shadow-sm">
                                                                        <div className="chat-markdown"><ChatMarkdown>{sayText}</ChatMarkdown></div>
                                                                    </div>
                                                                ),
                                                            };
                                                        }
                                                        const tln = (m.toolName || '').toLowerCase();
                                                        const isSub = /(?:^|[^a-z])(librarian|research|document|coding|browser)_agent(?:$|[^a-z])/.test(tln);
                                                        const st = m.toolStatus || 'completed';
                                                        return {
                                                            key: `tl-tool-${mIdx}`,
                                                            kind: 'tool' as const,
                                                            state: (st === 'running' ? 'pending' : st === 'error' ? 'error' : 'done') as TimelineAction['state'],
                                                            node: (
                                                                <div className={cn("max-w-[95%] rounded-lg transition-[outline] duration-150", stopHovered && m.toolStatus === 'running' ? "outline outline-2 outline-red-400/60" : "")}>
                                                                    <ToolMessage
                                                                        id={m.toolId || `tool-${mIdx}`}
                                                                        name={m.toolName || 'Unknown Tool'}
                                                                        status={m.toolStatus || 'completed'}
                                                                        result={m.content}
                                                                        args={m.toolArgs}
                                                                        startTime={m.toolStartTime}
                                                                        endTime={m.toolEndTime}
                                                                        onToggleScroll={preserveChatScroll}
                                                                        onToggle={isSub ? (nextExpanded: boolean) => { if (nextExpanded) openSubAgentWindow(true); else closeSubAgentWindow(true); } : undefined}
                                                                    />
                                                                </div>
                                                            ),
                                                        };
                                                    }) : [];

                                                    const bubbleContent = (
                                                        <>
                                                            {isBot && thought && !hasTimeline && <div className="mb-3"><ThinkingDetails thought={thought} isComplete={thinkingDone} durationKey={msg.timestamp} /></div>}

                                                            {isBot && action && <ActionDetails action={action} isComplete={actionDone} />}

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
                                                                                isBot ? "bg-white text-gray-800 rounded-tl-none border border-transparent" : "bg-gray-800 dark:bg-[#242424] text-white rounded-tr-none",
                                                                                // Voice-call delegation: the voice agent (not the
                                                                                // user) wrote this - red ring + soft STATIC glow
                                                                                // (never an animated shadow, GPU-leak rule).
                                                                                !isBot && msg.kind === 'voice_delegation' && "border border-red-500/60 shadow-[0_0_10px_2px_rgba(239,68,68,0.22)]")}>
                                                                                {!isBot && displayAnswer.length > 1169 ? (
                                                                                    <>
                                                                                        <div className="chat-markdown">
                                                                                            <ChatMarkdown dark>{expandedMsgs.has(trueIndex) ? displayAnswer : displayAnswer.slice(0, 1169) + '…'}</ChatMarkdown>
                                                                                        </div>
                                                                                        <button
                                                                                            onClick={() => toggleMsgExpanded(trueIndex)}
                                                                                            className="mt-2 text-xs text-gray-400 hover:text-white transition-colors"
                                                                                        >
                                                                                            {expandedMsgs.has(trueIndex) ? '▲ Show less' : `▼ Show more (${displayAnswer.length.toLocaleString()} chars)`}
                                                                                        </button>
                                                                                    </>
                                                                                ) : isBot && cleanAnswer.length > BOT_COLLAPSE_THRESHOLD && messages.slice(trueIndex + 1).some(m => m.role === 'user') && !expandedBotMsgs.has(msg.timestamp) ? (
                                                                                    <>
                                                                                        <div className="chat-markdown">
                                                                                            <ChatMarkdown>{cleanAnswer.slice(0, BOT_COLLAPSED_PREVIEW) + '…'}</ChatMarkdown>
                                                                                        </div>
                                                                                        <button
                                                                                            onClick={() => setExpandedBotMsgs(prev => new Set(prev).add(msg.timestamp))}
                                                                                            className="mt-1 text-xs text-gray-400 hover:text-gray-700 transition-colors"
                                                                                        >
                                                                                            ▼ Show full response ({cleanAnswer.length.toLocaleString()} chars)
                                                                                        </button>
                                                                                    </>
                                                                                ) : (
                                                                                    <div className="chat-markdown"><ChatMarkdown dark={!isBot}>{isBot ? cleanAnswer : displayAnswer}</ChatMarkdown></div>
                                                                                )}
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
                                                                    {/* User message: inline image thumbnails */}
                                                                    {!isBot && msg.images && msg.images.length > 0 && (
                                                                        <div className="flex gap-2 flex-wrap mt-1 justify-end">
                                                                            {msg.images.map((img, idx) => (
                                                                                // eslint-disable-next-line @next/next/no-img-element
                                                                                <img
                                                                                    key={idx}
                                                                                    src={img.url}
                                                                                    alt={img.name}
                                                                                    className="h-24 max-w-[180px] object-cover rounded-xl border border-gray-200 shadow-sm"
                                                                                    title={img.name}
                                                                                />
                                                                            ))}
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
                                                                    {/* Code Viewer chip — shown on user messages where a file was open */}
                                                                    {!isBot && msg.codeViewerFile && (
                                                                        <div className="flex justify-end mt-1">
                                                                            <button
                                                                                onClick={() => { if (isHtmlFile(msg.codeViewerFile!.path)) { setHtmlViewerState({ isOpen: true, filePath: msg.codeViewerFile!.path, title: msg.codeViewerFile!.name }); } else { setCodeViewerState(prev => ({ ...prev, isOpen: true, filePath: msg.codeViewerFile!.path, title: msg.codeViewerFile!.name })); } setShowSubAgentPanel(true); }}
                                                                                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-violet-50 border border-violet-200 text-violet-700 text-[11px] font-medium hover:bg-violet-100 transition-colors"
                                                                                title={msg.codeViewerFile.path}
                                                                            >
                                                                                <svg xmlns="http://www.w3.org/2000/svg" className="w-3 h-3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                                                                                <span className="font-mono">{msg.codeViewerFile.name}</span>
                                                                                <span className="text-violet-400 font-normal">.{msg.codeViewerFile.ext} · {msg.codeViewerFile.lineCount}L</span>
                                                                            </button>
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

                                                            {/* Subtle timestamp below the bubble (messenger style);
                                                                voice-call delegations carry a "voice agent" tag left
                                                                of the time so the sender is obvious. */}
                                                            {(msg.role === 'user' || msg.role === 'assistant') && (
                                                                <div className={cn("w-full mt-0.5", isBot ? "text-left" : "text-right")}>
                                                                    {!isBot && msg.kind === 'voice_delegation' && (
                                                                        <span className="text-[10px] font-medium text-red-500/80 mr-1.5">{tMain('voiceAgentTag')}</span>
                                                                    )}
                                                                    <span className="text-[10px] text-gray-400" title={new Date(msg.timestamp).toLocaleString('en-US', userTimeFormat ? { hour12: userTimeFormat === '12h' } : undefined)}>{formatMessageTime(msg.timestamp, userTimeFormat)}</span>
                                                                </div>
                                                            )}
                                                            {/* Show status steps below the active message if streaming */}
                                                            {loading && isBot && i === visibleMessages.length - 1 && statusMessage && /[a-zA-Z0-9]/.test(statusMessage) && (
                                                                <span className="text-[10px] text-gray-400 mt-1 ml-1 animate-in fade-in">{statusMessage}</span>
                                                            )}
                                                            {/* File chips: html → HtmlViewer, code → CodeViewer, others → download */}
                                                            {isBot && msg.downloadFiles && msg.downloadFiles.length > 0 && (
                                                                <div className="mt-2 flex flex-wrap gap-2">
                                                                    {msg.downloadFiles.map((f, fi) => (
                                                                        isImageFile(f.path) ? (
                                                                            <button
                                                                                key={fi}
                                                                                onClick={() => openImageInViewer(f.path, f.name)}
                                                                                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-emerald-200 bg-emerald-50 text-emerald-700 text-xs font-medium hover:bg-emerald-100 transition-colors"
                                                                            >
                                                                                <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                                                                                {f.name}
                                                                            </button>
                                                                        ) : isHtmlFile(f.path) ? (
                                                                            <button
                                                                                key={fi}
                                                                                onClick={() => { setHtmlViewerState({ isOpen: true, filePath: f.path, title: f.name }); setShowSubAgentPanel(true); }}
                                                                                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-orange-200 bg-orange-50 text-orange-700 text-xs font-medium hover:bg-orange-100 transition-colors"
                                                                            >
                                                                                <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
                                                                                {f.name}
                                                                            </button>
                                                                        ) : (isCodeFile(f.path) || isTextFile(f.path) || !f.name.includes('.')) ? (
                                                                            // Code, text, AND extension-less files open in the in-app viewer
                                                                            // (it fetches + shows errors). Extension-less was the live
                                                                            // dead-end: it fell through to a raw navigation link.
                                                                            <button
                                                                                key={fi}
                                                                                onClick={() => { setCodeViewerState({ isOpen: true, filePath: f.path, title: f.name }); setShowSubAgentPanel(true); }}
                                                                                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-violet-200 bg-violet-50 text-violet-700 text-xs font-medium hover:bg-violet-100 transition-colors"
                                                                            >
                                                                                <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                                                                                {f.name}
                                                                            </button>
                                                                        ) : (
                                                                            // Binary/other: safe blob download (a 403 shows a toast, never
                                                                            // a full-window navigation to raw JSON).
                                                                            <button
                                                                                key={fi}
                                                                                onClick={() => safeDownloadFile(f.path, f.name)}
                                                                                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-blue-200 bg-blue-50 text-blue-700 text-xs font-medium hover:bg-blue-100 transition-colors"
                                                                            >
                                                                                <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                                                                                {f.name}
                                                                            </button>
                                                                        )
                                                                    ))}
                                                                </div>
                                                            )}
                                                        </>
                                                    );

                                                    return (
                                                        <div key={`bubble-${trueIndex}`} data-role={msg.role} data-msg-idx={trueIndex} className={cn("flex gap-4 pt-4 animate-in fade-in slide-in-from-bottom-2 duration-300", isBot ? "justify-center" : "justify-end", prevWasSystem ? "pt-2" : "pt-4")}>
                                                            {isBot ? (
                                                                hasTimeline ? (
                                                                    <div className={cn(
                                                                        "w-full max-w-[85%] max-md:max-w-full rounded-2xl transition-all duration-300",
                                                                        isLatestBot && stopHovered && isGenerating
                                                                            ? "outline outline-2 outline-red-400/60 shadow-[0_0_12px_4px_rgba(239,68,68,0.15)]"
                                                                            : ""
                                                                    )}>
                                                                        <TurnActionsTimeline
                                                                            actions={timelineActions}
                                                                            avatarMode={botAvatarMode}
                                                                            avatarDim={botAvatarDim}
                                                                            isLive={isLatestBot && isGenerating}
                                                                            expanded={timelineExpanded}
                                                                            onToggle={() => setTimelineExpand(prev => {
                                                                                const next = new Map(prev);
                                                                                next.set(msg.timestamp, !timelineExpanded);
                                                                                return next;
                                                                            })}
                                                                        >
                                                                            {bubbleContent}
                                                                        </TurnActionsTimeline>
                                                                    </div>
                                                                ) : (
                                                                <div className="w-full max-w-[85%] max-md:max-w-full flex gap-4 max-md:gap-2">
                                                                    <AgentAvatar mode={botAvatarMode} dim={botAvatarDim} />
                                                                    <div className={cn(
                                                                        "flex flex-col flex-1 min-w-0 shrink-0 items-start w-full rounded-2xl transition-all duration-300",
                                                                        isLatestBot && stopHovered && isGenerating
                                                                            ? "outline outline-2 outline-red-400/60 shadow-[0_0_12px_4px_rgba(239,68,68,0.15)]"
                                                                            : ""
                                                                    )}>
                                                                        {bubbleContent}
                                                                    </div>
                                                                </div>
                                                                )
                                                            ) : (
                                                                <div className={cn("flex flex-col", "max-w-[75%] items-end shrink-0")}>
                                                                    {bubbleContent}
                                                                </div>
                                                            )}
                                                        </div>
                                                    );
                                                })()}
                                            </Fragment>
                                        );
                                    });
                                })()}

                                {/* Live setup/plan indicator — one stable element for the whole setup/routing
                                    phase (loops continuously, only the text updates; system rows are suppressed
                                    above). Shown while the latest message is a system step during an active turn. */}
                                {!historyLoading && (loading || isGenerating) && visibleMessages[visibleMessages.length - 1]?.role === 'system' && (
                                    <div className="flex gap-4 justify-center pt-4">
                                        <div className="w-full max-w-[85%]">
                                            <SetupLine message={String(visibleMessages[visibleMessages.length - 1].content ?? '')} />
                                        </div>
                                    </div>
                                )}

                                {loading && messages.length > 0 && !(
                                    // Hide loading bubble when a tool is actively running (tool card shows spinner)
                                    (messages[messages.length - 1].role === 'tool' &&
                                    messages[messages.length - 1].toolStatus === 'running') ||
                                    // Or when last message is a system step (RAG, Router, Final tools, etc.) – no redundant avatar + dots row
                                    messages[messages.length - 1].role === 'system'
                                ) && (
                                    <div className="flex gap-4 justify-center pt-4">
                                        <div className="w-full max-w-[85%] flex gap-4">
                                            <AgentAvatar mode="plan" />
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
                                "absolute left-0 right-0 w-full z-40 transition-all duration-500 ease-out max-md:pointer-events-none",
                                messages.length === 0
                                    ? "top-1/2 -translate-y-1/2 bottom-auto max-md:top-[38%]"
                                    : "top-auto bottom-0 translate-y-0"
                            )}
                        >
                            <div className="bg-gradient-to-t from-white via-white to-transparent pt-10 pb-8 px-6 max-md:px-3 max-md:pt-6 max-md:pb-[max(1.5rem,calc(var(--safe-bottom)+0.75rem))] max-md:[&>*]:pointer-events-auto">
                                {messages.length === 0 && !historyLoading && (
                                    <div className={cn(chatWidthClass, "mx-auto mb-4 text-center")}>
                                        <div className="flex justify-center mb-8 max-md:mb-4 origin-center scale-[1.8] max-md:scale-[1.35]">
                                            <AgentAvatar mode="idle" />
                                        </div>
                                        <h2 className="text-xl font-bold text-gray-800">
                                            <TypingTitle text={welcomeText} />
                                        </h2>
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
                                                            ? "bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white"
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
                                    <div className={cn(chatWidthClass, "mx-auto mb-2 flex items-center gap-2")}>
                                        {/* Spacer mirroring the input's w-9 stop-button slot, so the banner is
                                            centered on the message box rather than flush-left under the row. */}
                                        <div className="w-9 shrink-0" aria-hidden="true" />
                                        <div className="flex-1 min-w-0 flex justify-center">
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
                                    </div>
                                )}

                                {/* Attachment Indexing Banner — same UI as the Memory Learning banner,
                                    shown while the LLM indexes attached documents for retrieval. */}
                                {activeAttachmentIndexStatus && (
                                    <div className={cn(chatWidthClass, "mx-auto mb-2 flex items-center gap-2")}>
                                        <div className="w-9 shrink-0" aria-hidden="true" />
                                        <div className="flex-1 min-w-0 flex justify-center">
                                            <div className={cn(
                                                "flex items-center gap-2 px-4 py-2 rounded-xl border shadow-sm transition-all animate-in fade-in slide-in-from-bottom-2",
                                                activeAttachmentIndexStatus === 'indexing'
                                                    ? "bg-amber-50 border-amber-300 text-amber-700"
                                                    : activeAttachmentIndexStatus === 'error'
                                                        ? "bg-red-50 border-red-300 text-red-700"
                                                        : "bg-green-50 border-green-300 text-green-700"
                                            )}>
                                                {activeAttachmentIndexStatus === 'indexing' ? (
                                                    <div className="w-4 h-4 border-2 border-amber-500 border-t-transparent rounded-full animate-spin" />
                                                ) : activeAttachmentIndexStatus === 'error' ? (
                                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                                    </svg>
                                                ) : (
                                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                                                    </svg>
                                                )}
                                                <span className="text-sm font-medium">
                                                    {activeAttachmentIndexStatus === 'indexing'
                                                        ? tMain('indexingAttachments', { count: activeAttachmentIndexCount || 1 })
                                                        : activeAttachmentIndexStatus === 'error'
                                                            ? tMain('indexingError')
                                                            : tMain('indexingComplete')}
                                                </span>
                                            </div>
                                        </div>
                                    </div>
                                )}

                                {/* File chips are now rendered inside the assistant message bubble (see msg.downloadFiles above) */}

                                {/* Model download progress (visible when downloading, e.g. after closing Settings) */}
                                {downloadModelStatus?.status === 'downloading' && (
                                    <div className={cn(chatWidthClass, "mx-auto mb-2 flex items-center gap-2")}>
                                        {/* spacer mirroring the input's w-9 stop-button slot, so the banner lines up with the message box */}
                                        <div className="w-9 shrink-0" aria-hidden="true" />
                                        <div className="flex-1 min-w-0 flex items-center gap-3 px-4 py-2.5 rounded-xl border border-gray-200 bg-white shadow-sm">
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
                                <div className={cn(chatWidthClass, "mx-auto mb-1 flex items-center gap-2 min-h-[16px]")}>
                                    {/* Mirror the input row geometry: the stop-button column (w-9) only exists
                                        while generating, so add the matching spacer ONLY then — otherwise the
                                        indicators sit flush-left, aligned with the (slot-less) input field. */}
                                    {(isGenerating || isWorkflowRunning || isSubAgentRunning || isStoppingGeneration || stopPulsing || isIndexing) && (
                                        <div className="w-9 shrink-0" />
                                    )}
                                    <div className="flex min-w-0 flex-1 items-baseline justify-end gap-2 px-2 max-md:flex-wrap max-md:gap-y-1">
                                    {/* Workspace chip: leftmost element; mr-auto pushes RAG/tokens to the right edge */}
                                    {workspaceInfo?.path && (
                                        <span
                                            className="mr-auto inline-flex items-center gap-1 text-[10px] font-mono text-gray-400 opacity-80 px-2 py-0.5 rounded cursor-pointer border border-gray-200 leading-none hover:text-violet-600 hover:opacity-100 hover:bg-violet-50 hover:border-violet-200 transition-all select-none"
                                            title={workspaceInfo.path}
                                            onClick={() => { setWorkspaceNavHist([]); setIsWorkspaceModalOpen(true); refreshWorkspace(); }}
                                        >
                                            <Folder size={10} className="shrink-0" />
                                            <span className="max-w-[160px] truncate">{workspaceInfo.name}</span>
                                        </span>
                                    )}
                                    {ragResults?.sources?.length > 0 && (
                                        <div ref={ragGroupRef} className="group relative inline-flex items-center pt-3">
                                            <span
                                                tabIndex={0}
                                                role="button"
                                                onClick={() => setRagSnipOpen((o) => !o)}
                                                className="text-[10px] font-mono text-gray-400 opacity-80 px-2 py-0.5 rounded cursor-help max-md:cursor-pointer border border-gray-200 bg-transparent leading-none outline-none focus:outline-none focus-visible:ring-1 focus-visible:ring-violet-300 group-hover:text-violet-600 group-hover:opacity-100 group-hover:bg-violet-50 group-hover:border-violet-200 group-focus-within:text-violet-600 group-focus-within:opacity-100 group-focus-within:bg-violet-50 group-focus-within:border-violet-200 transition-all"
                                                title="RAG snippets passed to model this turn"
                                            >
                                                {tMain('ragHits', { count: ragResults.sources.length })}
                                            </span>
                                            <div className={cn("hidden group-hover:block group-focus-within:block absolute right-0 bottom-full mb-0 pb-2 z-[80] w-80 max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-xl p-3 text-left max-md:!fixed max-md:!inset-x-2 max-md:!bottom-[88px] max-md:!top-auto max-md:!w-auto max-md:!max-h-[55vh]", ragSnipOpen && "max-md:!block")}>
                                                <div className="text-xs font-semibold text-gray-700 mb-2">{tMain('ragSnippetsThisTurn')}</div>
                                                <div className="space-y-2">
                                                    {[...ragResults.sources].sort((a: { score?: number }, b: { score?: number }) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 10).map((s: { text?: string; full_text?: string; score?: number; metadata?: { title?: string; source?: string; tags?: string[] } }, i: number) => {
                                                        const src = s.metadata?.source;
                                                        const title = s.metadata?.title;
                                                        const displayLabel = title
                                                            ? title.replace(/^Attachment:\s*/i, '').slice(0, 60)
                                                            : src
                                                                ? src.replace(/^memory\//, '')
                                                                : null;
                                                        const tags = s.metadata?.tags;
                                                        return (
                                                        <div key={i} className="text-xs text-gray-600 bg-gray-50 p-2 rounded border border-gray-100">
                                                            <div className="flex items-center justify-between gap-2 mb-1">
                                                                {displayLabel && (
                                                                    <span className="text-gray-400 truncate text-[10px] max-w-[180px]" title={title ?? src ?? ''}>{displayLabel}</span>
                                                                )}
                                                                {s.score !== undefined && <span className="text-violet-600 font-mono shrink-0 ml-auto">{(s.score * 100).toFixed(0)}%</span>}
                                                            </div>
                                                            {tags && tags.length > 0 && (
                                                                <div className="flex flex-wrap gap-1 mb-1">
                                                                    {tags.map((t, ti) => (
                                                                        <span key={ti} className="text-[10px] bg-violet-50 text-violet-600 border border-violet-200 px-1 py-0.5 rounded">#{t}</span>
                                                                    ))}
                                                                </div>
                                                            )}
                                                            <div className="line-clamp-3">{(s.full_text ?? s.text ?? '').slice(0, 300)}{((s.full_text ?? s.text ?? '').length > 300 ? '…' : '')}</div>
                                                        </div>
                                                        );
                                                    })}
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
                                            {Math.round(contextStats.percent)}%<span className="max-md:hidden"> ({contextStats.tokens.toLocaleString()}/{contextStats.max_tokens.toLocaleString()})</span>
                                        </span>
                                    )}
                                    </div>
                                </div>

                                {/* Stop button left of message box — show when chat is loading, a workflow is running, or a sub-agent is active.
                                    During a live call the SLOT stays reserved even without a button:
                                    otherwise the call bar jumps 44px wider/narrower whenever the
                                    stop button appears (live report: bar "too long" until stop shows). */}
                                <div className={cn(chatWidthClass, "mx-auto flex items-center")}>
                                    {(isGenerating || isWorkflowRunning || isSubAgentRunning || isStoppingGeneration || stopPulsing || isIndexing || (voiceCallActive && !voiceCallClosing)) && (
                                        <div className="w-9 mr-2 shrink-0 flex items-center justify-center">
                                        {(isGenerating || isWorkflowRunning || isSubAgentRunning || isStoppingGeneration || stopPulsing || isIndexing) && (
                                            <div className="relative flex items-center justify-center">
                                                {/* Hover aura — inline, only needs to surround the button itself */}
                                                {stopHovered && !stopPulsing && (
                                                    <span className="absolute inset-0 rounded-full pointer-events-none"
                                                        style={{ boxShadow: '0 0 0 6px rgba(239,68,68,0.28), 0 0 16px 8px rgba(239,68,68,0.15)', borderRadius: '50%' }} />
                                                )}
                                                <button
                                                    ref={stopBtnRef}
                                                    type="button"
                                                    onClick={stopGeneration}
                                                    disabled={isStoppingGeneration}
                                                    onMouseEnter={() => setStopHovered(true)}
                                                    onMouseLeave={() => setStopHovered(false)}
                                                    title={isStoppingGeneration ? tMain('stoppingGeneration') : tMain('stopGeneration')}
                                                    className={cn(
                                                        "relative z-10 p-2 rounded-full text-white text-sm font-medium transition-all shadow-md flex items-center justify-center animate-in fade-in slide-in-from-bottom-2",
                                                        isStoppingGeneration
                                                            ? "bg-red-500 cursor-wait"
                                                            : "bg-red-500 hover:bg-red-600"
                                                    )}
                                                >
                                                    {isStoppingGeneration ? (
                                                        <Loader2 size={12} className="animate-spin" />
                                                    ) : (
                                                        <Square size={12} fill="currentColor" />
                                                    )}
                                                </button>
                                            </div>
                                        )}
                                        </div>
                                    )}
                                    <form
                                        onSubmit={sendMessage}
                                        className={cn(
                                            "relative flex-1 min-w-0 flex items-end rounded-2xl border shadow-xl transition-all overflow-hidden",
                                            voiceCallActive && !voiceCallClosing
                                                ? "border-red-500/60 bg-[#fdecec] dark:bg-[#2a1a1a]"
                                                : "bg-white border-gray-200 focus-within:border-gray-400"
                                        )}
                                    >
                                        {voiceCallActive && <VoiceCallBar />}
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
                                                "shrink-0 ml-2 mb-1.5 h-10 w-10 flex items-center justify-center rounded-xl transition-colors",
                                                attachedImages.length > 0 ? "text-violet-600 hover:bg-violet-50" : documentViewerState.isOpen ? "text-blue-600 hover:bg-blue-50" : "text-gray-400 hover:text-gray-700 hover:bg-gray-100"
                                            )}
                                            title={tMain('attachmentsDocumentViewer')}
                                        >
                                            <Paperclip size={19} />
                                        </button>
                                        <div className="flex-1 relative flex flex-col min-w-0">
                                            {attachedImages.length > 0 && (
                                                <div className="flex flex-wrap gap-2 px-2 pt-2 pb-1 border-b border-gray-100">
                                                    {attachedImages.map((img) => (
                                                        <div key={img.id} className="relative group shrink-0">
                                                            {/* eslint-disable-next-line @next/next/no-img-element */}
                                                            <img
                                                                src={img.url}
                                                                alt={img.name}
                                                                className="h-16 w-16 object-cover rounded-lg border border-gray-200"
                                                                title={img.name}
                                                            />
                                                            <button
                                                                type="button"
                                                                onClick={() => setAttachedImages(prev => prev.filter(i => i.id !== img.id))}
                                                                className="absolute -top-1.5 -right-1.5 bg-gray-800 text-white rounded-full w-4 h-4 flex items-center justify-center text-[10px] opacity-0 group-hover:opacity-100 transition-opacity dark:bg-[#e6e6e6] dark:text-[#181818]"
                                                            >✕</button>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                            {imageViewerState.isOpen && imageMark && (
                                                <div className="flex flex-wrap items-center gap-1.5 px-2 pt-2 pb-1 border-b border-gray-100">
                                                    <span className="inline-flex items-center gap-1.5 rounded-full border border-yellow-300 bg-yellow-50 px-2.5 py-1 text-xs font-medium text-yellow-800">
                                                        <span className="h-2 w-2 rounded-sm bg-yellow-400" />
                                                        Markierung aktiv — deine Frage bezieht sich auf den markierten Bereich
                                                        <button
                                                            type="button"
                                                            onClick={clearImageMark}
                                                            className="ml-0.5 rounded-full px-1 leading-none text-yellow-700 hover:bg-yellow-200"
                                                            title="Clear marking"
                                                        >✕</button>
                                                    </span>
                                                </div>
                                            )}
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
                                                {/* inline-autocomplete ghost: mirrors the input as transparent text so the
                                                    grey suggestion sits exactly at the cursor. Must wrap + scroll EXACTLY
                                                    like the textarea (pre-wrap/break-words + synced scrollTop), or on
                                                    multi-line input the suggestion drifts onto the wrong line. */}
                                                <div ref={ghostRef} className="absolute inset-0 py-4 px-1 pointer-events-none text-sm text-gray-400 whitespace-pre-wrap break-words overflow-hidden">
                                                    <span className="text-transparent">{input}</span>
                                                    {suggestion}
                                                </div>
                                                <textarea
                                                    ref={inputRef}
                                                    rows={1}
                                                    value={input}
                                                    onChange={handleInputChange}
                                                    onScroll={(e) => { if (ghostRef.current) ghostRef.current.scrollTop = e.currentTarget.scrollTop; }}
                                                    onKeyDown={handleKeyDown}
                                                    placeholder={isIndexing ? tMain('indexingAttachments', { count: activeAttachmentIndexCount || 1 }) : input ? "" : "Ask anything..."}
                                                    className="w-full min-h-[2.5rem] max-md:min-h-[3.25rem] max-h-[12.5rem] py-4 px-1 bg-transparent border-none focus:ring-0 focus:outline-none text-sm relative z-10 resize-none overflow-y-auto"
                                                    // Chat-while-subagent-runs: never lock typing while a sub-agent is
                                                    // active — a lingering isGenerating (delegation turn / late events)
                                                    // must not freeze the input for the whole run.
                                                    disabled={(isGenerating && !isSubAgentRunning) || isIndexing}
                                                />
                                            </div>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={isRecording ? stopRecording : startRecording}
                                            disabled={isProcessingAudio || (isGenerating && !isSubAgentRunning)}
                                            className={cn(
                                                "shrink-0 mb-1.5 mr-2 h-10 w-10 flex items-center justify-center rounded-xl transition-colors",
                                                isRecording ? "bg-red-500 text-white" :
                                                    isProcessingAudio ? "text-gray-400" :
                                                        "text-gray-500 hover:text-gray-900 hover:bg-gray-100 disabled:text-gray-300"
                                            )}
                                            style={{
                                                boxShadow: isRecording ? `0 0 0 ${Math.min(volume / 5, 15)}px rgba(239, 68, 68, 0.4)` : 'none',
                                                transition: 'box-shadow 0.05s ease-out'
                                            }}
                                            title={isRecording ? tMain('stopRecording') : isProcessingAudio ? tMain('processing') : tMain('voiceInput')}
                                        >
                                            {isProcessingAudio ? (
                                                <Loader2 size={18} className="animate-spin" />
                                            ) : isRecording ? (
                                                <MicOff size={18} />
                                            ) : (
                                                <Mic size={18} />
                                            )}
                                        </button>
                                        {/* Live-Call: the voice-agent first layer (bar morphs red, agent window top-left).
                                            Without a voice profile the first click OFFERS the guided enrollment
                                            (voice-gated delegation needs a profile); a skip is remembered. */}
                                        <button
                                            type="button"
                                            onClick={() => {
                                                if (!speakerProfile && !localStorage.getItem('vaf_voice_enroll_skipped')) {
                                                    setShowEnrollOffer(true);
                                                } else {
                                                    useVoiceCallStore.getState().start();
                                                }
                                            }}
                                            disabled={isRecording || isProcessingAudio}
                                            className="shrink-0 mb-1.5 mr-2 h-10 w-10 flex items-center justify-center rounded-xl transition-colors text-gray-500 hover:text-gray-900 hover:bg-gray-100 disabled:text-gray-300"
                                            title="Live-Call"
                                        >
                                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                                <path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z" />
                                            </svg>
                                        </button>
                                        {/* Mobile-only Send button (40px square, primary) — desktop submits via Enter */}
                                        <button
                                            type="submit"
                                            disabled={(isGenerating && !isSubAgentRunning) || isIndexing || !input.trim()}
                                            aria-label="Send"
                                            className="shrink-0 mb-1.5 mr-2 h-10 w-10 hidden max-md:flex items-center justify-center rounded-xl bg-gray-900 text-white hover:bg-black disabled:bg-gray-200 disabled:text-gray-400 transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                                        >
                                            <Send size={18} />
                                        </button>
                                    </form>
                                </div>
                            </div>
                        </div>
                    </div>
                    {/* Mobile: compact preview bar for the active agent window — tap to open it as a full-screen sheet */}
                    {anyDockPanelOpen && !subAgentSheetOpen && (
                        <button
                            type="button"
                            onClick={() => setSubAgentSheetOpen(true)}
                            className="lg:hidden fixed left-1/2 -translate-x-1/2 bottom-[92px] z-[55] flex items-center gap-2 max-w-[88%] pl-2 pr-2.5 py-1.5 rounded-full bg-gray-900 text-white shadow-lg active:bg-black dark:bg-[#e6e6e6] dark:text-[#181818] dark:shadow-none"
                        >
                            <span className="w-6 h-6 rounded-full bg-white/10 flex items-center justify-center shrink-0"><Bot size={14} /></span>
                            <span className="text-xs font-medium truncate">{subAgentState.agentName || 'Agent window'}{subAgentState.status ? ` · ${subAgentState.status}` : ''}</span>
                            <span className="text-[11px] font-semibold shrink-0 px-2 py-0.5 rounded-full bg-white/15">Open</span>
                        </button>
                    )}
                    {/* Right Panel: CodeViewer, DocumentViewer, DocumentEditor, or SubAgentWindow (dock mode) */}
                    {showSubAgentPanel && (
                        <div
                            className={cn(
                                "hidden lg:flex h-full items-stretch overflow-hidden transition-all duration-300 ease-out",
                                (subAgentState.isOpen || documentEditorState.isOpen || documentViewerState.isOpen || codeViewerState.isOpen || htmlViewerState.isOpen || imageViewerState.isOpen)
                                    // Wide window ONLY while the SubAgentWindow itself renders a
                                    // custom view (coder/research data or a browser frame). The
                                    // viewers/editors take render priority over the SubAgentWindow,
                                    // so when one of them is open (e.g. the Document Editor after a
                                    // research run) the panel must drop back to the classic width.
                                    ? ((subAgentState.coder || subAgentState.research || subAgentState.document || subAgentState.librarian || subAgentState.browserFrame || subAgentState.browser)
                                        && !documentEditorState.isOpen && !documentViewerState.isOpen
                                        && !codeViewerState.isOpen && !htmlViewerState.isOpen && !imageViewerState.isOpen
                                        ? "w-[72%] min-w-[760px] max-w-[1400px] opacity-100"
                                        : "w-[58%] min-w-[704px] max-w-[1000px] opacity-100")
                                    : "w-0 min-w-0 max-w-0 opacity-0 pointer-events-none",
                                stopHovered && isSubAgentRunning
                                    ? "outline outline-2 outline-red-400/60 shadow-[0_0_16px_6px_rgba(239,68,68,0.12)]"
                                    : "",
                                // Mobile: full-screen sheet when opened via the mini-bar (overrides the hidden/zero-width dock)
                                subAgentSheetOpen
                                    ? "max-lg:!flex max-lg:fixed max-lg:inset-0 max-lg:z-[70] max-lg:!w-full max-lg:!max-w-none max-lg:!min-w-0 max-lg:!opacity-100 max-lg:!pointer-events-auto max-lg:h-[100dvh] max-lg:bg-white"
                                    : ""
                            )}
                            aria-hidden={!subAgentState.isOpen && !documentEditorState.isOpen && !documentViewerState.isOpen && !codeViewerState.isOpen && !htmlViewerState.isOpen && !imageViewerState.isOpen}
                        >
                            {/* Mobile sheet: back-to-chat button (the desktop dock has no such button) */}
                            {subAgentSheetOpen && (
                                <button
                                    type="button"
                                    onClick={() => setSubAgentSheetOpen(false)}
                                    aria-label="Back to chat"
                                    className="lg:hidden absolute top-2 left-2 z-[80] h-9 w-9 flex items-center justify-center rounded-full bg-white/90 shadow-md border border-gray-200 text-gray-700 active:bg-gray-100"
                                >
                                    <ChevronLeft size={20} />
                                </button>
                            )}
                            {imageViewerState.isOpen ? (
                                <ImageViewer
                                    isOpen={imageViewerState.isOpen}
                                    filePath={imageViewerState.filePath}
                                    title={imageViewerState.title}
                                    initialContent={imageViewerState.src}
                                    description={imageViewerState.description}
                                    descriptionLoading={imageViewerState.descLoading}
                                    onMark={setImageMark}
                                    clearMarkToken={markClearToken}
                                    onClose={() => { setImageViewerState(prev => ({ ...prev, isOpen: false })); setImageMark(null); }}
                                />
                            ) : htmlViewerState.isOpen ? (
                                <HtmlViewer
                                    isOpen={htmlViewerState.isOpen}
                                    filePath={htmlViewerState.filePath}
                                    title={htmlViewerState.title}
                                    initialContent={htmlViewerState.initialContent}
                                    onClose={() => setHtmlViewerState(prev => ({ ...prev, isOpen: false }))}
                                />
                            ) : codeViewerState.isOpen ? (
                                <CodeViewer
                                    isOpen={codeViewerState.isOpen}
                                    filePath={codeViewerState.filePath}
                                    title={codeViewerState.title}
                                    initialContent={codeViewerState.initialContent}
                                    liveRefresh={codeViewerState.liveRefresh ?? isGenerating}
                                    onClose={() => setCodeViewerState(prev => ({ ...prev, isOpen: false }))}
                                    onContentLoad={(content) => setCodeViewerState(prev => ({ ...prev, loadedContent: content }))}
                                />
                            ) : documentViewerState.isOpen ? (
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
                                    indexStatus={activeAttachmentIndexStatus}
                                    canClose={!isIndexing}
                                />
                            ) : documentEditorState.isOpen ? (
                                <DocumentEditor
                                    key={`${currentSessionId ?? 'default'}-ed-${documentEditorState.filePath || 'nofile'}`}
                                    isOpen={documentEditorState.isOpen}
                                    onClose={() => setDocumentEditorState(prev => ({ ...prev, isOpen: false }))}
                                    filePath={documentEditorState.filePath}
                                    title={documentEditorState.title}
                                    initialContent={documentEditorState.content ?? ''}
                                    initialDocxModel={documentEditorState.docxModel ?? null}
                                    onContentChange={(content) => setDocumentEditorState(prev => ({ ...prev, content }))}
                                    onDocxModelChange={(docxModel) => setDocumentEditorState(prev => ({ ...prev, docxModel }))}
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
                                    agentKind={subAgentState.agentKind}
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
                                    browserFrame={subAgentState.browserFrame}
                                    browserUrl={subAgentState.browserUrl}
                                    coder={subAgentState.coder}
                                    research={subAgentState.research}
                                    document={subAgentState.document}
                                    librarian={subAgentState.librarian}
                                    browser={subAgentState.browser}
                                />
                            )}
                        </div>
                    )}
                </div>
            </div>
            {/* Active Tools Panel Moved Inline */}
            <VAFWorkflowRuntime />

            {/* Browser live view, tiled to the LEFT of the Workflow Runtime window (which is
                ~500px wide on lg) so the two visual windows sit side by side instead of overlapping. */}
            {workflowPanelOpen && !browserTileClosed && (
                <BrowserLiveTile
                    frame={subAgentState.browserFrame}
                    url={subAgentState.browserUrl}
                    agentName={subAgentState.agentName || 'Browser Agent'}
                    rightOffset={500}
                    onClose={() => setBrowserTileClosed(true)}
                />
            )}

            {/* Right-click selected text → copy + brief confirmation toast */}
            <CopyOnRightClick />

            {/* Stop-button ripple portal — rendered at document.body level to bypass all overflow:hidden parents */}
            {stopPulsing && stopBtnPos && typeof document !== 'undefined' && createPortal(
                <>
                    {[0, 1, 2].map(i => (
                        <span key={i} style={{
                            position: 'fixed',
                            left: stopBtnPos.x,
                            top: stopBtnPos.y,
                            width: 36, height: 36,
                            transform: 'translate(-50%, -50%)',
                            borderRadius: '50%',
                            backgroundColor: 'rgba(239,68,68,0.45)',
                            pointerEvents: 'none',
                            zIndex: 99999,
                            animation: 'stopRipple 1.1s ease-out infinite',
                            animationDelay: `${i * 0.37}s`,
                        }} />
                    ))}
                </>,
                document.body
            )}

            {/* Context Window Modal - Clean & Professional */}
            {isWorkspaceModalOpen && (workspaceView === 'index' || workspaceInfo?.path) && (
                <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-[100] flex items-center justify-center p-4 max-md:p-0 animate-in fade-in duration-200">
                    <div className="relative bg-white w-full max-w-[1320px] min-h-[720px] max-h-[90vh] rounded-2xl shadow-2xl overflow-hidden flex flex-col animate-in zoom-in-95 duration-200 border border-gray-200 max-md:max-w-none max-md:min-h-0 max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0">
                        {/* Header */}
                        <div className="shrink-0 px-8 py-5 border-b border-gray-100 bg-gray-50/80">
                            <div className="flex justify-between items-center">
                                <h3 className="text-2xl font-bold text-gray-900 flex items-center gap-3 min-w-0">
                                    <Folder className="text-gray-800 shrink-0" size={22} />
                                    {workspaceView === 'index' ? (
                                        <span className="truncate">My Workspaces</span>
                                    ) : (
                                        <>
                                            <span className="truncate">{workspaceInfo?.displayName || workspaceInfo?.name}</span>
                                            <span className="text-[10px] font-bold text-violet-700 bg-violet-100 px-2 py-0.5 rounded shrink-0">
                                                Chat Workspace
                                            </span>
                                        </>
                                    )}
                                </h3>
                                <button
                                    onClick={() => setIsWorkspaceModalOpen(false)}
                                    className="p-2 hover:bg-gray-200 rounded-full transition-colors text-gray-400 hover:text-gray-700"
                                >
                                    <span className="sr-only">Close</span>
                                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                                </button>
                            </div>
                        </div>

                        {/* Explorer toolbar: Back / Up / address bar / refresh / upload (folder view only) */}
                        {workspaceView === 'folder' && workspaceInfo?.path && (
                        <div className="shrink-0 flex items-center gap-2 border-b border-gray-100 bg-white px-6 py-2.5">
                            <button
                                onClick={() => {
                                    if (workspaceNavHist.length === 0 && !workspaceInfo?.subpath) { setWorkspaceView('index'); refreshAllWorkspaces(); }
                                    else { workspaceGoBack(); }
                                }}
                                className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800"
                                title={workspaceNavHist.length === 0 && !workspaceInfo?.subpath ? 'All workspaces' : 'Back'}
                            >
                                <ArrowLeft size={16} />
                            </button>
                            {/* Address bar */}
                            <div className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-1.5 font-mono text-xs text-gray-500">
                                <Folder size={12} className="shrink-0 text-amber-400" />
                                <button
                                    onClick={() => navigateWorkspace('')}
                                    className={cn('shrink-0 rounded px-1 py-0.5 hover:bg-violet-100 hover:text-violet-700', !workspaceInfo.subpath && 'font-semibold text-gray-700')}
                                >
                                    {workspaceInfo.name}
                                </button>
                                {workspaceInfo.subpath.split('/').filter(Boolean).map((seg, i, segs) => (
                                    <Fragment key={i}>
                                        <ChevronRight size={11} className="shrink-0 text-gray-300" />
                                        <button
                                            onClick={() => navigateWorkspace(segs.slice(0, i + 1).join('/'))}
                                            className={cn('truncate rounded px-1 py-0.5 hover:bg-violet-100 hover:text-violet-700', i === segs.length - 1 && 'font-semibold text-gray-700')}
                                        >
                                            {seg}
                                        </button>
                                    </Fragment>
                                ))}
                            </div>
                            <span className="shrink-0 text-[11px] text-gray-300">
                                {workspaceInfo.dirs.length + workspaceInfo.files.length} {workspaceInfo.dirs.length + workspaceInfo.files.length === 1 ? 'item' : 'items'}
                            </span>
                            <button
                                onClick={() => refreshWorkspace()}
                                className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800"
                                title="Refresh"
                            >
                                <RefreshCw size={15} />
                            </button>
                            <button
                                onClick={() => workspaceFileInputRef.current?.click()}
                                disabled={workspaceUploading}
                                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-gray-900 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-gray-700 disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                            >
                                {workspaceUploading ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                                {workspaceUploading ? 'Uploading…' : 'Upload'}
                            </button>
                        </div>
                        )}

                        {/* Central index: ALL of this user's workspaces (live + orphaned), incl. those from deleted chats */}
                        {workspaceView === 'index' && (
                            <div className="relative min-h-0 flex-1 overflow-y-auto px-4 py-3">
                                <div className="flex items-center justify-between px-3 pb-1">
                                    <span className="text-[11px] text-gray-300">{allWorkspaces.length} {allWorkspaces.length === 1 ? 'workspace' : 'workspaces'}</span>
                                    <button onClick={() => refreshAllWorkspaces()} className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800" title="Refresh"><RefreshCw size={15} /></button>
                                </div>
                                <div className="grid content-start gap-1 p-2 [grid-template-columns:repeat(auto-fill,minmax(150px,1fr))]">
                                    {allWorkspaces.map(w => (
                                        <div
                                            key={w.sessionId}
                                            role="button"
                                            tabIndex={0}
                                            onClick={() => openWorkspace(w.sessionId)}
                                            onKeyDown={(e) => { if (e.key === 'Enter') openWorkspace(w.sessionId); }}
                                            className={cn(
                                                "group relative flex cursor-pointer flex-col items-center gap-1 rounded-xl border px-2 py-3 text-center transition-colors",
                                                w.sessionId === currentSessionId
                                                    ? "border-emerald-300 bg-emerald-50/50"
                                                    : "border-transparent hover:border-violet-100 hover:bg-violet-50/60"
                                            )}
                                            title={w.sessionId === currentSessionId ? `${w.displayName} — current chat` : w.displayName}
                                        >
                                            {w.sessionId === currentSessionId && (
                                                <span
                                                    className="absolute left-1.5 top-1.5 z-10 h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-white"
                                                    title="Current chat"
                                                />
                                            )}
                                            <div className="absolute right-1.5 top-1.5 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                                                <button onClick={(e) => { e.stopPropagation(); renameWorkspace(w.sessionId, w.displayName); }} className="rounded-full bg-white p-1.5 text-gray-400 shadow-sm hover:bg-violet-100 hover:text-violet-600" title="Rename"><Edit2 size={13} /></button>
                                                <button onClick={(e) => { e.stopPropagation(); setWorkspaceDeleteTarget({ name: w.displayName, isDir: true, items: w.fileCount + w.folderCount, kind: 'workspace', sessionId: w.sessionId }); }} className="rounded-full bg-white p-1.5 text-gray-400 shadow-sm hover:bg-red-100 hover:text-red-600" title="Delete workspace"><Trash2 size={13} /></button>
                                            </div>
                                            <Folder size={44} strokeWidth={1} className={w.orphan ? 'text-gray-300' : 'text-amber-400'} fill={w.orphan ? '#f3f4f6' : '#fde68a'} />
                                            <span className="line-clamp-2 w-full break-words text-xs font-medium leading-tight text-gray-800">{w.displayName}</span>
                                            {w.orphan && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[9px] font-bold text-amber-700">orphan</span>}
                                            <span className="text-[10px] text-gray-300">{w.fileCount} {w.fileCount === 1 ? 'file' : 'files'}</span>
                                        </div>
                                    ))}
                                    {allWorkspaces.length === 0 && (
                                        <div className="col-span-full flex h-48 items-center justify-center text-sm text-gray-300">No workspaces yet.</div>
                                    )}
                                </div>
                            </div>
                        )}

                        {/* File browser (folders + files, scoped to the chat folder) */}
                        {workspaceView === 'folder' && workspaceInfo?.path && (
                        <div
                            className={cn(
                                "relative min-h-0 flex-1 overflow-y-auto px-4 py-3 transition-colors",
                                workspaceDragOver && "bg-violet-50/60"
                            )}
                            onDragOver={(e) => { e.preventDefault(); setWorkspaceDragOver(true); }}
                            onDragLeave={() => setWorkspaceDragOver(false)}
                            onDrop={(e) => {
                                e.preventDefault();
                                setWorkspaceDragOver(false);
                                const dropped = Array.from(e.dataTransfer.files || []);
                                if (dropped.length) uploadWorkspaceFiles(dropped);
                            }}
                        >
                            {workspaceDragOver && (
                                <div className="pointer-events-none absolute inset-3 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-violet-300 bg-violet-50/80 text-sm font-semibold text-violet-600">
                                    Drop files to upload into {workspaceInfo.subpath ? workspaceInfo.subpath.split('/').pop() : workspaceInfo.name}
                                </div>
                            )}
                            {/* Explorer-style icon grid */}
                            <div className="grid content-start gap-1 p-2 [grid-template-columns:repeat(auto-fill,minmax(130px,1fr))]">
                                {workspaceInfo.dirs.map(d => (
                                    <div
                                        key={`dir-${d.name}`}
                                        role="button"
                                        tabIndex={0}
                                        onClick={() => navigateWorkspace(workspaceInfo.subpath ? `${workspaceInfo.subpath}/${d.name}` : d.name)}
                                        onKeyDown={(e) => { if (e.key === 'Enter') navigateWorkspace(workspaceInfo.subpath ? `${workspaceInfo.subpath}/${d.name}` : d.name); }}
                                        className="group relative flex cursor-pointer flex-col items-center gap-1 rounded-xl border border-transparent px-2 py-3 text-center transition-colors hover:border-violet-100 hover:bg-violet-50/60"
                                        title={`${d.name} (${d.items} ${d.items === 1 ? 'item' : 'items'})`}
                                    >
                                        <button
                                            onClick={(e) => { e.stopPropagation(); setWorkspaceDeleteTarget({ name: d.name, isDir: true, items: d.items }); }}
                                            className="absolute right-1.5 top-1.5 rounded-full bg-white p-1.5 text-gray-400 opacity-0 shadow-sm transition-opacity hover:bg-red-100 hover:text-red-600 group-hover:opacity-100"
                                            title={`Delete folder ${d.name}`}
                                        >
                                            <Trash2 size={13} />
                                        </button>
                                        <Folder size={44} strokeWidth={1} className="text-amber-400" fill="#fde68a" />
                                        <span className="line-clamp-2 w-full break-all text-xs font-medium leading-tight text-gray-800">{d.name}</span>
                                        <span className="text-[10px] text-gray-300">{d.items} {d.items === 1 ? 'item' : 'items'}</span>
                                    </div>
                                ))}
                                {workspaceInfo.files.map(f => {
                                    const fileUrl = `${getApiBase()}/api/file?path=${encodeURIComponent(`${workspaceInfo.path}${workspaceInfo.subpath ? `/${workspaceInfo.subpath}` : ''}/${f.name}`)}`;
                                    return (
                                        <div
                                            key={`file-${f.name}`}
                                            role="button"
                                            tabIndex={0}
                                            draggable
                                            onClick={() => openWorkspaceFile(f.name)}
                                            onKeyDown={(e) => { if (e.key === 'Enter') openWorkspaceFile(f.name); }}
                                            onDragStart={(e) => {
                                                // Drag out of the browser (Chromium: DownloadURL)
                                                const abs = fileUrl.startsWith('http') ? fileUrl : `${window.location.origin}${fileUrl}`;
                                                e.dataTransfer.setData('DownloadURL', `application/octet-stream:${f.name}:${abs}`);
                                                e.dataTransfer.setData('text/uri-list', abs);
                                            }}
                                            className="group relative flex cursor-pointer flex-col items-center gap-1 rounded-xl border border-transparent px-2 py-3 text-center transition-colors hover:border-violet-100 hover:bg-violet-50/60"
                                            title={`${f.name} — ${f.modified}`}
                                        >
                                            <div className="absolute right-1.5 top-1.5 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                                                <a
                                                    href={fileUrl}
                                                    download={f.name}
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        // Desktop window: use the native Save-As bridge
                                                        // (QtWebEngine's own download path is brittle).
                                                        // Browser: let the <a download> proceed.
                                                        const api = (window as unknown as { pywebview?: { api?: { save_file_as?: (p: string) => Promise<unknown> } } }).pywebview?.api;
                                                        if (api?.save_file_as) {
                                                            e.preventDefault();
                                                            api.save_file_as(workspaceFileAbsPath(f.name));
                                                        }
                                                    }}
                                                    className="rounded-full bg-white p-1.5 text-gray-400 shadow-sm hover:bg-violet-100 hover:text-violet-700"
                                                    title={`Download ${f.name}`}
                                                >
                                                    <Download size={13} />
                                                </a>
                                                <button
                                                    onClick={(e) => { e.stopPropagation(); setWorkspaceDeleteTarget({ name: f.name, isDir: false }); }}
                                                    className="rounded-full bg-white p-1.5 text-gray-400 shadow-sm hover:bg-red-100 hover:text-red-600"
                                                    title={`Delete ${f.name}`}
                                                >
                                                    <Trash2 size={13} />
                                                </button>
                                            </div>
                                            <FileText size={44} strokeWidth={1} className="text-gray-400" fill="#f9fafb" />
                                            <span className="line-clamp-2 w-full break-all font-mono text-xs leading-tight text-gray-800">{f.name}</span>
                                            <span className="text-[10px] text-gray-300">{f.size >= 1024 ? `${(f.size / 1024).toFixed(1)} KB` : `${f.size} B`}</span>
                                        </div>
                                    );
                                })}
                                {workspaceInfo.dirs.length === 0 && workspaceInfo.files.length === 0 && (
                                    <div className="col-span-full flex h-48 items-center justify-center text-sm text-gray-300">
                                        This folder is empty — drop files here to upload.
                                    </div>
                                )}
                            </div>
                        </div>
                        )}

                        {/* Delete confirmation dialog */}
                        {workspaceDeleteTarget && (
                            <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/30 backdrop-blur-[2px] animate-in fade-in duration-150">
                                <div className="w-full max-w-sm rounded-2xl border border-gray-200 bg-white p-5 shadow-2xl animate-in zoom-in-95 duration-150">
                                    <div className="flex items-start gap-3">
                                        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-red-100 text-red-600">
                                            <Trash2 size={16} />
                                        </div>
                                        <div className="min-w-0">
                                            <div className="text-sm font-bold text-gray-900">
                                                Delete {workspaceDeleteTarget.kind === 'workspace' ? 'workspace' : workspaceDeleteTarget.isDir ? 'folder' : 'file'}?
                                            </div>
                                            <p className="mt-1 text-xs leading-relaxed text-gray-500">
                                                <span className="font-mono font-semibold text-gray-700">{workspaceDeleteTarget.name}</span>
                                                {workspaceDeleteTarget.isDir
                                                    ? ` and everything inside it (${workspaceDeleteTarget.items ?? 0} ${(workspaceDeleteTarget.items ?? 0) === 1 ? 'item' : 'items'}) will be permanently deleted.`
                                                    : ' will be permanently deleted.'}
                                                {' '}This cannot be undone.
                                            </p>
                                        </div>
                                    </div>
                                    <div className="mt-4 flex justify-end gap-2">
                                        <button
                                            onClick={() => setWorkspaceDeleteTarget(null)}
                                            disabled={workspaceDeleting}
                                            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-semibold text-gray-600 transition-colors hover:bg-gray-50 disabled:opacity-50"
                                        >
                                            Cancel
                                        </button>
                                        <button
                                            onClick={deleteWorkspaceEntry}
                                            disabled={workspaceDeleting}
                                            className="inline-flex items-center gap-1.5 rounded-lg bg-red-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-red-700 disabled:opacity-50"
                                        >
                                            {workspaceDeleting ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
                                            {workspaceDeleting ? 'Deleting…' : 'Delete'}
                                        </button>
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* Footer hint */}
                        <div className="shrink-0 border-t border-gray-100 bg-gray-50/60 px-8 py-2.5">
                            <p className="text-xs text-gray-400">
                                Drag files in to upload, drag files out to save them — the agent sees everything here instantly.
                            </p>
                            <input
                                ref={workspaceFileInputRef}
                                type="file"
                                multiple
                                className="hidden"
                                onChange={(e) => {
                                    const files = Array.from(e.target.files || []);
                                    if (files.length) uploadWorkspaceFiles(files);
                                    e.target.value = '';
                                }}
                            />
                        </div>
                    </div>
                </div>
            )}

            {isContextModalOpen && contextStats && (
                <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-[100] flex items-center justify-center p-4 max-md:p-0 animate-in fade-in duration-200">
                    <div className="bg-white w-full max-w-[1320px] min-h-[720px] max-h-[90vh] rounded-2xl shadow-2xl overflow-hidden flex flex-col animate-in zoom-in-95 duration-200 border border-gray-200 max-md:max-w-none max-md:min-h-0 max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0">
                        {/* Header */}
                        <div className="shrink-0 px-8 py-6 border-b border-gray-100 bg-gray-50/80 max-md:px-4 max-md:py-4">
                            <div className="flex justify-between items-start">
                                <div>
                                    <div className="flex items-center gap-4 max-md:flex-wrap max-md:gap-2">
                                        <h3 className="text-2xl font-bold text-gray-900 flex items-center gap-2 max-md:text-xl">
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
                        <div className="flex px-8 py-6 flex-1 min-h-0 gap-6 max-md:flex-col max-md:px-4 max-md:py-4 max-md:gap-4 max-md:overflow-y-auto">
                            {/* Legend - Left side with token count and percentage */}
                            <div className="shrink-0 w-52 flex flex-col justify-start gap-3 text-sm max-md:w-full">
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

                            {/* Diagram - Center (uses contextBreakdown) */}
                            <div className="flex-1 flex flex-col min-h-0 max-md:flex-none max-md:min-h-[300px]">
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

                            {/* Brain — same flat style as left legend, no extra box */}
                            <div className="shrink-0 w-52 flex flex-col gap-3 text-sm border-l border-gray-100 pl-4 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden max-md:w-full max-md:border-l-0 max-md:border-t max-md:pl-0 max-md:pt-3 max-md:overflow-visible">
                                <div className="text-[10px] font-bold text-gray-400 uppercase tracking-widest border-b border-gray-100 pb-1">🧠 Agent Brain</div>

                                {/* Goal */}
                                {brainData?.intent && (
                                    <div className="flex flex-col gap-1">
                                        <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Goal</div>
                                        <div className="text-xs text-gray-700 leading-relaxed min-h-[1rem] max-h-24 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                                            {brainData.intent}
                                        </div>
                                    </div>
                                )}

                                {/* Plan */}
                                {brainData && brainData.plan.length > 0 && (
                                    <div className="flex flex-col gap-1">
                                        <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Plan</div>
                                        <div className="flex flex-col gap-0.5 min-h-[1rem] max-h-40 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                                            {brainData.plan.map((step, i) => (
                                                <div key={i} className="flex items-start gap-1.5 text-xs text-gray-700">
                                                    <span className="shrink-0 font-mono text-[10px] text-gray-400 mt-0.5">{i + 1}.</span>
                                                    <span className="leading-relaxed">{step}</span>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {/* Notes */}
                                {brainData && brainData.notes.length > 0 && (
                                    <div className="flex flex-col gap-1">
                                        <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Notes</div>
                                        <div className="flex flex-col gap-0.5 min-h-[1rem] max-h-32 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                                            {brainData.notes.map((n, i) => (
                                                <div key={i} className="flex items-start gap-1.5 text-xs text-gray-600">
                                                    <span className="shrink-0 text-gray-300 mt-0.5">·</span>
                                                    <span className="leading-relaxed">{n}</span>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {/* Team */}
                                {brainData && brainData.agents.length > 0 && (
                                    <div className="flex flex-col gap-1">
                                        <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Team</div>
                                        <div className="flex flex-col gap-1.5 min-h-[1rem] max-h-32 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                                            {brainData.agents.map((a, i) => {
                                                const dot = a.status === 'running' ? 'bg-emerald-500 animate-pulse'
                                                    : a.status === 'failed' ? 'bg-red-500'
                                                    : a.status === 'needs_clarification' ? 'bg-amber-400 animate-pulse'
                                                    : 'bg-gray-400';
                                                return (
                                                    <div key={i} className="flex items-start gap-1.5">
                                                        <span className={cn('shrink-0 mt-1 h-1.5 w-1.5 rounded-full', dot)} />
                                                        <div className="min-w-0">
                                                            <div className="text-[11px] font-semibold text-gray-800 truncate">{a.agent_type.replace(/_/g, ' ')}</div>
                                                            {a.task && <div className="text-[10px] text-gray-500 truncate">{a.task}</div>}
                                                            {a.question && <div className="text-[10px] text-amber-600 truncate">❓ {a.question}</div>}
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    </div>
                                )}

                                {!brainData && <div className="text-xs text-gray-400 italic">Loading…</div>}
                                {brainData && !brainData.intent && brainData.plan.length === 0 && brainData.notes.length === 0 && brainData.agents.length === 0 && (
                                    <div className="text-xs text-gray-400 italic">Nothing active</div>
                                )}
                            </div>

                            {/* Tasks — same flat style, own column */}
                            <div className="shrink-0 w-44 flex flex-col gap-3 text-sm border-l border-gray-100 pl-4 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                                <div className="text-[10px] font-bold text-gray-400 uppercase tracking-widest border-b border-gray-100 pb-1">✅ Tasks</div>
                                {brainData && brainData.tasks.length > 0 ? (
                                    <div className="flex flex-col gap-1 min-h-[1rem] max-h-full overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                                        {brainData.tasks.map((t, i) => (
                                            <div key={i} className="flex items-start gap-1.5 text-xs">
                                                <span className={cn('shrink-0 mt-0.5 leading-none', t.status === 'done' ? 'text-emerald-500' : 'text-amber-400')}>
                                                    {t.status === 'done' ? '✓' : '○'}
                                                </span>
                                                <span className={cn('leading-relaxed', t.status === 'done' ? 'text-gray-400 line-through' : 'text-gray-700')}>
                                                    {t.text}
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                ) : (
                                    brainData && <div className="text-xs text-gray-400 italic">No tasks</div>
                                )}
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

            <VoiceCallLayer
                ws={ws}
                sessionId={currentSessionId}
                mainBusy={isGenerating || isWorkflowRunning || isSubAgentRunning || loading}
                onLocalMessage={(role, content, kind) => {
                    setMessages(prev => [...prev, { role, content, timestamp: Date.now(), kind }]);
                }}
            />
            <VoiceEnrollmentCall
                open={voiceCallOpen}
                ws={ws}
                displayName={currentUser?.username || 'Ich'}
                onClose={(saved) => {
                    setVoiceCallOpen(false);
                    if (saved) ws?.send(JSON.stringify({ type: 'speaker_profile_get' }));
                    if (enrollThenCallRef.current) {
                        // Enrollment was offered from the call button: on
                        // success go straight into the call the user wanted;
                        // on abort just return to the chat (no Settings).
                        enrollThenCallRef.current = false;
                        if (saved) useVoiceCallStore.getState().start();
                    } else {
                        setIsSettingsOpen(true);
                    }
                }}
            />
            {showEnrollOffer && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
                    onClick={() => setShowEnrollOffer(false)}>
                    <div className="w-[420px] max-w-[calc(100vw-2rem)] rounded-2xl border border-black/10 dark:border-white/10 bg-white dark:bg-[#1f1f1f] shadow-2xl p-5"
                        onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-3 mb-2">
                            <AgentAvatar mode="permission" />
                            <p className="text-sm font-semibold text-gray-800 dark:text-gray-200">{tMain('voiceEnrollOfferTitle')}</p>
                        </div>
                        <p className="text-sm text-gray-600 dark:text-gray-400">{tMain('voiceEnrollOfferText')}</p>
                        <div className="flex items-center justify-end gap-2 mt-4">
                            <button type="button"
                                onClick={() => {
                                    localStorage.setItem('vaf_voice_enroll_skipped', '1');
                                    setShowEnrollOffer(false);
                                    useVoiceCallStore.getState().start();
                                }}
                                className="px-3 py-2 text-sm font-medium rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10">
                                {tMain('voiceEnrollOfferSkip')}
                            </button>
                            <button type="button"
                                onClick={() => {
                                    setShowEnrollOffer(false);
                                    enrollThenCallRef.current = true;
                                    setVoiceCallOpen(true);
                                }}
                                className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818]">
                                {tMain('voiceEnrollOfferSetup')}
                            </button>
                        </div>
                    </div>
                </div>
            )}
            <SettingsModal
                isOpen={isSettingsOpen}
                onClose={handleSettingsClose}
                config={config}
                onSave={handleSaveConfig}
                speakerProfile={speakerProfile}
                onStartVoiceEnrollment={() => {
                    setIsSettingsOpen(false);
                    setVoiceCallOpen(true);
                }}
                onDeleteSpeakerProfile={() => ws?.send(JSON.stringify({ type: 'speaker_profile_delete' }))}
                onRefreshSpeakerProfile={() => ws?.send(JSON.stringify({ type: 'speaker_profile_get' }))}
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
                onCreateCustomTool={(name, code, sharedWith) => {
                    setIsCustomToolSaving(true);
                    setCustomToolBackendError(null);
                    ws?.send(JSON.stringify({ type: 'create_custom_tool', name, code, shared_with: sharedWith }));
                }}
                onUpdateCustomTool={(name, code, sharedWith) => {
                    setIsCustomToolSaving(true);
                    setCustomToolBackendError(null);
                    ws?.send(JSON.stringify({ type: 'update_custom_tool', name, code, shared_with: sharedWith }));
                }}
                onDeleteCustomTool={(name) => {
                    setIsCustomToolSaving(true);
                    setCustomToolBackendError(null);
                    ws?.send(JSON.stringify({ type: 'delete_custom_tool', name }));
                }}
                customToolUsers={customToolUsers}
                onGetCustomToolUsers={() => ws?.send(JSON.stringify({ type: 'get_custom_tool_users' }))}
                isCustomToolSaving={isCustomToolSaving}
                customToolBackendError={customToolBackendError}
                workflows={workflows}
                onCreateWorkflow={(data) => {
                    setIsWorkflowSaving(true);
                    setWorkflowBackendError(null);
                    ws?.send(JSON.stringify({ type: 'create_workflow', ...data }));
                }}
                onUpdateWorkflow={(data) => {
                    setIsWorkflowSaving(true);
                    setWorkflowBackendError(null);
                    ws?.send(JSON.stringify({ type: 'update_workflow', ...data }));
                }}
                onDeleteWorkflow={(id) => {
                    setIsWorkflowSaving(true);
                    setWorkflowBackendError(null);
                    ws?.send(JSON.stringify({ type: 'delete_workflow', workflow_id: id }));
                }}
                isWorkflowSaving={isWorkflowSaving}
                workflowBackendError={workflowBackendError}
                skills={skills}
                onCreateSkill={(data) => {
                    setIsSkillSaving(true);
                    setSkillBackendError(null);
                    ws?.send(JSON.stringify({ type: 'create_skill', ...data }));
                }}
                onUpdateSkill={(data) => {
                    setIsSkillSaving(true);
                    setSkillBackendError(null);
                    ws?.send(JSON.stringify({ type: 'update_skill', ...data }));
                }}
                onDeleteSkill={(id) => {
                    setIsSkillSaving(true);
                    setSkillBackendError(null);
                    ws?.send(JSON.stringify({ type: 'delete_skill', skill_id: id }));
                }}
                onUploadSkill={(filename, base64, override) => {
                    setIsSkillSaving(true);
                    setSkillBackendError(null);
                    ws?.send(JSON.stringify({ type: 'upload_skill', filename, data: base64, override }));
                }}
                isSkillSaving={isSkillSaving}
                skillBackendError={skillBackendError}
                skillSavedTick={skillSavedTick}
                mcpServers={mcpServers}
                onRefreshMcpServers={() => ws?.send(JSON.stringify({ type: 'get_mcp_servers' }))}
                onSaveMcpServer={(d) => { setIsMcpSaving(true); setMcpBackendError(null); ws?.send(JSON.stringify({ type: 'create_mcp_server', ...d })); }}
                onDeleteMcpServer={(name) => { setIsMcpSaving(true); setMcpBackendError(null); ws?.send(JSON.stringify({ type: 'delete_mcp_server', name })); }}
                isMcpSaving={isMcpSaving}
                mcpBackendError={mcpBackendError}
                onTestMcpServer={(cfg) => { setIsMcpTesting(true); setMcpTestResult(null); ws?.send(JSON.stringify({ type: 'test_mcp_server', ...cfg })); }}
                mcpTestResult={mcpTestResult}
                isMcpTesting={isMcpTesting}
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
                    if (typeof window !== 'undefined') {
                        window.location.replace(`${window.location.origin}/login`);
                    } else {
                        router.replace('/login');
                    }
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
                userTimeFormat={userTimeFormat}
            />
            <AnnouncementModal
                isOpen={announcement !== null}
                onClose={handleAnnouncementClose}
                variant={announcement === 'changelog' ? 'changelog' : 'intro'}
                versionDisplay={formatVersion(rawVersion).display}
                channel={formatVersion(rawVersion).channel}
                entry={announcement === 'changelog' ? latestEntry() : null}
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
            {/* Trust Gate Dialog — shown when agent needs confirmation for a risky tool */}
            {gateRequest && (
                <div className="fixed inset-0 z-[80] flex items-center justify-center p-4">
                    <div className="absolute inset-0 bg-black/60" />
                    <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md border border-gray-200 animate-in fade-in zoom-in-95 duration-200">
                        {/* Header */}
                        <div className="flex items-center gap-3 p-5 border-b border-gray-200 bg-amber-50 rounded-t-2xl">
                            <div className="w-9 h-9 rounded-xl bg-amber-500 flex items-center justify-center shrink-0">
                                <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                                </svg>
                            </div>
                            <div>
                                <p className="font-semibold text-gray-900 text-sm">Security Confirmation</p>
                                <p className="text-xs text-amber-700">Agent wants to run a risky tool</p>
                            </div>
                        </div>
                        {/* Body */}
                        <div className="p-5 space-y-3">
                            <div className="flex items-center gap-2">
                                <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">Tool</span>
                                <code className="text-sm font-mono bg-gray-100 px-2 py-0.5 rounded text-gray-900">{gateRequest.tool}</code>
                            </div>
                            {gateRequest.args_preview && (
                                <div>
                                    <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">Arguments</p>
                                    <pre className="text-xs font-mono bg-gray-50 border border-gray-200 rounded-lg p-3 whitespace-pre-wrap break-all max-h-32 overflow-y-auto text-gray-800">{gateRequest.args_preview}</pre>
                                </div>
                            )}
                            {gateRequest.reason && (
                                <p className="text-xs text-gray-500">{gateRequest.reason}</p>
                            )}
                        </div>
                        {/* Actions */}
                        <div className="flex items-center gap-2 p-5 pt-0">
                            <button
                                onClick={() => { ws?.send(JSON.stringify({ type: 'gate_response', decision: 'cancel' })); setGateRequest(null); }}
                                className="flex-1 px-4 py-2 rounded-lg border border-gray-200 text-gray-600 text-sm font-medium hover:bg-gray-100 transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={() => { ws?.send(JSON.stringify({ type: 'gate_response', decision: 'allow_once' })); setGateRequest(null); }}
                                className="flex-1 px-4 py-2 rounded-lg bg-amber-500 hover:bg-amber-600 text-white text-sm font-medium transition-colors"
                            >
                                Allow Once
                            </button>
                            <button
                                onClick={() => { ws?.send(JSON.stringify({ type: 'gate_response', decision: 'allow_always' })); setGateRequest(null); }}
                                className="flex-1 px-4 py-2 rounded-lg bg-gray-900 hover:bg-gray-800 text-white text-sm font-medium transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                            >
                                Always Allow
                            </button>
                        </div>
                    </div>
                </div>
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
