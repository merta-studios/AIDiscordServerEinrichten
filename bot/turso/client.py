"""
Turso (libSQL) HTTP Client & SQLite-Fallback.

Unterstützt:
- Turso Cloud (libSQL Pipeline Protocol v2 über HTTPS)
- Lokale SQLite-Datenbanken (für Offline/Tests/Fallback)
- Speicher- & Auslastungsberechnung (PRAGMA page_count, page_size)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

log = logging.getLogger("relay.turso")

__all__ = ("TursoClient", "TursoResult", "TursoError", "normalize_turso_url")

# Turso Free Tier Kontingent: 9 GB Speicher
TURSO_FREE_QUOTA_BYTES = 9 * 1024 * 1024 * 1024  # 9 GB


class TursoError(RuntimeError):
    """Fehler bei der Kommunikation mit Turso oder Ausführung von SQL."""


@dataclass(slots=True)
class TursoResult:
    columns: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    raw_rows: List[List[Any]] = field(default_factory=list)
    affected_rows: int = 0
    last_insert_id: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "count": len(self.rows),
            "affected_rows": self.affected_rows,
            "last_insert_id": self.last_insert_id,
        }


def normalize_turso_url(url: str) -> str:
    """Wandelt libsql:// oder unvollständige URLs in eine saubere HTTPS-Basis-URL um."""
    u = (url or "").strip().rstrip("/")
    if u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    elif not u.startswith("http://") and not u.startswith("https://") and not u.startswith("sqlite:") and u != ":memory:":
        u = "https://" + u
    return u


def _to_turso_arg(val: Any) -> Dict[str, Any]:
    if val is None:
        return {"type": "null"}
    if isinstance(val, bool):
        return {"type": "integer", "value": "1" if val else "0"}
    if isinstance(val, int):
        return {"type": "integer", "value": str(val)}
    if isinstance(val, float):
        return {"type": "float", "value": val}
    if isinstance(val, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(val).decode("ascii")}
    return {"type": "text", "value": str(val)}


def _from_turso_cell(cell: Dict[str, Any]) -> Any:
    cell_type = cell.get("type", "null")
    if cell_type == "null":
        return None
    if cell_type == "integer":
        raw = cell.get("value")
        return int(raw) if raw is not None else 0
    if cell_type == "float":
        raw = cell.get("value")
        return float(raw) if raw is not None else 0.0
    if cell_type == "text":
        return cell.get("value", "")
    if cell_type == "blob":
        b64 = cell.get("base64", "")
        try:
            return base64.b64decode(b64)
        except Exception:
            return b64
    return cell.get("value")


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


