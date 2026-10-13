"""
Simulierte Discord-Objekte für ``scripts/smoke_test.py``.

Warum echte Unterklassen von ``discord.Role``/``discord.TextChannel``/…?

Die API prüft an mehreren Stellen mit ``isinstance`` gegen discord.py-Typen —
völlig zu Recht, denn im Betrieb kommen genau diese Objekte zurück. Würde der
Test eigene, nur ähnlich aussehende Attrappen verwenden, liefen alle diese
Prüfungen ins Leere und der Test wäre wertlos. Also: echte Klassen, ohne
``__init__`` aufgerufen zu haben (der bräuchte einen ``ConnectionState``).

Gelesene Properties (``type``, ``mention``, ``created_at``, ``permissions``,
``colour``) liefern dadurch **echtes** discord.py-Verhalten; nur die wenigen
schreibbaren werden durch setzbare Varianten überlagert.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord

__all__ = (
    "FakeAsset", "FakeRole", "FakeCategory", "FakeTextChannel", "FakeVoiceChannel",
    "FakeStageChannel", "FakeForumChannel", "FakeMember", "FakeUser", "FakeGuild",
    "FakeClient", "FakeHTTP", "FakeMessage", "FakeInvite", "FakeAutoModRule",
    "FakeWebhook", "MutableGuild", "GUILD_ID", "BOT_ID", "OWNER_ID", "USER_ID",
    "fake_response", "invalid_form_body", "is_components_v2_view",
    "validate_message_payload", "INVALID_FORM_BODY_CODE",
)

GUILD_ID = 111111111111111111
BOT_ID = 222222222222222222
OWNER_ID = 333333333333333333
USER_ID = 444444444444444444
CATEGORY_ID = 555555555555555555
TEXT_ID = 666666666666666666
VOICE_ID = 777777777777777777
ROLE_MOD = 999999999999999999

EVERYONE_DEFAULTS = (
    "view_channel", "send_messages", "send_messages_in_threads", "create_public_threads",
    "read_message_history", "add_reactions", "embed_links", "attach_files",
    "use_external_emojis", "use_external_stickers", "change_nickname",
    "create_instant_invite", "connect", "speak", "use_voice_activation", "stream",
    "send_tts_messages", "use_application_commands", "use_embedded_activities",
    "view_guild_insights",
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def everyone_permissions() -> discord.Permissions:
    """Discords Standardrechte für ``@everyone`` (discord.py kennt kein ``Permissions.default()``)."""
    perms = discord.Permissions.none()
    for name in EVERYONE_DEFAULTS:
        if hasattr(perms, name):          # je nach discord.py-Version unterschiedlich
            setattr(perms, name, True)
    return perms


def fake_response(status: int = 404, reason: str = "Not Found") -> Any:
    """Attrappe für ``aiohttp.ClientResponse``, wie discord.py sie in Errors ablegt."""
    return types.SimpleNamespace(status=status, reason=reason, headers={})


def not_found(message: str = "404: Not Found") -> discord.NotFound:
    """``discord.NotFound(response, message)`` — die Signatur hat kein ``data=``."""
    return discord.NotFound(fake_response(), message)


# Discord-Fehlercode für „Feld X passt nicht zum Rest des Bodies“.
INVALID_FORM_BODY_CODE = 50035
LEGACY_FIELDS_WITH_V2 = "MESSAGE_CANNOT_USE_LEGACY_FIELDS_WITH_COMPONENTS_V2"


def invalid_form_body(field: str, message: str,
                      *, error_code: str = LEGACY_FIELDS_WITH_V2,
                      status: int = 400) -> discord.HTTPException:
    """
    Baut Discords ``400 Bad Request (50035): Invalid Form Body`` nach.

    Der Text ist bewusst identisch zur echten API — genau so steht er später im
    Log bzw. in der Fehlermeldung, die der Nutzer sieht::

        400 Bad Request (error code: 50035): Invalid Form Body
        In content: The 'content' field cannot be used when using
        MessageFlags.IS_COMPONENTS_V2
    """
    return discord.HTTPException(
        fake_response(status=status, reason="Bad Request"),
        {
            "code": INVALID_FORM_BODY_CODE,
            "message": "Invalid Form Body",
            "errors": {
                field: {"_errors": [{"code": error_code, "message": message}]},
            },
        },
    )


def is_components_v2_view(view: Any) -> bool:
    """
    ``True``, wenn Discord die Nachricht als **Components V2** behandelt.

    Entscheidend ist, was wirklich geschickt wird (``to_components()``): Liegt
    oben ein Layout-Baustein (Container 17, Section 9, TextDisplay 10, …),
    setzt Discord intern ``MessageFlags.IS_COMPONENTS_V2``. Eine ``LayoutView``
    mit ausschließlich Action Rows (Typ 1) bleibt dagegen eine klassische
    V1-Nachricht — dort ist ``content`` weiterhin erlaubt (so verschickt die
    REST-API ihre Buttons).
    """
    to_components = getattr(view, "to_components", None)
    if callable(to_components):
        try:
            parts = to_components()
        except Exception:  # noqa: BLE001 — kaputte Attrappen sollen nicht den Test killen
            parts = None
        if isinstance(parts, list):
            return any(
                isinstance(part, dict) and part.get("type") not in (None, 1)
                for part in parts
            )
    checker = getattr(view, "has_components_v2", None)
    return bool(checker()) if callable(checker) else False


def validate_message_payload(*, content: Any = None, view: Any = None,
                             embed: Any = None, embeds: Any = None,
                             attachments: Any = None, files: Any = None) -> None:
    """
    Emuliert Discords härteste Nachrichten-Regel für **Components V2**.

    Sobald eine Nachricht Layout-Komponenten (Container, TextDisplay, …) trägt,
    setzt Discord intern ``MessageFlags.IS_COMPONENTS_V2`` und lehnt alle
    Legacy-Felder ab — ``content``, ``embeds`` und ``attachments``:

        400 Bad Request (error code: 50035): Invalid Form Body
        In content: The 'content' field cannot be used when using
        MessageFlags.IS_COMPONENTS_V2

    Genau daran ist die Datenbank-Einrichtung gescheitert: Die Erfolgsmeldung
    „✅ …“ wurde als ``content`` **neben** dem Container-View geschickt. Die
    Datenbank war zu dem Zeitpunkt bereits verknüpft, der Admin sah trotzdem
    nur einen Fehler. Leere Werte (``None``, ``""``, ``[]``) sind erlaubt — so
    räumt :meth:`RelayClient._show` alte Felder beim Bearbeiten bewusst leer.
    """
    if not is_components_v2_view(view):
        return

    legacy: List[tuple] = [
        ("content", content),
        ("embeds", embed if embed is not None else embeds),
        ("attachments", attachments if attachments is not None else files),
    ]
    for field, value in legacy:
        if value is None or value == "" or value == []:
            continue
        raise invalid_form_body(
            field,
            f"The '{field}' field cannot be used when using MessageFlags.IS_COMPONENTS_V2",
        )


def _fake_state() -> Any:
    """
    Minimaler ``ConnectionState``-Ersatz.

    discord.py nutzt ``_state.http`` für REST-Calls und ``_state.loop`` für
    Events. Der Test ruft beides nicht über echte Objekte auf, aber einige
    Properties (z. B. ``Guild._state``) würden sonst mit ``None`` crashen.
    """
    state = types.SimpleNamespace()
    state.http = FakeHTTP()
    state.loop = None
    state.store_user = lambda data: None
    state._get_guild = lambda gid: None
    state.member_cache_flags = types.SimpleNamespace(joined=False, voice=False)
    state.intents = discord.Intents.default()
    return state


class FakeAsset:
    """Imitiert ``discord.Asset`` — hat immer eine ``.url``."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.key = url.rsplit("/", 1)[-1]

    def __str__(self) -> str:
        return self.url


