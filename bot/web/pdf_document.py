"""
Erstellung des **Server-Dokuments (PDF)** — komplette Server-Übersicht.

Wird aus dem Adminpanel (Button „Server-Dokument erstellen“) aufgerufen,
erzeugt ein PDF mit allen verfügbaren Server-Daten und liefert die PDF-Bytes
zurück. Der Versand per DM an den Server-Owner erfolgt im Aufrufer
(``RelayClient._handle_admin_create_doc``), damit der Server selbst nichts
bemerkt.

Alle Discord-API-Aufrufe sind fehlertolerant: Fehlende Rechte (z. B. keine
Nachrichtenhistorie) führen nicht zum Abbruch, sondern werden als „nicht
verfügbar“ ausgewiesen.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp
import discord
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger("relay.pdf")

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 16 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

MAX_MEMBERS_DETAILED = 50
MAX_MESSAGES_PER_CHANNEL = 10

#: Fortschritts-Callback: (prozent: int, text: str) -> None
ProgressCallback = Callable[[int, str], None]


# ─────────────────────────────────────────────────────────────────────────────
#  Bild-Download (concurrent, gecacht pro URL)
# ─────────────────────────────────────────────────────────────────────────────


async def _download_image(
    session: aiohttp.ClientSession,
    url: Optional[str],
    cache: Dict[str, Optional[ImageReader]],
    semaphore: asyncio.Semaphore,
) -> Optional[ImageReader]:
    """Lädt ein Bild herunter (ggf. aus Cache) und liefert einen ImageReader."""
    if not url:
        return None
    if url in cache:
        return cache[url]
    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    cache[url] = None
                    return None
                data = await resp.read()
            reader = ImageReader(io.BytesIO(data))
            cache[url] = reader
            return reader
        except Exception as exc:  # noqa: BLE001 — ein fehlendes Bild darf nicht abbrechen
            log.debug("Bild konnte nicht geladen werden %s: %s", url, exc)
            cache[url] = None
            return None


async def _fetch_images(
    session: aiohttp.ClientSession,
    urls: List[Optional[str]],
) -> Dict[str, Optional[ImageReader]]:
    """Lädt mehrere Bilder parallel herunter (max. 8 gleichzeitig)."""
    cache: Dict[str, Optional[ImageReader]] = {}
    semaphore = asyncio.Semaphore(8)
    await asyncio.gather(
        *(
            _download_image(session, url, cache, semaphore)
            for url in urls
            if url and url not in cache
        )
    )
    return cache


# ─────────────────────────────────────────────────────────────────────────────
#  Stil-Helfer
# ─────────────────────────────────────────────────────────────────────────────


def _styles() -> Dict[str, ParagraphStyle]:
    base = {
        "title": ParagraphStyle(
            "title", fontName="Helvetica-Bold", fontSize=17, leading=21,
            textColor=colors.HexColor("#2C2F33"), alignment=TA_LEFT, spaceAfter=6,
        ),
        "h1": ParagraphStyle(
            "h1", fontName="Helvetica-Bold", fontSize=13, leading=16,
            textColor=colors.HexColor("#5865F2"), spaceBefore=10, spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "h2", fontName="Helvetica-Bold", fontSize=10.5, leading=13,
            textColor=colors.HexColor("#2C2F33"), spaceBefore=6, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body", fontName="Helvetica", fontSize=9, leading=12,
            textColor=colors.HexColor("#2C2F33"), spaceAfter=2,
        ),
        "small": ParagraphStyle(
            "small", fontName="Helvetica", fontSize=7.5, leading=9.5,
            textColor=colors.HexColor("#6B7280"), spaceAfter=1,
        ),
        "bullet": ParagraphStyle(
            "bullet", parent=None, fontName="Helvetica", fontSize=8.5, leading=11,
            textColor=colors.HexColor("#2C2F33"), leftIndent=10, spaceAfter=1,
        ),
    }
    return base


def _escape(text: Optional[str]) -> str:
    """HTML-escapet für Paragraph (reportlab nutzt ein eigenes XML-ähnliches Markup)."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Wichtigste Mitglieder (seltenste Rollen, sonst zufällig)
# ─────────────────────────────────────────────────────────────────────────────


