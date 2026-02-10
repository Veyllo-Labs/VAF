# VAF Document Reading Support

## Overview

The VAF Librarian Agent now supports reading multiple document formats including PDF, Word, Excel, and PowerPoint files. This enhancement allows users to extract text content from various document types seamlessly.

### Key Features

✅ **Multiple Formats** - PDF, Word, Excel, PowerPoint, and text files
✅ **Auto-Chunking** - Automatically handles large files without context overflow
✅ **Configurable Limits** - Adjust size limits per file type
✅ **Smart Navigation** - Jump to specific pages or sections
✅ **Cross-Platform** - Works on Windows, macOS, and Linux
✅ **Multilingual** - Supports German, English, Turkish, and more
✅ **Auto-Install** - Dependencies checked and installed automatically on first start

### Quick Start

**Install (if not auto-installed):**
```bash
pip install -r requirements.txt
```

**Read a document:**
```
User: "Read the file document.pdf"
User: "Lies die Datei dokument.pdf"
```

**Read large files (auto-chunking):**
```
User: "Read large_report.pdf"  # Automatically chunks if > 50 MB
User: "Read pages 10-20 of large_report.pdf"  # Navigate to specific sections
```

**Configure limits:**
```bash
# Edit ~/.vaf/config.json
{
  "librarian_max_pdf_size_mb": 100,
  "librarian_auto_chunk_large_files": true
}
```

## Supported Formats

| Format | Extensions | Description | Default Max Size | Configurable |
|--------|-----------|-------------|------------------|--------------|
| **PDF** | `.pdf` | Adobe PDF documents | 50 MB | ✅ Yes |
| **Word** | `.docx` | Microsoft Word documents | 20 MB | ✅ Yes |
| **Excel** | `.xlsx`, `.xls` | Microsoft Excel spreadsheets | 30 MB | ✅ Yes |
| **PowerPoint** | `.pptx` | Microsoft PowerPoint presentations | 20 MB | ✅ Yes |
| **Text** | `.txt`, `.md`, `.json`, `.xml`, `.csv`, etc. | Plain text files | 500 KB | ✅ Yes |

