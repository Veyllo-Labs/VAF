# Local Network & Secure Access Portal

This document provides a detailed overview of the **Local Network** tab in the settings and the associated **Login Portal**. The core network functionality — LAN hosting via the integrated HTTPS proxy, automatic firewall opening, user management, and the live Network Map — is implemented and live. A few presentational elements (the Server Info cards, parts of the Login Portal) are still illustrative placeholders and are called out as such below.

---

## 1. Local Network Tab (Settings)

The **Local Network** tab is the central command center for managing local network access to the VAF instance. It allows the main user (Admin) to configure who can access the agent from other devices in the same network.

### 1.1 UI Structure & Features

#### **A. Global Toggle**
*   **Element:** "Enable Local Network Hosting" Switch.
*   **Behavior:** Starts/Stops the integrated HTTPS proxy that fronts VAF for the LAN. The Next.js frontend stays bound to `127.0.0.1` (localhost only); LAN devices reach VAF exclusively via the proxy at `https://<LAN-IP>:8443`. The proxy binds the configured `local_network_https_port` (default 443) and automatically falls back to `8443` on any platform (Linux/macOS/Windows) when 443 is privileged/unbindable by a non-root user; the effective bound port is what the UI displays. On Linux the firewall port is opened automatically: VAF prefers `firewalld` and adds a rich rule scoped to the LAN subnet for the effective proxy port (e.g. `8443`), elevating via a `pkexec` polkit password prompt in a desktop session (or `sudo -n` headless); `iptables`/`ufw` are the fallback when firewalld is not running. Disabling hosting truly stops the proxy and the internal plain channel so LAN access closes (the permanent firewall rule remains, harmless, as nothing then listens on the port).
*   **Access URL for other devices:** The tab shows the full copyable LAN URL — including `https://` and the effective proxy port (e.g. `https://192.168.1.50:8443`) — that other devices open. The backend port (8001) is shown for reference but binds `127.0.0.1` and is unreachable from the LAN. The values come from `GET /api/network/access-url`.
*   **CLI alternative:** The same behavior can be triggered from the terminal: `vaf server on` (enable hosting + SSL), `vaf server off` (disable), `vaf server status` (show status and network URLs). The Tray observes config changes and restarts backend/frontend automatically.

#### **B. Network Topology Visualization**
*   **Element:** "Open Network Map" Button & Full-Screen Modal.
*   **Visualization:** Interactive node graph (`ReactFlow`) with the VAF Host at the centre and one node per remote device that currently has a live connection. The map polls `GET /api/network/connections` (every 4s while the full-screen map modal is open, 15s while the tab is open), de-duplicates by IP (keeping the most recently active connection), and filters out localhost/the host itself — so a device only appears while it is actually connected, and there is no separate offline state on map nodes.
*   **Device type:** Derived from each connection's User-Agent — `mobile` maps to a Smartphone icon (pink), `tablet` to a Laptop icon (purple), and everything else/desktop/unknown to a Monitor icon (green). Edges from the host to each device are animated blue lines.
*   **Identity:** Each device node shows the logged-in user's name (their alias, or an em dash if none) above the device's real internal IP in monospace; the central VAF Host node shows `host:port` (or `localhost` when no LAN host is known). The real client IP reaches the backend because the HTTPS proxy forwards it via the `X-Forwarded-For` header.
*   **Active devices count:** The count on the "Open Network Map" card reports connected remote devices only — it excludes the central host node and floors at 0.
*   **Future Logic:** Per-node bandwidth visualization remains future work.

#### **C. User Management (CRUD)**
*   **Element:** Searchable data table (columns centered).
*   **Columns:** Username, Role, Last active, Status, Actions.
*   **Status / Last active are REAL (not the account flag):** a user shows `Active` (green dot) when they currently have a live WebSocket connection; `Last active` shows `now` while online, otherwise the last login time (or `—`). Source: `GET /api/users` returns an `online` field computed from the connection manager's live user scopes (with an admin-scope fallback so the local admin is detected reliably). This is distinct from the `is_active` account-enabled flag.
*   **Actions:**
    *   **Create User:** Modal to add a local account (see Access presets below).
    *   **Edit User:** Update email, role, or reset credentials.
    *   **Delete User:** Remove access permanently.
*   **Detail view:** Clicking a user reveals their profile (authorized tools, workflows, Memory DB usage).

##### Create User — Access presets

Instead of ticking ~95 individual tool checkboxes (overwhelming for a non-expert), the Add-User modal offers a single **Access** level. Presets are computed from the LIVE tool/workflow lists, so the admin's own **custom** tools/workflows are covered automatically — the rules match on the tool name, not a fixed list.

