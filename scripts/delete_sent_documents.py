#!/usr/bin/env python3
"""
Löscht bereits zugestellte Server-Dokument-DMs — SOFORT, ohne Deploy.

Der Bot hat früher das Server-Dokument per DM an den Server-Owner geschickt;
jetzt geht es an den Bot-Owner. Dieses Skript räumt die alten DMs auf:

    DISCORD_BOT_TOKEN=<Bot-Token> python scripts/delete_sent_documents.py

Ohne Argumente werden bereinigt:
  * die Legacy-Empfänger aus ``bot.discord_bot.LEGACY_DOCUMENT_RECIPIENTS``
    (inkl. des Dokuments, das an den Server-Owner zugestellt wurde),
  * der Empfänger aus ``data/server_document_delivery.json`` (letzte Zustellung).

Optional können eigene Empfänger-IDs als Argumente übergeben werden:
    python scripts/delete_sent_documents.py 1015212381619626014 234567890123456789

Gelöscht werden in den DM-Verläufen (bis 500 Nachrichten je Empfänger) alle
Nachrichten mit einem Anhang ``server-dokument-*.pdf`` — sonst nichts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, List

import discord

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bot.discord_bot import DOCUMENT_DELIVERY_PATH, LEGACY_DOCUMENT_RECIPIENTS  # noqa: E402

SCAN_LIMIT = 500


def _is_document_message(message: Any) -> bool:
    for att in getattr(message, "attachments", None) or []:
        name = (getattr(att, "filename", "") or "").lower()
        if name.startswith("server-dokument-") and name.endswith(".pdf"):
            return True
    return False


def _target_ids() -> List[int]:
    ids: List[int] = []
    for rid in LEGACY_DOCUMENT_RECIPIENTS:
        rid = int(rid)
        if rid not in ids:
            ids.append(rid)
    try:
        with open(REPO_ROOT / DOCUMENT_DELIVERY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rid = (data.get("last_delivery") or {}).get("recipient_id")
        if rid is not None and str(rid).lstrip("-").isdigit() and int(rid) not in ids:
            ids.append(int(rid))
    except (FileNotFoundError, OSError, ValueError):
        pass
    for arg in sys.argv[1:]:
        if arg.lstrip("-").isdigit() and int(arg) not in ids:
            ids.append(int(arg))
    return ids


class DocCleaner(discord.Client):
    def __init__(self, targets: List[int]) -> None:
        super().__init__(intents=discord.Intents.default())
        self.targets = targets
        self.total_deleted = 0

    async def on_ready(self) -> None:
        print(f"Eingeloggt als {self.user} — bereinige {len(self.targets)} Empfänger …")
        self.cleaned: List[int] = []
        for uid in self.targets:
            try:
                user = self.get_user(uid) or await self.fetch_user(uid)
            except discord.HTTPException as exc:
                print(f"  ✗ Empfänger {uid}: nicht erreichbar ({exc})")
                continue
            try:
                dm = user.dm_channel or await user.create_dm()
            except discord.HTTPException as exc:
                print(f"  ✗ Empfänger {uid}: DM-Kanal nicht möglich ({exc})")
                continue
            deleted = 0
            try:
                async for message in dm.history(limit=SCAN_LIMIT):
                    if _is_document_message(message):
                        try:
                            await message.delete()
                            deleted += 1
                        except discord.HTTPException as exc:
                            print(f"  ✗ Nachricht {message.id}: Löschen fehlgeschlagen ({exc})")
            except discord.HTTPException as exc:
                print(f"  ✗ Empfänger {uid}: Verlauf nicht lesbar ({exc})")
            self.total_deleted += deleted
            self.cleaned.append(uid)
            print(f"  ✓ Empfänger {uid} ({user}): {deleted} Server-Dokument-DM(s) gelöscht")
        print(f"Fertig — insgesamt {self.total_deleted} Nachricht(en) gelöscht.")
        self._mark_cleaned()
        await self.close()

    def _mark_cleaned(self) -> None:
        """Bereinigte IDs im Status-File markieren, damit der Bot nicht erneut scannt."""
        try:
            path = REPO_ROOT / DOCUMENT_DELIVERY_PATH
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                if not isinstance(state, dict):
                    state = {}
            except (FileNotFoundError, OSError, ValueError):
                state = {}
            cleaned = {int(x) for x in (state.get("cleaned_recipient_ids") or [])
                       if str(x).lstrip("-").isdigit()}
            cleaned.update(self.cleaned)
            state["cleaned_recipient_ids"] = sorted(cleaned)
            state.pop("last_delivery", None)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            print(f"Status-File aktualisiert: {path}")
        except OSError as exc:
            print(f"Warnung: Status-File konnte nicht geschrieben werden: {exc}")


async def amain() -> int:
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token or token == "ci.placeholder.token":
        print("Fehler: DISCORD_BOT_TOKEN fehlt (Token des Produktions-Bots setzen).", file=sys.stderr)
        return 2
    targets = _target_ids()
    if not targets:
        print("Keine Empfänger angegeben und keine Legacy-/Zustell-IDs gefunden — nichts zu tun.")
        return 0
    client = DocCleaner(targets)
    async with client:
        await client.login(token)
        await client.connect()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(amain()))
    except KeyboardInterrupt:
        raise SystemExit(130)
