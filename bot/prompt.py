"""
Erzeugt den **fertigen Prompt** für Arena AI.

Es gibt drei Varianten:

``short``
    Passt in einen Discord-Codeblock (< 1900 Zeichen). Zum direkten Kopieren.
``long``
    Vollständige Version mit Endpoint-Referenz & Workflow. Wird auf der
    Console angezeigt und als ``arena-prompt.md`` an die Nachricht angehängt.
``system``
    Optionaler Systemprompt-Baustein, falls Arena AI ein separates
    System-Prompt-Feld hat.

Der Prompt ist bewusst auf Deutsch, imperativ und mit konkreten ``curl``-
Beispielen formuliert: Je weniger Interpretationsspielraum, desto zuverlässiger
arbeitet die KI.

Seit dem Arena-Upgrade 2.0 trägt der Prompt zusätzlich die **Gestaltungs- und
Hygiene-Regeln**, die in der Praxis den Unterschied machen:

* Nachrichten-Personas über Webhooks statt nüchterner Bot-Nachrichten,
* Kanäle räumen, bevor neue Regeln/Infos gepostet werden,
* Branding komplett (Server-Icon, Banner, **Bot-Avatar pro Server**),
* Unicode-Design für Kanäle/Kategorien/Rollen,
* Berechtigungen IMMER mitdenken,
* niemals Mention-Platzhalter,
* ehrliche Grenzen bei Fremd-Bots (Self Roles) inkl. fertiger Anleitung.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = ("PromptContext", "build_prompt", "short_prompt", "long_prompt", "system_prompt")

DISCORD_MESSAGE_LIMIT = 2000
EMBED_FIELD_LIMIT = 1024


@dataclass(slots=True)
class PromptContext:
    base_url: str
    token: str
    guild_name: str
    guild_id: int
    mode_label: str
    scope: str
    expires_label: str
    session_id: str
    member_count: Optional[int] = None
    console_url: Optional[str] = None

    def as_dict(self, *, include_token: bool = True) -> Dict[str, Any]:
        data = {
            "base_url": self.base_url,
            "guild_name": self.guild_name,
            "guild_id": str(self.guild_id),
            "mode_label": self.mode_label,
            "scope": self.scope,
            "expires_label": self.expires_label,
            "session_id": self.session_id,
            "member_count": self.member_count,
            "console_url": self.console_url,
        }
        if include_token:
            data["token"] = self.token
        return data


def _header(ctx: PromptContext) -> str:
    return (
        f"DISCORD-RELAY-ZUGANG · Server: {ctx.guild_name}\n"
        f"API-BASIS: {ctx.base_url}\n"
        f"TOKEN: {ctx.token}"
    )


def short_prompt(ctx: PromptContext) -> str:
    """Kompakter Prompt für den Discord-Codeblock (Copy & Paste nach Arena AI)."""
    members = f" · {ctx.member_count} Mitglieder" if ctx.member_count else ""
    return f"""{_header(ctx)}

Du (Arena AI) steuerst "{ctx.guild_name}"{members} per REST-API als Admin-Bot ({ctx.mode_label}, bis {ctx.expires_label}).
BASE=API-BASIS, AUTH="Authorization: Bearer TOKEN". Bei POST/PATCH: JSON-Header.
Start: GET $BASE/api/v1/capabilities und /guild/snapshot.

REGELN:
• IDs als Strings. "ok":false → error.hint folgen. 429 → warten. Massen: POST /api/v1/setup.
• DATENBANK & SYSTEME: Self Roles, Welcome, Timer (5 Uhr)? Prüfe GET /api/v1/database/status. Falls false ⇒ STOPPE SOFORT deine Antwort und fordere Nutzer auf, /manage_database auf Discord auszuführen (Turso kostenlos!). Falls true ⇒ POST /api/v1/systems oder /database/query!
• HYGIENE: Regeln erneuern? Erst löschen: POST /api/v1/channels/{{id}}/purge {{"bots_only":true,"confirm":true}} (>14 Tage ⇒ "bulk":false), dann posten+"pin":true.
• PERSONAS: Regeln/News/Willkommen als Webhook (Name+Avatar): POST …/messages {{"webhook":{{"name":"📜 Regeln"}}}}
• BRANDING: Icon/Banner: PATCH /api/v1/guild · Bot-Avatar/Nick: PATCH /api/v1/members/me · Bot-Global: PATCH /api/v1/bot
• UNICODE-DESIGN: 「✦」regeln · 「📌」 INFORMATION · ꒰👑꒱ Owner · ✦・chat (Ideen: GET /api/v1/guides/design)
• RECHTE: Rolle ⇒ permissions; Kategorie ⇒ overwrites (Info: @everyone deny send_messages)
• KEINE PLATZHALTER: Nie <@123…>/<#000…> erfinden — echte IDs per GET auflösen.
• FREMD-BOTS: Drittanbieter-Rollenbots: Vorarbeit + Anleitung GET /api/v1/guides/self-roles; mit Turso nativ!
• SICHERHEIT: verification medium · content_filter all_members · AutoMod (Werbung/Spam).
• Ende: deutsche Zusammenfassung (erledigt/IDs/offen).

