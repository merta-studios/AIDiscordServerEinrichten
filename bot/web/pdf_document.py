"""
Erstellung des **Server-Dokuments (PDF)** — komplette, gut lesbare
Server-Übersicht mit Bildern.

Wird aus dem Adminpanel (Button „Server-Dokument erstellen“) aufgerufen,
erzeugt ein PDF mit allen verfügbaren Server-Daten und liefert die PDF-Bytes
zurück. Der Versand per DM an den Bot-Owner erfolgt im Aufrufer
(``RelayClient._handle_admin_create_doc``), damit der Server selbst nichts
bemerkt.

Design-Prinzipien dieser Neuauflage
------------------------------------
* **Bilder wirklich eingebettet** — Icon, Banner, Avatare, Emojis und Sticker
  werden einmalig heruntergeladen (``Dict[URL, Bytes]``), per Pillow in
  druckfähige PNGs umgewandelt und als echte Bilder ins PDF eingebettet.
  (Der Vorgänger lud Bilder zwar herunter, warf das Ergebnis aber weg —
  deshalb enthielt das PDF keine einzige Grafik.)
* **Keine schwarzen Vierecke mehr** — statt Helvetica (die keine Emojis,
  Pfeile oder kyrillische Zeichen kennt) wird DejaVu Sans mit dem Bot
  mitgeliefert und eingebettet. Emojis (👋, 🎉) und Unicode-„Schriftarten“
  (𝗖𝗵𝗮𝘁, 𝕏, 𝔊) werden über mitgelieferte Noto-Fonts (Monochrome Emoji +
  Noto Sans Math) als echte Zeichen gerendert — der Paragraph-Renderer wählt
  pro Zeichen den passenden Font; Zeichen ohne Glyphe in ALLEN Fonts werden
  gefiltert, sodass niemals „Notdef“-Kästen entstehen können.
* **Nichts läuft mehr über den Rand** — alle langen Texte (Berechtigungen,
  Rollenlisten, Nachrichteninhalte) sind ReportLab-``Paragraph``-Flowables
  mit sauberem Zeilenumbruch; jede Tabelle bleibt schmaler als der Satzspiegel.
* **Lesbar auch auf dem Handy** — größere Schrift (≈ 9 pt Fließtext statt
  7 pt), mehr Weißraum, klare Abschnittsblöcke, Farb-Chips statt eng gestellter
  Spalten.

Alle Discord-API-Aufrufe sind fehlertolerant: Fehlende Rechte (z. B. keine
Nachrichtenhistorie) führen nicht zum Abbruch, sondern werden als „nicht
verfügbar“ ausgewiesen. Fehlende Optionals (Pillow/emoji/Font-Dateien)
degradieren das Layout, lassen den Build aber niemals crashen.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp
import discord
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    Indenter,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

try:  # Pillow ist für Bildkonvertierung (WebP/GIF/RGBA) zuständig
    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover — requirements machen das unwahrscheinlich
    _PILImage = None

log = logging.getLogger("relay.pdf")

# ─────────────────────────────────────────────────────────────────────────────
#  Geometrie & Farben
# ─────────────────────────────────────────────────────────────────────────────

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 18 * mm
TOP_MARGIN = 21 * mm
BOTTOM_MARGIN = 18 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN  # ≈ 174 mm

#: angenommene native Auflösung der CDN-Bilder (96 dpi)
PX_PER_MM = 96 / 25.4
#: Zielpixel pro mm beim Skalieren (≈ 203 dpi — drucktauglich, kleine Dateien)
RENDER_PX_PER_MM = 8

MAX_MEMBERS_DETAILED = 50
MAX_MESSAGES_PER_CHANNEL = 10
MAX_EMOJIS_SHOWN = 70
MAX_STICKERS_SHOWN = 24
EMOJI_GRID_COLUMNS = 7
STICKER_GRID_COLUMNS = 4

# Nachrichten-Rendering: Discord erlaubt 4000–8000 Zeichen pro Nachricht und
# bis zu 10 Embeds mit je 4096 Zeichen. Würde alles in EINE Tabellenzeile
# wandern, wäre diese Zeile höher als eine PDF-Seite — ReportLab kann
# Tabellenzeilen nicht splitten und bricht mit „too large on page" ab
# (in Produktion auf Seite 24 passiert). Deshalb: Nachrichten werden als
# einzelne, splittbare Flowables gerendert und großzügig, aber begrenzt
# gekürzt; Embed-Boxen haben eine Zeile pro Feld und können dadurch selbst
# seitenumbruchfähig splitten.
MESSAGE_CONTENT_MAX_CHARS = 3000
MAX_EMBEDS_PER_MESSAGE = 5
EMBED_TEXT_MAX_CHARS = 800
EMBED_TEXT_MAX_LINES = 20
EMBED_TITLE_MAX_CHARS = 120
EMBED_LABEL_MAX_CHARS = 80
MAX_ATTACHMENT_NAMES = 6
#: Bildanhänge, die als Bild gerendert werden (global pro Dokument)
MAX_ATTACHMENT_IMAGES = 60
#: Hart-Timeout pro Bild-Request (Sekunden) — Sicherheitsnetz über dem
#: aiohttp-Timeout (fängt DNS-/Connector-Stalls mit ab).
_IMAGE_TIMEOUT_S = 20.0
#: Globales Budget für die GESAMTE Bild-Download-Phase: Danach wird mit den
#: bereits geladenen Bildern weitergebaut statt sich ewig aufzuhängen.
_IMAGES_BUDGET_S = 150.0
#: Alle N fertigen Bildern eine Fortschrittsmeldung senden.
_IMAGES_PROGRESS_EVERY = 10
#: Ab wie vielen Sekunden ein einzelner Bild-Download als „langsam“ geloggt wird.
_SLOW_IMAGE_S = 10.0
#: Max. Pixel pro Bild — Header-Check VOR dem Dekodieren (PIL.open ist lazy).
#: Schützt vor Dekodier-Bomben, die Minuten CPU fressen können. 16 MP ≈ 64 MB
#: RGBA-Decode-Spitze (bei max. 2 parallelen Konvertierungen).
_MAX_IMAGE_PIXELS = 16_000_000
#: Max. Dateigröße pro Bild-Download (Bytes) — größere Anhänge werden gar
#: nicht erst in den RAM geladen (Render Free: 512 MB!).
_MAX_IMAGE_BYTES = 8_000_000
#: Zielauflösung der Sofort-Konvertierung (px Breite) — deckt alle
#: Anzeige-Größen im PDF ab (Breitbild-Banner bis ~1024 px ≈ 165 dpi).
_FINALIZE_MAX_PX_W = 1024
#: Max. 2 parallele PIL-Konvertierungen — jede kostet bis zu ~64 MB Spitze.
_CONVERT_SEM = asyncio.Semaphore(2)
#: Max. Zeit für den kompletten PDF-Aufbau (Flowables + PIL + reportlab im
#: Worker-Thread) — danach sichtbare Fehlermeldung statt ewiger Stille.
_ASSEMBLY_TIMEOUT_S = 600.0
#: Intervall des Fortschritts-Tickers während der Aufbau-Phase (Sekunden).
_ASSEMBLY_TICKER_S = 15.0
#: Nachrichten pro Chatverlauf-Teil-PDF: Jeder Teil wird SOFORT nach dem Bau
#: versendet und verworfen — RAM bleibt flach („fertige Sachen senden, Daten
#: löschen, Platz für neue“), egal wie groß der Verlauf ist.
HISTORY_BATCH_MESSAGES = 100
COMPONENTS_TEXT_MAX_CHARS = 500
#: Max. Höhe einer Nachrichten-Kästchen-Box in Punkt — darüber wird die
#: Nachricht unboxiert (splittbar) gerendert (LayoutError-Schutz).
MAX_MESSAGE_BOX_PT = 660.0

#: Fortschritts-Callback: (prozent: int, text: str) -> None
ProgressCallback = Callable[[int, str], None]


class _C:
    """Farbpalette (an Discord angelehnt, aber druckfreundlich)."""

    blurple = colors.HexColor("#5865F2")
    dark = colors.HexColor("#2B2D31")
    text = colors.HexColor("#23262C")
    muted = colors.HexColor("#6B7280")
    light = colors.HexColor("#F2F4F8")
    lighter = colors.HexColor("#F8F9FB")
    border = colors.HexColor("#D5D9E0")
    green = colors.HexColor("#23A559")
    amber = colors.HexColor("#F0B232")
    amber_text = colors.HexColor("#3A2F05")
    pink = colors.HexColor("#EB459E")
    cyan = colors.HexColor("#00A8FC")
    gray_chip = colors.HexColor("#80848E")
    red = colors.HexColor("#ED4245")
    white = colors.white


# ─────────────────────────────────────────────────────────────────────────────
#  Fonts (DejaVu — mitgeliefert, damit überall vollständige Zeichensätze
#  verfügbar sind; Fallback: System-DejaVu, dann Base-14-Fonts)
# ─────────────────────────────────────────────────────────────────────────────

_FONT_DIR_CANDIDATES = (
    Path(__file__).resolve().parent / "fonts",  # mitgeliefert (Repo/Docker)
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/dejavu-slim"),
    Path("/usr/local/share/fonts"),
)
_FONT_FILES: Dict[str, Tuple[str, str]] = {
    # Rolle: (Dateiname, Base-14-Fallback)
    "sans": ("DejaVuSans.ttf", "Helvetica"),
    "bold": ("DejaVuSans-Bold.ttf", "Helvetica-Bold"),
    "mono": ("DejaVuSansMono.ttf", "Courier"),
    "mono_bold": ("DejaVuSansMono-Bold.ttf", "Courier-Bold"),
}
#: Optionale Zusatz-Fonts (fehlende Datei degradiert nur die Darstellung):
#: * emoji — Monochromes Noto Emoji: Echte Emoji-Glyphen im PDF (👋 statt
#:   „winkende Hand“), inkl. ZWJ-Sequenzen und Regional-Indikatoren.
#: * math — Noto Sans Math: Unicode-„Schriftarten“ wie 𝗖𝗵𝗮𝘁 (Mathematical
#:   Alphanumeric Symbols, Fraktur, Double-Struck …), die DejaVu nicht kennt.
#: * cjk_sc / cjk_kr — Noto Sans SC/KR (statische Regular-Instanzen):
#:   Chinesisch (vereinfacht/traditionell), Japanisch (Kana+Kanji), Koreanisch
#:   (Hangul), dazu CJK-Punkte, Circled/Fullwidth-Formen.
#: * Scripts — die restlichen Sprach-Schriften, die DejaVu fehlen
#:   (Indic, Thai/Lao/Khmer/Myanmar, Ethiopic, Cherokee, Tibetan …).
_EXTRA_FONT_FILES: Dict[str, str] = {
    "emoji": "NotoEmoji.ttf",
    "math": "NotoSansMath.ttf",
    "cjk_sc": "NotoSansSC.ttf",
    "cjk_kr": "NotoSansKR.ttf",
    "devanagari": "NotoSansDevanagari.ttf",
    "bengali": "NotoSansBengali.ttf",
    "gurmukhi": "NotoSansGurmukhi.ttf",
    "gujarati": "NotoSansGujarati.ttf",
    "oriya": "NotoSansOriya.ttf",
    "tamil": "NotoSansTamil.ttf",
    "telugu": "NotoSansTelugu.ttf",
    "kannada": "NotoSansKannada.ttf",
    "malayalam": "NotoSansMalayalam.ttf",
    "sinhala": "NotoSansSinhala.ttf",
    "thai": "NotoSansThai.ttf",
    "lao": "NotoSansLao.ttf",
    "myanmar": "NotoSansMyanmar.ttf",
    "khmer": "NotoSansKhmer.ttf",
    "ethiopic": "NotoSansEthiopic.ttf",
    "cherokee": "NotoSansCherokee.ttf",
    "mongolian": "NotoSansMongolian.ttf",
    "tibetan": "NotoSerifTibetan.ttf",
}
#: Auswertungsreihenfolge in _font_markup (speziell → allgemein).
_EXTRA_FONT_ORDER: Tuple[str, ...] = (
    "emoji", "math", "cjk_sc", "cjk_kr",
    "devanagari", "bengali", "gurmukhi", "gujarati", "oriya",
    "tamil", "telugu", "kannada", "malayalam", "sinhala",
    "thai", "lao", "myanmar", "khmer", "ethiopic", "cherokee",
    "mongolian", "tibetan",
)
_FONTS: Dict[str, Optional[str]] = {role: fallback for role, (_file, fallback) in _FONT_FILES.items()}
_FONTS.update({role: None for role in _EXTRA_FONT_FILES})  # bis geladen: None
_FONTS_READY = False
#: Menge der im Font vorhandenen Codepoints (für die Notdef-Filterung)
_GLYPH_COVERAGE: Optional[set] = None
#: Coverage je Font (None = nicht verfügbar); „sans“ ist die Basis-Abdeckung
_FONT_COVERAGE: Dict[str, Optional[set]] = {"sans": None}
_FONT_COVERAGE.update({role: None for role in _EXTRA_FONT_FILES})


def _ensure_fonts() -> Dict[str, str]:
    """Registriert DejaVu (idempotent) und liefert die aktive Font-Zuordnung."""
    global _FONTS_READY, _GLYPH_COVERAGE
    if _FONTS_READY:
        return _FONTS

    fonts_dir: Optional[Path] = None
    for candidate in _FONT_DIR_CANDIDATES:
        try:
            if all((candidate / fname).is_file() for fname, _fb in _FONT_FILES.values()):
                fonts_dir = candidate
                break
        except OSError:
            continue

    if fonts_dir is not None:
        try:
            registered: Dict[str, Optional[str]] = {}
            for role, (fname, _fallback) in _FONT_FILES.items():
                name = f"DejaVu-{role}"
                pdfmetrics.registerFont(TTFont(name, str(fonts_dir / fname)))
                registered[role] = name
            # <b>/<i>-Markup in Paragraphs auf die DejaVu-Familie mappen
            pdfmetrics.registerFontFamily(
                registered["sans"],
                normal=registered["sans"],
                bold=registered["bold"],
                italic=registered["sans"],
                boldItalic=registered["bold"],
            )
            pdfmetrics.registerFontFamily(
                registered["mono"],
                normal=registered["mono"],
                bold=registered["mono_bold"],
                italic=registered["mono"],
                boldItalic=registered["mono_bold"],
            )
            _FONTS.update(registered)
            face = pdfmetrics.getFont(_FONTS["sans"]).face
            _GLYPH_COVERAGE = set(getattr(face, "charToGlyph", {}).keys())
            _FONT_COVERAGE["sans"] = _GLYPH_COVERAGE
            log.debug("PDF-Fonts geladen aus %s", fonts_dir)
        except Exception as exc:  # noqa: BLE001 — Fallback unten greift
            log.warning("DejaVu-Fonts konnten nicht geladen werden (%s) — Fallback auf Base-14", exc)

        # Zusatz-Fonts (Emoji + Math) — einzeln, fehlgeschlagen = optional
        for role, fname in _EXTRA_FONT_FILES.items():
            path = fonts_dir / fname
            if not path.is_file():
                continue
            try:
                name = f"DejaVu-{role}"
                pdfmetrics.registerFont(TTFont(name, str(path)))
                _FONTS[role] = name
                _FONT_COVERAGE[role] = set(
                    getattr(pdfmetrics.getFont(name).face, "charToGlyph", {}).keys()
                )
                log.debug("PDF-Zusatzfont %s geladen (%s)", role, fname)
            except Exception as exc:  # noqa: BLE001 — Zusatzfont ist optional
                log.warning("Zusatz-Font %s (%s) konnte nicht geladen werden: %s", role, fname, exc)
                _FONTS[role] = None
                _FONT_COVERAGE[role] = None
    else:
        log.warning("Keine DejaVu-Fonts gefunden — das PDF nutzt Base-14-Fallbacks")

    _FONTS_READY = True
    return _FONTS


# ─────────────────────────────────────────────────────────────────────────────
#  Text-Aufbereitung: Emojis & Unicode-Schriftarten bleiben sichtbar,
#  Notdef-frei, Markup-sicher
# ─────────────────────────────────────────────────────────────────────────────

#: Custom-Discord-Emojis ``<:name:id>`` / ``<a:name:id>``
_DISCORD_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d{10,25})>")
#: Mentions in Nachrichtsinhalten — lesbar umschreiben statt Roh-Syntax
_MEMBER_MENTION_RE = re.compile(r"<@!?(\d{10,25})>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d{10,25})>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d{10,25})>")
#: Demojize-Tokens ``:party_popper:`` / ``:reißzwecke:`` — muss mit einem
#: Buchstaben beginnen, damit Zeitangaben wie „12:30:00“ nicht zerstört werden.
_DEMOJI_TOKEN_RE = re.compile(r":([^\W\d_][\w+\-]{1,59}):", re.UNICODE)
#: Kontroll- und unsichtbare Zeichen (Bidi-Overrides, Zero-Width …). ZWJ
#: (U+200D) bleibt erhalten — es verbindet Emoji-Sequenzen (👨‍👩‍👦)!
_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f\u200b-\u200c\u200e-\u202e\u2066-\u2069\uFEFF]"
)
_MULTI_SPACE_RE = re.compile(r"[ \t\f\v]{2,}")


def _emoji_token_to_text(match: "re.Match[str]") -> str:
    token = match.group(1)
    return " " + token.replace("_", " ").replace("-", " ").strip() + " "


def _coverage_filter(s: str) -> str:
    """Entfernt Zeichen, die der Sans-Font nicht darstellen kann (Canvas-Pfade)."""
    if _GLYPH_COVERAGE is None:
        return s
    return "".join(ch for ch in s if ch == "\n" or ord(ch) in _GLYPH_COVERAGE)


#: Auflösung von Mentions zu Namen (wird pro Build gesetzt): ID → Name für
#: Mitglieder (@Name), Rollen (@Rollenname) und Kanäle (#kanalname).
_MENTION_NAMES: Dict[int, str] = {}


def _mentions_to_text(text: str) -> str:
    def _member(m: "re.Match[str]") -> str:
        name = _MENTION_NAMES.get(int(m.group(1)))
        return f"@{name}" if name else f"@nutzer-{m.group(1)}"

    def _role(m: "re.Match[str]") -> str:
        name = _MENTION_NAMES.get(int(m.group(1)))
        return f"@{name}" if name else f"@rolle-{m.group(1)}"

    def _channel(m: "re.Match[str]") -> str:
        name = _MENTION_NAMES.get(int(m.group(1)))
        return f"#{name}" if name else f"#kanal-{m.group(1)}"

    text = _MEMBER_MENTION_RE.sub(_member, text)
    text = _ROLE_MENTION_RE.sub(_role, text)
    text = _CHANNEL_MENTION_RE.sub(_channel, text)
    return text


def _prepare_text(text: Optional[str], *, keep_newlines: bool = False) -> str:
    """
    Roh-Text (Discord) → sauberer, druckbarer Text (noch NICHT HTML-escaped).

    Unicode-Emojis (👋, 🎉 …) und Unicode-„Schriftarten“ (𝗖𝗵𝗮𝘁 …) bleiben
    als echte Zeichen erhalten — die Paragraph-Renderer wählen dafür
    automatisch den Emoji- bzw. Math-Font (``_font_markup``). Custom-Emojis
    (``<:name:id>``) werden in ihren Namen aufgelöst, Mentions lesbar
    umschrieben, Steuerzeichen entfernt, Whitespace normalisiert.
    """
    if text is None:
        return ""
    s = str(text)
    s = _DISCORD_EMOJI_RE.sub(
        lambda m: " " + m.group(1).replace("_", " ").replace("-", " ").strip() + " ", s
    )
    s = _mentions_to_text(s)
    s = _DEMOJI_TOKEN_RE.sub(_emoji_token_to_text, s)
    s = _CONTROL_RE.sub(" ", s)
    s = s.replace("\r", "\n")
    if keep_newlines:
        s = re.sub(r"[ \t\f\v]+", " ", s)
        s = re.sub(r" ?\n ?", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
    else:
        s = s.replace("\n", " ")
        s = _MULTI_SPACE_RE.sub(" ", s)
    return s.strip()


def _esc(text: Optional[str]) -> str:
    """HTML-escaping für ReportLab-Paragraphs (nach ``_prepare_text``)."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─────────────────────────────────────────────────────────────────────────────
