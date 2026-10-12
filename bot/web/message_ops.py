"""
Nachrichten bauen: Embeds, Buttons, Anhänge, Umfragen, Mentions.

Wird von ``routes/messages.py`` **und** vom Setup-Wizard genutzt, damit eine
Nachricht überall gleich aussieht.

Unterstützt wird das volle Discord-Feature-Set, das eine KI sinnvoll per JSON
beschreiben kann: ``content``, ``embeds``, ``components`` (Buttons),
``attachments`` (per URL), ``poll``, ``allowed_mentions``, ``reply_to``,
``tts``, ``silent``, ``stickers``, ``suppress_embeds``, ``delete_after``.
"""

from __future__ import annotations

import datetime as _dt
import io
import os
import re

import aiohttp
from typing import Any, Dict, List, Mapping, Optional, Tuple

import discord

from ..util import ApiError, as_id, parse_bool, parse_int, parse_str
from .context import guard

__all__ = ("build_message_kwargs", "build_embeds", "build_view", "build_poll", "download_attachments")

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\-\u00C0-\uFFFF]+")


def build_embeds(raw: Any, *, field: str = "embeds", limit: int = 10) -> List[discord.Embed]:
    from ..serializers import embed_from_dict

    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        raise ApiError.bad_request(f"{field} muss eine Liste von Embed-Objekten sein.")
    if len(raw) > limit:
        raise ApiError.bad_request(f"{field}: Discord erlaubt maximal {limit} Embeds pro Nachricht.")

    embeds: List[discord.Embed] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, discord.Embed):
            embeds.append(entry)
            continue
        if not isinstance(entry, Mapping):
            raise ApiError.bad_request(f"{field}[{index}]: erwartet ein Objekt.")
        try:
            embeds.append(embed_from_dict(entry))
        except ApiError as exc:
            raise ApiError.bad_request(f"{field}[{index}]: {exc.message}", hint=exc.hint,
                                       code=exc.code) from exc
        except Exception as exc:  # noqa: BLE001
            raise ApiError.bad_request(f"{field}[{index}]: {exc}") from exc

    if not embeds:
        return []
    total = sum(len(e.title or "") + len(e.description or "") for e in embeds)
    if total > 6000:
        raise ApiError.bad_request(
            f"{field}: insgesamt {total} Zeichen — Discord erlaubt maximal 6000 pro Nachricht.",
            code="EMBED_TOO_LONG",
        )
    return embeds


def _button_emoji(guild: Optional[discord.Guild], value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, Mapping):
        name = value.get("name")
        emoji_id = value.get("id")
        if emoji_id:
            return discord.PartialEmoji(name=str(name or "emoji"), id=as_id(emoji_id, field="emoji.id"),
                                        animated=bool(value.get("animated")))
        return str(name or "")
    text = str(value).strip()
    match = re.match(r"<a?:(?P<name>[^:>]+):(?P<id>\d{15,25})>", text)
    if match:
        emoji_id = int(match.group("id"))
        if guild is not None:
            found = guild.get_emoji(emoji_id)
            if found:
                return found
        return discord.PartialEmoji(name=match.group("name"), id=emoji_id,
                                    animated=text.startswith("<a:"))
    if guild is not None and text.startswith(":") and text.endswith(":"):
        name = text[1:-1].lower()
        for emoji in guild.emojis:
            if emoji.name and emoji.name.lower() == name:
                return emoji
        raise ApiError.not_found(f"Server-Emoji '{text}' nicht gefunden.", code="EMOJI_NOT_FOUND")
    if guild is not None and text.isdigit():
        found = guild.get_emoji(int(text))
        if found:
            return found
        raise ApiError.not_found(f"Emoji {text} nicht gefunden.", code="EMOJI_NOT_FOUND")
    return text


