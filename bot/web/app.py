"""
Der aiohttp-Server: Middleware, Authentifizierung, Rate-Limiting, CORS.

Läuft **im selben Event-Loop** wie der Discord-Client — ein Prozess, keine
IPC, keine doppelte Zustandsverwaltung. Genau deshalb ist aiohttp (das
discord.py ohnehin mitbringt) die richtige Wahl statt FastAPI + Uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

import discord
from aiohttp import web
from aiohttp.web_urldispatcher import MatchInfoError

from ..config import Config
from ..sessions import ActionRecord, SessionStore, Session
from ..util import extract_bearer_token, ApiError, iso, now_utc
from .context import APP_CTX_KEY, Ctx
from .registry import Endpoint, sorted_endpoints

log = logging.getLogger("relay.web")

__all__ = ("AppState", "build_app", "start_web_server", "stop_web_server")


# ─────────────────────────────────────────────────────────────────────────────
#  Zustand
# ─────────────────────────────────────────────────────────────────────────────


class AppState:
    """Gemeinsamer Zustand für Bot, API und Console."""

    def __init__(self, client: discord.Client, config: Config, store: SessionStore) -> None:
        self.client = client
        self.config = config
        self.store = store
        # Discord-Verbindungsstatus für /api/health. Wird vom Login-Loop
        # (bot.main) und on_ready gepflegt — damit man bei „Bot offline"
        # sofort sieht, OB gewartet wird und WANN es weitergeht, statt zu raten:
        # connecting | online | rate_limited | waiting | invalid_token |
        # connection_refused | closed
        self.discord_status: str = "connecting"
        self.discord_last_error: Optional[str] = None
        self.discord_retry_at: Optional[str] = None
        # Netzwerk-Diagnose (bot.netcheck): ausgehende IP + Erreichbarkeit von
        # discord.com. Beantwortet die Frage „Bot kaputt oder IP gesperrt?".
        self.net_report: Optional[Any] = None
        self.net_checked_mono: float = 0.0
        self.login_attempts: int = 0
        self.login_failures: int = 0
        self.ban_watches: int = 0
        self.restart_requested: Optional[str] = None
        # Neustart-Buch (bot.restarts.RestartLedger): zählt bewusste Neustarts
        # wegen IP-Sperre über Prozessgrenzen hinweg und merkt sich die dabei
        # gesehenen Ausgangs-IPs. None, solange main() es nicht gesetzt hat.
        self.restart_ledger: Optional[Any] = None
        # Ausgangs-IPs, die dieser Prozess selbst gemessen hat (in Reihenfolge,
        # ohne Doppelte) — zeigt, ob sich die IP innerhalb eines Containers ändert.
        self.egress_ips_seen: list = []
        self.egress_ip_changes: int = 0
        # Gesetzt, wenn das Neustart-Limit erreicht ist: Der Bot würfelt nicht
        # mehr, sondern probt nur noch. Text = Begründung für Log/Diagnose.
        self.platform_verdict: Optional[str] = None
        self.commands_synced_at: Optional[Any] = None
        self._net_ready: Optional[asyncio.Event] = None
        self.started_at = time.monotonic()
        self.started_wall = now_utc()
        self.request_count = 0
        self.error_count = 0
        self.detected_base_url: Optional[str] = None
        self.http_session: Optional[Any] = None
        self._rate: Dict[str, Deque[float]] = {}
        self._last_save = 0.0

    # ── Zeit & Metrik ────────────────────────────────────────────────────────
    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def now_iso(self) -> str:
        return iso(now_utc()) or ""

    # ── Netzwerk-Diagnose ────────────────────────────────────────────────────
    def net_ready(self) -> asyncio.Event:
        """
        Event, das gesetzt wird, sobald die erste Netz-Diagnose vorliegt.

        Wird bewusst *lazy* erzeugt: ``AppState`` entsteht auch in Tests ohne
        laufenden Event-Loop.
        """
        if self._net_ready is None:
            self._net_ready = asyncio.Event()
        return self._net_ready

    def set_net_report(self, report: Any) -> Optional[bool]:
        """
        Hinterlegt ein :class:`bot.netcheck.NetReport` und weckt Wartende.

        Liefert ``True``, wenn der Report eine **andere** Ausgangs-IP zeigt als
        die zuletzt gemessene (``False`` = gleiche IP, ``None`` = keine IP im
        Report). Reports ohne IP-Messung (``with_egress_ip=False``) erben die
        zuletzt bekannte IP, damit ``/api/health`` sie nicht verliert.
        """
        previous = getattr(self.net_report, "egress_ip", None)
        new_ip = getattr(report, "egress_ip", None)
        changed: Optional[bool] = None
        if new_ip:
            if new_ip not in self.egress_ips_seen:
                self.egress_ips_seen.append(new_ip)
                self.egress_ips_seen = self.egress_ips_seen[-32:]
            if previous:
                changed = new_ip != previous
                if changed:
                    self.egress_ip_changes += 1
        elif previous is not None and hasattr(report, "egress_ip"):
            # Probe ohne IP-Messung: alte Messung übernehmen.
            with contextlib.suppress(Exception):
                report.egress_ip = previous
                report.egress_source = getattr(self.net_report, "egress_source", None)
        self.net_report = report
        self.net_checked_mono = time.monotonic()
        self.net_ready().set()
        return changed

    def net_report_age(self) -> float:
        """Alter der letzten Diagnose in Sekunden (``inf``, wenn es keine gibt)."""
        return float("inf") if not self.net_checked_mono else max(
            0.0, time.monotonic() - self.net_checked_mono
        )

    @property
    def egress_ip(self) -> Optional[str]:
        return getattr(self.net_report, "egress_ip", None)

    def net_dict(self) -> Dict[str, Any]:
        """JSON-fähige Ansicht der letzten Diagnose (für /api/health)."""
        report = self.net_report
        if report is None:
            return {"verdict": "not_checked"}
        try:
            return report.to_dict()
        except Exception:  # noqa: BLE001 — Diagnose darf nie eine Antwort killen
            return {"verdict": getattr(report, "verdict", "unknown")}

    # ── Öffentliche Basis-URL ────────────────────────────────────────────────
    def base_url(self) -> str:
        """
        Öffentlich erreichbare Wurzel dieses Dienstes.

        ``PUBLIC_URL`` (explizit) > ``RENDER_EXTERNAL_URL`` (Render setzt das
        automatisch) > aus dem letzten Request gelernt (Host/X-Forwarded-*).
        """
        if self.config.base_url:
            return self.config.base_url
        return self.config.resolved_base_url(self.detected_base_url)

    def learn_base_url(self, request: web.Request) -> None:
        """Merkt sich die öffentliche URL anhand der Proxy-Header."""
        if self.config.base_url:
            return
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
        host = (
            request.headers.get("X-Forwarded-Host")
            or request.headers.get("X-Original-Host")
            or request.headers.get("Host")
        )
        if not host:
            return
        host = host.split(",")[0].strip()
        candidate = f"{proto}://{host}".rstrip("/")
        if candidate.startswith("http://127.") or candidate.startswith("http://localhost"):
            return
        self.detected_base_url = candidate

    # ── Persistenz (entprellt) ───────────────────────────────────────────────
    def maybe_save(self, *, min_interval: float = 20.0, force: bool = False) -> None:
        """
        Schreibt die Sessions höchstens alle ``min_interval`` Sekunden auf Platte.

        Ohne Drosselung würde jeder API-Aufruf eine Datei schreiben — bei einem
        Setup mit 60 Aufrufen unnötiger I/O auf einem Free-Container.
        """
        now = time.monotonic()
        if force or now - self._last_save >= min_interval:
            self._last_save = now
            self.store.save()

    # ── Rate-Limiting (pro Token, Sliding Window) ────────────────────────────
    def check_rate_limit(self, key: str) -> Optional[float]:
        """Liefert ``retry_after`` in Sekunden oder ``None``, wenn alles frei ist."""
        limit = self.config.api_rate_limit
        window = float(self.config.api_rate_window)
        if limit <= 0:
            return None
        now = time.monotonic()
        bucket = self._rate.setdefault(key, deque())
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return round(window - (now - bucket[0]), 2)
        bucket.append(now)
        # Speicherhygiene: verwaiste Buckets rauswerfen
        if len(self._rate) > 512:
            stale = [k for k, v in self._rate.items() if not v or now - v[-1] > window * 4]
            for k in stale:
                self._rate.pop(k, None)
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  JSON-Antworten
# ─────────────────────────────────────────────────────────────────────────────


def _dumps(value: Any) -> str:
    from ..serializers import jsonable

    return json.dumps(value, ensure_ascii=False, default=jsonable, separators=(",", ":"))


def ok_response(data: Any = None, *, status: int = 200, request_id: str = "",
                extra_headers: Optional[Dict[str, str]] = None) -> web.Response:
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Request-ID": request_id,
    }
    if extra_headers:
        headers.update(extra_headers)
    return web.Response(body=_dumps({"ok": True, "data": data}).encode("utf-8"),
                        status=status, headers=headers)


def error_response(exc: ApiError, *, request_id: str = "",
                   extra_headers: Optional[Dict[str, str]] = None) -> web.Response:
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Request-ID": request_id,
    }
    if extra_headers:
        headers.update(extra_headers)
    retry_after = (exc.details or {}).get("retry_after")
    if retry_after:
        headers["Retry-After"] = str(int(retry_after) + 1)
    return web.Response(body=_dumps(exc.to_dict(request_id)).encode("utf-8"),
                        status=exc.status, headers=headers)


def _extract_token(request: web.Request) -> Optional[str]:
    """Dünne Weiterleitung auf :func:`bot.util.extract_bearer_token`."""
    return extract_bearer_token(request)


# ─────────────────────────────────────────────────────────────────────────────
#  Handler-Fabrik
# ─────────────────────────────────────────────────────────────────────────────


def make_handler(endpoint: Endpoint, state: AppState) -> Callable[[web.Request], Any]:
    async def handler(request: web.Request) -> web.Response:
        request_id = secrets.token_hex(6)
        started = time.perf_counter()
        session: Optional[Session] = None
        ctx: Optional[Ctx] = None
        token = _extract_token(request)

        try:
            if token:
                try:
                    session = state.store.verify(token, remote=request.remote)
                except ApiError:
                    if not endpoint.public:
                        raise
                    session = None

            if not endpoint.public and session is None:
                if token:
                    session = state.store.verify(token, remote=request.remote)
                else:
                    raise ApiError.unauthorized(
                        "Zugriff verweigert: kein Token übermittelt.",
                        hint='Sende den Header: Authorization: Bearer <TOKEN>. '
                             "Das Token bekommst du auf Discord über /connect.",
                        code="TOKEN_MISSING",
                    )

            if session is not None:
                retry_after = state.check_rate_limit(session.token_hash)
                if retry_after is not None:
                    raise ApiError.too_many(
                        f"Zu viele Anfragen: mehr als {state.config.api_rate_limit} pro "
                        f"{state.config.api_rate_window}s für dieses Token.",
                        hint="Warte die angegebene Zeit und drossle deine Aufrufe. "
                             "Für Massenänderungen POST /api/v1/setup oder /bulk nutzen.",
                        retry_after=retry_after,
                    )
                if not session.allows(endpoint.scope):
                    ctx_probe = Ctx(request, client=state.client, config=state.config,
                                    store=state.store, session=session, endpoint=endpoint)
                    ctx_probe.require(endpoint.scope)

            ctx = Ctx(request, client=state.client, config=state.config, store=state.store,
                      session=session, endpoint=endpoint)
            data = await endpoint.handler(ctx)
            if isinstance(data, web.Response):
                # Escape-Hatch für Endpoints, die bewusst kein JSON liefern
                # (z. B. /api/v1/prompt?format=text).
                response = data
                response.headers.setdefault("X-Request-ID", request_id)
                response.headers.setdefault("Cache-Control", "no-store")
            else:
                response = ok_response(data, request_id=request_id)
            status = response.status
            error_code = None

        except ApiError as exc:
            response = error_response(exc, request_id=request_id)
            status, error_code = exc.status, exc.code
            state.error_count += 1
            if exc.status >= 500:
                log.error("%s %s → %s %s: %s", request.method, request.path_qs,
                          exc.status, exc.code, exc.message)
            else:
                log.info("%s %s → %s %s: %s", request.method, request.path_qs,
                         exc.status, exc.code, exc.message)
        except web.HTTPException as exc:
            api_error = ApiError(
                f"{exc.reason or exc.__class__.__name__}: {request.method} {request.path} "
                "ist nicht verfügbar.",
                code="HTTP_" + str(exc.status),
                status=exc.status,
                hint="GET /api/v1/capabilities listet alle gültigen Endpoints.",
            )
            response = error_response(api_error, request_id=request_id)
            status, error_code = exc.status, api_error.code
            state.error_count += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - letzte Verteidigungslinie
            log.exception("Unbehandelter Fehler bei %s %s", request.method, request.path_qs)
            api_error = ApiError(
                f"Interner Fehler: {exc.__class__.__name__}: {exc}",
                code="INTERNAL_ERROR", status=500,
                hint="Das ist ein Fehler im Relay-Bot. Bitte den Log auf Render prüfen "
                     "und den Aufruf vereinfachen.",
                details={"request_id": request_id},
            )
            response = error_response(api_error, request_id=request_id)
            status, error_code = 500, api_error.code
            state.error_count += 1

        duration_ms = (time.perf_counter() - started) * 1000
        if session is not None:
            # Action-Log: reine Buchführung — ein Fehler hier darf niemals die
            # eigentliche Antwort killen, deshalb komplett abgesichert.
            try:
                remote = ctx.remote_ip() if ctx is not None else request.remote
                state.store.record_action(
                    ActionRecord(
                        ts=now_utc(), session_id=session.id, guild_id=session.guild_id,
                        method=request.method, path=request.path, status=status,
                        duration_ms=duration_ms, remote=remote,
                        ok=status < 400, error_code=error_code,
                    )
                )
                state.maybe_save()      # gedrosselt, nicht bei jedem Aufruf
            except Exception as exc:  # noqa: BLE001
                log.warning("Action-Log konnte nicht geschrieben werden: %s", exc)
        if status >= 500 or duration_ms > 3000:
            log.warning("%s %s → %d in %.0fms", request.method, request.path_qs, status, duration_ms)
        elif log.isEnabledFor(logging.DEBUG):
            log.debug("%s %s → %d in %.0fms", request.method, request.path_qs, status, duration_ms)
        return response

    handler.__name__ = f"{endpoint.method.lower()}_{endpoint.path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
    return handler


# ─────────────────────────────────────────────────────────────────────────────
#  Middleware
# ─────────────────────────────────────────────────────────────────────────────


@web.middleware
async def infrastructure_middleware(request: web.Request, handler: Callable) -> web.Response:
    """Basis-URL lernen, CORS, Preflight, JSON-Fehler für unbekannte API-Pfade."""
    state: AppState = request.app[APP_CTX_KEY]
    state.request_count += 1
    state.learn_base_url(request)

    if request.method == "OPTIONS":
        return web.Response(status=204, headers=_cors_headers(request, state, "OPTIONS"))

    # Unbekannte Pfade / falsche Methode: für /api* JSON statt aiohttp-HTML
    if isinstance(request.match_info, MatchInfoError) and request.path.startswith("/api"):
        # aiohttp nennt das Attribut 'http_exception' — sonst fängt hier niemand
        # die 404/405 ab und es kommt eine HTML-Fehlerseite statt JSON.
        exc = request.match_info.http_exception
        status = exc.status
        if status == 405:
            allowed = getattr(exc, "headers", {}).get("Allow", "")
            message = f"Methode {request.method} ist für {request.path} nicht erlaubt."
            hint = f"Erlaubt: {allowed}." if allowed else ""
            code = "METHOD_NOT_ALLOWED"
        else:
            message = f"Endpoint {request.method} {request.path} existiert nicht."
            hint = "GET /api/v1/capabilities listet alle gültigen Endpoints."
            code = "ENDPOINT_NOT_FOUND"
        api_error = ApiError(message, code=code, status=status, hint=hint or None)
        response = error_response(api_error)
        response.headers.update(_cors_headers(request, state, request.method))
        return response

    response = await handler(request)
    response.headers.update(_cors_headers(request, state, request.method))
    return response


SERVER_HEADER = "AIDiscordServerEinrichten"


def _cors_headers(request: web.Request, state: AppState, method: str) -> Dict[str, str]:
    origin = request.headers.get("Origin", "*")
    allowed = state.config.allowed_origin
    if allowed == "*":
        allow_origin = "*"
    else:
        allow_origin = origin if origin in {o.strip() for o in allowed.split(",")} else ""
    headers = {
        # Kein Server-Versions-Leak: aiohttp würde sonst "Python/3.x aiohttp/y.z"
        # schicken. Für einen Scanner ist das eine unnötige Information.
        "Server": SERVER_HEADER,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "no-referrer",
    }
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin
        headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Request-ID, X-Api-Token"
        headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        headers["Access-Control-Max-Age"] = "600"
        if allow_origin != "*":
            headers["Vary"] = "Origin"
    return headers


# ─────────────────────────────────────────────────────────────────────────────
#  App-Aufbau
# ─────────────────────────────────────────────────────────────────────────────


def build_app(state: AppState) -> web.Application:
    """Erzeugt die aiohttp-Application und registriert alle Endpoints."""
    from .console import register_console_routes
    from .routes import (  # noqa: F401  — Import registriert die Endpoints
        channels, events, expressions, guides, guild, invites, members, messages,
        meta, moderation, roles, setup, webhooks,
    )

    app = web.Application(client_max_size=state.config.max_body_bytes)
    app[APP_CTX_KEY] = state
    app.middlewares.append(infrastructure_middleware)

    for endpoint in sorted_endpoints():
        handler = make_handler(endpoint, state)
        if endpoint.method == "GET":
            # UptimeRobot und manche Load-Balancer prüfen HTTP-URLs standardmäßig
            # per HEAD. ``add_route('GET', ...)`` registriert in aiohttp KEIN
            # automatisches HEAD; Ergebnis war 405 Method Not Allowed und Render
            # wurde trotz „5-Minuten-Ping" nicht zuverlässig wach gehalten.
            # ``add_get(..., allow_head=True)`` macht alle GET-Endpunkte HEAD-
            # kompatibel; aiohttp unterdrückt dabei den Body automatisch.
            app.router.add_get(endpoint.path, handler, allow_head=True)
        else:
            app.router.add_route(endpoint.method, endpoint.path, handler)

    register_console_routes(app, state)

    async def on_startup(app: web.Application) -> None:
        import aiohttp

        state.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, connect=15),
            headers={"User-Agent": "AIDiscordServerEinrichten/1.0"},
        )
        log.info("HTTP-Session für Bild-/Datei-Downloads gestartet.")

    async def on_cleanup(app: web.Application) -> None:
        if state.http_session is not None and not state.http_session.closed:
            await state.http_session.close()
            log.info("HTTP-Session geschlossen.")
        state.store.save()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    log.info("REST-API registriert: %d Endpoints.", len(sorted_endpoints()))
    return app


async def start_web_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port, shutdown_timeout=10.0, reuse_address=True)
    await site.start()
    log.info("Web-Server lauscht auf http://%s:%d", host, port)
    return runner


async def stop_web_server(runner: web.AppRunner) -> None:
    try:
        await runner.cleanup()
    except Exception as exc:  # noqa: BLE001
        log.warning("Web-Server konnte nicht sauber beendet werden: %s", exc)