#  Apple-Emoji-Darstellung: Sprite-Sheet (iamcal/emoji-data) + Positions-Index.
#  Emojis werden als farbige <img>-Inline-Bilder gerendert (iOS-Look), statt
#  als monochrome Noto-Glyphen. Fallback (Sheet fehlt): monochromer Font.
# ─────────────────────────────────────────────────────────────────────────────

_EMOJI_APPLE_DIR = Path(__file__).resolve().parent / "emoji_apple"
_APPLE_INDEX_PATH = _EMOJI_APPLE_DIR / "index.json"
_APPLE_SHEET_PATH = _EMOJI_APPLE_DIR / "sheet_apple_64.webp"
#: Zellgröße im Apple-Sheet (4158 px / 66 px = 63 Spalten)
_APPLE_CELL = 66.0
#: Inline-Bildgröße in Paragraphs (Punkt)
_APPLE_IMG_PT = 10.6
_apple_sheet_img: Optional[Any] = None  # PIL-Image (lazy)
_apple_index: Dict[str, List[int]] = {}
_apple_tmpdir: Optional[str] = None
_apple_png_cache: Dict[str, str] = {}  # Key → Pfad der zugeschnittenen PNG

#: Candidate-Ranges für den Beginn/Fortlauf von Emoji-Sequenzen
def _emoji_cp(o: int) -> bool:
    return (
        0x1F000 <= o <= 0x1FAFF
        or 0x2600 <= o <= 0x27BF
        or 0x2B00 <= o <= 0x2BFF
        or 0x2300 <= o <= 0x23FF
        or 0x25A0 <= o <= 0x25FF
        or o in (0x200D, 0x20E3, 0xFE0E, 0xFE0F)
        or 0x1F1E6 <= o <= 0x1F1FF  # Regional-Indikatoren (Flaggen)
    )


def _ensure_apple_emoji() -> bool:
    """Lädt Index + Sheet (lazy). False, wenn Assets fehlen."""
    global _apple_tmpdir
    if _apple_tmpdir is not None:
        return bool(_apple_index)
    if not (_APPLE_INDEX_PATH.is_file() and _APPLE_SHEET_PATH.is_file()):
        _apple_tmpdir = ""
        return False
    try:
        with open(_APPLE_INDEX_PATH, "r", encoding="utf-8") as fh:
            _apple_index.update(json.load(fh))
        _apple_tmpdir = tempfile.mkdtemp(prefix="apple-emoji-")
        return True
    except (OSError, ValueError) as exc:
        log.warning("Apple-Emoji-Assets nicht ladbar: %s", exc)
        _apple_tmpdir = ""
        return False


def _close_apple_sheet() -> None:
    """Gibt das geladene Sprite-Sheet frei (RAM) — Crops bleiben als PNGs gecacht."""
    global _apple_sheet_img
    if _apple_sheet_img is not None:
        try:
            _apple_sheet_img.close()
        except Exception:  # noqa: BLE001 — Aufräumen darf nie werfen
            pass
        _apple_sheet_img = None


def _apple_emoji_png(key: str, pos: List[int]) -> Optional[str]:
    """Schneidet ein Emoji aus dem Apple-Sheet und liefert den PNG-Pfad."""
    global _apple_sheet_img
    cached = _apple_png_cache.get(key)
    if cached:
        return cached
    try:
        if _apple_sheet_img is None:
            if _PILImage is None:
                return None
            _apple_sheet_img = _PILImage.open(str(_APPLE_SHEET_PATH))
        x, y = pos
        c = _APPLE_CELL
        tile = _apple_sheet_img.crop(
            (round(x * c), round(y * c), round((x + 1) * c), round((y + 1) * c))
        )
        if tile.mode != "RGBA":
            tile = tile.convert("RGBA")
        path = os.path.join(_apple_tmpdir or "", f"{key}.png")
        tile.save(path, "PNG")
        _apple_png_cache[key] = path
        return path
    except Exception:  # noqa: BLE001 — Emoji-Bild darf nie den Build brechen
        log.debug("Apple-Emoji-Crop fehlgeschlagen: %s", key, exc_info=True)
        return None


def _apple_emoji_seq_img(run: str) -> Optional[str]:
    """
    Versucht, eine Emoji-Zeichenfolge (bereits aus dem Text gegriffen) als
    Apple-Sprite zu rendern → Pfad der PNG, sonst None. VS16/Variationen
    werden normalisiert (Sheet indiziert ohne/mit FE0F).
    """
    if not _ensure_apple_emoji():
        return None
    norm = []
    for ch in run:
        o = ord(ch)
        if o == 0xFE0E:  # VS15 (Text-Stil) — Sheet kennt nur Farb-Stil
            continue
        norm.append(o)
    keys = [
        "-".join(f"{cp:X}" for cp in norm),
        "-".join(f"{cp:X}" for cp in norm if cp != 0xFE0F),
    ]
    for key in keys:
        pos = _apple_index.get(key)
        if pos is not None:
            return _apple_emoji_png(key, pos)
    return None


def _apple_img_tag(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return (
        f'<img src="{path}" width="{_APPLE_IMG_PT}" height="{_APPLE_IMG_PT}" '
        'valign="middle"/>'
    )


def _font_markup(escaped: str) -> str:
    """
    Escaped Text → Paragraph-Markup mit Font-Wechseln pro Zeichenbereich.

    Priorität: Sans (DejaVu) → Emoji-Font (Noto Emoji) → Math-Font
    (Noto Sans Math). Zeichen, die KEIN verfügbarer Font darstellen kann,
    werden entfernt — so kann niemals ein „Notdef“-Kasten entstehen.
    Dadurch sind echte Emojis (👋, 🎉) und Unicode-Schriftarten (𝗖𝗵𝗮𝘁,
    𝕏, 𝔊) sichtbar, obwohl DejaVu sie nicht kennt. ``\\n`` wird zu ``<br/>``.
    """
    cov_sans = _FONT_COVERAGE.get("sans")
    cascade: List[Tuple[str, set]] = [
        (role, cov) for role in _EXTRA_FONT_ORDER
        if (cov := _FONT_COVERAGE.get(role)) is not None
    ]
    out: List[str] = []
    cur: Optional[str] = None

    def _cls(ch: str) -> Optional[str]:
        """None = Sans/Base (kein Tag), sonst Rolle — oder „drop“ als Sentinel."""
        o = ord(ch)
        if o in (0xFE0E, 0xFE0F, 0x200D, 0x20E3) or 0x1F3FB <= o <= 0x1F3FF:
            # Emoji-Modifier ohne eigene Bedeutung: im Mono-Fallback lieber
            # weglassen (Apple-Pfad rendert sie IN der Sequenz-Bilddatei).
            return "drop"
        if cov_sans is not None:
            if o in cov_sans:
                return None
        elif o < 0x100:  # kein DejaVu geladen: Base-14 deckt Latin-1 ab
            return None
        for role, cov in cascade:
            if o in cov:
                return role
        return "drop"

    use_apple = _ensure_apple_emoji()
    i = 0
    n = len(escaped)
    while i < n:
        ch = escaped[i]
        if ch == "\n":
            if cur is not None:
                out.append("</font>")
                cur = None
            out.append("<br/>")
            i += 1
            continue
        o = ord(ch)
        if use_apple and _emoji_cp(o):
            # Möglichst langen Emoji-Run greifen (Sequenzen mit VS16,
            # Hauttönen, ZWJ, Flaggen) und den längsten im Sheet bekannten
            # Prefix als Apple-Bild rendern — Rest fällt in die Kaskade.
            j = i + 1
            while j < n and _emoji_cp(ord(escaped[j])):
                j += 1
            run = escaped[i:j]
            matched = False
            for k in range(len(run), 0, -1):
                img = _apple_emoji_seq_img(run[:k])
                if img:
                    if cur is not None:
                        out.append("</font>")
                        cur = None
                    out.append(_apple_img_tag(img) or "")
                    i += k
                    matched = True
                    break
            if matched:
                continue
        cls = _cls(ch)
        if cls == "drop":
            # Kein Font kennt das Zeichen → wegfiltern (No-Notdef-Garantie).
            if cur is not None:
                out.append("</font>")
                cur = None
            i += 1
            continue
        if cls != cur:
            if cur is not None:
                out.append("</font>")
            if cls is not None:
                out.append(f"<font name='{_FONTS[cls]}'>")
            cur = cls
        out.append(ch)
        i += 1
    if cur is not None:
        out.append("</font>")
    return "".join(out)


def _clean(text: Optional[str]) -> str:
    """Einzeilig, escaped, druckbar — für Namen, IDs, Tabellenzellen."""
    return _font_markup(_esc(_prepare_text(text)))


def _clean_ml(text: Optional[str]) -> str:
    """Wie ``_clean``, behält aber Zeilenumbrüche (→ ``<br/>`` für Paragraphs)."""
    return _font_markup(_esc(_prepare_text(text, keep_newlines=True)))


def _short(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _cap(text: str, max_chars: int, max_lines: Optional[int] = None, note: str = " … (gekürzt)") -> str:
    """
    Kürzt bereits aufbereitetes (noch nicht escapedes) Textmaterial.

    Erst auf ``max_chars`` Zeichen, dann (optional) auf ``max_lines``
    Zeilenumbrüche — jeweils mit Hinweis am Ende. Verhindert, dass eine
    einzelne Tabelle/Paragraph höher als eine PDF-Seite wird.
    """
    if len(text) > max_chars:
        text = text[: max_chars - len(note)].rstrip() + note
    if max_lines is not None:
        lines = text.split("\n")
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines]) + note
    return text


def _id_para(value: Any, *, col_mm: float = 26.0) -> str:
    """
    Discord-ID als Monospace-Font-Ausschnitt, dessen Größe so berechnet ist,
    dass auch 18-stellige Snowflakes ohne Zeilenumbruch in die Spalte passen.
    """
    text = str(value)
    content_pt = col_mm * mm - 9  # Zell-Padding links+rechts + Sicherheitsrand
    size = min(6.4, content_pt / max(len(text), 1) / 0.64)
    size = max(size, 4.6)
    return f"<font name='{_FONTS['mono']}' size='{size:.1f}'>{text}</font>"


def _fmt_date(dt: Optional[datetime]) -> str:
    if not dt:
        return "unbekannt"
    return dt.strftime("%d.%m.%Y")


def _fmt_datetime(dt: Optional[datetime]) -> str:
    if not dt:
        return "unbekannt"
    return dt.strftime("%d.%m.%Y, %H:%M") + " UTC"


# ─────────────────────────────────────────────────────────────────────────────
#  Bild-Download & -Konvertierung
# ─────────────────────────────────────────────────────────────────────────────


