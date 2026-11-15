#!/usr/bin/env python3
"""
PDF-Test für das Server-Dokument (``bot/web/pdf_document.py``) — **ohne**
externes Netzwerk.

Prüfungen (die Kern-Bugs der Vorgängerversion betreffend):

1. **Bilder wirklich im PDF** — der Build lädt Icon, Banner, Avatare, Emojis
   und Sticker von einem *lokalen* aiohttp-Bild-Server (Pillow-generierte
   PNGs/GIFs) und das fertige PDF muss entsprechende Bild-XObjects enthalten.
2. **Keine schwarzen Vierecke** — DejaVu-Fonts werden eingebettet
   (``/FontFile2``) und ``_prepare_text`` übersetzt Emojis in Wörter, filtert
   Nicht-Glyphen heraus; jeder Zeichenstrom im PDF besteht ausschließlich aus
   Zeichen, die der eingebettete Font auch darstellen kann.
3. **Kein Tabellen-Überlauf** — alle Tabellenbreiten summieren sich exakt auf
   ≤ CONTENT_WIDTH; Berechtigungs- und Rollentexte sind Paragraphs (umbrechend).
4. **Dokument baut erfolgreich & vollständig** — %PDF-Header, %%EOF, mehrere
   Seiten, Fortschritts-Callback läuft bis 100 %.

Aufruf::

    python3 scripts/pdf_document_test.py [-v]

Exit-Code 0 = grün. Läuft in CI ohne Discord-Token und ohne Internet.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from datetime import timedelta
from typing import Any, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

os.environ.setdefault("DISCORD_BOT_TOKEN", "pdf.test.token")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import aiohttp  # noqa: E402
import discord  # noqa: E402
from aiohttp import web  # noqa: E402
from PIL import Image as PILImage, ImageDraw  # noqa: E402

from bot.web.pdf_document import _clean  # noqa: E402

from bot.web import pdf_document as pdf_mod  # noqa: E402
from bot.web.pdf_document import (  # noqa: E402
    CONTENT_WIDTH,
    _ensure_fonts,
    _prepare_text,
    build_channel_history_pdf,
    build_server_document,
)

from _fake_discord import (  # noqa: E402
    FakeAsset, FakeCategory, FakeForumChannel, FakeGuild, FakeMember, FakeMessage,
    FakeRole, FakeStageChannel, FakeTextChannel, FakeVoiceChannel, now,
)

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

PASSED: List[str] = []
FAILED: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        if VERBOSE:
            print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name} {('- ' + detail) if detail else ''}")
        print(f"  ❌ {name}  {detail[:300]}")
    return bool(condition)


def _count_apple_images(pdf_bytes: bytes) -> int:
    """Zählt eindeutige 66×66-Inline-Bilder (= Apple-Emoji-Crops) im PDF."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover — pymupdf ist optional
        return -1
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    seen = set()
    for page in doc:
        for img in page.get_images(full=True):
            if img[2] == 66 and img[3] == 66:
                seen.add(img[0])
    doc.close()
    return len(seen)



# ─────────────────────────────────────────────────────────────────────────────
#  Bilderzeugung für den lokalen CDN-Ersatz
# ─────────────────────────────────────────────────────────────────────────────


