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
  Der Config-Save triggert einen `RELOAD_CONFIG`-Pfad im Headless Runner. Dabei wird der Agent auf API-Betrieb umgestellt und die lokale Agent-LLM-Instanz zurückgesetzt. Der Server-Prozess wird nicht über einen dedizierten `provider`-Observer in `tray.py` gestoppt, sondern folgt dem laufenden Runtime-/Idle-Management.

- **Wechsel von API zu Local:**  
  Nach `RELOAD_CONFIG` wird wieder der lokale Laufweg aktiv. Das lokale Modell/der Server wird dann über die normale Aktivitäts- und Laderoutine bereitgestellt.

Die Umschaltung läuft über Config-Save + Queue-Kommando (`__CMD__:RELOAD_CONFIG`) und nicht über einen expliziten `provider`-Branch in `tray.py`.

## Technische Stichpunkte

- **Config/WebSocket:** Beim Speichern markiert die API den Providerwechsel (`requires_refresh: true`) und queued `__CMD__:RELOAD_CONFIG`.
- **Headless runner:** `RELOAD_CONFIG` aktualisiert den Agenten-Kontext (Provider/Backend, LLM-Reset, `use_server`-Pfad).
- **Tray:** `on_config_changed` behandelt model-/context-/gpu- und network-bezogene Schlüssel; kein eigener `provider`-Sonderzweig.
- **WebSocket:** Beim Speichern der Config kann die Antwort `config_saved` das Feld `requires_refresh: true` enthalten (z. B. bei Provider-Änderung). Die Web-UI zeigt in diesem Fall dasselbe Overlay und lädt nach 5 Sekunden die Seite neu.

## Verwandte Dokumentation

- **WEB_UI.md** – Übersicht Web-UI und Status-Anzeigen
- **WEBUI_WEBSOCKET_FLOW.md** – Nachrichtentypen (u. a. `config_saved`, `model_state`)
- **SYSTEM_TRAY.md** – Tray, Idle-Timeout und Persistent-Modus
