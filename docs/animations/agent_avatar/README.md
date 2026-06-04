# VAF Agent Animation

Eigenständige HTML-Dateien, die den **VAF Agent-Avatar** (den lebenden weißen Punkt)
zeigen — zum Anschauen, für Screenshots und für Post-Content.

> Design-Referenz für den Agent-Avatar. Liegt im Repo unter `docs/animations/agent_avatar/`,
> ist aber nicht Teil des gebauten Produkts (reine Referenz/Spec).
> Die echte React-Integration steht in `docs/AgentAvatar.md`.

| Datei | Inhalt |
|---|---|
| **`agent-all-animations.html`** | **Gesamtübersicht** — alle Animationen aus allen Dateien in einer, jeweils gelabelt, mit globalem Hell/Dunkel-Umschalter. Der beste Startpunkt. |
| **`agent-avatar-showcase.html`** | Die fünf Basis-Zustände aus der App (idle · waiting · thinking · talking · dim); konsistent mit den anderen (eigenes Körper-Element + Zwinkern im idle) |
| **`agent-character-emotions.html`** | Der **Punkt als Charakter** (Punkt-Animationen wie in der App, der Körper reagiert subtil mit + Zwinkern) — überrascht, neugierig, Idee, fröhlich, Erfolg … + Performance-Modus |
| **`agent-away-scenes.html`** | **„User-away"-Szenen** — der Agent vertreibt sich die Zeit (liest Zeitung, schaut Fernsehen, Kaffeepause, jongliert …) + Leerlauf-Modus. Für alte/archivierte Chats |
| **`agent-activity-states.html`** | **Funktionale Zustände** — zeigt, *was der Agent gerade tut*: 21 Zustände in 4 Clustern (Tool & Aktion · Status & Ergebnis · Lebenszyklus · Multi-Agent & Lernen), dunkel + hell + Durchlauf |

Beide nutzen **denselben Punkt** und dieselben Basis-Keyframes, 1:1 aus dem echten
VAF-Code portiert (`web/app/page.tsx` + `web/app/globals.css`).

---

## Starten / Ansehen

Beides sind ganz normale HTML-Dateien — **kein Server, keine Installation nötig**.
Einfach im Browser öffnen (Doppelklick im Dateimanager).

**Per Terminal (Linux):**
```bash
xdg-open "agent-all-animations.html"       # ALLES auf einen Blick (Startpunkt)
xdg-open "agent-activity-states.html"      # die funktionalen Zustände
xdg-open "agent-away-scenes.html"          # die "User-away"-Szenen
xdg-open "agent-character-emotions.html"   # die Charakter-Seite
xdg-open "agent-avatar-showcase.html"      # die Basis-Zustände
```

**macOS:** `open agent-all-animations.html`
**Windows:** `start agent-all-animations.html`

---

## Was die Seite zeigt

| Abschnitt | Inhalt |
|---|---|
| **Live — interaktiv** | Dropdown für jeden Modus, Größen-Regler, Umschalter dunkel/transparent |
| **Alle Zustände** | `idle` · `idle+dim` · `waiting` · `thinking` · `talking` live & groß |
| **Echte UI-Größe** | 36 px — exakt wie im Chat |
| **Größen-Raster** | 36 / 72 / 126 / 180 / 252 / 360 px |
| **Export** | Avatare auf transparentem Hintergrund (Schachbrett = transparent) |

### Die 5 Zustände

- **idle** — weißer Punkt + statische Aura, sanftes Schweben (Float 15 s)
- **idle + dim** — grau, komplett still (ältere Nachrichten / Archiv)
- **waiting** — langsames Morphen (5.5 s) + Atmen (4.0 s)
- **thinking** — fokussiertes Pulsieren (Morph 1.0 s + Atmen 0.7 s) + Glow
- **talking** — rhythmisches Sprechen (Talk 0.75 s)

Der Punkt ist **weiß** (`#ffffff`) auf dunklem, abgerundetem Container
(36 px, `bg-gray-900`, `rounded-xl`).

---

## Die Gesamtübersicht (`agent-all-animations.html`)

Eine einzige Datei, die **alles** zeigt, was wir haben — jeder Zustand einzeln gelabelt, in
vier Abschnitten: **Basis**, **Emotionen**, **Away-Szenen**, **Aktivität**. Sie vereint
beide Darstellungs-Ebenen derselben Identität (der **Punkt** für Basis/Emotionen, die
**Figur** aus Körper + Auge für Away/Aktivität) und hat oben einen globalen
**Hell/Dunkel-Umschalter**. Bester Startpunkt für einen schnellen Gesamteindruck.