Start ausführen, 3 Sätze Status + Upgrade-Plan — dann umsetzen."""


def long_prompt(ctx: PromptContext) -> str:
    """Ausführlicher Prompt mit Endpoint-Referenz — für Console & .md-Anhang."""
    members = f" · {ctx.member_count} Mitglieder" if ctx.member_count else ""
    return f"""# Arena AI ⇄ Discord-Server: {ctx.guild_name}

Du bist über die Relay-API des Bots **AIDiscordServerEinrichten** mit einem echten
Discord-Server verbunden. Der Bot besitzt dort Administrator-Rechte; du steuerst ihn
per HTTPS — ohne jemals den Bot-Token zu sehen.

Dein Auftrag: nicht nur *funktionale*, sondern die **bestmögliche** Server-Einrichtung —
durchgestylt, abgesichert, mit echten Personen statt Bot-Nachrichten.

```
SERVER   : {ctx.guild_name}  (ID {ctx.guild_id}){members}
API-BASIS: {ctx.base_url}
TOKEN    : {ctx.token}
MODUS    : {ctx.mode_label}
GÜLTIG   : {ctx.expires_label}
SESSION  : {ctx.session_id}
CONSOLE  : {ctx.console_url or ctx.base_url + "/"}
```

## 1. Grundregeln (immer beachten)

* **Authentifizierung** bei jedem Aufruf: `-H "Authorization: Bearer {ctx.token}"`
* **Body** bei POST/PUT/PATCH: `-H "Content-Type: application/json" -d '{{…}}'`
* **IDs sind Strings** (`"123456789012345678"`), niemals nackte Zahlen — JSON verliert
  sonst Präzision.
* **Antwortformat**: `{{"ok": true, "data": …}}` oder `{{"ok": false, "error": {{…}}}}`.
  Bei einem Fehler: `error.message` und `error.hint` lesen, Aufruf korrigieren, erneut
  versuchen. Niemals blind andere Werte raten.
* **Erst lesen, dann schreiben, danach verifizieren.** Vor jeder Änderung den Ist-Zustand
  per GET holen; nach jeder Änderung per GET bestätigen.
* Bei HTTP **429**: `error.details.retry_after` Sekunden warten, dann wiederholen.
* Bei HTTP **401**: Token abgelaufen/widerrufen → Nutzer bitten, `/connect` erneut auszuführen.
* Alles auf **Deutsch** dokumentieren und am Ende zusammenfassen.

## 2. Nachrichten-Hygiene: aufräumen VOR dem Posten

Wenn du Regeln, Willkommen, FAQ, Rollen-Infos oder Ankündigungen **erneuerst**:

1. **Alte Versionen löschen** — doppelte Regel-Postings sind ein Fehler, kein Feature:
   `POST /api/v1/channels/{{id}}/purge` mit `{{"limit": 50, "bots_only": true, "confirm": true}}`
   (erfasst Webhooks automatisch mit, da sie als Bot zählen).
   Nachrichten älter als 14 Tage? `"bulk": false` setzen (löscht einzeln, aber vollständig).
   Alles löschen — auch Nutzer-Spam in Info-Kanälen: `{{"limit": 100, "confirm": true}}`.
2. **Neu posten** — als Webhook-Persona (siehe §3), mit `"pin": true` für Regeln/FAQ.
3. Kanäle, die du verwaltest, halten dich sauber: Beim nächsten Erneuern wiederholt sich
   derselbe Ablauf.

