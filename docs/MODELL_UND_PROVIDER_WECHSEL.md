# Wechsel zwischen lokalem Modell und API

Beim Wechsel des Providers in den Einstellungen (von „Local“ zu einer API wie OpenAI oder umgekehrt) sorgt VAF dafür, dass die Oberfläche ein kurzes Hinweis-Overlay anzeigt und der Speicher des lokalen Modells korrekt behandelt wird.

## Ablauf in der Web-UI

1. Du änderst in den Einstellungen den **AI Provider** (z. B. von „Local“ auf „OpenAI“ oder zurück) und speicherst.
2. Es erscheint ein Overlay in der Mitte des Bildschirms mit dem Text **„Changing model“** und einem kurzen Hinweis.
3. Das Overlay bleibt etwa 5 Sekunden sichtbar, danach lädt die Seite neu.
4. Nach dem Neuladen gilt der neue Provider; bei Local ist das lokale Modell geladen oder wird bei der nächsten Anfrage geladen.

Das Overlay entspricht dem Verhalten beim Umschalten der Netzwerk-Einstellungen (Local Network ein/aus): gleiche Darstellung, keine Schaltfläche zum Schließen, automatischer Reload nach einigen Sekunden.

## Verhalten im Backend (Speicher / VRAM)

- **Wechsel von Local zu API:**  
  Das lokale Modell wird sofort aus dem Speicher (VRAM) entladen. Der llama-Server wird beendet, damit keine unnötige Grafikspeicher-Belegung entsteht.

- **Wechsel von API zu Local:**  
  Das lokale Modell wird nach dem Speichern der Einstellung geladen. Der Tray startet den Server und das Modell liegt im VRAM bereit, so dass die erste Anfrage ohne zusätzliche Wartezeit ausgeführt werden kann.

Die Steuerung erfolgt über den Config-Observer: Änderungen am Schlüssel `provider` werden an den Tray gemeldet; der Tray führt Entladen bzw. Laden aus und die Activity-Loop arbeitet wie beim normalen Idle-Timeout bzw. bei Aktivität.

## Technische Stichpunkte

- **Config:** Der Schlüssel `provider` zählt zu den „critical keys“. Beim Speichern der Config werden die Observer mit altem und neuem Wert aufgerufen.
- **Tray:** `on_config_changed` erkennt Provider-Wechsel und ruft entweder `server_mgr.stop_server()` + `set_model_loaded(False)` auf (Local → API) oder setzt `request_model_load = True` (API → Local). Die Activity-Loop startet dann den Server.
- **WebSocket:** Beim Speichern der Config kann die Antwort `config_saved` das Feld `requires_refresh: true` enthalten (z. B. bei Provider-Änderung). Die Web-UI zeigt in diesem Fall dasselbe Overlay und lädt nach 5 Sekunden die Seite neu.

## Verwandte Dokumentation

- **WEB_UI.md** – Übersicht Web-UI und Status-Anzeigen
- **WEBUI_WEBSOCKET_FLOW.md** – Nachrichtentypen (u. a. `config_saved`, `model_state`)
- **SYSTEM_TRAY.md** – Tray, Idle-Timeout und Persistent-Modus
