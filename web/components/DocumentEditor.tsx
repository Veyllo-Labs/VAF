'use client';

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import dynamic from 'next/dynamic';
import { X, Download, FileText, Save, Loader2, CheckCircle2, Circle, Plus, Trash2, ChevronDown, Bold, Italic, Underline, List, ListOrdered, AlignLeft, AlignCenter, AlignRight, Highlighter, Eraser, Printer } from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';
import { CHIP_BG_CLASSES, INSERTION_COLOR_CLASSES } from '@/components/DocumentViewer';
import NativeDocxEditor from '@/components/NativeDocxEditor';
import type { NativeDocxDocument } from '@/lib/docxNative';

const PdfWithHighlights = dynamic(() => import('@/components/PdfWithHighlights'), { ssr: false });

/** Document for attachments mode (extracted text/image). */
export type DocumentEditorAttachment = {
    id: string;
    name: string;
    mimeType?: string;
    content?: string;
    /** Base64 or data-URL for raw file; used to display original PDF. */
    data?: string;
};

/** Check if attachment is a PDF with raw data for original display. */
function isPdfWithData(doc: DocumentEditorAttachment): boolean {
    if (!doc.data) return false;
    const name = (doc.name || '').toLowerCase();
    const mime = (doc.mimeType || '').toLowerCase();
    return name.endsWith('.pdf') || mime === 'application/pdf';
}

/** Build data URL for PDF from base64 or existing data URL. */
function pdfDataUrl(doc: DocumentEditorAttachment): string {
    const d = doc.data || '';
    if (d.startsWith('data:')) return d;
    return `data:application/pdf;base64,${d}`;
}

/** One inserted selection for quote chips and persistent highlight. */
export type InsertedSelectionRange = {
    text: string;
    start: number;
    end: number;
    documentId: string;
    pageNumber?: number;
    itemIndices?: number[];
};

export type DocumentEditorProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    /** Kernel editor: path to file to edit. When set, attachments props are ignored. */
    filePath?: string;
    title?: string;
    initialContent?: string;
    /** Called whenever content changes (typing, load, toolbar). Parent can store per-session to restore on chat switch. */
    onContentChange?: (content: string) => void;
    initialDocxModel?: NativeDocxDocument | null;
    onDocxModelChange?: (document: NativeDocxDocument) => void;
    mode?: 'overlay' | 'dock';
    status?: string;
    presence?: 'online' | 'idle' | 'error';
    steps?: Array<{
        id: string;
        title: string;
        description?: string;
        status: 'pending' | 'running' | 'completed';
    }>;
    /** Attachments mode: list of documents (no right-side list). */
    documents?: DocumentEditorAttachment[];
    onAddFiles?: (files: File[]) => void;
    onRemoveDocument?: (id: string) => void;
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string; pageNumber?: number; itemIndices?: number[] }) => void;
    insertedSelectionsCount?: number;
    insertedSelections?: InsertedSelectionRange[];
};

const FILE_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.txt,.md,.json,.csv';

/** Only PDF cannot be edited in the editor. .docx, .xlsx, .pptx are loaded as HTML; .html/.htm are loaded and edited as HTML (rendered). */
const BINARY_DOCUMENT_EXTENSIONS = /\.pdf$/i;
function isBinaryDocumentPath(path: string): boolean {
    return Boolean(path && BINARY_DOCUMENT_EXTENSIONS.test(path));
}
/** Office formats: load via as-html, save via save-docx / save-xlsx / save-pptx. */
function isDocxPath(path: string): boolean {
    return Boolean(path && /\.docx?$/i.test(path));
}
function isXlsxPath(path: string): boolean {
    return Boolean(path && /\.xlsx$/i.test(path));
}
function isPptxPath(path: string): boolean {
    return Boolean(path && /\.pptx$/i.test(path));
}
function isOfficePath(path: string): boolean {
    return isDocxPath(path) || isXlsxPath(path) || isPptxPath(path);
}
function isMarkdownPath(path: string): boolean {
    return Boolean(path && /\.(md|mdx|markdown)$/i.test(path));
}

function buildHighlightSegments(
    contentLength: number,
    ranges: { start: number; end: number; colorIndex: number }[]
): { start: number; end: number; colorIndex: number }[] {
    type Event = { pos: number; type: 'start' | 'end'; colorIndex: number };
    const events: Event[] = [];
    for (const r of ranges) {
        const start = Math.max(0, Math.min(r.start, contentLength));
        const end = Math.max(0, Math.min(r.end, contentLength));
        if (start >= end) continue;
        events.push({ pos: start, type: 'start', colorIndex: r.colorIndex });
        events.push({ pos: end, type: 'end', colorIndex: r.colorIndex });
    }
    events.sort((a, b) => (a.pos !== b.pos ? a.pos - b.pos : (a.type === 'end' ? 0 : 1) - (b.type === 'end' ? 0 : 1)));
    const stack: number[] = [];
    const segments: { start: number; end: number; colorIndex: number }[] = [];
    let prevPos = 0;
    for (const e of events) {
        if (e.pos > prevPos && stack.length > 0)
            segments.push({ start: prevPos, end: e.pos, colorIndex: stack[stack.length - 1] });
        if (e.type === 'start') stack.push(e.colorIndex);
        else stack.pop();
        prevPos = e.pos;
    }
    return segments;
}

/** Same colors as CHIP_BG – applied via setProperty(..., 'important') so they win in the iframe. */
const INSERTION_HIGHLIGHT_COLORS: { bg: string; text: string }[] = [
    { bg: '#1f2937', text: '#ffffff' },
    { bg: '#f97316', text: '#ffffff' },
    { bg: '#ec4899', text: '#ffffff' },
    { bg: '#3b82f6', text: '#ffffff' },
    { bg: '#059669', text: '#ffffff' },
];

