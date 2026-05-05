"""
VAF Workflow Templates - Pre-defined pipelines for common tasks

Workflows are automatically discovered and loaded from:
1. vaf/workflows/workflows/*.py (built-in workflows)
2. ~/.vaf/workflows/*.py (user-generated workflows)

Each workflow file must define a WORKFLOW dictionary with:
- name: Display name
- description: What the workflow does
- triggers: List of trigger phrases
- trigger_patterns: List of regex patterns
- variables: Dictionary of required variables
- defaults: (optional) Default values for variables
- steps: List of tool call steps
"""

import os
import importlib.util
from pathlib import Path
from typing import Dict, Any, List


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOMATIC WORKFLOW LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def _load_workflow_from_file(file_path: Path) -> tuple[str, Dict[str, Any]]:
    """
    Load a WORKFLOW dictionary from a Python file.
    
    Returns:
        (workflow_id, workflow_dict) tuple
    """
    workflow_id = file_path.stem  # filename without .py extension
    
    # Load the module
    spec = importlib.util.spec_from_file_location(workflow_id, file_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load workflow from {file_path}")
    
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    # Get the WORKFLOW dictionary
    if not hasattr(module, "WORKFLOW"):
        raise ValueError(f"Workflow file {file_path} must define a WORKFLOW dictionary")
    
    workflow = module.WORKFLOW
    
    # Validate required fields
    required_fields = ["name", "description", "triggers", "steps"]
    for field in required_fields:
        if field not in workflow:
            raise ValueError(f"Workflow {workflow_id} missing required field: {field}")
    
    return workflow_id, workflow


def _discover_workflows(directory: Path) -> Dict[str, Dict[str, Any]]:
    """
    Discover and load all workflows from a directory.
    
    Args:
        directory: Path to directory containing workflow .py files
        
    Returns:
        Dictionary mapping workflow_id -> workflow_dict
    """
    workflows = {}
    
    if not directory.exists():
        return workflows
    
    # Find all .py files (except __init__.py)
    for file_path in directory.glob("*.py"):
        if file_path.name == "__init__.py":
            continue
        
        try:
            workflow_id, workflow = _load_workflow_from_file(file_path)
            workflows[workflow_id] = workflow
        except Exception as e:
            # Log error but continue loading other workflows
            print(f"Warning: Could not load workflow from {file_path}: {e}")
            continue
    
    return workflows


def _load_all_workflows() -> Dict[str, Dict[str, Any]]:
    """
    Load all workflows from built-in and user directories.
    
    Returns:
        Dictionary mapping workflow_id -> workflow_dict
    """
    workflows = {}
    
    # 1. Load built-in workflows from vaf/workflows/workflows/
    builtin_dir = Path(__file__).parent / "workflows"
    if builtin_dir.exists():
        builtin_workflows = _discover_workflows(builtin_dir)
        workflows.update(builtin_workflows)
    
    # 2. Load user-generated workflows from ~/.vaf/workflows/
    user_workflows_dir = Path.home() / ".vaf" / "workflows"
    if user_workflows_dir.exists():
        user_workflows = _discover_workflows(user_workflows_dir)
        # User workflows can override built-in ones
        workflows.update(user_workflows)
    
    return workflows


# Load all workflows automatically
WORKFLOW_TEMPLATES: Dict[str, Dict[str, Any]] = _load_all_workflows()


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_template(name: str) -> Dict[str, Any]:
    """Get a workflow template by name."""
    return WORKFLOW_TEMPLATES.get(name)


def list_templates() -> List[Dict[str, str]]:
    """List all available templates with name and description."""
    return [
        {
            "id": key,
            "name": template["name"],
            "description": template["description"],
            "steps": len(template["steps"]),
        }
        for key, template in WORKFLOW_TEMPLATES.items()
    ]


def get_template_names() -> List[str]:
    """Get list of template names."""
    return list(WORKFLOW_TEMPLATES.keys())


def reload_workflows() -> None:
    """Reload all workflows (useful after adding new user workflows)."""
    global WORKFLOW_TEMPLATES
    WORKFLOW_TEMPLATES = _load_all_workflows()
