"""
Fertige Anleitungen („Guides”), die Arena AI 1:1 an den Nutzer weitergeben kann.

Warum ein eigener Endpoint? Die KI stößt bei manchen Wünschen an echte Grenzen
— der klassische Fall sind **Self Roles / Reaction Roles**: Das Einrichten
erfolgt über Drittanbieter-Bots, und ein Bot darf die Konfiguration eines
anderen Bots nicht anfassen. Genau dann braucht es nicht Ausreden, sondern
eine ehrliche Erklärung **plus** eine Schritt-für-Schritt-Anleitung, die der
Nutzer selbst ausführen kann.

Die Guides sind bewusst als Daten (statt als Prompt-Text) hinterlegt:

* ``GET /api/v1/guides`` listet die Themen,
* ``GET /api/v1/guides/{topic}`` liefert den ausformulierten Text,
  den die KI in den Kanal (oder als Chat-Antwort) übernehmen und an den
  konkreten Server anpassen darf.

Alle Texte sind Deutsch, in Discord-Markdown formuliert und mit konkreten
Kanal-/Rollen-Platzhaltern versehen, die die KI gegen echte IDs auflöst.
"""

from __future__ import annotations

from typing import Any, Dict

from ...util import ApiError
from ..registry import route

__all__ = ("GUIDES",)


def _self_roles_guide() -> Dict[str, Any]:
    return {
        "titel": "Self Roles / Reaction Roles einrichten",
        "beschreibung": (
            "Self Roles (Reaction Roles) lassen sich nicht von diesem Bot "
            "automatisch einrichten, da ein Bot die Konfiguration eines anderen "
            "Bots nicht anfassen darf. Verwende stattdessen einen "
            "Drittanbieter-Rollenbot oder Discord-Onboarding."
        ),
        "wichtig_fuer_ki": [
            "Bots dürfen andere Bots nicht konfigurieren — für Self Roles einen Drittanbieter-Bot empfehlen oder diese Anleitung weitergeben.",
            "Alternativ: Discord-Onboarding auf Community-Servern nutzen: PATCH /api/v1/guild/onboarding.",
        ],
        "schritte_nutzer": [
            "Einen Rollenbot wählen (z. B. Carl-bot, Dyno oder MEE6).",
            "Den Bot zum Server einladen und Rollen + Reaktionen konfigurieren.",
            "Alternativ: Discord-Onboarding unter Servereinstellungen → Onboarding einrichten.",
        ],
        "bot_anleitungen": {
            "carl-bot": "/reactionrole create",
            "dyno": "web.dyno.gg → Modules → Reaction Roles",
            "mee6": "mee6.xyz/dashboard → Reaction Roles",
        },
        "was_die_ki_ubernehmen_kann": [
            "Rollen anlegen: POST /api/v1/roles (Name, Farbe, Rechte, Icon)",
            "Kanal 'rollen-wahl' anlegen + @everyone Schreibrechte entziehen",
            "Übersichts-Nachricht posten (Webhook-Persona)",
            "Discord-Onboarding einrichten: PATCH /api/v1/guild/onboarding",
        ],
    }


