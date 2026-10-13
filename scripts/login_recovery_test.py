#!/usr/bin/env python3
"""
Test der Login-Wiederherstellung — **ohne Netzwerk, ohne Discord, ohne Wartezeit**.

Genau hier lag der Fehler, der den Bot dauerhaft offline hielt: Bei einem
Cloudflare-Block der ausgehenden IP (HTTP 429 / Error 1015) hat der Bot den
``Retry-After``-Wert der Blockseite geschluckt, bis zu **1800 s** gewartet und
dabei auch noch seinen eigenen Fehlerzähler zurückgesetzt („Fehlversuch 1 in
Folge" für immer). Ergebnis: ein Login-Versuch alle 30 Minuten, jeder davon
erneut in eine aktive Sperre — der Bot kam nie wieder hoch.

Dieser Test fährt die vier Fälle durch, die der neue Code unterscheiden muss:

A. IP-Sperre hebt sich      → tokenlose Proben, sofortiger Login danach
B. IP bleibt gesperrt       → ``RestartRequested`` (Exit-Code 3 = frische IP)
B2. Neustart deaktiviert    → endlos weiter probieren, kein Exit
C. IP frei, Login 429       → Token-Problem: lange warten, KEIN Neustart
D. Echtes Discord-Rate-Limit→ ``Retry-After`` wird gedeckelt (nie 1800 s)
D2. HTTP-Fehler             → Backoff eskaliert wieder (Zähler verfällt nicht)

Dazu kommen Unit-Prüfungen für die Erkennung selbst (``Via``-Header,
Cloudflare-HTML, Backoff-Rechnung, Konfigurations-Validierung, Proxy-Maskierung)
und eine Gruppe „Unit: Client-Setup", die den echten ``RelayClient`` durch
``setup_hook()`` fährt — genau der Pfad, der bei jedem Login-Versuch läuft und
in dem ein ``AttributeError`` ('add_listener' gibt es nur auf ``commands.Bot``,
nicht auf ``discord.Client``) den Bot dauerhaft offline hielt.

Die Zeit wird durch eine Fake-Uhr ersetzt: Der Test läuft in Millisekunden und
prüft trotzdem die echten Wartewerte.

Aufruf::

    python3 scripts/login_recovery_test.py            # alles
    python3 scripts/login_recovery_test.py -v         # mit Details

Exit-Code 0 = grün. Läuft in CI ohne Netzwerk und ohne Bot-Token.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time as _real_time
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISCORD_BOT_TOKEN", "recovery.test.token")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault("PUBLIC_URL", "https://relay.example.com")

import discord  # noqa: E402

from bot import main as botmain  # noqa: E402
from bot.config import Config, ConfigError, load_config, mask_proxy_url  # noqa: E402
from bot.discord_bot import (  # noqa: E402
    CID_CONNECT, CID_DISCONNECT, CID_REGENERATE, RelayClient,
    connected_view, welcome_view,
)
from bot.netcheck import (  # noqa: E402
    DISCORD_PROBE_URL,
    VERDICT_IP_BLOCKED,
    VERDICT_NETWORK_ERROR,
    VERDICT_OK,
    NetReport,
    ProbeResult,
    classify_response,
)
from bot.sessions import SessionStore  # noqa: E402
from bot.web.app import AppState  # noqa: E402

# Echte discord.py-Subklassen für die Guild-/Member-Doubles (gleiche Datei wie
# im Smoke-Test — die isinstance()-Prüfungen der Bot-Logik sollen greifen).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fake_discord import (  # noqa: E402
    FakeGuild, FakeMember, FakeRole, validate_message_payload,
)

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

PASSED: List[str] = []
FAILED: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    """Verzeichnet eine einzelne Prüfung."""
    if condition:
        PASSED.append(name)
        if VERBOSE:
            print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  ❌ {name}{(' — ' + detail) if detail else ''}")
    return bool(condition)


# ─────────────────────────────────────────────────────────────────────────────
#  Test-Doubles
# ─────────────────────────────────────────────────────────────────────────────


class FakeResponse:
    """Minimaler ``aiohttp.ClientResponse``-Ersatz für discord.py-Exceptions."""

    def __init__(self, status: int, headers: Optional[Dict[str, str]] = None,
                 reason: str = "Too Many Requests") -> None:
        self.status = status
        self.headers = headers if headers is not None else {}
        self.reason = reason


CLOUDFLARE_BODY = (
    "<html><head><title>Access denied | discord.com used Cloudflare to restrict access</title>"
    "</head><body>Error 1015 — You are being rate limited</body></html>"
)


def cloudflare_429(retry_after: Optional[str] = "3600") -> discord.HTTPException:
    """429 *ohne* Via-Header + HTML-Seite = Cloudflare-Block (Error 1015)."""
    headers = {"CF-RAY": "76714c325ede2a1e-FRA", "Server": "cloudflare"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return discord.HTTPException(FakeResponse(429, headers), CLOUDFLARE_BODY)


def discord_429(retry_after: float = 12.0) -> discord.RateLimited:
    """Echtes Discord-Rate-Limit (429 *mit* Via-Header, JSON-Body)."""
    return discord.RateLimited(retry_after)


def server_error(status: int = 500) -> discord.HTTPException:
    """Beliebiger HTTP-Fehler mit Via-Header (also von Discord, nicht Cloudflare)."""
    return discord.HTTPException(FakeResponse(status, {"Via": "1.1 google"}), "boom")


def blocked_report(ip: str = "203.0.113.7", retry_after: Optional[float] = 120.0) -> NetReport:
    """
    Report: ausgehende IP wird von Cloudflare blockiert.

    ``retry_after`` klein (Default 120 s) ⇒ normales 10-Minuten-Fenster.
    Werte oberhalb von ``BAN_FAST_RESTART_ABOVE_SECONDS`` (600 s) lösen den
    verkürzten Schnell-Neustart aus — siehe Test B4.
    """
    return NetReport(
        verdict=VERDICT_IP_BLOCKED,
        egress_ip=ip,
        egress_source="https://api.ipify.org",
        discord=ProbeResult(url=DISCORD_PROBE_URL, status=429, blocked=True,
                            cloudflare_page=True, cf_ray="deadbeef-FRA",
                            retry_after=retry_after, ok=False),
        hint="Cloudflare blockt die ausgehende IP.",
        checked_at="2026-01-01T00:00:00Z",
    )


def ok_report(ip: str = "203.0.113.7") -> NetReport:
    """Report: discord.com ist ohne Token erreichbar."""
    return NetReport(
        verdict=VERDICT_OK,
        egress_ip=ip,
        egress_source="https://api.ipify.org",
        discord=ProbeResult(url=DISCORD_PROBE_URL, status=200, ok=True),
        hint="discord.com erreichbar.",
        checked_at="2026-01-01T00:00:00Z",
    )


def dead_report() -> NetReport:
    """Report: discord.com gar nicht erreichbar (DNS/TCP)."""
    return NetReport(
        verdict=VERDICT_NETWORK_ERROR,
        discord=ProbeResult(url=DISCORD_PROBE_URL, error="ClientConnectorError: cannot connect"),
        hint="nicht erreichbar",
    )


class FakeClock:
    """
    Deterministische Uhr: ersetzt ``time.monotonic`` in :mod:`bot.main`.

    Jeder „Schlaf" im Test dreht die Uhr weiter — so lassen sich Wartewerte,
    Eskalation und Beobachtungsfenster in Millisekunden prüfen.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(float(seconds), 3))
        self.now += float(seconds)

    def __getattr__(self, name: str) -> Any:
        """Alles außer ``monotonic`` unverändert vom echten ``time``-Modul."""
        return getattr(_real_time, name)


class Scenario:
    """Zustand + Patches + gescriptetes Verhalten für einen Testlauf."""

    def __init__(self, *, reports: List[NetReport], start_behavior: Callable[[int], Any],
                 config_overrides: Optional[Dict[str, Any]] = None) -> None:
        self.clock = FakeClock()
        self.reports = list(reports)
        self.start_behavior = start_behavior
        self.start_calls = 0
        self.probe_calls = 0
        self.status_seen: List[str] = []
        self.errors_seen: List[str] = []
        self.config = Config(**{
            "discord_token": "recovery.test.token",
            "ban_probe_interval_seconds": 30.0,
            "ban_watch_seconds": 600.0,
            "restart_delay_seconds": 30.0,
            "login_retry_base_seconds": 15.0,
            "login_retry_max_seconds": 600.0,
            "fatal_retry_seconds": 300.0,
            "netcheck_timeout_seconds": 8.0,
            **(config_overrides or {}),
        })
        self.store = SessionStore(path=None, persist=False)
        self.client = RelayClient(self.config, self.store, state=None)
        self.state = AppState(client=self.client, config=self.config, store=self.store)
        self.client.state = self.state
        self.stop = asyncio.Event()
        self._patches: List[Tuple[Any, str, Any]] = []
        self._client_patches: List[Tuple[str, Any]] = []

    # ── Patches ─────────────────────────────────────────────────────────────
    def __enter__(self) -> "Scenario":
        self._patch("time", self.clock)
        self._patch("_sleep_or_stop", self._fake_sleep)
        self._patch("run_netcheck", self._fake_netcheck)
        self._client_patch("start", self._fake_start)
        self._client_patch("close", self._fake_close)
        return self

    def __exit__(self, *exc: Any) -> None:
        for target, name, old in reversed(self._patches):
            setattr(target, name, old)
        for name, old in reversed(self._client_patches):
            setattr(RelayClient, name, old)
        self._patches.clear()
        self._client_patches.clear()

    def _patch(self, name: str, value: Any) -> None:
        self._patches.append((botmain, name, getattr(botmain, name)))
        setattr(botmain, name, value)

    def _client_patch(self, name: str, value: Any) -> None:
        self._client_patches.append((name, getattr(RelayClient, name)))
        setattr(RelayClient, name, value)

    async def _fake_sleep(self, stop: asyncio.Event, seconds: float) -> None:
        # Beim Warten ist der interessante Status gesetzt (wait_and_report bzw.
        # _watch_ip_ban setzen ihn unmittelbar vorher).
        self.status_seen.append(self.state.discord_status)
        self.errors_seen.append(self.state.discord_last_error or "")
        self.clock.sleep(seconds)

    async def _fake_netcheck(self, **kwargs: Any) -> NetReport:
        self.probe_calls += 1
        return self.reports[min(self.probe_calls - 1, len(self.reports) - 1)]

    async def _fake_start(self, token: str, *, reconnect: bool = True) -> None:
        self.start_calls += 1
        result = self.start_behavior(self.start_calls)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result()
        if asyncio.iscoroutine(result):
            await result

    async def _fake_close(self) -> None:
        return None

    def finish(self) -> Callable[[], Any]:
        """Rückgabe für ``start_behavior``: beendet den Loop wie ein sauberer Start."""
        async def _inner() -> None:
            self.stop.set()
        return _inner

    # ── Lauf ────────────────────────────────────────────────────────────────
    async def run(self, *, first_report: Optional[NetReport] = None,
                  timeout: float = 5.0) -> Optional[BaseException]:
        """Führt ``_connect_bot`` aus und liefert die Exception (oder None)."""
        if first_report is not None:
            self.state.set_net_report(first_report)
        else:
            self.state.net_ready().set()
        try:
            await asyncio.wait_for(
                botmain._connect_bot(self.config, self.store, self.state, self.stop),
                timeout=timeout,
            )
        except BaseException as exc:  # noqa: BLE001 — genau die wollen wir bewerten
            return exc
        return None

    @property
    def longest_wait(self) -> float:
        return max(self.clock.slept) if self.clock.slept else 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  A: IP-Sperre hebt sich → Proben statt 30-Minuten-Schlaf
