"""
List Workflows Tool

Allows the agent to view available workflows on-demand.
This prevents workflow lists from cluttering the conversation context.
"""

from vaf.tools.base import BaseTool


class ListWorkflowsTool(BaseTool):
    name = "list_workflows"
    permission_level = "read"
    side_effect_class = "none"
    description = """List all available multi-step workflows.
    
    Use this tool when:
    - You need to see what workflows are available
    - User's request might benefit from a multi-step workflow
    - You want to suggest workflow options to the user
    
    This tool returns a list of workflows with their descriptions.
    You can then use 'execute_workflow' to run a specific workflow.
    
    Example usage:
    - User asks for help with ambiguous task → Check workflows first
    - User wants complex document/analysis → See if workflow exists
    """
    
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    
    def run(self) -> str:
        """
        List all available workflows.
        
        Returns:
            Formatted list of workflows with descriptions
        """
        try:
            from vaf.workflows.templates import list_templates
            
            workflows = list_templates()
            
            if not workflows:
                return "ℹ️ No workflows are currently available."
            
            result = f"📋 **Available Workflows** ({len(workflows)} total):\n\n"
            
            # Group by category (rough heuristic)
            research_wf = []
            document_wf = []
            code_wf = []
            other_wf = []
            
            for wf in workflows:
                wf_id = wf['id']
                desc = wf['description']
                
                if 'research' in wf_id.lower():
                    research_wf.append(f"  • **{wf_id}**: {desc}")
                elif 'document' in wf_id.lower() or 'contract' in wf_id.lower():
                    document_wf.append(f"  • **{wf_id}**: {desc}")
                elif 'code' in wf_id.lower() or 'review' in wf_id.lower():
                    code_wf.append(f"  • **{wf_id}**: {desc}")
                else:
                    other_wf.append(f"  • **{wf_id}**: {desc}")
            
            # Build categorized output
            if research_wf:
                result += "**Research Workflows:**\n" + "\n".join(research_wf) + "\n\n"
            
            if document_wf:
                result += "**Document Workflows:**\n" + "\n".join(document_wf) + "\n\n"
            
            if code_wf:
                result += "**Code Workflows:**\n" + "\n".join(code_wf) + "\n\n"
            
            if other_wf:
                result += "**Other Workflows:**\n" + "\n".join(other_wf) + "\n\n"
            
            result += (
                "💡 **Tip:** Use `execute_workflow(workflow_id='...', variables={...})` "
                "to run a specific workflow."
            )
            
            return result
            
        except Exception as e:
            return f"❌ Error listing workflows: {str(e)}"
