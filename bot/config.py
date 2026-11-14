"""Zentrale Konfiguration — alles läuft über Umgebungsvariablen (12-Factor)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ("Config", "load_config", "ConfigError", "mask_proxy_url")

_TRUE = {"1", "true", "yes", "y", "on", "ja", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "nein", "disable", "disabled"}


class ConfigError(RuntimeError):
    """Wird geworfen, wenn eine Pflicht-Umgebungsvariable fehlt/ungültig ist."""


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _int(key: str, default: int) -> int:
    raw = _str(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Umgebungsvariable {key} muss eine ganze Zahl sein (Wert: {raw!r}).") from None


def _float(key: str, default: float) -> float:
    raw = _str(key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"Umgebungsvariable {key} muss eine Zahl sein (Wert: {raw!r}).") from None


def _bool(key: str, default: bool) -> bool:
    raw = _str(key, "").lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"Umgebungsvariable {key} muss true/false sein (Wert: {raw!r}).")


def _list(key: str, default: Optional[List[str]] = None) -> List[str]:
    raw = _str(key, "")
    if not raw:
        return list(default or [])
    return [part.strip() for part in re.split(r"[,;\s]+", raw) if part.strip()]


def _clean_url(url: str) -> str:
    """ trailing Slash entfernen, http(s) sicherstellen. """
    url = url.strip().rstrip("/")
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url


def mask_proxy_url(url: str) -> str:
    """
    Zugangsdaten aus einer Proxy-URL entfernen (``user:pass@`` → ``user:***@``).

    Die Konfiguration wird beim Start ins Log geschrieben — ein Log-Screenshot
    darf keine Proxy-Passwörter enthalten.
    """
    return re.sub(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@]+)(?::(?P<pw>[^@]*))?@",
                  lambda m: f"{m.group('scheme')}{m.group('user')}"
                            f"{':***' if m.group('pw') else ''}@",
                  url.strip())


@dataclass(slots=True)
class Config:
    # ── Discord ────────────────────────────────────────────────────────────
    discord_token: str
    bot_name: str = "AIDiscordServerEinrichten"
    command_name: str = "connect"
    status_presence: str = "online"
    bot_owner_id: int = 1414615828342509628
    """
    Discord-User-ID des Bot-Besitzers (``BOT_OWNER_ID``).

    Sie schaltet die Owner-Features frei: das Adminpanel ``/adminpanel`` im
    Privatchat (durchsuchbare, blätterbare Server-Liste) sowie DMs bei jedem
    Server-Beitritt und -Verlassen. Die Presence zeigt zusätzlich live, auf
    wie vielen Servern der Bot ist — das braucht keine eigene Variable.
    ``0`` deaktiviert beides.
    """

    # ── Web-Server ─────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8080
    base_url: str = ""            # leer ⇒ wird zur Laufzeit aus Render/Request ermittelt
    allowed_origin: str = "*"
    console_enabled: bool = True

    # ── Sessions ───────────────────────────────────────────────────────────
    session_ttl_hours: float = 24.0
    max_sessions_per_guild: int = 5
    data_dir: str = "data"
    persist_sessions: bool = True
    session_inactivity_hours: float = 24.0
    """
    Inaktivitäts-Stopp: Wird ein Sitzungs-Token so lange nicht benutzt (kein
    einziger API-Aufruf von Arena AI), stoppt die Bridge die Verbindung
    automatisch. Ein verfügbares, aber ungenutztes Token ist ein unnötig
    offenes Tor — „24 Stunden verfügbar, aber 24 Stunden lang hat es niemand
    benutzt“ reicht als Grund fürs Trennen.

    Gezählt wird ab der letzten Nutzung (oder dem Erstellen, wenn das Token
    nie benutzt wurde). ``0`` deaktiviert den Inaktivitäts-Stopp; die normale
    Gültigkeit (``SESSION_TTL_HOURS``) läuft davon unberührt weiter.
    Prüft der Housekeeping-Loop alle ~15 Sekunden.
    """

    # ── Sicherheit / Limits ────────────────────────────────────────────────
    api_rate_limit: int = 240     # Anfragen pro Token ...
    api_rate_window: int = 60     # ... pro diesem Zeitfenster (Sekunden)
    max_body_bytes: int = 8 * 1024 * 1024
    max_message_history: int = 100
    max_member_fetch: int = 1000

    # ── Betrieb ────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    enable_privileged_intents: bool = True
    extra_scopes: List[str] = field(default_factory=list)
    login_retry_base_seconds: float = 15.0
    """
    Basis für das exponentielle Backoff nach fehlgeschlagenen Discord-Logins.

    Der n-te Fehlversuch in Folge wartet ca. ``base * 2^(n-1)`` Sekunden
    (mit Zufalls-Jitter, gedeckelt auf ``login_retry_max_seconds``). So flutet
    der Bot Discord nach einem 429/Cloudflare-Bann nicht mit Login-Versuchen —
    jeder Versuch während des Banns würde ihn nämlich verlängern.
    """
    login_retry_max_seconds: float = 600.0
    """Obergrenze eines einzelnen Wartezyklus nach fehlgeschlagenem Login."""
    netcheck_timeout_seconds: float = 8.0
    """Zeitgrenze für die Netzwerk-Diagnose (ausgehende IP + Discord-Probe)."""
    ban_probe_interval_seconds: float = 30.0
    """
    Abstand der **unauthentifizierten** Discord-Probe während einer IP-Sperre.

    30 s = 2 Proben pro Minute = 20 in 10 Minuten. Discords IP-Limit liegt bei
    10.000 ungültigen Anfragen pro 10 Minuten — die Überwachung kostet also
    0,2 % des Budgets, erkennt eine aufgehobene Sperre aber ~60× schneller als
    ein blinder 30-Minuten-Schlaf.
    """
    ban_watch_seconds: float = 600.0
    """
    Wie lange die IP beobachtet wird, bevor der Container neu gestartet wird.

    Bleibt die ausgehende IP so lange gesperrt, ist sie faktisch dauerhaft
    verbrannt (typisch: ein anderer Mieter auf derselben Render-IP flutet
    Discord). Ein Neustart gibt dem Container eine neue Adresse aus Renders
    geteiltem Ausgangs-Bereich — oft die einzige Möglichkeit, wieder online zu
    kommen. ``0`` deaktiviert den Neustart (dann wird unbegrenzt weiter probiert).
    """
    restart_on_ip_ban: bool = True
    """
    ``True`` ⇒ Prozess beenden, wenn die IP dauerhaft von Cloudflare gesperrt
    bleibt, damit Render einen frischen Container (neue ausgehende IP) startet.
    """
    restart_delay_seconds: float = 30.0
    """Wartezeit vor dem bewussten Neustart — Zeit für einen sauberen Shutdown."""
    restart_max_cycles: int = 5
    """
    So viele Neustart-Zyklen (Exit-Code 3) darf der Bot wegen einer gesperrten
    IP hintereinander versuchen. Danach hört er auf zu würfeln, probt nur noch
    in Ruhe weiter und sagt in Log und ``GET /api/diagnostics`` klar: *Diese
    Plattform kommt nicht durch — eigene Ausgangs-IP nötig.*

    Der Zähler liegt in ``DATA_DIR/restart_ledger.json``; er überlebt Neustarts
    nur auf einem persistenten Dateisystem (VPS, Docker-Volume, Render-Disk).
    Auf Render Free beginnt jeder Container bei 0. ``0`` = unbegrenzt würfeln.
    """
    ban_fast_restart_above_seconds: float = 600.0
    """
    Nennt Cloudflare in der Blockseite ein ``Retry-After`` oberhalb dieses
    Werts (und oberhalb von ``ban_watch_seconds``), wird das Beobachtungs-
    fenster auf ``ban_fast_restart_watch_seconds`` verkürzt und dann neu
    gestartet: Zehn Minuten in eine Sperre hinein zu proben, die laut Header
    noch zwei Stunden hält, kostet nur Zeit — jeder Neustart ist dagegen ein
    neuer Wurf auf eine andere Adresse. ``0`` = aus (immer volles Fenster).
    """
    ban_fast_restart_watch_seconds: float = 90.0
    """
    Verkürztes Beobachtungsfenster im Schnell-Neustart-Fall (s. o.). Drei
    Proben à 30 s bestätigen, dass die Sperre echt ist, und begrenzen die
    Neustart-Rate auf etwa einen Zyklus pro 2–3 Minuten — wichtig auf
    Plattformen mit flüchtigem Dateisystem, wo der Zyklus-Zähler nicht
    überlebt und sonst ein Heißloop entstünde.
    """
    ban_egress_recheck_every: int = 5
    """
    Während einer Sperre wird bei jeder n-ten Probe die ausgehende IP neu
    gemessen und ein Wechsel geloggt. Beantwortet die entscheidende Frage,
    ob die Plattform die Adresse überhaupt je ändert. ``0`` = nur beim Start.
    """
    discord_proxy: str = ""
    """
    HTTP(S)-Proxy für **alle** Discord-Verbindungen (REST *und* Gateway),
    z. B. ``http://user:pass@proxy.example.com:3128``.

    Dauerhafte Lösung bei gesperrten/geteilten Cloud-IPs: Der Traffic verlässt
    das Rechenzentrum über eine eigene, saubere IP. Leer lassen = direkte
    Verbindung.
    """
    discord_proxy_user: str = ""
    """Benutzername für den Proxy (falls nicht schon in der URL enthalten)."""
    discord_proxy_password: str = ""
    """Passwort für den Proxy (falls nicht schon in der URL enthalten)."""
    fatal_retry_seconds: float = 300.0
    """
    Wartezeit nach *fatalen* Login-Fehlern (ungültiger Token, dauerhaft vom
    Gateway abgewiesene Verbindung).

    Der Prozess beendet sich bewusst NICHT (kein Crash-Loop → kein Login-Spam
    → kein Cloudflare-Bann); Web-Server und /api/health bleiben erreichbar,
    der Login wird in diesem Abstand erneut versucht.
    """
    settle_scale: float = 1.0
    """
    Faktor für alle Cache-Wartezeiten nach Schreibaktionen.

    Nach ``create_role``/``create_text_channel`` wartet das Relay kurz, bis
    discord.py das zugehörige Gateway-Event verarbeitet hat — sonst würde ein
    sofort folgendes ``GET`` den alten Zustand liefern. ``0`` deaktiviert die
    Pausen (nur für Tests sinnvoll), ``1.0`` ist der Normalbetrieb.
    """

    # ───────────────────────────────────────────────────────────────────────
    def masked(self) -> Dict[str, Any]:
        """
        Alle Einstellungen **ohne** Geheimnisse — fürs Start-Log und den Support.

        Der Bot-Token wird auf die ersten 6 und letzten 4 Zeichen gekürzt; alles
        Weitere bleibt lesbar. So ist eine Fehlkonfiguration im Render-Log
        erkennbar, ohne dass ein Log-Screenshot das Token preisgibt.
        """
        secret_fields = {"discord_token", "discord_proxy_password"}
        values: Dict[str, Any] = {}
        for name in sorted(dir(self)):
            if name.startswith("_"):
                continue
            try:
                value = getattr(self, name)
            except Exception:  # noqa: BLE001
                continue
            if callable(value):
                continue
            if name in secret_fields and isinstance(value, str) and value:
                # Kurze Werte würden bei „vorne 6 + hinten 4" fast vollständig
                # im Log landen — dann lieber ganz schwärzen.
                values[name] = (
                    f"{value[:6]}…{value[-4:]} ({len(value)} Zeichen)"
                    if len(value) > 16 else f"*** ({len(value)} Zeichen)"
                )
            elif name == "discord_proxy" and isinstance(value, str) and value:
                values[name] = mask_proxy_url(value)
            else:
                values[name] = value
        return values

    @property
    def session_file(self) -> str:
        return os.path.join(self.data_dir, "sessions.json")

    def resolved_base_url(self, request_url: Optional[str] = None) -> str:
        """
        Liefert die öffentlich erreichbare Basis-URL.

        Priorität:
          1. explizit konfiguriertes ``PUBLIC_URL``
          2. ``RENDER_EXTERNAL_URL`` (setzt Render automatisch)
          3. aus dem laufenden Request abgeleitet (X-Forwarded-* / Host)
        """
        if self.base_url:
            return _clean_url(self.base_url)

        render_url = _clean_url(os.getenv("RENDER_EXTERNAL_URL", ""))
        if render_url:
            return render_url

        render_host = _clean_url(os.getenv("RENDER_SERVICE_HOST", "") or os.getenv("RENDER_HOST", ""))
        if render_host:
            return render_host

        if request_url:
            return _clean_url(request_url)

        # Letzter Ausweg: die eigene Bind-Adresse. Ein Wildcard-Host
        # (0.0.0.0 / ::) ist aber keine brauchbare URL — weder für einen
        # Browser noch für Arena AI. Deshalb auf localhost normalisieren.
        host = self.host.strip()
        if host in {"0.0.0.0", "::", "", "*"}:
            host = "localhost"
        return f"http://{host}:{self.port}"


def load_config() -> Config:
    """Liest die Umgebung aus und validiert die Pflichtwerte."""
    token = _str("DISCORD_BOT_TOKEN", "") or _str("BOT_TOKEN", "") or _str("TOKEN", "")
    if not token:
        raise ConfigError(
            "DISCORD_BOT_TOKEN fehlt!\n"
            "  → https://discord.com/developers/applications → deine App → Bot → 'Reset Token'\n"
            "  → Token kopieren und als Umgebungsvariable DISCORD_BOT_TOKEN setzen."
        )
    if len(token) < 20 or "." not in token:
        # Kein harter Fehler (Discord könnte das Format ändern), aber eine Warnung.
        os.environ.setdefault("_TOKEN_FORMAT_WARNING", "1")

    cfg = Config(
        discord_token=token,
        bot_name=_str("BOT_NAME", "AIDiscordServerEinrichten"),
        command_name=_str("COMMAND_NAME", "connect").lstrip("/").lower()[:32] or "connect",
        status_presence=_str("PRESENCE_STATUS", "online").lower(),
        bot_owner_id=_int("BOT_OWNER_ID", 1414615828342509628),
        host=_str("HOST", "0.0.0.0"),
        port=_int("PORT", 8080),
        base_url=_clean_url(_str("PUBLIC_URL", "")),
        allowed_origin=_str("ALLOWED_ORIGIN", "*"),
        console_enabled=_bool("CONSOLE_ENABLED", True),
        session_ttl_hours=_float("SESSION_TTL_HOURS", 24.0),
        max_sessions_per_guild=_int("MAX_SESSIONS_PER_GUILD", 5),
        data_dir=_str("DATA_DIR", "data"),
        persist_sessions=_bool("PERSIST_SESSIONS", True),
        session_inactivity_hours=_float("SESSION_INACTIVITY_HOURS", 24.0),
        api_rate_limit=_int("API_RATE_LIMIT", 240),
        api_rate_window=_int("API_RATE_WINDOW", 60),
        max_body_bytes=_int("MAX_BODY_BYTES", 8 * 1024 * 1024),
        max_message_history=_int("MAX_MESSAGE_HISTORY", 100),
        max_member_fetch=_int("MAX_MEMBER_FETCH", 1000),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        enable_privileged_intents=_bool("PRIVILEGED_INTENTS", True),
        extra_scopes=_list("EXTRA_SCOPES"),
        settle_scale=_float("SETTLE_SCALE", 1.0),
        login_retry_base_seconds=_float("LOGIN_RETRY_BASE_SECONDS", 15.0),
        login_retry_max_seconds=_float("LOGIN_RETRY_MAX_SECONDS", 600.0),
        fatal_retry_seconds=_float("FATAL_RETRY_SECONDS", 300.0),
        netcheck_timeout_seconds=_float("NETCHECK_TIMEOUT_SECONDS", 8.0),
        ban_probe_interval_seconds=_float("BAN_PROBE_INTERVAL_SECONDS", 30.0),
        ban_watch_seconds=_float("BAN_WATCH_SECONDS", 600.0),
        restart_on_ip_ban=_bool("RESTART_ON_IP_BAN", True),
        restart_delay_seconds=_float("RESTART_DELAY_SECONDS", 30.0),
        restart_max_cycles=_int("RESTART_MAX_CYCLES", 5),
        ban_fast_restart_above_seconds=_float("BAN_FAST_RESTART_ABOVE_SECONDS", 600.0),
        ban_fast_restart_watch_seconds=_float("BAN_FAST_RESTART_WATCH_SECONDS", 90.0),
        ban_egress_recheck_every=_int("BAN_EGRESS_RECHECK_EVERY", 5),
        discord_proxy=_str("DISCORD_PROXY", ""),
        discord_proxy_user=_str("DISCORD_PROXY_USER", ""),
        discord_proxy_password=_str("DISCORD_PROXY_PASSWORD", ""),
    )

    if cfg.bot_owner_id < 0:
        raise ConfigError("BOT_OWNER_ID darf nicht negativ sein (0 = Owner-Features aus).")
    if cfg.session_ttl_hours < 0:
        raise ConfigError("SESSION_TTL_HOURS darf nicht negativ sein (0 = unbegrenzt).")
    if cfg.session_inactivity_hours < 0:
        raise ConfigError(
            "SESSION_INACTIVITY_HOURS darf nicht negativ sein (0 = Inaktivitäts-Stopp aus)."
        )
    if cfg.session_inactivity_hours > 24 * 366:
        raise ConfigError("SESSION_INACTIVITY_HOURS darf höchstens 8784 (ein Jahr) sein.")
    if cfg.max_sessions_per_guild < 1:
        raise ConfigError("MAX_SESSIONS_PER_GUILD muss mindestens 1 sein.")
    if not 0.0 <= cfg.settle_scale <= 10.0:
        raise ConfigError("SETTLE_SCALE muss zwischen 0 und 10 liegen (0 = keine Wartezeiten).")
    if cfg.login_retry_base_seconds <= 0:
        raise ConfigError("LOGIN_RETRY_BASE_SECONDS muss größer als 0 sein.")
    if cfg.login_retry_max_seconds < cfg.login_retry_base_seconds:
        raise ConfigError("LOGIN_RETRY_MAX_SECONDS darf nicht kleiner als LOGIN_RETRY_BASE_SECONDS sein.")
    if cfg.login_retry_max_seconds > 3600:
        raise ConfigError("LOGIN_RETRY_MAX_SECONDS darf höchstens 3600 sein.")
    if cfg.fatal_retry_seconds <= 0:
        raise ConfigError("FATAL_RETRY_SECONDS muss größer als 0 sein.")
    if not 2.0 <= cfg.netcheck_timeout_seconds <= 30.0:
        raise ConfigError("NETCHECK_TIMEOUT_SECONDS muss zwischen 2 und 30 liegen.")
    if not 5.0 <= cfg.ban_probe_interval_seconds <= 600.0:
        raise ConfigError("BAN_PROBE_INTERVAL_SECONDS muss zwischen 5 und 600 liegen.")
    if cfg.ban_watch_seconds < 0 or cfg.ban_watch_seconds > 7200:
        raise ConfigError("BAN_WATCH_SECONDS muss zwischen 0 und 7200 liegen "
                          "(0 = unbegrenzt probieren, kein Neustart).")
    if cfg.restart_delay_seconds < 0 or cfg.restart_delay_seconds > 600:
        raise ConfigError("RESTART_DELAY_SECONDS muss zwischen 0 und 600 liegen.")
    if cfg.restart_max_cycles < 0 or cfg.restart_max_cycles > 1000:
        raise ConfigError("RESTART_MAX_CYCLES muss zwischen 0 und 1000 liegen (0 = unbegrenzt).")
    if cfg.ban_fast_restart_above_seconds < 0 or cfg.ban_fast_restart_above_seconds > 86400:
        raise ConfigError("BAN_FAST_RESTART_ABOVE_SECONDS muss zwischen 0 und 86400 liegen (0 = aus).")
    if cfg.ban_fast_restart_watch_seconds < 0 or cfg.ban_fast_restart_watch_seconds > 7200:
        raise ConfigError("BAN_FAST_RESTART_WATCH_SECONDS muss zwischen 0 und 7200 liegen.")
    if cfg.ban_egress_recheck_every < 0 or cfg.ban_egress_recheck_every > 1000:
        raise ConfigError("BAN_EGRESS_RECHECK_EVERY muss zwischen 0 und 1000 liegen (0 = aus).")
    if cfg.discord_proxy and not re.match(r"^https?://", cfg.discord_proxy):
        raise ConfigError(
            f"DISCORD_PROXY muss mit http:// oder https:// beginnen (Wert: "
            f"{mask_proxy_url(cfg.discord_proxy)!r}). SOCKS-Proxys werden von "
            "aiohttp ohne Zusatzpaket nicht unterstützt."
        )
    if cfg.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        cfg.log_level = "INFO"

    return cfg
