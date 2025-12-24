# VAF Workflows

This directory contains the workflow system for VAF, which allows you to define multi-step tool pipelines that execute automatically based on user input.

## Structure

- **workflows/** - Individual workflow files (one per workflow)
- **templates.py** - Automatically loads all workflows from `workflows/` and `~/.vaf/workflows/`
- **selector.py** - Matches user input to workflows
- **engine.py** - Executes workflow steps
- **__init__.py** - Module exports

## Adding New Workflows

### 1. Create a New Workflow File

Create a new `.py` file in `vaf/workflows/workflows/` (for built-in workflows) or `~/.vaf/workflows/` (for user-generated workflows).

**Example: `vaf/workflows/workflows/my_workflow.py`**

```python
"""
My Custom Workflow

Description of what this workflow does.
"""

WORKFLOW = {
    "name": "My Workflow",
    "description": "What this workflow does",
    "triggers": [
        # Keywords that trigger this workflow
        "keyword1", "keyword2", 
        "phrase to match",
    ],
    "trigger_patterns": [
        # Regex patterns for matching
        r"pattern.*match",
        r"another.*pattern",
    ],
    "variables": {
        # Variables to extract from user input
        "var_name": "Description of variable",
    },
    "defaults": {
        # Optional: Default values for variables
        "var_name": "default_value",
    },
    "steps": [
        # Sequence of tool calls
        {
            "tool": "tool_name",
            "input": "Input using {variables}",
            "output": "output_var",
            "description": "What this step does",
        },
    ],
}
```

### 2. Automatic Loading

Workflows are automatically discovered and loaded:
- **Built-in workflows**: `vaf/workflows/workflows/*.py` (loaded at startup)
- **User workflows**: `~/.vaf/workflows/*.py` (loaded at startup, can override built-in)

No need to register or import - just create the file!

### 3. Example: Simple File Creation Workflow

**File: `vaf/workflows/workflows/create_config.py`**

```python
"""
Create Config File Workflow

Generate a configuration file.
"""

WORKFLOW = {
    "name": "Create Config File",
    "description": "Generate a configuration file",
    "triggers": [
        "create config", "generate config",
        "make config file",
    ],
    "trigger_patterns": [
        r"create.*config",
        r"generate.*config",
    ],
    "variables": {
        "format": "Config format (json, yaml, etc.)",
    },
    "defaults": {
        "format": "json",
    },
    "steps": [
        {
            "tool": "coding_agent",
            "input": "Create a {format} configuration file with common settings",
            "output": "config_content",
            "description": "Generate config content",
        },
        {
            "tool": "write_file",
            "input": '{"path": "config.{format}", "content": "{config_content}"}',
            "output": "result",
            "description": "Save config file",
        },
    ],
}
```

### 4. User-Generated Workflows

To create a user-generated workflow that persists across VAF updates:

1. Create `~/.vaf/workflows/` directory (if it doesn't exist)
2. Add your workflow file there (e.g., `~/.vaf/workflows/my_custom_workflow.py`)
3. Restart VAF - the workflow will be automatically loaded

**Note**: User workflows can override built-in workflows with the same filename.

### 5. Variable Handling

Variables can be:
- Extracted from user input automatically
- Passed between steps using `{variable_name}`
- Used in tool inputs with curly braces
- Have default values in `defaults` dictionary

### 6. Available Tools

Common tools you can use in workflows:
- `coding_agent` - Generate code or content
- `web_search` - Search the web
- `write_file` - Write to a file
- `read_file` - Read from a file
- `bash` - Execute shell commands
- `librarian_agent` - File/info retrieval
- `python_sandbox` - Execute Python code safely

### 7. Multi-Step Workflows

Steps execute sequentially. Output from one step becomes input for the next:

```python
"steps": [
    {
        "tool": "web_search",
        "input": "Search for: {topic}",
        "output": "search_results",
        "description": "Find information",
    },
    {
        "tool": "coding_agent",
        "input": "Based on this info: {search_results}\nCreate code for: {task}",
        "output": "code",
        "description": "Generate code",
    },
    {
        "tool": "write_file",
        "input": '{"path": "{filename}", "content": "{code}"}',
        "output": "saved",
        "description": "Save to file",
    },
]
```

## Testing Your Workflow

1. Create your workflow file in `workflows/` directory
2. Restart VAF (workflows are loaded at startup)
3. Test with a phrase that matches your triggers
4. Check the debug output to see workflow matching

## Brain-Based Matching

The workflow system uses two matching methods:

1. **Brain (LLM)** - Understands intent in any language, ignores typos
2. **Pattern Matching** - Fallback using triggers and regex patterns

Make sure to include:
- Clear triggers (common phrases)
- Regex patterns for flexibility
- Good description for brain matching

## Best Practices

1. **Clear Descriptions** - Help the brain understand when to use this workflow
2. **Multiple Triggers** - Include variations and common phrasings
3. **Flexible Patterns** - Use regex to catch different formulations
4. **Meaningful Variables** - Name variables clearly
5. **Atomic Steps** - Each step should do one thing well
6. **Error Handling** - Consider what happens if a step fails
7. **One File Per Workflow** - Keep workflows modular and maintainable

## Workflow Priority

Workflows are matched by confidence score. Higher confidence wins.

To increase match probability:
- Add more trigger phrases
- Add trigger patterns
- Use clear, specific descriptions
- Include common variations

## Existing Workflows

Check the existing workflows in `workflows/` for examples:
- `create_website.py` - Multi-file web project
- `research_and_code.py` - Web search then code generation
- `analyze_website.py` - Fetch and analyze web content
- `code_review.py` - Review code files
- `deep_research.py` - Multi-query research
- `web_lookup.py` - Quick web search
- `generate_docs.py` - Generate documentation
- `create_file.py` - Create a single file

## Reloading Workflows

If you add a new workflow file while VAF is running, you can reload workflows:

```python
from vaf.workflows.templates import reload_workflows
reload_workflows()
```

Or simply restart VAF (workflows are loaded at startup).

## Need Help?

If your workflow isn't matching:
1. Check debug output: `| Debug Workflow analysis returned: ...`
2. Verify triggers match common phrasings
3. Test pattern matching separately
4. Ensure description is clear and specific
5. Check that your file defines `WORKFLOW` dictionary correctly
