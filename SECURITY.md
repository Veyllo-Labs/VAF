# Security Policy & Risk Disclosure

VAF is a powerful AI automation framework designed for **local and private network use**. Because it gives an LLM (Large Language Model) the ability to interact with your filesystem, execute code, and access your connected accounts, it carries significant security risks if misconfigured.

---

## 🛑 CRITICAL WARNING: NO PUBLIC WEB HOSTING

**NEVER host the VAF Web UI or API directly on the public internet.**

VAF is not designed to be a public-facing web application. Even with network mode and authentication enabled, exposing this software to the open web puts your entire machine and all connected accounts at extreme risk.

**Safe usage patterns:**
- **Localhost:** Running only on `127.0.0.1` (Default).
- **VPN / SSH Tunnel:** Accessing your home VAF instance through a secure tunnel (e.g., Tailscale, WireGuard).
- **Private LAN:** Running on a local network with a trusted firewall.

---

## Technical Risks

### 1. LLM Tool Execution (Prompt Injection)
The agent has access to "Heavy Tools" such as:
- `bash` / `run_shell_command` (Direct OS access)
- `python` (Code execution)
- `write_file` / `delete_file` (Filesystem modification)

While VAF implements "Trust Gating" (asking for permission on risky tools), a sophisticated **Prompt Injection** attack (where the model is tricked by malicious input or a website it searches) could potentially bypass these safeguards or convince the user to approve a dangerous action.

### 2. Credential Safety
VAF stores your API keys and OAuth tokens in your **OS Keyring** (Windows Credential Manager, macOS Keychain) or an **encrypted fallback file**.
- **Risk:** Anyone with physical or remote access to your user account on the host machine can potentially retrieve these credentials.
- **Risk:** If you share your VAF instance in "Local Network Mode", other users are isolated via `user_scope_id`, but the host administrator still has full visibility into all data.

### 3. Sub-Agent Isolation
Sub-agents (Coder, Librarian, Research) run in separate processes. While they are instructed to stay within certain directories, they are **not fully sandboxed** unless you are running in Docker mode. Even in Docker mode, excessive resource consumption (RAM/CPU) can lead to Denial of Service (DoS) on the host machine.

---

## Multi-Tenant Security (Network Mode)

VAF uses a **UUID-based Scoping System** (`user_scope_id`) to isolate data between users.
- **Isolation:** User A cannot see User B's memories, emails, or contacts.
- **Fallback Logic:** In single-user setups, VAF uses robust fallbacks to ensure local tools keep working if session IDs change. This is a trade-off between "perfect isolation" and "local usability".

---

## Best Practices for Users

1.  **Use a Dedicated Machine:** If possible, run VAF on a separate machine or a dedicated Virtual Machine (VM).
2.  **Enable Docker:** Use the Docker-based sandbox for code execution to limit the impact of malicious code.
3.  **Review Tool Calls:** Never blindly approve a tool call (especially `bash` or `write_file`) without reading the arguments.
4.  **Keep it Private:** Do not port-forward port `8001` or `3000` on your router.
5.  **Secure your Keyring:** Ensure your OS user account has a strong password, as it protects your connected account tokens.

---

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not** open a public issue. Instead, report it privately via the contact methods specified in the repository owner's profile or via a private security advisory on GitHub.

---

## Disclaimer

**VAF is provided "as is", without warranty of any kind.** The authors and contributors are not responsible for any damage, data loss, or unauthorized access resulting from the use of this software. By using VAF, you accept all associated risks.
