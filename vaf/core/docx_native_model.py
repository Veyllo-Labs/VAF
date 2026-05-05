from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal


Alignment = Literal["left", "center", "right", "justify"]
BlockType = Literal["paragraph", "table", "image", "page_break", "unsupported"]
ListKind = Literal["none", "bullet", "numbered"]


@dataclass
class DocxWarning:
    id: str
    code: str
    message: str
    severity: Literal["info", "warning"] = "warning"


@dataclass
class DocxRun:
    id: str
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_name: str = ""
    font_size_pt: float | None = None
    color: str = ""
    highlight: str = ""


@dataclass
class DocxParagraph:
    id: str
    type: Literal["paragraph"] = "paragraph"
    style_name: str = "Normal"
    alignment: Alignment = "left"
    list_kind: ListKind = "none"
    list_level: int = 0
    page_break_before: bool = False
    keep_with_next: bool = False
    keep_together: bool = False
    runs: list[DocxRun] = field(default_factory=list)


@dataclass
class DocxTableCell:
    id: str
    paragraphs: list[DocxParagraph] = field(default_factory=list)
    column_span: int = 1
    row_span: int = 1


@dataclass
class DocxTableRow:
    id: str
    cells: list[DocxTableCell] = field(default_factory=list)


@dataclass
class DocxTable:
    id: str
    type: Literal["table"] = "table"
    rows: list[DocxTableRow] = field(default_factory=list)
    style_name: str = "Table Grid"


@dataclass
class DocxImage:
    id: str
    type: Literal["image"] = "image"
    alt_text: str = ""
    filename: str = ""
    content_type: str = ""
    base64_data: str = ""
    width_px: int | None = None
    height_px: int | None = None
    anchor_kind: Literal["inline", "anchor"] = "inline"


@dataclass
class DocxPageBreak:
    id: str
    type: Literal["page_break"] = "page_break"


@dataclass
class DocxUnsupportedBlock:
    id: str
    type: Literal["unsupported"] = "unsupported"
    label: str = "Unsupported OOXML content"
    xml_tag: str = ""
    xml_payload: str = ""


DocxBlock = DocxParagraph | DocxTable | DocxImage | DocxPageBreak | DocxUnsupportedBlock


@dataclass
class DocxHeaderFooter:
    paragraphs: list[DocxParagraph] = field(default_factory=list)


@dataclass
class DocxSectionProperties:
    page_width_twips: int | None = None
    page_height_twips: int | None = None
    margin_top_twips: int | None = None
    margin_right_twips: int | None = None
    margin_bottom_twips: int | None = None
    margin_left_twips: int | None = None
    start_type: str = "newPage"


@dataclass
class DocxSection:
    id: str
    properties: DocxSectionProperties = field(default_factory=DocxSectionProperties)
    header: DocxHeaderFooter = field(default_factory=DocxHeaderFooter)
    footer: DocxHeaderFooter = field(default_factory=DocxHeaderFooter)
    blocks: list[DocxBlock] = field(default_factory=list)


@dataclass
class DocxSelectionPoint:
    section_index: int = 0
    block_index: int = 0
    row_index: int | None = None
    cell_index: int | None = None
    paragraph_index: int | None = None
    run_index: int | None = None
    offset: int = 0


@dataclass
class DocxSelectionRange:
    anchor: DocxSelectionPoint = field(default_factory=DocxSelectionPoint)
    focus: DocxSelectionPoint = field(default_factory=DocxSelectionPoint)


@dataclass
class NativeDocxDocument:
    schema_version: int = 1
    source_format: str = "docx"
    title: str = "Document"
    path: str = ""
    sections: list[DocxSection] = field(default_factory=list)
    warnings: list[DocxWarning] = field(default_factory=list)
    active_selection: DocxSelectionRange | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NativeDocxDocument":
        sections = [section_from_dict(item) for item in payload.get("sections", [])]
        warnings = [warning_from_dict(item) for item in payload.get("warnings", [])]
        selection_payload = payload.get("active_selection")
        active_selection = (
            selection_range_from_dict(selection_payload) if isinstance(selection_payload, dict) else None
        )
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            source_format=str(payload.get("source_format", "docx")),
            title=str(payload.get("title", "Document")),
            path=str(payload.get("path", "")),
            sections=sections,
            warnings=warnings,
            active_selection=active_selection,
        )