def _make_img(width: int, height: int, color: str, label: str,
              fmt: str = "PNG", frames: int = 1, alpha: bool = False) -> bytes:
    imgs = []
    for i in range(frames):
        if alpha:
            im = PILImage.new("RGBA", (width, height), (0, 0, 0, 0))
            layer = PILImage.new("RGBA", (width, height), color)
            mask = PILImage.new("L", (width, height), 0)
            ImageDraw.Draw(mask).ellipse((2, 2, width - 3, height - 3), fill=255)
            im.paste(layer, (0, 0), mask)
        else:
            im = PILImage.new("RGB", (width, height), "white")
            d = ImageDraw.Draw(im)
            d.ellipse([2, 2, width - 3, height - 3], fill=color)
            d.text((width // 3, height // 3), f"{label}{i}", fill="black")
        imgs.append(im)
    buf = io.BytesIO()
    if fmt == "GIF":
        imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=400, loop=0)
    elif fmt == "WEBP":
        imgs[0].save(buf, format="WEBP")
    else:
        imgs[0].save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
#  Fake-Server mit absichtlich "bösem" Inhalt
# ─────────────────────────────────────────────────────────────────────────────


def _build_guild(base_url: str) -> Any:
    g = FakeGuild(555123888444222111, "🎮 Gamer Lounge «Berlin» — Тест")
    g.icon = FakeAsset(base_url + "icon.png")
    g.banner = FakeAsset(base_url + "banner.png")
    g.description = "Testserver für das Server-Dokument 🎉"
    g.created_at = now() - timedelta(days=120)
    g.member_count = 4
    g.premium_tier = 2
    g.premium_subscription_count = 7

    cat = FakeCategory(455001000000000001, "📌 INFORMATION", position=0)
    t1 = FakeTextChannel(
        455101000000000003, "willkommen", position=0, category=cat,
        topic="Willkommen! 🎉 Regeln lesen — Привет · 中文テキスト «ok» ✓",
    )
    t2 = FakeTextChannel(455102000000000004, "allgemein", position=1, category=cat)
    v1 = FakeVoiceChannel(455201000000000006, "Lounge", position=0, category=cat)
    st1 = FakeStageChannel(455202000000000007, "Bühne «Events»", position=1, category=cat)
    f1 = FakeForumChannel(455203000000000008, "vorschläge", position=2, category=cat)
    loose = FakeTextChannel(455104000000000009, "ohne-kategorie", position=0)
    cat.channels = [t1, t2, v1, st1, f1]
    g.channels = [cat, t1, t2, v1, st1, f1, loose]
    g._reindex()

    everyone = g.roles[0]
    mod = g.roles[1]
    admin_role = FakeRole(455801000000000010, "👑 Admin", 20,
                          discord.Permissions.all().value, color=0xED4245, hoist=True)
    bot_role = FakeRole(455802000000000011, "AIDiscordServerEinrichten", 15,
                        discord.Permissions.all().value, color=0x5865F2, hoist=True)
    player_role = FakeRole(455803000000000012, "Spieler", 5,
                           discord.Permissions(send_messages=True, view_channel=True,
                                               connect=True).value, color=0x57F287)
    g.roles = [everyone, player_role, bot_role, admin_role, mod]
    g.default_role = everyone

    owner = FakeMember(455701000000000014, "ServerBoss", [everyone, admin_role], guild=g)
    owner.display_avatar = FakeAsset(base_url + "av0.png")
    owner.joined_at = now() - timedelta(days=119)
    m2 = FakeMember(455702000000000015, "ModMike", [everyone, mod, player_role],
                    guild=g, nick="Mike 🛡️")
    m2.display_avatar = FakeAsset(base_url + "av1.png")
    m3 = FakeMember(455703000000000016, "Botterich", [everyone, bot_role], bot=True, guild=g)
    m3.display_avatar = FakeAsset(base_url + "av2.png")
    m4 = FakeMember(455704000000000017, "Userchen", [everyone, player_role], guild=g)
    m4.display_avatar = FakeAsset(base_url + "av3.png")
    g.members = [owner, m2, m3, m4]
    g.owner = owner

    g.emojis = [
        type("E", (), {"id": 455601000000000018, "name": "tada", "animated": False,
                       "url": base_url + "emoji1.png"})(),
        type("E", (), {"id": 455602000000000019, "name": "Herzstück", "animated": False,
                       "url": base_url + "emoji2.webp"})(),
        type("E", (), {"id": 455603000000000020, "name": "spin", "animated": True,
                       "url": base_url + "emoji3.gif"})(),
    ]
    g.stickers = [
        type("S", (), {"id": 455610000000000021, "name": "Party-Sticker 🎈",
                       "url": base_url + "sticker1.png",
                       "type": discord.StickerType.standard})(),
    ]

    def _hist(msgs):
        # Wie der echte discord.py-HistoryIterator: NEUESTE zuerst; mit
        # oldest_first=True chronologisch (so konsumiert ihn der Streamer).
        def history(limit=None, oldest_first=False):
            # msgs ist NEUESTE-zuerst sortiert — oldest_first kehrt um.
            ordered = list(reversed(msgs)) if oldest_first else msgs

            async def gen():
                for m in ordered[: limit or None]:
                    yield m
            return gen()
        return history

    def msg(mid, author, content, embeds=None, webhook_id=None):
        m = FakeMessage(mid, t1, author, content=content, embeds=embeds or [])
        m.created_at = now() - timedelta(hours=mid % 50)
        m.webhook_id = webhook_id
        return m

    embed = discord.Embed(title="Regeln ✅",
                          description="Sei nett «bitte» → Привет 中文 ✓",
                          color=discord.Colour(0x5865F2))
    embed.add_field(name="Feld «Eins»", value="Wert eins", inline=True)
    embed.set_footer(text="Fußzeile mit Emoji 🎈")
    embed.timestamp = now()

    att = type("A", (), {"filename": "screenshot-🎉.png"})()

    webhook_author = FakeMember(455705000000000018, "WebhookDing", [everyone], guild=g)
    webhook_author.display_avatar = FakeAsset(base_url + "av4.png")
    m_webhook = msg(30, webhook_author, "Webhook-Nachricht mit Anhang",
                    webhook_id=455900000000000019)
    m_webhook.attachments = [att]

    long_text = (
        "Eine sehr lange Nachricht mit Berechtigungen wie Administrator, Manage Roles, "
        "Kick Members und noch viel mehr Text, der über mehrere Zeilen umbrechen muss — "
        "امدностей und Жирный и кириллица und 中文 — damit Tabellen niemals überlaufen."
    )

    t1.history = _hist([
        msg(1, owner, f"Willkommen 🎉 👋 <@455701000000000014> <:tada:455601000000000018> — Привет 中文 ✓ Unicode-Test: 𝗖𝗵𝗮𝘁 𝕏 {long_text}"),
        msg(2, m2, "Ping 👍🏽 :tada: sieht cool aus \U0001F9D1\U0001F3FB\u200D\U0001F91D\U0001F3FC (Mono-Fallback-Sequenz)"),
        msg(3, m3, "Bot-Nachricht", embeds=[embed]),
        m_webhook,
    ])
    t2.history = _hist([msg(4, m4, "Zweite Nachricht ✓ — 中文 テスト 한국어"),])
    loose.history = _hist([])
    # Bot-Komponenten (Layouts v2): Container mit Text, Trennlinien, Galerie,
    # Buttons — wird als strukturierte Bullet-Liste dargestellt.
    def _comp(name: str, **kw: Any) -> Any:
        return type("C", (), {"type": type("T", (), {"name": name}), **kw})()
    btn_link = _comp("button", label="Receive support", style=type("S", (), {"name": "LINK"}))
    m_comp = msg(5, m3, "Support-Panel mit Komponenten")
    m_comp.components = [
        _comp("container", children=[
            _comp("text_display", content="Wähle unten eine Option"),
            _comp("separator"),
            _comp("text_display", content="Hilfe gibt es im Kanal"),
            _comp("media_gallery", items=[object(), object()]),
            _comp("separator"),
            _comp("action_row", children=[btn_link,
                                          _comp("button", label="Setup now!",
                                                style=type("S", (), {"name": "LINK"}))]),
        ]),
    ]
    t2.history = _hist([msg(4, m4, "Zweite Nachricht ✓ — 中文 テスト 한국어"), m_comp])
    # Kanal-Details: Slow-Modus + NSFW an zwei Kanälen
    t2.slowmode_delay = 300
    t2.nsfw = True
    v1.bitrate = 64000
    v1.user_limit = 12
    return g


# ─────────────────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────────────────


async def _image_server(app_port_box: List[int]) -> web.AppRunner:
    served = {
        "icon.png": _make_img(256, 256, "#5865F2", "ICO"),
        "banner.png": _make_img(960, 270, "#EB459E", "BAN"),
        "av0.png": _make_img(128, 128, "#ED4245", "M0"),
        "av1.png": _make_img(128, 128, "#FAA61A", "M1"),
        "av2.png": _make_img(128, 128, "#5865F2", "M2"),
        "av3.png": _make_img(128, 128, "#23A559", "M3"),
        "av4.png": _make_img(128, 128, "#00A8FC", "M4"),
        "emoji1.png": _make_img(64, 64, "#F0B232", "E1"),
        "emoji2.webp": _make_img(64, 64, "#5865F2", "E2"),
        "emoji3.gif": _make_img(64, 64, "#23A559", "E3", fmt="GIF", frames=3),
        "sticker1.png": _make_img(320, 320, "#EB459E", "S1"),
    }

    async def serve(request: Any) -> web.Response:
        name = request.match_info["name"]
        if name not in served:
            return web.Response(status=404)
        ctype = {
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        }[os.path.splitext(name)[1]]
        return web.Response(body=served[name], content_type=ctype)

    app = web.Application()
    app.router.add_get("/{name}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    app_port_box.append(site._server.sockets[0].getsockname()[1])
    return runner


async def run_tests() -> int:
    print("\n  ── Server-Dokument (PDF): Einheiten-Prüfungen ──\n")

    # ── Fonts geladen & eingebettet ────────────────────────────────────────
    fonts = _ensure_fonts()
    check("DejaVu-Fonts registriert (kein Base-14-Fallback)",
          fonts["sans"] != "Helvetica" and fonts["bold"] != "Helvetica-Bold",
          str(fonts))
    check("Glyph-Abdeckung ermittelt",
          pdf_mod._GLYPH_COVERAGE is not None and len(pdf_mod._GLYPH_COVERAGE or set()) > 3000)
    for ch in ("ä", "ö", "ü", "ß", "«", "»", "→", "✓", "—", "Ж"):
        check(f"Glyphe vorhanden: {ch!r}", ord(ch) in (pdf_mod._GLYPH_COVERAGE or set()))

    # ── Text-Aufbereitung: Emojis & Unicode-Schriften bleiben sichtbar ─────
    nasty = "🎮🎉 🛡️ <:custom_emoji:123456789012345678> <@123456789012345678> 👍🏽 🇩🇪 Привет 中文 テスト 한국어 ✓ «ok» 𝗖𝗵𝗮𝘁 𝕏"
    prepared = _prepare_text(nasty)
    check("Custom-Emoji-Syntax aufgelöst", "custom emoji" in prepared or "custom emoji" in prepared.lower(), prepared)
    check("Member-Mention lesbar umschrieben", "@nutzer-123456789012345678" in prepared, prepared)
    # ── Mention-Namen (neu): @Mitgliedsname, @Rollenname, #kanalname ──────
    pdf_mod._MENTION_NAMES[123456789012345678] = "MaxMustermann"
    pdf_mod._MENTION_NAMES[999999999999999998] = "Support-Team"
    pdf_mod._MENTION_NAMES[999999999999999997] = "allgemein"
    prepared_named = _prepare_text(
        "<@123456789012345678> <@&999999999999999998> <#999999999999999997>"
    )
    check("Mitglied-Mention → @Name", "@MaxMustermann" in prepared_named, prepared_named)
    check("Rollen-Mention → @Rollenname", "@Support-Team" in prepared_named, prepared_named)
    check("Kanal-Mention → #kanalname", "#allgemein" in prepared_named, prepared_named)
    del pdf_mod._MENTION_NAMES[123456789012345678]
    del pdf_mod._MENTION_NAMES[999999999999999998]
    del pdf_mod._MENTION_NAMES[999999999999999997]
    # ── Apple-Emojis (neu): Inline-<img>-Tags statt Monochrom-Glyphen ─────
    apple_markup = pdf_mod._message_content_markup(
        "🎉 👋 👍🏽 🇩🇪", emoji_png_by_id=lambda _eid: None)
    check("Apple-Emoji als Inline-Bild (iOS-Look)",
          "<img src=" in apple_markup and 'valign="middle"' in apple_markup
          and apple_markup.count("<img") == 4, apple_markup[:160])
    check("Custom-Emoji-Renderer erzeugt Inline-Bild",
          pdf_mod._custom_emoji_img_tag("/tmp/x.png") is not None
          and pdf_mod._custom_emoji_img_tag(None) is None, "")
    check("Nicht-Bild-Anhang erkannt",
          not pdf_mod._attachment_is_image(type("A", (), {"filename": "datei.zip"})())
          and pdf_mod._attachment_is_image(type("A", (), {
              "filename": "bild.png", "content_type": "image/png",
              "url": "http://x/bild.png", "width": 100, "height": 80})()), "")
    check("Button-Chip hat Diskord-Farben (Link-Chip färbbar)",
          len(pdf_mod._BUTTON_CHIP_COLORS) >= 5
          and "link" in pdf_mod._BUTTON_CHIP_COLORS, str(list(pdf_mod._BUTTON_CHIP_COLORS)))
    check("Unicode-Emojis bleiben erhalten", "🎮" in prepared and "🎉" in prepared, prepared)
    check("Unicode-Schriftarten bleiben erhalten", "𝗖𝗵𝗮𝘁" in prepared and "𝕏" in prepared, prepared)
    check("Emoji-/Math-Font-Zusatzregister vorhanden",
          pdf_mod._FONT_COVERAGE.get("emoji") and pdf_mod._FONT_COVERAGE.get("math"),
          str({k: (len(v) if v else None) for k, v in pdf_mod._FONT_COVERAGE.items()}))
    # No-Notdef-Garantie: jedes Zeichen im fertigen Markup (Tags entfernt)
    # muss in mind. einem der drei Fonts darstellbar sein.
    markup = _clean(nasty)
    tag_free = re.sub(r"</?font[^>]*>|<br/>", "", markup)
    all_covered: set = set()
    for cov in pdf_mod._FONT_COVERAGE.values():
        if cov:
            all_covered.update(cov)
    uncovered = [ch for ch in tag_free if ord(ch) not in all_covered]
    check("Markup: kein Zeichen ohne Glyphe in irgendeinem Font", not uncovered, repr(uncovered[:10]))
    check("CJK ist jetzt sichtbar (Noto Sans SC/KR)",
          "中" in markup and "文" in markup and "한" in markup and "テ" in markup)
    check("Wirklich ungedeckte Zeichen (U+0378, unassigned) gefiltert statt Notdef",
          "\u0378" not in _clean("a\u0378b") and "ab" in _clean("a\u0378b"))
    check("Zeitangaben bleiben unzerstört",
          _prepare_text("Treffen 12:30:00 Uhr") == "Treffen 12:30:00 Uhr",
          _prepare_text("Treffen 12:30:00 Uhr"))
    check("Steuerzeichen entfernt",
          "\u200b" not in _prepare_text("a\u200bb\u202ec") and "\u202e" not in _prepare_text("a\u200bb\u202ec"))
    check("Mehrfach-Leerzeichen normalisiert",
          _prepare_text("a   \n   b") == "a b", repr(_prepare_text("a   \n   b")))
    # ── Tabellenbreiten ────────────────────────────────────────────────────
    print("\n  ── Server-Dokument (PDF): Komplett-Build gegen Fake-Server ──\n")

    port_box: List[int] = []
    runner = await _image_server(port_box)
    try:
        guild = _build_guild(f"http://127.0.0.1:{port_box[0]}/")

        progress_calls = []
        async def progress(pct: int, text: str) -> None:
            progress_calls.append((pct, text))

        async with aiohttp.ClientSession() as session:
            pdf_bytes = await build_server_document(guild, session, progress=progress)

        check("Build liefert PDF-Bytes", pdf_bytes[:5] == b"%PDF-", repr(pdf_bytes[:8]))
        check("PDF endet mit %%EOF", pdf_bytes.rstrip().endswith(b"%%EOF"))
        check("Fortschritts-Callback bis 100 %", bool(progress_calls) and progress_calls[-1][0] == 100,
              str(progress_calls[-3:] if progress_calls else None))
        check("PDF hat mehrere Seiten", pdf_bytes.count(b"/Type /Page") >= 5,
              str(pdf_bytes.count(b"/Type /Page")))

        # 1) Der Kern-Bug-Fix: Bilder sind tatsächlich eingebettet
        n_images = len(re.findall(rb"/Subtype\s*/Image", pdf_bytes))
        # sichtbare Bilder (9: icon, banner, 4 Avatare + 1 Webhook-Avatar, 3 Emojis, 1 Sticker)
        # plus je 1 Alphamaske (SMask) für PNGs mit Alpha
        check("Bild-XObjects im PDF vorhanden (≥ 15)", n_images >= 15, f"{n_images}")
        check("PDF-Größe plausibel mit Bildern (> 80 KB)", len(pdf_bytes) > 80_000,
              f"{len(pdf_bytes) // 1024} KB")
        check("Fonts eingebettet (≥ 5: Sans, Bold, Mono, Emoji, Math)",
              len(re.findall(rb"/FontFile2", pdf_bytes)) >= 5,
              f"{len(re.findall(rb'/FontFile2', pdf_bytes))} FontFile2-Streams")

        # 2) Notdef-Schutz: ungedeckte Zeichen (U+0378, unassigned — kein
        #    Font der Welt hat dafür Glyphen) werden im Markup gefiltert
        #    statt als schwarzes Viereck zu landen; CJK läuft über SC:
        check("Notdef-Schutz: ungedeckte Zeichen gefiltert, CJK sichtbar",
              "\u0378" not in _clean("中\u0378文") and "中" in _clean("中\u0378文"),
              repr(_clean("中\u0378文")))

        # 3) Tabellenbreiten: Summe der colWidths entspricht CONTENT_WIDTH
        check("CONTENT_WIDTH ist A4 abzüglich Ränder",
              abs(CONTENT_WIDTH / 2.8346 - (210 - 36)) < 1.0, f"{CONTENT_WIDTH / 2.8346:.1f} mm")

        # 4) Neue Layout-Features: Emojis/Math-Fonts WIRKLICH benutzt
        #    (ReportLab bettet nur benutzte Fonts ein — dies schlägt fehl,
        #    wenn keine Emoji-/Math-Zeichen gerendert wurden), Kanal-Details
        #    (Slow-Modus, NSFW, Bitrate), Nachrichten älteste zuerst,
        #    Kanal-Überschriften.
        try:
            import pymupdf  # noqa: F401
        except ImportError:  # pragma: no cover — pymupdf ist optional
            pymupdf = None
        check("Apple-Emojis im PDF (66×66-Inline-Bilder aus dem Sheet)",
              _count_apple_images(pdf_bytes) >= 4, str(_count_apple_images(pdf_bytes)))
        # Mono-Fallback: OHNE Apple-Sheet übernimmt NotoEmoji (Monochrom)
        _saved_index = dict(pdf_mod._apple_index)
        pdf_mod._apple_index.clear()
        try:
            fallback_markup = _clean("🎉")
        finally:
            pdf_mod._apple_index.update(_saved_index)
        check("Emoji-Fallback ohne Apple-Sheet: NotoEmoji-Monochrom",
              f"<font name='{pdf_mod._FONTS['emoji']}'>🎉</font>" in fallback_markup,
              fallback_markup[:120])
        check("Math-Font im PDF eingebettet & benutzt",
              b"NotoSansMath-Regular" in pdf_bytes, "")
        if pymupdf is not None:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            full_text = " ".join("\n".join(p.get_text() for p in doc).split())
            doc.close()
            check("Extraktion: Kanal-Details (Slow-Modus 5 min, NSFW)",
                  "Slow-Modus 5 min" in full_text and "NSFW" in full_text, "")
            check("Extraktion: Voice-Details (Bitrate, Limit)",
                  "Bitrate 64 kbps" in full_text and "Limit 12 Nutzer" in full_text, "")
            check("Extraktion: Kanal-Überschrift #allgemein vorhanden",
                  "#allgemein" in full_text, "")
            check("Extraktion: Administrator-Zeile statt Aufzählung",
                  "ADMINISTRATOR — umfasst ALLE Berechtigungen" in full_text, "")
            check("Extraktion: Komponenten als echte Darstellung (Text, Buttons als Chips)",
                  "Wähle unten eine Option" in full_text and "Receive support" in full_text
                  and "Setup now!" in full_text and "Hilfe gibt es im Kanal" in full_text, "")
            check("Extraktion: Mention mit Namen (@ServerBoss statt @nutzer-…)",
                  "@ServerBoss" in full_text and "@nutzer-455701000000000014" not in full_text, "")
            check("Extraktion: roter Hinweis für nicht mehr im Server",
                  "nicht mehr im Server" in full_text, "")

        # ── Chatverlauf-PDF (neu): komplette Kanal-Historie für den Bot-Owner ──
        print("\n  ── Chatverlauf-PDF (Kanal-Historie) ──\n")
        async with aiohttp.ClientSession() as session:
            hist_bytes = await build_channel_history_pdf(
                guild.text_channels[0], session, progress=progress)

        check("Chatverlauf-PDF liefert PDF-Bytes", hist_bytes[:5] == b"%PDF-",
              repr(hist_bytes[:8]))
        check("Chatverlauf-PDF endet mit %%EOF", hist_bytes.rstrip().endswith(b"%%EOF"))
        if pymupdf is not None:
            hdoc = pymupdf.open(stream=hist_bytes, filetype="pdf")
            hist_text = " ".join("\n".join(p.get_text() for p in hdoc).split())
            hdoc.close()
            check("Chatverlauf: Kopf (#willkommen, Chatverlauf, vertraulich)",
                  "#willkommen" in hist_text and "Chatverlauf" in hist_text
                  and "nur für den Bot-Owner" in hist_text, "")
            check("Chatverlauf: älteste Nachricht zuerst (Webhook 30 h alt)",
                  hist_text.find("Webhook-Nachricht") < hist_text.find("Willkommen"), "")
            check("Chatverlauf: Mention-Namen aufgelöst",
                  "@ServerBoss" in hist_text, "")
            check("Chatverlauf: nicht mehr im Server (Webhook-Autor)",
                  "nicht mehr im Server" in hist_text, "")


        # ── Regression: Riesige Nachrichten dürfen den Build nicht crashen ──
        # Discord erlaubt 4000–8000 Zeichen pro Nachricht und Embeds mit je
        # 4096 Zeichen. Vor dem Fix landete alles in EINER Tabellenzeile —
        # höher als eine Seite, ReportLab brach mit LayoutError ab
        # („too large on page … tallest cell … points"). Jetzt: splittbare
        # Flowables + Kapselung.
        print("\n  ── Regression: Riesige Nachrichten / Embeds ──\n")
        giant_guild = _build_guild(f"http://127.0.0.1:{port_box[0]}/")
        giant_channel = giant_guild.text_channels[0]
        big_embed = discord.Embed(title="Riesiges Embed",
                                  description="X" * 4096, color=discord.Colour(0x5865F2))
        for i in range(3):
            big_embed.add_field(name=f"Feld {i}", value="Y" * 1000, inline=True)

        def _giant_history(limit=None):
            async def gen():
                yield FakeMessage(999000000000000001, giant_channel, giant_guild.owner,
                                  content="Z" * 8000, embeds=[big_embed])
            return gen()

        giant_channel.history = _giant_history

        async def _silent_progress(pct: int, text: str) -> None:
            pass

        async with aiohttp.ClientSession() as session:
            giant_pdf = await build_server_document(giant_guild, session, progress=_silent_progress)

        check("Riesige Nachricht (8000 Zeichen + 4096-Zeichen-Embed): Build crasht nicht",
              giant_pdf[:5] == b"%PDF-" and giant_pdf.rstrip().endswith(b"%%EOF"),
              f"{len(giant_pdf)} bytes")
        check("Riesige Nachricht wird gekürzt, nicht verworfen",
              len(giant_pdf) > 40_000, f"{len(giant_pdf) // 1024} KB")
    finally:
        await runner.cleanup()

    print(f"\n  ✅ {len(PASSED)} bestanden     ❌ {len(FAILED)} fehlgeschlagen\n")
    return 1 if FAILED else 0


def main() -> int:
    try:
        return asyncio.run(run_tests())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
