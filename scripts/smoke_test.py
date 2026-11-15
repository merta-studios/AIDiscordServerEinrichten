#!/usr/bin/env python3
"""
Smoke-Test für AIDiscordServerEinrichten — **ohne** Discord-Verbindung.

Er startet den echten aiohttp-Server mit einem simulierten Discord-Client und
einem simulierten Server, erzeugt echte Sitzungs-Tokens und fährt die
wichtigsten Endpoints durch. Damit ist sichergestellt, dass

* alle Module importierbar und alle Routen registrierbar sind,
* Auth, Scope-Prüfung, Rate-Limit und Fehlerformat funktionieren,
* die Serialisierer mit echten Objekten klarkommen,
* der Setup-Plan-Validator Vorlagen akzeptiert und Fehler erkennt,
* Console, Healthcheck und Prompt-Erzeugung liefern, was sie sollen.

Aufruf::

    python3 scripts/smoke_test.py            # alles
    python3 scripts/smoke_test.py -v         # mit Details

Exit-Code 0 = grün. Läuft in CI ohne Netzwerk und ohne Bot-Token.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import socket
import sys
from datetime import timedelta
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISCORD_BOT_TOKEN", "smoke.test.token")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("PUBLIC_URL", "https://relay.example.com")
os.environ.setdefault("SESSION_FILE", "/tmp/adse-smoke/sessions.json")

import aiohttp  # noqa: E402
import discord  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.sessions import SessionStore  # noqa: E402
from bot.web.app import AppState, build_app, start_web_server, stop_web_server  # noqa: E402

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# ─────────────────────────────────────────────────────────────────────────────
#  Simulierter Discord-Server (eigene Datei, echte discord.py-Subklassen)
# ─────────────────────────────────────────────────────────────────────────────

# Die simulierten Discord-Objekte leben in ``scripts/_fake_discord.py`` — echte
# discord.py-Subklassen, damit die isinstance()-Prüfungen der API greifen.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fake_discord import (  # noqa: E402
    GUILD_ID, OWNER_ID, TEXT_ID, USER_ID, FakeClient, FakeGuild, FakeMessage,
    MutableGuild, now,
)

OTHER_GUILD_ID = 1212121212121212
_now = now

# ─────────────────────────────────────────────────────────────────────────────
#  Test-Rahmen
# ─────────────────────────────────────────────────────────────────────────────

PASSED: List[str] = []
FAILED: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    """Verzeichnet einen einzelnen Test."""
    if condition:
        PASSED.append(name)
        if VERBOSE:
            print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  ❌ {name}{(' — ' + detail) if detail else ''}")
    return bool(condition)


def free_port() -> int:
    """Sucht einen freien TCP-Port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Api:
    """Kleiner HTTP-Wrapper für die Tests."""

    def __init__(self, base: str, token: Optional[str] = None) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, path: str, *, body: Any = None,
                   raw: Optional[str] = None,
                   token: Optional[str] = "__default__") -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        effective = self.token if token == "__default__" else token
        if effective:
            headers["Authorization"] = f"Bearer {effective}"
        kwargs: Dict[str, Any] = {"headers": headers}
        if raw is not None:
            headers["Content-Type"] = "application/json"
            kwargs["data"] = raw.encode("utf-8")
        elif body is not None:
            kwargs["json"] = body
        async with self.session.request(method, self.base + path, **kwargs) as response:
            text = await response.text()
            try:
                parsed = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = {"_raw": text[:400]}
            return {"status": response.status, "json": parsed, "text": text,
                    "headers": dict(response.headers)}


class Harness:
    """Hält Server, Store, Tokens und die API-Clients zusammen."""

    def __init__(self, state: AppState, base: str, store: SessionStore) -> None:
        self.state = state
        self.base = base
        self.store = store
        self.tokens: Dict[str, str] = {}
        self.apis: Dict[str, Api] = {}
        self.anon = Api(base)

    async def new_token(self, key: str, mode: str, *, guild_id: int = GUILD_ID,
                        ttl_hours: float = 1.0) -> str:
        session, token = await self.store.create(
            guild_id=guild_id, guild_name="Smoke-Test Server", created_by=USER_ID,
            created_by_name="Tester", mode=mode, ttl_hours=ttl_hours,
        )
        self.tokens[key] = token
        self.apis[key] = Api(self.base, token)
        return token

    def api(self, key: str) -> Api:
        return self.apis[key]

    async def close(self) -> None:
        for api in list(self.apis.values()) + [self.anon]:
            await api.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Testgruppen
# ─────────────────────────────────────────────────────────────────────────────


async def t_public(h: Harness) -> None:
    print("── Öffentliche Endpoints ─────────────────────────────────────")
    health = await h.anon.call("GET", "/api/health")
    check("GET /api/health → 200", health["status"] == 200, str(health["json"])[:200])
    head_health = await h.anon.call("HEAD", "/api/health")
    check("HEAD /api/health → 200 (UptimeRobot-kompatibel)",
          head_health["status"] == 200, str(head_health["status"]))
    check("health.ok == true", health["json"].get("ok") is True)
    data = health["json"].get("data", {})
    check("health.status == 'healthy'", data.get("status") == "healthy", str(data)[:200])
    check("health nennt Service-Namen", data.get("service") == "AIDiscordServerEinrichten")
    check("health meldet Bot verbunden", data.get("bot") == "connected", str(data.get("bot")))
    check("health meldet Gateway-Latenz", isinstance(data.get("gateway_latency_ms"), (int, float)))
    check("health nennt öffentliche URL", data.get("base_url") == "https://relay.example.com")
    check("CORS-Header *", health["headers"].get("Access-Control-Allow-Origin") == "*")
    check("X-Request-ID gesetzt", bool(health["headers"].get("X-Request-ID")))
    check("Server-Header nicht leakend", "aiohttp" not in (health["headers"].get("Server") or ""))

    root = await h.anon.call("GET", "/")
    check("GET / → 200 ok", root["status"] == 200 and root["json"].get("ok") is True)

    diag = await h.anon.call("GET", "/api/diagnostics")
    check("GET /api/diagnostics ohne Token → 200", diag["status"] == 200, str(diag["json"])[:200])
    ddata = diag["json"].get("data", {})
    check("diagnostics nennt verdict + what_to_do",
          "verdict" in ddata and isinstance(ddata.get("what_to_do"), list), str(ddata)[:200])
    check("diagnostics nennt Neustart-Buch + Plattform-Urteil",
          isinstance(ddata.get("restarts"), dict) and "platform_verdict" in ddata, str(ddata)[:200])
    check("diagnostics verrät kein Token",
          "adse_" not in diag["text"] and os.environ["DISCORD_BOT_TOKEN"] not in diag["text"])

    console = await h.anon.call("GET", "/console")
    check("GET /console → HTML", console["status"] == 200
          and "text/html" in console["headers"].get("Content-Type", ""))
    check("Console hat Copy-Button", "Prompt kopieren" in console["text"])
    check("Console enthält kein echtes Token",
          not re.search(r"adse_[A-Za-z0-9_-]{20,}", console["text"]))

    robots = await h.anon.call("GET", "/robots.txt")
    check("robots.txt verbietet alles", robots["status"] == 200 and "Disallow: /" in robots["text"])

    caps = await h.anon.call("GET", "/api/v1/capabilities")
    check("capabilities ohne Token erreichbar", caps["status"] == 200, str(caps["json"])[:200])
    caps_data = caps["json"].get("data", {})
    check("capabilities listet >100 Endpoints", caps_data.get("endpoint_count", 0) > 100,
          str(caps_data.get("endpoint_count")))
    check("capabilities erklärt Bearer-Auth",
          "bearer" in json.dumps(caps_data.get("authentication", {})).lower())
    check("capabilities hat curl-Templates", "curl" in json.dumps(caps_data.get("curl_templates", {})))
    check("capabilities nennt die Rate-Limits", "rate_limits" in caps_data)
    check("capabilities listet Setup-Templates",
          len(caps_data.get("setup_templates", [])) >= 5)


async def t_auth(h: Harness) -> None:
    print("── Authentifizierung ─────────────────────────────────────────")
    no_token = await h.anon.call("GET", "/api/v1/guild")
    check("Ohne Token → 401", no_token["status"] == 401, str(no_token["json"])[:200])
    check("401 ok:false", no_token["json"].get("ok") is False)
    check("401 code TOKEN_MISSING",
          no_token["json"].get("error", {}).get("code") == "TOKEN_MISSING")
    check("401 enthält Hint", bool(no_token["json"].get("error", {}).get("hint")))
    check("401 enthält request_id",
          bool(no_token["json"].get("error", {}).get("request_id")))

    bad = await h.anon.call("GET", "/api/v1/guild", token="adse_falsch")
    check("Falsches Token → 401", bad["status"] == 401)
    check("Falsches Token → TOKEN_INVALID",
          bad["json"].get("error", {}).get("code") == "TOKEN_INVALID")

    token = await h.new_token("danger", "danger")
    check("Token beginnt mit adse_", token.startswith("adse_"))
    check("Token hat genug Entropie (>= 40 Zeichen)", len(token) >= 40, str(len(token)))
    session = h.store.verify(token)
    check("Token wird nur als Hash gespeichert", session.token_hash != token)
    check("Session-Prefix maskiert das Token", "…" in session.token_prefix)
    check("Session-Public-Dict enthält kein Klartext-Token",
          token not in json.dumps(session.to_public_dict()))

    api = h.api("danger")
    me = await api.call("GET", "/api/v1/me")
    check("GET /api/v1/me → 200", me["status"] == 200, str(me["json"])[:300])
    me_data = me["json"].get("data", {})
    check("me nennt den Server", me_data.get("guild", {}).get("name") == "Smoke-Test Server")
    check("me meldet Bot-Administrator",
          me_data.get("permissions", {}).get("administrator") is True)
    check("me meldet keine fehlenden Rechte",
          me_data.get("permissions", {}).get("missing_for_full_control") == [])

    session_info = await api.call("GET", "/api/v1/session")
    check("GET /api/v1/session → 200", session_info["status"] == 200)
    check("Session-Antwort ohne Klartext-Token", token not in session_info["text"])

    prompt_text = await api.call("GET", "/api/v1/prompt?variant=short&format=text")
    check("Prompt als text/plain", prompt_text["status"] == 200
          and "text/plain" in prompt_text["headers"].get("Content-Type", ""))
    check("Prompt nennt capabilities", "/api/v1/capabilities" in prompt_text["text"])
    check("Prompt < 2000 Zeichen (Discord-Limit)", len(prompt_text["text"]) < 2000,
          f"{len(prompt_text['text'])} Zeichen")
    check("Prompt nennt den Server-Namen", "Smoke-Test Server" in prompt_text["text"])
    check("Prompt enthält das echte Token", token in prompt_text["text"])

    prompt_long = await api.call("GET", "/api/v1/prompt?variant=long")
    check("Langer Prompt enthält Setup-Endpoints", "/api/v1/setup" in prompt_long["text"])
    prompt_sys = await api.call("GET", "/api/v1/prompt?variant=system&format=text")
    check("System-Prompt → text/plain", prompt_sys["status"] == 200
          and "text/plain" in prompt_sys["headers"].get("Content-Type", ""))
    check("System-Prompt nennt API-Pfad + Token",
          "/api/v1/capabilities" in prompt_sys["text"] and token in prompt_sys["text"])
    check("System-Prompt warnt vor destruktiven Aktionen",
          "destruktiv" in prompt_sys["text"] or "Destruktives" in prompt_sys["text"])
    prompt_bad = await api.call("GET", "/api/v1/prompt?variant=unbekannt")
    check("Unbekannte Prompt-Variante → 400", prompt_bad["status"] == 400)


