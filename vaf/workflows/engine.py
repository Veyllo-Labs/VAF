"""
VAF Workflow Engine - Core execution logic for multi-step pipelines

The engine executes a sequence of tool calls, automatically passing
outputs from one step as inputs to the next.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable
from enum import Enum


class StepStatus(Enum):
    """Status of a workflow step."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WorkflowStep:
    """A single step in a workflow."""
    tool: str                           # Tool name to execute
    input_template: str                 # Input with {variables}
    output_name: str                    # Name for this step's output
    description: str = ""               # Human-readable description
    optional: bool = False              # Skip on failure instead of abort
    condition: Optional[str] = None     # Only run if condition met
    
    # Runtime state
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    duration: float = 0.0


@dataclass
class WorkflowResult:
    """Result of a complete workflow execution."""
    success: bool
    outputs: Dict[str, Any]             # All step outputs by name
    final_output: Any                   # Last step's output
    steps: List[WorkflowStep]           # All steps with status
    total_duration: float
    error: Optional[str] = None


class WorkflowEngine:
    """
    Executes multi-step workflows with automatic output chaining.
    
    Example:
        engine = WorkflowEngine(tools)
        result = engine.execute([
            WorkflowStep("web_search", "{query}", "research"),
            WorkflowStep("coding_agent", "Create code based on:\n{research}", "code"),
            WorkflowStep("write_file", '{"path": "output.py", "content": "{code}"}', "saved"),
        ], variables={"query": "Python web scraping"})
    """
    
    def __init__(self, tools: Dict[str, Any], callback: Callable = None):
        """
        Initialize the workflow engine.
        
        Args:
            tools: Dict mapping tool names to tool instances
            callback: Optional callback for progress updates
        """
        self.tools = tools
        self.callback = callback or (lambda *args: None)
    
    def execute(
        self, 
        steps: List[WorkflowStep], 
        variables: Dict[str, Any] = None,
        stop_on_error: bool = True
    ) -> WorkflowResult:
        """
        Execute a workflow with the given steps.
        
        Args:
            steps: List of workflow steps to execute
            variables: Initial variables (user inputs)
            stop_on_error: Stop workflow on first error (default: True)
            
        Returns:
            WorkflowResult with all outputs and status
        """
        from vaf.cli.ui import UI
        
        start_time = time.time()
        outputs: Dict[str, Any] = dict(variables or {})
        final_output = None
        error = None
        
        UI.event("Workflow", f"Starting {len(steps)}-step workflow...", style="bold cyan")
        
        for i, step in enumerate(steps, 1):
            step_start = time.time()
            step.status = StepStatus.RUNNING
            
            # Check condition if specified
            if step.condition and not self._evaluate_condition(step.condition, outputs):
                step.status = StepStatus.SKIPPED
                self.callback("skip", step, i, len(steps))
                UI.event("Workflow", f"Step {i}/{len(steps)}: {step.tool} [Skipped]", style="dim")
                continue
            
            # Progress callback
            self.callback("start", step, i, len(steps))
            UI.event("Workflow", f"Step {i}/{len(steps)}: {step.tool}", style="info")
            
            # Check if tool exists
            if step.tool not in self.tools:
                step.status = StepStatus.FAILED
                step.error = f"Tool not found: {step.tool}"
                error = step.error
                UI.error(f"  → {step.error}")
                
                if stop_on_error and not step.optional:
                    break
                continue
            
            # Resolve input template with current outputs
            try:
                resolved_input = self._resolve_template(step.input_template, outputs)
            except KeyError as e:
                step.status = StepStatus.FAILED
                step.error = f"Missing variable: {e}"
                error = step.error
                UI.error(f"  → {step.error}")
                
                if stop_on_error and not step.optional:
                    break
                continue
            
            # Execute the tool
            try:
                tool = self.tools[step.tool]
                
                # Parse input - could be JSON dict or simple string
                if resolved_input.strip().startswith("{") and resolved_input.strip().endswith("}"):
                    import json
                    try:
                        args = json.loads(resolved_input)
                    except json.JSONDecodeError:
                        # Not valid JSON, treat as single argument
                        args = self._infer_args(step.tool, resolved_input)
                else:
                    args = self._infer_args(step.tool, resolved_input)
                
                # Run the tool
                result = tool.run(**args)
                
                step.status = StepStatus.SUCCESS
                step.result = result
                step.duration = time.time() - step_start
                
                # Store output for next steps
                outputs[step.output_name] = result
                final_output = result
                
                # Truncate for display
                display_result = str(result)[:100] + "..." if len(str(result)) > 100 else str(result)
                UI.event("Workflow", f"  ✓ {step.output_name}: {display_result}", style="success")
                
                self.callback("success", step, i, len(steps))
                
            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                step.duration = time.time() - step_start
                error = step.error
                
                UI.error(f"  ✗ Error: {step.error}")
                self.callback("error", step, i, len(steps))
                
                if stop_on_error and not step.optional:
                    break
        
        total_duration = time.time() - start_time
        
        # Determine overall success
        success = all(
            s.status in (StepStatus.SUCCESS, StepStatus.SKIPPED) 
            for s in steps
        )
        
        if success:
            UI.event("Workflow", f"Completed in {total_duration:.1f}s", style="success")
        else:
            UI.event("Workflow", f"Failed after {total_duration:.1f}s", style="error")
        
        return WorkflowResult(
            success=success,
            outputs=outputs,
            final_output=final_output,
            steps=steps,
            total_duration=total_duration,
            error=error
        )
    
    def _resolve_template(self, template: str, variables: Dict[str, Any]) -> str:
        """
        Replace {variable} placeholders with actual values.
        
        Supports:
        - Simple: {query}
        - Nested: {step1.field}
        - Default: {var|default_value}
        """
        def replacer(match):
            key = match.group(1)
            
            # Handle default values: {var|default}
            if "|" in key:
                key, default = key.split("|", 1)
                return str(variables.get(key.strip(), default.strip()))
            
            # Handle nested access: {step.field}
            if "." in key:
                parts = key.split(".")
                value = variables
                for part in parts:
                    if isinstance(value, dict):
                        value = value.get(part)
                    else:
                        value = getattr(value, part, None)
                    if value is None:
                        raise KeyError(key)
                return str(value)
            
            # Simple variable
            if key not in variables:
                raise KeyError(key)
            return str(variables[key])
        
        return re.sub(r"\{([^}]+)\}", replacer, template)
    
    def _infer_args(self, tool_name: str, value: str) -> Dict[str, Any]:
        """
        Infer the argument name for a tool based on its expected parameters.
        """
        # Common tool argument mappings
        ARG_MAPPINGS = {
            "web_search": "query",
            "webfetch": "url",
            "coding_agent": "task",
            "librarian_agent": "task",
            "read_file": "path",
            "write_file": "content",  # Usually needs path too
            "bash": "command",
            "list_files": "path",
            "move_file": "source",
        }
        
        arg_name = ARG_MAPPINGS.get(tool_name, "input")
        return {arg_name: value}
    
    def _evaluate_condition(self, condition: str, variables: Dict[str, Any]) -> bool:
        """
        Evaluate a simple condition string.
        
        Supports:
        - {var} exists
        - {var} == "value"
        - {var} != "value"
        - {var} contains "text"
        """
        # Resolve any variables in the condition
        resolved = self._resolve_template(condition, variables)
        
        # Simple truthy check
        if resolved.lower() in ("true", "yes", "1"):
            return True
        if resolved.lower() in ("false", "no", "0", ""):
            return False
        
        # If it's just a variable name check, see if it exists and has value
        return bool(resolved.strip())


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def create_workflow(template: Dict[str, Any]) -> List[WorkflowStep]:
    """
    Create workflow steps from a template definition.
    
    Args:
        template: Dict with 'steps' list
        
    Returns:
        List of WorkflowStep objects
    """
    steps = []
    for i, step_def in enumerate(template.get("steps", [])):
        steps.append(WorkflowStep(
            tool=step_def["tool"],
            input_template=step_def["input"],
            output_name=step_def.get("output", f"step_{i+1}"),
            description=step_def.get("description", ""),
            optional=step_def.get("optional", False),
            condition=step_def.get("condition"),
        ))
    return steps