def dataclass_to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {key: dataclass_to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [dataclass_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: dataclass_to_dict(item) for key, item in value.items()}
    return value


def warning_from_dict(payload: dict[str, Any]) -> DocxWarning:
    return DocxWarning(
        id=str(payload.get("id", "")),
        code=str(payload.get("code", "warning")),
        message=str(payload.get("message", "")),
        severity=str(payload.get("severity", "warning")),
    )


def run_from_dict(payload: dict[str, Any]) -> DocxRun:
    size_value = payload.get("font_size_pt")
    try:
        font_size_pt = float(size_value) if size_value is not None else None
    except (TypeError, ValueError):
        font_size_pt = None
    return DocxRun(
        id=str(payload.get("id", "")),
        text=str(payload.get("text", "")),
        bold=bool(payload.get("bold", False)),
        italic=bool(payload.get("italic", False)),
        underline=bool(payload.get("underline", False)),
        font_name=str(payload.get("font_name", "")),
        font_size_pt=font_size_pt,
        color=str(payload.get("color", "")),
        highlight=str(payload.get("highlight", "")),
    )


def paragraph_from_dict(payload: dict[str, Any]) -> DocxParagraph:
    runs = [run_from_dict(item) for item in payload.get("runs", [])]
    return DocxParagraph(
        id=str(payload.get("id", "")),
        style_name=str(payload.get("style_name", "Normal")),
        alignment=str(payload.get("alignment", "left")),
        list_kind=str(payload.get("list_kind", "none")),
        list_level=int(payload.get("list_level", 0) or 0),
        page_break_before=bool(payload.get("page_break_before", False)),
        keep_with_next=bool(payload.get("keep_with_next", False)),
        keep_together=bool(payload.get("keep_together", False)),
        runs=runs or [DocxRun(id=f"{payload.get('id', 'para')}-run-0", text="")],
    )


def cell_from_dict(payload: dict[str, Any]) -> DocxTableCell:
    paragraphs = [paragraph_from_dict(item) for item in payload.get("paragraphs", [])]
    return DocxTableCell(
        id=str(payload.get("id", "")),
        paragraphs=paragraphs or [DocxParagraph(id=f"{payload.get('id', 'cell')}-p-0")],
        column_span=int(payload.get("column_span", 1) or 1),
        row_span=int(payload.get("row_span", 1) or 1),
    )


def row_from_dict(payload: dict[str, Any]) -> DocxTableRow:
    cells = [cell_from_dict(item) for item in payload.get("cells", [])]
    return DocxTableRow(id=str(payload.get("id", "")), cells=cells)


def table_from_dict(payload: dict[str, Any]) -> DocxTable:
    rows = [row_from_dict(item) for item in payload.get("rows", [])]
    return DocxTable(
        id=str(payload.get("id", "")),
        rows=rows,
        style_name=str(payload.get("style_name", "Table Grid")),
    )


def image_from_dict(payload: dict[str, Any]) -> DocxImage:
    width_value = payload.get("width_px")
    height_value = payload.get("height_px")
    return DocxImage(
        id=str(payload.get("id", "")),
        alt_text=str(payload.get("alt_text", "")),
        filename=str(payload.get("filename", "")),
        content_type=str(payload.get("content_type", "")),
        base64_data=str(payload.get("base64_data", "")),
        width_px=int(width_value) if isinstance(width_value, (int, float)) else None,
        height_px=int(height_value) if isinstance(height_value, (int, float)) else None,
        anchor_kind=str(payload.get("anchor_kind", "inline")),
    )


def unsupported_from_dict(payload: dict[str, Any]) -> DocxUnsupportedBlock:
    return DocxUnsupportedBlock(
        id=str(payload.get("id", "")),
        label=str(payload.get("label", "Unsupported OOXML content")),
        xml_tag=str(payload.get("xml_tag", "")),
        xml_payload=str(payload.get("xml_payload", "")),
    )


def block_from_dict(payload: dict[str, Any]) -> DocxBlock:
    block_type = payload.get("type", "paragraph")
    if block_type == "table":
        return table_from_dict(payload)
    if block_type == "image":
        return image_from_dict(payload)
    if block_type == "page_break":
        return DocxPageBreak(id=str(payload.get("id", "")))
    if block_type == "unsupported":
        return unsupported_from_dict(payload)
    return paragraph_from_dict(payload)


def header_footer_from_dict(payload: dict[str, Any] | None) -> DocxHeaderFooter:
    if not isinstance(payload, dict):
        return DocxHeaderFooter()
    return DocxHeaderFooter(
        paragraphs=[paragraph_from_dict(item) for item in payload.get("paragraphs", [])]
    )


def section_properties_from_dict(payload: dict[str, Any] | None) -> DocxSectionProperties:
    if not isinstance(payload, dict):
        return DocxSectionProperties()
    return DocxSectionProperties(
        page_width_twips=_maybe_int(payload.get("page_width_twips")),
        page_height_twips=_maybe_int(payload.get("page_height_twips")),
        margin_top_twips=_maybe_int(payload.get("margin_top_twips")),
        margin_right_twips=_maybe_int(payload.get("margin_right_twips")),
        margin_bottom_twips=_maybe_int(payload.get("margin_bottom_twips")),
        margin_left_twips=_maybe_int(payload.get("margin_left_twips")),
        start_type=str(payload.get("start_type", "newPage")),
    )


def section_from_dict(payload: dict[str, Any]) -> DocxSection:
    blocks = [block_from_dict(item) for item in payload.get("blocks", [])]
    return DocxSection(
        id=str(payload.get("id", "")),
        properties=section_properties_from_dict(payload.get("properties")),
        header=header_footer_from_dict(payload.get("header")),
        footer=header_footer_from_dict(payload.get("footer")),
        blocks=blocks,
    )


def selection_point_from_dict(payload: dict[str, Any]) -> DocxSelectionPoint:
    return DocxSelectionPoint(
        section_index=int(payload.get("section_index", 0) or 0),
        block_index=int(payload.get("block_index", 0) or 0),
        row_index=_maybe_int(payload.get("row_index")),
        cell_index=_maybe_int(payload.get("cell_index")),
        paragraph_index=_maybe_int(payload.get("paragraph_index")),
        run_index=_maybe_int(payload.get("run_index")),
        offset=int(payload.get("offset", 0) or 0),
    )


def selection_range_from_dict(payload: dict[str, Any]) -> DocxSelectionRange:
    return DocxSelectionRange(
        anchor=selection_point_from_dict(payload.get("anchor", {})),
        focus=selection_point_from_dict(payload.get("focus", {})),
    )


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def make_warning(code: str, message: str, severity: Literal["info", "warning"] = "warning") -> DocxWarning:
    return DocxWarning(id=f"warning-{code}-{abs(hash(message))}", code=code, message=message, severity=severity)


def flatten_document_text(document: NativeDocxDocument) -> str:
    parts: list[str] = []
    for section in document.sections:
        for paragraph in section.header.paragraphs:
            text = flatten_paragraph_text(paragraph)
            if text:
                parts.append(text)
        for block in section.blocks:
            if isinstance(block, DocxParagraph):
                text = flatten_paragraph_text(block)
                if text:
                    parts.append(text)
            elif isinstance(block, DocxTable):
                for row in block.rows:
                    for cell in row.cells:
                        for paragraph in cell.paragraphs:
                            text = flatten_paragraph_text(paragraph)
                            if text:
                                parts.append(text)
            elif isinstance(block, DocxImage):
                if block.alt_text:
                    parts.append(block.alt_text)
            elif isinstance(block, DocxUnsupportedBlock):
                if block.label:
                    parts.append(f"[{block.label}]")
        for paragraph in section.footer.paragraphs:
            text = flatten_paragraph_text(paragraph)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def flatten_paragraph_text(paragraph: DocxParagraph) -> str:
    return "".join(run.text for run in paragraph.runs).strip()
