"""
RAM-Kontrolle für den Relay-Bot (Render Free: 512 MB Container-Limit).

Der Bot zählt seinen eigenen RAM (VmRSS aus /proc/self/status):

- ``current_rss_mb()``: aktueller RAM-Verbrauch in MB (Linux-Container).
- ``relieve_ram()``: aggressives Aufräumen (GC + PDF-Bildcaches schließen).
- ``ram_report_loop()``: Hintergrund-Task — loggt den RAM alle 60 s,
  warnt bei der Warnschwelle und greift bei der kritischen Schwelle durch
  (Aufräumen + CRITICAL-Log). Ein OOM-Kill selbst lässt sich nicht
  verhindern, aber die Logs zeigen die RAM-Kurve — und die
  Startup-DM (siehe discord_bot) meldet jeden Neustart sofort.
"""

import asyncio
import gc
import logging
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

#: Render Free: 512 MB — ab hier warnen / durchgreifen (MB).
RAM_LIMIT_MB = 512
RAM_WARN_MB = 300
RAM_CRITICAL_MB = 420
#: Log-Intervall des Monitors (Sekunden).
RAM_REPORT_INTERVAL_S = 60.0


def current_rss_mb() -> float:
    """Aktueller RAM-Verbrauch des Prozesses in MB (−1 bei Fehlern)."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    # „VmRSS:    123456 kB“
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource

        ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux: kB — macOS: Bytes; genau genug für beide Fälle:
        return ru_maxrss / 1024.0
    except Exception:  # noqa: BLE001 — Messung darf nie werfen
        return -1.0


def container_memory_mb() -> "Tuple[float, float]":
    """
    ``(genutzt MB, limit MB)`` des GESAMTEN Containers — aus dem cgroup-
    Memory-Controller (v2, Fallback v1). Genau diese Zahl sieht Render, wenn
    sie das Limit überschreitet: OOM-Kill.

    Der Prozess-RSS (``current_rss_mb``) ist NICHT die ganze Wahrheit: Der
    Container zählt alle Prozesse + Seiten-Cache (z. B. Datei-Lese-Cache des
    Emoji-Sheets) mit. Deshalb bauen Guards und Anzeigen auf dieser Zahl auf,
    wenn sie lesbar ist.

    Rückgabe: ``(-1.0, -1.0)``, wenn nicht lesbar (kein cgroup/andere Plattform).
    """
    for used_path, limit_path in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),  # cgroup v2
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",                  # cgroup v1
         "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            with open(used_path, "r", encoding="utf-8") as fh:
                used = int(fh.read().strip())
            with open(limit_path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
            limit = int(raw)
            if limit >= (1 << 59):  # v1: „unbegrenzt“ = riesige Zahl
                limit = -1
            elif raw == "max":      # v2: „unbegrenzt“
                limit = -1
            return used / 1048576.0, limit / 1048576.0
        except (OSError, ValueError):
            continue
    return -1.0, -1.0


def malloc_trim() -> None:
    """
    Gibt freigegebenen Heap-Speicher an das Betriebssystem zurück (glibc).

    Hilft gegen Fragmentierung (glibc hält freigegebenen Heap zurück).
    Der Kumulativ-OOM der Chatverlauf-Erstellung war primär eine Retention
    (Teil-PDFs blieben bis zum Ende referenziert — siehe
    ``pdf_document._finish_batch``); seit sie verworfen werden, hält
    ``malloc_trim`` den RSS zusätzlich flach. Best-Effort.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:  # noqa: BLE001 — Trim ist Best-Effort (nicht-Linux: egal)
        pass


def relieve_ram() -> float:
    """
    Aggressives Aufräumen: GC + PDF-Bildcaches schließen (das Apple-Sheet
    ist ≈69 MB RGBA; die zugeschnittenen Emoji-PNGs bleiben auf Disk) +
    ``malloc_trim`` (Heap-Fragmentierung ans OS zurückgeben).

    Rückgabe: RAM-Verbrauch nach dem Aufräumen (MB).
    """
    gc.collect()
    try:
        from bot.web import pdf_document as _pdf

        _pdf._close_apple_sheet()
        _pdf._custom_png_cache.clear()
        _pdf._apple_png_cache.clear()
    except Exception:  # noqa: BLE001 — Aufräumen darf nie werfen
        log.debug("PDF-Caches konnten nicht geleert werden.", exc_info=True)
    gc.collect()
    malloc_trim()
    return current_rss_mb()


async def ram_report_loop(interval_s: float = RAM_REPORT_INTERVAL_S) -> None:
    """
    Hintergrund-Task: loggt den RAM-Verbrauch regelmäßig. Bei der
    Warnschwelle WARNING, bei der kritischen Schwelle CRITICAL + Aufräumen.
    Läuft bis zum Cancel (z. B. beim Herunterfahren des Clients).
    """
    log.info("RAM-Monitor aktiv (Warn ab %d MB, kritisch ab %d MB, Limit ~%d MB).",
             RAM_WARN_MB, RAM_CRITICAL_MB, RAM_LIMIT_MB)
    while True:
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
        container_mb, container_limit = container_memory_mb()
        mb = container_mb if container_mb > 0 else current_rss_mb()
        if mb < 0:
            log.debug("RAM-Messung nicht verfügbar — Monitor beendet.")
            return
        limit_txt = (f"{container_limit:.0f}" if container_limit > 0
                     else f"~{RAM_LIMIT_MB:.0f} (geschätzt)")
        proc_mb = current_rss_mb()
        detail = (f" (Prozess {proc_mb:.0f} MB)"
                  if container_mb > 0 and proc_mb > 0 and abs(container_mb - proc_mb) > 8 else "")
        if mb >= RAM_CRITICAL_MB:
            log.critical(
                "RAM KRITISCH: %.0f/%s MB%s — räume aggressiv auf (Apple-Sheet, "
                "Bild-Caches, GC) …", mb, limit_txt, detail,
            )
            after = relieve_ram()
            log.critical("RAM nach Aufräumen: %.0f MB — bleibt es hoch, droht "
                         "ein OOM-Neustart des Containers.", after)
        elif mb >= RAM_WARN_MB:
            log.warning("RAM hoch: %.0f/%s MB%s (Warnschwelle %d MB).",
                        mb, limit_txt, detail, RAM_WARN_MB)
        else:
            log.info("RAM: %.0f/%s MB%s", mb, limit_txt, detail)


def ram_overview_lines() -> list:
    """Kurze RAM-Übersicht für Startup-DMs / Diagnose (deutsch)."""
    lines = []
    container_mb, container_limit = container_memory_mb()
    if container_mb > 0:
        limit_txt = (f"{container_limit:.0f} MB" if container_limit > 0
                     else f"~{RAM_LIMIT_MB} MB (geschätzt)")
        lines.append(f"**RAM (Container):** {container_mb:.0f} MB von {limit_txt}"
                     " — diese Zahl entscheidet über OOM-Kills")
    mb = current_rss_mb()
    if mb > 0:
        lines.append(f"**RAM (Bot-Prozess):** {mb:.0f} MB")
    if not lines:
        return ["**RAM:** Messung in dieser Umgebung nicht verfügbar."]
    return lines


def monitor_task_from(client: Optional[Any] = None) -> Optional["asyncio.Task[Any]"]:
    """Startet (oder gibt) den RAM-Monitor-Task — für Tests praktisch."""
    return asyncio.create_task(ram_report_loop())
