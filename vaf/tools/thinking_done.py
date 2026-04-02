"""
Thinking-done tool: only available when VAF_THINKING_MODE=1.
The agent calls this to signal that the current thinking pass is finished.
"""
from vaf.tools.base import BaseTool


class ThinkingDoneTool(BaseTool):
    """Signal that this thinking pass is complete. Call when you have nothing left to do for the user in this run."""

    name = "thinking_done"
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "Call this when you have finished this thinking pass. "
        "You may use multiple turns to work on todos, automations, and messages; when you are done, call thinking_done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Optional short summary of what you did in this pass.",
            }
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        summary = kwargs.get("summary", "").strip()
        return summary or "Done."
