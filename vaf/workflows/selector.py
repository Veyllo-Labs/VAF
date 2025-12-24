"""
VAF Workflow Selector - Intelligent template matching

The selector analyzes user input and determines:
1. If a workflow template matches
2. Which variables need to be extracted
3. Falls back to dynamic workflow building if no match
"""

import re
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from vaf.workflows.templates import WORKFLOW_TEMPLATES, get_template


@dataclass
class SelectorResult:
    """Result of workflow selection."""
    matched: bool                       # Whether a template matched
    template_id: Optional[str]          # Template ID if matched
    template: Optional[Dict[str, Any]]  # Full template if matched
    confidence: float                   # Match confidence (0.0 - 1.0)
    variables: Dict[str, Any]           # Extracted/inferred variables
    missing_variables: List[str]        # Variables that couldn't be extracted
    suggestion: Optional[str]           # Suggestion for user if variables missing


class WorkflowSelector:
    """
    Matches user input to workflow templates.
    
    Uses multi-stage matching:
    1. Exact trigger phrase matching
    2. Regex pattern matching
    3. Keyword scoring
    4. Optional LLM fallback for complex cases
    """
    
    def __init__(self, templates: Dict[str, Any] = None):
        """
        Initialize the selector.
        
        Args:
            templates: Custom templates (default: WORKFLOW_TEMPLATES)
        """
        self.templates = templates or WORKFLOW_TEMPLATES
    
    def select(self, user_input: str) -> SelectorResult:
        """
        Select the best matching workflow template.
        
        Args:
            user_input: User's natural language input
            
        Returns:
            SelectorResult with match info and variables
        """
        input_lower = user_input.lower().strip()
        
        best_match = None
        best_score = 0.0
        
        for template_id, template in self.templates.items():
            score = self._calculate_match_score(input_lower, template)
            
            if score > best_score:
                best_score = score
                best_match = template_id
        
        # Threshold for accepting a match
        if best_score >= 0.3:  # 30% confidence minimum
            template = self.templates[best_match]
            variables, missing = self._extract_variables(user_input, template)
            
            suggestion = None
            if missing:
                suggestion = self._generate_suggestion(template, missing)
            
            return SelectorResult(
                matched=True,
                template_id=best_match,
                template=template,
                confidence=best_score,
                variables=variables,
                missing_variables=missing,
                suggestion=suggestion,
            )
        
        # No match found
        return SelectorResult(
            matched=False,
            template_id=None,
            template=None,
            confidence=best_score,
            variables={},
            missing_variables=[],
            suggestion=None,
        )
    
    def _calculate_match_score(self, input_lower: str, template: Dict[str, Any]) -> float:
        """
        Calculate how well the input matches a template.
        
        Returns score from 0.0 to 1.0
        """
        score = 0.0
        
        # Stage 1: Exact trigger phrase matching (highest weight)
        triggers = template.get("triggers", [])
        for trigger in triggers:
            if trigger.lower() in input_lower:
                # Full trigger match
                score = max(score, 0.9)
                break
            elif any(word in input_lower for word in trigger.lower().split()):
                # Partial word match
                score = max(score, 0.4)
        
        # Stage 2: Regex pattern matching
        patterns = template.get("trigger_patterns", [])
        for pattern in patterns:
            try:
                if re.search(pattern, input_lower, re.IGNORECASE):
                    score = max(score, 0.8)
                    break
            except re.error:
                continue
        
        # Stage 3: Keyword scoring (additive)
        keywords = self._extract_keywords(template)
        matching_keywords = sum(1 for kw in keywords if kw in input_lower)
        if keywords:
            keyword_score = matching_keywords / len(keywords) * 0.5
            score = max(score, keyword_score)
        
        # Bonus: URL detection for web-related workflows
        if "http" in input_lower or "www." in input_lower:
            if template.get("name") in ["Analyze Website", "Web Lookup"]:
                score += 0.2
        
        # Bonus: File path detection for file-related workflows
        if re.search(r"\.\w{2,4}$", input_lower) or "/" in input_lower or "\\" in input_lower:
            if "file" in template.get("name", "").lower():
                score += 0.2
        
        return min(score, 1.0)  # Cap at 1.0
    
    def _extract_keywords(self, template: Dict[str, Any]) -> List[str]:
        """Extract relevant keywords from a template."""
        keywords = set()
        
        # From name
        keywords.update(template.get("name", "").lower().split())
        
        # From description
        keywords.update(template.get("description", "").lower().split())
        
        # From triggers
        for trigger in template.get("triggers", []):
            keywords.update(trigger.lower().split())
        
        # Filter common words
        stopwords = {"the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "is", "it", "this", "that", "with"}
        keywords = {k for k in keywords if k not in stopwords and len(k) > 2}
        
        return list(keywords)
    
    def _extract_variables(
        self, 
        user_input: str, 
        template: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        Extract variable values from user input.
        
        Returns:
            Tuple of (extracted_variables, missing_variables)
        """
        variables = {}
        missing = []
        
        required_vars = template.get("variables", {})
        defaults = template.get("defaults", {})
        
        for var_name, var_desc in required_vars.items():
            value = self._extract_value(user_input, var_name, var_desc)
            
            if value:
                variables[var_name] = value
            elif var_name in defaults:
                variables[var_name] = defaults[var_name]
            else:
                missing.append(var_name)
        
        return variables, missing
    
    def _extract_value(self, user_input: str, var_name: str, var_desc: str) -> Optional[str]:
        """
        Try to extract a specific variable value from input.
        """
        input_lower = user_input.lower()
        
        # URL extraction
        if "url" in var_name.lower() or "url" in var_desc.lower():
            url_match = re.search(r'https?://[^\s<>"{}|\\^`\[\]]+', user_input)
            if url_match:
                return url_match.group(0)
        
        # File path extraction
        if "path" in var_name.lower() or "file" in var_name.lower():
            # Look for file paths
            path_match = re.search(r'["\']?([./\\]?[\w./\\-]+\.\w{1,10})["\']?', user_input)
            if path_match:
                return path_match.group(1)
        
        # Filename extraction
        if "filename" in var_name.lower():
            filename_match = re.search(r'["\']?([\w-]+\.\w{1,10})["\']?', user_input)
            if filename_match:
                return filename_match.group(1)
        
        # Query/Topic extraction - use the main content after removing trigger words
        if var_name.lower() in ("query", "topic", "description"):
            # Remove common trigger phrases
            cleaned = user_input
            trigger_phrases = [
                # German
                "recherchiere", "suche nach", "finde",
                "erstelle", "erstell", "erzeuge", "generiere",
                "mache", "mach mir", "baue", "bau mir",
                "was ist", "wie funktioniert", "analysiere",
                "basierend auf", "und", "dafür",
                "eine", "einen", "ein", "die", "der", "das",
                # English
                "search for", "find", "look up",
                "create", "make", "build", "generate",
                "what is", "how does", "analyze",
                "based on", "and", "for me",
                "a", "an", "the",
            ]
            for phrase in trigger_phrases:
                cleaned = re.sub(rf'\b{phrase}\b', '', cleaned, flags=re.IGNORECASE)
            
            # Clean up
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            
            # If cleaned is empty or too short, use original input
            if not cleaned or len(cleaned) < 5:
                return user_input
            
            return cleaned
        
        return None
    
    def _generate_suggestion(self, template: Dict[str, Any], missing: List[str]) -> str:
        """Generate a helpful suggestion for missing variables."""
        var_descs = template.get("variables", {})
        
        suggestions = []
        for var in missing:
            desc = var_descs.get(var, var)
            suggestions.append(f"  • {var}: {desc}")
        
        return f"Please provide:\n" + "\n".join(suggestions)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # UTILITY METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def list_available(self) -> List[Dict[str, str]]:
        """List all available workflow templates."""
        return [
            {
                "id": key,
                "name": template["name"],
                "description": template["description"],
                "triggers": template.get("triggers", [])[:3],  # First 3 triggers
            }
            for key, template in self.templates.items()
        ]
    
    def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific template by ID."""
        return self.templates.get(template_id)
    
    def suggest_for_input(self, user_input: str, top_n: int = 3) -> List[Dict[str, Any]]:
        """
        Suggest the top N matching workflows for an input.
        
        Useful for showing the user options.
        """
        scores = []
        input_lower = user_input.lower().strip()
        
        for template_id, template in self.templates.items():
            score = self._calculate_match_score(input_lower, template)
            if score > 0.1:  # Minimum threshold
                scores.append({
                    "id": template_id,
                    "name": template["name"],
                    "description": template["description"],
                    "confidence": round(score, 2),
                })
        
        # Sort by confidence
        scores.sort(key=lambda x: x["confidence"], reverse=True)
        
        return scores[:top_n]


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def select_workflow(user_input: str) -> SelectorResult:
    """
    Quick function to select a workflow.
    
    Usage:
        result = select_workflow("Recherchiere Python web scraping und erstelle code")
        if result.matched:
            print(f"Using: {result.template['name']}")
    """
    selector = WorkflowSelector()
    return selector.select(user_input)

