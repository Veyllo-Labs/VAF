# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
checkpoint_context — Agent-initiated context checkpoint for Plan-Act-Summarize.

The model calls this tool after completing a major step in a multi-step plan.
It archives the current conversation history and starts fresh with only the
system prompt, context glue, and last 2 messages — while working memory
(plan, notes, tasks) survives intact.

This enables unbounded multi-step execution even on small-context models:
the agent persists intermediate results to working memory, checkpoints,
and continues from a clean context.
"""
from vaf.tools.base import BaseTool


class CheckpointContextTool(BaseTool):
    """Reset conversation context after completing a major step."""

    name = "checkpoint_context"
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "Reset the conversation context after completing a major step. "
        "Archives the current history and starts fresh with only the plan "
        "and working memory preserved. Use this when context is getting large "
        "and you have already saved intermediate results to working memory."
    )
    input_examples = [
        {"summary": "Completed steps 1-3: read all PDFs, extracted key data, saved to working memory."},
        {"summary": "Research phase done. 5 sources analyzed, notes saved. Ready for synthesis."},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Brief summary of what was accomplished so far. "
                    "This becomes the narrative summary in the context glue "
                    "so the agent remembers what happened after the reset."
                ),
            },
        },
        "required": ["summary"],
    }

    def run(self, **kwargs) -> str:  # noqa: D401
        agent = kwargs.get("_agent")
        if agent is None:
            return "[ERROR] checkpoint_context requires agent context."
        summary = kwargs.get("summary", "")
        return agent.checkpoint_and_reset(summary=summary)
