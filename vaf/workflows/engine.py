"""
VAF Workflow Engine - Core execution logic for multi-step pipelines

The engine executes a sequence of tool calls, automatically passing
outputs from one step as inputs to the next.
"""

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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
    input_template: str                 # Input with {variables} (single-arg tools)
    output_name: str                    # Name for this step's output
    description: str = ""               # Human-readable description
    optional: bool = False              # Skip on failure instead of abort
    condition: Optional[str] = None     # Only run if condition met
    args_template: Optional[Dict[str, Any]] = None  # Multi-arg tool inputs (safer than JSON strings)
    
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
    paused: bool = False                # True if workflow is paused waiting for sub-agent
    workflow_id: Optional[str] = None   # ID for resuming paused workflow
    waiting_for_task: Optional[str] = None  # Task ID we're waiting for


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
        
        # Initialize context manager for workflow execution (like main agent)
        from vaf.core.context import ContextManager
        self.context_manager = ContextManager(max_tokens=8192)
    
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
        
        # Store defaults for use in template resolution (defaults not passed for automations)
        self._workflow_defaults = {}
        
        start_time = time.time()
        outputs: Dict[str, Any] = dict(variables or {})
        # Merge defaults into outputs if not already present
        for key, value in self._workflow_defaults.items():
            if key not in outputs:
                outputs[key] = value
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
            
            # Build tool args (prefer args_template to avoid fragile JSON templating)
            # Get defaults from workflow template if available
            defaults = getattr(self, '_workflow_defaults', {})
            try:
                if step.args_template is not None:
                    args: Dict[str, Any] = {}
                    # Debug: Prepare variable info for coding_agent
                    available_vars = list(outputs.keys()) if step.tool == "coding_agent" else []
                    
                    for k, v in step.args_template.items():
                        if isinstance(v, str):
                            # Debug: Show variable resolution for coding_agent
                            if step.tool == "coding_agent" and k == "task":
                                UI.event("System", f"Resolving variables in {step.tool} task parameter...", style="dim")
                                if available_vars:
                                    UI.event("System", f"Available variables: {', '.join(available_vars)}", style="dim")
                                # Show template before resolution
                                template_preview = v[:200] + "..." if len(v) > 200 else v
                                UI.event("System", f"Template (before): {template_preview}", style="dim")
                            
                            resolved_value = self._resolve_template(v, outputs, defaults)
                            args[k] = resolved_value
                            
                            # Debug: Show resolved value for coding_agent
                            if step.tool == "coding_agent" and k == "task":
                                resolved_preview = resolved_value[:300] + "..." if len(resolved_value) > 300 else resolved_value
                                UI.event("System", f"Resolved task (first 300 chars): {resolved_preview}", style="dim")
                                # Show variable sizes
                                if available_vars:
                                    for var_name in available_vars:
                                        var_value = str(outputs.get(var_name, ""))
                                        var_size = len(var_value)
                                        UI.event("System", f"  Variable '{var_name}': {var_size} chars", style="dim")
                        else:
                            args[k] = v
                else:
                    resolved_input = self._resolve_template(step.input_template, outputs, defaults)
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

                # Parse input - could be explicit args_template, JSON dict, or simple string
                if step.args_template is None:
                    if resolved_input.strip().startswith("{") and resolved_input.strip().endswith("}"):
                        import json
                        try:
                            args = json.loads(resolved_input)
                        except json.JSONDecodeError:
                            # Not valid JSON, treat as single argument
                            args = self._infer_args(step.tool, resolved_input)
                    else:
                        args = self._infer_args(step.tool, resolved_input)
                
                # ═══════════════════════════════════════════════════════════════
                # CONTEXT MANAGEMENT: Truncate large inputs before passing to tools
                # ═══════════════════════════════════════════════════════════════
                # For tools that accept large text inputs (like coding_agent),
                # truncate very large variable values to prevent context overflow
                MAX_INPUT_SIZE = 5000  # Max chars per input parameter
                args_snapshot = {k: v for k, v in args.items()}  # Snapshot for retry
                
                for key, value in args.items():
                    if isinstance(value, str) and len(value) > MAX_INPUT_SIZE:
                        truncated = value[:MAX_INPUT_SIZE] + f"\n\n[... {len(value) - MAX_INPUT_SIZE} more characters truncated to prevent context overflow ...]"
                        UI.event("Workflow", f"  [INFO] Truncated {key} input: {len(value)} → {MAX_INPUT_SIZE} chars", style="dim")
                        args[key] = truncated
                
                # Snapshot outputs before tool execution (for retry on failure)
                outputs_snapshot = {k: v for k, v in outputs.items()}
                
                # Run the tool with retry logic (like main agent)
                #
                # NOTE: Some tools (e.g. research_agent) use Rich Live/ANSI animations.
                # When running inside the workflow engine, those animations can spam the
                # output because workflow execution isn't always a "true" interactive TTY.
                # We set an env flag so tools can gracefully switch to a non-Live mode.
                import os
                prev_in_workflow = os.environ.get("VAF_IN_WORKFLOW")
                os.environ["VAF_IN_WORKFLOW"] = "1"

                # Sub-agent debug logging context for in-workflow sub-agent tools.
                # These tools may run in-process (not via `subagent run`), so we set
                # VAF_AGENT_TYPE/VAF_TASK_ID here to enable per-run logs.
                prev_agent_type = os.environ.get("VAF_AGENT_TYPE")
                prev_task_id = os.environ.get("VAF_TASK_ID")
                prev_in_subagent_term = os.environ.get("VAF_IN_SUBAGENT_TERMINAL")
                subagent_step_task_id = None
                is_subagent_tool = step.tool in ("coding_agent", "librarian_agent", "research_agent")
                if is_subagent_tool:
                    subagent_step_task_id = f"{step.tool}-{i}-{str(uuid.uuid4())[:6]}"
                    os.environ["VAF_AGENT_TYPE"] = step.tool
                    os.environ["VAF_TASK_ID"] = subagent_step_task_id
                    # Prevent nested terminal spawning during workflows.
                    if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() not in ("1", "true", "yes"):
                        os.environ["VAF_IN_SUBAGENT_TERMINAL"] = "1"
                    try:
                        from vaf.core.subagent_debug import get_subagent_logger_from_env
                        lg = get_subagent_logger_from_env()
                        if lg:
                            lg.event(
                                "workflow_subagent_step_start",
                                workflow_name=getattr(self, "_workflow_name", ""),
                                step_index=i,
                                total_steps=len(steps),
                            )
                    except Exception:
                        pass
                
                # Retry logic for context errors (like main agent)
                max_retries = 3
                retry_count = 0
                result = None
                
                while retry_count < max_retries:
                    try:
                        result = tool.run(**args)
                        
                        # Check if result contains context overflow error (like main agent)
                        result_str = str(result)
                        is_context_error_in_result = (
                            "context" in result_str.lower() and 
                            ("exceed" in result_str.lower() or "size" in result_str.lower() or "token" in result_str.lower())
                        ) or (
                            "400" in result_str and "context" in result_str.lower()
                        )
                        
                        # If context error in result, retry with more aggressive truncation
                        if is_context_error_in_result:
                            retry_count += 1
                            if retry_count < max_retries:
                                UI.event("Workflow", f"  [RETRY {retry_count}/{max_retries}] Context overflow in result, truncating inputs...", style="warning")
                                
                                # Restore args from snapshot
                                args = {k: v for k, v in args_snapshot.items()}
                                
                                # More aggressive truncation on retry (like main agent's aggressive compression)
                                new_max = MAX_INPUT_SIZE // (retry_count + 1)
                                for key, value in args.items():
                                    if isinstance(value, str) and len(value) > new_max:
                                        args[key] = value[:new_max] + f"\n\n[... {len(value) - new_max} more characters truncated (retry {retry_count}) ...]"
                                        UI.event("Workflow", f"  [RETRY] Aggressively truncated {key}: {len(value)} → {new_max} chars", style="dim")
                                
                                # Restore outputs from snapshot before retry
                                outputs = {k: v for k, v in outputs_snapshot.items()}
                                
                                # Continue retry loop
                                continue
                            else:
                                # Max retries reached - treat as error
                                step.status = StepStatus.FAILED
                                step.error = f"Context size error after {max_retries} retries"
                                step.result = result
                                step.duration = time.time() - step_start
                                error = step.error
                                
                                UI.error(f"  [FAIL] {step.error}")
                                self.callback("error", step, i, len(steps))
                                
                                if stop_on_error and not step.optional:
                                    break
                                continue
                        
                        # Success - exit retry loop
                        break
                        
                    except Exception as e:
                        error_str = str(e).lower()
                        # Check if it's a context size error (like main agent handles 500)
                        is_context_error = (
                            "context" in error_str and 
                            ("exceed" in error_str or "size" in error_str or "token" in error_str)
                        ) or (
                            hasattr(e, 'response') and 
                            hasattr(e.response, 'status_code') and 
                            e.response.status_code == 500 and
                            "context" in str(e.response.text or "").lower()
                        )
                        
                        if is_context_error:
                            retry_count += 1
                            if retry_count < max_retries:
                                # Restore from snapshot and truncate more aggressively (like main agent)
                                UI.event("Workflow", f"  [RETRY {retry_count}/{max_retries}] Context overflow detected, truncating inputs...", style="warning")
                                
                                # Restore args from snapshot
                                args = {k: v for k, v in args_snapshot.items()}
                                
                                # More aggressive truncation on retry (like main agent's aggressive compression)
                                new_max = MAX_INPUT_SIZE // (retry_count + 1)
                                for key, value in args.items():
                                    if isinstance(value, str) and len(value) > new_max:
                                        args[key] = value[:new_max] + f"\n\n[... {len(value) - new_max} more characters truncated (retry {retry_count}) ...]"
                                        UI.event("Workflow", f"  [RETRY] Aggressively truncated {key}: {len(value)} → {new_max} chars", style="dim")
                                
                                # Restore outputs from snapshot before retry
                                outputs = {k: v for k, v in outputs_snapshot.items()}
                                
                                continue
                            else:
                                # Max retries reached
                                raise Exception(f"Context size error after {max_retries} retries: {e}")
                        else:
                            # Not a context error - re-raise immediately
                            raise
                
                # Sub-agent step end (result summary only)
                if is_subagent_tool:
                    try:
                        from vaf.core.subagent_debug import get_subagent_logger_from_env, summarize_result
                        lg = get_subagent_logger_from_env()
                        if lg:
                            lg.event(
                                "workflow_subagent_step_end",
                                ok=True,
                                workflow_name=getattr(self, "_workflow_name", ""),
                                step_index=i,
                                total_steps=len(steps),
                                **summarize_result(result),
                            )
                    except Exception:
                        pass

                # Cleanup environment variable (after while loop)
                if prev_in_workflow is None:
                    os.environ.pop("VAF_IN_WORKFLOW", None)
                else:
                    os.environ["VAF_IN_WORKFLOW"] = prev_in_workflow

                # Restore sub-agent debug env vars (avoid leaking into other tools)
                if is_subagent_tool:
                    if prev_agent_type is None:
                        os.environ.pop("VAF_AGENT_TYPE", None)
                    else:
                        os.environ["VAF_AGENT_TYPE"] = prev_agent_type
                    if prev_task_id is None:
                        os.environ.pop("VAF_TASK_ID", None)
                    else:
                        os.environ["VAF_TASK_ID"] = prev_task_id
                    if prev_in_subagent_term is None:
                        os.environ.pop("VAF_IN_SUBAGENT_TERMINAL", None)
                    else:
                        os.environ["VAF_IN_SUBAGENT_TERMINAL"] = prev_in_subagent_term
                
                # ═══════════════════════════════════════════════════════════════
                # ASYNC SUB-AGENT HANDLING: Pause workflow and yield control
                # ═══════════════════════════════════════════════════════════════
                # If result contains async marker, save state and return immediately
                # The workflow will be resumed when the sub-agent finishes
                import re
                result_str_check = str(result) if result else ""
                async_match = re.search(r'\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]', result_str_check)
                
                if async_match:
                    task_id = async_match.group(1)
                    agent_type = async_match.group(2)
                    
                    # Generate workflow ID for tracking
                    workflow_id = str(uuid.uuid4())[:8]
                    
                    UI.event("Workflow", f"  ⏸️  Pausing workflow - {agent_type} [Task: {task_id}] running in background", style="cyan")
                    UI.info(f"  💡 You can continue using the main agent. Workflow will resume automatically.")
                    
                    from vaf.core.subagent_ipc import get_ipc, PausedWorkflow
                    
                    # Serialize current state
                    steps_data = []
                    for s in steps:
                        steps_data.append({
                            'tool': s.tool,
                            'input_template': s.input_template,
                            'output_name': s.output_name,
                            'description': s.description,
                            'optional': s.optional,
                            'condition': s.condition,
                            'args_template': s.args_template,
                            'status': s.status.value,
                            'result': s.result,
                            'error': s.error,
                            'duration': s.duration,
                        })
                    
                    # Save paused workflow state
                    paused_wf = PausedWorkflow(
                        workflow_id=workflow_id,
                        waiting_for_task_id=task_id,
                        current_step_index=i - 1,  # 0-based index (i is 1-based)
                        outputs=outputs,
                        variables=dict(variables or {}),
                        steps_data=steps_data,
                        workflow_name=getattr(self, '_workflow_name', 'unknown'),
                        created_at=datetime.now().isoformat()
                    )
                    
                    ipc = get_ipc()
                    ipc.pause_workflow(paused_wf)
                    
                    # Return paused result - control goes back to user
                    return WorkflowResult(
                        success=False,  # Not complete yet
                        outputs=outputs,
                        final_output=None,
                        steps=steps,
                        total_duration=time.time() - start_time,
                        error=None,
                        paused=True,
                        workflow_id=workflow_id,
                        waiting_for_task=task_id
                    )
                
                # Check if result indicates failure (even if no exception was raised)
                if result is None:
                    # Should not happen, but handle gracefully
                    step.status = StepStatus.FAILED
                    step.error = "Tool returned no result"
                    step.duration = time.time() - step_start
                    error = step.error
                    UI.error(f"  [FAIL] {step.error}")
                    self.callback("error", step, i, len(steps))
                    if stop_on_error and not step.optional:
                        break
                    continue
                
                result_str = str(result)
                is_error_result = (
                    result_str.startswith("### ❌") or
                    result_str.startswith("❌") or
                    result_str.startswith("Error:") or
                    "Task Failed" in result_str or
                    "task failed" in result_str.lower() or
                    (result_str.startswith("###") and "❌" in result_str[:200]) or
                    ("error:" in result_str.lower() and result_str.lower().startswith("error"))
                )
                
                if is_error_result:
                    step.status = StepStatus.FAILED
                    step.error = f"Tool returned error message: {result_str[:200]}"
                    step.result = result
                    step.duration = time.time() - step_start
                    error = step.error
                    
                    UI.error(f"  [FAIL] {step.error}")
                    self.callback("error", step, i, len(steps))
                    
                    if stop_on_error and not step.optional:
                        break
                    continue
                
                step.status = StepStatus.SUCCESS
                step.result = result
                step.duration = time.time() - step_start
                
                # Store output for next steps
                outputs[step.output_name] = result
                final_output = result
                
                # Truncate for display
                display_result = str(result)[:100] + "..." if len(str(result)) > 100 else str(result)
                # Use ASCII markers for maximum terminal compatibility (avoid UnicodeEncodeError on some Windows consoles)
                UI.event("Workflow", f"  [OK] {step.output_name}: {display_result}", style="success")
                
                self.callback("success", step, i, len(steps))
                
            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                step.duration = time.time() - step_start
                error = step.error
                
                UI.error(f"  [FAIL] Error: {step.error}")
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
    
    def resume_workflow(self, paused_wf, subagent_result: str) -> WorkflowResult:
        """
        Resume a paused workflow with the sub-agent's result.
        
        Args:
            paused_wf: The saved workflow state
            subagent_result: The result from the sub-agent
            
        Returns:
            WorkflowResult with completion status
        """
        from vaf.cli.ui import UI
        from vaf.core.subagent_ipc import get_ipc, PausedWorkflow
        
        UI.event("Workflow", f"▶️  Resuming workflow [{paused_wf.workflow_id}]...", style="bold cyan")
        
        # Restore steps from saved state
        steps: List[WorkflowStep] = []
        for step_data in paused_wf.steps_data:
            step = WorkflowStep(
                tool=step_data['tool'],
                input_template=step_data['input_template'],
                output_name=step_data['output_name'],
                description=step_data.get('description', ''),
                optional=step_data.get('optional', False),
                condition=step_data.get('condition'),
                args_template=step_data.get('args_template'),
            )
            # Restore status
            step.status = StepStatus(step_data['status'])
            step.result = step_data.get('result')
            step.error = step_data.get('error')
            step.duration = step_data.get('duration', 0.0)
            steps.append(step)
        
        # Restore outputs and add the sub-agent result
        outputs = dict(paused_wf.outputs)
        current_step = steps[paused_wf.current_step_index]
        
        # Update the current step with the result
        current_step.status = StepStatus.SUCCESS
        current_step.result = subagent_result
        outputs[current_step.output_name] = subagent_result
        
        UI.event("Workflow", f"  ✓ Got result for step: {current_step.tool}", style="success")
        
        # Remove from paused workflows
        ipc = get_ipc()
        ipc.remove_paused_workflow(paused_wf.workflow_id)
        
        # Continue with remaining steps
        remaining_steps = steps[paused_wf.current_step_index + 1:]
        
        if not remaining_steps:
            # Workflow is complete
            UI.event("Workflow", "Completed!", style="success")
            return WorkflowResult(
                success=True,
                outputs=outputs,
                final_output=subagent_result,
                steps=steps,
                total_duration=0.0,  # Can't track across pause
                error=None
            )
        
        # Execute remaining steps
        UI.event("Workflow", f"Continuing with {len(remaining_steps)} remaining steps...", style="info")
        
        # Create new steps list with completed + remaining
        remaining_result = self.execute(
            remaining_steps,
            variables=outputs,  # Use all outputs as variables
            stop_on_error=True
        )
        
        # If remaining steps also paused, propagate that
        if remaining_result.paused:
            return remaining_result
        
        # Merge results
        all_steps = steps[:paused_wf.current_step_index + 1] + remaining_result.steps
        all_outputs = {**outputs, **remaining_result.outputs}
        
        return WorkflowResult(
            success=remaining_result.success,
            outputs=all_outputs,
            final_output=remaining_result.final_output,
            steps=all_steps,
            total_duration=remaining_result.total_duration,
            error=remaining_result.error
        )
    
    def _resolve_template(self, template: str, variables: Dict[str, Any], defaults: Dict[str, Any] = None) -> str:
        """
        Replace {variable} placeholders with actual values.
        
        Supports:
        - Simple: {query}
        - Nested: {step1.field}
        - Default: {var|default_value}
        """
        if defaults is None:
            defaults = {}
        
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
            
            # Simple variable - check variables first, then defaults
            if key in variables:
                return str(variables[key])
            elif key in defaults:
                return str(defaults[key])
            else:
                raise KeyError(key)
        
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
            input_template=step_def.get("input", ""),
            args_template=step_def.get("args"),
            output_name=step_def.get("output", f"step_{i+1}"),
            description=step_def.get("description", ""),
            optional=step_def.get("optional", False),
            condition=step_def.get("condition"),
        ))
    return steps