def _select_important_members(members: List[discord.Member], guild: discord.Guild) -> List[discord.Member]:
    """
    Wählt die ``MAX_MEMBERS_DETAILED`` wichtigsten Mitglieder aus.

    Wichtigkeit = Summe der Kehrwerte der Rollen-Häufigkeiten (Je seltener eine
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
#  Nachrichten → Text (Embeds, Komponenten, Anhänge inclusive)
# ─────────────────────────────────────────────────────────────────────────────


def _embed_to_text(embed: discord.Embed) -> str:
    parts: List[str] = []
    if embed.title:
        parts.append(f"*Titel:* {_escape(embed.title)}")
    if embed.description:
        parts.append(f"*Inhalt:* {_escape(embed.description)}")
    if embed.color is not None:
        parts.append(f"*Farbe:* #{embed.color.value:06x}")
    for field in embed.fields or []:
        parts.append(
            f"*Feld «{_escape(field.name)}»: {_escape(field.value)}*"
            + (" (eingeklappt)" if field.inline else "")
        )
    if embed.footer and embed.footer.text:
        parts.append(f"*Fußzeile:* {_escape(embed.footer.text)}")
    if embed.timestamp:
        parts.append(f"*Zeitstempel:* {embed.timestamp.isoformat()}")
    return " | ".join(parts) if parts else "(leerer Embed)"


def _components_to_text(components: List[discord.Component]) -> str:
    parts: List[str] = []
    for comp in components:
        ctype = comp.type
        if ctype is discord.ComponentType.action_row:
            for child in comp.children:
                parts.append(_components_to_text([child]))
        elif ctype is discord.ComponentType.button:
            btn: discord.Button = comp
            parts.append(f"[Schaltfläche: {_escape(btn.label or '')} ({btn.style.name})]")
        elif ctype is discord.ComponentType.select_menu:
            sel: discord.SelectMenu = comp
            parts.append(f"[Auswahlmenü: {_escape(sel.placeholder or '')}]")
        elif ctype is discord.ComponentType.text_input:
            ti: discord.TextInput = comp
            parts.append(f"[Text-Eingabe: {_escape(ti.label or '')}]")
        elif ctype is discord.ComponentType.container:
            cont: discord.Container = comp
            for child in cont.children:
                parts.append(_components_to_text([child]))
        else:
            parts.append(f"[Komponente: {ctype.name}]")
    return " · ".join(parts)


def _message_text(message: discord.Message, member_avatars_cache: Dict[int, str]) -> str:
    """Wandelt eine Nachricht in eine lesbare Text-Zeile um."""
    parts: List[str] = []

    # Absender
    if message.webhook_id is not None:
        sender = f"Webhook: {_escape(message.author.name)} (ID {message.webhook_id})"
    else:
        author = message.author
        sender = (
            f"{_escape(author.display_name)} (@{_escape(author.name)}, ID {author.id})"
        )
        avatar_url = getattr(getattr(author, "display_avatar", None), "url", None)
        if avatar_url:
            member_avatars_cache[author.id] = avatar_url

    parts.append(f"*Von:* {sender}")
    parts.append(f"*Zeit:* {message.created_at.strftime('%d.%m.%Y %H:%M:%S')} UTC")
    parts.append(f"*Nachricht-ID:* {message.id}")

    if message.content:
        parts.append(f"*Inhalt:* {_escape(message.content)}")

    for embed in message.embeds or []:
        parts.append(f"*Embed:* {_embed_to_text(embed)}")

    if message.stickers:
        names = ", ".join(_escape(s.name) for s in message.stickers)
        parts.append(f"*Sticker:* {names}")

    if message.attachments:
        names = ", ".join(_escape(a.filename) for a in message.attachments)
        parts.append(f"*Anhänge:* {names}")

    # Komponenten (Buttons, Auswahlmenüs, Container …) in Text umwandeln
    try:
        comps = message.components or []
    except Exception:  # discord.py-Version ohne .components
        comps = []
    if comps:
        parts.append(f"*Komponenten:* {_components_to_text(comps)}")

    return " · ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  Kopf-/Fußzeile der PDF-Seiten
# ─────────────────────────────────────────────────────────────────────────────


def _draw_page(canvas, doc, guild_name: str) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#6B7280"))
    canvas.drawString(MARGIN, PAGE_HEIGHT - 10 * mm, f"Server-Dokument · {_escape(guild_name)}")
    canvas.drawRightString(PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 10 * mm, "AIDiscordServerEinrichten · Adminpanel")
    canvas.setStrokeColor(colors.HexColor("#C4C6CB"))
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, PAGE_HEIGHT - 12 * mm, PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 12 * mm)
    canvas.line(MARGIN, 14 * mm, PAGE_WIDTH - MARGIN, 14 * mm)
    canvas.drawString(MARGIN, 10 * mm, "Erstellt durch das Adminpanel — nur für den Bot-Owner bestimmt.")
    canvas.drawRightString(PAGE_WIDTH - MARGIN, 10 * mm, f"Seite {doc.page}")
    canvas.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
#  Hauptfunktion
# ─────────────────────────────────────────────────────────────────────────────


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
    image_cache: Dict[str, Optional[ImageReader]] = {}

    # ── 1. Basisdaten ──────────────────────────────────────────────────────
    await progress(5, "Server-Informationen werden gesammelt …")

    owner = guild.owner
    if owner is None and guild.owner_id:
        try:
            owner = await guild.fetch_member(guild.owner_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            owner = None

    created_at = guild.created_at.strftime("%d.%m.%Y %H:%M:%S") if guild.created_at else "unbekannt"
    member_count = guild.member_count or 0
    verification_level = str(guild.verification_level) if guild.verification_level else "unbekannt"

    # Icon & Owner-Avatar vorab laden
    icon_url = guild.icon.url if guild.icon else None
    owner_avatar_url = owner.display_avatar.url if owner else None
    await _fetch_images(http_session, [u for u in (icon_url, owner_avatar_url) if u])

    # ── 2. Kanäle & Kategorien ─────────────────────────────────────────────
    await progress(18, "Kanäle und Kategorien werden erfasst …")

    categories = list(guild.categories)
    text_channels = [c for c in guild.text_channels]
    voice_channels = [c for c in guild.voice_channels]
    stage_channels = [c for c in guild.stage_channels]
    forum_channels = [c for c in guild.forums]

    channel_rows: List[List[Any]] = [
        ["Typ", "Name", "ID", "Position", "Kategorie"]
    ]
    all_channels: List[discord.abc.GuildChannel] = []
    for cat in categories:
        all_channels.append(cat)
        channel_rows.append(["Kategorie", _escape(cat.name), str(cat.id), str(cat.position), "—"])
        for ch in cat.channels:
            all_channels.append(ch)
            type_name = {
                discord.ChannelType.text: "Text",
                discord.ChannelType.voice: "Voice",
                discord.ChannelType.stage_voice: "Bühne",
                discord.ChannelType.forum: "Forum",
                discord.ChannelType.news: "News",
                discord.ChannelType.media: "Medien",
            }.get(ch.type, str(ch.type))
            channel_rows.append(
                [type_name, _escape(ch.name), str(ch.id), str(ch.position), cat.name]
            )
    for ch in guild.channels:
        if ch not in all_channels:
            all_channels.append(ch)
            type_name = {
                discord.ChannelType.text: "Text",
                discord.ChannelType.voice: "Voice",
                discord.ChannelType.stage_voice: "Bühne",
                discord.ChannelType.forum: "Forum",
                discord.ChannelType.news: "News",
                discord.ChannelType.media: "Medien",
            }.get(ch.type, str(ch.type))
            channel_rows.append([type_name, _escape(ch.name), str(ch.id), str(ch.position), "—"])

    # ── 3. Rollen ──────────────────────────────────────────────────────────
    await progress(33, "Rollen, Farben und Berechtigungen werden erfasst …")

    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)
    role_member_counts: Dict[int, int] = {
        role.id: sum(1 for m in guild.members if role in m.roles) for role in roles
    }

    role_rows: List[List[Any]] = [
        ["Rolle", "Farbe", "Berechtigungen", "Mitglieder", "ID"]
    ]
    for role in roles:
        perms = ", ".join(
            name.replace("_", " ").title()
            for name, enabled in role.permissions
            if enabled
        ) or "—"
        role_rows.append([
            _escape(role.name),
            f"#{role.color.value:06x}" if role.color.value else "Standard",
            perms,
            str(role_member_counts.get(role.id, 0)),
            str(role.id),
        ])

    # ── 4. Emojis & Sticker ────────────────────────────────────────────────
    await progress(45, "Emojis und Sticker werden erfasst …")

    emojis = list(guild.emojis)
    stickers = list(guild.stickers)

    # Bild-URLs für Emojis/Sticker vorab laden
    emoji_sticker_urls: List[str] = []
    for emoji in emojis:
        emoji_sticker_urls.append(emoji.url)
    for sticker in stickers:
        url = getattr(sticker, "url", None)
        if url:
            emoji_sticker_urls.append(url)
    await _fetch_images(http_session, emoji_sticker_urls)

    # ── 5. Mitglieder ──────────────────────────────────────────────────────
    await progress(58, "Mitglieder werden erfasst …")

    members = list(guild.members)
    if len(members) <= MAX_MEMBERS_DETAILED:
        detailed_members = members
    else:
        detailed_members = _select_important_members(members, guild)

    member_avatar_urls: List[str] = []
    for member in detailed_members:
        member_avatar_urls.append(member.display_avatar.url)
    await _fetch_images(http_session, member_avatar_urls)

    # ── 6. Nachrichten (letzte 10 pro Textkanal) ───────────────────────────
    await progress(70, "Letzte Nachrichten der Textkanäle werden erfasst …")

    channel_messages: Dict[int, List[discord.Message]] = {}
    for channel in text_channels:
        try:
            msgs = [m async for m in channel.history(limit=MAX_MESSAGES_PER_CHANNEL)]
            channel_messages[channel.id] = msgs
            for m in msgs:
                av = getattr(getattr(m.author, "display_avatar", None), "url", None)
                if av:
                    member_avatar_urls.append(av)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            channel_messages[channel.id] = []
    await _fetch_images(http_session, [u for u in member_avatar_urls if u])

    # ── 7. PDF zusammenbauen ───────────────────────────────────────────────
    await progress(82, "PDF-Dokument wird erstellt …")

    flowables: List[Any] = []

    # ---- Titelkopf ----
    title_parts: List[Any] = []
    if icon_url and image_cache.get(icon_url):
        icon_img = Image(image_cache[icon_url], width=22 * mm, height=22 * mm)
        title_parts.append(icon_img)
    title_parts.append(
        Paragraph(
            f"<b>{_escape(guild.name)}</b><br/>"
            f"Server-ID: <code>{guild.id}</code> · Erstellt: {created_at}<br/>"
            f"Mitglieder: {member_count:,} · Verifizierung: {verification_level}",
            styles["body"],
        )
    )
    header_table = Table([[title_parts[0] if len(title_parts) > 1 else "", title_parts[-1]]],
                         colWidths=[26 * mm, CONTENT_WIDTH - 26 * mm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flowables.append(header_table)

    if owner:
        owner_avatar_flow = None
        if owner_avatar_url and image_cache.get(owner_avatar_url):
            owner_avatar_flow = Image(image_cache[owner_avatar_url], width=14 * mm, height=14 * mm)
        owner_row = [
            owner_avatar_flow or "",
            Paragraph(
                f"<b>Owner:</b> {_escape(owner.display_name)} (@{_escape(owner.name)})<br/>"
                f"Owner-ID: <code>{owner.id}</code>",
                styles["body"],
            ),
        ]
        owner_table = Table([owner_row], colWidths=[18 * mm, CONTENT_WIDTH - 18 * mm])
        owner_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        flowables.append(Spacer(1, 4 * mm))
        flowables.append(owner_table)

    flowables.append(Spacer(1, 6 * mm))
    flowables.append(Paragraph("Inhalt dieses Dokuments", styles["h1"]))
    flowables.append(Paragraph(
        "Dieses PDF enthält eine vollständige Übersicht des Servers: Kanäle & Kategorien, "
        "Rollen mit Farben und Berechtigungen, Emojis & Sticker, Mitglieder sowie die letzten "
        "Nachrichten pro Textkanal. Es wurde auf Anfrage des Bot-Owners über das Adminpanel "
        "erstellt und per DM an den Server-Owner versendet. Der Server selbst hat davon "
        "nichts bemerkt.", styles["body"]))
    flowables.append(PageBreak())

    # ---- Kanäle & Kategorien ----
    flowables.append(Paragraph("1. Kanäle & Kategorien", styles["h1"]))
    flowables.append(Paragraph(
        f"Kategorien: {len(categories)} · Textkanäle: {len(text_channels)} · "
        f"Voicekanäle: {len(voice_channels)} · Bühnenkanäle: {len(stage_channels)} · "
        f"Forumkanäle: {len(forum_channels)}", styles["body"]))
    flowables.append(Spacer(1, 3 * mm))
    channel_table = Table(channel_rows, repeatRows=1, colWidths=[20 * mm, 55 * mm, 22 * mm, 16 * mm, CONTENT_WIDTH - 113 * mm])
    channel_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#5865F2")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LEADING", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EDF2FF")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C4C6CB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    flowables.append(channel_table)
    flowables.append(Spacer(1, 4 * mm))
    flowables.append(Paragraph("Channel-IDs", styles["h2"]))
    flowables.append(Paragraph(
        " · ".join(f"<code>{ch.id}</code>" for ch in all_channels) or "—",
        styles["small"],
    ))
    flowables.append(PageBreak())

    # ---- Rollen ----
    flowables.append(Paragraph("2. Rollen", styles["h1"]))
    flowables.append(Paragraph(f"Anzahl Rollen: {len(roles)}", styles["body"]))
    flowables.append(Spacer(1, 3 * mm))
    role_table = Table(role_rows, repeatRows=1,
                       colWidths=[45 * mm, 22 * mm, 60 * mm, 18 * mm, CONTENT_WIDTH - 145 * mm])
    role_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C2F33")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LEADING", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F7F9")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C4C6CB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    flowables.append(role_table)
    flowables.append(Spacer(1, 4 * mm))
    flowables.append(Paragraph("Rollen-IDs (Liste)", styles["h2"]))
    flowables.append(Paragraph(
        " · ".join(f"<code>{role.id}</code>" for role in roles) or "—",
        styles["small"],
    ))
    flowables.append(PageBreak())

    # ---- Emojis & Sticker ----
    flowables.append(Paragraph("3. Emojis & Sticker", styles["h1"]))
    flowables.append(Paragraph(f"Emojis: {len(emojis)} · Sticker: {len(stickers)}", styles["body"]))
    flowables.append(Spacer(1, 3 * mm))

    if emojis:
        flowables.append(Paragraph("Emojis", styles["h2"]))
        emoji_rows: List[List[Any]] = [["Bild", "Name", "ID", "Animiert"]]
        for emoji in emojis[:60]:
            img_flow = ""
            if image_cache.get(emoji.url):
                img_flow = Image(image_cache[emoji.url], width=6 * mm, height=6 * mm)
            emoji_rows.append([
                img_flow,
                _escape(emoji.name),
                str(emoji.id),
                "Ja" if emoji.animated else "Nein",
            ])
        emoji_table = Table(emoji_rows, colWidths=[10 * mm, 40 * mm, 20 * mm, 15 * mm, CONTENT_WIDTH - 85 * mm])
        emoji_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("LEADING", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F7F9")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C4C6CB")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ]))
        flowables.append(emoji_table)
        if len(emojis) > 60:
            flowables.append(Paragraph(f"(und {len(emojis) - 60} weitere Emojis)", styles["small"]))
        flowables.append(Spacer(1, 3 * mm))

    if stickers:
        flowables.append(Paragraph("Sticker", styles["h2"]))
        sticker_rows: List[List[Any]] = [["Bild", "Name", "ID", "Typ"]]
        for sticker in stickers[:60]:
            img_flow = ""
            sticker_url = getattr(sticker, "url", None)
            if sticker_url and image_cache.get(sticker_url):
                img_flow = Image(image_cache[sticker_url], width=6 * mm, height=6 * mm)
            sticker_rows.append([
                img_flow,
                _escape(sticker.name),
                str(sticker.id),
                str(sticker.type).replace("StickerType.", ""),
            ])
        sticker_table = Table(sticker_rows, colWidths=[10 * mm, 40 * mm, 20 * mm, 30 * mm, CONTENT_WIDTH - 100 * mm])
        sticker_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("LEADING", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F7F9")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C4C6CB")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ]))
        flowables.append(sticker_table)
        if len(stickers) > 60:
            flowables.append(Paragraph(f"(und {len(stickers) - 60} weitere Sticker)", styles["small"]))
    if not emojis and not stickers:
        flowables.append(Paragraph("Keine Emojis oder Sticker vorhanden.", styles["body"]))
    flowables.append(PageBreak())

    # ---- Mitglieder ----
    flowables.append(Paragraph("4. Mitglieder", styles["h1"]))
    if len(members) <= MAX_MEMBERS_DETAILED:
        flowables.append(Paragraph(
            f"Alle {len(members)} Mitglieder des Servers:", styles["body"]))
    else:
        flowables.append(Paragraph(
            f"Der Server hat {len(members)} Mitglieder (mehr als {MAX_MEMBERS_DETAILED}). "
            f"Es werden die {MAX_MEMBERS_DETAILED} wichtigsten Mitglieder angezeigt — sortiert nach "
            "Wichtigkeit (Mitglieder mit den seltensten Rollen zuerst, danach zufällig).",
            styles["body"]))
    flowables.append(Spacer(1, 3 * mm))

    member_rows: List[List[Any]] = [["Avatar", "Anzeigename", "Benutzername", "ID", "Rollen", "Beigetreten"]]
    for member in detailed_members:
        img_flow = ""
        if image_cache.get(member.display_avatar.url):
            img_flow = Image(image_cache[member.display_avatar.url], width=7 * mm, height=7 * mm)
        role_names = ", ".join(
            _escape(r.name) for r in member.roles if r != guild.default_role
        ) or "—"
        joined = member.joined_at.strftime("%d.%m.%Y") if member.joined_at else "unbekannt"
        member_rows.append([
            img_flow,
            _escape(member.display_name),
            _escape(member.name),
            str(member.id),
            role_names,
            joined,
        ])
    member_table = Table(member_rows, repeatRows=1,
                         colWidths=[11 * mm, 30 * mm, 28 * mm, 20 * mm, 50 * mm, CONTENT_WIDTH - 139 * mm])
    member_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LEADING", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F7F9")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C4C6CB")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    flowables.append(member_table)
    flowables.append(PageBreak())

    # ---- Nachrichten ----
    flowables.append(Paragraph("5. Letzte Nachrichten pro Textkanal", styles["h1"]))
    flowables.append(Paragraph(
        f"Bis zu {MAX_MESSAGES_PER_CHANNEL} letzte Nachrichten jeder Textkanäle, für die der "
        "Bot die Historie lesen darf. Embeds, Sticker, Anhänge und Komponenten (Schaltflächen, "
        "Auswahlmenüs, Container) werden in Text umgewandelt.", styles["body"]))
    flowables.append(Spacer(1, 3 * mm))

    any_messages = False
    for channel in text_channels:
        msgs = channel_messages.get(channel.id, [])
        flowables.append(Paragraph(f"#{_escape(channel.name)} ({channel.id})", styles["h2"]))
        if not msgs:
            flowables.append(Paragraph("Keine Nachrichten verfügbar (keine Historie oder keine Rechte).", styles["small"]))
        else:
            any_messages = True
            for m in msgs:
                # Absender-Avatar (klein daneben)
                sender_avatar_flow = ""
                av_url = getattr(getattr(m.author, "display_avatar", None), "url", None)
                if av_url and image_cache.get(av_url):
                    sender_avatar_flow = Image(image_cache[av_url], width=5 * mm, height=5 * mm)
                text = _message_text(m, {})
                row = Table(
                    [[sender_avatar_flow, Paragraph(text, styles["small"])]],
                    colWidths=[8 * mm, CONTENT_WIDTH - 8 * mm],
                )
                row.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#E5E7EB")),
                ]))
                flowables.append(row)
                flowables.append(Spacer(1, 1.5 * mm))
        flowables.append(Spacer(1, 2 * mm))

    if not any_messages:
        flowables.append(Paragraph("Keine Nachrichten konnten aus den Textkanälen geladen werden.", styles["body"]))

    # ---- Bauen ----
    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Server-Dokument · {guild.name}",
        author="AIDiscordServerEinrichten",
        subject=f"Komplette Server-Übersicht für {guild.name} (ID {guild.id})",
    )
    frame = Frame(MARGIN, 18 * mm, CONTENT_WIDTH, PAGE_HEIGHT - 36 * mm, id="main")
    doc.addPageTemplates([
        PageTemplate(id="page", frames=[frame],
                     onPage=lambda c, d: _draw_page(c, d, guild.name)),
    ])
    doc.build(flowables)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    await progress(100, "PDF fertiggestellt.")
    log.info(
        "Server-Dokument für '%s' (ID %s) erstellt: %d KB, %d Kanäle, %d Rollen, %d Emojis, %d Sticker, %d Mitglieder, %d Kanäle mit Nachrichten",
        guild.name, guild.id, len(pdf_bytes) // 1024,
        len(all_channels), len(roles), len(emojis), len(stickers),
        len(detailed_members), sum(1 for v in channel_messages.values() if v),
    )
    return pdf_bytes
