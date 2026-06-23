'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlignCenter, AlignLeft, AlignRight, Bold, ChevronDown, ChevronUp, Download, FileText,
  Italic, List, ListOrdered, Loader2, MessageSquare, Plus, Printer, Save, Trash2, Type, Underline, X,
} from 'lucide-react';

import { cn, getApiBase } from '@/lib/utils';
import {
  NativeDocxBlock,
  NativeDocxDocument,
  NativeDocxImage,
  NativeDocxParagraph,
  NativeDocxRun,
  NativeDocxSection,
  NativeDocxTable,
  NativeDocxBlockRange,
  collectBlockRanges,
  cloneNativeDocx,
  createEmptyNativeDocx,
  flattenNativeDocxText,
  flattenParagraphText,
} from '@/lib/docxNative';

type SelectionRangePayload = { start: number; end: number; documentId: string };

type EditorInsertedSelection = {
  start: number;
  end: number;
  documentId: string;
};

type NativeDocxEditorProps = {
  isOpen?: boolean;
  onClose?: () => void;
  canClose?: boolean;
  filePath: string;
  title: string;
  initialModel?: NativeDocxDocument | null;
  onModelChange?: (model: NativeDocxDocument) => void;
  onContentChange?: (content: string) => void;
  onInsertSelection?: (text: string, range: SelectionRangePayload) => void;
  insertedSelections?: EditorInsertedSelection[];
};

const MARK_COLORS = [
  { border: 'border-l-gray-700', bg: 'bg-gray-700/5', dot: 'bg-gray-700' },
  { border: 'border-l-orange-500', bg: 'bg-orange-500/5', dot: 'bg-orange-500' },
  { border: 'border-l-pink-500', bg: 'bg-pink-500/5', dot: 'bg-pink-500' },
  { border: 'border-l-blue-500', bg: 'bg-blue-500/5', dot: 'bg-blue-500' },
  { border: 'border-l-emerald-500', bg: 'bg-emerald-500/5', dot: 'bg-emerald-500' },
];

type PageSlice = {
  sectionIndex: number;
  section: NativeDocxSection;
  pageIndexInSection: number;
  totalPagesInSection: number;
  globalPageNumber: number;
  blocks: Array<{
    key: string;
    block: NativeDocxBlock;
    originalIndex: number;
    sliceIndex: number | null;
    startOffset: number | null;
    endOffset: number | null;
  }>;
};

type ParagraphPaginationSlice = {
  sliceIndex: number;
  startOffset: number;
  endOffset: number;
  text: string;
  paragraph: NativeDocxParagraph;
};

const MM_TO_PX = 96 / 25.4;
const PT_TO_PX = 96 / 72;
const A4_WIDTH_MM = 210;
const A4_HEIGHT_MM = 297;
// Real per-block chrome measured/rendered: InlineEditableBlock has px-1 py-0.5 (2px vertical padding)
// + border-2 (2px) on top and bottom = 8px. measureRenderedBlockHeight wraps blocks with the SAME chrome
// (see :measureRenderedBlockHeight). Keep this in sync with that wrapper, or pagination drifts.
const BLOCK_VERTICAL_CHROME_PX = 8;
const PAGE_FIT_SAFETY_PX = 6;

function twipsToMm(value: number | null | undefined, fallback: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return fallback;
  return value / 56.692913;
}

function sectionPageLayout(section: NativeDocxSection) {
  const pageWidthMm = A4_WIDTH_MM;
  const pageHeightMm = A4_HEIGHT_MM;
  const marginTopMm = Math.min(35, Math.max(12, twipsToMm(section.properties.margin_top_twips, 20)));
  const marginBottomMm = Math.min(35, Math.max(12, twipsToMm(section.properties.margin_bottom_twips, 20)));
  const marginLeftMm = Math.min(30, Math.max(12, twipsToMm(section.properties.margin_left_twips, 20)));
  const marginRightMm = Math.min(30, Math.max(12, twipsToMm(section.properties.margin_right_twips, 20)));
  const contentWidthMm = Math.max(80, pageWidthMm - marginLeftMm - marginRightMm);
  const contentHeightPx = Math.max(220, (pageHeightMm - marginTopMm - marginBottomMm) * MM_TO_PX - 24);
  return { pageWidthMm, pageHeightMm, marginTopMm, marginBottomMm, marginLeftMm, marginRightMm, contentWidthMm, contentHeightPx };
}

function paragraphCharsPerLine(contentWidthMm: number): number {
  return Math.max(18, Math.floor(contentWidthMm * 2.3));
}

function splitLineForPagination(text: string, maxChars: number): string[] {
  if (text.length <= maxChars) return [text];
  const parts: string[] = [];
  let start = 0;

  while (start < text.length) {
    const remaining = text.length - start;
    if (remaining <= maxChars) {
      parts.push(text.slice(start));
      break;
    }

    const tentativeEnd = start + maxChars;
    let splitAt = -1;
    for (let i = tentativeEnd; i > start; i -= 1) {
      if (/\s/.test(text[i])) {
        splitAt = i + 1;
        break;
      }
    }

    if (splitAt <= start) splitAt = tentativeEnd;
    parts.push(text.slice(start, splitAt));
    start = splitAt;
  }

  return parts.length > 0 ? parts : [''];
}

function splitParagraphForPagination(paragraph: NativeDocxParagraph, contentWidthMm: number): ParagraphPaginationSlice[] {
  const text = flattenParagraphText(paragraph);
  const lines = text.split(/\r?\n/);
  const templateRun = paragraph.runs[0] ?? {
    id: `${paragraph.id}-run-0`, text: '', bold: false, italic: false, underline: false, font_name: 'Arial', font_size_pt: 11, color: '', highlight: '',
  };
  const charsPerLine = paragraphCharsPerLine(contentWidthMm);
  const slices: ParagraphPaginationSlice[] = [];
  let offset = 0;

  lines.forEach((line, lineIndex) => {
    const wrapped = splitLineForPagination(line, charsPerLine);
    wrapped.forEach((part) => {
      const sliceIndex = slices.length;
      const startOffset = offset;
      const endOffset = startOffset + part.length;
      slices.push({
        sliceIndex,
        startOffset,
        endOffset,
        text: part,
        paragraph: {
          ...paragraph,
          id: `${paragraph.id}-slice-${sliceIndex}`,
          page_break_before: sliceIndex === 0 ? paragraph.page_break_before : false,
          runs: [{ ...templateRun, id: `${templateRun.id}-slice-${sliceIndex}`, text: part }],
        },
      });
      offset = endOffset;
    });
    if (lineIndex < lines.length - 1) offset += 1;
  });

  return slices.length > 0 ? slices : [{
    sliceIndex: 0,
    startOffset: 0,
    endOffset: 0,
    text: '',
    paragraph: {
      ...paragraph,
      id: `${paragraph.id}-slice-0`,
      runs: [{ ...templateRun, id: `${templateRun.id}-slice-0`, text: '' }],
    },
  }];
}

function estimateBlockHeight(block: NativeDocxBlock, contentWidthMm: number): number {
  if (block.type === 'paragraph') {
    // Mirror the rendered metrics (shared paragraphVisualStyle): real font-size / line-height + the block's
    // own top+bottom margin + the 8px wrapper chrome. The measured path is preferred in the browser; this
    // DOM-free estimate only needs to be close enough not to grossly over- or under-fill before measurement.
    const v = paragraphVisualStyle(block);
    const fontSizePt = parseFloat(String(v.style.fontSize)) || 11;
    const lineHeightPx = fontSizePt * PT_TO_PX * (typeof v.style.lineHeight === 'number' ? v.style.lineHeight : 1.4);
    const text = flattenParagraphText(block);
    const charsPerLine = paragraphCharsPerLine(contentWidthMm);
    const logicalLines = Math.max(1, Math.ceil(Math.max(text.length, 1) / charsPerLine));
    const m = paragraphMarginTopBottomPx(block);
    return Math.ceil(logicalLines * lineHeightPx + m.top + m.bottom + BLOCK_VERTICAL_CHROME_PX);
  }
  if (block.type === 'table') return Math.max(64, block.rows.length * 54) + BLOCK_VERTICAL_CHROME_PX;
  if (block.type === 'image') return Math.max(120, Math.min(256, block.height_px ?? 180)) + BLOCK_VERTICAL_CHROME_PX;
  if (block.type === 'unsupported') return 42 + BLOCK_VERTICAL_CHROME_PX;
  return 0;
}