# ─────────────────────────────────────────────────────────────────────────────


async def test_ip_ban_lifts() -> None:
    print("\n── A: IP-Sperre hebt sich während der Beobachtung ───────────")

    scenario = Scenario(
        reports=[blocked_report(), blocked_report(), ok_report()],
        start_behavior=lambda call: cloudflare_429() if call == 1 else None,
    )
    scenario.start_behavior = lambda call: cloudflare_429() if call == 1 else scenario.finish()

    with scenario:
        exc = await scenario.run(first_report=ok_report())

    check("A: Loop endet ohne Fehler", exc is None, f"{exc!r}")
    check("A: nach der Sperre wurde sofort neu eingeloggt", scenario.start_calls == 2,
          f"start_calls={scenario.start_calls}")
    check("A: davor tokenlos probiert", scenario.probe_calls >= 3,
          f"probe_calls={scenario.probe_calls}")
    check("A: Beobachtungszähler gesetzt", scenario.state.ban_watches >= 1,
          f"ban_watches={scenario.state.ban_watches}")
    check("A: KEIN 1800-Sekunden-Schlaf mehr", scenario.longest_wait <= 60.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("A: kein Neustart verlangt", scenario.state.restart_requested is None)
    check("A: Status war während der Sperre ip_blocked",
          "ip_blocked" in scenario.status_seen, str(scenario.status_seen))


# ─────────────────────────────────────────────────────────────────────────────
#  B: IP bleibt gesperrt → frischer Container (neue IP) statt Endlos-Warten
# ─────────────────────────────────────────────────────────────────────────────


async def test_ip_ban_persists() -> None:
    print("\n── B: IP bleibt gesperrt → RestartRequested (Exit-Code 3) ───")

    scenario = Scenario(
        reports=[blocked_report()] * 40,
        start_behavior=lambda call: cloudflare_429(),
    )
    with scenario:
        exc = await scenario.run(first_report=blocked_report())

    check("B: RestartRequested geworfen", isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B: Exit-Code 3 (Plattform soll neu starten)",
          getattr(exc, "exit_code", None) == botmain.EXIT_CODE_RESTART,
          str(getattr(exc, "exit_code", None)))
    check("B: Beobachtungsfenster ausgeschöpft (20 Proben à 30 s)",
          scenario.probe_calls == 20, f"probe_calls={scenario.probe_calls}")
    check("B: niemals 1800 s geschlafen", scenario.longest_wait <= 60.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("B: Grund im Zustand hinterlegt",
          str(scenario.state.restart_requested or "").startswith("ip_blocked"),
          str(scenario.state.restart_requested))
    check("B: Status ip_blocked (für /api/health)",
          scenario.state.discord_status == "ip_blocked", scenario.state.discord_status)
    check("B: kein einziger Login-Versuch in die aktive Sperre",
          scenario.start_calls == 0, f"start_calls={scenario.start_calls}")


async def test_ip_ban_persists_no_restart() -> None:
    print("\n── B2: RESTART_ON_IP_BAN=false → weiter probieren, kein Exit ─")

    scenario = Scenario(
        reports=[blocked_report()] * 200,
        start_behavior=lambda call: cloudflare_429(),
        config_overrides={"restart_on_ip_ban": False},
    )
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 3 else cloudflare_429()
    )

    with scenario:
        exc = await scenario.run(first_report=blocked_report())

    check("B2: kein RestartRequested", not isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B2: mehrfach neu versucht", scenario.start_calls == 3,
          f"start_calls={scenario.start_calls}")
    check("B2: zwei Beobachtungsfenster durchprobiert", scenario.probe_calls >= 40,
          f"probe_calls={scenario.probe_calls}")
    check("B2: Neustart-Flag bleibt leer", scenario.state.restart_requested is None)
    check("B2: Abkühlphase statt Heißloop (≥ 120 s zwischen den Versuchen)",
          scenario.longest_wait >= 120.0, f"längste Wartezeit {scenario.longest_wait:.0f} s")


async def test_unlimited_watch() -> None:
    print("\n── B3: BAN_WATCH_SECONDS=0 → unbegrenzt probieren, nie neu starten ─")

    blocked, ok = blocked_report(), ok_report()
    scenario = Scenario(
        reports=[blocked] * 5 + [ok] + [blocked] * 2 + [ok] * 3,
        start_behavior=lambda call: None,
        config_overrides={"ban_watch_seconds": 0.0},
    )
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 2 else cloudflare_429()
    )

    with scenario:
        exc = await scenario.run(first_report=blocked)

    check("B3: kein RestartRequested", not isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B3: Loop endet sauber", exc is None, f"{exc!r}")
    check("B3: unbegrenzt probiert, bis die IP frei war", scenario.probe_calls == 9,
          f"probe_calls={scenario.probe_calls}")
    check("B3: danach sofort neuer Login", scenario.start_calls == 2,
          f"start_calls={scenario.start_calls}")
    check("B3: kein langer Blind-Schlaf", scenario.longest_wait <= 60.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")


async def test_fast_restart_on_long_retry_after() -> None:
    print("\n── B4: Retry-After 8034 s → verkürztes Fenster, schneller Neustart ─")

    # Exakt der gemessene Render-Fall: Cloudflare-Ray …-PDX, Retry-After 8034 s.
    long_ban = blocked_report(ip="74.220.48.143", retry_after=8034.0)
    scenario = Scenario(
        reports=[long_ban] * 40,
        start_behavior=lambda call: cloudflare_429("8034"),
    )
    with scenario:
        exc = await scenario.run(first_report=long_ban)

    check("B4: RestartRequested geworfen", isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B4: nur 3 Proben statt 20 (Fenster 90 s statt 600 s)",
          scenario.probe_calls == 3, f"probe_calls={scenario.probe_calls}")
    total = sum(scenario.clock.slept)
    check("B4: Gesamtwartezeit bis zum Neustart ≤ 150 s (90 s Proben + 30 s Delay)",
          total <= 150.0, f"{total:.0f} s")
    check("B4: kein Login-Versuch in die aktive Sperre", scenario.start_calls == 0,
          f"start_calls={scenario.start_calls}")
    check("B4: Neustart im Buch verzeichnet (Zyklus 1)",
          scenario.state.restart_ledger is not None and scenario.state.restart_ledger.cycles == 1,
          str(getattr(scenario.state.restart_ledger, "cycles", None)))
    check("B4: gesperrte IP im Buch",
          "74.220.48.143" in getattr(scenario.state.restart_ledger, "ips", []),
          str(getattr(scenario.state.restart_ledger, "ips", None)))

    # Gegenprobe: kurzer Retry-After ⇒ volles Fenster (kein Schnell-Neustart).
    short_ban = blocked_report(retry_after=300.0)
    scenario = Scenario(reports=[short_ban] * 40, start_behavior=lambda call: cloudflare_429("300"))
    with scenario:
        exc = await scenario.run(first_report=short_ban)
    check("B4: Retry-After 300 s ⇒ normales Fenster (20 Proben)",
          scenario.probe_calls == 20, f"probe_calls={scenario.probe_calls}")

    # Schnell-Neustart abgeschaltet ⇒ volles Fenster trotz langem Retry-After.
    scenario = Scenario(reports=[long_ban] * 40, start_behavior=lambda call: cloudflare_429("8034"),
                        config_overrides={"ban_fast_restart_above_seconds": 0.0})
    with scenario:
        await scenario.run(first_report=long_ban)
    check("B4: BAN_FAST_RESTART_ABOVE_SECONDS=0 ⇒ volles Fenster",
          scenario.probe_calls == 20, f"probe_calls={scenario.probe_calls}")


async def test_restart_cycle_limit() -> None:
    print("\n── B5: Neustart-Limit → aufhören zu würfeln, nur noch proben ────")
    import tempfile

    from bot.restarts import RestartLedger

    data_dir = tempfile.mkdtemp(prefix="adse-ledger-")
    ban = blocked_report(ip="74.220.48.143")
    cycles_seen: List[int] = []

    # Fünf „Container-Leben" hintereinander — jedes lädt das Buch vom letzten.
    for life in range(1, 6):
        scenario = Scenario(reports=[ban] * 40, start_behavior=lambda call: cloudflare_429(),
                            config_overrides={"restart_max_cycles": 5})
        scenario.state.restart_ledger = RestartLedger.open(data_dir)
        with scenario:
            exc = await scenario.run(first_report=ban)
        cycles_seen.append(scenario.state.restart_ledger.cycles)
        check(f"B5: Leben {life} endet mit RestartRequested",
              isinstance(exc, botmain.RestartRequested), f"{exc!r}")

    check("B5: Zyklen zählen über Prozessgrenzen hoch (1…5)", cycles_seen == [1, 2, 3, 4, 5],
          str(cycles_seen))
    check("B5: Buch liegt auf Platte",
          os.path.exists(os.path.join(data_dir, "restart_ledger.json")))

    # Sechstes Leben: Limit erreicht ⇒ KEIN Neustart mehr, nur Proben + Abkühlen.
    scenario = Scenario(reports=[ban] * 200, start_behavior=lambda call: cloudflare_429(),
                        config_overrides={"restart_max_cycles": 5})
    scenario.state.restart_ledger = RestartLedger.open(data_dir)
    ledger = scenario.state.restart_ledger
    check("B5: Buch beim Start geladen (5 Zyklen)", ledger.cycles == 5 and ledger.found_at_start,
          f"cycles={ledger.cycles} found={ledger.found_at_start}")
    check("B5: exhausted() bei 5/5", ledger.exhausted(5))

    # Zwei Beobachtungsfenster + Abkühlphasen durchlaufen lassen, dann stoppen.
    probes_before_stop = 45

    async def counting_netcheck(**kwargs: Any) -> NetReport:
        scenario.probe_calls += 1
        if scenario.probe_calls >= probes_before_stop:
            scenario.stop.set()
        return ban

    with scenario:
        botmain.run_netcheck = counting_netcheck  # type: ignore[assignment]
        exc = await scenario.run(first_report=ban)

    check("B5: nach dem Limit KEIN RestartRequested",
          not isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B5: Zähler bleibt bei 5 (kein weiterer Zyklus gebucht)", ledger.cycles == 5,
          str(ledger.cycles))
    check("B5: Plattform-Urteil gesetzt", bool(scenario.state.platform_verdict),
          str(scenario.state.platform_verdict)[:120])
    check("B5: Urteil nennt die IP und die Lösung",
          "74.220.48.143" in (scenario.state.platform_verdict or "")
          and "VPS" in (scenario.state.platform_verdict or ""),
          str(scenario.state.platform_verdict)[:200])
    check("B5: Abkühlphase ≥ 120 s zwischen den Fenstern", scenario.longest_wait >= 120.0,
          f"{scenario.longest_wait:.0f} s")
    check("B5: weiter probiert (≥ 40 Proben)", scenario.probe_calls >= 40,
          f"probe_calls={scenario.probe_calls}")
    check("B5: Status ip_blocked", scenario.state.discord_status == "ip_blocked",
          scenario.state.discord_status)
    check("B5: last_error nannte das Plattform-Limit während der Abkühlphase",
          any("Plattform-Limit" in e for e in scenario.errors_seen),
          str(scenario.errors_seen[-3:]))

    # Erfolgreicher Login ⇒ Buch wird gelöscht.
    ledger.clear(reason="Test")
    check("B5: clear() entfernt die Datei",
          not os.path.exists(os.path.join(data_dir, "restart_ledger.json")))
    check("B5: clear() setzt Zähler zurück", ledger.cycles == 0 and ledger.ips == [])

    # RESTART_MAX_CYCLES=0 ⇒ unbegrenzt.
    scenario = Scenario(reports=[ban] * 40, start_behavior=lambda call: cloudflare_429(),
                        config_overrides={"restart_max_cycles": 0})
    scenario.state.restart_ledger = RestartLedger(path=None, cycles=99)
    with scenario:
        exc = await scenario.run(first_report=ban)
    check("B5: RESTART_MAX_CYCLES=0 ⇒ trotz 99 Zyklen weiter neu starten",
          isinstance(exc, botmain.RestartRequested), f"{exc!r}")


def test_ledger_units() -> None:
    print("\n── Unit: Neustart-Buch (RestartLedger) ───────────────────────")
    import tempfile

    from bot.restarts import RestartLedger

    data_dir = tempfile.mkdtemp(prefix="adse-ledger-unit-")
    ledger = RestartLedger.open(data_dir)
    check("frisches Buch: 0 Zyklen, nicht gefunden", ledger.cycles == 0 and not ledger.found_at_start)
    check("frisches Buch: persistent unbekannt", ledger.persistent is None)
    check("note_boot_ip ohne Vergleichsbasis → None", ledger.note_boot_ip("198.51.100.1") is None)

    ledger.record_restart(egress_ip="198.51.100.1", cause="Test")
    check("record_restart zählt hoch", ledger.cycles == 1)
    check("record_restart schreibt Datei", ledger.saved_ok is True)

    reloaded = RestartLedger.open(data_dir)
    check("Reload: Zyklus 1 gelesen", reloaded.cycles == 1 and reloaded.found_at_start, str(reloaded.cycles))
    check("Reload: persistent = True", reloaded.persistent is True)
    check("Reload: letzte IP bekannt", reloaded.last_ip == "198.51.100.1")
    check("dieselbe IP nach Neustart → False", reloaded.note_boot_ip("198.51.100.1") is False)
    check("same_ip_streak = 1", reloaded.same_ip_streak == 1, str(reloaded.same_ip_streak))
    check("andere IP nach Neustart → True", reloaded.note_boot_ip("198.51.100.2") is True)
    check("streak zurück auf 0", reloaded.same_ip_streak == 0)
    check("beide IPs gemerkt", reloaded.ips == ["198.51.100.1", "198.51.100.2"], str(reloaded.ips))
    check("remaining(5) = 4", reloaded.remaining(5) == 4)
    check("remaining(0) = None (unbegrenzt)", reloaded.remaining(0) is None)
    check("exhausted(1) = True", reloaded.exhausted(1))
    data = reloaded.to_dict(5)
    check("to_dict enthält Kernfelder",
          {"cycles", "ips_seen", "history", "exhausted", "ledger_persistent"} <= set(data))
    check("summary nennt Zyklus und IPs",
          "Zyklus 1/5" in reloaded.summary(5) and "198.51.100.2" in reloaded.summary(5),
          reloaded.summary(5))

    # Verjährung: uralter Stand wird verworfen.
    import json as _json
    path = os.path.join(data_dir, "restart_ledger.json")
    with open(path, encoding="utf-8") as fh:
        raw = _json.load(fh)
    raw["updated_at"] = "2020-01-01T00:00:00Z"
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump(raw, fh)
    stale = RestartLedger.open(data_dir)
    check("verjährter Stand → 0 Zyklen, stale_at_start", stale.cycles == 0 and stale.stale_at_start)

    # Kaputte Datei darf nichts kaputt machen.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ kaputt")
    broken = RestartLedger.open(data_dir)
    check("kaputte Datei → 0 Zyklen + load_error", broken.cycles == 0 and bool(broken.load_error))

    # Ohne Pfad: rein im Speicher, save() = False, nie eine Exception.
    mem = RestartLedger(path=None)
    mem.record_restart(egress_ip=None, cause="x")
    check("Speicher-Buch zählt, speichert aber nicht", mem.cycles == 1 and mem.saved_ok is False)
    check("Speicher-Buch: persistent = False", mem.persistent is False)
    mem.clear()
    check("clear() im Speicher-Buch", mem.cycles == 0)


def test_egress_tracking_units() -> None:
    print("\n── Unit: Ausgangs-IP-Wechsel innerhalb eines Prozesses ──────")
    cfg = load_config()
    store = SessionStore(path=None, persist=False)
    client = RelayClient(cfg, store, state=None)
    state = AppState(client=client, config=cfg, store=store)

    check("erste Messung → None", state.set_net_report(blocked_report(ip="10.0.0.1")) is None)
    check("gleiche IP → False", state.set_net_report(blocked_report(ip="10.0.0.1")) is False)
    check("andere IP → True", state.set_net_report(blocked_report(ip="10.0.0.2")) is True)
    check("Wechsel gezählt", state.egress_ip_changes == 1, str(state.egress_ip_changes))
    check("beide IPs gesehen", state.egress_ips_seen == ["10.0.0.1", "10.0.0.2"],
          str(state.egress_ips_seen))
    no_ip = blocked_report(ip=None)  # type: ignore[arg-type]
    no_ip.egress_ip = None
    check("Probe ohne IP-Messung → None", state.set_net_report(no_ip) is None)
    check("… erbt die letzte bekannte IP (für /api/health)", state.egress_ip == "10.0.0.2",
          str(state.egress_ip))


# ─────────────────────────────────────────────────────────────────────────────
#  C: IP frei, Login trotzdem 429 → Token-Problem (keine neue IP nötig)
# ─────────────────────────────────────────────────────────────────────────────


async def test_token_level_rate_limit() -> None:
    print("\n── C: IP frei, Login 429 → Token-Problem, kein Neustart ─────")

    scenario = Scenario(reports=[ok_report()] * 10, start_behavior=lambda call: None)
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 2 else cloudflare_429("3600")
    )

    with scenario:
        exc = await scenario.run(first_report=ok_report())

    check("C: Loop endet ohne Fehler", exc is None, f"{exc!r}")
    check("C: KEIN Neustart (neue IP würde nichts bringen)",
          not isinstance(exc, botmain.RestartRequested) and scenario.state.restart_requested is None)
    check("C: Status rate_limited", "rate_limited" in scenario.status_seen,
          str(scenario.status_seen))
    check("C: lange Wartezeit statt Proben-Spam", scenario.longest_wait >= 300.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("C: Fehlermeldung erwähnt die erreichbare IP/das Token",
          "429" in (scenario.state.discord_last_error or ""),
          str(scenario.state.discord_last_error))


# ─────────────────────────────────────────────────────────────────────────────
#  D: Echtes Discord-Rate-Limit → Retry-After gedeckelt (alter 1800-s-Bug)
# ─────────────────────────────────────────────────────────────────────────────


async def test_real_ratelimit_is_capped() -> None:
    print("\n── D: Discord-429 mit Via-Header → Wartezeit gedeckelt ──────")

    scenario = Scenario(reports=[ok_report()] * 5, start_behavior=lambda call: None)
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 2 else discord_429(1795.0)
    )

    with scenario:
        await scenario.run(first_report=ok_report())

    check("D: riesiges Retry-After wird gedeckelt",
          scenario.longest_wait <= botmain.MAX_SINGLE_WAIT + 1,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("D: MAX_SINGLE_WAIT ist nicht mehr 1800 s", botmain.MAX_SINGLE_WAIT <= 300.0,
          str(botmain.MAX_SINGLE_WAIT))
    check("D: Status rate_limited", "rate_limited" in scenario.status_seen,
          str(scenario.status_seen))


async def test_backoff_escalates() -> None:
    print("\n── D2: Backoff eskaliert wieder (Fehlerzähler verfällt nicht) ─")

    scenario = Scenario(reports=[dead_report()] * 10, start_behavior=lambda call: None)
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 5 else server_error(500)
    )

    with scenario:
        await scenario.run(first_report=dead_report())

    waits = [w for w in scenario.clock.slept if w > 0]
    check("D2: vier Fehlversuche gezählt", scenario.state.login_failures == 4,
          str(scenario.state.login_failures))
    check("D2: Wartezeiten steigen", len(waits) >= 4 and waits[-1] > waits[0], f"{waits}")
    check("D2: BACKOFF_RESET_AFTER > MAX_SINGLE_WAIT (sonst verfällt der Zähler)",
          botmain.BACKOFF_RESET_AFTER > botmain.MAX_SINGLE_WAIT,
          f"{botmain.BACKOFF_RESET_AFTER} vs {botmain.MAX_SINGLE_WAIT}")
    check("D2: Status network_error/waiting gesetzt",
          scenario.state.discord_status in {"waiting", "network_error", "closed"},
          scenario.state.discord_status)


# ─────────────────────────────────────────────────────────────────────────────
#  E: End-to-End — kompletter Prozess inklusive Web-Server und Exit-Code
# ─────────────────────────────────────────────────────────────────────────────


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_amain_exit_code() -> None:
    """
    Fährt den **echten** Hauptablauf: Web-Server hoch, Start-Diagnose, Login-Versuch,
    Beobachtung, bewusster Neustart — und prüft den Exit-Code, den Render sieht.
    """
    print("\n── E: End-to-End über amain() → Exit-Code 3 ─────────────────")
    import logging
    import tempfile

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="adse-recovery-")
    env_backup = {k: os.environ.get(k) for k in
                  ("PORT", "HOST", "DATA_DIR", "BAN_WATCH_SECONDS", "BAN_PROBE_INTERVAL_SECONDS",
                   "RESTART_DELAY_SECONDS", "LOG_LEVEL", "PERSIST_SESSIONS")}
    os.environ.update({
        "PORT": str(port), "HOST": "127.0.0.1", "DATA_DIR": data_dir,
        "BAN_WATCH_SECONDS": "600", "BAN_PROBE_INTERVAL_SECONDS": "30",
        "RESTART_DELAY_SECONDS": "30", "LOG_LEVEL": "CRITICAL",
        "PERSIST_SESSIONS": "false",
    })

    clock = FakeClock()
    patches: List[Tuple[Any, str, Any]] = []

    def patch(target: Any, name: str, value: Any) -> None:
        patches.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    health_holder = {"body": ""}

    async def fake_sleep(stop: asyncio.Event, seconds: float) -> None:
        clock.sleep(seconds)
        # Mitten in der Beobachtungsphase (3. Schlaf) den echten Web-Server
        # abfragen: Genau dann muss /api/health die Sperre ausweisen.
        if len(clock.slept) == 3 and not health_holder["body"]:
            import aiohttp

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{port}/api/health",
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as response:
                        health_holder["body"] = await response.text()
            except Exception:  # noqa: BLE001 — Server evtl. noch nicht bereit
                health_holder["body"] = ""

    async def fake_netcheck(**kwargs: Any) -> NetReport:
        return blocked_report()

    async def fake_start(self: Any, token: str, *, reconnect: bool = True) -> None:
        raise cloudflare_429()

    async def fake_close(self: Any) -> None:
        return None

    patch(botmain, "time", clock)
    patch(botmain, "_sleep_or_stop", fake_sleep)
    patch(botmain, "run_netcheck", fake_netcheck)
    patch(RelayClient, "start", fake_start)
    patch(RelayClient, "close", fake_close)

    logging.disable(logging.CRITICAL)
    exit_code: Any = None
    try:
        # Der Ablauf ist mit der Fake-Uhr in Millisekunden durch; /api/health
        # wird aus fake_sleep heraus abgefragt, also garantiert zur Laufzeit.
        exit_code = await asyncio.wait_for(botmain.amain(), timeout=15)
    except BaseException as exc:  # noqa: BLE001
        exit_code = exc
    finally:
        logging.disable(logging.NOTSET)
        for target, name, old_value in reversed(patches):
            setattr(target, name, old_value)
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    check("E: Exit-Code 3 für den Plattform-Neustart", exit_code == 3, str(exit_code))
    health_body = health_holder["body"]
    check("E: /api/health antwortete während der Sperre", '"status":"healthy"' in health_body,
          health_body[:160])
    check("E: Health nennt discord_status=ip_blocked", '"discord_status":"ip_blocked"' in health_body,
          health_body[:300])
    check("E: Health nennt discord_reachable=false", '"discord_reachable":false' in health_body,
          health_body[:300])
    check("E: Health nennt die gesperrte Ausgangs-IP", '"egress_ip":"203.0.113.7"' in health_body,
          health_body[:300])
    check("E: kein 1800-s-Schlaf im echten Ablauf",
          (max(clock.slept) if clock.slept else 0) <= 60.0,
          f"längste Wartezeit {max(clock.slept) if clock.slept else 0:.0f} s")