| Preset | Tools granted | Workflows granted |
| --- | --- | --- |
| **Standard** (default) | all tools EXCEPT destructively-named ones (name contains `delete` / `remove` / `drop` / `clear` / `reset` / `uninstall` / `kill` / `destroy` / `wipe` / `purge` / `revoke`) | all workflows |
| **Full** | every tool, including custom ones | every workflow |
| **Read-only** | only viewing/reading tools (`list` / `read` / `get` / `search` / `view` / `show` / `fetch` / `find` / `query` / `describe` / `status` / `info` / `count`), excluding any destructively-named tool | none |
| **Custom** | reveals the granular tool + workflow checkbox grids for manual selection | manual |

The selected preset writes the resolved tool names and workflow ids into the user's `permissions` (`{"tools": [...], "workflows": [...]}`). **Full** therefore stores the complete current set of all tools and all workflows that exist; the default of **Standard** replaces the previous behaviour where a new user was created with zero tools selected.

> Note: these per-user tool/workflow permissions are currently STORED but not yet enforced at agent runtime — `evaluate_tool_policy` gates on admin-only, channel restrictions, and the confirmation level, not on the per-user list. Treat the presets as the intended access model; runtime enforcement of the per-user list is a separate, still-open step.

#### **D. Server Info**
*   **Display:** Status cards for "Container Name", "Port", "Uptime".
*   **Live LAN status:** While hosting is enabled the tab polls `GET /api/network/status` (every 3s) and shows the real proxy state as a status dot — amber pulsing while the proxy comes up, green once it has actually bound, or red with the bind error if it failed to bind. The "authentication required" and "no public access" lines stay green.
*   **Future Logic:** Real Docker container stats (Container Name / Uptime) streamed via IPC.

---

## 2. Secure Login Portal (`/login`)

The Login Portal is the entry point for other devices on the network. It simulates a secure authentication flow required for local access.

### 2.1 Authentication Flow

#### **Step 1: Primary Authentication**
*   **URL (LAN devices):** `https://<LAN-IP>:8443/login` (TLS via the integrated HTTPS proxy; accept the self-signed certificate once). The desktop app reaches the local UI directly at `http://127.0.0.1:3000`; port 3000 is localhost-only and not reachable from the LAN.
*   **UI:** Clean, branded login form.
*   **Inputs:** Username & Password.
*   **Logic:**
    *   Checks credentials against the local SQLite user database.
    *   **Security:** Rate limiting to prevent brute-force attacks on the local network.

#### **Step 2: Mandatory 2FA Setup (First Time)**
*   **Trigger:** Occurs immediately after the first successful password entry for a new device/user.
*   **UI:**
    *   **QR Code:** Generated securely for Time-based One-Time Password (TOTP).
    *   **Backup Codes:** A set of one-time recovery codes displayed *once*.
*   **Why:** In a shared office LAN, password sniffing is a risk. 2FA ensures that even if a password is compromised, physical access (smartphone) is required to log in.

#### **Step 3: 2FA Verification (Subsequent Logins)**
*   **UI:** Input field for the 6-digit TOTP code.
*   **Logic:** Verifies the code against the server's time window.

#### **Step 4: User Dashboard**
*   **UI:** A "Welcome" landing page for connected users.
*   **Features:**
    *   **System Status:** Green/Red indicators for Memory DB and LLM availability.
    *   **Activity Log:** Audit trail of recent actions (e.g., "Logged in", "Ran Workflow").
    *   **Main Console Button:** Redirects to the main Chat/Agent interface (`/`).

---

## 3. Role-Based Access & User Isolation

To ensure security and privacy, the system strictly separates "Infrastructure Configuration" from "Personal Customization". This concept is technically referred to as **User Profile Isolation**.

### 3.1 Settings Visibility Matrix

When a user logs in via the Local Network, the Settings Interface adapts dynamically based on their role (`Admin` vs. `User`). Standard users operate within their own isolated "sandbox" and cannot modify system-critical parameters.

| Settings Category | Admin (Host) | Standard User (Client) | Scope / Logic |
| :--- | :---: | :---: | :--- |
| **General** | Visible | **Hidden** | API Keys & System-wide secrets. |
| **AI & Model** | Visible | **Hidden** | Global LLM selection (Llama/OpenAI), plus sub-agent and thinker provider/model, set by Admin. |
| **Local Network** | Visible | **Hidden** | Only Admin can manage users and network ports. |
| **Interface** | Visible | **Hidden** | Global UI defaults (e.g., auto-open folders). |
| **Connections** | Visible | **Visible** | **User-Scoped:** Integrations (WhatsApp, Discord) linked here apply *only* to this user. |
| **Automations** | Visible | **Visible** | **User-Scoped:** Users can schedule their own personal workflows. |
| **Advanced** | Visible | **Restricted** | Users see only personal settings; system management panels (Tools, MCP, Workflows) are hidden. |

