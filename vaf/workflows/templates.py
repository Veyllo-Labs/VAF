# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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

import ast
import importlib.util
import logging
import re
from pathlib import Path
from typing import Dict, Any, List

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# WRITING A USER WORKFLOW FILE
# ═══════════════════════════════════════════════════════════════════════════════

def workflow_text_problem(label: str, value: Any) -> str:
    """Why a workflow's name or description cannot be saved, or "".

    A workflow file is Python that the loader executes, and these texts reach its comment
    header; a line break or another control character there would end the comment. Refused
    rather than cleaned, so the saved name is the name that was asked for."""
    text = str(value or "")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return f"{label} must be a single line without control characters."
    return ""


# A header line that Python would read as a source-encoding declaration (PEP 263). None of the
# header lines carry model or user text any more, so this can only fire on a future caller, but
# a cookie in a comment would otherwise make the loader read the whole file in that encoding.
_PEP263_RE = re.compile(r"^[ \t\f]*#.*?coding[:=]")


def write_workflow_file(path: str, workflow: Dict[str, Any], header_lines: List[str]) -> None:
    """Write one user workflow file: FIXED comment header lines, then ``WORKFLOW = <literal>``.

    The one writer for ~/.vaf/workflows/*.py (the agent's create action and the web UI's
    create and update). The dict is written as a Python literal (``pprint.pformat``), never
    as JSON: JSON's true/false/null are not Python, so a step with ``optional: true`` made
    the file unloadable. The file is DATA, not code: the user loader reads only the literal
    (`_load_workflow_literal`), so the dict's own strings cannot run. The header lines carry
    no model or user text (the name lives inside the literal), are refused if they are not a
    single line or look like an encoding declaration, and the finished literal is parsed back
    with ``ast.literal_eval`` before the file is put in place, so a value that is not a plain
    literal (a non-finite float writes as ``inf``/``nan``, which are names) never lands as an
    unloadable file. Atomic: written to a temporary file, then replaced."""
    import os
    import pprint
    for line in header_lines:
        problem = workflow_text_problem("A header line", line)
        if problem:
            raise ValueError(problem)
        if _PEP263_RE.match("# " + line):
            raise ValueError("A header line must not look like a source-encoding declaration.")
    literal = pprint.pformat(workflow, indent=4, width=100, sort_dicts=False)
    try:
        if ast.literal_eval(literal) != workflow:
            raise ValueError("mismatch")
    except (ValueError, SyntaxError):
        raise ValueError("workflow contains a value that is not a plain literal "
                         "(for example a non-finite number); nothing was saved.")
    content = "".join(f"# {line}\n" for line in header_lines) + f"WORKFLOW = {literal}\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


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


def _require_workflow_fields(workflow_id: str, workflow: Any) -> None:
    if not isinstance(workflow, dict):
        raise ValueError(f"Workflow {workflow_id}: WORKFLOW is not a dictionary")
    for field in ("name", "description", "triggers", "steps"):
        if field not in workflow:
            raise ValueError(f"Workflow {workflow_id} missing required field: {field}")


def _load_workflow_literal(file_path: Path) -> tuple[str, Dict[str, Any]]:
    """Load a USER workflow file as DATA: the value of its single ``WORKFLOW = <literal>``
    assignment, read with ``ast.literal_eval`` and never executed.

    A file in ~/.vaf/workflows is written by `write_workflow_file` (the agent's create, the
    web UI) and is a plain literal; the loader does not run it, so a file dropped there by any
    other means - a write_file into the folder, a PEP 263 encoding line in a string - cannot
    run code. The bytes are decoded as UTF-8 here (a leading BOM is tolerated), so a
    source-encoding declaration in the file is ignored. The built-in templates shipped in the package keep their Python loader
    (`_load_workflow_from_file`); they are code VAF ships, not user data.

    Raises ValueError when the file is not exactly a module docstring (optional), comments and
    one ``WORKFLOW`` literal assignment."""
    workflow_id = file_path.stem
    source = file_path.read_bytes().decode("utf-8-sig")  # a BOM from a hand editor is fine
    module = ast.parse(source)
    assign = None
    for node in module.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # a module docstring or a bare string: harmless
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "WORKFLOW"):
            if assign is not None:
                raise ValueError(f"Workflow {workflow_id}: more than one WORKFLOW assignment")
            assign = node
            continue
        raise ValueError(f"Workflow {workflow_id}: a user workflow file must be a single "
                         f"WORKFLOW literal, found {type(node).__name__}")
    if assign is None:
        raise ValueError(f"Workflow {workflow_id}: no WORKFLOW assignment")
    workflow = ast.literal_eval(assign.value)
    _require_workflow_fields(workflow_id, workflow)
    return workflow_id, workflow


