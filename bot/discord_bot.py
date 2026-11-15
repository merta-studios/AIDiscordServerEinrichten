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
import io
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands

from . import __version__
from .config import Config, mask_proxy_url
from .sessions import DEFAULT_MODE, SessionStore
from .util import ApiError, now_utc
from .ram import (RAM_CRITICAL_MB, current_rss_mb, ram_report_loop,
                  relieve_ram)
from .web.pdf_document import build_channel_history_pdf, build_server_document

log = logging.getLogger("relay.discord")

# ── Server-Dokument-Zustellung ───────────────────────────────────────────────
# Das Dokument geht an den BOT-Owner (Application-Owner), nicht an den
# Server-Owner — der Bot-Betreiber ist der Empfänger.
#
# Vor jeder neuen Zustellung werden frühere „server-dokument-*.pdf"-DMs
# gelöscht (beim letzten Empfänger aus data/server_document_delivery.json und
# beim neuen Empfänger) — es gibt immer nur EIN aktuelles Dokument. Kein
# Start-Scan mehr: das einmalige Legacy-Aufräumen (Server-Owner
# 1015212381619626014) ist mit dem Deployment vom 14.11.2026 abgeschlossen
# (Log: „Server-Dokument-Aufräumen beim Start: 1 DM-Nachricht(en) gelöscht").
DOCUMENT_DELIVERY_PATH = "data/server_document_delivery.json"

