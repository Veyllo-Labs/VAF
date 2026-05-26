'use client';

import React, { useCallback, useMemo, useState } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';
import { cn } from '@/lib/utils';

pdfjs.GlobalWorkerOptions.workerSrc = `//unpkg.com/pdfjs-dist@${pdfjs.version}/build/pdf.worker.min.mjs`;

const HIGHLIGHT_COLORS = [
    { bg: '#1f2937', text: '#ffffff' },
    { bg: '#f97316', text: '#ffffff' },
    { bg: '#ec4899', text: '#ffffff' },
    { bg: '#3b82f6', text: '#ffffff' },
    { bg: '#059669', text: '#ffffff' },
] as const;

export type SelectionHighlight = {
    text: string;
    colorIndex: number;
    /** PDF page number (1-based) for per-page matching; when set, only match within this page. */
    pageNumber?: number;
    /** Exact PDF text item indices to highlight (avoids text-search issues); when set, used directly instead of text matching. */
    itemIndices?: number[];
};

export type PdfWithHighlightsProps = {
    src: string;
    title?: string;
    className?: string;
    /** Selections to highlight - either passed directly or computed from insertedSelections. */
    selections?: SelectionHighlight[];
    /** When provided with documentId, selections are computed from this (avoids flicker on parent re-renders). */
    insertedSelections?: Array<{ text: string; documentId: string; pageNumber?: number; itemIndices?: number[] }>;
    /** Index for the next selection's color (used for ::selection during marking). */
    nextSelectionColorIndex?: number;
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string; pageNumber?: number; itemIndices?: number[] }) => void;
    documentId?: string;
    /** Extracted content - used to compute start/end when user selects text in PDF. */
    content?: string;
};

/** Normalize whitespace for matching (PDF often has different spacing). */
function normalizeForMatch(s: string): string {
    return s.replace(/\s+/g, ' ').trim();
}

/**
 * Build mapping from PDF text item (pageNum-itemIndex) to highlight colorIndex.
 * Matches selection.text against PDF text.
 */
function buildHighlightMap(
    pdfTextByPage: { pageNum: number; items: { str: string }[] }[],
    selections: SelectionHighlight[]
): Map<string, number> {
    const map = new Map<string, number>();
    if (!selections.length) return map;

    const allItems: { pageNum: number; itemIndex: number; str: string }[] = [];
    for (const page of pdfTextByPage) {
        for (let i = 0; i < page.items.length; i++) {
            allItems.push({
                pageNum: page.pageNum,
                itemIndex: i,
                str: page.items[i].str,
            });
        }
    }
    const fullText = allItems.map((it) => it.str).join('');
    if (!fullText) return map;

    for (const sel of selections) {
        if (sel.pageNumber != null && sel.itemIndices?.length) {
            for (const idx of sel.itemIndices) {
                map.set(`${sel.pageNumber}-${idx}`, sel.colorIndex);
            }
            continue;
        }
        const searchText = normalizeForMatch(sel.text);
        if (!searchText) continue;
        const toFind = sel.text.length > 0 ? sel.text : searchText;
        const len = toFind.length;
        if (sel.pageNumber) {
            const page = pdfTextByPage.find((p) => p.pageNum === sel.pageNumber);
            if (!page) continue;
            const pageText = page.items.map((i) => i.str).join('');
            let idx = pageText.indexOf(toFind, 0);
            let matchLen = toFind.length;
            if (idx < 0) {
                idx = pageText.indexOf(searchText, 0);
                matchLen = searchText.length;
            }
            if (idx >= 0) {
                const end = idx + matchLen;
                let offset = 0;
                for (let i = 0; i < page.items.length; i++) {
                    const it = page.items[i];
                    const itemStart = offset;
                    const itemEnd = offset + it.str.length;
                    if (itemEnd > idx && itemStart < end) {
                        map.set(`${page.pageNum}-${i}`, sel.colorIndex);
                    }
                    offset = itemEnd;
                }
            }
        } else {
            let searchFrom = 0;
            let idx = fullText.indexOf(toFind, searchFrom);
            let matchLen = toFind.length;
            if (idx < 0) {
                idx = fullText.indexOf(searchText, searchFrom);
                matchLen = searchText.length;
            }
            while (idx >= 0) {
                const end = idx + matchLen;
                let offset = 0;
                for (const it of allItems) {
                    const itemStart = offset;
                    const itemEnd = offset + it.str.length;
                    if (itemEnd > idx && itemStart < end) {
                        map.set(`${it.pageNum}-${it.itemIndex}`, sel.colorIndex);
                    }
                    offset = itemEnd;
                }
                searchFrom = idx + matchLen;
                idx = fullText.indexOf(toFind, searchFrom);
                matchLen = toFind.length;
                if (idx < 0) {
                    idx = fullText.indexOf(searchText, searchFrom);
                    matchLen = searchText.length;
                }
            }
        }
    }
    return map;
}

