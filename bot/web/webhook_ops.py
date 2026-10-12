"""
Webhook-Personas: Nachrichten mit eigenem Namen und eigenem Avatar senden.

Warum Webhooks statt Bot-Nachrichten?
 * Eine Webhook-Nachricht tarnt sich als eigener Absender — ``📜 Serverregeln``,
   ``🎉 Willkommens-Team`` oder ``🛡️ Moderation`` — inklusive Profilbild.
 * Der Server wirkt dadurch gestaltet statt bot-betrieben; Regeln, Willkommen,
   Ankündigungen und Info-Postings bekommen jede ihre eigene Identität.
 * Ein Webhook pro Kanal reicht: Name und Avatar lassen sich **pro Nachricht**
   übersteuern (``username``/``avatar_url``).

Zusätzlich: :func:`resolve_mention_placeholders` ersetzt ``<#kanal-key>``- und
``<@&rollen-key>``-Referenzen aus Setup-Plänen durch echte Discord-Mentions —
Platzhalter wie ``<#regeln>`` oder ``<@1234567890>`` dürfen niemals im Chat
landen.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

import discord

from ..util import ApiError, parse_str
from .context import guard

__all__ = (
    "DEFAULT_WEBHOOK_NAME",
    "normalize_webhook_spec",
    "get_or_create_webhook",
    "send_as_webhook",
    "resolve_mention_placeholders",
    "resolve_placeholders_deep",
)

#: Fallback-Name, wenn ein Webhook-Spec keinen Namen enthält.
DEFAULT_WEBHOOK_NAME = "Arena"

#: Was ``Webhook.send`` akzeptiert (discord.py 2.7). Alles andere wird
#: gefiltert und als Hinweis zurückgemeldet, statt mit einem 500er zu sterben.
_WEBHOOK_SEND_FIELDS = {
    "content", "tts", "embeds", "files", "allowed_mentions", "view",
    "suppress_embeds", "silent", "poll",
}

_CHANNEL_MENTION_RE = re.compile(r"<#(?P<ref>[^<>#\s]{1,100})>")
_ROLE_MENTION_RE = re.compile(r"<@&(?P<ref>[^<>&\s]{1,100})>")


def normalize_webhook_spec(data: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Liest die Webhook-Angabe aus einem Nachrichten-Body.

    Unterstützte Formen::

        {"webhook": {"name": "📜 Regeln", "avatar": "https://…/x.png"}}
        {"webhook": "📜 Regeln"}                  # nur Name
        {"webhook": true}                         # Default-Persona "Arena"
        {"webhook_name": "…", "webhook_avatar": "…"}   # Kurzformen

    Rückgabe ``None`` heißt: normal als Bot senden.
    """
    raw = data.get("webhook")
    if raw is None and not (data.get("webhook_name") or data.get("webhook_avatar")):
        return None

    spec: Dict[str, Any] = {}
    if isinstance(raw, bool):
        if not raw:
            return None
    elif isinstance(raw, str):
        spec["name"] = raw
    elif isinstance(raw, Mapping):
        spec.update(dict(raw))
    elif raw is not None:
        raise ApiError.bad_request(
            f"webhook: erwartet true, einen Namen oder ein Objekt "
            f'{{"name": "…", "avatar": "…"}} (erhalten: {type(raw).__name__}).',
            hint='Beispiel: {"webhook": {"name": "📜 Serverregeln", '
                 '"avatar": "https://example.com/regeln.png"}}',
            code="WEBHOOK_SPEC_INVALID",
        )

    if data.get("webhook_name"):
        spec.setdefault("name", data["webhook_name"])
    if data.get("webhook_avatar"):
        spec.setdefault("avatar", data["webhook_avatar"])

    if spec.get("name"):
        spec["name"] = parse_str(spec["name"], field="webhook.name",
                                 max_length=80, allow_empty=False) or DEFAULT_WEBHOOK_NAME
    return spec


async def get_or_create_webhook(
    channel: Any,
    spec: Mapping[str, Any],
    *,
    reason: Optional[str] = None,
) -> discord.Webhook:
    """
    Liefert den Webhook mit dem gewünschten Namen im Kanal — oder legt ihn an.

    Pro Kanal wird maximal **ein** Webhook pro Persona-Name angelegt und
    wiederverwendet (Discord erlaubt nur ~10 Webhooks pro Kanal).
    """
    if not hasattr(channel, "webhooks") or not hasattr(channel, "create_webhook"):
        raise ApiError.bad_request(
            f"'{getattr(channel, 'name', channel.id)}' unterstützt keine Webhooks.",
            hint="Webhooks gibt es in Text-, Ankündigungs- und Voice-Text-Kanälen.",
            code="CHANNEL_TYPE_MISMATCH",
        )
    wanted = str(spec.get("name") or DEFAULT_WEBHOOK_NAME)
    hooks: List[discord.Webhook] = await guard(channel.webhooks(), action="Webhooks laden")
    for hook in hooks:
        if (hook.name or "").strip() == wanted:
            return hook
    hook = await guard(
        channel.create_webhook(name=wanted, reason=reason or f"Webhook '{wanted}' (Arena AI)"),
        action=f"Webhook '{wanted}' anlegen",
    )
    return hook