class FakeHTTP:
    """Imitiert ``client.http`` für die wenigen rohen REST-Aufrufe der API."""

    async def request(self, route: Any, **_kwargs: Any) -> Any:
        path = str(getattr(route, "path", route))
        if "voice/regions" in path:
            return [
                {"id": "europe", "name": "Europe", "optimal": True,
                 "deprecated": False, "custom": False},
                {"id": "eu-central", "name": "Central Europe", "optimal": False,
                 "deprecated": False, "custom": False},
            ]
        return {}


# ─────────────────────────────────────────────────────────────────────────────
#  Rollen
# ─────────────────────────────────────────────────────────────────────────────


class FakeRole(discord.Role):
    """
    Echte ``discord.Role``, ohne ``__init__``.

    ``permissions``, ``colour``, ``mention``, ``created_at`` und ``is_default()``
    kommen damit unverfälscht aus discord.py.
    """

    def __init__(self, rid: int, name: str, position: int, perms: int, *,
                 color: int = 0, hoist: bool = False, mentionable: bool = False,
                 managed: bool = False) -> None:
        self.id = rid
        self.name = name
        self.position = position
        self._permissions = int(perms or 0)
        # discord.py legt _permissions/_colour als nackte ints ab und baut die
        # Objekte erst in den Properties — genau so muss es hier auch sein.
        self._colour = int(getattr(color, "value", color) or 0)
        self.hoist = hoist
        self.mentionable = mentionable
        self.managed = managed
        self._icon = None
        self.unicode_emoji = None
        self.tags = None
        self._flags = 0
        self._state = _fake_state()
        self.guild: Any = None
        self._secondary_colour = None
        self._tertiary_colour = None


# ─────────────────────────────────────────────────────────────────────────────
#  Kanäle
# ─────────────────────────────────────────────────────────────────────────────


def _settable(attr: str) -> Any:
    """
    Erzeugt eine Property, die eine schreibgeschützte discord.py-Property überlagert.

    Ohne das ließe sich z. B. ``channel.category = …`` nicht setzen, weil
    ``GuildChannel.category`` nur lesbar ist.
    """
    return property(lambda self: getattr(self, attr, None),
                    lambda self, value: setattr(self, attr, value))


def _install_settable(cls: type, names: tuple) -> None:
    """
    Überlagert read-only Properties von discord.py mit setzbaren Varianten.

    ``discord.User``/``discord.Member`` definieren ``bot``, ``system``,
    ``discriminator``, ``flags`` … als Properties, die ihren Wert aus einem
    ``ConnectionState`` ziehen. Im Test werden sie direkt gesetzt; jede dieser
    Properties braucht deshalb ein setzbares Gegenstück mit ``_<name>``-Speicher.
    """
    for name in names:
        setattr(cls, name, _settable(f"_{name}"))