**Note:** All size limits are configurable via `~/.vaf/config.json`. See [Configuration](#configuration) section below.

## Installation

To use document reading features, install the required dependencies:

```bash
pip install -r requirements.txt
```

Or install specific packages individually:

```bash
pip install PyPDF2>=3.0.0          # For PDF support
pip install python-docx>=1.1.0     # For Word documents
pip install openpyxl>=3.1.0        # For Excel files
pip install python-pptx>=0.6.21    # For PowerPoint files
```

## Usage Examples

### Basic Document Reading

#### Reading a PDF File

You can pass a normal path or a **file URL** (e.g. from a browser or file manager):

```
User: "Read the file C:\Users\mert\Documents\mitgliedschaftsbescheinigung.pdf"
User: "Open file:///C:/Users/mert1/Downloads/20251110_075336_Bewilligungsbescheid.pdf"
```

The document_viewer tool and Librarian accept `file:///` URLs and convert them to the correct path on your OS. The librarian will:
1. Detect it's a PDF file
2. Extract text from all pages (up to 50 pages)
3. Return formatted content with page numbers

**Output:**
```
### PDF: mitgliedschaftsbescheinigung.pdf
**Pages:** 5

--- Page 1 ---
[PDF content from page 1]

--- Page 2 ---
[PDF content from page 2]
...
```

#### Reading a Word Document

```
User: "Show me the content of my report.docx"
```

The librarian will:
1. Extract all paragraphs
2. Extract tables (up to 5 tables shown)
3. Return formatted content

**Output:**
```
### Word Document: report.docx
**Paragraphs:** 25
**Tables:** 3

[Paragraph 1 content]
[Paragraph 2 content]
...

--- Tables ---
Table 1:
Header1 | Header2 | Header3
Value1  | Value2  | Value3
...
```

#### Reading an Excel File

```
User: "Read the spreadsheet data.xlsx"
```

The librarian will:
1. List all sheet names
2. Extract data from first 3 sheets
3. Show up to 50 rows and 20 columns per sheet

**Output:**
```
### Excel File: data.xlsx
**Sheets:** Sheet1, Sheet2, Sheet3

--- Sheet: Sheet1 ---
Column1 | Column2 | Column3
Value1  | Value2  | Value3
...
```

#### Reading a PowerPoint File

```
User: "Show me the presentation slides.pptx"
```

The librarian will:
1. Extract text from all shapes on each slide
2. Show up to 20 slides
3. Return formatted content

**Output:**
```
### PowerPoint: slides.pptx
**Slides:** 15

--- Slide 1 ---
Title Text
Bullet Point 1
Bullet Point 2

--- Slide 2 ---
...
```

### Working with Large Files (Chunking)

#### Example 1: Large PDF Report (Auto-Chunking)

```
User: "Read the annual_report_2025.pdf" (85 MB file, 300 pages)

Agent: [INFO] File is large (85.3 MB, max 50 MB direct read)
       
       **Auto-Chunking Enabled:** Reading in manageable sections...
       
       ### PDF Preview: annual_report_2025.pdf
       **Total Pages:** 300
       **Showing:** First 20 pages (preview)
       
       **Page 1:** Annual Report 2025 - Executive Summary...
       **Page 2:** Letter from the CEO...
       ...
       **Page 20:** Financial Highlights (continued)...
       
       **Note:** This is a preview. The full document has 300 pages.
       **Tip:** Ask me to 'read pages 50-75' for detailed financial section.
       **Tip:** Or ask 'summarize pages 1-20' for a quick overview.

User: "Read pages 100-120 of annual_report_2025.pdf"

Agent: ### PDF: annual_report_2025.pdf (Pages 100-120)
       
       --- Page 100 ---
       [Detailed content from page 100]
       
       --- Page 101 ---
       [Detailed content from page 101]
       ...
```

#### Example 2: Requesting Specific Sections

```
User: "Show me the introduction section from large_document.pdf"

Agent: [Reads first 20 pages, finds introduction]
       
       ### Introduction (Pages 5-15)
       [Introduction content]

User: "What about the conclusion?"

Agent: [The agent can ask you which pages, or you can specify]
       "The conclusion is typically at the end. Try: 'read pages 280-300'"
```

#### Example 3: Large Text File Analysis

```
User: "Analyze the log file system.log" (2 MB text file)

Agent: [INFO] File is large (2.1 MB, max 500 KB direct read)
       
       **Auto-Chunking Enabled:** Reading in sections...
       
       ### Text File Preview: system.log
       **Showing:** First 10,000 characters
       
       [2025-01-13 00:00:01] System startup...
       [2025-01-13 00:00:05] Service initialized...
       ...
       [2025-01-13 00:15:23] Checkpoint reached...
       
       ... (file continues)
       
       **Tip:** Ask me to 'search for ERROR in system.log' to find issues.
       **Tip:** Or 'read next section of system.log' to continue.
```

## Features

### Automatic Format Detection

The system automatically detects file types based on file extensions and uses the appropriate parser:

- `.pdf` → PyPDF2 PDF reader
- `.docx` → python-docx Word reader
- `.xlsx`, `.xls` → openpyxl Excel reader
- `.pptx` → python-pptx PowerPoint reader
- Others → Plain text reader (UTF-8 with error handling)

### Smart Content Limiting & Auto-Chunking

The Librarian Agent automatically handles large files to prevent context overflow:

#### Standard Limits (Direct Reading)
- **PDFs**: First 50 pages shown (configurable)
- **Word**: All paragraphs + first 5 tables (10 rows each)
- **Excel**: First 3 sheets (50 rows × 20 columns each)
- **PowerPoint**: First 20 slides
- **Text**: First 5,000 characters

#### Auto-Chunking for Large Files

When a file **exceeds the configured size limit**, the Librarian automatically switches to **chunking mode**:

**How it works:**
1. **Detection**: File size checked against configured limits
2. **Preview Generation**: First section extracted (e.g., first 20 pages for PDFs)
3. **Navigation Hints**: Suggestions for reading specific sections
4. **On-Demand Details**: User can request specific pages/sections

**Example Chunking Output:**
```
[INFO] File is large (75.3 MB, max 50 MB direct read)

**Auto-Chunking Enabled:** Reading file in manageable sections...

### PDF Preview: large_report.pdf
**Total Pages:** 250
**Showing:** First 20 pages (preview)

**Page 1:** Executive Summary...
**Page 2:** Introduction...
...
**Page 20:** Market Analysis...

**Note:** This is a preview. The full document has 250 pages.
**Tip:** Ask me to 'read pages 10-20 of large_report.pdf' for specific sections.

**Configuration:**
- To change size limits, edit `~/.vaf/config.json`
- Current limit for .pdf files: 50 MB
- Auto-chunking: Enabled
```

**Benefits:**
- ✅ **No Context Overflow**: Large files don't overwhelm the AI
- ✅ **Faster Processing**: Smaller chunks process more quickly
- ✅ **Better Summaries**: Focused reading produces better insights
- ✅ **Interactive Navigation**: Get overview first, then dive into details

**Supported Formats for Auto-Chunking:**
- ✅ PDF files (`.pdf`) - Page-based chunking
- ✅ Text files (`.txt`, `.md`, `.log`) - Character-based chunking
- ✅ Data files (`.json`, `.xml`, `.csv`) - Intelligent chunking

Content is automatically truncated with helpful messages if it exceeds limits.

### Error Handling

If a document parsing library is not installed, the system provides clear error messages:

```
Error: PDF support not installed. Run: pip install PyPDF2
```

### Cross-Platform Compatibility

All document reading features work consistently across:
- ✅ Windows
- ✅ macOS
- ✅ Linux

The implementation uses `pathlib` and platform-independent file handling.

## Technical Details

### PDF Reading (PyPDF2)

```python
import PyPDF2
with open(file_path, 'rb') as f:
    pdf_reader = PyPDF2.PdfReader(f)
    for page in pdf_reader.pages:
        text = page.extract_text()
```

**Limitations:**
- **Scanned PDFs (image-only):** If PyPDF2 extracts little or no text, the Librarian tries OCR when `librarian_ocr_fallback_for_pdf` is enabled (default: true). Requires optional deps: `pip install pdf2image pytesseract` and system tools: **poppler** (for pdf2image), **Tesseract** (for pytesseract). Install e.g. German with Tesseract for best results on German documents.
- Complex layouts may have formatting issues
- Password-protected PDFs are not supported

### Word Reading (python-docx)

```python
from docx import Document
doc = Document(file_path)
for para in doc.paragraphs:
    text = para.text
```

**Features:**
- Extracts paragraphs in order
- Extracts tables with row/column structure
- Preserves basic text content (formatting not preserved)

**Limitations:**
- Only `.docx` format supported (not `.doc`)
- Images and charts are not extracted
- Complex formatting is not preserved

### Excel Reading (openpyxl)

```python
import openpyxl
wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
for sheet in wb.worksheets:
    for row in sheet.iter_rows(values_only=True):
        # Process row
```

**Features:**
- Reads all sheets
- Supports formulas (shows calculated values)
- Handles multiple data types

**Limitations:**
- Formulas show values only (not formula text)
- Charts and images are not extracted
- Macros are not executed

### PowerPoint Reading (python-pptx)

```python
from pptx import Presentation
prs = Presentation(file_path)
for slide in prs.slides:
    for shape in slide.shapes:
        text = shape.text
```

**Features:**
- Extracts text from all text boxes
- Preserves slide order
- Handles multiple text shapes per slide

**Limitations:**
- Images and charts are not extracted
- Formatting is not preserved
- Speaker notes are not extracted

## Integration with Tools

### ReadFileTool (Direct Tool)

The `read_file` tool in `vaf/tools/filesystem.py` now supports all document formats:

```python
from vaf.tools.filesystem import ReadFileTool

tool = ReadFileTool()
result = tool.run(path="document.pdf")
print(result)
```

### Librarian Agent (LLM-Powered)

The Librarian Agent (`vaf/tools/librarian.py`) uses the enhanced `read_file` method internally:

```python
from vaf.tools.librarian import LibrarianTool

librarian = LibrarianTool()
result = librarian.run(task="Read the PDF file report.pdf")
print(result)
```

The agent automatically:
1. Detects file type from the task description
2. Extracts the file path
3. Calls the appropriate reader
4. Formats and returns the result

## Multilingual Support

The Librarian Agent understands document reading requests in multiple languages:

| Language | Example Request |
|----------|----------------|
| **English** | "Read the PDF file document.pdf" |
| **German** | "Lies die PDF-Datei dokument.pdf" |
| **Turkish** | "PDF dosyasını oku: döküman.pdf" |

## Chunking Strategies

### When to Use Chunking

**Auto-Chunking activates automatically** when files exceed configured limits. You can also **explicitly request** chunking:

```
User: "Give me a preview of large_file.pdf"
User: "Show me a chunk of data.txt"
User: "Gib mir eine Vorschau von dokument.pdf"
```

### Chunking vs. Direct Reading

| Feature | Direct Reading | Chunked Reading |
|---------|---------------|-----------------|
| **Speed** | Fast (single operation) | Moderate (multiple operations possible) |
| **Memory** | Higher (full file in memory) | Lower (only chunks in memory) |
| **Context** | Full document context | Section-focused context |
| **Navigation** | Limited | Flexible (jump to sections) |
| **Use Case** | Small-medium files | Large files, targeted reading |

### Best Practices for Large Files

#### 1. Start with Preview
```
User: "Preview the large_report.pdf"
# Get overview first, then dive into details
```

#### 2. Navigate to Sections
```
User: "Read pages 50-75 of report.pdf"
# Jump directly to relevant sections
```

#### 3. Search Instead of Reading
```
User: "Search for 'budget' in financial_report.pdf"
# Find specific information quickly
```

#### 4. Combine with Summarization
```
User: "Summarize pages 1-50 of report.pdf"
# Get key points without full text
```

### Chunking Limitations

**What Chunking Can Do:**
- ✅ Handle files larger than configured limits
- ✅ Provide navigable previews
- ✅ Reduce memory usage
- ✅ Speed up targeted reading

**What Chunking Cannot Do:**
- ❌ Provide full document analysis in one pass
- ❌ Maintain cross-references between distant sections
- ❌ Process files infinitely large (practical limits still apply)

**Workarounds:**
- For full analysis: Read in sections, then combine insights
- For cross-references: Read multiple specific sections
- For very large files: Use external tools to split first

## Troubleshooting

### Missing Dependencies

**Problem:** Error message about missing package

**Solution:** Install the required package:
```bash
pip install PyPDF2        # For PDF
pip install python-docx   # For Word
pip install openpyxl      # For Excel
pip install python-pptx   # For PowerPoint
```

### File Too Large

**Problem:** "File too large" error

**Solution:** The default limits are (configurable):
- PDF: 50 MB
- Word/PowerPoint: 20 MB
- Excel: 30 MB
- Text files: 500 KB

**Options:**
1. **Auto-Chunking** (automatic): System will chunk large files automatically
2. **Increase Limits**: Edit `~/.vaf/config.json` to increase size limits
3. **Split Files**: Divide large files into smaller parts
4. **Extract Sections**: Extract only needed pages/sheets

**Example Configuration:**
```json
{
  "librarian_max_pdf_size_mb": 100,
  "librarian_auto_chunk_large_files": true
}
```

See [Librarian Configuration](LIBRARIAN_CONFIGURATION.md) for details.

### Corrupted File

**Problem:** Error reading file

**Solution:** 
1. Verify the file opens in its native application
2. Try re-saving the file
3. Check for password protection or encryption

### Encoding Issues

**Problem:** Garbled text in output

**Solution:** The system uses UTF-8 encoding with error replacement. If issues persist, the file may use a non-standard encoding.

## Performance Considerations

### Speed

| Format | Typical Speed | Notes |
|--------|--------------|-------|
| PDF | ~500 KB/s | Varies by complexity |
| Word | ~1 MB/s | Fast extraction |
| Excel | ~200 KB/s | Slower with formulas |
| PowerPoint | ~500 KB/s | Medium speed |
| Text | ~5 MB/s | Very fast |

### Memory Usage

- **Small files (<1 MB)**: Minimal memory usage
- **Large files (5-10 MB)**: ~50-100 MB RAM
- **Very large files**: May cause performance issues

## Best Practices

### For Users

1. **Keep files organized**: Use descriptive filenames
2. **Provide full paths**: Be specific about file locations
3. **Check file sizes**: Stay within limits for best performance
4. **Use native apps for complex documents**: For documents with complex formatting, use native applications

### For Developers

1. **Always check file existence** before reading
2. **Handle exceptions gracefully**
3. **Truncate large outputs** to prevent token overflow
4. **Use read-only mode** for better performance (Excel)
5. **Close file handles properly** (use context managers)

## Future Enhancements

Planned improvements:

- [x] OCR fallback for scanned PDFs (optional: pdf2image + pytesseract + poppler + Tesseract)
- [ ] Image extraction from documents
- [ ] Support for older formats (.doc, .ppt, .xls)
- [ ] Password-protected file support
- [ ] Multi-file batch processing
- [ ] Document conversion (PDF → Word, etc.)
- [ ] Advanced table parsing (CSV export)
- [ ] Metadata extraction (author, date, etc.)

## Configuration

The Librarian Agent supports **user-configurable settings** for file size limits and behavior:

### Quick Configuration

Edit `~/.vaf/config.json`:

```json
{
  "librarian_max_pdf_size_mb": 50,
  "librarian_max_doc_size_mb": 20,
  "librarian_max_excel_size_mb": 30,
  "librarian_max_text_size_kb": 500,
  "librarian_auto_chunk_large_files": true,
  "librarian_pdf_max_pages_preview": 50
}
```

**See detailed configuration guide:** [Librarian Configuration](LIBRARIAN_CONFIGURATION.md)

### Auto-Chunking for Large Files

The Librarian automatically chunks large files that exceed configured size limits.

#### When Does Auto-Chunking Activate?

Auto-chunking activates when a file exceeds the configured maximum size for its type:

| File Type | Default Limit | When Auto-Chunking Activates |
|-----------|---------------|------------------------------|
| PDF | 50 MB | File size > 50 MB |
| Word/PowerPoint | 20 MB | File size > 20 MB |
| Excel | 30 MB | File size > 30 MB |
| Text | 500 KB | File size > 500 KB |

**Note:** These limits are fully configurable. See [Librarian Configuration](LIBRARIAN_CONFIGURATION.md).

#### Example Workflow

**Scenario:** Reading a 75 MB PDF with 250 pages

```
User: "Read the file large_report.pdf"

Agent: [INFO] File is large (75.3 MB, max 50 MB direct read)
       
       **Auto-Chunking Enabled:** Reading in manageable sections...
       
       ### PDF Preview: large_report.pdf
       **Total Pages:** 250
       **Showing:** First 20 pages (preview)
       
       **Page 1:** Executive Summary - Q4 2025...
       **Page 2:** Financial Overview - Revenue increased...
       ...
       **Page 20:** Market Analysis continues...
       
       **Tip:** Ask me to 'read pages 50-70 of large_report.pdf' for specific sections.
```

**User can then navigate:**
```
User: "Read pages 100-120 of large_report.pdf"
User: "Show me the conclusion section (pages 230-250)"
User: "Search for 'revenue' in large_report.pdf"
```

#### How Chunking Works

**For PDF Files:**
1. Extracts first N pages (default: 20, configurable)
2. Shows 200 characters preview per page
3. Provides total page count
4. Suggests navigation commands

**For Text Files:**
1. Reads first 10,000 characters
2. Indicates file continues
3. Offers search functionality

**For All Files:**
- Original file remains intact
- No temporary files created
- Full content available on request
- Smart navigation suggestions

## Related Documentation

- [Librarian Configuration](LIBRARIAN_CONFIGURATION.md) - Configure file size limits and behavior ⭐ **NEW**
- [VAF Sub-Agent IPC System](SUBAGENT_IPC.md) - How sub-agents communicate
- [VAF Speech Features](SPEECH_FEATURES.md) - Voice interaction with documents
- [VAF Context Management](CONTEXT_MANAGEMENT.md) - Handling large documents

## Examples

### Example 1: Analyzing PDF Reports

```
User: "Summarize the main points from the quarterly_report.pdf"

Librarian Agent:
1. Detects PDF file request
2. Reads all pages
3. Extracts text content
4. Returns formatted content for LLM to summarize
```

### Example 2: Extracting Excel Data

```
User: "Show me the sales data from Q4_sales.xlsx"

Librarian Agent:
1. Opens Excel file
2. Lists all sheets
3. Extracts data from relevant sheets
4. Formats as readable text
```

### Example 3: Reading Presentation Slides

```
User: "What's in the presentation.pptx?"

Librarian Agent:
1. Opens PowerPoint file
2. Extracts text from all slides
3. Shows slide-by-slide content
4. Returns formatted output
```

## Conclusion

The VAF document reading enhancement makes it easy to work with various file formats. The Librarian Agent can now seamlessly read and extract content from PDFs, Word documents, Excel spreadsheets, and PowerPoint presentations, making VAF a powerful tool for document analysis and information retrieval.

For questions or issues, please refer to the main README or open an issue on the project repository.