__all__ = (
    "RelayClient", "build_intents", "ADMIN_INVITE_SCOPES", "ARENA_URL",
    "connected_view", "welcome_view", "admin_panel_view",
    "admin_guild_detail_view", "admin_guild_leave_confirm_view",
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

# Legacy-IDs der Buttons vor dem Components-V2-Upgrade. Nachrichten aus der
# Zeit vor dem Deploy sollen nicht ins Leere laufen: Ihre Klicks werden auf
# die neuen Handler gemappt (die bearbeiten die alte Nachricht dann in die
# neue Optik).
CID_LEGACY_REGENERATE = "relay:regenerate"
CID_LEGACY_REVOKE_ALL = "relay:revoke_all"

#: Adminpanel: „Server-Dokument erstellen“-Button (Detailansicht eines Servers).
CID_ADMIN_CREATE_DOC = "relay:admin:create_doc"

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

def manage_database_unavailable_view() -> discord.ui.LayoutView:
    """
    Container-V2-Nachricht für /manage_database.

    Das Datenbank-System (Turso) wurde entfernt. Der Command existiert nur noch,
    um diesen Hinweis anzuzeigen: die Funktion ist aktuell nicht verfügbar,
    weil die Entwickler sie noch überarbeiten — damit Arena AI später Dinge
    coden kann, die der Bot auf dem Server immer ausführt.
    """
    container = discord.ui.Container(
        discord.ui.TextDisplay(
            "# 🗄️ Server-Datenbank\n\n"
            "Diese Funktion ist **aktuell nicht mehr verfügbar**.\n\n"
            "Die Entwickler arbeiten noch daran, die Funktion zu verbessern — "
            "damit **Arena AI Sachen coden kann, die der Bot immer ausführt**.\n\n"
            "Sobald das System wieder bereitsteht, wird `/manage_database` hier "
            "die Verwaltung der Server-Datenbank anbieten."
        ),
        discord.ui.Separator(),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Schließen",
                emoji="✖️",
                style=discord.ButtonStyle.secondary,
                custom_id=_admin_custom_id("close", 0, ""),
            ),
        ),
        accent_colour=WARN_COLOR,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


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

    if action in ("guild", "invite", "leave", "leave_confirm", "create_doc", "history"):
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

    # Zwei Reihen: Discord erlaubt max. 5 Buttons pro ActionRow.
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
            label="Server-Dokument erstellen", emoji="📄", style=discord.ButtonStyle.secondary,
            custom_id=_admin_guild_custom_id("create_doc", guild.id, page, query),
        ),
        discord.ui.Button(
            label="Chatverlauf abrufen", emoji="💬", style=discord.ButtonStyle.secondary,
            custom_id=_admin_guild_custom_id("history", guild.id, page, query),
        ),
        discord.ui.Button(
            label="Schließen", emoji="✖️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("close", page, query),
        ),
    ]

    components.extend([
        discord.ui.Separator(),
        discord.ui.ActionRow(*buttons[:4]),
        discord.ui.ActionRow(*buttons[4:]),
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


def _progress_bar(pct: int, width: int = 10) -> str:
    """Ladebalken aus Emojis: 🟩 gefüllt, ⬜ offen (10 Segmente)."""
    filled = max(0, min(width, int(round(width * max(0, min(pct, 100)) / 100.0))))
    return "🟩" * filled + "⬜" * (width - filled)


def startup_notify_view(
    *,
    ram_mb: float,
    version: str,
    ready_at: Optional[Any] = None,
) -> discord.ui.LayoutView:
    """
    Startup-DM an den Bot-Owner: bestätigt den (Neu)Start sofort — inkl.
    RAM-Stand und dem Hinweis, dass automatische Neustarts (OOM/Render)
    damit sofort sichtbar sind.
    """
    ts = getattr(ready_at, "strftime", lambda _f: "?")("%d.%m.%Y, %H:%M UTC") if ready_at else "?"
    ram_text = f"{ram_mb:.0f} MB" if ram_mb and ram_mb > 0 else "unbekannt"
    container = discord.ui.Container(
        discord.ui.TextDisplay("# 🟢 Bot ist online"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"**Neustart abgeschlossen:** {ts}\n"
            f"**Version:** v{version}\n"
            f"**RAM aktuell:** {ram_text} (Container-Limit ~512 MB)"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "Wenn du diesen Start nicht ausgelöst hast (Deploy oder „Restart“ "
            "in Render), war es ein **automatischer Neustart** — typische "
            "Ursache: zu wenig RAM (OOM) oder ein Fehler im Prozess. Der "
            "RAM-Verbrauch wird alle 60 s in die Render-Logs geschrieben "
            "(Warn ab 300 MB, kritisch ab 420 MB); der Chatverlauf-Abruf "
            "verschickt große Verläufe automatisch in mehreren Teil-PDFs, "
            "damit der RAM flach bleibt."
        ),
        accent_colour=CONNECTED_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


class AdminHistoryModal(discord.ui.Modal, title="💬 Chatverlauf abrufen"):
    """
    Fragt die Kanal-ID ab (steht im Server-Dokument) und schickt danach das
    komplette Chatverlauf-PDF an den **Bot-Owner** — niemals an einen Server
    oder den Server-Owner.
    """

    channel_id = discord.ui.TextInput(
        label="Kanal-ID (steht im Server-Dokument)",
        placeholder="z. B. 1234567890123456789",
        min_length=5,
        max_length=25,
        required=True,
    )

    def __init__(self, client: "RelayClient", guild: Optional[discord.Guild]) -> None:
        super().__init__(timeout=300)
        self.client = client
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """
        Erste Amtshandlung: die Interaktion SOFORT beantworten (Callback Typ 4)
        — bevor Guards, Kanalsuche (REST!) oder sonst etwas läuft. Discord
        zeigt sonst bei jeder Verzögerung/hängenden Antwort den generischen
        Dialog „Etwas ist schief gelaufen“. Danach läuft die gesamte Logik in
        try/except und meldet jedes Ergebnis per Edit sichtbar.
        """
        raw = (self.channel_id.value or "").strip()
        try:
            # Normale (nicht-ephemerale) Nachricht: kein „Nur du kannst das
            # sehen“-Banner, kein Verwerfen-Button — sie bleibt bestehen,
            # auch wenn der Discord-Client neu geöffnet wird.
            await interaction.response.send_message(
                "💬 Chatverlauf wird erstellt … (Kanal wird gesucht)",
            )
        except discord.HTTPException as exc:
            log.warning("Modal-Antwort konnte nicht gesendet werden: %s — Verarbeitung läuft weiter.", exc)

        progress_msg: Any = None
        try:
            progress_msg = await interaction.original_response()
        except Exception:  # noqa: BLE001 — ohne Original-Nachricht: Followup
            progress_msg = None

        async def _final(content: str) -> None:
            if progress_msg is not None:
                try:
                    await progress_msg.edit(
                        content=f"{content}\n{_progress_bar(100)}"
                    )
                    return
                except Exception:  # noqa: BLE001 — Token/Netz egal
                    pass
            try:
                await interaction.followup.send(content)
            except Exception:  # noqa: BLE001 — dann ist der Token endgültig tot
                log.warning("Chatverlauf-Ergebnis konnte nicht angezeigt werden: %s", content[:80])

        try:
            # Gleiches Guard-Prinzip wie das Panel: nur der Bot-Owner.
            if int(getattr(self.client.config, "bot_owner_id", 0) or 0) == 0:
                await _final("ℹ️ Chatverlauf-Abruf ist nicht aktiviert (kein `BOT_OWNER_ID` gesetzt).")
                return
            if interaction.user.id != self.client.config.bot_owner_id:
                log.warning("Chatverlauf-Abruf von %s — nicht der Bot-Owner.",
                            getattr(interaction.user, "id", "?"))
                await _final("⛔ Nur der Bot-Owner kann den Chatverlauf abrufen.")
                return

            if not raw.isdigit():
                await _final(
                    "❌ Das ist keine Kanal-ID — bitte nur Ziffern eingeben "
                    "(die ID steht im Server-Dokument beim jeweiligen Kanal)."
                )
                return
            cid = int(raw)

            # 1) Im Server, aus dem der Button kam; 2) in allen Servern des Bots —
            # der Bot muss in dem Server sein, sonst gibt es den Kanal für ihn nicht.
            channel = await self.client._resolve_history_channel(cid, self.guild)
            if channel is None:
                await _final(
                    "❌ Kanal nicht gefunden — der Bot ist (nicht mehr) in dem Server "
                    f"mit dem Kanal `{cid}`, oder die ID ist falsch."
                )
                return
            if not hasattr(channel, "history") or not isinstance(
                channel, (discord.TextChannel, discord.Thread)
            ):
                await _final(
                    "❌ Dieser Kanal hat keinen abrufbaren Chatverlauf "
                    "(z. B. Sprachkanal, Kategorie oder Forum selbst)."
                )
                return

            await _final(f"💬 Chatverlauf von **#{getattr(channel, 'name', '?')}** wird erstellt …")
            await self.client._handle_admin_history(interaction, channel)
        except Exception as error:  # noqa: BLE001 — JEDER Fehler wird sichtbar
            log.exception("Fehler im Chatverlauf-Abruf-Fenster.")
            await _final(f"⚠️ Chatverlauf-Abruf fehlgeschlagen: {error}")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        log.exception("Fehler im Chatverlauf-Abruf-Fenster.", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ Chatverlauf-Abruf fehlgeschlagen: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"⚠️ Chatverlauf-Abruf fehlgeschlagen: {error}", ephemeral=True)
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
        parts: List[str] = [
            f"🧹 **Aufräumen:** {cleanup.get('sessions_revoked', 0)} KI-Verbindung(en) gestoppt."
        ]
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
        # Startup-DM: genau EINMAL pro Prozess (ein OOM-/Crash-Restart ist ein
        # neuer Prozess → die DM kommt erneut — genau das gewünschte Signal).
        self._startup_dm_sent: bool = False
        self._ram_monitor_task: Optional[asyncio.Task[Any]] = None
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

        # Startup-DM an den Bot-Owner (einmal pro Prozess) — meldet jeden
        # (Neu)Start sofort, z. B. automatische Neustarts durch OOM/Render.
        await self._startup_dm_once()
        self._ensure_ram_monitor()

    async def _startup_dm_once(self) -> None:
        """Sendet die Startup-DM — garantiert genau EINMAL pro Prozess."""
        if self._startup_dm_sent:
            return
        self._startup_dm_sent = True
        try:
            await self._send_startup_dm()
        except Exception:  # noqa: BLE001 — Startup darf nie am DM hängen
            log.exception("Startup-DM konnte nicht gesendet werden.")

    async def _send_startup_dm(self) -> bool:
        """
        DM an den Bot-Owner: „Bot ist online“ mit RAM-Stand und dem klaren
        Hinweis, dass (automatische) Neustarts — z. B. OOM — sofort sichtbar
        sind. Gesendet wird genau einmal pro Prozess.
        """
        owner = self._bot_owner()
        if owner is None:
            log.debug("Startup-DM übersprungen — kein Bot-Owner konfiguriert.")
            return False
        view = startup_notify_view(
            ram_mb=current_rss_mb(),
            version=__version__,
            ready_at=self.ready_at,
        )
        sent = await self._dm_user(owner, view, context="startup")
        if sent:
            log.info("Startup-DM an den Bot-Owner gesendet.")
        return sent

    def _ensure_ram_monitor(self) -> None:
        """Startet den RAM-Monitor (einmalig; beim Neustart des Prozesses neu)."""
        task = getattr(self, "_ram_monitor_task", None)
        if task is not None and not task.done():
            return
        self._ram_monitor_task = asyncio.create_task(ram_report_loop())

    async def close(self) -> None:
        """Fährt den Client herunter und stoppt den RAM-Monitor."""
        task = getattr(self, "_ram_monitor_task", None)
        if task is not None:
            task.cancel()
        await super().close()

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
        Löscht alle Server-abhängigen Daten — läuft bei **jedem** Verlassen
        (Kick, Ban oder freiwillig), noch bevor der Owner informiert wird.

        * Alle aktiven KI-Sitzungen (Tokens) des Servers werden sofort widerrufen.

        Darf niemals die Leave-Benachrichtigung blockieren — Fehler werden
        geloggt und als Summary zurückgegeben.
        """
        cleanup: Dict[str, Any] = {"sessions_revoked": 0}

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

        return admin_guild_detail_view(
            guild,
            owner=guild_owner,
            owner_present=owner_present,
            sessions=sessions,
            page=page,
            query=query,
            invite_url=invite_url,
            notice=notice,
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

        if action == "create_doc":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            if guild is None:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._handle_admin_create_doc(interaction, guild)
            return

        if action == "history":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            # Kanal-ID wird im Modal abgefragt (steht im Server-Dokument).
            await interaction.response.send_modal(AdminHistoryModal(self, guild))
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

    # ── Server-Dokument: Zustellung & Aufräumen ──────────────────────────────
    def _doc_delivery_state_path(self) -> str:
        return DOCUMENT_DELIVERY_PATH

    def _load_doc_delivery(self) -> Dict[str, Any]:
        try:
            with open(self._doc_delivery_state_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def _save_doc_delivery(self, state: Dict[str, Any]) -> None:
        """Schreibt den Zustell-Status atomar (Temp-File + os.replace)."""
        path = self._doc_delivery_state_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Server-Dokument-Zustell-Status konnte nicht gespeichert werden: %s", exc)

    @staticmethod
    def _is_document_message(message: Any) -> bool:
        """True, wenn die Nachricht ein „server-dokument-*.pdf" anhängen hat."""
        for att in getattr(message, "attachments", None) or []:
            name = (getattr(att, "filename", "") or "").lower()
            if name.startswith("server-dokument-") and name.endswith(".pdf"):
                return True
        return False

    async def _delete_document_dms_for(self, user_id: int, *, limit: int = 200) -> int:
        """Löscht alle „server-dokument-*.pdf"-DMs an einen Empfänger. Beste Effort."""
        try:
            user = self.get_user(user_id)
            if user is None:
                user = await self.fetch_user(user_id)
            dm = getattr(user, "dm_channel", None) or await user.create_dm()
        except discord.HTTPException as exc:
            log.warning("DM-Kanal für %s nicht erreichbar: %s", user_id, exc)
            return 0
        deleted = 0
        try:
            async for message in dm.history(limit=limit):
                if self._is_document_message(message):
                    try:
                        await message.delete()
                        deleted += 1
                    except discord.HTTPException as exc:
                        log.warning("DM %s konnte nicht gelöscht werden: %s", message.id, exc)
        except discord.HTTPException as exc:
            log.warning("DM-Verlauf für %s nicht lesbar: %s", user_id, exc)
        return deleted

    @staticmethod
    def _is_history_document_message(message: Any) -> bool:
        """True, wenn die Nachricht ein „chatverlauf-*.pdf“ anhängen hat."""
        for att in getattr(message, "attachments", None) or []:
            name = (getattr(att, "filename", "") or "").lower()
            if name.startswith("chatverlauf-") and name.endswith(".pdf"):
                return True
        return False

    async def _cleanup_previous_history_documents(self, new_recipient_id: int) -> int:
        """
        Löscht bereits zugestellte **Chatverlauf**-PDFs („chatverlauf-*.pdf“)
        beim letzten Empfänger und beim neuen — es bleibt immer nur EIN
        aktuelles Chatverlauf-PDF pro Zustellung übrig. Fehler blockieren nie.
        """
        state = self._load_doc_delivery()
        record = state.get("last_history_delivery") or {}
        targets: List[int] = []
        rid = record.get("recipient_id")
        if rid is not None and str(rid).lstrip("-").isdigit():
            targets.append(int(rid))
        if new_recipient_id not in targets:
            targets.append(int(new_recipient_id))
        total = 0
        for uid in targets:
            try:
                user = self.get_user(uid) or await self.fetch_user(uid)
                dm = getattr(user, "dm_channel", None) or await user.create_dm()
            except discord.HTTPException:
                continue
            deleted = 0
            try:
                async for message in dm.history(limit=200):
                    if self._is_history_document_message(message):
                        try:
                            await message.delete()
                            deleted += 1
                        except discord.HTTPException as exc:
                            log.warning("Chatverlauf-DM %s konnte nicht gelöscht werden: %s", message.id, exc)
            except discord.HTTPException as exc:
                log.warning("DM-Verlauf für %s nicht lesbar: %s", uid, exc)
            total += deleted
        if targets:
            state["last_history_delivery"] = {}
            self._save_doc_delivery(state)
        return total

    async def _resolve_history_channel(
        self, channel_id: int, context_guild: Optional[discord.Guild] = None,
        *, guilds: Optional[List[discord.Guild]] = None,
    ) -> Optional[Any]:
        """
        Findet einen Kanal anhand seiner ID — der Bot muss in dem Server sein.
        Zuerst im Kontext-Server (aus dem der Button kam), dann in allen
        Servern des Bots (Cache, sonst REST-Fetch). ``guilds`` überschreibt
        die Serverliste (für Tests).
        """
        if guilds is None:
            guilds = []
            if context_guild is not None:
                guilds.append(context_guild)
            try:
                guilds.extend(g for g in self.guilds if g.id != getattr(context_guild, "id", None))
            except (AttributeError, TypeError):
                pass  # z. B. Client ohne ConnectionState — Kontext-Server genügt
        for guild in guilds:
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException,
                        AttributeError, TypeError):
                    channel = None
            if channel is not None:
                return channel
        return None

    async def _cleanup_previous_documents(self, new_recipient_id: Optional[int] = None) -> int:
        """
        Löscht bereits zugestellte Server-Dokument-DMs — beim letzten
        Empfänger (Status-File) und beim neuen Empfänger — bevor ein neues
        Dokument geht. Fehler blockieren niemals.
        """
        state = self._load_doc_delivery()
        record = state.get("last_delivery") or {}
        targets: List[int] = []
        rid = record.get("recipient_id")
        if rid is not None and str(rid).lstrip("-").isdigit():
            targets.append(int(rid))
        if new_recipient_id is not None:
            if new_recipient_id not in targets:
                targets.append(int(new_recipient_id))
        total = 0
        for uid in targets:
            total += await self._delete_document_dms_for(uid)
        if targets:
            cleaned = {int(x) for x in (state.get("cleaned_recipient_ids") or [])
                       if str(x).lstrip("-").isdigit()}
            cleaned.update(targets)
            state["cleaned_recipient_ids"] = sorted(cleaned)
            state.pop("last_delivery", None)
            self._save_doc_delivery(state)
        return total

    async def _resolve_document_recipient(
        self, guild: discord.Guild
    ) -> Tuple[Optional[Any], str]:
        """
        Empfänger des Server-Dokuments: der Bot-Owner (Application-Owner) —
        NICHT der Server-Owner. Fallback (nur wenn die Application nicht
        lesbar ist): der Server-Owner, damit das Dokument nie verloren geht.
        Rückgabe: ``(recipient, kind)`` mit kind ∈ {"bot_owner", "guild_owner_fallback"}.
        """
        try:
            app_info = await self.application_info()
            if app_info is not None and getattr(app_info, "owner", None) is not None:
                return app_info.owner, "bot_owner"
        except discord.HTTPException as exc:
            log.warning("Application-Owner nicht ermittelbar (%s) — Fallback auf Server-Owner.", exc)
        owner = guild.owner
        if owner is None and guild.owner_id:
            try:
                owner = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                owner = None
        return owner, "guild_owner_fallback"

    async def _handle_admin_create_doc(
        self, interaction: discord.Interaction, guild: discord.Guild
    ) -> None:
        """
        Erstellt ein Server-Dokument (PDF) und sendet es per DM an den Bot-Owner.

        Wird aus _handle_admin_button aufgerufen (Action "create_doc"),
        wobei ``interaction.response`` bereits deferred wurde.

        Der Ablauf:
        1. ``build_server_document`` generiert das PDF (Kanäle, Rollen, Mitglieder, …).
        2. Zuvor zugestellte Dokument-DMs werden gelöscht (letzter Empfänger +
           Legacy-Empfänger) — es gibt immer nur EIN aktuelles Dokument.
        3. Das PDF wird als Datei per DM an den **Bot-Owner** gesendet
           (Application-Owner; Fallback: Server-Owner).
        4. Der Admin erhält eine finale Bestätigung (ephemeral).
        """
        recipient, recipient_kind = await self._resolve_document_recipient(guild)
        if recipient is None:
            await interaction.followup.send(
                "Weder der Bot-Owner noch der Server-Owner konnte ermittelt werden — "
                "das Dokument kann nicht versandt werden.",
            )
            return

        # Fortschrittsmeldung als normale Nachricht (KEIN „Nur du kannst das
        # sehen“-Banner, kein Verwerfen-Button, bleibt beim Client-Neustart).
        progress_msg = await interaction.followup.send(
            f"📄 **Server-Dokument** für **{guild.name}**\n{_progress_bar(0)}\n"
            "Server-Daten werden gesammelt …",
            wait=True,
        )

        async def _progress(percent: int, text: str) -> None:
            try:
                await asyncio.wait_for(
                    progress_msg.edit(
                        content=(
                            f"📄 **Server-Dokument** für **{guild.name}**\n"
                            f"{_progress_bar(percent)} {percent} %\n{text}"
                        ),
                    ),
                    timeout=10,
                )
            except Exception:  # noqa: BLE001 — Fortschritt darf niemals blockieren
                pass  # z. B. abgelaufenes Interaktions-Token — der Build läuft weiter

        try:
            http_session = getattr(self.state, "http_session", None)
            if http_session is None:
                http_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60, connect=15),
                )
                own_session = True
            else:
                own_session = False
            try:
                pdf_bytes = await build_server_document(guild, http_session, progress=_progress)
            finally:
                if own_session:
                    await http_session.close()

            file = discord.File(io.BytesIO(pdf_bytes), filename=f"server-dokument-{guild.id}.pdf")

            # Alte Dokument-DMs bleiben bewusst BESTEHEN (Wunsch: nichts wird
            # mehr gelöscht) — jedes Dokument wird einfach neu zugestellt.

            kind_text = (
                "Bot-Owner (Empfänger dieses Dokuments)"
                if recipient_kind == "bot_owner"
                else f"Server-Owner — Fallback, Bot-Owner nicht ermittelbar ({recipient.display_name})"
            )
            try:
                sent = await recipient.send(
                    content=(
                        f"📄 **Server-Dokument für {guild.name}** (ID `{guild.id}`)\n\n"
                        "Dieses Dokument enthält eine vollständige Übersicht des Servers: "
                        "Kanäle & Kategorien, Rollen mit Farben und Berechtigungen, Emojis & Sticker, "
                        "Mitglieder sowie die letzten Nachrichten pro Textkanal.\n\n"
                        "Es wurde auf Anfrage über das Adminpanel erstellt und als vertrauliches "
                        "Dokument an dich (Bot-Owner) versendet. Der Server selbst hat nichts davon bemerkt.\n\n"
                        f"_Empfänger: {kind_text}_"
                    ),
                    file=file,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Server-Dokument konnte nicht per DM an %s (%s) gesendet werden: %s",
                            recipient.display_name, kind_text, exc)
                await progress_msg.edit(
                    content=(
                        f"❌ Das PDF wurde erstellt, konnte aber **nicht per DM an {recipient.display_name}** "
                        f"({kind_text.split(' — ')[0]}) zugestellt werden.\n"
                        "Mögliche Ursache: DMs vom Bot sind deaktiviert oder es gibt keinen gemeinsamen Server."
                    ),
                )
                return

            # Zustellung merken (Empfänger + Nachricht) — das nächste Dokument
            # löscht genau diese DM automatisch. Bestehenden Stand (insb.
            # ``cleaned_recipient_ids``) erhalten.
            state = self._load_doc_delivery()
            state["last_delivery"] = {
                "recipient_id": recipient.id,
                "recipient_kind": recipient_kind,
                "message_id": sent.id,
                "guild_id": guild.id,
                "guild_name": guild.name,
                "sent_at": now_utc().isoformat(),
            }
            self._save_doc_delivery(state)

            await progress_msg.edit(
                content=(
                    f"✅ Server-Dokument für **{guild.name}** erstellt und per DM an "
                    f"**{recipient.display_name}** ({kind_text.split(' — ')[0]}) gesendet.\n"
                    f"{_progress_bar(100)}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — jede Fehlermeldung wird dem Admin angezeigt
            log.exception("Fehler beim Erstellen des Server-Dokuments für '%s'", guild.name)
            try:
                await progress_msg.edit(content=f"❌ Fehler beim Erstellen des Dokuments: {exc}")
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                await interaction.followup.send(f"❌ Fehler: {exc}")

    async def _handle_admin_history(
        self, interaction: discord.Interaction, channel: Any
    ) -> None:
        """
        Kompletter Chatverlauf eines Kanals als PDF — **ausschließlich** per DM
        an den Bot-Owner (Application-Owner). Niemals an einen Server oder den
        Server-Owner; ist der Bot-Owner nicht ermittelbar, wird NICHT versendet.

        Wird aus AdminHistoryModal.on_submit aufgerufen — die Interaktion wurde
        bereits mit einer sofortigen Nachricht (Callback Typ 4) beantwortet.
        Jeder Ausgang (erfolgreich, DM fehlgeschlagen, Fehler) wird dem Owner
        SICHTBAR gemeldet — es gibt keinen stillen Pfad mehr, der nur Discords
        generischen „Etwas ist schief gelaufen“-Dialog hinterlassen könnte.
        """
        guild = channel.guild
        ch_name = getattr(channel, "name", "?")

        # Fortschritts-Nachricht: die Typ-4-Antwort des Modals (original) —
        # Fallback: Followup (falls der Aufrufer statt Typ 4 deferred hat).
        progress_msg: Any = None
        try:
            progress_msg = await interaction.original_response()
        except Exception:  # noqa: BLE001 — Aufruf-Shape/HTTP-Fehler sind egal
            progress_msg = None
        if progress_msg is None:
            try:
                progress_msg = await interaction.followup.send(
                    f"💬 **Chatverlauf von #{ch_name}**\n{_progress_bar(0)}\n"
                    "Chatverlauf wird geladen …",
                    wait=True,
                )
            except Exception:  # noqa: BLE001 — ohne Fortschritt läuft der Bau weiter
                progress_msg = None
        if progress_msg is not None:
            log.info("Chatverlauf-Abruf #%s: Fortschritts-Nachricht aktiv.", ch_name)

        async def _final(content: str) -> None:
            """Meldet das Ergebnis sichtbar — nie raisend, nie still."""
            if progress_msg is not None:
                try:
                    await progress_msg.edit(content=content)
                    return
                except Exception:  # noqa: BLE001 — z. B. Token nach 15 min abgelaufen
                    log.warning("Ergebnis-Edit der Chatverlauf-Nachricht fehlgeschlagen — Fallback auf Followup.")
            try:
                await interaction.followup.send(content)
            except Exception:  # noqa: BLE001 — dann ist der Token endgültig tot
                log.warning("Chatverlauf-Ergebnis konnte nicht angezeigt werden: %s", content[:80])

        async def _progress(percent: int, text: str) -> None:
            if progress_msg is None:
                return
            try:
                await asyncio.wait_for(
                    progress_msg.edit(
                        content=(
                            f"💬 **Chatverlauf von #{ch_name}**\n"
                            f"{_progress_bar(percent)} {percent} %\n{text}"
                        ),
                    ),
                    timeout=10,
                )
            except Exception:  # noqa: BLE001 — Fortschritt darf niemals blockieren
                pass  # z. B. Interaktions-Token nach 15 min abgelaufen — DM geht trotzdem raus

        try:
            try:
                app_info = await self.application_info()
                recipient = app_info.owner if app_info is not None else None
            except discord.HTTPException as exc:
                log.warning("Application-Owner nicht ermittelbar (%s) — kein Chatverlauf-Versand.", exc)
                recipient = None
            if recipient is None:
                await _final(
                    "❌ Der Bot-Owner konnte nicht ermittelt werden — der Chatverlauf "
                    "wird **nicht** versendet (niemals an Server oder Server-Owner)."
                )
                return

            # RAM-Guard: Vor jedem Teil-PDF den eigenen RAM prüfen — bei
            # kritischer Schwelle abbrechen (mit Aufräumen), BEVOR der
            # Container in den OOM-Kill läuft.
            async def _ram_guard() -> None:
                mb = current_rss_mb()
                if mb >= RAM_CRITICAL_MB:
                    after = relieve_ram()
                    if after >= RAM_CRITICAL_MB:
                        raise MemoryError(
                            f"RAM kritisch ({after:.0f} MB, Limit ~512 MB) — "
                            "Aufbau abgebrochen, um einen OOM-Neustart zu verhindern. "
                            "Bitte erneut versuchen; bleibt es kritisch, den Kanal "
                            "in kleineren Zeiträumen abrufen."
                        )

            # Alte Chatverlauf-DMs bleiben bewusst BESTEHEN (Wunsch: nichts
            # wird mehr gelöscht) — jede Anforderung hängt neue PDFs an.

            sent_ids: List[int] = []

            async def _send_part(pdf_bytes: bytes, idx: int, total: int) -> None:
                """
                Sendet einen fertigen Teil SOFORT (RAM-Batching: „fertige
                Sachen senden, Daten löschen, Platz für neue“). Bei
                DM-Fehlern wird abgebrochen — die restlichen Teile werden
                nicht mehr gebaut, der Fehler wird sichtbar gemeldet.
                """
                if total != 1:
                    von = f" von {total}" if total > 1 else ""
                    fname = (
                        f"chatverlauf-{guild.id}-{channel.id}-teil-{idx}"
                        + (f"-von-{total}" if total > 1 else "") + ".pdf"
                    )
                    content = (
                        f"💬 **Chatverlauf Teil {idx}{von}** von #{ch_name} in "
                        f"**{guild.name}** (Server-ID `{guild.id}`, Kanal-ID `{channel.id}`)\n\n"
                        "Große Verläufe werden auf mehrere Teil-PDFs verteilt, damit "
                        "die Erstellung zuverlässig bleibt. Alle Teile zusammen sind "
                        "der **komplette** Verlauf (Apple-Emojis, Bildanhänge, "
                        "Komponenten, Embeds, Mention-Namen).\n\n"
                        "_Vertraulich — nur für dich (Bot-Owner) bestimmt. Der Server "
                        "selbst hat davon nichts bemerkt._"
                    )
                else:
                    fname = f"chatverlauf-{guild.id}-{channel.id}.pdf"
                    content = (
                        f"💬 **Chatverlauf von #{ch_name}** in **{guild.name}** "
                        f"(Server-ID `{guild.id}`, Kanal-ID `{channel.id}`)\n\n"
                        "Dieses PDF enthält den **kompletten** Chatverlauf des Kanals "
                        "(inkl. Apple-Emojis, Bildanhängen, Komponenten, Embeds und "
                        "Mention-Namen) und wurde über das Adminpanel angefordert.\n\n"
                        "_Vertraulich — nur für dich (Bot-Owner) bestimmt. Der Server "
                        "selbst hat davon nichts bemerkt._"
                    )
                file = discord.File(io.BytesIO(pdf_bytes), filename=fname)
                try:
                    sent = await recipient.send(content=content, file=file)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Chatverlauf-Teil %d/%d konnte nicht per DM an %s gesendet werden: %s",
                                idx, total, recipient.display_name, exc)
                    raise RuntimeError(
                        f"Teil {idx}/{total} konnte nicht per DM an {recipient.display_name} "
                        "zugestellt werden (DMs vom Bot deaktiviert oder kein gemeinsamer "
                        "Server) — die restlichen Teile wurden nicht erstellt."
                    ) from exc
                sent_ids.append(sent.id)

            http_session = getattr(self.state, "http_session", None)
            if http_session is None:
                http_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60, connect=15),
                )
                own_session = True
            else:
                own_session = False
            try:
                meta = await build_channel_history_pdf(
                    channel, http_session, progress=_progress, max_messages=None,
                    send_part=_send_part, ram_guard=_ram_guard,
                )
            finally:
                if own_session:
                    await http_session.close()

            state = self._load_doc_delivery()
            state["last_history_delivery"] = {
                "recipient_id": recipient.id,
                "message_id": sent_ids[-1] if sent_ids else None,
                "parts": len(sent_ids),
                "guild_id": guild.id,
                "channel_id": channel.id,
                "channel_name": ch_name,
                "sent_at": now_utc().isoformat(),
            }
            self._save_doc_delivery(state)

            parts = int((meta or {}).get("parts", len(sent_ids) or 1))
            msg_count = int((meta or {}).get("messages", 0))
            span_text = str((meta or {}).get("span") or "—")
            size_kb = sum((meta or {}).get("bytes") or []) // 1024
            await _final(
                f"✅ Chatverlauf von **#{ch_name}** erstellt — **{parts} "
                f"Teil-PDF{'s' if parts != 1 else ''}** ({size_kb} KB, {msg_count} Nachrichten, "
                f"Zeitraum {span_text}) per DM an **{recipient.display_name}** "
                "(Bot-Owner) gesendet."
            )
        except Exception as exc:  # noqa: BLE001 — jede Fehlermeldung wird dem Admin angezeigt
            log.exception("Fehler beim Erstellen des Chatverlaufs für '%s'", ch_name)
            await _final(f"❌ Fehler beim Erstellen des Chatverlaufs: {exc}")

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
            description="🗄️ Server-Datenbank (aktuell nicht verfügbar)",
        )
        @app_commands.guild_only()
        async def manage_database(interaction: discord.Interaction) -> None:
            """Zeigt den Hinweis, dass die Server-Datenbank aktuell nicht verfügbar ist."""
            gate = await self._guard(interaction)
            if gate is None:
                return

            try:
                await interaction.response.send_message(
                    view=manage_database_unavailable_view(), ephemeral=True,
                )
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
        elif custom_id.startswith(CID_ADMIN_PREFIX):
            await self._handle_admin_button(interaction, custom_id)

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
