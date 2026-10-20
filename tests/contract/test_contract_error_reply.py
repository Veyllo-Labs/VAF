# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.is_error_reply` (docs/EMBEDDING.md, "A complete example, with error
handling" and "What is and isn't stable").

`run()` and `chat_step` do not raise for a provider that fails during the turn: the failure
comes back as the answer's text. This is the check an embedder runs before that text reaches
somebody who must not read internals.
"""
import vaf


def test_the_engine_status_and_the_provider_sentinel_are_failures():
    assert vaf.is_error_reply("[Error] API backend failure: connection refused")
    assert vaf.is_error_reply("[API Error from openai: Error code: 401 - Incorrect API key]")


def test_a_failure_after_streamed_text_is_still_a_failure():
    """An earlier round of the same turn streamed text, then the provider failed."""
    assert vaf.is_error_reply("Einen Moment.\n[API Error from deepseek: Error code: 402]")
    assert vaf.is_error_reply("Let me look.\n[Error] Server unavailable after retries.")


def test_an_answer_is_not_a_failure():
    assert not vaf.is_error_reply("Python is a programming language.")
    assert not vaf.is_error_reply("The error was fixed yesterday.")


def test_nothing_but_a_non_empty_string_is_a_failure_and_it_never_raises():
    for value in ("", None, 0, b"[Error] bytes", ["[Error] x"]):
        assert vaf.is_error_reply(value) is False


def test_it_is_the_engine_function_itself():
    import vaf.core.completion as completion  # re-export source named in EMBEDDING.md

    assert vaf.is_error_reply is completion.is_error_reply