async def t_read(h: Harness) -> None:
    print("── Lese-Endpoints ────────────────────────────────────────────")
    api = h.api("danger")

    g = await api.call("GET", "/api/v1/guild")
    check("GET /api/v1/guild → 200", g["status"] == 200, str(g["json"])[:300])
    gd = g["json"].get("data", {})
    check("Guild-ID ist String (kein JSON-Zahlenverlust)", isinstance(gd.get("id"), str))
    check("Guild-Zählungen stimmen", gd.get("counts", {}).get("channels") == 3,
          str(gd.get("counts")))
    check("Guild meldet Bot-Administrator",
          gd.get("bot_member", {}).get("is_administrator") is True)

    tree = await api.call("GET", "/api/v1/channels/tree")
    check("Kanalbaum → 200", tree["status"] == 200, str(tree["json"])[:200])
    td = tree["json"].get("data", {})
    check("Baum hat 1 Kategorie", len(td.get("categories", [])) == 1)
    check("Kategorie enthält 2 Kanäle",
          len(td["categories"][0].get("channels", [])) == 2 if td.get("categories") else False)

    channels = await api.call("GET", "/api/v1/channels")
    check("Kanalliste → 200", channels["status"] == 200)
    check("Kanalliste zählt 3", channels["json"]["data"].get("count") == 3)

    single = await api.call("GET", f"/api/v1/channels/{TEXT_ID}")
    check("Einzelkanal → 200", single["status"] == 200)
    check("Einzelkanal heißt 'regeln'", single["json"]["data"].get("name") == "regeln")

    missing = await api.call("GET", "/api/v1/channels/123456789012345678")
    check("Unbekannter Kanal → 404", missing["status"] == 404, str(missing["json"])[:250])
    check("404-Code ist CHANNEL_NOT_FOUND",
          missing["json"].get("error", {}).get("code") == "CHANNEL_NOT_FOUND")

    bad_id = await api.call("GET", "/api/v1/channels/1")
    check("Ungültige Snowflake-ID → 400 (nicht 404)", bad_id["status"] == 400,
          str(bad_id["json"])[:200])

    roles = await api.call("GET", "/api/v1/roles")
    check("Rollen → 200", roles["status"] == 200)
    rd = roles["json"]["data"]
    check("Rollen tragen editable-Flag",
          bool(rd.get("roles")) and all("editable_by_bot" in r for r in rd["roles"]))
    check("Rollenfarben als Zahl + Hex",
          all(isinstance(r.get("color"), int) for r in rd["roles"])
          and any((r.get("color_hex") or "").startswith("#") for r in rd["roles"]),
          json.dumps(rd["roles"][1])[:250])

    members = await api.call("GET", "/api/v1/members")
    check("Mitglieder → 200", members["status"] == 200, str(members["json"])[:300])
    check("3 Mitglieder geliefert", members["json"]["data"].get("returned") == 3)

    search = await api.call("GET", "/api/v1/members/search?q=Tester")
    check("Mitgliedersuche → 200", search["status"] == 200, str(search["json"])[:200])
    check("Suche findet 'Tester'", search["json"]["data"].get("count", 0) >= 1)
    check("Suche ohne ?q → 400", (await api.call("GET", "/api/v1/members/search"))["status"] == 400)

    perms = await api.call("GET", "/api/v1/permissions")
    check("Permission-Referenz → 200", perms["status"] == 200)
    check("Referenz kennt moderate_members",
          any(p["name"] == "moderate_members" for p in perms["json"]["data"]["permissions"]))
    preset = await api.call("GET", "/api/v1/permissions?preset=moderator")
    check("Rollen-Vorlage abrufbar", preset["status"] == 200
          and preset["json"]["data"]["preset"]["key"] == "moderator")

    templates = await api.call("GET", "/api/v1/setup/templates")
    check("Setup-Vorlagen → 200", templates["status"] == 200)
    check("6 Vorlagen vorhanden", templates["json"]["data"].get("count") == 6)

    snapshot = await api.call("GET", "/api/v1/guild/snapshot?members=10")
    check("Snapshot → 200", snapshot["status"] == 200, str(snapshot["json"])[:400])
    sd = snapshot["json"].get("data", {})
    check("Snapshot hat guild/channels/roles",
          all(k in sd for k in ("guild", "channels", "roles")))
    check("Snapshot meldet Teilfehler graceful (kein 500)",
          "_error" in json.dumps(sd) or "automod" in sd)

    actions = await api.call("GET", "/api/v1/actions?limit=10")
    check("Action-Log → 200", actions["status"] == 200)
    check("Action-Log hat Einträge", actions["json"]["data"].get("count", 0) > 0)

    audit = await api.call("GET", "/api/v1/guild/audit-logs")
    check("Audit-Log → 200", audit["status"] == 200, str(audit["json"])[:200])

    invites = await api.call("GET", "/api/v1/invites")
    check("Invites → 200", invites["status"] == 200)

    automod = await api.call("GET", "/api/v1/automod/rules")
    check("AutoMod → 200", automod["status"] == 200)

    regions = await api.call("GET", "/api/v1/voice/regions")
    check("Voice-Regions → 200 oder sauberer Fehler",
          regions["status"] in (200, 501, 409), str(regions["json"])[:200])


async def t_scopes(h: Harness) -> None:
    print("── Berechtigungs-Stufen ──────────────────────────────────────")
    await h.new_token("read", "read")
    api = h.api("read")

    check("read-Scope darf lesen", (await api.call("GET", "/api/v1/guild"))["status"] == 200)
    blocked = await api.call("POST", "/api/v1/channels", body={"name": "neu", "type": "text"})
    check("read-Scope darf nicht schreiben → 403", blocked["status"] == 403,
          str(blocked["json"])[:250])
    check("403 nennt SCOPE_INSUFFICIENT",
          blocked["json"].get("error", {}).get("code") == "SCOPE_INSUFFICIENT")
    check("403 verweist auf /connect",
          "connect" in (blocked["json"].get("error", {}).get("hint") or "").lower())

    await h.new_token("read_write", "read_write")
    api_rw = h.api("read_write")
    check("read_write-Modus darf moderieren (Banns lesen → 200)",
          (await api_rw.call("GET", "/api/v1/bans"))["status"] == 200)

    for key, expected_scope in (("read", "read"), ("read_write", "danger")):
        session = h.store.verify(h.tokens[key])
        check(f"Session '{key}' hat Scope '{expected_scope}'", session.scope == expected_scope)

    from bot.sessions import MODES, normalize_mode
    check("Genau zwei Modi definiert", set(MODES) == {"read", "read_write"})
    check("Legacy-Modi werden auf 'Lesen + Schreiben' gemappt",
          normalize_mode("danger") == "read_write" and normalize_mode("manage") == "read_write"
          and normalize_mode("write") == "read_write" and normalize_mode("read") == "read")
    for mode, info in MODES.items():
        check(f"Modus '{mode}' hat Label+Emoji+Beschreibung",
              bool(info.get("label")) and bool(info.get("emoji")) and bool(info.get("description")))


async def t_errors(h: Harness) -> None:
    print("── Fehlerformate ─────────────────────────────────────────────")
    api = h.api("danger")

    unknown_path = await api.call("GET", "/api/v1/gibtsnicht")
    check("Unbekannter Pfad → 404 JSON", unknown_path["status"] == 404
          and unknown_path["json"].get("ok") is False, str(unknown_path["text"])[:200])
    check("404 verweist auf capabilities", "capabilities" in json.dumps(unknown_path["json"]))

    wrong_method = await api.call("DELETE", "/api/v1/capabilities")
    check("Falsche Methode → 405 JSON", wrong_method["status"] == 405,
          str(wrong_method["text"])[:200])

    bad_json = await api.call("POST", "/api/v1/setup/preview", raw="{kaputt")
    check("Ungültiges JSON → 400", bad_json["status"] == 400, str(bad_json["text"])[:200])
    check("400 nennt BODY_INVALID_JSON",
          bad_json["json"].get("error", {}).get("code") == "BODY_INVALID_JSON")

    empty = await api.call("POST", "/api/v1/setup", body={})
    check("Leerer Setup-Plan → 400", empty["status"] == 400)

    await h.new_token("foreign", "danger", guild_id=OTHER_GUILD_ID)
    foreign = await h.api("foreign").call("GET", "/api/v1/guild")
    check("Token für fremden Server → GUILD_UNAVAILABLE",
          foreign["status"] == 409
          and foreign["json"]["error"]["code"] == "GUILD_UNAVAILABLE",
          str(foreign["json"])[:250])


async def t_setup(h: Harness) -> None:
    print("── Setup-Plan-Validierung ────────────────────────────────────")
    api = h.api("danger")

    templates = await api.call("GET", "/api/v1/setup/templates")
    names = [t["key"] for t in templates["json"]["data"]["templates"]]
    check("Vorlagen-Schlüssel lesbar", names == sorted(names) or len(names) == 5, str(names))
    for name in names:
        tpl = await api.call("GET", f"/api/v1/setup/templates?name={name}")
        check(f"Vorlage '{name}' abrufbar", tpl["status"] == 200)
        plan = tpl["json"]["data"].get("plan") or tpl["json"]["data"]
        preview = await api.call("POST", "/api/v1/setup/preview", body=plan)
        ok = preview["status"] == 200 and preview["json"]["data"].get("valid") is True
        check(f"Vorlage '{name}' validiert fehlerfrei", ok,
              json.dumps(preview["json"].get("data", {}).get("errors"), ensure_ascii=False)[:300])

    bad_plan = {
        "roles": [{"permissions": ["gibts_nicht"]}],
        "categories": [{"channels": [{"name": "x", "type": "telegram"}]}],
        "messages": [{"content": "ohne kanal"}],
    }
    preview = await api.call("POST", "/api/v1/setup/preview", body=bad_plan)
    check("Fehlerhafter Plan → valid:false", preview["status"] == 200
          and preview["json"]["data"].get("valid") is False)
    errors = preview["json"]["data"].get("errors") or []
    check("Fehlerhafter Plan meldet ≥2 Fehler", len(errors) >= 2, json.dumps(errors)[:300])

    executed = await api.call("POST", "/api/v1/setup", body=bad_plan)
    check("Fehlerhafter Plan wird NICHT ausgeführt → 400", executed["status"] == 400)
    check("400 nennt SETUP_PLAN_INVALID",
          executed["json"].get("error", {}).get("code") == "SETUP_PLAN_INVALID")

    unknown = await api.call("POST", "/api/v1/setup", body={"template": "gibtsnicht"})
    check("Unbekannte Vorlage → TEMPLATE_NOT_FOUND",
          unknown["status"] == 400
          and unknown["json"]["error"]["code"] == "TEMPLATE_NOT_FOUND")


async def t_session_lifecycle(h: Harness) -> None:
    print("── Sitzungs-Lebenszyklus ─────────────────────────────────────")
    await h.new_token("lifecycle", "read", ttl_hours=1)
    api = h.api("lifecycle")
    token = h.tokens["lifecycle"]

    check("Neue Session nutzbar", (await api.call("GET", "/api/v1/session"))["status"] == 200)

    regen = await api.call("POST", "/api/v1/session/regenerate")
    check("Regenerate → 200", regen["status"] == 200, str(regen["json"])[:250])
    new_token = regen["json"].get("data", {}).get("token") or ""
    check("Regenerate liefert neues Token", new_token.startswith("adse_"))
    check("Altes Token ist danach ungültig", not _token_valid(h.store, token))
    async with _temp_api(h.base, new_token) as fresh:
        check("Neues Token funktioniert",
              (await fresh.call("GET", "/api/v1/session"))["status"] == 200)

    async with _temp_api(h.base, new_token) as dying:
        revoked = await dying.call("DELETE", "/api/v1/session")
        check("Revoke → 200", revoked["status"] == 200, str(revoked["json"])[:250])
        check("Revokedes Token → 401", (await dying.call("GET", "/api/v1/session"))["status"] == 401)

    unlimited, unlimited_token = await h.store.create(
        guild_id=GUILD_ID, guild_name="Smoke-Test Server", created_by=USER_ID,
        created_by_name="Tester", mode="read", ttl_hours=0.0,
    )
    check("TTL 0 = unbegrenzt gültig", unlimited.expires_at is None)
    check("Unbegrenztes Token funktioniert", _token_valid(h.store, unlimited_token))

    expiring, expiring_token = await h.store.create(
        guild_id=GUILD_ID, guild_name="Smoke-Test Server", created_by=USER_ID,
        created_by_name="Tester", mode="read", ttl_hours=1.0,
    )
    check("Token mit TTL ist gültig", _token_valid(h.store, expiring_token))
    expiring.expires_at = _now() - timedelta(minutes=5)   # künstlich abgelaufen
    check("Abgelaufenes Token → ungültig", not _token_valid(h.store, expiring_token))

    # ── Inaktivitäts-Stopp: verfügbar, aber 24 h ungenutzt ─────────────────
    # (eigene Guild, damit die Tokens der anderen Testgruppen in Ruhe bleiben)
    idle, idle_token = await h.store.create(
        guild_id=OTHER_GUILD_ID, guild_name="Vergessliche Guild", created_by=USER_ID,
        created_by_name="Tester", mode="read", ttl_hours=0.0,
    )
    idle.last_used_at = _now() - timedelta(hours=25)      # künstlich vergesslich
    stopped = await h.store.revoke_inactive(timedelta(hours=24))
    check("24 h ungenutztes Token wird automatisch gestoppt",
          any(s.id == idle.id for s in stopped), str([s.id for s in stopped]))
    check("Inaktivitäts-Stopp vermerkt auto:inactivity",
          idle.revoked_by == "auto:inactivity", str(idle.revoked_by))
    check("Gestopptes Token ist ungültig", not _token_valid(h.store, idle_token))

    fresh, fresh_token = await h.store.create(
        guild_id=OTHER_GUILD_ID, guild_name="Vergessliche Guild", created_by=USER_ID,
        created_by_name="Tester", mode="read", ttl_hours=0.0,
    )
    _token_valid(h.store, fresh_token)                    # Nutzung = letzter Kontakt
    check("Frisch benutztes Token bleibt aktiv", _token_valid(h.store, fresh_token))
    check("Inaktivitäts-Stopp 0/None = deaktiviert",
          await h.store.revoke_inactive(timedelta(0)) == []
          and await h.store.revoke_inactive(None) == [])

    # ── 401er sagen Arena AI, was der Admin vermutlich getan hat ───────────
    async with _temp_api(h.base, fresh_token) as dying:
        # Fall: Admin hat (per Button/API) getrennt oder neu generiert
        await h.store.revoke(session_id=fresh.id, by="button:1")
        dead = await dying.call("GET", "/api/v1/session")
        error = dead["json"].get("error", {})
        check("Getrenntes Token → 401 TOKEN_REVOKED mit Grund",
              dead["status"] == 401 and error.get("code") == "TOKEN_REVOKED"
              and "getrennt" in error.get("message", ""), json.dumps(error, ensure_ascii=False)[:200])
        check("401 nennt schnelle Wiederanbindung (/connect + Verbinden)",
              "Verbinden" in (error.get("hint") or "") and "connect" in (error.get("hint") or ""))

    async with _temp_api(h.base, "adse_völligUnbekannt") as stranger:
        unknown = await stranger.call("GET", "/api/v1/session")
        hint = unknown["json"].get("error", {}).get("hint") or ""
        check("Unbekanntes Token → 401 TOKEN_INVALID",
              unknown["status"] == 401
              and unknown["json"].get("error", {}).get("code") == "TOKEN_INVALID")
        check("401 listet die wahrscheinlichen Ursachen",
              all(needle in hint for needle in
                  ("neues Token generiert", "zurückgesetzt", "24 Stunden")),
              hint[:200])


