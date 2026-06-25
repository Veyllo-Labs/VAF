# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import pytest
import json
import sys
from unittest.mock import MagicMock, patch

# Mock llama_cpp module BEFORE importing Agent
mock_llama_mod = MagicMock()
sys.modules["llama_cpp"] = mock_llama_mod

from vaf.core.agent import Agent

# Mock the Llama class to avoid dependency on a real model file
class MockLlamaTokenizer:
    """A mock tokenizer that simulates llama_cpp.Llama for tokenization."""
    def tokenize(self, text, special=False):
        # A very simple tokenization: count words.
        # This is enough to test if the logic is calling the tokenizer correctly.
        return text.decode('utf-8', 'ignore').split()

    def __call__(self, *args, **kwargs):
        # To handle the instance being called
        return self

class MockLlama:
    """A mock Llama instance."""
    def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):
        pass # Don't do anything in constructor

    def tokenize(self, text, special=False):
        return text.decode('utf-8', 'ignore').split()

class SimpleTool:
    """A simple mock tool object that holds serializable data."""
    def __init__(self, name, description):
        self.name = name
        self.description = description
        self.schema = {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        }

@pytest.fixture
def mock_agent():
    """Fixture to create an Agent instance with mocks."""
    # Mock config and other dependencies
    with patch('vaf.core.agent.Agent.ensure_model_exists'), \
         patch('vaf.core.config.Config.load') as mock_config_load, \
         patch('vaf.core.agent.Agent._load_tools') as mock_load_tools:
        
        # Configure the mock to return a mock config object
        mock_config = MagicMock()
        mock_config_load.return_value = mock_config
        
        # Define what the mock_config.get() method should return
        config_values = {
            "model": "TestModel-7B-v0.1.gguf",
            "n_ctx": 8192,
            "gpu_layers": 0,
            "provider": "local",
            "force_server": False,
            "persist_server": False,
            "language": "auto"
        }
        mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
        
        # Prevent real tools from loading
        mock_load_tools.return_value = None

        # Now, Agent() will use our mocked config and not load real tools
        agent = Agent()
        
        # Mock the tokenizer instance directly on the agent
        agent._tokenizer_instance = MockLlamaTokenizer()

        # Mock tools with our simple, serializable class
        mock_tool_1 = SimpleTool("tool1", "This is the first tool.")
        mock_tool_2 = SimpleTool("tool2", "This is tool number two.")

        agent.tools = {"tool1": mock_tool_1, "tool2": mock_tool_2}

        return agent

def test_get_token_usage_precise(mock_agent):
    """
    Test the precise token usage calculation with a mocked tokenizer.
    """
    # 1. Setup history
    mock_agent.history = [
        {"role": "user", "content": "Hello there, how are you?"},
        {"role": "assistant", "content": "I am fine, thank you!"}
    ]

    # 2. Setup active tools for this interaction
    mock_agent._active_tools = ["tool1"] # Pretend the router selected tool1

    # 3. Calculate token usage
    # We need to access the TOOLS property which applies the _active_tools filter
    print("DEBUG: History before token calculation:", mock_agent.history)
    with patch.object(Agent, 'TOOLS', mock_agent.TOOLS):
         tokens, context_size = mock_agent.get_token_usage()

    # 4. Assert the calculation
    # Let's manually calculate the expected tokens based on our mock tokenizer (word count)
    
    # History tokens:
    # "user": 1, "Hello there, how are you?": 5 -> 6 tokens
    # "assistant": 1, "I am fine, thank you!": 5 -> 6 tokens
    # Overhead per message: 5 * 2 = 10
    expected_history_tokens = (1 + 5) + (1 + 5) + 10 # = 22
    
    # Tool schema tokens:
    # We are using _active_tools = ["tool1"], so only tool1's schema should be counted.
    tool_schema = mock_agent.tools['tool1'].schema
    schema_str = json.dumps([tool_schema]) 
    # Our mock tokenizer splits by space.
    expected_tool_tokens = len(schema_str.split()) # json adds spaces

    # Total
    # Safety buffer: 100
    expected_total = expected_history_tokens + expected_tool_tokens + 100

    print(f"Calculated tokens: {tokens}")
    print(f"Expected history tokens: {expected_history_tokens}")
    print(f"Expected tool tokens: {expected_tool_tokens}")
    print(f"Expected total tokens: {expected_total}")

    # Check if the calculated value is in the right ballpark.
    # It won't be exact due to json formatting, but it should be very close.
    assert abs(tokens - expected_total) < 5 
    assert context_size == 8192
