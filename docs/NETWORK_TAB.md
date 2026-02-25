# Local Network & Secure Access Portal (Dummy UI Documentation)

This document provides a detailed overview of the **Local Network** tab in the settings and the associated **Login Portal**. Currently implemented as a high-fidelity Dummy UI, these components serve as the blueprint for the upcoming local network functionality of VAF.

---

## 1. Local Network Tab (Settings)

The **Local Network** tab is the central command center for managing local network access to the VAF instance. It allows the main user (Admin) to configure who can access the agent from other devices in the same network.

### 1.1 UI Structure & Current (Dummy) Features

#### **A. Global Toggle**
*   **Element:** "Enable Local Network Hosting" Switch.
*   **Current Behavior:** Visually enables/disables the underlying sections (opacity change).
*   **Future Logic:** Starts/Stops the internal Next.js server binding to `0.0.0.0` and opens the configured port in the local firewall.
*   **CLI alternative:** The same behavior can be triggered from the terminal: `vaf server on` (enable hosting + SSL), `vaf server off` (disable), `vaf server status` (show status and network URLs). The Tray observes config changes and restarts backend/frontend automatically.

#### **B. Network Topology Visualization**
*   **Element:** "Open Network Map" Button & Full-Screen Modal.
*   **Visualization:** Interactive node graph (`ReactFlow`) showing the host and connected devices (Desktop, Laptop, Mobile).
*   **Indicators:** 
    *   **Online/Offline:** Color-coded status dots (Green = Connected, Gray = Offline).
    *   **Device Type:** Specific icons for quick identification.
    *   **Identity:** Formatted as `username@device` (e.g., `admin@Main-Station`).
*   **Future Logic:** Real-time WebSocket connection tracking to visualize active sessions and bandwidth usage per node.

#### **C. User Management (CRUD)**
*   **Element:** Searchable Data Table.
*   **Columns:** Username, Role, Last Active, Status, Actions.
*   **Actions:**
    *   **Create User:** Modal to add new local accounts with specific permissions.
    *   **Edit User:** Update email, role, or reset credentials.
    *   **Delete User:** Remove access permanently.
*   **Detail View:** Clicking a user reveals a comprehensive profile showing:
    *   **Authorized Tools:** (e.g., Web Search, File System) - currently checkboxes.
    *   **Workflows:** Active automation scripts allowed for this user.
    *   **Memory DB:** Stats on the user's dedicated vector store usage.

#### **D. Server Info**
*   **Display:** Status cards for "Container Name", "Port", "Uptime".
*   **Future Logic:** Real Docker container stats streamed via IPC.

---

## 2. Secure Login Portal (`/login`)

The Login Portal is the entry point for other devices on the network. It simulates a secure authentication flow required for local access.

### 2.1 Authentication Flow

#### **Step 1: Primary Authentication**
*   **URL:** `http://<HOST_IP>:3000/login`
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
| **General** | ✅ Visible | ❌ **Hidden** | API Keys & System-wide secrets. |
| **AI & Model** | ✅ Visible | ❌ **Hidden** | Global LLM selection (Llama/OpenAI) is set by Admin. |
| **Local Network** | ✅ Visible | ❌ **Hidden** | Only Admin can manage users and network ports. |
| **Interface** | ✅ Visible | ❌ **Hidden** | Global UI defaults (e.g., auto-open folders). |
| **Connections** | ✅ Visible | ✅ **Visible** | **User-Scoped:** Integrations (WhatsApp, Discord) linked here apply *only* to this user. |
| **Automations** | ✅ Visible | ✅ **Visible** | **User-Scoped:** Users can schedule their own personal workflows. |
| **Advanced** | ✅ Visible | ⚠️ **Restricted** | Users see only personal memory settings; System sub-agent configs are hidden. |

### 3.2 The "User Box" (Data Scoping)

**"Scope"** determines where data is stored and who can use it.

1.  **Global Scope (Admin/System):**
    *   *Example:* The Admin configures the `OpenAI API Key`.
    *   *Result:* All users can *use* the AI to chat, but they cannot *see* or *change* the key.

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
*   Update `docker-compose.yml` to expose ports securely.
*   Implement a helper script to detect and display the Host Machine's LAN IP address automatically in the Settings UI.

---

## 4. User Experience (UX) Summary

The goal is to provide **Enterprise-Grade Security** for a **Local Tool**.

1.  **Admin** opens Settings on their PC, enables "Local Network", creates a user `colleague`.
2.  **Colleague** opens `http://192.168.1.X:3000` on their iPad.
3.  **Colleague** logs in with temp password.
4.  **Colleague** is forced to scan a QR code with their iPhone.
5.  **Colleague** enters the code and gains access to the specific Tools/Memories allowed by the Admin.

This ensures that VAF can be used securely in team environments without exposing the full system to everyone on the WiFi.