def build_view(raw: Any, *, guild: Optional[discord.Guild] = None,
               field: str = "components") -> Optional[discord.ui.LayoutView]:
    """
    Übersetzt ``components`` in eine ``LayoutView``.

    Unterstützt **Buttons** in allen Stilen. Andere Komponenten (Select-Menüs,
    TextInputs) brauchen einen Interaktions-Handler und sind für einen reinen
    Relay-Bot sinnlos — dafür gibt es eine klare Fehlermeldung statt eines
    stillen Ignorierens.
    """
    if raw is None:
        return None
    if isinstance(raw, discord.ui.LayoutView):
        return raw
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        raise ApiError.bad_request(f"{field} muss eine Liste von Action-Rows sein.")

    view = discord.ui.LayoutView()
    created_any = False

    for row_index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise ApiError.bad_request(f"{field}[{row_index}]: erwartet ein Objekt (Action Row).")
        items = row.get("components")
        if items is None and any(key in row for key in ("label", "url", "style", "emoji", "custom_id")):
            items = [row]
        if not isinstance(items, list):
            raise ApiError.bad_request(f"{field}[{row_index}].components muss eine Liste sein.")
        if len(items) > 5:
            raise ApiError.bad_request(f"{field}[{row_index}]: maximal 5 Buttons pro Zeile.")

        buttons: List[discord.ui.Button] = []
        for item_index, item in enumerate(items):
            where = f"{field}[{row_index}].components[{item_index}]"
            if not isinstance(item, Mapping):
                raise ApiError.bad_request(f"{where}: erwartet ein Objekt.")

            item_type = item.get("type")
            if item_type not in (None, 2, "button", "2"):
                raise ApiError.bad_request(
                    f"{where}: Komponententyp '{item_type}' wird nicht unterstützt.",
                    hint="Unterstützt werden Buttons (type 2). Für Auswahlmenüs bitte "
                         "die Discord-Oberfläche nutzen — Menüs brauchen einen "
                         "Interaktions-Handler, den ein Relay-Bot nicht anbietet.",
                    code="COMPONENT_UNSUPPORTED",
                )

            style_raw = item.get("style", "link" if item.get("url") else "primary")
            if isinstance(style_raw, int):
                style = discord.ButtonStyle(style_raw)
            else:
                text = str(style_raw).strip().lower()
                aliases = {
                    "primary": "primary", "blurple": "primary", "1": "primary",
                    "secondary": "secondary", "grey": "secondary", "gray": "secondary", "2": "secondary",
                    "success": "success", "green": "success", "grün": "success", "3": "success",
                    "danger": "danger", "red": "danger", "rot": "danger", "4": "danger",
                    "link": "link", "url": "link", "5": "link",
                    "premium": "premium", "6": "premium",
                }
                if text not in aliases:
                    raise ApiError.bad_request(
                        f"{where}.style '{style_raw}' ist unbekannt.",
                        hint="Erlaubt: primary, secondary, success, danger, link, premium.",
                    )
                style = getattr(discord.ButtonStyle, aliases[text])

            url = parse_str(item.get("url"), field=f"{where}.url", max_length=512)
            label = parse_str(item.get("label"), field=f"{where}.label", max_length=80)
            custom_id = parse_str(item.get("custom_id"), field=f"{where}.custom_id", max_length=100)

            if style is discord.ButtonStyle.link:
                if not url:
                    raise ApiError.bad_request(
                        f"{where}: Ein Link-Button braucht 'url'.",
                        code="BUTTON_URL_MISSING",
                    )
                custom_id = None
            else:
                url = None
                if custom_id:
                    raise ApiError.bad_request(
                        f"{where}: Buttons mit 'custom_id' brauchen einen Interaktions-Handler, "
                        "den dieser Relay-Bot nicht bereitstellt.",
                        hint="Nutze style 'link' mit einer 'url' — das ist der einzige "
                             "Button-Typ, der ohne Handler sinnvoll ist.",
                        code="BUTTON_CUSTOM_ID_UNSUPPORTED",
                    )
                custom_id = None

            if not label and not item.get("emoji"):
                raise ApiError.bad_request(f"{where}: Button braucht 'label' und/oder 'emoji'.")

            buttons.append(
                discord.ui.Button(
                    style=style,
                    label=label or None,
                    url=url,
                    emoji=_button_emoji(guild, item.get("emoji")),
                    disabled=bool(parse_bool(item.get("disabled"), field=f"{where}.disabled",
                                             default=False)),
                    custom_id=custom_id,
                )
            )
        if buttons:
            view.add_item(discord.ui.ActionRow(*buttons))
            created_any = True

    return view if created_any else None