@contextlib.asynccontextmanager
async def _temp_api(base: str, token: str):
    """Kurzlebiger API-Client, der seine aiohttp-Session wieder schließt."""
    api = Api(base, token)
    try:
        yield api
    finally:
        await api.close()


def _token_valid(store: SessionStore, token: str) -> bool:
    """``store.verify`` wirft bei ungültigen Tokens — hier als Ja/Nein."""
    try:
        return store.verify(token) is not None
    except Exception:
        return False


async def t_ratelimit(h: Harness) -> None:
    print("── Rate-Limiting ─────────────────────────────────────────────")
    await h.new_token("rl", "read")
    api = h.api("rl")
    limit = 5
    h.state.config.api_rate_limit = limit     # für diese Gruppe künstlich klein

    statuses: List[int] = []
    for _ in range(limit + 3):
        statuses.append((await api.call("GET", "/api/v1/session"))["status"])
    check(f"Rate-Limit ({limit}/Fenster) greift", 429 in statuses, f"Statuscodes: {statuses}")
    if 429 in statuses:
        blocked = await api.call("GET", "/api/v1/session")
        check("429-Antwort hat retry_after",
              blocked["json"].get("error", {}).get("details", {}).get("retry_after") is not None,
              json.dumps(blocked["json"])[:250])
        check("429 setzt Retry-After-Header", bool(blocked["headers"].get("Retry-After")))
        check("429 nennt RATE_LIMITED",
              blocked["json"].get("error", {}).get("code") == "RATE_LIMITED")

    await h.new_token("rl2", "read")
    check("Anderes Token hat ein eigenes Limit-Budget",
          (await h.api("rl2").call("GET", "/api/v1/session"))["status"] == 200)
    h.state.config.api_rate_limit = 400      # wieder hochsetzen für Folge-Gruppen


async def t_write(h: Harness) -> None:
    """
    Führt ``POST /api/v1/setup`` für ALLE Vorlagen wirklich aus.

    Das ist der Kern des ganzen Projekts: Rollen, Kategorien, Kanäle,
    Nachrichten, Invites und AutoMod-Regeln werden gegen einen beschreibbaren
    simulierten Server angelegt. Jede Vorlage muss fehlerfrei durchlaufen.
    """
    print("── Setup-Ausführung (alle Vorlagen) ──────────────────────────")
    api = h.api("danger")
    client = h.state.client

    templates = await api.call("GET", "/api/v1/setup/templates")
    names = [t["key"] for t in templates["json"]["data"]["templates"]]
    check("Vorlagenliste vollständig", len(names) == 6, str(names))

    for name in names:
        guild = MutableGuild()
        client.swap_guild(guild)                     # frischer Server pro Vorlage

        tpl = await api.call("GET", f"/api/v1/setup/templates?name={name}")
        plan = dict(tpl["json"]["data"])
        plan["delay_ms"] = 0                          # keine künstlichen Pausen

        result = await api.call("POST", "/api/v1/setup", body=plan)
        check(f"Vorlage '{name}' → HTTP 200", result["status"] == 200,
              str(result["json"])[:400])
        if result["status"] != 200:
            continue

        data = result["json"].get("data", {})
        failed = data.get("steps_failed")
        check(f"Vorlage '{name}' läuft ohne Fehlschritt", failed == 0,
              f"steps_failed={failed}, report=" + json.dumps(
                  [r for r in data.get("report", []) if not r.get("ok")],
                  ensure_ascii=False)[:500])
        check(f"Vorlage '{name}' meldet ok:true", data.get("ok") is True,
              f"aborted_at={data.get('aborted_at')}")
        check(f"Vorlage '{name}' legt Rollen an", len(data.get("created", {}).get("roles", [])) > 0)
        check(f"Vorlage '{name}' legt Kanäle an", len(guild.text_channels) + len(guild.voice_channels)
              + len(guild.categories) > 3,
              f"{len(guild.channels)} Kanäle gesamt")
        planned = len(plan.get("messages") or [])
        check(f"Vorlage '{name}' schreibt alle {planned} geplanten Nachricht(en)",
              len(guild.sent_messages) == planned,
              f"{len(guild.sent_messages)} gesendet, {planned} geplant")
        check(f"Vorlage '{name}' protokolliert Mutationen", len(guild.mutations) > 10,
              f"{len(guild.mutations)} Mutationen")

        # Ergebnis muss über die Lese-API sichtbar sein
        tree = await api.call("GET", "/api/v1/channels/tree")
        check(f"Vorlage '{name}': Kanalbaum danach lesbar", tree["status"] == 200,
              str(tree["json"])[:250])
        roles_after = await api.call("GET", "/api/v1/roles")
        check(f"Vorlage '{name}': Rollen danach lesbar", roles_after["status"] == 200)

    # Server zurücktauschen, damit die Folge-Gruppen wieder den Standard-Server sehen
    client.swap_guild(_standard_guild())


async def t_arena(h: Harness) -> None:
    """
    Der Arena-Upgrade-Flow: Webhook-Personen, Bot-Profil, Guides,
    echte Kanal-Mentions im Setup und Purge vor dem Erneuern.
    """
    print("── Arena-Flow: Webhooks, Bot-Profil, Guides, Mentions ───────")
    api = h.api("danger")
    client = h.state.client

    guild = MutableGuild()
    client.swap_guild(guild)
    try:
        # ── 1. Persona-Sendekomfort: Nachricht mit webhook-Spec ────────────
        sent = await api.call("POST", f"/api/v1/channels/{TEXT_ID}/messages", body={
            "webhook": {"name": "📜 Serverregeln"},
            "content": "Die neuen Regeln!",
            "pin": True,
        })
        check("Webhook-Persona senden → 200", sent["status"] == 200, str(sent["json"])[:300])
        sent_data = sent["json"].get("data", {})
        check("Antwort meldet via=webhook", sent_data.get("via") == "webhook")
        check("Webhook wurde angelegt", len(guild._webhooks) == 1,
              f"{len(guild._webhooks)} Webhooks")
        check("Persona-Mutation protokolliert",
              any(m.startswith("webhook.send:") for m in guild.mutations),
              str(guild.mutations[-5:]))

        # nochmal senden → derselbe Webhook wird wiederverwendet
        again = await api.call("POST", f"/api/v1/channels/{TEXT_ID}/messages", body={
            "webhook": {"name": "📜 Serverregeln"}, "content": "Update"})
        check("Zweite Persona-Nachricht → 200", again["status"] == 200)
        check("Webhook wird wiederverwendet (kein zweiter)", len(guild._webhooks) == 1,
              f"{len(guild._webhooks)} Webhooks")

        # ── 2. Webhook-Verwaltung ──────────────────────────────────────────
        hooks = await api.call("GET", "/api/v1/webhooks")
        check("GET /api/v1/webhooks listet Server-Webhooks",
              hooks["status"] == 200 and hooks["json"]["data"]["count"] == 1,
              str(hooks["json"])[:200])
        hook_id = hooks["json"]["data"]["webhooks"][0]["id"]

        patched = await api.call("PATCH", f"/api/v1/webhooks/{hook_id}",
                                 body={"name": "📜 Die Regeln"})
        check("Webhook umbenennen → 200", patched["status"] == 200, str(patched["json"])[:200])
        check("Webhook-Name geändert",
              patched["json"]["data"]["webhook"]["name"] == "📜 Die Regeln")

        direct = await api.call("POST", f"/api/v1/webhooks/{hook_id}/send", body={
            "content": "Direktgesendet", "username": "📣 News"})
        check("POST /webhooks/{id}/send → 200", direct["status"] == 200,
              str(direct["json"])[:300])
        check("Direkt-Send nutzt Override-Name",
              direct["json"]["data"]["webhook"]["name"] == "📣 News")

        single = await api.call("GET", f"/api/v1/webhooks/{hook_id}")
        check("GET /api/v1/webhooks/{id} → 200", single["status"] == 200)

        # ── 3. Bot-Profil (Nickname + Server-Avatar) ───────────────────────
        tiny_png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFc"
                    "SJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        prof = await api.call("PATCH", "/api/v1/members/me", body={
            "nick": "✨ Server-Assistent", "avatar": tiny_png})
        check("PATCH /api/v1/members/me → 200", prof["status"] == 200, str(prof["json"])[:300])
        check("Bot-Nickname gesetzt", prof["json"]["data"].get("nick") == "✨ Server-Assistent",
              str(prof["json"].get("data", {}).get("nick")))
        check("Avatar-Mutation protokolliert",
              any(m.startswith("member.edit:avatar:") for m in guild.mutations))

        empty = await api.call("PATCH", "/api/v1/members/me", body={})
        check("Leeres Profil-Update → NO_CHANGES", empty["status"] == 400
              and empty["json"]["error"]["code"] == "NO_CHANGES")

        # ── 4. Guides ──────────────────────────────────────────────────────
        guides = await api.call("GET", "/api/v1/guides")
        topics = {g["topic"] for g in guides["json"]["data"]["guides"]}
        check("Guides verzeichnen self-roles & design",
              {"self-roles", "design"} <= topics, str(topics))
        sr = await api.call("GET", "/api/v1/guides/self-roles")
        check("Self-Roles-Guide enthält Bot-Anleitungen",
              sr["status"] == 200 and "carl-bot" in json.dumps(sr["json"]["data"]).lower())
        check("Self-Roles-Guide ehrlich zu Grenzen",
              "nicht konfigurieren" in json.dumps(sr["json"], ensure_ascii=False).lower())
        design = await api.call("GET", "/api/v1/guides/design")
        check("Design-Guide zeigt Unicode-Stile",
              design["status"] == 200 and "「" in json.dumps(design["json"], ensure_ascii=False))
        missing_guide = await api.call("GET", "/api/v1/guides/gibtsnicht")
        check("Unbekannter Guide → 404", missing_guide["status"] == 404)

        # ── 5. Setup mit Mention-Auflösung + Persona + Purge ───────────────
        plan = {
            "roles": [{"key": "gamer", "name": "「🎮」 Gamer", "color": "#5865F2",
                       "permissions": ["view_channel", "send_messages"]}],
            "categories": [{
                "key": "info", "name": "「📌」 INFORMATION",
                "overwrites": [{"id": "@everyone", "deny": ["send_messages"]}],
                "channels": [{"key": "regeln", "name": "「✦」regeln", "type": "text"}],
            }],
            "messages": [
                {"channel": "regeln", "pin": True,
                 "webhook": {"name": "📜 Serverregeln"},
                 "content": " Lies <#regeln> — Mitglied: <@&gamer>!"},
            ],
        }
        result = await api.call("POST", "/api/v1/setup", body=plan)
        check("Mini-Setup mit Persona + Mention → 200", result["status"] == 200,
              str(result["json"])[:400])
        data = result["json"].get("data", {})
        check("Mini-Setup ohne Fehlschritt", data.get("steps_failed") == 0,
              json.dumps([r for r in data.get("report", []) if not r.get("ok")],
                         ensure_ascii=False)[:300])
        channel_id = data.get("keys", {}).get("regeln")
        role_id = data.get("keys", {}).get("gamer")
        contents = [m.content for m in guild.sent_messages
                    if getattr(m.channel, "id", None) == int(channel_id or 0)]
        check("<#key>-Mention wurde zu echter Kanal-Mention",
              any(f"<#{channel_id}>" in c for c in contents), str(contents))
        check("<@&key>-Rollen-Mention wurde echt",
              any(f"<@&{role_id}>" in c for c in contents), str(contents))
        check("Persona im Setup gesendet",
              any(m.startswith("webhook.send:") for m in guild.mutations[-6:]))
        check("Nachricht angepinnt", any(getattr(m, "pinned", False)
                                         for m in guild.sent_messages))

        # Purge vor dem Erneuern: alte Nachrichten weg
        purge = await api.call("POST", f"/api/v1/channels/{channel_id}/purge",
                               body={"bots_only": True, "confirm": True, "limit": 50})
        check("Purge vor Erneuerung → 200", purge["status"] == 200, str(purge["json"])[:300])
        check("Purge löscht die Persona-Nachrichten",
              purge["json"]["data"]["deleted_count"] >= 1,
              str(purge["json"]["data"]["deleted_count"]))
        no_confirm = await api.call("POST", f"/api/v1/channels/{channel_id}/purge",
                                    body={"limit": 10})
        check("Purge ohne confirm → CONFIRM_REQUIRED",
              no_confirm["status"] == 400
              and no_confirm["json"]["error"]["code"] == "CONFIRM_REQUIRED")

        # Platzhalter-Validation schlägt an
        preview = await api.call("POST", "/api/v1/setup/preview", body={
            "messages": [{"channel": "regeln", "content": "Siehe <#gibts-nicht>"}]})
        warnings = json.dumps(preview["json"].get("data", {}).get("warnings") or [],
                              ensure_ascii=False)
        check("Preview warnt vor unauflösbarem Platzhalter",
              "gibts-nicht" in warnings, warnings[:200])
    finally:
        client.swap_guild(_standard_guild())


