"""
Der Discord-Teil: Slash-Commands, Buttons und Rechteprüfungen.

``/connect`` ist *der* Command — bewusst **ohne Optionen**. Dauer und Modus
waren Stolperfallen; jetzt gelten die Defaults aus der Konfiguration
(24 h Gültigkeit, Modus „Lesen + Schreiben“ — alles per Umgebungsvariablen
änderbar).

Die Antwort ist eine **ephemerale Container-V2-Nachricht** (Discord
*Components V2*) mit genau einem Container, der zwei Zustände hat:

**Noch nicht verbunden** — die Bridge ist für diesen Server deaktiviert:

* ``# Willkommen!`` + kurzer Einstiegstext
* ein grüner **Verbinden**-Button: erzeugt das Sitzungs-Token und bearbeitet
  dieselbe Nachricht in den verbundenen Zustand

**Verbunden**:

* ``# Willkommen!`` — „Du kannst sofort mit Arena AI losarbeiten.“
* **1. Kopiere diesen Prompt** — ein Mini-Prompt im Codeblock: nur URL und
  Token. Alles Weitere (Regeln, Workflow, Endpoints) erfährt Arena AI *während
  der Arbeit* von der Bridge selbst (``GET /api/v1/capabilities`` samt
  Konventionen und den ``/guides``-Endpoints).
* **2. Öffne Arena AI …** — inklusive Link-Button direkt zum Arena-Agenten
* **Weitere Optionen** — **Verbindung trennen** (rot: widerruft *alle* Tokens
  dieses Servers) und **Neues Token generieren** (macht das alte sofort
  ungültig und zeigt den aktualisierten Prompt)

Die Buttons sind persistent (``timeout=None`` + Registrierung in
``setup_hook``): Auch nach einem Render-Deploy reagieren alte Nachrichten noch.
Der Prompt darin ist bewusst ein **einzeiliger** Codeblock — Discord-Mobile
kopiert einzeilige Codeboxen mit einem einzigen Tipp, mehrzeilige nicht.

Der Zugriff wird direkt in ``/connect`` über die Buttons verwaltet; separate
``/status``- und ``/revoke``-Commands gibt es nicht.

**Owner-Features** (``BOT_OWNER_ID``): Die Presence zeigt live
``/connect | 👀 N eingerichtete Server``. Im Privatchat mit dem Bot gibt es
``/adminpanel`` — eine durchsuchbare, blätterbare Server-Liste (sortiert nach
Mitgliederzahl; Server, auf denen der Owner **nicht** Mitglied ist, stehen mit
❗️ ganz oben). Die Server-Übersicht zeigt zusätzlich Kanäle, Rollen, Emojis,
Boosts, Erstellungsdatum, Bot-Rechte und den Datenbank-Status. Der Owner
bekommt zusätzlich eine DM, wenn der Bot einem Server beitritt oder ihn
verlässt — der Grund (Kick/Ban inkl. Ausführendem) wird jetzt **live über das
Gateway** mitgelesen (``GUILD_AUDIT_LOG_ENTRY_CREATE``), solange der Bot noch
Mitglied ist, und landet so zuverlässig in der DM. Beim Verlassen eines
Servers werden außerdem alle Daten des Servers gelöscht: aktive KI-Sitzungen
werden gestoppt und die Datenbank-Verknüpfung entfernt (exklusive
Server-Datenbanken werden komplett geleert, geteilte bleiben für die anderen
Server bestehen). Der Server-Owner erhält beim Beitritt eine Willkommens-DM
mit der Kurzanleitung.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands

from . import __version__
from .config import Config, mask_proxy_url
from .sessions import DEFAULT_MODE, SessionStore
from .util import ApiError, now_utc

log = logging.getLogger("relay.discord")

__all__ = (
    "RelayClient", "build_intents", "ADMIN_INVITE_SCOPES", "ARENA_URL",
    "connected_view", "welcome_view", "admin_panel_view",
    "admin_guild_detail_view", "admin_guild_leave_confirm_view",
    "database_unconfigured_view", "database_configured_view",
)

ADMIN_INVITE_SCOPES = ("bot", "applications.commands")

#: Mindestabstand zwischen zwei globalen Slash-Command-Syncs (Sekunden).
COMMAND_SYNC_MIN_INTERVAL = 3600.0

OK_COLOR = discord.Color.from_str("#2ECC71")
WARN_COLOR = discord.Color.from_str("#F1C40F")
ERR_COLOR = discord.Color.from_str("#E74C3C")

#: Ziel des „Arena AI öffnen“-Link-Buttons.
ARENA_URL = "https://arena.ai/agent"

#: Akzentfarbe der Container: Blurple fürs Willkommen, Grün für verbunden.
WELCOME_ACCENT = discord.Color.from_str("#5865F2")
CONNECTED_ACCENT = discord.Color.from_str("#2ECC71")

# Custom-IDs der persistenten Buttons — sie überleben einen Neustart.
CID_CONNECT = "relay:v2_connect"
CID_DISCONNECT = "relay:v2_disconnect"
CID_REGENERATE = "relay:v2_regenerate"

# Turso-Datenbank Buttons
CID_DB_CONNECT = "relay:v2_db_connect"
CID_DB_DISCONNECT = "relay:v2_db_disconnect"
CID_DB_REFRESH = "relay:v2_db_refresh"
CID_DB_SYSTEMS = "relay:v2_db_systems"
CID_DB_SYS_DEL = "relay:v2_db_sys_del"
#: Blauer Wiederverwenden-Button: eine auf einem anderen Server bereits
#: eingerichtete Datenbank auch für diesen Server nutzen. Die Custom-ID
#: trägt die Quell-Guild-ID: ``relay:v2_db_reuse:<guild_id>``.
CID_DB_REUSE = "relay:v2_db_reuse"

# Legacy-IDs der Buttons vor dem Components-V2-Upgrade. Nachrichten aus der
# Zeit vor dem Deploy sollen nicht ins Leere laufen: Ihre Klicks werden auf
# die neuen Handler gemappt (die bearbeiten die alte Nachricht dann in die
# neue Optik).
CID_LEGACY_REGENERATE = "relay:regenerate"
CID_LEGACY_REVOKE_ALL = "relay:revoke_all"

#: Adminpanel: Buttons + Suchen-Modal. Die Custom-IDs tragen die Ziel-Seite
#: und den Suchbegriff in sich (``relay:admin:nav:<seite>:<suche>``), damit
#: auch alte Panel-Nachrichten nach einem Neustart noch bedienbar sind.
CID_ADMIN_PREFIX = "relay:admin:"
CID_ADMIN_SEARCH = "relay:admin:search"

#: Wie viele Server das Adminpanel pro Seite anzeigt.
ADMIN_PAGE_SIZE = 10

#: Max. Länge des Suchbegriffs im Adminpanel — die Custom-IDs der Blätter-
#: Buttons transportieren ihn mit und sind auf 100 Zeichen begrenzt.
ADMIN_QUERY_MAX = 40

#: Wie lange (Sekunden) ein per Gateway empfangener Kick/Ban-Grund auf die
#: zugehörige ``on_guild_remove``-Nachricht wartet. Discord liefert den
#: Audit-Log-Eintrag typischerweise *vor* GUILD_DELETE; durch das Zeitfenster
#: funktioniert es auch umgekehrt.
PENDING_LEAVE_REASON_TTL = 600.0


def build_intents(privileged: bool = True) -> discord.Intents:
    """
    Intents zusammenstellen.

    ``members`` und ``message_content`` sind **privilegiert** und müssen im
    Developer Portal aktiviert sein. Ist ``privileged=False``, wird ohne sie
    gestartet — der Bot läuft dann weiter, nur die Mitgliederliste ist ggf.
    unvollständig (die API weicht automatisch auf REST/Gateway-Suche aus).
    """
    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = privileged
    intents.message_content = privileged
    intents.guild_messages = True
    intents.invites = True
    intents.emojis_and_stickers = True
    return intents


# ─────────────────────────────────────────────────────────────────────────────
#  Container-V2-Nachrichten (Components V2)
# ─────────────────────────────────────────────────────────────────────────────


def _bridge_prompt_line(base_url: str, token: str) -> str:
    """
    Der Mini-Prompt für Arena AI: **nur** URL und Token — ohne Leerzeichen.

    Das maschinenlesbare ``URL=…;TOKEN=…``-Format ist trotz seiner Kompaktheit
    eindeutig und lässt sich von Arena zuverlässig auswerten.

    Der komplette Regel- und Workflow-Katalog (Hygiene, Personas, Branding,
    Unicode-Design …) muss der Nutzer nicht mehr kopieren — die Bridge erklärt
    sich Arena AI selbst, sobald diese arbeitet: ``GET /api/v1/capabilities``
    liefert Konventionen und Endpoints, dazu die ``/api/v1/guides/*``-Texte.
    """
    return f"URL={base_url};TOKEN={token}"


#: Variation Selector-16 (U+FE0F) erzwingt die *emoji-Darstellung* eines
#: Zeichens (statt Text) und ist Teil vieler „richtiger“ Emoji-Strings, die
#: Editoren/Copy-Paste einfügen (z. B. ``❗️`` = U+2757 + U+FE0F). Discord
#: akzeptiert diese Sequenz bei ``Button.emoji`` klaglos, lehnt sie aber bei
#: ``SelectOption.emoji`` mit ``400 Invalid Form Body — Invalid emoji`` ab
#: (bekannte Inkonsistenz der Discord-API, siehe discord-api-docs #5511).
#: Genau das war die Ursache dafür, dass ``/adminpanel`` in Produktion mit
#: „Die Anwendung reagiert nicht“ endete, sobald mindestens ein ❗️-Eintrag
#: (Server ohne Bot-Owner als Mitglied) in der Liste stand — die Server-Suche
#: nach dem Root-Cause bestätigte per Render-Log exakt diesen Fehler:
#: ``components.0.components.5.components.0.options.0.emoji.name: Invalid emoji``.
_VARIATION_SELECTORS = "\ufe0e\ufe0f"


def _select_safe_emoji(emoji: str) -> str:
    """Entfernt Variation-Selectoren aus einem Emoji für ``discord.SelectOption``.

    Nur für Select-Menü-Optionen nötig — Buttons vertragen die volle
    Emoji-Sequenz inklusive Variation-Selector problemlos.
    """
    return emoji.translate({ord(c): None for c in _VARIATION_SELECTORS})


def welcome_view() -> discord.ui.LayoutView:
    """Container-V2-Nachricht für „Bridge noch deaktiviert“."""
    container = discord.ui.Container(
        discord.ui.TextDisplay(
            "# Willkommen!\n"
            "Du kannst sofort mit Arena AI losarbeiten. Die Bridge für diesen "
            "Server ist aktuell noch deaktiviert. Um zu beginnen, klicke auf "
            "diesen Button:"
        ),
        discord.ui.Separator(),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Verbinden",
                emoji="🔌",
                style=discord.ButtonStyle.success,
                custom_id=CID_CONNECT,
            )
        ),
        accent_colour=WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def connected_view(base_url: str, token: str) -> discord.ui.LayoutView:
    """Container-V2-Nachricht für „Bridge aktiv“: Prompt, Arena-Link, Optionen."""
    container = discord.ui.Container(
        discord.ui.TextDisplay(
            "# Willkommen!\n"
            "Du kannst sofort mit Arena AI losarbeiten."
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**1. Kopiere diesen Prompt:**\n"
            # Bewusst EIN einzeiliger Inline-Codeblock mit einfachen Backticks:
            # Discord-Mobile kopiert diesen mit einem einzigen Tipp.
            f"`{_bridge_prompt_line(base_url, token)}`"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**2. Öffne Arena AI, schreib ihm was er auf deinem Server "
            "einrichten soll und schick ihm den Prompt:**"
        ),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Arena AI öffnen",
                emoji="🤖",
                style=discord.ButtonStyle.link,
                url=ARENA_URL,
            )
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay("**Weitere Optionen:**"),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Verbindung trennen",
                emoji="⛔",
                style=discord.ButtonStyle.danger,
                custom_id=CID_DISCONNECT,
            ),
            discord.ui.Button(
                label="Neues Token generieren",
                emoji="🔄",
                style=discord.ButtonStyle.secondary,
                custom_id=CID_REGENERATE,
            ),
        ),
        accent_colour=CONNECTED_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def database_unconfigured_view(
    reusable: Optional[List[Dict[str, Any]]] = None,
) -> discord.ui.LayoutView:
    """
    Container-V2-Nachricht für /manage_database wenn noch keine DB verknüpft ist.

    Hat der ausführende Admin auf einem **anderen** Server bereits eine
    Datenbank eingerichtet (und sie dort noch aktiv), erscheint neben dem
    grünen „Datenbank verknüpfen“-Button ein **blauer** Wiederverwenden-Button
    pro gespeicherter Datenbank — ein Klick verbindet diesen Server mit der
    bestehenden Datenbank, ohne dass URL und Token neu eingetippt werden müssen.
    """
    reusable = [r for r in (reusable or []) if r.get("source_guild_id") is not None][:4]

    intro_text = (
        "# 🗄️ Turso-Datenbank einrichten\n"
        "Für diesen Server ist noch keine Turso-Datenbank verknüpft.\n\n"
        "**Warum Turso?**\n"
        "Mit Turso kann Arena AI eigene Server-Systeme völlig autonom für dich "
        "scripten: **Self Roles / Reaction Roles**, **Willkommensnachrichten & Auto-Rollen**, "
        "**Auto-Responder**, **Leveling** und **tägliche Timer** (z. B. um 5 Uhr morgens) — "
        "komplett ohne Drittanbieter-Bots!\n\n"
        "**100% Kostenlos:**\n"
        "Turso bietet im kostenlosen Free-Plan **9 GB Speicher** und **1 Milliarde Abfragen/Monat** "
        "(keine Kreditkarte erforderlich).\n\n"
        "**Schritt-für-Schritt Anleitung:**\n"
        "1️⃣ Erstelle einen Account auf https://turso.tech (Free Tier)\n"
        "2️⃣ Erstelle eine neue Datenbank (Name frei wählbar, z.B. für diesen Server)\n"
        "3️⃣ Kopiere die **Database URL** (`https://...` oder `libsql://...`)\n"
        "4️⃣ Erzeuge ein **Auth Token** und kopiere es\n"
        "5️⃣ Klicke unten auf **„Datenbank verknüpfen“** und füge beides ein!"
    )
    if reusable:
        names = ", ".join(f"**{r.get('source_guild_name') or 'Server'}**" for r in reusable)
        intro_text += (
            f"\n\n♻️ **Bereits eingerichtet:** Du hast auf {names} schon eine Datenbank "
            "verknüpft — mit dem blauen Button kannst du sie direkt auch für diesen Server "
            "verwenden (mehrere Server können sich eine Datenbank teilen)."
        )

    buttons = [
        discord.ui.Button(
            label="Datenbank verknüpfen",
            emoji="🗄️",
            style=discord.ButtonStyle.success,
            custom_id=CID_DB_CONNECT,
        ),
    ]
    for entry in reusable:
        source_name = entry.get("source_guild_name") or "anderem Server"
        buttons.append(
            discord.ui.Button(
                label=f"DB von „{source_name}“ nutzen"[:80],
                emoji="♻️",
                style=discord.ButtonStyle.primary,  # blau
                custom_id=f"{CID_DB_REUSE}:{entry['source_guild_id']}",
            ),
        )

    container = discord.ui.Container(
        discord.ui.TextDisplay(intro_text),
        discord.ui.Separator(),
        discord.ui.ActionRow(*buttons),
        accent_colour=WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def database_configured_view(
    status_data: Dict[str, Any], *, notice: Optional[str] = None,
) -> discord.ui.LayoutView:
    """
    Container-V2-Nachricht für /manage_database wenn die DB bereits aktiv ist.

    ``notice`` ist eine optionale Erfolgs-/Hinweiszeile (z. B. „✅ Datenbank
    verbunden“), die **als TextDisplay im Container** landet — niemals als
    ``content``: Components-V2-Nachrichten verbieten ``content``, Discord
    antwortet sonst mit ``400 (50035) Invalid Form Body — The 'content' field
    cannot be used when using MessageFlags.IS_COMPONENTS_V2``.
    """
    storage = status_data.get("storage", {})
    used_str = storage.get("used_formatted", "0 B")
    quota_str = storage.get("quota_formatted", "9.00 GB")
    free_str = storage.get("free_formatted", "9.00 GB")
    pct = storage.get("usage_percent", 0.0)
    tables_cnt = storage.get("tables_count", 0)
    db_url = status_data.get("database_url", "Turso Cloud")

    cache_stats = status_data.get("cache_stats", {})
    total_hits = cache_stats.get("total_cache_hits", 0)

    systems = status_data.get("systems", [])
    if systems:
        sys_lines = []
        for s in systems[:8]:
            status_emoji = "🟢" if s.get("enabled") else "⚪"
            trig_count = len(s.get("triggers", []))
            sys_lines.append(f"{status_emoji} **{s.get('name')}** (`{s.get('type')}`, {trig_count} Trigger)")
        systems_text = "\n".join(sys_lines)
    else:
        systems_text = "_Noch keine Systeme angelegt. Beauftrage Arena AI, Self Roles oder Welcome-Systeme für dich zu scripten!_"

    status_text = (
        f"# 🗄️ Turso-Datenbank — Status & Auslastung\n"
        f"**Status:** 🟢 Verbunden mit Turso (`{db_url}`)\n\n"
        f"**📊 Speicherauslastung:**\n"
        f"• Verbraucht: `{used_str}` von `{quota_str}` ({pct}%)\n"
        f"• Freier Speicher: `{free_str}`\n"
        f"• Tabellen: `{tables_cnt}` in der Datenbank\n"
        f"• Cache-Vorteil: `{total_hits}` Abfragen aus Speicher bedient (Rate Limits geschont!)\n\n"
        f"**⚙️ Geordnete Systeme ({len(systems)}):**\n"
        f"{systems_text}"
    )

    # Container-Kinder: optional die Erfolgszeile VOR dem Status-Block. Ein
    # Container darf höchstens 10 Kinder haben — mit Hinweis sind es 5.
    children: List[Any] = []
    if notice and str(notice).strip():
        children.append(discord.ui.TextDisplay(str(notice).strip()[:4000]))
        children.append(discord.ui.Separator())
    children.append(discord.ui.TextDisplay(status_text))
    children.append(discord.ui.Separator())
    children.append(discord.ui.ActionRow(
        discord.ui.Button(
            label="Systeme verwalten",
            emoji="📋",
            style=discord.ButtonStyle.primary,
            custom_id=CID_DB_SYSTEMS,
        ),
        discord.ui.Button(
            label="Aktualisieren",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_DB_REFRESH,
        ),
        discord.ui.Button(
            label="Neu einrichten",
            emoji="⚙️",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_DB_CONNECT,
        ),
        discord.ui.Button(
            label="Verbindung trennen",
            emoji="⛔",
            style=discord.ButtonStyle.danger,
            custom_id=CID_DB_DISCONNECT,
        ),
    ))

    container = discord.ui.Container(*children, accent_colour=CONNECTED_ACCENT)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def database_manage_systems_view(systems: List[Dict[str, Any]]) -> discord.ui.View:
    """View mit Select-Menü zum Löschen von Systemen."""
    view = discord.ui.View(timeout=300)
    if not systems:
        return view

    options = []
    for s in systems[:25]:
        options.append(
            discord.SelectOption(
                label=s.get("name", "System")[:100],
                value=s["id"][:100],
                description=f"Typ: {s.get('type')}"[:100],
                emoji="🗑️",
            )
        )
    select = discord.ui.Select(
        placeholder="Wähle ein System zum Löschen aus...",
        options=options,
        custom_id=CID_DB_SYS_DEL,
    )
    view.add_item(select)
    return view


class TursoConnectModal(discord.ui.Modal, title="🗄️ Turso-Datenbank verknüpfen"):
    """Modal zur Eingabe von Database-URL und Auth-Token."""

    db_url = discord.ui.TextInput(
        label="Turso Database URL",
        placeholder="https://deine-db-user.turso.io oder libsql://...",
        min_length=5,
        max_length=200,
        required=True,
    )
    auth_token = discord.ui.TextInput(
        label="Turso Auth Token",
        placeholder="eyJhbGciOi...",
        min_length=10,
        max_length=1000,
        required=True,
    )

    def __init__(self, client: "RelayClient", guild: discord.Guild) -> None:
        super().__init__(timeout=300)
        self.client = client
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        url = (self.db_url.value or "").strip()
        token = (self.auth_token.value or "").strip()

        if not self.client.state or not getattr(self.client.state, "turso_service", None):
            await interaction.followup.send("❌ Turso-Service ist noch nicht bereit.", ephemeral=True)
            return

        ok, msg = await self.client.state.turso_service.connect_guild_database(
            guild_id=self.guild.id,
            guild_name=self.guild.name,
            database_url=url,
            auth_token=token,
            configured_by=f"{interaction.user.display_name} ({interaction.user.id})",
            configured_by_id=str(interaction.user.id),
        )
        if not ok:
            await interaction.followup.send(f"❌ {msg}", ephemeral=True)
            return

        status_data = await self.client.state.turso_service.get_guild_status(self.guild.id)
        # ⚠️ Components V2 (LayoutView/Container) verbietet ``content`` — Discord
        # lehnt die Nachricht sonst mit ``400 Bad Request (50035): Invalid Form
        # Body — In content: The 'content' field cannot be used when using
        # MessageFlags.IS_COMPONENTS_V2`` ab, OBWOHL die Datenbank erfolgreich
        # verknüpft wurde. Die Erfolgszeile wandert deshalb als TextDisplay in
        # den Container (``notice=…``) und nicht in ``content``.
        view = database_configured_view(status_data, notice=f"✅ {msg}")
        await interaction.followup.send(view=view, ephemeral=True)
        log.info(
            "Turso-Datenbank für '%s' (%s) verknüpft durch %s (%s).",
            self.guild.name, self.guild.id,
            interaction.user.display_name, interaction.user.id,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Fehler im TursoConnectModal", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ Fehler: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ Fehler: {error}", ephemeral=True)
        except Exception:
            pass


def _button_registration_view() -> discord.ui.LayoutView:
    """
    Wird nie angezeigt: Diese View registriert in ``setup_hook`` nur die
    Custom-ID-Buttons bei discord.py, damit Klicks auf alte Nachrichten auch
    nach einem Neustart ihren Handler finden. Die eigentliche Arbeit passiert
    in :meth:`RelayClient.on_interaction` — die Buttons hier haben absichtlich
    keinen Callback.
    """
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.ActionRow(
                discord.ui.Button(label="Verbinden", custom_id=CID_CONNECT),
                discord.ui.Button(label="Verbindung trennen", custom_id=CID_DISCONNECT),
                discord.ui.Button(label="Neues Token generieren", custom_id=CID_REGENERATE),
            ),
            discord.ui.ActionRow(
                discord.ui.Button(label="DB Verbinden", custom_id=CID_DB_CONNECT),
                discord.ui.Button(label="DB Trennen", custom_id=CID_DB_DISCONNECT),
                discord.ui.Button(label="DB Refresh", custom_id=CID_DB_REFRESH),
                discord.ui.Button(label="DB Systeme", custom_id=CID_DB_SYSTEMS),
                discord.ui.Button(label="DB Wiederverwenden", custom_id=f"{CID_DB_REUSE}:0"),
            ),
        )
    )
    return view


# ─────────────────────────────────────────────────────────────────────────────
#  Adminpanel (Container V2, nur für den Bot-Owner im Privatchat)
# ─────────────────────────────────────────────────────────────────────────────


def _admin_custom_id(action: str, page: int, query: str) -> str:
    """Baut eine Admin-Custom-ID: Seite + Suchbegriff reisen in der ID mit."""
    return f"{CID_ADMIN_PREFIX}{action}:{page}:{query[:ADMIN_QUERY_MAX]}"


def _admin_guild_custom_id(action: str, guild_id: int, page: int, query: str) -> str:
    """Baut eine Admin-Custom-ID mit Guild-ID: ``relay:admin:<action>:<guild_id>:<seite>:<suche>``."""
    return f"{CID_ADMIN_PREFIX}{action}:{guild_id}:{page}:{query[:ADMIN_QUERY_MAX]}"


def _parse_admin_custom_id(custom_id: str) -> Optional[Dict[str, Any]]:
    """Zerlegt Admin-Custom-IDs — ``None`` wenn unpassend."""
    if not custom_id.startswith(CID_ADMIN_PREFIX):
        return None
    rest = custom_id[len(CID_ADMIN_PREFIX):]
    parts = rest.split(":")
    action = parts[0]
    if action == "search":
        return {"action": "search", "page": 0, "query": ""}

    if action in ("guild", "invite", "leave", "leave_confirm"):
        guild_id_raw = parts[1] if len(parts) > 1 else ""
        try:
            guild_id: Optional[int] = int(guild_id_raw)
        except ValueError:
            guild_id = None
        page_raw = parts[2] if len(parts) > 2 else "0"
        query = ":".join(parts[3:]) if len(parts) > 3 else ""
        try:
            page = max(0, int(page_raw))
        except ValueError:
            page = 0
        return {"action": action, "guild_id": guild_id, "page": page, "query": query[:ADMIN_QUERY_MAX]}

    page_raw = parts[1] if len(parts) > 1 else "0"
    query = ":".join(parts[2:]) if len(parts) > 2 else ""
    try:
        page = max(0, int(page_raw))
    except ValueError:
        page = 0
    return {"action": action, "page": page, "query": query[:ADMIN_QUERY_MAX]}


def admin_panel_view(
    entries: List[Dict[str, Any]],
    *,
    page: int,
    query: str,
    total_guilds: int,
) -> discord.ui.LayoutView:
    """
    Baut das Adminpanel aus **vorsortierten** Einträgen
    (``{"guild", "member_count", "owner_present"}``).

    Sortiert hat :meth:`RelayClient._collect_admin_guilds`: Server ohne
    Bot-Owner zuerst (❗️), dann Mitglieder abwärts. Suchbegriff und Seitenzahl
    stecken in den Custom-IDs der Buttons — so bleibt auch ein altes Panel
    nach einem Neustart bedienbar (Dispatch via ``on_interaction``).
    """
    page_size = ADMIN_PAGE_SIZE
    total = len(entries)
    pages = max(1, -(-total // page_size))          # aufrunden ohne math.ceil
    page = max(0, min(page, pages - 1))
    chunk = entries[page * page_size:(page + 1) * page_size]

    lines: List[str] = []
    select_options: List[discord.SelectOption] = []
    for entry in chunk:
        guild = entry["guild"]
        marker = "" if entry["owner_present"] else "❗️ "
        count = f"{entry['member_count']:,}".replace(",", ".")
        lines.append(f"{marker}**{guild.name}** · {count} Mitglieder")
        desc = f"{count} Mitglieder"
        if not entry["owner_present"]:
            desc += " · ❗️ Kein Mitglied"
        select_options.append(
            discord.SelectOption(
                label=getattr(guild, "name", "Server")[:100],
                value=str(guild.id),
                description=desc[:100],
                # WICHTIG: hier NUR das VS16-freie Emoji verwenden — siehe
                # _select_safe_emoji()-Docstring. Der Marker/Legend-Text darf
                # weiterhin die „hübsche“ Variante mit Variation-Selector
                # zeigen, denn TextDisplay ist davon nicht betroffen.
                emoji=_select_safe_emoji("❗️") if not entry["owner_present"] else "🏰",
            )
        )
    if not lines:
        lines.append("Keine Server gefunden." if query else "Der Bot ist auf keinem Server.")

    header = f"🛠️ Adminpanel · {total_guilds} Server gesamt · {total} angezeigt"
    if query:
        header += f" · Suche: „{query}“"
    legend = "❗️ = Du bist auf diesem Server noch **nicht** Mitglied — die stehen ganz oben."
    footer = f"Seite {page + 1}/{pages} · Sortierung: ❗️-Server zuerst, dann Mitgliederzahl"

    buttons = [
        discord.ui.Button(
            label="Zurück", emoji="◀️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", page - 1, query), disabled=page <= 0,
        ),
        discord.ui.Button(
            label="Suchen", emoji="🔍", style=discord.ButtonStyle.primary,
            custom_id=CID_ADMIN_SEARCH,
        ),
        discord.ui.Button(
            label="Weiter", emoji="▶️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", page + 1, query), disabled=page >= pages - 1,
        ),
        discord.ui.Button(
            label="Schließen", emoji="✖️", style=discord.ButtonStyle.danger,
            custom_id=_admin_custom_id("close", page, query),
        ),
    ]
    if query:
        buttons.append(discord.ui.Button(
            label="Suche löschen", emoji="✖️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", 0, ""),
        ))

    container_items: List[Any] = [
        discord.ui.TextDisplay(f"# {header}"),
        discord.ui.TextDisplay(legend),
        discord.ui.Separator(),
        discord.ui.TextDisplay("\n".join(lines)),
    ]

    if select_options:
        container_items.extend([
            discord.ui.Separator(),
            discord.ui.ActionRow(
                discord.ui.Select(
                    placeholder="Server für Details auswählen…",
                    custom_id=_admin_custom_id("select", page, query),
                    options=select_options,
                )
            ),
        ])

    container_items.extend([
        discord.ui.Separator(),
        discord.ui.TextDisplay(footer),
        discord.ui.ActionRow(*buttons),
    ])

    container = discord.ui.Container(
        *container_items,
        accent_colour=WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def _admin_sessions_detail_text(sessions: List[Any]) -> str:
    """Formatierte Liste aktiver Sitzungen für die Server-Detailansicht."""
    if not sessions:
        return ""
    lines: List[str] = []
    for s in sessions:
        info = s.to_public_dict()
        last_used = f" · zuletzt genutzt: {info['last_used_ago']}" if info.get("last_used_ago") else ""
        lines.append(
            f"• `{s.token_prefix}` ({info['mode_label']}) · Ablauf: {info['expires_in']}"
            f" · {info['request_count']} Requests{last_used} · von {info['created_by_name']}"
        )
    return "\n".join(lines)[:1000]


def _admin_guild_stats_lines(guild: discord.Guild) -> List[str]:
    """
    Statistik-Zeilen der Server-Übersicht: Kanäle, Rollen, Emojis, Boosts u. v. m.

    Alles reine Cache-Blicke (keine REST-Aufrufe) — die Übersicht bleibt also
    auch bei ausgefallener Discord-API bedienbar.
    """
    lines: List[str] = []
    created = getattr(guild, "created_at", None)
    if created is not None:
        lines.append(f"**Erstellt:** {created.strftime('%d.%m.%Y')} · **Server-ID:** `{guild.id}`")

    categories = len(getattr(guild, "categories", []) or [])
    text_ch = len(getattr(guild, "text_channels", []) or [])
    voice_ch = len(getattr(guild, "voice_channels", []) or [])
    stage_ch = len(getattr(guild, "stage_channels", []) or [])
    forum_ch = len(getattr(guild, "forums", []) or [])
    channel_parts = [f"{categories} 📁", f"{text_ch} 💬", f"{voice_ch} 🔊"]
    if stage_ch:
        channel_parts.append(f"{stage_ch} 🎤")
    if forum_ch:
        channel_parts.append(f"{forum_ch} 🪧")
    lines.append("**Kanäle:** " + " · ".join(channel_parts))

    roles = len(getattr(guild, "roles", []) or [])
    emojis = len(getattr(guild, "emojis", []) or [])
    stickers = len(getattr(guild, "stickers", []) or [])
    lines.append(f"**Rollen:** {roles} · **Emojis:** {emojis} · **Sticker:** {stickers}")

    tier = getattr(guild, "premium_tier", 0) or 0
    boosts = getattr(guild, "premium_subscription_count", 0) or 0
    if boosts or tier:
        lines.append(f"**Boosts:** Level {tier} · {boosts} Boost(s)")

    me = getattr(guild, "me", None)
    if me is not None:
        is_admin = bool(getattr(me, "guild_permissions", None) and me.guild_permissions.administrator)
        top_role = getattr(me, "top_role", None)
        role_name = getattr(top_role, "name", "?") if top_role is not None else "?"
        lines.append(
            f"**Bot-Rolle:** {role_name} · " + ("✅ Administrator" if is_admin else "⚠️ KEIN Administrator")
        )
    return lines


def _admin_db_status_text(db_status: Optional[Dict[str, Any]]) -> str:
    """Datenbank-Sektion der Server-Übersicht (Turso-Status inkl. Teilen-Info)."""
    if not db_status or not db_status.get("configured"):
        return (
            "**🗄️ Server-Datenbank:**\n"
            "⚪ Nicht eingerichtet — der Server-Owner kann mit `/manage_database` "
            "kostenlos eine Turso-Datenbank verknüpfen."
        )
    storage = db_status.get("storage") or {}
    pct = storage.get("usage_percent", 0.0)
    used = storage.get("used_formatted", "0 B")
    systems_count = db_status.get("systems_count", len(db_status.get("systems", []) or []))
    by = db_status.get("configured_by") or "unbekannt"
    updated = str(db_status.get("updated_at") or "")[:10]
    lines = [
        f"🟢 Verbunden (`{db_status.get('database_url', 'Turso')}`)",
        f"• **{systems_count}** System(e) · Speicher: `{used}` ({pct}%)",
        f"• Eingerichtet von **{by}**" + (f" am {updated}" if updated else ""),
    ]
    if db_status.get("shared"):
        shared_names = ", ".join(
            s.get("guild_name") or f"`{s.get('guild_id')}`" for s in (db_status.get("shared_with") or [])
        )
        lines.append(f"• 🔗 **Geteilte Datenbank** mit: {shared_names or 'anderen Servern'}")
    else:
        lines.append("• Exklusive Datenbank (nur dieser Server)")
    return "**🗄️ Server-Datenbank:**\n" + "\n".join(lines)


def admin_guild_detail_view(
    guild: discord.Guild,
    *,
    owner: Optional[Any],
    owner_present: bool,
    sessions: List[Any],
    page: int,
    query: str,
    invite_url: Optional[str] = None,
    notice: Optional[str] = None,
    db_status: Optional[Dict[str, Any]] = None,
) -> discord.ui.LayoutView:
    """Server-Übersicht (Container V2) im Adminpanel."""
    count = f"{(guild.member_count or 0):,}".replace(",", ".")
    owner_str = _owner_line(guild, owner)
    member_status = "✅ Du bist Mitglied auf diesem Server" if owner_present else "❗️ Du bist auf diesem Server **noch nicht** Mitglied"

    details = [
        f"**Server-ID:** `{guild.id}`",
        f"**Mitglieder:** {count}",
        owner_str,
        f"**Bot-Owner-Status:** {member_status}",
    ]

    components: List[Any] = [
        _guild_header_section(
            guild,
            f"🏰 {guild.name}",
            details,
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay("**📊 Server-Details:**\n" + "\n".join(_admin_guild_stats_lines(guild))),
        discord.ui.Separator(),
    ]

    if sessions:
        components.append(
            discord.ui.TextDisplay(
                f"**🔗 Aktive KI-Verbindung ({len(sessions)} Token):**\n"
                f"{_admin_sessions_detail_text(sessions)}"
            )
        )
    else:
        components.append(
            discord.ui.TextDisplay(
                "**🔗 KI-Verbindung:**\n"
                "⚪ Keine aktiven Tokens — Bridge für diesen Server ist nicht verbunden."
            )
        )

    components.extend([
        discord.ui.Separator(),
        discord.ui.TextDisplay(_admin_db_status_text(db_status)),
    ])

    if invite_url:
        components.extend([
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"**✉️ Einladungslink (1 Stunde gültig, 1 Nutzung):**\n<{invite_url}>"
            ),
        ])

    if notice:
        components.extend([
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"⚠️ **Hinweis:** {notice}"),
        ])

    buttons = [
        discord.ui.Button(
            label="Zurück", emoji="◀️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", page, query),
        ),
        discord.ui.Button(
            label="Einladung erstellen", emoji="✉️", style=discord.ButtonStyle.primary,
            custom_id=_admin_guild_custom_id("invite", guild.id, page, query),
        ),
        discord.ui.Button(
            label="Server verlassen", emoji="🚪", style=discord.ButtonStyle.danger,
            custom_id=_admin_guild_custom_id("leave", guild.id, page, query),
        ),
        discord.ui.Button(
            label="Schließen", emoji="✖️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("close", page, query),
        ),
    ]

    components.extend([
        discord.ui.Separator(),
        discord.ui.ActionRow(*buttons),
    ])

    container = discord.ui.Container(
        *components,
        accent_colour=CONNECTED_ACCENT if sessions else WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def admin_guild_leave_confirm_view(
    guild: discord.Guild,
    *,
    page: int,
    query: str,
) -> discord.ui.LayoutView:
    """Bestätigungsansicht (Container V2) vor dem Verlassen eines Servers."""
    container = discord.ui.Container(
        discord.ui.TextDisplay(f"# ⚠️ Server wirklich verlassen?"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"Möchtest du wirklich, dass der Bot den Server **{guild.name}** (`{guild.id}`) verlässt?\n\n"
            "Der Bot wird sofort vom Server entfernt und alle aktiven KI-Zugriffe werden gestoppt."
        ),
        discord.ui.Separator(),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Abbrechen", emoji="◀️", style=discord.ButtonStyle.secondary,
                custom_id=_admin_guild_custom_id("guild", guild.id, page, query),
            ),
            discord.ui.Button(
                label="Ja, Server verlassen", emoji="🚪", style=discord.ButtonStyle.danger,
                custom_id=_admin_guild_custom_id("leave_confirm", guild.id, page, query),
            ),
            discord.ui.Button(
                label="Schließen", emoji="✖️", style=discord.ButtonStyle.secondary,
                custom_id=_admin_custom_id("close", page, query),
            ),
        ),
        accent_colour=ERR_COLOR,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


class AdminSearchModal(discord.ui.Modal, title="🔍 Server suchen"):
    """Das Such-Fenster des Adminpanels — nach Absenden startet die Liste bei Seite 1."""

    query = discord.ui.TextInput(
        label="Servername (Teile genügen)",
        placeholder="z. B. Community",
        max_length=ADMIN_QUERY_MAX,
        required=False,
    )

    def __init__(self, client: "RelayClient") -> None:
        super().__init__(timeout=300)
        self.client = client

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = (self.query.value or "").strip()
        entries = await self.client._collect_admin_guilds()
        if query:
            lowered = query.lower()
            entries = [e for e in entries if lowered in e["guild"].name.lower()]
        view = admin_panel_view(entries, page=0, query=query, total_guilds=len(self.client.guilds))
        try:
            await interaction.response.send_message(view=view)
        except discord.HTTPException as exc:
            log.error("Adminpanel-Suche konnte nicht antworten: %s", exc)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        log.exception("Fehler im Adminpanel-Suchfenster.", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ Suche fehlgeschlagen: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ Suche fehlgeschlagen: {error}", ephemeral=True)
        except discord.HTTPException:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Owner-Benachrichtigungen (Container V2 mit Server-Icon)
# ─────────────────────────────────────────────────────────────────────────────


def _guild_header_section(guild: discord.Guild, title: str, detail_lines: List[str]) -> Any:
    """
    Kopfzeile einer Guild-DM: Titel + Details, das Server-Icon als Thumbnail.

    Mit Icon: ein ``Section`` mit Thumbnail-Accessory. **Ohne** Icon: ein
    schlichtes ``TextDisplay``. Der frühere Fallback mit einem leeren
    ``TextDisplay`` im Accessory-Slot einer Section war **ungültig** —
    Discord lässt dort ausschließlich *Button* oder *Thumbnail* zu und
    lehnte die komplette Nachricht mit ``400 Invalid Form Body`` ab. Das war
    der Grund dafür, dass die Server-Übersicht des Adminpanels (und die
    Join-/Leave-DMs) für jeden Server **ohne eigenes Icon** mit
    „Die Anwendung reagiert nicht" abbrachen.
    """
    icon = getattr(guild, "icon", None)
    text = discord.ui.TextDisplay(f"## {title}\n" + "\n".join(detail_lines))
    if icon is not None:
        try:
            return discord.ui.Section(text, accessory=discord.ui.Thumbnail(str(icon.url)))
        except Exception:  # noqa: BLE001 — Icon darf den Aufbau nie sprengen
            pass
    # Kein Icon (oder das Icon war nicht serialisierbar): Absichtlich KEINE
    # Section mit Ersatz-Accessory bauen — ein TextDisplay-Accessory ist für
    # Discord ungültige Payload (siehe Docstring oben).
    return text


def _owner_line(guild: discord.Guild, owner: Optional[Any]) -> str:
    if owner is not None:
        return f"Owner: {owner.mention} ({getattr(owner, 'name', '?')})"
    return f"Owner: <@{guild.owner_id}> (Konnte nicht geladen werden)"


def guild_join_notify_view(guild: discord.Guild, owner: Optional[Any]) -> discord.ui.LayoutView:
    """DM an den Bot-Owner: der Bot ist einem Server beigetreten."""
    count = f"{(guild.member_count or 0):,}".replace(",", ".")
    container = discord.ui.Container(
        _guild_header_section(
            guild,
            f"🟢 Server beigetreten: {guild.name}",
            [f"Mitglieder: **{count}**", _owner_line(guild, owner), f"ID: `{guild.id}`"],
        ),
        accent_colour=OK_COLOR,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def guild_remove_notify_view(
    guild: discord.Guild, owner: Optional[Any], reason: str,
    *, cleanup: Optional[Dict[str, Any]] = None,
) -> discord.ui.LayoutView:
    """DM an den Bot-Owner: der Bot hat einen Server verlassen (mit bestmöglichem Grund)."""
    count = f"{(guild.member_count or 0):,}".replace(",", ".")
    items: List[Any] = [
        _guild_header_section(
            guild,
            f"🔴 Server verlassen: {guild.name}",
            [f"Mitglieder: **{count}**", _owner_line(guild, owner), f"ID: `{guild.id}`"],
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(f"**Grund:** {reason}"),
    ]
    if cleanup is not None:
        summary = cleanup.get("database_summary") or {}
        parts: List[str] = [f"🧹 **Aufräumen:** {cleanup.get('sessions_revoked', 0)} KI-Verbindung(en) gestoppt,"]
        if cleanup.get("database_purged"):
            if summary.get("exclusive"):
                parts.append(
                    f"Datenbank komplett gelöscht ({len(summary.get('dropped_tables', []))} Tabellen geleert)."
                )
            else:
                shared = summary.get("shared_with") or []
                parts.append(
                    "Datenbank-Verknüpfung entfernt (die Datenbank wird von anderen Servern "
                    f"weiterverwendet: {', '.join(str(s) for s in shared) or 'andere'})."
                )
        else:
            parts.append("keine Datenbank-Verknüpfung vorhanden.")
        items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(" ".join(parts)))
    container = discord.ui.Container(*items, accent_colour=ERR_COLOR)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def server_owner_welcome_view(guild: discord.Guild) -> discord.ui.LayoutView:
    """
    DM an den Server-Owner direkt nach dem Beitritt: die Kurzanleitung.

    Bewusst freundlich und kurz — wer den Bot einlädt, soll in 30 Sekunden
    wissen, wie er Arena AI auf seinem Server arbeiten lässt.
    """
    container = discord.ui.Container(
        discord.ui.TextDisplay("# Hey! 👋"),
        discord.ui.TextDisplay(
            f"Danke, dass du mich auf **{guild.name}** eingeladen hast!\n\n"
            "Mit mir kannst du **Arena AI direkt auf deinem Discord-Server arbeiten "
            "lassen** — sie richtet dir alles ein: Rollen, Kanäle, Kategorien, "
            "Rechte, AutoMod, Branding, Willkommens-Nachrichten … einfach alles, "
            "was dein Server braucht."
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**So startest du (dauert keine 2 Minuten):**\n"
            f"1. Geh auf deinen Server **{guild.name}**\n"
            "2. Nutze den Command `/connect`\n"
            "3. Klicke auf **Verbinden** und schick Arena AI den Prompt aus der "
            "Nachricht — dann verstehst du schon alles."
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "Es geht wirklich schnell — und für die Einrichtung deines Servers "
            "spare ich dir massig Zeit. 🚀"
        ),
        accent_colour=WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view



# ─────────────────────────────────────────────────────────────────────────────
#  Client
# ─────────────────────────────────────────────────────────────────────────────


class RelayClient(discord.Client):
    """Discord-Client mit Slash-Command-Tree und Relay-Anbindung."""

    def __init__(
        self,
        config: Config,
        store: SessionStore,
        state: Any = None,
        *,
        intents: Optional[discord.Intents] = None,
    ) -> None:
        """
        ``state`` darf zunächst ``None`` sein.

        Client und ``AppState`` verweisen aufeinander; ``main.amain`` erzeugt
        deshalb erst den Client und setzt ``client.state`` direkt danach.
        """
        options: Dict[str, Any] = {"intents": intents or build_intents(True)}
        proxy = (getattr(config, "discord_proxy", "") or "").strip()
        if proxy:
            # discord.py reicht den Proxy an REST **und** Gateway weiter — genau
            # das brauchen wir, wenn die ausgehende IP des Containers von
            # Cloudflare gesperrt ist (Error 1015).
            options["proxy"] = proxy
            user = (getattr(config, "discord_proxy_user", "") or "").strip()
            if user:
                options["proxy_auth"] = aiohttp.BasicAuth(
                    user, getattr(config, "discord_proxy_password", "") or ""
                )
            log.info("Discord-Traffic läuft über Proxy %s", mask_proxy_url(proxy))
        super().__init__(**options)
        self.config = config
        self.store = store
        self.state = state
        self.tree = app_commands.CommandTree(self)
        self.ready_at: Optional[Any] = None
        # Kick/Ban-Gründe, die live per Gateway empfangen wurden (siehe
        # ``on_audit_log_entry_create``): guild_id -> {"action", "reason", "by", "ts"}.
        # Nach dem Verlassen des Servers hat der Bot keinen Audit-Log-Zugriff
        # mehr — deshalb wird der Grund abgehört, SOLANGE er noch Mitglied ist.
        self._pending_leave_reasons: Dict[int, Dict[str, Any]] = {}
        self.tree.on_error = self._on_command_error  # type: ignore[assignment]

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        self._register_commands()
        # Persistente Button-View registrieren: ``timeout=None`` plus diese
        # Registrierung sorgt dafür, dass Klicks auf ausgelieferte Nachrichten
        # auch nach einem Render-Deploy noch dispatched werden.
        self.add_view(_button_registration_view())
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """
        Registriert die Slash-Commands — höchstens einmal pro
        ``COMMAND_SYNC_MIN_INTERVAL``.

        Zwei Gründe für die Bremse:

        1. Ein globaler Sync ist ein eigener Rate-Limit-Bucket. Bei jedem
           Reconnect erneut zu syncen bringt nichts (die Commands stehen schon
           serverseitig), erzeugt aber zusätzliche Anfragen — genau die wollen
           wir bei einer Cloudflare-Sperre der IP vermeiden.
        2. Ein fehlgeschlagener Sync darf den Bot niemals in einen Reconnect-
           Loop zwingen: bereits registrierte Commands bleiben gültig.
        """
        last = getattr(self.state, "commands_synced_at", None)
        now = now_utc()
        if last is not None and (now - last).total_seconds() < COMMAND_SYNC_MIN_INTERVAL:
            log.debug("Slash-Commands wurden vor %.0f s registriert — Sync übersprungen.",
                      (now - last).total_seconds())
            return

        # ── Diagnostik ───────────────────────────────────────────────────────
        # Vor dem Sync: das PAYLOAD loggen, das an Discord geht — insbesondere
        # bei /adminpanel interessieren ``contexts``/``dm_permission``/
        # ``integration_types`` (Verdacht: könnte von Discord verworfen
        # werden). Schmales, aber dauerhaftes Logging.
        try:
            outgoing = [c.to_dict(self.tree) for c in self.tree.get_commands()]
            log.info("Slash-Command-Sync: sende %d Commands, Payload: %s",
                      len(outgoing), json.dumps(outgoing, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — Diagnostik darf den Sync nie verhindern
            log.exception("Slash-Command-Sync: Payload-Dump vor dem Sync fehlgeschlagen.")

        try:
            synced = await self.tree.sync()
        except discord.HTTPException as exc:
            log.exception(
                "Slash-Command-Sync fehlgeschlagen — status=%s discord_error_code=%s text=%r. "
                "Bot läuft weiter, bereits registrierte Commands bleiben aktiv.",
                getattr(exc, "status", None), getattr(exc, "code", None), getattr(exc, "text", None),
            )
            return
        except Exception as exc:  # noqa: BLE001 — RateLimited & Co. sollen den Bot nicht crashen
            log.exception(
                "Slash-Command-Sync fehlgeschlagen (%s) — Bot läuft weiter, "
                "bereits registrierte Commands bleiben aktiv.", exc,
            )
            return

        # Nach dem Sync: das, was Discord TATSÄCHLICH zurückgegeben hat —
        # zeigt z. B., ob contexts/dm_permission/integration_types vom
        # Server anders interpretiert oder stillschweigend genullt wurden.
        try:
            log.info("Slash-Command-Sync: Discord bestätigt %d Commands: %s",
                      len(synced), json.dumps([c.to_dict() for c in synced], ensure_ascii=False))
        except Exception:
            log.exception("Slash-Command-Sync: Payload-Dump nach dem Sync fehlgeschlagen.")

        if self.state is not None:
            self.state.commands_synced_at = now
        log.info("Slash-Commands global registriert (Sync kann bis zu 1 Stunde dauern; "
                 "auf bestehenden Servern meist sofort).")

    async def on_ready(self) -> None:
        self.ready_at = now_utc()
        # Login geglückt → Health-Status zurück auf „online“, alte Fehlermeldung weg.
        self.state.discord_status = "online"
        self.state.discord_last_error = None
        self.state.discord_retry_at = None
        self.state.platform_verdict = None
        # Erfolgreicher Login ⇒ Neustart-Zähler (IP-Sperre) zurücksetzen.
        ledger = getattr(self.state, "restart_ledger", None)
        if ledger is not None:
            try:
                ledger.clear(reason="Login erfolgreich")
            except Exception as exc:  # noqa: BLE001 — Buchführung darf on_ready nie stören
                log.debug("Neustart-Buch nicht zurücksetzbar: %s", exc)
        user = self.user
        guild_names = ", ".join(g.name for g in self.guilds[:8]) or "(keine)"
        log.info("═" * 68)
        log.info("  %s v%s ist ONLINE", self.config.bot_name, __version__)
        log.info("  Bot        : %s (ID %s)", getattr(user, "name", "?"), getattr(user, "id", "?"))
        log.info("  Server     : %d — %s", len(self.guilds), guild_names)
        log.info("  Öffentliche URL : %s", self.state.base_url())
        log.info("  Console    : %s/console", self.state.base_url())
        log.info("  Healthcheck: %s/api/health", self.state.base_url())
        if user:
            log.info("  Invite (Admin): %s", self.admin_invite_url())
        log.info("═" * 68)

        try:
            await self._update_presence()
        except discord.HTTPException as exc:
            log.warning("Presence konnte nicht gesetzt werden: %s", exc)

        self._check_guild_permissions()

        # Slash-Commands sofort auf allen bestehenden Servern registrieren
        for g in self.guilds:
            try:
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            except Exception as exc:  # noqa: BLE001
                log.debug("Guild-Sync für %s (%s): %s", g.id, g.name, exc)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Server beigetreten: %s (%d Mitglieder, ID %s)",
                 guild.name, guild.member_count, guild.id)
        self._check_guild_permissions()
        await self._update_presence()
        # Commands sofort auf neuem Server bereitstellen
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception as exc:  # noqa: BLE001
            log.debug("Guild-Sync bei Join für %s: %s", guild.id, exc)
        # Owner informieren (Bot-Owner per DM) und dem Server-Owner die
        # Kurzanleitung schicken — beides darf den Join niemals stören.
        try:
            await self._notify_owner_join(guild)
        except Exception as exc:  # noqa: BLE001
            log.warning("Join-Benachrichtigung fehlgeschlagen: %s", exc)
        try:
            await self._welcome_server_owner(guild)
        except Exception as exc:  # noqa: BLE001
            log.warning("Willkommens-DM fehlgeschlagen: %s", exc)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Server verlassen: %s (ID %s)", guild.name, guild.id)
        try:
            await self._update_presence()
        except Exception as exc:  # noqa: BLE001 — Presence darf die Bereinigung nie blockieren
            log.debug("Presence konnte nach Leave nicht aktualisiert werden: %s", exc)
        cleanup = await self._cleanup_guild_data(guild)
        try:
            await self._notify_owner_remove(guild, cleanup=cleanup)
        except Exception as exc:  # noqa: BLE001
            log.warning("Leave-Benachrichtigung fehlgeschlagen: %s", exc)

    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """
        Live-Audit-Log: Kick/Ban-Gründe empfangen, **solange** der Bot noch
        Mitglied ist.

        Das ist der zuverlässige Weg an den Grund zu kommen, den ein Moderator
        beim Kick/Ban angegeben hat: Nach dem Entfernen wäre der Bot nicht mehr
        berechtigt, das Audit-Log zu lesen — aber ``GUILD_AUDIT_LOG_ENTRY_CREATE``
        wird ihm per Gateway geliefert, sobald der Eintrag entsteht. Zielt der
        Eintrag auf den Bot selbst, merkt er sich Grund und Ausführenden für die
        Leave-DM (``_leave_reason``).
        """
        try:
            guild = entry.guild
            if guild is None:
                return
            if entry.action not in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                return
            bot_id = self.user.id if self.user else None
            target_id = getattr(getattr(entry, "target", None), "id", None)
            if bot_id is None or target_id != bot_id:
                return
            executor = getattr(entry, "user", None)
            executor_name = getattr(executor, "name", None) or (str(executor) if executor else "")
            self._pending_leave_reasons[guild.id] = {
                "action": "kick" if entry.action is discord.AuditLogAction.kick else "ban",
                "reason": (entry.reason or "").strip(),
                "by": executor_name,
                "ts": time.monotonic(),
            }
            log.info(
                "Leave-Grund live empfangen: Bot wird %s auf '%s' (Grund: %s, von %s).",
                "gekickt" if entry.action is discord.AuditLogAction.kick else "gebannt",
                guild.name, entry.reason or "(keiner)", executor,
            )
        except Exception as exc:  # noqa: BLE001 — Audit-Events dürfen nie crashen
            log.debug("audit_log_entry_create konnte nicht verarbeitet werden: %s", exc)

    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User) -> None:
        """
        Der Bot selbst wurde gebannt (``GUILD_BAN_ADD``, liefert keinen Grund —
        der Audit-Log-Eintrag mit Grund folgt typischerweise direkt danach und
        überschreibt diesen Notizeintrag).
        """
        try:
            if self.user is None or user.id != self.user.id:
                return
            existing = self._pending_leave_reasons.get(guild.id)
            if existing and existing.get("action") == "ban":
                return
            self._pending_leave_reasons[guild.id] = {
                "action": "ban",
                "reason": "",
                "by": "",
                "ts": time.monotonic(),
            }
            log.info("Bot wurde auf '%s' gebannt (Audit-Log-Details folgen).", guild.name)
        except Exception as exc:  # noqa: BLE001
            log.debug("member_ban konnte nicht verarbeitet werden: %s", exc)

    async def on_member_join(self, member: discord.Member) -> None:
        """Turso-Engine: Begrüßungen, Auto-Rollen und Join-Aktionen."""
        if self.state and getattr(self.state, "turso_service", None):
            try:
                await self.state.turso_service.engine.handle_member_join(member)
            except Exception as exc:  # noqa: BLE001
                log.error("Fehler in Turso handle_member_join: %s", exc)

    async def on_member_remove(self, member: discord.Member) -> None:
        """Turso-Engine: Verlassen-Nachrichten und Cleanup."""
        if self.state and getattr(self.state, "turso_service", None):
            try:
                await self.state.turso_service.engine.handle_member_remove(member)
            except Exception as exc:  # noqa: BLE001
                log.error("Fehler in Turso handle_member_remove: %s", exc)

    async def on_message(self, message: discord.Message) -> None:
        """Turso-Engine: Auto-Responder, Keyword-Filter, Leveling-Aktionen."""
        if self.state and getattr(self.state, "turso_service", None):
            try:
                await self.state.turso_service.engine.handle_message(message)
            except Exception as exc:  # noqa: BLE001
                log.error("Fehler in Turso handle_message: %s", exc)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Turso-Engine: Self Roles / Reaction Roles vergeben."""
        if self.state and getattr(self.state, "turso_service", None):
            try:
                await self.state.turso_service.engine.handle_reaction_add(self, payload)
            except Exception as exc:  # noqa: BLE001
                log.error("Fehler in Turso handle_reaction_add: %s", exc)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Turso-Engine: Self Roles / Reaction Roles entfernen."""
        if self.state and getattr(self.state, "turso_service", None):
            try:
                await self.state.turso_service.engine.handle_reaction_remove(self, payload)
            except Exception as exc:  # noqa: BLE001
                log.error("Fehler in Turso handle_reaction_remove: %s", exc)

    async def on_resumed(self) -> None:
        log.info("Gateway-Verbindung wiederhergestellt.")

    # ── Helfer ───────────────────────────────────────────────────────────────
    def admin_invite_url(self) -> str:
        """Invite-Link, der dem Bot Administrator + Slash-Commands gibt."""
        if not self.user:
            return ""
        return discord.utils.oauth_url(
            self.user.id,
            permissions=discord.Permissions(administrator=True),
            scopes=ADMIN_INVITE_SCOPES,
        )

    def _check_guild_permissions(self) -> None:
        """Warnt im Log, wenn der Bot irgendwo kein Administrator ist."""
        for guild in self.guilds:
            me = guild.me
            if me is None:
                continue
            if not me.guild_permissions.administrator:
                missing = [
                    name for name, value in me.guild_permissions
                    if not value and name in {"manage_guild", "manage_channels", "manage_roles",
                                              "kick_members", "ban_members", "moderate_members",
                                              "manage_messages", "view_audit_log"}
                ]
                log.warning(
                    "⚠ Auf '%s' (ID %s) ist der Bot KEIN Administrator. Fehlend u. a.: %s. "
                    "→ Servereinstellungen → Rollen → Bot-Rolle auf 'Administrator' "
                    "und ganz nach oben schieben. /connect verweigert sonst die Arbeit.",
                    guild.name, guild.id, ", ".join(sorted(missing)) or "viele",
                )

    def _base_url_warning(self) -> Optional[str]:
        base = self.state.base_url()
        if base.startswith(("http://127.", "http://localhost", "http://0.0.0.0")):
            return (
                f"⚠️ Die öffentliche URL ist nicht gesetzt (`{base}`). Auf Render wird "
                "`RENDER_EXTERNAL_URL` automatisch geliefert — lokal bitte `PUBLIC_URL` "
                "setzen, sonst funktioniert der Link für Arena AI nicht."
            )
        return None

    # ── Presence & Owner-Benachrichtigungen ──────────────────────────────────
    def _presence_text(self) -> str:
        """
        Der Live-Status: ``/connect | 👀 N eingerichtete Server``.

        Die Zahl ist die Anzahl der Server, auf denen der Bot ist — sie wird
        bei jedem Join/Leave aktualisiert.
        """
        count = len(self.guilds)
        server_word = "eingerichteter Server" if count == 1 else "eingerichtete Server"
        return f"/{self.config.command_name} | 👀 {count} {server_word}"

    async def _update_presence(self) -> None:
        """Setzt den Live-Status (Aufruf bei on_ready und jedem Server-Wechsel)."""
        presence = discord.Activity(type=discord.ActivityType.playing, name=self._presence_text())
        status = getattr(discord.Status, self.config.status_presence, discord.Status.online)
        await self.change_presence(activity=presence, status=status)

    def _bot_owner(self) -> Optional[discord.abc.User]:
        """Der Bot-Owner als User-Objekt (Cache, sonst REST — darf None sein)."""
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        if not owner_id:
            return None
        return self.get_user(owner_id)

    async def _dm_user(self, user: Optional[discord.abc.User], view: discord.ui.LayoutView,
                       *, context: str) -> bool:
        """
        Schickt eine Container-V2-DM — robust gegen geschlossene DMs.

        Geschlossene DMs oder andere Fehler dürfen den Ablauf niemals abreißen
        lassen; es wird nur geloggt, ob es geklappt hat.
        """
        if user is None:
            log.debug("DM (%s) übersprungen — Zielnutzer unbekannt.", context)
            return False
        try:
            await user.send(view=view)
            return True
        except discord.Forbidden:
            log.info("DM (%s) an %s nicht möglich — DMs sind geschlossen.",
                     context, getattr(user, "id", "?"))
        except discord.HTTPException as exc:
            log.warning("DM (%s) an %s fehlgeschlagen: %s", context, getattr(user, "id", "?"), exc)
        return False

    async def _notify_owner_join(self, guild: discord.Guild) -> None:
        """DM an den Bot-Owner: Name, Mitgliederzahl, Icon, Owner-Mention."""
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        if not owner_id:
            return
        bot_owner = self._bot_owner() or None
        guild_owner = guild.owner
        if guild_owner is None:
            try:
                guild_owner = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                guild_owner = None
        sent = await self._dm_user(
            bot_owner, guild_join_notify_view(guild, guild_owner),
            context=f"Join-Info {guild.id}",
        )
        if not sent:
            log.info("Join auf '%s' (ID %s, %s Mitglieder) konnte nicht per DM gemeldet werden.",
                     guild.name, guild.id, guild.member_count)

    async def _welcome_server_owner(self, guild: discord.Guild) -> None:
        """
        DM an den Server-Owner direkt nach dem Beitritt: die Kurzanleitung.

        Das ist die wichtigste DM überhaupt — sie erklärt, wie man Arena AI
        auf dem eigenen Server arbeiten lässt (``/connect`` → Verbinden).
        """
        target = guild.owner
        if target is None:
            try:
                target = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                target = None
        await self._dm_user(target, server_owner_welcome_view(guild),
                             context=f"Willkommen {guild.id}")

    def _prune_pending_leave_reasons(self) -> None:
        """Wirft veraltete Kick/Ban-Notizen weg (das Zeitfenster ist abgelaufen)."""
        now = time.monotonic()
        stale = [
            gid for gid, info in self._pending_leave_reasons.items()
            if now - float(info.get("ts", 0.0)) > PENDING_LEAVE_REASON_TTL
        ]
        for gid in stale:
            self._pending_leave_reasons.pop(gid, None)

    async def _leave_reason(self, guild: discord.Guild) -> str:
        """
        Bestmöglicher Grund, warum der Bot den Server verlassen hat.

        **Primäre Quelle:** der live per Gateway empfangene Kick/Ban-Eintrag
        (``on_audit_log_entry_create``) — er kommt an, SOLANGE der Bot noch
        Mitglied ist, und enthält exakt den Grund, den der Moderator angegeben
        hat, plus Ausführendem.

        **Fallback:** ein letzter Audit-Log-Leseversuch (kostet nichts, klappt
        aber nach dem Entfernen meist nicht mehr, weil der Bot dann keinen
        Zugriff mehr hat). Wenn beides nichts liefert, wird das ehrlich gesagt.
        """
        self._prune_pending_leave_reasons()
        pending = self._pending_leave_reasons.pop(guild.id, None)
        if pending:
            action = "gekickt" if pending.get("action") == "kick" else "gebannt"
            by = f" von **{pending['by']}**" if pending.get("by") else ""
            reason = (pending.get("reason") or "").strip()
            if reason:
                return f"Bot wurde {action}{by} — Grund: {reason}"
            return f"Bot wurde {action}{by} — kein Grund angegeben."

        await asyncio.sleep(1.5)  # Audit-Einträge brauchen einen Moment zum Anlegen
        try:
            async for entry in guild.audit_logs(limit=15):
                target_id = getattr(getattr(entry, "target", None), "id", None)
                if target_id != (self.user.id if self.user else -1):
                    continue
                if entry.action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                    action = "gekickt" if entry.action is discord.AuditLogAction.kick else "gebannt"
                    executor = getattr(entry, "user", None)
                    by = f" von **{executor}**" if executor else ""
                    reason = entry.reason or "kein Grund angegeben"
                    return f"Bot wurde {action}{by} — Grund: {reason}"
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        except Exception as exc:  # noqa: BLE001 — der Grund darf nie crashen
            log.debug("Audit-Log-Lookup nach Leave fehlgeschlagen: %s", exc)
        return (
            "nicht ermittelbar — nach dem Entfernen hat der Bot keinen Zugriff mehr auf "
            "das Audit-Log des Servers. Typische Ursachen: gekickt (ggf. mit Grund), "
            "gebannt oder der Server wurde gelöscht."
        )

    async def _cleanup_guild_data(self, guild: discord.Guild) -> Dict[str, Any]:
        """
        Löscht alle Daten des Servers — läuft bei **jedem** Verlassen
        (Kick, Ban oder freiwillig), noch bevor der Owner informiert wird.

        * Alle aktiven KI-Sitzungen (Tokens) werden sofort widerrufen.
        * Die Datenbank-Verknüpfung des Servers wird aus der Master-DB entfernt.
        * Ist die Server-Datenbank **exklusiv** (kein anderer Server teilt sie),
          wird sie komplett geleert. Bei **geteilten** Datenbanken bleiben die
          Daten für die übrigen Server bestehen.

        Alles davon darf niemals die Leave-Benachrichtigung blockieren —
        Fehler werden geloggt und als Summary zurückgegeben.
        """
        cleanup: Dict[str, Any] = {"sessions_revoked": 0, "database_purged": False}

        try:
            revoked = await self.store.revoke(guild_id=guild.id, by="server verlassen")
            cleanup["sessions_revoked"] = len(revoked)
            if revoked:
                if self.state is not None:
                    self.state.maybe_save(force=True)
                log.info(
                    "%d aktive KI-Verbindung(en) auf '%s' gestoppt (Server verlassen).",
                    len(revoked), guild.name,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Sitzungen von '%s' konnten nicht widerrufen werden: %s", guild.name, exc)

        turso = getattr(self.state, "turso_service", None) if self.state is not None else None
        if turso is None:
            return cleanup
        try:
            summary = await turso.purge_guild(guild.id)
            cleanup["database_purged"] = bool(summary.get("had_database"))
            cleanup["database_summary"] = summary
        except Exception as exc:  # noqa: BLE001
            log.warning("Datenbank-Bereinigung für '%s' fehlgeschlagen: %s", guild.name, exc)
        return cleanup

    async def _notify_owner_remove(
        self, guild: discord.Guild, *, cleanup: Optional[Dict[str, Any]] = None,
    ) -> None:
        """DM an den Bot-Owner: Name, Mitgliederzahl, Icon, Owner-Mention + Grund."""
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        if not owner_id:
            return
        bot_owner = self._bot_owner() or None
        reason = await self._leave_reason(guild)
        sent = await self._dm_user(
            bot_owner, guild_remove_notify_view(guild, guild.owner, reason, cleanup=cleanup),
            context=f"Leave-Info {guild.id}",
        )
        if not sent:
            log.warning(
                "Verlassen von '%s' (ID %s) konnte nicht per DM gemeldet werden. Grund: %s",
                guild.name, guild.id, reason,
            )

    # ── Adminpanel ───────────────────────────────────────────────────────────
    async def _collect_admin_guilds(self) -> List[Dict[str, Any]]:
        """
        Alle Server des Bots als sortierte Einträge fürs Adminpanel.

        Sortierung: Server, auf denen der Bot-Owner **nicht** Mitglied ist,
        kommen zuerst (im Panel mit ❗️), danach jeweils Mitglieder abwärts.

        Ist der Mitglieder-Cache vollständig (privilegiertes Intent, guild ist
        ``chunked``), genügt ein Blick in den Cache; sonst wird pro Server
        einmal per REST nachgeprüft.
        """
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        entries: List[Dict[str, Any]] = []
        for guild in self.guilds:
            owner_present = owner_id != 0 and guild.get_member(owner_id) is not None
            if not owner_present and owner_id and not guild.chunked:
                try:
                    await guild.fetch_member(owner_id)
                    owner_present = True
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    owner_present = False
            entries.append({
                "guild": guild,
                "member_count": guild.member_count or 0,
                "owner_present": owner_present,
            })
        entries.sort(key=lambda e: (e["owner_present"], -e["member_count"]))
        return entries

    async def _admin_panel_view(self, *, page: int = 0, query: str = "") -> discord.ui.LayoutView:
        """Sammelt die Serverdaten und baut das Panel (Suchfilter inklusive)."""
        entries = await self._collect_admin_guilds()
        if query:
            lowered = query.strip().lower()
            entries = [e for e in entries if lowered in e["guild"].name.lower()]
        return admin_panel_view(entries, page=page, query=query.strip(), total_guilds=len(self.guilds))

    async def _guild_detail_view(
        self,
        guild_or_id: Any,
        *,
        page: int = 0,
        query: str = "",
        invite_url: Optional[str] = None,
        notice: Optional[str] = None,
    ) -> discord.ui.LayoutView:
        """Baut die Detailansicht eines Servers für das Adminpanel."""
        if isinstance(guild_or_id, int):
            guild = self.get_guild(guild_or_id)
        else:
            guild = guild_or_id
        if guild is None:
            return await self._admin_panel_view(page=page, query=query)

        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        owner_present = owner_id != 0 and guild.get_member(owner_id) is not None
        if not owner_present and owner_id and not getattr(guild, "chunked", True):
            try:
                await guild.fetch_member(owner_id)
                owner_present = True
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                owner_present = False

        guild_owner = getattr(guild, "owner", None)
        if guild_owner is None and hasattr(guild, "owner_id") and hasattr(guild, "fetch_member"):
            try:
                guild_owner = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                guild_owner = None

        sessions = self.store.active_for_guild(guild.id)

        db_status: Optional[Dict[str, Any]] = None
        turso = getattr(self.state, "turso_service", None) if self.state is not None else None
        if turso is not None:
            try:
                db_status = await turso.get_guild_status(guild.id)
            except Exception as exc:  # noqa: BLE001 — DB-Status darf die Übersicht nie sprengen
                log.debug("DB-Status für '%s' nicht ladbar: %s", guild.name, exc)
                db_status = None

        return admin_guild_detail_view(
            guild,
            owner=guild_owner,
            owner_present=owner_present,
            sessions=sessions,
            page=page,
            query=query,
            invite_url=invite_url,
            notice=notice,
            db_status=db_status,
        )

    async def _handle_admin_button(self, interaction: discord.Interaction, custom_id: str) -> None:
        """Adminpanel-Interaktionen: Blättern, Suchen, Detailansicht, Invite, Leave."""
        if interaction.guild is not None:
            return  # Panel lebt ausschließlich im Privatchat
        if int(getattr(self.config, "bot_owner_id", 0) or 0) == 0:
            return
        if interaction.user.id != self.config.bot_owner_id:
            log.warning("Adminpanel-Klick von %s — nicht der Bot-Owner.",
                        getattr(interaction.user, "id", "?"))
            return

        parsed = _parse_admin_custom_id(custom_id)
        if parsed is None:
            return

        action = parsed["action"]
        page = parsed.get("page", 0)
        query = parsed.get("query", "")

        if action == "close":
            # Das Panel ist eine normale DM. Erst den Klick bestätigen, dann
            # die Panel-Nachricht vollständig entfernen.
            try:
                await interaction.response.defer()
                message = getattr(interaction, "message", None)
                if message is not None:
                    await message.delete()
                else:
                    await interaction.delete_original_response()
            except (discord.HTTPException, discord.NotFound, discord.InteractionResponded) as exc:
                log.warning("Adminpanel konnte nicht geschlossen werden: %s", exc)
            return

        if action == "search":
            await interaction.response.send_modal(AdminSearchModal(self))
            return

        if action == "select":
            values = (interaction.data or {}).get("values") or []
            if values:
                try:
                    guild_id = int(values[0])
                    view = await self._guild_detail_view(guild_id, page=page, query=query)
                    await self._show(interaction, view)
                    return
                except ValueError:
                    pass
            view = await self._admin_panel_view(page=page, query=query)
            await self._show(interaction, view)
            return

        if action == "guild":
            guild_id = parsed.get("guild_id")
            if guild_id is not None:
                view = await self._guild_detail_view(guild_id, page=page, query=query)
                await self._show(interaction, view)
            else:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
            return

        if action == "invite":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            if guild is None:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
                return

            invite_url: Optional[str] = None
            notice: Optional[str] = None
            try:
                # 1. Bevorzugt guild.invites.create() falls implementiert
                if hasattr(guild, "invites") and hasattr(guild.invites, "create"):
                    inv = await guild.invites.create(max_uses=1, max_age=3600, reason="Adminpanel-Einladung für den Bot-Owner")
                    invite_url = getattr(inv, "url", str(inv))
                else:
                    # 2. Suche nach passendem Kanal
                    target_channel = getattr(guild, "system_channel", None) or getattr(guild, "rules_channel", None)
                    if target_channel is None or not hasattr(target_channel, "create_invite"):
                        for c in getattr(guild, "text_channels", []) or []:
                            if hasattr(c, "create_invite"):
                                target_channel = c
                                break
                    if target_channel is None or not hasattr(target_channel, "create_invite"):
                        for c in getattr(guild, "channels", []) or []:
                            if hasattr(c, "create_invite"):
                                target_channel = c
                                break
                    if target_channel is None or not hasattr(target_channel, "create_invite"):
                        notice = "Kein Kanal gefunden, um eine Einladung zu erstellen."
                    else:
                        inv = await target_channel.create_invite(
                            max_uses=1, max_age=3600, reason="Adminpanel-Einladung für den Bot-Owner"
                        )
                        invite_url = getattr(inv, "url", str(inv))
            except discord.Forbidden:
                notice = "Fehlende Berechtigung: Dem Bot fehlt 'Sofortige Einladung erstellen' (create_instant_invite) auf diesem Server."
            except Exception as exc:
                notice = f"Einladung konnte nicht erstellt werden: {exc}"

            view = await self._guild_detail_view(
                guild, page=page, query=query, invite_url=invite_url, notice=notice
            )
            await self._show(interaction, view)
            return

        if action == "leave":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            if guild is None:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
                return
            view = admin_guild_leave_confirm_view(guild, page=page, query=query)
            await self._show(interaction, view)
            return

        if action == "leave_confirm":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            if guild is None:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
                return
            try:
                await guild.leave()
                log.info("Bot hat Server '%s' (ID %s) über das Adminpanel verlassen.",
                         guild.name, guild.id)
                if hasattr(self, "_connection") and hasattr(self._connection, "_guilds"):
                    self._connection._guilds.pop(guild.id, None)
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
            except Exception as exc:
                log.warning("Server '%s' (ID %s) konnte nicht verlassen werden: %s",
                            guild.name, guild.id, exc)
                view = await self._guild_detail_view(
                    guild, page=page, query=query,
                    notice=f"Server konnte nicht verlassen werden: {exc} (z. B. wenn der Bot Server-Owner ist)",
                )
                await self._show(interaction, view)
            return

        # nav, back oder sonstige Navigation
        view = await self._admin_panel_view(page=page, query=query)
        await self._show(interaction, view)

    async def _new_session(self, guild: discord.Guild, member: discord.Member, *, note: str):
        """
        Legt eine Sitzung mit den Defaults aus der Konfiguration an
        (Gültigkeit = ``SESSION_TTL_HOURS``, Modus = Lesen + Schreiben) und
        gibt ``(session, plaintext_token)`` zurück.
        """
        return await self.store.create(
            guild_id=guild.id,
            guild_name=guild.name,
            created_by=member.id,
            created_by_name=member.display_name,
            mode=DEFAULT_MODE,
            ttl_hours=self.config.session_ttl_hours,
            note=note,
        )

    # ── Command-Registrierung ────────────────────────────────────────────────
    def _register_commands(self) -> None:
        command_name = self.config.command_name

        @self.tree.command(
            name=command_name,
            description="🔗 Mit Arena AI verbinden (nur für dich sichtbar)",
        )
        @app_commands.guild_only()
        async def connect(interaction: discord.Interaction) -> None:
            """Der Haupt-Command: zeigt die Willkommens-Nachricht bzw. den verbundenen Zustand."""
            gate = await self._guard(interaction)
            if gate is None:
                return
            guild = gate["guild"]
            member = gate["member"]

            if self.store.active_for_guild(guild.id):
                # Bereits verbunden → verbundenen Zustand zeigen. Der Klartext
                # des alten Tokens liegt nicht mehr vor (gespeichert ist nur
                # sein Hash) — deshalb gibt es ein frisches Token; die alten
                # bleiben bis zu ihrem Ablauf gültig.
                session, token = await self._new_session(
                    guild, member, note="per /connect (bereits verbunden) erneuert"
                )
                self.state.maybe_save(force=True)
                try:
                    await interaction.response.send_message(
                        view=connected_view(self.state.base_url(), token), ephemeral=True,
                    )
                except discord.HTTPException as exc:
                    log.error("Antwort auf /%s fehlgeschlagen: %s", self.config.command_name, exc)
                    return
                log.info(
                    "/%s (bereits verbunden) von %s (%s) auf '%s' — frisches Token %s für Sitzung %s",
                    self.config.command_name, member.display_name, member.id, guild.name,
                    session.token_prefix, session.id,
                )
            else:
                # Noch nicht verbunden → Willkommen mit grünem Verbinden-Button.
                try:
                    await interaction.response.send_message(view=welcome_view(), ephemeral=True)
                except discord.HTTPException as exc:
                    log.error("Antwort auf /%s fehlgeschlagen: %s", self.config.command_name, exc)
                    return
                log.info(
                    "/%s (Willkommen, Bridge deaktiviert) von %s (%s) auf '%s'.",
                    self.config.command_name, member.display_name, member.id, guild.name,
                )

        @self.tree.command(
            name="adminpanel",
            description="🛠️ Server-Übersicht für den Bot-Owner (nur im Privatchat)",
        )
        @app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
        async def adminpanel(interaction: discord.Interaction) -> None:
            """Das Adminpanel: Server-Liste mit Suche und Blättern — Owner-only."""
            if interaction.guild is not None:
                await self._deny(
                    interaction, "Nur im Privatchat",
                    "Das Adminpanel funktioniert ausschließlich im **Privatchat mit dem Bot** — "
                    "die Serverliste ist nichts für öffentliche Channels.",
                )
                return
            if not self.config.bot_owner_id or interaction.user.id != self.config.bot_owner_id:
                await self._deny(
                    interaction, "Nur für den Bot-Owner",
                    "Dieser Command ist reserviert für den Besitzer dieses Bots.",
                )
                log.warning("adminpanel-Versuch von %s (%s) — nicht der Bot-Owner.",
                            interaction.user.name, interaction.user.id)
                return

            # Das Sammeln der Serverdaten kann (ohne privilegierte Intents)
            # ein paar REST-Aufrufe kosten — erst deferigen, dann liefern.
            #
            # ── Diagnostik ──────────────────────────────────────────────────
            # Zeitlich befristeter, aber bewusst dauerhaft belassener
            # Debug-Block (schmales Logging): defer() und followup.send()
            # werden EINZELN mit perf_counter gemessen und JEDE Exception
            # (nicht nur HTTPException!) wird mit vollem Traceback geloggt —
            # inklusive HTTP-Status/Code/Body bei discord.HTTPException. Das
            # hat den echten Bug hier gefunden: ein `SelectOption.emoji` mit
            # Variation-Selector (❗️) ließ Discord die followup.send()-Antwort
            # mit 400 „Invalid emoji“ ablehnen — ohne diesen Block sah man nur
            # den generischen discord.HTTPException-Text, aber weder Status
            # noch Feld-Pfad.
            t0 = time.perf_counter()
            defer_ok = False
            try:
                await interaction.response.defer()
                defer_ok = True
            except Exception as exc:  # noqa: BLE001 — wir wollen ALLES sehen
                log.exception(
                    "adminpanel: defer() fehlgeschlagen nach %.3fs — app_id=%s data=%r (%s)",
                    time.perf_counter() - t0, getattr(interaction, "application_id", None),
                    getattr(interaction, "data", None), type(exc).__name__,
                )
                return
            log.info("adminpanel: defer() OK nach %.3fs (app_id=%s)",
                      time.perf_counter() - t0, getattr(interaction, "application_id", None))

            t1 = time.perf_counter()
            try:
                view = await self._admin_panel_view(page=0, query="")
            except Exception:
                log.exception(
                    "adminpanel: Server-Liste konnte nicht gebaut werden (defer_ok=%s, %.3fs)",
                    defer_ok, time.perf_counter() - t1,
                )
                await self._deny(
                    interaction, "Adminpanel-Fehler",
                    "Die Server-Liste konnte nicht gebaut werden. Details stehen im Render-Log.",
                )
                return

            t2 = time.perf_counter()
            try:
                await interaction.followup.send(view=view)
                log.info("adminpanel: followup.send() OK nach %.3fs (defer+build+send=%.3fs)",
                          time.perf_counter() - t2, time.perf_counter() - t0)
            except discord.HTTPException as exc:
                # Nicht nur den String loggen — Klasse, HTTP-Status, Discord-
                # Fehlercode UND den Body zeigen genau, WELCHES Feld Discord
                # abgelehnt hat (z. B. ``options.0.emoji.name``).
                log.exception(
                    "adminpanel: followup.send() fehlgeschlagen nach %.3fs — "
                    "status=%s discord_error_code=%s text=%r app_id=%s",
                    time.perf_counter() - t2, getattr(exc, "status", None),
                    getattr(exc, "code", None), getattr(exc, "text", None),
                    getattr(interaction, "application_id", None),
                )
                # Der Owner soll trotzdem etwas sehen statt endlos auf
                # „Bot denkt nach…“ zu warten — der Interaction-Token lebt
                # nach dem defer() noch 15 Minuten, ein zweiter followup mit
                # einer einfachen Fehlermeldung (ohne den kaputten View) geht
                # praktisch immer durch.
                try:
                    await interaction.followup.send(
                        content="⛔ Adminpanel konnte nicht gesendet werden — Details im Render-Log.",
                    )
                except discord.HTTPException:
                    log.exception("adminpanel: auch die Fehlermeldung als followup schlug fehl.")
            except Exception:  # noqa: BLE001 — auch nicht-HTTP-Fehler sichtbar machen
                log.exception(
                    "adminpanel: followup.send() mit UNERWARTETER Exception nach %.3fs (app_id=%s)",
                    time.perf_counter() - t2, getattr(interaction, "application_id", None),
                )

        @self.tree.command(
            name="manage_database",
            description="🗄️ Eigene Turso-Datenbank verwalten (Speicher, Systeme, Setup)",
        )
        @app_commands.guild_only()
        async def manage_database(interaction: discord.Interaction) -> None:
            """Verwaltung der Server-Datenbank: Speicher-Auslastung, geordnete Systeme & Setup."""
            gate = await self._guard(interaction)
            if gate is None:
                return
            guild = gate["guild"]

            if not self.state or not getattr(self.state, "turso_service", None):
                await self._deny(interaction, "Dienst startet noch", "Turso-Service ist noch nicht bereit.")
                return

            configured = await self.state.turso_service.is_guild_db_configured(guild.id)
            if configured:
                status_data = await self.state.turso_service.get_guild_status(guild.id)
                view = database_configured_view(status_data)
            else:
                # Noch keine DB hier — aber hat dieser Admin woanders schon eine
                # eingerichtet? Dann gibt es den blauen Wiederverwenden-Button.
                try:
                    reusable = await self.state.turso_service.list_reusable_databases_for_user(
                        interaction.user.id, exclude_guild_id=guild.id,
                    )
                except Exception as exc:  # noqa: BLE001 — Anzeige-Feature darf den Command nie blockieren
                    log.warning("Wiederverwenden-Liste konnte nicht geladen werden: %s", exc)
                    reusable = []
                view = database_unconfigured_view(reusable=reusable)

            try:
                await interaction.response.send_message(view=view, ephemeral=True)
            except discord.HTTPException as exc:
                log.error("Antwort auf /manage_database fehlgeschlagen: %s", exc)

        # Hinweis: pro-Command on_error ist hier NICHT nötig — discord.py ruft
        # bei einem Fehler SOWOHL command.on_error ALS AUCH tree.on_error auf
        # (siehe CommandTree._dispatch_error). tree.on_error ist bereits in
        # __init__ gesetzt, daher würden diese Zuweisungen jede Fehlermeldung
        # doppelt senden. Bewusst weggelassen.

        # Kein add_listener() hier: das gibt es nur auf commands.Bot, nicht auf
        # discord.Client. Button-Klicks bedient stattdessen die Methode
        # ``on_interaction`` weiter unten — discord.py ruft für JEDE
        # Interaktion getattr(self, "on_interaction") auf.

    # ── Rechteprüfung ────────────────────────────────────────────────────────
    async def _guard(
        self, interaction: discord.Interaction, *, require_bot_admin: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Zentrale Prüfung vor jedem Command und Button-Klick.

        Liefert ``None``, wenn bereits eine Fehlerantwort gesendet wurde,
        sonst ``{"guild": …, "member": …}``.
        """
        guild = interaction.guild
        member = interaction.user

        if guild is None:
            await self._deny(
                interaction, "Nur auf einem Server",
                "Dieser Command funktioniert ausschließlich in einem Discord-Server, "
                "nicht in Direktnachrichten.",
            )
            return None

        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await self._deny(
                interaction, "Administrator-Rechte erforderlich",
                "Nur Mitglieder mit **Administrator**-Berechtigung dürfen einen KI-Zugriff "
                "freischalten.\n\n"
                "→ Servereinstellungen → Rollen → deine Rolle → *Administrator* aktivieren.",
                color=ERR_COLOR,
            )
            log.warning("%s (%s) versuchte /%s auf '%s' ohne Administrator-Rechte.",
                        getattr(member, "display_name", "?"), getattr(member, "id", "?"),
                        self.config.command_name, guild.name)
            return None

        if require_bot_admin:
            me = guild.me
            if me is None or not me.guild_permissions.administrator:
                invite = self.admin_invite_url()
                await self._deny(
                    interaction, "Der Bot braucht Administrator-Rechte",
                    "Damit Arena AI den Server wirklich komplett einrichten kann, muss "
                    "**dieser Bot Administrator** sein — genau wie du.\n\n"
                    "**So geht's:**\n"
                    "1. Servereinstellungen → Rollen → **"
                    f"{self.user.name if self.user else 'AIDiscordServerEinrichten'}**\n"
                    "2. *Administrator* aktivieren **und** die Rolle ganz nach oben schieben\n"
                    "3. `/connect` erneut ausführen"
                    + (f"\n\n**Oder Bot neu einladen:** {invite}" if invite else ""),
                    color=WARN_COLOR,
                )
                return None

        return {"guild": guild, "member": member}

    async def _deny(
        self, interaction: discord.Interaction, title: str, description: str,
        *, color: discord.Color = ERR_COLOR,
    ) -> None:
        embed = discord.Embed(title=f"⛔ {title}", description=description[:4000], color=color)
        embed.set_footer(text=self.config.bot_name)
        embed.timestamp = now_utc()
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException as exc:
            log.error("Fehlerantwort konnte nicht gesendet werden: %s", exc)

    # ── Button-Handler ───────────────────────────────────────────────────────
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """
        Wird von discord.py für JEDE Interaktion gerufen; Slash-Commands
        gehen über den CommandTree, hier zählen nur Komponenten-Klicks.

        Neue Components-V2-Buttons und die Legacy-IDs vor dem Upgrade werden
        beide bedient — alte Nachrichten laufen so nicht ins Leere.
        """
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")

        if custom_id == CID_CONNECT:
            await self._handle_connect(interaction)
        elif custom_id in (CID_DISCONNECT, CID_LEGACY_REVOKE_ALL):
            await self._handle_revoke_all(interaction)
        elif custom_id in (CID_REGENERATE, CID_LEGACY_REGENERATE):
            await self._handle_regenerate(interaction)
        elif custom_id == CID_DB_CONNECT:
            await self._handle_db_connect(interaction)
        elif custom_id == CID_DB_DISCONNECT:
            await self._handle_db_disconnect(interaction)
        elif custom_id == CID_DB_REFRESH:
            await self._handle_db_refresh(interaction)
        elif custom_id == CID_DB_SYSTEMS:
            await self._handle_db_systems(interaction)
        elif custom_id == CID_DB_SYS_DEL:
            await self._handle_db_delete_system(interaction)
        elif custom_id.startswith(f"{CID_DB_REUSE}:"):
            await self._handle_db_reuse(interaction, custom_id)
        elif custom_id.startswith(CID_ADMIN_PREFIX):
            await self._handle_admin_button(interaction, custom_id)

    async def _handle_connect(self, interaction: discord.Interaction) -> None:
        """Grüner „Verbinden“-Button: erzeugt das Token und bearbeitet die Nachricht."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        member = gate["member"]

        session, token = await self._new_session(guild, member, note="per Verbinden-Button erzeugt")
        self.state.maybe_save(force=True)
        await self._show(interaction, connected_view(self.state.base_url(), token))
        log.info(
            "Verbinden-Button von %s (%s) auf '%s' — Sitzung %s, Token %s, Modus %s.",
            member.display_name, member.id, guild.name, session.id,
            session.token_prefix, session.mode,
        )

    async def _handle_revoke_all(self, interaction: discord.Interaction) -> None:
        """Roter „Verbindung trennen“-Button: alle Tokens widerrufen, zurück zum Willkommen."""
        gate = await self._guard(interaction, require_bot_admin=False)
        if gate is None:
            return
        guild = gate["guild"]
        revoked = await self.store.revoke(guild_id=guild.id, by=f"button:{interaction.user.id}")
        self.state.maybe_save(force=True)
        await self._show(interaction, welcome_view())
        log.info(
            "Verbindung getrennt: %s (%s) hat %d Sitzung(en) auf '%s' widerrufen.",
            gate["member"].display_name, interaction.user.id, len(revoked), guild.name,
        )

    async def _handle_regenerate(self, interaction: discord.Interaction) -> None:
        """„Neues Token generieren“-Button: alte Tokens ungültig, frisches Token zeigen."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        member = gate["member"]

        await self.store.revoke(guild_id=guild.id, by=f"button:{interaction.user.id}")
        session, token = await self._new_session(guild, member, note="per Button neu generiert")
        self.state.maybe_save(force=True)
        await self._show(interaction, connected_view(self.state.base_url(), token))
        log.info(
            "Neues Token generiert: %s (%s) auf '%s' — Sitzung %s, Token %s "
            "(alte Tokens dieser Guild sind ungültig).",
            member.display_name, member.id, guild.name, session.id, session.token_prefix,
        )

    async def _handle_db_connect(self, interaction: discord.Interaction) -> None:
        """Öffnet das Modal zur Eingabe der Turso-Zugangsdaten."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        await interaction.response.send_modal(TursoConnectModal(self, gate["guild"]))

    async def _handle_db_reuse(self, interaction: discord.Interaction, custom_id: str) -> None:
        """
        Blauer „DB von … nutzen“-Button: verknüpft diesen Server mit der
        bereits vorhandenen Datenbank desselben Admins von einem anderen Server.
        """
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        service = getattr(self.state, "turso_service", None) if self.state else None
        if service is None:
            await interaction.response.send_message("❌ Turso-Service ist noch nicht bereit.", ephemeral=True)
            return

        raw = custom_id[len(CID_DB_REUSE) + 1:]
        try:
            source_guild_id = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Dieser Button ist nicht mehr gültig — führe `/manage_database` erneut aus.",
                ephemeral=True,
            )
            return

        ok, msg = await service.reuse_guild_database(
            guild_id=guild.id,
            guild_name=guild.name,
            source_guild_id=source_guild_id,
            configured_by=f"{interaction.user.display_name} ({interaction.user.id})",
            configured_by_id=str(interaction.user.id),
        )
        if not ok:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
            return

        status_data = await service.get_guild_status(guild.id)
        # Auch hier: KEIN ``content`` zu einer Components-V2-Nachricht, sonst
        # 400 (50035) — die Erfolgszeile steht als TextDisplay im Container.
        try:
            await interaction.response.send_message(
                view=database_configured_view(status_data, notice=f"✅ {msg}"),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            # Die Datenbank IST verknüpft — nur die hübsche Ansicht ging nicht
            # raus. Fallback auf eine schlichte Textmeldung, damit der Admin
            # nicht „Interaktion fehlgeschlagen“ sieht.
            log.error("Reuse-Bestätigung als Components V2 abgelehnt (%s) — Text-Fallback.", exc)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"✅ {msg}", ephemeral=True)
                else:
                    await interaction.response.send_message(f"✅ {msg}", ephemeral=True)
            except discord.HTTPException:
                log.exception("Auch der Text-Fallback der Reuse-Bestätigung schlug fehl.")
        log.info(
            "Bestehende Datenbank von Guild %s für '%s' (%s) wiederverwendet durch %s (%s).",
            source_guild_id, guild.name, guild.id, gate["member"].display_name, interaction.user.id,
        )

    async def _handle_db_disconnect(self, interaction: discord.Interaction) -> None:
        """Trennt die Turso-Datenbank und zeigt die unkonfigurierte Ansicht."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        if self.state and getattr(self.state, "turso_service", None):
            await self.state.turso_service.disconnect_guild_database(guild.id)
        await self._show(interaction, database_unconfigured_view())
        log.info("Turso-DB für '%s' getrennt von %s (%s).", guild.name, gate["member"].display_name, interaction.user.id)

    async def _handle_db_refresh(self, interaction: discord.Interaction) -> None:
        """Aktualisiert die Status-Ansicht der Turso-Datenbank."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        if not self.state or not getattr(self.state, "turso_service", None):
            return
        configured = await self.state.turso_service.is_guild_db_configured(guild.id)
        if configured:
            status_data = await self.state.turso_service.get_guild_status(guild.id)
            await self._show(interaction, database_configured_view(status_data))
        else:
            await self._show(interaction, database_unconfigured_view())

    async def _handle_db_systems(self, interaction: discord.Interaction) -> None:
        """Öffnet das System-Management-Menü."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        if not self.state or not getattr(self.state, "turso_service", None):
            return
        systems = await self.state.turso_service.list_systems(guild.id)
        if not systems:
            await interaction.response.send_message(
                "Es sind noch keine Systeme eingerichtet. Sag Arena AI, was für ein System (z. B. Self Roles, Willkommen) er bauen soll!",
                ephemeral=True,
            )
            return
        view = database_manage_systems_view(systems)
        await interaction.response.send_message(
            "📋 **Systeme verwalten** — Wähle ein System aus dem Menü, um es zu löschen:",
            view=view,
            ephemeral=True,
        )

    async def _handle_db_delete_system(self, interaction: discord.Interaction) -> None:
        """Löscht das ausgewählte System aus der Turso-Datenbank."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        values = (interaction.data or {}).get("values", [])
        if not values or not self.state or not getattr(self.state, "turso_service", None):
            return
        sys_id = values[0]
        deleted = await self.state.turso_service.delete_system(guild.id, sys_id)
        if deleted:
            await interaction.response.send_message(f"🗑️ System `{sys_id}` wurde erfolgreich gelöscht.", ephemeral=True)
            log.info("System %s auf '%s' gelöscht von %s.", sys_id, guild.name, gate["member"].display_name)
        else:
            await interaction.response.send_message(f"System `{sys_id}` konnte nicht gefunden werden.", ephemeral=True)

    async def _show(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        """
        Bearbeitet die Container-V2-Nachricht, auf der der Button sitzt.

        Components-V2-Nachrichten haben keinen ``content`` — beim Umstylen
        müssen ``content``/``embeds``/``attachments`` explizit geleert werden
        (sonst bleibt Alter Content stehen bzw. Discord lehnt ab). Klappt das
        Bearbeiten nicht mehr (Nachricht weg/zu alt), gibt es eine frische
        ephemeral Nachricht als Fallback.
        """
        try:
            await interaction.response.edit_message(
                view=view, content=None, embeds=[], attachments=[],
            )
            return
        except (discord.HTTPException, discord.InteractionResponded) as exc:
            log.warning("Nachricht konnte nicht bearbeitet werden (%s) — sende eine neue.", exc)
        try:
            await interaction.followup.send(view=view, ephemeral=True)
        except discord.HTTPException as exc:
            log.error("Ersatznachricht konnte nicht gesendet werden: %s", exc)

    # ── Fehlerbehandlung ─────────────────────────────────────────────────────
    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        inner = getattr(error, "__cause__", error)

        if isinstance(error, app_commands.MissingPermissions):
            await self._deny(
                interaction, "Administrator-Rechte erforderlich",
                "Dieser Command ist nur für Mitglieder mit **Administrator**-Berechtigung.",
            )
            return
        if isinstance(error, app_commands.BotMissingPermissions):
            await self._deny(
                interaction, "Dem Bot fehlen Rechte",
                f"Bitte gib dem Bot: {', '.join(error.missing_permissions)}",
                color=WARN_COLOR,
            )
            return
        if isinstance(error, app_commands.NoPrivateMessage):
            await self._deny(interaction, "Nur auf einem Server",
                             "Dieser Command funktioniert nicht in Direktnachrichten.")
            return
        if isinstance(error, app_commands.CommandOnCooldown):
            await self._deny(
                interaction, "Zu schnell",
                f"Bitte warte {error.retry_after:.1f} Sekunden.",
                color=WARN_COLOR,
            )
            return
        if isinstance(inner, ApiError):
            await self._deny(interaction, inner.code, f"{inner.message}\n\n{inner.hint or ''}",
                             color=WARN_COLOR)
            return

        log.exception("Command-Fehler", exc_info=inner)
        await self._deny(
            interaction, "Interner Fehler",
            f"```{type(inner).__name__}: {str(inner)[:600]}```\n"
            "Details stehen im Render-Log.",
            color=ERR_COLOR,
        )