function getTextNodesInOrder(root: Node): { node: Text; start: number; end: number }[] {
    const result: { node: Text; start: number; end: number }[] = [];
    let offset = 0;
    const walk = (node: Node) => {
        if (node.nodeType === Node.TEXT_NODE) {
            const len = (node.textContent || '').length;
            if (len > 0) {
                result.push({ node: node as Text, start: offset, end: offset + len });
                offset += len;
            }
        } else {
            for (let i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
        }
    };
    walk(root);
    return result;
}

/** Injected in document head so report CSS inside the page body cannot override A4 sizing. */
const A4_EDITOR_STYLE = `
                html, body.vaf-a4, body.vaf-a4 * { box-sizing: border-box !important; }
                html, body.vaf-a4 { scrollbar-width: none !important; -ms-overflow-style: none !important; }
                html::-webkit-scrollbar, body.vaf-a4::-webkit-scrollbar { display: none !important; }
                html { width: 100% !important; margin: 0 !important; padding: 0 !important; }
                html body.vaf-a4 { width: 100% !important; max-width: none !important; margin: 0 !important; padding: 16px 0 !important;
                    min-height: 100%; background: #e5e7eb !important; overflow-y: auto !important; scroll-snap-type: y mandatory; }
                body.vaf-a4 .a4-page * { max-width: 100% !important; }
                html body.vaf-a4 .vaf-a4-center { width: 100% !important; display: flex !important; flex-direction: column !important; align-items: center !important; justify-content: flex-start !important; flex-shrink: 0; }
                html body.vaf-a4 .vaf-a4-center > .a4-pages,
                html body.vaf-a4 .vaf-a4-center .a4-pages { display: flex !important; flex-direction: column !important; align-items: center !important; gap: 24px !important; width: 210mm !important; flex-shrink: 0 !important; margin: 0 auto !important; }
                html body.vaf-a4 .vaf-a4-center > .a4-page,
                html body.vaf-a4 .vaf-a4-center .a4-pages > .a4-page,
                html body.vaf-a4 .a4-page.a4-page {
                    width: 210mm !important; height: 297mm !important; min-height: 297mm !important; max-height: 297mm !important;
                    flex-shrink: 0 !important; margin: 0 auto !important; background: white !important;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.12) !important; padding: 25mm !important; box-sizing: border-box !important;
                    overflow: hidden !important; scroll-snap-align: start !important; scroll-snap-stop: always !important;
                }
                /* Print: one A4 sheet = one PDF/printer page (no app chrome, no gaps). */
                @page { size: A4; margin: 0; }
                @media print {
                    html body.vaf-a4 { padding: 0 !important; background: #ffffff !important; }
                    html body.vaf-a4 .vaf-a4-center .a4-pages { gap: 0 !important; }
                    html body.vaf-a4 .a4-page.a4-page { box-shadow: none !important; page-break-after: always !important; }
                    html body.vaf-a4 .a4-page.a4-page:last-child { page-break-after: auto !important; }
                }
            `;

/** Default iframe height is ~150px; expand so stacked A4 sheets are visible (outer panel scrolls). */
function resizeEditorIframe(iframe: HTMLIFrameElement | null): void {
    if (!iframe) return;
    const d = iframe.contentDocument;
    if (!d?.documentElement) return;
    const sh = Math.max(d.documentElement.scrollHeight, d.body?.scrollHeight ?? 0, 320);
    iframe.style.height = `${Math.ceil(sh)}px`;
}

function ensureA4EditorStyles(doc: Document): void {
    if (doc.querySelector('style[data-a4]')) return;
    const style = doc.createElement('style');
    style.setAttribute('data-a4', '1');
    style.textContent = A4_EDITOR_STYLE;
    const head = doc.head;
    if (head) head.appendChild(style);
    else doc.documentElement.appendChild(style);
}

/**
 * Research exports are full HTML documents. Feeding that string to doc.write() can yield fragile iframe layout;
 * we inject only body inner HTML plus original head styles so the editor shell (center + A4) is predictable.
 */
function extractEditorBodyAndHeadStyles(html: string): { bodyInner: string; headInjectHtml: string } {
    const t = html.trim();
    if (typeof window === 'undefined') {
        return { bodyInner: html, headInjectHtml: '' };
    }
    if (!/^\s*<!doctype\b|<html[\s>]/i.test(t)) {
        return { bodyInner: html, headInjectHtml: '' };
    }
    try {
        const parsed = new DOMParser().parseFromString(t, 'text/html');
        const headBits = Array.from(
            parsed.head.querySelectorAll('style, link[rel="stylesheet"], meta[charset], meta[name="viewport"]')
        )
            .map((el) => el.outerHTML)
            .join('');
        return { bodyInner: parsed.body.innerHTML, headInjectHtml: headBits };
    } catch {
        return { bodyInner: html, headInjectHtml: '' };
    }
}

/**
 * Last resort: one physical page still taller than A4 — redistribute direct block children into new pages.
 * Handles height=0 measurement glitches by splitting on block count + character length.
 */
function forceSplitIfSingleTallPage(doc: Document): void {
    const win = doc.defaultView;
    if (!win) return;
    const center = doc.body.querySelector('.vaf-a4-center');
    if (!center) return;

    let targetPage: HTMLElement | null = null;
    const pagesHost = center.querySelector(':scope > .a4-pages');
    if (pagesHost && pagesHost.children.length === 1) {
        const only = pagesHost.children[0];
        if (only.classList.contains('a4-page') && only instanceof HTMLElement) targetPage = only;
    } else if (!pagesHost) {
        const d = center.querySelector(':scope > .a4-page');
        if (d instanceof HTMLElement) targetPage = d;
    }
    if (!targetPage) return;

    applyA4PageBoxLock(targetPage);
    void targetPage.offsetHeight;
    void doc.body.offsetHeight;

    const limitPx = getA4UsableContentHeightPx(doc, targetPage);
    if (limitPx <= 0) return;

    let blocks = Array.from(targetPage.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
    if (blocks.length === 1) {
        const shell = blocks[0];
        const inner = Array.from(shell.children).filter((n): n is HTMLElement => n.nodeType === 1);
        if (inner.length >= 2) {
            for (const x of inner) targetPage.insertBefore(x, shell);
            targetPage.removeChild(shell);
            blocks = Array.from(targetPage.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
        }
    }
    if (blocks.length < 2) return;

    const heights = blocks.map((el) => measureBlockHeight(win, el));
    const useHeights = heights.some((h) => h >= 6);

    const container = doc.createElement('div');
    container.className = 'a4-pages';
    container.setAttribute('contenteditable', 'true');

    if (!useHeights) {
        const approxChars = (targetPage.innerText || '').trim().length;
        const targetPages = Math.min(18, Math.max(2, Math.ceil(approxChars / 2000)));
        const perPage = Math.max(1, Math.ceil(blocks.length / targetPages));
        for (let i = 0; i < blocks.length; i += perPage) {
            const pg = doc.createElement('div');
            pg.className = 'a4-page';
            pg.setAttribute('contenteditable', 'true');
            applyA4PageBoxLock(pg);
            for (let j = i; j < Math.min(i + perPage, blocks.length); j++) {
                pg.appendChild(blocks[j]);
            }
            container.appendChild(pg);
        }
    } else {
        let curPage = doc.createElement('div');
        curPage.className = 'a4-page';
        curPage.setAttribute('contenteditable', 'true');
        applyA4PageBoxLock(curPage);
        container.appendChild(curPage);
        let curH = 0;
        for (let i = 0; i < blocks.length; i++) {
            const el = blocks[i];
            const h = Math.max(8, heights[i]);
            if (curH > 0 && curH + h > limitPx) {
                curPage = doc.createElement('div');
                curPage.className = 'a4-page';
                curPage.setAttribute('contenteditable', 'true');
                applyA4PageBoxLock(curPage);
                container.appendChild(curPage);
                curH = 0;
            }
            curPage.appendChild(el);
            curH += h;
        }
    }

    if (container.children.length > 1) {
        targetPage.parentNode?.replaceChild(container, targetPage);
        applyA4PageLocksUnderCenter(doc);
    }
}

function countEditorA4Pages(doc: Document): number {
    const center = doc.body.querySelector('.vaf-a4-center');
    if (!center) return 0;
    const host = center.querySelector(':scope > .a4-pages');
    if (host) {
        return Array.from(host.children).filter((c) => (c as HTMLElement).classList?.contains('a4-page')).length || host.children.length;
    }
    return center.querySelector(':scope > .a4-page') ? 1 : 0;
}

/**
 * Hard guarantee for research HTML: split using only character count + top-level block count.
 * Does not use scrollHeight (often equals clientHeight when the "page" grows with content).
 */
function ensureMultipageFromBlockCountOnly(doc: Document): void {
    if (countEditorA4Pages(doc) > 1) return;
    const sp = findMonolithicA4Sheet(doc);
    if (!sp) return;
    applyA4PageBoxLock(sp);
    for (let pass = 0; pass < 12; pass++) {
        const secs = Array.from(sp.querySelectorAll(':scope > section, :scope > article'));
        if (secs.length === 0) break;
        for (const w of secs) {
            const par = w.parentNode;
            if (!par) continue;
            while (w.firstChild) par.insertBefore(w.firstChild, w);
            par.removeChild(w);
        }
    }
    expandMonolithicShell(sp);
    hoistMultiChildWrappers(sp);
    unwrapSingleBlockChildContainers(sp);
    hoistMultiChildWrappers(sp);
    for (let peel = 0; peel < 40 && sp.childElementCount === 1; peel++) {
        const sole = sp.firstElementChild as HTMLElement | null;
        if (!sole) break;
        const inner = Array.from(sole.children).filter((n): n is HTMLElement => n.nodeType === 1);
        if (inner.length < 2) break;
        for (const x of inner) sp.insertBefore(x, sole);
        sp.removeChild(sole);
    }
    const blocks = Array.from(sp.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
    const chars = (sp.innerText || '').trim().length;
    if (blocks.length < 2 || chars < 900) return;
    const desiredPages = Math.min(28, Math.max(2, Math.ceil(chars / 1300)));
    let perPage = Math.max(1, Math.ceil(blocks.length / desiredPages));
    if (perPage >= blocks.length) perPage = Math.max(1, Math.floor(blocks.length / 2));
    const container = doc.createElement('div');
    container.className = 'a4-pages';
    container.setAttribute('contenteditable', 'true');
    for (let i = 0; i < blocks.length; i += perPage) {
        const pg = doc.createElement('div');
        pg.className = 'a4-page';
        pg.setAttribute('contenteditable', 'true');
        applyA4PageBoxLock(pg);
        for (let j = i; j < Math.min(i + perPage, blocks.length); j++) {
            pg.appendChild(blocks[j]);
        }
        container.appendChild(pg);
    }
    if (container.children.length > 1) {
        sp.parentNode?.replaceChild(container, sp);
        applyA4PageLocksUnderCenter(doc);
    }
}

/**
 * Paginate *before* writing into the iframe, using a detached DOMParser document (always has no layout quirks).
 * The iframe's own `defaultView` can be missing briefly; `paginateIntoA4Pages` then no-ops forever.
 */
function prepareEditorBodyHtml(bodyInner: string): string {
    const raw = (bodyInner || '').trim();
    if (typeof window === 'undefined' || raw.length < 400) return bodyInner;
    const pageMarker = (raw.match(/\ba4-page\b/gi) || []).length;
    if (pageMarker >= 2 && /\ba4-pages\b/i.test(raw)) return bodyInner;

    try {
        const pd = new DOMParser().parseFromString(
            '<!DOCTYPE html><html><head></head><body class="vaf-a4"></body></html>',
            'text/html'
        );
        const b = pd.body;
        b.innerHTML = bodyInner;
        const wrap = pd.createElement('div');
        wrap.className = 'a4-page';
        wrap.setAttribute('contenteditable', 'true');
        while (b.firstChild) wrap.appendChild(b.firstChild);
        const center = pd.createElement('div');
        center.className = 'vaf-a4-center';
        center.appendChild(wrap);
        b.appendChild(center);
        ensureMultipageFromBlockCountOnly(pd);
        return b.innerHTML;
    } catch {
        return bodyInner;
    }
}

/** Inline lock wins over almost all embedded report stylesheets (prevents one “infinite” sheet). */
function applyA4PageBoxLock(el: HTMLElement): void {
    el.style.setProperty('width', '210mm', 'important');
    el.style.setProperty('height', '297mm', 'important');
    el.style.setProperty('min-height', '297mm', 'important');
    el.style.setProperty('max-height', '297mm', 'important');
    el.style.setProperty('box-sizing', 'border-box', 'important');
    el.style.setProperty('overflow', 'hidden', 'important');
}

/** Move direct element children of sole up to page (one shell → many blocks). */
function hoistDirectChildrenOntoPage(page: HTMLElement, sole: HTMLElement): boolean {
    const kids = Array.from(sole.children).filter((n): n is HTMLElement => n.nodeType === 1);
    if (kids.length < 2) return false;
    for (const k of kids) page.insertBefore(k, sole);
    page.removeChild(sole);
    return true;
}

/** Move only top-level <p> inside sole to page (common report pattern: one div > many p). */
function hoistTopLevelParagraphsOntoPage(page: HTMLElement, sole: HTMLElement): boolean {
    const kids = Array.from(sole.children) as HTMLElement[];
    if (kids.length < 2) return false;
    if (!kids.every((k) => k.tagName.toLowerCase() === 'p')) return false;
    for (const p of kids) page.insertBefore(p, sole);
    page.removeChild(sole);
    return true;
}

/** Split one table across pages by row height (TR must sit in TBODY). */
function paginateSingleTableIntoPages(
    doc: Document,
    singlePage: HTMLElement,
    table: HTMLTableElement,
    limit: number,
    win: Window
): HTMLDivElement | null {
    const rows = Array.from(table.querySelectorAll('tr')) as HTMLTableRowElement[];
    if (rows.length < 2) return null;
    const tableClass = table.className;
    const container = doc.createElement('div');
    container.className = 'a4-pages';
    container.setAttribute('contenteditable', 'true');
    let curPage = doc.createElement('div');
    curPage.className = 'a4-page';
    curPage.setAttribute('contenteditable', 'true');
    applyA4PageBoxLock(curPage);
    container.appendChild(curPage);
    let curH = 0;
    let rowBuf: HTMLTableRowElement[] = [];

    const flushBuf = () => {
        if (rowBuf.length === 0) return;
        const tbl = doc.createElement('table');
        if (tableClass) tbl.className = tableClass;
        const tb = doc.createElement('tbody');
        for (const r of rowBuf) tb.appendChild(r);
        tbl.appendChild(tb);
        curPage.appendChild(tbl);
        rowBuf = [];
    };

    for (const tr of rows) {
        const h = measureBlockHeight(win, tr);
        if (curH > 0 && curH + h > limit) {
            flushBuf();
            curPage = doc.createElement('div');
            curPage.className = 'a4-page';
            curPage.setAttribute('contenteditable', 'true');
            applyA4PageBoxLock(curPage);
            container.appendChild(curPage);
            curH = 0;
        }
        rowBuf.push(tr);
        curH += h;
    }
    flushBuf();
    return container.children.length > 1 ? container : null;
}

/** Research HTML often wraps everything in one div — unwrap so pagination sees many blocks. */
function unwrapSingleBlockChildContainers(root: HTMLElement): void {
    const BLOCK = new Set(['div', 'main', 'article', 'section']);
    for (let pass = 0; pass < 30; pass++) {
        const els = Array.from(root.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
        if (els.length !== 1) break;
        const only = els[0];
        const tag = only.tagName.toLowerCase();
        if (!BLOCK.has(tag)) break;
        while (only.firstChild) root.insertBefore(only.firstChild, only);
        root.removeChild(only);
    }
}

const HOIST_WRAPPER_TAGS = new Set(['div', 'main', 'article', 'section', 'center', 'form']);

/** Flatten div>div>…>content until multiple top-level blocks or nothing left to peel. */
function expandMonolithicShell(page: HTMLElement): void {
    for (let z = 0; z < 80; z++) {
        const els = Array.from(page.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
        if (els.length > 1) return;
        if (els.length === 0) return;
        const sole = els[0];
        if (hoistDirectChildrenOntoPage(page, sole)) continue;
        if (hoistTopLevelParagraphsOntoPage(page, sole)) continue;
        const tag = sole.tagName.toLowerCase();
        if (HOIST_WRAPPER_TAGS.has(tag) && sole.children.length === 1) {
            const inner = sole.children[0];
            page.insertBefore(inner, sole);
            page.removeChild(sole);
            continue;
        }
        return;
    }
}

/** Hoist when a single wrapper has multiple element children (nested report shells). */
function hoistMultiChildWrappers(page: HTMLElement): void {
    for (let pass = 0; pass < 40; pass++) {
        const els = Array.from(page.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
        if (els.length !== 1) break;
        const only = els[0];
        const tag = only.tagName.toLowerCase();
        if (!HOIST_WRAPPER_TAGS.has(tag)) break;
        const inner = Array.from(only.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
        if (inner.length < 2) break;
        while (only.firstChild) page.insertBefore(only.firstChild, only);
        page.removeChild(only);
    }
}

/**
 * The sheet we split: direct `.a4-page` under center, or a single `.a4-page` inside `.a4-pages`
 * (e.g. failed earlier pagination). Must NOT use document.querySelector('.a4-page') — that hits
 * the first page inside an already-split stack and nests broken structure.
 */
function findMonolithicA4Sheet(doc: Document): HTMLElement | null {
    const center = doc.body.querySelector('.vaf-a4-center');
    if (!center) return null;
    const pagesHost = center.querySelector(':scope > .a4-pages');
    if (pagesHost) {
        if (pagesHost.children.length > 1) return null;
        if (pagesHost.children.length === 1) {
            const only = pagesHost.children[0];
            if (only.classList.contains('a4-page') && only instanceof HTMLElement) return only;
        }
    }
    const direct = center.querySelector(':scope > .a4-page');
    return direct instanceof HTMLElement ? direct : null;
}

/**
 * Research HTML often wraps the whole report in one div; pagination needs direct block children.
 */
function hoistDirectChildrenIfOneWrapper(page: HTMLElement): void {
    const els = Array.from(page.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
    if (els.length !== 1) return;
    const shell = els[0];
    const tag = shell.tagName.toLowerCase();
    if (!['div', 'main', 'article', 'section'].includes(tag)) return;
    if (shell.children.length < 2) return;
    while (shell.firstChild) page.insertBefore(shell.firstChild, shell);
    page.removeChild(shell);
}

function measureBlockHeight(win: Window, el: HTMLElement): number {
    const cs = win.getComputedStyle(el);
    const mt = parseFloat(cs.marginTop) || 0;
    const mb = parseFloat(cs.marginBottom) || 0;
    // Embedded report CSS (flex/%) can make offsetHeight match the clipped page; scrollHeight keeps real content height.
    const h = Math.max(el.offsetHeight, el.scrollHeight, el.getBoundingClientRect().height);
    return h + mt + mb;
}

/** Pure text/inline paragraph that may be split across pages (no nested blocks/media). */
function isSplittableTextBlock(el: HTMLElement): boolean {
    const tag = el.tagName.toLowerCase();
    if (tag !== 'p' && tag !== 'div') return false;
    return !el.querySelector('p,div,ul,ol,table,h1,h2,h3,h4,h5,h6,img,figure,pre,blockquote');
}

/**
 * Split a long text block at a sentence boundary so the first part fills the
 * remaining space of the current page instead of leaving it as dead space.
 * Split points are only taken OUTSIDE inline tags (an <a>/<em> never gets cut).
 * Returns null when no sentence prefix fits.
 */
function splitBlockToFit(
    doc: Document,
    win: Window,
    el: HTMLElement,
    curPage: HTMLElement,
    remainingPx: number
): { first: HTMLElement; rest: HTMLElement } | null {
    const tokens = (el.innerHTML || '').split(/(<[^>]+>)/);
    // Candidate split offsets into a flat html string, only at depth 0 after sentence ends.
    const html = tokens.join('');
    const candidates: number[] = [];
    let pos = 0;
    let depth = 0;
    for (const tok of tokens) {
        if (tok.startsWith('<')) {
            if (/^<\//.test(tok)) depth = Math.max(0, depth - 1);
            else if (!/\/>$/.test(tok) && !/^<(br|hr|img|wbr)\b/i.test(tok)) depth += 1;
            pos += tok.length;
            continue;
        }
        if (depth === 0) {
            const re = /[.!?:;]["')\]]?\s+/g;
            let m: RegExpExecArray | null;
            while ((m = re.exec(tok)) !== null) candidates.push(pos + m.index + m[0].length);
        }
        pos += tok.length;
    }
    if (candidates.length < 1) return null;

    const probe = el.cloneNode(false) as HTMLElement;
    curPage.appendChild(probe);
    const fits = (cut: number): boolean => {
        probe.innerHTML = html.slice(0, cut);
        return measureBlockHeight(win, probe) <= remainingPx;
    };
    try {
        // Binary search the largest sentence prefix that still fits.
        let lo = 0, hi = candidates.length - 1, best = -1;
        if (!fits(candidates[0])) return null;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            if (fits(candidates[mid])) { best = mid; lo = mid + 1; }
            else hi = mid - 1;
        }
        if (best < 0) return null;
        const cut = candidates[best];
        const restHtml = html.slice(cut).trim();
        if (!restHtml) return null;
        const first = el.cloneNode(false) as HTMLElement;
        first.innerHTML = html.slice(0, cut).trim();
        const rest = el.cloneNode(false) as HTMLElement;
        rest.innerHTML = restHtml;
        return { first, rest };
    } finally {
        probe.remove();
    }
}

/**
 * Usable inner height for one A4 page (content box inside 25mm padding).
 * When embedded CSS lets `.a4-page` grow, clientHeight matches full content — use a probe
 * so we still paginate at real A4 size.
 */
function getA4UsableContentHeightPx(doc: Document, singlePage: HTMLElement): number {
    const win = doc.defaultView;
    if (!win) return 0;
    const probe = doc.createElement('div');
    probe.setAttribute('data-a4-height-probe', '1');
    probe.style.cssText =
        'position:absolute;left:-99999px;top:0;width:210mm;height:297mm;padding:25mm;box-sizing:border-box;margin:0;border:0;visibility:hidden;pointer-events:none;';
    doc.body.appendChild(probe);
    const pcs = win.getComputedStyle(probe);
    const pTop = parseFloat(pcs.paddingTop) || 0;
    const pBot = parseFloat(pcs.paddingBottom) || 0;
    const probeInner = probe.clientHeight - pTop - pBot;
    doc.body.removeChild(probe);

    const pageStyle = win.getComputedStyle(singlePage);
    const padTop = parseFloat(pageStyle.paddingTop) || 0;
    const padBot = parseFloat(pageStyle.paddingBottom) || 0;
    const fromClient = singlePage.clientHeight - padTop - padBot;

    if (fromClient <= 0) return probeInner;
    if (fromClient > probeInner * 1.12) return probeInner;
    return fromClient;
}

/**
 * Height-based pagination: measure rendered element heights and distribute
 * across A4 pages so content flows naturally like Word / Google Docs.
 */
function applyA4PageLocksUnderCenter(doc: Document): void {
    doc.querySelectorAll('.vaf-a4-center .a4-page').forEach((el) => {
        if (el instanceof HTMLElement) applyA4PageBoxLock(el);
    });
}

function paginateIntoA4Pages(doc: Document): void {
    const singlePage = findMonolithicA4Sheet(doc);
    if (!singlePage) return;

    applyA4PageBoxLock(singlePage);
    expandMonolithicShell(singlePage);

    hoistDirectChildrenIfOneWrapper(singlePage);

    for (let pass = 0; pass < 5; pass++) {
        const wrappers = singlePage.querySelectorAll(':scope > section, :scope > article');
        if (wrappers.length === 0) break;
        wrappers.forEach((w) => {
            const parent = w.parentNode;
            if (!parent) return;
            while (w.firstChild) parent.insertBefore(w.firstChild, w);
            parent.removeChild(w);
        });
    }

    hoistMultiChildWrappers(singlePage);
    unwrapSingleBlockChildContainers(singlePage);
    hoistMultiChildWrappers(singlePage);

    // One tall wrapper with many inner sections: hoist until we have multiple top-level blocks or layout stabilizes.
    for (let pass = 0; pass < 25; pass++) {
        const els = Array.from(singlePage.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
        if (els.length !== 1) break;
        const only = els[0];
        const tag = only.tagName.toLowerCase();
        if (!HOIST_WRAPPER_TAGS.has(tag)) break;
        const subs = Array.from(only.children).filter((n): n is HTMLElement => n.nodeType === 1);
        if (subs.length < 2) break;
        const overflow = singlePage.scrollHeight > singlePage.clientHeight + 8;
        const longText = (singlePage.innerText || '').trim().length > 900;
        if (!overflow && !longText) break;
        while (only.firstChild) singlePage.insertBefore(only.firstChild, only);
        singlePage.removeChild(only);
    }

    void singlePage.offsetHeight;
    void doc.body.offsetHeight;

    const win = doc.defaultView;
    if (!win) return;

    let pageContentHeight = getA4UsableContentHeightPx(doc, singlePage);
    if (pageContentHeight <= 0) return;

    expandMonolithicShell(singlePage);
    void singlePage.offsetHeight;
    const topBlocks = Array.from(singlePage.children) as HTMLElement[];
    if (topBlocks.length === 1 && topBlocks[0].tagName === 'TABLE') {
        const tableBox = paginateSingleTableIntoPages(doc, singlePage, topBlocks[0] as HTMLTableElement, pageContentHeight, win);
        if (tableBox && tableBox.children.length > 1) {
            singlePage.parentNode?.replaceChild(tableBox, singlePage);
            applyA4PageLocksUnderCenter(doc);
            return;
        }
    }

    const buildPages = (limit: number): HTMLDivElement | null => {
        const children = Array.from(singlePage.childNodes).filter(
            (n): n is HTMLElement => n.nodeType === 1
        );
        if (children.length <= 1) return null;

        const items: { el: HTMLElement; h: number }[] = children.map((el) => ({
            el,
            h: measureBlockHeight(win, el),
        }));

        const totalH = items.reduce((s, i) => s + i.h, 0);
        const overflowPx = singlePage.scrollHeight - singlePage.clientHeight;
        const visuallyOverflows = overflowPx > 10;
        const longDoc = (singlePage.innerText || '').trim().length > 1600;
        // When the box grows with content, scrollHeight ≈ clientHeight — still paginate long documents.
        if (totalH <= limit && !visuallyOverflows && !longDoc) return null;

        // Layout not ready (all heights ~0) but box already clips — split by child count (print-engine style fallback).
        if (visuallyOverflows && items.length >= 2 && items.every((i) => i.h < 4)) {
            const container = doc.createElement('div');
            container.className = 'a4-pages';
            container.setAttribute('contenteditable', 'true');
            const mid = Math.ceil(items.length / 2);
            for (let p = 0; p < 2; p++) {
                const curPage = doc.createElement('div');
                curPage.className = 'a4-page';
                curPage.setAttribute('contenteditable', 'true');
                applyA4PageBoxLock(curPage);
                const slice = p === 0 ? items.slice(0, mid) : items.slice(mid);
                for (const { el } of slice) curPage.appendChild(el);
                container.appendChild(curPage);
            }
            return container.children.length > 1 ? container : null;
        }

        const container = doc.createElement('div');
        container.className = 'a4-pages';
        container.setAttribute('contenteditable', 'true');

        let curPage = doc.createElement('div');
        curPage.className = 'a4-page';
        curPage.setAttribute('contenteditable', 'true');
        applyA4PageBoxLock(curPage);
        container.appendChild(curPage);
        let curH = 0;

        const newPage = () => {
            curPage = doc.createElement('div');
            curPage.className = 'a4-page';
            curPage.setAttribute('contenteditable', 'true');
            applyA4PageBoxLock(curPage);
            container.appendChild(curPage);
            curH = 0;
        };
        const measureDetached = (node: HTMLElement): number => {
            curPage.appendChild(node);
            const hh = measureBlockHeight(win, node);
            node.remove();
            return hh;
        };

        // Queue-based packing: long text blocks are split at sentence boundaries so
        // a block that does not fit fills the remaining space instead of moving to
        // the next page and leaving large dead space behind.
        const queue = items.slice();
        while (queue.length > 0) {
            const { el, h } = queue.shift()!;
            const remaining = limit - curH;
            if (h > remaining && isSplittableTextBlock(el)) {
                const room = curH > 0 ? remaining : limit;
                if (room > 140) {
                    const split = splitBlockToFit(doc, win, el, curPage, room);
                    if (split) {
                        curPage.appendChild(split.first);
                        el.remove();
                        queue.unshift({ el: split.rest, h: measureDetached(split.rest) });
                        newPage();
                        continue;
                    }
                }
            }
            if (curH > 0 && curH + h > limit) newPage();
            curPage.appendChild(el);
            curH += h;
        }

        return container.children.length > 1 ? container : null;
    };

    let container: HTMLDivElement | null = null;
    for (let attempt = 0; attempt < 8; attempt++) {
        expandMonolithicShell(singlePage);
        void singlePage.offsetHeight;
        container = buildPages(pageContentHeight);
        if (container) break;
        pageContentHeight *= 0.9;
        if (pageContentHeight < 120) break;
    }

    if (container && container.children.length > 1) {
        singlePage.parentNode?.replaceChild(container, singlePage);
        applyA4PageLocksUnderCenter(doc);
        return;
    }

    // Fallback: many blocks + long text but height/scroll metrics did not trigger a split (fonts, iframe timing).
    const runCharBlockFallback = (): void => {
        let blocks = Array.from(singlePage.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
        const approxChars = (singlePage.innerText || '').trim().length;
        const overflows = singlePage.scrollHeight > singlePage.clientHeight + 8;
        // Single outer shell: distribute its element children across pages (common for research HTML).
        if (blocks.length === 1 && approxChars > 500) {
            const shell = blocks[0];
            const inner = Array.from(shell.children).filter((n): n is HTMLElement => n.nodeType === 1);
            if (inner.length >= 2) {
                for (const n of inner) singlePage.insertBefore(n, shell);
                singlePage.removeChild(shell);
                blocks = Array.from(singlePage.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
            }
        }
        // One leaf block with lots of plain text (no inner elements) — split on blank lines so pagination can run.
        if (blocks.length === 1 && approxChars > 1400) {
            const sole = blocks[0];
            const t = sole.tagName.toLowerCase();
            if ((t === 'p' || t === 'div' || t === 'pre') && sole.children.length === 0) {
                const raw = sole.textContent || '';
                const paras = raw
                    .split(/\n{2,}/)
                    .map((s) => s.trim())
                    .filter((s) => s.length > 0);
                if (paras.length >= 2) {
                    sole.remove();
                    for (const pr of paras) {
                        const p = doc.createElement('p');
                        p.textContent = pr;
                        singlePage.appendChild(p);
                    }
                    blocks = Array.from(singlePage.childNodes).filter((n): n is HTMLElement => n.nodeType === 1);
                }
            }
        }
        const minBlocks = overflows && approxChars > 600 ? 2 : 3;
        if (blocks.length < minBlocks || approxChars < 500) return;
        if (!overflows && approxChars < 1200) return;
        const targetPages = Math.min(20, Math.max(2, Math.ceil(approxChars / 1800)));
        const perPage = Math.max(1, Math.ceil(blocks.length / targetPages));
        const fb = doc.createElement('div');
        fb.className = 'a4-pages';
        fb.setAttribute('contenteditable', 'true');
        for (let i = 0; i < blocks.length; i += perPage) {
            const pg = doc.createElement('div');
            pg.className = 'a4-page';
            pg.setAttribute('contenteditable', 'true');
            applyA4PageBoxLock(pg);
            for (let j = i; j < Math.min(i + perPage, blocks.length); j++) {
                pg.appendChild(blocks[j]);
            }
            fb.appendChild(pg);
        }
        if (fb.children.length > 1) {
            singlePage.parentNode?.replaceChild(fb, singlePage);
            applyA4PageLocksUnderCenter(doc);
        }
    };
    runCharBlockFallback();
    applyA4PageLocksUnderCenter(doc);
}

function injectHighlightsInBody(body: HTMLElement, segments: { start: number; end: number; colorIndex: number }[], doc: Document): void {
    const existing = body.querySelectorAll('span[data-highlight]');
    existing.forEach((span) => {
        const parent = span.parentNode;
        if (!parent) return;
        while (span.firstChild) parent.insertBefore(span.firstChild, span);
        parent.removeChild(span);
    });
    if (segments.length === 0) return;
    const textNodes = getTextNodesInOrder(body);
    const totalLen = textNodes.length ? textNodes[textNodes.length - 1].end : 0;
    if (totalLen === 0) return;
    for (const seg of segments) {
        const segStart = Math.max(0, seg.start);
        const segEnd = Math.min(totalLen, seg.end);
        if (segEnd <= segStart) continue;
        const overlapping: { node: Text; localStart: number; localEnd: number }[] = [];
        for (const { node, start, end } of textNodes) {
            const overlapStart = Math.max(segStart, start);
            const overlapEnd = Math.min(segEnd, end);
            if (overlapEnd <= overlapStart) continue;
            overlapping.push({
                node,
                localStart: overlapStart - start,
                localEnd: overlapEnd - start,
            });
        }
        for (const { node, localStart, localEnd } of overlapping.reverse()) {
            const range = doc.createRange();
            try {
                range.setStart(node, localStart);
                range.setEnd(node, localEnd);
            } catch {
                continue;
            }
            let fragment: DocumentFragment | null = null;
            try {
                fragment = range.extractContents();
                if (!fragment || fragment.childNodes.length === 0) {
                    if (fragment) range.insertNode(fragment);
                    continue;
                }
                const span = doc.createElement('span');
                span.setAttribute('data-highlight', String(seg.colorIndex));
                const colors = INSERTION_HIGHLIGHT_COLORS[seg.colorIndex % INSERTION_HIGHLIGHT_COLORS.length];
                span.style.setProperty('background-color', colors.bg, 'important');
                span.style.setProperty('color', colors.text, 'important');
                span.style.setProperty('padding', '0 2px', 'important');
                span.appendChild(fragment);
                range.insertNode(span);
            } catch {
                if (fragment) try { range.insertNode(fragment); } catch { /* restore */ }
            }
        }
    }
}

export default function DocumentEditor(props: DocumentEditorProps) {
    if (isDocxPath(props.filePath || '')) {
        return (
            <NativeDocxEditor
                isOpen={props.isOpen}
                onClose={props.onClose}
                canClose={props.canClose}
                filePath={props.filePath || ''}
                title={props.title || 'Document Editor'}
                initialModel={props.initialDocxModel}
                onModelChange={props.onDocxModelChange}
                onInsertSelection={
                    props.onInsertSelection
                        ? (text, range) => props.onInsertSelection?.(text, range)
                        : undefined
                }
                insertedSelections={props.insertedSelections}
            />
        );
    }

    return <LegacyDocumentEditor {...props} />;
}

function LegacyDocumentEditor({
    isOpen,
    onClose,
    canClose = true,
    filePath = '',
    title = 'Document Editor',
    initialContent = '',
    onContentChange,
    initialDocxModel,
    onDocxModelChange,
    mode = 'overlay',
    status = '',
    presence,
    steps = [],
    documents = [],
    onAddFiles,
    onRemoveDocument,
    onInsertSelection,
    insertedSelectionsCount = 0,
    insertedSelections = [],
}: DocumentEditorProps) {
    const [content, setContent] = useState<string>(initialContent);
    /**
     * Sync from parent when content is pushed from outside (e.g. agent replace_editor_selection).
     * Parent often keeps `content` undefined/'' while `loadDocument()` runs — must not wipe fetched HTML.
     */
    useEffect(() => {
        if (!isOpen || initialContent === undefined) return;
        if (initialContent === content) return;
        const ic = (initialContent || '').trim();
        // Parent often keeps '' while loadDocument() fills state — never wipe fetched HTML with empty prop.
        if (ic === '' && content.trim().length > 0) return;
        setContent(initialContent);
    }, [isOpen, initialContent, content]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [saveMessage, setSaveMessage] = useState<string | null>(null);
    /** Current selection format so toolbar reflects it (like Word). */
    const [selectionFormat, setSelectionFormat] = useState<{
        fontName: string;
        fontSize: string;
        foreColor: string;
        bold: boolean;
        italic: boolean;
        underline: boolean;
        justifyLeft: boolean;
        justifyCenter: boolean;
        justifyRight: boolean;
    }>({
        fontName: 'Arial',
        fontSize: '3',
        foreColor: '#000000',
        bold: false,
        italic: false,
        underline: false,
        justifyLeft: true,
        justifyCenter: false,
        justifyRight: false,
    });
    const iframeRef = useRef<HTMLIFrameElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const attachmentsContentRef = useRef<HTMLDivElement>(null);
    const onInsertSelectionRef = useRef(onInsertSelection);
    onInsertSelectionRef.current = onInsertSelection;
    const insertedSelectionsRef = useRef(insertedSelections);
    insertedSelectionsRef.current = insertedSelections;
    const onContentChangeRef = useRef(onContentChange);
    onContentChangeRef.current = onContentChange;
    /** When user requests close, show browser confirm and only call onClose if they confirm. */
    const handleRequestClose = useCallback(() => {
        if (typeof window === 'undefined') {
            onClose();
            return;
        }
        const message = 'Möchten Sie den Dokument-Editor wirklich schließen? Ungespeicherte Änderungen können verloren gehen.';
        if (window.confirm(message)) {
            onClose();
        }
    }, [onClose]);
    /** When true, the last content update came from the iframe (user typing). Skip rewriting iframe to avoid focus loss. */
    const contentFromIframeRef = useRef(false);
    /** While pagination moves DOM nodes, `input` can fire — syncing would re-enter useEffect and can exceed React update depth. */
    const paginatingIframeRef = useRef(false);
    /** Saved selection when user clicks toolbar (so we can restore and execCommand). */
    const savedSelectionRef = useRef<{ doc: Document; range: Range } | null>(null);
    const [selectedAttachmentId, setSelectedAttachmentId] = useState<string | null>(null);
    const [attachmentsDropdownOpen, setAttachmentsDropdownOpen] = useState(false);

    const isAttachmentsMode = Array.isArray(documents) && documents.length >= 0 && !filePath;

    // Use presence from props if available, otherwise infer from status text
    const statusLower = (status || '').toLowerCase();
    const inferredPresence = presence
        ? presence
        : statusLower.includes('error') || statusLower.includes('fail')
            ? 'error'
            : statusLower.includes('saving') || statusLower.includes('loading')
                ? 'online'
                : 'idle';
    const presenceLabel = inferredPresence === 'online' ? 'Active' : inferredPresence === 'error' ? 'Error' : 'Ready';
    const presenceTone = inferredPresence === 'online'
        ? 'bg-emerald-500'
        : inferredPresence === 'error'
            ? 'bg-red-500'
            : 'bg-gray-400';

    const hasWorkflow = steps.length > 0;
    const displayFile = filePath?.split(/[/\\]/).pop() || 'document.html';
    const isBinaryDocument = isBinaryDocumentPath(filePath);

    const selectedAttachment = documents.find((d) => d.id === selectedAttachmentId) ?? documents[0];
    const attachmentDisplayContent = selectedAttachment?.content ?? '';
    const isAttachmentImage = selectedAttachment?.mimeType?.startsWith('image/');
    const attachmentContentWithHighlights = useMemo(() => {
        const text = attachmentDisplayContent || '(Kein Textinhalt)';
        const rangesForDoc = insertedSelections
            .map((s, i) => ({ start: s.start, end: s.end, colorIndex: i, documentId: s.documentId }))
            .filter((s) => s.documentId === selectedAttachment?.id)
            .map(({ start, end, colorIndex }) => ({ start, end, colorIndex }));
        if (rangesForDoc.length === 0) return null;
        const segments = buildHighlightSegments(text.length, rangesForDoc);
        const parts: React.ReactNode[] = [];
        let lastEnd = 0;
        for (let i = 0; i < segments.length; i++) {
            const seg = segments[i];
            if (seg.start > lastEnd) parts.push(text.slice(lastEnd, seg.start));
            parts.push(
                <span key={i} className={cn('rounded-sm', CHIP_BG_CLASSES[seg.colorIndex % CHIP_BG_CLASSES.length])}>
                    {text.slice(seg.start, seg.end)}
                </span>
            );
            lastEnd = seg.end;
        }
        if (lastEnd < text.length) parts.push(text.slice(lastEnd));
        return parts;
    }, [attachmentDisplayContent, selectedAttachment?.id, insertedSelections]);

    const handleAttachmentsContentMouseUp = () => {
        if (!onInsertSelection || !selectedAttachment) return;
        const sel = typeof window !== 'undefined' ? window.getSelection() : null;
        if (!sel || !attachmentsContentRef.current || !sel.rangeCount) return;
        const text = sel.toString().trim();
        if (!text) return;
        if (!attachmentsContentRef.current.contains(sel.anchorNode)) return;
        const pre = attachmentsContentRef.current.querySelector('pre');
        if (!pre) return;
        const r = sel.getRangeAt(0);
        const startRange = document.createRange();
        startRange.setStart(pre, 0);
        startRange.setEnd(r.startContainer, r.startOffset);
        const start = startRange.toString().length;
        const end = start + text.length;
        onInsertSelection(text, { start, end, documentId: selectedAttachment.id });
    };

    const handleAttachmentFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = e.target.files ? Array.from(e.target.files) : [];
        if (files.length && onAddFiles) onAddFiles(files);
        e.target.value = '';
    };

    useEffect(() => {
        if (isAttachmentsMode && documents.length > 0 && !selectedAttachmentId)
            setSelectedAttachmentId(documents[0].id);
        if (isAttachmentsMode && documents.length > 0 && !documents.find((d) => d.id === selectedAttachmentId))
            setSelectedAttachmentId(documents[0].id);
    }, [isAttachmentsMode, documents, selectedAttachmentId]);

    // Load content from file when opened. Skip load when initialContent is set (restored from session).
    useEffect(() => {
        if (isOpen && filePath && !initialContent && !isBinaryDocument) {
            loadDocument();
        }
    }, [isOpen, filePath, isBinaryDocument, initialContent]);

    const FONT_SIZES = [['1', '10 pt'], ['2', '11 pt'], ['3', '12 pt'], ['4', '14 pt'], ['5', '16 pt'], ['6', '18 pt'], ['7', '24 pt']] as const;
    const FONT_FAMILIES = ['Arial', 'Helvetica', 'Times New Roman', 'Georgia', 'Courier New', 'Verdana'];

    /** Read current selection format from iframe and update toolbar state (Word-style). */
    const updateSelectionFormat = useCallback(() => {
        const doc = iframeRef.current?.contentDocument;
        if (!doc) return;
        const fontName = (doc.queryCommandValue('fontName') || '').replace(/['"]/g, '') || 'Arial';
        const fontSize = doc.queryCommandValue('fontSize') || '3';
        let foreColor = doc.queryCommandValue('foreColor') || '#000000';
        if (foreColor.startsWith('rgb')) {
            const m = foreColor.match(/\d+/g);
            if (m && m.length >= 3)
                foreColor = '#' + [Number(m[0]), Number(m[1]), Number(m[2])].map(x => x.toString(16).padStart(2, '0')).join('');
        }
        if (!foreColor.startsWith('#')) foreColor = '#000000';
        setSelectionFormat({
            fontName: FONT_FAMILIES.includes(fontName) ? fontName : 'Arial',
            fontSize: ['1', '2', '3', '4', '5', '6', '7'].includes(fontSize) ? fontSize : '3',
            foreColor,
            bold: doc.queryCommandState('bold'),
            italic: doc.queryCommandState('italic'),
            underline: doc.queryCommandState('underline'),
            justifyLeft: doc.queryCommandState('justifyLeft'),
            justifyCenter: doc.queryCommandState('justifyCenter'),
            justifyRight: doc.queryCommandState('justifyRight'),
        });
    }, []);

    // Update iframe content when content changes from outside (load/save); do NOT rewrite when user is typing (would steal focus)
    useEffect(() => {
        if (!iframeRef.current || !content) return;
        const doc = iframeRef.current.contentDocument;
        if (!doc) return;
        if (contentFromIframeRef.current) {
            contentFromIframeRef.current = false;
            return;
        }
        const { bodyInner: rawBody, headInjectHtml } = extractEditorBodyAndHeadStyles(content);
        const bodyInner = prepareEditorBodyHtml(rawBody);
        doc.open();
        doc.write(
            `<!DOCTYPE html><html><head><meta charset="utf-8"/>${headInjectHtml}</head><body></body></html>`
        );
        doc.close();
        doc.body.innerHTML = bodyInner;
        doc.body.classList.add('vaf-a4');
        doc.body.style.setProperty('margin', '0', 'important');
        doc.body.style.setProperty('max-width', 'none', 'important');
        // Before wrapping/measuring: lock A4 rules so embedded report CSS cannot expand one endless "page".
        ensureA4EditorStyles(doc);

        const scheduleA4Pagination = () => {
            const delays = [40, 120, 320, 700, 1400];
            paginatingIframeRef.current = true;
            let attemptIndex = 0;
            const runOne = () => {
                if (attemptIndex >= delays.length) {
                    paginatingIframeRef.current = false;
                    resizeEditorIframe(iframeRef.current);
                    return;
                }
                const ms = delays[attemptIndex];
                attemptIndex += 1;
                setTimeout(() => {
                    requestAnimationFrame(() => {
                        requestAnimationFrame(() => {
                            try {
                                ensureA4EditorStyles(doc);
                                void doc.body.offsetHeight;
                                paginateIntoA4Pages(doc);
                                forceSplitIfSingleTallPage(doc);
                                ensureMultipageFromBlockCountOnly(doc);
                                const center = doc.body.querySelector('.vaf-a4-center');
                                const pages = center?.querySelector(':scope > .a4-pages');
                                const onePage =
                                    pages?.children.length === 1
                                        ? (pages.children[0] as HTMLElement)
                                        : (center?.querySelector(':scope > .a4-page') as HTMLElement | null);
                                const stillOverflows =
                                    !!onePage &&
                                    onePage.scrollHeight > onePage.clientHeight + 12 &&
                                    (!pages || pages.children.length === 1);
                                if (stillOverflows && attemptIndex < delays.length) {
                                    runOne();
                                } else {
                                    paginatingIframeRef.current = false;
                                    resizeEditorIframe(iframeRef.current);
                                }
                            } catch {
                                paginatingIframeRef.current = false;
                                resizeEditorIframe(iframeRef.current);
                            }
                        });
                    });
                }, ms);
            };
            runOne();
            window.setTimeout(() => resizeEditorIframe(iframeRef.current), 1900);
        };

        // Loose HTML → center + one sheet. Skip if session restore already has .vaf-a4-center (avoid nesting).
        const hasPagesHost = !!doc.body.querySelector('.a4-pages');
        const hasCenter = !!doc.body.querySelector(':scope > .vaf-a4-center');
        if (!hasPagesHost && !hasCenter) {
            const wrap = doc.createElement('div');
            wrap.className = 'a4-page';
            wrap.setAttribute('contenteditable', 'true');
            while (doc.body.firstChild) wrap.appendChild(doc.body.firstChild);
            const centerWrap = doc.createElement('div');
            centerWrap.className = 'vaf-a4-center';
            centerWrap.appendChild(wrap);
            doc.body.appendChild(centerWrap);
        }
        const existingPages = doc.body.querySelector('.a4-pages') || doc.body.querySelector('.a4-page');
        if (existingPages && !doc.body.querySelector('.vaf-a4-center')) {
            const centerWrap = doc.createElement('div');
            centerWrap.className = 'vaf-a4-center';
            existingPages.parentNode?.insertBefore(centerWrap, existingPages);
            centerWrap.appendChild(existingPages);
        }
        scheduleA4Pagination();
        ensureA4EditorStyles(doc);
        const editRoot = doc.body.querySelector('.a4-pages') || doc.body.querySelector('.a4-page') || doc.body;
        (editRoot as HTMLElement).contentEditable = 'true';
        (editRoot as HTMLElement).style.outline = 'none';
        doc.body.style.outline = 'none';
        doc.body.style.padding = '0';
        const focusBodyOnMouseDown = (e: MouseEvent) => {
            const target = (e.target as Node);
            const root = doc.body.querySelector('.a4-pages') || doc.body.querySelector('.a4-page') || doc.body;
            if (root && root.contains(target)) (root as HTMLElement).focus();
        };
        doc.body.addEventListener('mousedown', focusBodyOnMouseDown);
        const captureContent = () => {
            if (paginatingIframeRef.current) return;
            contentFromIframeRef.current = true;
            const html = doc.body.innerHTML;
            setContent(html);
            onContentChangeRef.current?.(html);
        };
        doc.body.addEventListener('input', captureContent);
        const handleMouseUp = () => {
            const fn = onInsertSelectionRef.current;
            if (!fn) return;
            try {
                const sel = doc.getSelection();
                if (!sel || sel.isCollapsed) return;
                const r = sel.getRangeAt(0);
                const text = r.toString().trim();
                if (!text) return;
                const root = (doc.body.querySelector('.a4-pages') || doc.body.querySelector('.a4-page') || doc.body) as Node;
                if (!root.contains(r.startContainer) || !root.contains(r.endContainer)) return;
                const startRange = doc.createRange();
                startRange.selectNodeContents(root);
                startRange.setEnd(r.startContainer, r.startOffset);
                const start = startRange.toString().length;
                const end = start + text.length;
                const overlapsExisting = (insertedSelectionsRef.current || []).some(
                    (s) => s.documentId === 'editor' && start < s.end && end > s.start
                );
                if (overlapsExisting) return;
                fn(text, { start, end, documentId: 'editor' });
            } catch {
                // cross-origin or no selection
            }
        };
        let selectionChangeTimeoutId: ReturnType<typeof setTimeout> | null = null;
        const handleSelectionChange = () => {
            if (selectionChangeTimeoutId) clearTimeout(selectionChangeTimeoutId);
            selectionChangeTimeoutId = setTimeout(() => {
                selectionChangeTimeoutId = null;
                updateSelectionFormat();
            }, 50);
        };
        doc.body.addEventListener('mouseup', handleMouseUp);
        doc.addEventListener('selectionchange', handleSelectionChange);

        return () => {
            if (selectionChangeTimeoutId) clearTimeout(selectionChangeTimeoutId);
            doc.body.removeEventListener('mousedown', focusBodyOnMouseDown);
            doc.body.removeEventListener('input', captureContent);
            doc.body.removeEventListener('mouseup', handleMouseUp);
            doc.removeEventListener('selectionchange', handleSelectionChange);
        };
    }, [content, isOpen, updateSelectionFormat]);

    // ::selection color for next selection (same as DocumentViewer HtmlDocumentIframe)
    useEffect(() => {
        if (!iframeRef.current || !content || !isOpen) return;
        const iframeDoc = iframeRef.current.contentDocument;
        if (!iframeDoc?.head) return;
        let styleEl = iframeDoc.querySelector('style[data-selection-colors]');
        if (!styleEl) {
            styleEl = iframeDoc.createElement('style');
            styleEl.setAttribute('data-selection-colors', '1');
            iframeDoc.head.appendChild(styleEl);
        }
        const colors = INSERTION_HIGHLIGHT_COLORS[(insertedSelectionsCount ?? 0) % INSERTION_HIGHLIGHT_COLORS.length];
        styleEl.textContent = `*::selection { background-color: ${colors.bg} !important; color: ${colors.text} !important; }`;
    }, [content, isOpen, insertedSelectionsCount]);

    // Apply highlights when insertedSelections or content changes – do NOT rewrite iframe (same as DocumentViewer)
    // Run always (even when empty) to clear highlights on deletion. Delay only when content changed (pagination).
    const prevContentForHighlightsRef = useRef<string | null>(null);
    useEffect(() => {
        if (!iframeRef.current || !content || !isOpen) return;
        const doc = iframeRef.current.contentDocument;
        if (!doc?.body) return;
        const root = doc.body.querySelector('.a4-pages') || doc.body.querySelector('.a4-page') || doc.body;
        if (!root) return;
        const rangesForEditor = (insertedSelections || [])
            .map((s, i) => ({ start: s.start, end: s.end, colorIndex: i }))
            .filter((_, i) => (insertedSelections?.[i]?.documentId ?? '') === 'editor');
        const contentChanged = prevContentForHighlightsRef.current !== content;
        prevContentForHighlightsRef.current = content;
        const apply = () => {
            const bodyEl = root as HTMLElement;
            const segments = buildHighlightSegments((bodyEl.textContent || '').length, rangesForEditor);
            injectHighlightsInBody(bodyEl, segments, doc);
        };
        const delay = contentChanged ? 100 : 0;
        const id = setTimeout(() => requestAnimationFrame(apply), delay);
        return () => clearTimeout(id);
    }, [insertedSelections, content, isOpen]);

    const loadDocument = async () => {
        setIsLoading(true);
        setError(null);
        try {
            const base = getApiBase();
            const pathForUrl = (filePath || '').replace(/\\/g, '/');
            const url = isOfficePath(filePath)
                ? `${base}/api/file/as-html?path=${encodeURIComponent(pathForUrl)}`
                : `${base}/api/file?path=${encodeURIComponent(pathForUrl)}`;
            const response = await fetch(url);
            let text = await response.text();
            if (!response.ok) {
                const detail = text || response.statusText || 'Failed to load document';
                throw new Error(detail);
            }
            if (isMarkdownPath(filePath)) {
                // Render Markdown (research/document reports) instead of showing raw
                // source text; saving converts the edited HTML back to Markdown on
                // the backend (/api/file/save).
                const { marked } = await import('marked');
                text = await marked.parse(text, { gfm: true, breaks: false });
            }
            setContent(text);
            onContentChangeRef.current?.(text);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to load document');
        } finally {
            setIsLoading(false);
        }
    };

    const saveDocument = async () => {
        setIsSaving(true);
        setError(null);
        setSaveMessage(null);
        try {
            const base = getApiBase();
            const endpoint = isDocxPath(filePath)
                ? '/api/file/save-docx'
                : isXlsxPath(filePath)
                    ? '/api/file/save-xlsx'
                    : isPptxPath(filePath)
                        ? '/api/file/save-pptx'
                        : '/api/file/save';
            const response = await fetch(`${base}${endpoint}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: filePath, content }),
            });
            if (!response.ok) throw new Error('Failed to save document');
            setSaveMessage(filePath || 'Gespeichert.');
            setTimeout(() => setSaveMessage(null), 5000);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to save document');
        } finally {
            setIsSaving(false);
        }
    };

    // Native bridges exposed by the desktop window (QtWebEngine). Undefined in a
    // real browser, where the iframe's own print dialog / html2pdf are used.
    const getDesktopApi = () => (window as unknown as {
        pywebview?: { api?: {
            render_pdf?: (html: string, name: string, mode: string) => Promise<unknown>;
            save_text_as?: (content: string, name: string) => Promise<unknown>;
        } };
    }).pywebview?.api;
    const printBaseName = () => (displayFile || 'document').replace(/\.[^.]+$/, '') || 'document';
    // Full rendered HTML of the editor iframe (head A4 styles + paginated body) —
    // the desktop bridge renders this off-screen and prints it to PDF.
    const editorPrintHtml = () => iframeRef.current?.contentDocument?.documentElement?.outerHTML || '';

    const openPrintDialog = async () => {
        const iframe = iframeRef.current;
        if (!iframe?.contentWindow) return;
        // Desktop: render the A4 document to PDF and open it in the system viewer,
        // whose print dialog targets any printer or saves as PDF (iframe.print()
        // does not reliably reach QtWebEngine's print signal). Browser: native dialog.
        const api = getDesktopApi();
        if (api?.render_pdf) {
            await api.render_pdf(editorPrintHtml(), printBaseName(), 'print');
            return;
        }
        iframe.contentWindow.focus();
        iframe.contentWindow.print();
    };

    const [isExportingPdf, setIsExportingPdf] = useState(false);
    const exportAsPDF = async () => {
        const iframe = iframeRef.current;
        // Desktop: native save-as-PDF via QtWebEngine printToPdf (faithful A4,
        // unlike html2canvas which drops styles inside the iframe).
        const api = getDesktopApi();
        if (api?.render_pdf && iframe?.contentDocument) {
            await api.render_pdf(editorPrintHtml(), printBaseName(), 'pdf');
            return;
        }
        const body = iframe?.contentDocument?.body;
        if (!body) return;
        setIsExportingPdf(true);
        try {
            // Clone content into main document so html2canvas preserves fonts and formatting
            // (rendering from iframe body often drops styles in html2canvas)
            const wrapper = document.createElement('div');
            wrapper.style.position = 'absolute';
            wrapper.style.left = '-9999px';
            wrapper.style.top = '0';
            wrapper.style.width = '210mm';
            wrapper.style.background = 'white';
            wrapper.style.padding = '25mm';
            wrapper.style.boxSizing = 'border-box';
            const style = document.createElement('style');
            style.textContent = `
                .pdf-export-page { width: 210mm; min-height: 297mm; background: white; padding: 0; box-sizing: border-box; }
                .pdf-export-page b, .pdf-export-page strong { font-weight: bold; }
                .pdf-export-page i, .pdf-export-page em { font-style: italic; }
                .pdf-export-page u { text-decoration: underline; }
            `;
            wrapper.appendChild(style);
            const contentDiv = document.createElement('div');
            contentDiv.className = 'pdf-export-page';
            contentDiv.innerHTML = body.innerHTML;
            wrapper.appendChild(contentDiv);
            document.body.appendChild(wrapper);
            try {
                const html2pdf = (await import('html2pdf.js')).default;
                const baseName = (displayFile || 'document').replace(/\.[^.]+$/, '') || 'document';
                const filename = `${baseName}.pdf`;
                await html2pdf().set({
                    margin: 10,
                    filename,
                    image: { type: 'jpeg', quality: 0.98 },
                    html2canvas: {
                        scale: 2,
                        useCss: true,
                        letterRendering: true,
                        logging: false,
                    },
                }).from(wrapper).save();
            } finally {
                wrapper.remove();
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : 'PDF-Export fehlgeschlagen');
        } finally {
            setIsExportingPdf(false);
        }
    };

    const downloadHTML = async () => {
        // Desktop: native Save-As dialog writes the current content to disk.
        const api = getDesktopApi();
        if (api?.save_text_as) {
            await api.save_text_as(content, displayFile);
            return;
        }
        const blob = new Blob([content], { type: 'text/html' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = displayFile;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    };

    const saveSelectionFromIframe = useCallback(() => {
        const doc = iframeRef.current?.contentDocument;
        if (!doc) return;
        const sel = doc.getSelection();
        if (!sel || sel.rangeCount === 0) return;
        const range = sel.getRangeAt(0).cloneRange();
        savedSelectionRef.current = { doc, range };
    }, []);

    const execEditorCommand = useCallback((command: string, value?: string) => {
        const doc = iframeRef.current?.contentDocument;
        const win = iframeRef.current?.contentWindow;
        if (!doc || !win) return;
        doc.body.focus();
        const sel = doc.getSelection();
        if (sel && savedSelectionRef.current?.doc === doc) {
            sel.removeAllRanges();
            sel.addRange(savedSelectionRef.current.range);
        }
        try {
            doc.execCommand(command, false, value ?? '');
            contentFromIframeRef.current = true;
            const html = doc.body.innerHTML;
            setContent(html);
            onContentChangeRef.current?.(html);
            setTimeout(() => updateSelectionFormat(), 0);
        } finally {
            savedSelectionRef.current = null;
        }
    }, [updateSelectionFormat]);

    const toolbar = (
        <div
            className="flex flex-wrap items-center gap-0.5 border-b border-gray-200 bg-gray-50 px-2 py-1 shrink-0"
            onMouseDown={saveSelectionFromIframe}
        >
            <button type="button" onClick={() => execEditorCommand('bold')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.bold && "bg-gray-300")} title="Fett"><Bold size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('italic')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.italic && "bg-gray-300")} title="Kursiv"><Italic size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('underline')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.underline && "bg-gray-300")} title="Unterstrichen"><Underline size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[4rem]"
                value={selectionFormat.fontSize}
                onChange={(e) => execEditorCommand('fontSize', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
                title="Schriftgröße"
            >
                {FONT_SIZES.map(([val, label]) => <option key={val} value={val}>{label}</option>)}
            </select>
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[7rem]"
                value={selectionFormat.fontName}
                onChange={(e) => execEditorCommand('fontName', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
                title="Schriftart"
            >
                {FONT_FAMILIES.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <input
                type="color"
                className="w-7 h-7 cursor-pointer border border-gray-300 rounded p-0.5 bg-white"
                value={selectionFormat.foreColor}
                onMouseDown={saveSelectionFromIframe}
                onChange={(e) => execEditorCommand('foreColor', e.target.value)}
                title="Schriftfarbe"
            />
            <button type="button" onClick={() => execEditorCommand('backColor', '#ffff00')} className="p-1.5 rounded hover:bg-gray-200" title="Markieren"><Highlighter size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('justifyLeft')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyLeft && "bg-gray-300")} title="Links"><AlignLeft size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyCenter')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyCenter && "bg-gray-300")} title="Zentriert"><AlignCenter size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyRight')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyRight && "bg-gray-300")} title="Rechts"><AlignRight size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('insertUnorderedList')} className="p-1.5 rounded hover:bg-gray-200" title="Aufzählung"><List size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('insertOrderedList')} className="p-1.5 rounded hover:bg-gray-200" title="Nummerierung"><ListOrdered size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('removeFormat')} className="p-1.5 rounded hover:bg-gray-200" title="Formatierung entfernen"><Eraser size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={saveDocument} disabled={isSaving} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100 disabled:opacity-50" title="Speichern">
                {isSaving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                Save
            </button>
            <button type="button" onClick={openPrintDialog} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100" title="Drucken (Browser-Dialog)">
                <Printer size={14} />
                Print
            </button>
            <button type="button" onClick={exportAsPDF} disabled={isExportingPdf} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 disabled:opacity-50" title="Als PDF-Datei herunterladen">
                {isExportingPdf ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                PDF
            </button>
            <button type="button" onClick={downloadHTML} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100" title="Als HTML herunterladen">
                <Download size={14} />
                Download
            </button>
            {saveMessage && (
                <span className="text-[10px] text-emerald-600 font-mono ml-1 truncate max-w-[200px]" title={saveMessage}>Gespeichert: {saveMessage}</span>
            )}
        </div>
    );

    if (!isOpen && mode === 'overlay') return null;

    // Dock mode – attachments (no right-side list) or kernel editor
    if (mode === 'dock') {
        if (isAttachmentsMode) {
            const hasDocuments = documents.length > 0;
            return (
                <div
                    className={cn(
                        'relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out',
                        isOpen ? 'translate-x-0 opacity-100' : 'translate-x-8 opacity-0 pointer-events-none'
                    )}
                    aria-hidden={!isOpen}
                >
                    <div className="flex h-full w-full flex-col min-w-0 bg-[#F9FAFB] rounded-2xl overflow-hidden">
                        <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4 shrink-0">
                            <div className="flex items-center gap-3 min-w-0">
                                <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600 shrink-0">
                                    <FileText size={14} />
                                </div>
                                <div className="min-w-0">
                                    <div className="text-xs font-semibold text-gray-900 truncate">{title}</div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                        <span className="h-1.5 w-1.5 rounded-full bg-gray-400 shrink-0" />
                                        <span className="uppercase">Ready</span>
                                    </div>
                                </div>
                                <div className="relative flex items-center gap-1">
                                    <span className="text-[10px] text-gray-500 uppercase tracking-wide shrink-0">Anhänge</span>
                                    <button
                                        type="button"
                                        onClick={() => setAttachmentsDropdownOpen((o) => !o)}
                                        className="flex items-center gap-1 rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 min-w-0 max-w-[180px]"
                                    >
                                        <span className="truncate">{selectedAttachment?.name ?? 'Kein Dokument'}</span>
                                        <ChevronDown size={12} className="shrink-0" />
                                    </button>
                                    {attachmentsDropdownOpen && (
                                        <>
                                            <div className="fixed inset-0 z-10" aria-hidden onClick={() => setAttachmentsDropdownOpen(false)} />
                                            <div className="absolute left-0 top-full mt-1 z-20 w-56 max-h-48 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg py-1">
                                                <button
                                                    type="button"
                                                    onClick={() => { fileInputRef.current?.click(); setAttachmentsDropdownOpen(false); }}
                                                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-gray-700 hover:bg-blue-50"
                                                >
                                                    <Plus size={14} />
                                                    Dokument hinzufügen
                                                </button>
                                                <input
                                                    ref={fileInputRef}
                                                    type="file"
                                                    multiple
                                                    accept={FILE_ACCEPT}
                                                    className="hidden"
                                                    onChange={handleAttachmentFileChange}
                                                />
                                                {documents.map((doc) => (
                                                    <div
                                                        key={doc.id}
                                                        className={cn(
                                                            'flex items-center gap-2 px-3 py-2 group',
                                                            selectedAttachment?.id === doc.id ? 'bg-blue-50' : 'hover:bg-gray-50'
                                                        )}
                                                    >
                                                        <button
                                                            type="button"
                                                            onClick={() => { setSelectedAttachmentId(doc.id); setAttachmentsDropdownOpen(false); }}
                                                            className="flex-1 min-w-0 text-left text-xs truncate text-gray-900"
                                                        >
                                                            {doc.name}
                                                        </button>
                                                        {onRemoveDocument && (
                                                            <button
                                                                type="button"
                                                                onClick={(e) => { e.stopPropagation(); onRemoveDocument(doc.id); if (selectedAttachment?.id === doc.id) setSelectedAttachmentId(documents.filter((d) => d.id !== doc.id)[0]?.id ?? null); }}
                                                                className="rounded p-1 text-gray-400 opacity-0 group-hover:opacity-100 hover:bg-red-50 hover:text-red-600"
                                                                aria-label={`Remove ${doc.name}`}
                                                            >
                                                                <Trash2 size={12} />
                                                            </button>
                                                        )}
                                                    </div>
                                                ))}
                                            </div>
                                        </>
                                    )}
                                </div>
                            </div>
                            <button
                                onClick={handleRequestClose}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600 shrink-0"
                                aria-label="Close"
                            >
                                <X size={14} />
                            </button>
                        </div>
                        <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500 shrink-0">
                            <span className="truncate font-mono text-[11px]">{selectedAttachment?.name ?? 'Kein Dokument ausgewählt'}</span>
                        </div>
                        <div className="flex-1 min-h-0 min-w-0 overflow-hidden p-4 flex flex-col">
                            <div className="flex flex-1 min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-200 bg-white">
                                <div
                                    ref={attachmentsContentRef}
                                    onMouseUp={handleAttachmentsContentMouseUp}
                                    className="flex-1 min-h-0 min-w-0 overflow-auto bg-white p-4 w-full"
                                >
                                    {!hasDocuments ? (
                                        <div className="flex flex-col items-center justify-center h-full gap-2 text-gray-400 text-sm">
                                            <FileText size={32} className="opacity-50" />
                                            <p>Keine Dokumente. Klicke auf &quot;Dokument hinzufügen&quot;, um Anhänge zu öffnen.</p>
                                            <p className="text-xs">Der Assistent kann dann auf deren Inhalt antworten.</p>
                                        </div>
                                    ) : !selectedAttachment ? (
                                        <div className="text-gray-400 text-sm">Dokument auswählen.</div>
                                    ) : isAttachmentImage && selectedAttachment.content?.startsWith('data:') ? (
                                        <img src={selectedAttachment.content} alt={selectedAttachment.name} className="max-w-full h-auto" />
                                    ) : isPdfWithData(selectedAttachment) ? (
                                        <PdfWithHighlights
                                            src={pdfDataUrl(selectedAttachment)}
                                            title={selectedAttachment.name}
                                            className="w-full min-h-[500px] flex-1"
                                            insertedSelections={insertedSelections}
                                            nextSelectionColorIndex={insertedSelections?.length ?? 0}
                                            documentId={selectedAttachment.id}
                                            content={selectedAttachment.content}
                                            onInsertSelection={onInsertSelection}
                                        />
                                    ) : (
                                        <pre
                                            className={cn(
                                                'w-full max-w-full whitespace-pre-wrap break-words font-sans text-sm text-gray-800',
                                                INSERTION_COLOR_CLASSES[insertedSelectionsCount % INSERTION_COLOR_CLASSES.length]
                                            )}
                                        >
                                            {attachmentContentWithHighlights ?? (attachmentDisplayContent || '(Kein Textinhalt)')}
                                        </pre>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            );
        }

        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full min-w-0">
                    {/* Workflow panel (left side) - only shown if steps exist */}
                    {hasWorkflow && (
                        <div className="flex w-[36%] min-w-[280px] flex-col border-r border-gray-200 bg-white">
                            <div className="flex h-12 items-center justify-between border-b border-gray-100 px-4">
                                <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Document Steps</span>
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
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Main content panel (right side) - same dimensions as DocumentViewer */}
                    <div className={cn("flex flex-1 flex-col min-w-0 w-0 bg-[#F9FAFB] overflow-hidden", !hasWorkflow ? "rounded-2xl" : "rounded-r-2xl")}>
                        {/* Header: icon, filename (black) + path (gray), status, buttons */}
                        <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4 gap-2 min-w-0">
                            <div className="flex items-center gap-3 min-w-0 flex-1">
                                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600">
                                    <FileText size={14} />
                                </div>
                                <div className="min-w-0 flex-1 flex flex-col">
                                    <div className="flex items-center gap-2 text-xs min-w-0">
                                        <span className="font-semibold text-gray-900 shrink-0">{displayFile || title}</span>
                                        <span className="text-gray-400 truncate font-mono text-[11px]" title={filePath || undefined}>{filePath || 'No file selected'}</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                        <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", presenceTone)} />
                                        {status ? (
                                            <span className="text-gray-500">{status}</span>
                                        ) : (
                                            <span className="uppercase">{presenceLabel}</span>
                                        )}
                                    </div>
                                </div>
                            </div>
                            <div className="flex items-center gap-2 shrink-0">
                                {isBinaryDocument && (
                                    <a
                                        href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                        download={displayFile}
                                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors"
                                    >
                                        <Download size={14} />
                                        Download
                                    </a>
                                )}
                                <button
                                    onClick={handleRequestClose}
                                    className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                    aria-label="Close"
                                >
                                    <X size={14} />
                                </button>
                            </div>
                        </div>

                        {/* Content area – same structure as DocumentViewer */}
                        <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                            {isBinaryDocument ? (
                                <div className="flex flex-1 flex-col items-center justify-center gap-4 p-6 text-center bg-white">
                                    <FileText size={48} className="text-gray-300" />
                                    <p className="text-sm text-gray-600 max-w-md">
                                        PDF-Dateien können hier nicht bearbeitet werden.
                                    </p>
                                    <p className="text-xs text-gray-500">
                                        Word (.docx), Excel (.xlsx), PowerPoint (.pptx) und HTML (.html, .htm) werden im Editor als HTML geladen und bearbeitet.
                                    </p>
                                    <p className="font-mono text-[11px] text-gray-400 break-all px-2">{filePath}</p>
                                    <a
                                        href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                        download={displayFile}
                                        className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors"
                                    >
                                        <Download size={16} />
                                        Datei herunterladen
                                    </a>
                                </div>
                            ) : isLoading ? (
                                <div className="flex flex-1 items-center justify-center gap-2 text-gray-300 bg-white">
                                    <Loader2 size={14} className="animate-spin opacity-50" />
                                    <span className="text-xs">Loading document...</span>
                                </div>
                            ) : error ? (
                                <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-white">
                                    <p className="text-red-500 text-xs">{error}</p>
                                    <button
                                        onClick={loadDocument}
                                        className="text-xs text-blue-500 hover:underline"
                                    >
                                        Try again
                                    </button>
                                </div>
                            ) : (
                                <>
                                    {toolbar}
                                    <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                                        <div className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#e5e7eb] w-full scrollbar-hide">
                                            <div className="min-h-full flex flex-col items-center py-4 px-2 gap-6">
                                                <div className="w-[210mm] max-w-full flex justify-center">
                                                    <div className="w-[210mm] max-w-full min-h-[297mm] bg-white shadow-sm box-border rounded-sm flex flex-col overflow-visible">
                                                        <iframe
                                                            ref={iframeRef}
                                                            className="w-full min-h-[297mm] border-0 block"
                                                            title="Document Editor"
                                                            sandbox="allow-same-origin allow-scripts allow-modals"
                                                        />
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </>
                            )}
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    // Overlay mode - same layout as SubAgentWindow overlay mode
    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl min-w-0">
                {/* Workflow panel (left side) - only shown if steps exist */}
                {hasWorkflow && (
                    <div className="flex w-[35%] min-w-[320px] flex-col border-r border-gray-200 bg-white">
                        <div className="flex h-14 items-center justify-between border-b border-gray-100 px-5">
                            <span className="text-sm font-semibold text-gray-700">Document Steps</span>
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
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                )}

                {/* Main content panel (right side) - same as DocumentViewer overlay */}
                <div className="flex flex-1 flex-col min-w-0 w-0 bg-[#F9FAFB] overflow-hidden">
                    {/* Header */}
                    <div className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-6 gap-2 min-w-0">
                        <div className="flex items-center gap-3 min-w-0 flex-1">
                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-blue-500 text-white shadow-sm">
                                <FileText size={18} />
                            </div>
                            <div className="min-w-0 flex-1 flex flex-col">
                                <div className="flex items-center gap-2 text-sm min-w-0">
                                    <span className="font-semibold text-gray-900 shrink-0">{displayFile || title}</span>
                                    <span className="text-gray-400 truncate font-mono text-xs" title={filePath || undefined}>{filePath || 'No file selected'}</span>
                                </div>
                                <div className="flex items-center gap-2 text-xs text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", presenceTone)} />
                                    {status ? (
                                        <span className="text-gray-500">{status}</span>
                                    ) : (
                                        <span className="uppercase">{presenceLabel}</span>
                                    )}
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                            {isBinaryDocument && (
                                <a
                                    href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                    download={displayFile}
                                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors"
                                >
                                    <Download size={14} />
                                    Download
                                </a>
                            )}
                            <button
                                onClick={handleRequestClose}
                                className="rounded-full p-2 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Close"
                            >
                                <X size={16} />
                            </button>
                        </div>
                    </div>

                    {/* Content area – no padding, editor fills all space */}
                    <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
                        {isBinaryDocument ? (
                            <div className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center bg-white">
                                <FileText size={56} className="text-gray-300" />
                                <p className="text-sm text-gray-600 max-w-md">
                                    PDF-Dateien können hier nicht bearbeitet werden.
                                </p>
                                <p className="text-xs text-gray-500">
                                    Word (.docx), Excel (.xlsx), PowerPoint (.pptx) und HTML (.html, .htm) werden im Editor als HTML geladen und bearbeitet.
                                </p>
                                <p className="font-mono text-xs text-gray-400 break-all px-2">{filePath}</p>
                                <a
                                    href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                    download={displayFile}
                                    className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors"
                                >
                                    <Download size={18} />
                                    Datei herunterladen
                                </a>
                            </div>
                        ) : isLoading ? (
                            <div className="flex flex-1 items-center justify-center gap-2 text-gray-300 bg-white">
                                <Loader2 size={28} className="animate-spin opacity-50" />
                                <span className="text-xs">Loading document...</span>
                            </div>
                        ) : error ? (
                            <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-white">
                                <p className="text-red-500 text-sm">{error}</p>
                                <button
                                    onClick={loadDocument}
                                    className="text-sm text-blue-500 hover:underline"
                                >
                                    Try again
                                </button>
                            </div>
                        ) : (
                            <>
                                {toolbar}
                                <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                                    <div className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#e5e7eb] w-full scrollbar-hide">
                                        <div className="min-h-full flex flex-col items-center py-6 px-4 gap-6">
                                            <div className="w-[210mm] max-w-full flex justify-center">
                                                <div className="w-[210mm] max-w-full min-h-[297mm] bg-white shadow-sm box-border rounded-sm flex flex-col overflow-visible">
                                                    <iframe
                                                        ref={iframeRef}
                                                        className="w-full min-h-[297mm] border-0 block"
                                                        title="Document Editor"
                                                        sandbox="allow-same-origin allow-scripts allow-modals"
                                                    />
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
