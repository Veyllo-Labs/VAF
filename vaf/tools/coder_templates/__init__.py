"""
VAF Coder Templates - Deterministic file templates for common tasks

These templates are used by the Coding Agent to generate files quickly
without relying solely on LLM generation.
"""

import os
from pathlib import Path
from typing import Dict, Optional, List

TEMPLATE_DIR = Path(__file__).parent


class TemplateManager:
    """Manages code templates for the Coder Agent."""
    
    # Template registry: task_type -> list of files
    TEMPLATES = {
        "website": {
            "description": "Complete website with HTML, CSS, and JavaScript",
            "files": [
                {"name": "index.html", "template": "websites/basic_website/index.html"},
                {"name": "styles.css", "template": "websites/basic_website/styles.css"},
                {"name": "script.js", "template": "websites/basic_website/script.js"},
            ],
            "placeholders": {
                "{{TITLE}}": "Willkommen",
                "{{BUSINESS_NAME}}": "Mein Unternehmen",
                "{{HEADLINE}}": "Professionelle Dienstleistungen",
                "{{SUBHEADLINE}}": "Qualität, die überzeugt.",
                "{{SERVICE_1_TITLE}}": "Service 1",
                "{{SERVICE_1_DESC}}": "Beschreibung des ersten Services.",
                "{{SERVICE_2_TITLE}}": "Service 2",
                "{{SERVICE_2_DESC}}": "Beschreibung des zweiten Services.",
                "{{SERVICE_3_TITLE}}": "Service 3",
                "{{SERVICE_3_DESC}}": "Beschreibung des dritten Services.",
                "{{ABOUT_TEXT}}": "Wir sind ein erfahrenes Team mit Leidenschaft für Qualität.",
                "{{FEATURE_1}}": "Jahrelange Erfahrung",
                "{{FEATURE_2}}": "Faire Preise",
                "{{FEATURE_3}}": "Schneller Service",
                "{{ADDRESS}}": "Musterstraße 1, 10115 Berlin",
                "{{PHONE}}": "+49 30 12345678",
                "{{EMAIL}}": "info@example.de",
            }
        },
        "python_script": {
            "description": "Python script with main function and argparse",
            "files": [
                {"name": "main.py", "template": "python/script/main.py"},
            ],
            "placeholders": {
                "{{SCRIPT_NAME}}": "MyScript",
                "{{SCRIPT_DESCRIPTION}}": "A Python script",
                "{{SCRIPT_DETAILS}}": "This script performs a specific task."
            }
        },
        "python_server": {
            "description": "Python local server (Flask) with API endpoints",
            "files": [
                {"name": "server.py", "template": "python/local_server/server.py"},
                {"name": "requirements.txt", "template": "python/local_server/requirements.txt"},
            ],
            "placeholders": {
                "{{APP_NAME}}": "MyApp",
                "{{APP_DESCRIPTION}}": "A local development server",
                "{{PORT}}": "8000",
                "{{API_ENDPOINT}}": "data",
                "{{API_MESSAGE}}": "API endpoint is working"
            }
        },
        "python_cli": {
            "description": "Python CLI tool with subcommands",
            "files": [
                {"name": "cli.py", "template": "python/cli_tool/cli.py"},
            ],
            "placeholders": {
                "{{CLI_TOOL_NAME}}": "mytool",
                "{{CLI_DESCRIPTION}}": "A command-line tool"
            }
        },
        "java_application": {
            "description": "Java application with main class",
            "files": [
                {"name": "Main.java", "template": "java/application/Main.java"},
            ],
            "placeholders": {
                "{{APP_NAME}}": "MyApplication",
                "{{APP_DESCRIPTION}}": "A Java application"
            }
        },
        "java_server": {
            "description": "Java HTTP server",
            "files": [
                {"name": "Server.java", "template": "java/web_server/Server.java"},
            ],
            "placeholders": {
                "{{SERVER_NAME}}": "MyServer",
                "{{SERVER_DESCRIPTION}}": "A simple HTTP server",
                "{{PORT}}": "8080"
            }
        },
        "node_app": {
            "description": "Node.js application with HTTP server",
            "files": [
                {"name": "app.js", "template": "javascript/node_app/app.js"},
            ],
            "placeholders": {
                "{{APP_NAME}}": "MyApp",
                "{{APP_DESCRIPTION}}": "A Node.js application",
                "{{PORT}}": "3000",
                "{{API_ENDPOINT}}": "data",
                "{{API_MESSAGE}}": "API endpoint is working"
            }
        },
        "express_server": {
            "description": "Express.js server with API routes",
            "files": [
                {"name": "server.js", "template": "javascript/express_server/server.js"},
                {"name": "package.json", "template": "javascript/express_server/package.json"},
            ],
            "placeholders": {
                "{{APP_NAME}}": "my-express-app",
                "{{APP_DESCRIPTION}}": "An Express.js server",
                "{{PORT}}": "3000",
                "{{API_ENDPOINT}}": "data",
                "{{API_MESSAGE}}": "API endpoint is working"
            }
        },
    }
    
    @classmethod
    def get_template_types(cls) -> List[str]:
        """Get list of available template types."""
        return list(cls.TEMPLATES.keys())
    
    @classmethod
    def get_template_info(cls, template_type: str) -> Optional[Dict]:
        """Get info about a template type."""
        return cls.TEMPLATES.get(template_type)
    
    @classmethod
    def load_template(cls, template_file: str) -> Optional[str]:
        """
        Load a template file's contents.
        Supports both flat files and nested paths (e.g., 'websites/basic_website/index.html').
        """
        # Support both flat and nested paths
        # Normalize path separators for cross-platform compatibility
        template_file = template_file.replace('\\', '/')
        template_path = TEMPLATE_DIR / template_file
        
        if template_path.exists():
            return template_path.read_text(encoding='utf-8')
        return None
    
    @classmethod
    def generate_files(
        cls, 
        template_type: str, 
        output_dir: str,
        custom_placeholders: Dict[str, str] = None
    ) -> List[str]:
        """
        Generate all files for a template type.
        
        Args:
            template_type: Type of template (e.g., "website")
            output_dir: Directory to write files to
            custom_placeholders: Custom placeholder values
            
        Returns:
            List of created file paths
        """
        template_info = cls.TEMPLATES.get(template_type)
        if not template_info:
            return []
        
        # Merge default and custom placeholders
        placeholders = template_info.get("placeholders", {}).copy()
        if custom_placeholders:
            placeholders.update(custom_placeholders)
        
        created_files = []
        os.makedirs(output_dir, exist_ok=True)
        
        for file_info in template_info["files"]:
            template_content = cls.load_template(file_info["template"])
            if not template_content:
                continue
            
            # Replace placeholders
            content = template_content
            for placeholder, value in placeholders.items():
                content = content.replace(placeholder, value)
            
            # Write file
            file_path = os.path.join(output_dir, file_info["name"])
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            created_files.append(file_path)
        
        return created_files
    
    @classmethod
    def detect_template_type(cls, task: str) -> Optional[str]:
        """
        Detect which template type matches a task description.
        
        Args:
            task: Task description (e.g., "Create a website for a craftsman")
            
        Returns:
            Template type or None if no match
        """
        task_lower = task.lower()
        
        # Website detection
        website_keywords = [
            'website', 'webseite', 'webpage', 'homepage', 
            'landing page', 'web page', 'seite erstellen',
            'html', 'frontend'
        ]
        if any(kw in task_lower for kw in website_keywords):
            return "website"
        
        # Python server detection (prioritize over script)
        python_server_keywords = ['python server', 'flask', 'fastapi', 'local server', 'web server', 'api server']
        if any(kw in task_lower for kw in python_server_keywords):
            return "python_server"
        
        # Python CLI detection
        python_cli_keywords = ['python cli', 'command line', 'cli tool', 'command-line']
        if any(kw in task_lower for kw in python_cli_keywords):
            return "python_cli"
        
        # Python script detection
        python_keywords = ['python', 'script', 'skript', '.py']
        if any(kw in task_lower for kw in python_keywords):
            return "python_script"
        
        # Java server detection
        java_server_keywords = ['java server', 'java http', 'java web server']
        if any(kw in task_lower for kw in java_server_keywords):
            return "java_server"
        
        # Java application detection
        java_keywords = ['java', 'java application', 'java app']
        if any(kw in task_lower for kw in java_keywords):
            return "java_application"
        
        # Express.js detection (prioritize over node)
        express_keywords = ['express', 'express.js', 'express server']
        if any(kw in task_lower for kw in express_keywords):
            return "express_server"
        
        # Node.js detection
        node_keywords = ['node.js', 'nodejs', 'node app', 'node server']
        if any(kw in task_lower for kw in node_keywords):
            return "node_app"
        
        return None
    
    @classmethod
    def detect_template_type_with_llm(cls, task: str, user_preference: Optional[str] = None) -> tuple[Optional[str], str]:
        """
        Use LLM to intelligently detect which template type matches a task.
        This has its own context and runs BEFORE the main coding work begins.
        More accurate than keyword matching, especially for ambiguous cases.

        Args:
            task: Task description from the main agent
            user_preference: Optional hint about user's preference (e.g., "no_template", "content_only")

        Returns:
            Tuple of (template_type, decision_info):
            - template_type: Template type or None if no template should be used
            - decision_info: Detailed information about the decision process
        """
        import requests

        # Get available template types with descriptions
        available_templates = list(cls.TEMPLATES.keys())
        template_list = []
        for ttype in available_templates:
            desc = cls.TEMPLATES[ttype].get("description", ttype)
            files = cls.TEMPLATES[ttype].get("files", [])
            file_names = [f.get("name", "") for f in files]
            template_list.append(f"- {ttype}: {desc} (creates: {', '.join(file_names)})")

        # Add user preference hint to prompt if provided
        preference_hint = ""
        if user_preference:
            if user_preference == "no_template":
                preference_hint = "\n**User Preference**: User indicated they prefer NO template (keywords: NO_TEMPLATE, FROM_SCRATCH).\nHowever, you can still recommend a template if you believe it would significantly help!"
            elif user_preference == "content_only":
                preference_hint = "\n**User Preference**: User wants CONTENT_ONLY (keywords: CONTENT_ONLY, ONLY THE CODE).\nThis suggests minimal structure. Consider 'none' unless a simple template clearly fits."
            elif user_preference == "simple":
                preference_hint = "\n**User Preference**: User wants something simple (keywords: SIMPLE, BASIC, MINIMAL).\nPrefer simpler templates or 'none' if the task is straightforward."

        prompt = f"""You are a template selection assistant for a coding agent. Your job is to analyze a task and determine which template (if any) should be used.

Available Templates:
{chr(10).join(template_list)}

Task from Main Agent: "{task}"{preference_hint}

DECISION PROCESS:
1. **First, review all available templates above**
2. **Consider the user's preference (if provided) as a HINT, but you make the final decision**
3. **If a template matches the task well, use it** (e.g., "python_script", "website", etc.)
4. **If NO template matches or you think starting from scratch is better, return "none"**
5. **When you return "none", the coding agent will:**
   - Use `web_deep_search` to get information on how to implement this (returns a simple answer, no separate context)
   - Use that information to create a TODO list with `set_todos`
   - Then implement the solution from scratch

IMPORTANT: User preferences are HINTS, not commands. If a template would genuinely help despite user preference, recommend it!

Output ONLY the template type (e.g., "python_script", "website", "python_server", "none") - no explanation, no markdown, just the type."""
        
        try:
            payload = {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 50,  # Very short response
                "temperature": 0.1  # Low temperature for deterministic choice
            }
            
            res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=10)
            res.raise_for_status()
            response_data = res.json()
            
            if 'choices' not in response_data or len(response_data['choices']) == 0:
                # LLM returned empty response - no fallback, return None
                decision_info = "Warning: LLM returned empty response\n"
                decision_info += "No template selected - will use web_deep_search and create from scratch"
                return None, decision_info
            
            content = response_data['choices'][0]['message']['content'].strip().lower()
            
            # Clean up response (remove quotes, markdown, etc.)
            content = content.strip('"\'` \n\t')
            if content.startswith('```'):
                lines = content.split('\n')
                content = '\n'.join(lines[1:-1]) if len(lines) > 2 else content
                content = content.strip()
            
            # Build decision info
            decision_info = f"Available templates: {', '.join(available_templates)}\n"
            if user_preference:
                pref_names = {"no_template": "NO template", "content_only": "CONTENT_ONLY", "simple": "SIMPLE/MINIMAL"}
                decision_info += f"User preference hint: {pref_names.get(user_preference, user_preference)}\n"
            decision_info += f"Task analyzed: {task[:80]}{'...' if len(task) > 80 else ''}\n"
            decision_info += f"LLM response: '{content}'\n"
            
            # Check if it's a valid template type
            if content in available_templates:
                decision_info += f"Decision: Using template '{content}'"
                if user_preference == "no_template":
                    decision_info += " (overriding user preference - template will help)"
                return content, decision_info
            elif content == "none":
                decision_info += "Decision: No template"
                if user_preference:
                    decision_info += " (respecting user preference)\n"
                else:
                    decision_info += " (no template matches)\n"
                decision_info += "-> Will use web_deep_search to research implementation\n"
                decision_info += "-> Then create TODO list and implement from scratch"
                return None, decision_info
            else:
                # Invalid response - no fallback, return None
                decision_info += f"Warning: Invalid LLM response '{content}'\n"
                decision_info += "No template selected - will use web_deep_search and create from scratch"
                return None, decision_info
                
        except Exception as e:
            # If LLM call fails, return None (no fallback)
            from vaf.cli.ui import UI
            UI.event("Debug", f"LLM template detection failed: {e}", style="dim")
            decision_info = f"Warning: LLM call failed: {str(e)[:60]}\n"
            decision_info += "No template selected - will use web_deep_search and create from scratch"
            return None, decision_info
    
    @classmethod
    def extract_placeholders_from_task(cls, task: str, template_type: str) -> Dict[str, str]:
        """
        Extract placeholder values from task description using simple heuristics.
        
        Args:
            task: Task description
            template_type: Template type
            
        Returns:
            Dict of placeholder values
        """
        placeholders = {}
        task_lower = task.lower()
        
        if template_type == "website":
            # Try to extract business type
            business_patterns = [
                ("handwerker", {
                    "{{BUSINESS_NAME}}": "Meisterwerk Berlin",
                    "{{HEADLINE}}": "Ihr Handwerker in Berlin",
                    "{{SUBHEADLINE}}": "Qualitätsarbeit mit Tradition und Leidenschaft",
                    "{{SERVICE_1_TITLE}}": "Reparaturen",
                    "{{SERVICE_1_DESC}}": "Schnelle und zuverlässige Reparaturen aller Art.",
                    "{{SERVICE_2_TITLE}}": "Renovierung",
                    "{{SERVICE_2_DESC}}": "Komplette Renovierungen für Ihr Zuhause.",
                    "{{SERVICE_3_TITLE}}": "Montage",
                    "{{SERVICE_3_DESC}}": "Professionelle Montage von Möbeln und mehr.",
                    "{{ABOUT_TEXT}}": "Seit über 20 Jahren sind wir Ihr zuverlässiger Partner für alle Handwerksarbeiten in Berlin.",
                    "{{FEATURE_1}}": "Über 20 Jahre Erfahrung",
                    "{{FEATURE_2}}": "Kostenlose Beratung",
                    "{{FEATURE_3}}": "Festpreisgarantie",
                }),
                ("restaurant", {
                    "{{BUSINESS_NAME}}": "Gasthaus Berlin",
                    "{{HEADLINE}}": "Willkommen im Gasthaus Berlin",
                    "{{SUBHEADLINE}}": "Traditionelle Küche mit modernem Flair",
                    "{{SERVICE_1_TITLE}}": "Speisekarte",
                    "{{SERVICE_1_DESC}}": "Entdecken Sie unsere saisonalen Gerichte.",
                    "{{SERVICE_2_TITLE}}": "Reservierung",
                    "{{SERVICE_2_DESC}}": "Reservieren Sie Ihren Tisch online oder telefonisch.",
                    "{{SERVICE_3_TITLE}}": "Events",
                    "{{SERVICE_3_DESC}}": "Feiern Sie Ihre besonderen Anlässe bei uns.",
                }),
                ("portfolio", {
                    "{{BUSINESS_NAME}}": "Mein Portfolio",
                    "{{HEADLINE}}": "Kreative Lösungen",
                    "{{SUBHEADLINE}}": "Design & Entwicklung",
                }),
            ]
            
            for pattern, values in business_patterns:
                if pattern in task_lower:
                    placeholders.update(values)
                    break
            
            # Extract location if mentioned
            if "berlin" in task_lower:
                if "{{ADDRESS}}" not in placeholders:
                    placeholders["{{ADDRESS}}"] = "Musterstraße 1, 10115 Berlin"
        
        return placeholders