async def t_prompt(h: Harness) -> None:
    """Die Prompt-Varianten müssen die Upgrade-Regeln transportieren."""
    print("── Prompt-Inhalte (Hygiene, Personas, Branding, Grenzen) ────")
    api = h.api("danger")

    short = await api.call("GET", "/api/v1/prompt?variant=short&format=text")
    text = short["text"]
    check("Short-Prompt passt in einen Discord-Codeblock (<1900)",
          short["status"] == 200 and len(text) < 1900, f"{len(text)} Zeichen")
    for phrase in ("purge", "webhook", "members/me", "guides/self-roles",
                   "Platzhalter", "「✦」", "overwrites", "Unicode"):
        check(f"Short-Prompt nennt '{phrase}'", phrase.lower() in text.lower())

    long = (await api.call("GET", "/api/v1/prompt?variant=long&format=text"))["text"]
    check("Long-Prompt erklärt Personen & Bot-Avatar",
          "members/me" in long and "webhook" in long.lower())
    check("Long-Prompt enthält Guides & Endpunkt-Tabelle",
          "guides" in long and "/api/v1/webhooks" in long)
    check("Long-Prompt hat Hygiene-Abschnitt", "Nachrichten-Hygiene" in long)

    system = (await api.call("GET", "/api/v1/prompt?variant=system&format=text"))["text"]
    check("System-Prompt kompakt (<2200) und mit Regeln",
          len(system) < 2200 and "members/me" in system and "purge" in system,
          f"{len(system)} Zeichen")

    caps = await api.call("GET", "/api/v1/capabilities")
    conventions = json.dumps(caps["json"]["data"].get("conventions", {}), ensure_ascii=False)
    for key in ("webhook_first", "cleanup_before_repost", "mentions_no_placeholders",
                "unicode_design", "third_party_bots"):
        check(f"capabilities.conventions nennt '{key}'", key in conventions)

    from bot.discord_bot import (
        ARENA_URL, _bridge_prompt_line, connected_view,
        admin_panel_view, admin_guild_detail_view, admin_guild_leave_confirm_view,
    )
    p_line = _bridge_prompt_line("https://relay.example.com", "adse_smoke123")
    check("Bridge-Prompt-Line: nur URL und Token",
          p_line == "URL=https://relay.example.com;TOKEN=adse_smoke123"
          and " " not in p_line)
    cv = connected_view("https://relay.example.com", "adse_smoke123")
    check("connected_view ist LayoutView (Components V2)", cv.has_components_v2())
    check("ARENA_URL zeigt auf Agent-Endpoint", ARENA_URL == "https://arena.ai/agent")

    # Adminpanel Views
    g_smoke = FakeGuild(gid=9999, name="DetailServer")
    panel_v = admin_panel_view(
        [{"guild": g_smoke, "member_count": 42, "owner_present": True}],
        page=0, query="", total_guilds=1,
    )
    check("admin_panel_view ist Components V2", panel_v.has_components_v2())

    # Regression: Discord lehnt Variation-Selector-16 (U+FE0F) in
    # SelectOption.emoji mit 400 "Invalid emoji" ab (Buttons vertragen das
    # Zeichen klaglos - die Inkonsistenz ist eine bekannte Discord-API-
    # Eigenheit). Das war der Grund dafuer, dass /adminpanel in Produktion
    # mit "Die Anwendung reagiert nicht" endete, sobald mindestens ein
    # Server ohne Bot-Owner-Mitgliedschaft (❗️-Marker) in der Liste stand -
    # exakt bestaetigt durch den Render-Log-Fehler
    # ``options.0.emoji.name: Invalid emoji``.
    def select_option_emoji_names(view: Any) -> List[str]:
        names: List[str] = []
        flat: List[Dict[str, Any]] = []

        def walk(items: List[Dict[str, Any]]) -> None:
            for item in items:
                flat.append(item)
                walk(item.get("components", []))

        walk(view.to_components())
        for part in flat:
            if part.get("type") == 3:  # Select-Menue
                for option in part.get("options", []):
                    emoji = option.get("emoji") or {}
                    name = emoji.get("name")
                    if name:
                        names.append(name)
        return names

    g_no_owner = FakeGuild(gid=8888, name="KeinOwnerServer")
    panel_no_owner = admin_panel_view(
        [{"guild": g_no_owner, "member_count": 5, "owner_present": False}],
        page=0, query="", total_guilds=1,
    )
    emoji_names = select_option_emoji_names(panel_no_owner)
    check("admin_panel_view (❗️-Server): Select-Option-Emoji ohne Variation-Selector",
          bool(emoji_names) and all("\ufe0f" not in n and "\ufe0e" not in n for n in emoji_names),
          str([n.encode("unicode_escape") for n in emoji_names]))
    detail_v = admin_guild_detail_view(
        g_smoke, owner=g_smoke.owner, owner_present=True, sessions=[],
        page=0, query="",
    )
    check("admin_guild_detail_view ist Components V2", detail_v.has_components_v2())

    # Regression: Discord akzeptiert als Section-Accessory NUR Button/Thumbnail.
    # Früher baute die Kopfzeile bei Servern OHNE Icon ein TextDisplay-Accessory
    # → Discord lehnte die Detailansicht/Owner-DMs mit 400 ab
    # („Die Anwendung reagiert nicht" im Client).
    def bad_section_accessories(view: Any) -> List[Any]:
        bad: List[Any] = []
        flat: List[Dict[str, Any]] = []

        def walk(items: List[Dict[str, Any]]) -> None:
            for item in items:
                flat.append(item)
                walk(item.get("components", []))

        walk(view.to_components())
        for part in flat:
            if part.get("type") == 9:
                accessory = part.get("accessory") or {}
                if accessory.get("type") not in (2, 11):
                    bad.append(accessory.get("type"))
        return bad

    check("admin_guild_detail_view (ohne Icon): kein ungültiges Section-Accessory",
          bad_section_accessories(detail_v) == [],
          str(bad_section_accessories(detail_v)))
    from bot.discord_bot import guild_join_notify_view, guild_remove_notify_view
    check("Join-/Leave-DM (ohne Icon): kein ungültiges Section-Accessory",
          bad_section_accessories(guild_join_notify_view(g_smoke, None)) == []
          and bad_section_accessories(guild_remove_notify_view(g_smoke, None, "grund")) == [],
          "Section-Accessory muss Button/Thumbnail sein")

    # ── Adminpanel: Server-Dokument-Button & Leave-Aufräumen ─────────────────

    def _view_parts(view: Any) -> Dict[str, List[Dict[str, Any]]]:
        flat: List[Dict[str, Any]] = []

        def walk(items: List[Dict[str, Any]]) -> None:
            for item in items:
                flat.append(item)
                walk(item.get("components", []))

        walk(view.to_components())
        return {
            "texts": [p for p in flat if p.get("type") == 10],
            "buttons": [p for p in flat if p.get("type") == 2],
        }

    def _joined_text(view: Any) -> str:
        return "\n".join(p.get("content", "") for p in _view_parts(view)["texts"])

    detail_v = admin_guild_detail_view(
        g_smoke, owner=g_smoke.owner, owner_present=True, sessions=[],
        page=0, query="",
    )
    detail_text = _joined_text(detail_v)
    check("Server-Übersicht: Server-Details (Kanäle, Rollen, Bot-Rolle)",
          "Server-Details" in detail_text and "Kanäle:" in detail_text
          and "Rollen:" in detail_text and "Bot-Rolle:" in detail_text, detail_text[:300])
    check("Server-Übersicht: keine Datenbank-Status-Zeile mehr",
          "Server-Datenbank" not in detail_text and "Turso" not in detail_text,
          detail_text[:300])
    detail_buttons = [b.get("custom_id", "") for b in _view_parts(detail_v)["buttons"]]
    check("Server-Übersicht: 'Server-Dokument erstellen'-Button vorhanden",
          any(cid.startswith("relay:admin:create_doc:") for cid in detail_buttons),
          str(detail_buttons))
    check("Server-Übersicht: 'Chatverlauf abrufen'-Button vorhanden",
          any(cid.startswith("relay:admin:history:") for cid in detail_buttons),
          str(detail_buttons))

    leave_cleanup = guild_remove_notify_view(g_smoke, None, "Bot wurde gekickt — Grund: Test", cleanup={
        "sessions_revoked": 2,
    })
    leave_cleanup_text = _joined_text(leave_cleanup)
    check("Leave-DM nennt das Aufräumen (nur Sessions)",
          "Aufräumen" in leave_cleanup_text and "2 KI-Verbindung(en) gestoppt" in leave_cleanup_text
          and "Datenbank" not in leave_cleanup_text, leave_cleanup_text[:300])
    leave_v = admin_guild_leave_confirm_view(g_smoke, page=0, query="")
    check("admin_guild_leave_confirm_view ist Components V2", leave_v.has_components_v2())