def _finalize_image_sync(data: bytes) -> Optional[bytes]:
    """(Worker) Roh-Bytes → kleines, druckfertiges PNG (RAM-bewusst)."""
    try:
        im = _PILImage.open(io.BytesIO(data))
        iw, ih = im.size
        if iw <= 0 or ih <= 0:
            return None
        if iw * ih > _MAX_IMAGE_PIXELS:
            log.warning("Bild übersprungen (%dx%d px > %d MP) — RAM-Schutz.",
                        iw, ih, _MAX_IMAGE_PIXELS // 1_000_000)
            return None
        return _pil_to_png_bytes(im, max_px_w=_FINALIZE_MAX_PX_W, circle=False)
    except Exception:  # noqa: BLE001 — konvertierbares Bild oder gar keins
        log.debug("Bild-Konvertierung fehlgeschlagen (%d Bytes) — übersprungen.", len(data))
        return None


async def _finalize_image(data: Optional[bytes]) -> Optional[bytes]:
    """
    Roh-Bytes → finaler PNG-Bytesatz (max. ``_FINALIZE_MAX_PX_W`` breit).

    Ohne PIL werden die Rohbytes durchgereicht (ImageReader versucht sein
    Glück später). Die Konvertierung läuft max. 2× parallel und in einem
    Thread — der Event-Loop bleibt flott.
    """
    if not data:
        return None
    if len(data) > _MAX_IMAGE_BYTES:
        log.warning("Bild übersprungen (%.1f MB > %d MB Limit) — RAM-Schutz.",
                    len(data) / 1_000_000, _MAX_IMAGE_BYTES // 1_000_000)
        return None
    if _PILImage is None:
        return data
    async with _CONVERT_SEM:
        return await asyncio.to_thread(_finalize_image_sync, data)


async def _download_image(
    session: aiohttp.ClientSession,
    url: str,
    cache: Dict[str, Optional[bytes]],
    semaphore: asyncio.Semaphore,
) -> None:
    """Lädt ein Bild in den Byte-Cache (fehlertolerant, mit Hart-Timeout)."""
    if url in cache:
        return
    async with semaphore:
        if url in cache:
            return
        started = time.monotonic()
        try:
            # asyncio.wait_for als Sicherheitsnetz ÜBER dem aiohttp-Timeout:
            # fängt auch Hänger ab, die der Request-Timeout nicht erreicht
            # (z. B. DNS-/Connector-Stalls vor dem eigentlichen Request).
            async def _fetch() -> Optional[bytes]:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT_S)
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.read()

            data = await asyncio.wait_for(_fetch(), timeout=_IMAGE_TIMEOUT_S * 1.5)
            # RAM-Kernfix: Jedes Bild wird SOFORT zu einem kleinen finalen PNG
            # konvertiert und die Rohdaten verworfen. Vorher lagen im Cache
            # alle Roh-Downloads gleichzeitig (bei großen Anhängen hunderte MB
            # → OOM-Kill des Containers: „Einfrieren" OHNE jegliches Log,
            # Render startet neu, die Fortschritts-Nachricht bleibt stehen).
            cache[url] = await _finalize_image(data) if data else None
        except asyncio.CancelledError:
            raise  # Budget-Abschaltung — NICHT als Bildfehler verschlucken
        except Exception as exc:  # noqa: BLE001 — ein fehlendes Bild darf nicht abbrechen
            log.debug("Bild konnte nicht geladen werden %s: %s", url, exc)
            cache[url] = None
        finally:
            _dur = time.monotonic() - started
            if _dur > _SLOW_IMAGE_S:
                log.warning("Langsamer Bild-Download (%.1f s): %s", _dur, url[:120])


async def _download_images(
    session: aiohttp.ClientSession,
    urls: List[Optional[str]],
    progress_hook: Optional[Callable[[int, int], Any]] = None,
) -> Dict[str, Optional[bytes]]:
    """
    Lädt alle Bild-URLs parallel (max. 8 gleichzeitig) → ``{url: bytes|None}``.

    Ist gegen „ewiges Hängen“ gefeit: Jeder Request hat einen Hart-Timeout
    UND die gesamte Phase hat ein Budget (``_IMAGES_BUDGET_S``). Läuft das
    Budget ab, wird abgebrochen und mit den bereits geladenen Bildern
    weitergebaut (fehlende Bilder werden einfach nicht eingebettet — der
    PDF-Bau bricht nie wegen eines Bildes). ``progress_hook(done, total)``
    meldet sichtbaren Fortschritt zwischen den Prozent-Schritten.
    """
    cache: Dict[str, Optional[bytes]] = {}
    unique = [u for u in dict.fromkeys(u for u in urls if u)]
    if not unique:
        return cache
    total = len(unique)
    semaphore = asyncio.Semaphore(8)
    started = time.monotonic()
    done = [0]

    async def _one_counted(u: str) -> None:
        await _download_image(session, u, cache, semaphore)
        done[0] += 1
        if progress_hook is not None and (
            done[0] % _IMAGES_PROGRESS_EVERY == 0 or done[0] == total
        ):
            try:
                await progress_hook(done[0], total)
            except Exception:  # noqa: BLE001 — Fortschritt darf nie abbrechen
                pass

    log.info("Server-Dokument: %d Bild-URLs werden geladen …", total)
    try:
        await asyncio.wait_for(
            asyncio.gather(*(_one_counted(u) for u in unique)),
            timeout=_IMAGES_BUDGET_S,
        )
    except (asyncio.TimeoutError, TimeoutError):
        log.warning(
            "Server-Dokument: Bild-Download-Budget (%.0f s) erschöpft — fahre mit "
            "%d/%d geladenen Bildern fort (Rest wird nicht eingebettet).",
            _IMAGES_BUDGET_S, done[0], total,
        )
    except Exception:  # noqa: BLE001 — Download-Fehler dürfen nie den Build killen
        log.exception("Server-Dokument: unerwarteter Fehler im Bild-Download — fahre fort.")
    ok = sum(1 for v in cache.values() if v)
    fail = total - len(cache)
    log.info(
        "Server-Dokument: %d/%d Bilder geladen (%d fehlgeschlagen) in %.1f s",
        ok, total, fail, time.monotonic() - started,
    )
    return cache


def _pil_to_png_bytes(im: "_PILImage.Image", *, max_px_w: int, circle: bool) -> Optional[bytes]:
    """Wandelt ein PIL-Bild in ein druckfertiges PNG (skaliert, optional kreisförmig)."""
    try:
        # Pixel-Guard VOR jedem Dekodieren: PIL.open ist lazy — im.size liest
        # nur den Header. Übergroße Bilder (Dekodier-Bomben) werden übersprungen,
        # statt den Worker-Thread (und damit den Dokument-Bau) für Minuten zu
        # blockieren.
        _iw0, _ih0 = im.size
        if _iw0 * _ih0 > _MAX_IMAGE_PIXELS:
            log.warning("Bild übersprungen (%dx%d px > %d MP) — Dekodier-Schutz.",
                        _iw0, _ih0, _MAX_IMAGE_PIXELS // 1_000_000)
            return None
        if getattr(im, "is_animated", False):
            im.seek(0)
        if getattr(im, "format", "") == "JPEG":
            # JPEG-Draft: dekodiert direkt in reduzierter Auflösung —
            # dramatisch weniger RAM-Spitze bei großen Fotos.
            try:
                im.draft("RGB", (_FINALIZE_MAX_PX_W, _FINALIZE_MAX_PX_W))
            except Exception:  # noqa: BLE001 — draft ist optional
                pass
        has_alpha = im.mode in ("RGBA", "LA", "P") or "transparency" in getattr(im, "info", {})
        if has_alpha:
            im = im.convert("RGBA")
        elif im.mode != "RGB":
            im = im.convert("RGB")

        iw, ih = im.size
        if iw <= 0 or ih <= 0:
            return None
        scale = min(1.0, max_px_w / iw)
        tw, th = max(1, round(iw * scale)), max(1, round(ih * scale))
        if (tw, th) != (iw, ih):
            im = im.resize((tw, th), _PILImage.LANCZOS)

        if circle:
            # Runde Avatare/Icons wie in Discord (Alpha-Maske)
            side = min(im.size)
            left = (im.width - side) // 2
            top = (im.height - side) // 2
            im = im.crop((left, top, left + side, top + side))
            im = im.resize((max(16, side), max(16, side)), _PILImage.LANCZOS)
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            mask = _PILImage.new("L", (side * 4, side * 4), 0)
            from PIL import ImageDraw as _PILDraw

            _PILDraw.Draw(mask).ellipse((0, 0, side * 4 - 1, side * 4 - 1), fill=255)
            mask = mask.resize(im.size, _PILImage.LANCZOS)
            im.putalpha(mask)

        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.debug("Bildkonvertierung fehlgeschlagen: %s", exc)
        return None


def _image_flow(
    data: Optional[bytes],
    *,
    max_w: float,
    max_h: float,
    upscale: bool = False,
    circle: bool = False,
) -> Optional[Image]:
    """
    Bild-Bytes → in die Box (mm) eingepasstes ``Image``-Flowable.

    Ohne Pillow wird ein Fallback über ``ImageReader`` versucht (PNG/JPEG),
    sonst ``None`` — der Build bricht nie wegen eines Bildes.
    """
    if not data:
        return None

    png_bytes: Optional[bytes] = None

    if _PILImage is not None:
        try:
            im = _PILImage.open(io.BytesIO(data))
            png_bytes = _pil_to_png_bytes(
                im, max_px_w=max(int(max_w * RENDER_PX_PER_MM), 32), circle=circle
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("PIL-Konvertierung fehlgeschlagen (%s) — versuche Raw-Reader", exc)
            png_bytes = None

    payload = png_bytes if png_bytes else data
    try:
        probe = ImageReader(io.BytesIO(payload))
        native_w, native_h = probe.getSize()
    except Exception as exc:  # noqa: BLE001
        log.debug("Bild nicht darstellbar: %s", exc)
        return None

    if native_w <= 0 or native_h <= 0:
        return None

    # Zieldimensionen: Native-Größe (96 dpi angenommen), in Box eingepasst
    w_mm = native_w / PX_PER_MM
    h_mm = native_h / PX_PER_MM
    scale = min(max_w / w_mm, max_h / h_mm)
    if not upscale:
        scale = min(scale, 1.0)
    # Sicherheitsklemmen: kein Bild darf breiter als der Satzspiegel oder
    # höher als ~40 % der Seitenhöhe werden — egal was der CDN liefert
    # (pfadologische Bildmaße dürfen den Build niemals sprengen).
    scale = min(
        scale,
        (CONTENT_WIDTH / mm) / max(w_mm, 0.01),
        120.0 / max(h_mm, 0.01),
    )
    w_mm, h_mm = w_mm * scale, h_mm * scale
    # Frisches BytesIO: das Image-Flowable liest erst beim Zeichnen
    return Image(io.BytesIO(payload), width=w_mm * mm, height=h_mm * mm)


def _avatar_flow(data: Optional[bytes], size_mm: float) -> Optional[Image]:
    """Kreisförmiger Avatar/Icon in exakter Quadratgröße."""
    flow = _image_flow(data, max_w=size_mm, max_h=size_mm, upscale=True, circle=True)
    if flow is None:
        return None
    flow.drawWidth = size_mm * mm
    flow.drawHeight = size_mm * mm
    return flow


# ─────────────────────────────────────────────────────────────────────────────
#  Stile
# ─────────────────────────────────────────────────────────────────────────────


def _styles() -> Dict[str, ParagraphStyle]:
    f = _ensure_fonts()
    return {
        "title": ParagraphStyle(
            "title", fontName=f["bold"], fontSize=19, leading=23,
            textColor=_C.dark, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", fontName=f["sans"], fontSize=8.6, leading=12,
            textColor=_C.muted, spaceAfter=1,
        ),
        "h1": ParagraphStyle(
            "h1", fontName=f["bold"], fontSize=14, leading=18,
            textColor=_C.blurple, spaceBefore=6, spaceAfter=3,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=f["bold"], fontSize=10.8, leading=14,
            textColor=_C.dark, spaceBefore=8, spaceAfter=3,
        ),
        "h3": ParagraphStyle(
            "h3", fontName=f["bold"], fontSize=9.4, leading=12.5,
            textColor=_C.dark, spaceBefore=5, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body", fontName=f["sans"], fontSize=9.3, leading=13,
            textColor=_C.text, spaceAfter=2,
        ),
        "small": ParagraphStyle(
            "small", fontName=f["sans"], fontSize=7.9, leading=10.6,
            textColor=_C.muted, spaceAfter=1,
        ),
        "table": ParagraphStyle(
            "table", fontName=f["sans"], fontSize=8.4, leading=11.4,
            textColor=_C.text,
        ),
        "table_small": ParagraphStyle(
            "table_small", fontName=f["sans"], fontSize=7.8, leading=10.4,
            textColor=_C.text,
        ),
        "table_muted": ParagraphStyle(
            "table_muted", fontName=f["sans"], fontSize=7.6, leading=10,
            textColor=_C.muted,
        ),
        "table_head": ParagraphStyle(
            "table_head", fontName=f["bold"], fontSize=8.4, leading=11,
            textColor=_C.white,
        ),
        "table_head_small": ParagraphStyle(
            "table_head_small", fontName=f["bold"], fontSize=7.6, leading=10,
            textColor=_C.white,
        ),
        "mono": ParagraphStyle(
            "mono", fontName=f["mono"], fontSize=7.0, leading=9.6,
            textColor=_C.muted,
        ),
        "msg_head": ParagraphStyle(
            "msg_head", fontName=f["sans"], fontSize=8.6, leading=11.6,
            textColor=_C.text, spaceAfter=1,
        ),
        "msg_body": ParagraphStyle(
            "msg_body", fontName=f["sans"], fontSize=8.8, leading=12.2,
            textColor=_C.text,
        ),
        "msg_channel_head": ParagraphStyle(
            "msg_channel_head", fontName=f["bold"], fontSize=12.6, leading=16.4,
            textColor=_C.dark, spaceBefore=2, spaceAfter=1,
        ),
        "msg_channel_meta": ParagraphStyle(
            "msg_channel_meta", fontName=f["sans"], fontSize=7.8, leading=16.4,
            textColor=_C.muted,
        ),
        "chip_white": ParagraphStyle(
            "chip_white", fontName=f["bold"], fontSize=7.4, leading=9.4,
            textColor=_C.white,
        ),
        "chip_dark": ParagraphStyle(
            "chip_dark", fontName=f["bold"], fontSize=7.4, leading=9.4,
            textColor=_C.amber_text,
        ),
        "cat_head": ParagraphStyle(
            "cat_head", fontName=f["bold"], fontSize=9.2, leading=12,
            textColor=_C.white,
        ),
        "cat_meta": ParagraphStyle(
            "cat_meta", fontName=f["sans"], fontSize=7.4, leading=12,
            textColor=colors.HexColor("#C9CDD6"), alignment=TA_RIGHT,
        ),
        "embed_text": ParagraphStyle(
            "embed_text", fontName=f["sans"], fontSize=8.0, leading=11,
            textColor=_C.text,
        ),
        "center_small": ParagraphStyle(
            "center_small", fontName=f["sans"], fontSize=7.6, leading=9.8,
            textColor=_C.text, alignment=TA_CENTER,
        ),
        "center_tiny": ParagraphStyle(
            "center_tiny", fontName=f["mono"], fontSize=6.4, leading=8.6,
            textColor=_C.muted, alignment=TA_CENTER,
        ),
        "kv_label": ParagraphStyle(
            "kv_label", fontName=f["bold"], fontSize=7.8, leading=10.6,
            textColor=_C.muted,
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Kleine Bausteine: Chips, Tabellen-Stile, Karten
# ─────────────────────────────────────────────────────────────────────────────


def _luma(c: colors.Color) -> float:
    """Helligkeit einer Farbe (0–1) — für Kontrast-Entscheidungen."""
    return 0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue


def _chip(text: str, bg: colors.Color, dark_text: bool = False) -> Table:
    """
    Abgerundetes Farbetag (in Tabellenzellen und Chips-Zeilen verwendbar).

    Die Breite wird exakt am Label gemessen, damit der Text nie umbricht;
    die gemessene Breite liegt in ``chip._adse_width`` (Punkt).
    """
    label = _prepare_text(text)
    font = _FONTS["bold"]
    width = pdfmetrics.stringWidth(label, font, 7.4) + 9  # + Padding links/rechts
    inner = Table([[Paragraph(_font_markup(_esc(label)), _styles()["chip_dark" if dark_text else "chip_white"])]],
                  colWidths=[width])
    inner._adse_width = width
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("ROUNDEDCORNERS", [2.6, 2.6, 2.6, 2.6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3.4),
        ("TOPPADDING", (0, 0), (-1, -1), 1.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8),
    ]))
    return inner


def _chips_row(chips: List[Tuple[str, colors.Color, bool]]) -> Table:
    """Eine Zeile aus Chips mit festen Abständen (jede Spalte intrinsisch breit)."""
    cells: List[Any] = []
    widths: List[Any] = []
    for i, (label, bg, dark) in enumerate(chips):
        if i:
            cells.append("")
            widths.append(2.2 * mm)
        chip = _chip(label, bg, dark)
        cells.append(chip)
        widths.append(chip._adse_width)
    row = Table([cells], colWidths=widths, hAlign="LEFT")
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return row


_ZEBRA = ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_C.white, _C.lighter])


# ─────────────────────────────────────────────────────────────────────────────
#  Inline-Bilder in Nachrichten: Custom-Emojis + Bildanhänge
# ─────────────────────────────────────────────────────────────────────────────

_custom_png_cache: Dict[str, str] = {}


def _custom_emoji_png(data: Optional[bytes], key: str) -> Optional[str]:
    """Custom-Emoji-Bytes → kleine PNG-Datei (64 px) für Inline-Bilder."""
    cached = _custom_png_cache.get(key)
    if cached:
        return cached
    if not data or _PILImage is None or not _ensure_apple_emoji():
        return None
    try:
        im = _PILImage.open(io.BytesIO(data))
        im.seek(0)  # animierte GIFs: erster Frame
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        im.thumbnail((64, 64))
        path = os.path.join(_apple_tmpdir or "", f"custom-{key}.png")
        im.save(path, "PNG")
        _custom_png_cache[key] = path
        return path
    except Exception:  # noqa: BLE001 — Bildfehler darf nie crashen
        log.debug("Custom-Emoji-PNG fehlgeschlagen: %s", key, exc_info=True)
        return None


def _custom_emoji_img_tag(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return (
        f'<img src="{path}" width="{_APPLE_IMG_PT}" height="{_APPLE_IMG_PT}" '
        'valign="middle"/>'
    )


def _message_content_markup(
    raw_text: str, *, emoji_png_by_id: Callable[[int], Optional[str]]
) -> str:
    """
    Nachrichteninhalt → Paragraph-Markup; Custom-Emojis (<:name:id>) werden
    als echte Inline-Bilder gerendert (statt als Name-Text).
    """
    parts: List[str] = []
    last = 0

    def _plain(seg: str) -> str:
        return _font_markup(_esc(_prepare_text(seg, keep_newlines=True))).replace("\n", "<br/>")

    for m in _DISCORD_EMOJI_RE.finditer(raw_text):
        parts.append(_plain(raw_text[last:m.start()]))
        name = m.group(1).replace("_", " ").replace("-", " ").strip()
        try:
            eid = int(m.group(2))
        except ValueError:
            eid = -1
        tag = _custom_emoji_img_tag(emoji_png_by_id(eid))
        if tag:
            parts.append(tag)
        else:
            parts.append(_esc(name))
        last = m.end()
    parts.append(_plain(raw_text[last:]))
    return "".join(parts)


def _attachment_is_image(att: Any) -> bool:
    ctype = (getattr(att, "content_type", None) or "").lower()
    if ctype.startswith("image/"):
        return True
    name = (getattr(att, "filename", "") or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


# ─────────────────────────────────────────────────────────────────────────────
#  Komponenten (Bot-Nachrichten, Layouts v2) als echte Mini-Darstellung
# ─────────────────────────────────────────────────────────────────────────────

_BUTTON_CHIP_COLORS = {
    "primary": (_C.blurple, False),
    "secondary": (colors.HexColor("#4E5058"), False),
    "success": (_C.green, False),
    "danger": (colors.HexColor("#DA373C"), False),
    "link": (colors.HexColor("#4E5058"), False),
    "premium": (colors.HexColor("#F47B67"), False),
}
_MAX_COMPONENT_MEDIA = 30
_MAX_COMPONENT_TEXT_CHARS = 300


def _component_media_urls(components: List[Any]) -> List[str]:
    """Sammelt Media-URLs aus Komponenten (Galerien, Vorschaubilder, Dateien)."""
    urls: List[str] = []

    def _walk(comps: Any) -> None:
        for comp in comps or []:
            cname = getattr(getattr(comp, "type", None), "name", None) or ""
            if cname == "media_gallery":
                for item in (getattr(comp, "items", None) or getattr(comp, "attachments", None) or []):
                    u = getattr(getattr(item, "media", None), "url", None) or getattr(item, "url", None)
                    if u:
                        urls.append(u)
            elif cname == "thumbnail":
                u = getattr(getattr(comp, "media", None), "url", None) or getattr(comp, "url", None)
                if u:
                    urls.append(u)
            elif cname == "file":
                u = getattr(getattr(comp, "file", None), "url", None) or getattr(getattr(comp, "media", None), "url", None)
                if u:
                    urls.append(u)
            _walk(getattr(comp, "children", None))

    _walk(components)
    return urls[:_MAX_COMPONENT_MEDIA]


def _button_chip(label: str, style_name: str, *, is_link: bool) -> Table:
    """Rundet eine Schaltfläche als Farb-Chip ab (nicht klickbar im PDF)."""
    bg, dark_text = _BUTTON_CHIP_COLORS.get(style_name, (colors.HexColor("#4E5058"), False))
    text = _prepare_text(label) or ("Link" if is_link else "Button")
    if is_link:
        text = "↗ " + text
    font = _FONTS["sans"]
    size = 7.2
    width = min(pdfmetrics.stringWidth(text, font, size) + 10, CONTENT_WIDTH * 0.55)
    inner = Table([[Paragraph(
        _font_markup(_esc(text)),
        ParagraphStyle(
            "btn_chip", fontName=font, fontSize=size, leading=9.2,
            textColor=(colors.white if not dark_text else _C.dark),
        ),
    )]], colWidths=[width])
    inner._btn_w = width
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
    ]))
    return inner


