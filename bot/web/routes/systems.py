"""
Systeme & Turso-Datenbank-Endpunkte.

Erlaubt Arena AI:
- Status & Speicherauslastung der Turso-Datenbank abzufragen
- Geordnete Systeme anzulegen, zu listen und zu löschen (Self Roles, Welcome, Auto-Responder, Timer etc.)
- Trigger für Events (Reaktionen, Nachrichten, Member Join, 5 Uhr Cron) zu definieren
- Eigene Tabellen & SQL-Queries auszuführen
- Variablen & Listen zu verwalten
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...util import ApiError, parse_str
from ..context import Ctx
from ..registry import route


def _require_turso(ctx: Ctx) -> Any:
    turso = ctx.turso
    if turso is None:
        raise ApiError.unavailable("Turso-Service ist noch nicht bereit.", code="TURSO_UNAVAILABLE")
    return turso


async def _assert_database_configured(ctx: Ctx) -> Any:
    turso = _require_turso(ctx)
    configured = await turso.is_guild_db_configured(ctx.guild.id)
    if not configured:
        raise ApiError(
            "Für diesen Server ist noch keine Turso-Datenbank eingerichtet.",
            code="DATABASE_REQUIRED",
            status=412,
            hint="STOPPE HIER sofort deine Antwort und sage dem Nutzer: "
                 "'Für diese Funktion (z.B. Self Roles, Willkommens-System, Timer) wird eine "
                 "Turso-Datenbank benötigt. Die Einrichtung ist 100% kostenlos! Bitte führe auf "
                 "dem Server den Discord-Slash-Command /manage_database aus. Sobald die Datenbank "
                 "verbunden ist, erstelle ich das System sofort für dich.'",
        )
    return turso


@route(
    "GET", "/api/v1/database/status", scope="read", tags=("database", "systems"),
    summary="Status und Speicherauslastung der Server-Datenbank",
    description="Prüft, ob für diesen Server eine Turso-Datenbank verknüpft ist. "
                "Liefert bei vorhandener Datenbank die Speicherauslastung, freie Bytes, "
                "Tabellenanzahl und eine Übersicht aller Systeme.",
)
async def get_database_status(ctx: Ctx) -> Dict[str, Any]:
    turso = _require_turso(ctx)
    status = await turso.get_guild_status(ctx.guild.id)
    return status


@route(
    "GET", "/api/v1/systems", scope="read", tags=("systems",),
    summary="Alle aktiven Systeme des Servers abrufen",
    description="Listet alle geordneten Systeme (z.B. Self Roles, Welcome, Auto-Responder) "
                "inklusive ihrer konfigurierten Trigger.",
)
async def list_systems(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    systems = await turso.list_systems(ctx.guild.id)
    return {
        "count": len(systems),
        "systems": systems,
    }


@route(
    "POST", "/api/v1/systems", scope="manage", tags=("systems",),
    summary="Neues System oder Trigger anlegen / aktualisieren",
    body={
        "id": "str (optional, z.B. 'self_roles_colors') — wird generiert falls leer",
        "name": "str (Pflicht, z.B. 'Farbauswahl Self Roles')",
        "type": "'self_roles' | 'welcome' | 'auto_responder' | 'scheduled' | 'custom_command' | 'custom'",
        "description": "str (optional)",
        "enabled": "bool (Standard: true)",
        "config": "object (optional, Konfiguration des Systems)",
        "triggers": "[{event, filter, action}] — Event-Trigger für dieses System",
    },
    examples=[
        {
            "body": {
                "id": "self_roles_gaming",
                "name": "Gaming Rollen",
                "type": "self_roles",
                "description": "Gibt Mitgliedern bei Reaktion auf die Nachricht die passende Rolle",
                "triggers": [
                    {
                        "event": "reaction_add",
                        "filter": {"message_id": "1234567890", "emoji": "🎮"},
                        "action": {"type": "add_role", "role_id": "9876543210"},
                    },
                    {
                        "event": "reaction_remove",
                        "filter": {"message_id": "1234567890", "emoji": "🎮"},
                        "action": {"type": "remove_role", "role_id": "9876543210"},
                    },
                ],
            }
        },
        {
            "body": {
                "id": "welcome_system",
                "name": "Willkommens-System",
                "type": "welcome",
                "triggers": [
                    {
                        "event": "member_join",
                        "action": {
                            "type": "send_message",
                            "channel_id": "1234567890",
                            "content": "Herzlich Willkommen auf {server}, {user_mention}! 👋 Wir sind jetzt {member_count} Mitglieder.",
                            "webhook": {"name": "🎉 Willkommen"},
                        },
                    }
                ],
            }
        },
        {
            "body": {
                "id": "morning_post_5am",
                "name": "5 Uhr Morgengruß",
                "type": "scheduled",
                "triggers": [
                    {
                        "event": "cron",
                        "filter": {"time": "05:00"},
                        "action": {
                            "type": "send_message",
                            "channel_id": "1234567890",
                            "content": "Guten Morgen {server}! Es ist 5:00 Uhr — der neue Tag beginnt. ☕",
                        },
                    }
                ],
            }
        },
    ],
    description="Erstellt ein vollständiges, funktionierendes Server-System in der Turso-Datenbank. "
                "Die Bridge überwacht Discord-Events und führt die definierten Aktionen automatisch aus.",
)
async def create_or_update_system(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    data = await ctx.body()

    name = parse_str(data.get("name"), field="name", min_length=1, max_length=100, allow_empty=False)
    if not name:
        raise ApiError.bad_request("'name' fehlt oder ist leer.")

    sys_type = parse_str(data.get("type"), field="type", min_length=1, max_length=50, allow_empty=False) or "custom"
    saved = await turso.save_system(ctx.guild.id, data)
    return {"created": True, "system": saved}


@route(
    "GET", "/api/v1/systems/{system_id}", scope="read", tags=("systems",),
    summary="Einzelnes System abrufen",
)
async def get_system_detail(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    system_id = ctx.path("system_id")
    system = await turso.get_system(ctx.guild.id, system_id)
    if system is None:
        raise ApiError.not_found(f"System '{system_id}' nicht gefunden.", code="SYSTEM_NOT_FOUND")
    return system


@route(
    "PATCH", "/api/v1/systems/{system_id}", scope="manage", tags=("systems",),
    summary="System aktualisieren oder de-/aktivieren",
    body={
        "name": "str",
        "enabled": "bool",
        "description": "str",
        "config": "object",
        "triggers": "[{event, filter, action}]",
    },
)
async def patch_system(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    system_id = ctx.path("system_id")
    existing = await turso.get_system(ctx.guild.id, system_id)
    if existing is None:
        raise ApiError.not_found(f"System '{system_id}' nicht gefunden.", code="SYSTEM_NOT_FOUND")

    data = await ctx.body()
    merged = dict(existing)
    merged.update(data)
    merged["id"] = system_id

    saved = await turso.save_system(ctx.guild.id, merged)
    return {"updated": True, "system": saved}


@route(
    "DELETE", "/api/v1/systems/{system_id}", scope="manage", tags=("systems",),
    summary="System und zugehörige Trigger löschen",
)
async def delete_system(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    system_id = ctx.path("system_id")
    deleted = await turso.delete_system(ctx.guild.id, system_id)
    if not deleted:
        raise ApiError.not_found(f"System '{system_id}' nicht gefunden.", code="SYSTEM_NOT_FOUND")
    return {"deleted": True, "id": system_id}


# ── Direkte SQL-Ausführung & Key-Value-Store ─────────────────────────────────

@route(
    "POST", "/api/v1/database/query", scope="manage", tags=("database",),
    summary="SQL-Query direkt auf der Server-Datenbank ausführen",
    body={
        "sql": "str (Pflicht, z.B. 'CREATE TABLE ...' oder 'SELECT ...')",
        "args": "array (optional, Parameter für ?)",
    },
    description="Erlaubt Arena AI, eigene Tabellen anzulegen, Daten einzufügen oder "
                "komplexe Abfragen direkt auf der Turso-Datenbank des Servers auszuführen.",
)
async def execute_database_query(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    data = await ctx.body()
    sql = parse_str(data.get("sql"), field="sql", min_length=1, allow_empty=False)
    if not sql:
        raise ApiError.bad_request("'sql' fehlt.")
    args = data.get("args") or []
    if not isinstance(args, list):
        raise ApiError.bad_request("'args' muss eine Liste sein.")

    res = await turso.execute_query(ctx.guild.id, sql, args)
    return res


@route(
    "GET", "/api/v1/database/variables", scope="read", tags=("database",),
    summary="Alle Variablen des Servers abrufen",
)
async def get_variables(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    vars_list = await turso.get_variables(ctx.guild.id)
    return {"count": len(vars_list), "variables": vars_list}


@route(
    "POST", "/api/v1/database/variables", scope="write", tags=("database",),
    summary="Variable oder Zähler setzen",
    body={
        "key": "str (Pflicht)",
        "value": "beliebiger Wert (String, Zahl, JSON)",
        "type": "'string' | 'number' | 'json' | 'list'",
    },
)
async def set_variable(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    data = await ctx.body()
    key = parse_str(data.get("key"), field="key", min_length=1, allow_empty=False)
    if not key:
        raise ApiError.bad_request("'key' fehlt.")
    val = data.get("value")
    var_type = parse_str(data.get("type"), field="type") or "string"
    saved = await turso.set_variable(ctx.guild.id, key, val, var_type)
    return {"saved": True, "variable": saved}


@route(
    "DELETE", "/api/v1/database/variables/{key}", scope="manage", tags=("database",),
    summary="Variable löschen",
)
async def delete_variable(ctx: Ctx) -> Dict[str, Any]:
    turso = await _assert_database_configured(ctx)
    key = ctx.path("key")
    deleted = await turso.delete_variable(ctx.guild.id, key)
    return {"deleted": deleted, "key": key}
