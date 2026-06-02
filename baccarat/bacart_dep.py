from __future__ import annotations

import csv
import json
import random
import sys
import time
import traceback
import tkinter as tk
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, TextIO, Tuple

from PIL import ImageGrab, ImageTk
import pyautogui
from tkinter import messagebox, ttk


APP_TITLE = "Bacart 6x18 Calibrator"
CONFIG_PATH = Path(__file__).resolve().parent / "bacart_calibration.json"
SETTINGS_PATH = Path(__file__).resolve().parent / "bacart_settings.json"
INFO_TEMPLATE_PATH = Path(__file__).resolve().parent / "bacart_calibration_info.txt"
FILES_DIR = Path(__file__).resolve().parent / "files"
RESULTS_CSV_PATH = FILES_DIR / "results.csv"
TERMINAL_LOG_PATH = FILES_DIR / "terminal.log"
GRID_ROWS = 6
GRID_COLS = 18
COLOR_NAMES = ("Blue", "Green", "Red")
# Default settings (can be overridden by settings.json)
DEFAULT_SETTINGS = {
    "sample_radius": 3,
    "match_threshold": 30000.0,
    "refresh_ms": 800,
    "auto_idle_seconds": 12.0,
    "progression_type": "Default",          # NEW: "Default", "Fibonacci", "D'Alembert"
    "progression_steps": [10, 20, 40, 80, 150, 290, 460, 900, 1400, 2200, 3800, 6000, 10000],
    "max_bet": 10000,
    "stop_loss": -5000,                     # Fixed stop loss
    "trailing_stop_pct": 25.0,              # NEW: trailing stop % from peak
    "profit_target": 2000,                  # NEW: session profit target
    "soft_match_brightness_min": 60,
    "soft_match_channel_spread_min": 7,
    "soft_match_hue_score_max": 42000.0,
    "loss_streak_cooldown": 3,              # NEW: skip after this many consecutive losses
    "side_selection_strategy": "follow_streak",
}
SIDE_SELECTION_STRATEGIES = (
    "follow_streak",
    "opposite_streak",
    "majority",
    "alternate",
    "randomize",
    "follow_trend",
    "pattern_follow",
    "weighted",
)
LAST_BET_BOX_LABEL = "R6C11"
LAST_BET_SEQUENCE_LEN = ((11 - 1) * GRID_ROWS) + 6
CHIP_VALUES = (
    ("Chip 1250", 1250),
    ("Chip 250", 250),
    ("Chip 100", 100),
    ("Chip 50", 50),
    ("Chip 10", 10),
)
EXTRA_LABELS = (
    "PLR",
    "BNR",
    "Chip 10",
    "Chip 50",
    "Chip 100",
    "Chip 250",
    "Chip 1250",
    "Sample Blue",
    "Sample Green",
    "Sample Red",
    "CD",
)
CSV_HEADERS = (
    "timestamp",
    "counter",
    "round_box",
    "result",
    "bet_side",
    "bet_amount",
    "event",
    "profit_total",
    "progression_step",
    "resolved_bets",
    "hit_rate",
    "note",
)


class Color(Enum):
    BLUE = "Blue"
    GREEN = "Green"
    RED = "Red"
    BLANK = "Blank"


# ---------- Logging ----------
def append_terminal_log_line(message: str):
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with TERMINAL_LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


class TerminalTee(TextIO):
    def __init__(self, stream: TextIO, prefix: str = ""):
        self.stream = stream
        self.prefix = prefix
        self._buffer = ""

    def write(self, data: str) -> int:
        written = self.stream.write(data)
        self.stream.flush()
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                append_terminal_log_line(f"{self.prefix}{line}")
        return written

    def flush(self):
        self.stream.flush()
        if self._buffer.strip():
            append_terminal_log_line(f"{self.prefix}{self._buffer}")
        self._buffer = ""

    def isatty(self) -> bool:
        return getattr(self.stream, "isatty", lambda: False)()


def setup_terminal_logging():
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    if not isinstance(sys.stdout, TerminalTee):
        sys.stdout = TerminalTee(sys.stdout, "STDOUT ")
    if not isinstance(sys.stderr, TerminalTee):
        sys.stderr = TerminalTee(sys.stderr, "STDERR ")


def log_unhandled_exception(exc_type, exc_value, exc_traceback):
    lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    for line in "".join(lines).splitlines():
        if line.strip():
            append_terminal_log_line(f"EXCEPTION {line}")
    if sys.__excepthook__:
        sys.__excepthook__(exc_type, exc_value, exc_traceback)


# ---------- Data Classes ----------
@dataclass
class CalibratedPoint:
    label: str
    x: int
    y: int
    rgb_sample: Optional[Tuple[int, int, int]] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.label == "CD":
            data["rgb_sample"] = None
            return data
        if self.rgb_sample is not None:
            data["rgb_sample"] = list(self.rgb_sample)
        return data


@dataclass
class ScanSnapshot:
    refs: Dict[str, Tuple[int, int, int]]
    board_values: List[str]
    counts: Dict[str, int]
    sequence: List[str]
    latest_result: Optional[str]
    cd_value: str
    cd_score: float
    all_blank: bool
    invalid: bool
    invalid_reason: str


def grid_label(index: int) -> str:
    col = index // GRID_ROWS + 1
    row = index % GRID_ROWS + 1
    return f"R{row}C{col}"