Ganz oben sitzt der **Transition-Player** (Abschnitt „0 · Übergänge"): er geht eine
zufällige Liste **aller** Zustände durch und blendet **animiert** von jedem in den nächsten —
jeder Zustand kollabiert zu einem weichen Punkt und blüht in den nächsten auf (Cross-Dissolve
+ Scale + Blur, funktioniert über beide Modelle hinweg). Steuerung: Abspielen/Pause, neu
mischen, Dauer pro Zustand. So sieht man die **Übergänge** zwischen den Animationen.

> **In VAF einbauen:** Diese Übergänge sind im echten App-Code dokumentiert — siehe
> `docs/AgentAvatar.md`, Abschnitt *„Universal morph — any state to any state"*. Dort steht
> die konkrete React-Integration (collapse-to-neutral -> swap -> bloom) passend zu
> `web/components/AgentAvatar.tsx`. Der Transition-Player hier ist die optische Referenz dazu.

---

## Die Emotionen (`agent-character-emotions.html`)

Der lebende weiße **Punkt** ist der Star — seine Animationen sind **1:1 wie im App-Avatar**.
Neu: der **Körper** (das dunkle Viereck) reagiert jetzt *subtil* mit (eigene, dezente
Animation als eigenes Element) — der Punkt läuft unverändert daneben. Klassische
Animationsprinzipien (Squash & Stretch, Anticipation, Overshoot, Timing); jeder Loop endet
mit einer kurzen Ruhepause. Im **Ruhe-Zustand zwinkert** der Punkt gelegentlich.

| Gruppe | Zustand | Was es signalisiert |
|---|---|---|
| **Basis** | Ruhe | präsent, zwinkert ab und zu |
| | Wartet · Denkt nach · Spricht | wie im echten App-Avatar |
| **Reaktionen** | Überrascht | etwas Unerwartetes ist passiert |
| | Neugierig | schaut sich etwas genauer an (lehnt & lugt) |
| | Verwirrt | versteht etwas (noch) nicht |
| | Geistesblitz | hat eine Lösung gefunden (Aha-Aufleuchten) |
| **Gefühle** | Fröhlich · Aufgeregt | freut sich / voller Energie |
| | Niedergeschlagen · Müde | sackt ab / entspannt |
| **Antwort** | Zustimmung · Ablehnung | Nicken / Kopfschütteln |
| | Hört zu · Sucht / Scannt | nimmt auf / durchsucht (Bogen-Sweep) |
| **Höhepunkte** | Erfolg | Sieges-Sprung; die Energie-Ringe zünden bei der **Landung** (synchron mit dem Punkt) |
| | Arbeitet | verarbeitet im Hintergrund (Satellit kreist) |

Bühne mit Zustands-Auswahl, Größenregler, Hintergrund-Umschalter und einem
**Performance-Modus**, der eine kleine Szene durchspielt.

> Hinweis: Diese Datei nutzt das **Punkt-zentrierte** Modell (Punkt = Star, Körper reagiert
> subtil). Away & Aktivität nutzen das **Körper+Auge**-Modell (Körper trägt die Bewegung).
> Beide teilen dieselbe Identität — dunkles Viereck + weißer Punkt.

---

## Die Away-Szenen (`agent-away-scenes.html`)

Was macht der Agent, wenn gerade niemand mit ihm spricht? Diese kleinen Leerlauf-Szenen
sind gedacht für den Moment, in dem der User einen **alten oder archivierten Chat** öffnet:
der Agent „wartet" sichtbar auf dich, statt nur still zu sein.

Wichtig — der Agent besteht aus **zwei Teilen**: dem dunklen abgerundeten Viereck
(= sein **Körper**) und dem weißen Punkt (= sein **Auge / Gesicht**). Die Requisiten
(Zeitung, TV, Tasse …) liegen **außerhalb** seines Körpers; er hantiert damit — der
Körper lehnt und wippt, das Auge schaut, scannt und blinzelt.

| Szene | Was sie erzählt |
|---|---|
| Liest Zeitung | hält die Zeitung vor sich, Auge scannt die Zeilen, blättert um |
| Schaut Fernsehen | sitzt vor dem Fernseher, vom Flimmern beleuchtet, lacht ab und zu |
| Kaffeepause | lehnt sich zur Tasse, nimmt einen Schluck, seufzt „ahh" |
| Jongliert | drei Bälle über ihm im kreisenden Shower-Muster, Auge verfolgt sie |
| Summt vor sich hin | wippt im Takt, Noten steigen daneben auf |
| Spielt mit dem Ball | Ball hüpft vor ihm (Squash & Stretch), Auge folgt auf und ab |
| Schaut in die Sterne | lehnt sich zurück, blickt hoch, Sterne funkeln, Sternschnuppe |
| Nickerchen | Auge fast zu, atmet schwer, „z z z" steigen auf |

Auf der Bühne gibt es Szenen-Auswahl, Größenregler, einen **Hell/Dunkel-Umschalter** und
einen **Leerlauf-Modus**, der von selbst durch alle Beschäftigungen wechselt — genau wie es
der User im Away-Zustand sähe.

**Hell & dunkel:** Die Szenen funktionieren auf beidem. Der Körper bleibt das dunkle Viereck
und das Auge bleibt weiß; die externen Requisiten (Zeitung, Dampf, Noten, Bälle, Sterne,
„z z z") färben sich über Theme-Variablen (`--ink` etc.) von Weiß auf Tinte um, damit sie auf
hellem Hintergrund sichtbar bleiben. Es gibt zwei Galerien — eine auf dunklem, eine auf hellem
Hintergrund.

---

## Die funktionalen Zustände (`agent-activity-states.html`)

Die operative Ebene: **was der Agent gerade konkret tut** — damit der User in Echtzeit
versteht, woran er arbeitet, ohne Logs zu lesen. Gleiche Identität (Körper + Auge), die
Werkzeuge/Indikatoren liegen außerhalb und sind theme-fähig (dunkel + hell). 21 Zustände in
4 Clustern:

**1 — Tool & Aktion**

| Zustand | Was er tut |
|---|---|
| Sucht / durchsucht | Lupe wandert über ein Dokument, Auge folgt |
| Schreibt / editiert | tippt eine Zeile, Cursor blinkt |
| Führt Befehl aus | Terminal läuft, Spinner dreht, Körper unter Spannung |
| Surft im Web | Globus rotiert, Auge folgt |
| Lädt herunter | Datenpakete strömen in ihn hinein |
| Lädt hoch | Datenpakete strömen aus ihm heraus |

**2 — Status & Ergebnis**

| Zustand | Was er sagt |
|---|---|
| Erfolg | Aufgabe geschafft (Hüpfer + Häkchen + Ring) |
| Fehler | etwas ist schiefgelaufen (Ruck + „!") |
| Warnung | Vorsicht geboten (pulsierendes Alert-Symbol) |
| Braucht Erlaubnis | fragt nach, lehnt sich vor, wartet auf dein OK („?") |
| Blockiert | kommt nicht weiter (stößt gegen ein Schloss) |

**3 — Lebenszyklus & Verbindung**

| Zustand | Was er sagt |
|---|---|
| Erwacht | materialisiert sich, Auge öffnet sich |
| Verbindet | baut eine Verbindung zu einem Knoten auf |
| Offline | Verbindung verloren, Auge flackert aus |
| Verbindet erneut | Wiederholungs-Pulse, hofft auf Reconnect |
| Fährt herunter | schaltet sich ab (CRT-Kollaps) |

**4 — Multi-Agent & Lernen** (VAF-spezifisch)

| Zustand | Was er tut |
|---|---|
| Delegiert | knospt einen Sub-Agenten ab |
| Übergabe | reicht eine Aufgabe an einen zweiten Agenten weiter |
| Lernt / trainiert | nimmt Wissen auf — passt zu Whare Wananga |
| Erinnert sich | greift auf den Speicher zu (Knoten leuchten in Folge) |
| Plant | legt die Schritte aus, Auge scannt darüber |

Auf der Bühne: Zustands-Auswahl (gruppiert), Größenregler, Hell/Dunkel-Umschalter und ein
**Durchlauf**, der alle 21 automatisch abspielt.

---

## Screenshots / transparente Bilder erstellen

**Schnell:** Im Abschnitt *Export* die gewünschte Variante heranzoomen und mit dem
Screenshot-Tool ausschneiden:
- Linux: meist `Druck`-Taste oder ⇧ + `Druck` für Bereich
- macOS: ⇧ ⌘ 4
- Windows: Snipping-Tool (⊞ + ⇧ + S)

Das Schachbrettmuster markiert die transparenten Flächen.

**Echtes Alpha-PNG (transparenter Hintergrund, beliebige Größe):**
Dafür braucht es einen kleinen Render-Schritt mit Headless-Browser. Sag im Chat Bescheid —
dann lege ich hier ein `render.js` (Puppeteer) dazu, das jeden Zustand in jeder Größe
automatisch als `transparent.png` exportiert.

---

## Anpassen

Alle Farben, Größen und Animations-Timings stehen ganz oben in der `<style>`-Sektion
der HTML-Datei (CSS-Variablen + Keyframes). Sie sind **1:1 aus dem echten VAF-Code**
portiert (`web/app/page.tsx` + `web/app/globals.css`), also originalgetreu.
