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

---

## Technical Risks

### 1. LLM Tool Execution (Prompt Injection)
The agent has access to "Heavy Tools" such as:
- `bash` / `run_shell_command` (Direct OS access)
- `python` (Code execution)
- `write_file` / `delete_file` (Filesystem modification)

While VAF implements "Trust Gating" (asking for permission on risky tools), a sophisticated **Prompt Injection** attack (where the model is tricked by malicious input or a website it searches) could potentially bypass these safeguards or convince the user to approve a dangerous action.

### 2. Credential Safety
VAF stores your OAuth tokens and IMAP/SMTP passwords in your **OS Keyring** (Windows Credential Manager, macOS Keychain, Linux Secret Service) or, when no keyring is available, an **AES-256-GCM encrypted fallback file**. API provider keys live in `config.json`, which VAF writes with owner-only (`0600`) permissions.
- **Risk:** Anyone with access to your user account on the host machine can potentially retrieve these credentials. Setting a master passphrase (`VAF_MASTER_PASSPHRASE`) derives the fallback encryption key from it (scrypt) instead of writing the key to disk, so the encrypted fallback file resists access even by someone who can read your files.
- **Risk:** If you share your VAF instance in "Local Network Mode", other users are isolated via `user_scope_id`, but the host administrator still has full visibility into all data.

### 3. Browser Agent (browser_agent tool)

The `browser_agent` tool controls a real headless Chromium browser. This introduces specific risks:

- **Credential exposure:** If a task includes login credentials in the `task` parameter, they are passed to the LLM and executed in the browser. Credentials are never stored by VAF, but they appear in the agent's run history for the duration of the session.
- **Irreversible actions:** The browser can submit forms, complete purchases, and send messages. VAF classifies this tool as `permission_level = "dangerous"` and requires explicit user confirmation before each run.
- **CDP port:** The Chrome DevTools Protocol port (`9222`) is bound to `127.0.0.1` only and is never exposed to the network. Do not change this binding.
- **Browser network isolation:** The `vaf-browser` container runs on its own isolated Docker network (`vaf-browser-network`) and is **not** on `vaf-network`. It cannot reach `postgres` or `redis` by hostname. A compromised browser (e.g. via SSRF or malicious page) has no direct path to the database.
- **Bot detection bypass:** VAF injects a stealth script (`vaf/tools/_stealth_payload.js`) to reduce bot detection. This script is vendored directly in the repository — it does not update automatically and carries no runtime PyPI dependency.
- **Channel restriction:** `browser_agent` is blocked on Telegram, WhatsApp, and Discord channels by design. It can only be triggered from the Web UI or API.

### 4. Sub-Agent Isolation
Sub-agents (Coder, Librarian, Research) run in separate processes. While they are instructed to stay within certain directories, they are **not fully sandboxed** unless you are running in Docker mode. Even in Docker mode, excessive resource consumption (RAM/CPU) can lead to Denial of Service (DoS) on the host machine.

### 5. Email Accounts (IMAP/SMTP)

VAF connects to user-supplied mail servers, so it treats the IMAP/SMTP host you enter as an untrusted outbound target:

- **SSRF / private-host guard:** Before connecting (and on the "test connection" check), VAF resolves the host and refuses any address that is not globally routable — loopback, RFC-1918 private ranges, and link-local addresses (including the `169.254.169.254` cloud-metadata endpoint). This blocks a malicious or mistyped mail-host config from probing your internal network. Multicast/reserved/link-local are refused unconditionally. If you genuinely run a mail server on your LAN or the same host, set `email_allow_private_hosts=true` in `config.json` to allow loopback / private addresses (the metadata endpoint stays blocked even then). Default is `false`.
- **TLS verification:** IMAP and SMTP connections verify the server's TLS certificate against the system trust store; connection attempts time out rather than hanging.
- **Credential redaction over the API:** `GET /api/config` returns secrets (API keys, OAuth client secrets, the JWT/encryption keys, IMAP/SMTP passwords, and database URLs) only to admin users. Non-admin network users receive a redacted config and only their own scoped account list.

---

## Multi-Tenant Security (Network Mode)

VAF uses a **UUID-based Scoping System** (`user_scope_id`) to isolate data between users.
- **Isolation:** User A cannot see User B's memories, emails, or contacts.
- **Fallback Logic:** In single-user setups, VAF uses robust fallbacks to ensure local tools keep working if session IDs change. This is a trade-off between "perfect isolation" and "local usability".

---

## Supply-Chain Security

VAF takes the following measures to reduce dependency-based attack surface:

### Vendored dependencies
The following packages are copied directly into the VAF repository and are **not fetched from PyPI at runtime**:

| Package | Location | Version pinned | SHA-256 |
|---|---|---|---|
| `langid` (language detection) | `vaf/vendor/langid/` | 1.1.6 | `5e4d4991...` |
| `playwright-stealth` JS payload | `vaf/tools/_stealth_payload.js` | 2.0.3 | `5601b9cc...` |

These packages were selected for vendoring because they are maintained by single individuals, change rarely, and are small enough to audit manually.

### Tiered dependency strategy

Dependencies are handled at one of three tiers based on risk:

| Tier | When to use | Examples |
|---|---|---|
| **Vendor** | Single maintainer, small, changes rarely, no transitive deps | `langid`, `playwright-stealth` JS payload |
| **Pin + freeze** | Multi-contributor, well-audited, hard to replace, stdlib replacement not worth the cost | `schedule`, `requests`, `fastapi` |
| **Remove / replace with stdlib** | Scraper wrappers, thin single-maintainer shims with an obvious direct equivalent | `ddgs` → direct HTTP to `lite.duckduckgo.com`, `pyttsx3` → Docker TTS |

**`schedule` decision:** `schedule` is pinned in `requirements.lock` with a SHA-256 hash and is never updated automatically. The stdlib replacement would provide identical security at the cost of ~200 lines of datetime arithmetic — not worth the maintenance burden. Current pinned version: `1.2.2`.

### Pinned lockfile
`requirements.lock` pins every dependency (direct and transitive) to an exact version with SHA-256 hashes, so a compromised new version on PyPI cannot be installed silently. For a hardened install, use it explicitly:

```bash
pip install --require-hashes -r requirements.lock
```

**Honest caveat:** the default installer (`install.sh` / `install.ps1`) and `vaf update` install from `requirements.txt` (version ranges, no hashes) for portability across platforms and Python versions — so the hash-pinned protection above is **opt-in**, not the default path. On a trust-sensitive host, install from the lockfile yourself. The lockfile is regenerated intentionally with `pip-compile --generate-hashes` — never via an automated process.

### Updating dependencies
When a dependency needs to be updated:
1. Update the version in `requirements.txt`
2. Run `pip-compile --generate-hashes --allow-unsafe --output-file requirements.lock requirements.txt`
3. Review the diff in `requirements.lock` before installing
4. For vendored packages: copy the new source, verify the SHA-256, update the hash comment in the consuming file

### Installer downloads
The one-click installer/bootstrap provisions toolchain prerequisites by downloading them from their official vendors over HTTPS **when missing**: `uv` (astral.sh), a portable Node.js (nodejs.org), and on Windows a portable Git/MinGit (git-for-windows). These are fetched at install time, not bundled, and are **not checksum-pinned by VAF** — a deliberate trade-off (the same model comparable agent installers use). On a trust-sensitive host, install these prerequisites yourself first so the installer uses what is already present.

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
