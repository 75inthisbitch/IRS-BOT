"""Bot Discord do weryfikacji podatku ze screena (OCR).

Uruchomienie bez GUI:  python bot.py
Uruchomienie z GUI:    python gui.py
"""
import asyncio
import csv
import io
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import tasks

import core

log = logging.getLogger("podatek-bot")

MAX_SIZE = 8 * 1024 * 1024  # 8 MB

COMPANY_CHANNEL_LAYOUT = (
    ("💼 Firma Diamond Casino", ("💬 discord", "💬 czat")),
    ("💼 Firma Delux Taxi", ("💬 discord", "💬 czat")),
    ("💼 Firma Fresh", ("💬 czat",)),
    ("💼 Firma Bean Machine", ("💬 czat",)),
    ("💼 Firma Burger Shot", ("💬 discord", "💬 czat")),
    ("🚗 Car Dealer", ("📌 informacje", "💬 discord", "💬 chat")),
    ("💎 Firma Pearls", ("💬 czat",)),
    ("💼 Firma Tunnel Shop", ("💬 discord", "💬 czat")),
    ("🏦 Lombard", ("💬 czat",)),
    ("☕ Firma UwU Cafe", ("💬 discord", "💬 czat")),
    ("🚘 Gruppe6", ("📌 informacje", "💬 discord", "💬 chat")),
    ("⚛️ Firma UP Atom", ("💬 discord", "💬 czat")),
    ("🧵 Firma Stitchy", ("💬 czat",)),
    ("🛡️ Firma Armor", ("💬 discord", "💬 czat")),
    ("📡 Firma LSC", ("💬 discord", "💬 czat")),
)
COMMUNITY_CATEGORY_LAYOUT = (
    ("🏆 Topki 3.0", ("📊 podsumowanie", "🏅 ranking-firm")),
    (
        "💼 Oferty Pracy",
        (
            "📣 lsc-wolne",
            "📣 bennys",
            "📣 tunner-shop",
            "📣 lombard",
            "📣 casino-wolne",
            "📣 cardealer-wolne",
            "📣 taxi",
            "📣 uwucafe",
            "📣 pearls",
            "📣 fresh",
            "📣 stitchy",
            "📣 armor",
        ),
    ),
    ("🏛️ LOBBY", ("📌 informacje",)),
    ("🏙️ URZĄD MIASTA", ("📘 dostepne-informacje",)),
    (
        "🏢 Urząd Pracy",
        (
            "📣 ogloszenia",
            "📄 regulamin",
            "📢 ogloszenia-irs",
            "📻 radia-firmowe",
            "⚖️ kary-finansowe",
            "🏢 firmy-informacje",
            "📡 stacje-informacje",
            "💬 czat",
            "💡 propozycje",
        ),
    ),
    ("👥 Opiekunowie", ("🧑‍💼 doj", "📑 podatek-doj")),
)
COMMUNITY_ROOT_CHANNELS = ("📜 zasady",)


class GuildCommandSyncError(RuntimeError):
    """Raised when Discord refuses to sync commands to the configured server."""


def build_company_board_embed(guild_id: int) -> discord.Embed:
    companies = core.list_companies(guild_id)
    active_penalties = [
        penalty
        for penalty in core.list_active_financial_penalties(guild_id)
        if penalty.paid_at is None
    ]
    penalties_by_category: dict[int, list[core.FinancialPenalty]] = {}
    for penalty in active_penalties:
        penalties_by_category.setdefault(penalty.category_id, []).append(penalty)
    week_start, week_end = core.tax_week_range()
    if not companies:
        rows = ["Nie ma jeszcze firm. Użyj `/aktywna` w kanale kategorii firmy."]
    else:
        rows = []
        for company in companies:
            name = discord.utils.escape_markdown(
                core.normalize_company_name(company.name).replace("\n", " ")
            )
            activity = "🟢 Aktywna" if company.active else "⚪ Nieaktywna"
            if company.tax_exempt:
                tax = "🟦 Zwolniona z podatku w tym tygodniu"
            elif company.tax_paid is None:
                tax = "▫️ Podatek niezweryfikowany"
            elif company.tax_paid:
                tax = (
                    f"✅ Podatek zapłacony · {company.tax_date}"
                    if company.tax_date
                    else "✅ Podatek zapłacony"
                )
            else:
                tax = "🔴 Podatek nieopłacony"
            balance = (
                f" · Saldo: {discord.utils.escape_markdown(company.balance)}"
                if company.balance
                else ""
            )
            company_penalties = penalties_by_category.get(company.category_id, [])
            penalty_summary = ""
            if company_penalties:
                penalty_ids = ", ".join(
                    f"#{penalty.id} "
                    f"({core.format_stored_money(penalty.amount)})"
                    for penalty in company_penalties[:3]
                )
                remaining = len(company_penalties) - 3
                if remaining > 0:
                    penalty_ids += f" +{remaining}"
                penalty_summary = f"\n⚖️ **Aktywne kary:** {penalty_ids}"
            rows.append(f"**{name}**\n{activity}　·　{tax}{balance}{penalty_summary}")

    content = (
        f"**Tydzień podatkowy:** Niedziela {week_start:%d.%m.%Y} 00:00 "
        f"→ niedziela {week_end:%d.%m.%Y} 00:00\n\n"
        + "\n\n".join(rows)
    )
    if len(content) > 3900:
        content = content[:3860].rsplit("\n\n", 1)[0] + "\n\n_Lista skrócona — zbyt wiele firm._"
    embed = discord.Embed(
        title="📊  Firmy",
        description=content,
        color=discord.Color.from_rgb(83, 101, 242),
    )
    embed.set_footer(text="IRS · Statusy tygodniowe resetuje administrator komendą /muala")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def build_active_penalties_embed(
    guild_id: int,
    now: datetime | None = None,
) -> discord.Embed:
    penalties = core.list_active_financial_penalties(guild_id, now=now)
    embed = discord.Embed(
        title="⚖️ Aktywne kary finansowe",
        description="Opłacone kary pozostają widoczne przez 24 godziny.",
        color=discord.Color.from_rgb(210, 77, 95),
    )
    if not penalties:
        embed.add_field(
            name="Brak aktywnych kar",
            value="Nie ma obecnie kar oczekujących, po terminie ani opłaconych w ostatnich 24 godzinach.",
            inline=False,
        )
    else:
        character_count = len(embed.title or "") + len(embed.description or "")
        displayed_count = 0
        for penalty in penalties[:24]:
            status, _ = penalty_status_display(core.financial_penalty_status(penalty))
            details = (
                f"{status} · Termin: {penalty.due_date}\n"
                f"Powód: {discord.utils.escape_markdown(penalty.reason)[:300]}"
            )
            if penalty.paid_at:
                paid_by = discord.utils.escape_markdown(
                    penalty.paid_by_name or "nieznana osoba"
                )
                paid_timestamp = int(datetime.fromisoformat(penalty.paid_at).timestamp())
                details += (
                    f"\nOpłacił(a): {paid_by[:100]} · <t:{paid_timestamp}:R>"
                )
            title = (
                f"#{penalty.id} · "
                f"{discord.utils.escape_markdown(penalty.company_name)[:100]} · "
                f"{core.format_stored_money(penalty.amount)}"
            )
            if character_count + len(title) + len(details) > 5600:
                break
            embed.add_field(
                name=title,
                value=details[:1024],
                inline=False,
            )
            character_count += len(title) + len(details)
            displayed_count += 1
        omitted_count = len(penalties) - displayed_count
        if omitted_count:
            embed.add_field(
                name="Pozostałe wpisy",
                value=f"Nie pokazano jeszcze {omitted_count} aktywnych kar.",
                inline=False,
            )
    embed.set_footer(text="IRS · Opłacone kary znikają z tej tabeli po 24 godzinach")
    embed.timestamp = now or datetime.now(timezone.utc)
    return embed


def build_penalty_embed(penalty: core.FinancialPenalty) -> discord.Embed:
    status = core.financial_penalty_status(penalty)
    status_label, color = penalty_status_display(status)
    embed = discord.Embed(
        title=f"⚖️ Kara finansowa #{penalty.id}",
        color=color,
    )
    embed.add_field(
        name="Firma",
        value=discord.utils.escape_markdown(penalty.company_name)[:1024],
        inline=True,
    )
    embed.add_field(
        name="Kwota",
        value=core.format_stored_money(penalty.amount),
        inline=True,
    )
    embed.add_field(name="Status", value=status_label, inline=True)
    embed.add_field(name="ID kary", value=f"#{penalty.id}", inline=True)
    embed.add_field(
        name="Powód",
        value=discord.utils.escape_markdown(penalty.reason)[:1024],
        inline=False,
    )
    embed.add_field(name="Termin płatności", value=penalty.due_date, inline=True)
    embed.add_field(
        name="Nałożył(a)",
        value=discord.utils.escape_markdown(penalty.imposed_by_name)[:1024],
        inline=True,
    )
    created_timestamp = int(datetime.fromisoformat(penalty.created_at).timestamp())
    embed.add_field(
        name="Nałożono",
        value=f"<t:{created_timestamp}:F> · <t:{created_timestamp}:R>",
        inline=True,
    )
    if penalty.paid_at:
        paid_by = penalty.paid_by_name or "nieznana osoba"
        paid_timestamp = int(datetime.fromisoformat(penalty.paid_at).timestamp())
        embed.add_field(
            name="Opłacono",
            value=(
                f"<t:{paid_timestamp}:F> · <t:{paid_timestamp}:R>\n"
                f"Osoba: {discord.utils.escape_markdown(paid_by)[:900]}"
            ),
            inline=False,
        )
    embed.set_footer(text="IRS · Status po terminie naliczany automatycznie"    )
    return embed


def build_overdue_penalty_embed(penalty: core.FinancialPenalty) -> discord.Embed:
    embed = discord.Embed(
        title=f"⏰ Kara po terminie · #{penalty.id}",
        description="Termin płatności minął, a kara nadal oczekuje na opłacenie.",
        color=discord.Color.from_rgb(192, 57, 43),
    )
    embed.add_field(name="ID kary", value=f"#{penalty.id}", inline=True)
    embed.add_field(name="Firma", value=penalty.company_name, inline=True)
    embed.add_field(name="Kwota", value=core.format_stored_money(penalty.amount), inline=True)
    embed.add_field(name="Termin płatności", value=penalty.due_date, inline=True)
    embed.set_footer(text="IRS · Powiadomienie o zaległej karze")
    return embed


def build_due_reminder_embed(penalty: core.FinancialPenalty) -> discord.Embed:
    embed = discord.Embed(
        title=f"⏳ Przypomnienie o karze · #{penalty.id}",
        description="Do terminu płatności kary pozostał jeden dzień.",
        color=discord.Color.from_rgb(219, 160, 64),
    )
    embed.add_field(name="ID kary", value=f"#{penalty.id}", inline=True)
    embed.add_field(name="Firma", value=penalty.company_name, inline=True)
    embed.add_field(name="Kwota", value=core.format_stored_money(penalty.amount), inline=True)
    embed.add_field(name="Termin płatności", value=penalty.due_date, inline=True)
    embed.set_footer(text="IRS · Przypomnienie wysłane dzień przed terminem")
    return embed


def build_weekly_penalty_summary_embed(
    summary: core.WeeklyPenaltySummary,
    period_start: date,
    period_end: date,
) -> discord.Embed:
    embed = discord.Embed(
        title="📊 Tygodniowe zestawienie kar",
        description=(
            f"Okres: **{period_start:%d.%m.%Y} – {period_end:%d.%m.%Y}** "
            "(koniec okresu nie jest wliczany)."
        ),
        color=discord.Color.from_rgb(83, 101, 242),
    )
    embed.add_field(
        name="Nałożone w tym tygodniu",
        value=(
            f"**{summary.imposed_count}** · "
            f"{core.money(summary.imposed_total)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Wpłaty w tym tygodniu",
        value=f"**{summary.paid_count}** · {core.money(summary.paid_total)}",
        inline=True,
    )
    embed.add_field(
        name="Oczekujące obecnie",
        value=f"**{summary.pending_count}** · {core.money(summary.pending_total)}",
        inline=True,
    )
    embed.add_field(
        name="Po terminie obecnie",
        value=f"**{summary.overdue_count}** · {core.money(summary.overdue_total)}",
        inline=True,
    )
    embed.set_footer(text="IRS · Automatyczne podsumowanie tygodniowe")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def build_penalty_csv(penalties: list[core.FinancialPenalty]) -> bytes:
    def safe_cell(value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.lstrip()
        if stripped.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "ID",
            "Firma",
            "Kwota",
            "Powód",
            "Termin",
            "Status",
            "Nałożono",
            "Nałożył(a)",
            "Opłacono",
            "Opłacił(a)",
        )
    )
    for penalty in penalties:
        status = {
            "paid": "opłacona",
            "overdue": "po terminie",
            "pending": "oczekująca",
        }[core.financial_penalty_status(penalty)]
        writer.writerow(
            safe_cell(value)
            for value in (
                penalty.id,
                penalty.company_name,
                core.format_stored_money(penalty.amount),
                penalty.reason,
                penalty.due_date,
                status,
                penalty.created_at,
                penalty.imposed_by_name,
                penalty.paid_at or "",
                penalty.paid_by_name or "",
            )
        )
    return output.getvalue().encode("utf-8-sig")


def penalty_status_display(status: str) -> tuple[str, discord.Color]:
    return {
        "paid": ("✅ Opłacona", discord.Color.from_rgb(46, 160, 105)),
        "overdue": ("🔴 Po terminie", discord.Color.from_rgb(210, 77, 95)),
        "pending": ("🟡 Oczekująca", discord.Color.from_rgb(219, 160, 64)),
    }[status]