### 3.2 The "User Box" (Data Scoping)

**"Scope"** determines where data is stored and who can use it.

1.  **Global Scope (Admin/System):**
    *   *Example:* The Admin configures the `OpenAI API Key`.
    *   *Result:* All users can *use* the AI to chat, but they cannot *see* or *change* the key. This is enforced server-side: `GET /api/config` (and the WebSocket config push) redact secret values for non-admins via `Config.config_for_user()` / `Config.is_secret_config_key()` (API keys, OAuth client secrets, encryption/KEK keys, the JWT secret, and the Memory DB / Redis URLs), so secrets never reach a non-admin client even if a settings panel is opened directly. Admins still receive the full config.

2.  **User Scope (Personal Profile):**
    *   *Example:* User "John" connects his personal **WhatsApp** account in the "Connections" tab.
    *   *Result:*
        *   The token is stored securely in `users/john/credentials.enc`.
        *   Only John's agent session can read/write to that WhatsApp account.
        *   The Admin or other users cannot access John's messages.
    *   *Analogy:* Think of it like a corporate email. The company (Admin) provides the server (Infrastructure), but your mailbox (Personal Scope) is yours alone.

### 3.3 Conflict Prevention & Instance Isolation
Crucially, isolation extends to **runtime execution**.
*   **Parallel Integrations:** If User A and User B **both** have WhatsApp integrations active, the system spawns two distinct, isolated instances of the `WhatsAppService`.
    *   User A's instance connects to Phone A.
    *   User B's instance connects to Phone B.
    *   There is **zero overlap** or conflict. The system routes incoming events (e.g., a message received) specifically to the correct user session based on the instance ID.

### 3.4 Task & Queue Isolation (The "Queues")
Workloads are managed to prevent one user from hogging the system.
*   **User-Specific Command Queues:** When a user initiates a task (e.g., "Research this topic"), it is pushed to their personal **Task Queue**.
*   **Fair Scheduling:** The central "Task Manager" distributes compute resources fairly. User A's long-running automation will typically run in a background worker, ensuring that User B's interactive chat remains responsive.
*   **Non-Blocking:** A heavy database query by one user does not freeze the interface for others. Each "box" runs its processes independently.

---

## 4. Future Implementation Roadmap

To turn these Dummy UIs into functional features, the following backend logic is required:

### **Phase 1: Database Schema**
*   Create `LocalUsers` table in SQLite:
    ```sql
    CREATE TABLE local_users (
        id UUID PRIMARY KEY,
        username TEXT UNIQUE,
        password_hash TEXT,
        role TEXT, -- 'admin', 'user', 'guest'
        totp_secret TEXT,
        permissions JSON, -- List of allowed tools/workflows
        is_active BOOLEAN
    );
    ```

### **Phase 2: Middleware & Auth**
*   Implement NextAuth.js or a custom JWT middleware.
*   Protect all API routes (`/api/*`) to check for a valid session token from the `LocalUsers` table.
*   **Network Restriction:** Allow Login page access from any IP, but restrict Admin Settings strictly to `localhost` (the host machine) for safety.

### **Phase 3: 2FA Logic**
*   Integrate `otplib` for TOTP generation and verification.
*   Store `totp_secret` encrypted in the database.

### **Phase 4: Docker Networking**
*   Update `docker-compose.yml` to expose ports securely. The integrated HTTPS proxy port (`8443`) is the only port exposed to the LAN; the backend (`8001`) and frontend (`3000`) stay bound to `127.0.0.1` and are not LAN-reachable.
*   Implement a helper script to detect and display the Host Machine's LAN IP address automatically in the Settings UI.

---

## 4. User Experience (UX) Summary

The goal is a security model suitable for a local application.

1.  **Admin** opens Settings on their PC, enables "Local Network", creates a user `colleague`.
2.  **Colleague** opens `https://<LAN-IP>:8443` on their iPad (TLS via the integrated HTTPS proxy) and accepts the self-signed certificate once.
3.  **Colleague** logs in with temp password.
4.  **Colleague** is forced to scan a QR code with their iPhone.
5.  **Colleague** enters the code and gains access to the specific Tools/Memories allowed by the Admin.

This ensures that VAF can be used securely in team environments without exposing the full system to everyone on the WiFi.
