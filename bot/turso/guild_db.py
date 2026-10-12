"""
Verwaltung der per-Guild Turso-Datenbanken und Schema-Initialisierung.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .client import TursoClient, TursoError
from .master import MasterTursoStore, GuildDbRecord

log = logging.getLogger("relay.turso.guild_db")

__all__ = ("GuildDatabaseManager",)


class GuildDatabaseManager:
    """Verwaltet Verbindungen und Schema für Server-spezifische Turso-Datenbanken."""

    def __init__(self, master_store: MasterTursoStore) -> None:
        self.master_store = master_store
        self._clients: Dict[int, TursoClient] = {}
        self._schema_initialized: Dict[int, bool] = {}

    async def get_client_for_guild(self, guild_id: int) -> Optional[TursoClient]:
        """Liefert den TursoClient für einen Server (oder None, falls keine DB konfiguriert)."""
        record = await self.master_store.get_guild_db(guild_id)
        if record is None:
            return None

        cached = self._clients.get(guild_id)
        if cached is not None and cached.url == record.database_url and cached.auth_token == record.auth_token:
            return cached

        # Alten schließen, neuen anlegen
        if cached is not None:
            await cached.close()

        client = TursoClient(record.database_url, record.auth_token)
        self._clients[guild_id] = client
        return client

    async def ensure_schema(self, client: TursoClient, guild_id: Optional[int] = None) -> None:
        """Initialisiert die Standard-Tabellen (systems, triggers, key_value_store, custom_commands)."""
        if guild_id and self._schema_initialized.get(guild_id):
            return

        schema_statements: List[Tuple[str, List]] = [
            ("""
            CREATE TABLE IF NOT EXISTS systems (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                enabled INTEGER DEFAULT 1,
                config TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """, []),
            ("""
            CREATE TABLE IF NOT EXISTS triggers (
                id TEXT PRIMARY KEY,
                system_id TEXT,
                event_type TEXT NOT NULL,
                filter_json TEXT,
                action_json TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (system_id) REFERENCES systems(id) ON DELETE CASCADE
            );
            """, []),
            ("""
            CREATE TABLE IF NOT EXISTS key_value_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                type TEXT DEFAULT 'string',
                updated_at TEXT NOT NULL
            );
            """, []),
            ("""
            CREATE TABLE IF NOT EXISTS custom_commands (
                name TEXT PRIMARY KEY,
                description TEXT,
                system_id TEXT,
                response_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (system_id) REFERENCES systems(id) ON DELETE CASCADE
            );
            """, []),
        ]

        await client.execute_batch(schema_statements)
        if guild_id:
            self._schema_initialized[guild_id] = True
        log.debug("Schema für Guild-Datenbank erfolgreich initialisiert.")

    async def close_all(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        self._schema_initialized.clear()
