"""
VAF Workflow Engine - Automated Multi-Tool Pipelines

This module provides:
- WorkflowEngine: Execute multi-step tool chains automatically
- WorkflowSelector: Match user input to optimal workflow templates
- Pre-defined templates for common tasks

Usage:
    from vaf.workflows import WorkflowSelector, WorkflowEngine, create_workflow
    
    # Auto-select workflow from user input
    result = WorkflowSelector().select("Recherchiere Python und erstelle Code")
    if result.matched:
        steps = create_workflow(result.template)
        engine = WorkflowEngine(tools)
        engine.execute(steps, variables=result.variables)
"""

from vaf.workflows.engine import WorkflowEngine, WorkflowStep, WorkflowResult, create_workflow
from vaf.workflows.selector import WorkflowSelector, select_workflow
from vaf.workflows.templates import WORKFLOW_TEMPLATES, get_template, list_templates

__all__ = [
    # Engine
    "WorkflowEngine",
    "WorkflowStep", 
    "WorkflowResult",
    "create_workflow",
    # Selector
    "WorkflowSelector",
    "select_workflow",
    # Templates
    "WORKFLOW_TEMPLATES",
    "get_template",
    "list_templates",
]

