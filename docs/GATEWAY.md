# VAF Gateway Architecture

The **VAF Gateway** is the central nervous system of the Veyllo Agentic Framework. It decouples the Agent's logic from the interfaces (CLI, Discord, Web), allowing the agent to be omnipresent across multiple channels simultaneously.

## Architecture

Instead of a monolithic script, VAF uses a client-server model:

1.  **The Gateway (`vaf.core.gateway`):** A FastAPI/WebSocket server that maintains state and routes messages.
2.  **Clients:**
    *   **CLI:** The standard terminal interface.
    *   **Bridge:** Connectors for external platforms (Discord, Slack).
    *   **Web UI:** Active React/Next.js frontend used in production.

## Protocol

Communication uses strict Pydantic models defined in `vaf.core.protocol`.

### Message Types
*   **CommandRequest (`agent.prompt`):** Input from a user (e.g., "Fix this bug").
*   **EventFrame (`status`, `log`, `response`):** Output from the system.

## Running the Gateway

The gateway is built on Uvicorn and FastAPI.

```bash
# Start the Gateway Server
python -m vaf.core.gateway
```

By default, it listens on `ws://127.0.0.1:8000`.

## Connecting a Client (Example: Discord)

```bash
# Start the Discord Bridge (requires Gateway to be running)
vaf bridge discord --token "YOUR_BOT_TOKEN"
```
