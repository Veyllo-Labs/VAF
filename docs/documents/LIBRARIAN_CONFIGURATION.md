# Librarian Agent Configuration Guide

## Overview

The VAF Librarian Agent now supports **user-configurable settings** for file size limits, auto-chunking, and more. This allows you to customize the agent's behavior to match your needs.

**Access scope (multi-user):** the librarian runs under a per-user filesystem jail. A remote (non-admin) user can read only their own `VAF_Projects/<uid[:8]>/`; the local admin / machine owner keeps full access; another user's data is never readable. See [USER_ISOLATION.md](../security/USER_ISOLATION.md#librarian-agent-vaftoolslibrarianpy-vaftoolsfilesystempy).

**No delete capability (deliberate):** the librarian never deletes files or folders - there is no delete tool and the sandbox cannot modify host files. Deletion tasks are refused with an explicit capability statement (never an error-styled message that would invite retries); deletion must be handled elsewhere with explicit user confirmation.

## Configuration File

Settings are stored in: **`~/.vaf/config.json`**

On Windows: `C:\Users\<YourName>\.vaf\config.json`

## Librarian-Specific Settings

### Default Configuration

```json
{
  "librarian_max_pdf_size_mb": 50,
  "librarian_max_doc_size_mb": 20,
  "librarian_max_excel_size_mb": 30,
  "librarian_max_text_size_kb": 500,
  "librarian_auto_chunk_large_files": true,
  "librarian_pdf_max_pages_preview": 50,
  "librarian_ocr_fallback_for_pdf": true
}
```

### Settings Explanation

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `librarian_max_pdf_size_mb` | Integer | `50` | Maximum PDF file size in megabytes |
| `librarian_max_doc_size_mb` | Integer | `20` | Maximum Word/PowerPoint size in MB |
| `librarian_max_excel_size_mb` | Integer | `30` | Maximum Excel spreadsheet size in MB |
| `librarian_max_text_size_kb` | Integer | `500` | Maximum text file size in kilobytes |
| `librarian_auto_chunk_large_files` | Boolean | `true` | Auto-chunk large files into readable sections |
| `librarian_pdf_max_pages_preview` | Integer | `50` | Maximum PDF pages to show in preview |
| `librarian_ocr_fallback_for_pdf` | Boolean | `true` | When a PDF has no embedded text (scanned PDF), try OCR if pdf2image and pytesseract are installed (requires system: poppler, Tesseract) |

## Usage Examples

### Example 1: Increase PDF Size Limit

**Scenario:** You need to read large PDF reports (up to 100 MB)

**Solution:** Edit `~/.vaf/config.json`:

```json
{
  "librarian_max_pdf_size_mb": 100
}
```

Now the Librarian can handle PDFs up to 100 MB.

### Example 2: Disable Auto-Chunking

**Scenario:** You want full control over chunking decisions

**Solution:** Edit config:

```json
{
  "librarian_auto_chunk_large_files": false
}
```

Now the agent will ask you explicitly before chunking large files.

### Example 3: Increase Text File Limits

**Scenario:** You work with large log files (up to 5 MB)

**Solution:**

```json
{
  "librarian_max_text_size_kb": 5000
}
```

### Example 4: Increase PDF Preview Pages

**Scenario:** You want to see up to 100 pages in previews

**Solution:**

```json
{
  "librarian_pdf_max_pages_preview": 100
}
```

## Auto-Chunking Feature

### What is Auto-Chunking?

When you try to read a large file that exceeds the configured size limit, the Librarian Agent can automatically:

1. **Detect** the file is too large
2. **Split** it into manageable chunks
3. **Read** each chunk separately
4. **Provide** a preview with navigation hints

### Example Output

```
[INFO] File is large (75.3 MB, max 50 MB direct read)

**Auto-Chunking Enabled:** Reading file in manageable sections...

### PDF Preview: large_report.pdf
**Total Pages:** 250
**Showing:** First 50 pages (preview)

**Page 1:** Lorem ipsum dolor sit amet...
**Page 2:** Consectetur adipiscing elit...
...
**Page 50:** Final preview page...

**Note:** This is a preview. The full document has 250 pages.
**Tip:** Ask me to 'read pages 10-20 of large_report.pdf' for specific sections.

**Configuration:**
- To change size limits, edit `~/.vaf/config.json`
- Current limit for .pdf files: 50 MB
- Auto-chunking: Enabled
```

### Benefits of Auto-Chunking

