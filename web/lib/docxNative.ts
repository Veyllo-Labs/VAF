// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
export type Alignment = 'left' | 'center' | 'right' | 'justify';
export type ListKind = 'none' | 'bullet' | 'numbered';
export type NativeDocxBlockType = 'paragraph' | 'table' | 'image' | 'page_break' | 'unsupported';

export type NativeDocxWarning = {
  id: string;
  code: string;
  message: string;
  severity: 'info' | 'warning';
};

export type NativeDocxRun = {
  id: string;
  text: string;
  bold: boolean;
  italic: boolean;
  underline: boolean;
  font_name: string;
  font_size_pt: number | null;
  color: string;
  highlight: string;
};

export type NativeDocxParagraph = {
  id: string;
  type: 'paragraph';
  style_name: string;
  alignment: Alignment;
  list_kind: ListKind;
  list_level: number;
  page_break_before: boolean;
  keep_with_next: boolean;
  keep_together: boolean;
  runs: NativeDocxRun[];
};

export type NativeDocxTableCell = {
  id: string;
  paragraphs: NativeDocxParagraph[];
  column_span: number;
  row_span: number;
};

export type NativeDocxTableRow = {
  id: string;
  cells: NativeDocxTableCell[];
};

export type NativeDocxTable = {
  id: string;
  type: 'table';
  rows: NativeDocxTableRow[];
  style_name: string;
};

export type NativeDocxImage = {
  id: string;
  type: 'image';
  alt_text: string;
  filename: string;
  content_type: string;
  base64_data: string;
  width_px: number | null;
  height_px: number | null;
  anchor_kind: 'inline' | 'anchor';
};

export type NativeDocxPageBreak = {
  id: string;
  type: 'page_break';
};

export type NativeDocxUnsupportedBlock = {
  id: string;
  type: 'unsupported';
  label: string;
  xml_tag: string;
  xml_payload: string;
};

export type NativeDocxBlock =
  | NativeDocxParagraph
  | NativeDocxTable
  | NativeDocxImage
  | NativeDocxPageBreak
  | NativeDocxUnsupportedBlock;

export type NativeDocxHeaderFooter = {
  paragraphs: NativeDocxParagraph[];
};

export type NativeDocxSectionProperties = {
  page_width_twips: number | null;
  page_height_twips: number | null;
  margin_top_twips: number | null;
  margin_right_twips: number | null;
  margin_bottom_twips: number | null;
  margin_left_twips: number | null;
  start_type: string;
};

export type NativeDocxSection = {
  id: string;
  properties: NativeDocxSectionProperties;
  header: NativeDocxHeaderFooter;
  footer: NativeDocxHeaderFooter;
  blocks: NativeDocxBlock[];
};

export type NativeDocxSelectionPoint = {
  section_index: number;
  block_index: number;
  row_index: number | null;
  cell_index: number | null;
  paragraph_index: number | null;
  run_index: number | null;
  offset: number;
};

export type NativeDocxSelectionRange = {
  anchor: NativeDocxSelectionPoint;
  focus: NativeDocxSelectionPoint;
};

export type NativeDocxDocument = {
  schema_version: number;
  source_format: string;
  title: string;
  path: string;
  sections: NativeDocxSection[];
  warnings: NativeDocxWarning[];
  active_selection?: NativeDocxSelectionRange | null;
};

/* Helpers for replaceTextInNativeDocx */

export type NativeDocxBlockRange = {
  key: string;
  sectionIndex: number;
  part: 'header' | 'body' | 'footer';
  blockIndex: number;
  paragraphIndex: number | null;
  start: number;
  end: number;
  text: string;
};

export function cloneNativeDocx(document: NativeDocxDocument): NativeDocxDocument {
  return JSON.parse(JSON.stringify(document)) as NativeDocxDocument;
}

export function flattenNativeDocxText(document: NativeDocxDocument | null | undefined): string {
  if (!document) return '';
  return collectBlockRanges(document)
    .map((range) => range.text)
    .filter((text) => text.trim() !== '')
    .join('\n')
    .trim();
}

