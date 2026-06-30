# VAF Core Logic

The `vaf.core` module contains the fundamental building blocks of the Veyllo Agentic Framework. It handles the "brain" of the agent, resource management, and system-wide orchestration.

## Key Modules

- **agent.py**: The primary agent class that orchestrates tool use, planning, and response generation.
- **gateway.py**: The FastAPI-based gateway server for multi-client access (Web UI, Discord, etc.).
- **context.py**: Implements Cursor-style context management, compression, and token tracking.
- **backend.py**: Manages local LLM servers (e.g., llama-server) with GPU acceleration detection.
- **web_server.py**: WebSocket API server for the WebUI.
- **web_interface.py**: Session-scoped broadcast manager for WebUI updates.
- **headless_runner.py**: Headless agent loop used by the tray app.
- **system_prompt.py**: The dynamic prompt router that loads context modules on-demand.
- **snapshot.py**: Handles the Git-based undo and state tracking system.
- **trust.py**: Security and permission gating for tool execution.
- **subagent_ipc.py**: Sub-agent IPC and result coordination.

## Usage

This directory is not intended to be used directly by end-users. Instead, it provides the API and logic used by the CLI and Web interfaces. Developers should modify these files when changing the core behavior of the agent or its communication protocols.

## Architecture

VAF follows a modular architecture where the `CoreAgent` in `agent.py` interacts with:
1.  **Backends**: Local or Cloud-based LLMs.
2.  **ContextManager**: To maintain conversation state.
3.  **ToolRegistry**: To execute specialized tasks.

## Dependencies

- **FastAPI/Uvicorn**: For the gateway server.
- **Pydantic**: For data validation and the communication protocol.

Hardware acceleration is detected by `gpu_detection.py`, which probes the system via subprocess (e.g. `nvidia-smi` for NVIDIA, `rocm-smi` or Windows PowerShell/WMI for AMD) without requiring an extra Python package. The result is consumed by `backend.py` to choose a default model.
