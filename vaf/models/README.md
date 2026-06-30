# vaf/models

This directory is a placeholder package marker, not the runtime model store. VAF does not download, cache, or load any machine learning models here, and no code references this path.

## Where models actually live

Local models are stored in the repo-root `models/` directory (resolved as `base_dir/models`, where `base_dir` is the repository root). Typical contents:

- The local LLM in GGUF format (for example a `*.gguf` file).
- A `voices/` subfolder holding the Piper TTS voice models (`*.onnx` and matching `*.onnx.json`).
- A `.cache/` folder created at runtime (for example Hugging Face downloads).

## How models are managed

Local-model resolution and provisioning live in `vaf/core/backend.py`:

- `ensure_model_available(model_name, models_dir)` ensures the requested GGUF model is present, downloading it into `models_dir` when needed.
- `get_model_path(...)` returns the on-disk path for a model under the repo-root `models/` directory.

The `models_dir` passed to these functions is computed from the repository root (see `vaf/core/agent.py`, where `self.models_dir = base_dir/models`).
