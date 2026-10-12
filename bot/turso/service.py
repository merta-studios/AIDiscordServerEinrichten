"""
TursoService: Zentraler Service für Master-DB, Guild-DBs und Systems-Engine.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from ..util import ApiError, iso, now_utc
from .client import TursoClient, TursoError
from .engine import TursoSystemsEngine
from .guild_db import GuildDatabaseManager
from .master import MasterTursoStore

log = logging.getLogger("relay.turso.service")

__all__ = ("TursoService",)


class TursoService:
    """Zentrale Schnittstelle für die Bridge und den Discord-Bot."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.master = MasterTursoStore(config)
        self.guild_manager = GuildDatabaseManager(self.master)
        self.engine = TursoSystemsEngine(self.guild_manager)

    async def init(self) -> None:
        await self.master.init()

    async def is_guild_db_configured(self, guild_id: int) -> bool:
        record = await self.master.get_guild_db(guild_id)
        return record is not None and bool(record.database_url)

    async def connect_guild_database(
        self,
        guild_id: int,
        guild_name: str,
        database_url: str,
        auth_token: str,
        configured_by: str = "",
    ) -> Tuple[bool, str]:
        """Testet die Verbindung, initialisiert das Schema und verknüpft die Datenbank."""
        test_client = TursoClient(database_url.strip(), auth_token.strip())
        try:
            # 1. Verbindungstest
            res = await test_client.execute("SELECT 1 as test_connection;")
            if not res.rows or res.rows[0].get("test_connection") != 1:
                return False, "Verbindungstest fehlgeschlagen: Unerwartete Antwort von Turso."

            # 2. Schema initialisieren
            await self.guild_manager.ensure_schema(test_client, guild_id=guild_id)

            # 3. In Master-DB speichern
            await self.master.set_guild_db(
                guild_id=guild_id,
                guild_name=guild_name,
                database_url=database_url.strip(),
                auth_token=auth_token.strip(),
                configured_by=configured_by,
            )

            # 4. Engine-Cache invalidieren
            self.engine.invalidate(guild_id)
            return True, "Erfolgreich mit Turso-Datenbank verbunden und Schema initialisiert!"
        except Exception as exc:
            log.warning("Fehler beim Verbinden der Turso-DB für %s: %s", guild_id, exc)
            return False, f"Fehler bei der Verbindung: {exc}"
        finally:
            await test_client.close()

    async def disconnect_guild_database(self, guild_id: int) -> bool:
        success = await self.master.delete_guild_db(guild_id)
        self.engine.invalidate(guild_id)
        return success

    async def get_guild_status(self, guild_id: int) -> Dict[str, Any]:
        """Liefert den kompletten Status: Verbindung, Speicher, Tabellen, Systeme."""
        record = await self.master.get_guild_db(guild_id)
        if record is None:
            return {
                "configured": False,
                "message": "Keine Datenbank für diesen Server eingerichtet.",
                "setup_command": "/manage_database",
                "hint": "Führe /manage_database auf Discord aus, um kostenlos eine Turso-Datenbank zu verknüpfen.",
            }

        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            return {
                "configured": False,
                "message": "Datenbank-Konfiguration konnte nicht geladen werden.",
                "setup_command": "/manage_database",
            }

        await self.guild_manager.ensure_schema(client, guild_id=guild_id)
        storage_stats = await client.get_storage_stats()
        systems = await self.list_systems(guild_id)

        # URL maskieren für Anzeige
        raw_url = record.database_url
        masked_url = raw_url
        if "@" in raw_url:
            masked_url = raw_url.split("@")[-1]

        return {
            "configured": True,
            "database_url": masked_url,
            "configured_by": record.configured_by,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "storage": storage_stats,
            "systems_count": len(systems),
            "systems": systems,
            "cache_stats": {
                "master_hits": self.master.cache_hits,
                "engine_hits": self.engine.engine_cache_hits,
                "total_cache_hits": self.master.cache_hits + self.engine.engine_cache_hits,
            },
        }

    # ── Systeme & Trigger verwalten ──────────────────────────────────────────

    async def list_systems(self, guild_id: int) -> List[Dict[str, Any]]:
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            return []

        await self.guild_manager.ensure_schema(client, guild_id=guild_id)
        sql = "SELECT id, name, type, description, enabled, config, created_at, updated_at FROM systems ORDER BY created_at DESC;"
        res = await client.execute(sql)

        # Triggers nach system_id gruppieren
        trig_sql = "SELECT id, system_id, event_type, filter_json, action_json, enabled, created_at FROM triggers ORDER BY created_at ASC;"
        trig_res = await client.execute(trig_sql)
        triggers_by_sys: Dict[str, List[Dict[str, Any]]] = {}

        for tr in trig_res.rows:
            sys_id = tr["system_id"]
            if sys_id not in triggers_by_sys:
                triggers_by_sys[sys_id] = []
            f_json = {}
            a_json = {}
            try:
                if tr.get("filter_json"):
                    f_json = json.loads(tr["filter_json"]) if isinstance(tr["filter_json"], str) else tr["filter_json"]
            except Exception:
                pass
            try:
                if tr.get("action_json"):
                    a_json = json.loads(tr["action_json"]) if isinstance(tr["action_json"], str) else tr["action_json"]
            except Exception:
                pass

            triggers_by_sys[sys_id].append({
                "id": tr["id"],
                "event_type": tr["event_type"],
                "filter": f_json,
                "action": a_json,
                "enabled": bool(tr.get("enabled", 1)),
                "created_at": tr.get("created_at"),
            })

        systems: List[Dict[str, Any]] = []
        for r in res.rows:
            sys_id = r["id"]
            cfg_data = {}
            try:
                if r.get("config"):
                    cfg_data = json.loads(r["config"]) if isinstance(r["config"], str) else r["config"]
            except Exception:
                pass

            systems.append({
                "id": sys_id,
                "name": r["name"],
                "type": r["type"],
                "description": r.get("description") or "",
                "enabled": bool(r.get("enabled", 1)),
                "config": cfg_data,
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "triggers": triggers_by_sys.get(sys_id, []),
            })
        return systems

    async def get_system(self, guild_id: int, system_id: str) -> Optional[Dict[str, Any]]:
        systems = await self.list_systems(guild_id)
        for s in systems:
            if s["id"] == system_id:
                return s
        return None

    async def save_system(self, guild_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Erstellt oder aktualisiert ein geordnetes System inklusive Triggern."""
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            raise ApiError(
                "Keine Turso-Datenbank für diesen Server eingerichtet.",
                code="DATABASE_REQUIRED",
                status=412,
                hint="STOPPE HIER deine Antwort und fordere den Nutzer auf, "
                     "den Slash-Command /manage_database auf Discord auszuführen.",
            )

        await self.guild_manager.ensure_schema(client, guild_id=guild_id)
        now_str = iso(now_utc())

        sys_id = str(data.get("id") or f"sys_{uuid.uuid4().hex[:10]}")
        name = str(data.get("name") or "Unbenanntes System")
        sys_type = str(data.get("type") or "custom")
        desc = str(data.get("description") or "")
        enabled = 1 if data.get("enabled", True) else 0
        cfg_str = json.dumps(data.get("config") or {})

        existing = await self.get_system(guild_id, sys_id)
        created_str = existing["created_at"] if existing else now_str

        # 1. System speichern
        sys_sql = """
        INSERT INTO systems (id, name, type, description, enabled, config, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            description = excluded.description,
            enabled = excluded.enabled,
            config = excluded.config,
            updated_at = excluded.updated_at;
        """
        await client.execute(sys_sql, [sys_id, name, sys_type, desc, enabled, cfg_str, created_str, now_str])

        # 2. Trigger aktualisieren (falls im Request mitgegeben)
        triggers_input = data.get("triggers")
        if triggers_input is not None and isinstance(triggers_input, list):
            # Alte Trigger für dieses System löschen
            await client.execute("DELETE FROM triggers WHERE system_id = ?;", [sys_id])

            for trig in triggers_input:
                trig_id = str(trig.get("id") or f"trig_{uuid.uuid4().hex[:8]}")
                event_type = str(trig.get("event") or trig.get("event_type") or "message_create")
                filter_str = json.dumps(trig.get("filter") or {})
                action_str = json.dumps(trig.get("action") or {})
                trig_enabled = 1 if trig.get("enabled", True) else 0

                ins_trig_sql = """
                INSERT INTO triggers (id, system_id, event_type, filter_json, action_json, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """
                await client.execute(ins_trig_sql, [
                    trig_id, sys_id, event_type, filter_str, action_str, trig_enabled, now_str
                ])

        # Engine-Cache invalidieren
        self.engine.invalidate(guild_id)
        saved = await self.get_system(guild_id, sys_id)
        return saved or {}

    async def delete_system(self, guild_id: int, system_id: str) -> bool:
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            return False

        await client.execute("DELETE FROM triggers WHERE system_id = ?;", [system_id])
        res = await client.execute("DELETE FROM systems WHERE id = ?;", [system_id])
        self.engine.invalidate(guild_id)
        return res.affected_rows > 0

    # ── Direkte SQL-Abfragen & Variablen ─────────────────────────────────────

    async def execute_query(self, guild_id: int, sql: str, args: Optional[List[Any]] = None) -> Dict[str, Any]:
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            raise ApiError(
                "Keine Turso-Datenbank für diesen Server eingerichtet.",
                code="DATABASE_REQUIRED",
                status=412,
                hint="STOPPE HIER deine Antwort und fordere den Nutzer auf, "
                     "den Slash-Command /manage_database auf Discord auszuführen.",
            )

        res = await client.execute(sql, args or [])
        return res.as_dict()

    async def get_variables(self, guild_id: int) -> List[Dict[str, Any]]:
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            return []
        await self.guild_manager.ensure_schema(client, guild_id=guild_id)
        res = await client.execute("SELECT key, value, type, updated_at FROM key_value_store ORDER BY key;")
        return res.rows

    async def set_variable(self, guild_id: int, key: str, value: Any, var_type: str = "string") -> Dict[str, Any]:
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            raise ApiError(
                "Keine Turso-Datenbank für diesen Server eingerichtet.",
                code="DATABASE_REQUIRED",
                status=412,
            )
        await self.guild_manager.ensure_schema(client, guild_id=guild_id)
        now_str = iso(now_utc())
        val_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        sql = """
        INSERT INTO key_value_store (key, value, type, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            type = excluded.type,
            updated_at = excluded.updated_at;
        """
        await client.execute(sql, [key, val_str, var_type, now_str])
        return {"key": key, "value": value, "type": var_type, "updated_at": now_str}

    async def delete_variable(self, guild_id: int, key: str) -> bool:
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            return False
        res = await client.execute("DELETE FROM key_value_store WHERE key = ?;", [key])
        return res.affected_rows > 0
