# Coder Agent Templates

This directory contains pre-built templates that the Coding Agent uses to quickly scaffold projects. Templates are organized by programming language or project type.

## Structure

- **java/**: Templates for Java applications and web servers.
- **javascript/**: Templates for Node.js applications and Express servers.
- **python/**: Templates for CLI tools, local servers, and general scripts.
- **websites/**: Basic HTML/CSS/JS website templates.
- **__init__.py**: Template loader and manager.

## Organization

Each subdirectory contains language-specific templates. For example:
- `python/cli_tool/`: A structured template for a command-line application.
- `javascript/express_server/`: A boilerplate for an Express.js backend.
- `websites/basic_website/`: Standard HTML5/CSS3/JS structure.

## Current Templates

### Website Template
A responsive website template with:
- Modern HTML5 structure
- Professional CSS styling
- Interactive JavaScript
- Mobile-responsive design
- Placeholder content for customization

## Scaffold conventions (why these templates help weak models)

Each code template is a **runnable starting point**, not an empty skeleton, so even a small
model can adapt it reliably:

- **A working example instead of an empty `# TODO`.** The core function does something real
  (marked `<-- REPLACE ...`) so the scaffold runs immediately and the model has a concrete
  pattern to adapt rather than a blank to invent.
- **A test stub that is green out of the box.** Every code template ships a matching test
  (`test_*.py` for Python, `*.test.js` for Node) that passes against the example. The coder's
  `run_tests` runs the Python ones; the model updates the tests as it changes the code.
- **Importable, not auto-running.** Servers/apps guard their entry point
  (`if __name__ == "__main__"`, `require.main === module`) so tests can import them without
  starting a process.
- **English throughout**, with `{{PLACEHOLDER}}` markers filled from the registry `placeholders`.

When adding a template, follow the same pattern: a working example, a matching test stub
(add it to the template's `files` list), and clear `REPLACE` markers.

## Adding New Templates

### 1. Create Your Template Files

Create a folder for your template inside the matching language directory (for example
`python/flask_app/` or `websites/landing_page/`) and add the source files with the
appropriate extension:
- `.html` for HTML templates
- `.css` for CSS templates
- `.js` for JavaScript templates
- `.py` for Python templates
- etc.

### 2. Use Placeholders

Templates support placeholder replacement. Use double curly braces:

```html
<title>{{TITLE}}</title>
<h1>Welcome to {{BUSINESS_NAME}}</h1>
<p>{{HEADLINE}}</p>
```

Common placeholders:
- `{{TITLE}}` - Page title
- `{{BUSINESS_NAME}}` - Company/project name
- `{{HEADLINE}}` - Main headline
- `{{ADDRESS}}` - Physical address
- `{{PHONE}}` - Phone number
- `{{EMAIL}}` - Contact email

### 3. Register in __init__.py

Add an entry to the `TEMPLATES` dictionary on the `TemplateManager` class in
`__init__.py`. The dictionary is keyed by *task type*; each value declares a
`description`, the list of `files` to generate, and the default `placeholders`.

Each entry in `files` maps the output file name to a `template` path relative to
this directory (nested paths are supported):

```python
TEMPLATES = {
    # ... existing task types ...
    "your_template": {
        "description": "Short description of what this template scaffolds",
        "files": [
            {"name": "main.py", "template": "python/your_template/main.py"},
        ],
        "placeholders": {
            "{{APP_NAME}}": "MyApp",
            "{{APP_DESCRIPTION}}": "A description",
        },
    },
}
```

The task type you choose (the dictionary key) is what `detect_template_type` /
`detect_template_type_with_llm` will return and what `generate_files` expects.

### 4. Example: Adding a Python Flask Template

Create `python/flask_app/app.py`:

```python
from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html', 
        title='{{APP_NAME}}',
        description='{{APP_DESCRIPTION}}')

@app.route('/api/{{API_ENDPOINT}}')
def api_endpoint():
    return {'message': '{{API_MESSAGE}}'}

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port={{PORT}})
```

Register it as a new task type in `TEMPLATES`:

```python
"flask_app": {
    "description": "Python Flask web application",
    "files": [
        {"name": "app.py", "template": "python/flask_app/app.py"},
    ],
    "placeholders": {
        "{{APP_NAME}}": "MyApp",
        "{{APP_DESCRIPTION}}": "A Flask application",
        "{{PORT}}": "5000",
        "{{API_ENDPOINT}}": "data",
        "{{API_MESSAGE}}": "API endpoint is working",
    },
},
```

### 5. Multi-File Templates

A single task type can generate several files that work together. List each one in
the `files` array and the manager will create them all:

```python
"my_framework": {
    "description": "My framework starter project",
    "files": [
        {"name": "index.html", "template": "websites/my_framework/index.html"},
        {"name": "styles.css", "template": "websites/my_framework/styles.css"},
        {"name": "script.js", "template": "websites/my_framework/script.js"},
        {"name": "config.json", "template": "websites/my_framework/config.json"},
    ],
    "placeholders": {
        "{{TITLE}}": "My Framework App",
    },
},
```

## Using Templates in Code

The Coding Agent automatically detects and uses templates when creating projects.

Templates are copied and customized based on:
1. Project type (detected from user request)
2. Available placeholders
3. User requirements

## Template Best Practices

1. **Keep Structure** - Use semantic HTML, organized CSS, clean JS
2. **Use Comments** - Explain sections clearly
3. **Placeholders** - Use consistent naming: `{{UPPERCASE_WITH_UNDERSCORES}}`
4. **Mobile First** - Design for mobile, enhance for desktop
5. **Accessibility** - Include ARIA labels, semantic elements
6. **Modern Standards** - Use current best practices
7. **Clean Code** - Well-formatted, readable

## Placeholder Conventions

Use these standard placeholder formats:

**Business/Project Info:**
- `{{BUSINESS_NAME}}` - Main name
- `{{BUSINESS_DESCRIPTION}}` - Short description
- `{{BUSINESS_TYPE}}` - Type of business (e.g., "restaurant", "shop")

**Contact:**
- `{{CONTACT_EMAIL}}`
- `{{PHONE_NUMBER}}`
- `{{ADDRESS}}`
- `{{CITY}}`
- `{{POSTAL_CODE}}`

**Content:**
- `{{PAGE_TITLE}}`
- `{{MAIN_HEADING}}`
- `{{TAGLINE}}`
- `{{ABOUT_TEXT}}`

**Technical:**
- `{{APP_NAME}}`
- `{{PORT}}`
- `{{API_ENDPOINT}}`
- `{{DATABASE_URL}}`

## Template Validation

The Coding Agent checks templates for:
- Required structural elements
- Placeholder consistency
- Valid syntax
- Responsive design

If a template is modified incorrectly, the agent will warn and preserve the original structure.

## Example: Complete React Template

Create `react_component.jsx`:

```jsx
import React, { useState, useEffect } from 'react';
import './{{COMPONENT_NAME}}.css';

/**
 * {{COMPONENT_NAME}} Component
 * {{COMPONENT_DESCRIPTION}}
 */
