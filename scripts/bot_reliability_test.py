"""Offline regressions: python -m unittest discover -s scripts -p bot_reliability_test.py."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord
from bot.discord_bot import RelayClient, FeedbackModal, welcome_view, admin_panel_view
from bot.web.app import AppState


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_views_are_not_retained(self):
        client = discord.Client(intents=discord.Intents.none())
        await client._async_setup_hook()
        for i in range(1000):
            view = welcome_view()
            # Same storage path used when sending messages in discord.py.
            if view.is_dispatchable():
                client._connection.store_view(view, i)
        self.assertEqual(len(client._connection._view_store._views), 0)
        panel = admin_panel_view([], page=0, query="", total_guilds=0)
        self.assertFalse(panel.is_dispatchable())
        self.assertIn('relay:admin:restart:', str(panel.to_components()))
        await client.close()

    async def test_connect_button(self):
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        client = SimpleNamespace(
            _guard=AsyncMock(return_value={'guild': object(), 'member': object()}),
            _new_session=AsyncMock(return_value=(object(), 'secret')),
            _show=AsyncMock(), state=SimpleNamespace(base_url=lambda: 'https://example.com'),
        )
        await RelayClient._handle_connect(client, interaction)
        interaction.response.defer.assert_awaited_once()
        client._show.assert_awaited_once()

    async def test_restart_owner_gate_and_confirmation(self):
        state = SimpleNamespace(restart_requested=False, stop_event=asyncio.Event())
        client = SimpleNamespace(config=SimpleNamespace(bot_owner_id=42), state=state, _show=AsyncMock())
        interaction = SimpleNamespace(guild=None, user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=AsyncMock()))
        await RelayClient._handle_admin_button(client, interaction, 'relay:admin:restart_confirm:0:')
        self.assertFalse(state.stop_event.is_set())
        interaction.user.id = 42
        await RelayClient._handle_admin_button(client, interaction, 'relay:admin:restart:0:')
        self.assertFalse(state.stop_event.is_set())
        client._show.assert_awaited_once()
        await RelayClient._handle_admin_button(client, interaction, 'relay:admin:restart_confirm:0:')
        self.assertTrue(state.restart_requested)
        self.assertTrue(state.stop_event.is_set())

    async def test_feedback_delivery(self):
        owner = SimpleNamespace(send=AsyncMock())
        client = SimpleNamespace(config=SimpleNamespace(bot_owner_id=42), get_user=lambda _: owner)
        modal = FeedbackModal(client)
        modal.feedback._value = 'Super Bot!'
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), user=SimpleNamespace(id=7), guild='Test', guild_id=99)
        await modal.on_submit(interaction)
        owner.send.assert_awaited_once()
        self.assertIn('Danke', interaction.followup.send.call_args.args[0])

    async def test_feedback_failure_does_not_say_thanks(self):
        owner = SimpleNamespace(send=AsyncMock(side_effect=TimeoutError))
        client = SimpleNamespace(config=SimpleNamespace(bot_owner_id=42), get_user=lambda _: owner)
        modal = FeedbackModal(client)
        modal.feedback._value = 'Test'
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), user=SimpleNamespace(id=7), guild='Test', guild_id=99)
        await modal.on_submit(interaction)
        self.assertIn('nicht zugestellt', interaction.followup.send.call_args.args[0])

    async def test_buttons_deny_non_admin(self):
        client = SimpleNamespace(_guard=AsyncMock(return_value=None), _new_session=AsyncMock())
        for method in (RelayClient._handle_connect, RelayClient._handle_regenerate, RelayClient._handle_revoke_all):
            await method(client, object())
        client._new_session.assert_not_awaited()

    async def test_rate_bucket_cleanup(self):
        state = SimpleNamespace(config=SimpleNamespace(api_rate_window=60), _rate={'old': [0]})
        AppState.prune_rate_limits(state)
        self.assertEqual(state._rate, {})