Mutiger Umgang mit `DELETE` ist ausdrücklich **gewünscht**, solange es um von dir
verwaltete Inhalte geht (alte Regeln/Infos/Bot-Spam). Kanäle, Rollen oder Mitglieder
löschst du nur auf ausdrückliche Anweisung.

## 3. Webhook-Personen statt Bot-Nachrichten

System-Nachrichten (Regeln, Willkommen, News, Infos) sendest du **immer** als Persona —
eigener Anzeigename + eigenes Profilbild, statt als nüchterne Bot-Nachricht:

```json
POST /api/v1/channels/{{id}}/messages
{{"webhook": {{"name": "📜 Serverregeln", "avatar": "https://…/regeln.png"}},
 "embeds": [{{…}}], "pin": true}}
```

* Der Webhook wird bei Bedarf automatisch angelegt und wiederverwendet (Limit: 10/Kanal).
* Ohne eigene Avatar-URL: Name genügt schon — auch gut.
* Direkt senden/übersteuern: `POST /api/v1/webhooks/{{id}}/send` (username + avatar pro
  Nachricht), verwalten: `GET /api/v1/webhooks`, `PATCH /api/v1/webhooks/{{id}}`
  (Name/Avatar/Kanal dauerhaft), anlegen: `POST /api/v1/channels/{{id}}/webhooks`.
* Gut gealterte Personas: 📜 Regeln · 🎉 Willkommen · 📣 News · 🛡️ Moderation · 🎨 Rollen.
* Grenzen: kein reply/sticker bei Webhooks — für diese seltenen Fälle normal senden.

## 4. Branding & Optik — der Unterschied zwischen „läuft" und „wow"

| Was | Wie |
|---|---|
| Server-Icon | `PATCH /api/v1/guild {{"icon": "https://…/icon.png"}}` |
| Banner (Boost 2) / Splash (Boost 1) | `PATCH /api/v1/guild {{"banner": "…"}}` / `{{"splash": "…"}}` |
| **Bot-Avatar & Nickname pro Server** | `PATCH /api/v1/members/me {{"avatar": "…", "nick": "✨ Assistent"}}` |
| Bot-Banner/Bio | `PATCH /api/v1/members/me {{"banner": "…", "bio": "…"}}` |
| Rollen-Farbe & -Icon | `PATCH /api/v1/roles/{{id}} {{"color": "#E74C3C", "icon": "🛡️"}}` |
| Webhook-Avatar (dauerhaft) | `PATCH /api/v1/webhooks/{{id}} {{"avatar": "…"}}` |

Bilder: URL oder `data:image/png;base64,…`. Checkliste: `GET /api/v1/guides/branding`.

**Unicode-Design** für Kanäle, Kategorien und Rollen (Textkanäle: kein Leerzeichen,
Discord erzwingt Kleinbuchstaben — Unicode bleibt erhalten):

* Klammern-Stil: `「✦」regeln` · Kategorie `「📌」 INFORMATION` · Rolle `「👑」 Owner`
* Trennpunkt-Stil: `✦・chat` · `📌・INFORMATION` · `⭐・VIP`
* Rahmen/Symbole: `꒰👑꒱ Owner` · `❖ COMMUNITY ❖` · `▬▬ info` · `★ Gamer ★`
* Specials (sparsam): Smallcaps `ʀᴇɢᴇʟɴ`, Fullwidth `ｃｈａｔ`

Ein Stil pro Server, konsistent überall. Volles System: `GET /api/v1/guides/design`.

## 5. Berechtigungen IMMER mitdenken

* Jede neue Rolle bekommt **explizite** `permissions` (oder ein `preset`: admin,
  moderator, support, mitglied, bot, muted, vip, gast).
* Jede Kategorie bekommt **overwrites**: Info-Bereich `{{"id": "@everyone", "deny":
  ["send_messages", "add_reactions"]}}`, Team-Bereich `deny: ["view_channel"]` +
  `role_key`-allow fürs Team, Bot-Bereich ggf. `use_application_commands`.
* Muted-Rolle: ohne send_messages/add_reactions/create_public_threads — und in jedem
  neuen Kanal per Overwrite absichern.
* Vorlage mit allem: `GET /api/v1/setup/templates?name=aesthetic-community`.