class _FakeGuildChannelMixin:
    """Gemeinsame, setzbare Felder aller simulierten Kanal-Typen."""

    category = _settable("_category")
    overwrites = _settable("_overwrites_map")
    permissions_synced = _settable("_permissions_synced")
    members = _settable("_members")
    threads = _settable("_threads")

    def _init_channel(self, cid: int, name: str, *, position: int = 0,
                      category: Any = None, topic: Optional[str] = None) -> None:
        self.id = cid
        self.name = name
        self.guild: Any = None
        self.topic = topic
        self._state = _fake_state()
        self.nsfw = False
        self.category_id = category.id if category is not None else None
        self._category = category
        self.position = position
        self.slowmode_delay = 0
        self._overwrites: List[Any] = []
        self._overwrites_map: Dict[Any, discord.PermissionOverwrite] = {}
        self._permissions_synced = True
        self._members: List[Any] = []
        self._threads: List[Any] = []
        self.last_message_id = None

    def overwrites_for(self, target: Any) -> Optional[discord.PermissionOverwrite]:
        return self._overwrites_map.get(target)

    def is_news(self) -> bool:
        return self.type is discord.ChannelType.news

    async def create_invite(self, **kwargs: Any) -> "FakeInvite":
        g = getattr(self, "guild", None)
        inv_list = getattr(g, "created_invites", None)
        code = f"invite{len(inv_list):03d}" if inv_list is not None else "invite000"
        inv = FakeInvite(code, self)
        for k, v in kwargs.items():
            if hasattr(inv, k):
                setattr(inv, k, v)
        if inv_list is not None:
            inv_list.append(inv)
        if g and hasattr(g, "mutations"):
            g.mutations.append(f"create_invite:#{self.name}")
        return inv


class FakeCategory(_FakeGuildChannelMixin, discord.CategoryChannel):
    """Echte ``discord.CategoryChannel``; ``type`` liefert discord.py selbst."""

    channels = _settable("_channels")
    text_channels = _settable("_text_channels")
    voice_channels = _settable("_voice_channels")
    stage_channels = _settable("_stage_channels")
    forums = _settable("_forums")

    def __init__(self, cid: int, name: str, position: int = 0) -> None:
        self._init_channel(cid, name, position=position)
        self._channels: List[Any] = []
        self._text_channels: List[Any] = []
        self._voice_channels: List[Any] = []
        self._stage_channels: List[Any] = []
        self._forums: List[Any] = []


class FakeTextChannel(_FakeGuildChannelMixin, discord.TextChannel):
    """Echte ``discord.TextChannel`` (``_type`` steuert text vs. news)."""

    default_auto_archive_duration = _settable("_default_auto_archive_duration")
    default_thread_slowmode_delay = _settable("_default_thread_slowmode_delay")

    def __init__(self, cid: int, name: str, *, position: int = 0, category: Any = None,
                 topic: Optional[str] = None, news: bool = False) -> None:
        self._init_channel(cid, name, position=position, category=category, topic=topic)
        self._type = discord.ChannelType.news.value if news else discord.ChannelType.text.value
        self._default_auto_archive_duration = None
        self._default_thread_slowmode_delay = None

    @property
    def last_message(self) -> Any:
        return None


class FakeVoiceChannel(_FakeGuildChannelMixin, discord.VoiceChannel):
    """Echte ``discord.VoiceChannel``."""

    def __init__(self, cid: int, name: str, *, position: int = 0, category: Any = None) -> None:
        self._init_channel(cid, name, position=position, category=category)
        self.bitrate = 64000
        self.user_limit = 0
        self.rtc_region: Optional[str] = None
        self.video_quality_mode = discord.VideoQualityMode.auto
        self.status: Optional[str] = None


class FakeStageChannel(_FakeGuildChannelMixin, discord.StageChannel):
    """Echte ``discord.StageChannel``."""

    def __init__(self, cid: int, name: str, *, position: int = 0, category: Any = None) -> None:
        self._init_channel(cid, name, position=position, category=category)
        self.bitrate = 64000
        self.user_limit = 0
        self.rtc_region: Optional[str] = None
        self.video_quality_mode = discord.VideoQualityMode.auto
        self.topic = None

    @property
    def stage_instance(self) -> Any:
        return None


class FakeForumChannel(_FakeGuildChannelMixin, discord.ForumChannel):
    """Echte ``discord.ForumChannel`` (auch für ``media``-Kanäle genutzt)."""

    available_tags = _settable("_available_tags")

    def __init__(self, cid: int, name: str, *, position: int = 0, category: Any = None,
                 topic: Optional[str] = None, media: bool = False) -> None:
        self._init_channel(cid, name, position=position, category=category, topic=topic)
        self._type = (discord.ChannelType.media if media else discord.ChannelType.forum).value
        self._available_tags: List[Any] = []
        self.default_auto_archive_duration = None
        self.default_thread_slowmode_delay = None
        self.default_sort_order = None
        self._flags = 0


# ─────────────────────────────────────────────────────────────────────────────
#  Nutzer & Mitglieder
# ─────────────────────────────────────────────────────────────────────────────


_USER_OVERRIDES = (
    "id", "name", "bot", "system", "discriminator", "global_name", "display_name",
    "display_avatar", "avatar", "banner", "accent_color", "mention", "created_at",
    "public_flags", "dm_channel", "mutual_guilds", "default_avatar",
    "avatar_decoration", "primary_guild", "collectibles", "display_banner",
)


class FakeUser(discord.User):
    """Echte ``discord.User`` ohne ``__init__``."""

    def __init__(self, uid: int, name: str, *, bot: bool = False) -> None:
        values = {
            "id": uid, "name": name, "bot": bot, "system": False,
            "discriminator": "0", "global_name": name, "display_name": name,
            "display_avatar": FakeAsset(f"https://cdn.discordapp.com/embed/avatars/{uid % 5}.png"),
            "avatar": None, "banner": None, "accent_color": None,
            "mention": f"<@{uid}>", "created_at": now() - timedelta(days=400),
            "public_flags": discord.PublicUserFlags._from_value(0), "dm_channel": None,
            "mutual_guilds": [], "default_avatar": None, "avatar_decoration": None,
            "primary_guild": None, "collectibles": None, "display_banner": None,
        }
        for key, value in values.items():
            setattr(self, f"_{key}", value)
        self._state = _fake_state()


