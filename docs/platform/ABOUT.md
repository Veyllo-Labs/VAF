# About VAF (Veyllo Agentic Framework)

VAF is an agentic framework that runs locally on your own hardware. It works with local GGUF models and with external AI providers, and keeps data within your environment unless you connect an external service.

## Core principles

- **Local-first:** VAF runs on your own machine. There is no required cloud dependency; with the local provider, requests are not sent to external services.
- **Privacy:** No third-party tracking is built in. Data stays in your local environment unless you configure an external provider or integration.
- **Multi-user support:** Multiple users can share one instance. Each user gets an isolated scope — separate memory, preferences, and tasks. See [USER_ISOLATION.md](../security/USER_ISOLATION.md).
- **Long-term memory:** VAF stores information in a vector-backed memory system and retrieves it across sessions. See [MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md).
- **Automation:** VAF can run scheduled tasks and multi-step workflows. See [AUTOMATIONS.md](AUTOMATIONS.md).
- **Extensible and hybrid:** VAF supports local models and external API providers (OpenAI, Anthropic, Google, DeepSeek, OpenRouter). You can add your own tools and embed VAF as a library — see [ARCHITECTURE.md](../ARCHITECTURE.md) and [EMBEDDING.md](../EMBEDDING.md).
