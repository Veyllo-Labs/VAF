# Built-in Workflows

This directory contains the individual Python files that define the built-in workflows for VAF.

## Contents
Each `.py` file here defines a `WORKFLOW` dictionary that includes:
- **name**: Human-readable name.
- **triggers**: Keywords and phrases that trigger the workflow.
- **steps**: The sequence of tool calls to execute.

## Key Workflows
- **create_website.py**: Scaffolds a full web project.
- **analyze_website.py**: Analyzes a website's content and structure.
- **deep_research.py**: Performs multi-query topic investigation.
- **research_and_code.py**: Research + code generation pipeline.
- **youtube_summary.py**: Summarizes a YouTube video from its captions (yt-dlp in the sandbox, honest no-subs/rate-limit handling, validated summary step).
- **research_and_document.py**: Research + document creation pipeline.
- **code_review.py**: Analyzes and suggests improvements for code.
- **generate_docs.py**: Creates project documentation.
- **create_document.py**: Creates a document from user intent.
- **create_file.py**: Generates and creates a new file.
- **create_scheduled_task.py**: Creates a scheduled automation workflow.
- **legal_contract_research.py**: Legal research workflow.
- **technical_doc_research.py**: Technical research workflow.

## Usage
These workflows are automatically loaded by the `vaf.workflows.templates` module. To add a new one, simply place a valid workflow `.py` file in this directory.

For detailed instructions on how to write a workflow, refer to the parent `README.md`.