_install_settable(FakeUser, _USER_OVERRIDES)


_MEMBER_OVERRIDES = _USER_OVERRIDES + (
    "roles", "top_role", "guild_permissions", "colour", "color", "status",
    "raw_status", "mobile_status", "desktop_status", "web_status", "voice",
    "flags", "guild_avatar", "activity", "resolved_permissions", "display_icon",
    "timed_out",
)


class FakeMember(discord.Member):
    """
    Echte ``discord.Member``.

    Alle Properties, die discord.py aus dem ``ConnectionState`` ableitet, sind
    hier setzbar überlagert — sonst ließe sich kein Mitglied ohne Gateway bauen.
    """

    def __init__(self, uid: int, name: str, roles: List[FakeRole], *, bot: bool = False,
                 nick: Optional[str] = None, guild: Any = None) -> None:
        values = {
            "id": uid, "name": name, "bot": bot, "system": False,
            "discriminator": "0", "global_name": name,
            "display_name": nick or name,
            "display_avatar": FakeAsset(f"https://cdn.discordapp.com/embed/avatars/{uid % 5}.png"),
            "avatar": None, "banner": None, "accent_color": None,
            "mention": f"<@{uid}>", "created_at": now() - timedelta(days=400),
            "public_flags": discord.PublicUserFlags._from_value(0), "dm_channel": None,
            "mutual_guilds": [], "default_avatar": None, "avatar_decoration": None,
            "primary_guild": None, "collectibles": None, "display_banner": None,
            "roles": list(roles),
            "top_role": max(roles, key=lambda r: r.position) if roles else None,
            "guild_permissions": _merge_permissions(roles),
            "colour": discord.Colour(0), "color": discord.Colour(0),
            "status": discord.Status.online, "raw_status": "online",
            "mobile_status": discord.Status.offline, "desktop_status": discord.Status.online,
            "web_status": discord.Status.offline, "voice": None,
            "flags": discord.MemberFlags._from_value(0), "guild_avatar": None, "activity": None,
            "resolved_permissions": None, "display_icon": None,
            "timed_out": False,
        }
        for key, value in values.items():
            setattr(self, f"_{key}", value)
        self.nick = nick
        self.guild = guild
        self.joined_at = now() - timedelta(days=10)
        self.premium_since = None
        self.timed_out_until = None
        self.pending = False
        self._flags = 0
        self._state = _fake_state()
        self._user = FakeUser(uid, name, bot=bot)
        self._avatar = None
        self._avatar_decoration_sku_id = None
        self._primary_guild = None
        self._collectibles = None

    async def edit(self, **kwargs: Any) -> None:
        guild = self.guild
        if guild is not None and hasattr(guild, "mutations"):
            guild.mutations.append(f"member.edit:{self._name}")
        for key, value in kwargs.items():
            if key == "nick":
                self.nick = value
                self._display_name = value or self._name
            elif key in ("avatar", "banner", "bio"):
                # Eigenes Server-Profil (nur für das eigene Mitglied erlaubt) —
                # wird hier nur protokolliert, damit Tests die Mutation sehen.
                if guild is not None and hasattr(guild, "mutations"):
                    guild.mutations.append(
                        f"member.edit:{key}:{'gesetzt' if value is not None else 'entfernt'}")
                setattr(self, f"_{key}_payload", value)
            elif key == "roles" and value is not None:
                self._roles = list(value)
                self._top_role = (max(self._roles, key=lambda r: r.position)
                                  if self._roles else None)
                self._guild_permissions = _merge_permissions(self._roles)

    async def add_roles(self, *roles: Any, reason: Optional[str] = None) -> None:
        self._roles = list(self._roles) + [r for r in roles if r not in self._roles]
        self._guild_permissions = _merge_permissions(self._roles)

    async def remove_roles(self, *roles: Any, reason: Optional[str] = None) -> None:
        self._roles = [r for r in self._roles if r not in roles]
        self._guild_permissions = _merge_permissions(self._roles)

    async def timeout(self, until: Any, *, reason: Optional[str] = None) -> None:
        self.timed_out_until = until
        self._timed_out = until is not None

    async def move_to(self, channel: Any, *, reason: Optional[str] = None) -> None:
        return None


_install_settable(FakeMember, _MEMBER_OVERRIDES)


def _merge_permissions(roles: List[FakeRole]) -> discord.Permissions:
    value = 0
    for role in roles:
        value |= role.permissions.value
    return discord.Permissions(value)


# ─────────────────────────────────────────────────────────────────────────────
#  Server
# ─────────────────────────────────────────────────────────────────────────────


class FakeMessage:
    """Imitiert ``discord.Message`` so weit, wie ``serialize_message`` es braucht."""

    def __init__(self, mid: int, channel: Any, author: Any, content: str = "",
                 embeds: Optional[List[Any]] = None) -> None:
        self.id = mid
        self.channel = channel
        self.author = author
        self.content = content
        self.embeds = embeds or []
        self.created_at = now()
        self.edited_at = None
        self.tts = False
        self.pinned = False
        self.mention_everyone = False
        self.type = discord.MessageType.default
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{channel.id}/{mid}"
        self.attachments: List[Any] = []
        self.reactions: List[Any] = []
        self.mentions: List[Any] = []
        self.role_mentions: List[Any] = []
        self.channel_mentions: List[Any] = []
        self.reference = None
        self.stickers: List[Any] = []
        self.flags = discord.MessageFlags._from_value(0)
        self.components: List[Any] = []
        self.webhook_id: Optional[int] = None

    async def edit(self, **kwargs: Any) -> "FakeMessage":
        self.content = kwargs.get("content", self.content)
        return self

    async def delete(self, **_kwargs: Any) -> None:
        return None

    async def pin(self, **_kwargs: Any) -> None:
        self.pinned = True

    async def add_reaction(self, _emoji: Any) -> None:
        return None


