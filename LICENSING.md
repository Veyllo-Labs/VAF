# Licensing / Lizenzierung

**VAF (Veyllo Agentic Framework)** is dual-licensed by **Veyllo GmbH**.

| | Open Source | Commercial |
|---|---|---|
| **License** | GNU AGPL-3.0-or-later (see [LICENSE](LICENSE)) | Proprietary, granted by Veyllo GmbH |
| **Price** | Free | Paid — see [COMMERCIAL.md](COMMERCIAL.md) |
| **Use it for** | Anything, as long as you honor AGPL copyleft | Closed-source products and hosted SaaS without copyleft |
| **Source disclosure** | Required for modified versions you convey **or run as a network service** | Not required |

Pick **one** of the two. If the AGPL works for you, you owe nothing. If it does not fit
your product, a commercial license removes the copyleft obligations. Contact
**legal@veyllo.io** · **https://veyllo.io**.

---

## English

### 1. Open Source — GNU AGPL-3.0-or-later

The default and always-available license for VAF is the
[GNU Affero General Public License, version 3 or later](https://www.gnu.org/licenses/agpl-3.0.html).
The full text is in [LICENSE](LICENSE). In plain language:

- You may **use, study, modify, and distribute** VAF, free of charge.
- If you **distribute** VAF (modified or not), you must pass it on under the AGPL and
  make the corresponding **source code** available to your recipients.
- The AGPL's defining clause (Section 13): if you **run a modified VAF as a network
  service** (SaaS) that users interact with over a network, you must offer those users
  the **complete source code** of your modified version under the AGPL.

> **The AGPL is widely misunderstood — so to be precise about what it does *not* do:**
> The AGPL is **not** "viral" toward your private work. Running unmodified VAF — even
> commercially, even on your own servers — triggers **no** disclosure obligation.
> The source-sharing duty applies only when you (a) **distribute** VAF or (b) **convey
> it over a network in a modified form**. Internal use, evaluation, and building Plugins,
> Tools, and Workflows on top of VAF are explicitly fine (see the Additional Permission
> below).

The AGPL is an [OSI-approved](https://opensource.org/license/agpl-v3) open source
license and is published by the Free Software Foundation.

### 2. Commercial License (Proprietary)

If the AGPL's copyleft requirements do not fit your business — for example, you want to
embed VAF in a **closed-source product** or run a **proprietary SaaS** without disclosing
your source — Veyllo GmbH offers a commercial license that:

- permits integration of VAF into proprietary products **without source-code disclosure**;
- permits offering VAF-based **SaaS without AGPL Section 13 obligations**;
- includes support, maintenance, and legal-certainty options;
- can include warranty and indemnification terms not available under the AGPL.

Details, scope, and how to obtain one: **[COMMERCIAL.md](COMMERCIAL.md)**.

### Why dual licensing?

This is the same model used successfully by MySQL, MongoDB, GitLab, Grafana, and many
other open source companies. It keeps VAF **genuinely free and open** for the community
while letting Veyllo GmbH fund continued development through commercial licenses for
companies that need proprietary terms.

### Additional Permission under Section 7 of the AGPL — Plugins, Tools & Workflows

As the copyright holder — and, through the contributor terms in [CONTRIBUTING.md](CONTRIBUTING.md),
on behalf of the VAF copyright holders — **Veyllo GmbH grants the following additional
permission under Section 7 of the GNU AGPL-3.0**:

> You may develop, use, distribute, and license **Plugins, Tools, and Workflows** for VAF
> (for example, a `BaseTool` subclass in `vaf/tools/`, or a `WORKFLOW` definition under
> `~/.vaf/workflows/`) **under license terms of your own choosing**, including proprietary
> terms, **provided that** such Plugins, Tools, and Workflows interact with VAF only
> through its documented public interfaces and **do not copy, embed, or redistribute
> VAF's own source code**. Merely loading VAF as a library or invoking its public APIs from
> your Plugin does not, by itself, make your Plugin a derivative work subject to the AGPL.

This is analogous to a GPL "linking exception": it lets the ecosystem build on VAF without
their add-on code being forced open, while VAF's own code remains under the AGPL.

### Additional Term under Section 7(a) — Liability (Germany)

As permitted by Section 7(a) of the AGPL, the following liability terms apply to users
subject to the laws of Germany and **supplement** Sections 15 and 16 of the AGPL:

> **Haftung.** Die Software wird unentgeltlich zur Verfügung gestellt. Die Haftung des
> Lizenzgebers ist — gleich aus welchem Rechtsgrund — auf Vorsatz und grobe Fahrlässigkeit
> beschränkt. Bei einfacher Fahrlässigkeit haftet der Lizenzgeber nur bei Verletzung
> wesentlicher Vertragspflichten (Kardinalpflichten) und in diesem Fall begrenzt auf den
> typischerweise vorhersehbaren Schaden.
>
> **Zwingende Haftung.** Die Haftungsbeschränkungen gelten nicht für: a) Schäden aus der
> Verletzung des Lebens, des Körpers oder der Gesundheit; b) Ansprüche nach dem
> Produkthaftungsgesetz; c) arglistiges Verschweigen eines Mangels. Im Übrigen ist die
> Haftung ausgeschlossen, soweit gesetzlich zulässig.