def _select_chip(label: str, option_count: int) -> Table:
    text = "▾ " + (_prepare_text(label) or "Auswählen")
    if option_count:
        text += f"  ({option_count} Optionen)"
    width = min(pdfmetrics.stringWidth(text, _FONTS["sans"], 7.2) + 10, CONTENT_WIDTH * 0.6)
    inner = Table([[Paragraph(
        _font_markup(_esc(text)),
        ParagraphStyle("sel_chip", fontName=_FONTS["sans"], fontSize=7.2, leading=9.2, textColor=_C.text),
    )]], colWidths=[width])
    inner._btn_w = width
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E7E9EE")),
        ("BOX", (0, 0), (-1, -1), 0.7, _C.border),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
    ]))
    return inner


def _chips_row_of(chips: List[Table]) -> Table:
    """Mehrere fertige Chips mit Abstand nebeneinander (zeilenumbruchfrei)."""
    cells: List[Any] = []
    widths: List[float] = []
    for i, chip in enumerate(chips):
        if i:
            cells.append(Spacer(2 * mm, 1))
            widths.append(2 * mm)
        cells.append(chip)
        widths.append(chip._btn_w)
    t = Table([cells], colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _components_flowables(components: List[Any], ctx: Dict[str, Any]) -> List[Any]:
    """
    Bot-Komponenten als echte Mini-Darstellung: Text-Displays als Text,
    Trennlinien als Linien, Medien als Bilder, Buttons/Auswahlmenüs als Chips.
    """
    out: List[Any] = []
    styles = ctx["styles"]

    def _walk(comps: Any) -> None:
        for comp in comps or []:
            cname = getattr(getattr(comp, "type", None), "name", None) or ""
            if cname in ("action_row",):
                chips: List[Table] = []
                for child in getattr(comp, "children", None) or []:
                    ch_name = getattr(getattr(child, "type", None), "name", None) or ""
                    if ch_name == "button":
                        chips.append(_button_chip(
                            _prepare_text(getattr(child, "label", None) or ""),
                            str(getattr(getattr(child, "style", None), "name", "") or "").lower(),
                            is_link=str(getattr(getattr(child, "style", None), "name", "") or "").lower() == "link",
                        ))
                    elif ch_name in ("string_select", "user_select", "role_select",
                                     "channel_select", "mentionable_select"):
                        chips.append(_select_chip(
                            _prepare_text(getattr(child, "placeholder", None) or ""),
                            len(getattr(child, "options", None) or []),
                        ))
                if chips:
                    out.append(Spacer(1, 0.8 * mm))
                    out.append(_chips_row_of(chips[:5]))
            elif cname == "container":
                # Container: Kinder rekursiv rendern, dann als Box mit
                # Akzentkante zusammenfassen (wie ein Embed).
                start = len(out)
                _walk(getattr(comp, "children", None))
                children_flows = out[start:]
                if children_flows:
                    del out[start:]
                    box = Table([[children_flows]], colWidths=[CONTENT_WIDTH - 30 * mm])
                    box.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F7")),
                        ("LINEBEFORE", (0, 0), (0, -1), 2.2, _C.blurple),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
                    ]))
                    out.append(Spacer(1, 1 * mm))
                    out.append(box)
            elif cname == "text_display":
                content = _prepare_text(getattr(comp, "content", None) or "", keep_newlines=True)
                if content:
                    out.append(Paragraph(
                        _font_markup(_esc(_cap(content, _MAX_COMPONENT_TEXT_CHARS, 8))),
                        styles["msg_body"],
                    ))
            elif cname == "separator":
                out.append(Spacer(1, 1 * mm))
                out.append(HRFlowable(width="100%", thickness=0.7, color=_C.border, spaceAfter=1))
            elif cname == "media_gallery":
                media = getattr(comp, "items", None) or getattr(comp, "attachments", None) or []
                imgs: List[Any] = []
                for item in media[:4]:
                    u = getattr(getattr(item, "media", None), "url", None) or getattr(item, "url", None)
                    flow = ctx["img"](u, max_w=44, max_h=44) if u else None
                    if flow is not None:
                        imgs.append(flow)
                if imgs:
                    out.append(Spacer(1, 1 * mm))
                    row: List[Any] = []
                    for i, f in enumerate(imgs):
                        if i:
                            row.append(Spacer(2 * mm, 1))
                        row.append(f)
                    t = Table([row], hAlign="LEFT")
                    t.setStyle(TableStyle([
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]))
                    out.append(t)
            elif cname == "thumbnail":
                u = getattr(getattr(comp, "media", None), "url", None) or getattr(comp, "url", None)
                flow = ctx["img"](u, max_w=26, max_h=26) if u else None
                if flow is not None:
                    out.append(Spacer(1, 1 * mm))
                    out.append(flow)
            elif cname == "section":
                _walk(getattr(comp, "children", None))
                _walk([getattr(comp, "accessory", None)] if getattr(comp, "accessory", None) else [])
            elif cname == "file":
                fobj = getattr(comp, "file", None) or getattr(comp, "media", None)
                name = _prepare_text(getattr(fobj, "filename", None) or "") or "Datei"
                out.append(Paragraph(f"📎 {_clean(name)}", styles["table_muted"]))
            elif cname == "button":
                out.append(Spacer(1, 0.8 * mm))
                out.append(_button_chip(
                    _prepare_text(getattr(comp, "label", None) or ""),
                    str(getattr(getattr(comp, "style", None), "name", "") or "").lower(),
                    is_link=str(getattr(getattr(comp, "style", None), "name", "") or "").lower() == "link",
                ))
            elif cname in ("string_select", "user_select", "role_select",
                           "channel_select", "mentionable_select"):
                out.append(Spacer(1, 0.8 * mm))
                out.append(_select_chip(
                    _prepare_text(getattr(comp, "placeholder", None) or ""),
                    len(getattr(comp, "options", None) or []),
                ))

    _walk(components)
    return out


def _gray_chip(label: str, max_width: Optional[float] = None) -> Table:
    """
    Abgerundetes graues Kästchen (Mitglieder-Rollen). Breite = Label-Breite;
    bei sehr langen Rollennamen wird das Label mit „…“ auf ``max_width``
    gekürzt, damit kein Kästchen die Spalte sprengt.
    """
    text = _prepare_text(label)
    font = _FONTS["sans"]
    size = 7.0
    if max_width is not None:
        while len(text) > 1 and pdfmetrics.stringWidth(text, font, size) + 8 > max_width:
            text = text[:-2].rstrip() + "…"
    width = pdfmetrics.stringWidth(text, font, size) + 8
    inner = Table([[Paragraph(
        _font_markup(_esc(text)),
        ParagraphStyle("chip_gray", fontName=font, fontSize=size, leading=8.8, textColor=_C.text),
    )]], colWidths=[width])
    inner._gray_chip_w = width
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E7E9EE")),
        ("ROUNDEDCORNERS", [3, 3, 3, 3]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 1.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
    ]))
    return inner


def _chip_lines(labels: List[str], max_width: float) -> List[Any]:
    """
    Packt graue Rollen-Kästchen mit Padding nebeneinander (greedy) und
    untereinander als Zeilen — jede Zeile eine kleine Tabelle, linksbündig.
    """
    gap = 4.0
    lines: List[List[Table]] = [[]]
    line_widths: List[float] = [0.0]
    for label in labels:
        chip = _gray_chip(label, max_width)
        w = chip._gray_chip_w
        extra = w if not lines[-1] else gap + w
        if lines[-1] and line_widths[-1] + extra > max_width:
            lines.append([chip])
            line_widths.append(w)
        else:
            lines[-1].append(chip)
            line_widths[-1] += extra
    out: List[Any] = []
    for row in lines:
        if not row:
            continue
        cells: List[Any] = []
        widths: List[float] = []
        for i, c in enumerate(row):
            if i:
                cells.append(Spacer(gap, 1))
                widths.append(gap)
            cells.append(c)
            widths.append(c._gray_chip_w)
        t = Table([cells], colWidths=widths, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        out.append(t)
        out.append(Spacer(1, 1.6))
    if out:
        out.pop()  # letzter Zeilen-Abstand entfällt
    return out or [Paragraph("—", _styles()["table_small"])]


def _table_style(zebra: bool = True) -> TableStyle:
    cmds: List[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), _C.dark),
        ("TEXTCOLOR", (0, 0), (-1, 0), _C.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, _C.border),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, 0), 3.2),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3.4),
    ]
    if zebra:
        cmds.append(_ZEBRA)
    return TableStyle(cmds)


def _section(flowables: List[Any], num: str, title: str, styles: Dict[str, ParagraphStyle]) -> None:
    """Nummerierter Abschnittstitel mit Farbakzent."""
    flowables.append(Spacer(1, 2 * mm))
    flowables.append(Paragraph(f"{num} · {_clean(title)}", styles["h1"]))
    flowables.append(HRFlowable(width="100%", thickness=1.1, color=_C.blurple, spaceAfter=2.5 * mm))


def _kv_card(rows: List[Tuple[str, Any]], styles: Dict[str, ParagraphStyle]) -> Table:
    """Info-Kärtchen: zweispaltige Label/Wert-Tabelle mit hellem Hintergrund."""
    data = []
    for label, value in rows:
        label_para = Paragraph(f"<b>{_clean(label)}</b>", styles["kv_label"])
        data.append([label_para, value])
    t = Table(data, colWidths=[30 * mm, CONTENT_WIDTH - 30 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _C.light),
        ("ROUNDEDCORNERS", [3, 3, 3, 3]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E2E5EB")),
    ]))
    return t


# ─────────────────────────────────────────────────────────────────────────────
#  Typ-Mappings
# ─────────────────────────────────────────────────────────────────────────────

_CHANNEL_TYPE_INFO: Dict[Any, Tuple[str, colors.Color, bool]] = {
    discord.ChannelType.text: ("Text", _C.blurple, False),
    discord.ChannelType.voice: ("Voice", _C.green, False),
    discord.ChannelType.stage_voice: ("Bühne", _C.amber, True),
    discord.ChannelType.forum: ("Forum", _C.pink, False),
    discord.ChannelType.news: ("News", _C.cyan, False),
    discord.ChannelType.media: ("Medien", _C.pink, False),
}


def _channel_type_info(ch_type: Any) -> Tuple[str, colors.Color, bool]:
    return _CHANNEL_TYPE_INFO.get(
        ch_type, (str(getattr(ch_type, "name", ch_type)), _C.gray_chip, False)
    )


_VERIFICATION_LABELS = {
    "none": "Keine", "low": "Niedrig", "medium": "Mittel", "high": "Hoch", "highest": "Sehr hoch",
}
_CONTENT_FILTER_LABELS = {
    "disabled": "Aus", "no_role": "Keine Rollen-Mitglieder", "all_members": "Alle Mitglieder",
}


def _enum_label(value: Any, labels: Dict[str, str], fallback_prefix: str = "") -> str:
    name = str(getattr(value, "name", value) or "").lower()
    if name in labels:
        return labels[name]
    pretty = name.replace("_", " ").strip()
    return (fallback_prefix + pretty) if pretty else "unbekannt"


def _premium_label(guild: discord.Guild) -> str:
    tier = getattr(guild, "premium_tier", None)
    try:
        tier_val = int(getattr(tier, "value", tier) or 0)
    except (TypeError, ValueError):
        tier_val = 0
    boosts = getattr(guild, "premium_subscription_count", 0) or 0
    label = "kein Boost-Level" if tier_val == 0 else f"Level {tier_val}"
    if boosts:
        label += f" · {boosts} Boosts"
    return label


# ─────────────────────────────────────────────────────────────────────────────
#  Wichtigste Mitglieder (seltenste Rollen, sonst zufällig)
# ─────────────────────────────────────────────────────────────────────────────


async def _ensure_member_list(
    guild: discord.Guild, member_count: int, members: List[Any],
    progress: Optional[ProgressCallback] = None,
) -> Tuple[int, List[Any]]:
    """
    Stellt sicher, dass die Mitgliederliste aussagekräftig ist: Der Cache kann
    bei großen Servern (Mitglieder-Intent/Chunking nicht gelaufen) LEER sein —
    dann zeigte das Dokument „Alle 1 Mitglieder“ mit nur dem Bot. Fallback:
    ``fetch_members`` (best effort); Misslingen wird transparent dokumentiert.
    """
    target = min(member_count or 0, MAX_MEMBERS_DETAILED)
    if len(members) >= max(target, 1):
        return member_count, members
    try:
        async def _fetch_all() -> List[Any]:
            return [m async for m in guild.fetch_members(limit=max(target, 1))]

        # Hart-Budget: ein hängender fetch_members darf den Dokument-Bau
        # niemals blockieren (wie die Bild-Download-Phase).
        fetched = await asyncio.wait_for(_fetch_all(), timeout=45)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("fetch_members für %s überschritt 45 s — fahre mit Cache-Fortschritt fort.",
                    getattr(guild, "id", "?"))
        fetched = []
    except (discord.Forbidden, discord.HTTPException, discord.NotFound, AttributeError, TypeError):
        fetched = []
    if fetched:
        by_id = {m.id: m for m in fetched}
        by_id.update({m.id: m for m in members})
        members = list(by_id.values())
        if progress is not None:
            await progress(6, f"Mitgliederliste geladen ({len(members)}) …")
    member_count = max(member_count or 0, len(members))
    return member_count, members


