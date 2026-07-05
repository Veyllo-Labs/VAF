# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coder's provider map must never drift behind the central provider list.

Regression cover for the Veyllo gap: the provider was added centrally (config
PROVIDER_MODELS + api_backend, 2026-06-26) but the coder's private endpoint map
was not updated. With provider=veyllo the coder fell into the LOCAL branch and
either died with "VAF Server unreachable (Port 8080)" or — with a leftover
llama-server running — silently generated with the local model. This test turns
the next such drift into a red CI instead of a silent runtime failure.
"""
from vaf.core.config import PROVIDER_MODELS
from vaf.tools.coder import coder_api_providers


def test_coder_map_covers_every_central_provider():
    coder_map = coder_api_providers()
    central = set(PROVIDER_MODELS.keys())  # "local" is intentionally absent centrally
    missing = central - set(coder_map.keys())
    assert not missing, (
        f"coder_api_providers() is missing providers that exist centrally: {sorted(missing)}. "
        "Add the OpenAI-compatible endpoint for them — otherwise the coder silently "
        "falls back to the local :8080 branch on those providers."
    )


def test_coder_map_entries_are_wellformed():
    for name, (base_url, default_model) in coder_api_providers().items():
        assert base_url.startswith("https://"), f"{name}: base_url must be https ({base_url})"
        assert not base_url.endswith("/"), f"{name}: base_url must not end with '/' ({base_url})"
        assert default_model, f"{name}: default model must be non-empty"


def test_veyllo_entry_present_and_config_driven():
    m = coder_api_providers()
    assert "veyllo" in m
    base, model = m["veyllo"]
    assert "/v1" in base
    assert model == "veyllo-chat"