def build_poll(raw: Any, *, guild: Optional[discord.Guild] = None,
               field: str = "poll") -> Optional[discord.Poll]:
    """``{"question": "…", "answers": ["A","B"], "duration_hours": 24, "multiple": false}``"""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ApiError.bad_request(f"{field} muss ein Objekt sein.")

    question = parse_str(raw.get("question") or raw.get("title"), field=f"{field}.question",
                         max_length=300, allow_empty=False)
    if not question:
        raise ApiError.bad_request(f"{field}.question fehlt.")

    answers = raw.get("answers") or raw.get("options")
    if not isinstance(answers, list) or len(answers) < 2:
        raise ApiError.bad_request(
            f"{field}.answers braucht mindestens 2 Antworten.",
            hint='Beispiel: {"question": "Wann?", "answers": ["Freitag", "Samstag"]}',
        )
    if len(answers) > 10:
        raise ApiError.bad_request(f"{field}.answers: Discord erlaubt maximal 10 Antworten.")

    hours = parse_int(raw.get("duration_hours", raw.get("duration")), field=f"{field}.duration_hours",
                      default=24, minimum=1, maximum=168) or 24
    poll = discord.Poll(
        question=question,
        duration=_dt.timedelta(hours=hours),
        multiple=bool(parse_bool(raw.get("multiple", raw.get("allow_multiselect")),
                                 field=f"{field}.multiple", default=False)),
    )
    for index, answer in enumerate(answers):
        if isinstance(answer, Mapping):
            text = parse_str(answer.get("text") or answer.get("label"),
                             field=f"{field}.answers[{index}]", max_length=55, allow_empty=False)
            emoji = _button_emoji(guild, answer.get("emoji"))
        else:
            text = parse_str(answer, field=f"{field}.answers[{index}]", max_length=55, allow_empty=False)
            emoji = None
        if not text:
            raise ApiError.bad_request(f"{field}.answers[{index}] darf nicht leer sein.")
        poll.add_answer(text=text, emoji=emoji)
    return poll


def build_allowed_mentions(raw: Any, *, field: str = "allowed_mentions") -> Optional[discord.AllowedMentions]:
    if raw is None:
        return None
    if isinstance(raw, discord.AllowedMentions):
        return raw
    if not isinstance(raw, Mapping):
        raise ApiError.bad_request(f"{field} muss ein Objekt sein.")

    parse_list = raw.get("parse")
    kwargs: Dict[str, Any] = {}
    if isinstance(parse_list, list):
        for item in parse_list:
            key = str(item).strip().lower()
            if key in {"everyone", "all", "alle"}:
                kwargs["everyone"] = True
            elif key in {"users", "user", "nutzer"}:
                kwargs["users"] = True
            elif key in {"roles", "role", "rollen"}:
                kwargs["roles"] = True
            elif key in {"none", "keine"}:
                kwargs.update({"everyone": False, "users": False, "roles": False})
            else:
                raise ApiError.bad_request(
                    f"{field}.parse: '{item}' unbekannt.",
                    hint="Erlaubt: 'everyone', 'users', 'roles', 'none'.",
                )
    for key, target in (("users", "users"), ("roles", "roles")):
        if key in raw:
            value = raw[key]
            if isinstance(value, bool):
                kwargs[target] = value
            elif isinstance(value, list):
                kwargs[target] = [discord.Object(id=as_id(v, field=f"{field}.{key}")) for v in value]
            else:
                raise ApiError.bad_request(f"{field}.{key} muss bool oder Liste von IDs sein.")
    if "everyone" in raw:
        kwargs["everyone"] = bool(parse_bool(raw["everyone"], field=f"{field}.everyone"))
    if "replied_user" in raw:
        kwargs["replied_user"] = bool(parse_bool(raw["replied_user"], field=f"{field}.replied_user"))
    if not kwargs:
        return None
    return discord.AllowedMentions(**kwargs)


async def download_attachments(
    raw: Any, *, session, field: str = "attachments", max_files: int = 10,
    max_bytes: int = 25 * 1024 * 1024,
) -> List[discord.File]:
    """Lädt Anhänge von URLs und verpackt sie als ``discord.File``."""
    if not raw:
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        raise ApiError.bad_request(f"{field} muss eine Liste sein.")
    if len(raw) > max_files:
        raise ApiError.bad_request(f"{field}: maximal {max_files} Dateien pro Nachricht.")

    files: List[discord.File] = []
    for index, entry in enumerate(raw):
        where = f"{field}[{index}]"
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, Mapping):
            raise ApiError.bad_request(f"{where}: erwartet Objekt oder URL-String.")
        url = parse_str(entry.get("url"), field=f"{where}.url", allow_empty=False)
        if not url or not re.match(r"^https?://", url):
            raise ApiError.bad_request(f"{where}.url muss eine http(s)-URL sein.")

        filename = entry.get("filename") or os.path.basename(url.split("?")[0]) or f"datei-{index}.bin"
        filename = _FILENAME_SAFE.sub("_", str(filename))[:100] or f"datei-{index}.bin"

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    raise ApiError.bad_request(
                        f"{where}: {url} antwortet mit HTTP {response.status}.",
                        code="ATTACHMENT_FETCH_FAILED",
                    )
                payload = await response.read()
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiError.bad_request(
                f"{where}: Download fehlgeschlagen ({exc.__class__.__name__}: {exc}).",
                code="ATTACHMENT_FETCH_FAILED",
            ) from exc

        if not payload:
            raise ApiError.bad_request(f"{where}: leere Datei.")
        if len(payload) > max_bytes:
            raise ApiError.bad_request(
                f"{where}: Datei ist {len(payload) / 1024 / 1024:.1f} MiB groß — "
                f"Discord-Limit {max_bytes / 1024 / 1024:.0f} MiB.",
                code="ATTACHMENT_TOO_LARGE",
            )
        files.append(
            discord.File(
                io.BytesIO(payload),
                filename=filename,
                spoiler=bool(parse_bool(entry.get("spoiler"), field=f"{where}.spoiler", default=False)),
                description=parse_str(entry.get("description"), field=f"{where}.description",
                                      max_length=1024),
            )
        )
    return files


