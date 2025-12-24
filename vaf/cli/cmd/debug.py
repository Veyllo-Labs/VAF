"""
VAF Debug - AI-powered Error Analysis
Analyze error messages and provide solutions
"""
import typer
import requests
import os
import sys
import traceback
from pathlib import Path
from typing import Optional
from vaf.cli.ui import UI
from vaf.core.project_config import ProjectConfig

app = typer.Typer()

# ═══════════════════════════════════════════════════════════════════════════════
# DEBUG PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════

DEBUG_PROMPT = '''You are an experienced debugging expert. Analyze the following error and provide a clear, structured explanation:

**Error Message:**
```
{error}
```

{context}

**Your response should include:**

1. **Error Type**: What kind of error is this?
2. **Cause**: What typically causes this error?
3. **Solution**: Concrete steps to fix it
4. **Example Fix**: If possible, show corrected code

Be precise and helpful.
'''

ANALYZE_STACKTRACE_PROMPT = '''Analyze this stack trace and identify the error cause:

**Stack Trace:**
```
{stacktrace}
```

**Relevant Code Context:**
```{language}
{code_context}
```

Provide a detailed analysis:
1. Where exactly does the error occur?
2. What is the probable cause?
3. How can the error be fixed?
'''

# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def call_local_llm(prompt: str, temperature: float = 0.3) -> str:
    """Call the local LLM via VAF server."""
    try:
        response = requests.post(
            "http://127.0.0.1:8080/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 2048,
                "stream": False
            },
            timeout=120
        )
        
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"Error: Server returned {response.status_code}"
    except requests.exceptions.ConnectionError:
        return "Error: VAF Server not reachable. Start 'vaf run' first."
    except Exception as e:
        return f"Error: {e}"

def read_file_context(file_path: str, line_number: int, context_lines: int = 5) -> str:
    """Read context around a specific line in a file."""
    try:
        path = Path(file_path)
        if not path.exists():
            return ""
        
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        
        start = max(0, line_number - context_lines - 1)
        end = min(len(lines), line_number + context_lines)
        
        context_lines_list = []
        for i, line in enumerate(lines[start:end], start=start + 1):
            marker = ">>>" if i == line_number else "   "
            context_lines_list.append(f"{marker} {i:4d} | {line}")
        
        return "\n".join(context_lines_list)
    except Exception:
        return ""

def parse_stacktrace(error_text: str) -> list[dict]:
    """Parse a stack trace and extract file/line information."""
    import re
    
    # Python-Style: File "path", line N, in function
    python_pattern = r'File "([^"]+)", line (\d+)'
    
    # JavaScript/Node-Style: at function (path:line:col)
    js_pattern = r'at .+ \(([^:]+):(\d+):\d+\)'
    
    # Rust-Style: path:line:col
    rust_pattern = r'(\S+\.rs):(\d+):\d+'
    
    locations = []
    
    for pattern in [python_pattern, js_pattern, rust_pattern]:
        matches = re.findall(pattern, error_text)
        for file_path, line in matches:
            locations.append({
                "file": file_path,
                "line": int(line)
            })
    
    return locations

