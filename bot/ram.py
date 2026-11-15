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
import os
from typing import Any, Dict, Optional, Tuple

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


def _configure_malloc() -> None:
    """
    glibc-Allokator-Strategie begrenzen: M_ARENA_MAX=2 (statt bis 8×Kerne).
    Die PDF-Bildkonvertierung läuft in Worker-Threads — jeder Thread bekommt
    sonst eine eigene malloc-Arena, deren freigegebener Speicher kaum noch
    ans OS zurückgegeben wird (Beobachtung: langsamer RAM-Anstieg pro Teil).
    Muss VOR dem Start von Threads laufen → beim Modul-Import.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.mallopt(-8, 2)  # M_ARENA_MAX = 2
        libc.mallopt(-7, 4)  # M_ARENA_TEST = 4
    except Exception:  # noqa: BLE001 — Best-Effort (nicht-Linux: egal)
        pass


_configure_malloc()


def container_memory_stat() -> "Dict[str, float]":
    """
    Rohwerte des cgroup-Memory-Controllers (v2, Fallback v1):
    ``used`` (memory.current — inkl. Page-Cache), ``anon`` (OOM-relevanter
    Teil: anonyme Speicher der Prozesse), ``file`` (Page-Cache — gibt der
    Kernel bei Speicherdruck automatisch frei, KEIN OOM-Risiko) und
    ``limit``. Fehlende Werte: -1.
    """
    out = {"used": -1.0, "anon": -1.0, "file": -1.0, "limit": -1.0}
    used_path = limit_path = stat_path = None
    if os.path.isfile("/sys/fs/cgroup/memory.current"):
        used_path = "/sys/fs/cgroup/memory.current"          # cgroup v2
        limit_path = "/sys/fs/cgroup/memory.max"
        stat_path = "/sys/fs/cgroup/memory.stat"
    elif os.path.isfile("/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        used_path = "/sys/fs/cgroup/memory/memory.usage_in_bytes"  # cgroup v1
        limit_path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        stat_path = "/sys/fs/cgroup/memory/memory.stat"
    if used_path is None:
        return out
    try:
        with open(used_path, "r", encoding="utf-8") as fh:
            out["used"] = int(fh.read().strip()) / 1048576.0
        with open(limit_path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        limit = -1 if raw in ("max",) or int(raw) >= (1 << 59) else int(raw)
        out["limit"] = limit / 1048576.0
        with open(stat_path, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) == 2:
                    key, val = parts[0], float(parts[1])
                    if key in ("anon", "rss", "total_anon"):
                        out["anon"] = val / 1048576.0
                    elif key in ("file", "cache", "total_cache"):
                        out["file"] = val / 1048576.0
    except (OSError, ValueError):
        pass
    return out


def container_memory_mb() -> "Tuple[float, float]":
    """
    ``(OOM-relevanter RAM MB, Limit MB)`` des Containers.

    OOM-relevant ist der ANONYME Anteil (``memory.stat: anon``) — der
    Page-Cache (Datei-I/O) wird vom Kernel bei Speicherdruck automatisch
    freigegeben und hat KEINEN OOM-Einfluss; ihn mitzuzählen ließ die
    Anzeige/Guards künstlich hoch klettern. Ist ``anon`` nicht lesbar,
    fällt die Messung auf ``memory.current`` zurück. Genau diese Zahl
    triggert bei Überschreitung den OOM-Kill von Render.
    """
    st = container_memory_stat()
    used = st["anon"] if st["anon"] > 0 else st["used"]
    return used, st["limit"]


def container_cache_mb() -> float:
    """Page-Cache des Containers (MB) — freigebbar, nur zur Anzeige."""
    return container_memory_stat()["file"]


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
        _st = container_memory_stat()
        container_mb = _st["anon"] if _st["anon"] > 0 else _st["used"]
        container_limit = _st["limit"]
        mb = container_mb if container_mb > 0 else current_rss_mb()
        if mb < 0:
            log.debug("RAM-Messung nicht verfügbar — Monitor beendet.")
            return
        limit_txt = (f"{container_limit:.0f}" if container_limit > 0
                     else f"~{RAM_LIMIT_MB:.0f} (geschätzt)")
        proc_mb = current_rss_mb()
        detail = (f" (Prozess {proc_mb:.0f} MB)"
                  if container_mb > 0 and proc_mb > 0 and abs(container_mb - proc_mb) > 8 else "")
        if _st["file"] > 15:
            detail += f" [Cache {_st['file']:.0f} MB — freigebbar]"
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