async def t_pdf(h: Harness) -> None:
    """
    Server-Dokument (PDF): Build läuft komplett durch, DejaVu-Fonts sind
    eingebettet (keine schwarzen Vierecke), und fehlende Avatare/Icons
    (offline) crashen den Build nicht.
    """
    print("── Server-Dokument (PDF): Build & Fonts ──────────────────────")
    import re as _re

    import aiohttp as _aiohttp

    from bot.web import pdf_document as pdf_mod
    from bot.web.pdf_document import _FONTS, _clean, _ensure_fonts, _prepare_text, build_server_document

    _ensure_fonts()
    check("PDF: DejaVu-Sans registriert (kein Base-14-Fallback)",
          pdf_mod._FONTS["sans"] != "Helvetica", pdf_mod._FONTS["sans"])
    check("PDF: Glyph-Abdeckung ermittelt",
          pdf_mod._GLYPH_COVERAGE is not None and len(pdf_mod._GLYPH_COVERAGE) > 3000)

    # Text-Hygiene: Emojis bleiben als ECHTE Zeichen sichtbar (Noto-Emoji-
    # Font), keine Notdef-Kandidaten im Paragraph-Markup
    prepared = _prepare_text("Willkommen 🎉 <:ping:123456789012345678> Привет 中文 ✓ <@123456789012345678>")
    check("PDF: Unicode-Emoji bleibt als Zeichen erhalten", "🎉" in prepared, prepared)
    check("PDF: Custom-Emoji-Syntax aufgelöst",
          "ping" in prepared.lower() and "<:ping:" not in prepared, prepared)
    check("PDF: Mention lesbar umschrieben", "@nutzer-123456789012345678" in prepared, prepared)
    markup = _clean("👋 👍🏽 🎉 𝗖𝗵𝗮𝘁 𝕏 𝔊 ✓ 中文 Привет")
    check("PDF: Emojis werden als Apple-Inline-Bilder gesetzt (iOS-Look)",
          "<img src=" in markup and 'valign="middle"' in markup, markup[:220])
    check("PDF: Math-Font-Markup für 𝗖𝗵𝗮𝘁 & Co.",
          "name='DejaVu-math'" in markup and "𝗖" in markup and "𝕏" in markup, markup[:220])
    check("PDF: DejaVu-fähige Zeichen bleiben ohne Font-Tag",
          "Привет" in markup and "✓" in markup and "name='DejaVu-sans'" not in markup, markup[:220])
    check("PDF: CJK läuft über Noto-SC-Font", "name='DejaVu-cjk_sc'" in markup, markup[:220])
    check("PDF: Fonts registriert (Emoji + Math)",
          _FONTS.get("emoji") == "DejaVu-emoji" and _FONTS.get("math") == "DejaVu-math",
          str(_FONTS))
    # No-Notdef-Garantie im FERTIGEN Markup (Tags entfernt): jedes gerenderte
    # Zeichen muss in mind. einem der geladenen Fonts darstellbar sein.
    tag_free = re.sub(r"</?font[^>]*>|<br/>", "", markup)
    all_covered: set = set()
    for cov in pdf_mod._FONT_COVERAGE.values():
        if cov:
            all_covered.update(cov)
    leftovers = [ch for ch in tag_free if ord(ch) not in all_covered]
    check("PDF: kein Zeichen ohne Glyphe im Ergebnis", not leftovers, repr(leftovers[:10]))
    check("PDF: Zeitangaben bleiben unzerstört",
          _prepare_text("Treffen 12:30:00 Uhr") == "Treffen 12:30:00 Uhr")
    check("PDF: unsichtbare Bidi/Zero-Width-Zeichen entfernt",
          all(c not in _prepare_text("a\u200bb\u202ec") for c in ("\u200b", "\u202e")))

    # Build gegen den simulierten Server — offline: Avatar-Downloads scheitern
    # und dürfen den Build NICHT abbrechen (Regression: Rostring in Listen-Zelle).
    g = _standard_guild()

    def _history_factory(msgs):
        def history(limit=None):
            async def gen():
                for m in msgs[: limit or None]:
                    yield m
            return gen()
        return history

    channel = g.text_channels[0]
    embed = discord.Embed(title="Regeln ✅", description="Sei nett «bitte»",
                          color=discord.Colour(0x5865F2))
    embed.add_field(name="Feld «Eins»", value="Wert eins", inline=True)
    msgs = []
    for i, author in enumerate((g.owner, g.me)):
        m = FakeMessage(700000000000000000 + i, channel, author,
                        content=f"Testnachricht {i} — 🎉 👋 Привет ✓ 𝗖𝗵𝗮𝘁",
                        embeds=[embed] if i == 0 else [])
        msgs.append(m)
    channel.history = _history_factory(msgs)

    progress_seen: List[int] = []

    async def _progress(pct: int, text: str) -> None:
        progress_seen.append(pct)

    async with _aiohttp.ClientSession() as session:
        pdf_bytes = await build_server_document(g, session, progress=_progress)

    check("PDF: Build liefert %PDF-Bytes", pdf_bytes[:5] == b"%PDF-", repr(pdf_bytes[:8]))
    check("PDF: endet mit %%EOF", pdf_bytes.rstrip().endswith(b"%%EOF"))
    check("PDF: Fortschritt läuft bis 100 %", bool(progress_seen) and progress_seen[-1] == 100,
          str(progress_seen))
    n_fonts = len(_re.findall(rb"/FontFile2", pdf_bytes))
    # Emojis laufen über Apple-Inline-Bilder — NotoEmoji wird nur noch im
    # Fallback (fehlendes Sheet) eingebettet, daher ≥ 4 statt ≥ 5.
    check("PDF: Fonts eingebettet (≥ 4: Sans, Bold, Mono, Math — Emoji via Apple-Bilder)",
          n_fonts >= 4, f"{n_fonts} FontFile2-Streams")
    check("PDF: mehrere Seiten", pdf_bytes.count(b"/Type /Page") >= 5,
          str(pdf_bytes.count(b"/Type /Page")))
    check("PDF: kompakt (Font-Subset < 400 KB)", len(pdf_bytes) < 400_000,
          f"{len(pdf_bytes) // 1024} KB")

    # ── RAM-Schutz: Sofort-Konvertierung statt Rohbytes im Cache ────────────
    # Produktion (Render Free, 512 MB): die letzten Bild-URLs sind die
    # Anhänge (mehrere MB) — alle Rohbytes im Cache gleichzeitig drückten
    # den Container in den OOM-Kill: „Einfrieren" OHNE jegliches Log,
    # Render startete neu (Startup-Log um 01:50), Fortschritts-Nachricht
    # blieb eingefroren. Jetzt: jedes Bild wird direkt nach dem Download
    # zu einem ≤1024-px-PNG konvertiert, Rohbytes sofort verworfen.
    from PIL import Image as _PILImage2
    from aiohttp import web as _webmem

    def _noise_png(w: int, h: int, seed: int) -> bytes:
        import random as _rnd
        _rnd.seed(seed)
        buf = io.BytesIO()
        im = _PILImage2.new("RGB", (w, h))
        px = im.load()
        for y in range(0, h, 7):
            for x in range(w):
                px[x, y] = (_rnd.randrange(256), _rnd.randrange(256), _rnd.randrange(256))
        im.save(buf, "PNG")
        return buf.getvalue()

    async def _serve_one(payload: bytes) -> Any:
        async def _img(_req: Any) -> Any:
            return _webmem.Response(body=payload, content_type="image/png")
        app2 = _webmem.Application()
        app2.router.add_get("/img.png", _img)
        port2 = free_port()
        return await start_web_server(app2, "127.0.0.1", port2), port2

    _saved_bytes = pdf_mod._MAX_IMAGE_BYTES
    try:
        big_bytes = _noise_png(1400, 1400, 7)

        # 1) Byte-Guard in _finalize_image: über Limit → None (kein OOM-Risiko)
        pdf_mod._MAX_IMAGE_BYTES = 200_000
        check("RAM: Bild über Byte-Limit wird übersprungen (kein OOM-Risiko)",
              await pdf_mod._finalize_image(big_bytes) is None,
              f"{len(big_bytes)} Bytes > 200 KB Limit")

        # 2) Unter dem Limit: konvertiert zu ≤1024-px-PNG (deutlich kleiner)
        pdf_mod._MAX_IMAGE_BYTES = _saved_bytes
        out = await pdf_mod._finalize_image(big_bytes)
        w_conv = -1
        if out:
            w_conv = _PILImage2.open(io.BytesIO(out)).size[0]
        check("RAM: Rohbytes werden sofort zu ≤1024-px-PNG konvertiert",
              bool(out) and w_conv <= 1024,
              f"in={len(big_bytes)} out={len(out) if out else None} width={w_conv}")

        # 3) Download-Cache hält NUR konvertierte PNGs, keine Rohbytes
        runner2, p2 = await _serve_one(big_bytes)
        try:
            async with _aiohttp.ClientSession() as s3:
                cache3 = await pdf_mod._download_images(s3, [f"http://127.0.0.1:{p2}/img.png"])
                got = cache3.get(f"http://127.0.0.1:{p2}/img.png")
                ok3 = False
                if got:
                    ok3 = _PILImage2.open(io.BytesIO(got)).size[0] <= 1024
                check("RAM: Download-Cache hält konvertierte PNGs, keine Rohbytes",
                      ok3, f"raw={len(big_bytes)} cached={len(got) if got else None}")
        finally:
            await runner2.cleanup()
    finally:
        pdf_mod._MAX_IMAGE_BYTES = _saved_bytes

    # Apple-Sheet-RAM: nach dem Build freigegeben, Emoji-Crops bleiben nutzbar
    pdf_mod._apple_png_cache.clear()
    # Auch den Disk-Cache leeren (die PNGs bleiben sonst über Teile hinweg
    # liegen — Feature) — ohne das würde das Sheet gar nicht erst öffnen:
    if pdf_mod._apple_tmpdir:
        for _f in os.listdir(pdf_mod._apple_tmpdir):
            if _f.endswith(".png"):
                os.remove(os.path.join(pdf_mod._apple_tmpdir, _f))
    _ = pdf_mod._apple_emoji_seq_img("🎉")  # zwingend öffnen (lazy load)
    sheet_was = pdf_mod._apple_sheet_img
    pdf_mod._close_apple_sheet()
    sheet_after_close = pdf_mod._apple_sheet_img
    crop_after_close = pdf_mod._apple_emoji_seq_img("🎉")  # warmer Cache → kein Reopen
    check("RAM: Apple-Sheet freigegeben, gecachte Crops bleiben nutzbar",
          sheet_was is not None and sheet_after_close is None
          and crop_after_close is not None and pdf_mod._apple_sheet_img is None,
          f"was={sheet_was is not None} crop={crop_after_close is not None}")

    # ── Regression: PDF-Aufbau läuft im Worker-Thread mit Budget ────────────
    # Produktion: der gesamte Aufbau (Flowables + PIL + reportlab) lief als
    # EIN synchroner Block AUF DEM EVENT-LOOP — blockierte er (große Bilder),
    # fror ALLES ein: keine Fortschritts-Edits, keine Logs, keine Timer.
    # Jetzt: Worker-Thread + Ticker + Budget mit sichtbarer Fehlermeldung.
    _saved_budget = pdf_mod._ASSEMBLY_TIMEOUT_S
    _saved_render = pdf_mod._render_pdf
    try:
        import time as _time_mod
        pdf_mod._ASSEMBLY_TIMEOUT_S = 0.05

        def _slow_render(*a: Any, **k: Any) -> bytes:
            _time_mod.sleep(0.3)  # deterministisch über dem Budget
            return _saved_render(*a, **k)

        pdf_mod._render_pdf = _slow_render
        raised = None
        g_budget = _standard_guild()
        # Leere Historie-Fabrik: sonst greift die echte discord.py-History
        # (HistoryIterator → logs_from), die das Fake-HTTP nicht kennt.
        g_budget.text_channels[0].history = _history_factory([])
        async with _aiohttp.ClientSession() as s_budget:
            try:
                await build_server_document(g_budget, s_budget, progress=_progress)
            except RuntimeError as exc:
                raised = str(exc)
        check("Aufbau-Budget: Timeout → sichtbarer RuntimeError (kein ewiges Einfreeze)",
              raised is not None and "zu lange" in raised, str(raised)[:140])
    finally:
        pdf_mod._ASSEMBLY_TIMEOUT_S = _saved_budget
        pdf_mod._render_pdf = _saved_render

    # Pixel-Guard: übergroße Bilder werden VOR dem Dekodieren übersprungen.
    from PIL import Image as _PILTest
    _saved_mp = pdf_mod._MAX_IMAGE_PIXELS
    try:
        pdf_mod._MAX_IMAGE_PIXELS = 100
        _buf = io.BytesIO()
        _PILTest.new("RGB", (20, 20), (255, 0, 0)).save(_buf, "PNG")
        _im = _PILTest.open(_buf)
        check("Pixel-Guard: übergroße Bilder werden ohne Dekodieren übersprungen",
              pdf_mod._pil_to_png_bytes(_im, max_px_w=10, circle=False) is None)
    finally:
        pdf_mod._MAX_IMAGE_PIXELS = _saved_mp

    # ── Regression: Die Bild-Download-Phase darf sich NIEMALS ewig aufhängen ──
    # Produktion (Render): ein Server blieb bei „Bilder werden geladen …" für
    # immer stehen. Fix: Hart-Timeout pro Request + globales Budget für die
    # ganze Phase + Fortschritts-Callback + Log-Ausgabe.
    import time as _time
    from aiohttp import web as _web

    _ok_png_buf = io.BytesIO()
    _PILTest.new("RGB", (24, 24), (30, 40, 50)).save(_ok_png_buf, "PNG")
    ok_png = _ok_png_buf.getvalue()

    async def _stall_handler(_request: Any) -> Any:
        await asyncio.sleep(300)  # simuliert einen CDN-Hänger
        return _web.Response(body=b"x")

    async def _ok_handler(_request: Any) -> Any:
        return _web.Response(body=ok_png, content_type="image/png")

    stall_app = _web.Application()
    stall_app.router.add_get("/stall", _stall_handler)
    stall_app.router.add_get("/ok", _ok_handler)
    stall_port = free_port()
    stall_runner = await start_web_server(stall_app, "127.0.0.1", stall_port)
    stall_base = f"http://127.0.0.1:{stall_port}"
    _saved_dl = (pdf_mod._IMAGE_TIMEOUT_S, pdf_mod._IMAGES_BUDGET_S,
                 pdf_mod._IMAGES_PROGRESS_EVERY)
    try:
        pdf_mod._IMAGE_TIMEOUT_S = 1.0
        pdf_mod._IMAGES_BUDGET_S = 60.0
        pdf_mod._IMAGES_PROGRESS_EVERY = 1
        async with _aiohttp.ClientSession() as s2:
            t0 = _time.monotonic()
            cache1 = await pdf_mod._download_images(s2, [f"{stall_base}/stall"])
            elapsed1 = _time.monotonic() - t0
            check("Hängender Bild-Request bricht per Hart-Timeout ab (kein ewiges Hängen)",
                  cache1 == {f"{stall_base}/stall": None} and elapsed1 < 15,
                  f"{elapsed1:.1f}s, cache={cache1}")

            t0 = _time.monotonic()
            cache_ok = await pdf_mod._download_images(s2, [f"{stall_base}/ok"])
            elapsed_ok = _time.monotonic() - t0
            got_ok = cache_ok.get(f"{stall_base}/ok")
            ok_decodes = False
            if got_ok:
                _oi = _PILTest.open(io.BytesIO(got_ok))
                ok_decodes = _oi.size == (24, 24)
            check("Funktionierender Bild-Request liefert konvertiertes PNG",
                  ok_decodes and elapsed_ok < 15,
                  f"{elapsed_ok:.1f}s, cached={len(got_ok) if got_ok else None}")

            # Globales Budget: selbst wenn ALLE Requests hängen (und der
            # per-Request-Timeout sie NICHT erwischt), kehrt die Phase zurück.
            pdf_mod._IMAGE_TIMEOUT_S = 30.0
            pdf_mod._IMAGES_BUDGET_S = 2.0
            hook_calls: List[Any] = []

            async def _hook(done_n: int, total_n: int) -> None:
                hook_calls.append((done_n, total_n))

            stall_url = f"{stall_base}/stall?budget=1"
            ok_url = f"{stall_base}/ok?budget=2"
            t0 = _time.monotonic()
            cache_budget = await pdf_mod._download_images(
                s2, [stall_url, ok_url], progress_hook=_hook)
            elapsed_budget = _time.monotonic() - t0
            check("Download-Budget bricht die GESAMTE Phase ab und kehrt zurück",
                  isinstance(cache_budget, dict) and elapsed_budget < 12,
                  f"{elapsed_budget:.1f}s")
            _bi = _PILTest.open(io.BytesIO(cache_budget[ok_url])) \
                if cache_budget.get(ok_url) else None
            check("Bereits geladene Bilder überleben den Budget-Abbruch",
                  _bi is not None and _bi.size == (24, 24),
                  str(list(cache_budget)))
            check("Fortschritts-Callback wird aufgerufen",
                  len(hook_calls) >= 1 and hook_calls[0][1] == 2, str(hook_calls[:3]))
    finally:
        (pdf_mod._IMAGE_TIMEOUT_S, pdf_mod._IMAGES_BUDGET_S,
         pdf_mod._IMAGES_PROGRESS_EVERY) = _saved_dl
        await stall_runner.cleanup()


