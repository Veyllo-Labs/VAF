# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Three small bounds an audit found open, each on its own.

- A non-admin's config save removes provider keys from the payload and stores none: the
  save paths filter the body first, and this is the second lock the docstring promised
  but the loop did not check.
- The sandbox supervises itself, so nothing stood behind the model's timeout: 99999
  seconds meant 99999 seconds.
- The research agent's declared budget covers its own configured runtime, instead of the
  generic sub-agent 300 s that cut an in-process run long before its 900-second limit.
"""
from unittest.mock import patch


def test_a_non_admin_payload_never_stores_a_provider_key():
    from vaf.core import api_keys

    stored = []
    with patch.object(api_keys, "store_api_key", lambda name, key, **kw: stored.append(name)), \
         patch("vaf.core.channel_secrets.absorb_channel_secrets", lambda cfg, is_admin: cfg):
        cleaned = api_keys.absorb_config_keys({"api_key_openai": "sk-x", "theme": "dark"},
                                              is_admin=False)
    assert stored == [], "a non-admin save wrote a provider key"
    assert "api_key_openai" not in cleaned and cleaned["theme"] == "dark"


def test_an_admin_payload_still_stores_it():
    from vaf.core import api_keys

    stored = []
    with patch.object(api_keys, "store_api_key", lambda name, key, **kw: stored.append(name)), \
         patch("vaf.core.channel_secrets.absorb_channel_secrets", lambda cfg, is_admin: cfg):
        api_keys.absorb_config_keys({"api_key_openai": "sk-x"}, is_admin=True)
    assert stored == ["openai"]


def test_the_sandbox_timeout_is_bounded_and_defaults_on_nonsense():
    from vaf.tools.python_sandbox import PythonSandboxTool

    t = PythonSandboxTool._run_timeout
    assert t({"timeout": 99999}) == PythonSandboxTool.MAX_TIMEOUT_SECONDS
    assert t({"timeout": 0}) == 30          # "or 30": zero reads as unset
    assert t({"timeout": -5}) == 1
    assert t({"timeout": "abc"}) == 30 and t({}) == 30
    assert t({"timeout": 45}) == 45


def test_the_research_budget_covers_its_own_runtime():
    from vaf.tools.research_agent import ResearchAgentTool

    values = {"research_overall_timeout_seconds": 900, "research_web_search_timeout_seconds": 60,
              "research_section_llm_timeout_seconds": 240, "subagent_timeout_seconds": 300}
    with patch("vaf.core.config.Config.get", side_effect=lambda k, d=None: values.get(k, d)):
        budget = ResearchAgentTool().budget_seconds({})
    # The loop checks the overall limit between sections; the section running then can
    # still make three searches and three generations in sequence.
    assert budget >= 900 + 3 * 60 + 3 * 240, budget
