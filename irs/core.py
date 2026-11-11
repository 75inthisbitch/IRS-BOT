"""Wspólna logika OCR, analizy screenów finansów i konfiguracji."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Generator

CONFIG_PATH = Path(__file__).with_name("config.json")
COMPANY_DATABASE_PATH = Path(__file__).with_name("companies.sqlite3")
COMPANY_SUFFIX_RE = re.compile(
    r"(?:\s*[-_.|]\s*|\s+)"
    r"(?:firma|firmy|company|companies|business|biznes|sp[oó]łka|spolka|"
    r"llc|ltd|limited|inc|incorporated|corp|corporation|gmbh|"
    r"sp\s*\.?\s*z\s*\.?\s*o\s*\.?\s*o\s*\.?|s\.?\s*a\.?)\s*$",
    re.IGNORECASE,
)
SCREEN_TAX_RE = re.compile(
    r"\b(?:tax(?:es)?|withh?eld|podat(?:ek|ku|kowi)|danin[ay])\b",
    re.IGNORECASE,
)
SCREEN_PENALTY_RE = re.compile(
    r"\b(?:penalt(?:y|ies)|fine|fines|kara|kary|mandat(?:y|u)?)\b",
    re.IGNORECASE,
)
SCREEN_TRANSFER_RE = re.compile(
    r"\b(?:transfer|przelew|payment|paid|p[lł]atno[sś][cć])\b",
    re.IGNORECASE,
)
DEFAULT_CONFIG = {
    "token": "",
    "application_id": "",
    "guild_id": "",
    "tesseract_cmd": "",
    "audit_channel_id": "",
    "language": "pl",
    "ocr_language": "eng",
    "ocr_layout": "auto",
    "ocr_profiles": {
        "auto": {"language": "eng", "layout": "auto"},
        "dashboard": {"language": "eng", "layout": "dashboard"},
        "transaction-card": {"language": "eng", "layout": "transaction-card"},
    },
}
OCR_PROFILE_NAMES = ("auto", "dashboard", "transaction-card")
OCR_LANGUAGES = {"eng", "pol", "eng+pol"}
OCR_LAYOUTS = {"auto", "dashboard", "transaction-card"}
ENV_MAP = {
    "token": "DISCORD_TOKEN",
    "guild_id": "GUILD_ID",
    "tesseract_cmd": "TESSERACT_CMD",
    "audit_channel_id": "AUDIT_CHANNEL_ID",
}

# Kwota: "$1,234.56", "1,234.56", "$500", "-$20.00", "(20.00)"
AMOUNT_RE = re.compile(
    r"\(?-?\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\)?|\(?-?\d{1,3}(?:,\d{3})*\.\d{2}\)?"
)

# (etykieta, regex) - kolejność = priorytet przy wyborze "zapłaconego podatku"
PAID_KEYWORDS = [
    ("Total payments", re.compile(r"total\s+payments?", re.I)),
    ("Payments", re.compile(r"\bpayments?\b", re.I)),
    ("Amount paid", re.compile(r"\bpaid\b", re.I)),
    ("Tax withheld", re.compile(r"withh?eld", re.I)),
]
BALANCE_KEYWORDS = [
    ("Account balance", re.compile(r"account\s+balance|amount\s+you\s+owe|balance", re.I)),
]
SCREEN_AMOUNT_RE = re.compile(
    r"(?<![\w/])(?P<sign>[+-])?\s*(?P<open>\()?\s*\$?\s*"
    r"(?P<amount>\d[\d\s.,]*\d|\d)\s*\$?\s*(?P<close>\))?(?![\w/])"
)
SCREEN_DATE_RE = re.compile(
    r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2})\b"
)
SCREEN_BALANCE_RE = re.compile(
    r"\b(?:account\s+balance|current\s+balance|available\s+balance|"
    r"total\s+balance|balance|cash|funds|your\s+money|"
    r"stan\s+konta|stan\s+rachunku|saldo|balans|got[oó]wka)\b",
    re.IGNORECASE,
)
SCREEN_TABLE_HEADER_RE = re.compile(
    r"^\s*(?:data|date)\s+(?:kwota|amount)\b",
    re.IGNORECASE,
)
SCREEN_TRANSACTION_RE = re.compile(
    r"\b(?:payment|paid|transfer|tax|withh?eld|withdraw(?:al)?|debit|charge|fee|"
    r"transaction|op[lł]at[ayę]|p[lł]atno[sś][cć]|podat(?:ek|ku)|"
    r"przelew|transakcj[aeęi]|obci[aą][zż]eni[ae])\b",
    re.IGNORECASE,
)
SCREEN_SUMMARY_RE = re.compile(
    r"\b(?:total\s+payments?|payment\s+summary|transactions?\s+total|"
    r"tax\s+rate|tax\s+percentage)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PaymentMatch:
    transaction: AccountTransaction
    requires_confirmation: bool
    reason: str


@dataclass(frozen=True)
class AccountTransaction:
    amount: Decimal
    date: str | None = None
    time: str | None = None
    description: str = ""
    ocr_confidence: float = 100.0


@dataclass(frozen=True)
class AccountTaxAssessment:
    balance: Decimal
    required_tax: Decimal
    transactions: tuple[AccountTransaction, ...]
    balance_observed: bool = True
    ocr_confidence: float = 100.0

    @property
    def paid_transaction(self) -> AccountTransaction | None:
        return next(
            (
                transaction
                for transaction in self.transactions
                if transaction.amount < 0 and abs(transaction.amount) >= self.required_tax
            ),
            None,
        )


@dataclass(frozen=True)
class Company:
    guild_id: int
    category_id: int
    name: str
    active: bool
    tax_paid: bool | None
    tax_date: str | None
    balance: str | None
    channel_id: int | None
    tax_exempt: bool


@dataclass(frozen=True)
class FinancialPenalty:
    id: int
    guild_id: int
    category_id: int
    company_name: str
    amount: str
    reason: str
    due_date: str
    imposed_by_id: int
    imposed_by_name: str
    created_at: str
    paid_at: str | None
    paid_by_id: int | None
    paid_by_name: str | None
    company_overdue_notified_at: str | None
    audit_overdue_notified_at: str | None
    overdue_logged_at: str | None
    due_reminder_company_notified_at: str | None
    due_reminder_audit_notified_at: str | None


@dataclass(frozen=True)
class WeeklyPenaltySummary:
    imposed_count: int
    imposed_total: Decimal
    paid_count: int
    paid_total: Decimal
    pending_count: int
    pending_total: Decimal
    overdue_count: int
    overdue_total: Decimal


@dataclass(frozen=True)
class PresentationSession:
    guild_id: int
    category_id: int
    requester_id: int
    stage: int
    current_channel_id: int
    current_message_id: int | None


@dataclass(frozen=True)
class AdminAuditEntry:
    id: int
    guild_id: int
    actor_id: int
    actor_name: str
    action: str
    company_name: str | None
    details: str
    created_at: str


def normalize_company_name(name: str) -> str:
    """Removes common company labels/legal suffixes from a Discord category name."""
    normalized = re.sub(r"[_|]+", " ", name).strip()
    while True:
        shortened = COMPANY_SUFFIX_RE.sub("", normalized).strip(" \t-_.|")
        if not shortened or shortened == normalized:
            break
        normalized = shortened
    return normalized or name.strip()


# ---------- konfiguracja ----------

def load_config(use_env: bool = True) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["ocr_profiles"] = {
        name: dict(profile)
        for name, profile in DEFAULT_CONFIG["ocr_profiles"].items()
    }
    stored_config = {}
    try:
        stored_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(stored_config, dict):
            cfg.update(stored_config)
        else:
            stored_config = {}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    cfg["ocr_profiles"] = normalize_ocr_profiles(
        stored_config.get("ocr_profiles"),
        cfg.get("ocr_language", "eng"),
        cfg.get("ocr_layout", "auto"),
    )
    if use_env:
        for key, env in ENV_MAP.items():
            if os.environ.get(env):
                cfg[key] = os.environ[env]
    return cfg


def save_config(cfg: dict) -> None:
    existing = load_config(use_env=False)
    data = {}
    for key in DEFAULT_CONFIG:
        value = cfg[key] if key in cfg else existing.get(key, "")
        if key == "ocr_profiles":
            data[key] = normalize_ocr_profiles(
                value,
                str(cfg.get("ocr_language", existing.get("ocr_language", "eng"))),
                str(cfg.get("ocr_layout", existing.get("ocr_layout", "auto"))),
            )
        else:
            data[key] = str(value)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_ocr_profiles(
    profiles: object,
    fallback_language: str = "eng",
    fallback_layout: str = "auto",
) -> dict[str, dict[str, str]]:
    source = profiles if isinstance(profiles, dict) else {}
    language = fallback_language if fallback_language in OCR_LANGUAGES else "eng"
    layout = fallback_layout if fallback_layout in OCR_LAYOUTS else "auto"
    normalized = {
        "auto": {"language": language, "layout": layout},
        "dashboard": {"language": language, "layout": "dashboard"},
        "transaction-card": {
            "language": language,
            "layout": "transaction-card",
        },
    }
    if "auto" not in source:
        normalized["dashboard"]["layout"] = layout
    for name in OCR_PROFILE_NAMES:
        profile = source.get(name)
        if not isinstance(profile, dict):
            continue
        profile_language = profile.get("language")
        profile_layout = profile.get("layout")
        if profile_language in OCR_LANGUAGES:
            normalized[name]["language"] = profile_language
        if profile_layout in OCR_LAYOUTS:
            normalized[name]["layout"] = profile_layout
    return normalized


def get_ocr_profile(
    profiles: object,
    profile_name: str,
    fallback_language: str = "eng",
    fallback_layout: str = "auto",
) -> tuple[str, str]:
    normalized = normalize_ocr_profiles(
        profiles,
        fallback_language,
        fallback_layout,
    )
    selected = normalized.get(profile_name, normalized["auto"])
    return selected["language"], selected["layout"]


@contextmanager
def _company_database() -> Generator[sqlite3.Connection, None, None]:
    connection = sqlite3.connect(COMPANY_DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    guild_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    tax_paid INTEGER,
                    tax_date TEXT,
                    balance TEXT,
                    channel_id INTEGER,
                    tax_exempt INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, category_id)
                )
                """
            )
            company_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(companies)")
            }
            if "channel_id" not in company_columns:
                connection.execute("ALTER TABLE companies ADD COLUMN channel_id INTEGER")
            if "tax_exempt" not in company_columns:
                connection.execute(
                    "ALTER TABLE companies ADD COLUMN tax_exempt INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS company_boards (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    penalties_message_id INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_role_settings (
                    guild_id INTEGER PRIMARY KEY,
                    payment_verification_role_id INTEGER,
                    penalty_management_role_id INTEGER
                )
                """
            )
            board_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(company_boards)")
            }
            if "penalties_message_id" not in board_columns:
                connection.execute(
                    "ALTER TABLE company_boards ADD COLUMN penalties_message_id INTEGER"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS financial_penalties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    company_name TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    imposed_by_id INTEGER NOT NULL,
                    imposed_by_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    paid_at TEXT,
                    paid_by_id INTEGER,
                    paid_by_name TEXT,
                    company_overdue_notified_at TEXT,
                    audit_overdue_notified_at TEXT,
                    overdue_logged_at TEXT,
                    due_reminder_company_notified_at TEXT,
                    due_reminder_audit_notified_at TEXT
                )
                """
            )
            penalty_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(financial_penalties)")
            }
            for column, definition in (
                ("paid_at", "TEXT"),
                ("paid_by_id", "INTEGER"),
                ("paid_by_name", "TEXT"),
                ("company_overdue_notified_at", "TEXT"),
                ("audit_overdue_notified_at", "TEXT"),
                ("overdue_logged_at", "TEXT"),
                ("due_reminder_company_notified_at", "TEXT"),
                ("due_reminder_audit_notified_at", "TEXT"),
            ):
                if column not in penalty_columns:
                    connection.execute(
                        f"ALTER TABLE financial_penalties ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS weekly_penalty_reports (
                    guild_id INTEGER NOT NULL,
                    week_start TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, week_start)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS presentation_sessions (
                    guild_id INTEGER PRIMARY KEY,
                    category_id INTEGER NOT NULL,
                    requester_id INTEGER NOT NULL,
                    stage INTEGER NOT NULL DEFAULT 0,
                    current_channel_id INTEGER NOT NULL,
                    current_message_id INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS presentation_resources (
                    guild_id INTEGER NOT NULL,
                    resource_id INTEGER NOT NULL,
                    resource_type TEXT NOT NULL CHECK (resource_type IN ('category', 'channel')),
                    PRIMARY KEY (guild_id, resource_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    actor_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    company_name TEXT,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            yield connection
    finally:
        connection.close()


def set_company_active(
    guild_id: int,
    category_id: int,
    name: str,
    active: bool,
    channel_id: int | None = None,
) -> None:
    with _company_database() as connection:
        connection.execute(
            """
            INSERT INTO companies (guild_id, category_id, name, active, channel_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, category_id) DO UPDATE SET
                name = excluded.name,
                active = excluded.active,
                channel_id = COALESCE(excluded.channel_id, companies.channel_id)
            """,
            (guild_id, category_id, name, int(active), channel_id),
        )


def get_guild_role_settings(guild_id: int) -> tuple[int | None, int | None]:
    with _company_database() as connection:
        row = connection.execute(
            """
            SELECT payment_verification_role_id, penalty_management_role_id
            FROM guild_role_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
    if row is None:
        return None, None
    return row["payment_verification_role_id"], row["penalty_management_role_id"]


def set_guild_role_settings(
    guild_id: int,
    payment_verification_role_id: int | None,
    penalty_management_role_id: int | None,
) -> None:
    if payment_verification_role_id is None and penalty_management_role_id is None:
        with _company_database() as connection:
            connection.execute(
                "DELETE FROM guild_role_settings WHERE guild_id = ?",
                (guild_id,),
            )
        return
    with _company_database() as connection:
        connection.execute(
            """
            INSERT INTO guild_role_settings (
                guild_id, payment_verification_role_id, penalty_management_role_id
            )
            VALUES (?, ?, ?)
            ON CONFLICT (guild_id) DO UPDATE SET
                payment_verification_role_id = excluded.payment_verification_role_id,
                penalty_management_role_id = excluded.penalty_management_role_id
            """,
            (guild_id, payment_verification_role_id, penalty_management_role_id),
        )


def record_tax_result(
    guild_id: int,
    category_id: int,
    name: str,
    paid: bool,
    tax_date: str | None,
    balance: str,
    channel_id: int | None = None,
) -> None:
    with _company_database() as connection:
        connection.execute(
            """
            INSERT INTO companies (
                guild_id, category_id, name, active, tax_paid, tax_date, balance,
                channel_id
            )
            VALUES (?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT (guild_id, category_id) DO UPDATE SET
                name = excluded.name,
                tax_paid = excluded.tax_paid,
                tax_date = excluded.tax_date,
                balance = excluded.balance,
                channel_id = COALESCE(excluded.channel_id, companies.channel_id)
            """,
            (guild_id, category_id, name, int(paid), tax_date, balance, channel_id),
        )


def list_companies(guild_id: int) -> list[Company]:
    with _company_database() as connection:
        rows = connection.execute(
            """
            SELECT guild_id, category_id, name, active, tax_paid, tax_date, balance,
                   channel_id, tax_exempt
            FROM companies
            WHERE guild_id = ?
            ORDER BY name COLLATE NOCASE, category_id
            """,
            (guild_id,),
        ).fetchall()
    return [
        Company(
            guild_id=row["guild_id"],
            category_id=row["category_id"],
            name=row["name"],
            active=bool(row["active"]),
            tax_paid=None if row["tax_paid"] is None else bool(row["tax_paid"]),
            tax_date=row["tax_date"],
            balance=row["balance"],
            channel_id=row["channel_id"],
            tax_exempt=bool(row["tax_exempt"]),
        )
        for row in rows
    ]


def delete_company(guild_id: int, category_id: int) -> bool:
    """Removes a company from the board without deleting its penalty history."""
    with _company_database() as connection:
        cursor = connection.execute(
            "DELETE FROM companies WHERE guild_id = ? AND category_id = ?",
            (guild_id, category_id),
        )
        return cursor.rowcount > 0


def grant_company_tax_relief(guild_id: int, category_id: int, name: str) -> None:
    with _company_database() as connection:
        connection.execute(
            """
            INSERT INTO companies (guild_id, category_id, name, tax_exempt)
            VALUES (?, ?, ?, 1)
            ON CONFLICT (guild_id, category_id) DO UPDATE SET
                name = excluded.name,
                tax_exempt = 1
            """,
            (guild_id, category_id, name),
        )


def reset_weekly_taxes(guild_id: int) -> int:
    with _company_database() as connection:
        cursor = connection.execute(
            """
            UPDATE companies
            SET tax_paid = NULL, tax_date = NULL, balance = NULL, tax_exempt = 0
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        return cursor.rowcount


def register_presentation_session(
    guild_id: int,
    category_id: int,
    requester_id: int,
    current_channel_id: int,
) -> bool:
    with _company_database() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO presentation_sessions (
                guild_id, category_id, requester_id, stage, current_channel_id
            )
            VALUES (?, ?, ?, 0, ?)
            """,
            (guild_id, category_id, requester_id, current_channel_id),
        )
        if cursor.rowcount == 0:
            return False
        connection.executemany(
            """
            INSERT OR IGNORE INTO presentation_resources (
                guild_id, resource_id, resource_type
            )
            VALUES (?, ?, ?)
            """,
            (
                (guild_id, category_id, "category"),
                (guild_id, current_channel_id, "channel"),
            ),
        )
    return True


def presentation_session_exists(guild_id: int) -> bool:
    return get_presentation_session(guild_id) is not None


def get_presentation_session(guild_id: int) -> PresentationSession | None:
    with _company_database() as connection:
        row = connection.execute(
            """
            SELECT guild_id, category_id, requester_id, stage,
                   current_channel_id, current_message_id
            FROM presentation_sessions
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
    return PresentationSession(**dict(row)) if row is not None else None


def list_presentation_sessions() -> list[PresentationSession]:
    with _company_database() as connection:
        rows = connection.execute(
            """
            SELECT guild_id, category_id, requester_id, stage,
                   current_channel_id, current_message_id
            FROM presentation_sessions
            ORDER BY guild_id
            """
        ).fetchall()
    return [PresentationSession(**dict(row)) for row in rows]


def update_presentation_progress(
    guild_id: int,
    stage: int,
    current_channel_id: int,
    current_message_id: int | None,
) -> bool:
    if stage < 0:
        raise ValueError("Presentation stage cannot be negative")
    with _company_database() as connection:
        cursor = connection.execute(
            """
            UPDATE presentation_sessions
            SET stage = ?, current_channel_id = ?, current_message_id = ?
            WHERE guild_id = ?
            """,
            (stage, current_channel_id, current_message_id, guild_id),
        )
    return cursor.rowcount > 0


def track_presentation_resource(
    guild_id: int,
    resource_id: int,
    resource_type: str,
) -> None:
    if resource_type not in {"category", "channel"}:
        raise ValueError(f"Unknown presentation resource type: {resource_type}")
    with _company_database() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO presentation_resources (
                guild_id, resource_id, resource_type
            )
            VALUES (?, ?, ?)
            """,
            (guild_id, resource_id, resource_type),
        )


def list_presentation_resources(guild_id: int) -> list[tuple[int, str]]:
    with _company_database() as connection:
        rows = connection.execute(
            """
            SELECT resource_id, resource_type
            FROM presentation_resources
            WHERE guild_id = ?
            ORDER BY CASE resource_type WHEN 'channel' THEN 0 ELSE 1 END, resource_id
            """,
            (guild_id,),
        ).fetchall()
    return [(row["resource_id"], row["resource_type"]) for row in rows]


def untrack_presentation_resource(guild_id: int, resource_id: int) -> bool:
    with _company_database() as connection:
        cursor = connection.execute(
            """
            DELETE FROM presentation_resources
            WHERE guild_id = ? AND resource_id = ?
            """,
            (guild_id, resource_id),
        )
    return cursor.rowcount > 0


def delete_presentation_session(guild_id: int) -> bool:
    with _company_database() as connection:
        connection.execute(
            "DELETE FROM presentation_resources WHERE guild_id = ?",
            (guild_id,),
        )
        cursor = connection.execute(
            "DELETE FROM presentation_sessions WHERE guild_id = ?",
            (guild_id,),
        )
    return cursor.rowcount > 0


def is_presentation_category(guild_id: int, category_id: int) -> bool:
    with _company_database() as connection:
        row = connection.execute(
            """
            SELECT 1 FROM presentation_sessions
            WHERE guild_id = ? AND category_id = ?
            """,
            (guild_id, category_id),
        ).fetchone()
    return row is not None


def list_unpaid_active_companies(guild_id: int) -> list[Company]:
    return [
        company
        for company in list_companies(guild_id)
        if company.active and not company.tax_exempt and company.tax_paid is not True
    ]


def add_financial_penalty(
    guild_id: int,
    category_id: int,
    company_name: str,
    amount: str,
    reason: str,
    due_date: str,
    imposed_by_id: int,
    imposed_by_name: str,
    created_at: datetime,
) -> FinancialPenalty:
    with _company_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO financial_penalties (
                guild_id, category_id, company_name, amount, reason, due_date,
                imposed_by_id, imposed_by_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                category_id,
                company_name,
                amount,
                reason,
                due_date,
                imposed_by_id,
                imposed_by_name,
                created_at.isoformat(timespec="seconds"),
            ),
        )
        penalty_id = cursor.lastrowid
    if penalty_id is None:
        raise RuntimeError("Could not create financial penalty record")
    return FinancialPenalty(
        penalty_id,
        guild_id,
        category_id,
        company_name,
        amount,
        reason,
        due_date,
        imposed_by_id,
        imposed_by_name,
        created_at.isoformat(timespec="seconds"),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )


def financial_penalty_status(
    penalty: FinancialPenalty, today: date | None = None
) -> str:
    if penalty.paid_at is not None:
        return "paid"
    return "overdue" if penalty.due_date < (today or date.today()).isoformat() else "pending"


def list_financial_penalties(
    guild_id: int,
    limit: int | None = 20,
) -> list[FinancialPenalty]:
    limit_clause = "LIMIT ?" if limit is not None else ""
    parameters: tuple[int, ...] = (guild_id, limit) if limit is not None else (guild_id,)
    with _company_database() as connection:
        rows = connection.execute(
            f"""
            SELECT id, guild_id, category_id, company_name, amount, reason,
                   due_date, imposed_by_id, imposed_by_name, created_at,
                   paid_at, paid_by_id, paid_by_name,
                   company_overdue_notified_at, audit_overdue_notified_at,
                   overdue_logged_at, due_reminder_company_notified_at,
                   due_reminder_audit_notified_at
            FROM financial_penalties
            WHERE guild_id = ?
            ORDER BY id DESC
            {limit_clause}
            """,
            parameters,
        ).fetchall()
    return [FinancialPenalty(**dict(row)) for row in rows]


def list_active_financial_penalties(
    guild_id: int,
    now: datetime | None = None,
) -> list[FinancialPenalty]:
    current_time = now or datetime.now(timezone.utc)
    paid_cutoff = current_time - timedelta(hours=24)
    with _company_database() as connection:
        rows = connection.execute(
            """
            SELECT id, guild_id, category_id, company_name, amount, reason,
                   due_date, imposed_by_id, imposed_by_name, created_at,
                   paid_at, paid_by_id, paid_by_name,
                   company_overdue_notified_at, audit_overdue_notified_at,
                   overdue_logged_at, due_reminder_company_notified_at,
                   due_reminder_audit_notified_at
            FROM financial_penalties
            WHERE guild_id = ?
              AND (paid_at IS NULL OR julianday(paid_at) > julianday(?))
            ORDER BY id DESC
            """,
            (
                guild_id,
                paid_cutoff.isoformat(timespec="seconds"),
            ),
        ).fetchall()
    return [FinancialPenalty(**dict(row)) for row in rows]


def list_unnotified_overdue_penalties(
    guild_id: int,
    today: date | None = None,
) -> list[FinancialPenalty]:
    current_day = today or date.today()
    with _company_database() as connection:
        rows = connection.execute(
            """
            SELECT id, guild_id, category_id, company_name, amount, reason,
                   due_date, imposed_by_id, imposed_by_name, created_at,
                   paid_at, paid_by_id, paid_by_name,
                   company_overdue_notified_at, audit_overdue_notified_at,
                   overdue_logged_at, due_reminder_company_notified_at,
                   due_reminder_audit_notified_at
            FROM financial_penalties
            WHERE guild_id = ? AND paid_at IS NULL AND due_date < ?
              AND (
                  company_overdue_notified_at IS NULL
                  OR audit_overdue_notified_at IS NULL
                  OR overdue_logged_at IS NULL
              )
            ORDER BY id
            """,
            (guild_id, current_day.isoformat()),
        ).fetchall()
    return [FinancialPenalty(**dict(row)) for row in rows]


def list_unnotified_due_reminders(
    guild_id: int,
    today: date | None = None,
) -> list[FinancialPenalty]:
    reminder_date = (today or date.today()) + timedelta(days=1)
    with _company_database() as connection:
        rows = connection.execute(
            """
            SELECT id, guild_id, category_id, company_name, amount, reason,
                   due_date, imposed_by_id, imposed_by_name, created_at,
                   paid_at, paid_by_id, paid_by_name,
                   company_overdue_notified_at, audit_overdue_notified_at,
                   overdue_logged_at, due_reminder_company_notified_at,
                   due_reminder_audit_notified_at
            FROM financial_penalties
            WHERE guild_id = ? AND paid_at IS NULL AND due_date = ?
              AND (
                  due_reminder_company_notified_at IS NULL
                  OR due_reminder_audit_notified_at IS NULL
              )
            ORDER BY id
            """,
            (guild_id, reminder_date.isoformat()),
        ).fetchall()
    return [FinancialPenalty(**dict(row)) for row in rows]


def mark_penalty_due_reminder_notified(
    guild_id: int,
    penalty_id: int,
    destination: str,
    notified_at: datetime | None = None,
) -> bool:
    if destination not in {"company", "audit"}:
        raise ValueError(f"Unknown due reminder destination: {destination}")
    column = (
        "due_reminder_company_notified_at"
        if destination == "company"
        else "due_reminder_audit_notified_at"
    )
    event_time = notified_at or datetime.now(timezone.utc)
    with _company_database() as connection:
        cursor = connection.execute(
            f"""
            UPDATE financial_penalties
            SET {column} = ?
            WHERE guild_id = ? AND id = ? AND {column} IS NULL
            """,
            (
                event_time.isoformat(timespec="seconds"),
                guild_id,
                penalty_id,
            ),
        )
    return cursor.rowcount > 0


def summarize_financial_penalties(
    guild_id: int,
    period_start: date,
    period_end: date,
    as_of: date | None = None,
) -> WeeklyPenaltySummary:
    if period_end <= period_start:
        raise ValueError("The summary period must have a positive duration")
    current_day = as_of or date.today()
    penalties = list_financial_penalties(guild_id, limit=None)
    imposed = [
        penalty
        for penalty in penalties
        if period_start <= date.fromisoformat(penalty.created_at[:10]) < period_end
    ]
    paid = [
        penalty
        for penalty in penalties
        if penalty.paid_at is not None
        and period_start <= date.fromisoformat(penalty.paid_at[:10]) < period_end
    ]
    pending = [
        penalty
        for penalty in penalties
        if penalty.paid_at is None and penalty.due_date >= current_day.isoformat()
    ]
    overdue = [
        penalty
        for penalty in penalties
        if penalty.paid_at is None and penalty.due_date < current_day.isoformat()
    ]

    def total(items: list[FinancialPenalty]) -> Decimal:
        amounts = [parse_stored_money(penalty.amount) for penalty in items]
        if any(amount is None for amount in amounts):
            raise ValueError("Invalid stored penalty amount in weekly summary")
        return sum((amount for amount in amounts if amount is not None), Decimal(0))

    return WeeklyPenaltySummary(
        imposed_count=len(imposed),
        imposed_total=total(imposed),
        paid_count=len(paid),
        paid_total=total(paid),
        pending_count=len(pending),
        pending_total=total(pending),
        overdue_count=len(overdue),
        overdue_total=total(overdue),
    )


def weekly_penalty_report_sent(guild_id: int, week_start: date) -> bool:
    with _company_database() as connection:
        row = connection.execute(
            """
            SELECT 1 FROM weekly_penalty_reports
            WHERE guild_id = ? AND week_start = ?
            """,
            (guild_id, week_start.isoformat()),
        ).fetchone()
    return row is not None


def mark_weekly_penalty_report_sent(
    guild_id: int,
    week_start: date,
    sent_at: datetime | None = None,
) -> bool:
    event_time = sent_at or datetime.now(timezone.utc)
    with _company_database() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO weekly_penalty_reports (guild_id, week_start, sent_at)
            VALUES (?, ?, ?)
            """,
            (
                guild_id,
                week_start.isoformat(),
                event_time.isoformat(timespec="seconds"),
            ),
        )
    return cursor.rowcount > 0


def mark_penalty_overdue_notified(
    guild_id: int,
    penalty_id: int,
    destination: str,
    notified_at: datetime | None = None,
) -> bool:
    if destination not in {"company", "audit"}:
        raise ValueError(f"Unknown overdue notification destination: {destination}")
    column = (
        "company_overdue_notified_at"
        if destination == "company"
        else "audit_overdue_notified_at"
    )
    event_time = notified_at or datetime.now(timezone.utc)
    with _company_database() as connection:
        cursor = connection.execute(
            f"""
            UPDATE financial_penalties
            SET {column} = ?
            WHERE guild_id = ? AND id = ? AND {column} IS NULL
            """,
            (
                event_time.isoformat(timespec="seconds"),
                guild_id,
                penalty_id,
            ),
        )
    return cursor.rowcount > 0


def mark_penalty_overdue_logged(
    guild_id: int,
    penalty_id: int,
    logged_at: datetime | None = None,
) -> bool:
    event_time = logged_at or datetime.now(timezone.utc)
    with _company_database() as connection:
        cursor = connection.execute(
            """
            UPDATE financial_penalties
            SET overdue_logged_at = ?
            WHERE guild_id = ? AND id = ? AND overdue_logged_at IS NULL
            """,
            (
                event_time.isoformat(timespec="seconds"),
                guild_id,
                penalty_id,
            ),
        )
    return cursor.rowcount > 0


def get_financial_penalty(guild_id: int, penalty_id: int) -> FinancialPenalty | None:
    with _company_database() as connection:
        row = connection.execute(
            """
            SELECT id, guild_id, category_id, company_name, amount, reason,
                   due_date, imposed_by_id, imposed_by_name, created_at,
                   paid_at, paid_by_id, paid_by_name,
                   company_overdue_notified_at, audit_overdue_notified_at,
                   overdue_logged_at, due_reminder_company_notified_at,
                   due_reminder_audit_notified_at
            FROM financial_penalties
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, penalty_id),
        ).fetchone()
    return FinancialPenalty(**dict(row)) if row is not None else None


def mark_financial_penalty_paid(
    guild_id: int,
    penalty_id: int,
    paid_by_id: int,
    paid_by_name: str,
    paid_at: datetime,
) -> tuple[FinancialPenalty | None, bool]:
    with _company_database() as connection:
        cursor = connection.execute(
            """
            UPDATE financial_penalties
            SET paid_at = ?, paid_by_id = ?, paid_by_name = ?
            WHERE guild_id = ? AND id = ? AND paid_at IS NULL
            """,
            (
                paid_at.isoformat(timespec="seconds"),
                paid_by_id,
                paid_by_name,
                guild_id,
                penalty_id,
            ),
        )
        if cursor.rowcount == 0:
            row = connection.execute(
                """
                SELECT id, guild_id, category_id, company_name, amount, reason,
                       due_date, imposed_by_id, imposed_by_name, created_at,
                       paid_at, paid_by_id, paid_by_name,
                       company_overdue_notified_at, audit_overdue_notified_at,
                       overdue_logged_at, due_reminder_company_notified_at,
                       due_reminder_audit_notified_at
                FROM financial_penalties
                WHERE guild_id = ? AND id = ?
                """,
                (guild_id, penalty_id),
            ).fetchone()
            return (FinancialPenalty(**dict(row)) if row is not None else None), False
    return get_financial_penalty(guild_id, penalty_id), cursor.rowcount > 0


def delete_financial_penalty(guild_id: int, penalty_id: int) -> bool:
    with _company_database() as connection:
        cursor = connection.execute(
            "DELETE FROM financial_penalties WHERE guild_id = ? AND id = ?",
            (guild_id, penalty_id),
        )
        return cursor.rowcount > 0


def record_admin_audit(
    guild_id: int,
    actor_id: int,
    actor_name: str,
    action: str,
    details: str,
    company_name: str | None = None,
    created_at: datetime | None = None,
) -> AdminAuditEntry:
    event_time = created_at or datetime.now().astimezone()
    with _company_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO admin_audit_log (
                guild_id, actor_id, actor_name, action, company_name, details, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                actor_id,
                actor_name,
                action,
                company_name,
                details,
                event_time.isoformat(timespec="seconds"),
            ),
        )
        entry_id = cursor.lastrowid
    if entry_id is None:
        raise RuntimeError("Could not create administrator audit entry")
    return AdminAuditEntry(
        entry_id,
        guild_id,
        actor_id,
        actor_name,
        action,
        company_name,
        details,
        event_time.isoformat(timespec="seconds"),
    )


def list_admin_audit(guild_id: int, limit: int = 25) -> list[AdminAuditEntry]:
    with _company_database() as connection:
        rows = connection.execute(
            """
            SELECT id, guild_id, actor_id, actor_name, action, company_name,
                   details, created_at
            FROM admin_audit_log
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    return [AdminAuditEntry(**dict(row)) for row in rows]


def tax_week_range(today: date | None = None) -> tuple[date, date]:
    """Returns the current Sunday 00:00 through next Sunday 00:00 tax week."""
    current_day = today or date.today()
    days_since_sunday = (current_day.weekday() + 1) % 7
    week_start = current_day - timedelta(days=days_since_sunday)
    return week_start, week_start + timedelta(days=7)


def set_board_channel(guild_id: int, channel_id: int) -> None:
    with _company_database() as connection:
        connection.execute(
            """
            INSERT INTO company_boards (guild_id, channel_id, message_id)
            VALUES (?, ?, NULL)
            ON CONFLICT (guild_id) DO UPDATE SET
                message_id = CASE
                    WHEN company_boards.channel_id = excluded.channel_id
                    THEN company_boards.message_id
                    ELSE NULL
                END,
                penalties_message_id = CASE
                    WHEN company_boards.channel_id = excluded.channel_id
                    THEN company_boards.penalties_message_id
                    ELSE NULL
                END,
                channel_id = excluded.channel_id
            """,
            (guild_id, channel_id),
        )


def get_board(guild_id: int) -> tuple[int, int | None] | None:
    with _company_database() as connection:
        row = connection.execute(
            "SELECT channel_id, message_id FROM company_boards WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row is None:
        return None
    return row["channel_id"], row["message_id"]


def get_penalty_board_message(guild_id: int) -> tuple[int, int | None] | None:
    with _company_database() as connection:
        row = connection.execute(
            """
            SELECT channel_id, penalties_message_id
            FROM company_boards
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
    if row is None:
        return None
    return row["channel_id"], row["penalties_message_id"]


def set_board_message(guild_id: int, message_id: int) -> None:
    with _company_database() as connection:
        cursor = connection.execute(
            "UPDATE company_boards SET message_id = ? WHERE guild_id = ?",
            (message_id, guild_id),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"No company board configured for guild {guild_id}")


def clear_board_message(guild_id: int, message_id: int) -> bool:
    with _company_database() as connection:
        cursor = connection.execute(
            """
            UPDATE company_boards
            SET message_id = NULL
            WHERE guild_id = ? AND message_id = ?
            """,
            (guild_id, message_id),
        )
    return cursor.rowcount > 0


def set_penalty_board_message(guild_id: int, message_id: int) -> None:
    with _company_database() as connection:
        cursor = connection.execute(
            "UPDATE company_boards SET penalties_message_id = ? WHERE guild_id = ?",
            (message_id, guild_id),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"No company board configured for guild {guild_id}")


def clear_penalty_board_message(guild_id: int, message_id: int) -> bool:
    with _company_database() as connection:
        cursor = connection.execute(
            """
            UPDATE company_boards
            SET penalties_message_id = NULL
            WHERE guild_id = ? AND penalties_message_id = ?
            """,
            (guild_id, message_id),
        )
    return cursor.rowcount > 0


def configure_tesseract(path: str | None) -> None:
    import pytesseract

    configured_path = path.strip() if path else ""
    pytesseract.pytesseract.tesseract_cmd = (
        configured_path or _find_tesseract_executable()
    )


def _find_tesseract_executable() -> str:
    executable = shutil.which("tesseract")
    if executable:
        return executable

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get(
        "ProgramFiles(x86)",
        r"C:\Program Files (x86)",
    )
    candidates = (
        Path(program_files) / "Tesseract-OCR" / "tesseract.exe",
        Path(program_files_x86) / "Tesseract-OCR" / "tesseract.exe",
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "tesseract"


OCR_LANGUAGE = "eng"
OCR_LAYOUT = "auto"


def configure_ocr(language: str | None = None, layout: str | None = None) -> None:
    global OCR_LANGUAGE, OCR_LAYOUT
    if language in {"eng", "pol", "eng+pol"}:
        OCR_LANGUAGE = language
    if layout in {"auto", "dashboard", "transaction-card"}:
        OCR_LAYOUT = layout


def _available_ocr_language(pytesseract, language: str | None = None) -> str:
    requested = (language or OCR_LANGUAGE).split("+")
    available = set(pytesseract.get_languages(config=""))
    selected = [language for language in requested if language in available]
    if not selected:
        raise RuntimeError(
            f"Brak plików językowych Tesseract dla ustawienia "
            f"{language or OCR_LANGUAGE!r}."
        )
    return "+".join(selected)


# ---------- OCR ----------

def preprocess(img):
    """Skala szarości, powiększenie i kontrast - poprawia skuteczność OCR."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img).convert("L")
    img = img.resize(
        (img.width * 2, img.height * 2),
        Image.Resampling.LANCZOS,
    )
    return ImageOps.autocontrast(img)


def run_ocr(data: bytes, language: str | None = None) -> str:
    import pytesseract
    from PIL import Image

    img = preprocess(Image.open(io.BytesIO(data)))
    selected_language = _available_ocr_language(pytesseract, language)
    return pytesseract.image_to_string(
        img,
        lang=selected_language,
        config="--psm 6",
    )


def _read_crop(
    img,
    box: tuple[float, float, float, float],
    config: str,
    language: str | None = None,
) -> str:
    import pytesseract
    from PIL import Image, ImageOps

    left, top, right, bottom = box
    crop = img.crop(
        (int(img.width * left), int(img.height * top),
         int(img.width * right), int(img.height * bottom))
    )
    crop = ImageOps.autocontrast(crop.convert("L"))
    crop = crop.resize((crop.width * 5, crop.height * 5), Image.Resampling.LANCZOS)
    return pytesseract.image_to_string(
        crop,
        lang=language or _available_ocr_language(pytesseract),
        config=config,
    ).strip()


def _parse_screen_amount(text: str) -> Decimal | None:
    match = SCREEN_AMOUNT_RE.search(text)
    if not match:
        return None
    raw_amount = match.group("amount").strip()
    compact = re.sub(r"[\s\u00a0]", "", raw_amount)
    comma = compact.rfind(",")
    dot = compact.rfind(".")
    decimal_separator = None
    if comma >= 0 and dot >= 0:
        decimal_separator = "," if comma > dot else "."
    elif comma >= 0 or dot >= 0:
        separator = "," if comma >= 0 else "."
        trailing_digits = len(compact) - compact.rfind(separator) - 1
        if trailing_digits in (1, 2):
            decimal_separator = separator
        elif trailing_digits != 3:
            decimal_separator = separator

    if decimal_separator:
        thousands_separator = "," if decimal_separator == "." else "."
        normalized = compact.replace(thousands_separator, "")
        normalized = normalized.replace(decimal_separator, ".")
    else:
        normalized = compact.replace(",", "").replace(".", "")
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    is_negative = (
        match.group("sign") == "-"
        or (match.group("open") == "(" and match.group("close") == ")")
    )
    return -amount if is_negative else amount


def _parse_transaction_date(text: str) -> tuple[str | None, str | None]:
    if re.search(r"\b(?:today|dzi[sś])\b", text, re.IGNORECASE):
        relative_time_match = re.search(r"\b\d{1,2}:\d{2}\b", text)
        return date.today().isoformat(), (
            relative_time_match.group(0) if relative_time_match else None
        )
    date_match = SCREEN_DATE_RE.search(text)
    time_match = re.search(r"\b\d{1,2}:\d{2}\b", text)
    transaction_date = None
    if date_match:
        raw_date = date_match.group(0)
        for date_format in (
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y.%m.%d",
            "%d/%m/%Y",
            "%d.%m.%Y",
            "%d-%m-%Y",
        ):
            try:
                transaction_date = datetime.strptime(raw_date, date_format).date().isoformat()
                break
            except ValueError:
                continue
    return transaction_date, time_match.group(0) if time_match else None


def assess_account_tax(
    balance: Decimal,
    transactions: list[AccountTransaction],
    balance_observed: bool = True,
    ocr_confidence: float = 100.0,
) -> AccountTaxAssessment:
    required_tax = (balance * Decimal("0.12")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return AccountTaxAssessment(
        balance,
        required_tax,
        tuple(transactions),
        balance_observed,
        ocr_confidence,
    )


def _transaction_date_sort_key(transaction: AccountTransaction) -> tuple[int, int]:
    if transaction.date is None:
        return (1, 0)
    try:
        transaction_date = date.fromisoformat(transaction.date)
    except ValueError:
        return (1, 0)
    return (0, abs((date.today() - transaction_date).days))


def find_tax_payment_candidate(
    assessment: AccountTaxAssessment,
) -> PaymentMatch | None:
    if not assessment.balance_observed:
        return None
    candidates = [
        transaction
        for transaction in assessment.transactions
        if transaction.amount < 0
        and abs(transaction.amount) >= assessment.required_tax
    ]
    if not candidates:
        return None
    transaction = min(
        candidates,
        key=lambda item: (
            0 if SCREEN_TAX_RE.search(item.description) else 1,
            abs(abs(item.amount) - assessment.required_tax),
            *_transaction_date_sort_key(item),
        ),
    )
    explicitly_labeled = SCREEN_TAX_RE.search(transaction.description) is not None
    confidence = min(assessment.ocr_confidence, transaction.ocr_confidence)
    if explicitly_labeled and confidence >= 75:
        return PaymentMatch(
            transaction,
            False,
            "Opis transakcji wskazuje podatek, kwota pokrywa wymagane 12%, "
            f"a pewność OCR wynosi {confidence:.0f}%.",
        )
    if not explicitly_labeled:
        reason = "Opis transakcji nie potwierdza jednoznacznie, że jest to podatek."
    else:
        reason = f"Pewność OCR jest zbyt niska ({confidence:.0f}%)."
    return PaymentMatch(transaction, True, reason)


def find_penalty_payment_candidate(
    assessment: AccountTaxAssessment,
    penalty_amount: Decimal,
) -> PaymentMatch | None:
    candidates = [
        transaction
        for transaction in assessment.transactions
        if transaction.amount < 0
        and abs(transaction.amount) >= penalty_amount
    ]
    if not candidates:
        return None
    transaction = min(
        candidates,
        key=lambda item: (
            0 if SCREEN_PENALTY_RE.search(item.description) else 1,
            abs(abs(item.amount) - penalty_amount),
            *_transaction_date_sort_key(item),
        ),
    )
    explicitly_labeled = SCREEN_PENALTY_RE.search(transaction.description) is not None
    confidence = min(assessment.ocr_confidence, transaction.ocr_confidence)
    if explicitly_labeled and confidence >= 75:
        return PaymentMatch(
            transaction,
            False,
            "Opis transakcji wskazuje karę, kwota pokrywa jej wartość, "
            f"a pewność OCR wynosi {confidence:.0f}%.",
        )
    if not explicitly_labeled:
        reason = "Opis transakcji nie wskazuje jednoznacznie opłacenia kary."
    else:
        reason = f"Pewność OCR jest zbyt niska ({confidence:.0f}%)."
    return PaymentMatch(transaction, True, reason)


def format_ocr_diagnostics(
    assessment: AccountTaxAssessment | None,
    raw_text: str = "",
) -> str:
    if assessment is None:
        excerpt = raw_text.strip()[:1200] or "Brak tekstu rozpoznanego przez OCR."
        return (
            "Wynik OCR: nie można bezpiecznie odczytać salda i transakcji.\n"
            "Rozpoznany tekst (fragment):\n"
            f"{excerpt}\n"
        )
    lines = [
        f"Pewność OCR: {assessment.ocr_confidence:.0f}%",
        (
            f"Saldo: {money(assessment.balance)}"
            if assessment.balance_observed
            else "Saldo: nie było widoczne na screenie"
        ),
        (
            f"Podatek obliczony jako 12% salda: {money(assessment.required_tax)}"
            if assessment.balance_observed
            else "Podatek wymagany: nie można obliczyć bez odczytanego salda"
        ),
        "Odczytane transakcje:",
    ]
    lines.extend(
        f"- {money(transaction.amount)}"
        + (
            f" · {' '.join(part for part in (transaction.date, transaction.time) if part)}"
            if transaction.date or transaction.time
            else ""
        )
        + (f" · {transaction.description}" if transaction.description else "")
        + f" · OCR {transaction.ocr_confidence:.0f}%"
        for transaction in assessment.transactions
    )
    match = find_tax_payment_candidate(assessment)
    if match is None:
        if assessment.balance_observed:
            lines.append("Decyzja: nie znaleziono obciążenia w wymaganej kwocie.")
        else:
            lines.append("Decyzja: nie można zweryfikować podatku bez salda.")
    else:
        lines.append(
            "Decyzja: "
            + (
                "wymaga potwierdzenia przez użytkownika"
                if match.requires_confirmation
                else "kandydat ma jednoznaczny opis podatku"
            )
        )
        lines.append(f"Powód: {match.reason}")
    return "\n".join(lines)


def format_account_result(assessment: AccountTaxAssessment) -> str:
    lines = [
        f"Stan konta: {money(assessment.balance)}",
        f"Wymagany podatek (12%): {money(assessment.required_tax)}",
    ]
    paid_transaction = assessment.paid_transaction
    if paid_transaction:
        lines.append("Podatek zapłacony poprawnie.")
        lines.append(
            f"Wykryta ujemna transakcja: -{money(abs(paid_transaction.amount))}"
        )
        transaction_date = " ".join(
            part for part in (paid_transaction.date, paid_transaction.time) if part
        )
        if transaction_date:
            lines.append(f"Data transakcji na screenie: {transaction_date}")
    else:
        lines.append(
            "Nie wykryto zapłaty wymaganej kwoty podatku na widocznych transakcjach."
        )
        lines.append("Wyślij kolejny screen, jeśli zapłata jest na innej pozycji.")
        largest_debit = max(
            (item for item in assessment.transactions if item.amount < 0),
            key=lambda item: abs(item.amount),
            default=None,
        )
        if largest_debit:
            lines.append(
                f"Największa wykryta ujemna transakcja: -{money(abs(largest_debit.amount))}"
            )
        else:
            lines.append("Na screenie nie znaleziono ujemnej transakcji.")
    return "\n".join(lines)


def analyze_account_screen(
    data: bytes,
    allow_zero_balance: bool = False,
    require_balance: bool = True,
    language: str | None = None,
    layout: str | None = None,
) -> AccountTaxAssessment | None:
    """Odczytuje saldo i widoczne transakcje z różnych układów ekranu finansów."""
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img = img.copy()
    configured_language = language or OCR_LANGUAGE
    requested_layout = layout or OCR_LAYOUT
    available_languages = set(pytesseract.get_languages(config=""))
    requested_languages = configured_language.split("+")
    selected_languages = [
        item for item in requested_languages if item in available_languages
    ]
    if not selected_languages:
        raise RuntimeError(
            f"Brak plików językowych Tesseract dla ustawienia {configured_language!r}."
        )
    ocr_language = "+".join(selected_languages)
    if requested_layout == "dashboard":
        page_segmentations = (6,)
    elif requested_layout == "transaction-card":
        page_segmentations = (11, 7)
    else:
        page_segmentations = (6, 11, 7)
    best_assessment = None
    for candidate in _ocr_image_variants(img):
        for page_segmentation in page_segmentations:
            data_dict = pytesseract.image_to_data(
                candidate,
                lang=ocr_language,
                config=f"--psm {page_segmentation} -c preserve_interword_spaces=1",
                output_type=pytesseract.Output.DICT,
            )
            text, confidence = _text_and_confidence_from_ocr(data_dict)
            if not text.strip():
                continue
            assessment = _parse_account_screen_text(
                text,
                allow_zero_balance,
                require_balance,
                confidence,
            )
            if assessment is not None:
                if (
                    best_assessment is None
                    or assessment.ocr_confidence > best_assessment.ocr_confidence
                ):
                    best_assessment = assessment
                if (
                    assessment.balance_observed
                    and assessment.ocr_confidence >= 90
                ):
                    return assessment
    focused_balance = None
    if best_assessment is None or not best_assessment.balance_observed:
        focused_balance = _read_balance_from_image(img, ocr_language)
    if best_assessment is not None:
        if focused_balance is not None:
            balance, balance_confidence = focused_balance
            if balance > 0 or allow_zero_balance:
                return assess_account_tax(
                    balance,
                    list(best_assessment.transactions),
                    balance_observed=True,
                    ocr_confidence=max(
                        best_assessment.ocr_confidence,
                        balance_confidence,
                    ),
                )
        return best_assessment
    if focused_balance is not None:
        balance, _ = focused_balance
    else:
        balance = None

    # Keep support for the original finance-screen layout as a last resort.
    img = img.convert("RGB")
    if balance is None:
        balance_text = _read_crop(
            img,
            (0.27, 0.24, 0.42, 0.31),
            "--psm 7",
            ocr_language,
        )
        balance = _parse_screen_amount(balance_text)
    if balance is None or balance < 0 or (balance == 0 and not allow_zero_balance):
        return None

    rows = (
        (0.255, 0.34),
        (0.34, 0.42),
        (0.42, 0.50),
        (0.50, 0.58),
        (0.58, 0.67),
    )
    transactions = []
    for top, bottom in rows:
        amount_text = _read_crop(
            img,
            (0.89, top, 0.985, bottom),
            "--psm 7 -c tessedit_char_whitelist=0123456789.$,+-",
            ocr_language,
        )
        amount = _parse_screen_amount(amount_text)
        if amount is None:
            continue
        date_text = _read_crop(
            img,
            (0.78, top, 0.89, bottom),
            "--psm 6",
            ocr_language,
        )
        transaction_date, transaction_time = _parse_transaction_date(date_text)
        transactions.append(AccountTransaction(amount, transaction_date, transaction_time))

    if not transactions:
        return None
    return assess_account_tax(balance, transactions)


def _ocr_image_variants(img):
    from PIL import Image, ImageOps

    grayscale = ImageOps.exif_transpose(img).convert("L")
    width, height = grayscale.size
    scale = min(2, max(1, 3200 // max(width, height)))
    if scale > 1:
        grayscale = grayscale.resize(
            (width * scale, height * scale),
            Image.Resampling.LANCZOS,
        )
    contrast = ImageOps.autocontrast(grayscale)
    equalized = ImageOps.equalize(contrast)
    return (contrast, equalized)


def _text_and_confidence_from_ocr(data_dict: dict) -> tuple[str, float]:
    groups: dict[tuple[int, int, int], list[tuple[int, str, float]]] = {}
    confidences = []
    count = len(data_dict.get("text", []))
    for index in range(count):
        word = str(data_dict["text"][index]).strip()
        if not word:
            continue
        try:
            confidence = float(data_dict["conf"][index])
        except (ValueError, TypeError, IndexError):
            continue
        if confidence < 0:
            continue
        confidences.append(confidence)
        key = (
            int(data_dict["block_num"][index]),
            int(data_dict["par_num"][index]),
            int(data_dict["line_num"][index]),
        )
        groups.setdefault(key, []).append(
            (int(data_dict["word_num"][index]), word, confidence)
        )
    lines = []
    for words in groups.values():
        lines.append(" ".join(word for _, word, _ in sorted(words)))
    average_confidence = (
        sum(confidences) / len(confidences) if confidences else 0.0
    )
    return "\n".join(lines), average_confidence


def _extract_labeled_balance(text: str) -> Decimal | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        label = SCREEN_BALANCE_RE.search(line)
        if label is None or SCREEN_TRANSACTION_RE.search(line):
            continue
        candidates = [line[label.end():]]
        candidates.extend(lines[index + 1:index + 3])
        for candidate in candidates:
            if (
                SCREEN_TRANSACTION_RE.search(candidate)
                or SCREEN_TABLE_HEADER_RE.search(candidate)
                or SCREEN_DATE_RE.search(candidate)
            ):
                break
            for amount in _screen_amounts(candidate):
                if amount >= 0 and (
                    amount >= 100 or "$" in candidate or "€" in candidate
                ):
                    return amount
    return None


def _read_balance_from_image(img, language: str) -> tuple[Decimal, float] | None:
    import pytesseract
    from PIL import Image, ImageOps

    regions = (
        (0.27, 0.18, 0.47, 0.32),
        (0.10, 0.09, 0.46, 0.25),
    )
    best_result = None
    for left, top, right, bottom in regions:
        crop = img.crop(
            (
                int(img.width * left),
                int(img.height * top),
                int(img.width * right),
                int(img.height * bottom),
            )
        )
        crop = ImageOps.autocontrast(crop.convert("L"))
        crop = crop.resize(
            (crop.width * 5, crop.height * 5),
            Image.Resampling.LANCZOS,
        )
        for page_segmentation in (11, 6):
            data_dict = pytesseract.image_to_data(
                crop,
                lang=language,
                config=(
                    f"--psm {page_segmentation} "
                    "-c preserve_interword_spaces=1"
                ),
                output_type=pytesseract.Output.DICT,
            )
            text, confidence = _text_and_confidence_from_ocr(data_dict)
            balance = _extract_labeled_balance(text)
            if balance is not None and (
                best_result is None or confidence > best_result[1]
            ):
                best_result = balance, confidence
    return best_result


def _screen_amounts(text: str) -> list[Decimal]:
    text_without_dates = SCREEN_DATE_RE.sub(" ", text)
    text_without_dates = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text_without_dates)
    amounts = []
    for match in SCREEN_AMOUNT_RE.finditer(text_without_dates):
        raw_amount = match.group("amount")
        digits = re.sub(r"\D", "", raw_amount)
        has_currency = "$" in match.group(0)
        is_explicit = match.group("sign") is not None or (
            match.group("open") == "(" and match.group("close") == ")"
        )
        if len(digits) >= 7 and not has_currency and not is_explicit:
            continue
        amount = _parse_screen_amount(match.group(0))
        if amount is not None:
            amounts.append(amount)
    return amounts


def _parse_account_screen_text(
    text: str,
    allow_zero_balance: bool = False,
    require_balance: bool = True,
    ocr_confidence: float = 100.0,
) -> AccountTaxAssessment | None:
    """Extracts a labeled balance and transaction rows from OCR text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    balance = None
    for index, line in enumerate(lines):
        if (
            not SCREEN_BALANCE_RE.search(line)
            or SCREEN_TRANSACTION_RE.search(line)
        ):
            continue
        adjacent_balance_values = []
        for candidate_line in lines[index + 1:index + 3]:
            if (
                SCREEN_DATE_RE.search(candidate_line)
                or re.search(r"\d{1,2}:\d{2}", candidate_line)
                or SCREEN_BALANCE_RE.search(candidate_line)
                or SCREEN_TRANSACTION_RE.search(candidate_line)
                or SCREEN_TABLE_HEADER_RE.search(candidate_line)
            ):
                break
            candidate_amounts = _screen_amounts(candidate_line)
            if candidate_amounts:
                adjacent_balance_values.append(candidate_line)
                break
            if re.search(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]", candidate_line):
                break
        for candidate_line in (line, *adjacent_balance_values):
            label = SCREEN_BALANCE_RE.search(candidate_line)
            balance_text = (
                candidate_line[label.end():]
                if candidate_line is line and label is not None
                else candidate_line
            )
            amounts = _screen_amounts(balance_text)
            if amounts:
                balance = next(
                    (amount for amount in amounts if amount >= 0),
                    None,
                )
                if balance is not None:
                    break
        if balance is not None:
            break
    balance_observed = balance is not None
    if balance is None and not require_balance:
        balance = Decimal("0")
    if balance is None or balance < 0 or (balance == 0 and not allow_zero_balance):
        return None

    transactions: list[AccountTransaction] = []
    consumed_lines: set[int] = set()
    for index, line in enumerate(lines):
        if index in consumed_lines:
            continue
        if (
            SCREEN_TABLE_HEADER_RE.search(line)
            and not SCREEN_TRANSACTION_RE.search(line)
        ):
            continue
        if SCREEN_BALANCE_RE.search(line) or SCREEN_SUMMARY_RE.search(line):
            continue
        amounts = _screen_amounts(line)
        source_line = line
        if not amounts:
            adjacent_lines = []
            adjacent_indices = []
            found_adjacent_amount = False
            for adjacent_index in range(index + 1, min(index + 5, len(lines))):
                adjacent = lines[adjacent_index]
                if found_adjacent_amount:
                    if (
                        SCREEN_DATE_RE.search(adjacent)
                        or re.search(r"\d{1,2}:\d{2}", adjacent)
                        or re.search(r"\b(?:today|dzi[sś])\b", adjacent, re.I)
                    ):
                        adjacent_lines.append(adjacent)
                        adjacent_indices.append(adjacent_index)
                    break
                if (
                    SCREEN_BALANCE_RE.search(adjacent)
                    or SCREEN_SUMMARY_RE.search(adjacent)
                ):
                    break
                if (
                    len(re.sub(r"\D", "", adjacent)) >= 10
                    and not _screen_amounts(adjacent)
                    and not SCREEN_DATE_RE.search(adjacent)
                    and not re.search(r"\d{1,2}:\d{2}", adjacent)
                ):
                    continue
                adjacent_lines.append(adjacent)
                adjacent_indices.append(adjacent_index)
                if _screen_amounts(adjacent):
                    found_adjacent_amount = True
            source_line = " ".join((line, *adjacent_lines))
            amounts = _screen_amounts(source_line)
            if amounts:
                consumed_lines.update(adjacent_indices)
        if not amounts:
            continue
        has_payment_label = SCREEN_TRANSACTION_RE.search(source_line) is not None
        transaction_date, transaction_time = _parse_transaction_date(source_line)
        signed_amounts = [
            amount for amount in amounts if amount < 0
        ]
        if signed_amounts:
            amount = signed_amounts[-1]
        elif has_payment_label and (transaction_date or re.search(r"\d{1,2}:\d{2}", source_line)):
            amount = -abs(amounts[-1])
        else:
            continue
        description = SCREEN_DATE_RE.sub(" ", source_line)
        description = re.sub(r"\b\d{1,2}:\d{2}\b", " ", description)
        description = SCREEN_AMOUNT_RE.sub(" ", description)
        description = re.sub(
            r"^\s*(?:data|date)\s+(?:kwota|amount)\s+",
            " ",
            description,
            flags=re.IGNORECASE,
        )
        description = re.sub(
            r"\b(?:transactions?|transakcje|historia transakcji)\b",
            " ",
            description,
            flags=re.IGNORECASE,
        )
        description = re.sub(r"\s+", " ", description).strip(" |:-")
        transactions.append(
            AccountTransaction(
                amount,
                transaction_date,
                transaction_time,
                description,
                ocr_confidence,
            )
        )
    if not transactions:
        return None
    unique_transactions: list[AccountTransaction] = []
    for transaction in transactions:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(unique_transactions)
                if (
                    existing.amount,
                    existing.date,
                    existing.time,
                )
                == (
                    transaction.amount,
                    transaction.date,
                    transaction.time,
                )
            ),
            None,
        )
        if duplicate_index is None:
            unique_transactions.append(transaction)
        elif len(transaction.description) > len(
            unique_transactions[duplicate_index].description
        ):
            unique_transactions[duplicate_index] = transaction
    return assess_account_tax(
        balance,
        unique_transactions,
        balance_observed,
        ocr_confidence,
    )


# ---------- parser ----------

def to_decimal(text: str) -> Decimal | None:
    stripped = text.strip()
    negative = "-" in stripped or (stripped.startswith("(") and stripped.endswith(")"))
    cleaned = re.sub(r"[^\d.]", "", stripped)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def find_amount(lines: list[str], idx: int) -> Decimal | None:
    """Szuka kwoty w linii ze słowem kluczowym, a jeśli jej brak - w następnej."""
    for i in (idx, idx + 1):
        if i < len(lines):
            matches = AMOUNT_RE.findall(lines[i])
            if matches:
                return to_decimal(matches[-1])
    return None


def extract(text: str, keywords, used: set[int] | None = None) -> list[tuple[str, str, Decimal]]:
    """Zwraca listę (etykieta, linia, kwota). Każda linia jest użyta co najwyżej raz."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    used = used if used is not None else set()
    found = []
    for label, pattern in keywords:
        for idx, line in enumerate(lines):
            if idx in used or not pattern.search(line):
                continue
            amount = find_amount(lines, idx)
            if amount is not None:
                used.add(idx)
                found.append((label, line, amount))
                break
    return found


def analyze(text: str):
    """Zwraca (lista_zapłaconych, lista_sald). Pierwsza pozycja zapłaconych = główny wynik."""
    paid = extract(text, PAID_KEYWORDS)
    balance = extract(text, BALANCE_KEYWORDS)
    return paid, balance


def money(value: Decimal) -> str:
    rounded_value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded_value == 0:
        rounded_value = abs(rounded_value)
    units = (
        (Decimal("1000000000000000000"), "tryl"),
        (Decimal("1000000000000000"), "bld"),
        (Decimal("1000000000000"), "bln"),
        (Decimal("1000000000"), "mld"),
        (Decimal("1000000"), "mln"),
        (Decimal("1000"), "tys."),
    )
    for divisor, suffix in units:
        if abs(rounded_value) >= divisor:
            scaled = (rounded_value / divisor).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if abs(scaled) >= 1000:
                continue
            number = _format_polish_decimal(scaled)
            return f"{number} {suffix} $"
    return f"{_format_polish_decimal(rounded_value)} $"


def _format_polish_decimal(value: Decimal) -> str:
    formatted = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return formatted.rstrip("0").rstrip(",")


def format_stored_money(value: str) -> str:
    parsed = parse_stored_money(value)
    if parsed is None:
        raise ValueError(f"Invalid stored monetary amount: {value!r}")
    return money(parsed)


def parse_stored_money(value: str) -> Decimal | None:
    match = re.fullmatch(
        r"\s*([+-]?)\s*\$?\s*([\d\s.,]+?)\s*"
        r"(tryl\.?|bld\.?|bln\.?|mld\.?|mln\.?|tys\.?)?\s*\$?\s*",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return to_decimal(value)
    sign, number, suffix = match.groups()
    compact_suffixes = {
        "tryl": Decimal("1000000000000000000"),
        "bld": Decimal("1000000000000000"),
        "bln": Decimal("1000000000000"),
        "mld": Decimal("1000000000"),
        "mln": Decimal("1000000"),
        "tys": Decimal("1000"),
    }
    compact = suffix.lower().rstrip(".") if suffix else None
    if compact:
        normalized = number.replace(" ", "")
        if "," in normalized:
            normalized = normalized.replace(".", "").replace(",", ".")
        elif "." in normalized:
            integer_part, separator, decimal_part = normalized.rpartition(".")
            if separator and 1 <= len(decimal_part) <= 2:
                normalized = integer_part.replace(".", "") + "." + decimal_part
            else:
                normalized = normalized.replace(".", "")
    else:
        normalized = number.replace(" ", "")
        if "," in normalized and "." not in normalized:
            integer_part, separator, decimal_part = normalized.rpartition(",")
            if separator and 1 <= len(decimal_part) <= 2:
                normalized = integer_part.replace(",", "") + "." + decimal_part
            else:
                normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(",", "")
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    if sign == "-":
        amount = -amount
    if compact:
        amount *= compact_suffixes[compact]
    return amount


def format_result_text(paid, balance, raw_text: str = "") -> str:
    if not paid:
        snippet = raw_text.strip()[:700] or "(OCR nic nie rozpoznał)"
        return f"Nie znalazłem kwoty zapłaconego podatku.\nTekst z OCR:\n{snippet}"
    label, _, amount = paid[0]
    out = [f"Zapłacony podatek ({label}): {money(amount)}"]
    if balance:
        out.append(f"Saldo konta: {money(balance[0][2])}")
    for lbl, _, amt in paid[1:]:
        out.append(f"  inne: {lbl}: {money(amt)}")
    return "\n".join(out)
