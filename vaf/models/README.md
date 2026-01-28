# VAF Models Cache

This directory is used by VAF to store and cache large machine learning models, ensuring they don't need to be re-downloaded across different sessions or project instances.

## Structure

- **.cache/**: Contains internal cache files for the model management system (e.g., Hugging Face cache).

## Usage

Models stored here are managed by:
- `vaf/core/backend.py` for local LLMs (GGUF format).
- Specialized sub-agents that might require dedicated models (e.g., speech or vision models).

## Management

To clean up disk space, you can safely delete the contents of the `.cache` directory, but note that VAF will re-download required models during their next use.
