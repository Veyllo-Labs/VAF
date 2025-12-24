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
                {"name": "index.html", "template": "website_html.html"},
                {"name": "styles.css", "template": "website_css.css"},
                {"name": "script.js", "template": "website_js.js"},
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
            "description": "Python script with main function",
            "files": [
                {"name": "main.py", "template": "python_main.py"},
            ],
            "placeholders": {}
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
        """Load a template file's contents."""
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
        
        # Python script detection
        python_keywords = ['python', 'script', 'skript', '.py']
        if any(kw in task_lower for kw in python_keywords):
            return "python_script"
        
        return None
    
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

