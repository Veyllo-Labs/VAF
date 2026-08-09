# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The document-learning config keys have a real home (Rule 2).

learn_document_max_pages and learn_max_sections existed only as inline
Config.get fallbacks - a key without a DEFAULTS entry and a schema row is not a
config key, and the silent 200/40 literals meant 3.9% of a 1000-page book was
learned and reported as success. These tests pin the homes, the owner's
learn-everything default, and the admin-only classification of every key that
multiplies LLM spend.
"""
import re
from pathlib import Path

from vaf.core.config import Config

_REPO = Path(__file__).resolve().parents[1]
_SCHEMA = _REPO / "docs" / "setup" / "CONFIG_SCHEMA.md"

_LEARN_KEYS = (
    "learn_document_max_pages",
    "learn_max_sections",
    "learn_batch_pages",
    "memory_document_extraction_max_tokens",
)


def test_defaults_are_learn_everything():
    """Deliberate: the default is the WHOLE document; a cap is opt-in."""
    assert Config.DEFAULTS["learn_document_max_pages"] == 0
    assert Config.DEFAULTS["learn_max_sections"] == 0
    assert Config.DEFAULTS["learn_batch_pages"] == 10
    assert Config.DEFAULTS["memory_document_extraction_max_tokens"] == 1200
    assert Config.DEFAULTS["librarian_ocr_fallback_for_pdf"] is True


def test_every_learn_default_has_a_schema_row():
    doc = _SCHEMA.read_text(encoding="utf-8")
    for key in _LEARN_KEYS + ("librarian_ocr_fallback_for_pdf",):
        assert f"`{key}`" in doc, f"CONFIG_SCHEMA.md row missing for {key}"


def test_spend_keys_are_admin_only():
    """Pages/sections/batch/tokens decide how many LLM calls one learn run makes
    and how large each is - per-user writable would let a LAN user multiply
    instance spend (precedent: mail_composer_max_*)."""
    for key in _LEARN_KEYS:
        assert Config.is_global_config_key(key), f"{key} is writable by non-admins"
    # librarian_ is already an admin-only PREFIX; pin that the new key rides it.
    assert Config.is_global_config_key("librarian_ocr_fallback_for_pdf")


def test_the_extraction_docstring_stopped_lying():
    """agent.py claimed default 800 while the code used 1200."""
    from vaf.core.agent import Agent

    doc = Agent._generate_for_document_extraction.__doc__ or ""
    assert "800" not in doc
    assert "1200" in doc


def test_the_hard_section_clamp_is_gone():
    """max(2, min(80, ...)) capped even an explicit config raise to 80 and
    turned the documented 0 into 40 (0 is falsy -> `or 40`). Only the opt-in
    cap semantics may remain."""
    import inspect

    import vaf.tools.learn_document as ld

    src = inspect.getsource(ld.ingest_document_knowledge)
    assert "min(80" not in src, "the hard 80 clamp is back"
    assert 'Config.get("learn_max_sections", 0)' in src, \
        "call-site literal drifted from the DEFAULTS value (bounded_run lesson)"
