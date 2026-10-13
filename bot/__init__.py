"""
AIDiscordServerEinrichten
=========================

Ein Discord-Bot, der genau einen Job hat: einem Server-Administrator per
Slash-Command einen **Link + Token** (nur für ihn sichtbar) auszugeben.

Dieser Link + Token schaltet eine REST-API frei, über die eine externe KI
(z. B. Arena AI) den kompletten Discord-Server als Bot mit Administrator-
Rechten lesen und umbauen kann — ohne jemals den eigentlichen Bot-Token
zu Gesicht zu bekommen.

Module
------
config        Umgebungsvariablen → ``Config``
util          Snowflakes, Farben, Permissions, JSON-Helfer
sessions      Session-/Token-Verwaltung (gehasht, mit TTL, Scope, Persistenz)
serializers   discord.py-Objekte → JSON-sichere Dictionaries
prompt        Erzeugt den fertigen Arena-AI-Prompt
discord_bot   Der Discord-Client + Slash-Commands + Buttons
web           aiohttp-Server (REST-API, Console, Healthcheck)
main          Verdrahtet alles und startet Bot + Web im selben Event-Loop
"""

__version__ = "1.0.5"
__botname__ = "AIDiscordServerEinrichten"
__author__ = "mertastudios"
