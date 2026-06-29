# Security Policy & Risk Disclosure

VAF is a powerful AI automation framework designed for **local and private network use**. Because it gives an LLM (Large Language Model) the ability to interact with your filesystem, execute code, and access your connected accounts, it carries significant security risks if misconfigured.

> **Status: early alpha.** VAF is under active development — expect breaking changes, rough edges, and incomplete hardening. Use it for **private / internal** purposes only. Do **not** expose the login, Web UI, or API to the public internet, and do not rely on it for production or with data you cannot afford to lose. The "no public web hosting" rule below is not optional.

---

## 🛑 CRITICAL WARNING: NO PUBLIC WEB HOSTING

**NEVER host the VAF Web UI directly on the public internet.**

VAF is not designed to be a public-facing web application. Even with network mode and authentication enabled, exposing this software to the open web puts your entire machine and all connected accounts at extreme risk.

**Safe usage patterns:**
- **Localhost:** Running only on `127.0.0.1` (Default).
- **VPN / SSH Tunnel:** Accessing your home VAF instance through a secure tunnel (e.g., Tailscale, WireGuard).
- **Private LAN:** Running on a local network with a trusted firewall.


## Best Practices for Users

1.  **Use a Dedicated Machine:** If possible, run VAF on a separate machine or a dedicated Virtual Machine (VM).
2.  **Enable Docker:** Use the Docker-based sandbox for code execution to limit the impact of malicious code.
3.  **Review Tool Calls:** Never blindly approve a tool call (especially `bash` or `write_file`) without reading the arguments.
4.  **Keep it Private:** Do not port-forward port `8001` or `3000` on your router.
5.  **Secure your Keyring:** Ensure your OS user account has a strong password, as it protects your connected account tokens.



## Detailed security documentation

This file is intentionally brief. The specific risks, mitigations, and isolation guarantees are documented in depth here:

- **Sandboxing & code execution** — how `bash`/`python`/sub-agents are isolated in Docker mode, and what is *not* sandboxed otherwise: [docs/security/SANDBOXING.md](docs/security/SANDBOXING.md), [docs/security/SANDBOX_MODULES.md](docs/security/SANDBOX_MODULES.md)
- **Multi-user & network-mode isolation** — `user_scope_id` scoping, credential redaction over the API, and the login portal: [docs/security/USER_ISOLATION.md](docs/security/USER_ISOLATION.md), [docs/setup/NETWORK_FEATURES.md](docs/setup/NETWORK_FEATURES.md), [docs/setup/SERVER_MODE.md](docs/setup/SERVER_MODE.md)
- **Tool trust-gating & permission levels** — how risky tools require explicit confirmation (relevant to prompt-injection): [docs/agents/TOOL_SUPERVISION.md](docs/agents/TOOL_SUPERVISION.md)
- **Browser agent** — headless Chromium risks, Docker network isolation, and channel restrictions: [docs/agents/BROWSER_AGENT.md](docs/agents/BROWSER_AGENT.md)
- **Connected accounts & credentials** — OS keyring / encrypted fallback, OAuth, and the email IMAP/SMTP SSRF / private-host guard: [docs/integrations/CONNECTIONS.md](docs/integrations/CONNECTIONS.md), [docs/setup/CONFIG_SCHEMA.md](docs/setup/CONFIG_SCHEMA.md)


## Reporting a Vulnerability

If you discover a security vulnerability, please **do not** open a public issue. Instead, report it privately via the contact methods specified in the repository owner's profile or via a private security advisory on GitHub.



## Disclaimer

**VAF is provided "as is", without warranty of any kind.** The authors and contributors are not responsible for any damage, data loss, or unauthorized access resulting from the use of this software. By using VAF, you accept all associated risks.
