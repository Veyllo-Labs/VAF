# Nginx Reverse Proxy für HTTPS (Netzwerkmodus)

**Note:** VAF includes an **integrated HTTPS proxy**. If you enable **Local Network** and **SSL/TLS** in settings and set certificate/key paths, VAF starts the proxy on port 443 (or `local_network_https_port`, e.g. 8443 on Windows). **Nginx is not required**—the integrated proxy is the single HTTPS entry point (localhost and LAN). It forwards `/api` and `/ws` to the internal backend channel and allows all HTTP methods (so login and API calls work). See [NETWORK_FEATURES.md](NETWORK_FEATURES.md).

Wenn du stattdessen **Nginx** verwenden willst (z.B. für erweiterte Konfiguration), gilt das Folgende.

Wenn du VAF im Netzwerkmodus mit **HTTPS** nutzen willst (z.B. von anderen Geräten unter `https://192.168.2.114`), kannst du **Nginx** als Reverse Proxy verwenden. Nginx übernimmt TLS; VAF läuft weiter mit HTTP (localhost).

**Ergebnis (mit Nginx):**
- **Am gleichen PC:** `http://localhost:3000` wie gewohnt (ohne Nginx).
- **Andere Geräte im Netz:** `https://<DEINE-IP>` (z.B. `https://192.168.2.114`) – Nginx terminiert SSL und leitet an VAF weiter.

---

## Voraussetzungen

- Nginx installiert (Windows: [nginx für Windows](https://nginx.org/en/docs/windows.html), Linux/macOS: `apt install nginx` / `brew install nginx`).
- SSL-Zertifikat und Schlüssel (z.B. aus den VAF-Einstellungen oder selbst signiert).

---

## VAF-Einstellung

- **Lokales Netzwerk:** aktiviert. Dann bindet VAF nur an 127.0.0.1 – Zugriff **nur** über Nginx (https://deine-IP).
- **SSL/TLS in VAF:** **aus** (Nginx macht TLS). Backend und Frontend laufen mit HTTP.

---

## Nginx-Konfiguration

Erstelle eine Datei (z.B. `vaf-https.conf`) und binde sie in deiner Nginx-Installation ein.

**Pfade anpassen:**
- `ssl_certificate` / `ssl_certificate_key`: Pfad zu deinem Zertifikat und Schlüssel (z.B. die aus VAF verwendeten).
- Optional: `listen 8443` statt `443`, wenn 443 schon belegt ist.

```nginx
# VAF hinter Nginx mit HTTPS
# Einbindung z.B. in nginx.conf: include /path/to/vaf-https.conf;

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name _;   # oder deine IP / Hostname

    ssl_certificate     /pfad/zu/deinem/cert.pem;
    ssl_certificate_key /pfad/zu/deinem/key.pem;

    # Optional: nur lokales Netzwerk
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

## Nginx starten / neu laden

- **Windows:** Nginx-Ordner z.B. `C:\nginx`, Konfiguration anpassen, dann:
  ```text
  nginx.exe
  ```
  Nach Änderungen: `nginx.exe -s reload`
- **Linux/macOS:**
  ```bash
  sudo nginx -t && sudo systemctl reload nginx
  ```

---

## Nutzung

| Wo              | URL                          |
|-----------------|------------------------------|
| Gleicher PC     | `http://localhost:3000`      |
| Andere Geräte   | `https://192.168.2.114` (oder deine IP, Port 443) |

Das Frontend erkennt den Zugriff über Port 443 und verwendet für API und WebSocket dieselbe Origin (`/api/`, `/ws`), sodass Nginx alles korrekt weiterleitet.

---

## Fehlerbehebung

- **502 Bad Gateway:** VAF (Tray) muss laufen; Frontend auf 3000, Backend auf 8001.
- **WebSocket schließt sofort:** Stelle sicher, dass der Block `location /ws` mit `Upgrade` und `Connection` vorhanden ist.
- **Zertifikatswarnung:** Bei selbst signierten Zertifikaten im Browser einmal „Erweitert“ → „Trotzdem fortfahren“ wählen.
