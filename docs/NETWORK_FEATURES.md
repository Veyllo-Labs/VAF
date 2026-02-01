# 🌐 VAF Network Features & Security

VAF (Veyllo Agent Framework) includes robust networking capabilities designed to allow secure, local collaboration. This document details the architecture, security measures, and usage of these features.

## 🔒 Security Model

Security is the primary design constraint for VAF's network features. The system employs a "Defense in Depth" strategy:

### 1. Firewall Automation
When "Local Network Hosting" is enabled, VAF automatically configures the OS firewall (Windows Firewall, macOS pf, or Linux iptables) to:
- **Allow**: Traffic from RFC 1918 Private IP ranges (e.g., `192.168.x.x`, `10.x.x.x`).
- **Allow**: Localhost traffic.
- **Block**: All other incoming traffic to VAF ports (default 3000/8001).

### 2. Authentication
- **Localhost Bypass**: Accessing VAF from the host machine (`127.0.0.1`) grants automatic administrative access for convenience.
- **Remote Access**: Any connection from a non-localhost IP **must** authenticate using a valid username and password.
- **2FA**: Two-Factor Authentication (TOTP) is enforced for administrative accounts and optional for standard users.

### 3. Connection Tracking
The system actively tracks all connections (WebSocket and HTTP) to the VAF backend.
- **Real-time Monitoring**: The "Network Topology" map in Settings visualizes all active devices.
- **Pre-Auth Tracking**: Devices are detected and displayed as "Guest" or "Unauthenticated" immediately upon connection, ensuring visibility of unauthorized access attempts.

---

## 🛠️ Configuration

Network settings are managed via the Web UI (Settings -> Local Network).

### Customizable Parameters
- **Enable Local Network Hosting**: Master toggle for external access.
- **Port**: The frontend port can be customized (default: 3000) to avoid conflicts.
- **Host IP**: Displays the detected LAN IP address for sharing.

### Live Updates
Changes to network settings trigger an automatic, orchestrated restart of the frontend and backend services to apply new bindings (e.g., switching from `127.0.0.1` to `0.0.0.0`).

---

## 📡 API Reference

### 1. Get Access URL
**GET** `/api/network/access-url`

Returns the correct URL for other devices to connect to VAF.

**Response:**
```json
{
  "host": "192.168.1.50",
  "port": 3000,
  "url": "http://192.168.1.50:3000"
}
```

### 2. Get Active Connections
**GET** `/api/network/connections`

Returns a list of currently connected devices for the Network Topology map.

**Response:**
```json
[
  {
    "id": "ws_123456",
    "type": "websocket",
    "ip": "192.168.1.102",
    "device_type": "mobile",
    "username": "Guest (Connecting...)",
    "connected_at": 1700000000.0
  }
]
```