def _design_guide() -> Dict[str, Any]:
    return {
        "titel": "Unicode-Design-System für Kanäle, Kategorien & Rollen",
        "beschreibung": (
            "Stilvolle Kanalnamen mit Unicode-Zeichen statt langweiliger "
            "Kleinbuchstaben-Kanäle."
        ),
        "regeln": [
            "Text-Kanäle: Discord erzwingt Kleinbuchstaben und wandelt Leerzeichen "
            "in Bindestriche — Unicode-Zeichen und Emojis bleiben aber erhalten.",
            "Kategorien & Voice-Kanäle: erlauben Großbuchstaben und Leerzeichen.",
            "Ein Stil pro Server — nicht mischen. Emojis zusätzlich zum Symbol "
            "sparen Platz und bleiben auch dann lesbar, wenn Symbole fehlen.",
            " Weniger ist mehr: 1 Symbol + Trennzeichen + Begriff reicht; "
            "Rollen-/Kanallisten müssen scannbar bleiben.",
        ],
        "stile": {
            "klammern": {
                "beispiel_kategorie": "「📌」 INFORMATION",
                "beispiel_kanal": "「✦」regeln",
                "beispiel_voice": "「🎧」 Lounge",
                "beispiel_rolle": "「👑」 Owner",
                "zeichen": "「 」",
            },
            "trennpunkt": {
                "beispiel_kategorie": "📌・INFORMATION",
                "beispiel_kanal": "✦・regeln",
                "beispiel_voice": "🎧・Lounge",
                "beispiel_rolle": "⭐・VIP",
                "zeichen": "・(katakana middle dot)",
            },
            "rahmen": {
                "beispiel_kategorie": "▁▁▁ 📌 REGELN ▁▁▁",
                "beispiel_kanal": "▬▬inline-regeln",
                "beispiel_voice": "╭──── Lounge ────╮",
                "beispiel_rolle": "꒰ 👑 ꒱ Owner",
                "zeichen": "▁ ▬ ╭ ╮ ꒰ ꒱",
            },
            "symbole": {
                "beispiel_kanal": "✧ regeln · ✧ infos · ✧ willkommen",
                "beispiel_kategorie": "❖ COMMUNITY ❖",
                "beispiel_rolle": "★ Gamer ★",
                "zeichen": "✧ ✦ ❖ ★ ☆ ✵ ♡ ➤",
            },
            "spezialbuchstaben": {
                "fullwidth": "ｒｅｇｅｌｎ · ｃｈａｔ · ｉｎｆｏｓ",
                "smallcaps": "ʀᴇɢᴇʟɴ · ᴄʜᴀᴛ · ɪɴꜰᴏꜱ",
                "hochgestellt": "ʳᵉᵍᵉˡⁿ · ᶜʰᵃᵗ",
                "fett": "𝗋𝖾𝗀𝖾𝗅𝗇 (mathematische Buchstaben)",
                "hinweis": "Für Akzente & einzelne Bereiche — ganze Server nur in "
                           "Smallcaps werden schwer lesbar und schwer zu erwähnen.",
            },
            "trennlinien": {
                "kanal": "┃ infos · ┃ regeln",
                "kategorie": "═══ ⚡ SPIELE ═══",
                "zeichen": "┃ ═ ══ ─ ┄ ┈ » «",
            },
        },
        "komplett_beispiel": {
            "kategorien": ["「📌」 INFORMATION", "「💬」 COMMUNITY", "「🎮」 SPIELE", "「🔒」 TEAM"],
            "text_kanaele": ["「✦」regeln", "「✦」ankündigungen", "「✦」rollen-wahl",
                             "✦・allgemein", "✦・memes", "✦・clips"],
            "voice_kanaele": ["「🎧」 Lounge", "「🎧」 Gaming", "「🎧」 AFK"],
            "rollen": ["「👑」 Owner", "「🛡️」 Mod", "「⭐」 VIP", "「🎮」 Gamer"],
        },
        "hinweise": [
            "Vorsicht bei kombinierten Unicode-Buchstaben (fett/smallcaps) in Kanalnamen "
            "von Textkanälen: Sie bleiben erhalten, aber Discord-Suche/Autovervollständigung "
            "findet sie schlechter — deshalb für Textkanäle lieber Symbol-Stile.",
            "Voice-Kanal-Status (Boost) und Kanal-Themen sind der richtige Ort für "
            "Verzierungen, wenn der Name clean bleiben soll.",
        ],
    }


def _branding_guide() -> Dict[str, Any]:
    return {
        "titel": "Branding-Paket: Server-Bilder & Bot-Profil",
        "beschreibung": (
            "Alles Optische in einem Durchgang: Server-Icon, Banner, Splash, "
            "Bot-Avatar pro Server, Bot-Nickname, Rollenfarben & -Icons, "
            "Webhook-Avatare."
        ),
        "endpoints": {
            "server_icon": 'PATCH /api/v1/guild {"icon": "https://…/icon.png"}',
            "server_banner": 'PATCH /api/v1/guild {"banner": "https://…/banner.png"} (Boost-Stufe 2)',
            "invite_splash": 'PATCH /api/v1/guild {"splash": "https://…/splash.png"} (Boost-Stufe 1)',
            "bot_avatar_dieser_server": 'PATCH /api/v1/members/me {"avatar": "https://…"}',
            "bot_nickname": 'PATCH /api/v1/members/me {"nick": "✨ Server-Assistent"}',
            "bot_bio": 'PATCH /api/v1/members/me {"bio": "…"}',
            "rollen_farbe_icon": 'PATCH /api/v1/roles/{id} {"color": "#E74C3C", "icon": "🛡️"}',
            "webhook_avatar": 'PATCH /api/v1/webhooks/{id} {"avatar": "https://…"}',
            "webhook_persona_pro_nachricht": '{"webhook": {"name": "📜 Regeln", "avatar": "https://…"}}',
        },
        "checkliste": [
            "Server-Icon: quadratisch, ≥512×512, PNG (wird rund angezeigt)",
            "Banner: 960×540 (16:9), Boost-Stufe 2 nötig",
            "Splash (Invite-Hintergrund): 1920×1080, Boost-Stufe 1",
            "Bot-Avatar pro Server — unabhängig vom globalen Bot-Bild",
            "Einheitliche Farbpalette für Rollen (z. B. Rot=Trial, Blau=Team, "
            "Grün=Mitglied, Grau=Bot)",
            "Rollen-Icons (Emoji oder PNG) für Premium-Look",
            "Webhook-Personen mit passenden Avataren für Regeln/News/Willkommen",
        ],
        "hinweis": "Bilder dürfen auch als Data-URI (data:image/png;base64,…) übergeben werden.",
    }