# ─────────────────────────────────────────────────────────────────────────────
#  Unit-Prüfungen
# ─────────────────────────────────────────────────────────────────────────────


def test_detection_units() -> None:
    print("\n── Unit: Cloudflare-Erkennung ───────────────────────────────")

    check("429 ohne Via-Header = Cloudflare",
          botmain._is_cloudflare_ban(cloudflare_429()) is True)
    with_via_html = discord.HTTPException(FakeResponse(429, {"Via": "1.1 google"}), CLOUDFLARE_BODY)
    check("429 mit Via, aber HTML-Body = Cloudflare/Proxy",
          botmain._is_cloudflare_ban(with_via_html) is True)
    json_429 = discord.HTTPException(
        FakeResponse(429, {"Via": "1.1 google"}),
        {"message": "You are being rate limited.", "retry_after": 12.5},
    )
    check("429 mit Via + JSON = echtes Discord-Limit (kein IP-Bann)",
          botmain._is_cloudflare_ban(json_429) is False)
    check("500er ist kein Cloudflare-Bann", botmain._is_cloudflare_ban(server_error()) is False)
    check("Retry-After aus dem Header gelesen",
          botmain._extract_retry_after(cloudflare_429("1795")) == 1795.0)
    check("Retry-After aus RateLimited gelesen",
          botmain._extract_retry_after(discord_429(42.0)) == 42.0)
    check("kein Retry-After → None", botmain._extract_retry_after(json_429) is None)

    blocked, cf_page, _ = classify_response(429, {"Server": "cloudflare"}, CLOUDFLARE_BODY)
    check("classify_response erkennt Blockseite", blocked and cf_page)
    blocked, cf_page, _ = classify_response(200, {"Via": "1.1 google"}, '{"url":"wss://x"}')
    check("classify_response: 200 ist nicht blockiert", not blocked and not cf_page)
    blocked, _, _ = classify_response(429, {"Via": "1.1 google"}, '{"message":"too many"}')
    check("classify_response: Discord-429 mit Via nicht als Block gewertet", not blocked)