### Trademarks and Brand Assets

The code license does **not** grant trademark rights. "VAF", "Veyllo Agentic Framework",
"Veyllo", the VAF logo, and the agent avatar — the "living dot" visual identity and its
animated states (see `docs/web-ui/AgentAvatar.md` and `docs/animations/agent_avatar/`) —
are trademarks and brand assets of Veyllo GmbH. The AGPL and the commercial license cover
source code and documentation only; neither grants permission to use the Veyllo names, the
VAF logo, or the agent-avatar brand assets to identify your own products or in any way that
suggests endorsement or affiliation, beyond the reasonable and customary use needed to
describe the origin of the Software. All rights in these marks and brand assets are
reserved by Veyllo GmbH.

### Third-Party Components

The licenses above apply **only** to original code authored by Veyllo GmbH (VAF). All
third-party components (libraries, frameworks, runtimes, model loaders, dependencies)
remain under their respective original licenses (MIT, Apache-2.0, BSD, LGPL, etc.), which
are unmodified and unrestricted by VAF's licensing. All original copyright notices and
license texts of third-party components must be retained as required by those licenses.
The in-app **About → Licenses** view and `web/lib/licenses_data.ts` list the bundled
third-party components.

---

## Deutsch

### 1. Open Source — GNU AGPL-3.0-or-later