export const {{COMPONENT_NAME}} = ({ {{PROPS}} }) => {
  const [state, setState] = useState({{INITIAL_STATE}});

  useEffect(() => {
    // {{EFFECT_DESCRIPTION}}
  }, [{{DEPENDENCIES}}]);

  return (
    <div className="{{COMPONENT_NAME_LOWER}}">
      <h2>{{COMPONENT_TITLE}}</h2>
      <p>{{COMPONENT_CONTENT}}</p>
      {/* Add your component logic here */}
    </div>
  );
};

export default {{COMPONENT_NAME}};
```

## Advanced: Dynamic Templates

For complex scenarios, templates can include:
- Conditional sections (handled by Coding Agent)
- Nested placeholders
- Multiple variants

The Coding Agent intelligently selects and customizes based on context.

## Testing Your Template

1. Add the template files to the matching language folder
2. Register the task type in the `TEMPLATES` dictionary in `__init__.py`
3. Restart VAF
4. Ask the Coding Agent to create a project using your template type
5. Verify placeholders are replaced correctly
6. Check structure is preserved

## Need Help?

If your template isn't working:
1. Check the template files exist at the `template` paths declared in `TEMPLATES`
2. Verify the task type is registered in the `TEMPLATES` dictionary in `__init__.py`
3. Ensure placeholders use `{{CORRECT_FORMAT}}`
4. Test with a simple project request
5. Check Coding Agent debug output