def test_backoff_units() -> None:
    print("\n── Unit: Backoff-Rechnung ───────────────────────────────────")
    first = [botmain._backoff_delay(15.0, 600.0, 1) for _ in range(50)]
    check("erster Versuch wartet 7,5–15 s", all(7.4 <= v <= 15.1 for v in first),
          f"min={min(first):.1f} max={max(first):.1f}")
    tenth = [botmain._backoff_delay(15.0, 600.0, 10) for _ in range(50)]
    check("zehnter Versuch bleibt unter dem Maximum", all(v <= 600.0 for v in tenth),
          f"max={max(tenth):.1f}")
    check("Jitter sorgt für Streuung", len({round(v, 3) for v in first}) > 10)


def test_config_units() -> None:
    print("\n── Unit: Konfiguration & Maskierung ─────────────────────────")
    cfg = load_config()
    check("Proxy-Default leer", cfg.discord_proxy == "")
    check("Neustart bei IP-Sperre ist Standard", cfg.restart_on_ip_ban is True)
    check("Proben-Intervall 30 s", cfg.ban_probe_interval_seconds == 30.0)
    check("Beobachtungsfenster 600 s", cfg.ban_watch_seconds == 600.0)
    check("neue Optionen erscheinen im Start-Log",
          {"ban_watch_seconds", "restart_on_ip_ban", "discord_proxy"} <= set(cfg.masked()))

    os.environ["DISCORD_PROXY"] = "http://user:geheim@proxy.example.com:3128"
    os.environ["DISCORD_PROXY_PASSWORD"] = "geheim"
    try:
        cfg = load_config()
        masked = cfg.masked()
        check("Proxy-Passwort in der URL maskiert", "geheim" not in masked["discord_proxy"],
              masked["discord_proxy"])
        check("Proxy-Passwort-Feld maskiert", "geheim" not in str(masked["discord_proxy_password"]))
        check("mask_proxy_url behält Host",
              "proxy.example.com:3128" in mask_proxy_url(cfg.discord_proxy))

        os.environ["DISCORD_PROXY"] = "socks5://proxy.example.com:1080"
        try:
            load_config()
            check("SOCKS-Proxy wird abgelehnt (aiohttp kann das nicht)", False, "keine ConfigError")
        except ConfigError:
            check("SOCKS-Proxy wird abgelehnt (aiohttp kann das nicht)", True)

        os.environ["DISCORD_PROXY"] = "http://proxy.example.com:3128"
        os.environ["BAN_PROBE_INTERVAL_SECONDS"] = "1"
        try:
            load_config()
            check("zu kleines Proben-Intervall wird abgelehnt", False, "keine ConfigError")
        except ConfigError:
            check("zu kleines Proben-Intervall wird abgelehnt", True)
    finally:
        for key in ("DISCORD_PROXY", "DISCORD_PROXY_PASSWORD", "BAN_PROBE_INTERVAL_SECONDS"):
            os.environ.pop(key, None)


def test_state_units() -> None:
    print("\n── Unit: Zustand & Health-Felder ────────────────────────────")
    cfg = load_config()
    store = SessionStore(path=None, persist=False)
    client = RelayClient(cfg, store, state=None)
    state = AppState(client=client, config=cfg, store=store)

    check("ohne Diagnose: keine Angabe", state.net_dict() == {"verdict": "not_checked"})
    check("Alter ohne Diagnose = unendlich", state.net_report_age() == float("inf"))
    state.set_net_report(blocked_report())
    data = state.net_dict()
    check("Report landet im Zustand", data["verdict"] == VERDICT_IP_BLOCKED)
    check("ausgehende IP ablesbar", state.egress_ip == "203.0.113.7")
    check("discord_reachable=false", data["discord_reachable"] is False)
    check("net_ready-Event gesetzt", state.net_ready().is_set())
    check("Cloudflare-Ray im Report", data["discord_probe"]["cloudflare_ray"] == "deadbeef-FRA")


async def test_client_setup() -> None:
    """
    Der echte ``RelayClient`` durch ``setup_hook()`` — ohne Netzwerk.

    Genau dieser Pfad lief in keinem anderen Test: ``smoke_test.py`` fährt den
    Web-Server gegen ``FakeClient``, hier wird ``client.start()`` weggepatcht.
    Deshalb fiel der ``AttributeError`` (``add_listener`` existiert nur auf
    ``commands.Bot``, nicht auf ``discord.Client``) erst auf Render auf —
    ``setup_hook()`` wird von ``login()`` aufgerufen, der Login-Loop fing den
    Crash als „Unerwarteter Fehler" und wiederholte ihn endlos.
    """
    print("\n── Unit: Client-Setup (setup_hook / on_interaction) ──────────")
    cfg = load_config()
    store = SessionStore(path=None, persist=False)
    client = RelayClient(cfg, store, state=None)
    client.state = AppState(client=client, config=cfg, store=store)

    sync_calls: List[Any] = []

    async def fake_sync(guild: Any = None) -> List[Any]:
        sync_calls.append(guild)
        return []

    client.tree.sync = fake_sync  # type: ignore[method-assign]

    # 1) setup_hook() darf nicht werfen — hier lag der AttributeError.
    await client._async_setup_hook()
    try:
        await client.setup_hook()
        setup_ok, setup_detail = True, ""
    except Exception as exc:  # noqa: BLE001 — genau das soll sichtbar werden
        setup_ok, setup_detail = False, repr(exc)
    check("setup_hook() wirft nicht", setup_ok, setup_detail)

    names = {c.name for c in client.tree.get_commands()}
    check("connect, adminpanel und manage_database sind registriert",
          names == {"connect", "adminpanel", "manage_database"}, str(sorted(names)))
    db_command = client.tree.get_command("manage_database")
    check("manage_database ist als Slash-Command vorhanden", db_command is not None)
    panel_command = client.tree.get_command("adminpanel")
    panel_contexts = getattr(panel_command, "allowed_contexts", None)
    check("adminpanel ist ausschließlich im privaten Bot-DM registriert",
          panel_contexts is not None
          and panel_contexts.guild is False
          and panel_contexts.dm_channel is True
          and panel_contexts.private_channel is False,
          str(panel_contexts))
    check("genau eine persistente Button-View",
          len(client.persistent_views) == 1, str(len(client.persistent_views)))
    check("Command-Sync wurde aufgerufen", len(sync_calls) == 1)

    # 1b) /connect ist optionenlos und antwortet als Container-V2-Nachricht.
    connect_cmd = client.tree.get_command("connect")
    check("connect hat keine Command-Optionen mehr",
          connect_cmd is not None and not connect_cmd._params,
          str(getattr(connect_cmd, "_params", None)))

    def _flatten(view: Any) -> List[Dict[str, Any]]:
        flat: List[Dict[str, Any]] = []

        def walk(items: List[Dict[str, Any]]) -> None:
            for item in items:
                flat.append(item)
                walk(item.get("components", []))

        walk(view.to_components())
        return flat

    def _texts(view: Any) -> str:
        return "\n".join(p.get("content", "") for p in _flatten(view) if p.get("type") == 10)

    def _buttons(view: Any) -> List[Dict[str, Any]]:
        return [b for p in _flatten(view) if p.get("type") == 2 for b in [p]]

    welcome = welcome_view()
    check("Willkommen ist Components V2 (Container)",
          welcome.has_components_v2() and welcome.to_components()[0]["type"] == 17)
    check("Willkommen: Titel + 'deaktiviert' + Verbinden-Button",
          "# Willkommen!" in _texts(welcome) and "deaktiviert" in _texts(welcome)
          and any(b.get("custom_id") == CID_CONNECT and b.get("style") == 3
                  for b in _buttons(welcome)))

    linked = connected_view("https://relay.example.com", "adse_UNITTEST")
    check("Verbunden ist Components V2 (Container)",
          linked.has_components_v2() and linked.to_components()[0]["type"] == 17)
    linked_text = _texts(linked)
    check("Verbunden: Prompt-Block enthält nur Verbindung",
          "URL=https://relay.example.com" in linked_text
          and "TOKEN=adse_UNITTEST" in linked_text
          and "REGELN:" not in linked_text
          and "/api/v1/capabilities" not in linked_text, linked_text[:200])
    linked_codeblocks = re.findall(r"`([^`\n]+)`", linked_text)
    check("Verbunden: Prompt ist EIN einzeiliger Codeblock (Mobile-Tap-Copy)",
          any(cb == "URL=https://relay.example.com;TOKEN=adse_UNITTEST" for cb in linked_codeblocks)
          and all(" " not in cb for cb in linked_codeblocks),
          str(linked_codeblocks))
    check("Verbunden: Arena-Link + roter Trennen + Neues Token",
          any(b.get("url") == "https://arena.ai/agent" for b in _buttons(linked))
          and any(b.get("custom_id") == CID_DISCONNECT and b.get("style") == 4
                  for b in _buttons(linked))
          and any(b.get("custom_id") == CID_REGENERATE for b in _buttons(linked)))
    check("Verbunden: unter Discords 4000-Zeichen-Limit",
          linked.content_length() < 4000, str(linked.content_length()))

    # 2) Button-Klicks: Die registrierten Buttons haben keinen Callback —
    #    sie kommen ausschließlich über das globale on_interaction an.
    hits: List[str] = []

    async def fake_regenerate(interaction: Any) -> None:
        hits.append("relay:regenerate")

    async def fake_revoke_all(interaction: Any) -> None:
        hits.append("relay:revoke_all")

    client._handle_regenerate = fake_regenerate  # type: ignore[method-assign]
    client._handle_revoke_all = fake_revoke_all  # type: ignore[method-assign]

    def fake_interaction(type_: Any, custom_id: Optional[str] = None) -> Any:
        class FakeInteraction:  # noqa: D401 — minimales Double
            pass

        it = FakeInteraction()
        it.type = type_  # type: ignore[attr-defined]
        it.data = {"custom_id": custom_id} if custom_id else None  # type: ignore[attr-defined]
        return it

    client.dispatch("interaction",
                    fake_interaction(discord.InteractionType.component, "relay:regenerate"))
    await asyncio.sleep(0.05)
    client.dispatch("interaction",
                    fake_interaction(discord.InteractionType.component, "relay:revoke_all"))
    await asyncio.sleep(0.05)
    client.dispatch("interaction",
                    fake_interaction(discord.InteractionType.application_command))
    await asyncio.sleep(0.05)

    check("dispatch liefert 'relay:regenerate' an den Handler",
          "relay:regenerate" in hits, str(hits))
    check("dispatch liefert 'relay:revoke_all' an den Handler",
          "relay:revoke_all" in hits, str(hits))
    check("application_command-Interaktion wird ignoriert",
          hits == ["relay:regenerate", "relay:revoke_all"], str(hits))

    # 3) Der Login-Loop erzeugt ab Versuch 2 pro Versuch einen neuen Client:
    #    zwei frische Clients nacheinander dürfen setup_hook() überstehen
    #    (kein CommandAlreadyRegistered o. Ä.).
    twice_ok, twice_detail = True, ""
    for _ in range(2):
        c2 = RelayClient(cfg, SessionStore(path=None, persist=False), state=None)
        c2.state = AppState(client=c2, config=cfg, store=store)
        c2.tree.sync = fake_sync  # type: ignore[method-assign]
        await c2._async_setup_hook()
        try:
            await c2.setup_hook()
        except Exception as exc:  # noqa: BLE001
            twice_ok, twice_detail = False, repr(exc)
    check("zwei frische Clients überstehen setup_hook() (Login-Loop)",
          twice_ok, twice_detail)

    # 4) Statischer Guard gegen den Rückfall: Bot-only-APIs (add_listener &
    #    Co. existieren nur auf commands.Bot) dürfen in discord_bot.py nicht
    #    wieder auftauchen — der Bot ist ein nacktes discord.Client.
    with open(os.path.join(ROOT, "bot", "discord_bot.py"), encoding="utf-8") as fh:
        source = fh.read()
    banned = re.search(
        r"self\.(add_listener|remove_listener|listen|add_cog|load_extension"
        r"|add_command|process_commands|get_context|get_command|hybrid"
        r"|command_prefix)\b",
        source,
    )
    check("keine Bot-only-API in discord_bot.py (statischer Guard)",
          banned is None, banned.group(0) if banned else "")


# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
#  Unit: Owner-Features (Presence, Adminpanel, Join/Leave-DMs)
# ─────────────────────────────────────────────────────────────────────────────


def _validate_payload(args: tuple, kwargs: Dict[str, Any], *, view: Any = None,
                      embed: Any = None) -> None:
    """
    Lässt jede simulierte Nachricht durch Discords Components-V2-Prüfung.

    ``content``/``embeds``/``attachments`` neben einem Layout-Baustein
    (Container, TextDisplay, …) quittiert Discord mit
    ``400 Bad Request (error code: 50035): Invalid Form Body`` — egal ob die
    Nachricht neu geschickt oder bearbeitet wird. Der Test wirft dann denselben
    ``discord.HTTPException``, den auch die echte API werfen würde.
    """
    payload: Dict[str, Any] = {key: kwargs[key] for key in
                               ("content", "embed", "embeds", "attachments", "files")
                               if key in kwargs}
    if args and "content" not in payload:
        payload["content"] = args[0]
    if embed is not None and "embed" not in payload:
        payload["embed"] = embed
    validate_message_payload(view=view, **payload)


class _DmCapture:
    """Ein User/Member-Double, das DMs (Views) nur einsammelt."""

    def __init__(self, uid: int, name: str) -> None:
        self.id = uid
        self.name = name
        self.mention = f"<@{uid}>"
        self.sent: List[Any] = []

    async def send(self, *args: Any, view: Any = None, **kwargs: Any) -> Any:
        _validate_payload(args, kwargs, view=view)
        self.sent.append(view)
        return None


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.modals: List[Any] = []
        self.messages: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []
        self.followups: List[Dict[str, Any]] = []
        self.embeds: List[Any] = []

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *args: Any, **kwargs: Any) -> None:
        self.deferred = True

    async def send_modal(self, modal: Any) -> None:
        self.deferred = True
        self.modals.append(modal)

    async def send_message(self, *args: Any, embed: Any = None, view: Any = None,
                           ephemeral: bool = False, **kwargs: Any) -> None:
        # Discords echte Prüfung: content/embeds + Components V2 ⇒ 400 (50035).
        _validate_payload(args, kwargs, view=view, embed=embed)
        self.deferred = True
        self.messages.append({"embed": embed, "view": view, "ephemeral": ephemeral,
                              "content": kwargs.get("content") or " ".join(str(a) for a in args)})

    async def edit_message(self, *args: Any, view: Any = None, **kwargs: Any) -> None:
        _validate_payload(args, kwargs, view=view)
        self.deferred = True
        self.edits.append({"view": view})


class _FakeFollowup:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.sends: List[Dict[str, Any]] = []

    async def send(self, *args: Any, view: Any = None, ephemeral: bool = False,
                   **kwargs: Any) -> None:
        _validate_payload(args, kwargs, view=view)
        self.sends.append({"view": view, "ephemeral": ephemeral,
                           "content": kwargs.get("content") or (args[0] if args else None)})


def _panel_interaction(user_id: int, *, guild: Any = None) -> Any:
    class _It:
        pass
    it = _It()
    it.guild = guild
    it.user = _DmCapture(user_id, f"User{user_id % 100}")
    it.response = _FakeResponse()
    it.followup = _FakeFollowup(it.response)
    return it


def _flatten_view(view: Any) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []

    def walk(items: List[Dict[str, Any]]) -> None:
        for item in items:
            flat.append(item)
            walk(item.get("components", []))

    walk(view.to_components())
    return flat


def _view_text(view: Any) -> str:
    return "\n".join(p.get("content", "") for p in _flatten_view(view) if p.get("type") == 10)


def _view_buttons(view: Any) -> List[Dict[str, Any]]:
    return [p for p in _flatten_view(view) if p.get("type") == 2]


def _bad_section_accessories(view: Any) -> List[Any]:
    """
    Discord lässt als Section-Accessory NUR Button (2) oder Thumbnail (11) zu —
    ein TextDisplay-Accessory macht die komplette Nachricht ungültig
    (400 Invalid Form Body → „Die Anwendung reagiert nicht"). Das war der Bug
    hinter dem toten Adminpanel: Server ohne Icon bekamen genau diesen
    Fallback und gingen bei JEDER Detailansicht schief.
    """
    bad: List[Any] = []
    for part in _flatten_view(view):
        if part.get("type") == 9:  # Section
            accessory = part.get("accessory") or {}
            if accessory.get("type") not in (2, 11):
                bad.append(accessory.get("type"))
    return bad


def _bad_select_option_emojis(view: Any) -> List[str]:
    """
    Discord lehnt Variation-Selector-16 (U+FE0F) in ``SelectOption.emoji`` mit
    400 "Invalid emoji" ab, obwohl dieselbe Emoji-Sequenz bei Buttons klaglos
    durchgeht (bekannte Inkonsistenz der Discord-API). Genau das brach
    /adminpanel in Produktion: sobald mindestens ein Server ohne
    Bot-Owner-Mitgliedschaft (❗️-Marker) in der Liste stand, lehnte Discord
    followup.send() mit ``options.0.emoji.name: Invalid emoji`` ab — im
    Client sichtbar als endloses "Bot denkt nach..." gefolgt von
    "Die Anwendung reagiert nicht".
    """
    bad: List[str] = []
    for part in _flatten_view(view):
        if part.get("type") == 3:  # Select-Menü
            for option in part.get("options", []):
                emoji = option.get("emoji") or {}
                name = emoji.get("name")
                if name and ("\ufe0f" in name or "\ufe0e" in name):
                    bad.append(name)
    return bad