class TursoClient:
    """Verbindungs-Client zu einer Turso-Datenbank oder lokalem SQLite."""

    def __init__(
        self,
        database_url: str,
        auth_token: str = "",
        *,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = 15.0,
    ) -> None:
        self.raw_url = database_url
        self.url = normalize_turso_url(database_url)
        self.auth_token = auth_token.strip()
        self._session = session
        self._owns_session = False
        self.timeout = timeout
        self.is_sqlite = self.url.startswith("sqlite:") or self.url == ":memory:" or not (self.url.startswith("http://") or self.url.startswith("https://"))
        self._sqlite_path: Optional[str] = None
        if self.is_sqlite:
            if self.url == ":memory:":
                self._sqlite_path = ":memory:"
            elif self.url.startswith("sqlite:///"):
                self._sqlite_path = self.url[len("sqlite:///"):]
            elif self.url.startswith("sqlite://"):
                self._sqlite_path = self.url[len("sqlite://"):]
            elif self.url.startswith("sqlite:"):
                self._sqlite_path = self.url[len("sqlite:"):]
            else:
                self._sqlite_path = self.url

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def execute(self, sql: str, args: Optional[Sequence[Any]] = None) -> TursoResult:
        results = await self.execute_batch([(sql, args or [])])
        return results[0] if results else TursoResult()

    async def execute_batch(self, statements: Sequence[Tuple[str, Sequence[Any]]]) -> List[TursoResult]:
        if not statements:
            return []

        if self.is_sqlite:
            return await asyncio.to_thread(self._execute_sqlite_batch, statements)

        session = await self._get_session()
        pipeline_url = f"{self.url}/v2/pipeline"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
        }

        requests_payload: List[Dict[str, Any]] = []
        for sql, args in statements:
            turso_args = [_to_turso_arg(a) for a in args]
            requests_payload.append({
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": turso_args,
                },
            })
        requests_payload.append({"type": "close"})

        body = {"requests": requests_payload}

        try:
            async with session.post(
                pipeline_url,
                headers=headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status == 401 or response.status == 403:
                    raise TursoError("Authentifizierung bei Turso fehlgeschlagen (ungültiges Token).")
                if response.status != 200:
                    text = await response.text()
                    raise TursoError(f"Turso API antwortet mit HTTP {response.status}: {text[:300]}")

                data = await response.json()
        except asyncio.TimeoutError as exc:
            raise TursoError("Zeitüberschreitung bei der Anfrage an die Turso-Datenbank.") from exc
        except aiohttp.ClientError as exc:
            raise TursoError(f"Netzwerkfehler bei Turso-Verbindung: {exc}") from exc

        raw_results = data.get("results", [])
        output: List[TursoResult] = []

        for item in raw_results:
            item_type = item.get("type")
            if item_type == "error":
                err_msg = item.get("error", {}).get("message", "Unbekannter SQL-Fehler")
                raise TursoError(f"SQL-Fehler in Turso: {err_msg}")

            if item_type == "ok":
                resp = item.get("response", {})
                if resp.get("type") == "execute":
                    res = resp.get("result", {})
                    cols = [c.get("name", "") for c in res.get("cols", [])]
                    rows: List[Dict[str, Any]] = []
                    raw_rows: List[List[Any]] = []

                    for raw_row in res.get("rows", []):
                        parsed_cells = [_from_turso_cell(cell) for cell in raw_row]
                        raw_rows.append(parsed_cells)
                        row_dict = {col: val for col, val in zip(cols, parsed_cells)}
                        rows.append(row_dict)

                    output.append(TursoResult(
                        columns=cols,
                        rows=rows,
                        raw_rows=raw_rows,
                        affected_rows=res.get("affected_row_count", 0),
                        last_insert_id=res.get("last_insert_rowid"),
                    ))

        return output

    def _execute_sqlite_batch(self, statements: Sequence[Tuple[str, Sequence[Any]]]) -> List[TursoResult]:
        path = self._sqlite_path or ":memory:"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        results: List[TursoResult] = []

        try:
            for sql, args in statements:
                cursor.execute(sql, tuple(args))
                cols = [d[0] for d in cursor.description] if cursor.description else []
                raw_rows: List[List[Any]] = []
                rows: List[Dict[str, Any]] = []

                if cols:
                    fetched = cursor.fetchall()
                    for r in fetched:
                        raw_rows.append(list(r))
                        rows.append(dict(r))

                results.append(TursoResult(
                    columns=cols,
                    rows=rows,
                    raw_rows=raw_rows,
                    affected_rows=cursor.rowcount,
                    last_insert_id=cursor.lastrowid,
                ))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise TursoError(f"SQLite-Fehler: {exc}") from exc
        finally:
            conn.close()

        return results

    async def get_storage_stats(self) -> Dict[str, Any]:
        """Ermittelt Auslastung, Speicherverbrauch und Tabellen der Datenbank."""
        batch_queries = [
            ("PRAGMA page_count;", []),
            ("PRAGMA page_size;", []),
            ("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;", []),
        ]
        try:
            results = await self.execute_batch(batch_queries)
        except Exception as exc:
            log.warning("Fehler beim Abruf der Turso-Storage-Stats: %s", exc)
            return {
                "used_bytes": 0,
                "used_formatted": "0 B",
                "quota_bytes": TURSO_FREE_QUOTA_BYTES,
                "quota_formatted": format_bytes(TURSO_FREE_QUOTA_BYTES),
                "free_bytes": TURSO_FREE_QUOTA_BYTES,
                "free_formatted": format_bytes(TURSO_FREE_QUOTA_BYTES),
                "usage_percent": 0.0,
                "page_count": 0,
                "page_size": 4096,
                "tables_count": 0,
                "tables": [],
            }

        page_count = 0
        if results[0].rows:
            page_count = int(list(results[0].rows[0].values())[0] or 0)

        page_size = 4096
        if len(results) > 1 and results[1].rows:
            page_size = int(list(results[1].rows[0].values())[0] or 4096)

        tables: List[str] = []
        if len(results) > 2 and results[2].rows:
            tables = [str(r.get("name") or "") for r in results[2].rows if r.get("name")]

        used_bytes = page_count * page_size
        quota_bytes = TURSO_FREE_QUOTA_BYTES
        free_bytes = max(0, quota_bytes - used_bytes)
        usage_pct = round((used_bytes / quota_bytes) * 100, 4) if quota_bytes > 0 else 0.0

        return {
            "used_bytes": used_bytes,
            "used_formatted": format_bytes(used_bytes),
            "quota_bytes": quota_bytes,
            "quota_formatted": format_bytes(quota_bytes),
            "free_bytes": free_bytes,
            "free_formatted": format_bytes(free_bytes),
            "usage_percent": usage_pct,
            "page_count": page_count,
            "page_size": page_size,
            "tables_count": len(tables),
            "tables": tables,
        }