# ---------- Main Application ----------
class BacartCalibratorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        window_width = 550
        screen_margin = 20
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        available_height = max(540, min(760, screen_height - (screen_margin * 2)))
        x_offset = screen_width - window_width - screen_margin
        y_offset = 0
        self.root.geometry(f"{window_width}x{available_height}+{x_offset}+{y_offset}")
        self.root.configure(bg="#f4efe6")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        # Load settings
        self.settings = self._load_settings()
        self.progression_type = self.settings["progression_type"]
        self.default_progression_steps = self.settings.get(
            "default_progression_steps",
            self.settings["progression_steps"],
        )
        self.progression_steps = self._build_progression_steps_for_type(self.progression_type)
        self.max_bet = self.settings["max_bet"]
        self.stop_loss = self.settings["stop_loss"]
        self.trailing_stop_pct = self.settings["trailing_stop_pct"]
        self.profit_target = self.settings["profit_target"]
        self.loss_streak_cooldown = self.settings["loss_streak_cooldown"]
        self.side_selection_strategy = self.settings.get("side_selection_strategy", "follow_streak")
        # FIX: Ensure auto_idle_seconds is initialised from settings (prevent AttributeError)
        self.auto_idle_seconds = self.settings.get("auto_idle_seconds", 12.0)

        # Progression state for Fibonacci/D'Alembert
        self.current_bet_index = 0           # for Default progression (martingale-like)
        self.fib_prev = 0                    # for Fibonacci: previous bet amount
        self.fib_curr = 1                    # for Fibonacci: current bet amount
        self.dalembert_current = 1           # for D'Alembert: current unit

        self.point_labels: List[str] = [grid_label(i) for i in range(GRID_ROWS * GRID_COLS)] + list(EXTRA_LABELS)
        self.points: Dict[str, CalibratedPoint] = {}
        self.grid_value_labels: Dict[str, tk.Label] = {}
        self.grid_frame_labels: Dict[str, tk.Widget] = {}
        self.named_value_labels: Dict[str, tk.Label] = {}
        self.capture_overlay: Optional[tk.Toplevel] = None
        self.capture_canvas: Optional[tk.Canvas] = None
        self.capture_instruction_var = tk.StringVar(value="")
        self.capture_background_photo: Optional[ImageTk.PhotoImage] = None
        self.capture_index = 0
        self.monitoring = False
        self.auto_betting = False
        self.auto_sim_betting = False
        self.monitor_after_id: Optional[str] = None
        self.total_rounds = 0
        self.win_count = 0
        self.loss_count = 0
        self.tie_count = 0
        self.profit_total = 0
        self.last_bet_side: Optional[str] = None
        self.last_bet_amount = 0
        self.last_result_value = "None"
        self.last_sequence_len = 0
        self.last_valid_sequence_len: Optional[int] = None
        self.last_bet_basis_len = -1
        self.pending_bet_side: Optional[str] = None
        self.pending_bet_amount = 0
        self.pending_bet_basis_len = -1
        self.pending_bet_note = ""
        self.pending_bet_ready_at = 0.0
        self.bet_waiting_for_reset = False
        self.record_counter = 1
        self.skip_count = 0
        self.last_logged_sequence_len = -1
        self.log_path = RESULTS_CSV_PATH
        self.terminal_log_path = TERMINAL_LOG_PATH
        self.peak_profit_total = 0
        self.max_drawdown = 0
        self.current_loss_streak = 0
        self.max_loss_streak = 0
        self.resolved_bet_count = 0
        self.last_bet_progression_index = 0
        self.click_after_ids: List[str] = []  # For staggered clicks
        self.cooldown_skip_active = False     # NEW: skip one round after loss streak

        self.sample_radius_var = tk.IntVar(value=self.settings["sample_radius"])
        self.match_threshold_var = tk.DoubleVar(value=self.settings["match_threshold"])
        self.refresh_ms_var = tk.IntVar(value=self.settings["refresh_ms"])
        self.status_var = tk.StringVar(value="Ready")
        self.cd_var = tk.StringVar(value="CD Area: N/A")
        self.progress_var = tk.StringVar(value=self._build_progress_text())

        self._build_ui()
        self._load_config()
        self._ensure_log_file()
        self._set_status("Ready")
        self._refresh_progress()
        self._bind_shortcuts()
        self._log_audit(
            "app_started",
            settings_path=str(SETTINGS_PATH),
            results_csv=str(RESULTS_CSV_PATH),
            terminal_log=str(TERMINAL_LOG_PATH),
            progression_type=self.progression_type,
            side_selection_strategy=self.side_selection_strategy,
        )

    def _load_settings(self) -> dict:
        if SETTINGS_PATH.exists():
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                merged = DEFAULT_SETTINGS.copy()
                merged.update(saved)
                return merged
        return DEFAULT_SETTINGS.copy()

    def _save_settings(self):
        # FIX: Use settings dict for auto_idle_seconds instead of instance attribute (prevent AttributeError)
        self.settings.update({
            "sample_radius": int(self.sample_radius_var.get()),
            "match_threshold": float(self.match_threshold_var.get()),
            "refresh_ms": int(self.refresh_ms_var.get()),
            "auto_idle_seconds": self.auto_idle_seconds,
            "progression_type": self.progression_type,
            "progression_steps": self.progression_steps,
            "default_progression_steps": self.default_progression_steps,
            "max_bet": self.max_bet,
            "stop_loss": self.stop_loss,
            "trailing_stop_pct": self.trailing_stop_pct,
            "profit_target": self.profit_target,
            "loss_streak_cooldown": self.loss_streak_cooldown,
            "side_selection_strategy": self.side_selection_strategy,
        })
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=2)
        self._log_audit("settings_saved", settings=self.settings)

    def _bind_shortcuts(self):
        self.root.bind("<Escape>", lambda e: self.emergency_stop())

    def emergency_stop(self):
        """Keyboard shortcut to stop all automation and clear pending bet."""
        self.stop_monitor()
        self.stop_auto_bet(reset_status=False)
        self.stop_auto_sim(reset_status=False)
        self.pending_bet_side = None
        self.pending_bet_amount = 0
        self.pending_bet_basis_len = -1
        self.pending_bet_ready_at = 0.0
        self.pending_bet_note = ""
        self.bet_waiting_for_reset = False
        self._set_status("EMERGENCY STOP: All automation halted.")
        self._log_audit("emergency_stop")
        self._update_stats_display()

    # ---------- UI Building ----------
    def _build_ui(self):
        self.main_canvas = tk.Canvas(self.root, bg="#f4efe6", highlightthickness=0, bd=0)
        self.main_canvas.pack(side="left", fill="both", expand=True)
        self.main_scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.main_canvas.yview)
        self.main_scrollbar.pack(side="right", fill="y")
        self.main_canvas.configure(yscrollcommand=self.main_scrollbar.set)

        self.main_frame = ttk.Frame(self.main_canvas)
        self.main_canvas_window = self.main_canvas.create_window((0, 0), window=self.main_frame, anchor="nw")
        self.main_frame.bind("<Configure>", self._on_main_frame_configure)
        self.main_canvas.bind("<Configure>", self._on_main_canvas_configure)
        self.main_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        toolbar = ttk.Frame(self.main_frame, padding=10)
        toolbar.pack(fill="x")

        button_row_top = ttk.Frame(toolbar)
        button_row_top.pack(fill="x", anchor="w")
        tk.Button(button_row_top, text="Calibrate", command=self.start_calibration, bg="#d97706", fg="white", width=10).pack(side="left", padx=4, pady=(0, 4))
        tk.Button(button_row_top, text="Save", command=self._save_config, bg="#2563eb", fg="white", width=8).pack(side="left", padx=4, pady=(0, 4))
        tk.Button(button_row_top, text="Load", command=self._load_config, bg="#0891b2", fg="white", width=8).pack(side="left", padx=4, pady=(0, 4))
        tk.Button(button_row_top, text="Clear", command=self.clear_points, bg="#6b7280", fg="white", width=8).pack(side="left", padx=4, pady=(0, 4))
        tk.Button(button_row_top, text="Settings", command=self._open_settings_dialog, bg="#4b5563", fg="white", width=8).pack(side="left", padx=4, pady=(0, 4))
        tk.Button(button_row_top, text="Exit", command=self._exit_app, bg="#dc2626", fg="white", width=8).pack(side="left", padx=4, pady=(0, 4))

        button_row_bottom = ttk.Frame(toolbar)
        button_row_bottom.pack(fill="x", anchor="w")
        tk.Button(button_row_bottom, text="Scan Once", command=self.scan_once, bg="#7c3aed", fg="white", width=10).pack(side="left", padx=4, pady=(0, 4))
        self.monitor_btn = tk.Button(button_row_bottom, text="Start Monitor", command=self.toggle_monitor, bg="#15803d", fg="white", width=12)
        self.monitor_btn.pack(side="left", padx=4, pady=(0, 4))
        self.auto_btn = tk.Button(button_row_bottom, text="Start Auto", command=self.toggle_auto_bet, bg="#1d4ed8", fg="white", width=10)
        self.auto_btn.pack(side="left", padx=4, pady=(0, 4))
        self.autosim_btn = tk.Button(button_row_bottom, text="Start AutoSim", command=self.toggle_auto_sim, bg="#0f766e", fg="white", width=12)
        self.autosim_btn.pack(side="left", padx=4, pady=(0, 4))
        tk.Button(button_row_bottom, text="Recalibrate Point", command=self.recalibrate_point, bg="#b45309", fg="white", width=14).pack(side="left", padx=4, pady=(0, 4))

        settings_row = ttk.Frame(toolbar)
        settings_row.pack(fill="x", anchor="w")
        ttk.Label(settings_row, text="Radius").pack(side="left", padx=(4, 4))
        ttk.Entry(settings_row, textvariable=self.sample_radius_var, width=4, justify="center").pack(side="left")
        ttk.Label(settings_row, text="Threshold").pack(side="left", padx=(16, 4))
        ttk.Entry(settings_row, textvariable=self.match_threshold_var, width=10, justify="center").pack(side="left")
        ttk.Label(settings_row, text="Refresh ms").pack(side="left", padx=(16, 4))
        ttk.Entry(settings_row, textvariable=self.refresh_ms_var, width=6, justify="center").pack(side="left")
        ttk.Button(settings_row, text="Info", command=self._show_calibration_info).pack(side="left", padx=(16, 4))

        info = ttk.Frame(self.main_frame, padding=(10, 0, 10, 10))
        info.pack(fill="x")
        ttk.Label(info, textvariable=self.progress_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.cd_var).pack(anchor="w", pady=(2, 0))
        self.status_text = tk.Text(
            info,
            height=2,
            width=64,
            wrap="word",
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            bg=self.root.cget("bg"),
            font="TkDefaultFont",
            padx=0,
            pady=0,
        )
        self.status_text.tag_configure("reset_warning", foreground="red")
        self.status_text.configure(state="disabled")
        self.status_text.pack(anchor="w", fill="x", pady=(2, 0))

        stats = tk.LabelFrame(
            self.main_frame,
            text="Statistics",
            bg="#f4efe6",
            padx=10,
            pady=8,
        )
        stats.pack(fill="x", anchor="nw", padx=10, pady=(0, 10))
        stats.columnconfigure(0, weight=1)
        stats.columnconfigure(1, weight=1)
        self.auto_state_var = tk.StringVar(value="Auto: OFF")
        self.rounds_var = tk.StringVar(value="Rounds: 0")
        self.win_loss_var = tk.StringVar(value="Wins: 0 | Losses: 0")
        self.tie_var = tk.StringVar(value="Ties: 0")
        self.profit_var = tk.StringVar(value="Profit: 0")
        self.progression_var = tk.StringVar(value=f"Prog: {self.progression_type} | Step: ?")
        self.analysis_var = tk.StringVar(value="Hit Rate: 0.0% | Resolved: 0 | Skips: 0")
        self.risk_var = tk.StringVar(value="Peak: 0 | Drawdown: 0 | Max DD: 0 | Max LStreak: 0")
        self.pending_var = tk.StringVar(value="Pending Bet: None")
        self.last_result_var = tk.StringVar(value="Last Result: None")
        self.last_bet_var = tk.StringVar(value="Last Bet: None")
        ttk.Label(stats, textvariable=self.auto_state_var).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=2)
        ttk.Label(stats, textvariable=self.rounds_var).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(stats, textvariable=self.win_loss_var).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=2)
        ttk.Label(stats, textvariable=self.profit_var).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(stats, textvariable=self.tie_var).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=2)
        ttk.Label(stats, textvariable=self.progression_var).grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(stats, textvariable=self.pending_var).grid(row=3, column=0, sticky="w", padx=(0, 12), pady=2)
        ttk.Label(stats, textvariable=self.last_result_var).grid(row=3, column=1, sticky="w", pady=2)
        ttk.Label(stats, textvariable=self.last_bet_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(stats, textvariable=self.analysis_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(stats, textvariable=self.risk_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=2)

        board_container = ttk.Frame(self.main_frame, padding=10)
        board_container.pack(fill="x", anchor="nw")

        board = tk.LabelFrame(
            board_container,
            text="Board 6 x 18",
            bg="#f4efe6",
            padx=8,
            pady=8,
        )
        board.pack(fill="x", anchor="nw")

        board_canvas = tk.Canvas(board, bg="#f4efe6", highlightthickness=0, bd=0, height=160)
        board_canvas.pack(fill="x", expand=True, side="top")
        board_scrollbar = ttk.Scrollbar(board, orient="horizontal", command=board_canvas.xview)
        board_scrollbar.pack(fill="x", side="bottom", pady=(6, 0))
        board_canvas.configure(xscrollcommand=board_scrollbar.set)

        board_grid = tk.Frame(board_canvas, bg="#f4efe6")
        board_canvas_window = board_canvas.create_window((0, 0), window=board_grid, anchor="nw")

        def _sync_board_scrollregion(_event):
            board_canvas.configure(scrollregion=board_canvas.bbox("all"))

        def _sync_board_window_width(event):
            required_width = board_grid.winfo_reqwidth()
            canvas_width = event.width
            board_canvas.itemconfigure(
                board_canvas_window,
                width=required_width if required_width > canvas_width else canvas_width,
            )

        board_grid.bind("<Configure>", _sync_board_scrollregion)
        board_canvas.bind("<Configure>", _sync_board_window_width)

        for row in range(GRID_ROWS):
            for col in range(GRID_COLS):
                label = f"R{row + 1}C{col + 1}"
                cell = tk.Label(
                    board_grid,
                    text=label,
                    width=6,
                    height=2,
                    relief="ridge",
                    bd=1,
                    bg="#ddd6c8",
                    fg="#2b2118",
                    font=("Arial", 6, "bold"),
                    justify="center",
                    anchor="center",
                )
                cell.grid(row=row, column=col, padx=1, pady=1, sticky="nsew")
                self.grid_frame_labels[label] = cell
                self.grid_value_labels[label] = cell

        for col in range(GRID_COLS):
            board_grid.grid_columnconfigure(col, weight=0)

        side = tk.LabelFrame(
            self.main_frame,
            text="Named Points",
            bg="#f4efe6",
            padx=10,
            pady=10,
        )
        side.pack(fill="x", anchor="nw", padx=10, pady=(0, 10))
        for col in range(4):
            side.grid_columnconfigure(col, weight=0)

        items_per_column = (len(EXTRA_LABELS) + 1) // 2
        for index, label in enumerate(EXTRA_LABELS):
            group_col = index // items_per_column
            group_row = index % items_per_column
            label_col = group_col * 2
            value_col = label_col + 1

            ttk.Label(side, text=label, width=14).grid(row=group_row, column=label_col, sticky="w", pady=2, padx=(0, 6))
            value = tk.Label(
                side,
                text="Not set",
                width=18,
                relief="ridge",
                bd=1,
                bg="#e5e7eb",
                fg="#374151",
                font=("Arial", 9, "bold"),
                anchor="w",
                padx=6,
            )
            value.grid(row=group_row, column=value_col, sticky="w", pady=2, padx=(0, 14))
            self.grid_frame_labels[label] = value
            self.named_value_labels[label] = value

    def _open_settings_dialog(self):
        """Dialog to edit progression type, steps, limits, etc."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Advanced Settings")
        dialog.attributes("-topmost", True)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill="both", expand=True)

        # Progression Type dropdown
        ttk.Label(frame, text="Progression Type:").grid(row=0, column=0, sticky="w", pady=5)
        prog_type_var = tk.StringVar(value=self.progression_type)
        prog_type_menu = ttk.Combobox(frame, textvariable=prog_type_var, values=["Default", "Fibonacci", "D'Alembert"], state="readonly")
        prog_type_menu.grid(row=0, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Progression Steps:").grid(row=1, column=0, sticky="w", pady=5)
        prog_var = tk.StringVar(value=",".join(str(s) for s in self.progression_steps))
        prog_entry = ttk.Entry(frame, textvariable=prog_var, width=50)
        prog_entry.grid(row=1, column=1, sticky="w", pady=5)

        def refresh_progression_steps_preview(_event=None):
            selected_type = prog_type_var.get()
            if selected_type == "Default":
                steps = self.default_progression_steps
                prog_entry.configure(state="normal")
            else:
                steps = self._build_progression_steps_for_type(selected_type)
                prog_entry.configure(state="readonly")
            prog_var.set(",".join(str(s) for s in steps))

        prog_type_menu.bind("<<ComboboxSelected>>", refresh_progression_steps_preview)
        refresh_progression_steps_preview()

        ttk.Label(frame, text="Max Bet:").grid(row=2, column=0, sticky="w", pady=5)
        max_bet_var = tk.IntVar(value=self.max_bet)
        max_bet_entry = ttk.Entry(frame, textvariable=max_bet_var, width=10)
        max_bet_entry.grid(row=2, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Fixed Stop Loss (profit threshold):").grid(row=3, column=0, sticky="w", pady=5)
        stop_loss_var = tk.IntVar(value=self.stop_loss)
        stop_loss_entry = ttk.Entry(frame, textvariable=stop_loss_var, width=10)
        stop_loss_entry.grid(row=3, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Trailing Stop (%) from peak:").grid(row=4, column=0, sticky="w", pady=5)
        trailing_var = tk.DoubleVar(value=self.trailing_stop_pct)
        trailing_entry = ttk.Entry(frame, textvariable=trailing_var, width=10)
        trailing_entry.grid(row=4, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Profit Target (session):").grid(row=5, column=0, sticky="w", pady=5)
        target_var = tk.IntVar(value=self.profit_target)
        target_entry = ttk.Entry(frame, textvariable=target_var, width=10)
        target_entry.grid(row=5, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Auto Idle Seconds (after bet):").grid(row=6, column=0, sticky="w", pady=5)
        idle_var = tk.DoubleVar(value=self.auto_idle_seconds)
        idle_entry = ttk.Entry(frame, textvariable=idle_var, width=10)
        idle_entry.grid(row=6, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Loss Streak Cooldown (skip after N losses):").grid(row=7, column=0, sticky="w", pady=5)
        loss_streak_var = tk.IntVar(value=self.loss_streak_cooldown)
        loss_streak_entry = ttk.Entry(frame, textvariable=loss_streak_var, width=10)
        loss_streak_entry.grid(row=7, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Side Selection Strategy:").grid(row=8, column=0, sticky="w", pady=5)
        side_strategy_var = tk.StringVar(value=self.side_selection_strategy)
        side_strategy_menu = ttk.Combobox(
            frame,
            textvariable=side_strategy_var,
            values=SIDE_SELECTION_STRATEGIES,
            state="readonly",
            width=24,
        )
        side_strategy_menu.grid(row=8, column=1, sticky="w", pady=5)

        def save_settings():
            try:
                new_type = prog_type_var.get()
                self.progression_type = new_type
                self.max_bet = max_bet_var.get()
                if new_type == "Default":
                    new_steps = [int(x.strip()) for x in prog_var.get().split(",") if x.strip()]
                    if new_steps:
                        self.default_progression_steps = new_steps
                self.progression_steps = self._build_progression_steps_for_type(new_type)
                self.stop_loss = stop_loss_var.get()
                self.trailing_stop_pct = trailing_var.get()
                self.profit_target = target_var.get()
                self.auto_idle_seconds = idle_var.get()
                self.loss_streak_cooldown = loss_streak_var.get()
                self.side_selection_strategy = side_strategy_var.get()
                # Update settings dict
                self.settings.update({
                    "progression_type": self.progression_type,
                    "progression_steps": self.progression_steps,
                    "default_progression_steps": self.default_progression_steps,
                    "max_bet": self.max_bet,
                    "stop_loss": self.stop_loss,
                    "trailing_stop_pct": self.trailing_stop_pct,
                    "profit_target": self.profit_target,
                    "auto_idle_seconds": self.auto_idle_seconds,
                    "loss_streak_cooldown": self.loss_streak_cooldown,
                    "side_selection_strategy": self.side_selection_strategy,
                })
                self._save_settings()
                self._reset_progression_state()  # Reset progression counters
                self._update_stats_display()
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("Error", f"Invalid settings: {e}")

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=9, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="Save", command=save_settings).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side="left", padx=5)

    def _reset_progression_state(self):
        """Reset progression index and internal counters when changing settings."""
        self.current_bet_index = 0
        self.fib_prev = 0
        self.fib_curr = 1
        self.dalembert_current = 1
        self.cooldown_skip_active = False

    def _build_progression_steps_for_type(self, progression_type: str) -> List[int]:
        """Build the visible/active progression values for the selected progression type."""
        base_steps = getattr(self, "default_progression_steps", None) or DEFAULT_SETTINGS["progression_steps"]
        base_unit = max(1, int(base_steps[0])) if base_steps else 10
        max_bet = int(getattr(self, "max_bet", DEFAULT_SETTINGS["max_bet"]))
        length = max(len(base_steps), 13)

        if progression_type == "Default":
            return [int(step) for step in base_steps]
        if progression_type == "Fibonacci":
            values: List[int] = []
            prev, curr = 0, 1
            for _ in range(length):
                values.append(min(max_bet, curr * base_unit))
                prev, curr = curr, prev + curr
            return values
        if progression_type == "D'Alembert":
            return [min(max_bet, (index + 1) * base_unit) for index in range(length)]
        return [int(step) for step in base_steps]

    def _base_progression_unit(self) -> int:
        """Use the first configured Default step as the unit for unit-based progressions."""
        if self.default_progression_steps:
            return max(1, int(self.default_progression_steps[0]))
        return 10

    def _get_current_bet_amount(self) -> int:
        """Return the next bet amount based on selected progression type."""
        if not self.progression_steps:
            return 10
        if self.progression_type == "Default":
            idx = min(self.current_bet_index, len(self.progression_steps) - 1)
            return self.progression_steps[idx]
        elif self.progression_type == "Fibonacci":
            # FIX: Safer Fibonacci step lookup using nearest value instead of exact index
            target = self.fib_curr * self._base_progression_unit()
            # Find closest step (or floor) to avoid ValueError
            closest_idx = min(range(len(self.progression_steps)), key=lambda i: abs(self.progression_steps[i] - target))
            return self.progression_steps[closest_idx]
        elif self.progression_type == "D'Alembert":
            idx = min(self.dalembert_current - 1, len(self.progression_steps) - 1)
            return self.progression_steps[idx]
        else:
            return 10  # fallback

    def _get_current_progression_step_value(self) -> int:
        """Return the current progression step value for display and CSV logging."""
        if self.progression_type == "Default":
            if not self.progression_steps:
                return 0
            return min(self.current_bet_index, len(self.progression_steps) - 1)
        if self.progression_type == "Fibonacci":
            return self.fib_curr
        if self.progression_type == "D'Alembert":
            return self.dalembert_current
        return 0

    def _update_progression_on_result(self, won: bool):
        """Update progression state after a win/loss (ignored for ties)."""
        if self.progression_type == "Default":
            if won:
                self.current_bet_index = 0
            else:
                self.current_bet_index = min(self.current_bet_index + 1, len(self.progression_steps) - 1)
        elif self.progression_type == "Fibonacci":
            if won:
                # Move two steps back
                new_prev = max(0, self.fib_prev - self.fib_curr)
                new_curr = max(1, self.fib_prev)
                self.fib_prev, self.fib_curr = new_prev, new_curr
                # Ensure minimum 1
                if self.fib_curr < 1:
                    self.fib_curr = 1
                if self.fib_prev < 0:
                    self.fib_prev = 0
            else:
                # Move one step forward
                next_val = self.fib_prev + self.fib_curr
                self.fib_prev, self.fib_curr = self.fib_curr, next_val
        elif self.progression_type == "D'Alembert":
            if won:
                self.dalembert_current = max(1, self.dalembert_current - 1)
            else:
                max_units = max(1, self.max_bet // self._base_progression_unit())
                self.dalembert_current = min(max_units, self.dalembert_current + 1)

    # ---------- Improved decision logic ----------
    def _choose_bet_side_weighted(self, sequence: List[str]) -> Tuple[str, str]:
        """Weighted moving average of last up to 20 decisive results."""
        # Filter only Blue/Red
        decisive = [res for res in sequence if res in ("Blue", "Red")]
        if not decisive:
            return random.choice(("PLR", "BNR")), "No decisive history, random fallback."
        # Weighted sum: more weight to recent results
        total_weight = 0.0
        weighted_sum = 0.0
        for i, res in enumerate(decisive[-20:]):  # max 20 lookback
            # exponential decay weight
            weight = 0.9 ** (len(decisive[-20:]) - i - 1)
            total_weight += weight
            if res == "Blue":
                weighted_sum += weight
            else:  # Red
                weighted_sum -= weight
        avg = weighted_sum / total_weight if total_weight > 0 else 0
        # Threshold 0.2 to avoid noise
        if avg > 0.2:
            return "PLR", f"Weighted trend favors Blue ({avg:.2f})"
        elif avg < -0.2:
            return "BNR", f"Weighted trend favors Red ({avg:.2f})"
        else:
            # Neutral – fallback to last decisive result
            last = decisive[-1]
            side = "PLR" if last == "Blue" else "BNR"
            return side, f"Weighted neutral ({avg:.2f}), following last result {last}"

    def _handle_tie_adjustment(self, sequence: List[str]) -> Optional[str]:
        """If last result is Green (tie), bet opposite of the result before the tie."""
        if len(sequence) >= 2 and sequence[-1] == "Green":
            prev = sequence[-2]
            if prev in ("Blue", "Red"):
                return "BNR" if prev == "Blue" else "PLR"
        return None

    def _choose_bet_side_by_strategy(self, sequence: List[str]) -> Tuple[str, str]:
        """Choose PLR/BNR using the configured side-selection strategy."""
        strategy = self.side_selection_strategy
        history = [value for value in sequence if value in ("Blue", "Red")]
        if not history:
            side = random.choice(("PLR", "BNR"))
            return side, f"{strategy}: no decisive history, random fallback {side}."

        last = history[-1]
        if strategy == "follow_streak":
            side = "PLR" if last == "Blue" else "BNR"
            return side, f"follow_streak: following latest decisive {last}."

        if strategy == "opposite_streak":
            side = "BNR" if last == "Blue" else "PLR"
            return side, f"opposite_streak: betting opposite latest decisive {last}."

        if strategy == "majority":
            recent = history[-6:]
            blue = sum(1 for value in recent if value == "Blue")
            red = len(recent) - blue
            if blue > red:
                return "PLR", f"majority: last {len(recent)} favors Blue {blue}-{red}."
            if red > blue:
                return "BNR", f"majority: last {len(recent)} favors Red {red}-{blue}."
            side = random.choice(("PLR", "BNR"))
            return side, f"majority: tied {blue}-{red}, random fallback {side}."

        if strategy == "alternate":
            side = "PLR" if len(history) % 2 == 0 else "BNR"
            return side, f"alternate: decisive count {len(history)} selected {side}."

        if strategy == "randomize":
            side = random.choice(("PLR", "BNR"))
            return side, f"randomize: selected {side}."

        if strategy == "follow_trend":
            recent = history[-5:]
            blue = sum(1 for value in recent if value == "Blue")
            red = len(recent) - blue
            if blue > red:
                return "PLR", f"follow_trend: last {len(recent)} favors Blue {blue}-{red}."
            if red > blue:
                return "BNR", f"follow_trend: last {len(recent)} favors Red {red}-{blue}."
            side = random.choice(("PLR", "BNR"))
            return side, f"follow_trend: tied {blue}-{red}, random fallback {side}."

        if strategy == "pattern_follow":
            if len(history) >= 3:
                last_three = history[-3:]
                if last_three[0] == last_three[2] and last_three[0] != last_three[1]:
                    side = "BNR" if last == "Blue" else "PLR"
                    return side, f"pattern_follow: zigzag {last_three}, betting opposite latest {last}."
            if len(history) >= 2 and history[-1] == history[-2]:
                side = "PLR" if last == "Blue" else "BNR"
                return side, f"pattern_follow: pair {history[-2:]}, following {last}."
            side = "PLR" if last == "Blue" else "BNR"
            return side, f"pattern_follow: following latest decisive {last}."

        if strategy == "weighted":
            return self._choose_bet_side_weighted(sequence)

        side = random.choice(("PLR", "BNR"))
        return side, f"Unknown strategy {strategy}, random fallback {side}."

    def _apply_long_streak_reduction(self, amount: int, sequence: List[str]) -> int:
        """If last 4 decisive results are same, reduce bet (streak cautious)."""
        decisive = [res for res in sequence if res in ("Blue", "Red")]
        if len(decisive) >= 4 and len(set(decisive[-4:])) == 1:
            reduced = self._normalize_bet_amount(int(amount * 0.7))
            self._log_analysis(f"Long streak of {decisive[-1]}, reducing bet from {amount} to {reduced}")
            return reduced
        return amount

    def _normalize_bet_amount(self, amount: int) -> int:
        """Snap a bet amount to a value that can be made with configured chips."""
        smallest_chip = min(value for _label, value in CHIP_VALUES)
        if amount <= smallest_chip:
            return smallest_chip
        normalized = (amount // smallest_chip) * smallest_chip
        return max(smallest_chip, normalized)

    def _check_trailing_stop(self) -> bool:
        """Return True if trailing stop triggered."""
        if self.peak_profit_total == 0:
            return False
        drawdown_pct = (self.peak_profit_total - self.profit_total) / abs(self.peak_profit_total) * 100
        if drawdown_pct >= self.trailing_stop_pct:
            self.emergency_stop()
            self._set_status(f"Trailing stop triggered: drawdown {drawdown_pct:.1f}% from peak {self.peak_profit_total}")
            return True
        return False

    def _check_profit_target(self) -> bool:
        if self.profit_target and self.profit_total >= self.profit_target:
            self.emergency_stop()
            self._set_status(f"Profit target {self.profit_target} reached. Stopping.")
            return True
        return False

    def recalibrate_point(self):
        """Allow user to recalibrate a single point without redoing all."""
        if not self.capture_overlay:
            point_list = sorted(self.point_labels)
            dialog = tk.Toplevel(self.root)
            dialog.title("Select Point to Recalibrate")
            dialog.attributes("-topmost", True)
            dialog.transient(self.root)
            ttk.Label(dialog, text="Choose point:").pack(pady=5)
            listbox = tk.Listbox(dialog, height=15)
            for p in point_list:
                listbox.insert(tk.END, p)
            listbox.pack(padx=10, pady=5)

            def on_select():
                sel = listbox.curselection()
                if sel:
                    label = point_list[sel[0]]
                    dialog.destroy()
                    self._recalibrate_single_point(label)
                else:
                    messagebox.showwarning("Select", "Please select a point.")
            ttk.Button(dialog, text="Recalibrate", command=on_select).pack(pady=5)
        else:
            messagebox.showinfo("Busy", "Please finish current calibration first.")

    def _recalibrate_single_point(self, label: str):
        self.stop_monitor()
        self.stop_auto_bet(reset_status=False)
        self.stop_auto_sim(reset_status=False)
        self._close_overlay()
        self.root.iconify()
        self.root.update_idletasks()
        time.sleep(0.15)
        frozen_screen = ImageGrab.grab().convert("RGB")
        self.capture_overlay = tk.Toplevel(self.root)
        self.capture_overlay.attributes("-fullscreen", True)
        self.capture_overlay.attributes("-topmost", True)
        self.capture_overlay.configure(bg="#000000")
        # Set crosshair cursor on the overlay window
        self.capture_overlay.configure(cursor="cross")
        self.capture_canvas = tk.Canvas(
            self.capture_overlay,
            bg="#000000",
            highlightthickness=0,
            bd=0,
            cursor="cross",          # crosshair cursor
        )
        self.capture_canvas.pack(fill="both", expand=True)
        self.capture_background_photo = ImageTk.PhotoImage(frozen_screen)
        self.capture_canvas.create_image(0, 0, image=self.capture_background_photo, anchor="nw")
        self.capture_canvas.bind("<Button-1>", lambda e: self._single_capture_click(e, label))
        self.capture_overlay.bind("<Escape>", lambda _event: self._cancel_calibration())
        panel = tk.Frame(self.capture_overlay, bg="#111827", padx=18, pady=12)
        panel.place(relx=0.5, rely=0.04, anchor="n")
        tk.Label(
            panel,
            text=f"Click on point: {label}",
            fg="white",
            bg="#111827",
            font=("Arial", 16, "bold"),
        ).pack()
        tk.Label(
            panel,
            text="Press Esc to cancel",
            fg="#d1d5db",
            bg="#111827",
            font=("Arial", 10),
        ).pack(pady=(4, 0))
        self._set_status(f"Recalibrating point {label}...")

    def _single_capture_click(self, event, label: str):
        self.points[label] = CalibratedPoint(label=label, x=int(event.x_root), y=int(event.y_root))
        self._log_audit("point_recalibrated", label=label, x=int(event.x_root), y=int(event.y_root))
        self._close_overlay()
        self.root.deiconify()
        self.root.lift()
        self._refresh_progress()
        self._set_status(f"Recalibrated {label} at ({event.x_root}, {event.y_root})")
        self._save_config()

    # ---------- Calibration (unchanged) ----------
    def start_calibration(self):
        self.stop_monitor()
        self.stop_auto_bet(reset_status=False)
        self.stop_auto_sim(reset_status=False)
        self._log_audit("calibration_started", total_points=len(self.point_labels))
        self.capture_index = 0
        self._close_overlay()
        self.root.iconify()
        self.root.update_idletasks()
        time.sleep(0.15)
        frozen_screen = ImageGrab.grab().convert("RGB")
        self.capture_overlay = tk.Toplevel(self.root)
        self.capture_overlay.attributes("-fullscreen", True)
        self.capture_overlay.attributes("-topmost", True)
        self.capture_overlay.configure(bg="#000000")
        # Set crosshair cursor on the overlay window
        self.capture_overlay.configure(cursor="cross")
        self.capture_canvas = tk.Canvas(
            self.capture_overlay,
            bg="#000000",
            highlightthickness=0,
            bd=0,
            cursor="cross",          # crosshair cursor
        )
        self.capture_canvas.pack(fill="both", expand=True)
        self.capture_background_photo = ImageTk.PhotoImage(frozen_screen)
        self.capture_canvas.create_image(0, 0, image=self.capture_background_photo, anchor="nw")
        self.capture_canvas.bind("<Button-1>", self._capture_click)
        self.capture_overlay.bind("<Escape>", lambda _event: self._cancel_calibration())
        panel = tk.Frame(self.capture_overlay, bg="#111827", padx=18, pady=12)
        panel.place(relx=0.5, rely=0.04, anchor="n")
        tk.Label(
            panel,
            textvariable=self.capture_instruction_var,
            fg="white",
            bg="#111827",
            font=("Arial", 16, "bold"),
        ).pack()
        tk.Label(
            panel,
            text="Press Esc to cancel calibration",
            fg="#d1d5db",
            bg="#111827",
            font=("Arial", 10),
        ).pack(pady=(4, 0))
        self._update_capture_overlay()
        self._set_status("Calibration started. Click each point in order.")

    def _update_capture_overlay(self):
        if not self.capture_overlay:
            return
        if self.capture_index >= len(self.point_labels):
            self._close_overlay()
            self._refresh_progress()
            self._set_status("Calibration complete.")
            self._log_audit("calibration_completed", total_points=len(self.points))
            return

        current_label = self.point_labels[self.capture_index]
        self.capture_instruction_var.set(
            f"Click point {self.capture_index + 1}/{len(self.point_labels)}: {current_label}"
        )

    def _capture_click(self, event):
        if self.capture_index >= len(self.point_labels):
            return
        label = self.point_labels[self.capture_index]
        self.points[label] = CalibratedPoint(label=label, x=int(event.x_root), y=int(event.y_root))
        self._log_audit(
            "calibration_point_captured",
            label=label,
            index=self.capture_index + 1,
            total=len(self.point_labels),
            x=int(event.x_root),
            y=int(event.y_root),
        )
        if self.capture_canvas:
            self.capture_canvas.create_oval(
                event.x - 5,
                event.y - 5,
                event.x + 5,
                event.y + 5,
                fill="red",
                outline="white",
            )
            self.capture_canvas.create_text(
                event.x + 10,
                event.y - 10,
                text=str(self.capture_index + 1),
                fill="white",
                anchor="w",
                font=("Arial", 10, "bold"),
            )
        self.capture_index += 1
        self._update_capture_overlay()

    def _cancel_calibration(self):
        self._log_audit("calibration_cancelled", captured_points=self.capture_index)
        self._close_overlay()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._set_status("Calibration cancelled.")

    def _close_overlay(self):
        if self.capture_overlay:
            try:
                self.capture_overlay.destroy()
            except Exception:
                pass
            self.capture_overlay = None
            self.capture_canvas = None
            self.capture_background_photo = None
        if self.capture_index >= len(self.point_labels) or not self.capture_overlay:
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
            except Exception:
                pass

    # ---------- Config Save/Load ----------
    def _save_config(self):
        data = {
            "sample_radius": int(self.sample_radius_var.get()),
            "match_threshold": float(self.match_threshold_var.get()),
            "refresh_ms": int(self.refresh_ms_var.get()),
            "points": {label: point.to_dict() for label, point in self.points.items()},
            "updated": time.time(),
        }
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        self._set_status(f"Saved calibration to {CONFIG_PATH.name}")
        self._log_audit(
            "calibration_saved",
            path=str(CONFIG_PATH),
            points=len(self.points),
            sample_radius=data["sample_radius"],
            match_threshold=data["match_threshold"],
            refresh_ms=data["refresh_ms"],
        )

    def _load_config(self):
        if not CONFIG_PATH.exists():
            self._set_status("No saved calibration file found yet.")
            return
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.sample_radius_var.set(int(data.get("sample_radius", self.sample_radius_var.get())))
        self.match_threshold_var.set(float(data.get("match_threshold", self.match_threshold_var.get())))
        self.refresh_ms_var.set(int(data.get("refresh_ms", self.refresh_ms_var.get())))
        self.points.clear()
        for label, point_data in data.get("points", {}).items():
            self.points[label] = CalibratedPoint(
                label=label,
                x=int(point_data["x"]),
                y=int(point_data["y"]),
                rgb_sample=tuple(point_data["rgb_sample"]) if point_data.get("rgb_sample") else None,
            )
        self._refresh_progress()
        self._set_status(f"Loaded calibration from {CONFIG_PATH.name}")
        self._log_audit("calibration_loaded", path=str(CONFIG_PATH), points=len(self.points))

    # ---------- Utility Methods ----------
    def _build_progress_text(self) -> str:
        return f"Calibrated points: {len(self.points)}/{len(self.point_labels)}"

    def _ensure_log_file(self):
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists():
            return
        with self.log_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(CSV_HEADERS)

    def _append_terminal_log(self, message: str):
        append_terminal_log_line(message)

    def _log_analysis(self, message: str):
        append_terminal_log_line(f"ANALYSIS {message}")

    def _log_audit(self, action: str, **fields):
        payload = {"action": action, **fields}
        append_terminal_log_line(f"AUDIT {json.dumps(payload, sort_keys=True, default=str)}")

    def _set_status(self, message: str):
        self.status_var.set(message)
        warning_text = "Monitoring only until board reset"
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        warning_start = message.find(warning_text)
        if warning_start == -1:
            self.status_text.insert("1.0", message)
        else:
            warning_end = warning_start + len(warning_text)
            self.status_text.insert("1.0", message[:warning_start])
            self.status_text.insert("end", message[warning_start:warning_end], "reset_warning")
            self.status_text.insert("end", message[warning_end:])
        self.status_text.configure(state="disabled")
        if message != getattr(self, "_last_logged_status_message", None):
            self._append_terminal_log(message)
            self._last_logged_status_message = message

    def _append_record(
        self,
        event: str,
        *,
        round_box: str = "",
        progression_step: Optional[int] = None,
        bet_side: str = "",
        bet_amount: int = 0,
        result: str = "",
        note: str = "",
    ):
        self._ensure_log_file()
        try:
            with self.log_path.open("a", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(
                    [
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        self.record_counter,
                        round_box,
                        result,
                        bet_side,
                        bet_amount,
                        event,
                        self.profit_total,
                        "" if progression_step is None else progression_step,
                        self.resolved_bet_count,
                        f"{self._hit_rate():.2f}",
                        note,
                    ]
                )
        except PermissionError as exc:
            message = f"CSV write skipped: {self.log_path.name} is locked or not writable ({exc})."
            self._append_terminal_log(message)
            self._set_status(message)
            self._log_audit(
                "csv_write_failed",
                event=event,
                round_box=round_box,
                result=result,
                bet_side=bet_side,
                bet_amount=bet_amount,
                progression_step=progression_step,
                note=note,
                error=str(exc),
            )
            return
        self._log_audit(
            "csv_record",
            counter=self.record_counter,
            event=event,
            round_box=round_box,
            result=result,
            bet_side=bet_side,
            bet_amount=bet_amount,
            profit_total=self.profit_total,
            progression_step=progression_step,
            resolved_bets=self.resolved_bet_count,
            hit_rate=f"{self._hit_rate():.2f}",
            note=note,
        )
        self.record_counter += 1

    def _result_box_label(self, sequence_index: int) -> str:
        if sequence_index < 0 or sequence_index >= GRID_ROWS * GRID_COLS:
            return ""
        return grid_label(sequence_index)

    def _brief_analysis_note(self, sequence: List[str], side: str) -> str:
        non_tie_results = [value for value in sequence if value in ("Blue", "Red")]
        if len(non_tie_results) < 2:
            return f"rnd->{side.lower()}"

        latest = non_tie_results[-1]
        previous = non_tie_results[-2]
        if latest == previous:
            return f"streak {latest.lower()}->{side.lower()}"

        recent_window = non_tie_results[-4:]
        blue_count = sum(1 for value in recent_window if value == "Blue")
        red_count = sum(1 for value in recent_window if value == "Red")
        if blue_count == red_count:
            return f"tie {latest.lower()}->{side.lower()}"
        lead_color = "blue" if blue_count > red_count else "red"
        return f"{lead_color} {blue_count}-{red_count}->{side.lower()}"

    def _hit_rate(self) -> float:
        decisive_total = self.win_count + self.loss_count
        if decisive_total <= 0:
            return 0.0
        return (self.win_count / decisive_total) * 100.0

    def _current_drawdown(self) -> int:
        return self.peak_profit_total - self.profit_total

    def _refresh_risk_metrics(self):
        if self.profit_total > self.peak_profit_total:
            self.peak_profit_total = self.profit_total
        self.max_drawdown = max(self.max_drawdown, self._current_drawdown())

    def _active_auto_mode_label(self) -> str:
        return "AutoSim" if self.auto_sim_betting else "Auto"

    def _has_pending_bet(self) -> bool:
        return bool(self.pending_bet_side)

    def _guard_auto_mode_switch(self, target_label: str) -> bool:
        current_label = None
        if self.auto_betting and target_label != "Auto":
            current_label = "Auto"
        elif self.auto_sim_betting and target_label != "AutoSim":
            current_label = "AutoSim"
        if not current_label or not self._has_pending_bet():
            return True

        warning_text = (
            f"Cannot switch to {target_label} yet because {current_label} still has an unresolved pending bet.\n\n"
            "Wait for the current round to settle first."
        )
        self._set_status(f"{current_label} has a pending bet. Resolve it before switching to {target_label}.")
        messagebox.showwarning("Pending Bet", warning_text)
        return False

    def _update_stats_display(self):
        current_amount = self._get_current_bet_amount()
        current_step = self._get_current_progression_step_value()
        prog_display = f"Prog: {self.progression_type}"
        if self.progression_type == "Default":
            prog_display += f" | Step: {current_step} | Amount: {current_amount}"
        elif self.progression_type == "Fibonacci":
            prog_display += f" | Step: {current_step} | Amount: {current_amount}"
        elif self.progression_type == "D'Alembert":
            prog_display += f" | Step: {current_step} | Amount: {current_amount}"
        self.progression_var.set(prog_display)

        if self.auto_betting:
            self.auto_state_var.set("Mode: Auto ON")
        elif self.auto_sim_betting:
            self.auto_state_var.set("Mode: AutoSim ON")
        else:
            self.auto_state_var.set("Mode: OFF")
        self.rounds_var.set(f"Rounds: {self.total_rounds}")
        self.win_loss_var.set(f"Wins: {self.win_count} | Losses: {self.loss_count}")
        self.tie_var.set(f"Ties: {self.tie_count}")
        self.profit_var.set(f"Profit: {self.profit_total}")
        self.analysis_var.set(
            f"Hit Rate: {self._hit_rate():.1f}% | Resolved: {self.resolved_bet_count} | Skips: {self.skip_count}"
        )
        self.risk_var.set(
            f"Peak: {self.peak_profit_total} | Drawdown: {self._current_drawdown()} | Max DD: {self.max_drawdown} | Max LStreak: {self.max_loss_streak}"
        )
        if self.pending_bet_side:
            self.pending_var.set(
                f"Pending Bet: {self.pending_bet_side} / {self.pending_bet_amount} | wait {max(0, int(self.pending_bet_ready_at - time.time()))}s"
            )
        else:
            self.pending_var.set("Pending Bet: None")
        self.last_result_var.set(f"Last Result: {self.last_result_value}")
        if self.last_bet_side:
            self.last_bet_var.set(f"Last Bet: {self.last_bet_side} / {self.last_bet_amount}")
        else:
            self.last_bet_var.set("Last Bet: None")

    def _refresh_progress(self):
        self.progress_var.set(self._build_progress_text())
        self._update_stats_display()
        for label in EXTRA_LABELS:
            widget = self.named_value_labels.get(label)
            if not widget:
                continue
            point = self.points.get(label)
            widget.configure(
                text=f"{point.x}, {point.y}" if point else "Not set",
                bg="#e5e7eb",
                fg="#374151",
            )

    def clear_points(self):
        self.stop_monitor()
        self.stop_auto_bet(reset_status=False)
        self.stop_auto_sim(reset_status=False)
        self.points.clear()
        self._refresh_progress()
        self._reset_board_display()
        self.cd_var.set("CD Area: N/A")
        self._set_status("Cleared all calibration points.")

    # ---------- Color Classification ----------
    def _require_points(self) -> bool:
        missing = [label for label in self.point_labels if label not in self.points]
        if missing:
            self._set_status(f"Missing calibration points: {', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}")
            return False
        return True

    def _sample_rgb(self, screenshot, point: CalibratedPoint) -> Tuple[int, int, int]:
        radius = max(0, int(self.sample_radius_var.get()))
        left = max(0, point.x - radius)
        top = max(0, point.y - radius)
        right = min(screenshot.width - 1, point.x + radius)
        bottom = min(screenshot.height - 1, point.y + radius)

        total_r = total_g = total_b = 0
        count = 0
        for px in range(left, right + 1):
            for py in range(top, bottom + 1):
                r, g, b = screenshot.getpixel((px, py))
                total_r += r
                total_g += g
                total_b += b
                count += 1
        if count <= 0:
            return screenshot.getpixel((point.x, point.y))
        return (total_r // count, total_g // count, total_b // count)

    @staticmethod
    def _normalize_color(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
        total = max(sum(rgb), 1)
        return tuple(channel / total for channel in rgb)

    def _color_score(self, rgb: Tuple[int, int, int], reference: Tuple[int, int, int]) -> float:
        raw = sum((rgb[i] - reference[i]) ** 2 for i in range(3))
        nrgb = self._normalize_color(rgb)
        nref = self._normalize_color(reference)
        balance = sum((nrgb[i] - nref[i]) ** 2 for i in range(3)) * 100000
        return raw + balance

    def _dominant_channel_index(self, rgb: Tuple[int, int, int]) -> int:
        return max(range(3), key=lambda index: rgb[index])

    def _soft_color_score(self, rgb: Tuple[int, int, int], reference: Tuple[int, int, int]) -> float:
        nrgb = self._normalize_color(rgb)
        nref = self._normalize_color(reference)
        balance = sum((nrgb[i] - nref[i]) ** 2 for i in range(3)) * 100000
        dominance_penalty = 0.0
        if self._dominant_channel_index(rgb) != self._dominant_channel_index(reference):
            dominance_penalty = 50000.0
        return balance + dominance_penalty

    def _classify_soft_mixed_rgb(
        self,
        rgb: Tuple[int, int, int],
        refs: Dict[str, Tuple[int, int, int]],
    ) -> Tuple[str, float]:
        brightness = sum(rgb) / 3.0
        channel_spread = max(rgb) - min(rgb)
        soft_min = self.settings.get("soft_match_brightness_min", 60)
        soft_spread = self.settings.get("soft_match_channel_spread_min", 7)
        soft_max = self.settings.get("soft_match_hue_score_max", 42000.0)
        if brightness < soft_min or channel_spread < soft_spread:
            return "Blank", float("inf")

        best_name = "Blank"
        best_score = float("inf")
        for color_name in COLOR_NAMES:
            score = self._soft_color_score(rgb, refs[color_name])
            if score < best_score:
                best_score = score
                best_name = color_name

        if best_score <= soft_max:
            return best_name, best_score
        return "Blank", best_score

    def _classify_rgb(self, rgb: Tuple[int, int, int], refs: Dict[str, Tuple[int, int, int]]) -> Tuple[str, float]:
        best_name = "Blank"
        best_score = float("inf")
        for color_name in COLOR_NAMES:
            score = self._color_score(rgb, refs[color_name])
            if score < best_score:
                best_score = score
                best_name = color_name
        if best_score > float(self.match_threshold_var.get()):
            soft_name, soft_score = self._classify_soft_mixed_rgb(rgb, refs)
            if soft_name != "Blank":
                return soft_name, soft_score
            return "Blank", best_score
        return best_name, best_score

    def _class_color(self, value: str) -> Tuple[str, str]:
        if value == "Blue":
            return "#dbeafe", "#1d4ed8"
        if value == "Green":
            return "#dcfce7", "#15803d"
        if value == "Red":
            return "#fee2e2", "#b91c1c"
        return "#e5e7eb", "#374151"

    def _set_named_status(self, label: str, text: str, value: str = "Blank"):
        widget = self.named_value_labels.get(label)
        if not widget:
            return
        bg, fg = self._class_color(value)
        widget.configure(text=text, bg=bg, fg=fg)

    def _set_point_rgb_sample(self, label: str, rgb: Tuple[int, int, int]):
        point = self.points.get(label)
        if not point:
            return
        point.rgb_sample = tuple(int(channel) for channel in rgb)

    def _validate_board_values(self, board_values: List[str], sequence_len: int) -> Tuple[bool, str]:
        non_blank_values = [value for value in board_values if value != "Blank"]
        if not non_blank_values:
            return False, ""

        if len(non_blank_values) == len(board_values) and len(set(non_blank_values)) == 1:
            return True, f"Invalid board: all {len(board_values)} boxes detected as {non_blank_values[0]}."

        blank_seen = False
        irregular_positions: List[str] = []
        for index, value in enumerate(board_values):
            if value == "Blank":
                blank_seen = True
                continue
            if blank_seen:
                irregular_positions.append(grid_label(index))

        if irregular_positions:
            preview = ", ".join(irregular_positions[:8])
            suffix = "..." if len(irregular_positions) > 8 else ""
            return True, f"Invalid board: irregular detections after blanks at {preview}{suffix}."

        previous_len = self.last_valid_sequence_len
        if previous_len is not None and sequence_len < previous_len:
            previous_label = grid_label(previous_len - 1) if previous_len > 0 else "empty board"
            latest_label = grid_label(sequence_len - 1) if sequence_len > 0 else "empty board"
            return (
                True,
                "Invalid board: sequence moved backward from "
                f"{previous_label} to {latest_label}; waiting for stable board or full blank reset.",
            )
        if previous_len is not None and sequence_len > previous_len + 1:
            previous_label = grid_label(previous_len - 1) if previous_len > 0 else "empty board"
            expected_label = grid_label(previous_len)
            latest_label = grid_label(sequence_len - 1)
            return (
                True,
                "Invalid board: sequence jumped from "
                f"{previous_label} to {latest_label}; expected next value at {expected_label}.",
            )

        return False, ""

    def _reset_board_display(self):
        for label, widget in self.grid_value_labels.items():
            bg, fg = self._class_color("Blank")
            widget.configure(text=label, bg=bg, fg=fg)
        for label in EXTRA_LABELS:
            point = self.points.get(label)
            text = f"{point.x}, {point.y}" if point else "Not set"
            self._set_named_status(label, text, "Blank")

    def _capture_snapshot(self) -> Optional[ScanSnapshot]:
        if not self._require_points():
            return None
        try:
            screenshot = ImageGrab.grab().convert("RGB")
        except Exception as exc:
            self._set_status(f"Capture failed: {exc}")
            return None

        try:
            refs = {
                "Blue": self._sample_rgb(screenshot, self.points["Sample Blue"]),
                "Green": self._sample_rgb(screenshot, self.points["Sample Green"]),
                "Red": self._sample_rgb(screenshot, self.points["Sample Red"]),
            }
            self._set_point_rgb_sample("Sample Blue", refs["Blue"])
            self._set_point_rgb_sample("Sample Green", refs["Green"])
            self._set_point_rgb_sample("Sample Red", refs["Red"])

            board_values: List[str] = []
            counts = {"Blue": 0, "Green": 0, "Red": 0, "Blank": 0}
            for index in range(GRID_ROWS * GRID_COLS):
                label = grid_label(index)
                rgb = self._sample_rgb(screenshot, self.points[label])
                value, _score = self._classify_rgb(rgb, refs)
                board_values.append(value)
                counts[value] += 1

            sequence: List[str] = []
            for value in board_values:
                if value == "Blank":
                    break
                sequence.append(value)

            invalid, invalid_reason = self._validate_board_values(board_values, len(sequence))

            cd_rgb = self._sample_rgb(screenshot, self.points["CD"])
            cd_value, cd_score = self._classify_rgb(cd_rgb, refs)
            return ScanSnapshot(
                refs=refs,
                board_values=board_values,
                counts=counts,
                sequence=sequence,
                latest_result=sequence[-1] if sequence else None,
                cd_value=cd_value,
                cd_score=cd_score,
                all_blank=all(value == "Blank" for value in board_values),
                invalid=invalid,
                invalid_reason=invalid_reason,
            )
        finally:
            screenshot.close()

    def _render_snapshot(self, snapshot: ScanSnapshot):
        for index, value in enumerate(snapshot.board_values):
            label = grid_label(index)
            bg, fg = self._class_color(value)
            self.grid_value_labels[label].configure(
                text=f"{label}\n{value}",
                bg=bg,
                fg=fg,
            )

        self._set_named_status(
            "CD",
            f"{self.points['CD'].x}, {self.points['CD'].y} | {snapshot.cd_value}",
            snapshot.cd_value,
        )
        if snapshot.cd_value == "Green":
            self.cd_var.set(f"CD Area: GREEN DETECTED (score {snapshot.cd_score:.0f})")
        else:
            self.cd_var.set(f"CD Area: {snapshot.cd_value} (score {snapshot.cd_score:.0f})")
        if snapshot.all_blank:
            self.last_sequence_len = 0
            self.last_valid_sequence_len = 0
            self.last_result_value = "None"
        elif not snapshot.invalid:
            self.last_sequence_len = len(snapshot.sequence)
            self.last_valid_sequence_len = len(snapshot.sequence)
            self.last_result_value = snapshot.latest_result or "None"
        self._update_stats_display()

    # ---------- Betting ----------
    def _build_chip_plan(self, amount: int) -> List[Tuple[str, int]]:
        remaining = self._normalize_bet_amount(amount)
        plan: List[Tuple[str, int]] = []
        for label, value in CHIP_VALUES:
            count, remaining = divmod(remaining, value)
            if count:
                plan.append((label, count))
        if remaining != 0:
            raise ValueError(f"Cannot express amount {amount} with available chips.")
        return plan

    def _click_point(self, label: str):
        point = self.points.get(label)
        if not point:
            raise ValueError(f"Missing calibration for {label}")
        pyautogui.click(point.x, point.y)

    def _build_bet_click_sequence(self, side: str, amount: int) -> List[str]:
        plan = self._build_chip_plan(amount)
        sequence: List[str] = []
        for chip_label, count in plan:
            sequence.append(chip_label)
            for _ in range(count):
                sequence.append(side)
        return sequence

    def _place_bet(self, side: str, amount: int):
        """Place bet using staggered clicks via after() to avoid blocking UI."""
        labels = self._build_bet_click_sequence(side, amount)
        self._log_audit("place_bet_click_sequence", mode="Auto", side=side, amount=amount, labels=labels)
        self._staggered_clicks(labels)

    def _staggered_clicks(self, labels: List[str], index: int = 0):
        """Perform clicks with delay between each."""
        if index >= len(labels):
            self.click_after_ids = []
            return
        try:
            self._click_point(labels[index])
        except Exception as e:
            self._set_status(f"Click error: {e}")
            return
        after_id = self.root.after(120, lambda: self._staggered_clicks(labels, index + 1))
        self.click_after_ids.append(str(after_id))

    def _show_click_sequence_markers(self, labels: List[str]):
        self._log_audit("place_bet_click_sequence", mode="AutoSim", labels=labels)
        marker_windows = []

        def show_marker(index: int, label: str):
            point = self.points.get(label)
            if not point:
                return
            marker = tk.Toplevel(self.root)
            marker.overrideredirect(True)
            marker.attributes("-topmost", True)
            marker.configure(bg="#ff00ff")
            try:
                marker.wm_attributes("-transparentcolor", "#ff00ff")
            except tk.TclError:
                pass
            marker.geometry(f"36x36+{max(0, point.x - 18)}+{max(0, point.y - 18)}")
            tk.Label(
                marker,
                text=str(index + 1),
                font=("Arial", 18, "bold"),
                fg="#ff0000",
                bg="#ff00ff",
            ).pack(fill="both", expand=True)
            marker_windows.append(marker)

        for index, label in enumerate(labels):
            self.root.after(index * 250, lambda idx=index, item=label: show_marker(idx, item))

        total_delay = max(0, len(labels) - 1) * 250 + 12000

        def destroy_markers():
            for marker in marker_windows:
                try:
                    marker.destroy()
                except Exception:
                    pass

        self.root.after(total_delay, destroy_markers)

    def _resolve_bet(self, result_value: str):
        expected = {"PLR": "Blue", "BNR": "Red"}.get(self.pending_bet_side or "", "")
        before_profit_total = self.profit_total
        before_progression_step = self._get_current_progression_step_value()
        self.total_rounds += 1
        self.resolved_bet_count += 1
        self.last_result_value = result_value
        profit_change = 0
        if result_value == "Green":
            self.tie_count += 1
            self.current_loss_streak = 0
            result_text = "TIE"
            # Progression unchanged (won = None handled later)
            won = None
        elif result_value == expected:
            self.win_count += 1
            profit_change = self.pending_bet_amount
            self.profit_total += profit_change
            self.current_loss_streak = 0
            result_text = "WIN"
            won = True
        else:
            self.loss_count += 1
            profit_change = -self.pending_bet_amount
            self.profit_total += profit_change
            self.current_loss_streak += 1
            self.max_loss_streak = max(self.max_loss_streak, self.current_loss_streak)
            result_text = "LOSE"
            won = False

        # Update progression based on outcome (ignore ties)
        if result_value != "Green":
            self._update_progression_on_result(won)

        # Stop loss checks
        if self.stop_loss is not None and self.profit_total <= self.stop_loss:
            self._set_status(f"FIXED STOP LOSS reached ({self.profit_total} <= {self.stop_loss}). Halting auto modes.")
            self.emergency_stop()
        if self._check_trailing_stop():
            return
        if self._check_profit_target():
            return

        # FIX: Update last_logged_sequence_len after resolving a bet (prevents duplicate processing)
        self.last_logged_sequence_len = self.pending_bet_basis_len + 1

        self._refresh_risk_metrics()
        self._log_audit(
            "bet_resolved",
            event=result_text.lower(),
            bet_side=self.pending_bet_side,
            bet_amount=self.pending_bet_amount,
            expected=expected,
            result=result_value,
            profit_before=before_profit_total,
            profit_after=self.profit_total,
            progression_before=before_progression_step,
            progression_after=self._get_current_progression_step_value(),
            win_count=self.win_count,
            loss_count=self.loss_count,
            tie_count=self.tie_count,
            resolved_bets=self.resolved_bet_count,
        )
        basis_len = self.pending_bet_basis_len
        if basis_len < 0 or basis_len >= GRID_ROWS * GRID_COLS:
            round_box_str = ""
        else:
            round_box_str = self._result_box_label(basis_len)
        self._append_record(
            result_text.lower(),
            round_box=round_box_str,
            progression_step=self._get_current_progression_step_value(),
            bet_side=self.pending_bet_side or "",
            bet_amount=self.pending_bet_amount,
            result=result_value,
            note=self.pending_bet_note,
        )
        self._set_status(
            f"Resolved {result_text}: {self.pending_bet_side} vs {result_value} | Profit {self.profit_total}"
        )
        self.pending_bet_side = None
        self.pending_bet_amount = 0
        self.pending_bet_basis_len = -1
        self.pending_bet_note = ""
        self.pending_bet_ready_at = 0.0
        self.cooldown_skip_active = False  # reset cooldown after resolution
        self._update_stats_display()

    def _handle_auto_logic(self, snapshot: ScanSnapshot):
        now = time.time()
        mode_label = self._active_auto_mode_label()
        if snapshot.invalid:
            self._set_status(snapshot.invalid_reason)
            self._update_stats_display()
            return

        if snapshot.all_blank:
            self.record_counter = 1
            self.last_logged_sequence_len = -1
            self.current_loss_streak = 0
            self.pending_bet_side = None
            self.pending_bet_amount = 0
            self.pending_bet_basis_len = -1
            self.pending_bet_note = ""
            self.pending_bet_ready_at = 0.0
            self.last_bet_basis_len = -1
            self.bet_waiting_for_reset = False
            self.cooldown_skip_active = False
            self._set_status("Board reset detected: all cells blank.")
            self._update_stats_display()
            return

        if self.pending_bet_side:
            if now < self.pending_bet_ready_at:
                self._update_stats_display()
                return
            if len(snapshot.sequence) > self.pending_bet_basis_len:
                # FIX: ensure result is taken from correct index (basis_len)
                result_at_index = snapshot.sequence[self.pending_bet_basis_len]
                self._resolve_bet(result_at_index)
            else:
                self._set_status(f"{mode_label}: result index out of range, cannot resolve.")
            return

        current_basis_len = len(snapshot.sequence)
        has_new_result = current_basis_len > self.last_logged_sequence_len and current_basis_len > 0
        if has_new_result:
            self.last_logged_sequence_len = current_basis_len
        if self.bet_waiting_for_reset or current_basis_len >= LAST_BET_SEQUENCE_LEN:
            if has_new_result:
                self.skip_count += 1
                self._append_record(
                    "skip",
                    round_box=self._result_box_label(current_basis_len - 1),
                    progression_step=self._get_current_progression_step_value(),
                    result=snapshot.sequence[-1],
                    note=f"cooldown wait_reset cd={snapshot.cd_value.lower()}",
                )
            if not self.bet_waiting_for_reset:
                self.bet_waiting_for_reset = True
                self._log_analysis(
                    f"Bet cutoff reached at {LAST_BET_BOX_LABEL}. Monitoring and recording only until board reset."
                )
            self._set_status(
                f"Bet cutoff reached at {LAST_BET_BOX_LABEL}. Monitoring only until board reset."
            )
            return
        if snapshot.cd_value != "Green":
            if has_new_result:
                self.skip_count += 1
                self._log_analysis(
                    f"Skipped round at {self._result_box_label(current_basis_len - 1)} because CD was "
                    f"{snapshot.cd_value}, not Green. Sequence length={current_basis_len}."
                )
                self._append_record(
                    "skip",
                    round_box=self._result_box_label(current_basis_len - 1),
                    progression_step=self._get_current_progression_step_value(),
                    result=snapshot.sequence[-1],
                    note=f"cd {snapshot.cd_value.lower()}",
                )
            self._set_status(f"{mode_label} ready: waiting for CD green.")
            return
        if current_basis_len == self.last_bet_basis_len:
            self._set_status(f"{mode_label} ready: waiting for next round slot.")
            return

        # FIX: Reset cooldown flag if loss streak no longer meets threshold
        if self.current_loss_streak < self.loss_streak_cooldown:
            self.cooldown_skip_active = False

        # Loss streak cooldown
        if self.loss_streak_cooldown > 0 and self.current_loss_streak >= self.loss_streak_cooldown and not self.cooldown_skip_active:
            self.cooldown_skip_active = True
            self.skip_count += 1
            self._append_record(
                "skip",
                round_box=self._result_box_label(current_basis_len - 1),
                progression_step=self._get_current_progression_step_value(),
                result=snapshot.sequence[-1],
                note=f"loss streak {self.current_loss_streak} cooldown",
            )
            self._set_status(f"Loss streak {self.current_loss_streak} >= {self.loss_streak_cooldown}, skipping one round.")
            self._update_stats_display()
            return
        self.cooldown_skip_active = False

        # --- Decision with configured side-selection strategy ---
        side, analysis_reason = self._choose_bet_side_by_strategy(snapshot.sequence)

        # 2. Base bet amount from progression
        amount = self._get_current_bet_amount()

        # 3. Apply long streak reduction (cautious)
        amount = self._apply_long_streak_reduction(amount, snapshot.sequence)
        amount = self._normalize_bet_amount(amount)

        # 4. Enforce max bet
        if amount > self.max_bet:
            self._set_status(f"Bet amount {amount} exceeds max bet {self.max_bet}. Halting auto.")
            self.emergency_stop()
            return

        self._log_analysis(
            f"Round basis={self._result_box_label(current_basis_len - 1)} latest={snapshot.sequence[-1]} "
            f"cd_score={snapshot.cd_score:.0f} progression={self.progression_type} amount={amount} decision={side}. "
            f"{analysis_reason}"
        )
        self._log_audit(
            "bet_decision",
            mode=mode_label,
            round_box=self._result_box_label(current_basis_len - 1),
            latest_result=snapshot.sequence[-1],
            sequence_len=current_basis_len,
            cd_value=snapshot.cd_value,
            cd_score=f"{snapshot.cd_score:.0f}",
            progression_type=self.progression_type,
            progression_step=self._get_current_progression_step_value(),
            side_selection_strategy=self.side_selection_strategy,
            decision=side,
            amount=amount,
            reason=analysis_reason,
        )
        click_sequence = self._build_bet_click_sequence(side, amount)
        try:
            if self.auto_sim_betting:
                self._show_click_sequence_markers(click_sequence)
            else:
                self._place_bet(side, amount)
        except Exception as exc:
            self._set_status(f"{mode_label} failed: {exc}")
            return
        self.last_bet_side = side
        self.last_bet_amount = amount
        self.last_bet_progression_index = self._get_current_progression_step_value()
        self.pending_bet_side = side
        self.pending_bet_amount = amount
        self.pending_bet_basis_len = current_basis_len
        self.pending_bet_note = self._brief_analysis_note(snapshot.sequence, side)
        self.pending_bet_ready_at = now + self.auto_idle_seconds
        self.last_bet_basis_len = current_basis_len
        if self.auto_sim_betting:
            self._set_status(
                f"AutoSim marked: {side} / {amount}. Waiting {int(self.auto_idle_seconds)}s for result."
            )
        else:
            self._set_status(
                f"Auto bet placed: {side} / {amount}. Waiting {int(self.auto_idle_seconds)}s for result."
            )
        self._update_stats_display()

    # ---------- Scan and Monitor ----------
    def scan_once(self):
        snapshot = self._capture_snapshot()
        if not snapshot:
            return
        self._render_snapshot(snapshot)
        if snapshot.invalid:
            self._set_status(snapshot.invalid_reason)
            return
        if not self.auto_betting:
            self._set_status(
                "Scan complete. "
                f"Blue={snapshot.counts['Blue']} Green={snapshot.counts['Green']} "
                f"Red={snapshot.counts['Red']} Blank={snapshot.counts['Blank']}"
            )

    def toggle_monitor(self):
        if self.monitoring:
            self.stop_monitor()
        else:
            self.start_monitor()

    def toggle_auto_bet(self):
        if self.auto_betting:
            self.stop_auto_bet()
        else:
            self.start_auto_bet()

    def toggle_auto_sim(self):
        if self.auto_sim_betting:
            self.stop_auto_sim()
        else:
            self.start_auto_sim()

    def start_monitor(self):
        if not self._require_points():
            return
        self.monitoring = True
        self.monitor_btn.configure(text="Stop Monitor")
        self._set_status("Live monitor started.")
        self._log_audit("mode_started", mode="Monitor", refresh_ms=int(self.refresh_ms_var.get()))
        if not self.monitor_after_id:
            self._monitor_tick()

    def stop_monitor(self):
        self.monitoring = False
        self.monitor_btn.configure(text="Start Monitor")
        self._log_audit("mode_stopped", mode="Monitor")
        if self.monitor_after_id and not self.auto_betting and not self.auto_sim_betting:
            try:
                self.root.after_cancel(self.monitor_after_id)
            except Exception:
                pass
            self.monitor_after_id = None

    def start_auto_bet(self):
        if not self._require_points():
            return
        if not self._guard_auto_mode_switch("Auto"):
            return
        if self.auto_sim_betting:
            self.stop_auto_sim(reset_status=False)
        self.auto_betting = True
        self.last_logged_sequence_len = self.last_sequence_len
        self.auto_btn.configure(text="Stop Auto")
        self._set_status("Auto betting started.")
        self._log_audit(
            "mode_started",
            mode="Auto",
            last_sequence_len=self.last_sequence_len,
            progression_type=self.progression_type,
            progression_step=self._get_current_progression_step_value(),
            side_selection_strategy=self.side_selection_strategy,
            amount=self._get_current_bet_amount(),
        )
        self._update_stats_display()
        if not self.monitor_after_id:
            self._monitor_tick()

    def stop_auto_bet(self, reset_status: bool = True):
        self.auto_betting = False
        self.auto_btn.configure(text="Start Auto")
        self.pending_bet_side = None
        self.pending_bet_amount = 0
        self.pending_bet_basis_len = -1
        self.pending_bet_ready_at = 0.0
        self.pending_bet_note = ""
        self.bet_waiting_for_reset = False
        self.cooldown_skip_active = False
        self._update_stats_display()
        self._log_audit("mode_stopped", mode="Auto", reset_status=reset_status)
        if reset_status:
            self._set_status("Auto betting stopped.")
        if self.monitor_after_id and not self.monitoring and not self.auto_sim_betting:
            try:
                self.root.after_cancel(self.monitor_after_id)
            except Exception:
                pass
            self.monitor_after_id = None

    def start_auto_sim(self):
        if not self._require_points():
            return
        if not self._guard_auto_mode_switch("AutoSim"):
            return
        if self.auto_betting:
            self.stop_auto_bet(reset_status=False)
        self.auto_sim_betting = True
        self.last_logged_sequence_len = self.last_sequence_len
        self.autosim_btn.configure(text="Stop AutoSim")
        self._set_status("AutoSim started.")
        self._log_audit(
            "mode_started",
            mode="AutoSim",
            last_sequence_len=self.last_sequence_len,
            progression_type=self.progression_type,
            progression_step=self._get_current_progression_step_value(),
            side_selection_strategy=self.side_selection_strategy,
            amount=self._get_current_bet_amount(),
        )
        self._update_stats_display()
        if not self.monitor_after_id:
            self._monitor_tick()

    def stop_auto_sim(self, reset_status: bool = True):
        self.auto_sim_betting = False
        self.autosim_btn.configure(text="Start AutoSim")
        self.pending_bet_side = None
        self.pending_bet_amount = 0
        self.pending_bet_basis_len = -1
        self.pending_bet_ready_at = 0.0
        self.pending_bet_note = ""
        self.bet_waiting_for_reset = False
        self.cooldown_skip_active = False
        self._update_stats_display()
        self._log_audit("mode_stopped", mode="AutoSim", reset_status=reset_status)
        if reset_status:
            self._set_status("AutoSim stopped.")
        if self.monitor_after_id and not self.monitoring and not self.auto_betting:
            try:
                self.root.after_cancel(self.monitor_after_id)
            except Exception:
                pass
            self.monitor_after_id = None

    def _monitor_tick(self):
        self.monitor_after_id = None
        if not (self.monitoring or self.auto_betting or self.auto_sim_betting):
            return
        snapshot = self._capture_snapshot()
        if snapshot:
            self._render_snapshot(snapshot)
            if snapshot.invalid:
                self._set_status(snapshot.invalid_reason)
            elif self.auto_betting or self.auto_sim_betting:
                self._handle_auto_logic(snapshot)
            elif self.monitoring:
                self._set_status(
                    "Scan complete. "
                    f"Blue={snapshot.counts['Blue']} Green={snapshot.counts['Green']} "
                    f"Red={snapshot.counts['Red']} Blank={snapshot.counts['Blank']}"
                )
        delay = max(150, int(self.refresh_ms_var.get()))
        self.monitor_after_id = self.root.after(delay, self._monitor_tick)

    def _show_calibration_info(self):
        help_text = self._build_calibration_info_text()

        info_window = tk.Toplevel(self.root)
        info_window.title("Calibration Info")
        info_window.attributes("-topmost", True)
        info_window.resizable(True, True)
        info_window.transient(self.root)
        info_window.update_idletasks()
        popup_width = 620
        popup_height = 640
        screen_width = info_window.winfo_screenwidth()
        x_offset = max(0, screen_width - popup_width - 20)
        y_offset = 0
        info_window.geometry(f"{popup_width}x{popup_height}+{x_offset}+{y_offset}")

        frame = ttk.Frame(info_window, padding=12)
        frame.pack(fill="both", expand=True)
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(text_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        text_widget = tk.Text(
            text_frame,
            wrap="word",
            yscrollcommand=scrollbar.set,
            bg="#fbf7ef",
            fg="#2b2118",
            font=("Georgia", 11),
            padx=14,
            pady=12,
            relief="flat",
            bd=0,
        )
        text_widget.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=text_widget.yview)
        text_widget.insert("1.0", help_text)
        text_widget.configure(state="disabled")

        ttk.Button(frame, text="Close", command=info_window.destroy).pack(anchor="e", pady=(10, 0))

    def _build_calibration_info_text(self) -> str:
        progression_text = ", ".join(str(step) for step in self.progression_steps)
        fallback_text = (
            "Calibration info file is missing.\n\n"
            f"Expected file: {INFO_TEMPLATE_PATH.name}\n"
            f"Last window bet box: {LAST_BET_BOX_LABEL}\n"
            f"Progression: {progression_text}\n"
        )
        try:
            template = INFO_TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError:
            return fallback_text
        return template.format(
            auto_idle_seconds=int(self.auto_idle_seconds),
            last_bet_box_label=LAST_BET_BOX_LABEL,
            progression_text=progression_text,
            results_csv_name=RESULTS_CSV_PATH.name,
            terminal_log_name=TERMINAL_LOG_PATH.name,
        )

    def _exit_app(self):
        self.stop_monitor()
        self.stop_auto_bet(reset_status=False)
        self.stop_auto_sim(reset_status=False)
        self.root.destroy()

    def _on_main_frame_configure(self, _event):
        self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

    def _on_main_canvas_configure(self, event):
        self.main_canvas.itemconfigure(self.main_canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        delta = -1 * int(event.delta / 120) if event.delta else 0
        if delta:
            self.main_canvas.yview_scroll(delta, "units")


def main():
    setup_terminal_logging()
    sys.excepthook = log_unhandled_exception
    try:
        import PIL  # noqa: F401
        import pyautogui  # noqa: F401
    except ImportError as exc:
        print(f"Missing dependency: {exc}")
        print("Please install: pip install pillow pyautogui")
        return

    pyautogui.FAILSAFE = True
    root = tk.Tk()
    app = BacartCalibratorApp(root)

    def _report_callback_exception(exc_type, exc_value, exc_traceback):
        log_unhandled_exception(exc_type, exc_value, exc_traceback)

    root.report_callback_exception = _report_callback_exception
    root.protocol("WM_DELETE_WINDOW", lambda: (app.emergency_stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()