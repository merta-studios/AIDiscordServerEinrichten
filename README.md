# AIDiscordServerEinrichten

> **Ein Discord-Bot, der Arena AI die Schlüssel zu deinem Server gibt.**
> Du führst `/connect` aus, kopierst Link + Token in Arena AI — und die KI richtet
> deinen kompletten Discord-Server ein: Rollen, Kanäle, Kategorien, Berechtigungen,
> AutoMod, Welcome-Screen, Onboarding, Nachrichten. Alles per natürlicher Sprache.

[![CI](https://github.com/mertastudios/AIDiscordServerEinrichten/actions/workflows/ci.yml/badge.svg)](https://github.com/mertastudios/AIDiscordServerEinrichten/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?logo=discord&logoColor=white)](https://discordpy.readthedocs.io/)
[![Render](https://img.shields.io/badge/Deploy-auf%20Render-46E3B7?logo=render&logoColor=black)](#3-auf-render-deployen)

---

## Inhaltsverzeichnis

1. [Wie es funktioniert](#wie-es-funktioniert)
2. [Schnellstart (≈ 15 Minuten)](#schnellstart)
3. [Betrieb auf einem eigenen Server (VPS / Fly.io / Proxy)](#betrieb-auf-einem-eigenen-server)
4. [Der `/connect`-Command](#der-connect-command)
5. [Der `/manage_database`-Command & Turso](#der-manage_database-command)
6. [Owner-Features: Presence, Adminpanel & Benachrichtigungen](#owner-features)
7. [Sicherheitsmodell](#sicherheitsmodell)
8. [Die REST-API](#die-rest-api)
9. [Die Console](#die-console)
10. [UptimeRobot — Bot dauerhaft online halten](#uptimerobot)
11. [Konfiguration](#konfiguration)
12. [Lokal entwickeln](#lokal-entwickeln)
13. [Tests](#tests)
14. [Fehlerbehebung](#fehlerbehebung)
15. [Projektstruktur](#projektstruktur)

---

## Wie es funktioniert

Das Problem: Arena AI kann nicht direkt mit Discord sprechen — und dein Bot-Token
solltest du niemals an einen Dritten weitergeben.

Die Lösung: **ein Relay**. Der Bot läuft auf Render, ist mit Discord verbunden und
öffnet zusätzlich eine REST-API. `/connect` erzeugt ein kurzlebiges, auf deinen
Server beschränktes Sitzungs-Token. Genau dieses Token bekommt Arena AI — nicht
dein Bot-Token.

```
┌──────────────┐   /connect    ┌───────────────────────────────────────┐
│    Discord   │ ────────────▶ │  AIDiscordServerEinrichten (Render)   │
│    Server    │               │                                       │
│              │ ◀──────────── │  ┌─────────────┐   ┌───────────────┐  │
└──────────────┘  ephemeral:   │  │ discord.py  │   │  aiohttp API  │  │
                  Link+Token   │  │   Client     │◀──│  121 Endpoints│  │
                               │  └──────┬──────┘   └───────▲───────┘  │
                               │         │                  │          │
                               │    gleicher Prozess, gleicher Loop    │
                               └─────────┼──────────────────┼──────────┘
                                         │                  │
                            Bot-Token    │                  │  Sitzungs-Token
                          (bleibt hier!) │                  │  (adse_…)
                                         ▼                  │
                                  ┌────────────┐    ┌───────┴────────┐
                                  │  Discord   │    │    Arena AI    │
                                  │  Gateway   │    │  (curl / REST) │
                                  └────────────┘    └────────────────┘
```

**Entscheidend:** Der Bot und die API laufen in *einem* Prozess auf *einem*
Event-Loop. Kein zweiter Dienst, keine Datenbank, keine Webhooks — ein Container.

---

## Schnellstart

### 1. Discord-Application anlegen

1. <https://discord.com/developers/applications> → **New Application**
2. Name z. B. `AIDiscordServerEinrichten` → **Create**
3. Links **Bot** → **Reset Token** → Token kopieren (wird gleich gebraucht)
4. Auf derselben Seite **Privileged Gateway Intents**:
   - ✅ `SERVER MEMBERS INTENT`
   - ✅ `MESSAGE CONTENT INTENT`

   > Vergessen? Kein Abbruchgrund. Der Bot startet automatisch ohne sie weiter
   > und bleibt online — nur die Mitgliederliste ist dann eventuell lückenhaft.
   > Im Render-Log steht in dem Fall eine klare Anleitung.

5. Links **OAuth2** → **OAuth2 URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: **Administrator**
   - Die erzeugte URL öffnet den Bot auf deinem Server

   > Der Bot **muss** Administrator sein. `/connect` prüft das und verweigert
   > sich sonst mit einer Schritt-für-Schritt-Anleitung.

### 2. Auf deinem Server

1. Bot über die URL aus Schritt 1.5 einladen
2. **Servereinstellungen → Rollen**: die Bot-Rolle auf *Administrator* und
   **ganz nach oben** schieben

   > Ganz nach oben ist wichtig: Discord lässt einen Bot keine Rolle verwalten,
   > die auf gleicher oder höherer Position liegt. Sonst kann die KI keine
   > Admin-Rolle anlegen.

### 3. Auf Render deployen

**Variante A — Blueprint (empfohlen)**

1. Dieses Repo auf GitHub pushen (bzw. forken)
2. Render Dashboard → **New +** → **Blueprint**
3. Repo auswählen — Render findet [`render.yaml`](render.yaml) automatisch
4. Im Wizard `DISCORD_BOT_TOKEN` eintragen → **Apply**

**Variante B — manuell**

1. **New +** → **Web Service** → Repo verbinden
2. Runtime: **Docker** (das [`Dockerfile`](Dockerfile) wird automatisch erkannt)
3. Environment: `DISCORD_BOT_TOKEN` = dein Token
4. **Health Check Path**: `/api/health`
5. Plan: **Free** reicht
6. **Create Web Service**

**Wichtig:** `numInstances: 1` (in `render.yaml` bereits gesetzt). Die
Sitzungs-Tokens leben im Speicher der Instanz — bei mehreren Instanzen würde ein
Token zufällig „unbekannt" sein.

Nach dem Deploy steht im Render-Log:

```
Öffentliche Basis-URL: https://aidiscordservereinrichten.onrender.com
  · Healthcheck : https://…/api/health   ← hierhin UptimeRobot zeigen
  · Console     : https://…/console
```

> **⚠ Render Free und Discord — bitte vorher lesen.** Auf Render Free teilt sich
> der Bot die **ausgehende IP** mit allen anderen Free-Diensten der Region
> (Default: Oregon). Sperrt Cloudflare diesen Pool für `discord.com` (HTTP 429,
> *Error 1015*), kommt **kein** Bot aus dem Pool mehr rein — der Code ist dann
> völlig unschuldig. Gemessener Fall (2026-09-06): IP `74.220.48.143`,
> Cloudflare-Ray `…-PDX`, `Retry-After 8034 s`. Bots, die schon eingeloggt
> *waren*, laufen dabei weiter (bestehende Gateway-Session, `RESUME`); nur
> **neue Logins** scheitern. Deshalb kann ein Bot „seit Monaten laufen", während
> ein neuer nie online kommt.
>
> Der Bot erkennt das selbst (`GET /api/diagnostics` → `"verdict": "ip_blocked"`),
> probt tokenlos weiter, startet sich bis zu `RESTART_MAX_CYCLES`-mal neu
> und sagt dann klar, dass die Plattform nicht durchkommt. **Zuverlässig** läuft
> er nur mit eigener Ausgangs-IP → [Betrieb auf einem eigenen Server](#betrieb-auf-einem-eigenen-server).
> Kostenloser Versuch davor: Service **neu in Frankfurt** anlegen (EU-Pool ist
> weniger bot-lastig) — siehe [Fehlerbehebung](#fehlerbehebung).

### 4. Loslegen

Auf Discord `/connect` ausführen → auf **Verbinden** klicken → den kleinen
Prompt (URL + Token) kopieren → bei Arena AI einfügen und sagen, was die KI
bauen soll. Fertig.

---

## Betrieb auf einem eigenen Server

**Wann:** `GET /api/diagnostics` zeigt `"verdict": "ip_blocked"` und der Bot
kommt auf Render nicht online (oder `platform_verdict` ist gesetzt). Dann ist
die **ausgehende IP** das Problem — und die einzige zuverlässige Lösung ist eine
eigene. Alles in diesem Abschnitt nutzt das **unveränderte** `Dockerfile` und
dieselben Umgebungsvariablen wie auf Render (`PORT`, `HOST`, `PUBLIC_URL`,
`DATA_DIR`).

| | Option 1 · **VPS** (empfohlen) | Option 2 · **Fly.io** | Option 3 · **Render + Proxy** |
| --- | --- | --- | --- |
| Kosten | ≈ 4 €/Monat (Hetzner CX22/CAX11) | ≈ 3–4 $/Monat | VPS ≈ 4 €/Monat *oder* QuotaGuard ab 19 $/Monat |
| Eigene IP | ✅ dedizierte IPv4 | ✅ pro Maschine | ✅ Proxy-IP |
| Spin-down / UptimeRobot | ❌ nicht nötig | ❌ nicht nötig (`auto_stop = off`) | ⚠ weiterhin nötig |
| Sessions überleben Neustart | ✅ Volume | ✅ Volume | ❌ (Render Free) |
| Aufwand | 15 min, Server-Grundkenntnisse | 10 min, kein Server | 10 min + Render-Env |
| Dateien | [`deploy/docker-compose.yml`](deploy/docker-compose.yml), [`deploy/relay.service`](deploy/relay.service) | [`deploy/fly.toml`](deploy/fly.toml) | [`deploy/proxy/`](deploy/proxy/) |

**Nicht** sinnvoll: Renders „Dedicated IPs" (Pro-Workspace, ~100 $/Monat), ein
bezahlter Render-Plan allein (bleibt ein *geteilter* CIDR-Bereich), Residential-/
Rotating-Proxies (ToS-Grauzone) und jede Form von Cloudflare-Umgehung
(Challenge-Solver, gefälschte Header) — das endet mit einem Account-Bann.

### Option 1 — Kleiner VPS mit Docker (≈ 15 Minuten)

**Du brauchst:** einen VPS mit Debian 12 / Ubuntu 22.04+ (Hetzner CX22 ≈ 4 €/Monat,
Standort Falkenstein/Nürnberg), Root-Zugang per SSH, optional eine (Sub-)Domain.

```bash
# 1) Auf dem Server: Docker installieren (einmalig, ~1 Minute)
curl -fsSL https://get.docker.com | sh

# 2) Repo holen
git clone https://github.com/mertastudios/AIDiscordServerEinrichten.git
cd AIDiscordServerEinrichten/deploy

# 3) Konfiguration: Token + öffentliche URL eintragen
cp relay.env.example relay.env
nano relay.env          # DISCORD_BOT_TOKEN=…   PUBLIC_URL=…   RELAY_DOMAIN=…

# 4) Starten (baut das Image aus dem vorhandenen Dockerfile)
docker compose up -d --build

# 5) Zuschauen, bis „ist ONLINE" erscheint (Strg+C beendet nur die Anzeige)
docker compose logs -f relay
```

**Domain oder keine Domain?**

- **Mit Domain (empfohlen):** DNS-A-Record `relay.deine-domain.tld → Server-IPv4`
  anlegen, **bevor** du startest. `RELAY_DOMAIN=relay.deine-domain.tld` und
  `PUBLIC_URL=https://relay.deine-domain.tld` in `relay.env`. Der mitgelieferte
  Caddy holt das Let's-Encrypt-Zertifikat automatisch; Ports 80/443 müssen offen
  sein (`ufw allow 80,443/tcp`).
- **Ohne Domain:** In `docker-compose.yml` den `caddy`-Dienst löschen, beim
  `relay`-Dienst `"127.0.0.1:8080:8080"` durch `"8080:8080"` ersetzen,
  `PUBLIC_URL=http://<Server-IPv4>:8080` setzen, `ufw allow 8080/tcp`. Funktioniert,
  aber ohne TLS — der Session-Token geht dann unverschlüsselt durchs Netz.

**Prüfen (vom Laptop aus):**

```bash
curl -s https://relay.deine-domain.tld/api/diagnostics | python3 -m json.tool | head -20
#  → "verdict": "ok",  "discord_reachable": true,  "egress_ip": "<deine VPS-IP>"
curl -s https://relay.deine-domain.tld/api/health | grep -o '"bot":"[a-z]*"'
#  → "bot":"connected"
```

Dann auf Discord `/connect` — der Prompt enthält jetzt deine VPS-URL.

**Betrieb:** Sessions und das Neustart-Buch liegen im Docker-Volume `relay-data`
und überleben `docker compose restart`, Server-Reboots und Updates.
`restart: unless-stopped` startet den Container nach jedem Ende neu.
Update: `git pull && docker compose up -d --build`. Logs: `docker compose logs -f relay`.
Danach den Render-Service **löschen oder pausieren** — zwei Instanzen mit demselben
Token stören sich gegenseitig.

**Ohne Docker (systemd):** [`deploy/relay.service`](deploy/relay.service) enthält
im Kopf die komplette Einrichtung (venv unter `/opt/adse-relay`, Daten in
`/var/lib/adse-relay`, Härtung, `Restart=always`).

### Option 2 — Fly.io (kein Server zu verwalten)

```bash
curl -L https://fly.io/install.sh | sh                      # flyctl installieren
fly auth signup                                              # oder: fly auth login
cd AIDiscordServerEinrichten && cp deploy/fly.toml ./fly.toml
fly launch --no-deploy --copy-config --name adse-relay --region fra   # Namen frei wählen
fly volumes create relay_data --region fra --size 1
fly secrets set DISCORD_BOT_TOKEN=DEIN_TOKEN PUBLIC_URL=https://adse-relay.fly.dev
fly deploy
fly logs                                                     # „ist ONLINE" abwarten
```

`fly.toml` setzt `auto_stop_machines = "off"` (kein Spin-down), Region `fra`,
ein 1-GB-Volume für `DATA_DIR` und den Healthcheck auf `/api/health`.
Prüfen: `curl -s https://adse-relay.fly.dev/api/diagnostics`.

### Option 3 — Render behalten, Discord über eigenen Proxy

Nur wenn Render aus anderen Gründen bleiben soll. Der Bot läuft weiter auf
Render, aber **alle** Discord-Verbindungen (REST + Gateway) gehen über einen
authentifizierten Squid-Proxy auf deinem VPS. discord.py 2.7 reicht `DISCORD_PROXY`
an beides weiter; aiohttp kann dabei nur `http://`/`https://`-Proxys (kein SOCKS5).

```bash
# Auf dem VPS
curl -fsSL https://get.docker.com | sh
git clone https://github.com/mertastudios/AIDiscordServerEinrichten.git
cd AIDiscordServerEinrichten/deploy/proxy
export PROXY_USER=relay PROXY_PASS="$(openssl rand -hex 24)"; echo "$PROXY_PASS"   # merken!
docker compose up -d && ufw allow 3128/tcp

# Vom Laptop testen — muss {"url":"wss://gateway.discord.gg"} liefern:
curl -x "http://relay:$PROXY_PASS@<VPS-IP>:3128" https://discord.com/api/v10/gateway
```

Dann in Render → *Environment*: `DISCORD_PROXY = http://relay:<PASSWORT>@<VPS-IP>:3128`
→ **Save Changes**. Im Log erscheint `Discord-Traffic läuft über Proxy …`, in
`/api/diagnostics` steht `"proxy"` und `"verdict": "ok"`. Der Proxy erlaubt
ausschließlich `CONNECT :443` zu Discord-Domains (plus die IP-Dienste der
Diagnose) — kein offenes Relay. Alternative ohne VPS: QuotaGuard Static
(ab 19 $/Monat, dieselbe Variable).

### Nach dem Umzug

1. `GET /api/diagnostics` → `"verdict": "ok"`, `"discord_reachable": true`.
2. Log: `… ist ONLINE` mit Bot-Name und Serverliste.
3. Discord: Bot grün, `/connect` antwortet ephemeral mit Link + Token.
4. Alten Render-Service löschen/pausieren (doppelte Logins vermeiden).
5. UptimeRobot ist auf VPS/Fly **nicht** nötig — schadet aber auch nicht.

---

## Der `/connect`-Command

Der eine Command, um den es geht — **ohne Optionen**. Dauer und Modus gibt es
nicht mehr zum Auswählen: Es gelten die Defaults aus der Konfiguration
(24 h Gültigkeit, Modus *Lesen + Schreiben*; `SESSION_TTL_HOURS` und Co. bleiben
anpassbar). Ein Klick weniger, eine Stolperfalle weniger.

**Was du bekommst — und wer es sieht:**

Die Antwort ist **ephemeral** und eine moderne **Container-V2-Nachricht**
(Discord *Components V2*): ein Container mit Akzentfarbe, Textblöcken,
Trennlinien und Buttons. Nur du siehst sie; für alle anderen im Channel ist sie
unsichtbar.

**Zustand 1 — Bridge noch deaktiviert:**

> # Willkommen!
> Du kannst sofort mit Arena AI losarbeiten. Die Bridge für diesen Server ist
> aktuell noch deaktiviert. Um zu beginnen, klicke auf diesen Button:
> ─────────────
> **[🔌 Verbinden]** *(grün)*

Der Klick erzeugt das Sitzungs-Token **erst in diesem Moment** und bearbeitet
dieselbe Nachricht in Zustand 2.

**Zustand 2 — verbunden** (auch das bekommst du zu sehen, wenn du `/connect`
aufrufst, während bereits eine Verbindung besteht):

> # Willkommen!
> Du kannst sofort mit Arena AI losarbeiten.
> ─────────────
> **1. Kopiere diesen Prompt:**
> `URL=https://dein-relay.onrender.com;TOKEN=adse_…`
> ─────────────
> **2. Öffne Arena AI, schreib ihm was er auf deinem Server einrichten soll
> und schick ihm den Prompt:**
> **[🤖 Arena AI öffnen]**
> ─────────────
> **Weitere Optionen:**
> **[⛔ Verbindung trennen]** *(rot)* **[🔄 Neues Token generieren]**

Der Prompt ist bewusst **minimal und frei von Leerzeichen** — das eindeutige
`URL=…;TOKEN=…`-Format ist für Arena gut lesbar und auf Mobilgeräten mit einem
Tipp kopierbar. Alles Weitere (Regeln, Workflow, Konventionen, Vorlagen)
erzählt die Bridge Arena AI **während der Arbeit** selbst über `GET /api/v1/capabilities` und die `/guides`-Endpoints.
Die Buttons sind persistent (funktionieren auch nach einem Render-Deploy noch):

| Button | Wirkung |
| ------ | ------- |
| 🔌 **Verbinden** | Erzeugt das Token, bearbeitet die Nachricht in den verbundenen Zustand. |
| 🤖 **Arena AI öffnen** | Link-Button direkt zum Arena-Agenten. |
| ⛔ **Verbindung trennen** | Widerruft **alle** Tokens dieses Servers sofort; die Nachricht springt zurück in Zustand 1. |
| 🔄 **Neues Token generieren** | Macht die alten Tokens sofort ungültig und zeigt den Prompt mit dem frischen Token. |

**Beide Prüfungen, beide Richtungen.** `/connect` (und jeder Button) verweigert sich, wenn

- **du** kein Administrator bist → `⛔ Administrator-Rechte erforderlich`
- **der Bot** kein Administrator ist → Anleitung, wie du die Rolle hochstufst,
  inklusive einem frischen Invite-Link mit gesetztem Administrator-Recht

Jeder abgelehnte Versuch wird im Render-Log protokolliert.

---

## Der `/manage_database`-Command & Turso

Um interaktive Server-Systeme wie **Self Roles (Reaction Roles)**, **Willkommensnachrichten**, **Auto-Responder**, **Levels** oder **zeitgesteuerte Cron-Aktionen (z. B. jeden Tag um 5:00 Uhr)** direkt und ohne Drittanbieter-Bots auszuführen, bringt der Bot eine native Integration mit [Turso](https://turso.tech) (libSQL) mit.

### Kostenlose Turso-Datenbank in 2 Minuten:
1. Registriere dich kostenlos auf [turso.tech](https://turso.tech) (Free Plan: bis zu 9 GB Speicher und 1 Mrd. Lesevorgänge).
2. Erstelle eine neue Datenbank (z. B. `my-server-db`).
3. Kopiere die Datenbank-URL (z. B. `libsql://my-server-db-...turso.io`) und erstelle ein Auth-Token (`turso db tokens create ...`).
4. Führe auf deinem Discord-Server `/manage_database` aus und klicke auf **Datenbank verknüpfen**.
5. Trage URL und Token in das Modal ein — fertig!

### Features von `/manage_database`:
- **Live-Speicheranzeige:** Belegter Speicher, Turso-Free-Quota (9 GB) und visuelle Statusübersicht.
- **System-Verwaltung:** Alle aktiven Systeme (Self Roles, Welcome, Cron, Responder) geordnet einsehen und bei Bedarf mit einem Klick löschen.
- **Sicher & Isoliert:** Jeder Server hat seine eigene Datenbank. Der Bot speichert die Server-Zuordnung in einer Master-Datenbank und hält Konfigurationen im schnellen In-Memory-Cache.
- **Arena AI Anbindung:** Fordert ein Nutzer Arena AI auf, Self Roles oder automatisierte Abläufe zu bauen, prüft Arena die Turso-Verbindung (`GET /api/v1/database/status`). Ist noch keine Datenbank verknüpft, stoppt Arena sofort und bittet den Admin, `/manage_database` auf Discord auszuführen. Sobald verknüpft, registriert Arena die Trigger und Aktionen automatisch über `POST /api/v1/systems`!

---

Wer den Bot betreibt, will den Überblick behalten — ohne Discord Developer
Portal oder Render-Logs zu öffnen. Drei Dinge laufen von selbst:

### Live-Presence

Der Statustext des Bots zeigt jederzeit, auf wie vielen Servern er aktiv ist:

```
/connect | 👀 12 eingerichtete Server
```

Bei jedem Server-Beitritt und -Verlassen aktualisiert sich die Zahl sofort
(Singular bei einem Server: *1 eingerichteter Server*).

### `/adminpanel` — die Server-Liste & Serverdetailansicht im Privatchat

Nur für den Bot-Besitzer (`BOT_OWNER_ID`), und nur im **Privatchat mit dem
Bot**. Discord registriert den Command ausschließlich für Bot-DMs, nicht in
Server-Channels oder Gruppen-DMs; zusätzlich prüft jeder Aufruf und Button-Klick
die Owner-ID. Die Liste zeigt:

- **Sortiert nach Mitgliederzahl, abwärts** — die größten Server zuerst.
- **Höchste Priorität:** Server, auf denen der Bot-Owner **noch nicht
  Mitglied** ist, stehen ganz oben und tragen ein ❗️ — genau die Server, bei
  denen ein Blick lohnt.
- **Pagination** (◀️ Zurück / ▶️ Weiter, 10 pro Seite), **Suche** 🔍
  (öffnet ein Eingabefenster, filtert auf Teiltreffer im Servernamen) und ein
  **Schließen-Button** ✖️, der die Panel-DM wieder entfernt.
- **Serverauswahl per Dropdown** (Select-Menü der aktuellen Seite): Öffnet
  sofort die **Server-Übersicht**.

#### Server-Übersicht (Detailansicht)
- **Server-Details:** Name, Server-ID, Mitgliederzahl, Owner-Mention und
  Status, ob der Bot-Owner Mitglied auf diesem Server ist.
- **KI-Verbindungsstatus:** Zeigt sofort an, ob die Bridge auf dem Server
  aktiv ist (inkl. Token-Präfix, Ablaufzeit, Request-Anzahl und letzter Nutzung)
  oder ob noch keine aktiven Sitzungen existieren.
- **Einladungslink für den Owner:** Button ✉️ *Einladung erstellen* generiert
  einen 1-Stunde gültigen Einzellink (`max_uses=1, max_age=3600`) und zeigt
  ihn direkt im Panel an (ehrliche Fehlermeldung bei fehlendem
  `create_instant_invite`-Recht).
- **Server verlassen:** Danger-Button 🚪 *Server verlassen* mit zweistufigem
  Bestätigungsdialog (`guild.leave()`), fängt Fehler ab und kehrt zur
  aktualisierten Liste zurück.
- **Zurück-Button:** Bringt dich mit gemerkter Seite und aktivem Suchbegriff
  zurück in die Listenansicht.

Auch die Panel-Buttons und Selects überleben einen Neustart: Seite, Suchbegriff
und Server-IDs reisen in den Custom-IDs mit — ein altes Panel bleibt bedienbar.

### DM-Benachrichtigungen bei Beitritt & Verlassen

Der Bot-Owner bekommt bei jedem Server-Wechsel eine DM (Container V2 mit
Server-Icon, falls vorhanden): Servername, Mitgliederzahl und Owner-Mention.
Beim **Verlassen** enthält die DM zusätzlich den Grund — sofern er ermittelbar
ist. Ehrlich gesagt: Nach dem Entfernen hat der Bot keinen Zugriff mehr auf
das Audit-Log des Servers, deshalb liefert Discord den Grund meist nicht.
Der Bot versucht es trotzdem (Kick/Ban-Eintrag inkl. Ausführendem und
Begründung) und sagt dir andernfalls offen, dass der Grund nicht ermittelbar
ist — statt etwas zu erfinden.

### Willkommens-DM an den Server-Owner

Der wichtigste Moment ist der Beitritt: Der Owner des Servers, zu dem der Bot
gerade gejoint ist, bekommt sofort eine DM mit der Kurzanleitung — dankend,
kurz, mit dem Servernamen und dem einzigen Schritt, der zählt: `/connect`
auf dem eigenen Server, auf **Verbinden** klicken, den Prompt an Arena AI
schicken. Dauert keine zwei Minuten, dann arbeitet Arena AI auf dem Server.

## Sicherheitsmodell

| Maßnahme | Umsetzung |
| -------- | --------- |
| **Bot-Token bleibt privat** | Arena AI sieht ausschließlich das Sitzungs-Token. Der Bot-Token verlässt den Container nie. |
| **Token werden nie im Klartext gespeichert** | Nur SHA-256-Hashes. Selbst ein Diebstahl von `data/sessions.json` liefert keine nutzbaren Tokens. |
| **Token sind server-gebunden** | Ein Token für Server A funktioniert auf Server B nicht (`409 GUILD_UNAVAILABLE`). |
| **Token laufen ab** | Standard 24 h, konfigurierbar. `ttl_hours=0` für unbegrenzt ist bewusst möglich, aber nicht empfohlen. |
| **Inaktivitäts-Stopp** | War ein Token 24 h verfügbar, wurde aber 24 h lang nicht benutzt, stoppt die Bridge die Verbindung automatisch (`SESSION_INACTIVITY_HOURS`, `0` = aus). Arena AI bekommt beim nächsten Aufruf einen 401er mit Anleitung zum schnellen Wiederanbinden. |
| **Sofortiger Widerruf** | Der `⛔ Verbindung trennen`-Button in `/connect`, die Console oder `DELETE /api/v1/session`. |
| **Sitzungs-Limit** | Maximal 5 aktive Tokens pro Server; das älteste wird automatisch widerrufen. |
| **Berechtigungs-Stufen** | `read` · `write` · `manage` · `danger` — jeder Endpoint hat einen Minimal-Scope. |
| **Rate-Limiting** | 240 Anfragen pro Token und Minute, sliding window. Bei `429` kommt `retry_after`. |
| **Ephemeral + Spoiler** | Die Discord-Antwort sieht nur der Aufrufer; das Token ist zusätzlich gespoilert. |
| **Kein Versions-Leak** | `Server: AIDiscordServerEinrichten` statt `Python/3.x aiohttp/y.z`. |
| **Maskierte Logs** | `config.masked()` kürzt den Token auf `inval…test (28 Zeichen)`. Log-Screenshots sind damit unkritisch. |
| **CORS einschränkbar** | `ALLOWED_ORIGIN` statt `*`, wenn du es enger willst. |

> **Was das Modell bewusst *nicht* leistet:** Im Modus ✍️ **Lesen + Schreiben**
> hat die KI Administrator-Macht über deinen Server — genau das ist der Zweck.
> Sie kann Kanäle löschen, Mitglieder bannen und Einstellungen ändern. Nutze
> 👁️ **Nur lesen**, wenn du erst zuschauen willst, und entzieh den Zugriff mit
> **Verbindung trennen** in `/connect`, sobald du fertig bist.

---

## Arena-Verhalten: Wie die KI deinen Server aufbaut

Der Prompt und die API-Manager schreiben Arena AI seit dem Upgrade nicht nur
*vor, welche Endpoints es gibt*, sondern **wie ein guter Server-Einrichter
arbeitet**:

| Prinzip | Bedeutung |
| ------- | --------- |
| **Nachrichten-Hygiene** | Erneuert die KI Regeln/Infos/Willkommen, löscht sie **zuerst** die alten Nachrichten im Kanal (`purge`, auch >14 Tage alte via `bulk: false`) und pinnt die neue Version an. Doppelte Regel-Postings sind ein Fehler, kein Feature. |
| **Webhook-Personen** | Regeln, News & Willkommen werden nie als nüchterne Bot-Nachricht, sondern als Persona gesendet — eigener Name + eigenes Avatar-Bild (`"webhook": {"name": "📜 Serverregeln"}`). Der Webhook wird automatisch angelegt und wiederverwendet. |
| **Branding komplett** | Server-Icon/Banner (`PATCH /guild`), Webhook-Avatare (`PATCH /webhooks/{id}`) und **das Profilbild des Bots für diesen Server** (`PATCH /members/me` — Nickname, Avatar, Banner, Bio) gehören zum Standard-Setup. |
| **Unicode-Design** | Kanäle, Kategorien und Rollen bekommen einen einheitlichen Unicode-Stil: `「✦」regeln`, `「📌」 INFORMATION`, `꒰👑꒱ Owner`, `✦・chat`. Der Style-Guide liegt unter `GET /api/v1/guides/design`. |
| **Rechte immer mitdenken** | Jede Rolle bekommt explizite Permissions, jede Kategorie Overwrites — Info-Kanäle gesperrt für @everyone, Team-Bereiche unsichtbar, Muted-Rolle berücksichtigt. Der Setup-Validator warnt, wenn etwas fehlt. |
| **Keine Platzhalter** | Die KI erfindet nie `<@123…>`/`<#000…>`-Mentions, sondern löst echte IDs per GET auf. Im Setup-Plan werden `<#key>`-Mentions automatisch zu echten Kanal-Mentions aufgelöst. |
| **Ehrlich bei Fremd-Bots** | Self Roles/Reaction Roles konfiguriert ein Drittanbieter-Bot — Bots dürfen andere Bots nicht einrichten. Die KI bereitet alles Vorarbeitbare (Rollen, Kanal, Persona-Nachricht mit echten Rollen-Mentions), darf Commands anderer Bots aufrufen und postet dem Nutzer eine fertige Schritt-für-Schritt-Anleitung (`GET /api/v1/guides/self-roles` — mit Textbausteinen für Carl-bot, Dyno, MEE6, YAGPDB …). |
| **Sicherheit als Standard** | `verification_level: medium`, Inhaltsfilter, AutoMod-Regeln gegen Werbung/Beleidigungen/Mention-Raid — siehe `GET /api/v1/guides/security`. |

---

## Die REST-API

**121 Endpoints.** Alles unter `/api/v1/*`, Authentifizierung per
`Authorization: Bearer <TOKEN>`.

Die API ist **selbstbeschreibend** — das ist der wichtigste Designentscheid:

```bash
curl -s "https://DEINE-URL/api/v1/capabilities" \
     -H "Authorization: Bearer $TOKEN"
```

liefert jeden Endpoint mit Methode, Pfad, Beschreibung, benötigtem Scope,
Query-Parametern, Body-Feldern und Beispielen — plus Konventionen
(u. a. Webhook-Personas, Nachrichten-Hygiene, keine Mention-Platzhalter,
Unicode-Design), curl-Templates, Rate-Limits und die Liste der Setup-Vorlagen.
Arena AI muss deshalb nichts vorher wissen und nichts auswendig lernen.

### Konventionen

- Erfolg: `{"ok": true, "data": …}`
- Fehler: `{"ok": false, "error": {"code", "message", "hint", "status", "request_id"}}`
  — `hint` sagt der KI konkret, was sie als Nächstes tun soll
- **IDs sind immer Strings.** Snowflakes sind 19-stellig und verlieren in
  JavaScript/JSON als Zahl Präzision.
- Zeitstempel in ISO-8601 UTC
- Permissions als Namen (`"manage_channels"`), Farben als `"#RRGGBB"`
- Fast jeder schreibende Endpoint akzeptiert `"reason"` fürs Audit-Log
- **Webhook zuerst**: System-Nachrichten als Persona (`{"webhook": {"name": …}}`)
- **Aufräumen vor dem Posten**: alte Regeln/Infos per `purge` löschen, nie doppelt
- **Keine Platzhalter-Mentions** — echte IDs, im Setup-Plan `<#key>`-Mentions

### Die wichtigsten Endpoints

| Endpoint | Zweck |
| -------- | ----- |
| `GET /api/health` | Öffentlich. Für UptimeRobot und den Render Health Check. |
| `GET /api/diagnostics` | Öffentlich. Ausgehende IP, Erreichbarkeit von Discord, Login-Status, nächste Schritte. |
| `GET /api/v1/capabilities` | Komplette API-Beschreibung. **Immer zuerst aufrufen.** |
| `GET /api/v1/guild/snapshot` | Der gesamte Ist-Zustand in **einem** Aufruf. |
| `GET /api/v1/me` | Wer bin ich, wo bin ich, was darf ich? |
| `GET /api/v1/channels/tree` | Kanalstruktur als Baum. |
| `GET /api/v1/guides` | Fertige Anleitungen: Self Roles, Unicode-Design, Branding, Webhooks, Security. |
| `POST /api/v1/setup` | **Komplettes Server-Setup in einem Aufruf.** |
| `POST /api/v1/setup/preview` | Denselben Plan validieren, ohne etwas zu ändern. |
| `GET /api/v1/setup/templates` | Sechs fertige deutsche Server-Vorlagen. |
| `POST /api/v1/channels/{id}/messages` | Nachricht senden — mit `"webhook"`-Feld als Persona mit eigenem Namen & Avatar. |
| `POST /api/v1/webhooks/{id}/send` | Direkt als Webhook senden (Name/Avatar pro Nachricht übersteuern). |
| `GET/PATCH /api/v1/webhooks[/{id}]` | Webhooks listen, umbenennen, Avatar setzen, verschieben. |
| `PATCH /api/v1/members/me` | **Server-Profil des Bots**: Nickname, Avatar, Banner, Bio — nur für diesen Server. |
| `GET /api/v1/prompt` | Den Arena-AI-Prompt (`short` / `long` / `system`). |

Dazu: Rollen, Kanäle (Text, Voice, Stage, Forum, Media, Kategorien, Threads,
Positionen, Overwrites), Mitglieder (Timeout, Kick, Ban, Prune, Nicknames,
Voice-Moves, Massen-Timeouts), Nachrichten (Senden, Bearbeiten, Bulk-Delete,
Purge, Pins, Reaktionen, Embeds, Polls, Komponenten), Server-Einstellungen
(Community, Verification Level, Welcome Screen, Onboarding, Widget, MFA, Vanity),
AutoMod, Emojis & Sticker, Webhooks, Invites, Scheduled Events, Audit-Logs,
Voice-States, Action-Log.

### `POST /api/v1/setup` — der Kraftmeier

Ein einziger Aufruf, der einen kompletten Server aufbaut — inklusive
Unicode-Design, Webhook-Personen, Bot-Profil und echter Kanal-Mentions:

```json
{
  "template": "aesthetic-community",
  "guild":     {"name": "Mein Server", "icon": "https://…/icon.png",
                "verification_level": "medium"},
  "roles":     [{"key": "mod", "name": "「🛡️」 Mod", "preset": "moderator", "position": 16}],
  "categories": [{"key": "info", "name": "「📌」 INFORMATION", "position": 0,
                  "overwrites": [{"id": "@everyone", "deny": ["send_messages"]}],
                  "channels": [{"key": "regeln", "name": "「✦」regeln", "type": "text",
                                "topic": "Bitte lesen"}]}],
  "bot_profile": {"nick": "✨ Server-Assistent", "avatar": "https://…/avatar.png"},
  "messages":  [{"channel": "regeln", "pin": true,
                 "webhook": {"name": "📜 Serverregeln", "avatar": "https://…/regeln.png"},
                 "embeds": [{"title": "📜 Regeln", "color": "#5865F2",
                             "description": "Start in <#regeln>!"}]}],
  "settings":  {"system_channel": "welcome", "rules_channel": "regeln",
                "afk_channel": "AFK", "afk_timeout": 900, "community": true},
  "automod":   [{"name": "Werbung", "trigger_type": "keyword",
                 "regex_patterns": ["discord\\.(gg|com)/\\w+"],
                 "actions": [{"type": "block_message"}]}],
  "invites":   [{"channel": "welcome", "max_age": 0}],
  "reason":    "Server-Setup durch Arena AI"
}
```

Der Executor arbeitet in der richtigen Reihenfolge (Rollen **vor** Kanälen,
damit Overwrites die neuen Rollen schon referenzieren können; Server-Einstellungen
**nach** Kanälen, damit `afk_channel` existiert; Bot-Profil **vor** den
Nachrichten), bündelt Rollenpositionen in einen einzigen API-Call, übersetzt
`key`-Referenzen über eine interne Tabelle, ersetzt `<#key>`-Mentions durch
**echte** Kanal-Mentions, sendet Nachrichten mit `webhook`-Feld als Persona
(anlegen + anpinnen inklusive) und meldet am Ende **jeden einzelnen Schritt**
mit OK/Fehler:

```json
{"ok": true, "steps_total": 41, "steps_ok": 41, "steps_failed": 0,
 "report": […], "keys": {"mod": "1234…", "regeln": "5678…"},
 "created": {"roles": […], "channels": […], "messages": […], "webhooks": […]},
 "next_steps": […]}
```

**Sechs fertige Vorlagen** — alle deutsch, alle validiert, alle direkt ausführbar:

| Vorlage | Inhalt |
| ------- | ------ |
| `aesthetic-community` | Unicode-Design (「✦」, ꒰꒱), Webhook-Personen, Rollen-Wahl-Kanal |
| `gaming-community` | Voice-Lounges, LFG, Turnier-Kanäle, Team-Bereich — im Klammern-Stil |
| `creator-streamer` | Stream-Ankündigungen, Subscriber-Bereich, Clip-Forum |
| `lerngruppe` | Fächerkanäle, Ressourcen-Forum, Lernräume, Tutoren-Rollen |
| `business-support` | Support-Kategorien, Ticket-Forum, Wissensdatenbank |
| `freunde` | Klein und gemütlich — ohne Overhead |

`POST /api/v1/setup/preview` validiert denselben Plan **ohne** etwas zu ändern —
ideal, bevor die KI etwas Destruktives tut.

---

## Die Console

Unter `https://DEINE-URL/console` läuft ein dunkles Dashboard ohne Build-Schritt
und ohne eigene Geheimnisse — eine einzige, in sich geschlossene HTML-Datei, die
alles über die REST-API holt.

- **Token-Eingabe** — automatisch übernommen aus `?t=…` (so kommt der Button im
  Discord-Embed daher)
- **Prompt in drei Varianten** — `short` (passt in Discord), `long` (mit
  API-Referenz), `system` — jeweils mit *Kopieren* und *Als `.md` speichern*
- **Verbindungsstatus** — Server, Modus, Gültigkeit, Aufruf-Statistik
- **Server-Snapshot** — der komplette Ist-Zustand zum Aufklappen
- **Live-Action-Log** — pollt `/api/v1/actions` und zeigt jeden API-Aufruf der KI
- **API-Tester** — Endpoints direkt im Browser ausprobieren
- **Token widerrufen / erneuern** — zwei Buttons, ohne zurück zu Discord

Mit `CONSOLE_ENABLED=false` lässt sie sich komplett abschalten; `/console`
antwortet dann mit einem JSON-404.

---

## UptimeRobot

Auf Render Free schläft ein Web Service nach 15 Minuten ohne Anfrage ein — und
mit ihm der Bot. Ein externer Ping hält ihn wach.

**Warum das mehr als Kosmetik ist:** Jeder Wake-up ist ein **neuer Discord-Login**
über Renders geteilte Ausgangs-IP — und damit ein neues Risiko, in eine
Cloudflare-Sperre zu laufen (siehe [Fehlerbehebung](#fehlerbehebung)). Ein Bot,
der einmal drin ist, hält seine Gateway-Session per `RESUME` auch durch Sperren
hindurch — solange er nicht einschläft. Auf einem eigenen Server oder Fly.io
entfällt das Problem komplett.

**Ziel-URL:**

```
https://DEINE-RENDER-URL.onrender.com/api/health
```

**Einrichten:**

1. <https://uptimerobot.com> → kostenloser Account
2. **Add New Monitor**
   - Monitor Type: **HTTP(s)**
   - Friendly Name: `AIDiscordServerEinrichten`
   - URL: die Health-URL von oben
   - Monitoring Interval: **5 minutes** (Minimum im Free-Plan)
3. **Create Monitor**

`/api/health` ist **öffentlich** (kein Token nötig), antwortet in wenigen
Millisekunden und liefert:

```json
{"ok": true, "data": {
  "status": "healthy", "bot": "connected", "guilds": 3,
  "gateway_latency_ms": 42.1, "uptime_seconds": 8123.4,
  "active_sessions": 1, "requests_served": 118,
  "version": "1.0.0", "timestamp": "2026-09-05T18:33:15Z"
}}
```

> **Bewusst so gebaut:** `status` ist schon `"healthy"`, während der Bot noch
> mit Discord verbindet. Der Healthcheck darf nie einen 500er liefern — sonst
> meldet Render das Deploy als fehlgeschlagen und UptimeRobot schlägt grundlos
> Alarm. Der Discord-Status steht separat im Feld `bot`.

**Alternativen,** falls du UptimeRobot nicht magst: derselbe Endpoint funktioniert
mit Cron-job.org, Healthchecks.io, besserping oder einem GitHub-Action-Cron.
Trage die URL zusätzlich in Render unter *Settings → Health Check Path* ein.

---

## Konfiguration

Alles über Umgebungsvariablen. Nur `DISCORD_BOT_TOKEN` ist Pflicht.
Eine kommentierte Vorlage liegt in [`.env.example`](.env.example).

| Variable | Default | Bedeutung |
| -------- | ------- | --------- |
| `DISCORD_BOT_TOKEN` | — | **Pflicht.** Bot-Token aus dem Developer Portal. |
| `PUBLIC_URL` | *(leer)* | Erzwingt die öffentliche Basis-URL. Auf Render nicht nötig. |
| `HOST` | `0.0.0.0` | Bind-Adresse. Nicht ändern — Render braucht `0.0.0.0`. |
| `PORT` | `8080` | Wird von Render automatisch gesetzt. |
| `COMMAND_NAME` | `connect` | Name des Slash-Commands. |
| `BOT_NAME` | `AIDiscordServerEinrichten` | Anzeigename in Antworten. |
| `PRESENCE_STATUS` | `online` | `online` / `idle` / `dnd` / `invisible`. Der Statustext ist live: `/connect \| 👀 N eingerichtete Server`. |
| `BOT_OWNER_ID` | *(fest)* | Discord-User-ID des Bot-Besitzers. Schaltet `/adminpanel` im Privatchat und die DM-Benachrichtigungen bei Server-Beitritt/-Verlassen frei. `0` = aus. |
| `SESSION_TTL_HOURS` | `24` | Standard-Gültigkeit eines Tokens. `0` = unbegrenzt. |
| `SESSION_INACTIVITY_HOURS` | `24` | Verbindung wird automatisch gestoppt, wenn das Token so lange ungenutzt bleibt. `0` = aus. |
| `MAX_SESSIONS_PER_GUILD` | `5` | Aktive Tokens pro Server, bevor das älteste widerrufen wird. |
| `PERSIST_SESSIONS` | `true` | Tokens in `DATA_DIR/sessions.json` sichern. |
| `DATA_DIR` | `data` | Datenverzeichnis (auf Render Free flüchtig). |
| `API_RATE_LIMIT` | `240` | Anfragen pro Token … |
| `API_RATE_WINDOW` | `60` | … pro diesem Fenster in Sekunden. |
| `ALLOWED_ORIGIN` | `*` | CORS. Kommagetrennte Liste für mehr Enge. |
| `CONSOLE_ENABLED` | `true` | Dashboard unter `/console` an/aus. |
| `MAX_BODY_BYTES` | `8388608` | Maximale Request-Größe (Base64-Icons!). |
| `MAX_MESSAGE_HISTORY` | `100` | Obergrenze beim Nachrichten-Lesen. |
| `MAX_MEMBER_FETCH` | `1000` | Obergrenze beim Mitglieder-Abruf. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `PRIVILEGED_INTENTS` | `true` | Members- und Message-Content-Intent anfordern. |
| `SETTLE_SCALE` | `1.0` | Faktor für Cache-Wartezeiten. `0` nur für Tests. |
| `DISCORD_PROXY` | *(leer)* | HTTP(S)-Proxy für **alle** Discord-Verbindungen (REST + Gateway). Dauerhafte Lösung bei gesperrter Cloud-IP. |
| `DISCORD_PROXY_USER` | *(leer)* | Proxy-Benutzername (falls nicht in der URL). |
| `DISCORD_PROXY_PASSWORD` | *(leer)* | Proxy-Passwort (erscheint maskiert im Log). |
| `NETCHECK_TIMEOUT_SECONDS` | `8` | Zeitgrenze der Netzwerk-Diagnose beim Start. |
| `BAN_PROBE_INTERVAL_SECONDS` | `30` | Abstand der tokenlosen Proben, während die IP gesperrt ist. |
| `BAN_WATCH_SECONDS` | `600` | So lange wird die Sperre beobachtet, bevor der Container neu gestartet wird. `0` = nie. |
| `RESTART_ON_IP_BAN` | `true` | Bei dauerhaft gesperrter IP: Prozess beenden (Exit-Code 3) → Render startet neu → neue IP. |
| `RESTART_DELAY_SECONDS` | `30` | Pause vor diesem bewussten Neustart. |
| `RESTART_MAX_CYCLES` | `5` | So viele Neustart-Zyklen hintereinander, dann nur noch proben + klares Urteil in Log und `/api/diagnostics`. Zähler in `DATA_DIR/restart_ledger.json` (überlebt Neustarts nur mit persistentem Volume). `0` = unbegrenzt. |
| `BAN_FAST_RESTART_ABOVE_SECONDS` | `600` | Meldet Cloudflare ein `Retry-After` darüber (und über `BAN_WATCH_SECONDS`), wird das Fenster auf `BAN_FAST_RESTART_WATCH_SECONDS` verkürzt → mehr IP-Würfe pro Stunde. `0` = aus. |
| `BAN_FAST_RESTART_WATCH_SECONDS` | `90` | Verkürztes Beobachtungsfenster im Schnell-Neustart-Fall (3 Proben à 30 s). |
| `BAN_EGRESS_RECHECK_EVERY` | `5` | Jede n-te Probe misst die Ausgangs-IP neu; ein Wechsel wird geloggt und in `/api/diagnostics` gezählt. `0` = nur beim Start. |
| `LOGIN_RETRY_BASE_SECONDS` | `15` | Basis des Backoffs nach anderen Login-Fehlern. |
| `LOGIN_RETRY_MAX_SECONDS` | `600` | Obergrenze des Backoffs. |
| `FATAL_RETRY_SECONDS` | `300` | Abstand nach fatalen Fehlern (ungültiges Token, Gateway 4004 …). |

---

## Lokal entwickeln

```bash
git clone https://github.com/mertastudios/AIDiscordServerEinrichten.git
cd AIDiscordServerEinrichten

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Token eintragen
export $(grep -v '^#' .env | xargs)

python -m bot
```

Der Bot lauscht dann auf `http://localhost:8080`:

- `http://localhost:8080/console` — Dashboard
- `http://localhost:8080/api/health` — Healthcheck
- `http://localhost:8080/api/v1/capabilities` — API-Beschreibung

**Lokal vor Arena AI sichtbar machen:** Arena AI erreicht `localhost` nicht.
Nutze einen Tunnel und setze `PUBLIC_URL` darauf:

```bash
cloudflared tunnel --url http://localhost:8080
export PUBLIC_URL=https://irgendwas.trycloudflare.com
```

Slash-Commands brauchen nach der ersten Registrierung bis zu einer Stunde, bis
sie in allen Clients erscheinen (`/connect` auf bestehenden Servern meist sofort).

---

## Tests

```bash
python scripts/smoke_test.py          # 283 Prüfungen, ohne Discord-Verbindung
python scripts/smoke_test.py -v       # jede einzelne Prüfung anzeigen
python scripts/smoke_test.py auth read write   # nur ausgewählte Gruppen

python scripts/login_recovery_test.py # 201 Prüfungen: Login/Rate-Limit + Client-Setup + Owner-Features
python -m bot.netcheck                # echte Netz-Diagnose (IP + discord.com)
```

Der Smoke-Test startet den **echten** Web-Server gegen einen simulierten
Discord-Client ([`scripts/_fake_discord.py`](scripts/_fake_discord.py)) und prüft:

| Gruppe | Inhalt |
| ------ | ------ |
| `public` | Healthcheck, Landingpage, Console, robots.txt, Capabilities ohne Token |
| `auth` | 401-Pfade, Token-Format, Hash-Speicherung, `/me`, Prompt-Varianten |
| `read` | Guild, Kanalbaum, Rollen, Mitglieder, Suche, Permissions, Snapshot, Audit-Log |
| `scopes` | `read` darf nicht schreiben, `read_write` darf alles · Legacy-Modi werden gemappt |
| `errors` | JSON statt HTML bei 404/405, ungültiges JSON, fremder Server |
| `setup` | Alle sechs Vorlagen validieren; fehlerhafte Pläne werden abgewiesen |
| `lifecycle` | Token erneuern, widerrufen, Ablauf |
| `ratelimit` | `429` mit `retry_after` und `Retry-After`-Header, Budget pro Token |
| `write` | **Alle sechs Vorlagen werden wirklich ausgeführt** — Rollen, Kategorien, Kanäle, Nachrichten, Invites, AutoMod |
| `arena` | Webhook-Personen (Anlegen/Wiederverwenden/`/send`), Bot-Profil via `PATCH /members/me`, Guides, echte `<#key>`-Mentions im Setup, Purge vor dem Erneuern, Platzhalter-Warnung |
| `prompt` | Prompt-Varianten transportieren Hygiene/Personas/Branding/Grenzen; `capabilities.conventions` komplett |
| `limit` | Sitzungs-Limit pro Server, Auto-Revoke des ältesten Tokens |
| `security` | Kein Token-Leak in Antworten, Logs oder der Console; CORS; Header |

Die Test-Double sind **echte Unterklassen** von `discord.Role`,
`discord.TextChannel`, `discord.Member` usw. — damit die `isinstance`-Prüfungen
der API tatsächlich greifen und der Test nicht an Attrappen vorbeiläuft.

[`scripts/login_recovery_test.py`](scripts/login_recovery_test.py) prüft den Teil,
der einen Bot auf Free-Hosting zuverlässig offline hält: das Verhalten bei
`429 / Cloudflare Error 1015`. Er ersetzt die Uhr durch eine Fake-Uhr und scriptet
die Discord-Antworten — läuft also in Millisekunden, ohne Netzwerk und ohne Token:

| Fall | Erwartetes Verhalten |
| ------ | -------------------- |
| IP-Sperre hebt sich | tokenlose Proben alle 30 s, danach **sofort** neuer Login |
| IP bleibt gesperrt | nach `BAN_WATCH_SECONDS` → `RestartRequested` (Exit-Code 3) |
| `RESTART_ON_IP_BAN=false` | unbegrenzt weiter probieren, kein Prozess-Exit |
| `Retry-After 8034 s` (der gemessene Render-Fall) | Fenster auf 90 s verkürzt, dann Neustart — nicht 10 min in eine 2-h-Sperre proben |
| 5 Neustarts ohne Erfolg (Buch auf Platte) | kein weiterer Neustart, `platform_verdict` gesetzt, weiter proben |
| Ausgangs-IP wechselt innerhalb eines Prozesses | wird erkannt, gezählt und geloggt |
| IP frei, Login trotzdem 429 | Token-Problem: lange warten, **kein** Container-Neustart |
| Discord-429 mit `Via`-Header | `Retry-After` wird gedeckelt — nie wieder 1800 s blind schlafen |
| HTTP 5xx mehrfach | Backoff eskaliert (der Fehlerzähler verfällt nicht mehr) |
| Client-Setup (echter `RelayClient`, kein Fake) | `setup_hook()` registriert 2 Commands + 1 persistente View; Button-Klicks (`relay:regenerate` / `relay:revoke_all`) erreichen ihre Handler; keine Bot-only-API in der Quelle |

Der GitHub-Actions-Workflow liegt in [`ci/ci.yml`](ci/ci.yml) und prüft bei jedem
Push und Pull Request Python **3.11 und 3.12**, baut zusätzlich das Docker-Image
und verlangt `/api/health` im laufenden Container.

> **Einmalig aktivieren** — die Datei liegt in `ci/`, weil der Deploy-Bot keine
> `workflows`-Berechtigung hat und GitHub den Push sonst komplett ablehnt:
>
> ```bash
> mkdir -p .github/workflows && git mv ci/ci.yml .github/workflows/ci.yml
> git commit -m "ci: GitHub-Actions-Workflow aktivieren" && git push
> ```
>
> Details in [`ci/README.md`](ci/README.md). Das Badge oben wird erst sichtbar,
> sobald die Datei unter `.github/workflows/` liegt.

---

## Fehlerbehebung

**`/adminpanel` hängt endlos bei „Bot denkt nach…“ und endet mit „Die Anwendung reagiert nicht“**
Versionen vor **1.0.4** konnten die Server-Liste nicht senden, sobald
mindestens ein Server dabei war, auf dem der Bot-Owner (noch) **kein
Mitglied** ist (❗️-Markierung): Der Zeilen-Marker im Suchfeld nutzt ein
vollständiges Emoji mit Variation-Selector (`❗️` = U+2757 + U+FE0F). Discord
akzeptiert diese Sequenz bei **Buttons** klaglos, lehnt sie aber bei
**Select-Menü-Optionen** mit `400 Invalid Form Body — Invalid emoji` ab (eine
bekannte Inkonsistenz der Discord-API). `defer()` war dabei immer erfolgreich
— erst der `followup.send()` mit der fertigen Liste scheiterte, weshalb der
Client bis zum Timeout „Bot denkt nach…“ zeigte. Auf `main` aktualisieren
(`/api/health` → `version` sollte `1.0.4` oder neuer sein) und `/adminpanel`
erneut ausführen. Im Render-Log steht bei diesem Fehler
`followup.send() fehlgeschlagen … discord_error_code=…`.

**`AttributeError: 'RelayClient' object has no attribute 'add_listener'` im Log**
Versionen vor **1.0.1** hatten einen Fehler in `setup_hook()` (Bot-only-API auf
einem nackten `discord.Client`). Auf `main` aktualisieren — `/api/health` zeigt
unter `version`, ob das Deploy die gefixte Version enthält (`1.0.1` oder neuer).

**`/connect` erscheint nicht im Discord-Menü**
Slash-Commands brauchen nach der Registrierung bis zu einer Stunde. Prüfe im
Render-Log `Slash-Commands global registriert`. Falls der Bot nicht auf dem
Server ist: neu über den OAuth2-Link aus dem Discord Developer Portal einladen.

**`⛔ Der Bot braucht Administrator-Rechte`**
Servereinstellungen → Rollen → Bot-Rolle → *Administrator* aktivieren **und die
Rolle ganz nach oben** schieben. Danach `/connect` erneut ausführen.

**`409 GUILD_UNAVAILABLE`**
Der Bot ist noch nicht mit dem Gateway synchronisiert (typisch direkt nach einem
Cold-Start) oder wurde vom Server entfernt. 5–10 Sekunden warten, erneut
versuchen; sonst `/connect` neu ausführen.

**`401 TOKEN_MISSING` / `TOKEN_INVALID` / `TOKEN_REVOKED`**
Token fehlt, ist abgelaufen, wurde widerrufen oder die Verbindung wurde nach
24 Stunden ohne Nutzung automatisch gestoppt. Die Meldungen richten sich an
Arena AI: Sie sagen der KI, was der Server-Admin **vermutlich getan** hat
(neues Token generiert, Verbindung getrennt/zurückgesetzt, Inaktivität) und wie
die Verbindung schnell wieder aufgebaut wird — `/connect`, **Verbinden**
klicken, neuen Prompt schicken. Der Admin selbst führt einfach `/connect` erneut
aus.

**Bot bleibt offline: `429 Too Many Requests` / Cloudflare `Error 1015`**

Das ist in über 90 % der Fälle **kein Fehler im Bot und keiner im Token**,
sondern eine gesperrte **ausgehende IP-Adresse**. Discord liegt hinter
Cloudflare; überschreitet eine IP das Limit (10.000 *ungültige* Anfragen pro
10 Minuten bzw. 50 Anfragen/s), blockt Cloudflare jede Anfrage von dieser IP mit
`429` und einer HTML-Seite („Error 1015 — You are being rate limited"). Auf
Free-Plänen teilen sich hunderte Kunden wenige Ausgangs-IPs: Flutet ein anderer
Mieter Discord, ist die IP für **alle** gesperrt — dein Bot kommt dann nie
online, obwohl er völlig korrekt ist.

**Schritt 1 — Diagnose (30 Sekunden):**

```
https://<dein-service>.onrender.com/api/diagnostics
```

Öffentlich, ohne Token, funktioniert auch bei offline-Bot (der Web-Server läuft
immer). Alternativ in der Render-Shell: `python -m bot.netcheck`. Entscheidend
ist `verdict`:

| `verdict` | Bedeutung | Was zu tun ist |
| --------- | --------- | -------------- |
| `ip_blocked` | Cloudflare sperrt die ausgehende IP (`egress_ip`). | **Nichts am Bot ändern** — der regelt das selbst (siehe unten). Dauerhaft: eigene IP besorgen. |
| `ok`, Bot trotzdem offline | Discord ist erreichbar, der Login scheitert aus einem anderen Grund. | `login.status` lesen: `invalid_token` → Token resetten; `rate_limited` → doppelte Prozesse stoppen; `connection_refused` → Intents prüfen. |
| `network_error` | `discord.com` ist gar nicht erreichbar (DNS/TCP/TLS). | Ausgehenden Traffic/Firewall des Hosters prüfen, ggf. `DISCORD_PROXY`. |

**Schritt 2 — was der Bot automatisch tut** (bei `ip_blocked`):

1. Er misst die Sperre mit einem **tokenlosen** Aufruf von
   `GET https://discord.com/api/v10/gateway`. Der liegt hinter derselben
   Cloudflare-Regel wie der Login, zählt aber nicht als ungültige Anfrage.
2. Er probt alle `BAN_PROBE_INTERVAL_SECONDS` (30 s = 20 Anfragen pro
   10 Minuten = 0,2 % von Discords 10.000er-Limit) und loggt sich **sofort**
   ein, sobald die Sperre fällt. Blind 30 Minuten zu schlafen — wie es eine
   frühere Version tat — verpasst genau dieses Fenster.
3. Bleibt die IP `BAN_WATCH_SECONDS` (10 min) lang gesperrt — oder meldet
   Cloudflare gleich ein `Retry-After` über `BAN_FAST_RESTART_ABOVE_SECONDS`
   (dann nur 90 s) — beendet sich der Prozess mit **Exit-Code 3**. Render
   startet einen frischen Container, der *vielleicht* eine andere Adresse aus
   dem geteilten Ausgangs-Bereich zieht. Im Log steht vor jedem Neustart die
   gesperrte IP, nach jedem Neustart, ob sie gewechselt hat.
4. Nach `RESTART_MAX_CYCLES` (5) erfolglosen Zyklen hört er auf zu würfeln,
   probt nur noch und schreibt ein klares Urteil in Log und
   `/api/diagnostics` (`platform_verdict`): entweder *„jeder Neustart lieferte
   dieselbe IP"* oder *„n verschiedene IPs, alle gesperrt"*. Beides heißt:
   diese Plattform kommt nicht durch → eigene Ausgangs-IP. (Auf Render Free
   ist das Dateisystem flüchtig — der Zähler beginnt dort bei jedem Container
   neu; das Urteil siehst du zuverlässig auf VPS/Fly oder mit Render-Disk.)
5. `GET /api/health` zeigt laufend `discord_status` (`ip_blocked`, `rate_limited`,
   `network_error`, `connecting`, `online`), `egress_ip`, `discord_reachable`,
   `restarts` und `discord_retry_at`. `GET /api/diagnostics` zusätzlich alle
   gesehenen Ausgangs-IPs, das Neustart-Buch und konkrete nächste Schritte.

**„Aber meine anderen Bots auf Render laufen doch!"** — Ja, und das passt
zusammen: Eine Cloudflare-Sperre blockt **neue REST-Logins**, killt aber keine
**bestehende** Gateway-Verbindung. Bots, die reingekommen sind, als der Pool
gerade sauber war, laufen per `RESUME` wochenlang weiter. Ein neuer Bot braucht
den Login *jetzt* — und jetzt ist der Pool dicht. Genau deshalb muss der
Bot, sobald er einmal drin ist, den Login nie wieder verlieren (UptimeRobot
gegen Spin-down, keine unnötigen Deploys).

**Manuell eingreifen musst du, wenn es nicht von selbst weggeht:**

- **Kostenloser Versuch — Service neu in Frankfurt anlegen.** `region: frankfurt`
  in `render.yaml` greift nur bei **Neuanlage**; ein bestehender Service bleibt
  in Oregon (Render kann Regionen nicht umziehen). Also: alten Service im
  Dashboard löschen → *New + → Blueprint* → Repo wählen → Token eintragen →
  *Apply* → im Service unter *Settings* prüfen, dass **Region: Frankfurt** steht
  → nach dem Deploy `GET /api/diagnostics`: `egress_ip` sollte eine EU-Adresse
  sein und `verdict` idealerweise `ok`. Der Frankfurt-Pool ist deutlich weniger
  bot-lastig als Oregon — eine Garantie ist das nicht.
- **Zuverlässige Lösung — eigene Ausgangs-IP** (≈ 4 €/Monat):
  [Betrieb auf einem eigenen Server](#betrieb-auf-einem-eigenen-server) —
  VPS (`deploy/docker-compose.yml`), Fly.io (`deploy/fly.toml`) oder Render +
  eigener Proxy (`deploy/proxy/`, Variable `DISCORD_PROXY`).
- **Doppelte Prozesse ausschließen** — häufigste *selbstgemachte* Ursache:
  derselbe Token läuft lokal **und** auf Render, oder ein zweiter Render-Service
  nutzt dasselbe Token. Alles außer einem stoppen. `numInstances` muss `1` sein.
- **Kein Ausweg:** bezahlter Render-Plan allein (bleibt ein geteilter
  CIDR-Bereich), Renders Dedicated IPs (~100 $/Monat), Rotating-Proxies oder
  Cloudflare-Umgehung (Account-Bann).
- **Neustart abschalten**, falls du lieber selbst deployen willst:
  `RESTART_ON_IP_BAN=false` (dann probt der Bot unbegrenzt weiter).

> **Früher stand hier „warte einfach und deploye nicht neu".** Das war bei
> einer *geteilten* IP der falsche Rat: Der `Retry-After`-Wert der Blockseite
> ließ den Bot bis zu 1800 s schlafen, und weil der Zähler für Fehlversuche nach
> 300 s ohne Versuch zurückgesetzt wurde, eskalierte nichts — es stand jedes Mal
> „Fehlversuch 1 in Folge" im Log, und der Bot hing für immer in einem
> 30-Minuten-Takt fest. Deshalb gibt es jetzt die Diagnose, die kurzen Proben
> und den IP-Wechsel per Container-Neustart.

**Bot startet nicht: `LoginFailure`**
`DISCORD_BOT_TOKEN` ist ungültig. Im Developer Portal **Reset Token**, neues
Token in Render unter *Environment* eintragen, **Save Changes**. Der Prozess
bleibt dabei bewusst am Leben (grünes `/api/health`) und versucht den Login
alle 5 Minuten erneut — kein Crash-Loop, kein Login-Spam.

**Bot startet nicht: `PrivilegedIntentsRequired`**
Der Bot startet automatisch ohne privilegierte Intents neu und bleibt online.
Dauerhafte Lösung: Developer Portal → Bot → `SERVER MEMBERS INTENT` aktivieren.

**Token ist nach einem Deploy weg**
Auf Render Free ist das Dateisystem flüchtig. `/connect` erneut ausführen. Für
Persistenz einen bezahlten Plan mit Disk nutzen — in [`render.yaml`](render.yaml)
ist der Block bereits auskommentiert vorbereitet.

**Arena AI meldet `429`**
Das Rate-Limit greift. Die Antwort enthält `error.details.retry_after`. Für
Massenänderungen die Sammel-Endpoints nutzen (`POST /api/v1/setup`,
`/api/v1/channels/bulk`, `/api/v1/roles/bulk`) — die zählen als ein Aufruf.

**Arena AI erreicht die URL nicht**
`PUBLIC_URL` muss gesetzt sein oder `RENDER_EXTERNAL_URL` von Render kommen. Im
Log steht beim Start die erkannte Basis-URL; bei `localhost` erscheint eine
ausdrückliche Warnung.

---

## Projektstruktur

```
AIDiscordServerEinrichten/
├── bot/
│   ├── main.py              Einstiegspunkt: Bot + Web-Server in einem Loop
│   ├── __main__.py          python -m bot
│   ├── discord_bot.py       /connect, /manage_database, privates /adminpanel, Container-V2-Buttons, Gateway-Events
│   ├── config.py            Umgebungsvariablen, Validierung, maskierte Ausgabe
│   ├── netcheck.py          Netz-Diagnose: ausgehende IP + discord.com-Probe
│   ├── restarts.py          Neustart-Buch: Zyklen + gesehene IPs über Prozessgrenzen
│   ├── sessions.py          Token erzeugen, hashen, verifizieren, widerrufen
│   ├── prompt.py            Die drei Prompt-Varianten für Arena AI
│   ├── serializers.py       discord.py-Objekte → JSON-sichere Dicts
│   ├── util.py              Parsing: IDs, Farben, Permissions, Enums, Overwrites
│   ├── web_errors.py        ApiError mit code/message/hint/status
│   ├── turso/               Turso libSQL-Client, Master-DB, Guild-DB Manager, Trigger-Engine & Service
│   └── web/
│       ├── app.py           aiohttp-App, Middleware, Rate-Limit, CORS
│       ├── console.py       Das Dashboard (eine HTML-Datei, kein Build)
│       ├── context.py       Pro-Request-Kontext aller Handler
│       ├── registry.py      @route-Dekorator + Endpoint-Verzeichnis
│       ├── channel_ops.py   Kanäle anlegen/bearbeiten/auflösen
│       ├── message_ops.py   Nachrichten, Embeds, Komponenten
│       ├── webhook_ops.py   Webhook-Personen (wait=True) + echte Mentions statt Platzhalter
│       ├── images.py        Icons/Banner aus URL, Base64 oder Emoji
│       └── routes/
│           ├── meta.py      health, capabilities, me, session, prompt, snapshot, bot profile (/bot)
│           ├── guild.py     Server-Einstellungen, Welcome, Onboarding, Widget
│           ├── channels.py  Kanäle, Kategorien, Threads, Positionen
│           ├── roles.py     Rollen, Positionen, Vorlagen
│           ├── members.py   Mitglieder, Suche, Permissions, Bot-Profil (/members/me)
│           ├── moderation.py Timeout, Kick, Ban, Prune, AutoMod
│           ├── messages.py  Senden (auch als Persona), Purge, Pins, Reaktionen
│           ├── webhooks.py  Webhooks listen/bearbeiten/löschen/senden
│           ├── guides.py    Fertige Anleitungen (Self Roles, Design, Branding …)
│           ├── expressions.py Emojis und Sticker
│           ├── invites.py   Invites auflisten, anlegen, aufräumen
│           ├── events.py    Scheduled Events
│           ├── systems.py   Turso-Datenbankstatus, Queries, Variablen & native Systeme
│           └── setup.py     Der Setup-Wizard + sechs Vorlagen
├── scripts/
│   ├── smoke_test.py        283 Prüfungen ohne Discord-Verbindung
│   ├── login_recovery_test.py 202 Prüfungen: 429/1015 + Client-Setup + Owner-Features
│   └── turso_systems_test.py Turso Client, Master-DB, Trigger-Engine & REST-APIs
│   └── _fake_discord.py     Echte discord.py-Subklassen als Test-Double
├── deploy/
│   ├── docker-compose.yml   VPS: Bot + Caddy (HTTPS) aus dem vorhandenen Dockerfile
│   ├── Caddyfile            Reverse-Proxy mit Let's Encrypt
│   ├── relay.env.example    Vorlage für Token/PUBLIC_URL auf dem Server
│   ├── relay.service        systemd-Unit (Betrieb ohne Docker)
│   ├── fly.toml             Fly.io (Region fra, Volume, kein Spin-down)
│   └── proxy/               Option 3: Squid-Proxy mit statischer IP für DISCORD_PROXY
├── ci/
│   ├── ci.yml               GitHub-Actions-Workflow (siehe ci/README.md)
│   └── README.md            So wird er aktiviert
├── Dockerfile               Production-Image (tini als PID 1, non-root)
├── render.yaml              Render-Blueprint
├── requirements.txt
├── .env.example             Kommentierte Konfigurationsvorlage
└── README.md
```

---

## Hinweis

**Kurz gesagt:** Dieses Projekt gibt einer KI Administrator-Rechte über deinen
Discord-Server. Lies den Abschnitt [Sicherheitsmodell](#sicherheitsmodell), bevor
du es auf einem Server einsetzt, der dir wichtig ist — und trenne die Verbindung,
wenn du fertig bist.
