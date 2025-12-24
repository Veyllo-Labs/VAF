"""
VAF Generate - AI Code Generation
Generate code snippets, API endpoints, classes, etc.
"""
import typer
import os
import requests
import json
from pathlib import Path
from vaf.cli.ui import UI
from vaf.core.project_config import ProjectConfig

app = typer.Typer()

# ═══════════════════════════════════════════════════════════════════════════════
# GENERATION TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════

GENERATION_PROMPTS = {
    "api": {
        "python-flask": '''Generate a Flask API endpoint for {endpoint}.
Include:
- Route decorator with appropriate HTTP method
- Request validation
- Error handling
- Docstring
- Return JSON response

Output ONLY the Python code, no explanations.''',

        "python-fastapi": '''Generate a FastAPI endpoint for {endpoint}.
Include:
- Pydantic model for request/response
- Path operation decorator
- Type hints
- Error handling
- Docstring

Output ONLY the Python code, no explanations.''',

        "javascript": '''Generate an Express.js route for {endpoint}.
Include:
- Router handler
- Input validation
- Error handling
- JSDoc comments
- JSON response

Output ONLY the JavaScript code, no explanations.''',

        "typescript": '''Generate an Express.js TypeScript route for {endpoint}.
Include:
- Type definitions
- Router handler with types
- Input validation
- Error handling
- TSDoc comments

Output ONLY the TypeScript code, no explanations.''',
    },
    
    "class": {
        "python": '''Generate a Python class for {description}.
Include:
- Type hints
- Docstrings
- __init__ method
- Useful methods
- __repr__ method

Output ONLY the Python code, no explanations.''',

        "typescript": '''Generate a TypeScript class for {description}.
Include:
- Type definitions
- Constructor
- Public/private methods
- Getters/setters if appropriate
- TSDoc comments

Output ONLY the TypeScript code, no explanations.''',

        "rust": '''Generate a Rust struct and impl for {description}.
Include:
- Struct definition with derive macros
- impl block with methods
- Documentation comments
- Display trait if appropriate

Output ONLY the Rust code, no explanations.''',
    },
    
    "function": {
        "python": '''Generate a Python function for: {description}
Include:
- Type hints
- Docstring with examples
- Error handling
- Return type annotation

Output ONLY the Python code, no explanations.''',

        "javascript": '''Generate a JavaScript function for: {description}
Include:
- JSDoc comments
- Parameter validation
- Error handling
- Clear return

Output ONLY the JavaScript code, no explanations.''',

        "typescript": '''Generate a TypeScript function for: {description}
Include:
- Full type annotations
- TSDoc comments
- Generic types if appropriate
- Error handling

Output ONLY the TypeScript code, no explanations.''',
    },
    
    "test": {
        "python": '''Generate pytest tests for: {description}
Include:
- Multiple test cases
- Edge cases
- Fixtures if needed
- Descriptive test names

Output ONLY the Python test code, no explanations.''',

        "javascript": '''Generate Jest tests for: {description}
Include:
- describe/it blocks
- Multiple test cases
- Edge cases
- Mock setup if needed

Output ONLY the JavaScript test code, no explanations.''',
    },
    
    "component": {
        "react": '''Generate a React functional component for: {description}
Include:
- TypeScript types
- Props interface
- useState/useEffect if needed
- Clean JSX
- Basic styling

Output ONLY the TSX code, no explanations.''',

        "svelte": '''Generate a Svelte component for: {description}
Include:
- Props with types
- Reactive statements if needed
- Clean markup
- Scoped styles

Output ONLY the Svelte code, no explanations.''',

        "vue": '''Generate a Vue 3 component for: {description}
Include:
- script setup with TypeScript
- defineProps/defineEmits
- Reactive refs
- Clean template

Output ONLY the Vue SFC code, no explanations.''',
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# GENERATE COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

def call_local_llm(prompt: str, temperature: float = 0.2) -> str:
    """Call the local LLM via VAF server."""
    try:
        response = requests.post(
            "http://127.0.0.1:8080/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4096,
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

@app.command("api")
def generate_api(
    endpoint: str = typer.Argument(..., help="API endpoint (e.g. /users, /products/{id})"),
    language: str = typer.Option("auto", "--lang", "-l", help="Language (python, javascript, typescript)"),
    framework: str = typer.Option("auto", "--framework", "-f", help="Framework (flask, fastapi, express)"),
    output: str = typer.Option(None, "--output", "-o", help="Output file")
):
    """
    Generate an API endpoint.
    
    Examples:
        vaf generate api /users
        vaf generate api /products/{id} --lang python --framework fastapi
    """
    # Load project configuration
    config = ProjectConfig.load()
    
    if language == "auto":
        language = config.get("language", "python")
    
    if framework == "auto":
        framework = config.get("framework", "flask" if language == "python" else "express")
    
    # Select template
    template_key = f"{language}-{framework}" if f"{language}-{framework}" in GENERATION_PROMPTS["api"] else language
    
    if template_key not in GENERATION_PROMPTS["api"]:
        UI.error(f"No API template for {template_key}")
        UI.print(f"[dim]Available: {', '.join(GENERATION_PROMPTS['api'].keys())}[/dim]")
        raise typer.Exit(1)
    
    prompt = GENERATION_PROMPTS["api"][template_key].format(endpoint=endpoint)
    
    UI.event("Generate", f"Creating API endpoint: {endpoint}", style="cyan")
    UI.event("Config", f"Language: {language}, Framework: {framework}", style="dim")
    
    with UI.console.status("[bold cyan]Generating code...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    # Extract code (if in markdown blocks)
    code = extract_code(result)
    
    if output:
        Path(output).write_text(code, encoding="utf-8")
        UI.success(f"Code written to: {output}")
    else:
        UI.print()
        UI.console.print(code, markup=False)

@app.command("function")
def generate_function(
    description: str = typer.Argument(..., help="Function description"),
    language: str = typer.Option("auto", "--lang", "-l", help="Language"),
    output: str = typer.Option(None, "--output", "-o", help="Output file")
):
    """
    Generate a function.
    
    Examples:
        vaf generate function "Parse CSV file and return dict"
        vaf generate function "Calculate fibonacci" --lang rust
    """
    config = ProjectConfig.load()
    
    if language == "auto":
        language = config.get("language", "python")
    
    if language not in GENERATION_PROMPTS["function"]:
        UI.error(f"No function template for {language}")
        raise typer.Exit(1)
    
    prompt = GENERATION_PROMPTS["function"][language].format(description=description)
    
    UI.event("Generate", f"Creating function: {description[:50]}...", style="cyan")
    
    with UI.console.status("[bold cyan]Generating code...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    code = extract_code(result)
    
    if output:
        Path(output).write_text(code, encoding="utf-8")
        UI.success(f"Code written to: {output}")
    else:
        UI.print()
        UI.console.print(code, markup=False)

@app.command("class")
def generate_class(
    description: str = typer.Argument(..., help="Class description"),
    language: str = typer.Option("auto", "--lang", "-l", help="Language"),
    output: str = typer.Option(None, "--output", "-o", help="Output file")
):
    """
    Generate a class/struct.
    
    Examples:
        vaf generate class "User with name, email, and age"
        vaf generate class "Config manager" --lang typescript
    """
    config = ProjectConfig.load()
    
    if language == "auto":
        language = config.get("language", "python")
    
    if language not in GENERATION_PROMPTS["class"]:
        UI.error(f"No class template for {language}")
        raise typer.Exit(1)
    
    prompt = GENERATION_PROMPTS["class"][language].format(description=description)
    
    UI.event("Generate", f"Creating class: {description[:50]}...", style="cyan")
    
    with UI.console.status("[bold cyan]Generating code...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    code = extract_code(result)
    
    if output:
        Path(output).write_text(code, encoding="utf-8")
        UI.success(f"Code written to: {output}")
    else:
        UI.print()
        UI.console.print(code, markup=False)

@app.command("test")
def generate_test(
    description: str = typer.Argument(..., help="What to test"),
    language: str = typer.Option("auto", "--lang", "-l", help="Language"),
    output: str = typer.Option(None, "--output", "-o", help="Output file")
):
    """
    Generate tests.
    
    Examples:
        vaf generate test "User authentication flow"
        vaf generate test "calculateTotal function" --lang javascript
    """
    config = ProjectConfig.load()
    
    if language == "auto":
        language = config.get("language", "python")
    
    if language not in GENERATION_PROMPTS["test"]:
        UI.error(f"No test template for {language}")
        raise typer.Exit(1)
    
    prompt = GENERATION_PROMPTS["test"][language].format(description=description)
    
    UI.event("Generate", f"Creating tests: {description[:50]}...", style="cyan")
    
    with UI.console.status("[bold cyan]Generating tests...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    code = extract_code(result)
    
    if output:
        Path(output).write_text(code, encoding="utf-8")
        UI.success(f"Tests written to: {output}")
    else:
        UI.print()
        UI.console.print(code, markup=False)

@app.command("component")
def generate_component(
    description: str = typer.Argument(..., help="Component description"),
    framework: str = typer.Option("react", "--framework", "-f", help="Framework (react, svelte, vue)"),
    output: str = typer.Option(None, "--output", "-o", help="Output file")
):
    """
    Generate a UI component.
    
    Examples:
        vaf generate component "Login form with email and password"
        vaf generate component "Product card with image" --framework svelte
    """
    if framework not in GENERATION_PROMPTS["component"]:
        UI.error(f"No component template for {framework}")
        UI.print(f"[dim]Available: {', '.join(GENERATION_PROMPTS['component'].keys())}[/dim]")
        raise typer.Exit(1)
    
    prompt = GENERATION_PROMPTS["component"][framework].format(description=description)
    
    UI.event("Generate", f"Creating {framework} component...", style="cyan")
    
    with UI.console.status("[bold cyan]Generating component...[/bold cyan]", spinner="dots"):
        result = call_local_llm(prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    code = extract_code(result)
    
    if output:
        Path(output).write_text(code, encoding="utf-8")
        UI.success(f"Component written to: {output}")
    else:
        UI.print()
        UI.console.print(code, markup=False)

@app.command("free")
def generate_free(
    prompt: str = typer.Argument(..., help="Free description"),
    output: str = typer.Option(None, "--output", "-o", help="Output file")
):
    """
    Free code generation with any prompt.
    
    Examples:
        vaf generate free "Create a Python decorator for caching"
        vaf generate free "Write a bash script to backup folders"
    """
    full_prompt = f"Generate code for: {prompt}\n\nOutput ONLY the code, no explanations."
    
    UI.event("Generate", f"Free generation...", style="cyan")
    
    with UI.console.status("[bold cyan]Generating code...[/bold cyan]", spinner="dots"):
        result = call_local_llm(full_prompt)
    
    if result.startswith("Error:"):
        UI.error(result)
        raise typer.Exit(1)
    
    code = extract_code(result)
    
    if output:
        Path(output).write_text(code, encoding="utf-8")
        UI.success(f"Code written to: {output}")
    else:
        UI.print()
        UI.console.print(code, markup=False)

# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def extract_code(text: str) -> str:
    """Extract code from markdown blocks or return text."""
    import re
    
    # Search for code blocks
    code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', text, re.DOTALL)
    
    if code_blocks:
        return "\n\n".join(code_blocks)
    
    return text.strip()

@app.callback(invoke_without_command=True)
def generate_callback(ctx: typer.Context):
    """
    VAF Generate - AI-powered code generation.
    """
    if ctx.invoked_subcommand is None:
        UI.panel("VAF Generate - Code Generation", style="cyan")
        UI.print("  [bold]api[/bold]        - Generate API endpoint")
        UI.print("  [bold]function[/bold]   - Generate function")
        UI.print("  [bold]class[/bold]      - Generate class/struct")
        UI.print("  [bold]test[/bold]       - Generate tests")
        UI.print("  [bold]component[/bold]  - Generate UI component")
        UI.print("  [bold]free[/bold]       - Free generation")
        UI.print()
        UI.print("[dim]Example: vaf generate function \"Parse JSON config\"[/dim]")