async def build_message_kwargs(
    data: Mapping[str, Any], *, ctx, channel: Any
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Übersetzt ein JSON-Objekt in ``channel.send(**kwargs)``-Argumente.

    Validiert zusätzlich die Discord-Limits (2000 Zeichen Content, 10 Embeds,
    6000 Embed-Zeichen, 10 Anhänge), **bevor** der Request rausgeht.
    """
    guild = getattr(channel, "guild", None)
    kwargs: Dict[str, Any] = {}
    notes: List[str] = []

    content = data.get("content")
    if content is not None:
        text = parse_str(content, field="content", max_length=4000)
        if text is not None and len(text) > 2000:
            raise ApiError.bad_request(
                f"content hat {len(text)} Zeichen — Discord erlaubt maximal 2000 "
                "(4000 mit Nitro, was der Bot nicht garantiert).",
                hint="Längeren Text auf mehrere Nachrichten oder Embeds aufteilen.",
                code="CONTENT_TOO_LONG",
            )
        kwargs["content"] = text

    if "tts" in data:
        kwargs["tts"] = bool(parse_bool(data["tts"], field="tts", default=False))

    embeds = build_embeds(data.get("embeds", data.get("embed")))
    if embeds:
        kwargs["embeds"] = embeds

    if "poll" in data:
        poll = build_poll(data["poll"], guild=guild)
        if poll is not None:
            kwargs["poll"] = poll
            notes.append("Umfragen kann nur der Bot selbst beenden/auswerten.")

    view = build_view(data.get("components"), guild=guild)
    if view is not None:
        kwargs["view"] = view

    files = await download_attachments(data.get("attachments"), session=ctx.http_session())
    if files:
        kwargs["files"] = files

    if data.get("stickers"):
        raw = data["stickers"]
        if isinstance(raw, (str, int)):
            raw = [raw]
        stickers = []
        for item in raw:
            sticker_id = as_id(item, field="stickers")
            found = None
            if guild is not None:
                found = guild.get_sticker(sticker_id)
            if found is None:
                try:
                    found = await guard(ctx.client.fetch_sticker(sticker_id), action="Sticker laden")
                except ApiError:
                    raise ApiError.not_found(
                        f"Sticker {sticker_id} nicht gefunden.",
                        hint="GET /api/v1/stickers listet die verfügbaren Sticker.",
                        code="STICKER_NOT_FOUND",
                    ) from None
            stickers.append(found)
        kwargs["stickers"] = stickers[:3]

    if data.get("allowed_mentions") is not None:
        kwargs["allowed_mentions"] = build_allowed_mentions(data["allowed_mentions"])

    reply_to = data.get("reply_to", data.get("reference", data.get("message_id")))
    if reply_to:
        message_id = as_id(reply_to, field="reply_to")
        kwargs["reference"] = channel.get_partial_message(message_id) if hasattr(
            channel, "get_partial_message") else discord.PartialMessageable(
            state=ctx.client._state, id=message_id, guild_id=guild.id if guild else None  # noqa: SLF001
        )
        kwargs["mention_author"] = bool(parse_bool(
            data.get("mention_reply", data.get("mention_author")),
            field="mention_reply", default=False,
        ))

    if "suppress_embeds" in data:
        kwargs["suppress_embeds"] = bool(parse_bool(data["suppress_embeds"],
                                                    field="suppress_embeds", default=False))
    if "silent" in data:
        kwargs["silent"] = bool(parse_bool(data["silent"], field="silent", default=False))

    delete_after = data.get("delete_after")
    if delete_after is not None:
        seconds = parse_int(delete_after, field="delete_after", minimum=1, maximum=86400)
        if seconds:
            kwargs["delete_after"] = seconds
            notes.append(f"Nachricht löscht sich nach {seconds}s selbst.")

    if not kwargs.get("content") and not kwargs.get("embeds") and not kwargs.get("files") \
            and not kwargs.get("stickers") and not kwargs.get("poll"):
        raise ApiError.bad_request(
            "Die Nachricht wäre leer.",
            hint="Mindestens eines angeben: content, embeds, attachments, stickers oder poll.",
            code="MESSAGE_EMPTY",
        )
    return kwargs, notes
