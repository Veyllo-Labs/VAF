# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The paired device is the Veyllo App, never "the phone".

What the person reads, and what the model reads and repeats to them (tool descriptions and
results, the prompt line, the fences), calls the paired device the Veyllo App, or a device
where the hardware is meant. A phone number, WhatsApp, the 2FA phone and the screen size keep
their meaning; so do the identifiers (`user_phone`, `phone_result`, `phone_tasks`), which name
stored or wired things. Comments and docstrings are not scanned: they are for the reader of the
code, and nothing shows them to a person.

MUTATION: put "phone" back into `phone_tasks.NOT_REACHABLE` - red.
"""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PHONE = re.compile(r"\bphones?\b|phone's|Handy|Telefon", re.IGNORECASE)
_IDENTIFIERS = ("user_phone", "phone_result", "phone_tasks", "to_phone", "phone_number",
                "phoneFallback")
_CODE = sorted((ROOT / "vaf" / "core" / "app_link").glob("*.py")) + [
    ROOT / "vaf" / "tools" / "user_phone.py", ROOT / "vaf" / "tools" / "send_veyllo_app.py",
    ROOT / "vaf" / "core" / "agent_origin.py", ROOT / "vaf" / "api" / "app_link_routes.py"]
LANGS = ("de", "en", "ja", "ko", "th", "tr", "zh")


def _hits(text: str) -> bool:
    for name in _IDENTIFIERS:
        text = text.replace(name, "")
    return bool(_PHONE.search(text))


def _strings(path: Path):
    """Every string constant of a module except its docstrings, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant):
            docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node.lineno, node.value


def _values(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _values(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _values(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def test_the_scan_sees_the_tool_texts():
    """A scan that finds nothing to read proves nothing."""
    seen = {p.name: [s for _l, s in _strings(p)] for p in _CODE}
    assert any("Veyllo App" in s for s in seen["user_phone.py"])
    assert any("Veyllo App" in s for s in seen["phone_tasks.py"])
    assert _hits("The phone is not reachable") and not _hits("call user_phone again")


def test_no_tool_text_or_answer_calls_the_device_a_phone():
    found = [f"{p.relative_to(ROOT).as_posix()}:{line}: {text[:80]!r}"
             for p in _CODE for line, text in _strings(p) if _hits(text)]
    assert not found, "say Veyllo App (or device):\n" + "\n".join(found)


def test_no_app_sentence_of_the_vocabulary_calls_it_a_phone():
    files = sorted((ROOT / "vaf" / "core" / "vocab" / "data").glob("app_*.json"))
    assert len(files) >= 15
    found = [f"{p.name}{where}" for p in files
             for where, text in _values(json.loads(p.read_text(encoding="utf-8"))) if _hits(text)]
    assert not found, found


def test_no_text_of_the_veyllo_app_window_calls_it_a_phone():
    found = []
    for lang in LANGS:
        catalogue = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(encoding="utf-8"))
        found += [f"{lang}{where}" for where, text in _values(catalogue["settings"]["veylloApp"])
                  if _hits(text)]
    assert not found, found


def test_the_design_doc_calls_it_the_veyllo_app():
    doc = (ROOT / "docs" / "integrations" / "VEYLLO_APP.md").read_text(encoding="utf-8")
    found = [f"{n}: {line}" for n, line in enumerate(doc.splitlines(), 1) if _hits(line)]
    assert not found, found


def test_the_busy_reason_is_worded_in_every_language():
    """MUTATION: delete `failureReasons.busy` from one catalogue - red."""
    for lang in LANGS:
        catalogue = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(encoding="utf-8"))
        text = catalogue["settings"]["veylloApp"]["failureReasons"].get("busy", "")
        assert "{seconds}" in text and "\u2014" not in text, lang
    wizard = (ROOT / "web" / "components" / "connections" / "VeylloAppSetupWizard.tsx").read_text(
        encoding="utf-8")
    assert "t('failureReasons.busy', { seconds:" in wizard
    from vaf.core.app_link import relay
    assert "busy" in relay.FAILURE_REASONS
