"""
Verwaltung der Haupt-Turso-Datenbank (Master-DB).

Die Haupt-Turso-Datenbank speichert für jeden Discord-Server (Guild)
die Zugangsdaten (Database-URL + Auth-Token) zu dessen eigener Turso-Datenbank.

Caching:
Um die Rate-Limits der Haupt-Datenbank zu schonen, werden gelesene Einträge
im Speicher (In-Memory) gehalten und bei Änderungen invalidiert.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import Config
from ..util import iso, now_utc
from .client import TursoClient, TursoError

log = logging.getLogger("relay.turso.master")

__all__ = ("MasterTursoStore", "GuildDbRecord", "legacy_configured_user_id")

_LEGACY_USER_ID_RE = re.compile(r"\((\d{5,25})\)\s*$")


def legacy_configured_user_id(configured_by: str) -> str:
    """
    Zieht aus einem Legacy-``configured_by``-String (``"Name (123456789)"``)
    die Nutzer-ID heraus — leer, wenn kein Muster passt.

    Alte Einträge (vor dem Mehr-Server-Update) speicherten nur Name + ID im
    Freitext; die Wiederverwenden-Suche soll auch für diese Bestände greifen,
    ohne dass der Admin die Zugangsdaten neu eintippen muss.
    """
    match = _LEGACY_USER_ID_RE.search(configured_by or "")
    return match.group(1) if match else ""


@dataclass(slots=True)
class GuildDbRecord:
    guild_id: int
    guild_name: str
    database_url: str
    auth_token: str
    configured_by: str
    configured_by_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self, *, include_token: bool = False) -> Dict[str, Any]:
        data = {
            "guild_id": str(self.guild_id),
            "guild_name": self.guild_name,
            "database_url": self.database_url,
            "configured_by": self.configured_by,
            "configured_by_id": self.configured_by_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_token:
            data["auth_token"] = self.auth_token
        return data

    @property
    def owner_user_id(self) -> str:
        """Der Admin, der die Datenbank eingerichtet hat (Legacy-fähig)."""
        return self.configured_by_id or legacy_configured_user_id(self.configured_by)


class MasterTursoStore:
    """Verwaltet den Zugriff auf die Master-Datenbank mit In-Memory-Caching."""

    CACHE_TTL_SECONDS = 600.0  # 10 Minuten Cache

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Optional[TursoClient] = None
        self._cache: Dict[int, Optional[GuildDbRecord]] = {}
        self._cache_timestamps: Dict[int, float] = {}
        self._initialized = False
        self.cache_hits: int = 0
        self.cache_misses: int = 0

    def _get_client(self) -> TursoClient:
        if self._client is not None:
            return self._client

        url = self.config.turso_database_url
        token = self.config.turso_auth_token

        if not url:
            # Fallback: Lokale SQLite-Datenbank in data_dir
            db_dir = self.config.data_dir
            os.makedirs(db_dir, exist_ok=True)
            db_path = os.path.join(db_dir, "master_turso.db")
            url = f"sqlite:///{db_path}"
            token = ""
            log.info("TURSO_DATABASE_URL nicht gesetzt — nutze lokales Fallback unter %s", db_path)

        self._client = TursoClient(url, token)
        return self._client

    async def init(self) -> None:
        """Initialisiert die Tabelle in der Master-Datenbank."""
        if self._initialized:
            return

        client = self._get_client()
        sql = """
        CREATE TABLE IF NOT EXISTS server_databases (
            guild_id TEXT PRIMARY KEY,
            guild_name TEXT,
            database_url TEXT NOT NULL,
            auth_token TEXT NOT NULL,
            configured_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        try:
            await client.execute(sql)
            # Migration für Bestände vor dem Mehr-Server-Update: die Spalte
            # ``configured_by_id`` merkt sich, WELCHER Admin die Zugangsdaten
            # eingegeben hat (exakte ID statt Freitext). Existiert die Spalte
            # bereits, schlägt das ALTER fehl — das ist der Normalfall und wird
            # still ignoriert.
            try:
                await client.execute("ALTER TABLE server_databases ADD COLUMN configured_by_id TEXT;")
            except Exception:  # noqa: BLE001 — "duplicate column" ist der Erwartungsfall
                pass
            self._initialized = True
            log.info("Master-Datenbank initialisiert (%s)", "Turso Cloud" if not client.is_sqlite else "SQLite Fallback")
        except Exception as exc:
            log.error("Fehler beim Initialisieren der Master-Datenbank: %s", exc)

    async def get_guild_db(self, guild_id: int) -> Optional[GuildDbRecord]:
        """Holt die Datenbank-Konfiguration für einen Server (bevorzugt aus Cache)."""
        now = time.monotonic()
        if guild_id in self._cache:
            ts = self._cache_timestamps.get(guild_id, 0.0)
            if now - ts < self.CACHE_TTL_SECONDS:
                self.cache_hits += 1
                return self._cache[guild_id]

        self.cache_misses += 1
        await self.init()
        client = self._get_client()
        sql = ("SELECT guild_id, guild_name, database_url, auth_token, configured_by, "
               "configured_by_id, created_at, updated_at FROM server_databases WHERE guild_id = ? LIMIT 1;")

        try:
            res = await client.execute(sql, [str(guild_id)])
            if not res.rows:
                self._cache[guild_id] = None
                self._cache_timestamps[guild_id] = now
                return None

            r = res.rows[0]
            record = GuildDbRecord(
                guild_id=int(r["guild_id"]),
                guild_name=r.get("guild_name") or "",
                database_url=r.get("database_url") or "",
                auth_token=r.get("auth_token") or "",
                configured_by=r.get("configured_by") or "",
                configured_by_id=r.get("configured_by_id") or "",
                created_at=r.get("created_at") or "",
                updated_at=r.get("updated_at") or "",
            )
            self._cache[guild_id] = record
            self._cache_timestamps[guild_id] = now
            return record
        except Exception as exc:
            log.error("Fehler beim Abrufen der Guild-Datenbank für %s: %s", guild_id, exc)
            return None

    async def set_guild_db(
        self,
        guild_id: int,
        guild_name: str,
        database_url: str,
        auth_token: str,
        configured_by: str = "",
        configured_by_id: str = "",
    ) -> GuildDbRecord:
        """Speichert oder aktualisiert die Datenbank-Konfiguration eines Servers."""
        await self.init()
        client = self._get_client()
        now_str = iso(now_utc())

        # Prüfen ob vorhanden
        existing = await self.get_guild_db(guild_id)
        created_str = existing.created_at if existing else now_str
        # Ohne explizite Admin-ID den bisherigen Wert behalten (Legacy-Einträge
        # behalten so ihre über den Freitext ermittelbare ID).
        if not configured_by_id and existing is not None:
            configured_by_id = existing.configured_by_id

        sql = """
        INSERT INTO server_databases (guild_id, guild_name, database_url, auth_token, configured_by, configured_by_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            guild_name = excluded.guild_name,
            database_url = excluded.database_url,
            auth_token = excluded.auth_token,
            configured_by = excluded.configured_by,
            configured_by_id = excluded.configured_by_id,
            updated_at = excluded.updated_at;
        """
        await client.execute(sql, [
            str(guild_id),
            guild_name,
            database_url.strip(),
            auth_token.strip(),
            configured_by,
            configured_by_id,
            created_str,
            now_str,
        ])

        record = GuildDbRecord(
            guild_id=guild_id,
            guild_name=guild_name,
            database_url=database_url.strip(),
            auth_token=auth_token.strip(),
            configured_by=configured_by,
            configured_by_id=configured_by_id,
            created_at=created_str,
            updated_at=now_str,
        )
        self._cache[guild_id] = record
        self._cache_timestamps[guild_id] = time.monotonic()
        log.info("Turso-DB-Zugriff für Guild %s (%s) gespeichert.", guild_id, guild_name)
        return record

    async def delete_guild_db(self, guild_id: int) -> bool:
        """Löscht die Datenbank-Verknüpfung für einen Server."""
        await self.init()
        client = self._get_client()
        sql = "DELETE FROM server_databases WHERE guild_id = ?;"
        try:
            res = await client.execute(sql, [str(guild_id)])
            self._cache.pop(guild_id, None)
            self._cache_timestamps.pop(guild_id, None)
            return res.affected_rows > 0
        except Exception as exc:
            log.error("Fehler beim Löschen der Guild-DB für %s: %s", guild_id, exc)
            return False

    async def list_all(self) -> List[GuildDbRecord]:
        """Listet alle registrierten Server-Datenbanken."""
        await self.init()
        client = self._get_client()
        sql = ("SELECT guild_id, guild_name, database_url, auth_token, configured_by, "
               "configured_by_id, created_at, updated_at FROM server_databases ORDER BY guild_name;")
        try:
            res = await client.execute(sql)
            output: List[GuildDbRecord] = []
            for r in res.rows:
                output.append(GuildDbRecord(
                    guild_id=int(r["guild_id"]),
                    guild_name=r.get("guild_name") or "",
                    database_url=r.get("database_url") or "",
                    auth_token=r.get("auth_token") or "",
                    configured_by=r.get("configured_by") or "",
                    configured_by_id=r.get("configured_by_id") or "",
                    created_at=r.get("created_at") or "",
                    updated_at=r.get("updated_at") or "",
                ))
            return output
        except Exception as exc:
            log.error("Fehler beim Listen aller Guild-DBs: %s", exc)
            return []

    async def list_by_user(self, user_id: str) -> List[GuildDbRecord]:
        """
        Alle Datenbanken, die ein bestimmter Admin eingerichtet hat.

        Matchkriterium ist die exakte ``configured_by_id``; Legacy-Einträge
        ohne diese Spalte werden über die im Freitext gespeicherte ID
        (``"Name (123456789)"``) nachgewiesen — so funktioniert die
        Wiederverwenden-Suche auch für Bestände vor dem Update.
        """
        records = await self.list_all()
        uid = str(user_id)
        if not uid:
            return []
        return [r for r in records if r.owner_user_id == uid]

    async def list_guilds_by_database(self, database_url: str, exclude_guild_id: Optional[int] = None) -> List[GuildDbRecord]:
        """
        Alle Server, die dieselbe physische Datenbank nutzen (geteilte DB).

        Wichtig fürs Bereinigen beim Server-Verlassen: Nur wenn **kein**
        anderer Server mehr auf dieselbe URL zeigt, dürfen die Tabellen der
        Datenbank fallen.
        """
        url = (database_url or "").strip()
        if not url:
            return []
        records = await self.list_all()
        return [
            r for r in records
            if r.database_url.strip() == url and r.guild_id != exclude_guild_id
        ]

    def invalidate_cache(self, guild_id: Optional[int] = None) -> None:
        if guild_id is not None:
            self._cache.pop(guild_id, None)
            self._cache_timestamps.pop(guild_id, None)
        else:
            self._cache.clear()
            self._cache_timestamps.clear()