def _select_important_members(members: List[discord.Member], guild: discord.Guild) -> List[discord.Member]:
    """
    Wählt die ``MAX_MEMBERS_DETAILED`` wichtigsten Mitglieder aus.

    Wichtigkeit = Summe der Kehrwerte der Rollen-Häufigkeiten (je seltener eine
    Rolle, desto wichtiger der Inhaber) + kleiner Zufallsanteil für
    Unentschieden.
    """
    role_counts: Dict[int, int] = {}
    for role in guild.roles:
        role_counts[role.id] = sum(1 for m in guild.members if role in m.roles)

    scored: List[Tuple[float, discord.Member]] = []
    for member in members:
        score = sum(
            1.0 / max(1, role_counts.get(role.id, 1))
            for role in member.roles
            if role != guild.default_role
        )
        scored.append((score + random.random() * 0.001, member))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [member for _, member in scored[:MAX_MEMBERS_DETAILED]]


# ─────────────────────────────────────────────────────────────────────────────
#  Nachrichten → Fließtext (Embeds, Komponenten, Anhänge inclusive)
# ─────────────────────────────────────────────────────────────────────────────


#: Lesbare Labels für die Discord-Komponententypen (Layouts v2 inklusive).
_COMPONENT_TYPE_LABELS = {
    "action_row": "Aktionsreihe",
    "button": "Schaltfläche",
    "string_select": "Auswahlmenü",
    "user_select": "Benutzerauswahl",
    "role_select": "Rollenauswahl",
    "channel_select": "Kanalauswahl",
    "mentionable_select": "Auswahl (Nutzer/Rolle)",
    "text_input": "Text-Eingabe",
    "text_display": "Text",
    "thumbnail": "Vorschaubild",
    "media_gallery": "Medien-Galerie",
    "file": "Datei",
    "separator": "Trennlinie",
    "section": "Abschnitt",
    "container": "Container",
}
_BUTTON_STYLE_LABELS = {
    "primary": "Primär", "secondary": "Sekundär", "success": "Erfolg",
    "danger": "Gefahr", "link": "Link", "premium": "Premium",
}


def _component_items(components: List[Any]) -> List[str]:
    """
    Komponenten-Baum → flache Liste beschreibender Einträge (eine Zeile je
    Komponente). Container/Abschnitte/Aktionsreihen werden rekursiv
    absteigend aufgelöst; Text-Displays werden mit ihrem Inhalt angezeigt.
    """
    items: List[str] = []

    def _walk(comps: Any) -> None:
        for comp in comps or []:
            ctype = getattr(comp, "type", None)
            cname = getattr(ctype, "name", None) or str(ctype)
            label = _COMPONENT_TYPE_LABELS.get(cname, str(cname).replace("_", " "))

            if cname in ("action_row", "section", "container"):
                _walk(getattr(comp, "children", None))
            elif cname == "text_display":
                content = _prepare_text(getattr(comp, "content", None) or "")
                text = _short(content, 180) if content else ""
                items.append(f"{label}: {text}" if text else label)
            elif cname == "button":
                lbl = _prepare_text(getattr(comp, "label", None) or "")
                style_name = str(getattr(getattr(comp, "style", None), "name", "") or "").lower()
                style_lbl = _BUTTON_STYLE_LABELS.get(style_name, style_name)
                suffix = f" ({style_lbl})" if style_lbl else ""
                url = getattr(comp, "url", None)
                link = f" → {url}" if (url and style_name == "link") else ""
                items.append(f"{label} „{lbl}“{suffix}{link}" if lbl else f"{label}{suffix}{link}")
            elif cname in ("string_select", "user_select", "role_select",
                           "channel_select", "mentionable_select"):
                ph = _prepare_text(getattr(comp, "placeholder", None) or "")
                opts = getattr(comp, "options", None) or []
                count = f", {len(opts)} Optionen" if opts else ""
                items.append(f"{label} „{ph}“{count}" if ph else f"{label}{count}")
            elif cname == "media_gallery":
                media = getattr(comp, "items", None) or getattr(comp, "attachments", None) or []
                items.append(f"{label} ({len(media)} Medien)" if media else label)
            else:
                items.append(label)

    _walk(components)
    return items


def _components_to_text(components: List[discord.Component]) -> str:
    parts: List[str] = []
    for comp in components:
        ctype = comp.type
        action_row = getattr(discord.ComponentType, "action_row", None)
        button = getattr(discord.ComponentType, "button", None)
        select = getattr(discord.ComponentType, "select", None) or getattr(
            discord.ComponentType, "select_menu", None
        )
        text_input = getattr(discord.ComponentType, "text_input", None)
        container = getattr(discord.ComponentType, "container", None)
        if ctype is action_row:
            parts.append(_components_to_text(comp.children))
        elif ctype is button:
            parts.append(f"Schaltfläche „{_prepare_text(comp.label or '')}“ ({comp.style.name})")
        elif ctype is select:
            parts.append(f"Auswahlmenü „{_prepare_text(getattr(comp, 'placeholder', '') or '')}“")
        elif ctype is text_input:
            parts.append(f"Text-Eingabe „{_prepare_text(getattr(comp, 'label', '') or '')}“")
        elif ctype is container:
            parts.append(_components_to_text(comp.children))
        else:
            parts.append(f"Komponente {getattr(ctype, 'name', ctype)}")
    return " · ".join(p for p in parts if p)


def _embed_rows(embed: discord.Embed) -> List[Tuple[str, str]]:
    """
    Embed → Textzeilen (Titel, Inhalt, Felder, Fußzeile).

    Jede Zeile wird gekürzt (Zeichen + Zeilen), damit eine einzelne
    Embed-Box-Zeile nie höher als eine PDF-Seite werden kann.
    """
    rows: List[Tuple[str, str]] = []
    if embed.title:
        rows.append(("Titel", _cap(_prepare_text(embed.title, keep_newlines=True), EMBED_TITLE_MAX_CHARS)))
    if embed.description:
        rows.append((
            "Inhalt",
            _cap(_prepare_text(embed.description, keep_newlines=True),
                 EMBED_TEXT_MAX_CHARS, EMBED_TEXT_MAX_LINES),
        ))
    for field in (embed.fields or [])[:10]:
        value = _cap(_prepare_text(field.value, keep_newlines=True),
                     EMBED_TEXT_MAX_CHARS, EMBED_TEXT_MAX_LINES)
        label = _cap(_prepare_text(field.name), EMBED_LABEL_MAX_CHARS)
        rows.append((f"Feld „{label}“", value))
    if embed.footer and embed.footer.text:
        rows.append(("Fußzeile", _cap(_prepare_text(embed.footer.text), EMBED_LABEL_MAX_CHARS)))
    if embed.timestamp:
        rows.append(("Zeitstempel", _fmt_datetime(embed.timestamp)))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  Kopf-/Fußzeile (mit „Seite X von Y“)
# ─────────────────────────────────────────────────────────────────────────────


class _NumberedCanvas(pdf_canvas.Canvas):
    """Zeichnet Kopf-/Fußzeile erst beim Speichern — dadurch ist die Gesamtseitenzahl bekannt."""

    def __init__(self, *args: Any, header_left: str = "", footer_left: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._page_states: List[Dict[str, Any]] = []
        self._header_left = header_left
        self._footer_left = footer_left

    def showPage(self) -> None:
        self._page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._page_states)
        for state in self._page_states:
            self.__dict__.update(state)
            self._draw_chrome(total)
            pdf_canvas.Canvas.showPage(self)
        pdf_canvas.Canvas.save(self)

    def _draw_chrome(self, total: int) -> None:
        f = _FONTS
        self.saveState()
        header_y = PAGE_HEIGHT - 12 * mm
        self.setFont(f["sans"], 7.2)
        self.setFillColor(_C.muted)
        self.drawString(MARGIN, header_y, _coverage_filter(self._header_left))
        self.drawRightString(PAGE_WIDTH - MARGIN, header_y, "Server-Dokument · AIDiscordServerEinrichten")
        self.setStrokeColor(_C.border)
        self.setLineWidth(0.5)
        self.line(MARGIN, header_y - 2.4 * mm, PAGE_WIDTH - MARGIN, header_y - 2.4 * mm)

        footer_y = 11.5 * mm
        self.line(MARGIN, footer_y + 3.4 * mm, PAGE_WIDTH - MARGIN, footer_y + 3.4 * mm)
        self.setFont(f["sans"], 7.2)
        self.drawString(MARGIN, footer_y, _coverage_filter(self._footer_left))
        self.setFont(f["bold"], 7.4)
        self.setFillColor(_C.dark)
        self.drawRightString(PAGE_WIDTH - MARGIN, footer_y, f"Seite {self._pageNumber} von {total}")
        self.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
#  Hauptfunktion
# ─────────────────────────────────────────────────────────────────────────────


def _message_flowables(m: Any, styles: Dict[str, ParagraphStyle], ctx: Dict[str, Any]) -> List[Any]:
    """
    Eine Discord-Nachricht als fertige Flowables (graue, abgerundete Box —
    oder unboxiert/splittbar, wenn die Box zu hoch für eine Seite wäre).

    Enthält: Kopf (Avatar, Name, BOT/Webhook, Zeit, ggf. roter
    „nicht mehr im Server"-Hinweis), Inhalt (Apple-Emojis, Custom-Emojis als
    Inline-Bilder), Embed-Boxen, Bildanhänge, Komponenten als Mini-Darstellung.
    """
    avatar_img = ctx["avatar_img"]
    img = ctx["img"]
    emoji_png_by_id = ctx["emoji_png"]
    member_ids: set = ctx.get("member_ids") or set()
    content_width = ctx.get("content_width", CONTENT_WIDTH)

    flows: List[Any] = []
    author = m.author

    # Kopfzeile: Avatar + Name/Zeit — winzige 1-Zeilen-Tabelle.
    avatar_cell: List[Any] = []
    av = avatar_img(getattr(author, "id", -1), 7)
    if av is not None:
        avatar_cell.append(av)
    else:
        # Achtung: niemals einen Rostring in eine Listen-Zelle —
        # ReportLab bricht sonst beim wrapOn ab.
        avatar_cell.append(Spacer(1, 1))

    head = f"<b>{_clean(getattr(author, 'display_name', None) or author.name)}</b>"
    if getattr(author, "bot", False):
        head += " <font color='#5865F2' size='6.6'><b>BOT</b></font>"
    if m.webhook_id is not None:
        head += f" <font color='#6B7280' size='6.8'>Webhook {m.webhook_id}</font>"
    if getattr(author, "id", None) and member_ids and author.id not in member_ids:
        head += " <font color='#ED4245' size='6.8'><b>• nicht mehr im Server</b></font>"
    head += (
        f"  <font color='#9AA0AB' size='6.8'>{m.created_at.strftime('%d.%m.%Y %H:%M')} UTC</font>"
    )
    box_inner_w = content_width - 14
    msg_header = Table(
        [[avatar_cell, Paragraph(head, styles["msg_head"])]],
        colWidths=[10 * mm, box_inner_w - 10 * mm],
    )
    msg_header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 1),
        ("RIGHTPADDING", (0, 0), (0, 0), 2),
        ("LEFTPADDING", (1, 0), (1, 0), 2),
        ("RIGHTPADDING", (1, 0), (1, 0), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))

    # Inhalt: einzelne Paragraphs/Tabellen (splittbar im Notfall-Pfad).
    body: List[Any] = []

    if _prepare_text(m.content, keep_newlines=True):
        # Discord-Nachrichten sind ≤ 4000 Zeichen; der Cap ist ein
        # Sicherheitsnetz gegen pathologische Webhook-/System-Inhalte.
        raw_content = (m.content or "")[:MESSAGE_CONTENT_MAX_CHARS]
        body.append(Paragraph(
            _message_content_markup(raw_content, emoji_png_by_id=emoji_png_by_id),
            styles["msg_body"],
        ))

    shown_embeds = (m.embeds or [])[:MAX_EMBEDS_PER_MESSAGE]
    for embed in shown_embeds:
        rows = _embed_rows(embed)
        if not rows:
            continue
        embed_color = (
            colors.HexColor(f"#{embed.color.value:06x}")
            if embed.color is not None else _C.blurple
        )
        box = Table(
            [
                [Paragraph(f"<b>{_clean(label)}:</b> {_clean_ml(value)}", styles["embed_text"])]
                for label, value in rows
            ],
            colWidths=[box_inner_w - 6],
        )
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, embed_color),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 2.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
        ]))
        body.append(Spacer(1, 1.4 * mm))
        body.append(box)
    hidden_embeds = len(m.embeds or []) - len(shown_embeds)
    if hidden_embeds > 0:
        body.append(Paragraph(f"(+{hidden_embeds} weitere Embeds gekürzt)", styles["table_muted"]))

    # Bildanhänge als echte Bilder, Rest als Dateiname
    atts = getattr(m, "attachments", None) or []
    image_atts = [a for a in atts if _attachment_is_image(a) and getattr(a, "url", None)]
    other_atts = [a for a in atts if a not in image_atts]
    for att in image_atts[:4]:
        flow = img(att.url, max_w=48, max_h=48)
        if flow is not None:
            body.append(Spacer(1, 1.2 * mm))
            body.append(flow)

    extras: List[str] = []
    if other_atts:
        names = ", ".join(
            _prepare_text(a.filename) for a in other_atts[:MAX_ATTACHMENT_NAMES]
        )
        extra_count = len(other_atts) - min(len(other_atts), MAX_ATTACHMENT_NAMES)
        label = "Anhang: " if len(other_atts) == 1 else "Anhänge: "
        extras.append(label + names + (f" (+{extra_count} weitere)" if extra_count > 0 else ""))
    if m.stickers:
        extras.append("Sticker: " + ", ".join(_prepare_text(s.name) for s in m.stickers))
    try:
        comps = m.components or []
    except Exception:  # pragma: no cover — ältere discord.py-Versionen
        comps = []
    if extras:
        body.append(Spacer(1, 0.8 * mm))
        body.append(Paragraph(_clean(" | ".join(extras)), styles["table_muted"]))

    # Komponenten als echte Mini-Darstellung (Buttons, Galerien, …);
    # Fallback: Stichpunktliste.
    if comps:
        comp_flows = _components_flowables(comps, {**ctx, "styles": styles})
        if comp_flows:
            body.append(Spacer(1, 1 * mm))
            body.extend(comp_flows)
        else:
            items = _component_items(comps)
            if items:
                body.append(Spacer(1, 0.6 * mm))
                body.append(Paragraph("<b>Komponenten:</b>", styles["table_muted"]))
                for item in items[:12]:
                    body.append(Paragraph(
                        f"•&nbsp;&nbsp;{_font_markup(_esc(_short(item, 160)))}",
                        ParagraphStyle(
                            "comp_item", parent=styles["table_muted"],
                            leftIndent=8, firstLineIndent=-8,
                        ),
                    ))

    # Nachricht als graues, abgerundetes Kästchen. Sicherheitsnetz
    # (LayoutError-Schutz): die Box-Höhe wird vorher gemessen — passt
    # sie nicht sicher auf eine Seite, wird die Nachricht unboxiert
    # mit splittbaren Flowables ausgegeben (Tabellenzeilen splitten
    # nicht!). Siehe LayoutError-Fix: never KeepTogether unbounded.
    msg_box = Table([[ [msg_header] + body ]], colWidths=[content_width])
    msg_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F1F3F7")),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    try:
        _bw, _bh = msg_box.wrap(content_width, 100000)
    except Exception:  # noqa: BLE001 — Messung darf nie crashen
        _bh = MAX_MESSAGE_BOX_PT + 1
    if _bh <= MAX_MESSAGE_BOX_PT:
        flows.append(msg_box)
    else:
        # Box zu hoch für eine Seite → unboxiert, splittbar ausgeben.
        flows.append(msg_header)
        if body:
            flows.append(Indenter(left=7))
            flows.extend(body)
            flows.append(Indenter(left=-7))
    return flows


