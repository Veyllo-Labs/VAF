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

## Adding New Templates

### 1. Create Your Template File

Add a new file inside a language folder (or create a new folder) with the appropriate extension:
- `.html` for HTML templates
- `.css` for CSS templates
- `.js` for JavaScript templates
- `.py` for Python templates
- etc.

### 2. Use Placeholders

Templates support placeholder replacement. Use double curly braces:

```html
<title>{{BUSINESS_NAME}}</title>
<h1>Welcome to {{BUSINESS_NAME}}</h1>
<p>{{BUSINESS_DESCRIPTION}}</p>
```

Common placeholders:
- `{{BUSINESS_NAME}}` - Company/project name
- `{{BUSINESS_DESCRIPTION}}` - Short description
- `{{CONTACT_EMAIL}}` - Contact email
- `{{PHONE_NUMBER}}` - Phone number
- `{{ADDRESS}}` - Physical address
- `{{CITY}}` - City name

### 3. Register in __init__.py

Add your template to the template loader:

```python
def get_template(template_name: str) -> str:
    templates = {
        'website_html': 'website_html.html',
        'website_css': 'website_css.css',
        'website_js': 'website_js.js',
        'your_template': 'your_template.ext',  # Add this
    }
    
    template_file = templates.get(template_name)
    if not template_file:
        return None
    
    template_path = Path(__file__).parent / template_file
    with open(template_path, 'r', encoding='utf-8') as f:
        return f.read()
```

### 4. Example: Adding a Python Flask Template

Create `flask_app.py`:

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

Register in `__init__.py`:

```python
'flask_app': 'flask_app.py',
```

### 5. Template Sets

You can create related templates that work together:

```
my_framework_html.html
my_framework_css.css
my_framework_js.js
my_framework_config.json
```

Register all of them:

```python
'my_framework_html': 'my_framework_html.html',
'my_framework_css': 'my_framework_css.css',
'my_framework_js': 'my_framework_js.js',
'my_framework_config': 'my_framework_config.json',
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

1. Add template file to this directory
2. Register in `__init__.py`
3. Restart VAF
4. Ask the Coding Agent to create a project using your template type
5. Verify placeholders are replaced correctly
6. Check structure is preserved

## Need Help?

If your template isn't working:
1. Check file is in this directory
2. Verify registration in `__init__.py`
3. Ensure placeholders use `{{CORRECT_FORMAT}}`
4. Test with a simple project request
5. Check Coding Agent debug output