- **No context overflow** - Large files don't overwhelm the AI's context window
- **Faster processing** - Smaller chunks are processed more quickly
- **Better summaries** - Focused reading produces better insights
- **Navigation support** - Get previews and then deep-dive into specific sections

### Supported Formats for Chunking

- PDF files (`.pdf`)
- Text files (`.txt`, `.md`, `.log`)
- Data files (`.json`, `.xml`, `.csv`)

### How to Use Chunking

**Automatic (Default):**
```
User: "Read the file large_document.pdf"
```
The agent automatically chunks if the file is too large.

**Explicit Request:**
```
User: "Read the file report.pdf in chunks"
User: "Show me a preview of data.txt"
User: "Gib mir eine Vorschau von dokument.pdf"
```

**Specific Sections:**
```
User: "Read pages 10-20 of report.pdf"
User: "Show me the first 5 pages of document.pdf"
User: "Read the beginning of large_file.txt"
```

## Error Messages with Suggestions

### Enhanced Error Reporting

When a file is too large, you now get **detailed error messages** with actionable suggestions:

#### Before (Generic Error):
```
Error: File too large (5123KB). Max 100KB.
```

#### After (Enhanced Error):
```
[ERROR] File too large: 5.0 MB

**File:** large_document.pdf
**Size:** 5,123 KB (5.00 MB)
**Maximum:** 50 MB for .pdf files

**Suggestions:**
- Split the file into smaller parts
- Extract specific pages/sections you need
- For PDFs: Use a PDF editor to extract pages
- For Excel: Export specific sheets as separate files
- Compress the file if possible
```

### Smart Suggestions by File Type

**PDF Files:**
- Use PDF editors (Adobe, PDFtk, etc.) to extract pages
- Compress with PDF optimization tools
- Split into multiple smaller PDFs

**Word Documents:**
- Split into multiple documents by chapter
- Remove images/media to reduce size
- Use compression tools

**Excel Files:**
- Export specific sheets as separate files
- Remove unused columns/rows
- Use CSV format for data-only exports

**Text Files:**
- Split into multiple parts
- Use compression (zip, gzip)
- Extract only relevant sections

## Configuration Scenarios

### Scenario 1: Research & Academic Use

**Need:** Large PDFs, academic papers, books

```json
{
  "librarian_max_pdf_size_mb": 100,
  "librarian_max_doc_size_mb": 50,
  "librarian_max_text_size_kb": 2000,
  "librarian_auto_chunk_large_files": true,
  "librarian_pdf_max_pages_preview": 100
}
```

### Scenario 2: Software Development

**Need:** Large log files, documentation, code files

```json
{
  "librarian_max_text_size_kb": 5000,
  "librarian_max_pdf_size_mb": 30,
  "librarian_auto_chunk_large_files": true,
  "librarian_pdf_max_pages_preview": 30
}
```

### Scenario 3: Business & Office Work

**Need:** Reports, presentations, spreadsheets

```json
{
  "librarian_max_pdf_size_mb": 50,
  "librarian_max_doc_size_mb": 30,
  "librarian_max_excel_size_mb": 50,
  "librarian_max_text_size_kb": 1000,
  "librarian_auto_chunk_large_files": true,
  "librarian_pdf_max_pages_preview": 50
}
```

### Scenario 4: Data Analysis

**Need:** Large CSV, JSON, Excel files

```json
{
  "librarian_max_excel_size_mb": 100,
  "librarian_max_text_size_kb": 10000,
  "librarian_auto_chunk_large_files": true,
  "librarian_pdf_max_pages_preview": 20
}
```

### Scenario 5: Conservative/Low Memory

**Need:** Smaller limits for slower systems

```json
{
  "librarian_max_pdf_size_mb": 20,
  "librarian_max_doc_size_mb": 10,
  "librarian_max_excel_size_mb": 15,
  "librarian_max_text_size_kb": 200,
  "librarian_auto_chunk_large_files": true,
  "librarian_pdf_max_pages_preview": 20
}
```

## How to Edit Configuration

### Method 1: Manual Editing

1. Open the config file in a text editor:
   ```bash
   # Windows
   notepad %USERPROFILE%\.vaf\config.json
   
   # Linux/macOS
   nano ~/.vaf/config.json
   ```

2. Add or modify the librarian settings:
   ```json
   {
     "model": "auto",
     "librarian_max_pdf_size_mb": 100,
     "librarian_auto_chunk_large_files": true
   }
   ```

3. Save and close the file

4. Restart VAF or reload config (automatic on next use)

### Method 2: Via VAF Settings Menu (Future Enhancement)

