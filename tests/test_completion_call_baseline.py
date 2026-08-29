# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Direct chat_completion call sites may only SHRINK - one-shots route through complete().

THE MEASUREMENT BEHIND THE RATCHET: ~22 places hand-rolled the same single completion,
and the hand-rolls disagreed about correctness (metadata frames in commit messages,
error sentinels as content, reasoning burned into empty local answers). Six were
deleted onto vaf/core/completion.py; the sites below remain DELIBERATELY, each for a
named structural reason - and nothing new may join them.

AST and not substring, deliberately: browser_agent.py carries two doc-comments naming
`APIBackendManager.chat_completion()` - a text scan would count them as calls.

THE NAMED FOLLOW-UP, recorded where the next reader looks: vaf/core/agent.py is
excluded below and carries 13 chat_completion calls, of which 11 are one-shot
hand-rolls (five factored helpers: _run_validation_llm, _generate_summary,
_generate_for_compaction, _generate_for_document_extraction,
_generate_for_classification; six inline: analyze_intent, analyze_workflow,
_route_tools, the async-ack translation, _detect_false_tool_promise, the vision
fallback in _prepare_messages). The follow-up rebases those onto self.complete() /
the completion helpers, one round with its own baselines - the OTHER two calls are
chat_step's conversation lane (streaming + its non-stream fallback) and NEVER convert.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Excluded: the conversation engine, the manager itself, and the primitive.
EXCLUDED = {
    "vaf/core/agent.py",
    "vaf/core/api_backend.py",
    "vaf/core/completion.py",
}

# GENERATED, never typed (regenerate with the collector below after a deliberate
# change). May only SHRINK. Every entry names why it is not a complete() consumer.
DIRECT_CALL_SITES = {
    "vaf/api/mail_routes.py": 1,      # SSE streaming composer; AST-guarded in test_mail_composer_guards.py
    "vaf/core/vision_infer.py": 1,    # vision block arrays + its own sentinel/strip collector
    "vaf/core/voice_agent.py": 1,     # chunk-spanning <think> state machine, latency lane
    "vaf/memory/rag.py": 1,           # _stream_answer: genuinely streaming (SSE), shares the frame predicate
    "vaf/tools/browser_agent.py": 1,  # browser-use multi-turn bridge with mid-stream stop (the lane's
                                      # vision call is gone: it routes through vision_infer now)
    "vaf/tools/librarian.py": 1,      # tool-calling agent loop
}


def _live_counts() -> dict:
    counts = {}
    for f in sorted((ROOT / "vaf").rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if rel in EXCLUDED:
            continue
        tree = ast.parse(f.read_bytes())
        n = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("chat_completion", "chat_completion_stream")
        )
        if n:
            counts[rel] = n
    return counts


def test_direct_completion_calls_only_shrink():
    live = _live_counts()

    grown = {
        rel: (DIRECT_CALL_SITES.get(rel, 0), n)
        for rel, n in live.items()
        if n > DIRECT_CALL_SITES.get(rel, 0)
    }
    assert not grown, (
        f"a new direct chat_completion call site appeared: "
        f"{ {k: f'{a} -> {b}' for k, (a, b) in grown.items()} }. One-shot completions "
        f"route through vaf.core.completion.complete (catalog row in "
        f"docs/llm/PROVIDER_MODES.md). If this lane is genuinely streaming or "
        f"multi-turn, add it to DIRECT_CALL_SITES with a comment naming why."
    )

    shrunk = {
        rel: (n, live.get(rel, 0))
        for rel, n in DIRECT_CALL_SITES.items()
        if live.get(rel, 0) < n
    }
    assert not shrunk, (
        f"debt was paid down - lock it in so it cannot creep back: {shrunk}; update "
        f"DIRECT_CALL_SITES in {Path(__file__).name} to the new (smaller) counts."
    )
