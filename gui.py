"""Desktop control center for the Discord tax verification bot."""
import importlib.util
import logging
import platform
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import core
from bot import BotRunner

READY = "__ready__"
STOPPED = "__stopped__"
OCR_OK = "__ocr_ok__"
OCR_ERROR = "__ocr_error__"


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IRS · Discord Tax Assistant")
        self.geometry("1040x820")
        self.minsize(900, 700)
        self._configure_theme()

        self.cfg = core.load_config(use_env=False)
        self.language = "en" if self.cfg.get("language", "pl").lower() == "en" else "pl"
        self.connection_state = "stopped"
        self.connection_name = ""
        self.runner = None
        self._closing = False
        self.log_queue = queue.Queue()
        self.log_history = []

        self._setup_logging()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._poll_queue)

    def _configure_theme(self):
        self.configure(background="#0B0F16")
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        background = "#0B0F16"
        panel = "#141B26"
        field = "#0D131D"
        foreground = "#E8EDF5"
        muted = "#99A7BA"
        accent = "#6574E8"
        border = "#293444"

        style.configure(".", font=("Segoe UI", 10), foreground=foreground)
        style.configure("TFrame", background=background)
        style.configure("Card.TFrame", background=panel)
        style.configure("TLabel", background=panel, foreground=foreground)
        style.configure("Muted.TLabel", background=panel, foreground=muted)
        style.configure(
            "Title.TLabel",
            background=background,
            foreground="#F5F7FC",
            font=("Segoe UI Semibold", 25),
        )
        style.configure(
            "Subtitle.TLabel",
            background=background,
            foreground=muted,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background=background,
            foreground="#F5F7FC",
            font=("Segoe UI Semibold", 14),
        )
        style.configure(
            "CardTitle.TLabel",
            background=panel,
            foreground="#F5F7FC",
            font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "Body.TLabel",
            background=panel,
            foreground=foreground,
            font=("Segoe UI", 10),
        )
        style.configure(
            "MetricTitle.TLabel",
            background=panel,
            foreground=muted,
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "MetricValue.TLabel",
            background=panel,
            foreground="#F5F7FC",
            font=("Segoe UI Semibold", 12),
        )
        style.configure(
            "Status.TLabel",
            background=panel,
            foreground="#B2BDCB",
            padding=(11, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "Status.On.TLabel",
            background=panel,
            foreground="#75D6A0",
            padding=(11, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "Status.Connecting.TLabel",
            background=panel,
            foreground="#F2C66D",
            padding=(11, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "TEntry",
            fieldbackground=field,
            foreground=foreground,
            insertcolor=foreground,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=(10, 9),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", accent)],
            lightcolor=[("focus", accent)],
            darkcolor=[("focus", accent)],
        )
        style.configure(
            "TButton",
            background="#263243",
            foreground=foreground,
            bordercolor="#344358",
            focuscolor=accent,
            padding=(12, 9),
            relief="flat",
        )
        style.map(
            "TButton",
            background=[
                ("disabled", "#1B2430"),
                ("pressed", "#313E51"),
                ("active", "#303D50"),
            ],
            foreground=[("disabled", "#738094")],
        )
        style.configure(
            "Accent.TButton",
            background=accent,
            foreground="#FFFFFF",
            bordercolor=accent,
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", "#343B68"),
                ("pressed", "#5867D2"),
                ("active", "#7886F2"),
            ],
            foreground=[("disabled", "#A1A7C6")],
        )
        style.configure(
            "Danger.TButton",
            background="#422631",
            foreground="#FFC7D0",
            bordercolor="#633744",
        )
        style.map(
            "Danger.TButton",
            background=[
                ("disabled", "#211D25"),
                ("pressed", "#603744"),
                ("active", "#583440"),
            ],
            foreground=[("disabled", "#8A737B")],
        )
        style.configure("TCheckbutton", background=panel, foreground=muted)
        style.map(
            "TCheckbutton",
            background=[("active", panel)],
            foreground=[("active", foreground)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=field,
            background="#263243",
            foreground=foreground,
            arrowcolor=foreground,
            padding=(8, 6),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", field)],
            foreground=[("readonly", foreground)],
        )
        style.configure(
            "TNotebook",
            background=background,
            borderwidth=0,
            tabmargins=(0, 4, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=panel,
            foreground=muted,
            padding=(18, 11),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#2B3748"), ("active", "#202B39")],
            foreground=[("selected", "#FFFFFF"), ("active", foreground)],
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background="#293444",
            troughcolor=panel,
            bordercolor=panel,
            arrowcolor=muted,
            width=12,
        )

    def _t(self, polish: str, english: str) -> str:
        return english if self.language == "en" else polish

    def _setup_logging(self):
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        for name in ("podatek-bot", "discord"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)

    def _status_text(self):
        if self.connection_state == "connected":
            return self._t(
                f"Połączony jako {self.connection_name}",
                f"Connected as {self.connection_name}",
            )
        return {
            "stopped": self._t("Zatrzymany", "Stopped"),
            "connecting": self._t("Łączenie…", "Connecting…"),
            "stopping": self._t("Zatrzymywanie…", "Stopping…"),
        }.get(self.connection_state, self._t("Nieznany", "Unknown"))

    def _set_connection_state(self, state: str, name: str = ""):
        self.connection_state = state
        self.connection_name = name
        status = self._status_text()
        self.status_var.set(f"{self._t('Status', 'Status')}: {status}")
        self.bot_summary_var.set(status)
        style = {
            "connected": "Status.On.TLabel",
            "connecting": "Status.Connecting.TLabel",
            "stopping": "Status.Connecting.TLabel",
        }.get(state, "Status.TLabel")
        self.status_label.configure(style=style)

    def _build_ui(self):
        for child in self.winfo_children():
            child.destroy()

        self.status_var = tk.StringVar()
        self.bot_summary_var = tk.StringVar()
        self.guild_summary_var = tk.StringVar(
            value=self.cfg.get("guild_id") or self._t("Globalne", "Global")
        )
        self.ocr_summary_var = tk.StringVar(
            value=self._t("Automatyczna ścieżka", "Automatic path")
            if not self.cfg.get("tesseract_cmd")
            else self._t("Tesseract skonfigurowany", "Tesseract configured")
        )

        header = ttk.Frame(self)
        header.pack(fill="x", padx=28, pady=(23, 16))
        brand = ttk.Frame(header)
        brand.pack(side="left", fill="x", expand=True)
        ttk.Label(brand, text="IRS", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            brand,
            text=self._t(
                "PANEL ZARZĄDZANIA  /  PODATKI I FIRMY",
                "CONTROL CENTER  /  TAX & COMPANIES",
            ),
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(1, 0))
        language_bar = ttk.Frame(header, style="Card.TFrame")
        language_bar.pack(side="right", padx=(12, 0), pady=4)
        ttk.Label(
            language_bar,
            text=self._t("JĘZYK", "LANGUAGE"),
            style="MetricTitle.TLabel",
        ).pack(side="left", padx=(12, 6), pady=9)
        self.language_var = tk.StringVar(
            value="English" if self.language == "en" else "Polski"
        )
        language_select = ttk.Combobox(
            language_bar,
            textvariable=self.language_var,
            values=("Polski", "English"),
            state="readonly",
            width=11,
        )
        language_select.pack(side="left", padx=(0, 10), pady=7)
        language_select.bind("<<ComboboxSelected>>", self._change_language)

        controls = ttk.Frame(self, style="Card.TFrame", padding=(14, 11))
        controls.pack(fill="x", padx=28, pady=(0, 14))
        self.start_btn = ttk.Button(
            controls,
            text=self._t("▶  URUCHOM BOTA", "▶  START BOT"),
            command=self.start_bot,
            style="Accent.TButton",
            width=20,
        )
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(
            controls,
            text=self._t("■  ZATRZYMAJ", "■  STOP"),
            command=self.stop_bot,
            state="normal"
            if self.connection_state in ("connected", "connecting")
            else "disabled",
            style="Danger.TButton",
            width=15,
        )
        self.stop_btn.pack(side="left", padx=(0, 8))
        ttk.Button(
            controls,
            text=self._t("Zapisz konfigurację", "Save configuration"),
            command=self.save_settings,
        ).pack(side="left")
        status_area = ttk.Frame(controls, style="Card.TFrame")
        status_area.pack(side="right", padx=(8, 0))
        ttk.Label(
            status_area,
            text=self._t("STATUS BOTA", "BOT STATUS"),
            style="MetricTitle.TLabel",
        ).pack(anchor="e", padx=6)
        self.status_label = ttk.Label(
            status_area,
            textvariable=self.status_var,
            style="Status.TLabel",
        )
        self.status_label.pack(anchor="e", pady=(2, 0))
        if self.connection_state in ("connected", "connecting"):
            self.start_btn.config(state="disabled")

        overview = ttk.Frame(self)
        overview.pack(fill="x", padx=28, pady=(0, 14))
        metrics = ttk.Frame(overview)
        metrics.pack(fill="x", expand=True)
        for index, (title, variable) in enumerate(
            (
                (self._t("POŁĄCZENIE", "CONNECTION"), self.bot_summary_var),
                (self._t("SERWER", "SERVER"), self.guild_summary_var),
                ("TESSERACT OCR", self.ocr_summary_var),
            )
        ):
            card = ttk.Frame(metrics, style="Card.TFrame")
            card.pack(
                side="left",
                fill="both",
                expand=True,
                padx=(0 if index == 0 else 9, 0),
            )
            ttk.Label(card, text=title, style="MetricTitle.TLabel").pack(
                anchor="w", padx=15, pady=(12, 3)
            )
            ttk.Label(card, textvariable=variable, style="MetricValue.TLabel").pack(
                anchor="w", padx=15, pady=(0, 12)
            )

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=28, pady=(0, 14))
        dashboard = ttk.Frame(self.notebook, padding=22)
        settings_page = ttk.Frame(self.notebook, padding=22)
        ocr_page = ttk.Frame(self.notebook, padding=22)
        support_page = ttk.Frame(self.notebook, padding=22)
        logs_page = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(dashboard, text=self._t("Przegląd", "Overview"))
        self.notebook.add(settings_page, text=self._t("Konfiguracja", "Configuration"))
        self.notebook.add(ocr_page, text="OCR")
        self.notebook.add(support_page, text=self._t("Pomoc i wsparcie", "Help & support"))
        self.notebook.add(logs_page, text=self._t("Dziennik", "Activity log"))

        self._build_dashboard(dashboard)
        self._build_settings(settings_page)
        self._build_ocr(ocr_page)
        self._build_support(support_page)
        self._build_logs(logs_page)

        self._set_connection_state(self.connection_state, self.connection_name)

    def _build_dashboard(self, parent):
        hero = ttk.Frame(parent, style="Card.TFrame", padding=22)
        hero.pack(fill="x", pady=(0, 17))
        ttk.Label(
            hero,
            text=self._t("Wszystko pod kontrolą.", "Your operations, at a glance."),
            style="CardTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            hero,
            text=self._t(
                "Zarządzaj botem, sprawdzaj podatki ze screenów i śledź status firm "
                "z jednego miejsca.",
                "Run the bot, verify tax screenshots, and keep company status up to date "
                "from one workspace.",
            ),
            style="Body.TLabel",
            wraplength=800,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Label(
            parent,
            text=self._t("Pierwsze kroki", "Getting started"),
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 9))
        checklist = ttk.Frame(parent, style="Card.TFrame", padding=18)
        checklist.pack(fill="x", pady=(0, 16))
        checks = (
            (
                bool(self.cfg.get("token")),
                self._t("Token bota dodany", "Bot token added"),
            ),
            (
                bool(self.cfg.get("application_id", "").strip()),
                self._t("Application ID ustawione", "Application ID configured"),
            ),
            (
                bool(self.cfg.get("guild_id", "").strip()),
                self._t(
                    "ID serwera podane (zalecane do szybkiej synchronizacji)",
                    "Server ID added (recommended for faster command sync)",
                ),
            ),
            (
                bool(self.cfg.get("tesseract_cmd", "").strip()),
                self._t(
                    "Ścieżka Tesseract ustawiona lub dostępna w PATH",
                    "Tesseract path set or available in PATH",
                ),
            ),
        )
        for row, (complete, label) in enumerate(checks):
            ttk.Label(
                checklist,
                text="✓" if complete else "○",
                foreground="#75D6A0" if complete else "#F2C66D",
                background="#141B26",
                font=("Segoe UI Semibold", 12),
            ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            ttk.Label(checklist, text=label, style="Body.TLabel").grid(
                row=row, column=1, sticky="w", pady=5
            )
        checklist.columnconfigure(1, weight=1)

        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self._action_card(
            actions,
            self._t("Konfiguracja", "Configuration"),
            self._t(
                "Uzupełnij token, ID aplikacji i serwera.",
                "Add your token, application ID, and server ID.",
            ),
            self._t("Otwórz ustawienia", "Open settings"),
            lambda: self.notebook.select(1),
        ).pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._action_card(
            actions,
            self._t("Sprawdź OCR", "Test OCR"),
            self._t(
                "Przetestuj odczyt na screenie lokalnie, bez wysyłania go na Discorda.",
                "Test screenshot recognition locally without uploading it to Discord.",
            ),
            self._t("Otwórz narzędzia OCR", "Open OCR tools"),
            lambda: self.notebook.select(2),
        ).pack(side="left", fill="both", expand=True, padx=(8, 0))

    def _action_card(self, parent, title, description, button_text, command):
        card = ttk.Frame(parent, style="Card.TFrame", padding=18)
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text=description,
            style="Muted.TLabel",
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(7, 13))
        ttk.Button(card, text=button_text, command=command).pack(anchor="w")
        return card

    def _build_settings(self, parent):
        ttk.Label(
            parent,
            text=self._t("Połączenie z Discordem", "Discord connection"),
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 12))
        settings = ttk.Frame(parent, style="Card.TFrame", padding=18)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)

        self.token_var = tk.StringVar(value=self.cfg.get("token", ""))
        self.app_id_var = tk.StringVar(value=self.cfg.get("application_id", ""))
        self.guild_var = tk.StringVar(value=self.cfg.get("guild_id", ""))
        self.tess_var = tk.StringVar(value=self.cfg.get("tesseract_cmd", ""))
        language_labels = {
            "eng": self._t("Angielski", "English"),
            "pol": self._t("Polski", "Polish"),
            "eng+pol": self._t("Angielski + polski", "English + Polish"),
        }
        layout_labels = {
            "auto": self._t("Automatyczny", "Automatic"),
            "dashboard": self._t("Panel finansów", "Finance dashboard"),
            "transaction-card": self._t("Karta transakcji", "Transaction card"),
        }
        profile_labels = {
            "auto": self._t("Ogólny / automatyczny", "General / automatic"),
            "dashboard": self._t("Panel finansów", "Finance dashboard"),
            "transaction-card": self._t("Karta transakcji", "Transaction card"),
        }
        self.ocr_profiles = core.normalize_ocr_profiles(
            self.cfg.get("ocr_profiles"),
            self.cfg.get("ocr_language", "eng"),
            self.cfg.get("ocr_layout", "auto"),
        )
        self._ocr_profile_values = {
            value: key for key, value in profile_labels.items()
        }
        self.ocr_profile_var = tk.StringVar(value=profile_labels["auto"])
        configured_language, configured_layout = core.get_ocr_profile(
            self.ocr_profiles,
            "auto",
        )
        self._selected_ocr_profile = "auto"
        self.ocr_language_var = tk.StringVar(
            value=language_labels.get(configured_language, language_labels["eng"])
        )
        self.ocr_layout_var = tk.StringVar(
            value=layout_labels.get(configured_layout, layout_labels["auto"])
        )
        self._ocr_language_values = {
            value: key for key, value in language_labels.items()
        }
        self._ocr_layout_values = {value: key for key, value in layout_labels.items()}
        self.show_token = tk.BooleanVar(value=False)
        pad = {"padx": 9, "pady": 9}

        ttk.Label(settings, text=self._t("Token bota", "Bot token")).grid(
            row=0, column=0, sticky="w", **pad
        )
        self.token_entry = ttk.Entry(settings, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Checkbutton(
            settings,
            text=self._t("pokaż", "show"),
            variable=self.show_token,
            command=self._toggle_token,
        ).grid(row=0, column=2, **pad)

        ttk.Label(settings, text="Application ID").grid(
            row=1, column=0, sticky="w", **pad
        )
        ttk.Entry(settings, textvariable=self.app_id_var).grid(
            row=1, column=1, sticky="ew", **pad
        )
        ttk.Button(
            settings,
            text=self._t("Zaproś bota", "Invite bot"),
            command=self.open_invite,
        ).grid(row=1, column=2, **pad)

        ttk.Label(settings, text=self._t("ID serwera", "Server ID")).grid(
            row=2, column=0, sticky="w", **pad
        )
        ttk.Entry(settings, textvariable=self.guild_var).grid(
            row=2, column=1, sticky="ew", **pad
        )
        ttk.Label(
            settings,
            text=self._t(
                "Zalecane: komendy pojawią się szybciej.",
                "Recommended: commands appear faster.",
            ),
            style="Muted.TLabel",
            wraplength=230,
        ).grid(row=2, column=2, sticky="w", **pad)

        ttk.Label(settings, text=self._t("Tesseract (ścieżka)", "Tesseract (path)")).grid(
            row=3, column=0, sticky="w", **pad
        )
        ttk.Entry(settings, textvariable=self.tess_var).grid(
            row=3, column=1, sticky="ew", **pad
        )
        ttk.Button(
            settings,
            text=self._t("Wybierz plik…", "Browse…"),
            command=self.browse_tesseract,
        ).grid(row=3, column=2, **pad)
        ttk.Label(settings, text=self._t("Profil screena", "Screenshot profile")).grid(
            row=4, column=0, sticky="w", **pad
        )
        ttk.Combobox(
            settings,
            textvariable=self.ocr_profile_var,
            values=tuple(profile_labels.values()),
            state="readonly",
            width=24,
        ).grid(row=4, column=1, sticky="w", **pad)
        self.ocr_profile_var.trace_add("write", self._ocr_profile_changed)
        ttk.Label(settings, text=self._t("Język OCR", "OCR language")).grid(
            row=5, column=0, sticky="w", **pad
        )
        ttk.Combobox(
            settings,
            textvariable=self.ocr_language_var,
            values=tuple(language_labels.values()),
            state="readonly",
            width=24,
        ).grid(row=5, column=1, sticky="w", **pad)
        ttk.Label(settings, text=self._t("Układ screena", "Screenshot layout")).grid(
            row=6, column=0, sticky="w", **pad
        )
        ttk.Combobox(
            settings,
            textvariable=self.ocr_layout_var,
            values=tuple(layout_labels.values()),
            state="readonly",
            width=24,
        ).grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(
            settings,
            text=self._t(
                "Token jest przechowywany lokalnie w config.json. Nigdy nie udostępniaj "
                "tego pliku ani zrzutów ekranu z tokenem.",
                "The token is stored locally in config.json. Never share that file or "
                "screenshots showing your token.",
            ),
            style="Muted.TLabel",
            wraplength=780,
            justify="left",
        ).grid(row=7, column=0, columnspan=3, sticky="w", padx=9, pady=(14, 4))

        ttk.Label(
            parent,
            text=self._t(
                "Bot potrzebuje: wyświetlania kanałów, wysyłania wiadomości, "
                "osadzania linków oraz historii wiadomości.",
                "The bot needs permission to view channels, send messages, embed links, "
                "and read message history.",
            ),
            style="Muted.TLabel",
            wraplength=800,
        ).pack(anchor="w", pady=(13, 0))

    def _build_ocr(self, parent):
        ttk.Label(
            parent,
            text=self._t("Testowanie odczytu", "Recognition test"),
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 12))
        test = ttk.Frame(parent, style="Card.TFrame", padding=20)
        test.pack(fill="x")
        ttk.Label(
            test,
            text=self._t(
                "Weryfikacja screena bez wysyłania go na Discorda",
                "Verify a screenshot without uploading it to Discord",
            ),
            style="CardTitle.TLabel",
            wraplength=760,
        ).pack(anchor="w")
        ttk.Label(
            test,
            text=self._t(
                "Wybierz panel finansów albo pojedynczą kartę transakcji. Odczyt "
                "działa lokalnie; dziennik pokaże saldo, kwoty, opisy, pewność OCR "
                "i powód decyzji. Obraz nie jest zapisywany.",
                "Choose a finance dashboard or a single transaction card. Recognition "
                "runs locally; the activity log shows balances, amounts, descriptions, "
                "OCR confidence, and the decision reason. The image is not saved.",
            ),
            style="Muted.TLabel",
            wraplength=780,
            justify="left",
        ).pack(anchor="w", pady=(8, 16))
        ttk.Button(
            test,
            text=self._t("Wybierz screen i uruchom test", "Choose screenshot and run test"),
            command=self.test_ocr,
        ).pack(anchor="w")
        ttk.Label(
            parent,
            text=self._t(
                "Wskazówka: używaj czytelnego screena bez danych wrażliwych. Wynik OCR "
                "jest automatyczną oceną — sprawdź go ręcznie, jeśli obraz jest nieostry.",
                "Tip: use a clear screenshot without sensitive data. OCR is an automated "
                "assessment—review the result manually if the image is unclear.",
            ),
            style="Muted.TLabel",
            wraplength=800,
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

    def _build_support(self, parent):
        ttk.Label(
            parent,
            text=self._t("Centrum pomocy", "Support center"),
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 12))
        help_card = ttk.Frame(parent, style="Card.TFrame", padding=18)
        help_card.pack(fill="both", expand=True)
        guide = self._t(
            "SZYBKI START\n"
            "1. Uzupełnij token bota, Application ID i ID serwera w zakładce Konfiguracja.\n"
            "2. Zainstaluj Tesseract OCR. Jeśli nie jest w PATH, wskaż plik tesseract.exe.\n"
            "3. Zapisz konfigurację i uruchom bota.\n"
            "4. Na Discordzie ustaw kanały: /ustaw_kanal_podatkow i /ustaw_kanal_firm.\n"
            "5. Użyj /aktywna w kanale kategorii firmy; następnie /podatek ze screenem.\n\n"
            "PRZYDATNE KOMENDY\n"
            "/pomoc — instrukcja bezpośrednio na Discordzie (PL/EN).\n"
            "/przypomnienie — przypomnienia dla aktywnych, nieopłaconych firm.\n"
            "/kara_finansowa i /rejestr_kar — raporty i rejestr kar.\n"
            "/ulga i /muala — ulga podatkowa oraz ręczny reset okresu.\n\n"
            "ROZWIĄZYWANIE PROBLEMÓW\n"
            "• 403 Missing Access: sprawdź ID serwera i dodaj aplikację z zakresem "
            "applications.commands.\n"
            "• Bot nie odczytuje obrazu: sprawdź instalację Tesseract i wykonaj test OCR.\n"
            "• Nie udostępniaj tokena, config.json ani screenów z danymi osobowymi.",
            "QUICK START\n"
            "1. Add your bot token, Application ID, and server ID in Configuration.\n"
            "2. Install Tesseract OCR. If it is not on PATH, select tesseract.exe.\n"
            "3. Save configuration and start the bot.\n"
            "4. In Discord, configure channels with /ustaw_kanal_podatkow and /ustaw_kanal_firm.\n"
            "5. Run /aktywna in a company category, then use /podatek with a screenshot.\n\n"
            "USEFUL COMMANDS\n"
            "/pomoc — this guide in Discord (Polish/English).\n"
            "/przypomnienie — reminders for active companies with unconfirmed tax.\n"
            "/kara_finansowa and /rejestr_kar — penalty reports and register.\n"
            "/ulga and /muala — tax relief and manual period reset.\n\n"
            "TROUBLESHOOTING\n"
            "• 403 Missing Access: verify the server ID and invite the app with "
            "applications.commands scope.\n"
            "• OCR cannot read the image: check Tesseract and run the OCR test.\n"
            "• Never share your token, config.json, or screenshots with personal data.",
        )
        ttk.Label(
            help_card,
            text=guide,
            style="Body.TLabel",
            justify="left",
            wraplength=810,
        ).pack(anchor="nw", fill="both", expand=True)
        support_actions = ttk.Frame(parent)
        support_actions.pack(fill="x", pady=(13, 0))
        ttk.Button(
            support_actions,
            text=self._t("Otwórz README", "Open README"),
            command=self.open_readme,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            support_actions,
            text=self._t("Portal deweloperski Discord", "Discord Developer Portal"),
            command=lambda: webbrowser.open(
                "https://discord.com/developers/applications"
            ),
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            support_actions,
            text=self._t("Kopiuj bezpieczny raport", "Copy safe diagnostic report"),
            command=self.copy_diagnostic_report,
        ).pack(side="left")

    def _build_logs(self, parent):
        logs_header = ttk.Frame(parent)
        logs_header.pack(fill="x", pady=(0, 10))
        ttk.Label(
            logs_header,
            text=self._t("Dziennik aplikacji", "Application activity"),
            style="Section.TLabel",
        ).pack(side="left")
        ttk.Button(
            logs_header,
            text=self._t("Wyczyść", "Clear"),
            command=self.clear_logs,
        ).pack(side="right")
        log_panel = ttk.Frame(parent, style="Card.TFrame", padding=8)
        log_panel.pack(fill="both", expand=True)
        self.log_box = tk.Text(
            log_panel,
            height=18,
            state="disabled",
            wrap="word",
            background="#0D131D",
            foreground="#D6DFEC",
            insertbackground="#FFFFFF",
            selectbackground="#4655A8",
            selectforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=10,
            font=("Cascadia Mono", 9),
        )
        log_scrollbar = ttk.Scrollbar(
            log_panel,
            orient="vertical",
            command=self.log_box.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.log_box.configure(yscrollcommand=log_scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        log_scrollbar.pack(side="right", fill="y")
        self._render_logs()

    def _render_logs(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        if self.log_history:
            self.log_box.insert("end", "\n".join(self.log_history) + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _append_log(self, text: str):
        self.log_history.append(text)
        if len(self.log_history) > 2000:
            del self.log_history[:500]
        self.log_box.config(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _collect(self) -> dict:
        selected_profile = self._ocr_profile_values.get(
            self.ocr_profile_var.get(),
            "auto",
        )
        self.ocr_profiles[self._selected_ocr_profile] = {
            "language": self._ocr_language_values.get(
                self.ocr_language_var.get(),
                "eng",
            ),
            "layout": self._ocr_layout_values.get(
                self.ocr_layout_var.get(),
                "auto",
            ),
        }
        if selected_profile != self._selected_ocr_profile:
            profile_language, profile_layout = core.get_ocr_profile(
                self.ocr_profiles,
                selected_profile,
            )
        else:
            profile_language = self.ocr_profiles[selected_profile]["language"]
            profile_layout = self.ocr_profiles[selected_profile]["layout"]
        return {
            "token": self.token_var.get().strip(),
            "application_id": self.app_id_var.get().strip(),
            "guild_id": self.guild_var.get().strip(),
            "tesseract_cmd": self.tess_var.get().strip(),
            "ocr_language": profile_language,
            "ocr_layout": profile_layout,
            "ocr_profiles": {
                name: dict(profile)
                for name, profile in self.ocr_profiles.items()
            },
            "language": self.language,
        }

    def _ocr_profile_changed(self, *_args):
        selected_profile = self._ocr_profile_values.get(
            self.ocr_profile_var.get(),
            "auto",
        )
        if selected_profile == self._selected_ocr_profile:
            return
        self.ocr_profiles[self._selected_ocr_profile] = {
            "language": self._ocr_language_values.get(
                self.ocr_language_var.get(),
                "eng",
            ),
            "layout": self._ocr_layout_values.get(
                self.ocr_layout_var.get(),
                "auto",
            ),
        }
        language, layout = core.get_ocr_profile(
            self.ocr_profiles,
            selected_profile,
        )
        language_labels = {
            "eng": self._t("Angielski", "English"),
            "pol": self._t("Polski", "Polish"),
            "eng+pol": self._t("Angielski + polski", "English + Polish"),
        }
        layout_labels = {
            "auto": self._t("Automatyczny", "Automatic"),
            "dashboard": self._t("Panel finansów", "Finance dashboard"),
            "transaction-card": self._t("Karta transakcji", "Transaction card"),
        }
        self._selected_ocr_profile = selected_profile
        self.ocr_language_var.set(language_labels[language])
        self.ocr_layout_var.set(layout_labels[layout])

    def _change_language(self, _event=None):
        language = "en" if self.language_var.get() == "English" else "pl"
        if language == self.language:
            return
        settings = self._collect()
        settings["language"] = language
        try:
            core.save_config(settings)
        except OSError as exc:
            messagebox.showerror(
                self._t("Błąd zapisu", "Save error"),
                self._t(
                    f"Nie udało się zapisać ustawienia języka: {exc}",
                    f"Could not save the language preference: {exc}",
                ),
            )
            self.language_var.set("English" if self.language == "en" else "Polski")
            return
        self.cfg = settings
        self.language = language
        self._build_ui()

    def _toggle_token(self):
        self.token_entry.config(show="" if self.show_token.get() else "•")

    def save_settings(self, silent=False):
        settings = self._collect()
        try:
            core.save_config(settings)
        except OSError as exc:
            messagebox.showerror(
                self._t("Błąd zapisu", "Save error"),
                self._t(
                    f"Nie udało się zapisać konfiguracji: {exc}",
                    f"Could not save configuration: {exc}",
                ),
            )
            return
        self.cfg = settings
        self.guild_summary_var.set(
            settings["guild_id"] or self._t("Globalne", "Global")
        )
        self.ocr_summary_var.set(
            self._t("Automatyczna ścieżka", "Automatic path")
            if not settings["tesseract_cmd"]
            else self._t("Tesseract skonfigurowany", "Tesseract configured")
        )
        self.log_queue.put(
            self._t("Konfiguracja została zapisana.", "Configuration saved.")
        )
        if not silent:
            messagebox.showinfo(
                self._t("Zapisano", "Saved"),
                self._t(
                    "Konfiguracja została zapisana lokalnie. Nie udostępniaj pliku config.json.",
                    "Configuration saved locally. Do not share the config.json file.",
                ),
            )

    def browse_tesseract(self):
        path = filedialog.askopenfilename(
            title=self._t("Wskaż plik Tesseract", "Select Tesseract executable"),
            filetypes=[
                (self._t("Programy", "Executables"), "*.exe")
                if sys.platform == "win32"
                else (self._t("Wszystkie pliki", "All files"), "*"),
                (self._t("Wszystkie pliki", "All files"), "*.*"),
            ],
        )
        if path:
            self.tess_var.set(path)

    def open_invite(self):
        app_id = self.app_id_var.get().strip()
        if not app_id.isdigit():
            messagebox.showerror(
                self._t("Brak Application ID", "Application ID required"),
                self._t(
                    "Wpisz numeryczne Application ID z Discord Developer Portal.",
                    "Enter the numeric Application ID from the Discord Developer Portal.",
                ),
            )
            return
        required_permissions = 84992
        url = (
            "https://discord.com/oauth2/authorize"
            f"?client_id={app_id}&scope=bot%20applications.commands"
            f"&permissions={required_permissions}"
        )
        self.clipboard_clear()
        self.clipboard_append(url)
        webbrowser.open(url)
        self.log_queue.put(
            self._t(
                "Link zaproszenia skopiowano do schowka i otwarto w przeglądarce.",
                "Invite link copied to clipboard and opened in your browser.",
            )
        )

    def start_bot(self):
        cfg = self._collect()
        if not cfg["token"]:
            messagebox.showerror(
                self._t("Brak tokena", "Missing token"),
                self._t("Wpisz token bota.", "Enter the bot token."),
            )
            self.notebook.select(1)
            return
        if cfg["guild_id"] and not cfg["guild_id"].isdigit():
            messagebox.showerror(
                self._t("Nieprawidłowe ID serwera", "Invalid server ID"),
                self._t("ID serwera może zawierać tylko cyfry.", "Server ID must contain digits only."),
            )
            self.notebook.select(1)
            return
        try:
            core.save_config(cfg)
        except OSError as exc:
            messagebox.showerror(
                self._t("Błąd zapisu", "Save error"),
                self._t(
                    f"Nie udało się zapisać konfiguracji: {exc}",
                    f"Could not save configuration: {exc}",
                ),
            )
            return
        self.cfg = cfg
        core.configure_tesseract(cfg["tesseract_cmd"])

        self.runner = BotRunner(
            cfg["token"],
            cfg["guild_id"] or None,
            on_ready=lambda name: self.log_queue.put((READY, name)),
            on_stopped=lambda: self.log_queue.put((STOPPED, "")),
        )
        self.runner.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._set_connection_state("connecting")
        self.log_queue.put(self._t("Uruchamianie bota…", "Starting the bot…"))

    def stop_bot(self):
        if self.runner:
            self._set_connection_state("stopping", self.connection_name)
            self.runner.stop()

    def test_ocr(self):
        path = filedialog.askopenfilename(
            title=self._t("Wybierz screen finansów", "Choose a finance screenshot"),
            filetypes=[
                (
                    self._t("Obrazy", "Images"),
                    "*.png *.jpg *.jpeg *.webp *.bmp",
                ),
                (self._t("Wszystkie pliki", "All files"), "*.*"),
            ],
        )
        if not path:
            return
        core.configure_tesseract(self.tess_var.get().strip())
        core.configure_ocr(
            self._ocr_language_values.get(self.ocr_language_var.get(), "eng"),
            self._ocr_layout_values.get(self.ocr_layout_var.get(), "auto"),
        )
        self.ocr_summary_var.set(self._t("Przetwarzanie…", "Processing…"))
        self.notebook.select(4)
        self.log_queue.put(
            self._t(
                f"Test OCR: {Path(path).name} …",
                f"OCR test: {Path(path).name} …",
            )
        )
        threading.Thread(
            target=self._ocr_worker,
            args=(
                path,
                self._ocr_language_values.get(self.ocr_language_var.get(), "eng"),
                self._ocr_layout_values.get(self.ocr_layout_var.get(), "auto"),
            ),
            daemon=True,
        ).start()

    def _ocr_worker(self, path, language="eng", layout="auto"):
        try:
            data = Path(path).read_bytes()
            assessment = core.analyze_account_screen(
                data,
                allow_zero_balance=True,
                require_balance=False,
                language=language,
                layout=layout,
            )
            if assessment:
                self.log_queue.put(
                    self._t("Diagnostyka OCR:\n", "OCR diagnostics:\n")
                    + core.format_ocr_diagnostics(assessment)
                )
                self.log_queue.put((OCR_OK, ""))
                return

            text = core.run_ocr(data)
            paid, balance = core.analyze(text)
            self.log_queue.put(
                self._t("Diagnostyka OCR:\n", "OCR diagnostics:\n")
                + core.format_result_text(paid, balance, text)
            )
            self.log_queue.put((OCR_OK, ""))
        except Exception as exc:
            self.log_queue.put(
                self._t(f"Błąd OCR: {exc}", f"OCR error: {exc}")
            )
            self.log_queue.put((OCR_ERROR, ""))

    def open_readme(self):
        readme = Path(__file__).with_name("README.md")
        if not readme.exists():
            messagebox.showerror(
                self._t("Brak dokumentacji", "Documentation not found"),
                self._t(
                    "Nie znaleziono README.md w folderze aplikacji.",
                    "README.md was not found in the application folder.",
                ),
            )
            return
        webbrowser.open(readme.resolve().as_uri())

    def copy_diagnostic_report(self):
        cfg = self._collect()
        modules = {
            "discord.py": "discord",
            "Pillow": "PIL",
            "pytesseract": "pytesseract",
        }
        dependencies = []
        for display_name, module_name in modules.items():
            try:
                available = importlib.util.find_spec(module_name) is not None
            except (ImportError, ValueError):
                available = False
            dependencies.append(
                f"{display_name}: {'available' if available else 'missing'}"
            )
        report = "\n".join(
            (
                "IRS Discord Bot — diagnostic summary",
                f"Application version: 1.0",
                f"Operating system: {platform.system()} {platform.release()}",
                f"Python: {platform.python_version()}",
                f"Bot process running: {bool(self.runner and self.runner.running)}",
                f"Token configured: {bool(cfg['token'])}",
                f"Application ID configured: {bool(cfg['application_id'])}",
                f"Server ID configured: {bool(cfg['guild_id'])}",
                f"Tesseract path configured: {bool(cfg['tesseract_cmd'])}",
                f"UI language: {cfg['language']}",
                "Dependencies: " + ", ".join(dependencies),
                "",
                "Secrets and identifiers are intentionally excluded.",
            )
        )
        self.clipboard_clear()
        self.clipboard_append(report)
        messagebox.showinfo(
            self._t("Raport skopiowany", "Report copied"),
            self._t(
                "Bezpieczny raport diagnostyczny skopiowano do schowka. Nie zawiera tokena ani ID.",
                "A safe diagnostic report was copied. It does not contain your token or IDs.",
            ),
        )

    def clear_logs(self):
        self.log_history.clear()
        self._render_logs()

    def _poll_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple):
                    kind, value = item
                    if kind == READY:
                        self._set_connection_state("connected", value)
                        self.start_btn.config(state="disabled")
                        self.stop_btn.config(state="normal")
                    elif kind == STOPPED:
                        self._set_connection_state("stopped")
                        self.start_btn.config(state="normal")
                        self.stop_btn.config(state="disabled")
                    elif kind == OCR_OK:
                        self.ocr_summary_var.set(self._t("Odczyt gotowy", "Recognition complete"))
                    elif kind == OCR_ERROR:
                        self.ocr_summary_var.set(self._t("Błąd odczytu", "Recognition failed"))
                else:
                    self._append_log(item)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        if self.runner and self.runner.running:
            self.runner.stop()
            self.after(100, self._finish_close)
            return
        self.destroy()

    def _finish_close(self):
        if self.runner and self.runner.running:
            self.after(100, self._finish_close)
            return
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
