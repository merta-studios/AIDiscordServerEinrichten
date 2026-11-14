"""
Einstiegspunkt: startet Discord-Client **und** Web-Server in einem Prozess.

Ablauf
------
1. Konfiguration laden & validieren (klare Fehlermeldung bei fehlendem Token).
2. Logging einrichten.
3. Session-Store öffnen (lädt persistente Tokens).
4. ``AppState`` + ``RelayClient`` bauen und miteinander verdrahten.
5. aiohttp-Server auf ``0.0.0.0:$PORT`` starten → ab jetzt antwortet
   ``/api/health`` und UptimeRobot kann den Dienst wach halten.
6. Netzwerk-Diagnose im Hintergrund: ausgehende IP ermitteln und prüfen, ob
   ``discord.com`` ohne Token erreichbar ist (``/api/v10/gateway``). Damit ist
   im Log sofort sichtbar, OB die IP von Cloudflare gesperrt ist — die
   häufigste Ursache für einen „offline"-Bot auf Free-Hosting.
7. Bot mit Discord verbinden. Klappt das wegen nicht freigeschalteter
   **privilegierter Intents** nicht, wird automatisch ohne sie neu gestartet —
   der Bot bleibt also online, statt im Crash-Loop zu hängen. Blockt
   Cloudflare die **ausgehende IP** (429 / Error 1015), probt der Bot alle
   30 s tokenlos, ob die Sperre weg ist, und loggt sich sofort wieder ein.
   Bleibt die IP zehn Minuten lang gesperrt, beendet sich der Prozess mit
   Exit-Code 3 — Render startet dann einen frischen Container, der eine
   andere (hoffentlich saubere) Ausgangs-IP bekommt.
8. Auf ``SIGTERM``/``SIGINT`` warten (Render schickt das bei jedem Deploy) und
   sauber herunterfahren.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import sys
import time
from datetime import timedelta
from typing import Any, Optional

import aiohttp
import discord

from . import __botname__, __version__
from .config import Config, ConfigError, load_config
from .discord_bot import RelayClient, build_intents
from .netcheck import (
    VERDICT_NETWORK_ERROR,
    NetReport,
    proxy_auth_from,
    run_netcheck,
)
from .restarts import RestartLedger
from .sessions import SessionStore
from .util import iso, now_utc
from .web.app import AppState, build_app, start_web_server, stop_web_server

log = logging.getLogger("relay.main")

__all__ = ("main", "amain")

BANNER = r"""
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║   A I D i s c o r d S e r v e r E i n r i c h t e n               ║
║                                                                   ║
║   Discord  ⇄  Arena AI · Relay-Bot mit Administrator-Rechten      ║
║   Ein Command:  /connect                                          ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝
"""


def setup_logging(level: str) -> None:
    """Einheitliches, container-freundliches Logging (eine Zeile, UTC-Zeit)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s │ %(levelname)-7s │ %(name)-16s │ %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)

    # Lärm reduzieren
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("discord.state").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _build_store(config: Config) -> SessionStore:
    path = config.session_file if config.persist_sessions else None
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        except OSError as exc:
            log.warning("Datenverzeichnis %s nicht anlegbar (%s) — Sessions bleiben flüchtig.",
                        path, exc)
            path = None
    return SessionStore(
        default_ttl_hours=config.session_ttl_hours,
        max_per_guild=config.max_sessions_per_guild,
        path=path,
        persist=config.persist_sessions and bool(path),
    )


