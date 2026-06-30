# Sub-Agent File Path Extraction

## Overview

Automatically extract file paths from sub-agent results so the main agent can immediately access generated files.

## Problem

**Before:**
```
Sub-Agent Result:
Saved to: C:\Users\...\research_report.html

User: "Can you take a look at the file?"

Agent: Confused → asks for the file path (even though it is already there!)
```

**Now:**
```
Sub-Agent Result:
Saved to: C:\Users\...\research_report.html

🔗 **EXTRACTED FILE PATHS (from Sub-Agent output):**
- C:\Users\...\research_report.html

💡 TIP: Use read_file('...') or librarian_agent(file='...')

User: "Can you take a look at the file?"

Agent: read_file("C:\Users\...\research_report.html")
```

## Implementation

### 1. Automatic extraction (`vaf/core/agent.py`)

```python
def _process_subagent_result(self, task):
    # Extract file paths from result
    file_paths = re.findall(
        r'(?:Saved to|Output|File|Path|Ausgabe|Datei):\s*([^\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?))',
        task.result,
        re.IGNORECASE
    )
    
    if file_paths:
        file_hint = "\n\n🔗 **EXTRACTED FILE PATHS:**\n"
        for fp in cleaned_paths[:3]:
            file_hint += f"- `{fp}`\n"
        file_hint += (
            "\n💡 **TIP:** To read/analyze this file, use:\n"
            f"- `read_file('{cleaned_paths[0]}')` for quick reading\n"
            f"- `librarian_agent(file='{cleaned_paths[0]}', task='...')` for detailed analysis\n"
        )
```

### 2. System Prompt Guidelines (`vaf/core/system_prompt.py`)

```markdown
### Extracting File Paths from Context
**CRITICAL:** When sub-agent results mention file paths, EXTRACT and USE them directly!

**Common patterns:**
- "Saved to: [path]"
- "Output: [path]"
- "Ausgabe: [path]"

**Best Practice:** Look for "🔗 EXTRACTED FILE PATHS" section!
```

### 3. Tool Descriptions (`vaf/tools/filesystem.py`)

```python
description = """
Use this when:
- Sub-agent created a file (look for "🔗 EXTRACTED FILE PATHS"!)
- User asks to read a file

**IMPORTANT:** If sub-agent just created a file, 
the path is already in the conversation context!
"""
```

## Supported Patterns

### Recognized keywords:
- **English:** "Saved to", "Output", "File", "Path"
- **German:** "Ausgabe", "Datei"

### Supported file types:
- `.html`, `.htm` (Research Reports)
- `.pdf` (Documents)
- `.docx`, `.doc` (Word Documents)
- `.txt`, `.md` (Text/Markdown)
- `.json`, `.csv` (Data)
- `.xlsx`, `.xls` (Excel)

### Supported paths:
- Windows: `C:\Users\...\file.html`
- Linux: `/home/user/file.html`
- macOS: `/Users/user/file.html`

## Use Cases

### Use Case 1: Coding sub-agent → read the file directly

```
1. User: "Create a JSON file with sample values"
2. coding_agent creates the file → output_data.json
3. System Message:
   🔗 **EXTRACTED FILE PATHS (from Sub-Agent output):**
   - C:\Users\...\output_data.json
   💡 TIP: read_file('C:\Users\...\output_data.json')
4. User: "Show me the contents"
5. Agent: read_file("C:\Users\...\output_data.json")
```

### Use Case 2: Hints for Research/Document sub-agents

```
1. User starts `research_agent` or `document_agent`
2. The result is processed as a document-oriented follow-up hint (editor/viewer flow)
3. The usual "EXTRACTED FILE PATHS" block is not necessarily present
4. For these agent types, the main agent should follow the document-specific hints
```

### Use Case 3: Multiple files

```
Sub-Agent Result:
Generated 3 files:
- File: /home/user/report.pdf
- Output: /home/user/summary.txt
- Saved to: /home/user/data.json

System Message:
🔗 **EXTRACTED FILE PATHS (from Sub-Agent output):**
- /home/user/report.pdf
- /home/user/summary.txt
- /home/user/data.json

💡 TIP: read_file('/home/user/report.pdf')
```

## Testing

Run the project test suite for sub-agent result handling (no standalone `test_file_path_extraction.py` in this repository).

**Test Coverage:**
- Generic sub-agent result parsing
- Multiple Files
- Linux/macOS Paths
- Windows Paths
- No File Path (negative test)
- System Message Generation

## Benefits

### Before:
- Agent asks for the file path (even though it is already there)
- User has to copy the path manually
- Multiple interactions needed

### After:
- Agent detects the path automatically
- Direct use of `read_file` or `librarian_agent`
- A single interaction is enough

## Implementation Details

### Regex Pattern
```python
r'(?:Saved to|Output|File|Path|Ausgabe|Datei):\s*([^\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?))'
```

**How it works:**
1. `(?:Saved to|Output|...)` - Looks for keywords (non-capturing group)
2. `:\s*` - Match `:` followed by optional whitespace
3. `([^\n]+` - Capture everything up to the end of the line
4. `\.(?:html?|pdf|...)` - Match only known file types
5. `re.IGNORECASE` - Case-insensitive matching

### Cleaning
```python
cleaned_paths = [re.sub(r'\x1b\[[0-9;]*m', '', fp).strip() for fp in file_paths]
```
- Strips ANSI color codes
- Trims whitespace
- Limits to the first 3 files

## Related Features

- **Document Reading:** See `docs/documents/DOCUMENT_READING.md`
- **Research Workflows:** See `docs/agents/RESEARCH_AND_DOCUMENT_WORKFLOWS.md`
- **Sub-Agent IPC:** See `docs/agents/SUBAGENT_IPC.md`
- **Librarian Configuration:** See `docs/documents/LIBRARIAN_CONFIGURATION.md`

## Notes

- Paths are **not validated** (an existence check happens at read time)
- Limited to **3 files** in the system message (to conserve context)
- Mainly for sub-agent types with classic text-result output (e.g. coding/librarian-style outputs)
- Supports **Windows, Linux, macOS** paths