async def send_as_webhook(
    channel: Any,
    spec: Mapping[str, Any],
    kwargs: Dict[str, Any],
    *,
    reason: Optional[str] = None,
) -> tuple:
    """
    Sendet ``kwargs`` als Webhook-Persona und gibt ``(message, notes)`` zurück.

    ``username`` und ``avatar_url`` kommen aus dem Spec — dadurch reicht ein
    einziger Webhook für beliebig viele Gesichter pro Kanal.
    """
    hook = await get_or_create_webhook(channel, spec, reason=reason)

    send_kwargs: Dict[str, Any] = {}
    notes: List[str] = []
    for key, value in kwargs.items():
        if key in _WEBHOOK_SEND_FIELDS:
            send_kwargs[key] = value
        else:
            notes.append(f"Webhook-Nachrichten unterstützen '{key}' nicht — Feld wurde verworfen.")
    username = str(spec.get("username") or spec.get("name") or DEFAULT_WEBHOOK_NAME)
    send_kwargs["username"] = parse_str(username, field="webhook.name",
                                        max_length=80, allow_empty=False)
    avatar = spec.get("avatar") or spec.get("avatar_url")
    if avatar:
        avatar_text = parse_str(avatar, field="webhook.avatar", max_length=2048,
                                allow_empty=False)
        if not re.match(r"^(https?://|data:image/)", avatar_text or ""):
            raise ApiError.bad_request(
                "webhook.avatar muss eine http(s)-Bild-URL sein.",
                code="WEBHOOK_AVATAR_INVALID",
            )
        send_kwargs["avatar_url"] = avatar_text

    message = await guard(
        hook.send(**send_kwargs, wait=True),
        action=f"Webhook-Nachricht als '{send_kwargs['username']}' senden",
    )
    return message, notes


# ─────────────────────────────────────────────────────────────────────────────
#  Echte Mentions statt Platzhalter
# ─────────────────────────────────────────────────────────────────────────────


def _replace_channel_mentions(text: str, guild: discord.Guild,
                              keys: Optional[Mapping[str, Any]]) -> str:
    def repl(match: re.Match) -> str:
        ref = match.group("ref")
        if ref.isdigit():
            return match.group(0)  # echte ID → unverändert lassen
        resolved = _try_resolve_channel(guild, ref, keys)
        return f"<#{resolved.id}>" if resolved is not None else match.group(0)

    return _CHANNEL_MENTION_RE.sub(repl, text)


def _replace_role_mentions(text: str, guild: discord.Guild,
                           keys: Optional[Mapping[str, Any]]) -> str:
    def repl(match: re.Match) -> str:
        ref = match.group("ref")
        if ref.isdigit():
            return match.group(0)
        role = _try_resolve_role(guild, ref, keys)
        return f"<@&{role.id}>" if role is not None else match.group(0)

    return _ROLE_MENTION_RE.sub(repl, text)


def _try_resolve_channel(guild: discord.Guild, ref: str,
                         keys: Optional[Mapping[str, Any]]):
    from .channel_ops import resolve_channel_by_ref

    if keys is not None:
        hit = keys.get(ref)
        if hit is not None and hasattr(hit, "id"):
            return hit
    try:
        return resolve_channel_by_ref(guild, ref)
    except ApiError:
        return None


def _try_resolve_role(guild: discord.Guild, ref: str,
                      keys: Optional[Mapping[str, Any]]):
    if keys is not None:
        hit = keys.get(ref)
        if isinstance(hit, discord.Role):
            return hit
    try:
        from ..util import resolve_role

        return resolve_role(guild, ref)
    except ApiError:
        return None


def resolve_mention_placeholders(
    text: Optional[str], guild: discord.Guild,
    keys: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """
    Ersetzt ``<#key-oder-name>`` / ``<@&key-oder-name>`` durch echte Mentions.

    ``<#regeln>`` wird im Setup-Kontext z. B. zu ``<#123456789012345678>``, weil
    ``regeln`` ein Kanal-Key des Plans ist. Nicht auflösbare Referenzen bleiben
    unverändert — die API rät nie eine ID, sondern liefert sie zurück, damit der
    Absender (KI) den Fehler sieht.
    """
    if not text:
        return text
    result = _replace_channel_mentions(text, guild, keys)
    result = _replace_role_mentions(result, guild, keys)
    return result


def resolve_placeholders_deep(value: Any, guild: discord.Guild,
                              keys: Optional[Mapping[str, Any]] = None) -> Any:
    """Wendet :func:`resolve_mention_placeholders` auf alle Strings tief an."""
    if isinstance(value, str):
        return resolve_mention_placeholders(value, guild, keys)
    if isinstance(value, dict):
        return {k: resolve_placeholders_deep(v, guild, keys) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_placeholders_deep(item, guild, keys) for item in value]
    return value
