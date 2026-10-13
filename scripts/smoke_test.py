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
    GUILD_ID, TEXT_ID, USER_ID, FakeClient, FakeGuild, MutableGuild, now,
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

    # ── Mehr-Server-Datenbank: Wiederverwenden-Button & Leave-Aufräumen ──────
    from bot.discord_bot import database_unconfigured_view, CID_DB_REUSE

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

    db_plain = database_unconfigured_view()
    plain_parts = _view_parts(db_plain)
    plain_ids = [b.get("custom_id", "") for b in plain_parts["buttons"]]
    check("DB-View ohne Wiederverwenden: nur der grüne Verknüpfen-Button",
          "relay:v2_db_connect" in plain_ids and not any(i.startswith(CID_DB_REUSE) for i in plain_ids),
          str(plain_ids))

    db_reuse = database_unconfigured_view(reusable=[
        {"source_guild_id": 424242, "source_guild_name": "Mein Anderer Server",
         "database_url": "https://x.turso.io", "database_url_masked": "x.turso.io",
         "configured_by": "Klaus", "configured_at": "2026-01-01"},
    ])
    reuse_parts = _view_parts(db_reuse)
    reuse_buttons = [b for b in reuse_parts["buttons"] if b.get("custom_id", "").startswith(CID_DB_REUSE)]
    check("DB-View mit Wiederverwenden: blauer Button neben dem grünen",
          "relay:v2_db_connect" in [b.get("custom_id") for b in reuse_parts["buttons"]]
          and len(reuse_buttons) == 1
          and reuse_buttons[0].get("style") == int(discord.ButtonStyle.primary)  # blau
          and reuse_buttons[0].get("custom_id") == f"{CID_DB_REUSE}:424242",
          str([b.get("custom_id") for b in reuse_parts["buttons"]]))
    check("DB-View mit Wiederverwenden: Hinweistext nennt den Quell-Server",
          "Mein Anderer Server" in _joined_text(db_reuse) and "teilen" in _joined_text(db_reuse))

    detail_db = admin_guild_detail_view(
        g_smoke, owner=g_smoke.owner, owner_present=True, sessions=[],
        page=0, query="",
        db_status={"configured": True, "database_url": "demo.turso.io", "configured_by": "Klaus",
                   "systems_count": 3, "storage": {"usage_percent": 1.5, "used_formatted": "1.2 MB"},
                   "shared": True, "shared_with": [{"guild_id": "77", "guild_name": "Partner"}],
                   "updated_at": "2026-02-03T04:05:06+00:00"},
    )
    detail_db_text = _joined_text(detail_db)
    check("Server-Übersicht: Server-Details (Kanäle, Rollen, Bot-Rolle)",
          "Server-Details" in detail_db_text and "Kanäle:" in detail_db_text
          and "Rollen:" in detail_db_text and "Bot-Rolle:" in detail_db_text, detail_db_text[:300])
    check("Server-Übersicht: Datenbank-Status mit Teilen-Info",
          "Server-Datenbank" in detail_db_text and "3" in detail_db_text
          and "Geteilte Datenbank" in detail_db_text and "Partner" in detail_db_text,
          detail_db_text[:400])

    leave_cleanup = guild_remove_notify_view(g_smoke, None, "Bot wurde gekickt — Grund: Test", cleanup={
        "sessions_revoked": 2, "database_purged": True, "database_summary": {
            "exclusive": True, "shared_with": [], "dropped_tables": ["systems", "triggers"],
        },
    })
    leave_cleanup_text = _joined_text(leave_cleanup)
    check("Leave-DM nennt das Aufräumen (Sessions + geleerte DB)",
          "Aufräumen" in leave_cleanup_text and "2 KI-Verbindung(en) gestoppt" in leave_cleanup_text
          and "komplett gelöscht" in leave_cleanup_text, leave_cleanup_text[:300])
    leave_v = admin_guild_leave_confirm_view(g_smoke, page=0, query="")
    check("admin_guild_leave_confirm_view ist Components V2", leave_v.has_components_v2())


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
        ("prompt", t_prompt), ("limit", t_session_limit), ("security", t_security),
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