class FakeInvite:
    def __init__(self, code: str, channel: Any) -> None:
        self.code = code
        self.url = f"https://discord.gg/{code}"
        self.channel = channel
        self.guild = getattr(channel, "guild", None)
        self.inviter = None
        self.max_age = 0
        self.max_uses = 0
        self.uses = 0
        self.temporary = False
        self.created_at = now()
        self.approximate_member_count = None
        self.approximate_presence_count = None


class FakeAutoModRule:
    def __init__(self, rid: int, name: str, guild: Any) -> None:
        self.id = rid
        self.name = name
        self.guild = guild
        self.creator_id = BOT_ID
        # discord.py nennt den Enum-Typ AutoModRuleTriggerType (nicht AutoModTriggerType!)
        self.event_type = discord.AutoModRuleEventType.message_send
        self.trigger_type = discord.AutoModRuleTriggerType.keyword
        self.trigger = None
        self.actions: List[Any] = []
        self.enabled = True
        self.exempt_roles: List[Any] = []
        self.exempt_channels: List[Any] = []


class FakeWebhook:
    """Imitiert ``discord.Webhook`` für die Persona-Tests."""

    def __init__(self, wid: int, name: str, channel: Any, guild: Any) -> None:
        self.id = wid
        self.name = name
        self.type = discord.WebhookType.incoming
        self.channel_id = channel.id
        self.guild_id = guild.id
        self.channel = channel
        self.guild = guild
        self.user = guild.me
        self.token = f"fake-token-{wid}"
        self.avatar = None
        self.created_at = now()
        self._persona = name

    @property
    def url(self) -> str:
        return f"https://discord.com/api/webhooks/{self.id}/{self.token}"

    async def send(self, content: Optional[str] = None, **kwargs: Any) -> "FakeMessage":
        validate_message_payload(content=content, view=kwargs.get("view"),
                                 embed=kwargs.get("embed"), embeds=kwargs.get("embeds"),
                                 files=kwargs.get("files"))
        persona = kwargs.get("username") or self.name
        self._persona = persona
        author = FakeUser(int(self.id), persona, bot=True)
        embeds = list(kwargs.get("embeds") or [])
        message = FakeMessage(self.guild._new_id(), self.channel, author,  # noqa: SLF001
                              content=content or "", embeds=embeds)
        message.webhook_id = self.id
        self.guild.sent_messages.append(message)  # noqa: SLF001
        self.guild._log(f"webhook.send:{persona}")  # noqa: SLF001
        return message

    async def edit(self, **kwargs: Any) -> "FakeWebhook":
        for key, value in kwargs.items():
            if key in ("name", "avatar", "channel") and value is not None:
                setattr(self, key, value)
        self.guild._log(f"webhook.edit:{self.name}")  # noqa: SLF001
        return self

    async def delete(self, **_kwargs: Any) -> None:
        self.guild._webhooks = [w for w in self.guild._webhooks if w.id != self.id]  # noqa: SLF001
        self.guild._log(f"webhook.delete:{self.name}")  # noqa: SLF001


