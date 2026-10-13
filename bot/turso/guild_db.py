"""
Verwaltung der per-Guild Turso-Datenbanken und Schema-Initialisierung.

Mehrere Server können sich **eine** Datenbank teilen: Der Master-Eintrag jedes
Servers zeigt auf dieselbe URL + Token, und :class:`GuildDatabaseManager`
hält pro URL+Token nur **einen** gemeinsamen :class:`TursoClient` (statt einen
pro Server). Damit teilen sich alle Server auf derselben Datenbank auch eine
Verbindung, einen Schema-Check und die Rate-Limits.
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
        # Ein Client pro URL+Token — Server, die sich eine Datenbank teilen,
        # teilen sich auch das Verbindungsobjekt.
        self._clients: Dict[str, TursoClient] = {}
        # guild_id -> Client-Key, damit beim Umbauen/Trennen eines Servers
        # der gemeinsame Client anderer Server nicht mit geschlossen wird.
        self._guild_keys: Dict[int, str] = {}
        self._schema_initialized: Dict[int, bool] = {}

    @staticmethod
    def _client_key(database_url: str, auth_token: str) -> str:
        return f"{database_url.strip()}|{auth_token.strip()}"

    async def get_client_for_guild(self, guild_id: int) -> Optional[TursoClient]:
        """Liefert den TursoClient für einen Server (oder None, falls keine DB konfiguriert)."""
        record = await self.master_store.get_guild_db(guild_id)
        if record is None:
            return None

        key = self._client_key(record.database_url, record.auth_token)
        cached = self._clients.get(key)
        if cached is None or cached.auth_token != record.auth_token.strip() or cached.raw_url != record.database_url:
            if cached is not None:
                await cached.close()
                self._clients.pop(key, None)
            cached = TursoClient(record.database_url, record.auth_token)
            self._clients[key] = cached

        # Alte Zuordnung entfernen, BEVOR der alte Client geprüft wird — sonst
        # würde dieser Server selbst als noch-nutzend zählen und der alte
        # Client könnte nie geschlossen werden.
        old_key = self._guild_keys.pop(guild_id, None)
        if old_key is not None and old_key != key:
            # Der Server zeigt jetzt auf eine andere Datenbank — den alten
            # Client nur schließen, wenn kein anderer Server mehr darauf zeigt.
            await self._maybe_close(old_key)

        self._guild_keys[guild_id] = key
        return cached

    async def _maybe_close(self, key: str) -> None:
        """Schließt den Client hinter ``key`` — nur wenn er frei geworden ist."""
        client = self._clients.get(key)
        if client is None:
            return
        if key in self._guild_keys.values():
            return  # Mindestens ein Server nutzt ihn noch (geteilte DB)
        self._clients.pop(key, None)
        await client.close()

    async def close_guild(self, guild_id: int) -> None:
        """
        Gibt die Verbindung eines Servers frei (Aufruf beim Trennen/Verlassen).

        Bei geteilten Datenbanken bleibt der gemeinsame Client für die anderen
        Server bestehen; exklusive Verbindungen werden geschlossen.
        """
        key = self._guild_keys.pop(guild_id, None)
        self._schema_initialized.pop(guild_id, None)
        if key is not None:
            await self._maybe_close(key)

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
        self._guild_keys.clear()
        self._schema_initialized.clear()