async def t_session_limit(h: Harness) -> None:
    print("── Sitzungs-Limit pro Server ─────────────────────────────────")
    from bot.sessions import SessionStore

    limited = SessionStore(path=None, persist=False, max_per_guild=3)
    tokens: List[str] = []
    for _i in range(4):
        _session, token = await limited.create(
            guild_id=GUILD_ID, guild_name="Smoke-Test Server", created_by=USER_ID,
            created_by_name="Tester", mode="read", ttl_hours=1,
        )
        tokens.append(token)

    active = limited.active_for_guild(GUILD_ID)
    check("Limit 3 pro Server wird eingehalten", len(active) == 3, f"{len(active)} aktiv")
    check("Ältestes Token wurde automatisch widerrufen", not _token_valid(limited, tokens[0]))
    check("Neue Tokens bleiben gültig",
          all(_token_valid(limited, t) for t in tokens[1:]),
          str([_token_valid(limited, t) for t in tokens]))

    bad_mode, _tok = await limited.create(
        guild_id=GUILD_ID, guild_name="Smoke-Test Server", created_by=USER_ID,
        created_by_name="Tester", mode="gibts_nicht", ttl_hours=1,
    )
    check("Unbekannter Modus fällt auf 'Lesen + Schreiben' zurück",
          bad_mode.mode == "read_write", bad_mode.mode)


async def t_docdelivery(h: Harness) -> None:
    """
    Server-Dokument-Zustellung: Empfänger ist der BOT-Owner (Application-Owner),
    nicht der Server-Owner. Vor jeder neuen Zustellung werden frühere
    „server-dokument-*.pdf"-DMs gelöscht (letzte Zustellung + neuer
    Empfänger) — es gibt immer nur EIN aktuelles Dokument. Kein Start-Scan
    mehr (das einmalige Legacy-Aufräumen ist abgeschlossen).
    """
    print("── Server-Dokument: Zustellung (Bot-Owner) & Aufräumen ───────")
    import tempfile
    from types import SimpleNamespace

    from bot.discord_bot import RelayClient

    bot_owner = SimpleNamespace(id=424242424242424242, display_name="Bot-Betreiber")
    guild_owner = SimpleNamespace(id=1015212381619626014, display_name="Server-Owner")

    class _User:
        def __init__(self, uid: int) -> None:
            self.id = uid
            self.display_name = f"User-{uid}"
            self.dm_channel = None

        async def create_dm(self):
            if self.dm_channel is None:
                self.dm_channel = _DM()
            return self.dm_channel

    class _DM:
        def __init__(self) -> None:
            self.messages: List[Any] = []

        def history(self, limit=None):
            async def gen():
                for m in self.messages[: limit or None]:
                    yield m
            return gen()

    class _Att:
        def __init__(self, filename: str) -> None:
            self.filename = filename

    class _Msg:
        def __init__(self, mid: int, filename: str) -> None:
            self.id = mid
            self.attachments = [] if filename is None else [_Att(filename)]
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

    # RelayClient ohne __init__ (keine Gateway-Verbindung nötig) — nur die
    # Dokument-Helper werden getestet.
    client = RelayClient.__new__(RelayClient)
    users = {424242424242424242: _User(424242424242424242),
             1015212381619626014: _User(1015212381619626014),
             888888888888888888: _User(888888888888888888)}
    client.get_user = lambda uid: users.get(uid)
    client.fetch_user = users.get  # type: ignore[method-assign]

    tmpdir = tempfile.mkdtemp(prefix="docdelivery-")
    state_path = os.path.join(tmpdir, "delivery.json")
    client._doc_delivery_state_path = lambda: state_path  # type: ignore[method-assign]

    async def _app_info():
        return SimpleNamespace(owner=bot_owner)
    client.application_info = _app_info  # type: ignore[method-assign]

    guild = SimpleNamespace(owner=guild_owner, owner_id=guild_owner.id)

    recipient, kind = await client._resolve_document_recipient(guild)
    check("Dokument-Empfänger ist der Bot-Owner (nicht Server-Owner)",
          recipient is bot_owner and kind == "bot_owner", str(kind))

    async def _app_info_fail():
        raise discord.HTTPException(
            SimpleNamespace(status=500, reason="Internal Server Error"),
            "application nicht lesbar",
        )
    client.application_info = _app_info_fail  # type: ignore[method-assign]
    recipient, kind = await client._resolve_document_recipient(guild)
    check("Fallback: ohne Application-Info geht das Dokument an den Server-Owner",
          recipient is guild_owner and kind == "guild_owner_fallback", str(kind))
    client.application_info = _app_info  # type: ignore[method-assign]

    check("Erkennung: server-dokument-*.pdf wird erkannt",
          RelayClient._is_document_message(SimpleNamespace(attachments=[_Att("server-dokument-123.pdf")])))
    check("Erkennung: fremde Anhänge werden ignoriert",
          not RelayClient._is_document_message(SimpleNamespace(attachments=[_Att("anderes.pdf")])),
          "anderes.pdf")

    # Prä-Zustellungs-Aufräumen: neue Zustellung an den Bot-Owner → Dokument-DM
    # beim Bot-Owner UND beim letzten Empfänger werden gelöscht, sonst nichts.
    await users[424242424242424242].create_dm()
    await users[888888888888888888].create_dm()
    dm_owner = users[424242424242424242].dm_channel
    dm_last = users[888888888888888888].dm_channel
    assert dm_owner is not None and dm_last is not None
    m_owner_doc = _Msg(9, "server-dokument-555.pdf")
    m_owner_keep = _Msg(10, "wichtiges-protokoll.pdf")
    m_last_doc = _Msg(77, "server-dokument-999.pdf")
    dm_owner.messages = [m_owner_doc, m_owner_keep]
    dm_last.messages = [m_last_doc]
    current = {"last_delivery": {"recipient_id": 888888888888888888, "message_id": 77}}
    json.dump(current, open(state_path, "w", encoding="utf-8"))

    deleted = await client._cleanup_previous_documents(424242424242424242)
    check("Prä-Zustellung: alte Dokument-DMs beim Bot-Owner und letzten Empfänger gelöscht",
          deleted == 2 and m_owner_doc.deleted and m_last_doc.deleted, f"{deleted} gelöscht")
    check("Fremde DMs bleiben unberührt", not m_owner_keep.deleted)
    state = json.load(open(state_path, encoding="utf-8"))
    check("Status-File: last_delivery entfernt, Empfänger als bereinigt markiert",
          "last_delivery" not in state
          and set(state.get("cleaned_recipient_ids") or []) == {424242424242424242, 888888888888888888},
          str(state))

    # Gelöschte Nachrichten verschwinden real aus dem DM-Verlauf:
    dm_owner.messages = [m for m in dm_owner.messages if not getattr(m, "deleted", False)]
    dm_last.messages = [m for m in dm_last.messages if not getattr(m, "deleted", False)]
    deleted_again = await client._cleanup_previous_documents(424242424242424242)
    check("Erneutes Aufräumen ohne alte Zustellung: keine Löschungen mehr",
          deleted_again == 0, str(deleted_again))


