# VAF Workflows

This directory contains the workflow system for VAF, which allows you to define multi-step tool pipelines that execute automatically based on user input.

## Structure

- **templates.py** - Workflow template definitions
- **selector.py** - Matches user input to workflows
- **engine.py** - Executes workflow steps
- **__init__.py** - Module exports

## Adding New Workflows

### 1. Define Your Workflow in templates.py

Add a new entry to the `WORKFLOW_TEMPLATES` dictionary:

```python
"your_workflow_id": {
    "name": "Your Workflow Name",
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

### 2. Example: Simple File Creation Workflow

```python
"create_config": {
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
    "steps": [
        {
            "tool": "coding_agent",
            "input": "Create a {format} configuration file with common settings",
            "output": "config_content",
            "description": "Generate config content",
        },
        {
            "tool": "write_file",
            "input": '{{"path": "config.{format}", "content": "{config_content}"}}',
            "output": "result",
            "description": "Save config file",
        },
    ],
}
```

### 3. Variable Handling

Variables can be:
- Extracted from user input automatically
- Passed between steps using `{variable_name}`
- Used in tool inputs with curly braces

### 4. Available Tools

Common tools you can use in workflows:
- `coding_agent` - Generate code or content
- `web_search` - Search the web
- `write_file` - Write to a file
- `read_file` - Read from a file
- `bash` - Execute shell commands
- `librarian_agent` - File/info retrieval

### 5. Multi-Step Workflows

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
        "input": '{{"path": "{filename}", "content": "{code}"}}',
        "output": "saved",
        "description": "Save to file",
    },
]
```

## Testing Your Workflow

1. Add your workflow to `WORKFLOW_TEMPLATES`
2. Restart VAF
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

## Workflow Priority

Workflows are matched by confidence score. Higher confidence wins.

To increase match probability:
- Add more trigger phrases
- Add trigger patterns
- Use clear, specific descriptions
- Include common variations

## Examples in templates.py

Check the existing workflows for examples:
- `create_website` - Multi-file web project
- `research_and_code` - Web search then code generation
- `analyze_website` - Fetch and analyze web content
- `code_review` - Review code files
- `deep_research` - Multi-query research

## Need Help?

If your workflow isn't matching:
1. Check debug output: `| Debug Workflow analysis returned: ...`
2. Verify triggers match common phrasings
3. Test pattern matching separately
4. Ensure description is clear and specific