```
vaf run → S (Settings) → Librarian Settings → File Size Limits
```

*(This feature is planned for a future release)*

## Best Practices

### 1. Start with Defaults

The default settings work well for most use cases. Only adjust if you have specific needs.

### 2. Monitor Memory Usage

Larger file limits require more RAM:
- **50 MB PDFs**: ~200-300 MB RAM
- **100 MB PDFs**: ~400-500 MB RAM
- **Large Excel files**: Can use 2-3x the file size in RAM

### 3. Use Auto-Chunking

Keep `librarian_auto_chunk_large_files` enabled for best results with large documents.

### 4. Adjust Preview Pages

Balance between detail and performance:
- **20 pages**: Fast, good for quick overviews
- **50 pages**: Default, good balance
- **100 pages**: Detailed, slower processing

### 5. Test Your Changes

After modifying settings, test with a sample file:
```
User: "Read the test file my_large_document.pdf"
```

## Troubleshooting

### Problem: Settings Not Applied

**Solution:**
1. Check JSON syntax (no trailing commas, proper quotes)
2. Restart VAF
3. Check file location: `~/.vaf/config.json`

### Problem: File Still Too Large

**Solution:**
1. Increase the relevant size limit in config
2. Enable auto-chunking if disabled
3. Split the file into smaller parts

### Problem: Out of Memory Errors

**Solution:**
1. Reduce size limits in config
2. Close other applications
3. Use auto-chunking for large files

### Problem: Slow Performance

**Solution:**
1. Reduce `librarian_pdf_max_pages_preview`
2. Lower file size limits
3. Use chunking for large files

## Technical Details

### Memory Usage Estimates

| File Size | Estimated RAM Usage | Recommended System RAM |
|-----------|-------------------|----------------------|
| 10 MB | ~50 MB | 4 GB |
| 50 MB | ~250 MB | 8 GB |
| 100 MB | ~500 MB | 16 GB |
| 200 MB | ~1 GB | 32 GB |

### Processing Speed

| File Type | Typical Speed | Notes |
|-----------|--------------|-------|
| PDF (text) | ~1 MB/s | Faster for simple text |
| PDF (complex) | ~200 KB/s | Slower for scanned/complex layouts |
| Word | ~2 MB/s | Fast extraction |
| Excel | ~500 KB/s | Slower with formulas |
| Text | ~10 MB/s | Very fast |

### Chunking Strategy

**PDF Files:**
- Chunks of 20 pages per preview
- First N pages shown (configurable)
- Navigation hints provided

**Text Files:**
- Chunks of 10,000 characters
- Shows first chunk as preview
- Offers search functionality

## Related Documentation

- [Document Reading Guide](DOCUMENT_READING.md) - General usage
- [VAF Configuration](../../README.md#configuration) - Main config
- [Sub-Agent IPC](../agents/SUBAGENT_IPC.md) - Sub-agent settings

## Example Workflows

### Workflow 1: Reading a Large Research Paper

```
User: "Read the research paper large_study.pdf"

Agent: [INFO] File is large (85.2 MB, max 50 MB direct read)
       Auto-Chunking Enabled: Reading file in manageable sections...
       
       [Shows preview of first 50 pages]
       
       Tip: Ask me to 'read pages 75-100' for the methodology section.

User: "Read pages 75-100 of large_study.pdf"

Agent: [Reads and displays specific section]
```

### Workflow 2: Analyzing Excel Data

```
User: "Show me the sales data from Q4_report.xlsx"

Agent: [Reads Excel file with 3 sheets shown]
       
       Sheet: Summary, Revenue, Expenses
       [Shows first 50 rows of each]

User: "What's in the Revenue sheet?"

Agent: [Provides detailed summary of Revenue data]
```

### Workflow 3: Configuration Adjustment

```
User: "The PDF limit is too small for my needs"

Agent: You can increase the PDF size limit:
       
       1. Edit ~/.vaf/config.json
       2. Add: "librarian_max_pdf_size_mb": 100
       3. Save and restart VAF
       
       This will allow PDFs up to 100 MB.
```

## Conclusion

The enhanced Librarian Agent configuration system provides:

**Flexibility** - Adjust limits to your needs
**Intelligence** - Auto-chunking for large files
**Clarity** - Detailed error messages with suggestions
**Performance** - Optimized settings for different use cases
**Usability** - Easy JSON-based configuration

Configure the Librarian Agent to work exactly how you need it!

For questions or issues, see the main [Document Reading Guide](DOCUMENT_READING.md).