async def build_server_document(
    guild: discord.Guild,
    http_session: aiohttp.ClientSession,
    *,
    progress: ProgressCallback,
) -> bytes:
    """
    Erzeugt das Server-Dokument als PDF-Bytes.

    ``progress`` wird nach jedem Hauptschritt mit (Prozent, Status-Text)
    aufgerufen, damit das Adminpanel einen Ladebalken anzeigen kann.
    """
    styles = _styles()
    now_utc = datetime.now(timezone.utc)

    # ── 1. Basisdaten sammeln ──────────────────────────────────────────────
    await progress(5, "Server-Informationen werden gesammelt …")

    owner = guild.owner
    if owner is None and getattr(guild, "owner_id", None):
        try:
            owner = await guild.fetch_member(guild.owner_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            owner = None

    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)
    members = list(guild.members)
    member_count = guild.member_count or len(members)
    member_count, members = await _ensure_member_list(guild, member_count, members, progress)

    categories = list(guild.categories)
    text_channels = list(guild.text_channels)
    voice_channels = list(guild.voice_channels)
    stage_channels = list(guild.stage_channels)
    forum_channels = list(guild.forums)
    uncategorized = [
        c for c in guild.channels
        if c.category is None and c.type is not discord.ChannelType.category
    ]
    emojis = list(guild.emojis)
    stickers = list(guild.stickers)
    detailed_members = (
        members if len(members) <= MAX_MEMBERS_DETAILED
        else _select_important_members(members, guild)
    )

    # Mention-Namen (Rollen/Kanäle/Mitglieder) für @Name statt @id
    _MENTION_NAMES.clear()
    for r in roles:
        _MENTION_NAMES[r.id] = r.name
    for ch in guild.channels:
        _MENTION_NAMES[ch.id] = ch.name
    for mb in members:
        _MENTION_NAMES[mb.id] = getattr(mb, "display_name", None) or mb.name

    def _member_avatar(m: Any) -> Optional[str]:
        return getattr(getattr(m, "display_avatar", None), "url", None) or None

    avatar_urls: Dict[int, str] = {}
    for m in ([owner] if owner else []) + detailed_members:
        url = _member_avatar(m)
        if url:
            avatar_urls.setdefault(getattr(m, "id", 0), url)

    # ── 2. Nachrichtenhistorie ─────────────────────────────────────────────
    await progress(16, "Letzte Nachrichten der Textkanäle werden geladen …")

    channel_messages: Dict[int, List[discord.Message]] = {}
    for channel in text_channels:
        try:
            history = getattr(channel, "history", None)
            if history is None:
                channel_messages[channel.id] = []
                continue
            channel_messages[channel.id] = [
                m async for m in history(limit=MAX_MESSAGES_PER_CHANNEL)
            ]
            for m in channel_messages[channel.id]:
                url = _member_avatar(m.author)
                if url:
                    avatar_urls.setdefault(getattr(m.author, "id", 0), url)
                # Mention-Namen aus den Nachrichten ergänzen (Autoren,
                # Erwähnte, Rollen) — auch für nicht gecachte Mitglieder.
                if getattr(m, "author", None) is not None:
                    _MENTION_NAMES.setdefault(getattr(m.author, "id", 0),
                                              getattr(m.author, "display_name", None) or m.author.name)
                for u in getattr(m, "mentions", None) or []:
                    _MENTION_NAMES.setdefault(u.id, getattr(u, "display_name", None) or u.name)
                for r in getattr(m, "role_mentions", None) or []:
                    _MENTION_NAMES.setdefault(r.id, r.name)
                for c in getattr(m, "channel_mentions", None) or []:
                    _MENTION_NAMES.setdefault(c.id, c.name)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound,
                AttributeError, TypeError):
            channel_messages[channel.id] = []

    # ── 3. Alle Bilder parallel herunterladen ──────────────────────────────
    await progress(34, "Bilder (Icon, Banner, Avatare, Emojis) werden geladen …")

    image_urls: List[Optional[str]] = [
        getattr(guild.icon, "url", None) if guild.icon else None,
        getattr(guild.banner, "url", None) if getattr(guild, "banner", None) else None,
        *avatar_urls.values(),
    ]
    for emoji in emojis:
        image_urls.append(getattr(emoji, "url", None))
    for sticker in stickers:
        image_urls.append(getattr(sticker, "url", None))

    # Bildanhänge + Komponenten-Medien (mit globalen Obergrenzen)
    all_msgs = [m for msgs in channel_messages.values() for m in msgs]
    emoji_url_by_id: Dict[int, Optional[str]] = {e.id: getattr(e, "url", None) for e in emojis}
    att_image_urls: List[str] = []
    for m in all_msgs:
        for att in getattr(m, "attachments", None) or []:
            if (_attachment_is_image(att) and getattr(att, "url", None)
                    and (getattr(att, "size", 0) or 0) <= _MAX_IMAGE_BYTES):
                att_image_urls.append(att.url)
    att_image_urls = att_image_urls[:MAX_ATTACHMENT_IMAGES]
    image_urls.extend(att_image_urls)
    image_urls.extend(_component_media_urls(
        [c for m in all_msgs for c in (getattr(m, "components", None) or [])]
    ))

    async def _img_progress(done_n: int, total_n: int) -> None:
        pct = 34 + min(int(27 * done_n / max(total_n, 1)), 27)
        await progress(pct, f"Bilder werden geladen ({done_n}/{total_n}) …")

    image_cache = await _download_images(http_session, image_urls, progress_hook=_img_progress)

    _dl_ok = sum(1 for v in image_cache.values() if v)
    await progress(62, f"Bilder geladen ({_dl_ok}/{len(image_cache)}) — Dokument wird aufbereitet …")

    # Fonts/Emoji-Assets VOR dem Thread vorwärmen (kein Lazy-Init-Rennen).
    _ensure_fonts()
    _ensure_apple_emoji()

    # Die komplette Aufbereitung (Flowables, PIL-Bildkonvertierung, reportlab)
    # ist EIN Block synchroner Arbeit. Früher lief er AUF DEM EVENT-LOOP und
    # blockierte dort ALLE Timer, Logs und Fortschritts-Edits — das Dokument
    # „hing" dann sichtbar ewig (z. B. bei 58 %) ohne jegliches Log. Jetzt:
    # Worker-Thread (Event-Loop bleibt lebendig) + Live-Ticker + Budget mit
    # sichtbarer Fehlermeldung statt ewiger Stille.
    loop = asyncio.get_running_loop()
    _last_pct = [62]

    def _emit_soon(pct: int, text: str) -> None:
        _last_pct[0] = max(_last_pct[0], min(pct, 99))
        asyncio.ensure_future(progress(pct, text))

    def _emit(pct: int, text: str) -> None:
        try:
            loop.call_soon_threadsafe(_emit_soon, pct, text)
        except RuntimeError:
            pass  # Loop bereits geschlossen — Aufbau läuft weiter

    async def _assembly_ticker(started: float) -> None:
        while True:
            await asyncio.sleep(_ASSEMBLY_TICKER_S)
            try:
                await progress(_last_pct[0], f"Dokument wird erstellt … "
                               f"({time.monotonic() - started:.0f} s verstrichen)")
            except Exception:  # noqa: BLE001 — Ticker darf niemals abbrechen
                pass

    def _assemble() -> bytes:
        """(Worker-Thread) Abschnitte 4+5: Flowables + PDF-Bau."""
        # ── 4. PDF-Datenstrukturen ─────────────────────────────────────────────
        _emit(62, "Tabellen werden aufbereitet …")

        flowables: List[Any] = []

        def img(url: Optional[str], **kwargs: Any) -> Optional[Image]:
            return _image_flow(image_cache.get(url) if url else None, **kwargs)

        def avatar_img(member_id: int, size_mm: float) -> Optional[Image]:
            return _avatar_flow(image_cache.get(avatar_urls.get(member_id, "")), size_mm)

        def emoji_png(eid: int) -> Optional[str]:
            return _custom_emoji_png(image_cache.get(emoji_url_by_id.get(eid, "")), f"emoji-{eid}")

        # Kontext für den gemeinsamen Nachrichten-Renderer (auch vom
        # Chatverlauf-PDF genutzt).
        msg_ctx: Dict[str, Any] = {
            "styles": styles,
            "img": img,
            "avatar_img": avatar_img,
            "emoji_png": emoji_png,
            "member_ids": {getattr(m, "id", None) for m in members if getattr(m, "id", None)},
            "content_width": CONTENT_WIDTH,
        }

        # ---- Titelkopf ----
        icon_url = getattr(guild.icon, "url", None) if guild.icon else None
        banner_url = getattr(guild.banner, "url", None) if getattr(guild, "banner", None) else None

        banner_flow = img(banner_url, max_w=CONTENT_WIDTH / mm, max_h=52, upscale=True)
        banner_hint: str
        if banner_flow is not None:
            flowables.append(banner_flow)
            flowables.append(Spacer(1, 5 * mm))
            banner_hint = "Server-Banner: <b>hochgeladen</b> — als Bild oben."
        else:
            # Kein Banner: Akzentfarbe zeigen — die Farbe der höchsten farbigen
            # Rolle (die „ausgewählte Farbe“ des Servers), sonst Blurple.
            accent_role: Any = None
            for r in roles:
                if r.color.value:
                    accent_role = r
                    break
            if accent_role is not None:
                accent_hex = f"#{accent_role.color.value:06x}"
                accent_src = f"höchste farbige Rolle „{_clean(accent_role.name)}“"
            else:
                accent_hex = "#5865F2"
                accent_src = "Standard-Akzentfarbe (keine farbige Rolle)"
            accent_color = colors.HexColor(accent_hex)
            band_text_color = colors.white if _luma(accent_color) < 0.6 else _C.dark
            band = Table(
                [[Paragraph(
                    "Server-Banner",
                    ParagraphStyle(
                        "banner_band", fontName=_FONTS["bold"], fontSize=13, leading=16,
                        textColor=band_text_color, alignment=TA_CENTER,
                    ),
                )]],
                colWidths=[CONTENT_WIDTH],
            )
            band.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), accent_color),
                ("ROUNDEDCORNERS", [4, 4, 4, 4]),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
            ]))
            flowables.append(band)
            flowables.append(Spacer(1, 5 * mm))
            banner_hint = (
                f"Kein Banner hochgeladen — Hinweis: Farbband in der Akzentfarbe "
                f"<font name='{_FONTS['mono']}' size='7.6'>{accent_hex}</font> ({accent_src})."
            )

        icon_flow = _avatar_flow(image_cache.get(icon_url), 24) if icon_url else None
        meta_bits = [
            f"Server-ID <font name='{_FONTS['mono']}' size='7.6'>{guild.id}</font>",
            f"Erstellt {_fmt_date(guild.created_at)}",
            f"{member_count:,} Mitglieder".replace(",", "."),
            f"Verifizierung {_clean(_enum_label(guild.verification_level, _VERIFICATION_LABELS))}",
        ]
        title_cell: List[Any] = [
            Paragraph(_clean(guild.name), styles["title"]),
            Paragraph(" · ".join(meta_bits), styles["subtitle"]),
        ]
        header_cells: List[Any] = [icon_flow if icon_flow is not None else ""] + [title_cell]
        header_widths = [30 * mm, CONTENT_WIDTH - 30 * mm] if icon_flow is not None else [CONTENT_WIDTH]
        header = Table([header_cells], colWidths=header_widths)
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        flowables.append(header)
        flowables.append(Spacer(1, 1.2 * mm))
        flowables.append(Paragraph(banner_hint, styles["small"]))
        flowables.append(Spacer(1, 3 * mm))

        # Statistik-Chips
        stat_chips: List[Tuple[str, colors.Color, bool]] = [
            (f"{member_count} Mitglieder", _C.blurple, False),
            (f"{len(roles)} Rollen", _C.dark, False),
            (f"{len(categories)} Kategorien", _C.gray_chip, False),
            (f"{len(text_channels)} Text", _C.blurple, False),
            (f"{len(voice_channels) + len(stage_channels)} Voice/Bühne", _C.green, False),
            (f"{len(forum_channels)} Forum", _C.pink, False),
            (f"{len(emojis)} Emojis", _C.amber, True),
            (f"{len(stickers)} Sticker", _C.amber, True),
        ]
        flowables.append(_chips_row(stat_chips))
        flowables.append(Spacer(1, 4 * mm))

        # ---- Info-Kärtchen (Owner + Server-Einstellungen) ----
        info_rows: List[Tuple[str, Any]] = []
        if getattr(guild, "description", None):
            info_rows.append(("Beschreibung", Paragraph(_clean_ml(guild.description), styles["table"])))
        if owner is not None:
            info_rows.append((
                "Owner",
                Paragraph(
                    f"<b>{_clean(owner.display_name)}</b> (@{_clean(owner.name)}) · "
                    f"<font name='{_FONTS['mono']}' size='7.2'>{owner.id}</font>",
                    styles["table"],
                ),
            ))
        info_rows.append(("Erstellt", Paragraph(_clean(_fmt_datetime(guild.created_at)), styles["table"])))
        info_rows.append((
            "Verifizierung",
            Paragraph(_clean(_enum_label(guild.verification_level, _VERIFICATION_LABELS)), styles["table"]),
        ))
        info_rows.append((
            "Inhaltsfilter",
            Paragraph(_clean(_enum_label(guild.explicit_content_filter, _CONTENT_FILTER_LABELS)), styles["table"]),
        ))
        info_rows.append(("Boosts", Paragraph(_clean(_premium_label(guild)), styles["table"])))
        locale = str(getattr(guild.preferred_locale, "name", guild.preferred_locale) or "").replace("_", " ").title()
        if locale:
            info_rows.append(("Sprache", Paragraph(_clean(locale), styles["table"])))
        features = ", ".join(_prepare_text(ft) for ft in (getattr(guild, "features", []) or [])[:8])
        if features:
            info_rows.append(("Features", Paragraph(_clean(_short(features, 160)), styles["table"])))
        flowables.append(_kv_card(info_rows, styles))
        flowables.append(Spacer(1, 4 * mm))

        # ---- Inhaltsverzeichnis ----
        toc = [
            ("1", "Kanäle & Kategorien", "Alle Kanäle mit Typ, ID, Kategorie und Themenbeschreibung."),
            ("2", "Rollen", "Hierarchie, Farben und alle Berechtigungen jeder Rolle."),
            ("3", "Emojis & Sticker", "Bild-Übersicht aller Server-Emojis und Sticker."),
            ("4", "Mitglieder", "Avatare, Rollen und Beitrittsdatum (bei großen Servern die wichtigsten)."),
            ("5", "Letzte Nachrichten", f"Die letzten {MAX_MESSAGES_PER_CHANNEL} Nachrichten je Textkanal."),
        ]
        toc_cells = [[
            Paragraph(
                f"<b>{num}</b>",
                ParagraphStyle("toc_num", parent=styles["body"], textColor=_C.blurple, fontSize=10.5),
            ),
            [
                Paragraph(_clean(t), styles["h3"]),
                Paragraph(_clean(d), styles["small"]),
            ],
        ] for num, t, d in toc]
        toc_table = Table(toc_cells, colWidths=[9 * mm, CONTENT_WIDTH - 9 * mm])
        toc_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1.4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
        ]))
        flowables.append(toc_table)
        flowables.append(PageBreak())

        # ---- 1. Kanäle & Kategorien ----
        _section(flowables, "1", "Kanäle & Kategorien", styles)
        flowables.append(_chips_row([
            (f"{len(categories)} Kategorien", _C.gray_chip, False),
            (f"{len(text_channels)} Text", _C.blurple, False),
            (f"{len(voice_channels)} Voice", _C.green, False),
            (f"{len(stage_channels)} Bühne", _C.amber, True),
            (f"{len(forum_channels)} Forum/Medien", _C.pink, False),
            (f"{len(uncategorized)} ohne Kategorie", _C.dark, False),
        ]))
        flowables.append(Spacer(1, 3 * mm))

        CH_W = [24 * mm, CONTENT_WIDTH - 24 * mm - 30 * mm, 30 * mm]

        def _fmt_duration(seconds: int) -> str:
            if seconds < 60:
                return f"{seconds} s"
            minutes, rest = divmod(seconds, 60)
            if minutes < 60:
                return f"{minutes} min" + (f" {rest} s" if rest else "")
            hours, minutes = divmod(minutes, 60)
            return f"{hours} h" + (f" {minutes} min" if minutes else "")

        def _channel_details(ch: Any) -> str:
            """Zusätzliche Kanal-Details als eine muted-Zeile (ohne Thema)."""
            bits: List[str] = []
            delay = getattr(ch, "slowmode_delay", 0) or 0
            if delay:
                bits.append(f"Slow-Modus {_fmt_duration(int(delay))}")
            thread_delay = getattr(ch, "default_thread_slowmode_delay", 0) or 0
            if thread_delay:
                bits.append(f"Thread-Slow-Modus {_fmt_duration(int(thread_delay))}")
            if getattr(ch, "nsfw", False):
                bits.append("NSFW")
            bitrate = getattr(ch, "bitrate", None)
            if bitrate:
                bits.append(f"Bitrate {int(bitrate) // 1000} kbps")
            user_limit = getattr(ch, "user_limit", 0) or 0
            if user_limit:
                bits.append(f"Limit {user_limit} Nutzer")
            video_quality = getattr(ch, "video_quality_mode", None)
            quality_name = getattr(video_quality, "name", None)
            if quality_name == "FULL":
                bits.append("Video 720p60")
            return " · ".join(_prepare_text(b) for b in bits)

        def _channel_table(channels: List[Any]) -> Table:
            rows: List[List[Any]] = []
            for ch in channels:
                label, bg, dark = _channel_type_info(ch.type)
                name_cell: List[Any] = [Paragraph(f"<b>{_clean(ch.name)}</b>", styles["table"])]
                topic = _prepare_text(getattr(ch, "topic", None))
                if topic:
                    name_cell.append(Paragraph(_font_markup(_esc(_short(topic, 220))), styles["table_muted"]))
                details = _channel_details(ch)
                if details:
                    name_cell.append(Paragraph(_font_markup(_esc(details)), styles["table_muted"]))
                id_cell = Paragraph(
                    f"<font name='{_FONTS['mono']}' size='6.8'>{ch.id}</font>",
                    ParagraphStyle("id_r", parent=styles["mono"], alignment=TA_RIGHT),
                )
                rows.append([_chip(label, bg, dark), name_cell, id_cell])
            t = Table(rows, colWidths=CH_W, repeatRows=0)
            t.setStyle(TableStyle([
                _ZEBRA,
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2.4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.8),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, _C.border),
            ]))
            return t

        for cat in categories:
            cat_channels = list(cat.channels)
            head = Table(
                [[
                    Paragraph(_clean(cat.name), styles["cat_head"]),
                    Paragraph(
                        f"{_clean(str(len(cat_channels)))} Kanäle · "
                        f"ID <font name='{_FONTS['mono']}' size='6.6'>{cat.id}</font>",
                        styles["cat_meta"],
                    ),
                ]],
                colWidths=[CONTENT_WIDTH - 52 * mm, 52 * mm],
            )
            head.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), _C.dark),
                ("ROUNDEDCORNERS", [3, 3, 0, 0]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
            ]))
            body = (
                _channel_table(cat_channels)
                if cat_channels
                else Table(
                    [[Paragraph("Keine Kanäle in dieser Kategorie.", styles["table_muted"])]],
                    colWidths=[CONTENT_WIDTH],
                )
            )
            flowables.append(KeepTogether([head, body, Spacer(1, 3.4 * mm)]))

        if uncategorized:
            flowables.append(Paragraph("Ohne Kategorie", styles["h2"]))
            flowables.append(_channel_table(uncategorized))
        if not categories and not uncategorized:
            flowables.append(Paragraph("Keine Kanäle vorhanden.", styles["body"]))
        flowables.append(PageBreak())

        # ---- 2. Rollen ----
        _section(flowables, "2", "Rollen", styles)
        flowables.append(Paragraph(
            f"{len(roles)} Rollen, sortiert von oben (höchste) nach unten (niedrigste). "
            "Berechtigungen sind pro Rolle vollständig aufgelistet.",
            styles["body"]))
        flowables.append(Spacer(1, 3 * mm))

        _PERM_PRIORITY = (
            "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
            "manage_messages", "manage_threads", "manage_emojis_and_stickers", "manage_events",
            "manage_nicknames", "moderate_members", "kick_members", "ban_members",
            "view_channel", "send_messages", "read_message_history",
        )

        def _perm_sort_key(name: str) -> Tuple[int, str]:
            norm = name.lower().replace(" ", "_")
            prio = _PERM_PRIORITY.index(norm) if norm in _PERM_PRIORITY else len(_PERM_PRIORITY)
            return (prio, name)

        role_member_counts: Dict[int, int] = {
            role.id: sum(1 for m in guild.members if role in m.roles) for role in roles
        }
        role_rows: List[List[Any]] = [[
            Paragraph("Rolle", styles["table_head"]),
            Paragraph("Farbe", styles["table_head"]),
            Paragraph("Berechtigungen", styles["table_head"]),
            Paragraph("Mitglieder", styles["table_head_small"]),
            Paragraph("ID", styles["table_head_small"]),
        ]]
        for role in roles:
            is_admin = bool(getattr(role.permissions, "administrator", False))
            if is_admin:
                # Administrator heißt ALLE Berechtigungen — die Aufzählung der
                # einzelnen Rechte wäre redundant, daher rot hervorgehoben.
                perms_para = Paragraph(
                    "<b><font color='#ED4245'>ADMINISTRATOR — umfasst ALLE Berechtigungen</font></b>",
                    styles["table_small"],
                )
            else:
                perm_names = sorted(
                    (name.replace("_", " ") for name, enabled in role.permissions if enabled),
                    key=_perm_sort_key,
                )
                perms_para = Paragraph(
                    " · ".join(_clean(n) for n in perm_names) or "— keine eigenen Berechtigungen —",
                    styles["table_small"],
                )
            if role.color.value:
                color_para = Paragraph(
                    f"<font color='#{role.color.value:06x}'>●</font> "
                    f"<font name='{_FONTS['mono']}' size='6.8'>#{role.color.value:06x}</font>",
                    styles["table_small"],
                )
            else:
                color_para = Paragraph("<font color='#9AA0AB'>●</font> Standard", styles["table_small"])
            role_name_html = _clean(role.name)
            if is_admin:
                role_name_html = f"<font color='#ED4245'>{role_name_html}</font>"
            role_rows.append([
                Paragraph(f"<b>{role_name_html}</b>", styles["table"]),
                color_para,
                perms_para,
                Paragraph(
                    str(role_member_counts.get(role.id, 0)),
                    ParagraphStyle("cnt", parent=styles["table"], alignment=TA_CENTER),
                ),
                Paragraph(_id_para(role.id), styles["mono"]),
            ])
        role_table = Table(
            role_rows,
            repeatRows=1,
            colWidths=[
                40 * mm,
                22 * mm,
                CONTENT_WIDTH - 40 * mm - 22 * mm - 20 * mm - 26 * mm,
                20 * mm,
                26 * mm,
            ],
        )
        role_table.setStyle(_table_style())
        flowables.append(role_table)
        flowables.append(PageBreak())

        # ---- 3. Emojis & Sticker ----
        _section(flowables, "3", "Emojis & Sticker", styles)

        shown_emojis = emojis[:MAX_EMOJIS_SHOWN]
        if shown_emojis:
            flowables.append(Paragraph(
                f"{len(emojis)} Emojis"
                + (f" — die ersten {len(shown_emojis)} werden gezeigt"
                   if len(emojis) > len(shown_emojis) else ""),
                styles["h2"]))
            grid_rows: List[List[Any]] = []
            for start in range(0, len(shown_emojis), EMOJI_GRID_COLUMNS):
                chunk = shown_emojis[start:start + EMOJI_GRID_COLUMNS]
                row: List[Any] = []
                for emoji in chunk:
                    cell: List[Any] = []
                    flow = img(emoji.url, max_w=7.5, max_h=7.5, upscale=True)
                    if flow is None:
                        flow = Paragraph("kein Bild", styles["center_tiny"])
                    cell.append(flow)
                    cell.append(Spacer(1, 1 * mm))
                    cell.append(Paragraph(_clean(_short(emoji.name, 22)), styles["center_small"]))
                    cell.append(Paragraph(str(emoji.id), styles["center_tiny"]))
                    if emoji.animated:
                        cell.append(Paragraph("<b>animiert</b>", styles["center_tiny"]))
                    row.append(cell)
                while len(row) < EMOJI_GRID_COLUMNS:
                    row.append("")
                grid_rows.append(row)
            cell_w = CONTENT_WIDTH / EMOJI_GRID_COLUMNS
            emoji_grid = Table(grid_rows, colWidths=[cell_w] * EMOJI_GRID_COLUMNS)
            emoji_grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 3.4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
                ("GRID", (0, 0), (-1, -1), 0.4, _C.border),
            ]))
            flowables.append(emoji_grid)
            if len(emojis) > len(shown_emojis):
                flowables.append(Spacer(1, 1.5 * mm))
                flowables.append(Paragraph(f"(und {len(emojis) - len(shown_emojis)} weitere Emojis)", styles["small"]))
        else:
            flowables.append(Paragraph("Keine Emojis vorhanden.", styles["body"]))

        flowables.append(Spacer(1, 3 * mm))

        shown_stickers = stickers[:MAX_STICKERS_SHOWN]
        if shown_stickers:
            flowables.append(Paragraph(f"{len(stickers)} Sticker", styles["h2"]))
            s_rows: List[List[Any]] = []
            for start in range(0, len(shown_stickers), STICKER_GRID_COLUMNS):
                chunk = shown_stickers[start:start + STICKER_GRID_COLUMNS]
                row: List[Any] = []
                for sticker in chunk:
                    cell = [
                        img(sticker.url, max_w=26, max_h=30) or Paragraph("kein Bild", styles["center_tiny"]),
                        Spacer(1, 1.2 * mm),
                        Paragraph(_clean(_short(sticker.name, 30)), styles["center_small"]),
                        Paragraph(
                            f"{_clean(str(getattr(sticker, 'type', ''))).replace('StickerType.', '')} · {sticker.id}",
                            styles["center_tiny"],
                        ),
                    ]
                    row.append(cell)
                while len(row) < STICKER_GRID_COLUMNS:
                    row.append("")
                s_rows.append(row)
            s_cell_w = CONTENT_WIDTH / STICKER_GRID_COLUMNS
            sticker_grid = Table(s_rows, colWidths=[s_cell_w] * STICKER_GRID_COLUMNS)
            sticker_grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("GRID", (0, 0), (-1, -1), 0.4, _C.border),
            ]))
            flowables.append(sticker_grid)
            if len(stickers) > len(shown_stickers):
                flowables.append(Spacer(1, 1.5 * mm))
                flowables.append(Paragraph(f"(und {len(stickers) - len(shown_stickers)} weitere Sticker)", styles["small"]))
        if not emojis and not stickers:
            flowables.append(Paragraph("Keine Emojis oder Sticker vorhanden.", styles["body"]))
        flowables.append(PageBreak())

        # ---- 4. Mitglieder ----
        _section(flowables, "4", "Mitglieder", styles)
        if len(members) <= MAX_MEMBERS_DETAILED and len(members) >= member_count:
            flowables.append(Paragraph(f"Alle {len(members)} Mitglieder des Servers:", styles["body"]))
        elif len(members) <= MAX_MEMBERS_DETAILED:
            flowables.append(Paragraph(
                f"{len(members)} von {member_count} Mitgliedern im Cache — die Mitgliederliste "
                "könnte unvollständig sein (Bot konnte nicht alle Mitglieder laden):",
                styles["body"]))
        else:
            flowables.append(Paragraph(
                f"Der Server hat {member_count} Mitglieder — mehr als {MAX_MEMBERS_DETAILED}. "
                f"Angezeigt werden die {MAX_MEMBERS_DETAILED} wichtigsten Mitglieder "
                "(Inhaber der seltensten Rollen zuerst).", styles["body"]))
        flowables.append(Spacer(1, 3 * mm))

        member_rows: List[List[Any]] = [[
            Paragraph("", styles["table_head"]),
            Paragraph("Mitglied", styles["table_head"]),
            Paragraph("Rollen", styles["table_head"]),
            Paragraph("Beigetreten", styles["table_head_small"]),
            Paragraph("ID", styles["table_head_small"]),
        ]]
        for member in detailed_members:
            avatar = avatar_img(member.id, 9)
            display_clean = _prepare_text(getattr(member, "display_name", None))
            name_bits = f"<b>{_clean(member.display_name)}</b>"
            username = _clean(member.name)
            if username and username.lower() != display_clean.lower():
                name_bits += f"<br/><font color='#6B7280' size='7.2'>@{username}</font>"
            if getattr(member, "bot", False):
                name_bits += " <font color='#5865F2' size='7'><b>BOT</b></font>"
            # Rollen als abgerundete graue Kästchen: nebeneinander mit Padding,
            # umbrochen in Zeilen (statt ein langer „ · “-Text).
            member_role_names = [r.name for r in member.roles if r != guild.default_role]
            roles_col_pt = (CONTENT_WIDTH - 12 * mm - 44 * mm - 23 * mm - 26 * mm) / mm * 2.8346
            role_cell: List[Any] = _chip_lines(
                member_role_names, max_width=roles_col_pt - 9
            )
            member_rows.append([
                avatar if avatar is not None else "",
                Paragraph(name_bits, styles["table"]),
                role_cell,
                Paragraph(_clean(_fmt_date(member.joined_at)), styles["table_small"]),
                Paragraph(_id_para(member.id), styles["mono"]),
            ])
        member_table = Table(
            member_rows,
            repeatRows=1,
            colWidths=[
                12 * mm,
                44 * mm,
                CONTENT_WIDTH - 12 * mm - 44 * mm - 23 * mm - 26 * mm,
                23 * mm,
                26 * mm,
            ],
        )
        member_table.setStyle(_table_style())
        flowables.append(member_table)
        flowables.append(PageBreak())

        # ---- 5. Letzte Nachrichten ----
        _section(flowables, "5", "Letzte Nachrichten pro Textkanal", styles)
        flowables.append(Paragraph(
            f"Die letzten {MAX_MESSAGES_PER_CHANNEL} Nachrichten jedes Textkanals, für den der Bot "
            "die Historie lesen darf. Embeds, Anhänge, Sticker und Komponenten werden in Text umgewandelt.",
            styles["body"]))
        flowables.append(Spacer(1, 2.5 * mm))

        any_messages = False
        for channel in text_channels:
            msgs = channel_messages.get(channel.id, [])
            # Kanal-Überschrift: groß, fett, unterstrichen (Farblinie unten).
            head_table = Table([[Paragraph(
                f"<b>#{_clean(channel.name)}</b> "
                f"<font name='{_FONTS['mono']}' size='7.2' color='#6B7280'>{channel.id}</font> "
                f"<font color='#6B7280'>· {len(msgs)} Nachricht{'en' if len(msgs) != 1 else ''}</font>",
                styles["msg_channel_head"],
            )]], colWidths=[CONTENT_WIDTH])
            head_table.setStyle(TableStyle([
                ("LINEBELOW", (0, 0), (-1, -1), 1.1, _C.blurple),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
            ]))
            if not msgs:
                flowables.append(KeepTogether([
                    head_table,
                    Spacer(1, 1.4 * mm),
                    Paragraph("Keine Nachrichten verfügbar (leer, keine Historie oder keine Rechte).", styles["small"]),
                    Spacer(1, 3.2 * mm),
                ]))
                continue

            any_messages = True
            flowables.append(head_table)
            flowables.append(Spacer(1, 2.4 * mm))

            # ÄLTESTE zuerst: Discord liefert die Historie neueste-zuerst —
            # umgekehrt lesen wir sie von oben (alt) nach unten (neu).
            for m in reversed(msgs):
                flowables.extend(_message_flowables(m, styles, msg_ctx))
                flowables.append(Spacer(1, 2.4 * mm))

        if not any_messages:
            flowables.append(Spacer(1, 1.5 * mm))
            flowables.append(Paragraph(
                "Keine Nachrichten konnten aus den Textkanälen geladen werden.", styles["body"]))

        # ── 5. Bauen ────────────────────────────────────────────────────────────
        _emit(88, "PDF-Datei wird geschrieben …")

        pdf_bytes = _render_pdf(
            flowables,
            header_left=f"{guild.name} · Server-Dokument",
            footer_left=(
                "Vertraulich — nur für den Bot-Owner bestimmt. Erstellt am "
                + now_utc.strftime("%d.%m.%Y, %H:%M") + " UTC"
            ),
            title=f"Server-Dokument · {guild.name}",
            subject=f"Komplette Server-Übersicht für {guild.name} (ID {guild.id})",
        )
        return pdf_bytes

    assemble_started = time.monotonic()
    assemble_task = asyncio.create_task(asyncio.to_thread(_assemble))
    ticker_task = asyncio.create_task(_assembly_ticker(assemble_started))
    try:
        pdf_bytes = await asyncio.wait_for(assemble_task, timeout=_ASSEMBLY_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning(
            "Server-Dokument-Aufbereitung für '%s' hat nach %d s nicht abgeschlossen — "
            "Abbruch mit sichtbarer Fehlermeldung (der Thread läuft weiter, das Ergebnis wird verworfen).",
            guild.name, int(_ASSEMBLY_TIMEOUT_S),
        )
        raise RuntimeError(
            f"Die Dokument-Aufbereitung hat zu lange gedauert (>{int(_ASSEMBLY_TIMEOUT_S)} s) "
            "und wurde abgebrochen. Bitte erneut versuchen — passiert das bei diesem Server "
            "wiederholt, melde dich mit dem Servernamen beim Bot-Team."
        ) from None
    finally:
        ticker_task.cancel()

    log.info("Server-Dokument-Aufbereitung fertig in %.1f s", time.monotonic() - assemble_started)

    # RAM: das Apple-Sprite-Sheet (4158×4158 RGBA ≈ 69 MB) nach dem Bau
    # freigeben — die zugeschnittenen Emoji-PNGs bleiben auf Disk gecacht.
    _close_apple_sheet()

    await progress(100, "PDF fertiggestellt.")
    log.info(
        "Server-Dokument für '%s' (ID %s) erstellt: %d KB, %d Kategorien, %d Rollen, %d Emojis, "
        "%d Sticker, %d Mitglieder detailliert, %d Kanäle mit Nachrichten, %d/%d Bilder",
        guild.name, guild.id, len(pdf_bytes) // 1024,
        len(categories), len(roles), len(emojis), len(stickers),
        len(detailed_members), sum(1 for v in channel_messages.values() if v),
        sum(1 for v in image_cache.values() if v), len(image_cache),
    )
    return pdf_bytes


def _render_pdf(
    flowables: List[Any],
    *,
    header_left: str,
    footer_left: str,
    title: str,
    subject: str,
) -> bytes:
    """Flowables → PDF-Bytes (A4, Kopf/Fuß, „Seite X von Y")."""
    _ensure_fonts()
    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=_prepare_text(title),
        author="AIDiscordServerEinrichten",
        subject=_prepare_text(subject),
        creator="AIDiscordServerEinrichten · Adminpanel",
    )
    frame = Frame(
        MARGIN, BOTTOM_MARGIN, CONTENT_WIDTH, PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN, id="main"
    )
    doc.addPageTemplates([
        PageTemplate(id="page", frames=[frame]),  # Kopf/Fuß zeichnet _NumberedCanvas
    ])
    doc.build(
        flowables,
        canvasmaker=partial(
            _NumberedCanvas,
            header_left=_prepare_text(header_left),
            footer_left=_prepare_text(footer_left),
        ),
    )
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