async def _housekeeping(state: AppState, stop: asyncio.Event) -> None:
    """
    Hintergrundaufgabe:

    * entfernt abgelaufene/widerrufene Sitzungen und schreibt sie weg,
    * stoppt Verbindungen automatisch, deren Token zu lange ungenutzt war
      (Standard: 24 Stunden — „verfügbar, aber niemand benutzt es“),
    * protokolliert alle 5 Minuten einen Herzschlag (praktisch im Render-Log),
    * hält den Prozess beschäftigt, falls der Gateway kurz hängt.
    """
    last_beat = 0.0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass
        try:
            state.store._prune_locked()  # noqa: SLF001 — bewusst, gleiche Klasse
            idle_hours = float(getattr(state.config, "session_inactivity_hours", 0.0) or 0.0)
            stopped = await state.store.revoke_inactive(
                timedelta(hours=idle_hours) if idle_hours > 0 else None
            )
            if stopped:
                log.warning(
                    "⛔ %d Verbindung(en) automatisch gestoppt: Token länger als %.0f Stunde(n) "
                    "ungenutzt. Arena AI bekommt bei weiteren Versuchen einen 401er mit "
                    "Anleitung zum schnellen Wiederanbinden.",
                    len(stopped), idle_hours,
                )
                state.maybe_save(force=True)
            state.maybe_save(min_interval=60.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("Housekeeping-Fehler: %s", exc)

        now = time.monotonic()
        if now - last_beat >= 300:
            last_beat = now
            client = state.client
            online = bool(client and getattr(client, "user", None))
            report = state.net_report
            net = ""
            if not online:
                # Solange der Bot offline ist, gehört die Netz-Lage in jeden
                # Herzschlag: IP gesperrt oder Token-Problem? Ohne das raten
                # Nutzer im Render-Log herum.
                verdict = getattr(report, "verdict", "not_checked")
                net = (
                    f" · discord.com {'erreichbar' if getattr(report, 'discord_reachable', False) else 'GESPERRT'}"
                    f" ({verdict})"
                    f" · IP {getattr(report, 'egress_ip', None) or '?'}"
                    f" · Login-Status {state.discord_status}"
                    + (f" · nächster Versuch {state.discord_retry_at}" if state.discord_retry_at else "")
                )
            log.info(
                "❤ Uptime %dh%02dm · Server %d · Anfragen %d · Fehler %d · Sessions %d · Gateway %s%s",
                int(state.uptime_seconds() // 3600),
                int((state.uptime_seconds() % 3600) // 60),
                len(client.guilds) if client else 0,
                state.request_count, state.error_count,
                len(state.store.all_active()),
                f"{round(client.latency * 1000)} ms" if client and client.latency else "–",
                net,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Login-Wiederholung: IP-Sperre erkennen, kurz proben, notfalls IP wechseln
# ─────────────────────────────────────────────────────────────────────────────
#
# Was bei „HTTP 429 / Cloudflare Error 1015" wirklich passiert
# ------------------------------------------------------------
# Discord liegt hinter Cloudflare. Überschreitet eine IP Discords Limit
# (10.000 *ungültige* Anfragen — 401/403/429 — pro 10 Minuten bzw. 50
# Anfragen/s insgesamt), blockt Cloudflare die **IP-Adresse**, nicht den Token:
# Jede Anfrage von dort wird mit 429 und einer HTML-Seite beantwortet.
# discord.py erkennt das am fehlenden ``Via``-Header und wirft sofort, ohne
# internen Wiederholungsversuch.
#
# Auf Free-Plänen (Render, Railway, Replit …) teilen sich hunderte Kunden
# wenige Ausgangs-IPs. Flutet ein anderer Mieter Discord, ist die IP für *alle*
# gesperrt — der eigene Bot ist dann völlig unschuldig und kommt trotzdem nicht
# online. Ohne Diagnose sieht dieser Fall exakt aus wie ein kaputter Bot.
#
# Warum „30 Minuten schlafen und dann erneut versuchen" hier falsch war
# --------------------------------------------------------------------
# Der alte Code hat den ``Retry-After``-Wert der Blockseite respektiert und bis
# zu 1800 s gewartet. Bei einer *geteilten* IP ist das die schlechteste aller
# Strategien:
#
#   * Das Zeitfenster der Sperre ist häufig nach wenigen Minuten vorbei — wer
#     30 Minuten schläft, verpasst es und läuft direkt in die nächste Sperre.
#   * Flutet ein Nachbar dauerhaft, ist die IP *unbegrenzt* verbrannt. Dann
#     hilft kein Warten der Welt, sondern nur eine andere IP.
#   * Der Zähler „Fehlversuche in Folge" wurde nach 300 s ohne Versuch
#     zurückgesetzt — bei 1800 s Wartezeit eskalierte das Backoff also niemals
#     (im Log stand deshalb immer „Fehlversuch 1 in Folge" + 1800 s).
#
# Die neue Strategie
# ------------------
# 1. **Diagnose statt Raterei** (:mod:`bot.netcheck`): Vor dem ersten Login und
#    während jeder Sperre wird ``GET https://discord.com/api/v10/gateway``
#    *ohne Token* aufgerufen. Der Endpoint liegt hinter derselben Cloudflare-
#    Regel, erzeugt aber keine ungültige Anfrage. Damit ist sauber trennbar:
#    IP gesperrt (Probe 429) vs. Token-Problem (Probe 200, Login trotzdem 429).
#    Zusätzlich wird die ausgehende IP ermittelt und ins Log geschrieben — die
#    steht sonst nirgends, ist aber der eigentliche Übeltäter.
# 2. **Kurz und oft proben statt lange schlafen**: alle
#    ``BAN_PROBE_INTERVAL_SECONDS`` (Default 30 s) = 20 Proben pro 10 Minuten
#    = 0,2 % von Discords 10.000er-Limit. Sobald eine Probe grün ist, wird
#    *sofort* eingeloggt — statt bis zum Ablauf eines blinden Timers zu warten.
# 3. **IP wechseln, wenn die IP verbrannt ist**: bleibt die Sperre
#    ``BAN_WATCH_SECONDS`` (Default 10 min) bestehen, beendet sich der Prozess
#    mit Exit-Code 3. Render startet daraufhin einen frischen Container, und
#    der bekommt eine *andere* Adresse aus Renders geteiltem Ausgangs-Bereich
#    („your service might use any IP address within its associated ranges").
#    Genau das ist die Standard-Empfehlung bei diesem Fehler: neu deployen, bis
#    man eine nicht gesperrte IP erwischt. Vor jedem Neustart liegen immer
#    ≥ ``BAN_WATCH_SECONDS`` Beobachtung — ein heißer Crash-Loop entsteht nicht.
# 4. **Proxy als dauerhafte Lösung**: ``DISCORD_PROXY`` wird an discord.py
#    durchgereicht (REST *und* Gateway). Damit verlässt der Traffic das
#    Rechenzentrum über eine eigene, saubere IP.
#
# Fatale Fehler (falscher Token, dauerhaft abgewiesene Gateway-Verbindung)
# beenden den Prozess weiterhin NICHT: Ein Exit würde Render sofort neu starten
# lassen (Crash-Loop = Login-Spam = frische Sperre). Web-Server und
# /api/health bleiben erreichbar, der Login wird in großem Abstand versucht.

#: Kurze Abkühlphase nach einem Cloudflare-Block, bevor das Proben beginnt.
CLOUDFLARE_MIN_WAIT = 20.0
#: Mindestwartezeit nach einem echten Discord-Rate-Limit (429 *mit* Via-Header).
DISCORD_RATELIMIT_MIN_WAIT = 30.0
#: Harte Obergrenze für einen einzelnen Wartezyklus (war 1800 s — zu lang).
MAX_SINGLE_WAIT = 300.0
#: Verjährung des Fehlerzählers. Muss GRÖSSER als die längste Wartezeit sein,
#: sonst eskaliert das Backoff nie (genau das war der alte Bug).
BACKOFF_RESET_AFTER = 1800.0
#: Obergrenze, wenn die Sperre am Token hängt — eine neue IP bringt dann nichts.
TOKEN_BAN_MAX_WAIT = 3600.0
#: Exit-Code für „Plattform, bitte gib mir einen frischen Container".
EXIT_CODE_RESTART = 3
#: Wie lange auf die erste Netz-Diagnose gewartet wird, bevor der erste
#: Login-Versuch startet (verhindert einen Versuch in eine bekannt gesperrte IP).
NETCHECK_STARTUP_WAIT = 15.0


class RestartRequested(Exception):
    """
    Der Prozess soll beendet werden, damit die Plattform neu startet.

    Einziger Auslöser: eine nachweislich (per unauthentifizierter Probe)
    gesperrte ausgehende IP, die sich im Beobachtungsfenster nicht erholt hat.
    Ein frischer Container bekommt bei Render eine andere Adresse aus dem
    geteilten Ausgangs-Bereich — oft die einzige Möglichkeit, wieder online zu
    kommen.
    """

    def __init__(self, reason: str, exit_code: int = EXIT_CODE_RESTART) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


def _extract_retry_after(exc: BaseException) -> Optional[float]:
    """
    Liest einen vom Server gewünschten Wartewert aus einer Discord-Exception.

    Quellen: ``RateLimited.retry_after`` bzw. der ``Retry-After``-Header der
    HTTP-Antwort (Cloudflare/Discord). Liefert ``None``, wenn nichts dabei ist.
    """
    direct = getattr(exc, "retry_after", None)
    if isinstance(direct, (int, float)) and direct >= 0:
        return float(direct)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            raw = headers.get("Retry-After")
        except Exception:  # noqa: BLE001 — fremdes Header-Objekt, defensiv
            raw = None
        if raw:
            try:
                return max(0.0, float(str(raw).strip()))
            except (TypeError, ValueError):
                pass
    return None


def _is_cloudflare_ban(exc: discord.HTTPException) -> bool:
    """
    True, wenn der 429er von Cloudflare (z. B. Error 1015) statt von Discord kommt.

    discord.py wirft genau dann sofort (ohne internen Retry), wenn der
    429-Antwort der ``Via``-Header fehlt **oder** der Body kein JSON ist —
    beides ist bei einer Cloudflare-Fehlerseite der Fall. (Der ``error code: 0``
    allein taugt NICHT als Kriterium: Auch echte Discord-429-JSONs enthalten oft
    kein ``code``-Feld.)
    """
    if getattr(exc, "status", 0) != 429:
        return False
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return True  # nichts inspizierbar → vorsichtig wie einen Bann behandeln
    try:
        if not headers.get("Via"):
            return True
    except Exception:  # noqa: BLE001 — defensiv: im Zweifel wie ein Bann behandeln
        return True
    # Via ist da, aber discord.py hat trotzdem HTTPException (statt RateLimited)
    # geworfen ⇒ der Body war kein JSON ⇒ Fehlerseite von Cloudflare oder einem
    # Zwischensystem. Wichtig: NUR HTML zählt als Blockseite. Auf den Text
    # „rate limited" zu prüfen wäre falsch — Discords eigene 429-JSONs sagen
    # exakt das ("You are being rate limited.") und sind gerade KEIN IP-Bann.
    text = str(getattr(exc, "text", "") or "").strip().lower()
    if not text:
        return False
    if text.startswith("<") or "<html" in text:
        return True
    return "cloudflare" in text or "error 1015" in text


def _backoff_delay(base: float, cap: float, failures: int) -> float:
    """
    Exponentielles Backoff mit Equal-Jitter: ``base * 2^(n-1)``, gedeckelt auf
    ``cap``, plus Zufall (Hälfte fest, Hälfte zufällig). Der Jitter verhindert,
    dass mehrere gleichzeitig gestartete Instanzen im Gleichtakt loshämmern.
    """
    exp = min(cap, base * (2.0 ** max(0, failures - 1)))
    return exp / 2.0 + random.uniform(0, exp / 2.0)


def _proxy_auth(config: Config) -> Optional[Any]:
    """``aiohttp.BasicAuth`` für den Discord-Proxy, falls Zugangsdaten gesetzt sind."""
    return proxy_auth_from(config)


async def _run_netcheck(config: Config, state: AppState, *, with_egress_ip: bool = True) -> Any:
    """
    Führt die Netz-Diagnose aus und hinterlegt sie im Zustand.

    Wirft nie: Eine Diagnose, die selbst fehlschlägt, darf den Bot nicht
    aufhalten — dann steht das Ergebnis eben als Fehler im Report.

    Wechselt die gemessene Ausgangs-IP gegenüber der letzten Messung, wird das
    ausdrücklich geloggt: Genau diese Information entscheidet, ob Neustarts auf
    der Plattform überhaupt „würfeln" oder immer dieselbe Adresse liefern.
    """
    previous_ip = getattr(state.net_report, "egress_ip", None)
    try:
        report = await run_netcheck(
            proxy=(config.discord_proxy or None),
            proxy_auth=_proxy_auth(config),
            timeout=config.netcheck_timeout_seconds,
            with_egress_ip=with_egress_ip,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Netz-Diagnose fehlgeschlagen: %s", exc)
        report = NetReport(verdict=VERDICT_NETWORK_ERROR,
                           hint=f"Diagnose nicht ausführbar: {exc}",
                           checked_at=iso(now_utc()) or "")
    changed = state.set_net_report(report)
    if changed:
        log.warning(
            "🔀 Ausgangs-IP hat sich INNERHALB dieses Containers geändert: %s → %s "
            "(bisher %d Wechsel, gesehen: %s). discord.com jetzt %s.",
            previous_ip, report.egress_ip, state.egress_ip_changes,
            ", ".join(state.egress_ips_seen[-6:]),
            "erreichbar" if report.discord_reachable else "weiterhin gesperrt",
        )
    return report


def _restart_ledger(state: AppState) -> RestartLedger:
    """Das Neustart-Buch aus dem Zustand — notfalls ein flüchtiges (Tests)."""
    ledger = getattr(state, "restart_ledger", None)
    if not isinstance(ledger, RestartLedger):
        ledger = RestartLedger(path=None)
        with contextlib.suppress(Exception):
            state.restart_ledger = ledger
    return ledger


def _platform_gave_up_text(config: Config, ledger: RestartLedger) -> str:
    """Klartext für Log + Diagnose, wenn das Neustart-Limit erreicht ist."""
    ips = ", ".join(ledger.ips[-6:]) or "unbekannt"
    distinct = len(ledger.ips)
    if distinct <= 1:
        dice = (
            f"Jeder Neustart lieferte DIESELBE Ausgangs-IP ({ips}) — diese Plattform "
            "würfelt nicht, sie hat hier faktisch eine feste, gesperrte Adresse."
        )
    else:
        dice = (
            f"{distinct} verschiedene Ausgangs-IPs gesehen ({ips}) — alle gesperrt. "
            "Der komplette Ausgangs-Pool dieser Region ist bei Cloudflare verbrannt."
        )
    return (
        f"Neustart-Limit erreicht ({ledger.cycles}/{config.restart_max_cycles} Zyklen ohne "
        f"erfolgreichen Login). {dice} Weitere Neustarts bringen nichts; der Bot probt "
        f"jetzt nur noch alle {config.ban_probe_interval_seconds:.0f} s in Ruhe weiter. "
        "Lösung: eigene Ausgangs-IP — Option 1: kleiner VPS (deploy/docker-compose.yml, "
        "README-Abschnitt 'Betrieb auf einem eigenen Server'); Option 2: Fly.io "
        "(deploy/fly.toml); Option 3: Render behalten + DISCORD_PROXY mit statischer IP."
    )


def _log_report(report: Any, *, level: int = logging.INFO) -> None:
    for line in report.summary():
        log.log(level, "  %s", line)


async def _watch_ip_ban(config: Config, state: AppState, stop: asyncio.Event, *,
                        cause: str, report: Any) -> bool:
    """
    Beobachtet eine gesperrte IP, bis sie wieder frei ist oder das Fenster abläuft.

    Die Proben sind **unauthentifiziert** (``GET /api/v10/gateway``): Sie
    brauchen kein Token, zählen bei Discord nicht als ungültige Anfrage und
    liegen trotzdem hinter derselben Cloudflare-Regel wie der Login. Damit
    erkennt der Bot das Ende der Sperre innerhalb von Sekunden — statt einen
    blinden 30-Minuten-Timer abzusitzen.

    Liefert ``True``, sobald discord.com wieder erreichbar ist (→ sofort neu
    einloggen), sonst ``False``.
    """
    state.ban_watches += 1
    probe_interval = max(5.0, config.ban_probe_interval_seconds)
    budget = config.ban_watch_seconds
    # budget <= 0 heißt „unbegrenzt probieren, nie neu starten" — dann gibt es
    # keine Frist. Ohne diese Unterscheidung entstünde ein Heißloop, der Discord
    # mit Login-Versuchen flutet (genau das, was die Sperre verlängert).
    unlimited = budget <= 0.0
    ledger = _restart_ledger(state)
    may_restart = (
        config.restart_on_ip_ban and not unlimited
        and not ledger.exhausted(config.restart_max_cycles)
    )
    # Sagt Cloudflare selbst, dass die Sperre noch sehr lange hält (Retry-After
    # weit über dem Beobachtungsfenster), ist ein volles 10-Minuten-Fenster
    # verschwendet: Statt in eine 2-Stunden-Sperre hineinzuproben, wird das
    # Fenster auf wenige Proben verkürzt (Bestätigung, dass die Sperre echt
    # ist) und dann neu gestartet — jeder Neustart ist ein neuer Wurf auf eine
    # andere Adresse, und Würfe pro Stunde sind hier die entscheidende Größe.
    # Bewusst NICHT sofort: Auf einem flüchtigen Dateisystem überlebt der
    # Zyklus-Zähler den Neustart nicht, und ohne Mindestfenster entstünde ein
    # Heißloop aus Neustarts.
    retry_after = getattr(getattr(report, "discord", None), "retry_after", None)
    threshold = config.ban_fast_restart_above_seconds
    fast = (
        may_restart and threshold > 0 and retry_after is not None
        and retry_after > max(threshold, budget)
        and 0 < config.ban_fast_restart_watch_seconds < budget
    )
    if fast:
        budget = max(probe_interval, config.ban_fast_restart_watch_seconds)
    watch_no = state.ban_watches
    probe_no = 0
    recheck_every = max(0, int(config.ban_egress_recheck_every))

    log.error(
        "❌ Discord blockiert die AUSGEHENDE IP dieses Containers (%s).\n"
        "   Das ist kein Fehler im Bot und keiner im Token: Cloudflare sperrt\n"
        "   die IP (Error 1015), auf Free-Plänen ist sie mit anderen Kunden\n"
        "   geteilt. Der Bot probt jetzt alle %.0f s mit einem tokenlosen\n"
        "   Aufruf, ob die Sperre aufgehoben ist, und loggt sich SOFORT ein,\n"
        "   sobald discord.com wieder durchlässt (Beobachtungsfenster: %s).",
        cause, probe_interval, "unbegrenzt" if unlimited else f"{budget:.0f} s",
    )
    _log_report(report, level=logging.ERROR)
    if fast:
        log.error(
            "   ⏩ Cloudflare meldet Retry-After %.0f s — weit länger als das normale "
            "Fenster (%.0f s). Beobachtung deshalb auf %.0f s verkürzt, danach "
            "Neustart für eine neue Ausgangs-IP (BAN_FAST_RESTART_ABOVE_SECONDS=%.0f).",
            retry_after, config.ban_watch_seconds, budget, threshold,
        )
    if ledger.cycles:
        log.error("   %s", ledger.summary(config.restart_max_cycles))
    deadline = float("inf") if unlimited else time.monotonic() + budget

    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        wait = min(probe_interval, remaining)
        state.discord_status = "ip_blocked"
        state.discord_last_error = (
            f"Ausgehende IP von Cloudflare gesperrt ({cause}). "
            f"Ausgangs-IP: {report.egress_ip or 'unbekannt'}"
        )[:300]
        state.discord_retry_at = iso(now_utc() + timedelta(seconds=wait))
        await _sleep_or_stop(stop, wait)
        if stop.is_set():
            return False

        probe_no += 1
        # Jede n-te Probe misst die Ausgangs-IP mit: Ändert sie sich innerhalb
        # des Containers, loggt _run_netcheck das — die offene Frage „würfelt
        # Render überhaupt?" beantwortet sich damit von selbst.
        with_ip = bool(recheck_every) and probe_no % recheck_every == 0
        report = await _run_netcheck(config, state, with_egress_ip=with_ip)
        if report.discord_reachable:
            log.info(
                "✅ discord.com ist wieder erreichbar (Probe %d in Beobachtung %d) — "
                "Login wird sofort neu versucht.", probe_no, watch_no,
            )
            return True
        probe = report.discord
        left = deadline - time.monotonic()
        # Nicht jede Probe ins Log: alle 5. und die letzte reichen, der Rest auf
        # DEBUG. Sonst steht bei 10 Minuten Beobachtung zwanzigmal dieselbe
        # Zeile im Render-Log und verdeckt die wichtigen Meldungen.
        level = logging.WARNING if (probe_no == 1 or probe_no % 5 == 0 or left <= probe_interval) \
            else logging.DEBUG
        log.log(
            level,
            "⏳ IP weiter gesperrt (Probe %d, HTTP %s%s) — Restfenster %s.",
            probe_no,
            getattr(probe, "status", None) or "—",
            f", Ray {probe.cf_ray}" if getattr(probe, "cf_ray", None) else "",
            "unbegrenzt" if left == float("inf") else f"{max(0.0, left):.0f} s",
        )
        if left <= 0:
            return False
    return False


async def _give_up_on_ip(config: Config, state: AppState, stop: asyncio.Event, *,
                         cause: str, report: Any) -> None:
    """
    Entscheidung, wenn die IP im Beobachtungsfenster nicht frei wurde.

    Mit ``RESTART_ON_IP_BAN=true`` (Default) beendet sich der Prozess, damit die
    Plattform einen frischen Container startet — der bekommt eine andere
    Adresse aus dem geteilten Ausgangs-Bereich. Ohne diese Option wird einfach
    weiter probiert.
    """
    if stop.is_set():
        return  # Shutdown läuft — keine Entscheidung mehr nötig
    ledger = _restart_ledger(state)
    restart_enabled = config.restart_on_ip_ban and config.ban_watch_seconds > 0

    if restart_enabled and ledger.exhausted(config.restart_max_cycles):
        # ── Limit erreicht: aufhören zu würfeln, klar sagen, woran es liegt ──
        verdict = _platform_gave_up_text(config, ledger)
        if state.platform_verdict != verdict:
            state.platform_verdict = verdict
            log.critical("🛑 %s", verdict)
            log.critical("   %s", ledger.summary(config.restart_max_cycles))
        else:
            log.error("🛑 Neustart-Limit weiterhin erreicht (%d Zyklen) — nur noch Proben, "
                      "kein Neustart. Details: GET /api/diagnostics", ledger.cycles)
        cooldown = max(120.0, config.ban_probe_interval_seconds * 4)
        state.discord_status = "ip_blocked"
        state.discord_last_error = (
            f"Plattform-Limit: {ledger.cycles} Neustarts ohne Erfolg — eigene Ausgangs-IP nötig "
            f"(VPS/Fly.io/DISCORD_PROXY). IP: {report.egress_ip or 'unbekannt'}"
        )[:300]
        state.discord_retry_at = iso(now_utc() + timedelta(seconds=cooldown))
        await _sleep_or_stop(stop, cooldown)
        return

    if restart_enabled and not stop.is_set():
        cycle = ledger.record_restart(egress_ip=report.egress_ip, cause=cause)
        remaining = ledger.remaining(config.restart_max_cycles)
        persistence_note = (
            "" if ledger.saved_ok else
            "\n   ⚠ Neustart-Zähler konnte nicht gespeichert werden — auf einem flüchtigen\n"
            "     Dateisystem (Render Free) beginnt jeder Container wieder bei Zyklus 1."
        )
        log.critical(
            "🔁 Ausgehende IP bleibt gesperrt (%s) — dieser Container kommt so nie\n"
            "   online. Der Prozess beendet sich gleich mit Exit-Code %d, damit\n"
            "   die Plattform einen FRISCHEN Container startet. Der zieht mit etwas\n"
            "   Glück eine andere Adresse aus dem geteilten Ausgangs-Bereich.\n"
            "   Gesperrte IP : %s\n"
            "   Neustart-Zyklus %d%s · bisher gesehene IPs: %s%s\n"
            "   Dauerhaft ruhig wird es nur mit eigener Ausgangs-IP:\n"
            "     · kleiner VPS (deploy/docker-compose.yml) oder Fly.io (deploy/fly.toml)\n"
            "     · oder DISCORD_PROXY=http://user:pass@host:port mit statischer IP\n"
            "   Abschalten dieses Verhaltens: RESTART_ON_IP_BAN=false",
            cause, EXIT_CODE_RESTART, report.egress_ip or "unbekannt",
            cycle, f"/{config.restart_max_cycles}" if config.restart_max_cycles > 0 else "",
            ", ".join(ledger.ips[-6:]) or "–", persistence_note,
        )
        if remaining == 0:
            log.critical("   Das war der letzte erlaubte Neustart-Zyklus (RESTART_MAX_CYCLES=%d). "
                         "Bleibt die IP danach gesperrt, probt der Bot nur noch — ohne Neustart.",
                         config.restart_max_cycles)
        state.restart_requested = f"ip_blocked:{cause}"
        state.discord_status = "ip_blocked"
        await _sleep_or_stop(stop, config.restart_delay_seconds)
        if stop.is_set():
            return
        raise RestartRequested(f"Ausgehende IP dauerhaft von Cloudflare gesperrt ({cause})")

    # Neustart ist abgeschaltet: bewusst abkühlen, bevor weiter probiert wird.
    # Ohne diese Pause würde der Loop sofort den nächsten Login feuern — und
    # damit genau das tun, was eine IP-Sperre verlängert.
    cooldown = max(120.0, config.ban_probe_interval_seconds * 4)
    log.error(
        "IP bleibt gesperrt (%s) — Neustart ist deaktiviert (RESTART_ON_IP_BAN=false). "
        "%.0f s Abkühlphase, danach wird weiter probiert.", cause, cooldown,
    )
    state.discord_status = "ip_blocked"
    state.discord_last_error = (
        f"Ausgehende IP bleibt gesperrt ({cause}); Neustart deaktiviert."
    )[:300]
    state.discord_retry_at = iso(now_utc() + timedelta(seconds=cooldown))
    await _sleep_or_stop(stop, cooldown)


async def _await_first_netcheck(state: AppState, timeout: float = NETCHECK_STARTUP_WAIT) -> Any:
    """Wartet kurz auf die Start-Diagnose, damit der erste Login nicht blind ist."""
    event = state.net_ready()
    if event.is_set():
        return state.net_report
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log.debug("Start-Diagnose nicht innerhalb von %.0f s fertig — weiter ohne sie.", timeout)
    return state.net_report


async def _connect_bot(
    config: Config, store: SessionStore, state: AppState, stop: asyncio.Event
) -> None:
    """
    Verbindet den Bot mit Discord — mit Intent-Fallback, IP-Sperren-Erkennung
    und Backoff.

    Ohne den Intent-Fallback würde ein vergessener Schalter im Developer Portal
    (``SERVER MEMBERS INTENT``) zu einem endlosen Crash-Loop auf Render führen.
    Ohne die Sperren-Erkennung säße der Bot bei einer von Cloudflare gesperrten
    IP in einem blinden 30-Minuten-Timer fest und käme nie wieder online.
    """
    privileged = config.enable_privileged_intents
    attempt = 0
    failures = 0
    last_failure = 0.0
    token_bans = 0

    def note_failure() -> int:
        """Zählt einen Fehlversuch (mit Verjährung alter Ausfälle)."""
        nonlocal failures, last_failure
        now = time.monotonic()
        if now - last_failure > BACKOFF_RESET_AFTER:
            failures = 0
        failures += 1
        last_failure = now
        return failures

    def backoff() -> float:
        """Nächste Wartezeit aus Basis, Maximum und Fehlerzähler."""
        return _backoff_delay(
            config.login_retry_base_seconds,
            config.login_retry_max_seconds,
            note_failure(),
        )

    async def wait_and_report(delay: float, status: str, error: str) -> None:
        """Wartet (abbrechenbar) und spiegelt Grund + nächsten Versuch ins Health-API."""
        state.discord_status = status
        state.discord_last_error = error[:300]
        state.discord_retry_at = iso(now_utc() + timedelta(seconds=max(0.0, delay)))
        await _sleep_or_stop(stop, delay)

    # Start-Diagnose abwarten: Ist die IP schon VOR dem ersten Versuch gesperrt,
    # sparen wir uns eine unnötige (ungültige) Anfrage an Discord.
    startup_report = await _await_first_netcheck(state)
    if startup_report is not None and startup_report.ip_blocked:
        log.warning("Start-Diagnose: discord.com ist von dieser IP aus nicht erreichbar.")
        if not await _watch_ip_ban(config, state, stop, cause="Start-Diagnose",
                                   report=startup_report):
            await _give_up_on_ip(config, state, stop, cause="Start-Diagnose",
                                 report=startup_report)

    while not stop.is_set():
        attempt += 1
        state.login_attempts = attempt
        intents = build_intents(privileged)
        # WICHTIG: Ein ``client.close()`` beendet die interne aiohttp-Session
        # endgültig — derselbe Client kann danach NICHT erneut ``start()``en
        # (RuntimeError: "Session is closed"). Deshalb wird ab dem zweiten
        # Versuch immer ein frischer Client gebaut. Nur der allererste Durchlauf
        # nutzt den in amain() vorbereiteten, damit state.client nie None ist.
        client = state.client
        needs_fresh = (
            attempt > 1
            or not isinstance(client, RelayClient)
            or client.intents.value != intents.value
            or client.is_closed()
        )
        if needs_fresh:
            client = RelayClient(config, store, state, intents=intents)
            state.client = client

        state.discord_status = "connecting"
        state.discord_retry_at = None
        log.info("Verbinde Bot mit Discord (%s) …",
                 "mit privilegierten Intents" if privileged else "ohne privilegierte Intents")
        try:
            await client.start(config.discord_token, reconnect=True)
        except discord.PrivilegedIntentsRequired:
            with contextlib.suppress(Exception):
                await client.close()
            if privileged:
                log.error(
                    "❌ Discord verweigert die privilegierten Intents!\n"
                    "   Der Bot startet automatisch OHNE 'Server Members'/'Message Content' neu —\n"
                    "   er bleibt online, aber die Mitgliederliste ist eventuell unvollständig.\n"
                    "   ✅ Dauerhafte Lösung: https://discord.com/developers/applications\n"
                    "      → deine App → Bot → 'SERVER MEMBERS INTENT' aktivieren → speichern."
                )
                privileged = False
                continue
            # Sollte eigentlich nie passieren (ohne privilegierte Intents gibt es
            # nichts zu verweigern) — aber falls doch: kein Crash-Loop, sondern
            # in großem Abstand neu versuchen, Web-Server bleibt erreichbar.
            log.critical(
                "❌ Discord verweigert die Intents sogar ohne privilegierte Scopes. "
                "Neuer Versuch alle %.0f s — bitte Token und Developer-Portal prüfen.",
                config.fatal_retry_seconds,
            )
            await wait_and_report(
                config.fatal_retry_seconds, "connection_refused",
                "PrivilegedIntentsRequired trotz deaktivierter privilegierter Intents",
            )
            continue
        except discord.LoginFailure as exc:
            with contextlib.suppress(Exception):
                await client.close()
            # BEWUSST kein raise: Ein Exit würde Render sofort neu starten lassen
            # (Crash-Loop = Login-Spam = Cloudflare-Bann). Stattdessen bleibt der
            # Prozess mit grünem /api/health am Leben und versucht es später neu.
            log.critical(
                "❌ Login fehlgeschlagen: %s\n"
                "   DISCORD_BOT_TOKEN ist ungültig.\n"
                "   → https://discord.com/developers/applications → App → Bot → 'Reset Token'\n"
                "   → neues Token in Render unter Environment setzen → Save Changes.\n"
                "   Der Web-Server bleibt erreichbar, der Login wird alle %.0f s erneut versucht.",
                exc, config.fatal_retry_seconds,
            )
            await wait_and_report(
                config.fatal_retry_seconds, "invalid_token",
                f"LoginFailure: {exc}",
            )
            continue
        except discord.HTTPException as exc:
            with contextlib.suppress(Exception):
                await client.close()
            status = getattr(exc, "status", 0)
            state.login_failures += 1

            if status == 429 and _is_cloudflare_ban(exc):
                # ── Der Kernfall: Cloudflare blockt die ausgehende IP ────────
                # Zuerst messen, woran es wirklich hängt, statt zu raten:
                report = await _run_netcheck(config, state)
                ray = getattr(getattr(exc, "response", None), "headers", {})
                ray_id = ""
                with contextlib.suppress(Exception):
                    ray_id = str(ray.get("CF-RAY") or "")
                if report.ip_blocked:
                    failures = note_failure()
                    token_bans = 0
                    if await _watch_ip_ban(config, state, stop,
                                           cause=f"Login HTTP 429{f', Ray {ray_id}' if ray_id else ''}",
                                           report=report):
                        continue  # IP wieder frei → sofort neu einloggen
                    await _give_up_on_ip(config, state, stop, cause="Login HTTP 429", report=report)
                    continue
                if report.discord_reachable:
                    # discord.com kommt ohne Token durch, mit Token aber nicht:
                    # Die Sperre hängt am TOKEN/Account, nicht an der IP. Eine
                    # neue IP bringt hier nichts — also lange warten und deutlich
                    # sagen, was zu tun ist.
                    token_bans += 1
                    delay = min(config.fatal_retry_seconds * (2 ** (token_bans - 1)), TOKEN_BAN_MAX_WAIT)
                    log.critical(
                        "❌ Login rate-limitiert (HTTP 429), aber discord.com ist ohne Token\n"
                        "   erreichbar ⇒ die Sperre hängt am TOKEN, nicht an der IP.\n"
                        "   Ursachen: dasselbe Token läuft in zwei Prozessen (lokal + Render!),\n"
                        "   doppelte Render-Instanzen oder zu viele ungültige Anfragen.\n"
                        "   → Alle anderen Prozesse mit diesem Token stoppen.\n"
                        "   → Falls das nichts hilft: Developer Portal → Bot → 'Reset Token'\n"
                        "     und das neue Token in Render eintragen.\n"
                        "   Neuer Versuch in %.0f s.", delay,
                    )
                    await wait_and_report(delay, "rate_limited",
                                          f"HTTP 429 beim Login trotz erreichbarer IP: {exc}"[:300])
                    continue
                # Weder IP-Sperre noch erreichbar: Netzwerk-/DNS-Problem.
                delay = min(backoff(), MAX_SINGLE_WAIT)
                log.error(
                    "❌ discord.com ist nicht erreichbar (%s). Neuer Versuch in %.0f s.",
                    getattr(report.discord, "error", None) or f"HTTP {status}", delay,
                )
                await wait_and_report(delay, "network_error",
                                      f"discord.com nicht erreichbar: {exc}"[:300])
                continue

            retry_after = _extract_retry_after(exc)
            delay = min(backoff(), MAX_SINGLE_WAIT)
            if status == 429:
                delay = max(delay, DISCORD_RATELIMIT_MIN_WAIT)
            if retry_after is not None:
                # Retry-After wird respektiert, aber gedeckelt: Ein einzelner
                # Header darf den Bot nicht für eine halbe Stunde parken.
                delay = min(max(delay, retry_after + 5.0), MAX_SINGLE_WAIT)
            if status == 429:
                log.error(
                    "❌ Discord-Rate-Limit beim Login (HTTP 429, Via-Header vorhanden). "
                    "Neuer Versuch in %.0f s (Fehlversuch %d in Folge).", delay, failures,
                )
            else:
                log.error(
                    "❌ Discord meldete HTTP %s beim Login (%s). Neuer Versuch in %.0f s "
                    "(Fehlversuch %d in Folge).", status, exc, delay, failures,
                )
            await wait_and_report(
                delay, "rate_limited" if status == 429 else "waiting",
                f"HTTP {status} beim Login: {exc}"[:300],
            )
            continue
        except discord.RateLimited as exc:
            with contextlib.suppress(Exception):
                await client.close()
            retry_after = _extract_retry_after(exc) or 0.0
            delay = min(max(backoff(), retry_after + 5.0), MAX_SINGLE_WAIT)
            log.error(
                "❌ Discord-Rate-Limit beim Login (Retry in %.0f s). Neuer Versuch in %.0f s.",
                retry_after, delay,
            )
            await wait_and_report(
                delay, "rate_limited",
                f"RateLimited: Retry in {retry_after:.0f} s",
            )
            continue
        except discord.GatewayNotFound:
            with contextlib.suppress(Exception):
                await client.close()
            delay = min(backoff(), MAX_SINGLE_WAIT)
            log.error("Discord-Gateway nicht erreichbar — neuer Versuch in %.0f s.", delay)
            await wait_and_report(delay, "waiting", "GatewayNotFound: Gateway nicht erreichbar")
            continue
        except discord.ConnectionClosed as exc:
            with contextlib.suppress(Exception):
                await client.close()
            if getattr(exc, "code", None) in {4004, 4010, 4011, 4012, 4013, 4014}:
                # BEWUSST kein raise (siehe LoginFailure): kein Crash-Loop.
                log.critical(
                    "❌ Discord hat die Verbindung dauerhaft geschlossen (Code %s: %s).\n"
                    "   4004/4010/4011/4012/4013/4014 = Token oder Intents sind falsch.\n"
                    "   Bitte Token prüfen und die Intents im Developer Portal kontrollieren.\n"
                    "   Der Web-Server bleibt erreichbar, neuer Versuch alle %.0f s.",
                    exc.code, exc.reason, config.fatal_retry_seconds,
                )
                await wait_and_report(
                    config.fatal_retry_seconds, "connection_refused",
                    f"Gateway dauerhaft geschlossen (Code {getattr(exc, 'code', '?')})",
                )
                continue
            delay = min(backoff(), MAX_SINGLE_WAIT)
            log.warning("Gateway-Verbindung getrennt (Code %s) — neuer Versuch in %.0f s.",
                        getattr(exc, "code", "?"), delay)
            await wait_and_report(
                delay, "waiting",
                f"Gateway getrennt (Code {getattr(exc, 'code', '?')})",
            )
            continue
        except (aiohttp.ClientError, OSError) as exc:
            # DNS/TCP/TLS: discord.com ist von diesem Container aus schlicht nicht
            # erreichbar. Bewusst KEIN Traceback-Feuerwerk — die Ursache steht
            # bereits in der Netz-Diagnose (GET /api/diagnostics bzw. im
            # Start-Log), hier reicht eine Zeile mit dem nächsten Versuch.
            with contextlib.suppress(Exception):
                await client.close()
            delay = min(backoff(), MAX_SINGLE_WAIT)
            log.error(
                "❌ Netzwerkfehler beim Login (%s: %s). discord.com ist von diesem "
                "Container aus nicht erreichbar — das ist ein DNS-/Firewall-/Proxy-"
                "Problem und keines am Token. Neuer Versuch in %.0f s "
                "(Fehlversuch %d in Folge). Details: GET /api/diagnostics",
                type(exc).__name__, str(exc)[:160], delay, failures,
            )
            await wait_and_report(
                delay, "network_error", f"{type(exc).__name__}: {exc}"[:300]
            )
            continue
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await client.close()
            raise
        except RestartRequested:
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await client.close()
            delay = min(backoff(), MAX_SINGLE_WAIT)
            log.exception("Unerwarteter Fehler beim Bot-Start (Versuch %d) — neuer Versuch in %.0f s.",
                          attempt, delay)
            await wait_and_report(delay, "waiting", "Unerwarteter Fehler beim Bot-Start (siehe Log)")
            continue

        # client.start() kehrt nur bei sauberem close() zurück
        state.discord_status = "closed"
        log.info("Bot-Verbindung beendet.")
        break


def _log_restart_history(config: Config, state: AppState, report: Any) -> None:
    """
    Nach dem Start: Ist das ein Neustart wegen IP-Sperre? Hat er eine andere IP
    gebracht? Das ist die Information, die im Render-Log bisher fehlte.
    """
    ledger = _restart_ledger(state)
    changed = ledger.note_boot_ip(getattr(report, "egress_ip", None))
    if ledger.cycles <= 0:
        if ledger.stale_at_start:
            log.info("Neustart-Buch war älter als %.0f h — Zähler beginnt bei 0.",
                     ledger.max_age_seconds / 3600)
        return
    limit = f"/{config.restart_max_cycles}" if config.restart_max_cycles > 0 else ""
    if changed is True:
        log.warning(
            "🔁 Dies ist Neustart-Zyklus %d%s wegen gesperrter IP. Die Ausgangs-IP hat "
            "gewechselt (%s → %s) — die Plattform würfelt also tatsächlich. "
            "discord.com ist jetzt %s.",
            ledger.cycles, limit, ledger.last_ip, report.egress_ip,
            "ERREICHBAR ✅" if getattr(report, "discord_reachable", False) else "weiterhin GESPERRT",
        )
    elif changed is False:
        log.warning(
            "🔁 Dies ist Neustart-Zyklus %d%s wegen gesperrter IP — und der Container hat "
            "wieder DIESELBE Ausgangs-IP (%s) bekommen (%d× in Folge). Neustarts "
            "würfeln auf dieser Plattform offenbar nicht.",
            ledger.cycles, limit, report.egress_ip, ledger.same_ip_streak,
        )
    else:
        log.warning("🔁 Dies ist Neustart-Zyklus %d%s wegen gesperrter IP (Ausgangs-IP "
                    "diesmal nicht messbar). %s", ledger.cycles, limit,
                    ledger.summary(config.restart_max_cycles))
    if ledger.exhausted(config.restart_max_cycles) and not getattr(report, "discord_reachable", False):
        verdict = _platform_gave_up_text(config, ledger)
        state.platform_verdict = verdict  # sofort in /api/diagnostics sichtbar
        log.critical("🛑 %s", verdict)


async def _startup_netcheck(config: Config, state: AppState, stop: asyncio.Event) -> None:
    """
    Hintergrund-Task: einmalige Netz-Diagnose beim Start.

    Läuft parallel zum Web-Server (der antwortet sofort auf ``/api/health``) und
    wird vom Login-Loop abgewartet, bevor der erste Versuch gefeuert wird. So
    steht im Render-Log direkt, welche ausgehende IP der Container hat und ob
    ``discord.com`` überhaupt erreichbar ist — statt erst nach einem
    fehlgeschlagenen Login raten zu müssen.
    """
    if stop.is_set():
        state.net_ready().set()
        return
    log.info("Netz-Diagnose: ermittle ausgehende IP und prüfe discord.com (ohne Token) …")
    report = await _run_netcheck(config, state)
    _log_report(report, level=logging.INFO if report.verdict == "ok" else logging.WARNING)
    _log_restart_history(config, state, report)
    if report.ip_blocked:
        log.warning(
            "⚠ discord.com blockt diese ausgehende IP schon VOR dem ersten Login-Versuch. "
            "Der Login wird deshalb nicht blind gefeuert — der Bot wartet, bis die Sperre "
            "nachweislich aufgehoben ist (Details: GET /api/diagnostics)."
        )
    elif report.verdict != "ok":
        log.warning(
            "⚠ discord.com ist von diesem Container aus nicht erreichbar. Bleibt das "
            "so, ist es ein Netzwerk-/DNS-Problem: GET /api/diagnostics zeigt Details."
        )


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def amain() -> int:
    """Asynchroner Hauptablauf. Liefert einen Exit-Code."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"\n❌ Konfigurationsfehler:\n{exc}\n", file=sys.stderr, flush=True)
        return 2

    setup_logging(config.log_level)
    log.info("%s v%s startet (Python %s, discord.py %s)",
             __botname__, __version__, sys.version.split()[0], discord.__version__)
    if os.getenv("_TOKEN_FORMAT_WARNING"):
        log.warning("DISCORD_BOT_TOKEN sieht ungewöhnlich aus — bitte prüfen.")

    log.info("Konfiguration:")
    for key, value in config.masked().items():
        log.info("  %-26s %s", key, value)

    store = _build_store(config)

    # Der Client wird gebaut, BEVOR der Web-Server startet. Er ist dann zwar noch
    # nicht mit Discord verbunden, aber state.client ist nie None — sonst würde
    # /api/health in den ersten Sekunden eines Deploys einen 500er liefern und
    # Render bzw. UptimeRobot melden den Dienst fälschlich als tot.
    #
    # Client und AppState verweisen aufeinander, deshalb wird der Verweis einmal
    # nach dem Erzeugen von beiden Seiten gesetzt (``wire``).
    client = RelayClient(config, store, state=None,
                         intents=build_intents(config.enable_privileged_intents))
    state = AppState(client=client, config=config, store=store)
    client.state = state
    # Neustart-Buch: zählt Exit-Code-3-Zyklen über Prozessgrenzen hinweg
    # (nur mit persistentem DATA_DIR wirklich über Neustarts hinweg).
    state.restart_ledger = RestartLedger.open(
        config.data_dir if config.persist_sessions else None
    )
    if state.restart_ledger.load_error:
        log.warning("Neustart-Buch nicht lesbar (%s) — Zähler beginnt bei 0.",
                    state.restart_ledger.load_error)

    app = build_app(state)
    runner: Optional[Any] = None
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass

    try:
        runner = await start_web_server(app, config.host, config.port)
        public = state.base_url()
        log.info("Öffentliche Basis-URL: %s", public)
        if "localhost" in public or "127.0.0.1" in public:
            log.warning(
                "⚠ Keine öffentliche URL erkannt — Arena AI kann den Link so NICHT erreichen.\n"
                "   Auf Render passiert das nicht (RENDER_EXTERNAL_URL wird automatisch gesetzt).\n"
                "   Lokal/anderswo: PUBLIC_URL=https://deine-domain.tld setzen."
            )
        log.info("  · Healthcheck : %s/api/health   ← hierhin UptimeRobot zeigen",
                 state.base_url())
        log.info("  · API-Doku    : %s/api/v1/capabilities", state.base_url())
        log.info("  · Console     : %s/console", state.base_url())

        housekeeping = asyncio.create_task(_housekeeping(state, stop), name="housekeeping")
        netcheck_task = asyncio.create_task(_startup_netcheck(config, state, stop), name="netcheck")
        bot_task = asyncio.create_task(_connect_bot(config, store, state, stop), name="bot")

        waiter = asyncio.create_task(stop.wait(), name="shutdown-signal")
        done, _pending = await asyncio.wait(
            {bot_task, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if waiter in done:
            log.info("Stop-Signal empfangen — fahre herunter …")

        exit_code = 0
        if bot_task in done and not stop.is_set():
            exc = bot_task.exception()
            if isinstance(exc, RestartRequested):
                # Bewusster Neustart-Wunsch: Die ausgehende IP ist nachweislich
                # von Cloudflare gesperrt und hat sich im Beobachtungsfenster
                # nicht erholt. Ein frischer Container = neue Chance auf eine
                # saubere IP. Exit-Code 3 unterscheidet das im Render-Log von
                # einem echten Absturz (Exit-Code 1).
                log.critical("Beende den Prozess für einen Container-Neustart: %s", exc)
                exit_code = exc.exit_code
            elif exc is not None:
                log.critical("Bot-Task beendet mit Fehler: %r", exc)
                exit_code = 1
            if exit_code:
                stop.set()
                for task in (housekeeping, netcheck_task):
                    task.cancel()
                if runner:
                    await stop_web_server(runner)
                store.save()
                return exit_code

        stop.set()
        housekeeping.cancel()
        netcheck_task.cancel()
        bot_task.cancel()
        for task in (bot_task, housekeeping, netcheck_task):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=10)
    except KeyboardInterrupt:  # pragma: no cover
        log.info("Strg+C — beende.")
    except Exception:
        log.exception("Schwerer Fehler im Hauptablauf.")
        return 1
    finally:
        if runner:
            await stop_web_server(runner)
        store.save()
        log.info("Auf Wiedersehen. 👋")

    return 0


def main() -> int:
    """Synchroner Einstiegspunkt (``python -m bot``)."""
    print(BANNER, flush=True)
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