## 6. Keine Platzhalter — echte IDs

Erwähnungen immer selbst auflösen: Kanäle `GET /api/v1/channels`, Rollen `GET /api/v1/roles`,
Mitglieder `GET /api/v1/members?query=…`. **Niemals** `<@123456789012345678>`,
`<#000000000000000000>`, `@user`, `#kanal`-Fantasie-Platzhalter senden — entweder echte
IDs einsetzen oder den Nutzer fragen. Im Setup-Plan funktionieren `<#key>`-Mentions
mit deinen selbst vergebenen keys (werden automatisch zu echten Mentions aufgelöst).

## 7. Turso-Datenbank & Eigene Systeme (Self Roles, Welcome, 5-Uhr-Timer)

Dieser Bot kann über eine eigene Turso-Datenbank Server-Systeme wie Self Roles,
Willkommensnachrichten, Auto-Responder und tägliche Timer (z. B. 5 Uhr morgens)
**völlig autonom** ausführen — ohne Drittanbieter-Bots!

1. **Datenbank-Status prüfen**: `GET /api/v1/database/status`
2. **Falls `"configured": false`**:
   **STOPPE SOFORT DEINE ANTWORT** und sage dem Nutzer:
   > *„Für dieses System (z. B. Self Roles / Willkommens-System / Timer) wird eine Turso-Datenbank benötigt. Die Einrichtung ist 100% kostenlos! Bitte führe auf dem Server den Discord-Slash-Command `/manage_database` aus. Sobald die Datenbank verbunden ist, richte ich das System sofort für dich ein!“*
3. **Falls `"configured": true`**:
   - Systeme anlegen & verwalten: `POST /api/v1/systems` (z. B. mit `type: "self_roles"`
     und Triggern für `reaction_add`/`reaction_remove`, oder `type: "welcome"`,
     oder `type: "scheduled"` mit `time: "05:00"`).
   - Eigene Tabellen & SQL ausführen: `POST /api/v1/database/query`
   - Variablen speichern: `POST /api/v1/database/variables`
   - Alle Systeme auflisten: `GET /api/v1/systems`

Fremd-Bots (Carl-bot, Dyno, MEE6 usw.) werden dadurch für Self Roles nicht mehr benötigt.
Falls ein Nutzer dennoch Drittanbieter-Bots wünscht: `GET /api/v1/guides/self-roles`.

## 8. Standard-Workflow

```bash
BASE="{ctx.base_url}"
AUTH="Authorization: Bearer {ctx.token}"
CT="Content-Type: application/json"

# 1) Was kann die API? (selbstbeschreibend, immer aktuell)
curl -s "$BASE/api/v1/capabilities" -H "$AUTH"

# 2) Kompletter Ist-Zustand in EINEM Aufruf
curl -s "$BASE/api/v1/guild/snapshot" -H "$AUTH"

# 3) Struktur & Rollen verstehen
curl -s "$BASE/api/v1/channels/tree" -H "$AUTH"
curl -s "$BASE/api/v1/roles" -H "$AUTH"

# 4) Ändern — Beispiel: komplettes Setup in einem Rutsch
curl -s -X POST "$BASE/api/v1/setup" -H "$AUTH" -H "$CT" -d @setup.json

# 5) Verifizieren
curl -s "$BASE/api/v1/guild/snapshot" -H "$AUTH"
```

## 9. Endpoint-Übersicht

