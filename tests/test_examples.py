# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""CI guard for examples/: they must stay syntactically valid and honest.

Examples are the first code a new developer copies; a bit-rotted example is
worse than none. We do not RUN them (they need a configured provider), but
every script must compile, and the example tool package must be a loadable
BaseTool whose entry-point declaration actually points at the class.
"""
import importlib.util
import py_compile
import re
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_every_example_script_compiles(tmp_path):
    scripts = sorted(EXAMPLES.rglob("*.py"))
    assert len(scripts) >= 4, "examples went missing"
    for script in scripts:
        py_compile.compile(str(script), cfile=str(tmp_path / "c.pyc"), doraise=True)


def test_example_tool_is_a_valid_basetool():
    from vaf.tools.base import BaseTool

    tools_py = EXAMPLES / "vaf_example_tool" / "vaf_example_tool" / "tools.py"
    spec = importlib.util.spec_from_file_location("vaf_example_tool_tools", tools_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cls = module.DiceRollTool
    assert issubclass(cls, BaseTool)
    tool = cls()
    assert tool.name == "dice_roll"
    assert not tool.coder_only, "entry-point tools target the main agent"
    result = tool.run(sides=6, count=2)
    assert isinstance(result, str) and "2d6" in result


def test_example_tool_entry_point_declaration_matches():
    pyproject = (EXAMPLES / "vaf_example_tool" / "pyproject.toml").read_text()
    match = re.search(
        r'^dice_roll\s*=\s*"vaf_example_tool\.tools:DiceRollTool"',
        pyproject,
        re.MULTILINE,
    )
    assert match, "the vaf.tools entry point must point at vaf_example_tool.tools:DiceRollTool"
    assert '[project.entry-points."vaf.tools"]' in pyproject