def _discover_workflows(directory: Path, *, literal_only: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Discover and load all workflows from a directory.

    Args:
        directory: Path to directory containing workflow .py files
        literal_only: load each file as a WORKFLOW literal (user directory), never executing
            it; a file that is not a plain literal is skipped with a log line naming it.

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
            if literal_only:
                workflow_id, workflow = _load_workflow_literal(file_path)
            else:
                workflow_id, workflow = _load_workflow_from_file(file_path)
            workflows[workflow_id] = workflow
        except Exception as e:
            # Skip this file, load the rest. A user file that is not a plain WORKFLOW literal
            # is named here with the reason: the loader does not execute it, so a Python file
            # dropped into ~/.vaf/workflows by any other means never runs.
            log.warning("Skipping workflow file %s: %s", file_path, e)
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
        user_workflows = _discover_workflows(user_workflows_dir, literal_only=True)
        # User workflows can override built-in ones
        workflows.update(user_workflows)
    
    return workflows


def _workflows_signature() -> tuple:
    """Cheap fingerprint of the workflow dirs (count + newest .py mtime over the
    built-in and ~/.vaf/workflows directories). Changes when a workflow file is
    added, removed, or edited. Lets a newly-created or cross-process-written
    workflow become live without a restart. Just a few stat() calls; no watcher."""
    count = 0
    latest = 0
    for directory in (Path(__file__).parent / "workflows", Path.home() / ".vaf" / "workflows"):
        try:
            if not directory.exists():
                continue
            for f in directory.glob("*.py"):
                if f.name == "__init__.py":
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                count += 1
                if st.st_mtime_ns > latest:
                    latest = st.st_mtime_ns
        except Exception:
            pass
    return (count, latest)


# Load all workflows automatically; auto-refreshed on read when the dirs change
# (see ensure_fresh_workflows) and force-refreshed via reload_workflows().
WORKFLOW_TEMPLATES: Dict[str, Dict[str, Any]] = _load_all_workflows()
_WF_SIG: tuple = _workflows_signature()


def ensure_fresh_workflows() -> None:
    """Rescan the workflow dirs and rebind WORKFLOW_TEMPLATES only when files
    changed. Call before reading the registry so newly-added/edited workflows are
    picked up live, regardless of which process wrote them."""
    global WORKFLOW_TEMPLATES, _WF_SIG
    try:
        sig = _workflows_signature()
    except Exception:
        return
    if sig != _WF_SIG:
        WORKFLOW_TEMPLATES = _load_all_workflows()
        _WF_SIG = sig


def get_workflow_templates() -> Dict[str, Dict[str, Any]]:
    """Freshness-checked accessor for the workflow registry.

    Prefer this over importing the WORKFLOW_TEMPLATES global directly: a plain
    `from ... import WORKFLOW_TEMPLATES` binds a stale snapshot, whereas this
    rebinds the global on disk changes first and returns the current dict.
    """
    ensure_fresh_workflows()
    return WORKFLOW_TEMPLATES


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_template(name: str) -> Dict[str, Any]:
    """Get a workflow template by name."""
    ensure_fresh_workflows()
    return WORKFLOW_TEMPLATES.get(name)


def list_templates() -> List[Dict[str, str]]:
    """List all available templates with name and description."""
    ensure_fresh_workflows()
    user_dir = Path.home() / ".vaf" / "workflows"
    return [
        {
            "id": key,
            "name": template["name"],
            "description": template["description"],
            "steps": len(template["steps"]),
            # True when a .py file exists in the user workflows directory
            "is_custom": (user_dir / f"{key}.py").exists(),
        }
        for key, template in WORKFLOW_TEMPLATES.items()
    ]


def get_template_names() -> List[str]:
    """Get list of template names."""
    ensure_fresh_workflows()
    return list(WORKFLOW_TEMPLATES.keys())


def reload_workflows() -> None:
    """Reload all workflows (useful after adding new user workflows)."""
    global WORKFLOW_TEMPLATES, _WF_SIG
    WORKFLOW_TEMPLATES = _load_all_workflows()
    _WF_SIG = _workflows_signature()