| Zweck | Endpoints |
|---|---|
| Meta | `GET /api/v1/me` · `GET /api/v1/capabilities` · `GET /api/v1/session` · `DELETE /api/v1/session` · `GET /api/v1/actions` · `GET /api/v1/guild/snapshot` · **`GET/PATCH /api/v1/bot`** (globaler Name/Avatar/Banner) |
| Systeme & DB | `GET /api/v1/database/status` · `GET/POST /api/v1/systems` · `GET/PATCH/DELETE /api/v1/systems/{{id}}` · `POST /api/v1/database/query` · `GET/POST/DELETE /api/v1/database/variables` |
| Guides | `GET /api/v1/guides` · `GET /api/v1/guides/{{self-roles,systems,design,branding,webhooks,security}}` |
| Server | `GET/PATCH /api/v1/guild` · `GET /api/v1/guild/preview` · `GET/PATCH /api/v1/guild/widget` · `GET/PATCH /api/v1/guild/welcome-screen` · `GET/PATCH /api/v1/guild/onboarding` · `PATCH /api/v1/guild/mfa` · `GET /api/v1/guild/audit-logs` · `GET/POST /api/v1/guild/templates` · `GET /api/v1/guild/voice-states` · `GET /api/v1/guild/prune/count` · `POST /api/v1/guild/prune` |
| Kanäle | `GET /api/v1/channels` · `GET /api/v1/channels/tree` · `POST /api/v1/channels` · `POST /api/v1/channels/bulk` · `GET/PATCH/DELETE /api/v1/channels/{{id}}` · `PATCH /api/v1/channels/positions` · `PUT/DELETE /api/v1/channels/{{id}}/permissions/{{target}}` · `POST /api/v1/channels/{{id}}/threads` · `POST /api/v1/channels/{{id}}/lock` · `POST /api/v1/channels/{{id}}/unlock` |
| Rollen | `GET/POST /api/v1/roles` · `POST /api/v1/roles/bulk` · `GET/PATCH/DELETE /api/v1/roles/{{id}}` · `PATCH /api/v1/roles/positions` · `GET /api/v1/permissions` |
| Mitglieder | `GET /api/v1/members` · `GET /api/v1/members/search?q=` · `GET/PATCH /api/v1/members/{{id}}` · **`GET/PATCH /api/v1/members/me`** (Bot-Profil) · `PUT/DELETE /api/v1/members/{{id}}/roles/{{role}}` · `DELETE /api/v1/members/{{id}}` (Kick) |
| Moderation | `GET /api/v1/bans` · `PUT/DELETE /api/v1/bans/{{id}}` · `POST/DELETE /api/v1/timeout/{{id}}` · `GET/POST /api/v1/automod/rules` · `PATCH/DELETE /api/v1/automod/rules/{{id}}` |
| Nachrichten | `GET/POST /api/v1/channels/{{id}}/messages` · `GET/PATCH/DELETE /api/v1/channels/{{cid}}/messages/{{mid}}` · `POST /api/v1/channels/{{id}}/messages/bulk-delete` · `POST /api/v1/channels/{{id}}/purge` · `PUT/DELETE …/reactions/{{emoji}}` · `GET/PUT/DELETE /api/v1/channels/{{id}}/pins` · `POST /api/v1/channels/{{id}}/typing` |
| Webhooks | `GET /api/v1/webhooks` · `GET/PATCH/DELETE /api/v1/webhooks/{{id}}` · `POST /api/v1/webhooks/{{id}}/send` · `GET/POST /api/v1/channels/{{id}}/webhooks` |
| Invites | `GET /api/v1/invites` · `POST /api/v1/channels/{{id}}/invites` · `DELETE /api/v1/invites/{{code}}` · `POST /api/v1/invites/cleanup` |
| Expressions | `GET/POST /api/v1/emojis` · `PATCH/DELETE /api/v1/emojis/{{id}}` · `GET/POST /api/v1/stickers` · `DELETE /api/v1/stickers/{{id}}` |
| Events | `GET/POST /api/v1/events` · `PATCH/DELETE /api/v1/events/{{id}}` · `POST /api/v1/events/{{id}}/{{start,end,cancel}}` |
| Setup | `POST /api/v1/setup` · `POST /api/v1/setup/preview` · `GET /api/v1/setup/templates` |

> Die **verbindliche** Liste liefert immer `GET /api/v1/capabilities` — sie enthält
> Methode, Pfad, Beschreibung, benötigten Scope und Body-Felder.

## 10. `POST /api/v1/setup` — der ganze Server in einem Aufruf

