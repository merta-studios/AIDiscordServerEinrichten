"""
Tests für Turso libSQL Client, Master-DB, Guild-DB, Systems-Engine und Endpunkte.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
import discord
from bot.config import Config
from bot.turso.client import TursoClient, format_bytes, normalize_turso_url
from bot.turso.master import MasterTursoStore, legacy_configured_user_id
from bot.turso.guild_db import GuildDatabaseManager
from bot.turso.engine import TursoSystemsEngine, _render_template
from bot.turso.service import TursoService
from scripts._fake_discord import FakeGuild, FakeMember, FakeMessage, FakeRole, FakeUser


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✅ {name}")
    else:
        msg = f"  ❌ {name}" + (f" — {detail}" if detail else "")
        print(msg)
        raise AssertionError(msg)


async def run_tests() -> None:
    print("\n── Turso Client & URL Normalisierung ─────────────────────────")
    check("normalize libsql:// to https://",
          normalize_turso_url("libsql://my-db.turso.io") == "https://my-db.turso.io")
    check("normalize host to https://",
          normalize_turso_url("my-db.turso.io") == "https://my-db.turso.io")
    check("normalize sqlite preserves sqlite",
          normalize_turso_url("sqlite://test.db") == "sqlite://test.db")
    check("format_bytes KB", format_bytes(2048) == "2.0 KB")
    check("format_bytes MB", format_bytes(5 * 1024 * 1024) == "5.00 MB")
    check("format_bytes GB", format_bytes(9 * 1024 * 1024 * 1024) == "9.00 GB")

    test_dir = tempfile.mkdtemp(prefix="turso_test_")
    try:
        cfg = Config(
            discord_token="adse_test_token_12345678901234567890",
            data_dir=test_dir,
            turso_database_url=f"sqlite:///{test_dir}/master_test.db",
            turso_auth_token="",
        )

        service = TursoService(cfg)
        await service.init()

        print("\n── Master-DB & Caching ───────────────────────────────────────")
        check("Noch keine DB für Guild 123", not await service.is_guild_db_configured(123))

        guild_db_path = f"sqlite:///{test_dir}/guild_123.db"
        ok, msg = await service.connect_guild_database(
            guild_id=123,
            guild_name="Test Guild",
            database_url=guild_db_path,
            auth_token="",
            configured_by="Owner",
        )
        check("connect_guild_database erfolgreich", ok, msg)
        check("DB für Guild 123 jetzt konfiguriert", await service.is_guild_db_configured(123))

        # Cache-Hit prüfen
        rec = await service.master.get_guild_db(123)
        check("Guild-DB Record vorhanden", rec is not None and rec.guild_name == "Test Guild")
        check("Cache-Hits zählen hoch", service.master.cache_hits > 0)

        print("\n── Speicherauslastung & Status ───────────────────────────────")
        status = await service.get_guild_status(123)
        check("Status configured: true", status["configured"] is True)
        storage = status["storage"]
        check("Speicher-Quota 9 GB", storage["quota_formatted"] == "9.00 GB")
        check("Tabellen angelegt (systems, triggers, key_value_store, custom_commands)",
              {"systems", "triggers", "key_value_store", "custom_commands"} <= set(storage["tables"]))

        print("\n── Template Rendering & Systems Engine ───────────────────────")
        rendered = _render_template("Hallo {user_mention} auf {server}! Wir sind {member_count}.", {
            "user_mention": "<@123>",
            "server": "Mein Server",
            "member_count": 50,
        })
        check("Template Rendering korrekt", rendered == "Hallo <@123> auf Mein Server! Wir sind 50.")

        print("\n── Systeme & Trigger anlegen ─────────────────────────────────")
        sys_data = {
            "id": "self_roles_colors",
            "name": "Farben Self Roles",
            "type": "self_roles",
            "description": "Farbrollen per Klick",
            "triggers": [
                {
                    "event": "reaction_add",
                    "filter": {"message_id": "999", "emoji": "🔴"},
                    "action": {"type": "add_role", "role_id": "111"},
                },
                {
                    "event": "reaction_remove",
                    "filter": {"message_id": "999", "emoji": "🔴"},
                    "action": {"type": "remove_role", "role_id": "111"},
                },
                {
                    "event": "cron",
                    "filter": {"time": "05:00"},
                    "action": {"type": "send_message", "channel_id": "888", "content": "Guten Morgen um 5 Uhr!"},
                },
            ],
        }
        saved_sys = await service.save_system(123, sys_data)
        check("System gespeichert mit ID", saved_sys["id"] == "self_roles_colors")
        check("3 Trigger gespeichert", len(saved_sys["triggers"]) == 3)

        systems = await service.list_systems(123)
        check("Systeme listen liefert gespeichertes System", len(systems) >= 1)

        print("\n── Trigger-Ausführung (Self Roles) ───────────────────────────")
        fake_guild = FakeGuild(gid=123, name="Test Guild")
        role_red = FakeRole(111, "Rot", 1, 0)
        role_red.guild = fake_guild
        fake_guild.roles.append(role_red)
        fake_member = FakeMember(777, "UserRed", [], guild=fake_guild)
        fake_guild.members.append(fake_member)

        # Trigger simulieren: Member klickt auf Reaktion 🔴
        class FakeReactionPayload:
            guild_id = 123
            message_id = 999
            user_id = 777
            member = fake_member
            emoji = discord.PartialEmoji(name="🔴")

        class FakeClient:
            guilds = [fake_guild]
            def get_guild(self, gid: int):
                return fake_guild if gid == 123 else None

        fake_client = FakeClient()
        await service.engine.handle_reaction_add(fake_client, FakeReactionPayload())
        check("Rolle Rot nach reaction_add vergeben", role_red in fake_member.roles)

        # Reaction remove simulieren
        await service.engine.handle_reaction_remove(fake_client, FakeReactionPayload())
        check("Rolle Rot nach reaction_remove entfernt", role_red not in fake_member.roles)

        print("\n── SQL-Ausführung & Key-Value-Store ──────────────────────────")
        await service.execute_query(123, "CREATE TABLE custom_levels (user_id TEXT PRIMARY KEY, xp INTEGER);")
        await service.execute_query(123, "INSERT INTO custom_levels (user_id, xp) VALUES (?, ?);", ["777", 150])
        res = await service.execute_query(123, "SELECT xp FROM custom_levels WHERE user_id = ?;", ["777"])
        check("SQL Query liefert Daten", len(res["rows"]) == 1 and res["rows"][0]["xp"] == 150)

        await service.set_variable(123, "join_count", 42, "number")
        vars_res = await service.get_variables(123)
        check("Variable join_count gesetzt", any(v["key"] == "join_count" for v in vars_res))

        print("\n── System Löschen & Disconnect ───────────────────────────────")
        del_ok = await service.delete_system(123, "self_roles_colors")
        check("System gelöscht", del_ok is True)
        systems_after = await service.list_systems(123)
        check("Keine Systeme mehr vorhanden", len(systems_after) == 0)

        dis_ok = await service.disconnect_guild_database(123)
        check("Datenbank getrennt", dis_ok is True)
        check("is_guild_db_configured ist False", not await service.is_guild_db_configured(123))

        unconf_status = await service.get_guild_status(123)
        check("Status nach Trennung configured: false", unconf_status["configured"] is False)
        check("Status enthält setup_command /manage_database", unconf_status["setup_command"] == "/manage_database")

        print("\n── Mehrere Server teilen sich eine Datenbank ─────────────────")
        admin_id = "555000111222333444"
        shared_db_path = f"sqlite:///{test_dir}/guild_shared.db"
        ok, msg = await service.connect_guild_database(
            guild_id=201, guild_name="Server A", database_url=shared_db_path, auth_token="",
            configured_by=f"Klaus ({admin_id})", configured_by_id=admin_id,
        )
        check("Server A richtet DB ein (configured_by_id gespeichert)", ok, msg)

        # Wiederverwenden-Liste: derselbe Admin sieht seine DB auf einem anderen Server
        reusable = await service.list_reusable_databases_for_user(int(admin_id), exclude_guild_id=202)
        check("Wiederverwenden-Liste enthält Server A",
              len(reusable) == 1 and reusable[0]["source_guild_id"] == 201, str(reusable))
        # Ein fremder Admin sieht sie nicht
        check("Fremder Admin sieht keine fremde DB",
              len(await service.list_reusable_databases_for_user(999)) == 0)
        # Legacy-Erkennung über den Freitext ("Name (id)")
        check("legacy_configured_user_id parst Freitext",
              legacy_configured_user_id(f"Klaus ({admin_id})") == admin_id)
        by_user = await service.master.list_by_user(admin_id)
        check("list_by_user findet den Eintrag (exakte ID)", len(by_user) == 1 and by_user[0].guild_id == 201)

        # Server B übernimmt die Datenbank per Wiederverwenden (blauer Button)
        ok, msg = await service.reuse_guild_database(
            guild_id=202, guild_name="Server B", source_guild_id=201,
            configured_by=f"Klaus ({admin_id})", configured_by_id=admin_id,
        )
        check("Server B nutzt die Datenbank von Server A wieder", ok, msg)
        check("Beide Server konfiguriert",
              await service.is_guild_db_configured(201) and await service.is_guild_db_configured(202))

        # Ein fremder Admin darf die DB von Server A NICHT übernehmen
        ok_denied, msg_denied = await service.reuse_guild_database(
            guild_id=203, guild_name="Server C", source_guild_id=201,
            configured_by="Fremd (999)", configured_by_id="999",
        )
        check("Fremd-Übernahme abgelehnt", not ok_denied and "anderen Admin" in msg_denied, msg_denied)

        # Geteilte DB → gemeinsamer Client (eine Verbindung für beide Server)
        c_a = await service.guild_manager.get_client_for_guild(201)
        c_b = await service.guild_manager.get_client_for_guild(202)
        check("Geteilte DB → gemeinsamer Client", c_a is c_b)

        # Auf Server B angelegte Systeme sind auf Server A sichtbar (gleiche DB)
        await service.save_system(202, {
            "id": "shared_welcome", "name": "Geteilt", "type": "welcome",
            "triggers": [{"event": "member_join",
                          "action": {"type": "send_message", "channel_id": "1", "content": "Hi"}}],
        })
        triggers_a = await service.engine.get_active_triggers(201)
        check("System über Server A sichtbar (geteilte DB)",
              any(t["system_id"] == "shared_welcome" for t in triggers_a), str(triggers_a))

        # Eine Änderung über B invalidiert den Trigger-Cache auch für A
        await service.save_system(202, {"id": "shared_welcome", "name": "Geteilt", "type": "welcome",
                                        "enabled": False})
        triggers_a = await service.engine.get_active_triggers(201)
        check("Deaktivierung über B greift sofort auch für A",
              all(t["system_id"] != "shared_welcome" for t in triggers_a), str(triggers_a))

        # Status zeigt die Teilung
        status_a = await service.get_guild_status(201)
        check("Status: shared_with enthält Server B",
              status_a.get("shared") is True
              and any(s["guild_id"] == "202" for s in status_a.get("shared_with", [])),
              str(status_a.get("shared_with")))
        check("Status: configured_by_id gesetzt", status_a.get("configured_by_id") == admin_id)

        # Server B verlässt den Bot → geteilte DB bleibt, nur Verknüpfung weg
        summary_b = await service.purge_guild(202)
        check("Purge B (geteilt): Verknüpfung entfernt",
              summary_b["had_database"] and not summary_b["exclusive"], str(summary_b))
        check("Purge B (geteilt): keine Tabellen gelöscht", summary_b["dropped_tables"] == [], str(summary_b))
        check("Server A weiterhin konfiguriert", await service.is_guild_db_configured(201))
        systems_a_after = await service.list_systems(201)
        check("Daten für Server A noch vorhanden",
              any(s["id"] == "shared_welcome" for s in systems_a_after))

        # Server A verlässt den Bot → exklusive DB wird vollständig geleert
        summary_a = await service.purge_guild(201)
        check("Purge A (exklusiv): Tabellen gelöscht",
              summary_a["exclusive"] and len(summary_a["dropped_tables"]) >= 4, str(summary_a))
        check("Server A nicht mehr konfiguriert", not await service.is_guild_db_configured(201))
        raw = TursoClient(shared_db_path, "")
        res = await raw.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        check("Physische DB vollständig geleert", len(res.rows) == 0, str(res.rows))
        await raw.close()

        await service.guild_manager.close_all()

        print("\n── API-Endpoints: Database, Systems & Bot ────────────────────")
        from bot.web.app import build_app, start_web_server, stop_web_server, AppState
        from bot.sessions import SessionStore
        from scripts.smoke_test import Harness, free_port, _standard_guild, FakeClient

        guild = _standard_guild()
        fclient = FakeClient([guild])
        fstore = SessionStore(persist=False, max_per_guild=20)
        fstate = AppState(client=fclient, config=cfg, store=fstore)
        await fstate.turso_service.init()
        fapp = build_app(fstate)
        port = free_port()
        runner = await start_web_server(fapp, "127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"
        h = Harness(fstate, base, fstore)

        try:
            await h.new_token("manage", "manage", guild_id=guild.id)
            api = h.api("manage")
            # 1. Ohne konfigurierte DB
            status_res = await api.call("GET", "/api/v1/database/status")
            check("Status unconfigured: 200 OK", status_res["status"] == 200)
            check("configured ist False", status_res["json"]["data"]["configured"] is False)
            check("setup_command ist /manage_database", status_res["json"]["data"]["setup_command"] == "/manage_database")

            # systems ohne DB -> 412 DATABASE_REQUIRED
            sys_res = await api.call("GET", "/api/v1/systems")
            check("GET /api/v1/systems ohne DB -> 412", sys_res["status"] == 412)
            check("Fehlercode DATABASE_REQUIRED", sys_res["json"]["error"]["code"] == "DATABASE_REQUIRED")
            check("Hint verweist auf /manage_database", "/manage_database" in sys_res["json"]["error"]["hint"])

            post_sys_no_db = await api.call("POST", "/api/v1/systems", body={"name": "Test"})
            check("POST /api/v1/systems ohne DB -> 412", post_sys_no_db["status"] == 412)

            # 2. DB konfigurieren
            await fstate.turso_service.connect_guild_database(
                guild.id, guild.name, f"sqlite:///{test_dir}/guild_api.db", "", "Admin"
            )
            status_after = await api.call("GET", "/api/v1/database/status")
            check("Status nach Connect -> configured: true", status_after["json"]["data"]["configured"] is True)

            # 3. System anlegen
            create_res = await api.call("POST", "/api/v1/systems", body={
                "id": "welcome_bot",
                "name": "Willkommen System",
                "type": "welcome",
                "triggers": [
                    {
                        "event": "member_join",
                        "action": {"type": "send_message", "channel_id": "123", "content": "Hi {user}!"}
                    }
                ]
            })
            check("POST /api/v1/systems -> 200", create_res["status"] == 200)
            check("System angelegt", create_res["json"]["data"]["system"]["name"] == "Willkommen System")

            # 4. System lesen & updaten
            get_sys = await api.call("GET", "/api/v1/systems/welcome_bot")
            check("GET /api/v1/systems/welcome_bot -> 200", get_sys["status"] == 200)

            patch_sys = await api.call("PATCH", "/api/v1/systems/welcome_bot", body={"enabled": False})
            check("PATCH /api/v1/systems/welcome_bot -> 200", patch_sys["status"] == 200 and patch_sys["json"]["data"]["system"]["enabled"] is False)

            # 5. Raw SQL ausführen
            sql_res = await api.call("POST", "/api/v1/database/query", body={
                "sql": "CREATE TABLE test_scores (player TEXT, score INT);"
            })
            check("POST /api/v1/database/query -> 200", sql_res["status"] == 200)

            # 6. Variablen setzen
            var_post = await api.call("POST", "/api/v1/database/variables", body={"key": "max_players", "value": 100, "type": "number"})
            check("POST /api/v1/database/variables -> 200", var_post["status"] == 200)

            var_get = await api.call("GET", "/api/v1/database/variables")
            check("GET /api/v1/database/variables -> 200", var_get["status"] == 200 and len(var_get["json"]["data"]["variables"]) >= 1)

            # 7. Globales Bot-Profil GET /api/v1/bot
            bot_res = await api.call("GET", "/api/v1/bot")
            check("GET /api/v1/bot -> 200", bot_res["status"] == 200 and "username" in bot_res["json"]["data"])

            # 8. System löschen
            del_sys = await api.call("DELETE", "/api/v1/systems/welcome_bot")
            check("DELETE /api/v1/systems/welcome_bot -> 200", del_sys["status"] == 200)

        finally:
            await h.close()
            await stop_web_server(runner)
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)

    print("\n════════════════════════════════════════════════════════════════")
    print("  ✅ Alle Turso- & Systems-Tests erfolgreich bestanden!")
    print("════════════════════════════════════════════════════════════════\n")


if __name__ == "__main__":
    asyncio.run(run_tests())