export function collectBlockRanges(document: NativeDocxDocument): NativeDocxBlockRange[] {
  const ranges: NativeDocxBlockRange[] = [];
  let offset = 0;

  document.sections.forEach((section, sectionIndex) => {
    section.header.paragraphs.forEach((paragraph, paragraphIndex) => {
      const text = flattenParagraphText(paragraph);
      if (!text) return;
      ranges.push({
        key: `section-${sectionIndex}-header-${paragraphIndex}`,
        sectionIndex,
        part: 'header',
        blockIndex: -1,
        paragraphIndex,
        start: offset,
        end: offset + text.length,
        text,
      });
      offset += text.length + 1;
    });

    section.blocks.forEach((block, blockIndex) => {
      const text = flattenBlockText(block);
      if (!text) return;
      ranges.push({
        key: `section-${sectionIndex}-block-${blockIndex}`,
        sectionIndex,
        part: 'body',
        blockIndex,
        paragraphIndex: null,
        start: offset,
        end: offset + text.length,
        text,
      });
      offset += text.length + 1;
    });

    section.footer.paragraphs.forEach((paragraph, paragraphIndex) => {
      const text = flattenParagraphText(paragraph);
      if (!text) return;
      ranges.push({
        key: `section-${sectionIndex}-footer-${paragraphIndex}`,
        sectionIndex,
        part: 'footer',
        blockIndex: -2,
        paragraphIndex,
        start: offset,
        end: offset + text.length,
        text,
      });
      offset += text.length + 1;
    });
  });

  return ranges;
}

export function flattenBlockText(block: NativeDocxBlock): string {
  switch (block.type) {
    case 'paragraph':
      return flattenParagraphText(block);
    case 'table':
      return block.rows
        .map((row) =>
          row.cells
            .map((cell) => cell.paragraphs.map((paragraph) => flattenParagraphText(paragraph)).join(' '))
            .filter(Boolean)
            .join(' | ')
        )
        .filter(Boolean)
        .join('\n');
    case 'image':
      return block.alt_text || block.filename || '[Image]';
    case 'unsupported':
      return `[${block.label}]`;
    default:
      return '';
  }
}

export function flattenParagraphText(paragraph: NativeDocxParagraph): string {
  return paragraph.runs.map((run) => run.text).join('');
}

/**
 * Replace text in the native DOCX document at the given character range.
 * Uses collectBlockRanges on the original document so that offsets align
 * exactly with those the backend computes for editor tools.
 */
export function replaceTextInNativeDocx(
  document: NativeDocxDocument,
  start: number,
  end: number,
  newText: string
): NativeDocxDocument {
  const next = cloneNativeDocx(document);
  const ranges = collectBlockRanges(document);
  let inserted = false;

  for (const range of ranges) {
    if (end <= range.start || start >= range.end) continue;

    const localStart = Math.max(0, start - range.start);
    const localEnd = Math.min(range.end - range.start, end - range.start);
    const before = range.text.slice(0, localStart);
    const after = range.text.slice(localEnd);
    const middle = inserted ? '' : newText;
    inserted = true;
    const newBlockText = before + middle + after;

    const section = next.sections[range.sectionIndex];
    if (!section) continue;

    if (range.part === 'header' && range.paragraphIndex !== null) {
      const p = section.header.paragraphs[range.paragraphIndex];
      if (p) collapseRunsToText(p, newBlockText);
    } else if (range.part === 'footer' && range.paragraphIndex !== null) {
      const p = section.footer.paragraphs[range.paragraphIndex];
      if (p) collapseRunsToText(p, newBlockText);
    } else if (range.part === 'body') {
      const block = section.blocks[range.blockIndex];
      if (!block) continue;
      if (block.type === 'paragraph') {
        collapseRunsToText(block, newBlockText);
      }
    }
  }

  return next;
}

function collapseRunsToText(p: NativeDocxParagraph, text: string): void {
  if (p.runs.length > 0) {
    p.runs = [{ ...p.runs[0], text }];
  }
}

export function createEmptyNativeDocx(path = '', title = 'Document'): NativeDocxDocument {
  return {
    schema_version: 1,
    source_format: 'docx',
    title,
    path,
    warnings: [],
    active_selection: null,
    sections: [
      {
        id: 'section-0',
        properties: {
          page_width_twips: 12240,
          page_height_twips: 15840,
          margin_top_twips: 1440,
          margin_right_twips: 1440,
          margin_bottom_twips: 1440,
          margin_left_twips: 1440,
          start_type: 'newPage',
        },
        header: { paragraphs: [] },
        footer: { paragraphs: [] },
        blocks: [
          {
            id: 'paragraph-0',
            type: 'paragraph',
            style_name: 'Normal',
            alignment: 'left',
            list_kind: 'none',
            list_level: 0,
            page_break_before: false,
            keep_with_next: false,
            keep_together: false,
            runs: [
              {
                id: 'paragraph-0-run-0',
                text: '',
                bold: false,
                italic: false,
                underline: false,
                font_name: 'Arial',
                font_size_pt: 11,
                color: '',
                highlight: '',
              },
            ],
          },
        ],
      },
    ],
  };
}
