# Sub-Agent File Path Extraction

## 📋 Overview

Automatische Extraktion von Dateipfaden aus Sub-Agent Results, damit der Main Agent sofort auf generierte Dateien zugreifen kann.

## 🎯 Problem

**Vorher:**
```
Sub-Agent Result:
📄 Saved to: C:\Users\...\research_report.html

User: "Kannst du die Datei ansehen?"

Agent: ❌ Verwirrt → fragt nach Dateipfad (obwohl er schon da ist!)
```

**Jetzt:**
```
Sub-Agent Result:
📄 Saved to: C:\Users\...\research_report.html

🔗 EXTRACTED FILE PATHS:
- C:\Users\...\research_report.html

💡 TIP: Use read_file('...') or librarian_agent(file='...')

User: "Kannst du die Datei ansehen?"

Agent: ✅ read_file("C:\Users\...\research_report.html")
```

## 🔧 Implementation

### 1. Automatische Extraktion (`vaf/core/agent.py`)

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
### 🔍 Extracting File Paths from Context
**CRITICAL:** When sub-agent results mention file paths, EXTRACT and USE them directly!

**Common patterns:**
- "📄 Saved to: [path]"
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

## 📊 Supported Patterns

### Erkannte Schlüsselwörter:
- **English:** "Saved to", "Output", "File", "Path"
- **German:** "Ausgabe", "Datei"

### Unterstützte Dateitypen:
- `.html`, `.htm` (Research Reports)
- `.pdf` (Documents)
- `.docx`, `.doc` (Word Documents)
- `.txt`, `.md` (Text/Markdown)
- `.json`, `.csv` (Data)
- `.xlsx`, `.xls` (Excel)

### Unterstützte Pfade:
- ✅ Windows: `C:\Users\...\file.html`
- ✅ Linux: `/home/user/file.html`
- ✅ macOS: `/Users/user/file.html`

## 🎯 Use Cases

### Use Case 1: Research Agent → Zusammenfassung

```
1. User: "Recherchiere Quantum Computing"
2. research_agent erstellt Report → research_quantum_20260113.html
3. System Message:
   🔗 EXTRACTED FILE PATHS:
   - C:\Users\...\research_quantum_20260113.html
   💡 TIP: read_file('C:\Users\...\research_quantum_20260113.html')
4. User: "Fasse das zusammen"
5. Agent: ✅ read_file("C:\Users\...\research_quantum_20260113.html")
   → librarian_agent(file="...", task="Summarize")
```

### Use Case 2: Document Agent → Review

```
1. User: "Erstelle Arbeitsvertrag"
2. document_agent erstellt → Arbeitsvertrag_2026.docx
3. System Message:
   🔗 EXTRACTED FILE PATHS:
   - D:\Dokumente\Arbeitsvertrag_2026.docx
4. User: "Prüfe den Vertrag"
5. Agent: ✅ read_file("D:\Dokumente\Arbeitsvertrag_2026.docx")
   → Analyzes content
```

### Use Case 3: Mehrere Dateien

```
Sub-Agent Result:
Generated 3 files:
- File: /home/user/report.pdf
- Output: /home/user/summary.txt
- Saved to: /home/user/data.json

System Message:
🔗 EXTRACTED FILE PATHS:
- /home/user/report.pdf
- /home/user/summary.txt
- /home/user/data.json

💡 TIP: read_file('/home/user/report.pdf')
```

## 🧪 Testing

Run tests with:
```bash
python test_file_path_extraction.py
```

**Test Coverage:**
- ✅ Research Agent (English)
- ✅ Document Agent (German)
- ✅ Multiple Files
- ✅ Linux/macOS Paths
- ✅ Windows Paths
- ✅ No File Path (negative test)
- ✅ System Message Generation

## 📈 Benefits

### Before:
- ❌ Agent fragt nach Dateipfad (obwohl er schon da ist)
- ❌ User muss manuell Pfad kopieren
- ❌ Mehrere Interaktionen nötig

### After:
- ✅ Agent erkennt Pfad automatisch
- ✅ Direkte Nutzung von `read_file` oder `librarian_agent`
- ✅ Eine Interaktion reicht

## 🔍 Implementation Details

### Regex Pattern
```python
r'(?:Saved to|Output|File|Path|Ausgabe|Datei):\s*([^\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?))'
```

**Funktionsweise:**
1. `(?:Saved to|Output|...)` - Sucht nach Schlüsselwörtern (non-capturing group)
2. `:\s*` - Match `:` gefolgt von optionalem Whitespace
3. `([^\n]+` - Capture alles bis zum Zeilenende
4. `\.(?:html?|pdf|...)` - Match nur bekannte Dateitypen
5. `re.IGNORECASE` - Case-insensitive matching

### Cleaning
```python
cleaned_paths = [re.sub(r'\x1b\[[0-9;]*m', '', fp).strip() for fp in file_paths]
```
- Entfernt ANSI color codes
- Trimmt Whitespace
- Limitiert auf erste 3 Dateien

## 🚀 Related Features

- **Document Reading:** See `docs/DOCUMENT_READING.md`
- **Research Workflows:** See `docs/RESEARCH_AND_DOCUMENT_WORKFLOWS.md`
- **Sub-Agent IPC:** See `docs/SUBAGENT_IPC.md`
- **Librarian Configuration:** See `docs/LIBRARIAN_CONFIGURATION.md`

## 📝 Notes

- Paths werden **nicht validiert** (Existenz-Check erfolgt beim Lesen)
- Limit auf **3 Dateien** im System Message (um Context zu schonen)
- Funktioniert mit **allen Sub-Agents** (research, document, coding, librarian)
- Unterstützt **Windows, Linux, macOS** Pfade
