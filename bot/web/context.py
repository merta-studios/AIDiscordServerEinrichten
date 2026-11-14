"""
Request-Kontext für alle API-Handler.

Ein :class:`Ctx` bündelt alles, was ein Handler braucht: den aiohttp-Request,
die authentifizierte :class:`~bot.sessions.Session`, den Discord-``Guild`` und
eine Sammlung fehlertoleranter Helfer (Query, Body, IDs, Rollen, Kanäle …).

Dazu kommt :func:`guard` — es übersetzt ``discord.HTTPException`` und Co. in
saubere :class:`ApiError`-Antworten. Ohne diese Übersetzung würde jede
Discord-Laune als nackter 500er bei der KI landen.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Dict, List, Mapping, Optional, TypeVar

import discord
from aiohttp import web

from ..sessions import Session
from ..util import ApiError, as_id, parse_bool, parse_int, parse_str

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..discord_bot import RelayClient
    from ..sessions import SessionStore

log = logging.getLogger("relay.web")

T = TypeVar("T")

__all__ = ("Ctx", "guard", "APP_CTX_KEY")

APP_CTX_KEY = "relay_ctx"


async def guard(
    coro: Awaitable[T],
    *,
    action: str = "Aktion",
    hint: Optional[str] = None,
) -> T:
    """
    Führt einen discord.py-Aufruf aus und übersetzt Fehler in ``ApiError``.

    ``discord.Forbidden``  → 403 FORBIDDEN     (Rechte/Hierarchie)
    ``discord.NotFound``    → 404 NOT_FOUND     (Objekt weg)
    ``discord.HTTPException``→ 502 DISCORD_ERROR (mit Status & Text von Discord)
    """
    try:
        return await coro  # type: ignore[return-value]
    except discord.Forbidden as exc:
        raise ApiError.forbidden(
            f"{action} fehlgeschlagen: Der Bot hat nicht die nötigen Rechte.",
            hint=hint
            or "Prüfe, ob die Rolle des Bots über den betroffenen Rollen steht und ob der Bot "
            "Administrator ist. Discord-Meldung: " + str(exc.text or exc),
            code="DISCORD_FORBIDDEN",
            discord_status=getattr(exc, "status", None),
            discord_code=getattr(exc, "code", None),
            discord_text=str(getattr(exc, "text", "") or exc),
        ) from exc
    except discord.NotFound as exc:
        raise ApiError.not_found(
            f"{action} fehlgeschlagen: Das Ziel existiert nicht (mehr).",
            hint=hint or "Hole dir die aktuellen IDs (GET) und versuche es erneut. "
            "Discord-Meldung: " + str(exc.text or exc),
            code="DISCORD_NOT_FOUND",
            discord_text=str(getattr(exc, "text", "") or exc),
        ) from exc
    except discord.HTTPException as exc:
        status = getattr(exc, "status", 502) or 502
        text = str(getattr(exc, "text", "") or exc)
        code = getattr(exc, "code", None)
        friendly_hint = _translate_discord_code(code, text) or hint
        raise ApiError(
            f"{action} fehlgeschlagen: Discord meldet {status}"
            + (f" / Code {code}" if code else "")
            + (f" — {text}" if text else ""),
            code="DISCORD_ERROR",
            status=502 if status >= 500 else status,
            hint=friendly_hint,
            details={"discord_status": status, "discord_code": code, "discord_text": text},
        ) from exc
    except asyncio.TimeoutError as exc:
        raise ApiError.upstream(
            f"{action} fehlgeschlagen: Zeitüberschreitung bei der Anfrage an Discord.",
            hint="Versuche es erneut; ggf. die Anfrage verkleinern.",
            code="DISCORD_TIMEOUT",
        ) from exc
    except discord.DiscordException as exc:  # Gateway/Connection-Fehler
        raise ApiError.upstream(
            f"{action} fehlgeschlagen: Discord-Verbindungsproblem ({exc.__class__.__name__}).",
            hint="Der Bot ist möglicherweise gerade nicht mit dem Gateway verbunden. "
            "Kurz warten und erneut versuchen.",
            code="DISCORD_UNAVAILABLE",
        ) from exc


def _translate_discord_code(code: Optional[int], text: str) -> Optional[str]:
    """Menschliche/KI-lesbare Erklärung für die häufigsten Discord-Fehlercodes."""
    table = {
        50001: "Dem Bot fehlt die Berechtigung (Missing Access). Ist die Bot-Rolle hoch genug?",
        50013: "Fehlende Permissions. Prüfe Rolle/Overwrites — oder steht die Ziel-Rolle über der Bot-Rolle?",
        50035: "Ungültige Form/Feld. Details stehen in discord_text.",
        50041: "Ungültiger Embed-Inhalt (z. B. Feld zu lang).",
        50074: "Dieser Kanal-Typ erlaubt die Aktion nicht.",
        50086: "Embed/Anhang konnte nicht übernommen werden.",
        10003: "Kanal existiert nicht (mehr).",
        10004: "Server existiert nicht oder der Bot ist nicht mehr Mitglied.",
        10007: "Mitglied nicht gefunden.",
        10008: "Nachricht nicht gefunden.",
        10009: "Rolle nicht gefunden.",
        10011: "Channel/Kategorie nicht gefunden.",
        10013: "Invite existiert nicht oder wurde gelöscht.",
        10014: "Emoji nicht gefunden.",
        20001: "Bots dürfen sich nicht selbst bannen/kicken.",
        20012: "Reihenfolge: Der Bot kann keine Rolle über seiner eigenen Top-Rolle verwalten.",
        20016: "Reihenfolge: Die Rolle des Bots muss über der Zielrolle stehen.",
        30001: "Maximum erreicht (Discord-Limit, z. B. 500 Kanäle / 250 Rollen).",
        30013: "Maximum erreicht — zu viele Kanäle dieser Art.",
        30039: "Limit für Server-Boosts/Features erreicht.",
        40001: "Ungültiger OAuth2-Token.",
        50002: "Nur Bots dürfen diesen Endpoint nutzen.",
        50004: "Diese Aktion ist für diesen Kanal-Typ nicht erlaubt.",
        50007: "Nachrichten können an dieses Mitglied nicht gesendet werden (DM blockiert).",
        50017: "Webhooks dürfen diesen Endpoint nicht nutzen.",
        50026: "Ungültiges Bildformat (erlaubt: PNG, JPEG, GIF, WebP).",
        50045: "Bild zu groß (max. 256 KB für Emojis / 8,4 MB für Sticker).",
        50084: "Dieser Server hat das benötigte Feature nicht freigeschaltet.",
    }
    if code in table:
        return table[code]
    lowered = text.lower()
    if "missing access" in lowered:
        return table[50001]
    if "missing permission" in lowered:
        return table[50013]
    if "role hierarchy" in lowered or "highest role" in lowered:
        return table[20012]
    if "requires community" in lowered or "community" in lowered:
        return "Diese Funktion braucht einen aktivierten Community-Server: " "PATCH /api/v1/guild {\"community\": true}"
    if "two-factor" in lowered or "2fa" in lowered:
        return "Der Server verlangt 2FA für Moderationsaktionen — nur der Owner kann das ändern."
    return None


class Ctx:
    """Pro-Request-Kontext, den jeder Handler als einziges Argument bekommt."""

    __slots__ = (
        "request", "client", "config", "store", "session", "endpoint",
        "_guild", "_body", "_raw_body", "request_id",
    )

    def __init__(
        self,
        request: web.Request,
        *,
        client: "RelayClient",
        config: "Config",
        store: "SessionStore",
        session: Optional[Session],
        endpoint: Any,
    ) -> None:
        self.request = request
        self.client = client
        self.config = config
        self.store = store
        self.session = session
        self.endpoint = endpoint
        self._guild: Optional[discord.Guild] = None
        self._body: Optional[Any] = None
        self._raw_body: Optional[bytes] = None
        self.request_id = request.headers.get("X-Request-ID", "")

    # ── Kernobjekte ──────────────────────────────────────────────────────────
    @property
    def presented_token(self) -> Optional[str]:
        """
        Das Token, das der Aufrufer in DIESEM Request mitgeschickt hat.

        Kein Sicherheitsloch: Wer das Token präsentiert, kennt es bereits.
        ``GET /api/v1/prompt`` braucht es, um einen wirklich kopierfertigen
        Prompt zu erzeugen — sonst stünde dort nur ein Platzhalter.
        """
        from ..util import extract_bearer_token

        return extract_bearer_token(self.request)

    @property
    def guild(self) -> discord.Guild:
        """Der zur Sitzung gehörende Server — oder ein klarer Fehler."""
        if self._guild is not None:
            return self._guild
        if self.session is None:
            raise ApiError.unauthorized("Keine gültige Sitzung.", code="NO_SESSION")
        if self.client is None:
            # Passiert nur in den ersten Sekunden nach einem Deploy, solange der
            # Discord-Client noch gebaut wird. Kein Absturz — saubere 503.
            raise ApiError.unavailable(
                "Der Relay-Bot startet gerade erst und ist noch nicht mit Discord verbunden.",
                hint="Bitte in 5–10 Sekunden erneut versuchen. Der Healthcheck "
                     "GET /api/health zeigt 'bot: connected', sobald alles läuft.",
                code="SERVICE_STARTING",
            )
        guild = self.client.get_guild(self.session.guild_id)
        if guild is None:
            raise ApiError.conflict(
                f"Der Bot ist aktuell nicht mit dem Server (ID {self.session.guild_id}) verbunden.",
                hint="Mögliche Ursachen: Bot wurde entfernt, Server umbenannt/gelöscht oder der Bot "
                "ist noch nicht vollständig mit dem Gateway synchronisiert (passiert nach einem "
                "Cold-Start). Warte 5–10 Sekunden und versuche es erneut; sonst /connect neu ausführen.",
                code="GUILD_UNAVAILABLE",
            )
        if guild.unavailable:
            raise ApiError.conflict(
                f"Server '{guild.name}' ist gerade als 'unavailable' markiert (Discord-Störung).",
                hint="Bitte in einigen Sekunden erneut versuchen.",
                code="GUILD_UNAVAILABLE",
            )
        self._guild = guild
        return guild

    @property
    def bot_member(self) -> Optional[discord.Member]:
        return self.guild.me

    # ── Scope ────────────────────────────────────────────────────────────────
    def require(self, scope: str) -> None:
        if self.session is None:
            raise ApiError.unauthorized("Keine gültige Sitzung.", code="NO_SESSION")
        if not self.session.allows(scope):
            from ..sessions import MODES

            raise ApiError.forbidden(
                f"Diese Aktion benötigt die Berechtigungsstufe '{scope}', "
                f"das Token läuft aber im Modus '{self.session.mode}' "
                f"({MODES.get(self.session.mode, {}).get('label', self.session.mode)}).",
                hint="Bitte die Verbindung neu aufbauen: Auf dem Server /connect "
                "ausführen und auf „Verbinden“ klicken — neue Verbindungen haben "
                "immer den Modus 'Lesen + Schreiben'.",
                code="SCOPE_INSUFFICIENT",
            )

    # ── Query-Parameter ──────────────────────────────────────────────────────
    def q(self, name: str, default: Optional[str] = None) -> Optional[str]:
        value = self.request.query.get(name)
        if value is None or value == "":
            return default
        return value

    def q_int(self, name: str, default: Optional[int] = None, *,
              minimum: Optional[int] = None, maximum: Optional[int] = None) -> Optional[int]:
        raw = self.q(name)
        if raw is None:
            return default
        return parse_int(raw, field=f"?{name}", default=default, minimum=minimum, maximum=maximum)

    def q_bool(self, name: str, default: Optional[bool] = None) -> Optional[bool]:
        raw = self.q(name)
        if raw is None:
            return default
        return parse_bool(raw, field=f"?{name}", default=default)

    def q_id(self, name: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.q(name)
        if raw is None:
            return default
        return as_id(raw, field=f"?{name}")

    def q_list(self, name: str) -> List[str]:
        return self.request.query.getall(name, [])

    # ── Pfad-Parameter ───────────────────────────────────────────────────────
    def path(self, name: str, default: Optional[str] = None) -> Optional[str]:
        value = self.request.match_info.get(name)
        if value is None or value == "":
            if default is None:
                raise ApiError.bad_request(f"Pfadparameter '{{{name}}}' fehlt.")
            return default
        return value

    def path_id(self, name: str) -> int:
        return as_id(self.path(name), field=f"{{{name}}}")

    # ── Body ─────────────────────────────────────────────────────────────────
    async def body(self) -> Dict[str, Any]:
        """Liefert den JSON-Body als Objekt (leer ⇒ ``{}``)."""
        data = await self.body_any()
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            raise ApiError.bad_request(
                "Der Request-Body muss ein JSON-Objekt sein "
                f"(erhalten: {type(data).__name__}).",
                hint='Beispiel: -H "Content-Type: application/json" -d \'{"name": "chat"}\'',
                code="BODY_NOT_OBJECT",
            )
        return dict(data)

    async def body_any(self) -> Any:
        if self._body is not None:
            return self._body
        if self._raw_body is None:
            try:
                self._raw_body = await self.request.read()
            except Exception as exc:  # pragma: no cover
                raise ApiError.bad_request(f"Body konnte nicht gelesen werden: {exc}") from exc
        if not self._raw_body:
            self._body = {}
            return self._body
        content_type = (self.request.content_type or "").lower()
        if content_type and content_type not in {"application/json", "text/plain", ""}:
            raise ApiError.bad_request(
                f"Unsupported Content-Type '{content_type}'.",
                hint='Nutze: -H "Content-Type: application/json"',
                code="UNSUPPORTED_MEDIA_TYPE",
            )
        import json as _json

        try:
            self._body = _json.loads(self._raw_body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ApiError.bad_request("Body ist kein gültiges UTF-8.") from exc
        except _json.JSONDecodeError as exc:
            raise ApiError.bad_request(
                f"Body ist kein gültiges JSON: {exc.msg} (Zeile {exc.lineno}, Spalte {exc.colno}).",
                hint="Prüfe Anführungszeichen und Kommas. Beim Shell-Aufruf das JSON in "
                "einfache Hochkommata einschließen: -d '{\"name\":\"chat\"}'",
                code="BODY_INVALID_JSON",
            ) from exc
        return self._body

    # ── Bequemlichkeit ───────────────────────────────────────────────────────
    def reason(self, data: Optional[Mapping[str, Any]] = None, *, default: Optional[str] = None) -> Optional[str]:
        """Audit-Log-Grund: aus Body, Query oder Default."""
        source = data if data is not None else {}
        value = source.get("reason") or self.q("reason") or default
        if value is None:
            return default
        text = parse_str(value, field="reason", max_length=400)
        return text or default

    async def channel(self, name: str = "channel_id", *, kinds: Optional[tuple] = None) -> Any:
        channel_id = self.path_id(name)
        channel = self.guild.get_channel(channel_id) or self.guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await guard(
                    self.client.fetch_channel(channel_id),
                    action=f"Kanal {channel_id} laden",
                )
            except ApiError as exc:
                if exc.code in {"DISCORD_NOT_FOUND", "DISCORD_FORBIDDEN"}:
                    raise ApiError.not_found(
                        f"Kanal {channel_id} existiert nicht oder ist für den Bot nicht erreichbar.",
                        hint="Nutze GET /api/v1/channels für alle Kanal-IDs dieses Servers.",
                        code="CHANNEL_NOT_FOUND",
                    ) from exc
                raise
            if getattr(channel, "guild", None) and channel.guild.id != self.guild.id:
                raise ApiError.forbidden(
                    f"Kanal {channel_id} gehört zu einem anderen Server.",
                    code="CROSS_GUILD",
                )
        if channel is None:
            raise ApiError.not_found(
                f"Kanal {channel_id} existiert nicht.",
                hint="Nutze GET /api/v1/channels für alle Kanal-IDs.",
                code="CHANNEL_NOT_FOUND",
            )
        if kinds and not isinstance(channel, kinds):
            allowed = ", ".join(k.__name__ for k in kinds)
            raise ApiError.bad_request(
                f"Kanal {channel_id} ist vom Typ '{type(channel).__name__}', "
                f"erwartet wurde: {allowed}.",
                code="CHANNEL_TYPE_MISMATCH",
            )
        return channel

    def role(self, name: str = "role_id") -> discord.Role:
        role_id = self.path_id(name)
        role = self.guild.get_role(role_id)
        if role is None:
            raise ApiError.not_found(
                f"Rolle {role_id} existiert nicht auf Server '{self.guild.name}'.",
                hint="Nutze GET /api/v1/roles für alle Rollen-IDs.",
                code="ROLE_NOT_FOUND",
            )
        return role

    async def member(self, name: str = "member_id") -> discord.Member:
        member_id = self.path_id(name)
        member = self.guild.get_member(member_id)
        if member is None:
            try:
                member = await guard(
                    self.guild.fetch_member(member_id), action=f"Mitglied {member_id} laden"
                )
            except ApiError as exc:
                if exc.code == "DISCORD_NOT_FOUND":
                    raise ApiError.not_found(
                        f"Mitglied {member_id} ist nicht auf diesem Server.",
                        hint="Nutze GET /api/v1/members?query=<name> zum Suchen. "
                        "Hinweis: Für Nutzer, die nicht (mehr) Mitglied sind, "
                        "nutze PUT /api/v1/bans/{user_id} (bannt per ID ohne Mitgliedschaft).",
                        code="MEMBER_NOT_FOUND",
                    ) from exc
                raise
        return member

    def remote_ip(self) -> Optional[str]:
        forwarded = self.request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        peer = self.request.remote
        return peer

    # ── Cache-Synchronisation ────────────────────────────────────────────────
    async def settle(self, seconds: float = 0.45) -> None:
        """
        Kurz warten, bis Discord die Gateway-Events (``GUILD_UPDATE``,
        ``CHANNEL_CREATE``, ``GUILD_ROLE_CREATE`` …) geliefert hat.

        discord.py aktualisiert seinen Cache über diese Events, nicht über die
        REST-Antwort. Ohne diese Pause würde ein GET direkt nach einem POST
        gelegentlich den alten Zustand liefern — genau das verwirrt eine KI.
        """
        scale = float(getattr(self.config, "settle_scale", 1.0) or 0.0)
        if scale <= 0 or seconds <= 0:
            return
        await asyncio.sleep(seconds * scale)

    async def refresh_guild(self, delay: float = 0.45) -> discord.Guild:
        """Wartet auf Cache-Updates und liefert den (möglichst frischen) Guild."""
        await self.settle(delay)
        fresh = self.client.get_guild(self.session.guild_id if self.session else 0)
        if fresh is not None:
            self._guild = fresh
        return self._guild or fresh or self.guild

    def http_session(self):
        """Die geteilte aiohttp-Session des Servers (für Bild-Downloads)."""
        return self.request.app["relay_ctx"].http_session

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "method": self.request.method,
            "path": str(self.request.rel_url),
            "session": self.session.to_public_dict() if self.session else None,
            "guild": {"id": str(self.session.guild_id)} if self.session else None,
        }