async def test_owner_features() -> None:
    print("\n── Unit: Owner-Features (Presence / Adminpanel / DMs) ───────")
    cfg = load_config()
    store = SessionStore(path=None, persist=False)
    client = RelayClient(cfg, store, state=None)
    client.state = AppState(client=client, config=cfg, store=store)
    client.tree.sync = (lambda guild=None: _async_iter([]))  # type: ignore[method-assign]

    # ── Presence: Live-Zähler + Singular/Plural ────────────────────────────
    check("Presence: 0 Server (Plural)",
          client._presence_text() == "/connect | 👀 0 eingerichtete Server",
          client._presence_text())
    g_small = FakeGuild(gid=901, name="Klein"); g_small.member_count = 5
    g_big = FakeGuild(gid=902, name="Gross");   g_big.member_count = 900
    client._connection._guilds = {g.id: g for g in (g_small, g_big)}
    check("Presence: 2 Server (Plural)",
          client._presence_text() == "/connect | 👀 2 eingerichtete Server",
          client._presence_text())
    client._connection._guilds = {g_small.id: g_small}
    check("Presence: 1 Server (Singular 'eingerichteter')",
          client._presence_text() == "/connect | 👀 1 eingerichteter Server",
          client._presence_text())

    presence_calls: List[Any] = []

    async def fake_change_presence(*, activity: Any = None, status: Any = None) -> None:
        presence_calls.append(activity.name if activity else None)

    client.change_presence = fake_change_presence  # type: ignore[method-assign]

    # ── Join: Presence + DM an Bot-Owner + Willkommens-DM an Server-Owner ──
    bot_owner_dm = _DmCapture(cfg.bot_owner_id, "BotOwner")
    client.get_user = lambda uid: bot_owner_dm if uid == cfg.bot_owner_id else None  # type: ignore[method-assign]

    join_guild = FakeGuild(gid=903, name="Join Server"); join_guild.member_count = 777
    server_owner_dm = _DmCapture(join_guild.owner_id, "ServerOwner")
    join_guild.owner.send = server_owner_dm.send  # type: ignore[method-assign]

    # Der Gateway-Layer hätte die Guild schon im Cache — hier von Hand.
    client._connection._guilds[join_guild.id] = join_guild
    await client.on_guild_join(join_guild)
    await asyncio.sleep(0)
    check("Join aktualisiert die Presence", presence_calls == ["/connect | 👀 2 eingerichtete Server"],
          str(presence_calls))

    check("Join-DM an den Bot-Owner ging raus", len(bot_owner_dm.sent) == 1,
          str(len(bot_owner_dm.sent)))
    join_dm_text = _view_text(bot_owner_dm.sent[0]) if bot_owner_dm.sent else ""
    check("Join-DM: Components V2 + Servername + Mitglieder + Owner-Mention",
          bot_owner_dm.sent and bot_owner_dm.sent[0].has_components_v2()
          and "Server beigetreten: Join Server" in join_dm_text
          and "777" in join_dm_text
          and f"<@{join_guild.owner_id}>" in join_dm_text,
          join_dm_text[:200])
    check("Join-DM (ohne Icon) hat kein ungültiges Section-Accessory",
          bot_owner_dm.sent and _bad_section_accessories(bot_owner_dm.sent[0]) == [],
          "Section-Accessory muss Button/Thumbnail sein — sonst 400 von Discord")

    check("Willkommens-DM an den Server-Owner ging raus", len(server_owner_dm.sent) == 1,
          str(len(server_owner_dm.sent)))
    welcome_text = _view_text(server_owner_dm.sent[0]) if server_owner_dm.sent else ""
    check("Willkommens-DM: Server fett + /connect als Inline-Codebox + Verbinden",
          server_owner_dm.sent
          and "**Join Server**" in welcome_text
          and "`/connect`" in welcome_text
          and "Verbinden" in welcome_text,
          welcome_text[:200])
    check("Willkommens-DM: Container-Layout (V2)",
          server_owner_dm.sent and server_owner_dm.sent[0].has_components_v2())

    # ── Leave: Presence + DM mit Grund (Audit-Log-Fund und ehrlicher Fallback) ──
    client._connection.user = _DmCapture(222222222222222222, "BotSelf")
    del client._connection._guilds[join_guild.id]

    class _KickEntry:
        def __init__(self) -> None:
            self.action = discord.AuditLogAction.kick
            self.reason = "Ungepflegter Bot"
            self.target = _DmCapture(222222222222222222, "BotSelf")
            self.user = _DmCapture(999, "Moderator")

    class _AuditGuild(FakeGuild):
        def audit_logs(self, **kwargs: Any):  # noqa: ANN201
            async def _gen():
                yield _KickEntry()
                yield object()  # unabhängiger Eintrag, wird übersprungen
            return _gen()

    audit_guild = _AuditGuild(gid=904, name="Audit Server"); audit_guild.member_count = 42
    server_owner_dm2 = _DmCapture(audit_guild.owner_id, "ServerOwner2")
    audit_guild.owner.send = server_owner_dm2.send  # type: ignore[method-assign]
    bot_owner_dm.sent.clear()
    await client.on_guild_remove(audit_guild)
    check("Leave aktualisiert die Presence erneut", len(presence_calls) == 2
          and presence_calls[1] == "/connect | 👀 1 eingerichteter Server",
          str(presence_calls))
    check("Leave-DM an den Bot-Owner ging raus", len(bot_owner_dm.sent) == 1,
          str(len(bot_owner_dm.sent)))
    leave_text = _view_text(bot_owner_dm.sent[0]) if bot_owner_dm.sent else ""
    check("Leave-DM: Servername + Grund aus dem Audit-Log",
          "Server verlassen: Audit Server" in leave_text and "gekickt" in leave_text
          and "Ungepflegter Bot" in leave_text,
          leave_text[:200])
    check("Leave-DM (ohne Icon) hat kein ungültiges Section-Accessory",
          bot_owner_dm.sent and _bad_section_accessories(bot_owner_dm.sent[0]) == [],
          "Section-Accessory muss Button/Thumbnail sein — sonst 400 von Discord")

    # Ohne Audit-Log-Fund: ehrlicher Fallback statt erfundenem Grund.
    fallback_guild = FakeGuild(gid=905, name="Fallback Server")
    bot_owner_dm.sent.clear()
    await client.on_guild_remove(fallback_guild)
    leave_fallback = _view_text(bot_owner_dm.sent[0]) if bot_owner_dm.sent else ""
    check("Leave-DM ohne Audit-Zugriff: ehrlicher Fallback-Grund",
          "nicht ermittelbar" in leave_fallback and "Audit-Log" in leave_fallback,
          leave_fallback[:200])

    # ── Leave-Grund LIVE per Gateway (Audit-Log-Event, solange Bot Mitglied ist) ──
    kick_guild = FakeGuild(gid=907, name="Kick Server"); kick_guild.member_count = 12

    class _LiveKickEntry:
        action = discord.AuditLogAction.kick
        reason = "Bot-Regeln verletzt"
        target = _DmCapture(222222222222222222, "BotSelf")
        user = _DmCapture(4242, "Moderator Klaus")
        guild = kick_guild

    bot_owner_dm.sent.clear()
    await client.on_audit_log_entry_create(_LiveKickEntry())
    await client.on_guild_remove(kick_guild)
    leave_live = _view_text(bot_owner_dm.sent[0]) if bot_owner_dm.sent else ""
    check("Live-Kick: Grund + Ausführender aus dem Gateway-Event in der Leave-DM",
          "gekickt" in leave_live and "Bot-Regeln verletzt" in leave_live
          and "Moderator Klaus" in leave_live, leave_live[:300])
    check("Live-Kick: Aufräumen-Info in der DM (Sessions gestoppt, DB bereinigt)",
          "Aufräumen" in leave_live and "gestoppt" in leave_live, leave_live[:300])

    # Ban OHNE Grund (GUILD_BAN_ADD liefet keinen Grund, Audit-Eintrag blieb aus):
    # zumindest „gebannt — kein Grund angegeben“ statt „nicht ermittelbar“.
    ban_guild = FakeGuild(gid=908, name="Ban Server"); ban_guild.member_count = 8
    bot_owner_dm.sent.clear()
    await client.on_member_ban(ban_guild, _DmCapture(222222222222222222, "BotSelf"))
    await client.on_guild_remove(ban_guild)
    leave_ban = _view_text(bot_owner_dm.sent[0]) if bot_owner_dm.sent else ""
    check("Ban ohne Grund: 'gebannt — kein Grund angegeben'",
          "gebannt" in leave_ban and "kein Grund angegeben" in leave_ban, leave_ban[:300])

    # Audit-Event für einen ANDEREN Nutzer (nicht den Bot) darf nichts hinterlegen.
    other_kick_guild = FakeGuild(gid=909, name="Other Kick"); other_kick_guild.member_count = 3

    class _OtherKickEntry:
        action = discord.AuditLogAction.kick
        reason = "geht den Bot nichts an"
        target = _DmCapture(111111111111111111, "SomeoneElse")
        user = _DmCapture(4242, "Moderator Klaus")
        guild = other_kick_guild

    bot_owner_dm.sent.clear()
    await client.on_audit_log_entry_create(_OtherKickEntry())
    await client.on_guild_remove(other_kick_guild)
    leave_other = _view_text(bot_owner_dm.sent[0]) if bot_owner_dm.sent else ""
    check("Kick eines anderen Mitglieds ändert den Leave-Grund nicht",
          "geht den Bot nichts an" not in leave_other
          and "nicht ermittelbar" in leave_other, leave_other[:300])

    # ── Adminpanel: Command-Guards (DM-only + Owner-only) ──────────────────
    await client._async_setup_hook()   # Loop & Interna (macht sonst login())
    await client.setup_hook()          # Commands + persistente View
    panel_cmd = client.tree.get_command("adminpanel")
    check("adminpanel-Command ist registriert", panel_cmd is not None)

    guild_it = _panel_interaction(cfg.bot_owner_id, guild=g_small)
    await panel_cmd._do_call(guild_it, {})  # type: ignore[arg-type]
    check("adminpanel im Server-Channel → Abweisung 'Nur im Privatchat'",
          len(guild_it.response.messages) == 1
          and guild_it.response.messages[0]["embed"] is not None
          and "Privatchat" in getattr(guild_it.response.messages[0]["embed"], "title", ""),
          str([getattr(m["embed"], "title", "") for m in guild_it.response.messages]))

    stranger_it = _panel_interaction(123456789)  # DM, aber nicht der Owner
    await panel_cmd._do_call(stranger_it, {})  # type: ignore[arg-type]
    check("adminpanel von Fremden → Abweisung 'Nur für den Bot-Owner'",
          len(stranger_it.response.messages) == 1
          and "Bot-Owner" in getattr(stranger_it.response.messages[0]["embed"], "title", ""),
          str([getattr(m["embed"], "title", "") for m in stranger_it.response.messages]))

    # Owner im Privatchat: Panel mit sortierter Liste.
    guilds = []
    for i, (name, count, owner_in) in enumerate(
        [("Zeta", 100, True), ("Alpha", 900, False), ("Mid", 300, True), ("Tiny", 2, False)]
    ):
        g = FakeGuild(gid=1000 + i, name=name)
        g.member_count = count
        guilds.append(g)
        if owner_in:
            admin_role = FakeRole(5000 + i, "Admin", 9, discord.Permissions(administrator=True).value)
            g.members.append(FakeMember(cfg.bot_owner_id, "Owner", [g.default_role, admin_role], guild=g))
    client._connection._guilds = {g.id: g for g in guilds}
    owner_it = _panel_interaction(cfg.bot_owner_id)
    await panel_cmd._do_call(owner_it, {})  # type: ignore[arg-type]
    check("adminpanel: Owner bekommt followup (nach defer)", len(owner_it.followup.sends) == 1,
          str(len(owner_it.followup.sends)))
    panel = owner_it.followup.sends[0]["view"] if owner_it.followup.sends else None
    panel_text = _view_text(panel) if panel is not None else ""
    check("Panel ist Container V2", panel is not None and panel.has_components_v2())
    check("Panel (❗️-Server dabei): Select-Option-Emoji ohne Variation-Selector",
          panel is not None and _bad_select_option_emojis(panel) == [],
          str([n.encode("unicode_escape") for n in _bad_select_option_emojis(panel)]) if panel is not None else "kein Panel")
    check("Panel hat einen Schließen-Button",
          any(b.get("label") == "Schließen" and "close" in b.get("custom_id", "")
              for b in _view_buttons(panel)),
          str([b.get("label") for b in _view_buttons(panel)]))
    check("Panel: ❗️-Server (Owner fehlt) stehen ganz oben — nach Mitgliedern sortiert",
          panel_text.find("❗️ **Alpha**") < panel_text.find("❗️ **Tiny**")
          < panel_text.find("**Mid**") < panel_text.find("**Zeta**"),
          panel_text[:300])
    check("Panel: deutsche Zahlenformate (900 statt 900)", "900" in panel_text.replace(".", ""),
          panel_text[:100])
    check("Panel: Seiten-Fußzeile", "Seite 1/1" in panel_text and "4 Server gesamt" in panel_text,
          panel_text[:100])

    # Blättern: nav-Button über on_interaction (wie nach einem Neustart).
    async def fake_show(interaction: Any, view: Any) -> None:
        interaction.response.edits.append({"view": view})

    client._show = fake_show  # type: ignore[method-assign]
    nav_it = _panel_interaction(cfg.bot_owner_id)
    nav_it.data = {"custom_id": "relay:admin:nav:0:"}
    nav_it.type = discord.InteractionType.component
    client.dispatch("interaction", nav_it)
    await asyncio.sleep(0.05)
    check("nav-Button bearbeitet das Panel (via on_interaction)",
          len(nav_it.response.edits) == 1, str(len(nav_it.response.edits)))

    # Suche-Button öffnet das Modal.
    search_it = _panel_interaction(cfg.bot_owner_id)
    search_it.data = {"custom_id": "relay:admin:search"}
    search_it.type = discord.InteractionType.component
    client.dispatch("interaction", search_it)
    await asyncio.sleep(0.05)
    check("Suche-Button öffnet das Suchfenster (Modal)",
          len(search_it.response.modals) == 1
          and search_it.response.modals[0].title == "🔍 Server suchen",
          str([getattr(m, "title", "?") for m in search_it.response.modals]))

    # Schließen-Button bestätigt den Klick und löscht die Panel-DM.
    class _DeletableMessage:
        deleted = False

        async def delete(self) -> None:
            self.deleted = True

    close_it = _panel_interaction(cfg.bot_owner_id)
    close_it.message = _DeletableMessage()
    await client._handle_admin_button(close_it, "relay:admin:close:0:")
    check("Schließen-Button entfernt das Adminpanel",
          close_it.response.deferred and close_it.message.deleted)

    # Modal-Submit: Filter greift, Liste startet bei Seite 1.
    modal = search_it.response.modals[0]
    modal.query._value = "Zeta"  # setValue geht nur über den Refresh-Pfad
    submit_it = _panel_interaction(cfg.bot_owner_id)
    await modal.on_submit(submit_it)
    check("Modal-Submit antwortet mit gefiltertem Panel",
          len(submit_it.response.messages) == 1, str(len(submit_it.response.messages)))
    result_text = _view_text(submit_it.response.messages[0]["view"]) if submit_it.response.messages else ""
    check("Gefiltertes Panel zeigt nur den Treffer",
          "**Zeta**" in result_text and "**Alpha**" not in result_text
          and "Suche: „Zeta“" in result_text,
          result_text[:200])

    # Gefiltertes Panel zeigt Treffer.
    check("Gefiltertes Panel zeigt nur den Treffer",
          "**Zeta**" in result_text and "**Alpha**" not in result_text
          and "Suche: „Zeta“" in result_text,
          result_text[:200])

    # ── Dropdown-Serverauswahl & Serverdetailansicht ─────────────────────────
    panel_selects = [p for p in _flatten_view(panel) if p.get("type") == 3]
    check("Panel: Dropdown mit Servern der aktuellen Seite vorhanden",
          len(panel_selects) == 1 and len(panel_selects[0].get("options", [])) == 4,
          str(len(panel_selects)))

    # Dropdown-Auswahl -> Server-Übersicht (Detailansicht):
    select_it = _panel_interaction(cfg.bot_owner_id)
    select_it.data = {"custom_id": "relay:admin:select:0:", "values": ["1000"]}  # Zeta
    select_it.type = discord.InteractionType.component
    client.dispatch("interaction", select_it)
    await asyncio.sleep(0.05)
    check("Dropdown-Auswahl öffnet Server-Übersicht (via on_interaction)",
          len(select_it.response.edits) == 1, str(len(select_it.response.edits)))
    detail_view = select_it.response.edits[0]["view"] if select_it.response.edits else None
    detail_text = _view_text(detail_view) if detail_view else ""
    check("Server-Übersicht ist Container V2", detail_view is not None and detail_view.has_components_v2())
    check("Server-Übersicht (ohne Icon) hat kein ungültiges Section-Accessory",
          detail_view is not None and _bad_section_accessories(detail_view) == [],
          "Section-Accessory muss Button/Thumbnail sein — sonst 400 → 'reagiert nicht'")
    check("Server-Übersicht: Servername, ID, Mitglieder, Status",
          "Zeta" in detail_text and "1000" in detail_text and "100" in detail_text
          and "Du bist Mitglied" in detail_text, detail_text[:200])
    check("Server-Übersicht: inaktive KI-Verbindung",
          "Keine aktiven Tokens" in detail_text or "nicht verbunden" in detail_text,
          detail_text[:300])
    detail_btn_labels = [b.get("label") for b in _view_buttons(detail_view)] if detail_view else []
    check("Server-Übersicht: Buttons 'Zurück', 'Einladung erstellen', 'Server verlassen'",
          "Zurück" in detail_btn_labels and "Einladung erstellen" in detail_btn_labels
          and "Server verlassen" in detail_btn_labels, str(detail_btn_labels))

    # Server-Übersicht mit aktiver Sitzung:
    sess, token = await store.create(
        guild_id=1000, guild_name="Zeta", created_by=cfg.bot_owner_id,
        created_by_name="Owner", mode="rw", ttl_hours=24,
    )
    select_it_active = _panel_interaction(cfg.bot_owner_id)
    select_it_active.data = {"custom_id": "relay:admin:select:0:", "values": ["1000"]}
    select_it_active.type = discord.InteractionType.component
    client.dispatch("interaction", select_it_active)
    await asyncio.sleep(0.05)
    detail_active_text = _view_text(select_it_active.response.edits[0]["view"]) if select_it_active.response.edits else ""
    check("Server-Übersicht zeigt aktive Verbindung mit Token-Präfix & Ablauf",
          "Aktive KI-Verbindung" in detail_active_text and sess.token_prefix in detail_active_text
          and "Requests" in detail_active_text, detail_active_text[:300])

    # Einladung erstellen:
    invite_it = _panel_interaction(cfg.bot_owner_id)
    invite_it.data = {"custom_id": "relay:admin:invite:1000:0:"}
    invite_it.type = discord.InteractionType.component
    client.dispatch("interaction", invite_it)
    await asyncio.sleep(0.05)
    check("Einladung erstellen generiert Invite-URL im Panel",
          len(invite_it.response.edits) == 1, str(len(invite_it.response.edits)))
    invite_panel_text = _view_text(invite_it.response.edits[0]["view"]) if invite_it.response.edits else ""
    check("Invite-URL wird im Panel angezeigt",
          "discord.gg" in invite_panel_text and "Einladungslink" in invite_panel_text,
          invite_panel_text[:300])

    # Server verlassen: Schritt 1 - Bestätigungsansicht
    leave_ask_it = _panel_interaction(cfg.bot_owner_id)
    leave_ask_it.data = {"custom_id": "relay:admin:leave:1000:0:"}
    leave_ask_it.type = discord.InteractionType.component
    client.dispatch("interaction", leave_ask_it)
    await asyncio.sleep(0.05)
    check("Server verlassen öffnet Bestätigungsdialog (Confirm-Schritt)",
          len(leave_ask_it.response.edits) == 1, str(len(leave_ask_it.response.edits)))
    confirm_view = leave_ask_it.response.edits[0]["view"] if leave_ask_it.response.edits else None
    confirm_text = _view_text(confirm_view) if confirm_view else ""
    check("Bestätigungsansicht fragt nach",
          "Server wirklich verlassen" in confirm_text and "Zeta" in confirm_text, confirm_text[:200])

    # Server verlassen: Schritt 2 - Ausführen (Confirm)
    leave_confirm_it = _panel_interaction(cfg.bot_owner_id)
    leave_confirm_it.data = {"custom_id": "relay:admin:leave_confirm:1000:0:"}
    leave_confirm_it.type = discord.InteractionType.component
    client.dispatch("interaction", leave_confirm_it)
    await asyncio.sleep(0.05)
    check("Server verlassen führt guild.leave() aus und aktualisiert Liste",
          len(leave_confirm_it.response.edits) == 1, str(len(leave_confirm_it.response.edits)))
    after_leave_text = _view_text(leave_confirm_it.response.edits[0]["view"]) if leave_confirm_it.response.edits else ""
    check("Verlassener Server ist nicht mehr in der Liste",
          "**Zeta**" not in after_leave_text and "3 Server gesamt" in after_leave_text,
          after_leave_text[:300])
    check("FakeGuild hat leave-Mutation protokolliert",
          "guild.leave" in guilds[0].mutations, str(guilds[0].mutations))

    # Custom-ID-Roundtrip + Grenzen.
    from bot.discord_bot import _admin_custom_id, _admin_guild_custom_id, _parse_admin_custom_id
    parsed = _parse_admin_custom_id(_admin_custom_id("nav", 3, "Gaming"))
    check("Admin-Custom-ID: Seite + Suche überleben den Roundtrip",
          parsed == {"action": "nav", "page": 3, "query": "Gaming"}, str(parsed))
    parsed_guild = _parse_admin_custom_id(_admin_guild_custom_id("guild", 1000, 2, "Test"))
    check("Admin-Custom-ID: Guild-ID + Seite + Suche überleben den Roundtrip",
          parsed_guild == {"action": "guild", "guild_id": 1000, "page": 2, "query": "Test"}, str(parsed_guild))
    parsed_invite = _parse_admin_custom_id(_admin_guild_custom_id("invite", 1000, 0, ""))
    check("Admin-Custom-ID: Invite-Aktion überlebt den Roundtrip",
          parsed_invite == {"action": "invite", "guild_id": 1000, "page": 0, "query": ""}, str(parsed_invite))
    parsed_leave = _parse_admin_custom_id(_admin_guild_custom_id("leave_confirm", 1000, 1, "abc"))
    check("Admin-Custom-ID: LeaveConfirm-Aktion überlebt den Roundtrip",
          parsed_leave == {"action": "leave_confirm", "guild_id": 1000, "page": 1, "query": "abc"}, str(parsed_leave))
    check("Admin-Custom-ID bleibt unter Discords 100-Zeichen-Limit",
          len(_admin_guild_custom_id("leave_confirm", 9876543210123456789, 999, "x" * 40)) <= 100,
          str(len(_admin_guild_custom_id("leave_confirm", 9876543210123456789, 999, "x" * 40))))

    # ── Blauer Wiederverwenden-Button: End-to-End über on_interaction ─────────
    # Admin richtet auf Server A eine DB ein → klickt auf Server B auf
    # „♻️ DB von „QuellServer“ nutzen“ → Server B ist mit derselben DB verbunden.
    import tempfile
    import shutil as _shutil
    _reuse_dir = tempfile.mkdtemp(prefix="adse_reuse_")
    try:
        guild_a = FakeGuild(gid=1100, name="QuellServer")
        guild_b = FakeGuild(gid=1101, name="ZielServer")
        client._connection._guilds[guild_a.id] = guild_a
        client._connection._guilds[guild_b.id] = guild_b
        srv = client.state.turso_service
        ok, msg = await srv.connect_guild_database(
            guild_id=guild_a.id, guild_name=guild_a.name,
            database_url=f"sqlite:///{_reuse_dir}/shared.db", auth_token="",
            configured_by=f"Owner ({cfg.bot_owner_id})", configured_by_id=str(cfg.bot_owner_id),
        )
        check("Reuse-Setup: Quell-Server-Datenbank eingerichtet", ok, msg)

        admin_role_b = FakeRole(6000, "Admin", 9, discord.Permissions(administrator=True).value)
        admin_b = FakeMember(cfg.bot_owner_id, "Owner", [guild_b.default_role, admin_role_b], guild=guild_b)
        reuse_it = _panel_interaction(cfg.bot_owner_id, guild=guild_b)
        reuse_it.user = admin_b
        reuse_it.data = {"custom_id": f"relay:v2_db_reuse:{guild_a.id}"}
        reuse_it.type = discord.InteractionType.component
        reuse_error: Optional[str] = None
        try:
            await client.on_interaction(reuse_it)
        except discord.HTTPException as exc:  # genau der gemeldete 400er
            reuse_error = str(exc)
        check("Blauer Button: Discord nimmt die Antwort an (kein 400/50035)",
              reuse_error is None, str(reuse_error))
        check("Blauer Button: Erfolg mit konfiguriertem Status-View",
              len(reuse_it.response.messages) == 1
              and reuse_it.response.messages[0]["view"] is not None
              and "Status & Auslastung" in _view_text(reuse_it.response.messages[0]["view"]),
              str(len(reuse_it.response.messages)))
        check("Blauer Button: Ziel-Server nutzt dieselbe Datenbank",
              await srv.is_guild_db_configured(guild_b.id))

        # ── Regression: „content" neben Components V2 ⇒ 400 (50035) ──────────
        # Genau das war der Fehler bei der Datenbank-Einrichtung: Die
        # Erfolgsmeldung „✅ …" wurde als ``content`` ZUSAMMEN mit dem
        # Container-View geschickt. Discord lehnt das ab („The 'content' field
        # cannot be used when using MessageFlags.IS_COMPONENTS_V2"), obwohl die
        # Datenbank längst verknüpft war — der Admin sah nur ⚠️ Fehler.
        reuse_msg = reuse_it.response.messages[0] if reuse_it.response.messages else {}
        reuse_text = _view_text(reuse_msg["view"]) if reuse_msg.get("view") is not None else ""
        check("Blauer Button: KEIN content neben dem Container-View",
              not str(reuse_msg.get("content") or "").strip(),
              repr(reuse_msg.get("content")))
        check("Blauer Button: ✅-Erfolgszeile steht als TextDisplay IM Container",
              "✅" in reuse_text and "Status & Auslastung" in reuse_text
              and reuse_text.index("✅") < reuse_text.index("Status & Auslastung"),
              reuse_text[:200])

        # ── Modal „Turso-Datenbank verknüpfen" (der Weg des Nutzers) ─────────
        from bot.discord_bot import TursoConnectModal, database_configured_view

        guild_c = FakeGuild(gid=1102, name="ModalServer")
        client._connection._guilds[guild_c.id] = guild_c
        admin_role_c = FakeRole(6002, "Admin", 9, discord.Permissions(administrator=True).value)
        admin_c = FakeMember(cfg.bot_owner_id, "Owner",
                             [guild_c.default_role, admin_role_c], guild=guild_c)

        modal = TursoConnectModal(client, guild_c)
        modal.db_url._value = f"sqlite:///{_reuse_dir}/modal.db"
        modal.auth_token._value = "adse_modal_token_0123456789"
        modal_it = _panel_interaction(cfg.bot_owner_id, guild=guild_c)
        modal_it.user = admin_c
        modal_error: Optional[str] = None
        try:
            await modal.on_submit(modal_it)
        except discord.HTTPException as exc:  # genau der gemeldete 400er
            modal_error = str(exc)
        check("Modal-Submit: Discord nimmt die Antwort an (kein 400/50035)",
              modal_error is None, str(modal_error))
        check("Modal-Submit: deferred + genau ein Followup",
              modal_it.response.deferred and len(modal_it.followup.sends) == 1,
              str(len(modal_it.followup.sends)))
        sent = modal_it.followup.sends[0] if modal_it.followup.sends else {}
        sent_view = sent.get("view")
        sent_text = _view_text(sent_view) if sent_view is not None else ""
        check("Modal-Submit: Datenbank ist verknüpft (gültige Daten ⇒ Erfolg)",
              await srv.is_guild_db_configured(guild_c.id))
        check("Modal-Submit: KEIN content neben Components V2 (sonst 400/50035)",
              not str(sent.get("content") or "").strip()
              and sent_view is not None and sent_view.has_components_v2(),
              repr(sent.get("content")))
        check("Modal-Submit: ✅-Bestätigung + Statusansicht im Container",
              "✅" in sent_text and "Turso-Datenbank verbunden" in sent_text
              and "Status & Auslastung" in sent_text, sent_text[:300])
        check("Modal-Submit: Antwort ist ephemeral", bool(sent.get("ephemeral")))
        container_kids = (_flatten_view(sent_view)[0].get("components", [])
                          if sent_view is not None and _flatten_view(sent_view) else [])
        check("Modal-Submit: Container bleibt unter Discords 10-Kinder-Limit",
              0 < len(container_kids) <= 10, str(len(container_kids)))

        # Ungültige Zugangsdaten ⇒ klare ❌-Meldung als Text (kein V2-View).
        bad_modal = TursoConnectModal(client, guild_c)
        bad_modal.db_url._value = f"sqlite:///{_reuse_dir}/gibt-es-nicht/db.sqlite"
        bad_modal.auth_token._value = "adse_modal_token_0123456789"
        bad_it = _panel_interaction(cfg.bot_owner_id, guild=guild_c)
        bad_it.user = admin_c
        try:
            await bad_modal.on_submit(bad_it)
        except discord.HTTPException as exc:
            check("Modal-Submit (kaputte URL): keine 400er-Ablehnung", False, str(exc))
        bad_sent = bad_it.followup.sends[0] if bad_it.followup.sends else {}
        check("Modal-Submit (kaputte URL): ❌-Meldung ohne View",
              str(bad_sent.get("content") or "").startswith("❌")
              and bad_sent.get("view") is None,
              str(bad_sent)[:200])

        # ── Die Ansicht selbst: notice wandert in den Container ──────────────
        plain = database_configured_view({"database_url": "demo.turso.io"})
        with_notice = database_configured_view(
            {"database_url": "demo.turso.io"}, notice="✅ Verbunden!",
        )
        check("database_configured_view ohne notice: kein leerer Hinweis-Block",
              "✅ Verbunden" not in _view_text(plain)
              and _view_text(plain).startswith("# 🗄️"), _view_text(plain)[:80])
        check("database_configured_view mit notice: Hinweis steht VOR dem Status",
              _view_text(with_notice).startswith("✅ Verbunden!")
              and _view_text(with_notice).index("✅") < _view_text(with_notice).index("Status &"),
              _view_text(with_notice)[:120])
        check("database_configured_view: Buttons bleiben vollständig",
              {b.get("custom_id") for b in _view_buttons(with_notice)}
              == {"relay:v2_db_systems", "relay:v2_db_refresh",
                  "relay:v2_db_connect", "relay:v2_db_disconnect"},
              str([b.get("custom_id") for b in _view_buttons(with_notice)]))

        # ── Selbsttest der Simulation: wirft sie wirklich wie Discord? ───────
        def _v2_error(**payload: Any) -> Optional[str]:
            try:
                validate_message_payload(view=with_notice, **payload)
            except discord.HTTPException as exc:
                return str(exc)
            return None

        err = _v2_error(content="✅ Fertig")
        check("Simulation: content + Components V2 ⇒ exakt Discords 400/50035",
              err is not None and "(error code: 50035)" in err
              and "In content: The 'content' field cannot be used when using "
              "MessageFlags.IS_COMPONENTS_V2" in err, str(err))
        check("Simulation: embeds + Components V2 ⇒ ebenfalls 400/50035",
              (_v2_error(embeds=[discord.Embed(title="x")]) or "").startswith("400"))
        check("Simulation: leere Legacy-Felder (content=None, embeds=[]) sind erlaubt",
              _v2_error(content=None, embeds=[], attachments=[]) is None)

        v1_view = discord.ui.LayoutView()
        v1_view.add_item(discord.ui.ActionRow(
            discord.ui.Button(label="Link", style=discord.ButtonStyle.link, url="https://arena.ai"),
        ))
        try:
            validate_message_payload(view=v1_view, content="Nur Action Rows ⇒ V1")
            v1_ok = True
        except discord.HTTPException:
            v1_ok = False
        check("Simulation: LayoutView mit reinen Action Rows bleibt V1 (content erlaubt)",
              v1_ok)

        # Ein fremder Admin darf den Button nicht für sich nutzen:
        admin_role_x = FakeRole(6001, "Admin", 9, discord.Permissions(administrator=True).value)
        admin_x = FakeMember(987654321, "FremdAdmin", [guild_b.default_role, admin_role_x], guild=guild_b)
        deny_it = _panel_interaction(987654321, guild=guild_b)
        deny_it.user = admin_x
        deny_it.data = {"custom_id": f"relay:v2_db_reuse:{guild_a.id}"}
        deny_it.type = discord.InteractionType.component
        await client.on_interaction(deny_it)
        check("Blauer Button: fremder Admin wird abgewiesen (ephemeral Fehlermeldung)",
              len(deny_it.response.messages) == 1
              and "anderen Admin" in (deny_it.response.messages[0].get("content") or ""),
              str(deny_it.response.messages))
    finally:
        _shutil.rmtree(_reuse_dir, ignore_errors=True)

    # Kein Owner konfiguriert → gar nichts tun (kein Crash, keine DM).
    cfg_no_owner = Config(discord_token="x", bot_owner_id=0)
    client2 = RelayClient(cfg_no_owner, SessionStore(path=None, persist=False), state=None)
    client2.state = AppState(client=client2, config=cfg_no_owner,
                             store=SessionStore(path=None, persist=False))
    noowner_guild = FakeGuild(gid=906, name="NoOwner")
    nobody = _DmCapture(1, "nobody")
    noowner_guild.owner.send = nobody.send  # type: ignore[method-assign]
    noowner_calls: List[Any] = []

    async def fake_change_presence2(*, activity: Any = None, status: Any = None) -> None:
        noowner_calls.append(activity.name if activity else None)

    client2.change_presence = fake_change_presence2  # type: ignore[method-assign]
    await client2.on_guild_join(noowner_guild)
    await client2.on_guild_remove(noowner_guild)
    # BOT_OWNER_ID=0 schaltet nur die Bot-Owner-Features aus — die Willkommens-DM
    # an den SERVER-Owner ist ein Kern-Feature und geht trotzdem raus.
    check("BOT_OWNER_ID=0: Willkommens-DM geht trotzdem raus", len(nobody.sent) == 1,
          str(len(nobody.sent)))
    check("BOT_OWNER_ID=0: keine Owner-Benachrichtigungen (Join UND Leave)",
          len(nobody.sent) == 1 and "Hey! 👋" in _view_text(nobody.sent[0]),
          _view_text(nobody.sent[0])[:80] if nobody.sent else "")

    await client.close()
    await client2.close()


def _async_iter(items: List[Any]) -> Any:
    async def _coro() -> List[Any]:
        return items
    return _coro()


async def arun() -> int:
    print("🔁 Test der Login-Wiederherstellung (Cloudflare 429 / Error 1015)")
    test_detection_units()
    test_backoff_units()
    test_config_units()
    test_state_units()
    await test_client_setup()
    await test_owner_features()
    test_ledger_units()
    test_egress_tracking_units()
    await test_ip_ban_lifts()
    await test_ip_ban_persists()
    await test_ip_ban_persists_no_restart()
    await test_unlimited_watch()
    await test_fast_restart_on_long_retry_after()
    await test_restart_cycle_limit()
    await test_token_level_rate_limit()
    await test_real_ratelimit_is_capped()
    await test_backoff_escalates()
    await test_amain_exit_code()

    print("\n" + "═" * 64)
    print(f"  ✅ {len(PASSED)} bestanden     ❌ {len(FAILED)} fehlgeschlagen")
    print("═" * 64)
    if FAILED:
        print("\nFehlgeschlagen:")
        for item in FAILED:
            print(f"  · {item}")
        return 1
    return 0


def main() -> int:
    try:
        return asyncio.run(arun())
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