class ConfirmActionView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        action: Callable[[], Awaitable[str]],
        confirm_label: str = "Potwierdź usunięcie",
    ):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.action = action
        confirm_button = next(
            item
            for item in self.children
            if isinstance(item, discord.ui.Button)
            and item.custom_id == "confirm_action:confirm"
        )
        confirm_button.label = confirm_label

    def _disable_buttons(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(
        label="Potwierdź usunięcie",
        style=discord.ButtonStyle.danger,
        custom_id="confirm_action:confirm",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Tylko osoba, która rozpoczęła tę operację, może ją potwierdzić.",
                ephemeral=True,
            )
            return
        _button.disabled = True
        self._disable_buttons()
        await interaction.response.defer()
        try:
            result = await self.action()
        except discord.HTTPException:
            log.exception("Nie udało się wykonać potwierdzonej operacji Discord.")
            result = (
                "Discord nie pozwolił dokończyć operacji. Część zmian mogła już "
                "zostać wykonana; sprawdź stan serwera przed ponowną próbą."
            )
        try:
            await interaction.edit_original_response(
                content=result,
                embed=None,
                view=None,
            )
        except discord.NotFound:
            log.info(
                "Wiadomość potwierdzająca zniknęła przed pokazaniem wyniku operacji."
            )
            try:
                await interaction.followup.send(result, ephemeral=True)
            except discord.HTTPException:
                log.info("Nie można już wysłać wyniku przez token interakcji.")
        except discord.HTTPException:
            log.exception("Nie udało się pokazać wyniku potwierdzonej operacji.")
            try:
                await interaction.followup.send(
                    result,
                    ephemeral=True,
                )
            except discord.HTTPException:
                log.exception("Nie udało się wysłać wyniku jako follow-up.")

    @discord.ui.button(
        label="Anuluj",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Tylko osoba, która rozpoczęła tę operację, może ją anulować.",
                ephemeral=True,
            )
            return
        _button.disabled = True
        self._disable_buttons()
        await interaction.response.edit_message(
            content="Operacja została anulowana.",
            embed=None,
            view=None,
        )


class PaymentReviewView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        action: Callable[[], Awaitable[str | discord.Embed]],
        authorized_role_id: int | None = None,
    ):
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.action = action
        self.authorized_role_id = authorized_role_id

    def _can_review(self, interaction: discord.Interaction) -> bool:
        if getattr(
            getattr(interaction.user, "guild_permissions", None),
            "administrator",
            False,
        ):
            return True
        if interaction.user.id == self.requester_id:
            return True
        if self.authorized_role_id is None:
            return False
        return any(
            role.id == self.authorized_role_id
            for role in getattr(interaction.user, "roles", ())
        )

    @discord.ui.button(
        label="Potwierdź wpłatę",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if not self._can_review(interaction):
            await interaction.response.send_message(
                "Tylko osoba, która przesłała screen, osoba z rolą weryfikacji "
                "wpłat albo administrator może zatwierdzić ten odczyt.",
                ephemeral=True,
            )
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.action()
        except Exception:
            log.exception("Nie udało się zapisać zatwierdzonej wpłaty.")
            result = (
                "Nie udało się zapisać wpłaty. Sprawdź rejestr przed ponowną próbą."
            )
        try:
            if isinstance(result, discord.Embed):
                await interaction.edit_original_response(
                    content=None,
                    embed=result,
                    view=None,
                )
            else:
                await interaction.edit_original_response(
                    content=result,
                    embed=None,
                    view=None,
                )
        except discord.NotFound:
            log.info("Podgląd wpłaty zniknął przed pokazaniem wyniku.")
            try:
                if isinstance(result, discord.Embed):
                    await interaction.followup.send(embed=result, ephemeral=True)
                else:
                    await interaction.followup.send(result, ephemeral=True)
            except discord.HTTPException:
                log.info("Nie można już wysłać wyniku zatwierdzenia wpłaty.")
        except discord.HTTPException:
            log.exception("Nie udało się wyświetlić wyniku zatwierdzenia wpłaty.")
        self.stop()

    @discord.ui.button(
        label="To nie ta transakcja",
        style=discord.ButtonStyle.secondary,
    )
    async def reject(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if not self._can_review(interaction):
            await interaction.response.send_message(
                "Tylko osoba, która przesłała screen, osoba z rolą weryfikacji "
                "wpłat albo administrator może odrzucić ten odczyt.",
                ephemeral=True,
            )
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(
            content="Odczyt odrzucony. Rejestr nie został zmieniony.",
            embed=None,
            view=None,
        )
        self.stop()


class PresentationConfirmView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        action: Callable[[discord.Interaction], Awaitable[str]],
    ):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.action = action

    @discord.ui.button(
        label="Tak, uruchom prezentację",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Tylko osoba, która rozpoczęła prezentację, może ją potwierdzić.",
                ephemeral=True,
            )
            return
        _button.disabled = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.defer()
        result = await self.action(interaction)
        await interaction.edit_original_response(
            content=result,
            embed=None,
            view=None,
        )
        self.stop()

    @discord.ui.button(
        label="Anuluj",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Tylko osoba, która rozpoczęła prezentację, może ją anulować.",
                ephemeral=True,
            )
            return
        _button.disabled = True
        await interaction.response.edit_message(
            content="Prezentacja została anulowana. Serwer nie został zmieniony.",
            embed=None,
            view=None,
        )
        self.stop()


class PresentationStepView(discord.ui.View):
    FREQUENT_QUESTIONS = {
        0: (
            "Czy demo zmieni prawdziwe dane?",
            "Nie. Prezentacja tworzy tylko kanały demo. Przykładowe firmy i kary "
            "nie trafiają do rejestru, a komendy produkcyjne są w tych kanałach blokowane.",
        ),
        1: (
            "Skąd bot bierze nazwę i status firmy?",
            "Nazwa pochodzi z kategorii Discorda. Administrator ustawia status "
            "komendą `/aktywna` albo `/nieaktywna`; firma pojawia się na tablicy.",
        ),
        2: (
            "Czy sam duży przelew wystarczy, by uznać podatek?",
            "Nie zawsze. Bot porównuje kwotę z 12% salda, ale niejednoznaczny opis "
            "wymaga zatwierdzenia przez rolę weryfikacji wpłat.",
        ),
        3: (
            "Co się stanie po terminie kary?",
            "Kara zmieni status na zaległą. Bot przypomni przed terminem i wyśle "
            "informację na kanał firmy oraz raportowy, jeśli ma do nich dostęp.",
        ),
        4: (
            "Czy można pobrać i zachować rejestr?",
            "Tak. `/eksport_kar` tworzy plik CSV. Dostęp do eksportu może mieć "
            "administrator albo osoba z rolą zarządzania karami.",
        ),
    }

    def __init__(
        self,
        requester_id: int,
        action: Callable[[discord.Interaction], Awaitable[None]],
        label: str,
        stage: int = 0,
    ):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.action = action
        self.stage = stage
        for item in self.children:
            if (
                isinstance(item, discord.ui.Button)
                and item.custom_id == "irs_presentation:advance"
            ):
                item.label = label

    @discord.ui.button(
        label="Dalej",
        style=discord.ButtonStyle.primary,
        custom_id="irs_presentation:advance",
    )
    async def advance(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Ten krok może zatwierdzić tylko osoba, która uruchomiła prezentację.",
                ephemeral=True,
            )
            return
        if _button.disabled:
            await interaction.response.send_message(
                "Ten krok jest już przetwarzany.",
                ephemeral=True,
            )
            return
        _button.disabled = True
        await self.action(interaction)

    def _ensure_requester(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester_id

    @discord.ui.button(
        label="Pokaż przykład",
        style=discord.ButtonStyle.secondary,
        custom_id="irs_presentation:example",
    )
    async def show_example(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if not self._ensure_requester(interaction):
            await interaction.response.send_message(
                "Przykłady może otwierać osoba prowadząca tę prezentację.",
                ephemeral=True,
            )
            return
        examples = {
            0: (
                "Zaraz pokażę Ci przykładową firmę, jej status aktywności i "
                "podsumowanie podatku. Żadne przykładowe dane nie trafią do rejestru."
            ),
            1: (
                "Przykład: Up-n-Atom jest aktywna i ma opłacony podatek; "
                "Auto Exotic jest aktywna, ale ma zaległość oraz oczekującą karę."
            ),
            2: (
                "Przykład: saldo 1 mln $ oznacza wymagany podatek 120 tys. $. "
                "Wpłata musi być widoczna jako obciążenie; niejednoznaczny opis "
                "zostanie pokazany do ręcznego potwierdzenia."
            ),
            3: (
                "Przykład: kara #10001 na 5 tys. $ ma termin za tydzień. "
                "Bot przypomina dzień wcześniej i oznacza zaległość po terminie."
            ),
            4: (
                "Końcowy przykład zawiera tygodniowe zestawienie oraz demonstracyjny "
                "CSV. Możesz go pobrać z wiadomości poniżej."
            ),
        }
        await interaction.response.send_message(
            examples.get(self.stage, examples[0]),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Co zrobić dalej?",
        style=discord.ButtonStyle.secondary,
        custom_id="irs_presentation:next_help",
    )
    async def explain_next(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if not self._ensure_requester(interaction):
            await interaction.response.send_message(
                "Wskazówki może otwierać osoba prowadząca tę prezentację.",
                ephemeral=True,
            )
            return
        if self.stage >= 4:
            message = (
                "To już koniec pokazu. Jeśli chcesz usunąć utworzone kanały demo, "
                "użyj `/zakoncz_prezentacje`."
            )
        else:
            message = (
                "Gdy będziesz gotowy, kliknij **Dalej**. Następny etap zostanie "
                "utworzony w osobnym kanale. Możesz też wcześniej użyć **Pokaż przykład**."
            )
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(
        label="Najczęstsze pytanie",
        style=discord.ButtonStyle.secondary,
        custom_id="irs_presentation:faq",
    )
    async def show_faq(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if not self._ensure_requester(interaction):
            await interaction.response.send_message(
                "Pytania może otwierać osoba prowadząca tę prezentację.",
                ephemeral=True,
            )
            return
        question, answer = self.FREQUENT_QUESTIONS.get(
            self.stage,
            self.FREQUENT_QUESTIONS[0],
        )
        await interaction.response.send_message(
            f"**{question}**\n{answer}",
            ephemeral=True,
        )


def build_presentation_intro_embed() -> discord.Embed:
    embed = discord.Embed(
        title="✨ Prezentacja bota IRS",
        description=(
            "To jest odizolowany pokaz. Wszystkie firmy, kwoty, wpłaty i kary "
            "poniżej są przykładowe. Bot **nie zapisał** ich w rejestrze, "
            "nie zmienił kanałów produkcyjnych ani ustawień."
        ),
        color=discord.Color.from_rgb(83, 101, 242),
    )
    embed.add_field(
        name="Podatki i OCR",
        value=(
            "`/podatek` analizuje screen, porównuje ujemną transakcję z 12% salda "
            "i odsyła prywatny wynik. Kanał raportowy dostaje szczegółowy embed."
        ),
        inline=False,
    )
    embed.add_field(
        name="Firmy i tydzień podatkowy",
        value=(
            "`/aktywna` i `/nieaktywna` zmieniają status firmy; `/ulga` zwalnia "
            "z podatku do resetu, a `/muala` resetuje statusy tygodniowe. "
            "`/przypomnienie` przypomina aktywnym firmom o nieopłaconym podatku."
        ),
        inline=False,
    )
    embed.add_field(
        name="Kary, przypomnienia i raporty",
        value=(
            "`/kara_finansowa`, `/oplac_kare`, `/rejestr_kar` i `/usun_kare` "
            "obsługują rejestr kar. Bot przypomina dzień przed terminem, "
            "informuje po terminie i publikuje tygodniowe zestawienie."
        ),
        inline=False,
    )
    embed.add_field(
        name="Administracja i eksport",
        value=(
            "`/audyt` pokazuje działania administratorów, `/usun_firme` zarządza "
            "tablicą firm, a `/eksport_kar` pobiera prywatny plik CSV. "
            "Kanały produkcyjne konfiguruje się przez `/ustaw_kanal_firm` "
            "i `/ustaw_kanal_podatkow`. `/ustaw_role_irs` przydziela role "
            "weryfikacji wpłat i zarządzania karami."
        ),
        inline=False,
    )
    embed.set_footer(text="IRS · Przykładowe dane demonstracyjne · bez zmian w bazie")
    return embed


def build_presentation_company_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📊 Przykładowa tablica firm",
        description=(
            "**Up-n-Atom**\n"
            "🟢 Aktywna · ✅ Podatek zapłacony · Saldo: 1 mln $\n\n"
            "**Auto Exotic**\n"
            "🟢 Aktywna · 🔴 Podatek nieopłacony · Saldo: 250 tys. $\n"
            "⚖️ Aktywne kary: #10001 (5 tys. $)\n\n"
            "**Firma przykładowa**\n"
            "⚪ Nieaktywna · 🟦 Zwolniona z podatku"
        ),
        color=discord.Color.from_rgb(83, 101, 242),
    )
    embed.add_field(
        name="Jak powstaje prawdziwa tablica?",
        value=(
            "Administrator używa `/aktywna` w kanale kategorii firmy. "
            "Status i wynik OCR są potem publikowane na kanale ustawionym przez "
            "`/ustaw_kanal_firm`."
        ),
        inline=False,
    )
    embed.set_footer(text="IRS · To są przykłady, nie wpisy z rejestru")
    return embed


def build_presentation_tax_embed() -> discord.Embed:
    assessment = core.assess_account_tax(
        Decimal("1000000"),
        [
            core.AccountTransaction(
                Decimal("-120000"),
                date.today().isoformat(),
                "14:30",
            )
        ],
    )
    embed = build_tax_result_embed(assessment, "Up-n-Atom · DEMO")
    embed.title = "✅ Przykładowy wynik weryfikacji"
    embed.description = (
        "Przykład odpowiedzi użytkownika. W prawdziwym użyciu dane pochodzą ze screena."
    )
    embed.set_footer(text="IRS · Przykładowe dane demonstracyjne")
    return embed


def build_presentation_penalty_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚖️ Przykładowy rejestr kar · dane demo",
        description="Każdy wpis ma swój identyfikator i status.",
        color=discord.Color.from_rgb(210, 77, 95),
    )
    embed.add_field(
        name="#10001 · Auto Exotic · 5 tys. $",
        value=(
            f"🟡 Oczekująca · Termin: "
            f"{date.today() + timedelta(days=7):%Y-%m-%d}\n"
            "Przypomnienie: dzień przed terminem"
        ),
        inline=False,
    )
    embed.add_field(
        name="#10002 · Premium Motors · 2 tys. $",
        value="🔴 Po terminie · Bot powiadamia firmę i kanał raportowy",
        inline=False,
    )
    embed.add_field(
        name="#10003 · Up-n-Atom · 1 tys. $",
        value="✅ Opłacona · Osoba i czas potwierdzenia pozostają w rejestrze",
        inline=False,
    )
    embed.set_footer(text="IRS · Przykładowe dane — nie są zapisane jako prawdziwe kary")
    return embed


def build_tax_result_embed(
    assessment: core.AccountTaxAssessment,
    company_name: str,
    transaction: core.AccountTransaction | None = None,
    pending_confirmation: bool = False,
    match_evaluated: bool = False,
) -> discord.Embed:
    paid_transaction = (
        transaction
        if match_evaluated
        else (transaction or assessment.paid_transaction)
    )
    displayed_transaction = paid_transaction or max(
        (item for item in assessment.transactions if item.amount < 0),
        key=lambda item: abs(item.amount),
        default=None,
    )
    displayed_date = (
        displayed_transaction.date
        if displayed_transaction and displayed_transaction.date
        else "Nie odczytano"
    )
    embed = discord.Embed(
        color=(
            discord.Color.from_rgb(219, 160, 64)
            if pending_confirmation
            else (
                discord.Color.from_rgb(46, 204, 113)
                if paid_transaction
                else discord.Color.from_rgb(230, 126, 34)
            )
        )
    )
    embed.add_field(
        name="Podatek",
        value=(
            "Odczytano wpłatę — czeka na potwierdzenie"
            if pending_confirmation
            else (
                "Zapłacony poprawnie"
                if paid_transaction
                else "Nie zapłacony poprawnie — wyślij kolejny screen"
            )
        ),
        inline=False,
    )
    if pending_confirmation and displayed_transaction is not None:
        displayed_date = (
            f"{displayed_date} · kwota "
            f"-{core.money(abs(displayed_transaction.amount))}"
        )
    embed.add_field(name="Data", value=displayed_date, inline=False)
    embed.add_field(name="Nazwa Firmy", value=company_name, inline=False)
    embed.add_field(
        name="Stan konta",
        value=core.money(assessment.balance),
        inline=False,
    )
    if pending_confirmation:
        embed.add_field(
            name="Pewność odczytu",
            value=f"{assessment.ocr_confidence:.0f}% · wymagane potwierdzenie",
            inline=False,
        )
    return embed


def build_tax_audit_embed(
    assessment: core.AccountTaxAssessment | None,
    company_name: str,
    submitter: str,
    checked_at: datetime,
    transaction: core.AccountTransaction | None = None,
    largest_debit: core.AccountTransaction | None = None,
    unverified_balance: str | None = None,
    pending_confirmation: bool = False,
) -> discord.Embed:
    if assessment is None:
        embed = discord.Embed(
            title="⚠️ Nie udało się zweryfikować podatku",
            description="OCR nie odczytał układu transakcji potrzebnego do sprawdzenia 12%.",
            color=discord.Color.from_rgb(149, 165, 166),
            timestamp=checked_at,
        )
        embed.add_field(name="Firma", value=company_name, inline=True)
        embed.add_field(
            name="Stan konta",
            value=unverified_balance or "Nie odczytano",
            inline=True,
        )
        embed.add_field(name="Przesłał(a)", value=submitter, inline=True)
        embed.set_footer(text="IRS · Do weryfikacji potrzebny jest czytelny screen finansów")
        return embed

    is_paid = transaction is not None and not pending_confirmation
    title = (
        "🟡 Wpłata oczekuje na potwierdzenie"
        if pending_confirmation
        else (
            "✅ Podatek zapłacony poprawnie"
            if is_paid
            else "🟠 Nie wykryto wymaganej zapłaty podatku"
        )
    )
    embed = discord.Embed(
        title=title,
        color=discord.Color.from_rgb(46, 160, 105)
        if is_paid
        else discord.Color.from_rgb(219, 140, 55),
        timestamp=checked_at,
    )
    embed.add_field(name="Firma", value=company_name, inline=True)
    embed.add_field(name="Przesłał(a)", value=submitter, inline=True)
    embed.add_field(
        name="Wynik",
        value=(
            "Do potwierdzenia"
            if pending_confirmation
            else ("Potwierdzony" if is_paid else "Niepotwierdzony")
        ),
        inline=True,
    )
    embed.add_field(
        name="Stan konta",
        value=core.money(assessment.balance),
        inline=True,
    )
    embed.add_field(
        name="Wymagany podatek · 12%",
        value=core.money(assessment.required_tax),
        inline=True,
    )
    shown_transaction = transaction or largest_debit
    transaction_value = (
        f"-{core.money(abs(shown_transaction.amount))}"
        if shown_transaction
        else "Nie znaleziono ujemnej transakcji"
    )
    embed.add_field(
        name="Wykryta transakcja",
        value=transaction_value,
        inline=True,
    )
    if shown_transaction:
        transaction_date = " ".join(
            part for part in (shown_transaction.date, shown_transaction.time) if part
        )
        embed.add_field(
            name="Data transakcji",
            value=transaction_date or "Nie odczytano ze screena",
            inline=True,
        )
    if pending_confirmation and transaction is not None:
        embed.add_field(
            name="Pewność OCR",
            value=f"{assessment.ocr_confidence:.0f}% · {transaction.description or 'brak opisu'}",
            inline=False,
        )
    embed.set_footer(text="IRS · Raport weryfikacji podatku")
    return embed


def build_penalty_payment_result_embed(
    penalty: core.FinancialPenalty,
    transaction: core.AccountTransaction,
    balance: Decimal | None,
    confirmed_at: datetime,
) -> discord.Embed:
    transaction_date = " ".join(
        part for part in (transaction.date, transaction.time) if part
    ) or confirmed_at.strftime("%Y-%m-%d %H:%M")
    embed = discord.Embed(
        color=discord.Color.from_rgb(46, 160, 105),
    )
    embed.add_field(name="Kara", value="Opłacona poprawnie", inline=False)
    embed.add_field(name="Data", value=transaction_date, inline=False)
    embed.add_field(
        name="Nazwa Firmy",
        value=discord.utils.escape_markdown(penalty.company_name),
        inline=False,
    )
    embed.add_field(
        name="Kwota kary",
        value=core.format_stored_money(penalty.amount),
        inline=False,
    )
    embed.add_field(
        name="Stan konta",
        value=core.money(balance) if balance is not None else "Nie odczytano ze screena",
        inline=False,
    )
    return embed


async def record_penalty_payment(
    bot: "TaxBot",
    interaction: discord.Interaction,
    penalty: core.FinancialPenalty,
    transaction: core.AccountTransaction,
    assessment: core.AccountTaxAssessment,
) -> str | discord.Embed:
    guild = interaction.guild
    if guild is None:
        return "Ta operacja działa wyłącznie na serwerze Discord."
    confirmed_at = datetime.now(timezone.utc)
    updated_penalty, was_updated = core.mark_financial_penalty_paid(
        guild.id,
        penalty.id,
        interaction.user.id,
        str(interaction.user),
        confirmed_at,
    )
    if updated_penalty is None:
        return f"Nie znaleziono kary **#{penalty.id}** na tym serwerze."
    if not was_updated:
        return (
            f"Kara **#{penalty.id}** została już opłacona przez "
            f"**{updated_penalty.paid_by_name or 'inną osobę'}**."
        )

    transaction_date = " ".join(
        part for part in (transaction.date, transaction.time) if part
    ) or confirmed_at.strftime("%Y-%m-%d %H:%M")
    core.record_admin_audit(
        guild.id,
        interaction.user.id,
        str(interaction.user),
        "oplacenie_kary_ocr",
        (
            f"Kara #{penalty.id}; kwota kary "
            f"{core.format_stored_money(penalty.amount)}; "
            f"wykryta transakcja -{core.money(abs(transaction.amount))}; "
            f"opis: {transaction.description or 'nie odczytano'}; "
            f"data transakcji: {transaction_date}; "
            f"pewność OCR: {assessment.ocr_confidence:.0f}%."
        ),
        penalty.company_name,
        confirmed_at,
    )
    board_updated = await refresh_company_board(bot, guild.id)
    payment_embed = build_penalty_payment_result_embed(
        updated_penalty,
        transaction,
        assessment.balance if assessment.balance_observed else None,
        confirmed_at,
    )
    if not board_updated:
        payment_embed.add_field(
            name="Uwaga",
            value="Płatność zapisana, ale nie udało się odświeżyć tabeli firm i kar.",
            inline=False,
        )

    if bot.audit_channel_id:
        try:
            report_channel = bot.get_channel(bot.audit_channel_id)
            if report_channel is None:
                report_channel = await bot.fetch_channel(bot.audit_channel_id)
            if isinstance(report_channel, discord.TextChannel):
                audit_embed = build_penalty_embed(updated_penalty)
                audit_embed.title = f"✅ Potwierdzono opłacenie kary #{penalty.id}"
                audit_embed.add_field(
                    name="Potwierdził(a)",
                    value=discord.utils.escape_markdown(str(interaction.user)),
                    inline=True,
                )
                audit_embed.add_field(
                    name="Wykryta transakcja",
                    value=f"-{core.money(abs(transaction.amount))}",
                    inline=True,
                )
                audit_embed.add_field(
                    name="Opis transakcji",
                    value=transaction.description or "Nie odczytano ze screena",
                    inline=True,
                )
                audit_embed.add_field(
                    name="Pewność OCR",
                    value=f"{assessment.ocr_confidence:.0f}%",
                    inline=True,
                )
                audit_embed.add_field(
                    name="Stan konta",
                    value=(
                        core.money(assessment.balance)
                        if assessment.balance_observed
                        else "Nie odczytano ze screena"
                    ),
                    inline=True,
                )
                await report_channel.send(
                    embed=audit_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                payment_embed.add_field(
                    name="Uwaga",
                    value="Nie udało się wysłać raportu — kanał nie jest tekstowy.",
                    inline=False,
                )
                log.error("Kanał raportowy nie jest tekstowym kanałem Discorda.")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            payment_embed.add_field(
                name="Uwaga",
                value="Wpłata zapisana, ale raportu nie udało się wysłać.",
                inline=False,
            )
            log.exception(
                "Nie udało się opublikować potwierdzenia kary #%s.",
                penalty.id,
            )
    return payment_embed


def build_unverified_tax_embed(company_name: str, balance: str = "Nie odczytano") -> discord.Embed:
    embed = discord.Embed(color=discord.Color.from_rgb(149, 165, 166))
    embed.add_field(
        name="Podatek",
        value="Nie można zweryfikować — wyślij screen finansów",
        inline=False,
    )
    embed.add_field(name="Data", value="Nie odczytano", inline=False)
    embed.add_field(name="Nazwa Firmy", value=company_name, inline=False)
    embed.add_field(name="Stan konta", value=balance, inline=False)
    return embed


def build_help_embed(language: str = "pl") -> discord.Embed:
    if language.lower().startswith("pl"):
        embed = discord.Embed(
            title="📘 Pomoc IRS — weryfikacja podatku",
            description=(
                "Podatek jest liczony jako **12% stanu konta**. "
                "Bot sprawdza widoczne na screenie transakcje i nie zapisuje obrazu."
            ),
            color=discord.Color.from_rgb(83, 101, 242),
        )
        embed.add_field(
            name="Dla firm",
            value=(
                "`/podatek` — dołącz screen finansów w kanale kategorii firmy.\n"
                "Każdy członek serwera może zgłosić wpłatę i potwierdzić własny odczyt.\n"
                "`/pomoc` — pokaż tę instrukcję.\n"
                "Wynik widzisz prywatnie; administratorzy mogą zmienić widoczność."
            ),
            inline=False,
        )
        embed.add_field(
            name="Dla administratorów",
            value=(
                "`/aktywna` · `/nieaktywna` — status firmy.\n"
                "`/usun_firme` — usuń firmę z tablicy.\n"
                "`/ulga` · `/muala` — ulga i reset tygodnia.\n"
                "`/ustaw_role_irs` — role weryfikacji i kar.\n"
                "`/audyt` — historia działań."
            ),
            inline=False,
        )
        embed.add_field(
            name="Płatności i kary",
            value=(
                "`/przypomnienie` — wiadomości dla firm bez zapłaty.\n"
                "`/kara_finansowa` · `/rejestr_kar` · `/usun_kare` — zarządzanie karami.\n"
                "`/oplac_kare` — potwierdzenie własnej wpłaty kary.\n"
                "`/eksport_kar` — eksport CSV rejestru kar."
            ),
            inline=False,
        )
        embed.add_field(
            name="Układ serwera i demo",
            value=(
                "`/szkic` — pełny układ serwera z rolami i kanałami.\n"
                "`/szkic2` — prywatna kategoria DOJ.\n"
                "`/wyczysc` — czyszczenie wiadomości bota.\n"
                "`/wyczysc_tabele` — usuń opublikowane tabele bez kasowania danych.\n"
                "`/usun_wszystko` — pełne usunięcie zasobów serwera.\n"
                "`/prezentacja` · `/zakoncz_prezentacje` — demo bota."
            ),
            inline=False,
        )
        embed.add_field(
            name="Konfiguracja",
            value=(
                "Właściciel aplikacji ustawia kanały komendami "
                "`/ustaw_kanal_podatkow` i `/ustaw_kanal_firm`. "
                "Pomoc techniczna i checklisty są dostępne w panelu desktopowym."
            ),
            inline=False,
        )
        embed.set_footer(text="Nie wysyłaj screenów zawierających dane osobowe.")
        return embed

    embed = discord.Embed(
        title="📘 IRS Help — tax verification",
        description=(
            "Tax is calculated as **12% of the account balance**. "
            "The bot checks visible transactions and does not store screenshots."
        ),
        color=discord.Color.from_rgb(83, 101, 242),
    )
    embed.add_field(
        name="For companies",
        value=(
            "`/podatek` — attach a finance screenshot in a company category channel.\n"
            "Any server member can submit a payment and confirm their own review.\n"
            "`/pomoc` — show this guide.\n"
            "Results are private by default; administrators can change visibility."
        ),
        inline=False,
    )
    embed.add_field(
        name="For administrators",
        value=(
            "`/aktywna` · `/nieaktywna` — company status.\n"
            "`/usun_firme` — remove a company.\n"
            "`/ulga` · `/muala` — relief and weekly reset.\n"
            "`/ustaw_role_irs` — payment and penalty roles.\n"
            "`/audyt` — admin action history."
        ),
        inline=False,
    )
    embed.add_field(
        name="Payments and penalties",
        value=(
            "`/przypomnienie` — payment reminders.\n"
            "`/kara_finansowa` · `/rejestr_kar` · `/usun_kare` — penalty management.\n"
            "`/oplac_kare` — confirm a penalty payment.\n"
            "`/eksport_kar` — export a CSV register."
        ),
        inline=False,
    )
    embed.add_field(
        name="Server layout and demo",
        value=(
            "`/szkic` — full server layout.\n"
            "`/szkic2` — DOJ category.\n"
            "`/wyczysc` — clean bot messages.\n"
            "`/wyczysc_tabele` — remove published tables without deleting data.\n"
            "`/usun_wszystko` — full reset.\n"
            "`/prezentacja` · `/zakoncz_prezentacje` — bot demo."
        ),
        inline=False,
    )
    embed.add_field(
        name="Setup",
        value=(
            "The application owner configures report and company-board channels with "
            "`/ustaw_kanal_podatkow` and `/ustaw_kanal_firm`. "
            "Desktop-panel setup checklists and support are available in the GUI."
        ),
        inline=False,
    )
    embed.set_footer(text="Do not upload screenshots containing personal information.")
    return embed


def get_company_category(channel):
    category = getattr(channel, "category", None)
    if category is None and isinstance(channel, discord.Thread):
        category = getattr(channel.parent, "category", None)
    return category


async def refresh_company_board(bot: "TaxBot", guild_id: int) -> bool:
    board = core.get_board(guild_id)
    if board is None:
        log.warning("Nie ustawiono kanału tabeli firm dla serwera %s.", guild_id)
        return False

    channel_id, message_id = board
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.exception("Nie można uzyskać dostępu do kanału tabeli firm %s.", channel_id)
        return False

    if not isinstance(channel, discord.TextChannel):
        log.error("Kanał tabeli firm %s nie jest kanałem tekstowym.", channel_id)
        return False

    embed = build_company_board_embed(guild_id)
    company_board_updated = False
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Nie można odczytać wiadomości tabeli firm %s.", message_id)
            return False
        if message:
            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                log.exception("Nie udało się zaktualizować tabeli firm.")
                return False
            company_board_updated = True

    if not company_board_updated:
        try:
            message = await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Nie udało się utworzyć tabeli firm.")
            return False
        core.set_board_message(guild_id, message.id)
    return await refresh_active_penalties_board(bot, guild_id)


async def refresh_active_penalties_board(
    bot: "TaxBot",
    guild_id: int,
) -> bool:
    board = core.get_penalty_board_message(guild_id)
    if board is None:
        return False
    channel_id, message_id = board
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.exception("Nie można uzyskać dostępu do tabeli kar na kanale %s.", channel_id)
        return False
    if not isinstance(channel, discord.TextChannel):
        log.error("Kanał tabeli kar %s nie jest kanałem tekstowym.", channel_id)
        return False

    embed = build_active_penalties_embed(guild_id)
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Nie można odczytać wiadomości tabeli kar %s.", message_id)
            return False
        if message:
            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                log.exception("Nie udało się zaktualizować tabeli kar.")
                return False
            return True

    try:
        message = await channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("Nie udało się utworzyć tabeli aktywnych kar.")
        return False
    core.set_penalty_board_message(guild_id, message.id)
    return True


class TaxBot(discord.Client):
    def __init__(self, guild_id=None):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.guild_id = guild_id
        self.background_loops = []
        config = core.load_config()
        core.configure_tesseract(config.get("tesseract_cmd"))
        self.ocr_profiles = core.normalize_ocr_profiles(
            config.get("ocr_profiles"),
            config.get("ocr_language", "eng"),
            config.get("ocr_layout", "auto"),
        )
        core.configure_ocr(
            config.get("ocr_language", "eng"),
            config.get("ocr_layout", "auto"),
        )
        configured_channel = str(config.get("audit_channel_id", "")).strip()
        if configured_channel and configured_channel.isdigit():
            self.audit_channel_id = int(configured_channel)
        else:
            self.audit_channel_id = None
            if configured_channel:
                log.error("Nieprawidłowe ID kanału raportowego w config.json.")

    async def close(self):
        loop_tasks = []
        for background_loop in self.background_loops:
            if background_loop.is_running():
                background_loop.cancel()
            task = background_loop.get_task()
            if task is not None:
                loop_tasks.append(task)
        if loop_tasks:
            results = await asyncio.gather(*loop_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.error(
                        "Błąd podczas zatrzymywania zadania cyklicznego.",
                        exc_info=(type(result), result, result.__traceback__),
                    )
        await super().close()

    async def setup_hook(self):
        if self.guild_id:
            guild = discord.Object(id=int(self.guild_id))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden as exc:
                if exc.code == 50001:
                    raise GuildCommandSyncError(
                        "Discord odrzucił synchronizację komend: bot nie ma dostępu "
                        "do skonfigurowanego serwera. Sprawdź, czy ID serwera w GUI "
                        "jest poprawne i czy ten bot został zaproszony na ten serwer "
                        "z zakresem applications.commands."
                    ) from exc
                raise
            log.info("Zsynchronizowano komendy na serwerze %s.", self.guild_id)
        else:
            await self.tree.sync()
            log.info("Zsynchronizowano komendy globalnie (mogą być widoczne do ok. 1 godz.).")


def build_bot(guild_id=None, on_ready=None) -> TaxBot:
    ready_callback = on_ready
    bot = TaxBot(guild_id)

    async def ready_listener():
        if bot.user is None:
            log.error("Discord wywołał on_ready bez danych zalogowanego użytkownika.")
            return
        log.info("Zalogowano jako %s (id: %s).", bot.user, bot.user.id)
        if not refresh_expiring_penalty_boards.is_running():
            refresh_expiring_penalty_boards.start()
        for guild in bot.guilds:
            if core.get_board(guild.id):
                await refresh_company_board(bot, guild.id)
        await restore_presentation_views()
        if ready_callback:
            ready_callback(str(bot.user))

    ready_listener.__name__ = "on_ready"
    bot.event(ready_listener)

    async def resolve_text_channel(channel_id: int | None, context: str):
        if channel_id is None:
            return None
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.exception("Nie udało się uzyskać kanału (%s) o ID %s.", context, channel_id)
                return None
        if not isinstance(channel, discord.TextChannel):
            log.error("Kanał (%s) o ID %s nie jest kanałem tekstowym.", context, channel_id)
            return None
        return channel

    async def notify_upcoming_penalties(guild: discord.Guild, today: date) -> None:
        companies_by_category = {
            company.category_id: company
            for company in core.list_companies(guild.id)
        }
        for penalty in core.list_unnotified_due_reminders(guild.id, today):
            embed = build_due_reminder_embed(penalty)
            company = companies_by_category.get(penalty.category_id)
            company_channel = await resolve_text_channel(
                company.channel_id if company else None,
                f"firma dla przypomnienia o karze #{penalty.id}",
            )
            audit_channel = await resolve_text_channel(
                bot.audit_channel_id,
                f"raport przed terminem kary #{penalty.id}",
            )
            company_channel_id = company.channel_id if company else None
            audit_channel_id = bot.audit_channel_id
            same_destination = (
                company_channel_id is not None
                and company_channel_id == audit_channel_id
            )
            if same_destination:
                company_notified = penalty.due_reminder_company_notified_at is not None
                audit_notified = penalty.due_reminder_audit_notified_at is not None
                if company_notified or audit_notified:
                    if not company_notified:
                        core.mark_penalty_due_reminder_notified(
                            guild.id, penalty.id, "company"
                        )
                    if not audit_notified:
                        core.mark_penalty_due_reminder_notified(
                            guild.id, penalty.id, "audit"
                        )
                else:
                    shared_channel = company_channel or audit_channel
                    if shared_channel is not None:
                        try:
                            await shared_channel.send(
                                embed=embed,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        except discord.HTTPException:
                            log.exception(
                                "Nie udało się wysłać przypomnienia o karze #%s do wspólnego kanału.",
                                penalty.id,
                            )
                        else:
                            core.mark_penalty_due_reminder_notified(
                                guild.id, penalty.id, "company"
                            )
                            core.mark_penalty_due_reminder_notified(
                                guild.id, penalty.id, "audit"
                            )
                continue

            if (
                penalty.due_reminder_company_notified_at is None
                and company_channel is not None
            ):
                try:
                    await company_channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.exception(
                        "Nie udało się wysłać przypomnienia o karze #%s do firmy.",
                        penalty.id,
                    )
                else:
                    core.mark_penalty_due_reminder_notified(
                        guild.id, penalty.id, "company"
                    )
            if (
                penalty.due_reminder_audit_notified_at is None
                and audit_channel is not None
            ):
                try:
                    await audit_channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.exception(
                        "Nie udało się wysłać przypomnienia o karze #%s na kanał raportowy.",
                        penalty.id,
                    )
                else:
                    core.mark_penalty_due_reminder_notified(
                        guild.id, penalty.id, "audit"
                    )

    async def publish_previous_week_penalty_summary(
        guild: discord.Guild,
        today: date,
    ) -> None:
        this_week_start, _ = core.tax_week_range(today)
        period_end = this_week_start
        period_start = period_end - timedelta(days=7)
        if core.weekly_penalty_report_sent(guild.id, period_start):
            return
        audit_channel = await resolve_text_channel(
            bot.audit_channel_id,
            f"tygodniowe zestawienie kar dla serwera {guild.id}",
        )
        if audit_channel is None:
            log.debug(
                "Pominięto tygodniowe zestawienie serwera %s: brak dostępnego kanału raportowego.",
                guild.id,
            )
            return
        summary = core.summarize_financial_penalties(
            guild.id,
            period_start,
            period_end,
            as_of=today,
        )
        try:
            await audit_channel.send(
                embed=build_weekly_penalty_summary_embed(
                    summary,
                    period_start,
                    period_end,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception(
                "Nie udało się wysłać tygodniowego zestawienia kar dla serwera %s.",
                guild.id,
            )
            return
        if not core.mark_weekly_penalty_report_sent(
            guild.id,
            period_start,
            datetime.now(timezone.utc),
        ):
            log.error(
                "Wysłano zestawienie dla serwera %s, ale nie udało się zapisać jego stanu.",
                guild.id,
            )

    @tasks.loop(minutes=5)
    async def refresh_expiring_penalty_boards():
        today = date.today()
        for guild in bot.guilds:
            if core.get_penalty_board_message(guild.id):
                await refresh_active_penalties_board(bot, guild.id)
            await notify_upcoming_penalties(guild, today)
            await publish_previous_week_penalty_summary(guild, today)
            for penalty in core.list_unnotified_overdue_penalties(guild.id):
                embed = build_overdue_penalty_embed(penalty)
                company = next(
                    (
                        item
                        for item in core.list_companies(guild.id)
                        if item.category_id == penalty.category_id
                    ),
                    None,
                )
                if core.mark_penalty_overdue_logged(guild.id, penalty.id):
                    log.warning(
                        "Kara #%s dla firmy %s (%s) jest po terminie od %s.",
                        penalty.id,
                        penalty.company_name,
                        core.format_stored_money(penalty.amount),
                        penalty.due_date,
                    )

                company_channel = await resolve_text_channel(
                    company.channel_id if company else None,
                    f"firma dla przeterminowanej kary #{penalty.id}",
                )
                audit_channel = await resolve_text_channel(
                    bot.audit_channel_id,
                    f"raport przeterminowanej kary #{penalty.id}",
                )
                company_channel_id = company.channel_id if company else None
                audit_channel_id = bot.audit_channel_id
                same_destination = (
                    company_channel_id is not None
                    and company_channel_id == audit_channel_id
                )
                if same_destination:
                    company_notified = penalty.company_overdue_notified_at is not None
                    audit_notified = penalty.audit_overdue_notified_at is not None
                    if company_notified or audit_notified:
                        if not company_notified:
                            core.mark_penalty_overdue_notified(
                                guild.id, penalty.id, "company"
                            )
                        if not audit_notified:
                            core.mark_penalty_overdue_notified(
                                guild.id, penalty.id, "audit"
                            )
                    else:
                        shared_channel = company_channel or audit_channel
                        if shared_channel is not None:
                            try:
                                await shared_channel.send(
                                    embed=embed,
                                    allowed_mentions=discord.AllowedMentions.none(),
                                )
                            except discord.HTTPException:
                                log.exception(
                                    "Nie udało się opublikować przeterminowanej kary #%s we wspólnym kanale.",
                                    penalty.id,
                                )
                            else:
                                core.mark_penalty_overdue_notified(
                                    guild.id, penalty.id, "company"
                                )
                                core.mark_penalty_overdue_notified(
                                    guild.id, penalty.id, "audit"
                                )
                    continue

                if (
                    penalty.company_overdue_notified_at is None
                    and company_channel is not None
                ):
                    try:
                        await company_channel.send(
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        log.exception(
                            "Nie udało się powiadomić firmy o przeterminowanej karze #%s.",
                            penalty.id,
                        )
                    else:
                        core.mark_penalty_overdue_notified(
                            guild.id, penalty.id, "company"
                        )
                if (
                    penalty.audit_overdue_notified_at is None
                    and audit_channel is not None
                ):
                    try:
                        await audit_channel.send(
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        log.exception(
                            "Nie udało się zapisać przeterminowanej kary #%s na kanale raportowym.",
                            penalty.id,
                        )
                    else:
                        core.mark_penalty_overdue_notified(
                            guild.id, penalty.id, "audit"
                        )

    @refresh_expiring_penalty_boards.before_loop
    async def before_refresh_expiring_penalty_boards():
        await bot.wait_until_ready()

    bot.background_loops.append(refresh_expiring_penalty_boards)

    async def is_application_owner(interaction: discord.Interaction) -> bool:
        application = await bot.application_info()
        owner = application.owner
        owner_id = owner.owner_id if isinstance(owner, discord.Team) else owner.id
        return interaction.user.id == owner_id

    @bot.tree.command(
        name="pomoc",
        description="Pokazuje instrukcję komend i konfiguracji bota IRS",
    )
    async def pomoc(interaction: discord.Interaction):
        locale = getattr(interaction.locale, "value", str(interaction.locale))
        language = "pl" if locale.lower().startswith("pl") else "en"
        await interaction.response.send_message(
            embed=build_help_embed(language),
            ephemeral=True,
        )

    @bot.tree.command(
        name="ustaw_kanal_podatkow",
        description="Ustawia kanał raportów podatkowych",
    )
    @app_commands.describe(kanal="Kanał tekstowy na raporty sprawdzonych screenów")
    async def ustaw_kanal_podatkow(
        interaction: discord.Interaction,
        kanal: discord.TextChannel,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await is_application_owner(interaction):
            await interaction.followup.send(
                "Tę komendę może wykonać tylko właściciel aplikacji bota.",
            )
            return

        guild = interaction.guild
        if guild is None or kanal.guild.id != guild.id:
            await interaction.followup.send(
                "Wybierz kanał z tego samego serwera.",
            )
            return

        bot_member = kanal.guild.me
        permissions = kanal.permissions_for(bot_member) if bot_member else None
        if permissions is None or not permissions.view_channel or not permissions.send_messages:
            await interaction.followup.send(
                "Bot nie ma uprawnień do wysyłania wiadomości na tym kanale.",
            )
            return

        cfg = core.load_config(use_env=False)
        cfg["audit_channel_id"] = str(kanal.id)
        try:
            core.save_config(cfg)
        except OSError:
            log.exception("Nie udało się zapisać kanału raportowego w config.json.")
            await interaction.followup.send(
                "Nie udało się zapisać ustawienia kanału. Sprawdź uprawnienia do config.json.",
            )
            return

        bot.audit_channel_id = kanal.id
        await interaction.followup.send(
            f"Kanał raportowy ustawiony na {kanal.mention}.",
        )

    @bot.tree.command(
        name="ustaw_kanal_firm",
        description="Ustawia kanał tabeli firm",
    )
    @app_commands.describe(kanal="Kanał tekstowy, w którym ma być tabela firm")
    async def ustaw_kanal_firm(
        interaction: discord.Interaction,
        kanal: discord.TextChannel,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await is_application_owner(interaction):
            await interaction.followup.send(
                "Tę komendę może wykonać tylko właściciel aplikacji bota."
            )
            return

        guild = interaction.guild
        if guild is None or kanal.guild.id != guild.id:
            await interaction.followup.send("Wybierz kanał z tego samego serwera.")
            return

        bot_member = kanal.guild.me
        permissions = kanal.permissions_for(bot_member) if bot_member else None
        if (
            permissions is None
            or not permissions.view_channel
            or not permissions.send_messages
            or not permissions.embed_links
        ):
            await interaction.followup.send(
                "Bot potrzebuje na tym kanale uprawnień do wyświetlania kanału, "
                "wysyłania wiadomości i osadzania linków."
            )
            return

        core.set_board_channel(guild.id, kanal.id)
        if not await refresh_company_board(bot, guild.id):
            await interaction.followup.send(
                "Nie udało się utworzyć tabeli. Sprawdź uprawnienia bota na wybranym kanale."
            )
            return
        await interaction.followup.send(
            f"Tabela firm została ustawiona na {kanal.mention}."
        )

    async def change_company_activity(
        interaction: discord.Interaction,
        active: bool,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("Ta komenda działa tylko na serwerze.")
            return
        if not interaction.permissions.administrator:
            await interaction.followup.send(
                "Status firmy mogą zmieniać tylko administratorzy serwera."
            )
            return

        category = get_company_category(interaction.channel)
        if category is None:
            await interaction.followup.send(
                "Użyj tej komendy w kanale należącym do kategorii firmy."
            )
            return
        if core.is_presentation_category(interaction.guild.id, category.id):
            await interaction.followup.send(
                "Kanały prezentacyjne są tylko pokazem. Komendy nie zmieniają w nich "
                "firm ani rejestrów."
            )
            return

        company_name = core.normalize_company_name(category.name)
        previous_company = next(
            (
                company
                for company in core.list_companies(interaction.guild.id)
                if company.category_id == category.id
            ),
            None,
        )
        core.set_company_active(
            interaction.guild.id,
            category.id,
            company_name,
            active,
            interaction.channel.id if active and interaction.channel else None,
        )
        previous_active = previous_company.active if previous_company else None
        if previous_active is None or previous_active != active:
            previous_label = (
                "brak wpisu" if previous_active is None
                else "aktywna" if previous_active
                else "nieaktywna"
            )
            core.record_admin_audit(
                interaction.guild.id,
                interaction.user.id,
                str(interaction.user),
                "status_firmy",
                f"Status: {previous_label} → {'aktywna' if active else 'nieaktywna'}.",
                company_name,
            )
        board_updated = await refresh_company_board(bot, interaction.guild.id)
        status = "aktywna" if active else "nieaktywna"
        message = f"Firma **{company_name}** została oznaczona jako **{status}**."
        if not board_updated:
            message += (
                " Zmiana została zapisana, ale nie udało się odświeżyć tabeli. "
                "Właściciel bota powinien sprawdzić `/ustaw_kanal_firm` i uprawnienia."
            )
        await interaction.followup.send(message)

    @bot.tree.command(name="aktywna", description="Oznacza firmę jako aktywną")
    async def aktywna(interaction: discord.Interaction):
        await change_company_activity(interaction, True)

    @bot.tree.command(name="nieaktywna", description="Oznacza firmę jako nieaktywną")
    async def nieaktywna(interaction: discord.Interaction):
        await change_company_activity(interaction, False)

    async def require_guild_admin(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.", ephemeral=True
            )
            return False
        if not interaction.permissions.administrator:
            await interaction.response.send_message(
                "Tę komendę mogą wykonać tylko administratorzy serwera.",
                ephemeral=True,
            )
            return False
        return True

    async def require_irs_permission(
        interaction: discord.Interaction,
        permission: str,
    ) -> bool:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.",
                ephemeral=True,
            )
            return False
        if interaction.permissions.administrator:
            return True

        payment_role_id, penalty_role_id = core.get_guild_role_settings(guild.id)
        role_id = (
            payment_role_id if permission == "payment" else penalty_role_id
        )
        permission_label = (
            "weryfikacji wpłat"
            if permission == "payment"
            else "zarządzania karami"
        )
        if role_id is None:
            await interaction.response.send_message(
                "Ta funkcja jest obecnie dostępna tylko dla administratorów. "
                f"Administrator może przydzielić rolę {permission_label} przez "
                "`/ustaw_role_irs`.",
                ephemeral=True,
            )
            return False
        member_role_ids = {
            role.id for role in getattr(interaction.user, "roles", ())
        }
        if role_id in member_role_ids:
            return True
        await interaction.response.send_message(
            f"Do tej czynności potrzebujesz skonfigurowanej roli "
            f"{permission_label} albo uprawnień administratora.",
            ephemeral=True,
        )
        return False

    @bot.tree.command(
        name="ustaw_role_irs",
        description="Ustawia role weryfikacji wpłat i zarządzania karami",
    )
    @app_commands.choices(
        typ=[
            app_commands.Choice(name="Weryfikacja wpłat", value="payment"),
            app_commands.Choice(name="Zarządzanie karami", value="penalties"),
        ]
    )
    @app_commands.describe(
        typ="Wybierz zakres roli",
        rola="Wybierz rolę; pominięcie pola usuwa przypisaną rolę",
    )
    async def ustaw_role_irs(
        interaction: discord.Interaction,
        typ: app_commands.Choice[str],
        rola: discord.Role | None = None,
    ):
        if not await require_guild_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return
        if rola is not None and (rola.guild.id != guild.id or rola.is_default()):
            await interaction.response.send_message(
                "Wybierz niestandardową rolę z tego serwera.",
                ephemeral=True,
            )
            return

        payment_role_id, penalty_role_id = core.get_guild_role_settings(guild.id)
        if typ.value == "payment":
            payment_role_id = rola.id if rola else None
            role_label = "weryfikacji wpłat"
        else:
            penalty_role_id = rola.id if rola else None
            role_label = "zarządzania karami"
        core.set_guild_role_settings(
            guild.id,
            payment_role_id,
            penalty_role_id,
        )
        role_description = rola.mention if rola else "brak (tylko administratorzy)"
        record_admin_action(
            interaction,
            "ustawienie_roli",
            f"Rola {role_label}: {role_description}.",
        )
        await interaction.response.send_message(
            f"Rola {role_label} ustawiona na {role_description}. "
            "Administratorzy zachowują pełny dostęp.",
            ephemeral=True,
        )

    async def delete_presentation_resources(guild: discord.Guild) -> tuple[int, list[str]]:
        session = core.get_presentation_session(guild.id)
        if session is None:
            return 0, []
        failures: list[str] = []
        deleted = 0
        resources = core.list_presentation_resources(guild.id)
        for resource_id, resource_type in resources:
            if resource_type != "channel":
                continue
            channel = guild.get_channel(resource_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(resource_id)
                except discord.NotFound:
                    core.untrack_presentation_resource(guild.id, resource_id)
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "Nie udało się pobrać kanału demo %s do usunięcia.",
                        resource_id,
                    )
                    failures.append(f"kanał `{resource_id}`")
                    continue
            if not isinstance(channel, discord.TextChannel):
                failures.append(f"zasób `{resource_id}` nie jest kanałem tekstowym")
                continue
            if channel.guild.id != guild.id or channel.category_id != session.category_id:
                failures.append(
                    f"kanał `{channel.name}` został przeniesiony poza kategorię prezentacji"
                )
                continue
            if not channel.name.startswith("irs-demo"):
                failures.append(
                    f"kanał `{channel.name}` nie ma już nazwy kanału demonstracyjnego"
                )
                continue
            try:
                await channel.delete(reason=f"Zakończenie prezentacji przez {guild.name}")
            except discord.HTTPException:
                log.exception("Nie udało się usunąć kanału demo %s.", resource_id)
                failures.append(f"kanał `{channel.name}`")
            else:
                core.untrack_presentation_resource(guild.id, resource_id)
                deleted += 1

        for resource_id, resource_type in core.list_presentation_resources(guild.id):
            if resource_type != "category":
                continue
            category = guild.get_channel(resource_id)
            if category is None:
                try:
                    category = await bot.fetch_channel(resource_id)
                except discord.NotFound:
                    core.untrack_presentation_resource(guild.id, resource_id)
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Nie udało się pobrać kategorii demo %s do usunięcia.", resource_id)
                    failures.append(f"kategoria `{resource_id}`")
                    continue
            if not isinstance(category, discord.CategoryChannel):
                failures.append(f"zasób `{resource_id}` nie jest kategorią")
                continue
            if category.guild.id != guild.id or not category.name.startswith("IRS · Prezentacja"):
                failures.append(
                    f"kategoria `{category.name}` nie wygląda już na kategorię prezentacji"
                )
                continue
            if category.channels:
                failures.append(
                    f"kategoria `{category.name}` nadal zawiera nieśledzone kanały — sprawdź ją ręcznie"
                )
                continue
            try:
                await category.delete(reason=f"Zakończenie prezentacji przez {guild.name}")
            except discord.HTTPException:
                log.exception("Nie udało się usunąć kategorii demo %s.", resource_id)
                failures.append(f"kategoria `{category.name}`")
            else:
                core.untrack_presentation_resource(guild.id, resource_id)
                deleted += 1
        if not core.list_presentation_resources(guild.id):
            core.delete_presentation_session(guild.id)
        return deleted, failures

    async def create_demo_channel(
        guild: discord.Guild,
        session: core.PresentationSession,
        base_name: str,
        topic: str,
    ) -> discord.TextChannel:
        category = guild.get_channel(session.category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("Nie znaleziono kategorii prezentacji.")
        name = base_name
        suffix = 2
        existing_names = {channel.name for channel in guild.channels}
        while name in existing_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        channel = await guild.create_text_channel(
            name,
            category=category,
            topic=topic,
            reason="Krok prezentacji możliwości bota IRS",
        )
        try:
            core.track_presentation_resource(guild.id, channel.id, "channel")
        except Exception:
            try:
                await channel.delete(reason="Wycofanie niezapisanego kanału prezentacji")
            except discord.HTTPException:
                log.exception("Nie udało się wycofać kanału demo %s.", channel.id)
            raise
        return channel

    async def send_demo_step(
        interaction: discord.Interaction,
        session: core.PresentationSession,
        stage: int,
    ) -> tuple[discord.TextChannel, discord.Message]:
        guild = interaction.guild
        if guild is None:
            raise RuntimeError("Prezentacja może działać wyłącznie na serwerze.")
        steps = {
            1: (
                "irs-demo-firmy",
                "Tablica i przykładowe kanały firm",
                build_presentation_company_embed(),
            ),
            2: (
                "irs-demo-podatki",
                "Weryfikacja podatku przez OCR",
                build_presentation_tax_embed(),
            ),
            3: (
                "irs-demo-kary",
                "Kary, przypomnienia i statusy",
                build_presentation_penalty_embed(),
            ),
            4: (
                "irs-demo-raporty",
                "Zestawienie tygodniowe i eksport CSV",
                build_presentation_intro_embed(),
            ),
        }
        if stage not in steps:
            raise ValueError(f"Unknown presentation stage: {stage}")
        channel_name, topic, embed = steps[stage]
        narration = {
            1: (
                "No to zaczynamy od firm. Najpierw tablica, potem zajrzymy do dwóch przykładowych kanałów.",
                "Zwróć uwagę, że status aktywności, podatek i kara są pokazane oddzielnie.",
            ),
            2: (
                "Teraz podatki. Bot odczytuje saldo i widoczne transakcje, a 12% salda to kwota do porównania.",
                "Jeśli opis przelewu nie jest jednoznaczny, wynik zatrzyma się do zatwierdzenia — nie będę zgadywać.",
            ),
            3: (
                "Czas na kary. Każdy wpis ma swój numer, kwotę, powód i termin.",
                "Pokażę też, jak wyglądają przypomnienia oraz status po terminie.",
            ),
            4: (
                "Ostatni etap: raporty, eksport i role. Zobaczysz, kto może weryfikować wpłaty, a kto zarządza karami.",
                "Wszystko na tym kanale nadal jest przykładowe i nie zapisuje się do rejestru.",
            ),
        }[stage]
        embed.description = (
            f"**Krok {stage} z 4**\n{embed.description or ''}"
        )
        existing_resource_ids = {
            resource_id
            for resource_id, _ in core.list_presentation_resources(guild.id)
        }
        channel = await create_demo_channel(guild, session, channel_name, topic)
        try:
            for index, narration_message in enumerate(narration):
                if index:
                    await asyncio.sleep(0.6)
                await channel.send(
                    narration_message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            if stage == 1:
                company_samples = (
                    (
                        "irs-demo-firma-upn-atom",
                        "Up-n-Atom · firma przykładowa",
                        "🟢 Aktywna · ✅ Podatek zapłacony",
                    ),
                    (
                        "irs-demo-firma-auto-exotic",
                        "Auto Exotic · firma przykładowa",
                        "🟢 Aktywna · 🔴 Podatek nieopłacony · Kara #10001",
                    ),
                )
                for company_channel_name, display_name, status in company_samples:
                    company_channel = await create_demo_channel(
                        guild,
                        session,
                        company_channel_name,
                        f"Przykładowy kanał firmy {display_name}",
                    )
                    company_embed = discord.Embed(
                        title=f"🏢 {display_name}",
                        description=(
                            f"**Przykładowy status:** {status}\n\n"
                            "W prawdziwej konfiguracji `/aktywna` zapisuje firmę na "
                            "podstawie kategorii. Ten kanał jest tylko pokazem: komendy "
                            "produkcyjne są tu zablokowane i nic nie zapisują."
                        ),
                        color=discord.Color.from_rgb(83, 101, 242),
                    )
                    company_embed.set_footer(text="IRS · Przykładowa firma · bez wpisu w bazie")
                    await company_channel.send(
                        embed=company_embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                embed.add_field(
                    name="Przykładowe kanały firm",
                    value=(
                        "Bot utworzył osobne kanały demonstracyjne dla **Up-n-Atom** "
                        "i **Auto Exotic**. Są oznaczone jako demo i nie mogą zapisać "
                        "zmian do prawdziwego rejestru."
                    ),
                    inline=False,
                )
            elif stage == 2:
                embed.add_field(
                    name="Co dzieje się w prawdziwym użyciu?",
                    value=(
                        "Użytkownik wysyła screen przez `/podatek`; OCR odczytuje saldo "
                        "i transakcje, liczy wymagane 12%, a wynik potwierdza prywatnie. "
                        "Nieczytelny screen wymaga ponownego przesłania."
                    ),
                    inline=False,
                )
                embed.add_field(
                    name="Różne układy screenów",
                    value=(
                        "W panelu GUI można ustawić osobny język OCR i układ dla profilu "
                        "automatycznego, panelu finansów i karty transakcji. Profil wybiera "
                        "się też w `/podatek` lub `/oplac_kare`; niejednoznaczne wpłaty "
                        "czekają na zatwierdzenie zamiast zmieniać rejestr automatycznie."
                    ),
                    inline=False,
                )
            elif stage == 3:
                embed.add_field(
                    name="Role i zakres dostępu",
                    value=(
                        "Administrator może przypisać osobną rolę do weryfikacji wpłat "
                        "i osobną do zarządzania karami komendą `/ustaw_role_irs`. "
                        "Administratorzy zawsze zachowują dostęp; jeśli rola nie jest "
                        "ustawiona, dana funkcja pozostaje tylko dla administratorów."
                    ),
                    inline=False,
                )
                demo_summary = core.WeeklyPenaltySummary(
                    3, Decimal("8000"), 1, Decimal("1000"),
                    1, Decimal("5000"), 1, Decimal("2000"),
                )
                week_end = core.tax_week_range()[0] - timedelta(days=7)
                await channel.send(
                    embed=build_weekly_penalty_summary_embed(
                        demo_summary,
                        week_end - timedelta(days=7),
                        week_end,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            elif stage == 4:
                embed.title = "🧭 Raporty i wsparcie · krok końcowy"
                embed.description = (
                    "Bot automatycznie przypomina o karze dzień przed terminem, "
                    "ogłasza zaległość, wysyła tygodniowe podsumowanie i pozwala "
                    "administratorowi pobrać rejestr przez `/eksport_kar`."
                )
                example_penalty = core.FinancialPenalty(
                    id=10001,
                    guild_id=guild.id,
                    category_id=session.category_id,
                    company_name="Auto Exotic · DEMO",
                    amount="5000",
                    reason="Przykładowa kara demonstracyjna",
                    due_date=(date.today() + timedelta(days=7)).isoformat(),
                    imposed_by_id=interaction.user.id,
                    imposed_by_name=str(interaction.user),
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    paid_at=None,
                    paid_by_id=None,
                    paid_by_name=None,
                    company_overdue_notified_at=None,
                    audit_overdue_notified_at=None,
                    overdue_logged_at=None,
                    due_reminder_company_notified_at=None,
                    due_reminder_audit_notified_at=None,
                )
                await channel.send(
                    content=(
                        "Przykładowy eksport CSV — ten wiersz jest demonstracyjny "
                        "i nie pochodzi z rejestru."
                    ),
                    file=discord.File(
                        io.BytesIO(build_penalty_csv([example_penalty])),
                        filename="irs-prezentacja-rejestru-kar.csv",
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                embed.add_field(
                    name="Prezentacja ukończona",
                    value=(
                        "Przykładowe firmy i kary nie zostały zapisane w bazie. "
                        "Administrator może usunąć całą prezentację poleceniem "
                        "`/zakoncz_prezentacje`."
                    ),
                    inline=False,
                )

            if stage < 4:
                next_view = PresentationStepView(
                    session.requester_id,
                    advance_presentation,
                    "Dalej · pokaż następną funkcję",
                    stage=stage,
                )
                message = await channel.send(
                    embed=embed,
                    view=next_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                message = await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return channel, message
        except Exception:
            for resource_id, resource_type in core.list_presentation_resources(guild.id):
                if (
                    resource_type != "channel"
                    or resource_id in existing_resource_ids
                ):
                    continue
                resource = guild.get_channel(resource_id)
                if not isinstance(resource, discord.TextChannel):
                    core.untrack_presentation_resource(guild.id, resource_id)
                    continue
                if (
                    resource.category_id != session.category_id
                    or not resource.name.startswith("irs-demo")
                ):
                    continue
                try:
                    await resource.delete(reason="Wycofanie nieudanego kroku prezentacji")
                except discord.HTTPException:
                    log.exception("Nie udało się wycofać kanału demo %s.", resource_id)
                else:
                    core.untrack_presentation_resource(guild.id, resource_id)
            raise

    async def advance_presentation(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or interaction.message is None:
            await interaction.response.send_message(
                "Nie można odczytać bieżącego kroku prezentacji.",
                ephemeral=True,
            )
            return
        session = core.get_presentation_session(guild.id)
        if session is None:
            await interaction.response.send_message(
                "Ta prezentacja nie jest już aktywna.",
                ephemeral=True,
            )
            return
        if interaction.user.id != session.requester_id:
            await interaction.response.send_message(
                "Kolejny krok może uruchomić tylko osoba, która rozpoczęła demo.",
                ephemeral=True,
            )
            return
        if (
            interaction.message.id != session.current_message_id
            or interaction.channel_id != session.current_channel_id
        ):
            await interaction.response.send_message(
                "To nie jest już aktywny krok. Otwórz najnowszy kanał prezentacji.",
                ephemeral=True,
            )
            return
        if session.stage >= 4:
            await interaction.response.send_message(
                "Demo jest już ukończone. Administrator może użyć `/zakoncz_prezentacje`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.followup.send(
                "Jasne — przygotowuję następny etap. Za chwilę napiszę, co właśnie "
                "pokazuję, i podam przykład.",
                ephemeral=True,
            )
        except discord.HTTPException:
            log.exception("Nie udało się wysłać wiadomości o postępie prezentacji.")
        try:
            channel, message = await send_demo_step(
                interaction,
                session,
                session.stage + 1,
            )
        except (discord.HTTPException, RuntimeError, ValueError):
            log.exception(
                "Nie udało się utworzyć kroku %s prezentacji serwera %s.",
                session.stage + 1,
                guild.id,
            )
            await interaction.followup.send(
                "Nie udało się utworzyć następnego kroku. Przycisk został zablokowany, "
                "aby uniknąć powielania kanałów. Administrator powinien użyć "
                "`/zakoncz_prezentacje` i uruchomić pokaz ponownie.",
                ephemeral=True,
            )
            return
        if not core.update_presentation_progress(
            guild.id,
            session.stage + 1,
            channel.id,
            message.id,
        ):
            log.error(
                "Utworzono krok prezentacji %s na serwerze %s, ale nie zapisano postępu.",
                session.stage + 1,
                guild.id,
            )
            await interaction.followup.send(
                "Krok powstał, ale nie udało się zapisać postępu w bazie. "
                "Przycisk został wyłączony, aby uniknąć duplikatów. Administrator "
                "powinien użyć `/zakoncz_prezentacje` i rozpocząć pokaz ponownie.",
                ephemeral=True,
            )
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                log.exception("Nie udało się wyłączyć przycisku po błędzie zapisu kroku demo.")
            return
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            log.exception("Nie udało się wyłączyć przycisku poprzedniego kroku demo.")
        await interaction.followup.send(
            f"Następny etap jest gotowy: {channel.mention}. "
            "Przejdź do kanału i kliknij przycisk, gdy chcesz zobaczyć kolejną funkcję.",
            ephemeral=True,
        )

    async def restore_presentation_views() -> None:
        for session in core.list_presentation_sessions():
            if session.stage >= 4 or session.current_message_id is None:
                continue
            guild = bot.get_guild(session.guild_id)
            if guild is None:
                continue
            label = (
                "🚀 Rozpocznij demo"
                if session.stage == 0
                else "Dalej · pokaż następną funkcję"
            )
            bot.add_view(
                PresentationStepView(
                    session.requester_id,
                    advance_presentation,
                    label,
                    stage=session.stage,
                ),
                message_id=session.current_message_id,
            )

    async def create_presentation(interaction: discord.Interaction) -> str:
        guild = interaction.guild
        if guild is None:
            return "Nie udało się uruchomić prezentacji poza serwerem."
        if not interaction.permissions.administrator:
            return "Uprawnienia administratora zmieniły się — prezentacja nie została uruchomiona."
        if core.get_presentation_session(guild.id) is not None:
            return (
                "Na tym serwerze trwa już prezentacja. Użyj `/zakoncz_prezentacje`, "
                "aby ją usunąć, albo przejdź do istniejącego kanału `irs-demo-start`."
            )
        bot_member = guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_channels
            or not bot_member.guild_permissions.send_messages
            or not bot_member.guild_permissions.embed_links
        ):
            return (
                "Bot potrzebuje uprawnień **Zarządzanie kanałami**, "
                "**Wysyłanie wiadomości** i **Osadzanie linków**. "
                "Nie utworzono żadnych elementów."
            )

        category_name = "IRS · Prezentacja"
        suffix = 2
        while any(category.name == category_name for category in guild.categories):
            category_name = f"IRS · Prezentacja {suffix}"
            suffix += 1
        created_category: discord.CategoryChannel | None = None
        start_channel: discord.TextChannel | None = None
        registered_session = False
        try:
            created_category = await guild.create_category(
                category_name,
                reason=f"Prezentacja bota IRS uruchomiona przez {interaction.user}",
            )
            start_channel = await create_demo_channel(
                guild,
                core.PresentationSession(
                    guild_id=guild.id,
                    category_id=created_category.id,
                    requester_id=interaction.user.id,
                    stage=0,
                    current_channel_id=0,
                    current_message_id=None,
                ),
                "irs-demo-start",
                "Pierwszy krok interaktywnej prezentacji bota IRS",
            )
            if not core.register_presentation_session(
                guild.id,
                created_category.id,
                interaction.user.id,
                start_channel.id,
            ):
                raise RuntimeError("Na serwerze uruchomiono już inną prezentację.")
            registered_session = True
            intro = build_presentation_intro_embed()
            intro.add_field(
                name="Zacznij tutaj",
                value=(
                    "To jedyny kanał utworzony na start. Kliknij **Rozpocznij demo**, "
                    "a bot utworzy pierwsze kanały firm. Każdy kolejny etap pojawi się "
                    "dopiero po kliknięciu przycisku w poprzednim kroku."
                ),
                inline=False,
            )
            view = PresentationStepView(
                interaction.user.id,
                advance_presentation,
                "🚀 Rozpocznij demo",
            )
            message = await start_channel.send(
                content=(
                    "Cześć! Tu zaczyna się prezentacja IRS. Będę prowadzić Cię "
                    "krok po kroku — kliknij przycisk poniżej, kiedy będziesz gotowy. "
                    "Każdy etap pokażę w osobnym kanale, a wszystkie dane są przykładowe."
                ),
                embed=intro,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if not core.update_presentation_progress(
                guild.id,
                0,
                start_channel.id,
                message.id,
            ):
                raise RuntimeError("Nie udało się zapisać wiadomości startowej prezentacji.")
        except (discord.HTTPException, RuntimeError):
            log.exception("Nie udało się przygotować kanału startowego prezentacji.")
            cleanup_complete = True
            if start_channel is not None:
                try:
                    await start_channel.delete(reason="Wycofanie nieudanej prezentacji")
                except discord.HTTPException:
                    log.exception("Nie udało się usunąć kanału startowego demo.")
                    cleanup_complete = False
                else:
                    core.untrack_presentation_resource(guild.id, start_channel.id)
            if created_category is not None:
                try:
                    await created_category.delete(reason="Wycofanie nieudanej prezentacji")
                except discord.HTTPException:
                    log.exception("Nie udało się usunąć kategorii startowej demo.")
                    cleanup_complete = False
                else:
                    core.untrack_presentation_resource(guild.id, created_category.id)
            if registered_session and cleanup_complete:
                core.delete_presentation_session(guild.id)
            return (
                "Nie udało się przygotować kroku startowego. Sprawdź uprawnienia "
                "do tworzenia kategorii i kanałów. "
                + (
                    "Nieudane zasoby zostały usunięte."
                    if cleanup_complete
                    else "Część zasobów nadal istnieje; prezentacja pozostała w rejestrze, "
                    "aby `/zakoncz_prezentacje` mogło ponowić sprzątanie."
                )
            )

        record_admin_action(
            interaction,
            "prezentacja",
            f"Rozpoczęto krokową prezentację w kategorii {category_name}; utworzono "
            "wyłącznie kanał irs-demo-start.",
        )
        return (
            f"Utworzono kategorię {created_category.mention} i tylko pierwszy kanał "
            f"{start_channel.mention}. Przejdź tam i kliknij **Rozpocznij demo**. "
            "Kolejne kanały będą tworzone dopiero na żądanie przyciskiem."
        )

    @bot.tree.command(
        name="prezentacja",
        description="Tworzy odizolowaną prezentację możliwości bota IRS",
    )
    async def prezentacja(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return
        bot_member = guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_channels
            or not bot_member.guild_permissions.send_messages
            or not bot_member.guild_permissions.embed_links
        ):
            await interaction.response.send_message(
                "Bot potrzebuje uprawnień **Zarządzanie kanałami**, "
                "**Wysyłanie wiadomości** i **Osadzanie linków**, aby utworzyć "
                "kategorię i opisać kanały demonstracyjne.",
                ephemeral=True,
            )
            return

        warning = discord.Embed(
            title="⚠️ Potwierdź uruchomienie prezentacji",
            description=(
                "Po potwierdzeniu bot utworzy kategorię i tylko pierwszy kanał "
                "`irs-demo-start`. Dopiero gdy klikniesz tam **Rozpocznij demo**, "
                "będzie tworzyć kanały kolejnych etapów i przykładowych firm. "
                "Każdy krok wymaga osobnego kliknięcia **Dalej**.\n\n"
                "Bot nie zmieni istniejących kanałów, ustawień ani rejestrów firm i kar. "
                "Zapisze tylko informację o uruchomieniu pokazu w dzienniku audytu. "
                "Przykładowe dane nie są prawdziwymi wpłatami. Nowe elementy usuwa się "
                "ręcznie po pokazie."
            ),
            color=discord.Color.from_rgb(219, 140, 55),
        )
        warning.set_footer(text="Potwierdzenie jest ważne przez 60 sekund")
        await interaction.response.send_message(
            embed=warning,
            view=PresentationConfirmView(interaction.user.id, create_presentation),
            ephemeral=True,
        )

    @bot.tree.command(
        name="zakoncz_prezentacje",
        description="Usuwa kanały i kategorię utworzone przez prezentację IRS",
    )
    async def zakoncz_prezentacje(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return
        session = core.get_presentation_session(guild.id)
        if session is None:
            await interaction.response.send_message(
                "Na tym serwerze nie ma zapisanej aktywnej prezentacji.",
                ephemeral=True,
            )
            return
        resources = core.list_presentation_resources(guild.id)
        channel_count = sum(kind == "channel" for _, kind in resources)
        category_count = sum(kind == "category" for _, kind in resources)

        async def finish_presentation() -> str:
            if not interaction.permissions.administrator:
                return "Uprawnienia administratora zmieniły się — nie usunięto żadnych kanałów."
            deleted, failures = await delete_presentation_resources(guild)
            if deleted:
                record_admin_action(
                    interaction,
                    "zakonczenie_prezentacji",
                    f"Usunięto {deleted} śledzonych zasobów prezentacji.",
                )
            if failures:
                return (
                    f"Usunięto **{deleted}** zasobów. Nie udało się usunąć: "
                    + ", ".join(failures)
                    + ". Prezentacja pozostała zapisana, aby można było ponowić "
                    "sprzątanie. Nieśledzone elementy kategorii nie zostały usunięte."
                )
            return (
                f"Prezentacja zakończona. Usunięto **{deleted}** śledzonych elementów "
                f"({channel_count} kanałów i {category_count} kategorii). "
                "Pozostałe kanały serwera i rejestry nie zostały zmienione."
            )

        await interaction.response.send_message(
            "Czy na pewno zakończyć prezentację i usunąć wyłącznie zapisane kanały "
            f"demo oraz ich kategorię (**{channel_count} kanałów, "
            f"{category_count} kategorii**)? Przeniesione lub nieśledzone kanały "
            "zostaną zachowane.",
            view=ConfirmActionView(interaction.user.id, finish_presentation),
            ephemeral=True,
        )

    def company_category_for(interaction: discord.Interaction):
        category = get_company_category(interaction.channel)
        if (
            category is None
            or interaction.guild is None
            or core.is_presentation_category(interaction.guild.id, category.id)
        ):
            return None, None
        return category, core.normalize_company_name(category.name)

    def record_admin_action(
        interaction: discord.Interaction,
        action: str,
        details: str,
        company_name: str | None = None,
    ) -> None:
        if interaction.guild is None:
            raise RuntimeError("Administrator audit requires a guild interaction")
        core.record_admin_audit(
            interaction.guild.id,
            interaction.user.id,
            str(interaction.user),
            action,
            details,
            company_name,
        )

    def find_named_role(guild: discord.Guild, name: str) -> discord.Role | None:
        return next(
            (role for role in guild.roles if role.name.casefold() == name.casefold()),
            None,
        )

    def find_named_category(
        guild: discord.Guild,
        name: str,
    ) -> discord.CategoryChannel | None:
        return next(
            (
                category
                for category in guild.categories
                if category.name.casefold() == name.casefold()
            ),
            None,
        )

    async def ensure_category(
        guild: discord.Guild,
        name: str,
        overwrites: dict | None = None,
    ) -> tuple[discord.CategoryChannel, bool]:
        category = find_named_category(guild, name)
        if category is not None:
            if overwrites is not None:
                await category.edit(overwrites=overwrites)
                for channel in category.channels:
                    await channel.edit(sync_permissions=True)
            return category, False
        category = await guild.create_category(name, overwrites=overwrites)
        return category, True

    async def ensure_text_channel(
        guild: discord.Guild,
        category: discord.CategoryChannel,
        name: str,
    ) -> bool:
        if any(channel.name.casefold() == name.casefold() for channel in category.text_channels):
            return False
        await guild.create_text_channel(name, category=category)
        return True

    def role_color_for(name: str, index: int = 0) -> discord.Color:
        palette = (
            discord.Color.from_rgb(30, 144, 255),
            discord.Color.from_rgb(255, 128, 0),
            discord.Color.from_rgb(34, 197, 94),
            discord.Color.from_rgb(168, 85, 247),
            discord.Color.from_rgb(239, 68, 68),
            discord.Color.from_rgb(250, 204, 21),
            discord.Color.from_rgb(6, 182, 212),
            discord.Color.from_rgb(236, 72, 153),
        )
        digest = sum(ord(ch) for ch in name.casefold()) + index
        return palette[digest % len(palette)]

    async def ensure_role(
        guild: discord.Guild,
        name: str,
        color: discord.Color | None = None,
    ) -> tuple[discord.Role, bool]:
        role = find_named_role(guild, name)
        if role is not None:
            return role, False
        role = await guild.create_role(name=name, color=color or role_color_for(name), mentionable=False)
        return role, True

    async def build_server_skeleton(
        guild: discord.Guild,
        interaction: discord.Interaction,
    ) -> str:
        created_roles = 0
        created_categories = 0
        created_channels = 0
        try:
            bot_member = guild.me
            for category_name, channel_names in COMPANY_CHANNEL_LAYOUT:
                company_role, role_created = await ensure_role(
                    guild,
                    category_name,
                    role_color_for(category_name),
                )
                created_roles += int(role_created)
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=False,
                    ),
                    company_role: discord.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=True,
                    ),
                }
                if bot_member is not None:
                    overwrites[bot_member] = discord.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=True,
                        embed_links=True,
                        attach_files=True,
                    )
                category, category_created = await ensure_category(
                    guild,
                    category_name,
                    overwrites,
                )
                created_categories += int(category_created)
                for channel_name in channel_names:
                    created_channels += int(
                        await ensure_text_channel(
                            guild,
                            category,
                            channel_name,
                        )
                    )

            for category_name, channel_names in COMMUNITY_CATEGORY_LAYOUT:
                category, category_created = await ensure_category(
                    guild,
                    category_name,
                )
                created_categories += int(category_created)
                for channel_name in channel_names:
                    created_channels += int(
                        await ensure_text_channel(guild, category, channel_name)
                    )

            for channel_name in COMMUNITY_ROOT_CHANNELS:
                if not any(
                    channel.category is None
                    and channel.name.casefold() == channel_name.casefold()
                    for channel in guild.text_channels
                ):
                    await guild.create_text_channel(channel_name)
                    created_channels += 1
        except discord.HTTPException:
            log.exception("Nie udało się dokończyć tworzenia szkicu serwera.")
            return (
                "Discord przerwał tworzenie układu. Część zasobów mogła już powstać; "
                "uruchom `/szkic` ponownie, aby dokończyć.\n"
                f"Utworzono: {created_categories} kategorii, {created_channels} kanałów "
                f"i {created_roles} ról."
            )

        record_admin_action(
            interaction,
            "szkic_serwera",
            f"Utworzono {created_categories} kategorii, {created_channels} kanałów "
            f"i {created_roles} ról; istniejące zasoby pozostawiono.",
        )
        return (
            "Gotowe. "
            f"Utworzono **{created_categories}** kategorii, **{created_channels}** kanałów "
            f"i **{created_roles}** ról. Istniejące elementy zachowano. "
            "Kategorie firm są widoczne dla właściwej roli firmy, administratorów "
            "i bota. Członków przypisz do ról ręcznie."
        )

    async def build_doj_skeleton(
        guild: discord.Guild,
        interaction: discord.Interaction,
    ) -> str:
        created_roles = 0
        created_categories = 0
        created_channels = 0
        try:
            irs_role, irs_created = await ensure_role(
                guild,
                "🛡️ IRS",
                discord.Color.from_rgb(59, 130, 246),
            )
            doj_role, doj_created = await ensure_role(
                guild,
                "⚖️ DOJ",
                discord.Color.from_rgb(220, 38, 38),
            )
            created_roles += int(irs_created) + int(doj_created)

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=False,
                ),
                irs_role: discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                ),
                doj_role: discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                ),
            }
            if guild.me is not None:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    embed_links=True,
                    attach_files=True,
                )
            category, category_created = await ensure_category(
                guild,
                "🔒 DOJ",
                overwrites,
            )
            created_categories += int(category_created)
            chat_channel = None
            for channel_name in ("📊 tabela", "🧾 logi", "💬 chat"):
                created_channels += int(
                    await ensure_text_channel(guild, category, channel_name)
                )
                if channel_name == "💬 chat":
                    chat_channel = next(
                        channel
                        for channel in category.text_channels
                        if channel.name.casefold() == channel_name.casefold()
                    )

            if chat_channel is None:
                return (
                    "Kanały DOJ zostały utworzone, ale nie udało się znaleźć kanału "
                    "`chat`; ponów `/szkic2`."
                )
            for role in (irs_role, doj_role):
                await chat_channel.set_permissions(
                    role,
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                )

            table_channel = next(
                channel
                for channel in category.text_channels
                if channel.name.casefold() == "📊 tabela".casefold()
            )
            logs_channel = next(
                channel
                for channel in category.text_channels
                if channel.name.casefold() == "🧾 logi".casefold()
            )
            permissions = (
                table_channel.permissions_for(guild.me)
                if guild.me is not None
                else None
            )
            if (
                permissions is None
                or not permissions.view_channel
                or not permissions.send_messages
                or not permissions.embed_links
            ):
                return (
                    "Kanały i role utworzono, ale bot nie ma dostępu do publikowania "
                    "tabeli. Sprawdź jego uprawnienia nad rolą IRS/DOJ i uruchom "
                    "`/szkic2` ponownie.\n"
                    f"Utworzono: {created_categories} kategorii, {created_channels} "
                    f"kanałów i {created_roles} ról."
                )

            config = core.load_config(use_env=False)
            config["audit_channel_id"] = str(logs_channel.id)
            core.save_config(config)
            bot.audit_channel_id = logs_channel.id
            core.set_board_channel(guild.id, table_channel.id)
            board_updated = await refresh_company_board(bot, guild.id)
        except discord.HTTPException:
            log.exception("Nie udało się dokończyć tworzenia kategorii DOJ.")
            return (
                "Discord przerwał tworzenie lub konfigurację kategorii DOJ. Część "
                "zasobów mogła już powstać; uruchom `/szkic2` ponownie.\n"
                f"Utworzono: {created_categories} kategorii, {created_channels} kanałów "
                f"i {created_roles} ról."
            )
        except (OSError, sqlite3.Error):
            log.exception("Nie udało się zapisać ustawień kanałów DOJ.")
            return (
                "Kanały i role istnieją, ale zapisanie konfiguracji tabeli lub logów "
                "nie powiodło się. Sprawdź dostęp do `config.json` i bazy danych, "
                "a następnie uruchom `/szkic2` ponownie."
            )

        record_admin_action(
            interaction,
            "szkic_doj",
            f"Skonfigurowano kanały tabela ({table_channel.id}) i logi "
            f"({logs_channel.id}) w kategorii DOJ.",
        )
        result = (
            "Gotowe. Utworzono "
            f"**{created_categories}** kategorii, **{created_channels}** kanałów "
            f"i **{created_roles}** ról. Kanał `📊 tabela` jest podłączony do tablic firm "
            "i kar, a `🧾 logi` do raportów podatkowych i kar. Oba kanały są widoczne "
            "dla ról IRS i DOJ. `📊 tabela` i `🧾 logi` są tylko do odczytu, a `💬 chat` "
            "umożliwia im pisanie."
        )
        if not board_updated:
            result += (
                " Nie udało się opublikować tabeli — sprawdź uprawnienia bota "
                "do osadzania linków w `tabela`."
            )
        return result

    @bot.tree.command(
        name="szkic",
        description="Tworzy kompletny układ serwera z rolami firm, kategoriami i kanałami",
    )
    async def szkic(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return

        async def create_skeleton() -> str:
            guild = interaction.guild
            if guild is None:
                return "Ta komenda działa tylko na serwerze."
            return await build_server_skeleton(guild, interaction)

        await interaction.response.send_message(
            "Utworzę kompletne kategorie, kanały i kolorowe role firmowe zgodne z układem "
            "serwera. Role firmowe ograniczą widoczność kategorii do przypisanej roli, "
            "administratorów i bota. Istniejące elementy zostaną zsynchronizowane, a duplikaty "
            "będą pomijane. Kontynuować?",
            view=ConfirmActionView(
                interaction.user.id,
                create_skeleton,
                confirm_label="Potwierdź tworzenie",
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="szkic2",
        description="Tworzy prywatną kategorię DOJ z kanałami tabeli, logów i czatu",
    )
    async def szkic2(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return

        async def create_doj_skeleton() -> str:
            guild = interaction.guild
            if guild is None:
                return "Ta komenda działa tylko na serwerze."
            return await build_doj_skeleton(guild, interaction)

        await interaction.response.send_message(
            "Utworzę lub zaktualizuję role `🛡️ IRS` i `⚖️ DOJ`, prywatną kategorię `🔒 DOJ` "
            "oraz kanały `📊 tabela`, `🧾 logi` i `💬 chat`. Tylko role IRS/DOJ, administratorzy "
            "i bot będą widzieć tę kategorię. `📊 tabela` i `🧾 logi` zostaną podłączone do "
            "funkcji bota, a istniejące kanały zostaną zsynchronizowane z uprawnieniami. "
            "Kontynuować?",
            view=ConfirmActionView(
                interaction.user.id,
                create_doj_skeleton,
                confirm_label="Potwierdź tworzenie",
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="wyczysc",
        description="Usuwa wiadomości bota z bieżącego kanału",
    )
    async def wyczysc(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.",
                ephemeral=True,
            )
            return
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message(
                "Do czyszczenia wiadomości potrzebujesz uprawnienia "
                "**Zarządzanie wiadomościami**.",
                ephemeral=True,
            )
            return
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Użyj tej komendy na kanale tekstowym lub wątku.",
                ephemeral=True,
            )
            return
        bot_member = interaction.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member else None
        if (
            permissions is None
            or not permissions.view_channel
            or not permissions.read_message_history
            or not permissions.manage_messages
        ):
            await interaction.response.send_message(
                "Bot potrzebuje uprawnień do wyświetlania kanału, odczytu historii "
                "i zarządzania wiadomościami.",
                ephemeral=True,
            )
            return
        bot_user_id = bot.user.id if bot.user else None
        if bot_user_id is None:
            await interaction.response.send_message(
                "Nie można ustalić konta bota. Spróbuj ponownie po jego zalogowaniu.",
                ephemeral=True,
            )
            return

        async def clear_channel_bot_messages() -> str:
            try:
                deleted = await channel.purge(
                    limit=None,
                    check=lambda message: message.author.id == bot_user_id,
                    bulk=True,
                    reason=f"Wiadomości bota wyczyszczone przez {interaction.user}",
                )
            except discord.HTTPException:
                log.exception(
                    "Nie udało się wyczyścić wiadomości bota w kanale %s.",
                    channel.id,
                )
                return (
                    "Nie udało się dokończyć czyszczenia. Część wiadomości mogła "
                    "zostać usunięta; sprawdź kanał i uprawnienia bota."
                )
            try:
                if interaction.guild is not None:
                    core.record_admin_audit(
                        interaction.guild.id,
                        interaction.user.id,
                        str(interaction.user),
                        "wyczyszczenie_kanalu",
                        f"Usunięto {len(deleted)} wiadomości bota z kanału "
                        f"#{channel.name}.",
                    )
            except sqlite3.Error:
                log.exception("Nie udało się zapisać audytu czyszczenia kanału.")
                return (
                    f"Usunięto **{len(deleted)}** wiadomości bota, ale nie udało się "
                    "zapisać tej operacji w audycie."
                )
            return f"Usunięto **{len(deleted)}** wiadomości bota z tego kanału."

        await interaction.response.send_message(
            f"Czy na pewno usunąć wszystkie wiadomości bota z kanału {channel.mention}? "
            "Operacji nie można cofnąć.",
            view=ConfirmActionView(
                interaction.user.id,
                clear_channel_bot_messages,
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="wyczysc_tabele",
        description="Usuwa opublikowane tabele firm i kar bez kasowania danych",
    )
    async def wyczysc_tabele(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return

        board = core.get_board(guild.id)
        if board is None:
            await interaction.response.send_message(
                "Nie skonfigurowano kanału tabeli. Dane rejestru nie zostały zmienione.",
                ephemeral=True,
            )
            return
        channel_id, company_message_id = board
        penalty_board = core.get_penalty_board_message(guild.id)
        penalty_message_id = (
            penalty_board[1]
            if penalty_board is not None and penalty_board[0] == channel_id
            else None
        )
        targets = [
            ("firmy", company_message_id),
            ("kary", penalty_message_id),
        ]
        targets = [(label, message_id) for label, message_id in targets if message_id]
        if not targets:
            await interaction.response.send_message(
                "Nie ma opublikowanych tabel do usunięcia. Dane firm i kar pozostały bez zmian.",
                ephemeral=True,
            )
            return

        bot_member = guild.me
        channel = bot.get_channel(channel_id)
        permissions = channel.permissions_for(bot_member) if (
            isinstance(channel, discord.TextChannel) and bot_member is not None
        ) else None
        if (
            channel is not None
            and (
                not isinstance(channel, discord.TextChannel)
                or channel.guild.id != guild.id
            )
        ):
            await interaction.response.send_message(
                "Skonfigurowany kanał tabeli nie należy do tego serwera albo nie jest kanałem tekstowym.",
                ephemeral=True,
            )
            return
        if (
            bot_member is None
            or (
                channel is not None
                and (
                    permissions is None
                    or not permissions.view_channel
                    or not permissions.read_message_history
                )
            )
        ):
            await interaction.response.send_message(
                "Bot potrzebuje uprawnień do wyświetlania kanału tabeli i odczytu jego historii.",
                ephemeral=True,
            )
            return

        async def clear_published_tables() -> str:
            if not interaction.permissions.administrator:
                return "Uprawnienia administratora zmieniły się — tabele nie zostały usunięte."

            target_channel = channel
            if target_channel is None:
                try:
                    target_channel = await bot.fetch_channel(channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    log.exception(
                        "Nie można uzyskać dostępu do skonfigurowanego kanału tabeli %s.",
                        channel_id,
                    )
                    return (
                        "Nie udało się uzyskać dostępu do kanału tabeli. "
                        "Nie zmieniono danych ani zapisanych identyfikatorów."
                    )
            if (
                not isinstance(target_channel, discord.TextChannel)
                or target_channel.guild.id != guild.id
            ):
                return (
                    "Skonfigurowany kanał tabeli nie należy do tego serwera "
                    "albo nie jest kanałem tekstowym."
                )
            target_permissions = target_channel.permissions_for(bot_member)
            if not target_permissions.view_channel or not target_permissions.read_message_history:
                return (
                    "Bot nie ma dostępu do historii kanału tabeli. "
                    "Nie usunięto tabel."
                )

            deleted = 0
            failures: list[str] = []
            tracking_failures: list[str] = []
            for label, message_id in targets:
                try:
                    message = await target_channel.fetch_message(message_id)
                except discord.NotFound:
                    message = None
                except discord.Forbidden:
                    log.exception("Brak dostępu do wiadomości tabeli %s.", message_id)
                    failures.append(label)
                    continue
                except discord.HTTPException:
                    log.exception("Nie udało się pobrać wiadomości tabeli %s.", message_id)
                    failures.append(label)
                    continue

                if message is not None:
                    try:
                        await message.delete(
                            reason=f"Tabela wyczyszczona przez {interaction.user}"
                        )
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        log.exception("Brak uprawnień do usunięcia tabeli %s.", message_id)
                        failures.append(label)
                        continue
                    except discord.HTTPException:
                        log.exception("Nie udało się usunąć tabeli %s.", message_id)
                        failures.append(label)
                        continue

                deleted += 1
                try:
                    if label == "firmy":
                        core.clear_board_message(guild.id, message_id)
                    else:
                        core.clear_penalty_board_message(guild.id, message_id)
                except sqlite3.Error:
                    log.exception(
                        "Nie udało się wyczyścić zapisanego identyfikatora tabeli %s.",
                        message_id,
                    )
                    tracking_failures.append(label)

            if deleted:
                try:
                    record_admin_action(
                        interaction,
                        "wyczyszczenie_tabel",
                        f"Wyczyszczono {deleted} opublikowanych tabel; dane firm i kar zachowano.",
                    )
                except sqlite3.Error:
                    log.exception("Nie udało się zapisać audytu czyszczenia tabel.")
            result = (
                f"Wyczyszczono **{deleted}** opublikowanych tabel. "
                if deleted
                else "Nie udało się wyczyścić żadnej tabeli. "
            )
            result += (
                "Dane firm i kar oraz konfiguracja kanału pozostały bez zmian. "
                "Tabele pojawią się ponownie przy następnym odświeżeniu."
            )
            if failures:
                result += " Nie udało się usunąć tabel: " + ", ".join(failures) + "."
            if tracking_failures:
                result += (
                    " Nie udało się zaktualizować identyfikatorów tabel: "
                    + ", ".join(tracking_failures)
                    + "."
                )
            return result

        await interaction.response.send_message(
            "Usunę tylko opublikowane wiadomości tabeli firm i kar. "
            "Nie skasuję firm, kar ani konfiguracji kanału. Tabele mogą zostać "
            "opublikowane ponownie przy następnym odświeżeniu. Kontynuować?",
            view=ConfirmActionView(
                interaction.user.id,
                clear_published_tables,
                confirm_label="Wyczyść tabelę",
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="usun_wszystko",
        description="Usuwa kanały, kategorie i role serwera",
    )
    async def usun_wszystko(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.",
                ephemeral=True,
            )
            return

        async def wipe_server() -> str:
            if not interaction.permissions.administrator:
                return "Uprawnienia administratora zmieniły się — nic nie usunięto."

            deleted_channels = 0
            deleted_categories = 0
            deleted_roles = 0
            protected_role_ids = {
                guild.id,
                guild.me.id if guild.me is not None else None,
            }

            for channel in list(guild.channels):
                if isinstance(channel, discord.CategoryChannel):
                    continue
                try:
                    await channel.delete(reason=f"Pełne czyszczenie serwera przez {interaction.user}")
                    deleted_channels += 1
                except discord.HTTPException:
                    log.exception("Nie udało się usunąć kanału %s podczas resetu serwera.", channel.id)

            for category in list(guild.categories):
                try:
                    await category.delete(reason=f"Pełne czyszczenie serwera przez {interaction.user}")
                    deleted_categories += 1
                except discord.HTTPException:
                    log.exception("Nie udało się usunąć kategorii %s podczas resetu serwera.", category.id)

            for role in list(guild.roles):
                if role.is_default() or role.managed or role.id in protected_role_ids:
                    continue
                try:
                    await role.delete(reason=f"Pełne czyszczenie serwera przez {interaction.user}")
                    deleted_roles += 1
                except discord.HTTPException:
                    log.exception("Nie udało się usunąć roli %s podczas resetu serwera.", role.id)

            record_admin_action(
                interaction,
                "usun_wszystko",
                f"Wyczyszczono serwer: {deleted_channels} kanałów, {deleted_categories} kategorii i {deleted_roles} ról.",
            )
            return (
                f"Serwer został zresetowany. Usunięto **{deleted_channels}** kanałów, "
                f"**{deleted_categories}** kategorii i **{deleted_roles}** ról. "
                "Zachowano podstawowe role systemowe i ustawienia serwera."
            )

        await interaction.response.send_message(
            "Czy na pewno wykonać pełne czyszczenie serwera? To usunie kanały, kategorie i role "
            "stworzane przez bota i użytkowników, z zachowaniem podstawowych elementów systemowych. "
            "Operacji nie można cofnąć.",
            view=ConfirmActionView(
                interaction.user.id,
                wipe_server,
                confirm_label="Usuń wszystko",
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="przypomnienie",
        description="Wysyła przypomnienie o podatku dla aktywnych firm",
    )
    async def przypomnienie(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Ta komenda działa tylko na serwerze.")
            return

        companies = core.list_unpaid_active_companies(guild.id)
        sent = 0
        failed = 0
        for company in companies:
            if company.channel_id is None:
                failed += 1
                log.warning("Brak kanału przypomnienia dla firmy %s.", company.name)
                continue
            try:
                channel = bot.get_channel(company.channel_id)
                if channel is None:
                    channel = await bot.fetch_channel(company.channel_id)
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    failed += 1
                    log.error("Kanał przypomnienia firmy %s nie jest tekstowy.", company.name)
                    continue
                reminder = discord.Embed(
                    title="⏰ Przypomnienie podatkowe",
                    description=(
                        f"**Firma:** {discord.utils.escape_markdown(company.name)}\n\n"
                        "Podatek za bieżący tydzień nie został jeszcze potwierdzony. "
                        "Po opłaceniu użyj `/podatek` i dołącz screen finansów."
                    ),
                    color=discord.Color.from_rgb(219, 160, 64),
                )
                week_start, week_end = core.tax_week_range()
                reminder.add_field(
                    name="Tydzień podatkowy",
                    value=f"{week_start:%d.%m.%Y} – {week_end:%d.%m.%Y}",
                    inline=False,
                )
                reminder.set_footer(text="IRS · Automatyczna wiadomość przypominająca")
                await channel.send(
                    embed=reminder,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent += 1
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                failed += 1
                log.exception("Nie udało się wysłać przypomnienia dla firmy %s.", company.name)

        record_admin_action(
            interaction,
            "przypomnienia",
            f"Wysłano: {sent}; błędy: {failed}; firm do powiadomienia: {len(companies)}.",
        )
        await interaction.followup.send(
            f"Przypomnienia wysłano do **{sent}** firm."
            + (f" Nie udało się wysłać do **{failed}** firm." if failed else "")
            + (f" Firm do przypomnienia: **{len(companies)}**." if not companies else "")
        )

    @bot.tree.command(
        name="kara_finansowa",
        description="Nakłada karę finansową dla firmy z bieżącej kategorii",
    )
    @app_commands.describe(
        kwota="Dodatnia kwota kary, np. 2500.00",
        powod="Powód nałożenia kary",
        termin="Termin płatności w formacie RRRR-MM-DD",
    )
    async def kara_finansowa(
        interaction: discord.Interaction,
        kwota: str,
        powod: str,
        termin: str,
    ):
        if not await require_irs_permission(interaction, "penalties"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Ta komenda działa tylko na serwerze.")
            return
        category, company_name = company_category_for(interaction)
        if category is None or company_name is None:
            await interaction.followup.send(
                "Użyj tej komendy w kanale należącym do kategorii firmy."
            )
            return

        try:
            amount = Decimal(kwota.strip())
        except InvalidOperation:
            await interaction.followup.send(
                "Nieprawidłowa kwota. Wpisz dodatnią liczbę, np. `2500.00`."
            )
            return
        if not amount.is_finite() or amount <= 0:
            await interaction.followup.send(
                "Kwota kary musi być dodatnią, skończoną liczbą."
            )
            return
        due_date = termin.strip()
        try:
            parsed_due_date = date.fromisoformat(due_date)
        except ValueError:
            await interaction.followup.send(
                "Nieprawidłowy termin. Użyj formatu `RRRR-MM-DD`, np. `2026-08-01`."
            )
            return
        if parsed_due_date.isoformat() != due_date:
            await interaction.followup.send(
                "Nieprawidłowy termin. Użyj formatu `RRRR-MM-DD`, np. `2026-08-01`."
            )
            return
        reason = powod.strip()
        if not reason or len(reason) > 500:
            await interaction.followup.send(
                "Powód musi zawierać od 1 do 500 znaków."
            )
            return

        created_at = datetime.now(timezone.utc)
        penalty = core.add_financial_penalty(
            guild.id,
            category.id,
            company_name,
            format(amount, "f"),
            reason,
            due_date,
            interaction.user.id,
            str(interaction.user),
            created_at,
        )
        record_admin_action(
            interaction,
            "utworzenie_kary",
            f"Kara #{penalty.id}; kwota {core.money(amount)}; termin {due_date}; powód: {reason}",
            company_name,
        )
        board_updated = await refresh_company_board(bot, guild.id)
        report = build_penalty_embed(penalty)
        report.title = f"⚖️ Nałożono karę finansową #{penalty.id}"

        delivered = False
        if isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            try:
                await interaction.channel.send(
                    embed=report,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                delivered = True
            except discord.HTTPException:
                log.exception("Kara #%s została zapisana, ale nie udało się opublikować raportu.", penalty.id)
        else:
            log.error("Kanał wywołania kary #%s nie obsługuje wiadomości.", penalty.id)

        if bot.audit_channel_id and bot.audit_channel_id != getattr(interaction.channel, "id", None):
            try:
                audit_channel = bot.get_channel(bot.audit_channel_id)
                if audit_channel is None:
                    audit_channel = await bot.fetch_channel(bot.audit_channel_id)
                if isinstance(audit_channel, discord.TextChannel):
                    await audit_channel.send(
                        embed=report,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    delivered = True
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                log.exception("Nie udało się wysłać raportu kary #%s na kanał raportowy.", penalty.id)

        confirmation = f"Kara **#{penalty.id}** dla firmy **{company_name}** została zapisana w rejestrze."
        if not board_updated:
            confirmation += " Nie udało się odświeżyć tabeli firm lub kar."
        if not delivered:
            confirmation += " Nie udało się opublikować raportu — sprawdź uprawnienia bota."
        await interaction.followup.send(confirmation)

    @bot.tree.command(
        name="rejestr_kar",
        description="Pokazuje ostatnie wpisy w rejestrze kar",
    )
    async def rejestr_kar(interaction: discord.Interaction):
        if not await require_irs_permission(interaction, "penalties"):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.", ephemeral=True
            )
            return
        penalties = core.list_financial_penalties(guild.id, limit=20)
        if not penalties:
            await interaction.response.send_message(
                "Rejestr kar jest pusty.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📚 Rejestr kar finansowych",
            description="Najnowsze wpisy · statusy aktualizowane automatycznie:",
            color=discord.Color.from_rgb(83, 101, 242),
        )
        character_count = len(embed.title or "") + len(embed.description or "")
        displayed_count = 0
        for penalty in penalties:
            name = (
                f"#{penalty.id} · "
                f"{discord.utils.escape_markdown(penalty.company_name)[:100]} · "
                f"{core.format_stored_money(penalty.amount)}"
            )
            value = (
                f"{penalty_status_display(core.financial_penalty_status(penalty))[0]}\n"
                f"Termin: {penalty.due_date} · Nałożył(a): "
                f"{discord.utils.escape_markdown(penalty.imposed_by_name)[:100]}\n"
                f"Powód: {discord.utils.escape_markdown(penalty.reason)[:200]}"
                + (
                    f"\nOpłacił(a): "
                    f"{discord.utils.escape_markdown(penalty.paid_by_name or 'nieznana osoba')[:100]} "
                    f"· {penalty.paid_at[:10]}"
                    if penalty.paid_at
                    else ""
                )
            )
            if character_count + len(name) + len(value) > 5500:
                break
            embed.add_field(
                name=name,
                value=value,
                inline=False,
            )
            character_count += len(name) + len(value)
            displayed_count += 1
        if displayed_count < len(penalties):
            embed.add_field(
                name="Pozostałe wpisy",
                value=f"Nie pokazano jeszcze {len(penalties) - displayed_count} kar.",
                inline=False,
            )
        embed.set_footer(
            text="Do potwierdzenia zapłaty użyj /oplac_kare z numerem kary i screenem"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="eksport_kar",
        description="Eksportuje rejestr kar do pliku CSV",
    )
    async def eksport_kar(interaction: discord.Interaction):
        if not await require_irs_permission(interaction, "penalties"):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.",
                ephemeral=True,
            )
            return
        penalties = core.list_financial_penalties(guild.id, limit=None)
        if not penalties:
            await interaction.response.send_message(
                "Rejestr kar jest pusty — nie ma czego eksportować.",
                ephemeral=True,
            )
            return
        try:
            export_data = build_penalty_csv(penalties)
        except ValueError:
            log.exception("Nie udało się przygotować eksportu kar dla serwera %s.", guild.id)
            await interaction.response.send_message(
                "Nie udało się przygotować CSV. Sprawdź log bota — rejestr może zawierać "
                "nieprawidłową kwotę.",
                ephemeral=True,
            )
            return
        if len(export_data) > guild.filesize_limit:
            await interaction.response.send_message(
                "Plik przekracza limit załączników tego serwera. Ogranicz rejestr "
                "i spróbuj ponownie.",
                ephemeral=True,
            )
            return
        filename = f"rejestr-kar-{datetime.now():%Y-%m-%d}.csv"
        await interaction.response.send_message(
            content=f"Eksport zawiera **{len(penalties)}** wpisów.",
            file=discord.File(io.BytesIO(export_data), filename=filename),
            ephemeral=True,
        )

    @bot.tree.command(
        name="oplac_kare",
        description="Potwierdza opłatę kary na podstawie screena",
    )
    @app_commands.choices(
        profil_ocr=[
            app_commands.Choice(name="Automatyczny", value="auto"),
            app_commands.Choice(name="Panel finansów", value="dashboard"),
            app_commands.Choice(name="Karta transakcji", value="transaction-card"),
        ]
    )
    @app_commands.describe(
        kara_id="Numer kary widoczny w /rejestr_kar lub tabeli aktywnych kar",
        screen="Screen finansów z widoczną transakcją zapłaty kary",
        profil_ocr="Profil rozpoznawania użyty dla tego screena",
    )
    async def oplac_kare(
        interaction: discord.Interaction,
        kara_id: int,
        screen: discord.Attachment,
        profil_ocr: app_commands.Choice[str] | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.", ephemeral=True
            )
            return
        if kara_id < 1:
            await interaction.response.send_message(
                "Numer kary musi być dodatnią liczbą.", ephemeral=True
            )
            return
        penalty = core.get_financial_penalty(guild.id, kara_id)
        if penalty is None:
            await interaction.response.send_message(
                f"Nie znaleziono kary **#{kara_id}** na tym serwerze.",
                ephemeral=True,
            )
            return
        if penalty.paid_at is not None:
            await interaction.response.send_message(
                f"Kara **#{kara_id}** jest już oznaczona jako opłacona.",
                ephemeral=True,
            )
            return
        category = get_company_category(interaction.channel)
        if category is None or category.id != penalty.category_id:
            await interaction.response.send_message(
                "Użyj `/oplac_kare` w kanale kategorii firmy, której dotyczy ta kara.",
                ephemeral=True,
            )
            return
        if not (screen.content_type or "").startswith("image/"):
            await interaction.response.send_message(
                "Załącznik musi być obrazem (PNG/JPG).", ephemeral=True
            )
            return
        if screen.size > MAX_SIZE:
            await interaction.response.send_message(
                "Plik jest za duży (max 8 MB).", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            image_data = await screen.read()
            ocr_language, ocr_layout = core.get_ocr_profile(
                bot.ocr_profiles,
                profil_ocr.value if profil_ocr else "auto",
            )
            assessment = await asyncio.to_thread(
                core.analyze_account_screen,
                image_data,
                allow_zero_balance=True,
                require_balance=False,
                language=ocr_language,
                layout=ocr_layout,
            )
        except Exception:
            log.exception("Nie udało się odczytać screena przy opłacaniu kary #%s.", kara_id)
            await interaction.followup.send(
                "Nie udało się odczytać screena. Sprawdź Tesseracta lub wyślij "
                "wyraźniejszy obraz.",
                ephemeral=True,
            )
            return
        if assessment is None:
            await interaction.followup.send(
                "Nie mogę zweryfikować transakcji na screenie. Wyślij kolejny screen "
                "z widocznym stanem konta oraz listą transakcji.",
                ephemeral=True,
            )
            return

        penalty_amount = core.parse_stored_money(penalty.amount)
        if penalty_amount is None:
            log.error("Nieprawidłowa kwota zapisanej kary #%s: %r", kara_id, penalty.amount)
            await interaction.followup.send(
                "Nie można zweryfikować kwoty tej kary z powodu błędu w zapisanym wpisie. "
                "Administrator powinien sprawdzić `/rejestr_kar`.",
                ephemeral=True,
            )
            return
        payment_match = core.find_penalty_payment_candidate(
            assessment,
            penalty_amount,
        )
        if payment_match is None:
            largest_debit = max(
                (transaction for transaction in assessment.transactions if transaction.amount < 0),
                key=lambda transaction: abs(transaction.amount),
                default=None,
            )
            amount_info = (
                f"Największe widoczne obciążenie: "
                f"-{core.money(abs(largest_debit.amount))}."
                if largest_debit
                else "Nie znaleziono ujemnej transakcji."
            )
            await interaction.followup.send(
                f"Nie potwierdzono opłacenia kary **#{kara_id}**. "
                f"Wymagana kwota: **{core.format_stored_money(penalty.amount)}**. "
                f"{amount_info} Wyślij kolejny screen, jeśli właściwa transakcja "
                "nie jest widoczna.",
                ephemeral=True,
            )
            return

        transaction = payment_match.transaction
        preview = build_penalty_payment_result_embed(
            penalty,
            transaction,
            assessment.balance if assessment.balance_observed else None,
            datetime.now(timezone.utc),
        )
        preview.title = "🔎 Sprawdź odczyt przed zapisaniem wpłaty"
        preview.add_field(
            name="Opis odczytu",
            value=transaction.description or "Nie odczytano opisu",
            inline=False,
        )
        preview.add_field(
            name="Pewność OCR",
            value=f"{assessment.ocr_confidence:.0f}%",
            inline=True,
        )
        preview.add_field(
            name="Dlaczego wymaga potwierdzenia?",
            value=payment_match.reason,
            inline=False,
        )

        async def finalize_penalty_payment() -> str | discord.Embed:
            return await record_penalty_payment(
                bot,
                interaction,
                penalty,
                transaction,
                assessment,
            )

        if payment_match.requires_confirmation:
            payment_role_id, _ = core.get_guild_role_settings(guild.id)
            await interaction.followup.send(
                "Odczytałem możliwą wpłatę, ale nie zapiszę jej bez Twojego potwierdzenia.",
                embed=preview,
                view=PaymentReviewView(
                    interaction.user.id,
                    finalize_penalty_payment,
                    authorized_role_id=payment_role_id,
                ),
                ephemeral=True,
            )
            return

        result = await finalize_penalty_payment()
        if isinstance(result, discord.Embed):
            await interaction.followup.send(embed=result, ephemeral=True)
        else:
            await interaction.followup.send(result, ephemeral=True)

    @bot.tree.command(
        name="audyt",
        description="Pokazuje historię działań administratorów",
    )
    async def audyt(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.", ephemeral=True
            )
            return
        entries = core.list_admin_audit(guild.id, limit=20)
        if not entries:
            await interaction.response.send_message(
                "Dziennik audytu jest pusty.", ephemeral=True
            )
            return
        lines = []
        for entry in entries:
            company = (
                f" · **{discord.utils.escape_markdown(entry.company_name)}**"
                if entry.company_name
                else ""
            )
            timestamp = int(datetime.fromisoformat(entry.created_at).timestamp())
            line = (
                f"**#{entry.id} · {discord.utils.escape_markdown(entry.action)}**{company}\n"
                f"<t:{timestamp}:f> · {discord.utils.escape_markdown(entry.actor_name)}\n"
                f"{discord.utils.escape_markdown(entry.details)}"
            )
            if sum(map(len, lines)) + len(line) > 3800:
                lines.append("_Pozostałe wpisy pominięto z powodu limitu wiadomości._")
                break
            lines.append(line)
        embed = discord.Embed(
            title="🧾 Dziennik audytu administratorów",
            description="\n\n".join(lines),
            color=discord.Color.from_rgb(83, 101, 242),
        )
        embed.set_footer(text="Wpisy są ograniczone do tego serwera · pokazano do 20")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="ulga",
        description="Zwolni firmę z podatku do końca tygodnia",
    )
    async def ulga(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Ta komenda działa tylko na serwerze.")
            return
        category, company_name = company_category_for(interaction)
        if category is None or company_name is None:
            await interaction.followup.send(
                "Użyj tej komendy w kanale należącym do kategorii firmy."
            )
            return

        core.grant_company_tax_relief(guild.id, category.id, company_name)
        record_admin_action(
            interaction,
            "ulga_podatkowa",
            "Zwolnienie z podatku do kolejnego resetu tygodnia.",
            company_name,
        )
        board_updated = await refresh_company_board(bot, guild.id)
        message = (
            f"Firma **{company_name}** została zwolniona z podatku w bieżącym "
            "tygodniu. Ulga wygaśnie po użyciu `/muala`."
        )
        if not board_updated:
            message += " Tabela firm nie została odświeżona."
        await interaction.followup.send(message)

    @bot.tree.command(
        name="muala",
        description="Resetuje tygodniowe statusy firm i podatków",
    )
    async def muala(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Ta komenda działa tylko na serwerze.")
            return
        count = core.reset_weekly_taxes(guild.id)
        record_admin_action(
            interaction,
            "reset_podatkow",
            f"Zresetowano tygodniowe statusy podatkowe {count} firm.",
        )
        board_updated = await refresh_company_board(bot, guild.id)
        message = (
            f"Zresetowano tygodniowe statusy podatkowe **{count}** firm. "
            "Aktywność firm i rejestr kar pozostały bez zmian."
        )
        if not board_updated:
            message += " Tabela firm nie została odświeżona."
        await interaction.followup.send(message)

    @bot.tree.command(
        name="usun_firme",
        description="Usuwa firmę z tablicy po potwierdzeniu",
    )
    async def usun_firme(interaction: discord.Interaction):
        if not await require_guild_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.", ephemeral=True
            )
            return
        category, company_name = company_category_for(interaction)
        if category is None or company_name is None:
            await interaction.response.send_message(
                "Użyj tej komendy w kanale kategorii firmy.", ephemeral=True
            )
            return
        company = next(
            (
                item
                for item in core.list_companies(guild.id)
                if item.category_id == category.id
            ),
            None,
        )
        if company is None:
            await interaction.response.send_message(
                f"Firma **{company_name}** nie znajduje się na tablicy.",
                ephemeral=True,
            )
            return

        async def remove_company() -> str:
            if not core.delete_company(guild.id, category.id):
                return "Firma została już wcześniej usunięta z tablicy."
            record_admin_action(
                interaction,
                "usuniecie_firmy",
                "Usunięto firmę z tablicy; historia kar została zachowana.",
                company_name,
            )
            board_updated = await refresh_company_board(bot, guild.id)
            result = f"Firma **{company_name}** została usunięta z tablicy."
            result += " Historia kar pozostała w rejestrze."
            if not board_updated:
                result += " Nie udało się odświeżyć tablicy."
            return result

        await interaction.response.send_message(
            f"Czy na pewno usunąć firmę **{company_name}** z tablicy? "
            "Historia jej kar finansowych nie zostanie usunięta.",
            view=ConfirmActionView(interaction.user.id, remove_company),
            ephemeral=True,
        )

    @bot.tree.command(
        name="usun_kare",
        description="Usuwa karę z rejestru po potwierdzeniu",
    )
    @app_commands.describe(kara_id="Numer kary widoczny w /rejestr_kar")
    async def usun_kare(interaction: discord.Interaction, kara_id: int):
        if not await require_irs_permission(interaction, "penalties"):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.", ephemeral=True
            )
            return
        if kara_id < 1:
            await interaction.response.send_message(
                "Numer kary musi być dodatnią liczbą.", ephemeral=True
            )
            return
        penalty = core.get_financial_penalty(guild.id, kara_id)
        if penalty is None:
            await interaction.response.send_message(
                f"Nie znaleziono kary **#{kara_id}** na tym serwerze.",
                ephemeral=True,
            )
            return

        async def remove_penalty() -> str:
            if not core.delete_financial_penalty(guild.id, kara_id):
                return f"Kara **#{kara_id}** została już wcześniej usunięta."
            record_admin_action(
                interaction,
                "usuniecie_kary",
                f"Usunięto karę #{kara_id}; kwota "
                f"{core.format_stored_money(penalty.amount)}; powód: {penalty.reason}",
                penalty.company_name,
            )
            board_updated = await refresh_company_board(bot, guild.id)
            result = (
                f"Kara **#{kara_id}** dla firmy **{penalty.company_name}** "
                "została usunięta z rejestru."
            )
            if not board_updated:
                result += " Nie udało się odświeżyć tabel firm i kar."
            return result

        await interaction.response.send_message(
            f"Czy na pewno usunąć karę **#{penalty.id}** dla firmy "
            f"**{penalty.company_name}** na kwotę "
            f"**{core.format_stored_money(penalty.amount)}**? Tej operacji nie można cofnąć.",
            view=ConfirmActionView(interaction.user.id, remove_penalty),
            ephemeral=True,
        )

    @bot.tree.command(name="podatek", description="Weryfikuje podatek na podstawie screena")
    @app_commands.choices(
        profil_ocr=[
            app_commands.Choice(name="Automatyczny", value="auto"),
            app_commands.Choice(name="Panel finansów", value="dashboard"),
            app_commands.Choice(name="Karta transakcji", value="transaction-card"),
        ]
    )
    @app_commands.describe(
        screen="Screen finansów z widocznym stanem konta i ostatnimi transakcjami",
        publicznie="Czy odpowiedź ma być widoczna dla wszystkich (domyślnie: tylko dla Ciebie)",
        profil_ocr="Profil rozpoznawania użyty dla tego screena",
    )
    async def podatek(
        interaction: discord.Interaction,
        screen: discord.Attachment,
        publicznie: bool = False,
        profil_ocr: app_commands.Choice[str] | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Ta komenda działa tylko na serwerze.",
                ephemeral=True,
            )
            return
        category = get_company_category(interaction.channel)
        if (
            interaction.guild is not None
            and category is not None
            and core.is_presentation_category(interaction.guild.id, category.id)
        ):
            await interaction.response.send_message(
                "Kanały prezentacyjne zawierają wyłącznie statyczne przykłady. "
                "Screeny i komendy nie zapisują tam wyników ani firm.",
                ephemeral=True,
            )
            return
        if not (screen.content_type or "").startswith("image/"):
            await interaction.response.send_message("Załącznik musi być obrazem (PNG/JPG).", ephemeral=True)
            return
        if screen.size > MAX_SIZE:
            await interaction.response.send_message("Plik jest za duży (max 8 MB).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=not publicznie, thinking=True)

        if bot.audit_channel_id is None:
            await interaction.followup.send(
                "Najpierw właściciel bota musi ustawić kanał raportowy komendą "
                "`/ustaw_kanal_podatkow`."
            )
            return

        try:
            audit_channel = bot.get_channel(bot.audit_channel_id)
            if audit_channel is None:
                audit_channel = await bot.fetch_channel(bot.audit_channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.exception("Nie udało się znaleźć skonfigurowanego kanału raportowego.")
            await interaction.followup.send(
                "Nie mogę uzyskać dostępu do kanału raportowego. Właściciel bota musi "
                "ponownie ustawić go komendą `/ustaw_kanal_podatkow`."
            )
            return

        if not isinstance(audit_channel, discord.TextChannel):
            await interaction.followup.send(
                "Skonfigurowany kanał raportowy nie jest kanałem tekstowym. "
                "Właściciel bota musi ustawić go ponownie."
            )
            return
        audit_bot_member = audit_channel.guild.me
        audit_permissions = (
            audit_channel.permissions_for(audit_bot_member)
            if audit_bot_member
            else None
        )
        if (
            audit_permissions is None
            or not audit_permissions.view_channel
            or not audit_permissions.send_messages
        ):
            await interaction.followup.send(
                "Bot nie może wysyłać wiadomości na kanale raportowym. "
                "Właściciel bota musi sprawdzić jego uprawnienia."
            )
            return

        log.info("/podatek od %s", interaction.user)

        try:
            data = await screen.read()
            ocr_language, ocr_layout = core.get_ocr_profile(
                bot.ocr_profiles,
                profil_ocr.value if profil_ocr else "auto",
            )
            assessment = await asyncio.to_thread(
                core.analyze_account_screen,
                data,
                allow_zero_balance=True,
                require_balance=False,
                language=ocr_language,
                layout=ocr_layout,
            )
            text = (
                None
                if assessment
                else await asyncio.to_thread(
                    core.run_ocr,
                    data,
                    language=ocr_language,
                )
            )
        except Exception as exc:  # brak Tesseracta, uszkodzony obraz itp.
            log.error("Błąd OCR: %s", exc)
            report_time = int(interaction.created_at.timestamp())
            try:
                await audit_channel.send(
                    f"**Błąd odczytu screena**\n"
                    f"Przez: {interaction.user.mention} ({interaction.user})\n"
                    f"Kiedy: <t:{report_time}:F>\n"
                    "Nie udało się odczytać obrazu. Szczegóły są w logu aplikacji."
                )
            except discord.HTTPException:
                log.exception("Nie udało się wysłać raportu o błędzie OCR.")
            await interaction.followup.send(
                "Nie udało się przetworzyć obrazu. Sprawdź ustawienie Tesseracta "
                "lub wyślij wyraźniejszy screen."
            )
            return

        payment_match: core.PaymentMatch | None = None
        pending_confirmation = False
        transaction: core.AccountTransaction | None = None
        if assessment:
            payment_match = core.find_tax_payment_candidate(assessment)
            transaction = payment_match.transaction if payment_match else None
            pending_confirmation = bool(
                payment_match and payment_match.requires_confirmation
            )
            largest_debit = max(
                (item for item in assessment.transactions if item.amount < 0),
                key=lambda item: abs(item.amount),
                default=None,
            )
            category = get_company_category(interaction.channel)
            company_name = (
                core.normalize_company_name(category.name)
                if category
                else "Brak kategorii firmy"
            )

            if (
                category
                and interaction.guild
                and assessment.balance_observed
                and not pending_confirmation
            ):
                core.record_tax_result(
                    interaction.guild.id,
                    category.id,
                    company_name,
                    transaction is not None,
                    transaction.date if transaction and transaction.date else None,
                    core.money(assessment.balance),
                )
                if core.get_board(interaction.guild.id):
                    await refresh_company_board(bot, interaction.guild.id)

            if not assessment.balance_observed:
                response_embed = build_unverified_tax_embed(company_name)
                audit_embed = build_tax_audit_embed(
                    None,
                    company_name,
                    str(interaction.user),
                    interaction.created_at,
                    unverified_balance="Nie odczytano",
                )
            else:
                response_embed = build_tax_result_embed(
                    assessment,
                    company_name,
                    transaction=transaction,
                    pending_confirmation=pending_confirmation,
                    match_evaluated=True,
                )
                audit_embed = build_tax_audit_embed(
                    assessment,
                    company_name,
                    str(interaction.user),
                    interaction.created_at,
                    transaction=transaction,
                    largest_debit=largest_debit,
                    pending_confirmation=pending_confirmation,
                )
        else:
            _, balance = core.analyze(text or "")
            category = get_company_category(interaction.channel)
            company_name = (
                core.normalize_company_name(category.name)
                if category
                else "Brak kategorii firmy"
            )
            displayed_balance = core.money(balance[0][2]) if balance else "Nie odczytano"
            response_embed = build_unverified_tax_embed(company_name, displayed_balance)
            audit_embed = build_tax_audit_embed(
                None,
                company_name,
                str(interaction.user),
                interaction.created_at,
                unverified_balance=displayed_balance,
            )

        try:
            await audit_channel.send(
                embed=audit_embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Nie udało się wysłać raportu na kanał podatkowy.")

        if (
            assessment
            and assessment.balance_observed
            and payment_match is not None
            and pending_confirmation
            and transaction is not None
            and category is not None
            and interaction.guild is not None
        ):
            guild = interaction.guild
            company_category = category
            confirmed_transaction = transaction
            payment_role_id, _ = core.get_guild_role_settings(guild.id)
            response_embed.add_field(
                name="Opis transakcji",
                value=confirmed_transaction.description or "Nie odczytano ze screena",
                inline=False,
            )

            async def confirm_tax_payment() -> str:
                core.record_tax_result(
                    guild.id,
                    company_category.id,
                    company_name,
                    True,
                    confirmed_transaction.date
                    or interaction.created_at.date().isoformat(),
                    core.money(assessment.balance),
                )
                board_updated = True
                if core.get_board(guild.id):
                    board_updated = await refresh_company_board(
                        bot,
                        guild.id,
                    )
                confirmed_audit = build_tax_audit_embed(
                    assessment,
                    company_name,
                    str(interaction.user),
                    datetime.now(timezone.utc),
                    transaction=confirmed_transaction,
                )
                report_delivered = True
                try:
                    await audit_channel.send(
                        embed=confirmed_audit,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    report_delivered = False
                    log.exception(
                        "Nie udało się opublikować potwierdzonej wpłaty podatku."
                    )
                result = "Wpłata podatku została potwierdzona i zapisana."
                if not board_updated:
                    result += " Nie udało się odświeżyć tabeli firm."
                if not report_delivered:
                    result += " Nie udało się opublikować raportu na kanale podatkowym."
                return result

            await interaction.followup.send(
                content=(
                    f"Znalazłem możliwą wpłatę dla **{company_name}**, ale opis "
                    "nie potwierdza jednoznacznie, że to podatek. Sprawdź odczyt. "
                    "Rejestr zmieni się dopiero po kliknięciu potwierdzenia."
                ),
                embed=response_embed,
                view=PaymentReviewView(
                    interaction.user.id,
                    confirm_tax_payment,
                    authorized_role_id=payment_role_id,
                ),
            )
            return

        await interaction.followup.send(embed=response_embed)

    return bot


class BotRunner:
    """Uruchamia bota w osobnym wątku (z własną pętlą asyncio), żeby GUI się nie zawieszało."""

    def __init__(self, token, guild_id=None, on_ready=None, on_stopped=None):
        self.token = token
        self.guild_id = guild_id
        self.on_ready = on_ready
        self.on_stopped = on_stopped
        self.bot = None
        self.loop = None
        self.thread = None
        self._stop_requested = threading.Event()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            raise RuntimeError("Bot już działa.")
        self._stop_requested.clear()
        self.thread = threading.Thread(
            target=self._run,
            daemon=False,
            name="discord-bot",
        )
        self.thread.start()

    async def _start_bot(self):
        if self.bot is not None and not self._stop_requested.is_set():
            await self.bot.start(self.token)

    async def _shutdown_loop(self, loop: asyncio.AbstractEventLoop):
        try:
            if self.bot is not None and not self.bot.is_closed():
                await self.bot.close()
        except Exception:
            log.exception("Błąd podczas zamykania klienta Discord.")
        finally:
            current_task = asyncio.current_task()
            pending_tasks = [
                task
                for task in asyncio.all_tasks(loop)
                if task is not current_task and not task.done()
            ]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                results = await asyncio.gather(
                    *pending_tasks,
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        log.error(
                            "Błąd w zadaniu asyncio podczas zamykania.",
                            exc_info=(
                                type(result),
                                result,
                                result.__traceback__,
                            ),
                        )

    def _run(self):
        loop = asyncio.new_event_loop()
        self.loop = loop
        try:
            asyncio.set_event_loop(loop)
            self.bot = build_bot(self.guild_id, on_ready=self.on_ready)
            loop.run_until_complete(self._start_bot())
        except discord.LoginFailure:
            log.error("Nieprawidłowy token bota. Skopiuj go ponownie z Discord Developer Portal.")
        except GuildCommandSyncError as exc:
            log.error("%s", exc)
        except Exception:
            log.exception("Bot zatrzymał się z błędem")
        finally:
            try:
                if not loop.is_closed():
                    loop.run_until_complete(self._shutdown_loop(loop))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(loop.shutdown_default_executor())
            except Exception:
                log.exception("Błąd podczas pełnego zamykania pętli asyncio.")
            finally:
                if not loop.is_closed():
                    loop.close()
                asyncio.set_event_loop(None)
            log.info("Bot zatrzymany.")
            if self.on_stopped:
                try:
                    self.on_stopped()
                except Exception:
                    log.exception("Nie udało się powiadomić GUI o zatrzymaniu bota.")

    def stop(self):
        self._stop_requested.set()
        if (
            self.bot is None
            or self.bot.is_closed()
            or self.loop is None
            or not self.loop.is_running()
        ):
            return

        def schedule_close():
            if self.bot is not None and not self.bot.is_closed():
                asyncio.create_task(self.bot.close())

        try:
            self.loop.call_soon_threadsafe(schedule_close)
        except RuntimeError:
            log.info("Pętla bota jest już w trakcie zamykania.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    cfg = core.load_config()
    if not cfg["token"]:
        raise SystemExit("Brak tokena. Ustaw DISCORD_TOKEN albo wpisz token w gui.py i zapisz ustawienia.")
    core.configure_tesseract(cfg["tesseract_cmd"])
    try:
        build_bot(cfg["guild_id"] or None).run(cfg["token"], log_handler=None)
    except GuildCommandSyncError as exc:
        log.error("%s", exc)