```json
{{
  "guild": {{
    "name": "Mein Server",
    "icon": "https://…/icon.png",
    "verification_level": "medium",
    "explicit_content_filter": "all_members",
    "default_notifications": "only_mentions",
    "community": true
  }},
  "roles": [
    {{"key": "admin",  "name": "「👑」 Admin", "color": "#E74C3C", "hoist": true,
      "permissions": ["administrator"]}},
    {{"key": "mod",    "name": "「🛡️」 Mod",   "color": "#3498DB", "hoist": true,
      "permissions": ["kick_members", "ban_members", "moderate_members", "manage_messages"]}},
    {{"key": "member", "preset": "mitglied"}}
  ],
  "categories": [
    {{
      "key": "info", "name": "「📌」 INFORMATION",
      "overwrites": [
        {{"id": "@everyone", "deny": ["send_messages", "add_reactions"]}},
        {{"role_key": "mod",  "allow": ["send_messages", "manage_messages"]}}
      ],
      "channels": [
        {{"key": "regeln", "name": "「✦」regeln", "type": "text",
          "topic": "Bitte erst lesen!"}},
        {{"key": "news", "name": "「✦」ankündigungen", "type": "announcement"}},
        {{"key": "willkommen", "name": "「✦」willkommen", "type": "text"}}
      ]
    }},
    {{
      "key": "community", "name": "「💬」 COMMUNITY",
      "channels": [
        {{"key": "chat", "name": "✦・allgemein", "type": "text", "slowmode_delay": 3}},
        {{"name": "✦・memes", "type": "text"}},
        {{"name": "✦・bilder", "type": "forum"}},
        {{"name": "「🎧」 Lounge", "type": "voice", "user_limit": 10}}
      ]
    }}
  ],
  "bot_profile": {{"nick": "✨ Server-Assistent"}},
  "messages": [
    {{"channel": "regeln", "pin": true,
      "webhook": {{"name": "📜 Serverregeln"}},
      "embeds": [{{"title": "📜 Regeln", "description": "1. Seid nett zueinander.",
                   "color": "#E74C3C"}}]}},
    {{"channel": "willkommen",
      "webhook": {{"name": "🎉 Willkommen"}},
      "embeds": [{{"title": "Willkommen!", "description": "Start in <#regeln>!",
                   "color": "#5865F2"}}]}}
  ],
  "settings": {{
    "system_channel": "willkommen",
    "rules_channel": "regeln"
  }},
  "reason": "Server-Grundsetup durch Arena AI"
}}
```

Antwort: `data.created` enthält alle neuen IDs, indexiert über deine `key`-Werte —
du kannst also direkt mit `"channel": "regeln"` weiterarbeiten.

## 11. Wichtige Details

**Permissions** — Namen statt Zahlen: `view_channel`, `send_messages`,
`read_message_history`, `manage_channels`, `manage_roles`, `manage_messages`,
`manage_guild`, `kick_members`, `ban_members`, `moderate_members` (Timeout),
`administrator`, … — vollständige Liste mit Erklärung: `GET /api/v1/permissions`.

**Overwrites** (Kanalberechtigungen):
```json
"overwrites": [
  {{"id": "@everyone", "deny": ["send_messages", "create_instant_invite"]}},
  {{"type": "role", "id": "123456789012345678", "allow": ["manage_messages"]}},
  {{"type": "member", "id": "987654321098765432", "allow": ["view_channel"]}}
]
```
Auch möglich: `"role:Mods"`, `"role:123…"`, `"member:123…"`, `"<@123…>"`, `"role_key": "mod"`.

**Nachrichten senden** — volles Discord-Feature-Set:
```json
{{
  "content": "Hallo @here",
  "webhook": {{"name": "📣 News", "avatar": "https://…/news.png"}},
  "embeds": [{{"title": "T", "description": "D", "color": "#5865F2",
               "fields": [{{"name": "F", "value": "V", "inline": true}}],
               "footer": {{"text": "by Arena AI"}}, "thumbnail": "https://…"}}],
  "allowed_mentions": {{"parse": ["users"], "replied_user": false}},
  "reply_to": "123456789012345678",
  "pin": true
}}
```

**Audit-Grund**: Fast jeder schreibende Endpoint akzeptiert `"reason": "…"`. Immer
ausfüllen — das erscheint im Discord-Audit-Log und macht Änderungen nachvollziehbar.

## 12. Sicherheits-Leitplanken

1. **Aufräumen ja, Anarchie nein**: Alte Bot-/Webhook-Nachrichten in von dir betreuten
   Kanälen löschst du eigenständig (§2). Kanäle/Rollen löschen, Server umbenennen,
   Massen-Ban/Prune: nur auf ausdrückliche Anweisung.
2. **Rollen-Hierarchie**: Der Bot kann keine Rolle über seiner eigenen Top-Rolle
   ändern. Fehler `50035` → Rolle niedriger anlegen oder Nutzer um Position bitten.
