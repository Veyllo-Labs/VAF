"""
Workflow Executor Tool

Allows the Main Agent to manually execute a workflow when:
- No workflow automatically matched the request
- Agent determines a specific workflow would be best
- User explicitly requests a workflow
"""

from vaf.tools.base import BaseTool


class ExecuteWorkflowTool(BaseTool):
    name = "execute_workflow"
    permission_level = "write"
    side_effect_class = "reversible"
    description = """Execute a specific workflow by ID.
    
    Use this tool when:
    - You've been informed that workflows are available but none matched automatically
    - You determine that a specific workflow would handle the request best
    - User's request clearly fits a workflow but wasn't auto-detected
    
    Available workflows are usually shown in the conversation context when relevant.
    
    Example usage:
    - execute_workflow(workflow_id="legal_contract_research", variables={"contract_type": "employment"})
    - execute_workflow(workflow_id="research_and_document", variables={"topic": "Docker deployment"})
    """
    
    parameters = {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "ID of the workflow to execute (e.g., 'legal_contract_research', 'research_and_document')"
            },
            "variables": {
                "type": "object",
                "description": "Variables required by the workflow (e.g., {'topic': 'Docker', 'document_type': 'guide'})",
                "additionalProperties": True
            }
        },
        "required": ["workflow_id"]
    }
    
    def run(self, workflow_id: str, variables: dict = None) -> str:
        """
        Execute a workflow with given variables.
        
        Args:
            workflow_id: The workflow ID to execute
            variables: Dictionary of variables for the workflow
            
        Returns:
            Result message or error
        """
        try:
            from vaf.workflows.templates import get_template, list_templates
            from vaf.workflows.engine import WorkflowEngine, create_workflow
            from vaf.workflows.selector import WorkflowSelector
            
            # Get the workflow template
            template = get_template(workflow_id)
            if not template:
                # Show available workflows
                available = list_templates()
                workflow_list = "\n".join([f"- {w['id']}: {w['description']}" for w in available])
                return (
                    f"❌ Workflow '{workflow_id}' not found.\n\n"
                    f"Available workflows:\n{workflow_list}"
                )
            
            # Validate and fill in variables
            variables = variables or {}
            template_vars = template.get("variables", {})
            defaults = template.get("defaults", {})
            missing = []
            
            # Check for missing required variables
            for var_name in template_vars.keys():
                if var_name not in variables:
                    if var_name in defaults:
                        variables[var_name] = defaults[var_name]
                    else:
                        missing.append(var_name)
            
            if missing:
                var_descriptions = "\n".join([
                    f"  - {var}: {template_vars[var]}"
                    for var in missing
                ])
                return (
                    f"❌ Workflow '{workflow_id}' requires these variables:\n"
                    f"{var_descriptions}\n\n"
                    f"Please provide them using the 'variables' parameter."
                )
            
            # Build workflow steps
            steps = create_workflow(template)
            
            # Get tools for execution
            # Import necessary tools with proper error handling
            tools = {}
            
            try:
                from vaf.tools.search import WebSearchTool
                tools["web_search"] = WebSearchTool()
            except ImportError:
                pass
            
            try:
                from vaf.tools.filesystem import WriteFileTool, ReadFileTool, ListFilesTool
                tools["write_file"] = WriteFileTool()
                tools["read_file"] = ReadFileTool()
                tools["list_files"] = ListFilesTool()
            except ImportError:
                pass
            
            try:
                from vaf.tools.librarian import LibrarianAgentTool
                tools["librarian_agent"] = LibrarianAgentTool()
            except ImportError:
                pass
            
            try:
                from vaf.tools.research import ResearchAgentTool
                tools["research_agent"] = ResearchAgentTool()
            except ImportError:
                pass
            
            try:
                from vaf.tools.coder import CodingAgentTool
                tools["coding_agent"] = CodingAgentTool()
            except ImportError:
                pass
            
            try:
                from vaf.tools.document_writer import DocumentWriterTool
                tools["document_writer"] = DocumentWriterTool()
            except ImportError:
                pass
            
            if not tools:
                return "❌ No tools available for workflow execution. Please ensure VAF is properly installed."
            
            # Create and execute workflow
            engine = WorkflowEngine(tools)
            result = engine.execute(steps, variables=variables)
            
            if result.success:
                return f"✅ Workflow '{template['name']}' completed successfully!\n\n{result.final_output}"
            else:
                return f"❌ Workflow '{template['name']}' failed: {result.error}"
                
        except Exception as e:
            return f"❌ Error executing workflow: {str(e)}"
