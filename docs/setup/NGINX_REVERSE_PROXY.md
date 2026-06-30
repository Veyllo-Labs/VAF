# Nginx Reverse Proxy for HTTPS (Network Mode)

**Note:** VAF includes an **integrated HTTPS proxy**. If you enable **Local Network** and **SSL/TLS** in settings and set certificate/key paths, VAF starts the proxy on port 443 (or `local_network_https_port`, e.g. 8443 on Windows). **Nginx is not required**—the integrated proxy is the single HTTPS entry point (localhost and LAN). It forwards `/api` and `/ws` to the internal backend channel and allows all HTTP methods (so login and API calls work). See [NETWORK_FEATURES.md](NETWORK_FEATURES.md).

If you would rather use **Nginx** instead (e.g. for advanced configuration), the following applies.

If you want to run VAF in network mode with **HTTPS** (e.g. accessing it from other devices at `https://192.168.2.114`), you can use **Nginx** as a reverse proxy. Nginx handles TLS; VAF keeps running over HTTP (localhost).

**Result (with Nginx):**
- **On the same PC:** `http://localhost:3000` as usual (without Nginx).
- **Other devices on the network:** `https://<YOUR-IP>` (e.g. `https://192.168.2.114`) – Nginx terminates SSL and forwards to VAF.

---

## Prerequisites

- Nginx installed (Windows: [nginx for Windows](https://nginx.org/en/docs/windows.html), Linux/macOS: `apt install nginx` / `brew install nginx`).
- An SSL certificate and key (e.g. from the VAF settings or self-signed).

---

## VAF Settings

- **Local Network:** enabled. VAF then binds only to 127.0.0.1 – access **only** via Nginx (https://your-IP).
- **SSL/TLS in VAF:** **off** (Nginx handles TLS). Backend and frontend run over HTTP.

---

## Nginx Configuration

Create a file (e.g. `vaf-https.conf`) and include it in your Nginx installation.

**Adjust the paths:**
- `ssl_certificate` / `ssl_certificate_key`: path to your certificate and key (e.g. the ones used by VAF).
- Optional: `listen 8443` instead of `443` if 443 is already in use.

```nginx
# VAF behind Nginx with HTTPS
# Include it e.g. in nginx.conf: include /path/to/vaf-https.conf;

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name _;   # or your IP / hostname

    ssl_certificate     /pfad/zu/deinem/cert.pem;
    ssl_certificate_key /pfad/zu/deinem/key.pem;

    # Optional: local network only
    # allow 192.168.0.0/16;
    # allow 10.0.0.0/8;
    # allow 172.16.0.0/12;
    # deny all;

    # Frontend (Next.js)
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Backend-API
    location /api/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket (Backend)
    location /ws {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Starting / Reloading Nginx

- **Windows:** Nginx folder e.g. `C:\nginx`, adjust the configuration, then:
  ```text
  nginx.exe
  ```
  After changes: `nginx.exe -s reload`
- **Linux/macOS:**
  ```bash
  sudo nginx -t && sudo systemctl reload nginx
  ```

---

## Usage

| Where           | URL                          |
|-----------------|------------------------------|
| Same PC         | `http://localhost:3000`      |
| Other devices   | `https://192.168.2.114` (or your IP, port 443) |

The frontend detects access over port 443 and uses the same origin for the API and WebSocket (`/api/`, `/ws`), so Nginx forwards everything correctly.

---

## Troubleshooting

- **502 Bad Gateway:** VAF (Tray) must be running; frontend on 3000, backend on 8001.
- **WebSocket closes immediately:** Make sure the `location /ws` block with `Upgrade` and `Connection` is present.
- **Certificate warning:** For self-signed certificates, choose “Advanced” → “Proceed anyway” once in the browser.