class FakeGuild:
    """
    Simulierter Discord-Server.

    Bewusst **keine** ``discord.Guild``-Subklasse: Dort sind ``channels``,
    ``roles``, ``me`` und ``member_count`` read-only Properties, die einen
    echten ``ConnectionState`` samt Gateway-Cache voraussetzen. Für die
    Serialisierer reichen die gleichnamigen Attribute.
    """

    def __init__(self, gid: int = GUILD_ID, name: str = "Smoke-Test Server") -> None:
        self.id = gid
        self.name = name
        self.icon = None
        self.banner = None
        self.splash = None
        self.discovery_splash = None
        self.description = "Testserver"
        self.owner_id = OWNER_ID
        self.member_count = 4
        self.approximate_member_count = 4
        self.approximate_presence_count = 2
        self.max_members = 250000
        self.max_presences = None
        self.max_stage_video_channel_users = 30
        self.premium_subscription_count = 0
        self.premium_tier = 0                     # plain int in discord.py 2.x
        self.premium_progress_bar_enabled = False
        self.created_at = now() - timedelta(days=30)
        self.shard_id = None
        self.large = False
        self.unavailable = False

        self.verification_level = discord.VerificationLevel.medium
        self.explicit_content_filter = discord.ContentFilter.all_members
        self.default_notifications = discord.NotificationLevel.only_mentions
        self.mfa_level = discord.MFALevel.disabled
        self.nsfw_level = discord.NSFWLevel.default
        self.raid_alerts_disabled = False

        self.features = ["COMMUNITY"]
        self.preferred_locale = discord.Locale.german
        self.system_channel_flags = discord.SystemChannelFlags._from_value(0)
        self.vanity_url = None
        self.afk_timeout = 300
        self.widget_enabled = False

        everyone = FakeRole(gid, "@everyone", 0, everyone_permissions().value)
        everyone.guild = self
        mod = FakeRole(ROLE_MOD, "🛡️ Moderator", 5,
                       discord.Permissions(kick_members=True, moderate_members=True,
                                           manage_messages=True, view_channel=True,
                                           send_messages=True).value,
                       color=0x3498DB, hoist=True, mentionable=True)
        mod.guild = self
        self.roles: List[FakeRole] = [everyone, mod]
        self.default_role = everyone

        category = FakeCategory(CATEGORY_ID, "📌 INFORMATION", position=0)
        text = FakeTextChannel(TEXT_ID, "regeln", position=0, category=category,
                               topic="Bitte lesen")
        voice = FakeVoiceChannel(VOICE_ID, "Lounge", position=0, category=category)
        category.channels = [text, voice]
        self.channels: List[Any] = [category, text, voice]
        self._reindex()

        bot_role = FakeRole(gid + 1, "AIDiscordServerEinrichten", 10,
                            discord.Permissions.all().value, color=0x5865F2, hoist=True)
        bot_role.guild = self
        self.roles.append(bot_role)

        self.me = FakeMember(BOT_ID, "AIDiscordServerEinrichten",
                             [everyone, bot_role], bot=True, guild=self)
        self.owner = FakeMember(OWNER_ID, "Owner", [everyone, mod], guild=self)
        user = FakeMember(USER_ID, "Tester", [everyone], guild=self)
        self.members: List[FakeMember] = [self.me, self.owner, user]
        self.emojis: List[Any] = []
        self.stickers: List[Any] = []
        self.scheduled_events: List[Any] = []
        self.threads: List[Any] = []
        self.chunked = True
        self.region = None
        self.widget_channel = None
        self.system_channel = text
        self.rules_channel = text
        self.public_updates_channel = None
        self.safety_alerts_channel = None
        self.afk_channel = None
        self.community = True
        self.discoverable = False
        self.partnered = False
        self.verified = False
        self.invites_disabled = False
        self.mutations: List[str] = []
        self._webhooks: List[Any] = []
        self.created_invites: List[FakeInvite] = []

        async def _invites_call() -> List[Any]:
            return list(getattr(self, "_invites_list", []))

        async def _invites_create(**kwargs: Any) -> FakeInvite:
            channel = self.system_channel or (self.text_channels[0] if self.text_channels else None)
            invite = FakeInvite(f"panel{len(self.created_invites):03d}", channel)
            for k, v in kwargs.items():
                if hasattr(invite, k):
                    setattr(invite, k, v)
            self.created_invites.append(invite)
            self.mutations.append(f"invites.create:{getattr(channel, 'name', 'guild')}")
            return invite

        _invites_call.create = _invites_create  # type: ignore[attr-defined]
        self.invites = _invites_call  # type: ignore[assignment]

    async def webhooks(self) -> List[Any]:
        return list(self._webhooks)

    async def leave(self) -> None:
        self.mutations.append("guild.leave")
        if hasattr(self, "_state") and hasattr(self._state, "_guilds"):
            self._state._guilds.pop(self.id, None)

    # ── interne Pflege ───────────────────────────────────────────────────────
    def _reindex(self) -> None:
        """Berechnet die abgeleiteten Kanallisten neu (wie es das Gateway täte)."""
        for channel in self.channels:
            channel.guild = self
        self.categories = [c for c in self.channels
                           if c.type is discord.ChannelType.category]
        self.text_channels = [c for c in self.channels
                              if c.type in (discord.ChannelType.text, discord.ChannelType.news)]
        self.voice_channels = [c for c in self.channels if c.type is discord.ChannelType.voice]
        self.stage_channels = [c for c in self.channels
                               if c.type is discord.ChannelType.stage_voice]
        self.forums = [c for c in self.channels
                       if c.type in (discord.ChannelType.forum, discord.ChannelType.media)]

    # ── Lookup, wie discord.py ihn bietet ────────────────────────────────────
    def get_channel(self, cid: int) -> Optional[Any]:
        return next((c for c in self.channels if c.id == cid), None)

    def get_thread(self, _tid: int) -> Optional[Any]:
        return None

    def get_role(self, rid: int) -> Optional[FakeRole]:
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, mid: int) -> Optional[FakeMember]:
        return next((m for m in self.members if m.id == mid), None)

    def get_emoji(self, _eid: int) -> Optional[Any]:
        return None

    def role_by_name(self, name: str) -> Optional[FakeRole]:
        return next((r for r in self.roles if r.name.lower() == name.lower()), None)

    async def fetch_member(self, mid: int) -> FakeMember:
        member = self.get_member(mid)
        if member is None:
            raise not_found("Unknown Member")
        return member

    async def fetch_automod_rules(self) -> List[Any]:
        return []

    async def fetch_stickers(self) -> List[Any]:
        return []

    async def fetch_scheduled_events(self, *, with_counts: bool = True) -> List[Any]:
        return []

    async def welcome_screen(self) -> Any:
        raise not_found("Welcome screen not enabled")

    async def widget(self) -> Any:
        raise not_found("Widget not enabled")

    async def onboarding(self) -> Any:
        raise not_found("Onboarding not enabled")

    async def templates(self) -> List[Any]:
        return []

    async def vanity_invite(self) -> Any:
        return None

    async def query_members(self, query: Optional[str] = None, *, limit: int = 100,
                            user_ids: Any = None, presences: bool = False,
                            cache: bool = True) -> List[FakeMember]:
        """``Guild.search_members`` gibt es nicht — korrekt heißt es ``query_members``."""
        needle = (query or "").lower()
        found = [m for m in self.members if needle in m.display_name.lower()]
        return found[:limit]

    async def estimate_pruned_members(self, *, days: int = 7, roles: Any = ...) -> int:
        return 0

    def audit_logs(self, **_kwargs: Any):  # noqa: ANN201
        async def _gen():
            return
            yield  # pragma: no cover

        return _gen()

    def bans(self, **_kwargs: Any):  # noqa: ANN201
        async def _gen():
            return
            yield  # pragma: no cover

        return _gen()


