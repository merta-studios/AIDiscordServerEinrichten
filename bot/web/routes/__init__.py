"""Importiert alle Route-Module, damit sich die Endpoints registrieren."""

from __future__ import annotations

from . import (  # noqa: F401  (Import erzeugt die Registrierung)
    channels,
    events,
    expressions,
    guides,
    guild,
    invites,
    members,
    messages,
    meta,
    moderation,
    roles,
    setup,
    webhooks,
)

__all__ = [
    "meta",
    "guild",
    "channels",
    "roles",
    "members",
    "moderation",
    "messages",
    "webhooks",
    "invites",
    "expressions",
    "events",
    "guides",
    "setup",
]
