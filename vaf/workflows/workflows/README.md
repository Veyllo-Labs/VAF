# Built-in Workflows

This directory contains the individual Python files that define the built-in workflows for VAF.

## Contents
Each `.py` file here defines a `WORKFLOW` dictionary that includes:
- **name**: Human-readable name.
- **triggers**: Keywords and phrases that trigger the workflow.
- **steps**: The sequence of tool calls to execute.

## Key Workflows
- **create_website.py**: Scaffolds a full web project.
- **deep_research.py**: Performs multi-query topic investigation.
- **code_review.py**: Analyzes and suggests improvements for code.
- **generate_docs.py**: Creates project documentation.

## Usage
These workflows are automatically loaded by the `vaf.workflows.templates` module. To add a new one, simply place a valid workflow `.py` file in this directory.

For detailed instructions on how to write a workflow, refer to the parent `README.md`.
