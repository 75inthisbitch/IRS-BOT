import asyncio
import threading
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import discord

import core
from bot import (
    BotRunner,
    COMPANY_CHANNEL_LAYOUT,
    ConfirmActionView,
    GuildCommandSyncError,
    TaxBot,
    build_active_penalties_embed,
    build_bot,
    build_company_board_embed,
    build_help_embed,
    build_penalty_csv,
    build_penalty_embed,
    build_tax_audit_embed,
    build_tax_result_embed,
    build_unverified_tax_embed,
    build_weekly_penalty_summary_embed,
    PresentationConfirmView,
    PresentationStepView,
    PaymentReviewView,
)


class GuildCommandSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_tax_bot_close_cancels_background_loops_before_client_close(self):
        bot = TaxBot()
        cancelled = asyncio.Event()

        async def background_task():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(background_task())
        await asyncio.sleep(0)

        class BackgroundLoop:
            def is_running(self):
                return not task.done()

            def cancel(self):
                task.cancel()

            def get_task(self):
                return task

        bot.background_loops.append(BackgroundLoop())
        with patch.object(discord.Client, "close", new=AsyncMock()) as client_close:
            await bot.close()

        client_close.assert_awaited_once()
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        self.assertTrue(cancelled.is_set())

    async def test_runner_shutdown_cancels_and_awaits_remaining_asyncio_tasks(self):
        runner = BotRunner("unused")
        cancelled = asyncio.Event()

        async def pending_task():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(pending_task())
        await asyncio.sleep(0)

        await runner._shutdown_loop(asyncio.get_running_loop())

        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        self.assertTrue(cancelled.is_set())

    def test_runner_stop_waits_for_bot_and_thread_cleanup(self):
        started = threading.Event()
        stopped = threading.Event()
        background_cancelled = threading.Event()

        class FakeBot:
            def __init__(self):
                self.closed = False
                self.stop_event = None

            async def start(self, _token):
                self.stop_event = asyncio.Event()
                started.set()

                async def background_task():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        background_cancelled.set()

                asyncio.create_task(background_task())
                await self.stop_event.wait()

            async def close(self):
                self.closed = True
                if self.stop_event is not None:
                    self.stop_event.set()

            def is_closed(self):
                return self.closed

        fake_bot = FakeBot()
        runner = BotRunner("unused", on_stopped=stopped.set)
        with patch("bot.build_bot", return_value=fake_bot):
            runner.start()
            self.assertTrue(started.wait(timeout=2))
            runner.stop()
            runner.thread.join(timeout=2)

        self.assertFalse(runner.thread.is_alive())
        self.assertTrue(fake_bot.closed)
        self.assertTrue(background_cancelled.is_set())
        self.assertTrue(stopped.is_set())

    async def test_confirmation_handles_deleted_original_message(self):
        action = AsyncMock(return_value="Prezentacja zakończona.")
        view = ConfirmActionView(123, action)
        not_found = discord.NotFound(
            cast(Any, SimpleNamespace(status=404, reason="Not Found")),
            {"code": 10008, "message": "Unknown Message"},
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(side_effect=not_found),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await view.children[0].callback(cast(discord.Interaction, interaction))

        action.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "Prezentacja zakończona.",
            ephemeral=True,
        )

    async def test_missing_guild_access_has_actionable_error(self):
        bot = TaxBot(guild_id="123456789")
        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50001, "message": "Missing Access"},
        )

        with patch.object(bot.tree, "sync", new=AsyncMock(side_effect=forbidden)):
            with self.assertRaisesRegex(GuildCommandSyncError, "ID serwera"):
                await bot.setup_hook()

    async def test_other_forbidden_errors_are_not_rewritten(self):
        bot = TaxBot(guild_id="123456789")
        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50013, "message": "Missing Permissions"},
        )

        with patch.object(bot.tree, "sync", new=AsyncMock(side_effect=forbidden)):
            with self.assertRaises(discord.Forbidden) as raised:
                await bot.setup_hook()

        self.assertEqual(raised.exception.code, 50013)

    def test_company_commands_are_registered(self):
        bot = build_bot()
        names = {command.name for command in bot.tree.get_commands()}

        self.assertTrue(
            {
                "podatek",
                "pomoc",
                "aktywna",
                "nieaktywna",
                "usun_firme",
                "usun_kare",
                "ustaw_kanal_firm",
                "przypomnienie",
                "kara_finansowa",
                "oplac_kare",
                "rejestr_kar",
                "audyt",
                "ulga",
                "muala",
                "prezentacja",
                "zakoncz_prezentacje",
                "eksport_kar",
                "ustaw_role_irs",
                "szkic",
                "szkic2",
                "wyczysc",
                "wyczysc_tabele",
            }
            <= names
        )
        payment_command = bot.tree.get_command("oplac_kare")
        self.assertIsNotNone(payment_command)
        self.assertEqual(
            {parameter.name for parameter in payment_command.parameters},
            {"kara_id", "screen", "profil_ocr"},
        )
        tax_command = bot.tree.get_command("podatek")
        self.assertIsNotNone(tax_command)
        self.assertEqual(
            {parameter.name for parameter in tax_command.parameters},
            {"screen", "publicznie", "profil_ocr"},
        )

    async def test_szkic2_creates_private_channels_and_connects_board_and_logs(self):
        bot = build_bot()
        command = bot.tree.get_command("szkic2")
        self.assertIsNotNone(command)

        everyone = Mock(spec=discord.Role)
        everyone.id = 1
        guild_member = Mock(spec=discord.Member)
        guild_member.id = 50
        guild = SimpleNamespace(
            id=5,
            roles=[everyone],
            categories=[],
            text_channels=[],
            default_role=everyone,
            me=guild_member,
        )

        async def create_role(*, name, mentionable):
            role = Mock(spec=discord.Role)
            role.id = len(guild.roles) + 1
            role.name = name
            guild.roles.append(role)
            return role

        async def create_category(name, overwrites=None):
            category = Mock(spec=discord.CategoryChannel)
            category.id = len(guild.categories) + 100
            category.name = name
            category.overwrites = overwrites
            category.channels = []
            category.text_channels = []
            category.edit = AsyncMock()
            guild.categories.append(category)
            return category

        async def create_text_channel(name, category=None):
            channel = Mock(spec=discord.TextChannel)
            channel.id = len(guild.text_channels) + 200
            channel.name = name
            channel.category = category
            channel.permissions_for.return_value = SimpleNamespace(
                view_channel=True,
                send_messages=True,
                embed_links=True,
            )
            channel.set_permissions = AsyncMock()
            guild.text_channels.append(channel)
            if category is not None:
                category.channels.append(channel)
                category.text_channels.append(channel)
            return channel

        guild.create_role = AsyncMock(side_effect=create_role)
        guild.create_category = AsyncMock(side_effect=create_category)
        guild.create_text_channel = AsyncMock(side_effect=create_text_channel)
        interaction = SimpleNamespace(
            guild=guild,
            permissions=SimpleNamespace(administrator=True),
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await command.callback(interaction)
        view = interaction.response.send_message.await_args.kwargs["view"]
        confirmation = SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with (
            patch("bot.core.load_config", return_value={"audit_channel_id": ""}),
            patch("bot.core.save_config") as save_config,
            patch("bot.core.set_board_channel") as set_board_channel,
            patch(
                "bot.refresh_company_board",
                new=AsyncMock(return_value=True),
            ) as refresh_board,
            patch("bot.core.record_admin_audit"),
        ):
            await view.children[0].callback(confirmation)

        self.assertEqual(
            {role.name for role in guild.roles if role.name in {"IRS", "DOJ"}},
            {"IRS", "DOJ"},
        )
        self.assertEqual(
            {channel.name for channel in guild.text_channels},
            {"tabela", "logi", "chat"},
        )
        category = guild.categories[0]
        self.assertEqual(category.name, "DOJ")
        category_overwrites = guild.create_category.await_args.kwargs["overwrites"]
        self.assertFalse(category_overwrites[everyone].view_channel)
        irs_role = next(role for role in guild.roles if role.name == "IRS")
        doj_role = next(role for role in guild.roles if role.name == "DOJ")
        self.assertTrue(category_overwrites[irs_role].view_channel)
        self.assertFalse(category_overwrites[irs_role].send_messages)
        chat_channel = next(
            channel for channel in guild.text_channels if channel.name == "chat"
        )
        self.assertEqual(chat_channel.set_permissions.await_count, 2)
        self.assertTrue(
            chat_channel.set_permissions.await_args_list[0].kwargs["send_messages"]
        )
        table_channel = next(
            channel for channel in guild.text_channels if channel.name == "tabela"
        )
        logs_channel = next(
            channel for channel in guild.text_channels if channel.name == "logi"
        )
        save_config.assert_called_once()
        self.assertEqual(
            save_config.call_args.args[0]["audit_channel_id"],
            str(logs_channel.id),
        )
        self.assertEqual(bot.audit_channel_id, logs_channel.id)
        set_board_channel.assert_called_once_with(guild.id, table_channel.id)
        refresh_board.assert_awaited_once_with(bot, guild.id)
        self.assertIn("podłączona do tablic firm i kar", confirmation.edit_original_response.await_args.kwargs["content"])

    async def test_szkic_requires_admin_and_confirms_before_creating_channels(self):
        bot = build_bot()
        command = bot.tree.get_command("szkic")
        self.assertIsNotNone(command)
        guild = SimpleNamespace(id=5)
        denied = SimpleNamespace(
            guild=guild,
            permissions=SimpleNamespace(administrator=False),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await command.callback(denied)
        self.assertIn(
            "tylko administratorzy",
            denied.response.send_message.await_args.args[0],
        )

        allowed = SimpleNamespace(
            guild=guild,
            permissions=SimpleNamespace(administrator=True),
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await command.callback(allowed)
        prompt = allowed.response.send_message.await_args.args[0]
        self.assertIn("czterech screenów", prompt)
        self.assertIsInstance(
            allowed.response.send_message.await_args.kwargs["view"],
            ConfirmActionView,
        )
        self.assertEqual(len(COMPANY_CHANNEL_LAYOUT), 15)

    async def test_clear_command_requires_manage_messages_and_confirmation(self):
        bot = build_bot()
        command = bot.tree.get_command("wyczysc")
        self.assertIsNotNone(command)
        bot._connection.user = SimpleNamespace(id=50)
        channel = Mock(spec=discord.TextChannel)
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            manage_messages=True,
        )
        channel.mention = "#firma"
        channel.name = "firma"
        channel.purge = AsyncMock(return_value=[SimpleNamespace(id=1)])
        denied = SimpleNamespace(
            guild=SimpleNamespace(id=5, me=SimpleNamespace(id=1)),
            permissions=SimpleNamespace(manage_messages=False),
            channel=channel,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await command.callback(denied)
        self.assertIn(
            "Zarządzanie wiadomościami",
            denied.response.send_message.await_args.args[0],
        )
        self.assertTrue(denied.response.send_message.await_args.kwargs["ephemeral"])

        allowed = SimpleNamespace(
            guild=SimpleNamespace(id=5, me=SimpleNamespace(id=1)),
            permissions=SimpleNamespace(manage_messages=True),
            channel=channel,
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await command.callback(allowed)
        prompt = allowed.response.send_message.await_args.args[0]
        self.assertIn("wszystkie wiadomości bota", prompt)
        view = allowed.response.send_message.await_args.kwargs["view"]
        self.assertIsInstance(view, ConfirmActionView)
        confirmation = SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        bot_message = SimpleNamespace(author=SimpleNamespace(id=50), pinned=True)
        member_message = SimpleNamespace(author=SimpleNamespace(id=60), pinned=False)
        with patch("bot.core.record_admin_audit"):
            await view.children[0].callback(confirmation)

        channel.purge.assert_awaited_once()
        purge_options = channel.purge.await_args.kwargs
        self.assertIsNone(purge_options["limit"])
        self.assertTrue(purge_options["check"](bot_message))
        self.assertFalse(purge_options["check"](member_message))
        self.assertIn("Usunięto **1**", confirmation.edit_original_response.await_args.kwargs["content"])

    async def test_clear_table_removes_only_published_board_messages(self):
        bot = build_bot()
        command = bot.tree.get_command("wyczysc_tabele")
        self.assertIsNotNone(command)
        guild = SimpleNamespace(id=5, me=SimpleNamespace(id=50))
        channel = Mock(spec=discord.TextChannel)
        channel.id = 10
        channel.guild = guild
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True,
            read_message_history=True,
        )
        company_message = SimpleNamespace(delete=AsyncMock())
        penalty_message = SimpleNamespace(delete=AsyncMock())
        channel.fetch_message = AsyncMock(
            side_effect=[company_message, penalty_message]
        )
        interaction = SimpleNamespace(
            guild=guild,
            permissions=SimpleNamespace(administrator=True),
            user=SimpleNamespace(id=15),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with (
            patch.object(bot, "get_channel", return_value=channel),
            patch("bot.core.get_board", return_value=(10, 100)),
            patch("bot.core.get_penalty_board_message", return_value=(10, 200)),
            patch("bot.core.clear_board_message", return_value=True) as clear_company,
            patch(
                "bot.core.clear_penalty_board_message",
                return_value=True,
            ) as clear_penalties,
            patch("bot.core.record_admin_audit"),
        ):
            await command.callback(interaction)
            prompt = interaction.response.send_message.await_args.args[0]
            self.assertIn("Nie skasuję firm, kar", prompt)
            self.assertIsInstance(
                interaction.response.send_message.await_args.kwargs["view"],
                ConfirmActionView,
            )
            view = interaction.response.send_message.await_args.kwargs["view"]
            confirmation = SimpleNamespace(
                user=SimpleNamespace(id=15),
                response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock(),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            await view.children[0].callback(confirmation)

        company_message.delete.assert_awaited_once()
        penalty_message.delete.assert_awaited_once()
        clear_company.assert_called_once_with(5, 100)
        clear_penalties.assert_called_once_with(5, 200)
        result = confirmation.edit_original_response.await_args.kwargs["content"]
        self.assertIn("Wyczyszczono **2**", result)
        self.assertIn("Dane firm i kar", result)

    async def test_configure_permission_role_updates_only_selected_role(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                core.set_guild_role_settings(1, 100, 200)
                bot = build_bot()
                command = bot.tree.get_command("ustaw_role_irs")
                self.assertIsNotNone(command)
                response = SimpleNamespace(send_message=AsyncMock())
                guild = SimpleNamespace(id=1)
                role = SimpleNamespace(id=300, guild=guild, is_default=lambda: False, mention="<@&300>")
                interaction = SimpleNamespace(
                    guild=guild,
                    permissions=SimpleNamespace(administrator=True),
                    user=SimpleNamespace(id=10),
                    response=response,
                )

                await command.callback(
                    interaction,
                    discord.app_commands.Choice(name="Zarządzanie karami", value="penalties"),
                    role,
                )

                self.assertEqual(core.get_guild_role_settings(1), (100, 300))
                self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])

    async def test_penalty_management_role_grants_register_access(self):
        bot = build_bot()
        command = bot.tree.get_command("rejestr_kar")
        self.assertIsNotNone(command)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            permissions=SimpleNamespace(administrator=False),
            user=SimpleNamespace(
                id=10,
                roles=[SimpleNamespace(id=200)],
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(core, "get_guild_role_settings", return_value=(100, 200)), patch.object(
            core,
            "list_financial_penalties",
            return_value=[],
        ):
            await command.callback(interaction)

        self.assertIn(
            "Rejestr kar jest pusty",
            interaction.response.send_message.await_args.args[0],
        )

    async def test_payment_review_button_allows_submitter_and_verifier_role(self):
        submitter_action = AsyncMock(return_value="Wpłata potwierdzona.")
        view = PaymentReviewView(
            10,
            submitter_action,
            authorized_role_id=200,
        )
        submitter = SimpleNamespace(
            user=SimpleNamespace(id=10, roles=[], guild_permissions=None),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await view.children[0].callback(submitter)

        submitter_action.assert_awaited_once()
        submitter.edit_original_response.assert_awaited_once()

        reviewer_action = AsyncMock(return_value="Wpłata potwierdzona.")
        reviewer_view = PaymentReviewView(
            10,
            reviewer_action,
            authorized_role_id=200,
        )
        reviewer = SimpleNamespace(
            user=SimpleNamespace(
                id=20,
                roles=[SimpleNamespace(id=200)],
                guild_permissions=SimpleNamespace(administrator=False),
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await reviewer_view.children[0].callback(reviewer)

        reviewer_action.assert_awaited_once()
        reviewer.edit_original_response.assert_awaited_once()

        unauthorized_response = SimpleNamespace(send_message=AsyncMock())
        unauthorized_interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=30,
                roles=[],
                guild_permissions=SimpleNamespace(administrator=False),
            ),
            response=unauthorized_response,
        )
        protected_view = PaymentReviewView(
            10,
            AsyncMock(return_value="Wpłata potwierdzona."),
            authorized_role_id=200,
        )
        await protected_view.children[0].callback(unauthorized_interaction)
        unauthorized_response.send_message.assert_awaited_once()

    async def test_regular_member_can_submit_tax_screenshot(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                bot = build_bot()
                bot.audit_channel_id = 99
                audit_channel = Mock(spec=discord.TextChannel)
                audit_channel.guild.me = SimpleNamespace(id=50)
                audit_channel.permissions_for.return_value = SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                )
                audit_channel.send = AsyncMock()
                bot.get_channel = Mock(return_value=audit_channel)

                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(
                        category=SimpleNamespace(id=10, name="Test Company"),
                    ),
                    user=SimpleNamespace(id=2, roles=[]),
                    created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
                    response=SimpleNamespace(defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                screen = SimpleNamespace(
                    content_type="image/png",
                    size=100,
                    read=AsyncMock(return_value=b"image"),
                )
                assessment = core.assess_account_tax(
                    Decimal("1000"),
                    [core.AccountTransaction(Decimal("-50"), "2026-11-11")],
                )
                command = bot.tree.get_command("podatek")
                if command is None:
                    raise AssertionError("Tax command was not registered")

                with patch(
                    "bot.core.analyze_account_screen",
                    return_value=assessment,
                ) as analyze_screen:
                    await command.callback(interaction, screen)

                analyze_screen.assert_called_once()
                interaction.followup.send.assert_awaited_once()
                audit_channel.send.assert_awaited_once()

    async def test_penalty_payment_command_marks_only_a_matching_ocr_debit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                penalty = core.add_financial_penalty(
                    1,
                    10,
                    "Company",
                    "500.00",
                    "Test fine",
                    "2026-11-20",
                    1,
                    "Admin",
                    datetime(2026, 11, 1, tzinfo=timezone.utc),
                )
                bot = build_bot()
                bot.audit_channel_id = None
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    permissions=SimpleNamespace(administrator=False),
                    channel=SimpleNamespace(category=SimpleNamespace(id=10)),
                    user=SimpleNamespace(id=2, roles=[]),
                    response=SimpleNamespace(
                        send_message=AsyncMock(),
                        defer=AsyncMock(),
                    ),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                screen = SimpleNamespace(
                    content_type="image/png",
                    size=100,
                    read=AsyncMock(return_value=b"image"),
                )
                assessment = core.assess_account_tax(
                    Decimal("5000"),
                    [
                        core.AccountTransaction(
                            Decimal("-500"),
                            "2026-11-11",
                            "12:00",
                        )
                    ],
                )
                command = bot.tree.get_command("oplac_kare")
                if command is None:
                    raise AssertionError("Payment command was not registered")
                with (
                    patch(
                        "bot.core.analyze_account_screen",
                        return_value=assessment,
                    ) as analyze_screen,
                    patch("bot.refresh_company_board", new=AsyncMock(return_value=True)),
                ):
                    await command.callback(
                        interaction,
                        penalty.id,
                        screen,
                        discord.app_commands.Choice(
                            name="Karta transakcji",
                            value="transaction-card",
                        ),
                    )
                    analyze_screen.assert_called_once()
                    self.assertEqual(
                        analyze_screen.call_args.kwargs["layout"],
                        "transaction-card",
                    )
                    interaction.followup.send.assert_awaited_once()
                    preview_view = interaction.followup.send.await_args.kwargs["view"]
                    self.assertIsInstance(preview_view, PaymentReviewView)
                    pending_penalty = core.get_financial_penalty(1, penalty.id)
                    self.assertIsNotNone(pending_penalty)
                    assert pending_penalty is not None
                    self.assertIsNone(pending_penalty.paid_at)
                    confirmation_interaction = SimpleNamespace(
                        user=SimpleNamespace(id=2),
                        response=SimpleNamespace(defer=AsyncMock()),
                        edit_original_response=AsyncMock(),
                        followup=SimpleNamespace(send=AsyncMock()),
                    )
                    await preview_view.children[0].callback(
                        confirmation_interaction
                    )

                saved_penalty = core.get_financial_penalty(1, penalty.id)

        if saved_penalty is None:
            raise AssertionError("Penalty disappeared from the register")
        self.assertIsNotNone(saved_penalty.paid_at)
        self.assertEqual(saved_penalty.paid_by_id, 2)
        confirmation_interaction.edit_original_response.assert_awaited_once()

    async def test_penalty_payment_command_requests_another_screenshot_when_amount_is_low(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                penalty = core.add_financial_penalty(
                    1,
                    10,
                    "Company",
                    "500.00",
                    "Test fine",
                    "2026-11-20",
                    1,
                    "Admin",
                    datetime(2026, 11, 1, tzinfo=timezone.utc),
                )
                bot = build_bot()
                bot.audit_channel_id = None
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    permissions=SimpleNamespace(administrator=True),
                    channel=SimpleNamespace(category=SimpleNamespace(id=10)),
                    user=SimpleNamespace(id=2),
                    response=SimpleNamespace(
                        send_message=AsyncMock(),
                        defer=AsyncMock(),
                    ),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                screen = SimpleNamespace(
                    content_type="image/png",
                    size=100,
                    read=AsyncMock(return_value=b"image"),
                )
                assessment = core.assess_account_tax(
                    Decimal("5000"),
                    [core.AccountTransaction(Decimal("-400"), "2026-11-11", "12:00")],
                )
                command = bot.tree.get_command("oplac_kare")
                if command is None:
                    raise AssertionError("Payment command was not registered")
                with patch("bot.core.analyze_account_screen", return_value=assessment):
                    await command.callback(interaction, penalty.id, screen)

                saved_penalty = core.get_financial_penalty(1, penalty.id)

        if saved_penalty is None:
            raise AssertionError("Penalty disappeared from the register")
        self.assertIsNone(saved_penalty.paid_at)
        self.assertIn("Nie potwierdzono", interaction.followup.send.await_args.args[0])

    def test_ready_event_remains_registered(self):
        bot = build_bot()

        self.assertTrue(callable(getattr(bot, "on_ready", None)))

    def test_company_board_shows_activity_and_tax_state(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                core.set_company_active(1, 10, "Up-n-Atom", True)
                core.record_tax_result(
                    1, 10, "Up-n-Atom", True, "2026-07-20", "$894,410.00"
                )
                core.set_company_active(1, 11, "Nevada Poles", False)
                embed = build_company_board_embed(1)

        self.assertIn("**Up-n-Atom**", embed.description)
        self.assertIn("Tydzień podatkowy", embed.description)
        self.assertIn("Niedziela", embed.description)
        self.assertIn("🟢 Aktywna", embed.description)
        self.assertIn("✅ Podatek zapłacony · 2026-07-20", embed.description)
        self.assertIn("**Nevada Poles**", embed.description)
        self.assertIn("⚪ Nieaktywna", embed.description)
        self.assertIn("▫️ Podatek niezweryfikowany", embed.description)

    def test_company_board_shows_current_week_tax_relief(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                core.set_company_active(1, 10, "Up-n-Atom", True)
                core.grant_company_tax_relief(1, 10, "Up-n-Atom")
                embed = build_company_board_embed(1)

        self.assertIn("Zwolniona z podatku w tym tygodniu", embed.description)

    def test_company_board_and_separate_active_penalty_table_show_new_penalties(self):
        fixed_now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                core.set_company_active(1, 10, "Up-n-Atom", True)
                core.add_financial_penalty(
                    1,
                    10,
                    "Up-n-Atom",
                    core.money(Decimal("2500")),
                    "Unpaid tax",
                    "2026-11-20",
                    100,
                    "Admin",
                    fixed_now,
                )
                company_embed = build_company_board_embed(1)
                penalty_embed = build_active_penalties_embed(1, now=fixed_now)

        self.assertIn("Aktywne kary", company_embed.description)
        self.assertIn("2,5 tys. $", company_embed.description)
        self.assertEqual(penalty_embed.title, "⚖️ Aktywne kary finansowe")
        self.assertEqual(
            penalty_embed.fields[0].name,
            "#1 · Up-n-Atom · 2,5 tys. $",
        )
        self.assertIn("🟡 Oczekująca", penalty_embed.fields[0].value)

    def test_penalty_embed_displays_payment_status_and_audit_details(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                penalty = core.add_financial_penalty(
                    1,
                    10,
                    "Company",
                    "1 mln $",
                    "Late tax",
                    "2026-08-01",
                    100,
                    "Admin",
                    datetime(2026, 7, 1, tzinfo=timezone.utc),
                )
                penalty, changed = core.mark_financial_penalty_paid(
                    1,
                    penalty.id,
                    200,
                    "Treasurer",
                    datetime(2026, 7, 2, tzinfo=timezone.utc),
                )
                self.assertTrue(changed)
                if penalty is None:
                    raise AssertionError("Paid penalty was not returned")
                embed = build_penalty_embed(penalty)

        self.assertEqual(embed.fields[2].name, "Status")
        self.assertEqual(embed.fields[2].value, "✅ Opłacona")
        self.assertIn("Opłacono", [field.name for field in embed.fields])

    def test_help_embed_has_polish_and_english_localizations(self):
        polish = build_help_embed("pl")
        english = build_help_embed("en-US")

        self.assertIn("weryfikacja podatku", polish.title)
        self.assertIn("12% stanu konta", polish.description)
        self.assertIn("tax verification", english.title)
        self.assertIn("12% of the account balance", english.description)
        for embed in (polish, english):
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)

    def test_tax_response_contains_only_the_requested_four_fields(self):
        assessment = core.assess_account_tax(
            Decimal("894410"),
            [core.AccountTransaction(Decimal("-121965"), "2026-07-20", "14:39")],
        )
        embed = build_tax_result_embed(assessment, "Up-n-Atom")

        self.assertEqual(
            [field.name for field in embed.fields],
            ["Podatek", "Data", "Nazwa Firmy", "Stan konta"],
        )
        self.assertEqual(embed.fields[0].value, "Zapłacony poprawnie")
        self.assertEqual(embed.fields[1].value, "2026-07-20")
        self.assertEqual(embed.fields[2].value, "Up-n-Atom")
        self.assertEqual(embed.fields[3].value, "894,41 tys. $")

    def test_tax_audit_is_a_formatted_embed_with_status_and_amount_details(self):
        assessment = core.assess_account_tax(
            Decimal("894410"),
            [core.AccountTransaction(Decimal("-121965"), "2026-07-20", "14:39")],
        )
        embed = build_tax_audit_embed(
            assessment,
            "Up-n-Atom",
            "Treasurer",
            datetime(2026, 7, 20, 14, 40, tzinfo=timezone.utc),
            transaction=assessment.paid_transaction,
        )

        self.assertIn("zapłacony poprawnie", embed.title.lower())
        self.assertEqual(
            {field.name for field in embed.fields},
            {
                "Firma",
                "Przesłał(a)",
                "Wynik",
                "Stan konta",
                "Wymagany podatek · 12%",
                "Wykryta transakcja",
                "Data transakcji",
            },
        )

    def test_penalty_announcement_displays_explicit_penalty_id(self):
        penalty = core.FinancialPenalty(
            42,
            1,
            10,
            "Company",
            "$500",
            "Reason",
            "2026-08-01",
            1,
            "Admin",
            "2026-07-20T00:00:00+00:00",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

        embed = build_penalty_embed(penalty)

        self.assertIn("#42", embed.title)
        self.assertIn(("ID kary", "#42"), [(field.name, field.value) for field in embed.fields])

    def test_penalty_csv_has_complete_rows_and_blocks_spreadsheet_formulas(self):
        penalty = core.FinancialPenalty(
            7,
            1,
            10,
            "Company",
            "500.00",
            "=HYPERLINK(\"https://example.invalid\")",
            "2026-12-01",
            1,
            "Admin",
            "2026-11-01T00:00:00+00:00",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

        exported = build_penalty_csv([penalty]).decode("utf-8-sig")

        self.assertIn("ID,Firma,Kwota,Powód,Termin,Status", exported)
        self.assertIn("'=HYPERLINK", exported)
        self.assertIn("oczekująca", exported)

    def test_weekly_summary_embed_displays_all_requested_totals(self):
        summary = core.WeeklyPenaltySummary(
            3,
            Decimal("8000"),
            1,
            Decimal("1000"),
            1,
            Decimal("5000"),
            1,
            Decimal("2000"),
        )

        embed = build_weekly_penalty_summary_embed(
            summary,
            date(2026, 11, 1),
            date(2026, 11, 8),
        )

        self.assertEqual(len(embed.fields), 4)
        self.assertIn("Wpłaty w tym tygodniu", [field.name for field in embed.fields])
        self.assertIn("Po terminie obecnie", [field.name for field in embed.fields])
        self.assertIn("8 tys. $", embed.fields[0].value)

    async def test_presentation_warns_and_waits_for_explicit_confirmation(self):
        bot = build_bot()
        command = bot.tree.get_command("prezentacja")
        self.assertIsNotNone(command)
        response = SimpleNamespace(send_message=AsyncMock())
        create_category = AsyncMock()
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(
                    manage_channels=True,
                    send_messages=True,
                    embed_links=True,
                )
            ),
            create_category=create_category,
        )
        user = SimpleNamespace(
            id=2,
            guild_permissions=SimpleNamespace(administrator=True),
        )
        interaction = SimpleNamespace(
            guild=guild,
            permissions=SimpleNamespace(administrator=True),
            user=user,
            response=response,
        )

        await command.callback(interaction)

        response.send_message.assert_awaited_once()
        kwargs = response.send_message.await_args.kwargs
        self.assertIn("tylko pierwszy kanał", kwargs["embed"].description)
        self.assertIn("kliknięcia", kwargs["embed"].description)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"], PresentationConfirmView)
        create_category.assert_not_awaited()

    async def test_presentation_step_button_is_restricted_to_initiating_user(self):
        action = AsyncMock()
        view = PresentationStepView(123, action, "Dalej")
        button = view.children[0]
        unauthorized_response = SimpleNamespace(send_message=AsyncMock())
        unauthorized = SimpleNamespace(
            user=SimpleNamespace(id=456),
            response=unauthorized_response,
        )

        await button.callback(unauthorized)

        unauthorized_response.send_message.assert_awaited_once()
        action.assert_not_awaited()

        authorized = SimpleNamespace(user=SimpleNamespace(id=123))
        await button.callback(authorized)
        action.assert_awaited_once_with(authorized)

    async def test_presentation_faq_is_stage_specific_and_private(self):
        view = PresentationStepView(123, AsyncMock(), "Dalej", stage=2)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=response,
        )

        await view.children[-1].callback(interaction)

        response.send_message.assert_awaited_once()
        answer = response.send_message.await_args.args[0]
        self.assertIn("Czy sam duży przelew wystarczy", answer)
        self.assertIn("zatwierdzenia przez rolę weryfikacji wpłat", answer)
        self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])

    async def test_end_presentation_command_asks_before_deleting_resources(self):
        bot = build_bot()
        command = bot.tree.get_command("zakoncz_prezentacje")
        self.assertIsNotNone(command)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            permissions=SimpleNamespace(administrator=True),
            user=SimpleNamespace(id=2),
            response=response,
        )
        with patch.object(core, "get_presentation_session", return_value=core.PresentationSession(
            1, 10, 20, 1, 30, 40
        )), patch.object(core, "list_presentation_resources", return_value=[
            (30, "channel"), (10, "category")
        ]):
            await command.callback(interaction)

        kwargs = response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn(
            "usunąć wyłącznie zapisane kanały",
            response.send_message.await_args.args[0],
        )
        self.assertIsInstance(kwargs["view"], ConfirmActionView)

    async def test_penalty_export_command_sends_private_csv_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                core.add_financial_penalty(
                    1,
                    10,
                    "Company",
                    "500",
                    "Test",
                    "2026-12-01",
                    1,
                    "Admin",
                    datetime(2026, 11, 1, tzinfo=timezone.utc),
                )
                bot = build_bot()
                command = bot.tree.get_command("eksport_kar")
                if command is None:
                    raise AssertionError("Penalty export command was not registered")
                response = SimpleNamespace(send_message=AsyncMock())
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=1, filesize_limit=1024 * 1024),
                    permissions=SimpleNamespace(administrator=True),
                    response=response,
                )

                await command.callback(interaction)

        kwargs = response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["file"].filename.split(".")[-1], "csv")
        kwargs["file"].fp.seek(0)
        self.assertIn(b"Company", kwargs["file"].fp.read())
        kwargs["file"].close()

    def test_unverified_response_also_contains_only_the_requested_fields(self):
        embed = build_unverified_tax_embed("Up-n-Atom", "$100.00")

        self.assertEqual(
            [field.name for field in embed.fields],
            ["Podatek", "Data", "Nazwa Firmy", "Stan konta"],
        )
        self.assertIn("wyślij screen", embed.fields[0].value)
        self.assertEqual(embed.fields[2].value, "Up-n-Atom")
        self.assertEqual(embed.fields[3].value, "$100.00")


if __name__ == "__main__":
    unittest.main()