const PdfWithHighlightsInner = function PdfWithHighlights({
    src,
    title,
    className,
    selections: selectionsProp,
    insertedSelections,
    nextSelectionColorIndex,
    onInsertSelection,
    documentId,
    content,
}: PdfWithHighlightsProps) {
    const selections = useMemo(() => {
        if (insertedSelections?.length && documentId) {
            return insertedSelections
                .map((s, i) => ({ text: s.text, colorIndex: i, documentId: s.documentId, pageNumber: s.pageNumber, itemIndices: s.itemIndices }))
                .filter((s) => s.documentId === documentId)
                .map(({ text, colorIndex, pageNumber, itemIndices }) => ({ text, colorIndex, pageNumber, itemIndices }));
        }
        return selectionsProp ?? [];
    }, [insertedSelections, documentId, selectionsProp]);

    const [numPages, setNumPages] = useState<number>(0);
    const [pdfTextByPage, setPdfTextByPage] = useState<
        { pageNum: number; items: { str: string }[] }[]
    >([]);
    const containerRef = React.useRef<HTMLDivElement>(null);
    const [containerWidth, setContainerWidth] = useState(595);
    React.useEffect(() => {
        const el = containerRef.current;
        if (!el) return;
        const update = () => setContainerWidth(el.offsetWidth || 595);
        update();
        const ro = new ResizeObserver(update);
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    const onDocumentLoadSuccess = useCallback(
        async ({ numPages: n }: { numPages: number }) => {
            setNumPages(n);
            setPdfTextByPage([]);
        },
        []
    );

    const actualFile = useMemo(() => (src.startsWith('data:') ? src : { url: src }), [src]);

    const highlightMap = useMemo(() => {
        if (!pdfTextByPage.length) return new Map<string, number>();
        return buildHighlightMap(pdfTextByPage, selections);
    }, [pdfTextByPage, selections]);

    const customTextRenderer = useCallback(
        (args: { str: string; itemIndex: number; pageNumber: number }) => {
            const escaped = args.str
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
            const key = `${args.pageNumber}-${args.itemIndex}`;
            const colorIndex = highlightMap.get(key);
            const dataAttrs = `data-pdf-item="${args.itemIndex}" data-pdf-page="${args.pageNumber}"`;
            if (colorIndex == null) {
                return `<span ${dataAttrs}>${escaped}</span>`;
            }
            const colors = HIGHLIGHT_COLORS[colorIndex % HIGHLIGHT_COLORS.length];
            return `<span ${dataAttrs}><mark style="background-color:${colors.bg};color:${colors.text};border-radius:2px;padding:0 2px;">${escaped}</mark></span>`;
        },
        [highlightMap]
    );

    const onPageLoadSuccess = useCallback(
        async (page: Parameters<NonNullable<React.ComponentProps<typeof Page>['onLoadSuccess']>>[0]) => {
            try {
                const textContent = await page.getTextContent();
                const items = textContent.items.map((it) => ('str' in it ? (it.str as string) : ''));
                setPdfTextByPage((prev) => {
                    const filtered = prev.filter((p) => p.pageNum !== page.pageNumber);
                    return [...filtered, { pageNum: page.pageNumber, items: items.map((str) => ({ str })) }].sort(
                        (a, b) => a.pageNum - b.pageNum
                    );
                });
            } catch {
                // ignore
            }
        },
        []
    );

    const handleMouseUp = useCallback(() => {
        if (!onInsertSelection || !documentId) return;
        const sel = typeof window !== 'undefined' ? window.getSelection() : null;
        if (!sel || !sel.rangeCount) return;
        const text = sel.toString().trim();
        if (!text) return;
        const container = containerRef.current;
        if (!container) return;
        const anchorNode = sel.anchorNode;
        if (!anchorNode || !container.contains(anchorNode)) return;
        const range = sel.getRangeAt(0);
        let fragment: DocumentFragment;
        try {
            fragment = range.cloneContents();
        } catch {
            return;
        }
        const spansInSelection = fragment.querySelectorAll<HTMLElement>('[data-pdf-item][data-pdf-page]');
        const selectedIndicesByPage = new Map<number, number[]>();
        const normSel = text.normalize('NFC');
        for (const el of spansInSelection) {
            const spanText = (el.textContent ?? '').trim();
            if (!spanText || !normSel.includes(spanText.normalize('NFC'))) continue;
            const pageNum = parseInt(el.getAttribute('data-pdf-page') ?? '', 10);
            const itemIdx = parseInt(el.getAttribute('data-pdf-item') ?? '', 10);
            if (isNaN(pageNum) || isNaN(itemIdx)) continue;
            const arr = selectedIndicesByPage.get(pageNum) ?? [];
            if (!arr.includes(itemIdx)) arr.push(itemIdx);
            selectedIndicesByPage.set(pageNum, arr);
        }
        let pageNum: number | undefined;
        let itemIndices: number[] = [];
        if (selectedIndicesByPage.size === 1) {
            const [p, indices] = Array.from(selectedIndicesByPage.entries())[0];
            pageNum = p;
            itemIndices = indices.sort((a, b) => a - b);
        }
        const idx = content ? content.indexOf(text) : -1;
        const start = idx >= 0 ? idx : 0;
        const end = idx >= 0 ? idx + text.length : text.length;
        onInsertSelection(text, { start, end, documentId, pageNumber: pageNum, itemIndices: itemIndices.length ? itemIndices : undefined });
        sel.removeAllRanges();
    }, [onInsertSelection, documentId, content]);

    const nextColorIndex = nextSelectionColorIndex ?? selections.length;
    const nextSelectionColor = HIGHLIGHT_COLORS[nextColorIndex % HIGHLIGHT_COLORS.length];

    return (
        <div
            ref={containerRef}
            onMouseUp={handleMouseUp}
            className={cn('flex flex-col overflow-auto bg-[#d1d5db]', className)}
            data-pdf-highlights
        >
            <style>{`
                [data-pdf-highlights] .textLayer { user-select: text !important; }
                [data-pdf-highlights] .textLayer span { cursor: text; }
                [data-pdf-highlights] .textLayer ::selection {
                    background-color: ${nextSelectionColor.bg} !important;
                    color: ${nextSelectionColor.text} !important;
                }
            `}</style>
            <Document
                file={actualFile}
                onLoadSuccess={onDocumentLoadSuccess}
                loading={
                    <div className="flex items-center justify-center py-12 text-gray-500 text-sm">PDF wird geladen…</div>
                }
                error={
                    <div className="flex items-center justify-center py-12 text-red-500 text-sm">
                        PDF konnte nicht geladen werden.
                    </div>
                }
            >
                {Array.from({ length: numPages }, (_, i) => i + 1).map((pageNum) => (
                    <div key={pageNum} data-pdf-page-container={pageNum} className="mb-10 flex flex-col items-center">
                        <div className="mb-2 self-start text-xs font-medium text-gray-500">
                            Seite {pageNum} von {numPages}
                        </div>
                        <div className="rounded-sm bg-white shadow-md overflow-hidden">
                            <Page
                                pageNumber={pageNum}
                                width={containerWidth}
                                renderTextLayer={true}
                                customTextRenderer={(args) => customTextRenderer(args)}
                                onLoadSuccess={onPageLoadSuccess}
                            />
                        </div>
                    </div>
                ))}
            </Document>
        </div>
    );
};

export default React.memo(PdfWithHighlightsInner, (prev, next) => {
    if (prev.documentId !== next.documentId) return false;
    if (prev.src !== next.src) return false;
    if (prev.content !== next.content) return false;
    if ((prev.nextSelectionColorIndex ?? 0) !== (next.nextSelectionColorIndex ?? 0)) return false;
    const pa = prev.insertedSelections ?? prev.selections ?? [];
    const na = next.insertedSelections ?? next.selections ?? [];
    if (pa.length !== na.length) return false;
    for (let i = 0; i < pa.length; i++) {
        const p = pa[i], n = na[i];
        if (p?.text !== n?.text || p?.pageNumber !== n?.pageNumber) return false;
        const pi = p?.itemIndices ?? [], ni = n?.itemIndices ?? [];
        if (pi.length !== ni.length) return false;
        for (let j = 0; j < pi.length; j++) if (pi[j] !== ni[j]) return false;
    }
    return true;
});