class MutableGuild(FakeGuild):
    """
    Server, der Schreiboperationen wirklich ausführt und mitprotokolliert.

    Damit lässt sich ``POST /api/v1/setup`` komplett durchspielen, ohne Discord
    zu berühren — Rollen, Kategorien, Kanäle, Nachrichten, Invites, AutoMod.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent_messages: List[FakeMessage] = []
        self.created_invites: List[FakeInvite] = []
        self.automod_rules: List[FakeAutoModRule] = []
        self.automod = self.automod_rules
        self._next_id = 10 ** 17
        for channel in self.channels:
            # Auch die Kanäle aus der Basis-Ausstattung (regeln, Lounge, …)
            # bekommen die Schreib-/Webhook-/Purge-Attrappen.
            self._attach_helpers(channel)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _log(self, action: str) -> None:
        self.mutations.append(action)

    # ── Rollen ────────────────────────────────────────────────────────────────
    async def create_role(self, *, name: str = "neue Rolle", permissions: Any = None,
                          colour: Any = None, color: Any = None, hoist: bool = False,
                          mentionable: bool = False, reason: Optional[str] = None,
                          **kwargs: Any) -> FakeRole:
        value = getattr(permissions, "value", permissions or 0)
        chosen = color if color is not None else colour
        role = FakeRole(self._new_id(), name, len(self.roles), int(value or 0),
                        color=getattr(chosen, "value", chosen or 0),
                        hoist=hoist, mentionable=mentionable)
        role.guild = self
        icon = kwargs.get("display_icon")
        if isinstance(icon, str):
            role.unicode_emoji = icon
        self.roles.append(role)
        self._log(f"create_role:{name}")
        return role

    async def edit_role_positions(self, positions: Any, *,
                                  reason: Optional[str] = None) -> None:
        mapping = dict(positions)
        for role, position in mapping.items():
            target = self.get_role(getattr(role, "id", role))
            if target is not None:
                target.position = int(position)
        self._log(f"edit_role_positions:{len(mapping)}")

    async def edit(self, *, reason: Optional[str] = None, **fields: Any) -> "MutableGuild":
        for key, value in fields.items():
            if value is None or value is ...:
                continue
            if hasattr(self, key):
                setattr(self, key, value)
            self._log(f"guild.edit:{key}")
        return self

    async def edit_welcome_screen(self, **kwargs: Any) -> Any:
        self._log(f"edit_welcome_screen:{sorted(kwargs)}")
        return kwargs

    # ── Kanäle ────────────────────────────────────────────────────────────────
    def _attach_helpers(self, channel: Any) -> Any:
        """Setzt die schreibbaren Attrappen (send/webhooks/purge/…) auf einen Kanal."""
        channel.send = _make_send(self, channel)              # type: ignore[method-assign]
        channel.create_invite = _make_invite(self, channel)    # type: ignore[method-assign]
        channel.edit = _make_edit(self, channel)               # type: ignore[method-assign]
        channel.webhooks = _make_channel_webhooks(self, channel)  # type: ignore[method-assign]
        channel.create_webhook = _make_create_webhook(self, channel)  # type: ignore[method-assign]
        channel.purge = _make_purge(self, channel)             # type: ignore[method-assign]
        return channel

    def _attach(self, channel: Any) -> Any:
        channel.guild = self
        self.channels.append(channel)
        category = getattr(channel, "category", None)
        if category is not None and hasattr(category, "channels"):
            category.channels = list(category.channels or []) + [channel]
        self._reindex()
        self._attach_helpers(channel)
        return channel

    async def create_category(self, name: str, **kwargs: Any) -> FakeCategory:
        # discord.py-Codepad: fehlende Felder kommen als MISSING-Sentinel —
        # für den Test zählt das wie "nicht angegeben".
        pos = kwargs.get("position")
        position = pos if isinstance(pos, int) else len(self.categories)
        category = FakeCategory(self._new_id(), name, position=position)
        self._log(f"create_category:{name}")
        return self._attach(category)

    async def create_text_channel(self, name: str, **kwargs: Any) -> FakeTextChannel:
        channel = FakeTextChannel(
            self._new_id(), name,
            position=kwargs.get("position") or 0,
            category=kwargs.get("category"),
            topic=kwargs.get("topic"),
            news=bool(kwargs.get("news")),
        )
        channel.nsfw = bool(kwargs.get("nsfw", False))
        channel.slowmode_delay = kwargs.get("slowmode_delay", 0) or 0
        channel._overwrites_map = dict(kwargs.get("overwrites") or {})
        self._log(f"create_text_channel:{name}")
        return self._attach(channel)

    async def create_voice_channel(self, name: str, **kwargs: Any) -> FakeVoiceChannel:
        channel = FakeVoiceChannel(self._new_id(), name,
                                   position=kwargs.get("position") or 0,
                                   category=kwargs.get("category"))
        channel.bitrate = kwargs.get("bitrate", 64000)
        channel.user_limit = kwargs.get("user_limit", 0) or 0
        channel.rtc_region = kwargs.get("rtc_region")
        channel.nsfw = bool(kwargs.get("nsfw", False))
        self._log(f"create_voice_channel:{name}")
        return self._attach(channel)

    async def create_stage_channel(self, name: str, **kwargs: Any) -> FakeStageChannel:
        channel = FakeStageChannel(self._new_id(), name,
                                   position=kwargs.get("position") or 0,
                                   category=kwargs.get("category"))
        channel.bitrate = kwargs.get("bitrate", 64000)
        self._log(f"create_stage_channel:{name}")
        return self._attach(channel)

    async def create_forum(self, name: str, **kwargs: Any) -> FakeForumChannel:
        channel = FakeForumChannel(self._new_id(), name,
                                   position=kwargs.get("position") or 0,
                                   category=kwargs.get("category"),
                                   topic=kwargs.get("topic"),
                                   media=bool(kwargs.get("media")))
        channel._available_tags = list(kwargs.get("available_tags") or [])
        self._log(f"create_forum:{name}")
        return self._attach(channel)

    # ── AutoMod ───────────────────────────────────────────────────────────────
    async def create_automod_rule(self, *, name: str, event_type: Any = None,
                                  trigger: Any = None, actions: Any = None,
                                  enabled: bool = True, reason: Optional[str] = None,
                                  **kwargs: Any) -> FakeAutoModRule:
        rule = FakeAutoModRule(self._new_id(), name, self)
        rule.trigger = trigger
        rule.actions = list(actions or [])
        rule.enabled = enabled
        rule.event_type = event_type or rule.event_type
        rule.exempt_roles = [r for r in (kwargs.get("exempt_roles") or []) if r is not ...]
        rule.exempt_channels = [c for c in (kwargs.get("exempt_channels") or []) if c is not ...]
        self.automod_rules.append(rule)
        self._log(f"create_automod_rule:{name}")
        return rule


def _make_send(guild: MutableGuild, channel: Any):
    async def send(content: Optional[str] = None, **kwargs: Any) -> FakeMessage:
        # Wie die echte API: content/embeds/attachments sind neben Components V2
        # verboten (400, error code 50035).
        validate_message_payload(content=content, view=kwargs.get("view"),
                                 embed=kwargs.get("embed"), embeds=kwargs.get("embeds"),
                                 files=kwargs.get("files"))
        embeds = list(kwargs.get("embeds") or [])
        if kwargs.get("embed") is not None:
            embeds.append(kwargs["embed"])
        message = FakeMessage(guild._new_id(), channel, guild.me,
                              content=content or "", embeds=embeds)
        guild.sent_messages.append(message)
        guild._log(f"send:#{channel.name}")
        return message

    return send


def _make_invite(guild: MutableGuild, channel: Any):
    async def create_invite(**kwargs: Any) -> FakeInvite:
        invite = FakeInvite(f"smoke{len(guild.created_invites):03d}", channel)
        invite.max_age = kwargs.get("max_age", 0) or 0
        invite.max_uses = kwargs.get("max_uses", 0) or 0
        guild.created_invites.append(invite)
        guild._log(f"create_invite:#{channel.name}")
        return invite

    return create_invite


def _make_edit(guild: MutableGuild, channel: Any):
    async def edit(**kwargs: Any) -> Any:
        for key, value in kwargs.items():
            if value is None or value is ...:
                continue
            if hasattr(channel, key):
                setattr(channel, key, value)
            guild._log(f"channel.edit:{channel.name}:{key}")
        return channel

    return edit


def _make_channel_webhooks(guild: MutableGuild, channel: Any):
    async def webhooks() -> List[FakeWebhook]:
        return [w for w in guild._webhooks if w.channel_id == channel.id]  # noqa: SLF001

    return webhooks


def _make_create_webhook(guild: MutableGuild, channel: Any):
    async def create_webhook(*, name: str, avatar: Any = None,
                             reason: Optional[str] = None, **_kwargs: Any) -> FakeWebhook:
        hook = FakeWebhook(guild._new_id(), name, channel, guild)  # noqa: SLF001
        hook.avatar = avatar
        guild._webhooks.append(hook)  # noqa: SLF001
        guild._log(f"create_webhook:{name}")  # noqa: SLF001
        return hook

    return create_webhook


def _make_purge(guild: MutableGuild, channel: Any):
    async def purge(limit: int = 100, *, check: Any = None, bulk: bool = True,
                    reason: Optional[str] = None, **_kwargs: Any) -> List[FakeMessage]:
        victims: List[FakeMessage] = []
        remaining: List[FakeMessage] = []
        for message in guild.sent_messages:
            if message.channel is channel and len(victims) < limit:
                if check is None or check(message):
                    victims.append(message)
                    continue
            remaining.append(message)
        guild.sent_messages = remaining  # noqa: SLF001
        guild._log(f"purge:#{getattr(channel, 'name', channel.id)}:{len(victims)}")  # noqa: SLF001
        return victims

    return purge


class FakeClient:
    """Imitiert ``discord.Client`` so weit, wie die API es braucht."""

    def __init__(self, guilds: List[FakeGuild]) -> None:
        self.user = FakeUser(BOT_ID, "AIDiscordServerEinrichten", bot=True)
        self.guilds = list(guilds)
        self.latency = 0.042
        self._by_id = {g.id: g for g in guilds}

    def is_closed(self) -> bool:
        return False

    def get_guild(self, gid: int) -> Optional[FakeGuild]:
        return self._by_id.get(gid)

    def swap_guild(self, guild: FakeGuild) -> None:
        """Ersetzt den simulierten Server (für die Schreib-Tests)."""
        self._by_id[guild.id] = guild
        self.guilds = [g for g in self.guilds if g.id != guild.id] + [guild]

    async def fetch_channel(self, cid: int) -> Any:
        for guild in self.guilds:
            channel = guild.get_channel(cid)
            if channel is not None:
                return channel
        raise not_found("Unknown Channel")

    async def fetch_webhook(self, wid: int) -> Any:
        for guild in self.guilds:
            for hook in getattr(guild, "_webhooks", []) or []:
                if hook.id == wid:
                    return hook
        raise not_found("Unknown Webhook")

    @property
    def http(self) -> FakeHTTP:
        return FakeHTTP()