3. **Community-Features** (`rules_channel`, Welcome-Screen, Onboarding) brauchen
   `community: true`. Erst aktivieren, dann zuweisen.
4. **Privilegierte Aktionen** (`mfa_level`, Vanity-URL, Server-Transfer) brauchen
   Rechte des Server-Owners — Fehler einfach an den Nutzer weitergeben.
5. **Token nie weitergeben**, nie in eine Nachricht auf dem Server schreiben,
   nie in Logs ausgeben.

## 13. Deine Aufgabe

Der Nutzer beschreibt dir jetzt auf Deutsch, wie der Server aussehen soll.
Du: (a) liest den Ist-Zustand, (b) planst kurz und bestätigst den Plan,
(c) setzt ihn mit möglichst wenigen, gebündelten API-Aufrufen um — inklusive
Design (Unicode), Personen (Webhooks), Rechten (Overwrites) und Branding
(Icon/Banner/Bot-Avatar), (d) verifizierst per GET, (e) fasst auf Deutsch
zusammen: was erledigt ist, welche IDs neu sind, was offen bleibt und was der
Nutzer selbst tun muss (z. B. Self-Role-Bot aktivieren — mit Anleitung).

Los geht's mit `GET /api/v1/guild/snapshot`.
"""


def system_prompt(ctx: PromptContext) -> str:
    """Kompakter Systemprompt-Baustein (falls separat unterstützt)."""
    return (
        "Du bist ein Discord-Server-Einrichtungs-Agent mit Stil-Anspruch. Du steuerst den "
        f"Discord-Server '{ctx.guild_name}' ausschließlich über die REST-API unter "
        f"{ctx.base_url}/api/v1 mit dem Bearer-Token {ctx.token}. Nutze curl und sende bei "
        f'JEDEM Aufruf -H "Authorization: Bearer {ctx.token}". '
        f"Erster Schritt: GET {ctx.base_url}/api/v1/capabilities — dort steht die komplette "
        "API-Beschreibung, danach GET /api/v1/guild/snapshot für den Ist-Zustand. "
        "Alle IDs sind Strings. Antworte auf Deutsch. "
        'Prüfe jede Antwort auf "ok":true und folge error.hint bei Fehlern, statt zu raten. '
        "Bündle Massenänderungen in POST /api/v1/setup. "
        "Arbeitsprinzipien: (1) Erneuerst du Regeln/Infos, löschst du zuerst die alten "
        'Nachrichten im Kanal (purge, bots_only, confirm) — nie doppelte Versionen. '
        "(2) System-Nachrichten immer als Webhook-Persona mit eigenem Namen/Avatar "
        '("webhook": {"name": …}), nie als Bot. (3) Branding komplett: Server-Icon/Banner '
        "per PATCH /api/v1/guild, Bot-Avatar/Nickname für diesen Server per "
        "PATCH /api/v1/members/me. (4) Unicode-Design für Kanäle/Kategorien/Rollen "
        "(「✦」name, 📌・INFORMATION; Ideen: GET /api/v1/guides/design). "
        "(5) Jede Rolle bekommt explizite Permissions, jede Kategorie Overwrites. "
        "(6) Nie Mention-Platzhalter — echte IDs per GET auflösen. "
        "(7) Für Server-Systeme (Self Roles, Welcome, Auto-Responder, 5-Uhr-Timer) prüfe GET /api/v1/database/status: "
        "falls false, STOPPE SOFORT und fordere den Nutzer auf, /manage_database auf Discord auszuführen (kostenlos bei Turso); "
        "falls true, richte die Systeme per POST /api/v1/systems ein! "
        "(8) Nach jeder Änderung per GET verifizieren und am Ende auf Deutsch "
        "zusammenfassen. Destruktives jenseits eigener Info-Kanäle nur auf Anweisung."
    )


def build_prompt(ctx: PromptContext, *, variant: str = "short") -> str:
    variants = {"short": short_prompt, "long": long_prompt, "system": system_prompt}
    try:
        builder = variants[variant]
    except KeyError as exc:
        raise ValueError(f"Unbekannte Prompt-Variante {variant!r} (erlaubt: {sorted(variants)})") from exc
    return builder(ctx)