Die Standard- und stets verfügbare Lizenz für VAF ist die
[GNU Affero General Public License, Version 3 oder später](https://www.gnu.org/licenses/agpl-3.0.de.html).
Der vollständige Text steht in [LICENSE](LICENSE). In einfachen Worten:

- Du darfst VAF **nutzen, studieren, verändern und weitergeben** — kostenlos.
- Wenn du VAF (verändert oder nicht) **weitergibst**, musst du es unter der AGPL
  weitergeben und den **Quellcode** für deine Empfänger verfügbar machen.
- Die zentrale Klausel der AGPL (Abschnitt 13): Wenn du ein **verändertes VAF als
  Netzwerkdienst** (SaaS) betreibst, mit dem Nutzer über ein Netzwerk interagieren, musst
  du diesen Nutzern den **vollständigen Quellcode** deiner veränderten Version unter der
  AGPL anbieten.

> **Die AGPL wird häufig missverstanden — deshalb ausdrücklich, was sie *nicht* tut:**
> Die AGPL ist **nicht** „viral" gegenüber deiner privaten Arbeit. Der bloße Betrieb von
> **unverändertem** VAF — auch kommerziell, auch auf eigenen Servern — löst **keine**
> Offenlegungspflicht aus. Die Pflicht zur Quellcode-Weitergabe greift nur, wenn du VAF
> (a) **weitergibst** oder (b) **in veränderter Form über ein Netzwerk bereitstellst**.
> Interne Nutzung, Evaluierung sowie das Erstellen von Plugins, Tools und Workflows auf
> Basis von VAF sind ausdrücklich erlaubt (siehe Zusatzgenehmigung unten).

Die AGPL ist eine von der [OSI anerkannte](https://opensource.org/license/agpl-v3)
Open-Source-Lizenz und wird von der Free Software Foundation herausgegeben.

### 2. Kommerzielle Lizenz

Wenn die Copyleft-Pflichten der AGPL nicht zu deinem Geschäftsmodell passen — etwa weil du
VAF in ein **proprietäres Produkt** einbetten oder eine **proprietäre SaaS** ohne
Quellcode-Offenlegung betreiben willst — bietet die Veyllo GmbH eine kommerzielle Lizenz,
die:

- die Integration von VAF in proprietäre Produkte **ohne Quellcode-Offenlegung** erlaubt;
- den Betrieb von VAF-basierter **SaaS ohne AGPL-Abschnitt-13-Pflichten** ermöglicht;
- Support-, Wartungs- und Rechtssicherheits-Optionen einschließt;
- Gewährleistungs- und Freistellungsregelungen enthalten kann, die unter der AGPL nicht
  verfügbar sind.

Details, Umfang und Bezug: **[COMMERCIAL.md](COMMERCIAL.md)**.

### Warum Dual Licensing?

Dieses Modell wird erfolgreich von MySQL, MongoDB, GitLab, Grafana und vielen weiteren
Open-Source-Unternehmen eingesetzt. Es hält VAF für die Community **wirklich frei und
offen** und finanziert zugleich die Weiterentwicklung über kommerzielle Lizenzen für
Unternehmen, die proprietäre Bedingungen benötigen.

### Zusatzgenehmigung nach Abschnitt 7 der AGPL — Plugins, Tools & Workflows

Als Rechteinhaberin — und, über die Beitragsbedingungen in [CONTRIBUTING.md](CONTRIBUTING.md),
im Namen der VAF-Rechteinhaber — **gewährt die Veyllo GmbH folgende Zusatzgenehmigung nach
Abschnitt 7 der GNU AGPL-3.0**:

> Du darfst **Plugins, Tools und Workflows** für VAF (z. B. eine `BaseTool`-Unterklasse in
> `vaf/tools/` oder eine `WORKFLOW`-Definition unter `~/.vaf/workflows/`) **unter
> Lizenzbedingungen deiner Wahl** entwickeln, nutzen, weitergeben und lizenzieren —
> einschließlich proprietärer Bedingungen — **sofern** diese Plugins, Tools und Workflows
> ausschließlich über die dokumentierten öffentlichen Schnittstellen von VAF mit VAF
> interagieren und **keinen Quellcode von VAF selbst kopieren, einbetten oder
> weiterverteilen**. Das bloße Laden von VAF als Bibliothek oder das Aufrufen seiner
> öffentlichen APIs aus deinem Plugin macht dein Plugin für sich genommen nicht zu einem
> abgeleiteten Werk im Sinne der AGPL.

Dies entspricht einer GPL-„Linking-Exception": Das Ökosystem kann auf VAF aufbauen, ohne
dass der Erweiterungs-Code zwangsweise offengelegt werden muss, während VAFs eigener Code
unter der AGPL bleibt.

### Zusätzliche Bedingung nach Abschnitt 7(a) — Haftung (Deutschland)

Wie nach Abschnitt 7(a) der AGPL zulässig, gelten für Nutzer, die dem Recht der
Bundesrepublik Deutschland unterliegen, die folgenden Haftungsregelungen **ergänzend** zu
den Abschnitten 15 und 16 der AGPL:

> **Haftung.** Die Software wird unentgeltlich zur Verfügung gestellt. Die Haftung des
> Lizenzgebers ist — gleich aus welchem Rechtsgrund — auf Vorsatz und grobe Fahrlässigkeit
> beschränkt. Bei einfacher Fahrlässigkeit haftet der Lizenzgeber nur bei Verletzung
> wesentlicher Vertragspflichten (Kardinalpflichten) und in diesem Fall begrenzt auf den
> typischerweise vorhersehbaren Schaden.
>
> **Zwingende Haftung.** Die Haftungsbeschränkungen gelten nicht für: a) Schäden aus der
> Verletzung des Lebens, des Körpers oder der Gesundheit; b) Ansprüche nach dem
> Produkthaftungsgesetz; c) arglistiges Verschweigen eines Mangels. Im Übrigen ist die
> Haftung ausgeschlossen, soweit gesetzlich zulässig.

Dieser Wortlaut ist die maßgebliche Fassung; die englische Fassung oben gibt denselben
Inhalt wieder.

### Marken und Markenwerte

Die Code-Lizenz gewährt **keine** Markenrechte. „VAF", „Veyllo Agentic Framework",
„Veyllo", das VAF-Logo und der Agent-Avatar (die „Living-Dot"-Identität und ihre animierten
Zustände) sind Marken und Markenwerte der Veyllo GmbH. Alle Rechte daran bleiben
vorbehalten.

### Drittanbieter-Komponenten

Die obigen Lizenzen gelten **ausschließlich** für den von der Veyllo GmbH erstellten
Original-Code (VAF). Alle Drittanbieter-Komponenten bleiben unter ihren jeweiligen
Original-Lizenzen.

---

## Contact / Kontakt

**Veyllo GmbH** — for commercial licensing inquiries / für kommerzielle Lizenzanfragen:

- Email: **legal@veyllo.io**
- Web: **https://veyllo.io**

Copyright (c) 2026 Veyllo GmbH. VAF is a trademark of Veyllo GmbH.