# ═══════════════════════════════════════════════════════════════════════════════
# DEBUG COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@app.command("explain")
def explain_error(
    error: str = typer.Argument(..., help="The error message to analyze"),
    language: str = typer.Option("auto", "--lang", "-l", help="Programming language"),
    file: str = typer.Option(None, "--file", "-f", help="Relevant file for context")
):
    """
    Explain an error message and provide solutions.
    
    Examples:
        vaf debug explain "NameError: name 'x' is not defined"
        vaf debug explain "TypeError: Cannot read property 'map' of undefined"
        vaf debug explain "E0382: borrow of moved value" --lang rust
    """
    config = ProjectConfig.load()
    
    if language == "auto":
        language = config.get("language", "auto")
    
    # Build context
    context_parts = []
    
    if language != "auto":
        context_parts.append(f"**Language:** {language}")
    
    if file:
        file_content = Path(file).read_text(encoding="utf-8", errors="ignore") if Path(file).exists() else ""
        if file_content:
            # Truncated for context
            context_parts.append(f"**File ({file}):**\n```\n{file_content[:1500]}...\n```")
    
    context = "\n".join(context_parts) if context_parts else ""
    
    prompt = DEBUG_PROMPT.format(error=error, context=context)
    
    UI.event("Debug", "Analyzing error...", style="cyan")
    UI.print()
    
    with UI.console.status("[bold cyan]AI is analyzing the error...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    UI.console.print(result, markup=True)

@app.command("trace")
def analyze_trace(
    file: str = typer.Option(None, "--file", "-f", help="File with stack trace"),
    context: int = typer.Option(5, "--context", "-c", help="Context lines around error")
):
    """
    Analyze a stack trace from a file or stdin.
    
    Examples:
        vaf debug trace --file error.log
        cat error.log | vaf debug trace
    """
    # Read stack trace
    if file:
        trace_path = Path(file)
        if not trace_path.exists():
            UI.error(f"File not found: {file}")
            raise typer.Exit(1)
        stacktrace = trace_path.read_text(encoding="utf-8", errors="ignore")
    else:
        # Read from stdin
        UI.print("[dim]Paste stack trace (Ctrl+D to finish):[/dim]")
        try:
            stacktrace = sys.stdin.read()
        except KeyboardInterrupt:
            raise typer.Exit(0)
    
    if not stacktrace.strip():
        UI.error("No stack trace provided.")
        raise typer.Exit(1)
    
    UI.event("Debug", "Analyzing stack trace...", style="cyan")
    
    # Extract file locations
    locations = parse_stacktrace(stacktrace)
    
    code_context = ""
    if locations:
        # Take first relevant entry (usually the actual error location)
        for loc in locations:
            ctx = read_file_context(loc["file"], loc["line"], context)
            if ctx:
                code_context = ctx
                UI.event("Found", f"Error in {loc['file']}:{loc['line']}", style="dim")
                break
    
    # Detect language
    language = ""
    if locations:
        first_file = locations[0]["file"]
        if first_file.endswith(".py"):
            language = "python"
        elif first_file.endswith((".js", ".ts", ".tsx")):
            language = "javascript"
        elif first_file.endswith(".rs"):
            language = "rust"
        elif first_file.endswith(".go"):
            language = "go"
    
    prompt = ANALYZE_STACKTRACE_PROMPT.format(
        stacktrace=stacktrace[:3000],  # Limit
        language=language,
        code_context=code_context if code_context else "(not available)"
    )
    
    with UI.console.status("[bold cyan]AI is analyzing the stack trace...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    UI.print()
    UI.console.print(result, markup=True)

@app.command("why")
def why_error(
    question: str = typer.Argument(..., help="Question about the error")
):
    """
    Answer questions about programming errors.
    
    Examples:
        vaf debug why "Why am I getting a Segmentation Fault?"
        vaf debug why "What does 'lifetime may not live long enough' mean?"
        vaf debug why "Why is my variable undefined?"
    """
    prompt = f'''You are an experienced developer. Answer this question about a programming error:

**Question:** {question}

Provide a clear, understandable answer with:
1. Explanation of the problem
2. Common causes
3. Solution approaches
4. Example (if helpful)
'''
    
    UI.event("Debug", f"Question: {question[:50]}...", style="cyan")
    
    with UI.console.status("[bold cyan]AI is answering the question...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    UI.print()
    UI.console.print(result, markup=True)

@app.command("fix")
def suggest_fix(
    file: str = typer.Argument(..., help="File with error"),
    line: int = typer.Option(None, "--line", "-l", help="Line with error"),
    error: str = typer.Option(None, "--error", "-e", help="Error message")
):
    """
    Suggest a fix for an error in a file.
    
    Examples:
        vaf debug fix main.py --line 42 --error "NameError: name 'data' is not defined"
    """
    file_path = Path(file)
    
    if not file_path.exists():
        UI.error(f"File not found: {file}")
        raise typer.Exit(1)
    
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    
    # Detect language
    ext_to_lang = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
    }
    language = ext_to_lang.get(file_path.suffix, "")
    
    # Context around error line
    code_context = ""
    if line:
        code_context = read_file_context(file, line, context_lines=10)
    else:
        # First 50 lines as context
        code_context = "\n".join(content.splitlines()[:50])
    
    prompt = f'''Analyze this code and error, then suggest a fix:

**File:** {file}
**Language:** {language}
**Error:** {error or "(not specified)"}
**Error Line:** {line or "(not specified)"}

**Code Context:**
```{language}
{code_context}
```

**Full File Content (truncated):**
```{language}
{content[:2000]}
```

**Task:**
1. Identify the problem
2. Briefly explain the cause
3. Show the corrected code
4. Provide the fix as a complete function/block (copy-paste ready)
'''
    
    UI.event("Debug", f"Analyzing {file}...", style="cyan")
    
    with UI.console.status("[bold cyan]AI is looking for a solution...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    UI.print()
    UI.console.print(result, markup=True)

@app.callback(invoke_without_command=True)
def debug_callback(ctx: typer.Context):
    """
    VAF Debug - AI-powered error analysis.
    """
    if ctx.invoked_subcommand is None:
        UI.panel("VAF Debug - Error Analysis", style="cyan")
        UI.print("  [bold]explain[/bold]  - Explain error message")
        UI.print("  [bold]trace[/bold]    - Analyze stack trace")
        UI.print("  [bold]why[/bold]      - Ask questions about errors")
        UI.print("  [bold]fix[/bold]      - Suggest fix for file")
        UI.print()
        UI.print('[dim]Example: vaf debug explain "NameError: name x is not defined"[/dim]')
