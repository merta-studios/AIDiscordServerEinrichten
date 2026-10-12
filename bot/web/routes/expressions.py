"""Emojis und Sticker."""

from __future__ import annotations

import io
from typing import Any, Dict, List

import discord

from ...serializers import serialize_emoji, serialize_sticker
from ...util import ApiError, parse_str, resolve_role, sf
from ..context import Ctx, guard
from ..images import resolve_image
from ..registry import route

EMOJI_MAX_BYTES = 256 * 1024  # Discord-Limit für Custom-Emojis


@route(
    "GET", "/api/v1/emojis", scope="read", tags=("expressions",),
    summary="Alle Server-Emojis",
    query={"available_only": "true — nur verfügbare (Boost-Limit nicht überschritten)"},
)
async def list_emojis(ctx: Ctx) -> Dict[str, Any]:
    only_available = ctx.q_bool("available_only", False)
    emojis = list(ctx.guild.emojis)
    if only_available:
        emojis = [e for e in emojis if e.available]
    return {
        "count": len(emojis),
        "limit": 50 + 50,
        "note": "Discord erlaubt 50 statische + 50 animierte Emojis pro Server "
                "(mehr durch Server-Boosts).",
        "emojis": [serialize_emoji(e) for e in emojis],
    }


@route(
    "POST", "/api/v1/emojis", scope="manage", tags=("expressions",),
    summary="Server-Emoji hochladen",
    body={
        "name": "str (Pflicht, 2–32 Zeichen, nur a-z 0-9 _)",
        "image": "URL oder data:image/png;base64,… (Pflicht, max. 256 KB, min. 128×128)",
        "roles": '["Rollen-ID oder Name"] — optional, schränkt die Nutzung ein',
        "reason": "str",
    },
)
async def create_emoji(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    name = parse_str(data.get("name"), field="name", min_length=2, max_length=32, allow_empty=False)
    if not name:
        raise ApiError.bad_request(
            "'name' fehlt (2–32 Zeichen).",
            hint="Discord erlaubt nur Kleinbuchstaben, Ziffern und Unterstriche.",
        )
    if data.get("image") is None:
        raise ApiError.bad_request(
            "'image' fehlt.",
            hint='Beispiel: {"name": "logo", "image": "https://example.com/logo.png"}',
        )
    image = await resolve_image(data["image"], session=ctx.http_session(), field="image",
                                max_bytes=EMOJI_MAX_BYTES)
    if image is None:
        raise ApiError.bad_request("'image' darf hier nicht null sein.")

    roles: List[discord.Role] = [resolve_role(ctx.guild, item) for item in (data.get("roles") or [])]
    emoji = await guard(
        ctx.guild.create_custom_emoji(
            name=name, image=image, roles=roles,
            reason=ctx.reason(data, default=f"Emoji :{name}: hochgeladen (Arena AI)"),
        ),
        action=f"Emoji :{name}: hochladen",
        hint="Häufige Ursachen: Bild > 256 KB, Emoji-Slot voll (50/50), oder der Name "
             "enthält unerlaubte Zeichen.",
    )
    await ctx.settle(0.5)
    return {"created": serialize_emoji(emoji), "id": sf(emoji.id), "usage": f"<:{emoji.name}:{emoji.id}>"}


@route(
    "PATCH", "/api/v1/emojis/{emoji_id}", scope="manage", tags=("expressions",),
    summary="Emoji umbenennen oder Rollen-Zugriff ändern",
    body={"name": "str", "roles": '["Rollen-ID"] — leere Liste = alle dürfen es nutzen',
          "reason": "str"},
)
async def patch_emoji(ctx: Ctx) -> Dict[str, Any]:
    emoji_id = ctx.path_id("emoji_id")
    emoji = ctx.guild.get_emoji(emoji_id)
    if emoji is None:
        raise ApiError.not_found(f"Emoji {emoji_id} nicht gefunden.",
                                 hint="GET /api/v1/emojis zeigt alle.", code="EMOJI_NOT_FOUND")
    data = await ctx.body()
    kwargs: Dict[str, Any] = {}
    if "name" in data:
        kwargs["name"] = parse_str(data["name"], field="name", min_length=2, max_length=32,
                                   allow_empty=False) or emoji.name
    if "roles" in data:
        kwargs["roles"] = [resolve_role(ctx.guild, item) for item in (data["roles"] or [])]
    if not kwargs:
        raise ApiError.bad_request("Nichts zu tun: 'name' und/oder 'roles' angeben.", code="NO_CHANGES")
    kwargs["reason"] = ctx.reason(data, default=f"Emoji :{emoji.name}: aktualisiert (Arena AI)")
    updated = await guard(emoji.edit(**kwargs), action=f"Emoji :{emoji.name}: bearbeiten")
    await ctx.settle(0.4)
    return {"emoji": serialize_emoji(updated or emoji), "id": sf(emoji_id)}


@route(
    "DELETE", "/api/v1/emojis/{emoji_id}", scope="manage", tags=("expressions",),
    summary="Emoji löschen",
    query={"reason": "str"},
)
async def delete_emoji(ctx: Ctx) -> Dict[str, Any]:
    emoji_id = ctx.path_id("emoji_id")
    emoji = ctx.guild.get_emoji(emoji_id)
    if emoji is None:
        raise ApiError.not_found(f"Emoji {emoji_id} nicht gefunden.", code="EMOJI_NOT_FOUND")
    await guard(emoji.delete(reason=ctx.reason(default=f"Emoji :{emoji.name}: gelöscht (Arena AI)")),
                action=f"Emoji :{emoji.name}: löschen")
    await ctx.settle(0.4)
    return {"deleted": {"id": sf(emoji_id), "name": emoji.name}}


@route(
    "GET", "/api/v1/stickers", scope="read", tags=("expressions",),
    summary="Alle Server-Sticker",
)
async def list_stickers(ctx: Ctx) -> Dict[str, Any]:
    stickers = await guard(ctx.guild.fetch_stickers(), action="Sticker laden")
    return {"count": len(stickers), "stickers": [serialize_sticker(s) for s in stickers]}


@route(
    "POST", "/api/v1/stickers", scope="manage", tags=("expressions",),
    summary="Server-Sticker hochladen",
    body={
        "name": "str (Pflicht, 2–30)",
        "description": "str (Pflicht, 2–100)",
        "emoji": "zugeordnetes Unicode-Emoji (Pflicht, z. B. '😀')",
        "file": "URL oder Data-URI — PNG/APNG/Lottie/GIF, max. 512 KB, genau 320×320",
        "reason": "str",
    },
)
async def create_sticker(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    name = parse_str(data.get("name"), field="name", min_length=2, max_length=30, allow_empty=False)
    description = parse_str(data.get("description"), field="description", min_length=2,
                            max_length=100, allow_empty=False)
    emoji = parse_str(data.get("emoji"), field="emoji", max_length=200, allow_empty=False)
    if not (name and description and emoji):
        raise ApiError.bad_request("'name', 'description' und 'emoji' sind alle Pflicht.")
    payload = await resolve_image(data.get("file") or data.get("image"), session=ctx.http_session(),
                                  field="file", max_bytes=512 * 1024)
    if payload is None:
        raise ApiError.bad_request("'file' fehlt oder ist null.")

    sticker = await guard(
        ctx.guild.create_sticker(
            name=name, description=description, emoji=emoji,
            file=discord.File(io.BytesIO(payload), filename=f"{name[:20]}.png"),
            reason=ctx.reason(data, default=f"Sticker '{name}' hochgeladen (Arena AI)"),
        ),
        action=f"Sticker '{name}' hochladen",
        hint="Discord verlangt exakt 320×320 Pixel, PNG/APNG/GIF/Lottie und max. 512 KB.",
    )
    await ctx.settle(0.5)
    return {"created": serialize_sticker(sticker), "id": sf(sticker.id)}


@route(
    "GET", "/api/v1/stickers/{sticker_id}", scope="read", tags=("expressions",),
    summary="Einzelnen Sticker abrufen",
)
async def get_sticker(ctx: Ctx) -> Dict[str, Any]:
    sticker_id = ctx.path_id("sticker_id")
    sticker = next((s for s in ctx.guild.stickers if s.id == sticker_id), None)
    if sticker is None:
        try:
            sticker = await guard(ctx.guild.fetch_sticker(sticker_id), action="Sticker laden")
        except ApiError:
            sticker = None
    if sticker is None:
        raise ApiError.not_found(
            f"Sticker {sticker_id} nicht gefunden.",
            hint="GET /api/v1/stickers listet alle Server-Sticker.",
            code="STICKER_NOT_FOUND",
        )
    return serialize_sticker(sticker) or {}


@route(
    "PATCH", "/api/v1/stickers/{sticker_id}", scope="manage", tags=("expressions",),
    summary="Sticker bearbeiten (Name, Beschreibung, Emoji)",
    body={
        "name": "str (2–30)",
        "description": "str (2–100)",
        "emoji": "zugeordnetes Emoji (Unicode)",
        "reason": "str",
    },
)
async def patch_sticker(ctx: Ctx) -> Dict[str, Any]:
    sticker_id = ctx.path_id("sticker_id")
    sticker = next((s for s in ctx.guild.stickers if s.id == sticker_id), None)
    if sticker is None:
        try:
            sticker = await guard(ctx.guild.fetch_sticker(sticker_id), action="Sticker laden")
        except ApiError:
            sticker = None
    if sticker is None:
        raise ApiError.not_found(
            f"Sticker {sticker_id} nicht gefunden.",
            code="STICKER_NOT_FOUND",
        )
    data = await ctx.body()
    kwargs: Dict[str, Any] = {}
    if "name" in data:
        kwargs["name"] = parse_str(data["name"], field="name", min_length=2, max_length=30, allow_empty=False)
    if "description" in data:
        kwargs["description"] = parse_str(data["description"], field="description", min_length=2, max_length=100, allow_empty=False)
    if "emoji" in data:
        kwargs["emoji"] = parse_str(data["emoji"], field="emoji", max_length=200, allow_empty=False)
    if not kwargs:
        raise ApiError.bad_request("Nichts zu tun: 'name', 'description' und/oder 'emoji' angeben.", code="NO_CHANGES")
    kwargs["reason"] = ctx.reason(data, default=f"Sticker '{sticker.name}' bearbeitet (Arena AI)")
    updated = await guard(sticker.edit(**kwargs), action=f"Sticker '{sticker.name}' bearbeiten")
    await ctx.settle(0.4)
    return {"sticker": serialize_sticker(updated or sticker), "id": sf(sticker_id)}


@route(
    "DELETE", "/api/v1/stickers/{sticker_id}", scope="manage", tags=("expressions",),
    summary="Sticker löschen",
    query={"reason": "str"},
)
async def delete_sticker(ctx: Ctx) -> Dict[str, Any]:
    sticker_id = ctx.path_id("sticker_id")
    sticker = next((s for s in ctx.guild.stickers if s.id == sticker_id), None)
    if sticker is None:
        raise ApiError.not_found(
            f"Sticker {sticker_id} nicht gefunden.",
            hint="GET /api/v1/stickers listet alle Server-Sticker.",
            code="STICKER_NOT_FOUND",
        )
    await guard(sticker.delete(reason=ctx.reason(default="Sticker gelöscht (Arena AI)")),
                action="Sticker löschen")
    await ctx.settle(0.4)
    return {"deleted": {"id": sf(sticker_id), "name": getattr(sticker, "name", None)}}