async def t_history_delivery(h: Harness) -> None:
    """
    Chatverlauf-Abruf aus dem Adminpanel: Button → Kanal-ID-Modal → PDF —
    AUSSCHLIESSLICH per DM an den Bot-Owner (Application-Owner), niemals an
    einen Server oder den Server-Owner. Alte „chatverlauf-*.pdf“-DMs werden
    vor der neuen Zustellung gelöscht; Kanäle werden serverübergreifend
    gefunden (der Bot muss im Server des Kanals sein).
    """
    print("── Chatverlauf-Abruf: Bot-Owner-Zustellung & Aufräumen ───────")
    import json as _json
    import os as _os
    import tempfile as _tempfile
    from types import SimpleNamespace

    from bot.discord_bot import RelayClient

    bot_owner = SimpleNamespace(id=424242424242424242, display_name="Bot-Betreiber")
    bot_owner_user = None  # wird unten als _HUser angelegt

    check("Erkennung: chatverlauf-*.pdf wird erkannt",
          RelayClient._is_history_document_message(SimpleNamespace(
              attachments=[SimpleNamespace(filename="chatverlauf-123-456.pdf")])))
    check("Erkennung: Server-Dokument ist KEIN Chatverlauf-PDF",
          not RelayClient._is_history_document_message(SimpleNamespace(
              attachments=[SimpleNamespace(filename="server-dokument-123.pdf")])),
          "server-dokument-123.pdf")

    class _HDM:
        def __init__(self) -> None:
            self.messages: List[Any] = []

        def history(self, limit=None):
            async def gen():
                for m in self.messages[: limit or None]:
                    yield m
            return gen()

    class _HUser:
        def __init__(self, uid: int, name: str) -> None:
            self.id = uid
            self.display_name = name
            self.dm_channel: Optional[_HDM] = None
            self.sent: List[Dict[str, Any]] = []

        async def create_dm(self) -> _HDM:
            if self.dm_channel is None:
                self.dm_channel = _HDM()
            return self.dm_channel

        async def send(self, **kwargs: Any) -> Any:
            self.sent.append(kwargs)
            return SimpleNamespace(id=4242)

    class _ProgMsg:
        def __init__(self) -> None:
            self.content = ""
            self.edits: List[Optional[str]] = []

        async def edit(self, content: Optional[str] = None, **kw: Any) -> None:
            self.content = content or self.content
            self.edits.append(content)

    class _Followup:
        def __init__(self) -> None:
            self.msg = _ProgMsg()
            self.calls: List[Any] = []

        async def send(self, content: Optional[str] = None, wait: bool = True,
                       ephemeral: bool = False, **kw: Any) -> Any:
            self.calls.append((content, ephemeral))
            if wait:
                return self.msg
            return SimpleNamespace(id=1)

    users = {bot_owner.id: _HUser(bot_owner.id, "Bot-Betreiber")}
    bot_owner_user = users[bot_owner.id]
    client = RelayClient.__new__(RelayClient)
    client.config = SimpleNamespace(bot_owner_id=bot_owner.id)
    client.get_user = lambda uid: users.get(uid)  # type: ignore[method-assign]
    client.fetch_user = users.get  # type: ignore[method-assign]
    client.state = SimpleNamespace(http_session=None)

    tmpdir = _tempfile.mkdtemp(prefix="histdelivery-")
    state_path = _os.path.join(tmpdir, "delivery.json")
    client._doc_delivery_state_path = lambda: state_path  # type: ignore[method-assign]

    async def _app_info() -> Any:
        return SimpleNamespace(owner=bot_owner_user)

    client.application_info = _app_info  # type: ignore[method-assign]

    # Kanal serverübergreifend finden (der Bot muss im Server sein)
    found = SimpleNamespace(id=123, name="ziel")
    g_empty = SimpleNamespace(
        id=1, name="A",
        get_channel=lambda cid: None, get_thread=lambda cid: None,
    )
    g_hit = SimpleNamespace(
        id=2, name="B",
        get_channel=lambda cid: found if cid == 123 else None,
        get_thread=lambda cid: None,
    )
    res = await client._resolve_history_channel(123, None, guilds=[g_empty, g_hit])
    check("Kanal serverübergreifend gefunden", res is found, str(res))
    res_none = await client._resolve_history_channel(999, None, guilds=[g_empty, g_hit])
    check("Kanal ohne Bot-Mitgliedschaft → None", res_none is None, str(res_none))

    # E2E: kompletter Ablauf (leerer Kanal → PDF → DM an Bot-Owner)
    fguild = SimpleNamespace(
        id=555, name="HistServer", roles=[], channels=[], members=[],
        member_count=0, emojis=[], stickers=[], icon=None, owner=None, owner_id=1,
    )

    async def _gen(limit=None):
        if False:
            yield  # leerer Verlauf

    fchannel = SimpleNamespace(
        id=777, name="verlauf", guild=fguild, type=None,
        history=lambda limit=None: _gen(limit),
    )
    async def _aret(msg: Any) -> Any:
        return msg

    prog = _ProgMsg()
    interaction = SimpleNamespace(
        original_response=lambda: _aret(prog), followup=_Followup())
    await client._handle_admin_history(interaction, fchannel)

    owner_user = users[bot_owner.id]
    check("Chatverlauf-PDF ging an den Bot-Owner", len(owner_user.sent) == 1,
          str(len(owner_user.sent)))
    if owner_user.sent:
        fname = getattr(owner_user.sent[0].get("file"), "filename", "")
        check("Dateiname chatverlauf-<server>-<kanal>.pdf",
              fname.startswith("chatverlauf-555-777") and fname.endswith(".pdf"), fname)
        check("DM-Text: vertraulich, nur Bot-Owner",
              "nur für dich" in (owner_user.sent[0].get("content") or ""),
              (owner_user.sent[0].get("content") or "")[:120])
    state = _json.load(open(state_path, encoding="utf-8"))
    hist_state = state.get("last_history_delivery") or {}
    check("Status-File: last_history_delivery gespeichert",
          hist_state.get("channel_id") == 777 and hist_state.get("recipient_id") == bot_owner.id,
          str(state))
    check("Fortschritt endet mit ✅", bool(prog.edits)
          and "✅" in (prog.edits[-1] or ""), str(prog.edits[-1:]))

    # Fehlschlag: Application-Owner unlesbar → NICHT versendet (nie Server-Owner)
    async def _app_info_fail() -> Any:
        raise discord.HTTPException(
            SimpleNamespace(status=500, reason="Internal Server Error"), "kaputt")

    client.application_info = _app_info_fail  # type: ignore[method-assign]
    owner_user.sent.clear()
    prog_fail = _ProgMsg()
    interaction_fail = SimpleNamespace(
        original_response=lambda: _aret(prog_fail), followup=_Followup())
    await client._handle_admin_history(interaction_fail, fchannel)
    check("Ohne Application-Info: kein Versand (nie Server/Server-Owner)",
          len(owner_user.sent) == 0 and bool(prog_fail.edits)
          and "nicht" in (prog_fail.edits[-1] or ""), str(prog_fail.edits[-1:]))
    client.application_info = _app_info  # type: ignore[method-assign]

    # Prä-Zustellungs-Aufräumen: alte chatverlauf-*.pdf-DMs werden gelöscht
    class _HMsg:
        def __init__(self, mid: int, filename: str) -> None:
            self.id = mid
            self.attachments = [SimpleNamespace(filename=filename)]
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

    await users[bot_owner.id].create_dm()
    dm = users[bot_owner.id].dm_channel
    assert dm is not None
    m_hist = _HMsg(5, "chatverlauf-111-222.pdf")
    m_other = _HMsg(6, "wichtiges-protokoll.pdf")
    dm.messages = [m_hist, m_other]
    _json.dump({"last_history_delivery": {"recipient_id": bot_owner.id, "message_id": 5}},
               open(state_path, "w", encoding="utf-8"))
    deleted = await client._cleanup_previous_history_documents(bot_owner.id)
    check("Prä-Zustellung: alte Chatverlauf-DM gelöscht, fremde bleibt",
          deleted == 1 and m_hist.deleted and not m_other.deleted, f"{deleted} gelöscht")

    # ── E2E: Der ECHTE Modal-Flow (Button → Formular → Absenden → on_submit) ──
    # Regression: In Produktion zeigte Discord nach dem Absenden des Modals
    # „Etwas ist schief gelaufen" — der früher verwendete defer (Typ 5 mit
    # Ephemeral-Flags) ist bei Modal-Submits die riskante Antwort; jetzt
    # antwortet das Modal sofort per Typ 4 (send_message), und ALLE Ausgänge
    # (erfolgreich, DM fehlgeschlagen, Fehler) landen sichtbar in der Nachricht.
    print("── Chatverlauf-Modal: kompletter on_submit-Flow ──────────────")
    from bot.discord_bot import AdminHistoryModal

    g2 = _standard_guild()
    mchannel = g2.text_channels[0]

    def _hist_factory2(messages: List[Any]):
        # Wie der echte discord.py-HistoryIterator: neueste zuerst, mit
        # oldest_first=True chronologisch (so konsumiert ihn der Streamer).
        def history(limit=None, oldest_first=False):
            ordered = messages if oldest_first else list(reversed(messages))

            async def gen():
                for m in ordered[: limit or None]:
                    yield m
            return gen()
        return history

    gone_author = SimpleNamespace(
        id=999000000000000001, name="geloescht", display_name="Gelöscht 123",
        bot=False, display_avatar=SimpleNamespace(url="http://127.0.0.1:1/av.png"),
    )
    modal_msgs = [
        FakeMessage(910000000000000001, mchannel, g2.owner,
                    content=f"Hallo <@{OWNER_ID}> 🎉 — Verlauf-Test"),
        FakeMessage(910000000000000002, mchannel, g2.me,
                    content="Bot-Antwort mit 👍🏽 und <:tada:455601000000000018>"),
        FakeMessage(910000000000000003, mchannel, gone_author,
                    content="Nachricht eines Verlassenen"),
    ]
    mchannel.history = _hist_factory2(modal_msgs)

    owner2 = _HUser(555555555555555555, "Bot-Betreiber 2")
    users2 = {owner2.id: owner2}
    client2 = RelayClient.__new__(RelayClient)
    client2.config = SimpleNamespace(bot_owner_id=owner2.id)
    client2.get_user = lambda uid: users2.get(uid)  # type: ignore[method-assign]
    client2.fetch_user = users2.get  # type: ignore[method-assign]
    client2.state = SimpleNamespace(http_session=None)
    state_path2 = _os.path.join(tmpdir, "delivery2.json")
    client2._doc_delivery_state_path = lambda: state_path2  # type: ignore[method-assign]

    async def _app_info2() -> Any:
        return SimpleNamespace(owner=owner2)

    client2.application_info = _app_info2  # type: ignore[method-assign]

    class _Resp:
        def __init__(self) -> None:
            self.sent: List[Any] = []

        async def send_message(self, content: Optional[str] = None,
                               ephemeral: bool = False, **kw: Any) -> None:
            self.sent.append((content, ephemeral))

        def is_done(self) -> bool:
            return bool(self.sent)

    async def run_modal(client_x: Any, guild_x: Any, value: str,
                        user: Any, prog_x: _ProgMsg) -> _Resp:
        modal = AdminHistoryModal(client_x, guild_x)
        modal.channel_id._value = value  # noqa: SLF001 — Test-Injektion
        resp = _Resp()
        inter = SimpleNamespace(
            user=user, response=resp,
            original_response=lambda: _aret(prog_x), followup=_Followup())
        await modal.on_submit(inter)
        return resp

    resp_ok = await run_modal(client2, g2, str(mchannel.id), owner2, _ProgMsg())
    check("Modal antwortet SOFORT, NICHT-ephemeral (kein „Nur du“-Banner, kein Verwerfen)",
          len(resp_ok.sent) == 1 and resp_ok.sent[0][1] is False
          and "wird erstellt" in (resp_ok.sent[0][0] or ""), str(resp_ok.sent))
    check("Modal-E2E: PDF per DM an den Bot-Owner",
          len(owner2.sent) == 1
          and getattr(owner2.sent[0].get("file"), "filename", "").startswith(
              f"chatverlauf-{g2.id}-"), str([x.get("file") for x in owner2.sent]))
    state2 = _json.load(open(state_path2, encoding="utf-8"))
    check("Modal-E2E: Status-File gespeichert",
          (state2.get("last_history_delivery") or {}).get("channel_id") == mchannel.id,
          str(state2))

    # Fehlerfälle: die Interaktion wird IMMER sofort beantwortet (Typ 4),
    # die konkrete Fehlermeldung landet sichtbar als Edit in derselben
    # Nachricht — kein stiller Pfad, kein generischer Discord-Fehler.
    prog_missing = _ProgMsg()
    resp_missing = await run_modal(client2, g2, "888888888888888888", owner2, prog_missing)
    check("Modal: sofortige Antwort + Kanal-nicht-gefunden als sichtbares Edit",
          len(resp_missing.sent) == 1 and "wird erstellt" in (resp_missing.sent[0][0] or "")
          and bool(prog_missing.edits) and "Kanal nicht gefunden" in (prog_missing.edits[-1] or ""),
          f"resp={resp_missing.sent} edits={prog_missing.edits[-1:]}")

    prog_bad = _ProgMsg()
    resp_bad = await run_modal(client2, g2, "keine-zahl", owner2, prog_bad)
    check("Modal: ungültige Eingabe → sichtbares Edit",
          len(resp_bad.sent) == 1 and bool(prog_bad.edits)
          and "keine Kanal-ID" in (prog_bad.edits[-1] or ""),
          f"resp={resp_bad.sent} edits={prog_bad.edits[-1:]}")

    prog_not_owner = _ProgMsg()
    other_user = SimpleNamespace(id=111222333444555666, display_name="Angreifer")
    resp_not_owner = await run_modal(client2, g2, str(mchannel.id), other_user, prog_not_owner)
    check("Modal: Nicht-Owner abgewehrt (sofortige Antwort + sichtbares Edit)",
          len(resp_not_owner.sent) == 1 and bool(prog_not_owner.edits)
          and "Bot-Owner" in (prog_not_owner.edits[-1] or ""),
          f"resp={resp_not_owner.sent} edits={prog_not_owner.edits[-1:]}")

    # ── RAM-Batching: große Verläufe werden in Teil-PDFs verteilt ───────────
    # „Fertige Sachen senden, Daten löschen, Platz für neue“ — jeder Teil
    # wird SOFORT versendet und verworfen, egal wie groß der Verlauf ist.
    print("── Chatverlauf-Batching & RAM-Guard ─────────────────────────")
    import aiohttp as _aiohttp
    import bot.web.pdf_document as _pdfmod
    import bot.discord_bot as db_mod

    _pdfmod._APPLE_USE_CDN = False  # Tests ohne Netz — Sheet-Pfad deterministisch

    _saved_batch = _pdfmod.HISTORY_BATCH_MESSAGES
    _saved_rss = db_mod.current_rss_mb
    try:
        _pdfmod.HISTORY_BATCH_MESSAGES = 1  # 3 Nachrichten → 3 Teile
        collected: List[Any] = []

        async def _collect_part(pdf_bytes: bytes, idx: int, total: int) -> None:
            collected.append((pdf_bytes, idx, total))

        async def progress_b(pct: int, text: str) -> None:
            pass

        async with _aiohttp.ClientSession() as sb:
            meta = await _pdfmod.build_channel_history_pdf(
                mchannel, sb, progress=progress_b, max_messages=None,
                send_part=_collect_part,
            )
        check("Batching: 3 Nachrichten bei Batchgröße 1 → 3 Teil-PDFs",
              len(collected) == 3 and (meta or {}).get("parts") == 3
              and (meta or {}).get("messages") == 3, str(meta))
        check("Batching: Rückgabe enthält nur Teil-GRÖSSEN (Bytes sofort verworfen)",
              isinstance((meta or {}).get("bytes"), list)
              and all(isinstance(n, int) for n in (meta or {}).get("bytes") or [])
              and (meta or {}).get("bytes") == [len(b) for b, _, _ in collected],
              str((meta or {}).get("bytes")))
        if collected:
            import pymupdf as _pm
            t1 = _pm.open(stream=collected[0][0], filetype="pdf")
            part1_text = " ".join("".join(p.get_text() for p in t1).split())
            t1.close()
            check("Batching: Teil 1 enthält die ÄLTESTE Nachricht (Hallo …)",
                  "Hallo" in part1_text, part1_text[:120])
            check("Batching: Teil-Kopf nennt 'Teil 1 von 3' (Zähl-Durchlauf kennt total)",
                  "Teil 1 von 3" in part1_text, part1_text[:200])

        check("Teil-Bau lief im isolierten KINDPROZESS (fork) — RAM-Isolation aktiv",
              _pdfmod._FORK_BUILDS["ok"] >= 3, str(_pdfmod._FORK_BUILDS))

        # RAM-Guard: kritischer RAM → Abbruch VOR dem Teil (kein verlorener
        # Aufbau), Exception mit klarer Meldung — BEVOR der Container stirbt.
        collected.clear()

        async def _critical_guard() -> None:
            raise MemoryError("RAM kritisch (500 MB) — Aufbau abgebrochen.")

        raised_hist = None
        async with _aiohttp.ClientSession() as sb:
            try:
                await _pdfmod.build_channel_history_pdf(
                    mchannel, sb, progress=progress_b, max_messages=None,
                    send_part=_collect_part, ram_guard=_critical_guard,
                )
            except MemoryError as exc:
                raised_hist = str(exc)
        check("RAM-Guard: Abbruch VOR dem Teil mit klarer Meldung (vor OOM)",
              len(collected) == 0 and raised_hist is not None
              and "RAM kritisch" in raised_hist,
              f"sent={len(collected)} exc={raised_hist}")
        check("Teil-DM minimal: 'Teil 3/85:' ohne Boilerplate (Owner-Wunsch)",
              db_mod._part_message_content(3, 85) == "Teil 3/85:"
              and "Vertraulich" not in db_mod._part_message_content(3, 85)
              and "Chatverlauf" not in db_mod._part_message_content(3, 85),
              repr(db_mod._part_message_content(3, 85)))
    finally:
        _pdfmod.HISTORY_BATCH_MESSAGES = _saved_batch
        db_mod.current_rss_mb = _saved_rss

    # ── Startup-DM & RAM-Monitor ────────────────────────────────────────────
    print("── Startup-DM & RAM-Monitor ─────────────────────────────────")
    from bot.ram import current_rss_mb as _rss, relieve_ram as _relieve
    from bot.discord_bot import startup_notify_view

    mb = _rss()
    check("RAM-Messung: VmRSS lesbar (> 0 MB)", mb > 0, f"{mb} MB")
    _relieve()  # darf niemals werfen
    check("RAM-Aufräumen (relieve_ram) läuft ohne Fehler", True, "")

    from bot.ram import container_memory_mb as _container_mem
    _cu, _cl = _container_mem()
    check("Container-RAM-Messung: cgroup-Werte plausibel (-1 = nicht lesbar)",
          (_cu > 0 and _cl > 0) or (_cu < 0 and _cl < 0), f"used={_cu} limit={_cl}")

    _saved_cont = db_mod.container_memory_mb
    _saved_rss2 = db_mod.current_rss_mb
    db_mod.container_memory_mb = lambda: (128.0, 512.0)
    check("Auslastung: 128/512 MB → 25 %", db_mod._auslastung_pct() == 25,
          str(db_mod._auslastung_pct()))
    db_mod.container_memory_mb = lambda: (-1.0, -1.0)
    db_mod.current_rss_mb = lambda: 256.0
    check("Auslastung ohne cgroup: Prozess-RSS/512 → 50 %",
          db_mod._auslastung_pct() == 50, str(db_mod._auslastung_pct()))
    db_mod.container_memory_mb = _saved_cont
    db_mod.current_rss_mb = _saved_rss2

    start_view = startup_notify_view(
        ram_mb=mb, version="9.9", ready_at=now())
    check("Startup-DM-View ist Components V2", start_view.has_components_v2())

    owner2.sent.clear()
    client2._startup_dm_sent = False
    client2.ready_at = now()  # RelayClient.__new__ setzt keine Attribute
    await client2._startup_dm_once()
    await client2._startup_dm_once()  # zweiter on_ready-Durchlauf (Resync)
    check("Startup-DM: genau EINMAL pro Prozess (on_ready mehrfach ok)",
          len(owner2.sent) == 1, f"{len(owner2.sent)} DMs")
    if owner2.sent:
        kw = owner2.sent[0]
        sent_view = kw.get("view")
        check("Startup-DM: als LayoutView (Container) gesendet",
              sent_view is not None and sent_view.has_components_v2(),
              str(type(sent_view)))

    _saved_ausl = db_mod._auslastung_pct
    db_mod._auslastung_pct = lambda: 50
    class _PresStub:
        config = SimpleNamespace(command_name="connect")
        guilds = [1, 2, 3]
    check("Presence-Text im Owner-Format (… Servers | Auslastung …%)",
          db_mod.RelayClient._presence_text(_PresStub())
          == "/connect | 3 Servers | Auslastung 50%",
          db_mod.RelayClient._presence_text(_PresStub()))
    db_mod._auslastung_pct = _saved_ausl
    from bot.config import Config as _Cfg
    check("Presence-Status-Default: idle (gelber Mond)",
          _Cfg(discord_token="x").status_presence == "idle",
          _Cfg(discord_token="x").status_presence)

    # RAM-Guard im echten Handler: kritischer RAM → sichtbarer Fehler,
    # KEINE DM (Abbruch vor dem ersten Teil).
    import bot.ram as _rammod
    _saved_rss_ram = _rammod.current_rss_mb
    client2._startup_dm_sent = True
    owner2.sent.clear()
    prog_ram = _ProgMsg()
    saved_used = db_mod._ram_used_mb
    db_mod.current_rss_mb = lambda: 500.0  # > kritische Schwelle (420)
    db_mod._ram_used_mb = lambda: 500.0    # Guard liest den Container-RAM
    _rammod.current_rss_mb = lambda: 500.0  # relieve_ram() misst selbst
    try:
        await client2._handle_admin_history(
            SimpleNamespace(original_response=lambda: _aret(prog_ram),
                            followup=_Followup()),
            mchannel)
    finally:
        db_mod.current_rss_mb = _saved_rss
        db_mod._ram_used_mb = saved_used
        _rammod.current_rss_mb = _saved_rss_ram
    check("Handler-RAM-Guard: kritischer RAM → sichtbarer Fehler statt OOM",
          "RAM kritisch" in (prog_ram.edits[-1] if prog_ram.edits else "")
          and len(owner2.sent) == 0,
          f"edits={prog_ram.edits[-1:] if prog_ram.edits else None}")


