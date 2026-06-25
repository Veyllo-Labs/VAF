# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Basic integrity tests for VAF.
These tests run in CI/CD without needing a GPU or LLM.
"""
import pytest
import os
from vaf.core.system_prompt import SystemPromptManager
from vaf.tools.coder import CodingAgentTool

def test_system_prompt_manager():
    """Ensure the prompt manager initializes and builds a non-empty prompt."""
    manager = SystemPromptManager(tools=[], model_name="TestModel")
    prompt = manager.build_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 50

def test_coder_tool_initialization():
    """Ensure Coder tool can be instantiated (checks imports)."""
    coder = CodingAgentTool()
    assert coder.name == "coding_agent"
    
def test_import_all_modules():
    """Smoke test: Ensure all key modules can be imported without error."""
    import vaf.main
    import vaf.core.agent
    import vaf.core.config
    import vaf.cli.tui
    assert True