function createParagraphSlice(
  paragraph: NativeDocxParagraph,
  fullText: string,
  startOffset: number,
  endOffset: number,
  sliceIndex: number
): ParagraphPaginationSlice {
  const templateRun = paragraph.runs[0] ?? {
    id: `${paragraph.id}-run-0`, text: '', bold: false, italic: false, underline: false, font_name: 'Arial', font_size_pt: 11, color: '', highlight: '',
  };
  const text = fullText.slice(startOffset, endOffset);
  return {
    sliceIndex,
    startOffset,
    endOffset,
    text,
    paragraph: {
      ...paragraph,
      id: `${paragraph.id}-slice-${sliceIndex}`,
      page_break_before: sliceIndex === 0 ? paragraph.page_break_before : false,
      runs: [{ ...templateRun, id: `${templateRun.id}-slice-${sliceIndex}`, text }],
    },
  };
}

function createPreviewNode(measureDocument: Document, block: NativeDocxBlock): HTMLElement {
  if (block.type === 'paragraph') {
    // Match DocxBlockPreview exactly (shared paragraphVisualStyle) so measured height == rendered height.
    const v = paragraphVisualStyle(block);
    const node = measureDocument.createElement(v.tag);
    node.style.textAlign = block.alignment;
    node.style.whiteSpace = 'pre-wrap';
    node.style.overflowWrap = 'anywhere';
    node.style.fontFamily = v.fontFamily;
    node.style.fontSize = String(v.style.fontSize);
    node.style.lineHeight = String(v.style.lineHeight);
    node.style.fontWeight = String(v.style.fontWeight ?? 400);
    node.style.margin = String(v.style.margin ?? '0');                 // real margin -> counted in the wrapper box
    if (v.style.paddingBottom) node.style.paddingBottom = String(v.style.paddingBottom); // Title rule height
    if (v.style.borderBottom) node.style.borderBottom = String(v.style.borderBottom);    // Title rule height

    if (block.list_kind !== 'none') {
      const marker = measureDocument.createElement('span');
      marker.style.marginRight = v.listMarkerMarginRight;
      marker.style.color = '#9ca3af';
      marker.textContent = block.list_kind === 'bullet' ? '•' : '1.';
      node.appendChild(marker);
    }

    block.runs.forEach((run) => {
      const span = measureDocument.createElement('span');
      if (run.bold) span.style.fontWeight = '700';
      if (run.italic) span.style.fontStyle = 'italic';
      if (run.underline) span.style.textDecoration = 'underline';
      const ff = runFontFamily(run.font_name);                         // match render: only a non-default font
      if (ff) span.style.fontFamily = ff;
      if (run.font_size_pt && run.font_size_pt !== 11) span.style.fontSize = `${run.font_size_pt}pt`; // match render
      span.textContent = run.text || '\u00A0';
      node.appendChild(span);
    });
    return node;
  }

  if (block.type === 'table') {
    const table = measureDocument.createElement('table');
    table.style.width = '100%';
    table.style.borderCollapse = 'collapse';
    table.style.border = '1px solid #d1d5db';
    table.style.fontSize = '14px';
    const tbody = measureDocument.createElement('tbody');
    block.rows.forEach((row) => {
      const tr = measureDocument.createElement('tr');
      row.cells.forEach((cell) => {
        const td = measureDocument.createElement('td');
        td.style.border = '1px solid #d1d5db';
        td.style.padding = '4px 8px';
        td.style.verticalAlign = 'top';
        td.textContent = cell.paragraphs.map((p) => flattenParagraphText(p)).join('\n') || '\u00A0';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  if (block.type === 'image') {
    const node = measureDocument.createElement('div');
    node.style.height = `${Math.max(120, Math.min(256, block.height_px ?? 180))}px`;
    node.style.borderRadius = '8px';
    node.style.overflow = 'hidden';
    if (block.base64_data) {
      const img = measureDocument.createElement('img');
      img.src = `data:${block.content_type || 'image/png'};base64,${block.base64_data}`;
      img.alt = block.alt_text || block.filename;
      img.style.maxHeight = '256px';
      img.style.maxWidth = '100%';
      img.style.objectFit = 'contain';
      node.appendChild(img);
    } else {
      node.style.border = '1px dashed #d1d5db';
      node.style.display = 'flex';
      node.style.alignItems = 'center';
      node.style.justifyContent = 'center';
      node.style.fontSize = '12px';
      node.style.color = '#9ca3af';
      node.textContent = `Image: ${block.alt_text || block.filename}`;
    }
    return node;
  }

  if (block.type === 'page_break') {
    const node = measureDocument.createElement('div');
    node.style.margin = '8px 0';
    node.style.padding = '4px 0';
    node.style.borderTop = '2px dashed #d1d5db';
    node.style.fontSize = '10px';
    node.style.letterSpacing = '0.14em';
    node.style.textTransform = 'uppercase';
    node.style.textAlign = 'center';
    node.style.color = '#9ca3af';
    node.textContent = 'Page break';
    return node;
  }

  const node = measureDocument.createElement('div');
  node.style.border = '1px dashed #d1d5db';
  node.style.borderRadius = '8px';
  node.style.background = '#f9fafb';
  node.style.padding = '8px 12px';
  node.style.fontSize = '12px';
  node.style.color = '#6b7280';
  node.textContent = (block as any).label || 'Unsupported block';
  return node;
}

function createMeasureHost(measureDocument: Document): HTMLDivElement {
  const host = measureDocument.createElement('div');
  host.style.position = 'fixed';
  host.style.left = '-10000px';
  host.style.top = '0';
  host.style.visibility = 'hidden';
  host.style.pointerEvents = 'none';
  host.style.zIndex = '-1';
  host.style.width = '0';
  host.style.height = '0';
  host.style.overflow = 'hidden';
  measureDocument.body.appendChild(host);
  return host;
}

function measureRenderedBlockHeight(
  measureDocument: Document,
  host: HTMLDivElement,
  block: NativeDocxBlock,
  contentWidthPx: number
): number {
  const wrapper = measureDocument.createElement('div');
  wrapper.style.width = `${contentWidthPx}px`;
  wrapper.style.boxSizing = 'border-box';
  // Match the on-screen block wrapper (InlineEditableBlock: px-1 py-0.5 + border-2) so the measured
  // border-box height equals the rendered height. The 2px transparent border also stops the inner node's
  // margin collapsing out, so getBoundingClientRect includes the paragraph's real top/bottom margin.
  wrapper.style.padding = '2px 4px';
  wrapper.style.border = '2px solid transparent';
  wrapper.style.borderRadius = '4px';
  wrapper.appendChild(createPreviewNode(measureDocument, block));
  host.appendChild(wrapper);
  const height = Math.ceil(wrapper.getBoundingClientRect().height);
  host.removeChild(wrapper);
  return height;
}

function snapParagraphSliceEnd(text: string, startOffset: number, endOffset: number): number {
  if (endOffset >= text.length) return endOffset;
  const minimumSnap = Math.max(startOffset + 1, endOffset - Math.min(24, Math.floor((endOffset - startOffset) / 3)));
  for (let index = endOffset; index > minimumSnap; index -= 1) {
    if (/\s/.test(text[index - 1] || '')) return index;
  }
  return endOffset;
}

function fitParagraphSliceToHeight(
  measureDocument: Document,
  host: HTMLDivElement,
  paragraph: NativeDocxParagraph,
  fullText: string,
  startOffset: number,
  sliceIndex: number,
  availableHeightPx: number,
  contentWidthPx: number
): ParagraphPaginationSlice | null {
  if (availableHeightPx <= 0) return null;

  const wholeSlice = createParagraphSlice(paragraph, fullText, startOffset, fullText.length, sliceIndex);
  if (measureRenderedBlockHeight(measureDocument, host, wholeSlice.paragraph, contentWidthPx) <= availableHeightPx) {
    return wholeSlice;
  }

  let low = startOffset + 1;
  let high = fullText.length;
  let best = -1;

  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const candidate = createParagraphSlice(paragraph, fullText, startOffset, mid, sliceIndex);
    const height = measureRenderedBlockHeight(measureDocument, host, candidate.paragraph, contentWidthPx);
    if (height <= availableHeightPx) {
      best = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }

  if (best < startOffset + 1) return null;

  const snapped = snapParagraphSliceEnd(fullText, startOffset, best);
  if (snapped !== best) {
    const snappedCandidate = createParagraphSlice(paragraph, fullText, startOffset, snapped, sliceIndex);
    if (measureRenderedBlockHeight(measureDocument, host, snappedCandidate.paragraph, contentWidthPx) <= availableHeightPx) {
      return snappedCandidate;
    }
  }

  return createParagraphSlice(paragraph, fullText, startOffset, best, sliceIndex);
}

// A heading or a keep-together paragraph must NEVER be sliced mid-text across a page boundary (Word keeps
// headings whole). Such paragraphs are placed atomically by the paginator below.
function paragraphIsAtomic(block: NativeDocxParagraph): boolean {
  return block.style_name.startsWith('Heading') || block.style_name === 'Title' || block.keep_together === true;
}

function splitBlocksIntoPages(
  section: NativeDocxSection,
  measureDocument?: Document
): Array<Array<{
  key: string;
  block: NativeDocxBlock;
  originalIndex: number;
  sliceIndex: number | null;
  startOffset: number | null;
  endOffset: number | null;
}>> {
  const { contentWidthMm, contentHeightPx } = sectionPageLayout(section);
  const contentWidthPx = contentWidthMm * MM_TO_PX;
  const pages: Array<Array<{
    key: string;
    block: NativeDocxBlock;
    originalIndex: number;
    sliceIndex: number | null;
    startOffset: number | null;
    endOffset: number | null;
  }>> = [];
  let current: Array<{
    key: string;
    block: NativeDocxBlock;
    originalIndex: number;
    sliceIndex: number | null;
    startOffset: number | null;
    endOffset: number | null;
  }> = [];
  let currentHeight = 0;
  const host = measureDocument ? createMeasureHost(measureDocument) : null;

  try {
    section.blocks.forEach((sourceBlock, bi) => {
      if (sourceBlock.type === 'page_break') {
        pages.push(current);
        current = [];
        currentHeight = 0;
        return;
      }

      if (sourceBlock.type === 'paragraph' && host && measureDocument) {
        // Headings / keep-together paragraphs are placed WHOLE (never sliced). If the block does not fit on
        // the current (non-empty) page, it moves whole to the next page. Only if it is taller than an empty
        // page (rare) do we fall through to the slicer below — so we never overflow or infinite-loop.
        if (paragraphIsAtomic(sourceBlock)) {
          if (sourceBlock.page_break_before && current.length > 0) {
            pages.push(current);
            current = [];
            currentHeight = 0;
          }
          const fullHeight = measureRenderedBlockHeight(measureDocument, host, sourceBlock, contentWidthPx);
          const gap = 0; // inter-block spacing is the block's own margin, already in its measured height
          const availableHeightPx = contentHeightPx - currentHeight - gap - PAGE_FIT_SAFETY_PX;
          if (fullHeight > availableHeightPx && current.length > 0) {
            pages.push(current);
            current = [];
            currentHeight = 0;
          }
          if (fullHeight <= contentHeightPx - PAGE_FIT_SAFETY_PX || current.length > 0) {
            current.push({
              key: `${sourceBlock.id}-slice-0`,
              block: sourceBlock,
              originalIndex: bi,
              sliceIndex: 0,
              startOffset: 0,
              endOffset: flattenParagraphText(sourceBlock).length,
            });
            currentHeight += fullHeight; // block height already includes its own top/bottom margin
            return;
          }
          // else: taller than an empty page AND page already empty -> fall through to the slicer
        }

        const fullText = flattenParagraphText(sourceBlock);
        let startOffset = 0;
        let sliceIndex = 0;

        do {
          if (sliceIndex === 0 && sourceBlock.page_break_before && current.length > 0) {
            pages.push(current);
            current = [];
            currentHeight = 0;
          }

          const gap = 0; // inter-block spacing is the block's own margin, already in its measured height
          const availableHeightPx = contentHeightPx - currentHeight - gap - PAGE_FIT_SAFETY_PX;
          let fitted = fitParagraphSliceToHeight(
            measureDocument,
            host,
            sourceBlock,
            fullText,
            startOffset,
            sliceIndex,
            availableHeightPx,
            contentWidthPx
          );

          if (!fitted) {
            if (current.length > 0) {
              pages.push(current);
              current = [];
              currentHeight = 0;
              continue;
            }
            const forcedEnd = Math.min(fullText.length, startOffset + 1);
            fitted = createParagraphSlice(sourceBlock, fullText, startOffset, forcedEnd, sliceIndex);
          }

          const blockHeight = measureRenderedBlockHeight(measureDocument, host, fitted.paragraph, contentWidthPx);
          current.push({
            key: `${sourceBlock.id}-slice-${sliceIndex}`,
            block: fitted.paragraph,
            originalIndex: bi,
            sliceIndex,
            startOffset: fitted.startOffset,
            endOffset: fitted.endOffset,
          });
          currentHeight += blockHeight; // block height already includes its own top/bottom margin
          startOffset = fitted.endOffset;
          sliceIndex += 1;

          if (startOffset < fullText.length) {
            pages.push(current);
            current = [];
            currentHeight = 0;
          } else if (fullText.length === 0) {
            break;
          }
        } while (startOffset < fullText.length || (fullText.length === 0 && sliceIndex === 0));
        return;
      }

      // Non-measured fallback (pre-measure paint): keep headings / keep-together paragraphs whole too.
      if (sourceBlock.type === 'paragraph' && paragraphIsAtomic(sourceBlock)) {
        if (sourceBlock.page_break_before && current.length > 0) {
          pages.push(current);
          current = [];
          currentHeight = 0;
        }
        const blockHeight = estimateBlockHeight(sourceBlock, contentWidthMm);
        const gap = 0; // inter-block spacing is the block's own margin, already in its estimated height
        if (current.length > 0 && currentHeight + gap + blockHeight > contentHeightPx - PAGE_FIT_SAFETY_PX) {
          pages.push(current);
          current = [];
          currentHeight = 0;
        }
        current.push({
          key: `${sourceBlock.id}-slice-0`,
          block: sourceBlock,
          originalIndex: bi,
          sliceIndex: 0,
          startOffset: 0,
          endOffset: flattenParagraphText(sourceBlock).length,
        });
        currentHeight += blockHeight; // block height already includes its own top/bottom margin
        return;
      }

      const renderBlocks = sourceBlock.type === 'paragraph'
        ? splitParagraphForPagination(sourceBlock, contentWidthMm)
        : [{ sliceIndex: 0, startOffset: null as number | null, endOffset: null as number | null, text: '', paragraph: sourceBlock as never }];

      renderBlocks.forEach((renderBlock, fallbackIndex) => {
        const block = sourceBlock.type === 'paragraph' ? renderBlock.paragraph : sourceBlock;
        const forceNewPage = block.type === 'paragraph' && block.page_break_before && current.length > 0;
        if (forceNewPage) {
          pages.push(current);
          current = [];
          currentHeight = 0;
        }

        const blockHeight = estimateBlockHeight(block, contentWidthMm);
        const gap = 0; // inter-block spacing is the block's own margin, already in its estimated height
        if (current.length > 0 && currentHeight + gap + blockHeight > contentHeightPx - PAGE_FIT_SAFETY_PX) {
          pages.push(current);
          current = [];
          currentHeight = 0;
        }

        current.push({
          key: `${sourceBlock.id}-slice-${fallbackIndex}`,
          block,
          originalIndex: bi,
          sliceIndex: sourceBlock.type === 'paragraph' ? renderBlock.sliceIndex : null,
          startOffset: sourceBlock.type === 'paragraph' ? renderBlock.startOffset : null,
          endOffset: sourceBlock.type === 'paragraph' ? renderBlock.endOffset : null,
        });
        currentHeight += blockHeight; // block height already includes its own top/bottom margin
      });
    });
  } finally {
    if (host?.parentNode) host.parentNode.removeChild(host);
  }

  pages.push(current);
  const filtered = pages.filter((p) => p.length > 0);
  return filtered.length > 0 ? filtered : [[]];
}

type SelectedBlock =
  | { kind: 'block'; sectionIndex: number; blockIndex: number; renderKey?: string; sliceIndex?: number | null }
  | { kind: 'header'; sectionIndex: number; paragraphIndex: number }
  | { kind: 'footer'; sectionIndex: number; paragraphIndex: number }
  | null;

function normalizeFontSize(value: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 11;
  return Math.min(72, Math.max(8, parsed));
}

function ensureParagraphHasRun(paragraph: NativeDocxParagraph): NativeDocxParagraph {
  if (paragraph.runs.length > 0) return paragraph;
  return {
    ...paragraph,
    runs: [{
      id: `${paragraph.id}-run-0`, text: '', bold: false, italic: false, underline: false,
      font_name: 'Arial', font_size_pt: 11, color: '', highlight: '',
    }],
  };
}

function paragraphWithReplacementText(source: NativeDocxParagraph, text: string): NativeDocxParagraph {
  const safe = ensureParagraphHasRun(source);
  return {
    ...safe,
    runs: safe.runs.map((run, index) => index === 0 ? { ...run, text } : { ...run }),
  };
}

const FONT_SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48, 72];

// The document is shown in a clean serif (like the Document Viewer / a real Word print);
// Word's stock body fonts fall back to that serif so the page reads uniform. A font the
// user explicitly picks (anything else) is still honoured.
const DOC_BODY_FONT = "'Times New Roman', Times, serif";
const DEFAULT_DOC_FONTS = new Set(['Arial', 'Calibri', 'Calibri Light', 'Times New Roman', 'Times', '']);
const runFontFamily = (name?: string): string | undefined =>
  name && !DEFAULT_DOC_FONTS.has(name) ? name : undefined;

// SINGLE SOURCE OF TRUTH for a paragraph's visual geometry. Consumed by BOTH DocxBlockPreview (the
// on-screen render) AND createPreviewNode / estimateBlockHeight (off-screen height measurement). They must
// stay identical, or pagination drifts: if measurement is taller than render the pages underfill (dead
// space), if shorter they overflow the fixed-height page. Style by the paragraph's Word style name so the
// editor matches the saved .docx and the Document Viewer.
type ParagraphVisual = {
  tag: 'h1' | 'h2' | 'h3' | 'p';
  isHeading: boolean;
  style: React.CSSProperties; // color, fontSize(pt), fontWeight, lineHeight, margin(pt); Title adds the blue rule
  fontFamily: string;
  listMarkerMarginRight: string;
};

function paragraphVisualStyle(block: NativeDocxParagraph): ParagraphVisual {
  const styleName = block.style_name;
  const isTitle = styleName === 'Title';
  const isH1 = styleName === 'Heading 1';
  const isH2 = styleName === 'Heading 2';
  const isH3 = styleName === 'Heading 3';
  const isHeading = isTitle || isH1 || isH2 || isH3;
  const tag: ParagraphVisual['tag'] = isTitle || isH1 ? 'h1' : isH2 ? 'h2' : isH3 ? 'h3' : 'p';
  const style: React.CSSProperties =
    isTitle ? { color: '#1f3864', fontSize: '22pt', fontWeight: 700, lineHeight: 1.15, margin: '0 0 12pt', paddingBottom: '5pt', borderBottom: '1.5px solid #4472c4' }
    : isH1 ? { color: '#2f5496', fontSize: '16pt', fontWeight: 700, lineHeight: 1.2, margin: '14pt 0 4pt' }
    : isH2 ? { color: '#2f5496', fontSize: '13pt', fontWeight: 700, lineHeight: 1.25, margin: '12pt 0 3pt' }
    : isH3 ? { color: '#2f5496', fontSize: '11.5pt', fontWeight: 700, lineHeight: 1.3, margin: '10pt 0 3pt' }
    : { color: '#1f2328', fontSize: '11pt', fontWeight: 400, lineHeight: 1.4, margin: '0 0 6pt' };
  return { tag, isHeading, style, fontFamily: DOC_BODY_FONT, listMarkerMarginRight: '0.55em' };
}

// Top+bottom margin of a paragraph block in px, parsed from the shared style's CSS `margin` shorthand
// (e.g. '0 0 6pt' or '14pt 0 4pt'). The measured node carries the real margin so its border-box height
// already includes it; this helper is only for the DOM-free estimate fallback. getBoundingClientRect on
// the measured wrapper includes the node's margins (the wrapper's border stops them collapsing out).
function paragraphMarginTopBottomPx(block: NativeDocxParagraph): { top: number; bottom: number } {
  const parts = String(paragraphVisualStyle(block).style.margin ?? '0').trim().split(/\s+/);
  const toPx = (v: string): number => {
    const m = /([\d.]+)pt/.exec(v);
    return m ? parseFloat(m[1]) * PT_TO_PX : 0;
  };
  const top = parts[0] ?? '0';
  const bottom = parts.length >= 3 ? parts[2] : (parts[0] ?? '0');
  return { top: toPx(top), bottom: toPx(bottom) };
}

export default function NativeDocxEditor({
  isOpen = true, onClose, canClose = true, filePath, title,
  initialModel = null, onModelChange, onContentChange, onInsertSelection,
  insertedSelections = [],
}: NativeDocxEditorProps) {
  const [documentModel, setDocumentModel] = useState<NativeDocxDocument | null>(initialModel);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isExportingPdf, setIsExportingPdf] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [selectedBlock, setSelectedBlock] = useState<SelectedBlock>(null);
  const [showWarnings, setShowWarnings] = useState(false);
  const previewRef = useRef<HTMLDivElement>(null);
  const onModelChangeRef = useRef(onModelChange);
  const onContentChangeRef = useRef(onContentChange);
  const lastSentModelRef = useRef<NativeDocxDocument | null>(null);

  useEffect(() => { onModelChangeRef.current = onModelChange; }, [onModelChange]);
  useEffect(() => { onContentChangeRef.current = onContentChange; }, [onContentChange]);

  // Accept model from parent ONLY when it is genuinely new (not an echo of our own edit)
  useEffect(() => {
    if (initialModel && initialModel !== lastSentModelRef.current) {
      setDocumentModel(initialModel);
    }
  }, [initialModel]);

  // Notify parent when our model changes and track the reference so we can ignore the echo
  useEffect(() => {
    if (documentModel) {
      lastSentModelRef.current = documentModel;
      onModelChangeRef.current?.(documentModel);
      onContentChangeRef.current?.(flattenNativeDocxText(documentModel));
    }
  }, [documentModel]);

  useEffect(() => {
    if (documentModel || !filePath) return;
    let cancelled = false;
    (async () => {
      setIsLoading(true); setError(null);
      try {
        const res = await fetch(`${getApiBase()}/api/file/docx-model?path=${encodeURIComponent(filePath.replace(/\\/g, '/'))}`);
        const payload = await res.json();
        if (!res.ok) throw new Error(payload?.detail || 'Failed to load DOCX model');
        if (!cancelled) setDocumentModel(payload as NativeDocxDocument);
      } catch (e) {
        if (!cancelled) { setError(e instanceof Error ? e.message : 'Load failed'); setDocumentModel(createEmptyNativeDocx(filePath, title)); }
      } finally { if (!cancelled) setIsLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [documentModel, filePath, title]);

  const blockRanges = useMemo(() => (documentModel ? collectBlockRanges(documentModel) : []), [documentModel]);

  const allPages = useMemo<PageSlice[]>(() => {
    if (!documentModel) return [];
    const measureDocument = typeof window !== 'undefined' ? window.document : undefined;
    const result: PageSlice[] = [];
    let pageNum = 1;
    documentModel.sections.forEach((section, si) => {
      const pages = splitBlocksIntoPages(section, measureDocument);
      pages.forEach((pageBlocks, pi) => {
        result.push({
          sectionIndex: si,
          section,
          pageIndexInSection: pi,
          totalPagesInSection: pages.length,
          globalPageNumber: pageNum++,
          blocks: pageBlocks,
        });
      });
    });
    return result;
  }, [documentModel]);

  const selectedRenderedBlock = useMemo(() => {
    if (!selectedBlock || selectedBlock.kind !== 'block' || !selectedBlock.renderKey) return null;
    for (const page of allPages) {
      if (page.sectionIndex !== selectedBlock.sectionIndex) continue;
      const match = page.blocks.find((block) =>
        block.key === selectedBlock.renderKey && block.originalIndex === selectedBlock.blockIndex
      );
      if (match) return match;
    }
    return null;
  }, [allPages, selectedBlock]);

  const getMarkIndices = (part: 'header' | 'body' | 'footer', sectionIndex: number, blockIndex: number, paragraphIndex: number | null): number[] => {
    if (!insertedSelections.length || !blockRanges.length) return [];
    const range = blockRanges.find((r) =>
      r.sectionIndex === sectionIndex &&
      r.part === part &&
      (part === 'body' ? r.blockIndex === blockIndex : r.paragraphIndex === paragraphIndex)
    );
    if (!range) return [];
    return insertedSelections
      .map((s, i) => ({ sel: s, idx: i }))
      .filter(({ sel }) => sel.documentId === 'editor' && sel.start < range.end && sel.end > range.start)
      .map(({ idx }) => idx);
  };

  const updateDocument = (updater: (d: NativeDocxDocument) => NativeDocxDocument) => {
    setDocumentModel((c) => updater(c ? cloneNativeDocx(c) : createEmptyNativeDocx(filePath, title)));
  };

  const saveDocument = async () => {
    if (!documentModel) return;
    setIsSaving(true); setError(null);
    try {
      const res = await fetch(`${getApiBase()}/api/file/save-docx-native`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: filePath, document: documentModel }),
      });
      if (!res.ok) { const p = await res.json(); throw new Error(p?.detail || 'Save failed'); }
      setSaveMessage(filePath); setTimeout(() => setSaveMessage(null), 5000);
    } catch (e) { setError(e instanceof Error ? e.message : 'Save failed'); }
    finally { setIsSaving(false); }
  };

  // Native bridge exposed by the desktop window (QtWebEngine). Undefined in a real
  // browser. render_pdf returns {ok, error?}; mode 'pdf' opens a native save dialog,
  // mode 'print' opens the rendered PDF in the system viewer to print.
  const getDesktopApi = () => (window as unknown as {
    pywebview?: { api?: { render_pdf?: (html: string, name: string, mode: string) => Promise<{ ok?: boolean; error?: string } | unknown> } };
  }).pywebview?.api;

  // Self-contained HTML of the rendered A4 pages for the desktop PDF bridge. All text
  // styling lives in inline styles on the nodes (set in DocxBlockPreview), so the
  // off-screen render is faithful without the app's Tailwind. Editor-only chrome
  // (selection boxes, toolbars, markers) is stripped.
  const buildPrintHtml = (): string => {
    const root = previewRef.current;
    if (!root) return '';
    const clone = root.cloneNode(true) as HTMLElement;
    clone.querySelectorAll('[data-export-ignore="true"], [data-editor-marker="true"]').forEach((n) => n.remove());
    clone.querySelectorAll('[data-editor-block="true"]').forEach((n) => {
      (n as HTMLElement).removeAttribute('class');
      (n as HTMLElement).style.cssText = 'padding:0;margin:0;border:none;background:none;';
    });
    clone.querySelectorAll<HTMLElement>('.pdf-page').forEach((el) => {
      el.style.background = '#fff';
      el.style.boxShadow = 'none';
      el.style.margin = '0';
    });
    const css = `
      @page { size: A4; margin: 0; }
      html, body { margin: 0; padding: 0; background: #fff; }
      * { box-sizing: border-box; }
      .pdf-page { break-after: page; page-break-after: always; overflow: hidden; }
      .pdf-page:last-child { break-after: auto; page-break-after: auto; }
    `;
    return `<!DOCTYPE html><html><head><meta charset="utf-8"/><style>${css}</style></head><body>${clone.innerHTML}</body></html>`;
  };

  // PDF / Print — same approach as the .md (A4) editor: in the desktop app use the
  // native render_pdf bridge (real save / system-print dialog); in a browser print the
  // document HTML in a hidden iframe (the browser's own "Save as PDF" / print dialog).
  // A dialog always appears — no silent blob download that QtWebEngine swallows.
  const printOrPdf = async (mode: 'pdf' | 'print') => {
    if (!previewRef.current || !documentModel) return;
    if (mode === 'pdf') setIsExportingPdf(true);
    try {
      setSelectedBlock(null);
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
      );
      const html = buildPrintHtml();
      const name = (title || 'document').replace(/\.[^.]+$/, '') || 'document';

      const desktop = getDesktopApi();
      if (desktop?.render_pdf) {
        const res = (await desktop.render_pdf(html, name, mode)) as { ok?: boolean; error?: string } | undefined;
        if (res && res.ok === false) setError(res.error || 'PDF/Druck nicht verfügbar');
        return;
      }

      // Browser: print via a temporary off-screen iframe (reliable dialog everywhere).
      const frame = document.createElement('iframe');
      frame.setAttribute('aria-hidden', 'true');
      frame.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden;';
      document.body.appendChild(frame);
      const fdoc = frame.contentDocument;
      if (!fdoc) { frame.remove(); return; }
      fdoc.open(); fdoc.write(html); fdoc.close();
      // Wait for layout/fonts so the printed page isn't blank, then clean up after.
      setTimeout(() => {
        try { frame.contentWindow?.focus(); frame.contentWindow?.print(); }
        finally { setTimeout(() => frame.remove(), 1000); }
      }, 300);
    } catch (e) {
      console.error('[NativeDocxEditor] PDF/print failed:', e);
      setError(e instanceof Error ? `PDF/Druck fehlgeschlagen: ${e.message}` : 'PDF/Druck fehlgeschlagen');
    } finally {
      if (mode === 'pdf') setIsExportingPdf(false);
    }
  };
  const exportPdf = () => printOrPdf('pdf');
  const printDocument = () => printOrPdf('print');

  const selectedParagraph = useMemo(() => {
    if (!documentModel || !selectedBlock) return null;
    if (selectedBlock.kind === 'header') return documentModel.sections[selectedBlock.sectionIndex]?.header.paragraphs[selectedBlock.paragraphIndex] ?? null;
    if (selectedBlock.kind === 'footer') return documentModel.sections[selectedBlock.sectionIndex]?.footer.paragraphs[selectedBlock.paragraphIndex] ?? null;
    if (selectedRenderedBlock?.block.type === 'paragraph') return selectedRenderedBlock.block;
    const b = documentModel.sections[selectedBlock.sectionIndex]?.blocks[selectedBlock.blockIndex];
    return b?.type === 'paragraph' ? b : null;
  }, [documentModel, selectedBlock, selectedRenderedBlock]);

  const selectedBlockValue = useMemo(() => {
    if (!documentModel || !selectedBlock || selectedBlock.kind !== 'block') return null;
    return documentModel.sections[selectedBlock.sectionIndex]?.blocks[selectedBlock.blockIndex] ?? null;
  }, [documentModel, selectedBlock]);

  const updateSelectedParagraph = (updater: (p: NativeDocxParagraph) => NativeDocxParagraph) => {
    if (!selectedBlock) return;
    updateDocument((d) => {
      if (selectedBlock.kind === 'header') d.sections[selectedBlock.sectionIndex].header.paragraphs[selectedBlock.paragraphIndex] = updater(ensureParagraphHasRun(d.sections[selectedBlock.sectionIndex].header.paragraphs[selectedBlock.paragraphIndex]));
      else if (selectedBlock.kind === 'footer') d.sections[selectedBlock.sectionIndex].footer.paragraphs[selectedBlock.paragraphIndex] = updater(ensureParagraphHasRun(d.sections[selectedBlock.sectionIndex].footer.paragraphs[selectedBlock.paragraphIndex]));
      else {
        const b = d.sections[selectedBlock.sectionIndex].blocks[selectedBlock.blockIndex];
        if (b.type === 'paragraph') {
          if (
            selectedRenderedBlock?.block.type === 'paragraph' &&
            typeof selectedRenderedBlock.startOffset === 'number' &&
            typeof selectedRenderedBlock.endOffset === 'number'
          ) {
            const fullText = flattenParagraphText(b);
            const updatedSlice = updater(selectedRenderedBlock.block);
            const replacementText = flattenParagraphText(updatedSlice);
            const nextText =
              fullText.slice(0, selectedRenderedBlock.startOffset) +
              replacementText +
              fullText.slice(selectedRenderedBlock.endOffset);
            const result = paragraphWithReplacementText(b, nextText);
            const sliceRun = updatedSlice.runs[0];
            d.sections[selectedBlock.sectionIndex].blocks[selectedBlock.blockIndex] = {
              ...result,
              alignment: updatedSlice.alignment,
              style_name: updatedSlice.style_name,
              list_kind: updatedSlice.list_kind,
              runs: sliceRun
                ? result.runs.map((r, i) => i === 0 ? {
                    ...r,
                    bold: sliceRun.bold,
                    italic: sliceRun.italic,
                    underline: sliceRun.underline,
                    font_name: sliceRun.font_name,
                    font_size_pt: sliceRun.font_size_pt,
                    color: sliceRun.color,
                  } : r)
                : result.runs,
            };
          } else {
            d.sections[selectedBlock.sectionIndex].blocks[selectedBlock.blockIndex] = updater(ensureParagraphHasRun(b));
          }
        }
      }
      return d;
    });
  };

  // ── Global toolbar formatting: act on the currently selected paragraph (run[0] +
  //    paragraph props), so all controls live in the top toolbar like Word / the A4 editor. ──
  const selPara = selectedParagraph ? ensureParagraphHasRun(selectedParagraph) : null;
  const selRun = selPara?.runs[0] ?? null;
  const setSelRun = (u: (r: NativeDocxRun) => NativeDocxRun) =>
    updateSelectedParagraph((p) => { const s = ensureParagraphHasRun(p); return { ...s, runs: s.runs.map((r, i) => i === 0 ? u({ ...r }) : { ...r }) }; });
  const setSelPara = (patch: Partial<NativeDocxParagraph>) =>
    updateSelectedParagraph((p) => ({ ...ensureParagraphHasRun(p), ...patch }));

  const addBlock = (type: 'paragraph' | 'table' | 'page_break') => {
    updateDocument((d) => {
      const si = selectedBlock?.sectionIndex ?? 0;
      const after = selectedBlock?.kind === 'block' ? selectedBlock.blockIndex : d.sections[si].blocks.length - 1;
      const ts = Date.now();
      let newBlock: NativeDocxBlock;
      if (type === 'table') {
        newBlock = { id: `table-${ts}`, type: 'table', style_name: 'Table Grid', rows: [{ id: `table-${ts}-r0`, cells: [{ id: `table-${ts}-r0-c0`, column_span: 1, row_span: 1, paragraphs: [{ id: `table-${ts}-r0-c0-p0`, type: 'paragraph', style_name: 'Normal', alignment: 'left', list_kind: 'none', list_level: 0, page_break_before: false, keep_with_next: false, keep_together: false, runs: [{ id: `table-${ts}-r0-c0-p0-run0`, text: '', bold: false, italic: false, underline: false, font_name: 'Arial', font_size_pt: 11, color: '', highlight: '' }] }] }] }] };
      } else if (type === 'page_break') {
        newBlock = { id: `pb-${ts}`, type: 'page_break' };
      } else {
        newBlock = { id: `p-${ts}`, type: 'paragraph', style_name: 'Normal', alignment: 'left', list_kind: 'none', list_level: 0, page_break_before: false, keep_with_next: false, keep_together: false, runs: [{ id: `p-${ts}-run0`, text: '', bold: false, italic: false, underline: false, font_name: 'Arial', font_size_pt: 11, color: '', highlight: '' }] };
      }
      d.sections[si].blocks.splice(after + 1, 0, newBlock);
      return d;
    });
  };

  const deleteSelectedBlock = () => {
    if (!selectedBlock || selectedBlock.kind !== 'block') return;
    updateDocument((d) => {
      d.sections[selectedBlock.sectionIndex].blocks.splice(selectedBlock.blockIndex, 1);
      return d;
    });
    setSelectedBlock(null);
  };

  const insertSelectedBlockIntoChat = () => {
    if (!selectedBlock || !documentModel || !onInsertSelection) return;
    const range = blockRanges.find((item) => {
      if (item.sectionIndex !== selectedBlock.sectionIndex) return false;
      if (selectedBlock.kind === 'block') return item.part === 'body' && item.blockIndex === selectedBlock.blockIndex;
      if (selectedBlock.kind === 'header') return item.part === 'header' && item.paragraphIndex === selectedBlock.paragraphIndex;
      return item.part === 'footer' && item.paragraphIndex === selectedBlock.paragraphIndex;
    });
    if (!range) return;
    const alreadyMarked = insertedSelections.some(
      (s) => s.documentId === 'editor' && s.start === range.start && s.end === range.end
    );
    if (alreadyMarked) return;
    onInsertSelection(range.text, { start: range.start, end: range.end, documentId: 'editor' });
  };

  const isBlockSelected = (kind: string, sectionIndex: number, index: number, renderKey?: string) => {
    if (!selectedBlock) return false;
    if (selectedBlock.kind !== kind || selectedBlock.sectionIndex !== sectionIndex) return false;
    if (kind === 'block') {
      if ((selectedBlock as any).blockIndex !== index) return false;
      if (renderKey && (selectedBlock as any).renderKey) return (selectedBlock as any).renderKey === renderKey;
      return true;
    }
    return (selectedBlock as any).paragraphIndex === index;
  };

  if (isLoading || !documentModel) {
    return <div className="flex h-full items-center justify-center bg-[#F9FAFB] text-sm text-gray-500"><Loader2 size={18} className="mr-2 animate-spin" />Loading...</div>;
  }

  return (
    <div className={cn('flex h-full min-h-0 w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-[#F9FAFB] transition-all duration-300 ease-out', isOpen ? 'translate-x-0 opacity-100' : 'translate-x-8 opacity-0 pointer-events-none')}>
      {/* Header — matches the A4 editor: icon box + filename + status dot */}
      <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4 shrink-0">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600">
            <FileText size={14} />
          </div>
          <div className="min-w-0 flex-1 flex flex-col">
            <span className="truncate text-xs font-semibold text-gray-900">{title}</span>
            <div className="flex items-center gap-2 text-[10px] text-gray-500">
              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-gray-400" />
              {error ? <span className="truncate max-w-[240px] text-red-600">{error}</span>
                : saveMessage ? <span className="text-emerald-600">Gespeichert</span>
                  : <span className="uppercase">Ready</span>}
            </div>
          </div>
        </div>
        {canClose && onClose && (
          <button type="button" onClick={onClose} className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"><X size={14} /></button>
        )}
      </div>

      {/* Toolbar — global formatting (applies to the selected paragraph) + actions, all at
          the top like Word / the A4 editor. Click a paragraph to select it, then format here. */}
      <div className="flex flex-wrap items-center gap-0.5 border-b border-gray-200 bg-gray-50 px-2 py-1 shrink-0">
        <ToggleBtn active={!!selRun?.bold} disabled={!selectedParagraph} onClick={() => setSelRun((r) => ({ ...r, bold: !r.bold }))} title="Fett"><Bold size={16} /></ToggleBtn>
        <ToggleBtn active={!!selRun?.italic} disabled={!selectedParagraph} onClick={() => setSelRun((r) => ({ ...r, italic: !r.italic }))} title="Kursiv"><Italic size={16} /></ToggleBtn>
        <ToggleBtn active={!!selRun?.underline} disabled={!selectedParagraph} onClick={() => setSelRun((r) => ({ ...r, underline: !r.underline }))} title="Unterstrichen"><Underline size={16} /></ToggleBtn>
        <Sep />
        <ToggleBtn active={selPara?.alignment === 'left'} disabled={!selectedParagraph} onClick={() => setSelPara({ alignment: 'left' })} title="Links"><AlignLeft size={16} /></ToggleBtn>
        <ToggleBtn active={selPara?.alignment === 'center'} disabled={!selectedParagraph} onClick={() => setSelPara({ alignment: 'center' })} title="Zentriert"><AlignCenter size={16} /></ToggleBtn>
        <ToggleBtn active={selPara?.alignment === 'right'} disabled={!selectedParagraph} onClick={() => setSelPara({ alignment: 'right' })} title="Rechts"><AlignRight size={16} /></ToggleBtn>
        <Sep />
        <ToggleBtn active={selPara?.list_kind === 'bullet'} disabled={!selectedParagraph} onClick={() => setSelPara({ list_kind: selPara?.list_kind === 'bullet' ? 'none' : 'bullet' })} title="Aufzählung"><List size={16} /></ToggleBtn>
        <ToggleBtn active={selPara?.list_kind === 'numbered'} disabled={!selectedParagraph} onClick={() => setSelPara({ list_kind: selPara?.list_kind === 'numbered' ? 'none' : 'numbered' })} title="Nummerierung"><ListOrdered size={16} /></ToggleBtn>
        <Sep />
        <select value={selPara?.style_name ?? 'Normal'} disabled={!selectedParagraph} onChange={(e) => setSelPara({ style_name: e.target.value })} className="h-7 rounded border border-gray-200 bg-white px-1.5 text-xs text-gray-700 focus:outline-none disabled:opacity-40" title="Absatzstil">
          <option value="Normal">Normal</option>
          <option value="Title">Title</option>
          <option value="Heading 1">Heading 1</option>
          <option value="Heading 2">Heading 2</option>
          <option value="Heading 3">Heading 3</option>
        </select>
        <select value={String(selRun?.font_size_pt ?? 11)} disabled={!selectedParagraph} onChange={(e) => setSelRun((r) => ({ ...r, font_size_pt: normalizeFontSize(e.target.value) }))} className="h-7 w-14 rounded border border-gray-200 bg-white px-1 text-xs text-gray-700 focus:outline-none disabled:opacity-40" title="Schriftgröße">
          {FONT_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input value={selRun?.font_name || 'Arial'} disabled={!selectedParagraph} onChange={(e) => setSelRun((r) => ({ ...r, font_name: e.target.value }))} className="h-7 w-24 rounded border border-gray-200 bg-white px-1.5 text-xs text-gray-700 focus:outline-none disabled:opacity-40" placeholder="Schriftart" title="Schriftart" />
        <Sep />
        <button type="button" disabled={!selectedBlock || selectedBlock.kind !== 'block'} onClick={deleteSelectedBlock} className="rounded border border-gray-200 bg-white p-1.5 text-red-600 hover:bg-red-50 disabled:opacity-40 disabled:hover:bg-white" title="Block löschen"><Trash2 size={16} /></button>
        {onInsertSelection && <button type="button" disabled={!selectedParagraph} onClick={insertSelectedBlockIntoChat} className="rounded border border-gray-200 bg-white p-1.5 text-gray-500 hover:bg-gray-100 disabled:opacity-40 disabled:hover:bg-white" title="In den Chat"><MessageSquare size={16} /></button>}
        <Sep />
        <button type="button" onClick={() => addBlock('paragraph')} className="px-2 py-1 rounded text-xs text-gray-600 hover:bg-gray-200 inline-flex items-center gap-1" title="Absatz hinzufügen"><Plus size={14} />Absatz</button>
        <button type="button" onClick={() => addBlock('table')} className="px-2 py-1 rounded text-xs text-gray-600 hover:bg-gray-200" title="Tabelle hinzufügen">Tabelle</button>
        <button type="button" onClick={() => addBlock('page_break')} className="px-2 py-1 rounded text-xs text-gray-600 hover:bg-gray-200" title="Seitenumbruch">Umbruch</button>
        <span className="ml-auto" />
        <button type="button" onClick={saveDocument} disabled={isSaving} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100 disabled:opacity-50" title="Speichern">
          {isSaving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} Save
        </button>
        <button type="button" onClick={printDocument} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100" title="Drucken">
          <Printer size={14} /> Drucken
        </button>
        <button type="button" onClick={exportPdf} disabled={isExportingPdf} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 disabled:opacity-50" title="Als PDF speichern">
          {isExportingPdf ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />} PDF
        </button>
      </div>

      {/* Warnings banner */}
      {documentModel.warnings.length > 0 && (
        <button type="button" onClick={() => setShowWarnings(!showWarnings)} className="flex items-center gap-2 border-b border-amber-200 bg-amber-50 px-4 py-1.5 text-[11px] text-amber-800 hover:bg-amber-100">
          {showWarnings ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          {documentModel.warnings.length} import warning{documentModel.warnings.length > 1 ? 's' : ''}
          {showWarnings && (
            <ul className="ml-4 list-disc text-left">
              {documentModel.warnings.map((w) => <li key={w.id}>{w.message}</li>)}
            </ul>
          )}
        </button>
      )}

      {/* Document area -- full width, A4 pages on the same gray canvas as the A4 editor */}
      <div className="min-h-0 flex-1 overflow-auto bg-[#e5e7eb] p-4" onClick={(e) => { if (e.target === e.currentTarget) setSelectedBlock(null); }}>
        <div ref={previewRef} className="mx-auto flex w-[210mm] flex-col gap-6">
          {allPages.map((page) => {
            const { sectionIndex: si, section, pageIndexInSection, totalPagesInSection, globalPageNumber, blocks: pageBlocks } = page;
            const isFirstPage = pageIndexInSection === 0;
            const isLastPage = pageIndexInSection === totalPagesInSection - 1;
            const layout = sectionPageLayout(section);
            return (
              <div
                key={`${section.id}-page-${pageIndexInSection}`}
                className="pdf-page relative flex flex-col bg-white rounded-sm shadow-sm"
                style={{
                  paddingTop: `${layout.marginTopMm}mm`,
                  paddingRight: `${layout.marginRightMm}mm`,
                  paddingBottom: `${layout.marginBottomMm}mm`,
                  paddingLeft: `${layout.marginLeftMm}mm`,
                  width: `${layout.pageWidthMm}mm`,
                  height: `${layout.pageHeightMm}mm`,
                  boxSizing: 'border-box',
                  breakAfter: globalPageNumber === allPages.length ? 'auto' : 'page',
                  pageBreakAfter: globalPageNumber === allPages.length ? 'auto' : 'always',
                }}
              >
                {/* Header — first page of each section */}
                {isFirstPage && section.header.paragraphs.length > 0 && (
                  <div className="mb-4 border-b border-dashed border-gray-200 pb-2">
                    {section.header.paragraphs.map((p, pi) => {
                      const selected = isBlockSelected('header', si, pi);
                      return (
                      <InlineEditableBlock key={p.id} selected={selected} markIndices={getMarkIndices('header', si, -1, pi)} onSelect={() => setSelectedBlock({ kind: 'header', sectionIndex: si, paragraphIndex: pi })}>
                        <DocxBlockPreview
                          block={selected && selectedParagraph ? selectedParagraph : p}
                          editableParagraph={selected ? selectedParagraph : null}
                          onParagraphChange={selected ? (np) => updateSelectedParagraph(() => np) : undefined}
                        />
                      </InlineEditableBlock>
                    )})}
                  </div>
                )}

                {/* Body blocks for this page */}
                <div className="flex-1">
                  {pageBlocks.map(({ key, block, originalIndex: bi, sliceIndex }) => {
                    const sel = isBlockSelected('block', si, bi, key);
                    const marks = getMarkIndices('body', si, bi, null);
                    return (
                      <InlineEditableBlock key={key} selected={sel} markIndices={marks} onSelect={() => setSelectedBlock({ kind: 'block', sectionIndex: si, blockIndex: bi, renderKey: key, sliceIndex })}>
                        <DocxBlockPreview
                          block={sel && selectedParagraph ? selectedParagraph : block}
                          editableParagraph={sel ? selectedParagraph : null}
                          onParagraphChange={sel ? (np) => updateSelectedParagraph(() => np) : undefined}
                        />
                        {sel && block.type === 'table' && selectedBlockValue?.type === 'table' && (
                          <InlineTableEditor table={selectedBlockValue} onChange={(t) => updateDocument((d) => { d.sections[si].blocks[bi] = t; return d; })} onDelete={deleteSelectedBlock} onInsertToChat={onInsertSelection ? insertSelectedBlockIntoChat : undefined} />
                        )}
                        {sel && block.type === 'image' && selectedBlockValue?.type === 'image' && (
                          <InlineImageEditor image={selectedBlockValue} onChange={(img) => updateDocument((d) => { d.sections[si].blocks[bi] = img; return d; })} onDelete={deleteSelectedBlock} onInsertToChat={onInsertSelection ? insertSelectedBlockIntoChat : undefined} />
                        )}
                        {sel && block.type === 'unsupported' && (
                          <div data-export-ignore="true" className="mt-2 flex items-center gap-2">
                            <button type="button" onClick={deleteSelectedBlock} className="rounded border border-red-200 bg-red-50 px-2 py-1 text-[10px] text-red-700 hover:bg-red-100"><Trash2 size={12} className="inline mr-1" />Delete</button>
                            {onInsertSelection && <button type="button" onClick={insertSelectedBlockIntoChat} className="rounded border border-gray-300 bg-white px-2 py-1 text-[10px] text-gray-600 hover:bg-gray-100"><MessageSquare size={12} className="inline mr-1" />To chat</button>}
                          </div>
                        )}
                      </InlineEditableBlock>
                    );
                  })}
                </div>

                {/* Footer — last page of each section */}
                {isLastPage && section.footer.paragraphs.length > 0 && (
                  <div className="mt-auto border-t border-dashed border-gray-200 pt-2">
                    {section.footer.paragraphs.map((p, pi) => {
                      const selected = isBlockSelected('footer', si, pi);
                      return (
                      <InlineEditableBlock key={p.id} selected={selected} markIndices={getMarkIndices('footer', si, -2, pi)} onSelect={() => setSelectedBlock({ kind: 'footer', sectionIndex: si, paragraphIndex: pi })}>
                        <DocxBlockPreview
                          block={selected && selectedParagraph ? selectedParagraph : p}
                          editableParagraph={selected ? selectedParagraph : null}
                          onParagraphChange={selected ? (np) => updateSelectedParagraph(() => np) : undefined}
                        />
                      </InlineEditableBlock>
                    )})}
                  </div>
                )}

                {/* Page number */}
                <div className="absolute bottom-3 left-1/2 -translate-x-1/2 text-[10px] text-gray-400 select-none">
                  {globalPageNumber} / {allPages.length}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function InlineEditableBlock({ children, selected, onSelect, markIndices = [] }: {
  children: React.ReactNode; selected: boolean; onSelect: () => void; markIndices?: number[];
}) {
  const marked = markIndices.length > 0;
  const markColor = marked ? MARK_COLORS[markIndices[0] % MARK_COLORS.length] : null;
  return (
    <div
      data-editor-block="true"
      onClick={(e) => { e.stopPropagation(); onSelect(); }}
      className={cn(
        'relative rounded px-1 py-0.5 transition-all cursor-pointer',
        selected
          ? 'border-2 border-blue-400 bg-blue-50/40'
          : marked
            ? `border-2 border-transparent border-l-[3px] ${markColor!.border} ${markColor!.bg}`
            : 'border-2 border-transparent hover:border-gray-200 hover:bg-gray-50/50'
      )}
    >
      {marked && (
        <div data-editor-marker="true" className="absolute -top-1.5 -right-1.5 flex gap-0.5">
          {markIndices.map((idx) => (
            <div key={idx} className={cn('h-2.5 w-2.5 rounded-full ring-2 ring-white', MARK_COLORS[idx % MARK_COLORS.length].dot)} />
          ))}
        </div>
      )}
      {children}
    </div>
  );
}


function InlineTableEditor({ table, onChange, onDelete, onInsertToChat }: {
  table: NativeDocxTable; onChange: (t: NativeDocxTable) => void; onDelete?: () => void; onInsertToChat?: () => void;
}) {
  const updateCell = (ri: number, ci: number, val: string) => {
    onChange({ ...table, rows: table.rows.map((row, rIdx) => rIdx !== ri ? row : { ...row, cells: row.cells.map((cell, cIdx) => cIdx !== ci ? cell : { ...cell, paragraphs: cell.paragraphs.map((p, pIdx) => pIdx !== 0 ? p : { ...p, runs: p.runs.map((r, rI) => rI !== 0 ? r : { ...r, text: val }) }) }) }) });
  };
  return (
    <div data-export-ignore="true" className="mt-2 rounded-xl border border-gray-200 bg-white p-3 shadow-lg" onClick={(e) => e.stopPropagation()}>
      <div className="space-y-1.5">
        {table.rows.map((row, ri) => (
          <div key={row.id} className="grid gap-1.5" style={{ gridTemplateColumns: `repeat(${row.cells.length}, 1fr)` }}>
            {row.cells.map((cell, ci) => (
              <textarea key={cell.id} value={cell.paragraphs.map((p) => flattenParagraphText(p)).join('\n')} onChange={(e) => updateCell(ri, ci, e.target.value)} className="h-16 resize-y rounded border border-gray-200 bg-gray-50 p-2 text-xs text-gray-800 focus:border-blue-400 focus:outline-none" />
            ))}
          </div>
        ))}
      </div>
      <div className="mt-2 flex items-center gap-2">
        {onDelete && <button type="button" onClick={onDelete} className="rounded border border-red-200 bg-red-50 px-2 py-1 text-[10px] text-red-700 hover:bg-red-100"><Trash2 size={12} className="inline mr-1" />Delete table</button>}
        {onInsertToChat && <button type="button" onClick={onInsertToChat} className="rounded border border-gray-200 bg-white px-2 py-1 text-[10px] text-gray-600 hover:bg-gray-100"><MessageSquare size={12} className="inline mr-1" />To chat</button>}
      </div>
    </div>
  );
}

function InlineImageEditor({ image, onChange, onDelete, onInsertToChat }: {
  image: NativeDocxImage; onChange: (i: NativeDocxImage) => void; onDelete?: () => void; onInsertToChat?: () => void;
}) {
  return (
    <div data-export-ignore="true" className="mt-2 rounded-xl border border-gray-200 bg-white p-3 shadow-lg" onClick={(e) => e.stopPropagation()}>
      <input value={image.alt_text} onChange={(e) => onChange({ ...image, alt_text: e.target.value })} className="w-full rounded border border-gray-200 bg-gray-50 px-2.5 py-1.5 text-xs text-gray-800 focus:border-blue-400 focus:outline-none" placeholder="Alt text" />
      <div className="mt-2 flex items-center gap-2">
        {onDelete && <button type="button" onClick={onDelete} className="rounded border border-red-200 bg-red-50 px-2 py-1 text-[10px] text-red-700 hover:bg-red-100"><Trash2 size={12} className="inline mr-1" />Delete</button>}
        {onInsertToChat && <button type="button" onClick={onInsertToChat} className="rounded border border-gray-200 bg-white px-2 py-1 text-[10px] text-gray-600 hover:bg-gray-100"><MessageSquare size={12} className="inline mr-1" />To chat</button>}
      </div>
    </div>
  );
}

/* Renders a block in the document preview */
function DocxBlockPreview({
  block,
  editableParagraph,
  onParagraphChange,
}: {
  block: NativeDocxBlock;
  editableParagraph?: NativeDocxParagraph | null;
  onParagraphChange?: (paragraph: NativeDocxParagraph) => void;
}) {
  const editableSafe = editableParagraph ? ensureParagraphHasRun(editableParagraph) : null;
  const editableRun = editableSafe?.runs[0] ?? null;
  const [draftText, setDraftText] = useState(editableRun?.text ?? '');
  const isFocusedRef = useRef(false);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!isFocusedRef.current) {
      setDraftText(editableRun?.text ?? '');
    }
  }, [editableSafe?.id, editableRun?.text]);

  // Auto-grow the edit box to the full wrapped text (a paragraph is one long line with no
  // newlines, so a fixed row count would clip it into a tiny scrollable box).
  const autosize = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  };
  useEffect(() => { autosize(taRef.current); }, [draftText]);

  if (block.type === 'paragraph') {
    // Style by the paragraph's Word style name so the editor matches the saved .docx and
    // the Document Viewer (the "original" look): the Title is large with a thin blue rule;
    // Heading 1/2/3 are blue, no rule, decreasing sizes; body is a clean dark serif. The
    // template's per-run colour is ignored (as the Viewer does) so the body stays uniform.
    // Shared source of truth (also used by createPreviewNode / estimateBlockHeight for measurement, so
    // pagination measures exactly what is rendered here). See paragraphVisualStyle.
    const v = paragraphVisualStyle(block);
    const isHeading = v.isHeading;
    const Tag = v.tag;
    const blockStyle: React.CSSProperties = v.style;
    if (editableSafe && editableRun && onParagraphChange) {
      const commitDraft = () => {
        if (draftText === editableRun.text) return;
        onParagraphChange({
          ...editableSafe,
          runs: editableSafe.runs.map((r, i) => i === 0 ? { ...r, text: draftText } : { ...r }),
        });
      };

      return (
        <textarea
          ref={(el) => { taRef.current = el; autosize(el); }}
          data-export-ignore="true"
          value={draftText}
          onClick={(e) => e.stopPropagation()}
          onFocus={() => { isFocusedRef.current = true; }}
          onChange={(e) => { setDraftText(e.target.value); autosize(e.currentTarget); }}
          onBlur={() => { isFocusedRef.current = false; commitDraft(); }}
          className="block w-full resize-none overflow-hidden rounded-lg border border-gray-200 bg-white/90 px-0 py-0 text-sm text-gray-900 focus:border-blue-400 focus:outline-none focus:ring-0"
          style={{
            textAlign: editableSafe.alignment as any,
            // Match the rendered document look so editing a paragraph doesn't change it.
            fontFamily: runFontFamily(editableRun.font_name) ?? DOC_BODY_FONT,
            color: blockStyle.color,
            fontWeight: isHeading ? 700 : editableRun.bold ? 700 : 400,
            fontStyle: editableRun.italic ? 'italic' : undefined,
            textDecoration: editableRun.underline ? 'underline' : undefined,
            fontSize: !isHeading && editableRun.font_size_pt && editableRun.font_size_pt !== 11
              ? `${editableRun.font_size_pt}pt`
              : (blockStyle.fontSize as string),
            lineHeight: blockStyle.lineHeight as number,
          }}
          rows={1}
        />
      );
    }
    return (
      <Tag style={{ textAlign: block.alignment as any, fontFamily: v.fontFamily, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', ...blockStyle }}>
        {block.list_kind !== 'none' && <span style={{ marginRight: v.listMarkerMarginRight, color: blockStyle.color }}>{block.list_kind === 'bullet' ? '•' : '1.'}</span>}
        {block.runs.map((run) => (
          <span key={run.id} style={{ fontWeight: run.bold ? 700 : undefined, fontStyle: run.italic ? 'italic' : undefined, textDecoration: run.underline ? 'underline' : undefined, fontFamily: runFontFamily(run.font_name), fontSize: run.font_size_pt && run.font_size_pt !== 11 ? `${run.font_size_pt}pt` : undefined }}>
            {run.text || '\u00A0'}
          </span>
        ))}
      </Tag>
    );
  }
  if (block.type === 'table') {
    return (
      <table className="w-full border-collapse border border-gray-300 text-sm">
        <tbody>
          {block.rows.map((row) => (
            <tr key={row.id}>
              {row.cells.map((cell) => (
                <td key={cell.id} className="border border-gray-300 px-2 py-1 align-top">
                  {cell.paragraphs.map((p) => <div key={p.id}>{flattenParagraphText(p) || '\u00A0'}</div>)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (block.type === 'image') {
    return block.base64_data
      ? <img src={`data:${block.content_type || 'image/png'};base64,${block.base64_data}`} alt={block.alt_text || block.filename} className="max-h-64 rounded object-contain" />
      : <div className="rounded border border-dashed border-gray-300 px-4 py-6 text-center text-xs text-gray-400">Image: {block.alt_text || block.filename}</div>;
  }
  if (block.type === 'page_break') {
    return <div className="my-2 border-t-2 border-dashed border-gray-300 py-1 text-center text-[10px] uppercase tracking-widest text-gray-400">Page break</div>;
  }
  return <div className="rounded border border-dashed border-gray-300 bg-gray-50 px-3 py-2 text-xs text-gray-500">{(block as any).label || 'Unsupported block'}</div>;
}

function ToggleBtn({ active, onClick, title, children, disabled }: { active: boolean; onClick: () => void; title: string; children: React.ReactNode; disabled?: boolean }) {
  return (
    <button type="button" onClick={onClick} title={title} disabled={disabled} className={cn('rounded border p-1.5 transition-colors disabled:opacity-40 disabled:cursor-default disabled:hover:bg-white', active ? 'border-blue-400 bg-blue-50 text-blue-700' : 'border-gray-200 bg-white text-gray-500 hover:bg-gray-100')}>
      {children}
    </button>
  );
}

function Sep() { return <span className="mx-0.5 h-5 w-px bg-gray-200" />; }
