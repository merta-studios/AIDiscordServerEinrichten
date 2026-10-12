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
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import Config
from ..util import iso, now_utc
from .client import TursoClient, TursoError

log = logging.getLogger("relay.turso.master")

__all__ = ("MasterTursoStore", "GuildDbRecord")


@dataclass(slots=True)
class GuildDbRecord:
    guild_id: int
    guild_name: str
    database_url: str
    auth_token: str
    configured_by: str
    created_at: str
    updated_at: str

    def to_dict(self, *, include_token: bool = False) -> Dict[str, Any]:
        data = {
            "guild_id": str(self.guild_id),
            "guild_name": self.guild_name,
            "database_url": self.database_url,
            "configured_by": self.configured_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_token:
            data["auth_token"] = self.auth_token
        return data


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
        sql = "SELECT guild_id, guild_name, database_url, auth_token, configured_by, created_at, updated_at FROM server_databases WHERE guild_id = ? LIMIT 1;"

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
    ) -> GuildDbRecord:
        """Speichert oder aktualisiert die Datenbank-Konfiguration eines Servers."""
        await self.init()
        client = self._get_client()
        now_str = iso(now_utc())

        # Prüfen ob vorhanden
        existing = await self.get_guild_db(guild_id)
        created_str = existing.created_at if existing else now_str

        sql = """
        INSERT INTO server_databases (guild_id, guild_name, database_url, auth_token, configured_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            guild_name = excluded.guild_name,
            database_url = excluded.database_url,
            auth_token = excluded.auth_token,
            configured_by = excluded.configured_by,
            updated_at = excluded.updated_at;
        """
        await client.execute(sql, [
            str(guild_id),
            guild_name,
            database_url.strip(),
            auth_token.strip(),
            configured_by,
            created_str,
            now_str,
        ])

        record = GuildDbRecord(
            guild_id=guild_id,
            guild_name=guild_name,
            database_url=database_url.strip(),
            auth_token=auth_token.strip(),
            configured_by=configured_by,
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
        sql = "SELECT guild_id, guild_name, database_url, auth_token, configured_by, created_at, updated_at FROM server_databases ORDER BY guild_name;"
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
                    created_at=r.get("created_at") or "",
                    updated_at=r.get("updated_at") or "",
                ))
            return output
        except Exception as exc:
            log.error("Fehler beim Listen aller Guild-DBs: %s", exc)
            return []

    def invalidate_cache(self, guild_id: Optional[int] = None) -> None:
        if guild_id is not None:
            self._cache.pop(guild_id, None)
            self._cache_timestamps.pop(guild_id, None)
        else:
            self._cache.clear()
            self._cache_timestamps.clear()