async def t_security(h: Harness) -> None:
    print("── Sicherheits-Grundlagen ────────────────────────────────────")
    api = h.api("danger")
    cfg = h.state.config

    public_cfg = {k: getattr(cfg, k) for k in dir(cfg)
                  if not k.startswith("_") and not callable(getattr(cfg, k, None))
                  and "token" not in k.lower()}
    check("Bot-Token steht nicht in der (maskierten) Konfiguration",
          "smoke.test.token" not in json.dumps(public_cfg, default=str))
    masked = cfg.masked()
    masked_token = str(masked.get("discord_token", ""))
    check("Config.masked() kürzt den Token",
          "smoke.test.token" not in json.dumps(masked, default=str)
          and ("…" in masked_token or masked_token.startswith("***")), masked_token)
    check("Config.masked() schwärzt kurze Geheimnisse vollständig",
          masked_token.startswith("***") and "smoke" not in masked_token, masked_token)
    check("Config.masked() bleibt sonst lesbar", masked.get("command_name") == "connect")

    caps = await h.anon.call("GET", "/api/v1/capabilities")
    check("Public capabilities ohne Token-Text",
          "smoke.test.token" not in caps["text"])

    headers = (await api.call("GET", "/api/v1/session"))["headers"]
    check("Kein Server-Versions-Leak", "aiohttp" not in (headers.get("Server") or ""))
    check("X-Content-Type-Options gesetzt",
          headers.get("X-Content-Type-Options") == "nosniff")

    options = await h.anon.call("OPTIONS", "/api/v1/session")
    check("CORS-Preflight → 204/200", options["status"] in (200, 204), str(options["status"]))
    check("Preflight nennt Authorization",
          "authorization" in (options["headers"].get("Access-Control-Allow-Headers") or "").lower())

    store = h.store
    token = h.tokens["danger"]
    session = store.verify(token)
    check("Session-Hash ist nicht das Klartext-Token", session.token_hash != token)
    check("Session-Hash hat feste Länge (sha256 hex)", len(session.token_hash) == 64,
          str(len(session.token_hash)))
    check("Sessions sind pro Server getrennt",
          {s.guild_id for s in store.active_for_guild(GUILD_ID)} <= {GUILD_ID})

    from bot.serializers import _mask_webhook_url, _serialize_audit_changes, serialize_emoji

    class _Emoji:
        id = 1
        name = "test"
        animated = False
        managed = False
        require_colons = True
        available = True
        url = "https://cdn.example/emoji.png"
        roles: List[Any] = []
        created_at = _now()

    check("Emoji-Serializer unterstützt discord.py require_colons",
          serialize_emoji(_Emoji()).get("requires_colons") is True)
    check("Webhook-URLs werden ohne geheimen Token serialisiert",
          _mask_webhook_url("https://discord.com/api/webhooks/123/secret")
          == "https://discord.com/api/webhooks/123/***")

    class _Diff:
        def __init__(self, **items: Any) -> None:
            self.__dict__.update(items)

        def __iter__(self):
            return iter(self.__dict__.items())

    class _Changes:
        before = _Diff(name="alt")
        after = _Diff(name="neu", color=123)

    serialized_changes = _serialize_audit_changes(_Changes())
    check("AuditLogChanges aus discord.py 2.x sind serialisierbar",
          {c["attribute"] for c in serialized_changes} == {"name", "color"},
          str(serialized_changes))


# ─────────────────────────────────────────────────────────────────────────────
#  Hauptablauf
# ─────────────────────────────────────────────────────────────────────────────


def _standard_guild() -> FakeGuild:
    """Der unveränderliche Referenz-Server für alle Lese-Tests."""
    return FakeGuild()


async def run_tests() -> int:
    config = load_config()
    config.settle_scale = 0.0        # keine Cache-Wartezeiten im Test
    config.api_rate_limit = 400        # groß — die Rate-Limit-Gruppe regelt selbst runter
    config.api_rate_window = 60

    guild = _standard_guild()
    other = FakeGuild(OTHER_GUILD_ID, "Fremder Server")
    other.channels = []
    other.members = []
    client = FakeClient([guild])       # 'other' ist bewusst NICHT im Cache
    # Das reale Limit (5 Sessions/Server) würde die vielen Test-Tokens sofort
    # auto-revoken. Deshalb hier hoch — die Limit-Logik hat eine eigene Gruppe.
    store = SessionStore(path=os.environ["SESSION_FILE"], persist=False, max_per_guild=50)
    state = AppState(client=client, config=config, store=store)  # type: ignore[arg-type]
    app = build_app(state)

    port = free_port()
    runner = await start_web_server(app, "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    print(f"\n🚀 Test-Server läuft auf {base}  ({len(app.router.routes())} Routen)\n")

    harness = Harness(state, base, store)
    groups = (
        ("public", t_public), ("auth", t_auth), ("read", t_read), ("scopes", t_scopes),
        ("errors", t_errors), ("setup", t_setup), ("lifecycle", t_session_lifecycle),
        ("ratelimit", t_ratelimit), ("write", t_write), ("arena", t_arena),
        ("prompt", t_prompt), ("pdf", t_pdf), ("limit", t_session_limit),
        ("docdelivery", t_docdelivery), ("historydelivery", t_history_delivery),
        ("security", t_security),
    )
    selected = [g for g in sys.argv[1:] if not g.startswith("-")] or [name for name, _ in groups]

    try:
        for name, func in groups:
            if name in selected:
                try:
                    await func(harness)
                except Exception as exc:  # noqa: BLE001
                    import traceback

                    FAILED.append(f"[{name}] Testgruppe abgebrochen: {exc!r}")
                    print(f"  💥 Testgruppe '{name}' abgebrochen: {exc!r}")
                    traceback.print_exc()
    finally:
        await harness.close()
        await stop_web_server(runner)

    print(f"\n{'═' * 64}")
    print(f"  ✅ {len(PASSED)} bestanden     ❌ {len(FAILED)} fehlgeschlagen")
    if FAILED:
        print("\n  Fehlgeschlagen:")
        for item in FAILED:
            print(f"   · {item}")
    print(f"{'═' * 64}\n")
    return 1 if FAILED else 0


def main() -> int:
    try:
        return asyncio.run(run_tests())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