async def build_channel_history_pdf(
    channel: Any,
    http_session: aiohttp.ClientSession,
    *,
    progress: ProgressCallback,
    max_messages: Optional[int] = None,
    send_part: Optional[Callable[[bytes, int, int], Any]] = None,
    ram_guard: Optional[Callable[[], Any]] = None,
) -> Any:
    """
    Chatverlauf eines Textkanals/Threads als PDF(s) — NUR für den Bot-Owner.

    **RAM-Batching:** Ist ``send_part`` gegeben, wird der Verlauf in Teilen
    zu je ``HISTORY_BATCH_MESSAGES`` Nachrichten gebaut; jeder Teil wird
    SOFORT nach dem Bau per ``send_part(pdf_bytes, teil, teile_gesamt)``
    versendet und dann verworfen — der RAM bleibt flach, egal wie groß der
    Verlauf ist („fertige Sachen senden, Daten löschen, Platz für neue“).
    Zwischen den Teilen prüft ``ram_guard`` (async) den RAM; wirft sie,
    wird abgebrochen BEVOR der Container in den OOM-Kill läuft.

    Rückgabe:
    - ``send_part=None`` (Tests/Abwärtskompatibilität): die PDF-Bytes des
      GESAMTEN Verlaufs in EINEM Dokument.
    - sonst: Metadaten ``{"parts": N, "messages": M, "span": "…"}``.

    Älteste Nachrichten zuerst; Apple-Emojis, Custom-Emojis, Bildanhänge,
    Komponenten, Embeds; Mention-Namen (@Rolle, #kanal, @Mitglied);
    verlassene Mitglieder mit rotem „nicht mehr im Server“-Hinweis.
    """
    guild = channel.guild
    styles = _styles()
    now_utc = datetime.now(timezone.utc)
    ch_name = getattr(channel, "name", "?")
    icon_url = getattr(guild.icon, "url", None) if guild.icon else None

    await progress(5, "Chatverlauf wird geladen …")

    # Mention-Namen vorbefüllen (Rollen, Kanäle, gecachte Mitglieder)
    _MENTION_NAMES.clear()
    for r in guild.roles:
        _MENTION_NAMES[r.id] = r.name
    for c in guild.channels:
        _MENTION_NAMES[c.id] = c.name
    members = list(guild.members)
    member_count = guild.member_count or len(members)
    member_count, members = await _ensure_member_list(guild, member_count, members, progress)
    member_ids = {getattr(m, "id", None) for m in members if getattr(m, "id", None)}
    for mb in members:
        _MENTION_NAMES[mb.id] = getattr(mb, "display_name", None) or mb.name

    # ── STREAMING (RAM-Kernfix): Nachrichten werden STÜCKWEISE aus der
    # Historie gelesen und in Batches zu je HISTORY_BATCH_MESSAGES gebaut —
    # es liegen NIE alle Nachrichten im RAM. Ein 8.500-Nachrichten-Verlauf
    # hat vor dem Fix allein ~100+ MB Message-Objekte im Speicher gehalten
    # (OOM-Neustart bei Teil 1/85); jetzt fließt er Teil für Teil durch.
    history = getattr(channel, "history", None)
    ait: Optional[Any] = None
    if history is not None:
        try:
            # oldest_first=True → Discord liefert ÄLTESTE zuerst (der echte
            # Discord-Liefertyp ist neueste-zuerst; mit diesem Flag streamt
            # er chronologisch, genau wie die Tests ihre Fakes liefern).
            ait = history(limit=max_messages, oldest_first=True)
        except TypeError:
            # Fakes/Ältere Signaturen kennen kein oldest_first — deren
            # Listen sind bereits chronologisch, also direkt verwenden.
            try:
                ait = history(limit=max_messages)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound,
                    AttributeError, TypeError):
                ait = None
        except (discord.Forbidden, discord.HTTPException, discord.NotFound,
                AttributeError, TypeError):
            ait = None

    seen_count = 0
    first_seen: Optional[Any] = None
    last_seen: Optional[Any] = None

    def _register(m: Any) -> None:
        nonlocal seen_count, first_seen, last_seen
        seen_count += 1
        if first_seen is None:
            first_seen = m
        last_seen = m
        if getattr(m, "author", None) is not None:
            _MENTION_NAMES.setdefault(
                getattr(m.author, "id", 0),
                getattr(m.author, "display_name", None) or m.author.name,
            )
        for u in getattr(m, "mentions", None) or []:
            _MENTION_NAMES.setdefault(u.id, getattr(u, "display_name", None) or u.name)
        for r in getattr(m, "role_mentions", None) or []:
            _MENTION_NAMES.setdefault(r.id, r.name)
        for c in getattr(m, "channel_mentions", None) or []:
            _MENTION_NAMES.setdefault(c.id, c.name)

    def _span(a: Optional[Any], b: Optional[Any]) -> str:
        if a is None or b is None:
            return ""
        return (
            f"{getattr(a.created_at, 'strftime', lambda _f: '')('%d.%m.%Y %H:%M')} – "
            f"{getattr(b.created_at, 'strftime', lambda _f: '')('%d.%m.%Y %H:%M')} UTC"
        )

    full_span = _span(first_seen, last_seen) if seen_count else ""
    total = 0  # Gesamtzahl der Teile ist beim Streaming unbekannt (0 = offen)

    def _collect_batch(batch: List[Any]) -> Tuple[Dict[int, str], List[str],
                                                  Dict[int, Optional[str]], List[str]]:
        """Bild-URLs EINES Teils (Avatare, Anhänge, Custom-Emojis, Medien)."""
        avatar_urls: Dict[int, str] = {}
        att_urls: List[str] = []
        emoji_ids: List[int] = []
        for m in batch:
            av = getattr(getattr(m, "author", None), "display_avatar", None)
            if av is not None and getattr(av, "url", None):
                avatar_urls.setdefault(getattr(m.author, "id", 0), av.url)
            for att in getattr(m, "attachments", None) or []:
                if (_attachment_is_image(att) and getattr(att, "url", None)
                        and (getattr(att, "size", 0) or 0) <= _MAX_IMAGE_BYTES):
                    att_urls.append(att.url)
            for em in _DISCORD_EMOJI_RE.finditer(m.content or ""):
                try:
                    emoji_ids.append(int(em.group(2)))
                except ValueError:
                    pass
        seen: set = set()
        att_urls = [u for u in att_urls[:MAX_ATTACHMENT_IMAGES] if not (u in seen or seen.add(u))]
        comp_media = _component_media_urls(
            [c for m in batch for c in (getattr(m, "components", None) or [])]
        )
        emoji_url_by_id: Dict[int, Optional[str]] = {}
        emoji_urls: List[Optional[str]] = []
        wanted = set(emoji_ids)
        for e in guild.emojis:
            if e.id in wanted:
                emoji_url_by_id[e.id] = getattr(e, "url", None)
                emoji_urls.append(getattr(e, "url", None))
        return avatar_urls, att_urls, emoji_url_by_id, comp_media + emoji_urls

    def _make_ctx(image_cache: Dict[str, Optional[bytes]],
                  avatar_urls: Dict[int, str],
                  emoji_url_by_id: Dict[int, Optional[str]]) -> Dict[str, Any]:
        def img(url: Optional[str], **kwargs: Any) -> Optional[Image]:
            return _image_flow(image_cache.get(url) if url else None, **kwargs)

        def avatar_img(member_id: int, size_mm: float) -> Optional[Image]:
            return _avatar_flow(image_cache.get(avatar_urls.get(member_id, "")), size_mm)

        def emoji_png(eid: int) -> Optional[str]:
            return _custom_emoji_png(image_cache.get(emoji_url_by_id.get(eid, "")),
                                     f"hist-emoji-{eid}")

        return {
            "styles": styles,
            "img": img,
            "avatar_img": avatar_img,
            "emoji_png": emoji_png,
            "member_ids": member_ids,
            "content_width": CONTENT_WIDTH,
        }

    def _head_flowables(image_cache: Dict[str, Optional[bytes]],
                        idx: int, batch: List[Any]) -> List[Any]:
        """Kopf eines Teil-PDFs (Titel, Server/Kanal/IDs, Zeitraum des Teils)."""
        icon_flow = _avatar_flow(image_cache.get(icon_url), 18) if icon_url else None
        span = ""
        if batch:
            span = (
                f"{batch[0].created_at.strftime('%d.%m.%Y %H:%M')} – "
                f"{batch[-1].created_at.strftime('%d.%m.%Y %H:%M')} UTC"
            )
        is_multi = total != 1  # total == 0 = streaming (mehrere Teile möglich)
        title = "Chatverlauf" if not is_multi else f"Chatverlauf — Teil {idx + 1}"
        ch_type_name = str(getattr(getattr(channel, "type", None), "name", "kanal") or "kanal")
        head_bits = [
            f"Server <b>{_clean(guild.name)}</b> (ID {_clean(guild.id)})",
            f"{_clean(ch_type_name.replace('_', ' ').title())}: <b>#{_clean(ch_name)}</b> "
            f"(ID {_clean(channel.id)})",
            f"{len(batch)} Nachricht{'en' if len(batch) != 1 else ''}"
            + (f" · Zeitraum {span}" if span else ""),
        ]
        if is_multi:
            if total > 1:
                head_bits.append(f"Teil {idx + 1} von {total}")
            else:
                head_bits.append(f"Teil {idx + 1} des Verlaufs")
        head_cells: List[Any] = ([icon_flow] if icon_flow is not None else []) + [[
            Paragraph(title, ParagraphStyle(
                "hist_title", fontName=_FONTS["bold"], fontSize=17, leading=21, textColor=_C.dark,
            )),
            Paragraph(" · ".join(head_bits), styles["subtitle"]),
            Paragraph(
                "<font color='#ED4245'>Vertraulich — nur für den Bot-Owner bestimmt.</font> "
                "Nicht weitergeben.", styles["subtitle"],
            ),
        ]]
        head_widths = [24 * mm, CONTENT_WIDTH - 24 * mm] if icon_flow is not None else [CONTENT_WIDTH]
        head = Table([head_cells], colWidths=head_widths)
        head.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        flows: List[Any] = [head,
                            HRFlowable(width="100%", thickness=1.1, color=_C.blurple,
                                       spaceAfter=3 * mm)]
        if seen_count == 0:
            flows.append(Paragraph(
                "Keine Nachrichten abrufbar (leer, keine Historie-Rechte oder Kanal nicht gefunden).",
                styles["body"]))
        elif is_multi and idx > 0:
            flows.append(Paragraph(
                f"Fortsetzung des Verlaufs — Teil {idx + 1} beginnt bei "
                f"{batch[0].created_at.strftime('%d.%m.%Y %H:%M')} UTC.", styles["subtitle"]))
        return flows

    async def _build_and_deliver(idx: int, batch: List[Any]) -> bytes:
        """Ein Teil: Bilder laden → Flowables im Thread bauen → PDF → senden."""
        if total == 0:
            label = f"Teil {idx + 1}"
        elif total > 1:
            label = f"Teil {idx + 1}/{total}"
        else:
            label = "Chatverlauf"
        base_pct = (30 + int(58 * idx / max(total, 1))) if total else 38
        await progress(base_pct, f"{label}: Bilder werden geladen …")

        avatar_urls, att_urls, emoji_url_by_id, extra_urls = _collect_batch(batch)
        image_urls: List[Optional[str]] = [icon_url]
        for uid in list(avatar_urls)[:60]:
            image_urls.append(avatar_urls[uid])
        image_urls.extend(att_urls)
        image_urls.extend(extra_urls)

        image_cache = await _download_images(http_session, image_urls)
        _dl_ok = sum(1 for v in image_cache.values() if v)
        await progress(base_pct + 8,
                       f"{label}: Bilder geladen ({_dl_ok}/{len(image_cache)}) — "
                       "PDF wird aufbereitet …")

        _ensure_fonts()
        _ensure_apple_emoji()

        def _assemble() -> bytes:
            ctx = _make_ctx(image_cache, avatar_urls, emoji_url_by_id)
            flowables: List[Any] = _head_flowables(image_cache, idx, batch)
            for m in batch:
                flowables.extend(_message_flowables(m, styles, ctx))
                flowables.append(Spacer(1, 2.4 * mm))
            return _render_pdf(
                flowables,
                header_left=f"Chatverlauf #{ch_name} · {guild.name}",
                footer_left=(
                    "Vertraulich — nur für den Bot-Owner bestimmt. Erstellt am "
                    + now_utc.strftime("%d.%m.%Y, %H:%M") + " UTC"
                ),
                title=f"Chatverlauf #{ch_name} · {guild.name}",
                subject=f"Chatverlauf von #{ch_name} in {guild.name} (ID {guild.id})",
            )

        pdf_bytes = await asyncio.wait_for(
            asyncio.to_thread(_assemble), timeout=_ASSEMBLY_TIMEOUT_S
        )

        # RAM-Hygiene nach JEDEM Teil: Cache leeren, GC laufen lassen, Sheet
        # schließen (Crops bleiben auf Disk) — „Daten löschen, Platz für neue“.
        image_cache.clear()
        gc.collect()
        if ram_guard is not None:
            await ram_guard()

        if send_part is not None:
            await send_part(pdf_bytes, idx + 1, total)
        await progress(base_pct + 12, f"{label} fertig.")
        log.info("Chatverlauf-%s #%s: %d KB (%d Nachrichten)",
                 f"Teil {idx + 1}/{total}" if total > 1 else "PDF",
                 ch_name, len(pdf_bytes) // 1024, len(batch))
        return pdf_bytes

    parts_bytes: List[bytes] = []
    part_idx = 0
    batch: List[Any] = []
    loaded_since_guard = 0

    async def _maybe_guard() -> None:
        nonlocal loaded_since_guard
        loaded_since_guard += 1
        if ram_guard is not None and loaded_since_guard >= 200:
            loaded_since_guard = 0
            await ram_guard()

    async def _finish_batch() -> None:
        nonlocal part_idx, batch, full_span
        if not batch:
            return
        full_span = _span(first_seen, last_seen) if seen_count else full_span
        pdf_bytes = await _build_and_deliver(part_idx, batch)
        parts_bytes.append(pdf_bytes)
        part_idx += 1
        batch = []

    if ait is not None:
        try:
            async for m in ait:
                _register(m)
                batch.append(m)
                await _maybe_guard()
                if len(batch) >= HISTORY_BATCH_MESSAGES:
                    await _finish_batch()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass  # Historie bricht mitten im Stream ab — Bisheriges bleibt gültig
    await _finish_batch()
    if not parts_bytes:
        # Leerer Verlauf: genau ein (leeres) PDF mit Hinweis
        parts_bytes.append(await _build_and_deliver(0, []))
        part_idx = 1
    total = part_idx

    _close_apple_sheet()  # RAM: Sprite-Sheet freigeben (Crops bleiben gecacht)

    await progress(100, "Chatverlauf fertiggestellt.")
    log.info(
        "Chatverlauf für #%s in '%s' (ID %s): %d Teil-PDF(s), %d Nachrichten (streamend), Zeitraum %s",
        ch_name, guild.name, guild.id, total, seen_count, _span(first_seen, last_seen) or "—",
    )
    if send_part is None:
        return parts_bytes[0]
    return {"parts": total, "messages": seen_count,
            "span": _span(first_seen, last_seen),
            "bytes": [len(b) for b in parts_bytes]}