def _webhook_guide() -> Dict[str, Any]:
    return {
        "titel": "Webhook-Personen: Nachrichten mit eigenem Namen & Avatar",
        "beschreibung": (
            "Warum Webhooks statt Bot-Nachrichten und wie der Persona-Werkfluss aussieht."
        ),
        "warum": [
            "Bot-Nachrichten zeigen immer den Bot-Namen — der Kanal wirkt technisch.",
            "Webhook-Nachrichten zeigen beliebigen Namen + Avatar: '📜 Serverregeln', "
            "'🛡️ Moderation', '🎉 Events' — der Server wirkt kuratiert.",
            "Ein Webhook pro Kanal genügt: Name/Avatar lassen sich pro Nachricht übersteuern.",
            "Webhook-Nachrichten sind editier-/löschbar wie normale Nachrichten.",
        ],
        "werkfluss": [
            'Kurzel: POST /api/v1/channels/{id}/messages mit {"webhook": {"name": "📜 Serverregeln", '
            '"avatar": "https://…/regeln.png"}} — legt bei Bedarf automatisch einen Webhook an.',
            "Alternative: POST /api/v1/webhooks/{id}/send mit username/avatar pro Nachricht.",
            "Avatar dauerhaft für alle Nachrichten: PATCH /api/v1/webhooks/{id} {\"avatar\": …}.",
            "Vorhandene Personen: GET /api/v1/webhooks — wiederverwenden statt neu anlegen "
            "(Limit 10 pro Kanal).",
        ],
        "grenzen": [
            "Keine Umfragen-Threads, keine Sticker, kein Antwort-Bezug (reply) bei Webhooks — "
            "solche Features braucht der seltene Fall, dann normal senden.",
            "Webhooks zählen zum Kanal-Limit (10) — Persona-Pattern nutzt deshalb einen pro Kanal.",
        ],
    }


def _security_guide() -> Dict[str, Any]:
    return {
        "titel": "Sicherheit & Anti-Raid-Paket",
        "beschreibung": "Moderatives Grundgerüst, das praktisch jeder Server haben sollte.",
        "checkliste": [
            'Verifizierungsstufe: PATCH /api/v1/guild {"verification_level": "medium"}',
            'Inhaltsfilter: PATCH /api/v1/guild {"explicit_content_filter": "all_members"}',
            "AutoMod: Werbung blockieren (discord.gg-Regex), Beleidigungs-Presets, "
            "Mention-Spam-Limit 6 → POST /api/v1/automod/rules",
            "Muted-Rolle ohne Schreibrechte + passender Overwrite in neuen Kanälen",
            "Team-Kategorie nur für Mod-Rollen sichtbar (@everyone deny view_channel)",
            "Regeln-Kanal: @everyone deny send_messages — nur Team darf posten",
            "Mod-Log-Kanal für AutoMod-Alerts (send_alert_message)",
            "Invite-Hygiene: GET /api/v1/invites → alte/unkontrollierte Links löschen",
        ],
    }


GUIDES: Dict[str, Dict[str, Any]] = {
    "self-roles": _self_roles_guide(),
    "design": _design_guide(),
    "branding": _branding_guide(),
    "webhooks": _webhook_guide(),
    "security": _security_guide(),
}


@route(
    "GET", "/api/v1/guides", scope="read", tags=("guides",),
    summary="Fertige Anleitungen (Self Roles, Design, Branding, Webhooks, Security)",
    description="Ausformulierte deutsche Guides, die 1:1 an den Nutzer weitergegeben "
                "werden dürfen — inkl. der ehrlichen Grenzen ('Bots dürfen andere "
                "Bots nicht konfigurieren') und was die KI stattdessen übernehmen kann.",
)
async def list_guides(ctx) -> Dict[str, Any]:
    return {
        "count": len(GUIDES),
        "guides": [
            {"topic": key, "titel": guide.get("titel"), "beschreibung": guide.get("beschreibung")}
            for key, guide in sorted(GUIDES.items())
        ],
        "usage": "GET /api/v1/guides/self-roles → Text anpassen (echte IDs, echte Rollen) "
                 "→ als Webhook-Persona im Kanal posten.",
    }


@route(
    "GET", "/api/v1/guides/{topic}", scope="read", tags=("guides",),
    summary="Eine Anleitung im Detail",
    description="Der Inhalt darf und soll vom Nutzer sichtbar weiterverwendet werden: "
                "an den Server anpassen (echte Kanal-/Rollen-IDs einsetzen!) und "
                "als Nachricht posten.",
)
async def get_guide(ctx) -> Dict[str, Any]:
    topic = (ctx.path("topic") or "").strip().lower()
    aliases = {"unicode": "design", "design-unicode": "design", "styling": "design",
               "selfroles": "self-roles", "reaction-roles": "self-roles",
               "reactionroles": "self-roles", "branding-paket": "branding",
               "security-hardening": "security"}
    topic = aliases.get(topic, topic)
    if topic not in GUIDES:
        raise ApiError.not_found(
            f"Anleitung '{topic}' gibt es nicht.",
            hint="Verfügbar: " + ", ".join(sorted(GUIDES)),
            code="GUIDE_NOT_FOUND",
        )
    return {"topic": topic, **GUIDES[topic]}
