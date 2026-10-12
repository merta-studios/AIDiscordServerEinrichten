"""
Ausführungs-Engine für dynamische Server-Systeme & Scripting.

Verarbeitet Triggers für:
- Member Join (Welcome, Auto-Role, DM, Counter)
- Member Leave (Farewell, Counter)
- Message Create (Auto-Responder, Leveling/XP, Filter)
- Reaction Add/Remove (Self Roles, Reaction Roles, Role Toggle)
- Cron / Scheduled Tasks (z. B. täglich um 5 Uhr)
- Custom Commands

In-Memory Caching:
Aktive Systeme und Trigger werden im Speicher gehalten und bei Änderungen
invalidiert, sodass Discord-Events (Reaktionen, Nachrichten) keine unnötigen
Turso-Abfragen erzeugen und Rate Limits geschont werden.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import discord

from ..util import iso, now_utc
from .guild_db import GuildDatabaseManager

log = logging.getLogger("relay.turso.engine")

__all__ = ("TursoSystemsEngine",)

TEMPLATE_VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _render_template(template: str, variables: Dict[str, Any]) -> str:
    """Ersetzt {user_mention}, {server}, {member_count} etc. im Text."""
    if not template:
        return template

    def repl(m: re.Match) -> str:
        key = m.group(1).lower()
        if key in variables:
            return str(variables[key])
        return m.group(0)

    return TEMPLATE_VAR_RE.sub(repl, template)


def _render_dict_deep(data: Any, variables: Dict[str, Any]) -> Any:
    if isinstance(data, str):
        return _render_template(data, variables)
    if isinstance(data, dict):
        return {k: _render_dict_deep(v, variables) for k, v in data.items()}
    if isinstance(data, list):
        return [_render_dict_deep(item, variables) for item in data]
    return data


class TursoSystemsEngine:
    """Verarbeitet Event-Trigger und führt Scripting-Aktionen aus."""

    CACHE_TTL_SECONDS = 300.0  # 5 Minuten In-Memory-Cache

    def __init__(self, guild_manager: GuildDatabaseManager) -> None:
        self.guild_manager = guild_manager
        # guild_id -> list of active triggers
        self._triggers_cache: Dict[int, List[Dict[str, Any]]] = {}
        # guild_id -> list of systems
        self._systems_cache: Dict[int, List[Dict[str, Any]]] = {}
        self._cache_timestamps: Dict[int, float] = {}
        self._no_db_cache: Dict[int, float] = {}
        # Tracking für tägliche Cron-Trigger: (guild_id, trigger_id, date_str)
        self._executed_cron_runs: Set[Tuple[int, str, str]] = set()
        self.engine_cache_hits: int = 0
        self.engine_cache_misses: int = 0

    def invalidate(self, guild_id: int) -> None:
        """Invalidiert den Cache eines Servers bei Änderungen an Systemen."""
        self._triggers_cache.pop(guild_id, None)
        self._systems_cache.pop(guild_id, None)
        self._cache_timestamps.pop(guild_id, None)
        self._no_db_cache.pop(guild_id, None)

    async def get_active_triggers(self, guild_id: int) -> List[Dict[str, Any]]:
        """Lädt alle aktiven Trigger eines Servers (aus dem Cache oder der DB)."""
        now = time.monotonic()
        if guild_id in self._no_db_cache:
            if now - self._no_db_cache[guild_id] < 60.0:
                self.engine_cache_hits += 1
                return []

        if guild_id in self._triggers_cache:
            if now - self._cache_timestamps.get(guild_id, 0.0) < self.CACHE_TTL_SECONDS:
                self.engine_cache_hits += 1
                return self._triggers_cache[guild_id]

        self.engine_cache_misses += 1
        client = await self.guild_manager.get_client_for_guild(guild_id)
        if client is None:
            self._no_db_cache[guild_id] = now
            return []

        try:
            await self.guild_manager.ensure_schema(client, guild_id=guild_id)
            sql = """
            SELECT t.id, t.system_id, t.event_type, t.filter_json, t.action_json, t.enabled,
                   s.enabled as system_enabled, s.name as system_name
            FROM triggers t
            JOIN systems s ON t.system_id = s.id
            WHERE t.enabled = 1 AND s.enabled = 1;
            """
            res = await client.execute(sql)
            triggers: List[Dict[str, Any]] = []
            for r in res.rows:
                filter_data = {}
                action_data = {}
                try:
                    if r.get("filter_json"):
                        filter_data = json.loads(r["filter_json"]) if isinstance(r["filter_json"], str) else r["filter_json"]
                except Exception:
                    pass
                try:
                    if r.get("action_json"):
                        action_data = json.loads(r["action_json"]) if isinstance(r["action_json"], str) else r["action_json"]
                except Exception:
                    pass

                triggers.append({
                    "id": r["id"],
                    "system_id": r["system_id"],
                    "system_name": r.get("system_name"),
                    "event_type": r["event_type"],
                    "filter": filter_data,
                    "action": action_data,
                })

            self._triggers_cache[guild_id] = triggers
            self._cache_timestamps[guild_id] = now
            return triggers
        except Exception as exc:
            log.warning("Fehler beim Laden der Trigger für Guild %s: %s", guild_id, exc)
            return []

    # ── Event Handler ────────────────────────────────────────────────────────

    async def handle_member_join(self, member: discord.Member) -> None:
        """Wird aufgerufen, wenn ein Mitglied dem Server beitritt."""
        guild = member.guild
        triggers = await self.get_active_triggers(guild.id)
        join_triggers = [t for t in triggers if t["event_type"] == "member_join"]
        if not join_triggers:
            return

        variables = {
            "user": member.display_name,
            "user_name": member.name,
            "user_mention": member.mention,
            "user_id": str(member.id),
            "guild_name": guild.name,
            "server": guild.name,
            "member_count": str(guild.member_count or len(guild.members)),
        }

        for trig in join_triggers:
            action = trig.get("action", {})
            try:
                await self._execute_action(guild, action, member=member, variables=variables)
            except Exception as exc:
                log.error("Fehler beim Ausführen von Join-Trigger %s: %s", trig.get("id"), exc)

    async def handle_member_remove(self, member: discord.Member) -> None:
        """Wird aufgerufen, wenn ein Mitglied den Server verlässt."""
        guild = member.guild
        triggers = await self.get_active_triggers(guild.id)
        leave_triggers = [t for t in triggers if t["event_type"] == "member_leave"]
        if not leave_triggers:
            return

        variables = {
            "user": member.display_name,
            "user_name": member.name,
            "user_mention": member.mention,
            "user_id": str(member.id),
            "guild_name": guild.name,
            "server": guild.name,
            "member_count": str(guild.member_count or len(guild.members)),
        }

        for trig in leave_triggers:
            action = trig.get("action", {})
            try:
                await self._execute_action(guild, action, member=member, variables=variables)
            except Exception as exc:
                log.error("Fehler beim Ausführen von Leave-Trigger %s: %s", trig.get("id"), exc)

    async def handle_message(self, message: discord.Message) -> None:
        """Wird aufgerufen, wenn eine Nachricht gesendet wird."""
        guild = message.guild
        if guild is None:
            return

        triggers = await self.get_active_triggers(guild.id)
        msg_triggers = [t for t in triggers if t["event_type"] == "message_create"]
        if not msg_triggers:
            return

        content = (message.content or "").strip()
        author = message.author

        variables = {
            "user": author.display_name,
            "user_name": author.name,
            "user_mention": author.mention,
            "user_id": str(author.id),
            "guild_name": guild.name,
            "server": guild.name,
            "channel_id": str(message.channel.id),
            "channel_name": getattr(message.channel, "name", ""),
            "message_id": str(message.id),
            "content": content,
        }

        for trig in msg_triggers:
            filt = trig.get("filter", {})
            if filt.get("ignore_bots", True) and author.bot:
                continue

            target_channel = filt.get("channel_id")
            if target_channel and str(target_channel) != str(message.channel.id):
                continue

            # Textfilter
            if "contains" in filt and filt["contains"].lower() not in content.lower():
                continue
            if "equals" in filt and filt["equals"].lower() != content.lower():
                continue
            if "starts_with" in filt and not content.lower().startswith(filt["starts_with"].lower()):
                continue

            action = trig.get("action", {})
            try:
                await self._execute_action(
                    guild, action,
                    member=author if isinstance(author, discord.Member) else None,
                    message=message,
                    variables=variables,
                )
            except Exception as exc:
                log.error("Fehler beim Ausführen von Message-Trigger %s: %s", trig.get("id"), exc)

    async def handle_reaction_add(self, client: discord.Client, payload: discord.RawReactionActionEvent) -> None:
        """Wird aufgerufen, wenn eine Reaktion hinterlassen wird (Self Roles)."""
        guild_id = payload.guild_id
        if not guild_id:
            return

        guild = client.get_guild(guild_id)
        if guild is None:
            return

        triggers = await self.get_active_triggers(guild_id)
        reaction_triggers = [t for t in triggers if t["event_type"] == "reaction_add"]
        if not reaction_triggers:
            return

        member = payload.member
        if member is None:
            member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        emoji_str = str(payload.emoji)
        emoji_name = payload.emoji.name

        variables = {
            "user": member.display_name,
            "user_name": member.name,
            "user_mention": member.mention,
            "user_id": str(member.id),
            "guild_name": guild.name,
            "message_id": str(payload.message_id),
            "emoji": emoji_str,
        }

        for trig in reaction_triggers:
            filt = trig.get("filter", {})
            if "message_id" in filt and str(filt["message_id"]) != str(payload.message_id):
                continue

            target_emoji = filt.get("emoji")
            if target_emoji:
                if target_emoji != emoji_str and target_emoji != emoji_name:
                    continue

            action = trig.get("action", {})
            try:
                await self._execute_action(guild, action, member=member, variables=variables)
            except Exception as exc:
                log.error("Fehler beim Ausführen von Reaction-Add-Trigger %s: %s", trig.get("id"), exc)

    async def handle_reaction_remove(self, client: discord.Client, payload: discord.RawReactionActionEvent) -> None:
        """Wird aufgerufen, wenn eine Reaktion entfernt wird (Self Roles entfernen)."""
        guild_id = payload.guild_id
        if not guild_id:
            return

        guild = client.get_guild(guild_id)
        if guild is None:
            return

        triggers = await self.get_active_triggers(guild_id)
        reaction_triggers = [t for t in triggers if t["event_type"] in ("reaction_remove", "reaction_add")]
        if not reaction_triggers:
            return

        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        emoji_str = str(payload.emoji)
        emoji_name = payload.emoji.name

        variables = {
            "user": member.display_name,
            "user_name": member.name,
            "user_mention": member.mention,
            "user_id": str(member.id),
            "guild_name": guild.name,
            "message_id": str(payload.message_id),
            "emoji": emoji_str,
        }

        for trig in reaction_triggers:
            filt = trig.get("filter", {})
            if "message_id" in filt and str(filt["message_id"]) != str(payload.message_id):
                continue

            target_emoji = filt.get("emoji")
            if target_emoji:
                if target_emoji != emoji_str and target_emoji != emoji_name:
                    continue

            action = dict(trig.get("action", {}))
            # Wenn es ein Toggle- oder Add-Role-Trigger war, wandeln wir die Aktion beim Reaction-Remove in ein remove_role um
            action_type = action.get("type")
            if trig["event_type"] == "reaction_add":
                if action_type in ("add_role", "toggle_role"):
                    action["type"] = "remove_role"
                else:
                    continue

            try:
                await self._execute_action(guild, action, member=member, variables=variables)
            except Exception as exc:
                log.error("Fehler beim Ausführen von Reaction-Remove-Trigger %s: %s", trig.get("id"), exc)

    async def check_cron_triggers(self, client: discord.Client) -> None:
        """Prüft periodisch (z.B. alle 30s) zeitgesteuerte Trigger (z. B. 5 Uhr jeden Tag)."""
        now = datetime.datetime.now(datetime.timezone.utc)
        current_time_str = now.strftime("%H:%M")
        current_date_str = now.strftime("%Y-%m-%d")

        for guild in client.guilds:
            triggers = await self.get_active_triggers(guild.id)
            cron_triggers = [t for t in triggers if t["event_type"] == "cron"]
            if not cron_triggers:
                continue

            variables = {
                "guild_name": guild.name,
                "server": guild.name,
                "member_count": str(guild.member_count or len(guild.members)),
                "date": current_date_str,
                "time": current_time_str,
            }

            for trig in cron_triggers:
                filt = trig.get("filter", {})
                target_time = filt.get("time", "05:00").strip()

                if target_time == current_time_str:
                    run_key = (guild.id, trig["id"], current_date_str)
                    if run_key in self._executed_cron_runs:
                        continue  # Wurde heute schon ausgeführt

                    self._executed_cron_runs.add(run_key)
                    action = trig.get("action", {})
                    try:
                        log.info("Führe Cron-Trigger '%s' für Server '%s' um %s UTC aus.",
                                 trig.get("id"), guild.name, current_time_str)
                        await self._execute_action(guild, action, variables=variables)
                    except Exception as exc:
                        log.error("Fehler beim Ausführen von Cron-Trigger %s: %s", trig.get("id"), exc)

        # Aufräumen von alten Cron-Keys
        if len(self._executed_cron_runs) > 1000:
            self._executed_cron_runs = {k for k in self._executed_cron_runs if k[2] == current_date_str}

    # ── Aktionen ausführen ───────────────────────────────────────────────────

    async def _execute_action(
        self,
        guild: discord.Guild,
        action: Dict[str, Any],
        *,
        member: Optional[discord.Member] = None,
        message: Optional[discord.Message] = None,
        variables: Optional[Dict[str, Any]] = None,
    ) -> None:
        action_type = action.get("type", "").lower()
        vars_dict = variables or {}

        # 1. Rolle hinzufügen
        if action_type == "add_role":
            role_id = action.get("role_id")
            if role_id and member:
                role = guild.get_role(int(role_id))
                if role and role not in member.roles:
                    await member.add_roles(role, reason="System-Trigger (Turso Self Roles/Auto-Role)")
                    log.info("Rolle '%s' an %s (%s) vergeben.", role.name, member.display_name, member.id)

        # 2. Rolle entfernen
        elif action_type == "remove_role":
            role_id = action.get("role_id")
            if role_id and member:
                role = guild.get_role(int(role_id))
                if role and role in member.roles:
                    await member.remove_roles(role, reason="System-Trigger (Turso Self Roles)")
                    log.info("Rolle '%s' von %s (%s) entfernt.", role.name, member.display_name, member.id)

        # 3. Rolle togglen mit optionaler exklusiver Gruppe
        elif action_type == "toggle_role":
            role_id = action.get("role_id")
            if role_id and member:
                role = guild.get_role(int(role_id))
                if role:
                    # Exklusive Gruppe: andere Rollen dieser Gruppe entfernen
                    exclusive_group = action.get("exclusive_group") or []
                    for other_id in exclusive_group:
                        if int(other_id) != role.id:
                            other_role = guild.get_role(int(other_id))
                            if other_role and other_role in member.roles:
                                await member.remove_roles(other_role, reason="Self Roles (Exklusiv-Gruppe)")

                    if role in member.roles:
                        await member.remove_roles(role, reason="System-Trigger (Self Roles Toggle)")
                    else:
                        await member.add_roles(role, reason="System-Trigger (Self Roles Toggle)")

        # 4. Nachricht senden
        elif action_type == "send_message":
            channel_id = action.get("channel_id")
            content = _render_template(action.get("content") or "", vars_dict)
            embeds_raw = action.get("embeds") or []
            rendered_embeds: List[discord.Embed] = []

            for emb_data in embeds_raw:
                rendered = _render_dict_deep(emb_data, vars_dict)
                title = rendered.get("title")
                desc = rendered.get("description")
                col = rendered.get("color")
                c = discord.Color.from_str(str(col)) if col else discord.Color.blurple()
                embed = discord.Embed(title=title, description=desc, color=c)
                for f in rendered.get("fields", []):
                    embed.add_field(name=f.get("name", ""), value=f.get("value", ""), inline=f.get("inline", False))
                rendered_embeds.append(embed)

            # Direktnachricht an Mitglied?
            if action.get("dm", False) and member:
                try:
                    await member.send(content=content or None, embeds=rendered_embeds or None)
                    return
                except Exception as exc:
                    log.warning("DM an %s konnte nicht gesendet werden: %s", member.id, exc)

            target_channel = guild.get_channel(int(channel_id)) if channel_id else None
            if target_channel and hasattr(target_channel, "send"):
                # Persona/Webhook-Support
                webhook_data = action.get("webhook")
                if webhook_data and hasattr(target_channel, "webhooks"):
                    from ..web.webhook_ops import send_as_webhook
                    persona_spec = webhook_data if isinstance(webhook_data, dict) else {"name": str(webhook_data)}
                    send_kwargs: Dict[str, Any] = {}
                    if content:
                        send_kwargs["content"] = content
                    if rendered_embeds:
                        send_kwargs["embeds"] = rendered_embeds
                    await send_as_webhook(target_channel, persona_spec, send_kwargs)
                else:
                    await target_channel.send(content=content or None, embeds=rendered_embeds or None)

        # 5. Antworten auf Nachricht
        elif action_type == "reply" and message:
            content = _render_template(action.get("content") or "", vars_dict)
            await message.reply(content=content)

        # 6. Reaktion hinzufügen
        elif action_type == "add_reaction" and message:
            emoji = action.get("emoji")
            if emoji:
                await message.add_reaction(emoji)

        # 7. SQL ausführen
        elif action_type == "execute_sql":
            sql = action.get("sql")
            args = action.get("args") or []
            if sql:
                client = await self.guild_manager.get_client_for_guild(guild.id)
                if client:
                    rendered_args = [_render_template(str(a), vars_dict) if isinstance(a, str) else a for a in args]
                    await client.execute(sql, rendered_args)

        # 8. Variable setzen oder inkrementieren
        elif action_type in ("set_variable", "increment_variable"):
            key = action.get("key")
            val = action.get("value", "1")
            client = await self.guild_manager.get_client_for_guild(guild.id)
            if client and key:
                now_str = iso(now_utc())
                if action_type == "increment_variable":
                    by = int(action.get("by", 1))
                    sql = """
                    INSERT INTO key_value_store (key, value, type, updated_at)
                    VALUES (?, ?, 'number', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = CAST((CAST(key_value_store.value AS INTEGER) + ?) AS TEXT),
                        updated_at = excluded.updated_at;
                    """
                    await client.execute(sql, [key, str(by), now_str, by])
                else:
                    sql = """
                    INSERT INTO key_value_store (key, value, type, updated_at)
                    VALUES (?, ?, 'string', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at;
                    """
                    await client.execute(sql, [key, str(val), now_str])
