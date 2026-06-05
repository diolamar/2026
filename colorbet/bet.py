# ============ FILE: bet.py (COMPLETE ENHANCED VERSION) ============
import tkinter as tk
from tkinter import messagebox, ttk
import ctypes
from ctypes import wintypes
import pyautogui
import json
import csv
import os
import shutil
import sys
import time
import random
import threading
from pathlib import Path
from PIL import ImageGrab, ImageTk
from typing import Dict, List, Tuple, Optional
import logging
from datetime import datetime
from collections import Counter, defaultdict
import traceback
import statistics
import math

from autoclick_runtime import (
    ClickPlanError,
    build_bet_plan,
    create_click_actions,
    format_bet_plan,
    perform_click_actions,
)
from capture import ColorMatcher, GameAnalyzer, ScreenCaptureManager
from history_strategy import (
    HistoryStrategyModel,
    _is_complete_window,
    _is_window_start,
    _parse_history_rows,
    build_history_strategy_model,
)
from models import (
    AppConfig,
    AutomationState,
    CalibrationPoint,
    GameState,
    ThreadSafeState,
    DecisionRecord,
    DecisionStats,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

PROGRESSION_PROFILES: Dict[str, Tuple[int, ...]] = {
    "Slow Progress": (5, 10, 15, 25, 40, 65, 100, 160, 250, 400, 650, 1000, 1600),
    "Pure Fib": (5, 5, 10, 15, 25, 40, 65, 105, 170, 275, 445, 720, 1165),
    "Pure Mart": (5, 10, 20, 40, 80, 160, 320, 640, 1280, 2560, 5120, 10240, 20480),
    "Mart(plus5)": (5, 15, 35, 75, 155, 315, 635, 1275, 2555, 5115, 10235, 20475, 40955),
    "MartFib": (5, 15, 35, 75, 155, 315, 635, 1275, 2555, 3830, 6385, 10215, 16600),
}

APP_NAME = "AutoClickerPro"

if os.name == "nt":
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = wintypes.HWND(-1)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND


def _get_window_text(hwnd: int) -> str:
    if os.name != "nt":
        return ""

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""

    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def is_brave_window(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False

    if not user32.IsWindowVisible(hwnd):
        return False

    title = _get_window_text(hwnd)
    return bool(title and "brave" in title.lower())


def find_brave_window(preferred_hwnd: Optional[int] = None) -> Optional[int]:
    if os.name != "nt":
        return None

    if preferred_hwnd and is_brave_window(preferred_hwnd):
        return int(preferred_hwnd)

    foreground_hwnd = int(user32.GetForegroundWindow() or 0)
    if foreground_hwnd and is_brave_window(foreground_hwnd):
        return foreground_hwnd

    matches: List[int] = []

    @EnumWindowsProc
    def enum_windows_proc(hwnd, _lparam):
        try:
            if not is_brave_window(hwnd):
                return True
            matches.append(int(hwnd))
        except Exception:
            logger.exception("EnumWindows callback failed")
        return True

    user32.EnumWindows(enum_windows_proc, 0)
    return matches[0] if matches else None


def force_brave_window_on_top() -> bool:
    if os.name != "nt":
        return False

    hwnd = find_brave_window()
    if not hwnd:
        return False

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    return True


def get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    if os.name != "nt" or not hwnd:
        return None

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    return (
        int(rect.left),
        int(rect.top),
        int(rect.right - rect.left),
        int(rect.bottom - rect.top),
    )


def restore_window_rect(hwnd: int, rect: Tuple[int, int, int, int], bring_to_front: bool = False) -> bool:
    if os.name != "nt" or not hwnd:
        return False

    x, y, width, height = rect

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    success = bool(user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST if bring_to_front else wintypes.HWND(0),
        x,
        y,
        width,
        height,
        SWP_SHOWWINDOW,
    ))
    if bring_to_front and success:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    return success


def get_runtime_data_dir() -> Path:
    """Return a writable directory for runtime data in both .py and .exe modes."""
    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME

    return Path(__file__).resolve().parent / "config"


def get_legacy_config_dir() -> Path:
    """Old save location used before the executable-safe path fix."""
    base_path = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base_path / "config"


def ensure_runtime_data_dir() -> Path:
    runtime_dir = get_runtime_data_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def migrate_legacy_runtime_files(runtime_dir: Path):
    """Copy old config/calibration files into the new writable runtime directory once."""
    legacy_dir = get_legacy_config_dir()
    if legacy_dir.resolve() == runtime_dir.resolve() or not legacy_dir.exists():
        return

    for filename in ("app_config.json", "calibration.json", "calibration_frame.png", "simulate_results.csv", "simulate_round_detector.png"):
        source_path = legacy_dir / filename
        target_path = runtime_dir / filename
        if source_path.exists() and not target_path.exists():
            try:
                shutil.copy2(source_path, target_path)
            except Exception as exc:
                logger.warning(f"Failed to migrate {source_path} to {target_path}: {exc}")


def configure_file_logging(runtime_dir: Path):
    """Persist terminal-style logging to logs/terminal.log for all modules."""
    logs_dir = runtime_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_path = logs_dir / "terminal.log"
    resolved_log_path = str(log_path.resolve())
    root_logger = logging.getLogger()
    formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')

    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            base_filename = getattr(handler, "baseFilename", None)
            if base_filename == resolved_log_path:
                return
            if base_filename and Path(base_filename).name == "application.log":
                root_logger.removeHandler(handler)
                handler.close()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


class EventLogger:
    """Keep recent events in memory while routing file output to terminal.log."""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.logs_dir = config_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.terminal_log_path = self.logs_dir / "terminal.log"
        self.decision_log_path = self.logs_dir / "decision.log"
        self._decision_log_lock = threading.Lock()
        self.recent_events = []
        self.max_recent_events = 100

    def log_event(self, event_type: str, description: str, **kwargs):
        timestamp = datetime.now().isoformat()
        log_entry = {
            "timestamp": timestamp,
            "session_id": self.session_id,
            "event_type": event_type,
            "description": description,
            "data": kwargs,
        }

        self.recent_events.insert(0, {
            "timestamp": timestamp,
            "type": event_type,
            "description": description,
            "data": kwargs,
        })
        self.recent_events = self.recent_events[:self.max_recent_events]

        logger.info(description)
        logger.debug("Event details: %s", log_entry)

    def log_error(self, error_type: str, error: Exception, context: dict = None):
        details = context or {}
        logger.error("%s: %s | context=%s", error_type, error, details)
        logger.debug("Traceback for %s", error_type, exc_info=True)
        self.log_event("ERROR", f"{error_type}: {str(error)}", **{k: str(v) for k, v in details.items()})

    def log_decision(self, **kwargs):
        payload = {
            "timestamp": datetime.now().isoformat(),
            "session_id": self.session_id,
            **kwargs,
        }
        try:
            line = json.dumps(payload, ensure_ascii=False)
            with self._decision_log_lock:
                with open(self.decision_log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as exc:
            logger.warning("Decision log write error: %s", exc)

    def export_for_ai(self) -> str:
        export_data = {
            "session_id": self.session_id,
            "export_time": datetime.now().isoformat(),
            "logs_directory": str(self.logs_dir),
            "log_files": {
                "terminal_log": str(self.terminal_log_path),
                "decision_log": str(self.decision_log_path),
            },
            "recent_events": self.recent_events[:50]
        }
        return json.dumps(export_data, indent=2, ensure_ascii=False)

    def get_session_summary(self) -> str:
        summary = {
            "session_id": self.session_id,
            "start_time": self.session_id.replace("_", " "),
            "log_directory": str(self.logs_dir),
            "terminal_log": str(self.terminal_log_path),
            "decision_log": str(self.decision_log_path),
            "recent_events_count": len(self.recent_events)
        }
        return json.dumps(summary, indent=2)


class AutomationEngine:
    def __init__(self, config: AppConfig, capture_mgr: ScreenCaptureManager, 
                 color_matcher: ColorMatcher, game_analyzer: GameAnalyzer, 
                 app: Optional['AutoClickerPro'] = None):
        self.config = config
        self.capture_mgr = capture_mgr
        self.game_analyzer = game_analyzer
        self.app = app
        self.state = ThreadSafeState()
        
        self._monitor_thread: Optional[threading.Thread] = None
        self._simulate_thread: Optional[threading.Thread] = None
        self._autoclick_thread: Optional[threading.Thread] = None
        self._autosim_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._simulate_halted = False
        self.history_strategy_model: Optional[HistoryStrategyModel] = None
        self.last_history_gate_reason = ""
        self.last_history_swap_hint = ""
        self.last_bet_color: Optional[str] = None
        self.last_bet_value: Optional[str] = None
        self.last_bet_amount: Optional[str] = None
        self.last_bet_clicked_at: Optional[str] = None
        self.last_result: Optional[str] = None
        self.last_c1_boxes: List[str] = []
        self.c1_history: List[List[str]] = []
        self.pending_round_number = 1
        self.round_count = 0
        self.detected_round_count = 0
        self.display_round_counter = 0
        self._last_display_round_signature: Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]] = None
        self.win_count = 0
        self.lose_count = 0
        self.profit_total = 0.0
        self.loss_streak = 0
        self.bet_amount_steps = self._build_virtual_bet_steps(
            start=self.config.martingale_start,
            max_steps=self.config.martingale_max_steps,
            flat_cap=self._effective_max_bet_per_round() or None,
            profile_name=self.config.strategy_progression_profile,
        )
        self.martingale_index = 0
        self.max_loss_streak = len(self.bet_amount_steps)
        self.bet_mode = "martingale"
        self.last_prediction = "None"
        self.last_decision_reason = "Waiting for C1 history"
        self.last_decision_csv_reason = "WAIT_HISTORY"
        self.last_decision_score = 0.0
        self.last_decision_gap = 0.0
        self.last_decision_probability = 0.0
        self.last_expected_value = 0.0
        self.last_probability_edge = 0.0
        self.last_probability_samples = 0
        self.last_skip_reason = ""
        self.last_skip_csv_reason = ""
        self.last_regime = "RANGE"
        self.last_regime_reason = "Waiting for enough history"
        self._soft_idle_runtime_started_at: Optional[float] = None
        self._soft_idle_triggered_for_run = False
        self._scheduled_idle_trigger_date: Optional[str] = None
        self._scheduled_idle_waiting_for_win = False
        self._soft_idle_waiting_for_win = False
        self._scheduled_idle_waiting_for_profit = False
        self._awaiting_cycle_reset_sync = False
        self._blank_reference_visible_until: float = 0.0
        self._cycle_reset_candidate_since: Optional[float] = None
        self._weekly_idle_auto_resume_mode: Optional[str] = None
        self._weekly_idle_auto_resume_not_before: Optional[str] = None
        self._last_board_idle_signature: Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]] = None
        self._last_board_changed_at: Optional[float] = None
        self._board_idle_triggered_for_signature = False
        self._brave_lock_hwnd: Optional[int] = None
        self._brave_lock_rect: Optional[Tuple[int, int, int, int]] = None
        
        # NEW: Decision tracking
        self.decision_history: List[DecisionRecord] = []
        self.decision_stats = DecisionStats()
        self.learning_mode = self.config.adaptive_learning_enabled
        self.last_decision_time = 0.0
        self.consecutive_strategy_skips = 0
        self.skip_relief_applied_count = 0
        
        # Performance metrics
        self.start_time = time.time()
        self.total_clicks = 0
        self.last_performance_log = time.time()
        
        # Session recovery
        self._load_session_state()

    @staticmethod
    def _build_virtual_bet_steps(
        start: int,
        max_steps: int,
        flat_cap: Optional[int] = None,
        profile_name: Optional[str] = None,
    ) -> List[int]:
        del start
        base_steps = list(PROGRESSION_PROFILES.get(profile_name or "", PROGRESSION_PROFILES["Slow Progress"]))
        steps = [min(step, flat_cap) if flat_cap is not None else step for step in base_steps]
        if max_steps <= 0:
            return []
        if len(steps) >= max_steps:
            return steps[:max_steps]
        last_step = steps[-1]
        while len(steps) < max_steps:
            steps.append(last_step)
        return steps

    @staticmethod
    def _thread_is_running(thread: Optional[threading.Thread]) -> bool:
        return thread is not None and thread.is_alive()

    @staticmethod
    def _is_in_daily_idle_window(now: Optional[datetime] = None) -> bool:
        current = now or datetime.now()
        return (
            current.weekday() == 2
            and (current.hour, current.minute) >= (3, 0)
            and (current.hour, current.minute) < (5, 0)
        )

    def _get_soft_idle_runtime_minutes(self) -> int:
        return max(0, int(self.config.scheduled_idle_lead_minutes))

    def _get_board_idle_timeout_seconds(self) -> int:
        return max(0, int(self.config.board_idle_timeout_minutes)) * 60

    def _get_board_idle_signature(
        self,
        game_state: Optional[GameState],
    ) -> Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]]:
        if not game_state or not game_state.columns:
            return None
        return tuple(
            (column.column_index, tuple(column.boxes))
            for column in game_state.columns[:self.config.total_columns]
        )

    def _reset_board_idle_tracker(self, game_state: Optional[GameState] = None):
        self._last_board_idle_signature = self._get_board_idle_signature(game_state)
        self._last_board_changed_at = time.time() if self._last_board_idle_signature else None
        self._board_idle_triggered_for_signature = False

    def _handle_unchanged_board_idle(self, prefix: str, game_state: Optional[GameState]) -> bool:
        timeout_seconds = self._get_board_idle_timeout_seconds()
        if timeout_seconds <= 0:
            return False

        signature = self._get_board_idle_signature(game_state)
        if signature is None:
            self._reset_board_idle_tracker()
            return False

        now = time.time()
        if signature != self._last_board_idle_signature:
            self._last_board_idle_signature = signature
            self._last_board_changed_at = now
            self._board_idle_triggered_for_signature = False
            return False

        if self._last_board_changed_at is None:
            self._last_board_changed_at = now
            return False

        if self._board_idle_triggered_for_signature:
            return False

        unchanged_seconds = now - self._last_board_changed_at
        if unchanged_seconds < timeout_seconds:
            return False

        self._board_idle_triggered_for_signature = True
        timeout_minutes = max(1, int(self.config.board_idle_timeout_minutes))
        self._request_idle_shutdown(
            f"{prefix}: board unchanged for {timeout_minutes} minute(s). Status set to Idle."
        )
        return True

    def _mark_soft_idle_runtime_start(self, preserve_existing: bool = False):
        if preserve_existing and self._soft_idle_runtime_started_at is not None:
            return
        self._soft_idle_runtime_started_at = time.time()
        self._soft_idle_triggered_for_run = False

    def _is_soft_idle_due(self, now: Optional[datetime] = None) -> bool:
        runtime_minutes = self._get_soft_idle_runtime_minutes()
        if runtime_minutes <= 0:
            return False
        if self._soft_idle_runtime_started_at is None or self._soft_idle_triggered_for_run:
            return False
        return (time.time() - self._soft_idle_runtime_started_at) >= (runtime_minutes * 60)

    def _is_daily_idle_due(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now()
        if not self._is_in_daily_idle_window(current):
            return False
        today = current.strftime("%Y-%m-%d")
        return self._scheduled_idle_trigger_date != today

    def _clear_weekly_idle_auto_resume(self):
        self._weekly_idle_auto_resume_mode = None
        self._weekly_idle_auto_resume_not_before = None

    def _arm_weekly_idle_auto_resume(self, mode: Optional[str], now: Optional[datetime] = None):
        mode_key = str(mode or "").strip().lower()
        if mode_key not in {"simulate", "autoclick", "autosim"}:
            self._clear_weekly_idle_auto_resume()
            return

        current = now or datetime.now()
        resume_at = current.replace(hour=5, minute=0, second=0, microsecond=0)
        self._weekly_idle_auto_resume_mode = mode_key
        self._weekly_idle_auto_resume_not_before = resume_at.isoformat()

    def _is_weekly_idle_auto_resume_due(self, now: Optional[datetime] = None) -> bool:
        if not self._weekly_idle_auto_resume_mode or not self._weekly_idle_auto_resume_not_before:
            return False
        try:
            resume_at = datetime.fromisoformat(self._weekly_idle_auto_resume_not_before)
        except ValueError:
            self._clear_weekly_idle_auto_resume()
            return False
        current = now or datetime.now()
        return current >= resume_at

    def _get_weekly_idle_auto_resume_mode(self) -> Optional[str]:
        return self._weekly_idle_auto_resume_mode

    def _is_any_idle_waiting_for_win(self) -> bool:
        return self._scheduled_idle_waiting_for_win or self._soft_idle_waiting_for_win

    def _request_idle_shutdown(self, message: str, preserve_weekly_auto_resume: bool = False):
        self._clear_pending_bet()
        self._scheduled_idle_waiting_for_win = False
        self._soft_idle_waiting_for_win = False
        self._scheduled_idle_waiting_for_profit = False
        self._simulate_halted = True
        if not preserve_weekly_auto_resume:
            self._clear_weekly_idle_auto_resume()
        self._reset_board_idle_tracker()
        self.state.set_monitor_idle_until(0)
        self.state.update_status(message)
        self._stop_event.set()
        self.state.state = AutomationState.IDLE
        self._save_session_state()

    def _handle_daily_idle_cutoff(self, prefix: str, first_round: bool = False) -> bool:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        mode_map = {
            "SIMULATE": "simulate",
            "AUTOCLICK": "autoclick",
            "AUTOSIM": "autosim",
        }
        resume_mode = mode_map.get(str(prefix or "").upper())

        if self._is_daily_idle_due(now):
            self._scheduled_idle_trigger_date = today
            self._soft_idle_waiting_for_win = False
            self._scheduled_idle_waiting_for_profit = False
            self._arm_weekly_idle_auto_resume(resume_mode, now=now)
            if self.loss_streak > 0:
                self._scheduled_idle_waiting_for_win = True
                self.state.update_status(
                    f"{prefix}: Wednesday 03:00 mandatory idle reached during loss streak {self.loss_streak}. Extending until a win clears the streak, then status will switch to Idle."
                )
                self._save_session_state()
                return False
            self.loss_streak = 0
            self._reset_progression()
            self._request_idle_shutdown(
                f"{prefix}: Wednesday 03:00 mandatory idle reached. Loss streak already 0; auto-resume is armed after 05:00 AM."
                ,
                preserve_weekly_auto_resume=True,
            )
            return True
        if self._scheduled_idle_waiting_for_win:
            if self.loss_streak == 0:
                self._request_idle_shutdown(
                    f"{prefix}: Wednesday 03:00 mandatory idle extension completed after a win cleared the loss streak. Auto-resume is armed after 05:00 AM."
                    ,
                    preserve_weekly_auto_resume=True,
                )
                return True
            return False
        if self._soft_idle_waiting_for_win:
            if self.loss_streak <= 1:
                if self.profit_total > 0:
                    self._request_idle_shutdown(
                        f"{prefix}: soft idle extension completed after loss streak fell to {self.loss_streak} and profit recovered to {self.profit_total:.2f}. Status set to Idle."
                    )
                    return True
                self._soft_idle_waiting_for_win = False
                self._scheduled_idle_waiting_for_profit = True
                self.state.update_status(
                    f"{prefix}: soft idle streak extension completed, but profit is still {self.profit_total:.2f}. Extending until profit becomes positive, max loss streak hits {self.max_loss_streak}, or Wednesday 03:00 mandatory idle."
                )
                self._save_session_state()
                return False
            if self.loss_streak >= self.max_loss_streak:
                self._request_idle_shutdown(
                    f"{prefix}: max loss streak reached while waiting for soft idle win-clear extension. Status set to Idle."
                )
                return True
            return False
        if self._scheduled_idle_waiting_for_profit:
            if self.profit_total > 0:
                if self.loss_streak <= 1:
                    self._request_idle_shutdown(
                        f"{prefix}: soft idle recovery completed at profit {self.profit_total:.2f} with loss streak {self.loss_streak}. Status set to Idle."
                    )
                    return True
                self._scheduled_idle_waiting_for_profit = False
                self._soft_idle_waiting_for_win = True
                self.state.update_status(
                    f"{prefix}: soft idle profit recovery completed, but loss streak is still {self.loss_streak}. Extending until a win clears the streak to 1 or lower, then status will switch to Idle."
                )
                self._save_session_state()
                return False
            if self.loss_streak >= self.max_loss_streak:
                self._request_idle_shutdown(
                    f"{prefix}: max loss streak reached while waiting for soft idle recovery. Status set to Idle."
                )
                return True
            return False
        if not self._is_soft_idle_due(now):
            return False

        runtime_minutes = self._get_soft_idle_runtime_minutes()
        self._soft_idle_triggered_for_run = True
        runtime_text = f"{runtime_minutes} minute(s)"

        if self.loss_streak > 1:
            self._soft_idle_waiting_for_win = True
            self.state.update_status(
                f"{prefix}: soft idle runtime {runtime_text} reached during loss streak {self.loss_streak}. Extending until a win clears the streak to 1 or lower; if profit is still negative after that, soft idle will keep extending; Wednesday 03:00 mandatory idle remains active."
            )
            self._save_session_state()
            return False

        if self.profit_total > 0 and self.loss_streak <= 1:
            self._request_idle_shutdown(
                f"{prefix}: soft idle runtime {runtime_text} reached. Status set to Idle; Wednesday 03:00 mandatory idle remains active."
            )
            return True

        self._scheduled_idle_waiting_for_profit = True
        self.state.update_status(
            f"{prefix}: soft idle runtime {runtime_text} reached with profit {self.profit_total:.2f}. Extending until profit becomes positive and loss streak is 1 or lower, max loss streak hits {self.max_loss_streak}, or Wednesday 03:00 mandatory idle."
        )
        self._save_session_state()
        return False

    def _stop_worker_threads(self, timeout: float = 2.5):
        for thread in (self._monitor_thread, self._simulate_thread, self._autoclick_thread, self._autosim_thread):
            if self._thread_is_running(thread):
                thread.join(timeout=timeout)

    def _ensure_brave_on_top(self, context: str = ""):
        if force_brave_window_on_top():
            if context:
                logger.debug("Brave forced on top for %s", context)
        elif context:
            logger.debug("Brave window not found for %s", context)

    def _clear_brave_window_lock(self):
        self._brave_lock_hwnd = None
        self._brave_lock_rect = None

    def _get_resume_round_signature(
        self,
        game_state: Optional[GameState] = None,
    ) -> Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]]:
        current_state = game_state or self.state.get_game_state()
        if not current_state or self._is_cycle_reset_state(current_state):
            return None

        active_round = self._get_active_round_info(current_state)
        if not active_round:
            return None

        return active_round["signature"]

    def _arm_brave_window_lock(self, context: str = "") -> bool:
        if os.name != "nt" or not self.config.lock_brave_window_enabled:
            self._clear_brave_window_lock()
            return False

        hwnd = find_brave_window()
        if not hwnd:
            logger.warning("Brave lock skipped%s: window not found", f" for {context}" if context else "")
            self._clear_brave_window_lock()
            return False

        rect = None
        if self.app and hasattr(self.app, "get_saved_brave_rect"):
            rect = self.app.get_saved_brave_rect()

        if rect is None:
            rect = get_window_rect(hwnd)
        if not rect:
            logger.warning("Brave lock skipped%s: could not read window bounds", f" for {context}" if context else "")
            self._clear_brave_window_lock()
            return False

        self._brave_lock_hwnd = hwnd
        self._brave_lock_rect = rect
        logger.info("Brave lock armed%s at %s", f" for {context}" if context else "", rect)
        return True

    def _enforce_brave_window_lock(self, context: str = "", bring_to_front: bool = False) -> bool:
        if os.name != "nt" or not self.config.lock_brave_window_enabled:
            return False

        hwnd = self._brave_lock_hwnd
        target_rect = self._brave_lock_rect
        if not hwnd or not target_rect:
            if not self._arm_brave_window_lock(context):
                return False
            hwnd = self._brave_lock_hwnd
            target_rect = self._brave_lock_rect
            if not hwnd or not target_rect:
                return False

        current_rect = get_window_rect(hwnd)
        if current_rect != target_rect:
            success = restore_window_rect(hwnd, target_rect, bring_to_front=bring_to_front)
            if success:
                logger.info("Brave lock restored%s to %s", f" for {context}" if context else "", target_rect)
            return success

        if bring_to_front:
            return restore_window_rect(hwnd, target_rect, bring_to_front=True)

        return True

    def _get_active_round_info(self, game_state: Optional[GameState]) -> Optional[Dict[str, object]]:
        if not game_state or not game_state.columns:
            return None

        valid_suffix: List[Tuple[int, Tuple[str, ...]]] = []
        for column in reversed(game_state.columns[:self.config.total_columns]):
            if column.all_boxes_known and all(box in self.config.color_labels for box in column.boxes):
                valid_suffix.append((column.column_index, tuple(column.boxes)))
                continue
            break

        if not valid_suffix:
            return None

        valid_suffix.reverse()
        basis_index, basis_boxes = valid_suffix[0]

        if not self._has_blank_reference_allowance(game_state):
            return None

        left_prefix = game_state.columns[:basis_index - 1]
        if left_prefix:
            left_prefix_has_only_blank_unknown = all(
                all(box in {"Blank", "Unknown"} for box in column.boxes)
                for column in left_prefix
            )
            if not left_prefix_has_only_blank_unknown:
                return None

        return {
            "basis_index": basis_index,
            "boxes": list(basis_boxes),
            "valid_count": len(valid_suffix),
            "signature": tuple(valid_suffix),
        }

    def _refresh_blank_reference_allowance(self, game_state: Optional[GameState]):
        if (
            game_state
            and game_state.blank_reference_visible
            and game_state.blank_detected
            and game_state.confidence_score <= 0
        ):
            self._blank_reference_visible_until = max(
                self._blank_reference_visible_until,
                game_state.timestamp + self.config.monitor_blank_allowance_seconds,
            )

    def _has_blank_reference_allowance(self, game_state: Optional[GameState]) -> bool:
        if not game_state:
            return False
        if game_state.blank_reference_visible:
            return True
        return game_state.timestamp <= self._blank_reference_visible_until

    def _normalize_confidence_for_valid_detection(self, game_state: Optional[GameState]) -> Optional[GameState]:
        if not game_state:
            return game_state
        if self._get_active_round_info(game_state):
            game_state.confidence_score = 100.0
        return game_state

    @staticmethod
    def _calculate_history_overlap(
        stored_history: List[List[str]],
        visible_history: List[List[str]],
    ) -> int:
        max_overlap = min(len(stored_history), len(visible_history))
        for overlap in range(max_overlap, 0, -1):
            if stored_history[-overlap:] == visible_history[:overlap]:
                return overlap
        return 0

    def _get_visible_round_history(self, game_state: Optional[GameState]) -> List[List[str]]:
        active_round = self._get_active_round_info(game_state)
        if not active_round:
            return []

        signature = active_round["signature"]
        # Signature is newest->oldest within the valid suffix; reverse it so
        # history remains oldest->newest like c1_history.
        return [list(boxes) for _column_index, boxes in reversed(signature)]

    def _get_effective_round_history(
        self,
        game_state: Optional[GameState],
        limit: Optional[int] = None,
    ) -> List[List[str]]:
        stored_history = [list(round_boxes) for round_boxes in self.c1_history]
        visible_history = self._get_visible_round_history(game_state)

        if visible_history:
            overlap = self._calculate_history_overlap(stored_history, visible_history)
            combined_history = stored_history + visible_history[overlap:]
        else:
            combined_history = stored_history

        if limit is not None:
            return combined_history[-limit:]
        return combined_history

    @staticmethod
    def _get_signature_basis_index(
        signature: Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]],
    ) -> Optional[int]:
        if not signature:
            return None
        return int(signature[0][0])

    def _is_cycle_restart_signature(
        self,
        signature: Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]],
        previous_signature: Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]],
    ) -> bool:
        current_basis = self._get_signature_basis_index(signature)
        previous_basis = self._get_signature_basis_index(previous_signature)
        if current_basis is None or previous_basis is None:
            return False
        return current_basis > previous_basis

    def _is_cycle_restart_active_round(
        self,
        active_round: Optional[Dict[str, object]],
        previous_signature: Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]],
    ) -> bool:
        if not active_round:
            return False
        return self._is_cycle_restart_signature(
            active_round.get("signature"),
            previous_signature,
        )

    def _is_waiting_for_stable_match_state(
        self,
        game_state: Optional[GameState],
        active_round: Optional[Dict[str, object]] = None,
    ) -> bool:
        if not game_state or not game_state.columns:
            return False
        if game_state.has_invalid_board_pattern:
            return False
        if active_round is None:
            active_round = self._get_active_round_info(game_state)
        return game_state.any_unknown and not active_round

    def _should_start_unsynced(
        self,
        game_state: Optional[GameState] = None,
    ) -> bool:
        current_state = game_state or self.state.get_game_state()
        if not current_state or self._is_cycle_reset_state(current_state):
            return False
        if self._get_active_round_info(current_state):
            return False
        if current_state.has_invalid_board_pattern or current_state.any_unknown:
            return False

        return self._is_full_valid_board(current_state)

    def _has_unresolved_pending_bet(self) -> bool:
        return bool(self.last_bet_color)

    def _is_full_valid_board(
        self,
        game_state: Optional[GameState] = None,
    ) -> bool:
        current_state = game_state or self.state.get_game_state()
        if not current_state or not current_state.columns:
            return False
        if current_state.any_unknown or current_state.has_invalid_board_pattern:
            return False

        if current_state.all_columns_full:
            return True

        return self._get_visible_valid_suffix_count(current_state) >= self.config.total_columns

    def _get_visible_valid_suffix_count(
        self,
        game_state: Optional[GameState] = None,
    ) -> int:
        current_state = game_state or self.state.get_game_state()
        if not current_state or not current_state.columns:
            return 0

        valid_suffix_count = 0
        for column in reversed(current_state.columns[:self.config.total_columns]):
            if column.all_boxes_known and all(box in self.config.color_labels for box in column.boxes):
                valid_suffix_count += 1
                continue
            break

        return valid_suffix_count

    def _peek_startup_game_state(self) -> Optional[GameState]:
        with self.capture_mgr.capture_region() as (screenshot, bbox):
            if screenshot is None:
                return None

            game_state = self.game_analyzer.analyze_game_state(screenshot, bbox)
            self._refresh_blank_reference_allowance(game_state)
            self._normalize_confidence_for_valid_detection(game_state)
            self.state.set_game_state(game_state)
            return game_state

    def _is_safe_partial_synced_start(
        self,
        game_state: Optional[GameState] = None,
    ) -> bool:
        current_state = game_state or self.state.get_game_state()
        if not current_state or not current_state.columns:
            return False
        if self._is_cycle_reset_state(current_state):
            return False

        active_round = self._get_active_round_info(current_state)
        if not active_round:
            return False

        visible_valid_suffix = int(active_round["valid_count"])
        return 1 <= visible_valid_suffix < self.config.total_columns

    def _should_release_sync_wait_from_visible_suffix(
        self,
        game_state: Optional[GameState] = None,
    ) -> bool:
        return self._is_safe_partial_synced_start(game_state)

    def _resolve_startup_sync_wait(
        self,
        preserve_sync_handoff: bool = False,
    ) -> bool:
        startup_state = self._peek_startup_game_state()
        if startup_state and self._is_cycle_reset_state(startup_state):
            force_unsynced = False
        elif startup_state and self._is_safe_partial_synced_start(startup_state):
            force_unsynced = False
        else:
            force_unsynced = True
        if startup_state:
            logger.info(
                "Startup sync trap: all_full=%s any_unknown=%s invalid_pattern=%s blank_area=%s confidence=%.1f valid_suffix=%s safe_partial=%s force_unsynced=%s preserve_handoff=%s",
                startup_state.all_columns_full,
                startup_state.any_unknown,
                startup_state.has_invalid_board_pattern,
                startup_state.blank_reference_visible,
                startup_state.confidence_score,
                self._get_visible_valid_suffix_count(startup_state),
                self._is_safe_partial_synced_start(startup_state),
                force_unsynced,
                preserve_sync_handoff,
            )
        if preserve_sync_handoff:
            return force_unsynced
        return force_unsynced

    def _can_preserve_betting_handoff_sync(self) -> bool:
        if self._has_unresolved_pending_bet():
            return False

        current_state = self.state.get_game_state()
        if not current_state:
            return False

        if self._is_cycle_reset_state(current_state):
            return True

        active_round = self._get_active_round_info(current_state)
        if active_round is not None:
            return True

        if current_state.has_invalid_board_pattern or current_state.any_unknown:
            return False

        if self._should_start_unsynced(current_state):
            return False

        return False

    def _update_display_round_counter(self, game_state: Optional[GameState]):
        if self._is_cycle_reset_state(game_state):
            logger.info("Cycle counter reset to 0: confidence dropped to 0")
            self.display_round_counter = 0
            self._last_display_round_signature = None
            return

        active_round = self._get_active_round_info(game_state)
        if not active_round:
            return

        signature = active_round["signature"]
        if signature == self._last_display_round_signature:
            return

        if self.display_round_counter <= 0 or self._is_cycle_restart_signature(signature, self._last_display_round_signature):
            self.display_round_counter = int(active_round["valid_count"])
        else:
            self.display_round_counter = 1 if self.display_round_counter >= 100 else self.display_round_counter + 1

        self._last_display_round_signature = signature

    def _advance_detected_round_count(self):
        self.detected_round_count += 1

    def _effective_max_bet_per_round(self) -> int:
        configured = int(self.config.max_bet_per_round or 0)
        return max(5, configured) if configured > 0 else 0

    def _effective_loss_limit(self) -> Optional[float]:
        configured = self.config.loss_limit
        if configured is None or configured <= 0:
            return None
        return float(configured)

    def _effective_max_loss_streak(self) -> int:
        return max(1, self.max_loss_streak)

    def _reset_progression(self):
        self.bet_mode = "martingale"
        self.martingale_index = 0

    def _get_current_target_amount(self) -> int:
        amount = self.bet_amount_steps[self.martingale_index]
        effective_max = self._effective_max_bet_per_round()
        return min(amount, effective_max) if effective_max > 0 else amount

    @staticmethod
    def _amount_band_for_target_amount(amount: Optional[int]) -> str:
        if amount is None:
            return "LOW"
        amount_value = int(amount)
        if amount_value <= 15:
            return "LOW"
        if amount_value <= 155:
            return "MID"
        if amount_value <= 315:
            return "HIGH"
        return "DEEP"

    def _load_session_state(self):
        if self.app and hasattr(self.app, 'config_dir'):
            session_path = self.app.config_dir / "session_backup.json"
            if session_path.exists():
                try:
                    with open(session_path, 'r') as f:
                        session = json.load(f)
                    self.round_count = session.get("round_count", 0)
                    self.detected_round_count = session.get("detected_round_count", self.round_count)
                    self.win_count = session.get("win_count", 0)
                    self.lose_count = session.get("lose_count", 0)
                    self.profit_total = session.get("profit_total", 0.0)
                    self.loss_streak = session.get("loss_streak", 0)
                    restored_index = int(session.get("martingale_index", 0))
                    self.martingale_index = max(0, min(restored_index, len(self.bet_amount_steps) - 1))
                    self.c1_history = session.get("c1_history", [])
                    self.bet_mode = "martingale"
                    self._soft_idle_runtime_started_at = session.get("soft_idle_runtime_started_at")
                    self._soft_idle_triggered_for_run = bool(session.get("soft_idle_triggered_for_run", False))
                    self._scheduled_idle_trigger_date = session.get("scheduled_idle_trigger_date")
                    self._scheduled_idle_waiting_for_win = bool(
                        session.get("scheduled_idle_waiting_for_win", False)
                    )
                    self._soft_idle_waiting_for_win = bool(
                        session.get("soft_idle_waiting_for_win", False)
                    )
                    self._scheduled_idle_waiting_for_profit = bool(
                        session.get("scheduled_idle_waiting_for_profit", False)
                    )
                    self._weekly_idle_auto_resume_mode = session.get("weekly_idle_auto_resume_mode")
                    self._weekly_idle_auto_resume_not_before = session.get("weekly_idle_auto_resume_not_before")
                    if restored_index != self.martingale_index:
                        logger.warning(
                            "Clamped restored martingale index from %s to %s",
                            restored_index,
                            self.martingale_index,
                        )
                    logger.info(f"Session recovered: {self.round_count} rounds, profit: {self.profit_total:.2f}")
                except Exception as e:
                    logger.error(f"Failed to load session: {e}")
    
    def _save_session_state(self):
        if self.app and hasattr(self.app, 'config_dir'):
            session = {
                "round_count": self.round_count,
                "detected_round_count": self.detected_round_count,
                "win_count": self.win_count,
                "lose_count": self.lose_count,
                "profit_total": self.profit_total,
                "loss_streak": self.loss_streak,
                "martingale_index": self.martingale_index,
                "bet_mode": self.bet_mode,
                "c1_history": self.c1_history[-30:],
                "soft_idle_runtime_started_at": self._soft_idle_runtime_started_at,
                "soft_idle_triggered_for_run": self._soft_idle_triggered_for_run,
                "scheduled_idle_trigger_date": self._scheduled_idle_trigger_date,
                "scheduled_idle_waiting_for_win": self._scheduled_idle_waiting_for_win,
                "soft_idle_waiting_for_win": self._soft_idle_waiting_for_win,
                "scheduled_idle_waiting_for_profit": self._scheduled_idle_waiting_for_profit,
                "weekly_idle_auto_resume_mode": self._weekly_idle_auto_resume_mode,
                "weekly_idle_auto_resume_not_before": self._weekly_idle_auto_resume_not_before,
                "timestamp": datetime.now().isoformat()
            }
            try:
                session_path = self.app.config_dir / "session_backup.json"
                with open(session_path, 'w') as f:
                    json.dump(session, f, indent=2)
                logger.debug("Session state saved")
            except Exception as e:
                logger.error(f"Failed to save session: {e}")
    
    def _log_performance(self):
        if self.round_count > 0 and self.round_count % 100 == 0:
            elapsed = time.time() - self.start_time
            clicks_per_sec = self.total_clicks / elapsed if elapsed > 0 else 0
            rounds_per_hour = (self.round_count / elapsed) * 3600 if elapsed > 0 else 0
            logger.info(f"Performance: {clicks_per_sec:.2f} clicks/sec, {rounds_per_hour:.1f} rounds/hour")
            
            if hasattr(self.app, 'color_matcher'):
                cache_stats = self.app.color_matcher.get_cache_stats()
                logger.info(
                    "Color cache: %.1f%% hit rate (%s hits, %s misses, size %s/%s)",
                    cache_stats['hit_rate'],
                    cache_stats['hits'],
                    cache_stats['misses'],
                    cache_stats['cache_size'],
                    cache_stats['cache_maxsize'],
                )
    
    def _check_bet_limits(self, amount: int) -> bool:
        effective_max_bet = self._effective_max_bet_per_round()
        effective_loss_limit = self._effective_loss_limit()
        effective_max_loss_streak = self._effective_max_loss_streak()

        if self.loss_streak >= effective_max_loss_streak:
            self.state.update_status(
                f"Max loss streak reached after {self.loss_streak} consecutive losses "
                f"(limit {effective_max_loss_streak})."
            )
            self._simulate_halted = True
            self._stop_event.set()
            return False

        if effective_max_bet > 0 and amount > effective_max_bet:
            self.state.update_status(f"Bet {amount} exceeds max {effective_max_bet}")
            self._simulate_halted = True
            self._stop_event.set()
            return False
        
        if effective_loss_limit is not None and self.profit_total <= -effective_loss_limit:
            self.state.update_status(
                f"Loss limit reached ${self.profit_total:.2f} "
                f"<= -${effective_loss_limit:.2f}"
            )
            self._simulate_halted = True
            self._stop_event.set()
            return False
        
        if self.config.profit_target and self.profit_total >= self.config.profit_target:
            self.state.update_status(f"Profit target reached: ${self.profit_total:.2f}")
            self._stop_event.set()
            return False
        
        return True

    def start_monitoring(self) -> bool:
        if not self.capture_mgr.is_fully_calibrated():
            return False
        if not self.capture_mgr.validate_calibration():
            self.state.update_status("Calibration validation failed - points off-screen")
            return False
        if self._monitor_thread and self._monitor_thread.is_alive():
            return True
        self._stop_event.clear()
        self._clear_pending_bet()
        self._clear_weekly_idle_auto_resume()
        self._awaiting_cycle_reset_sync = self._should_start_unsynced()
        self._reset_board_idle_tracker()
        self._arm_brave_window_lock("monitor")
        self.state.state = AutomationState.MONITORING
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="MonitorThread", daemon=True)
        self._monitor_thread.start()
        return True
    
    def stop_all(self, preserve_soft_idle_runtime: bool = False):
        self._stop_event.set()
        self._stop_worker_threads()
        self._simulate_halted = False
        self._awaiting_cycle_reset_sync = False
        self._blank_reference_visible_until = 0.0
        self._cycle_reset_candidate_since = None
        self._clear_weekly_idle_auto_resume()
        self._clear_brave_window_lock()
        self._reset_board_idle_tracker()
        self.state.set_monitor_idle_until(0)
        self.state.state = AutomationState.IDLE
        if not preserve_soft_idle_runtime:
            self._soft_idle_runtime_started_at = None
            self._soft_idle_triggered_for_run = False
        self._save_session_state()

    def reset_records(self):
        self.stop_all()
        self.last_bet_color = None
        self.last_bet_value = None
        self.last_bet_amount = None
        self.last_bet_clicked_at = None
        self.last_result = None
        self.last_c1_boxes = []
        self.c1_history = []
        self.pending_round_number = 1
        self.round_count = 0
        self.detected_round_count = 0
        self.display_round_counter = 0
        self._last_display_round_signature = None
        self.win_count = 0
        self.lose_count = 0
        self.profit_total = 0.0
        self.loss_streak = 0
        self.martingale_index = 0
        self.bet_mode = "martingale"
        self.last_prediction = "None"
        self.last_decision_reason = "Waiting for C1 history"
        self.last_decision_csv_reason = "WAIT_HISTORY"
        self.last_decision_score = 0.0
        self.last_decision_gap = 0.0
        self.last_decision_probability = 0.0
        self.last_expected_value = 0.0
        self.last_probability_edge = 0.0
        self.last_probability_samples = 0
        self.last_skip_reason = ""
        self.last_skip_csv_reason = ""
        self.last_regime = "RANGE"
        self.last_regime_reason = "Waiting for enough history"
        self.last_history_swap_hint = ""
        self._soft_idle_runtime_started_at = None
        self._soft_idle_triggered_for_run = False
        self._scheduled_idle_trigger_date = None
        self._scheduled_idle_waiting_for_win = False
        self._soft_idle_waiting_for_win = False
        self._scheduled_idle_waiting_for_profit = False
        self._clear_weekly_idle_auto_resume()
        self._blank_reference_visible_until = 0.0
        self._cycle_reset_candidate_since = None
        self._reset_board_idle_tracker()
        self.start_time = time.time()
        self.total_clicks = 0
        self.decision_history = []
        self.decision_stats = DecisionStats()
        self.state.set_game_state(None)
        self.state.set_monitor_idle_until(0)
        self.state.update_status("Records reset. Status set to Idle.")
        
        if self.app and hasattr(self.app, 'config_dir'):
            session_path = self.app.config_dir / "session_backup.json"
            if session_path.exists():
                try:
                    session_path.unlink()
                except OSError as exc:
                    logger.warning("Failed to remove session backup %s: %s", session_path, exc)

    def start_simulation(
        self,
        preserve_sync_handoff: bool = False,
        inherited_sync_wait: Optional[bool] = None,
    ) -> bool:
        if not self.capture_mgr.is_fully_calibrated():
            return False
        if not self.capture_mgr.validate_calibration():
            self.state.update_status("Calibration validation failed - points off-screen")
            return False
        if self._simulate_thread and self._simulate_thread.is_alive() and not self._stop_event.is_set():
            return True
        if self._simulate_thread and self._simulate_thread.is_alive():
            self._simulate_thread.join(timeout=2)
        self._stop_event.clear()
        self._simulate_halted = False
        self._clear_pending_bet()
        self._clear_weekly_idle_auto_resume()
        self._mark_soft_idle_runtime_start(preserve_existing=inherited_sync_wait is not None)
        if inherited_sync_wait is None:
            self._awaiting_cycle_reset_sync = self._resolve_startup_sync_wait(
                preserve_sync_handoff=preserve_sync_handoff
            )
        else:
            self._awaiting_cycle_reset_sync = inherited_sync_wait
        self._reset_board_idle_tracker()
        self._arm_brave_window_lock("simulate")
        self.state.state = AutomationState.SIMULATING
        self._simulate_thread = threading.Thread(target=self._simulate_round, name="SimulateThread", daemon=True)
        self._simulate_thread.start()
        return True

    def start_autoclick(
        self,
        preserve_sync_handoff: bool = False,
        inherited_sync_wait: Optional[bool] = None,
    ) -> bool:
        if not self.capture_mgr.is_fully_calibrated():
            return False
        if not self.capture_mgr.validate_calibration():
            self.state.update_status("Calibration validation failed - points off-screen")
            return False
        if self._autoclick_thread and self._autoclick_thread.is_alive() and not self._stop_event.is_set():
            return True
        if self._autoclick_thread and self._autoclick_thread.is_alive():
            self._autoclick_thread.join(timeout=2)
        self._stop_event.clear()
        self._simulate_halted = False
        self._clear_pending_bet()
        self._clear_weekly_idle_auto_resume()
        self._mark_soft_idle_runtime_start(preserve_existing=inherited_sync_wait is not None)
        if inherited_sync_wait is None:
            self._awaiting_cycle_reset_sync = self._resolve_startup_sync_wait(
                preserve_sync_handoff=preserve_sync_handoff
            )
        else:
            self._awaiting_cycle_reset_sync = inherited_sync_wait
        self._reset_board_idle_tracker()
        self._arm_brave_window_lock("autoclick")
        self.state.state = AutomationState.AUTOCLICKING
        self._autoclick_thread = threading.Thread(target=self._autoclick_loop, name="AutoClickThread", daemon=True)
        self._autoclick_thread.start()
        return True

    def start_autosim(
        self,
        preserve_sync_handoff: bool = False,
        inherited_sync_wait: Optional[bool] = None,
    ) -> bool:
        if not self.capture_mgr.is_fully_calibrated():
            return False
        if not self.capture_mgr.validate_calibration():
            self.state.update_status("Calibration validation failed - points off-screen")
            return False
        if self._autosim_thread and self._autosim_thread.is_alive() and not self._stop_event.is_set():
            return True
        if self._autosim_thread and self._autosim_thread.is_alive():
            self._autosim_thread.join(timeout=2)
        self._stop_event.clear()
        self._simulate_halted = False
        self._clear_pending_bet()
        self._clear_weekly_idle_auto_resume()
        self._mark_soft_idle_runtime_start(preserve_existing=inherited_sync_wait is not None)
        if inherited_sync_wait is None:
            self._awaiting_cycle_reset_sync = self._resolve_startup_sync_wait(
                preserve_sync_handoff=preserve_sync_handoff
            )
        else:
            self._awaiting_cycle_reset_sync = inherited_sync_wait
        self._reset_board_idle_tracker()
        self._arm_brave_window_lock("autosim")
        self.state.state = AutomationState.AUTOSIMULATING
        self._autosim_thread = threading.Thread(target=self._autosim_loop, name="AutoSimThread", daemon=True)
        self._autosim_thread.start()
        return True
    
    def _get_confidence_level(
        self,
        score: float,
        gap: float,
        decision_probability: float = 0.0,
        expected_value: float = 0.0,
    ) -> str:
        if decision_probability >= 0.62 and expected_value >= 0.25:
            return "HIGH"
        elif decision_probability >= self.config.strategy_min_hit_probability and expected_value >= self.config.strategy_min_expected_value:
            return "MEDIUM"
        elif decision_probability > 0 or score > 0 or gap > 0:
            return "LOW"
        return "SKIP"
    
    def _calculate_color_scores(self, game_state: GameState) -> Dict[str, float]:
        if not game_state or not game_state.columns:
            return {}
        
        scores = {color: 0.0 for color in self.config.color_labels}
        column_weights = (1.60, 1.25, 0.95, 0.75, 0.55, 0.40, 0.28)
        recent_focus = {0: 0.55, 1: 0.30, 2: 0.15}
        
        for column_index, column in enumerate(game_state.columns[:self.config.total_columns]):
            counts = Counter(color for color in column.boxes if color in self.config.color_labels)
            if not counts:
                continue
            
            base_weight = column_weights[min(column_index, len(column_weights) - 1)]
            for color, count in counts.items():
                scores[color] += base_weight * count
                if count >= 2:
                    scores[color] += base_weight * 0.35 * (count - 1)
                if column_index in recent_focus:
                    scores[color] += recent_focus[column_index] * count
        
        recent_c1 = self._get_effective_round_history(game_state, limit=4)
        history_weights = (0.90, 0.65, 0.45, 0.30)
        for history_index, c1_boxes in enumerate(reversed(recent_c1)):
            counts = Counter(color for color in c1_boxes if color in self.config.color_labels)
            history_weight = history_weights[min(history_index, len(history_weights) - 1)]
            for color, count in counts.items():
                scores[color] += history_weight * count
        
        return scores

    def _build_board_signal_snapshot(self, game_state: Optional[GameState]) -> Dict:
        scores = self._calculate_color_scores(game_state) if game_state else {}
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_color = ranked[0][0] if ranked else None
        top_score = ranked[0][1] if ranked else 0.0
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        gap = top_score - second_score

        top_recent_hits = 0
        if game_state and game_state.columns and top_color:
            top_recent_hits = sum(1 for column in game_state.columns[:3] if top_color in column.boxes)

        return {
            "scores": scores,
            "ranked": ranked,
            "top_color": top_color,
            "top_score": top_score,
            "second_score": second_score,
            "gap": gap,
            "top_recent_hits": top_recent_hits,
        }

    def _build_regime_info(self, game_state: Optional[GameState], board_snapshot: Dict) -> Dict[str, object]:
        scores = board_snapshot.get("scores") or {}
        if not scores:
            return {
                "mode": "DATA",
                "reason": "Data-driven model only",
                "dominant_ratio": 0.0,
                "change_rate": 0.0,
                "entropy": 0.0,
            }

        priors = self._softmax_color_priors(scores)
        dominant_ratio = max(priors.values()) if priors else 0.0
        entropy = 0.0
        if priors:
            base = math.log(max(2, len(priors)))
            entropy_raw = -sum(value * math.log(max(value, 1e-9)) for value in priors.values())
            entropy = entropy_raw / base if base > 0 else 0.0

        recent_history = self._get_effective_round_history(
            game_state,
            limit=max(6, self.config.strategy_probability_window),
        )
        leaders: List[str] = []
        for boxes in recent_history:
            counts = Counter(color for color in boxes if color in self.config.color_labels)
            if counts:
                leaders.append(counts.most_common(1)[0][0])
        change_rate = 0.0
        if len(leaders) > 1:
            leader_changes = sum(1 for left, right in zip(leaders, leaders[1:]) if left != right)
            change_rate = leader_changes / max(1, len(leaders) - 1)

        top_color = board_snapshot.get("top_color") or "N/A"
        if dominant_ratio >= 0.40 and entropy <= 0.82 and change_rate <= 0.45:
            return {
                "mode": "TREND",
                "reason": (
                    f"Directional board: {top_color} at {dominant_ratio * 100:.0f}%, "
                    f"entropy {entropy:.2f}, leader changes {change_rate * 100:.0f}%"
                ),
                "dominant_ratio": dominant_ratio,
                "change_rate": change_rate,
                "entropy": entropy,
            }
        if entropy >= 0.95 and dominant_ratio <= 0.27 and change_rate >= 0.70:
            return {
                "mode": "CHAOS",
                "reason": (
                    f"High entropy {entropy:.2f}, weak dominance {dominant_ratio * 100:.0f}%, "
                    f"leader changes {change_rate * 100:.0f}%"
                ),
                "dominant_ratio": dominant_ratio,
                "change_rate": change_rate,
                "entropy": entropy,
            }
        return {
            "mode": "RANGE",
            "reason": (
                f"Balanced board: {top_color} at {dominant_ratio * 100:.0f}%, "
                f"entropy {entropy:.2f}, leader changes {change_rate * 100:.0f}%"
            ),
            "dominant_ratio": dominant_ratio,
            "change_rate": change_rate,
            "entropy": entropy,
        }

    def _softmax_color_priors(self, scores: Dict[str, float], temperature: float = 3.0) -> Dict[str, float]:
        if not scores:
            return {}

        max_score = max(scores.values())
        exp_values = {
            color: math.exp((score - max_score) / max(0.5, temperature))
            for color, score in scores.items()
        }
        total = sum(exp_values.values())
        if total <= 0:
            uniform = 1.0 / max(1, len(scores))
            return {color: uniform for color in scores}
        return {color: value / total for color, value in exp_values.items()}

    def _build_probability_snapshot(
        self,
        game_state: Optional[GameState],
        regime_info: Optional[Dict[str, object]] = None,
        board_weight_override: Optional[float] = None,
    ) -> Dict[str, object]:
        board_snapshot = self._build_board_signal_snapshot(game_state)
        board_priors = self._softmax_color_priors(board_snapshot["scores"])
        window = max(6, self.config.strategy_probability_window)
        history_rounds = self._get_effective_round_history(game_state, limit=window)
        sample_size = len(history_rounds)
        weighted_total = 0.0
        board_box_total = max(1, self.config.total_columns * self.config.boxes_per_column)
        front_box_total = max(1, min(3, self.config.total_columns) * self.config.boxes_per_column)
        active_board_weight = (
            float(board_weight_override)
            if board_weight_override is not None
            else float(self.config.strategy_probability_board_weight)
        )
        smoothing = 0.75
        models = []

        recent_weights: List[float] = []
        if history_rounds:
            denominator = max(1, len(history_rounds) - 1)
            for index in range(len(history_rounds)):
                progress = index / denominator if denominator else 1.0
                recent_weights.append(0.65 + (0.85 * progress))
            weighted_total = sum(recent_weights)

        for color in self.config.color_labels:
            counts = [smoothing, smoothing, smoothing, smoothing]

            for round_boxes, weight in zip(history_rounds, recent_weights):
                hits = sum(1 for box in round_boxes if box == color)
                counts[min(3, hits)] += weight

            probabilities = [count / sum(counts) for count in counts]
            board_prior = board_priors.get(color, 1.0 / max(1, len(self.config.color_labels)))

            board_boxes = 0
            front_boxes = 0
            recent_hits = 0
            if game_state and game_state.columns:
                board_boxes = sum(
                    1
                    for column in game_state.columns[:self.config.total_columns]
                    for box in column.boxes
                    if box == color
                )
                front_boxes = sum(
                    1
                    for column in game_state.columns[:3]
                    for box in column.boxes
                    if box == color
                )
                recent_hits = sum(1 for column in game_state.columns[:3] if color in column.boxes)

            board_support = (
                (board_prior * 0.55)
                + ((front_boxes / front_box_total) * 0.30)
                + ((board_boxes / board_box_total) * 0.15)
            )
            uniform_support = 1.0 / max(1, len(self.config.color_labels))
            boost = 1.0 + ((board_support / uniform_support) - 1.0) * active_board_weight
            boost = max(0.55, min(2.40, boost))

            adjusted = [
                probabilities[0] / max(0.65, boost),
                probabilities[1] * (0.92 + (0.08 * boost)),
                probabilities[2] * (0.80 + (0.20 * boost)),
                probabilities[3] * (0.65 + (0.35 * boost)),
            ]
            adjusted_total = sum(adjusted) or 1.0
            adjusted = [value / adjusted_total for value in adjusted]

            hit_probability = 1.0 - adjusted[0]
            double_probability = adjusted[2] + adjusted[3]
            triple_probability = adjusted[3]
            average_hits = adjusted[1] + (2.0 * adjusted[2]) + (3.0 * adjusted[3])
            expected_value = average_hits - adjusted[0]

            models.append({
                "color": color,
                "distribution": adjusted,
                "empirical_distribution": probabilities,
                "board_prior": board_prior,
                "board_support": board_support,
                "boost": boost,
                "board_boxes": board_boxes,
                "front_boxes": front_boxes,
                "top_recent_hits": recent_hits,
                "hit_probability": hit_probability,
                "double_probability": double_probability,
                "triple_probability": triple_probability,
                "average_hits": average_hits,
                "expected_value": expected_value,
            })

        ranked = sorted(
            models,
            key=lambda item: (item["expected_value"], item["hit_probability"], item["average_hits"]),
            reverse=True,
        )
        top_model = ranked[0] if ranked else None
        second_model = ranked[1] if len(ranked) > 1 else None

        probability_edge = 0.0
        hit_edge = 0.0
        if top_model and second_model:
            probability_edge = top_model["expected_value"] - second_model["expected_value"]
            hit_edge = top_model["hit_probability"] - second_model["hit_probability"]

        return {
            "sample_size": sample_size,
            "weighted_total": weighted_total,
            "regime": regime_info["mode"] if regime_info else "",
            "board_weight": active_board_weight,
            "board_snapshot": board_snapshot,
            "ranked": ranked,
            "top_model": top_model,
            "second_model": second_model,
            "probability_edge": probability_edge,
            "hit_edge": hit_edge,
        }

    def _get_data_driven_thresholds(
        self,
        regime_info: Optional[Dict[str, object]] = None,
        amount: Optional[int] = None,
        cycle_counter: Optional[int] = None,
    ) -> Dict[str, float]:
        thresholds = {
            "min_score": self.config.strategy_min_score,
            "min_gap": self.config.strategy_min_gap,
            "recent_columns": self.config.strategy_recent_columns_required,
            "min_hit_probability": self.config.strategy_min_hit_probability,
            "min_expected_value": self.config.strategy_min_expected_value,
            "min_probability_edge": self.config.strategy_min_probability_edge,
            "min_probability_samples": self.config.strategy_probability_min_samples,
            "board_weight": self.config.strategy_probability_board_weight,
            "allow_only_strong_signal": False,
            "amount_band": self._amount_band_for_target_amount(amount),
            "amount_value": int(amount) if amount is not None else 0,
            "skip_amount_bands": set(),
            "amount_band_blocked": False,
            "amount_block_reason": "",
        }
        model = self.history_strategy_model
        regime_name = str((regime_info or {}).get("mode", "") or "")
        if model and regime_name and amount is not None and not self.learning_mode:
            thresholds.update(
                model.coordinated_probability_thresholds(
                    base_thresholds=thresholds,
                    regime=regime_name,
                    amount=int(amount),
                    cycle_counter=cycle_counter,
                )
            )

        if regime_name == "CHAOS":
            thresholds["amount_band_blocked"] = True
            if not thresholds.get("amount_block_reason"):
                thresholds["amount_block_reason"] = "CHAOS_SKIP_ALL"

        if not thresholds.get("amount_band_blocked", False):
            amount_band = str(thresholds.get("amount_band", "LOW"))
            skip_amount_bands = thresholds.get("skip_amount_bands") or set()
            thresholds["amount_band_blocked"] = amount_band in skip_amount_bands
            if thresholds["amount_band_blocked"] and not thresholds.get("amount_block_reason"):
                thresholds["amount_block_reason"] = f"SKIP_BAND_{amount_band}"

        if self.consecutive_strategy_skips >= 50 and not thresholds.get("amount_band_blocked", False):
            relief_steps = min(3, 1 + ((self.consecutive_strategy_skips - 50) // 25))
            thresholds["min_hit_probability"] = max(
                0.40,
                float(thresholds["min_hit_probability"]) - (0.015 * relief_steps),
            )
            thresholds["min_expected_value"] = max(
                -0.02,
                float(thresholds["min_expected_value"]) - (0.015 * relief_steps),
            )
            thresholds["min_probability_edge"] = max(
                0.01,
                float(thresholds["min_probability_edge"]) - (0.006 * relief_steps),
            )
            thresholds["min_probability_samples"] = max(
                4,
                int(thresholds["min_probability_samples"]) - min(2, relief_steps),
            )
            thresholds["board_weight"] = min(
                0.75,
                float(thresholds.get("board_weight", self.config.strategy_probability_board_weight)) + (0.03 * relief_steps),
            )
            thresholds["skip_relief_active"] = True
            thresholds["skip_relief_steps"] = relief_steps
        else:
            thresholds["skip_relief_active"] = False
            thresholds["skip_relief_steps"] = 0

        return thresholds

    def _build_compact_decision_reason(
        self,
        chosen_color: Optional[str],
        regime: str,
        score: float,
        gap: float,
        decision_probability: float,
        expected_value: float,
        probability_edge: float,
        probability_samples: int,
        thresholds: Dict[str, float],
        runner_up: Optional[str] = None,
    ) -> str:
        if chosen_color:
            return (
                f"PICK|{regime}|{chosen_color}|P{decision_probability:.2f}"
                f"|EV{expected_value:.2f}|E{probability_edge:.2f}"
            )

        if probability_samples < thresholds["min_probability_samples"]:
            return (
                f"SKIP|{regime}|SAMPLES|{probability_samples}"
                f"<{thresholds['min_probability_samples']}"
            )
        if thresholds.get("amount_band_blocked", False):
            block_reason = thresholds.get("amount_block_reason", "") or "AMOUNT"
            return f"SKIP|{regime}|{block_reason}|{thresholds.get('amount_value', 0)}"
        if decision_probability < thresholds["min_hit_probability"]:
            return (
                f"SKIP|{regime}|PROB|{decision_probability:.2f}"
                f"<{thresholds['min_hit_probability']:.2f}"
            )
        if expected_value < thresholds["min_expected_value"]:
            return (
                f"SKIP|{regime}|EV|{expected_value:.2f}"
                f"<{thresholds['min_expected_value']:.2f}"
            )
        if probability_edge < thresholds["min_probability_edge"]:
            return (
                f"SKIP|{regime}|EDGE|{probability_edge:.2f}"
                f"<{thresholds['min_probability_edge']:.2f}|{runner_up or 'N/A'}"
            )
        if thresholds["allow_only_strong_signal"]:
            return (
                f"SKIP|{regime}|CHAOS_GATE|P{decision_probability:.2f}"
                f"|EV{expected_value:.2f}"
            )
        return f"SKIP|{regime}|OTHER|S{score:.2f}|G{gap:.2f}"

    def _record_decision(self, game_state: GameState, chosen_color: Optional[str],
                         amount: int, reason: str, reaction_time_ms: float = 0,
                         regime: str = "UNKNOWN", regime_reason: str = "",
                         active_thresholds: Optional[Dict[str, float]] = None,
                         probability_snapshot: Optional[Dict[str, object]] = None):
        snapshot = (probability_snapshot or {}).get("board_snapshot") if probability_snapshot else None
        if not snapshot:
            snapshot = self._build_board_signal_snapshot(game_state)
        top_score = snapshot["top_score"]
        gap = snapshot["gap"]
        active_thresholds = active_thresholds or {}
        top_model = (probability_snapshot or {}).get("top_model") or {}
        decision_probability = float(top_model.get("hit_probability", 0.0))
        expected_value = float(top_model.get("expected_value", 0.0))
        probability_edge = float((probability_snapshot or {}).get("probability_edge", 0.0))
        probability_samples = int((probability_snapshot or {}).get("sample_size", 0))
        
        decision = DecisionRecord(
            timestamp=time.time(),
            round_number=self.pending_round_number,
            predicted_color=chosen_color,
            actual_c1_boxes=list(game_state.columns[0].boxes) if game_state and game_state.columns else [],
            bet_amount=amount if chosen_color else 0,
            decision_score=top_score,
            decision_gap=gap,
            confidence_level=self._get_confidence_level(
                top_score,
                gap,
                decision_probability=decision_probability,
                expected_value=expected_value,
            ),
            decision_reason=reason,
            regime=regime,
            regime_reason=regime_reason,
            active_min_score=active_thresholds.get("min_score", self.config.strategy_min_score),
            active_min_gap=active_thresholds.get("min_gap", self.config.strategy_min_gap),
            active_recent_columns=active_thresholds.get("recent_columns", self.config.strategy_recent_columns_required),
            strong_signal_only=bool(active_thresholds.get("allow_only_strong_signal", False)),
            decision_probability=decision_probability,
            expected_value=expected_value,
            probability_edge=probability_edge,
            probability_samples=probability_samples,
            outcome="PENDING",
            reaction_time_ms=reaction_time_ms
        )
        
        self.decision_history.append(decision)
        self.decision_stats.total_decisions += 1
        
        if chosen_color:
            self.consecutive_strategy_skips = 0
            self.decision_stats.bets_placed += 1
            if decision.confidence_level == "HIGH":
                self.decision_stats.high_conf_bets += 1
            elif decision.confidence_level == "MEDIUM":
                self.decision_stats.medium_conf_bets += 1
            else:
                self.decision_stats.low_conf_bets += 1
        else:
            self.consecutive_strategy_skips += 1
            self.decision_stats.bets_skipped += 1
        
        if len(self.decision_history) > 1000:
            self.decision_history = self.decision_history[-1000:]
        
        if self.learning_mode and self.decision_stats.total_decisions % 50 == 0 and self.decision_stats.total_decisions > 0:
            self._adjust_thresholds()
        
    
    def _adjust_thresholds(self):
        if len(self.decision_history) < 50:
            return
        
        recent = self.decision_history[-50:]
        recent_bets = [d for d in recent if d.predicted_color]
        skip_rate = (len(recent) - len(recent_bets)) / len(recent) if recent else 0.0
        
        if len(recent_bets) < 20:
            return
        
        high_conf_bets = [d for d in recent_bets if d.confidence_level == "HIGH"]
        med_conf_bets = [d for d in recent_bets if d.confidence_level == "MEDIUM"]
        low_conf_bets = [d for d in recent_bets if d.confidence_level == "LOW"]
        close_edge_bets = [
            d for d in recent_bets
            if d.probability_edge <= self.config.strategy_min_probability_edge + 0.02
        ]
        
        high_conf_wins = len([d for d in high_conf_bets if d.outcome == "WIN"])
        med_conf_wins = len([d for d in med_conf_bets if d.outcome == "WIN"])
        low_conf_wins = len([d for d in low_conf_bets if d.outcome == "WIN"])
        close_edge_wins = len([d for d in close_edge_bets if d.outcome == "WIN"])
        
        high_win_rate = high_conf_wins / len(high_conf_bets) if high_conf_bets else 0
        med_win_rate = med_conf_wins / len(med_conf_bets) if med_conf_bets else 0
        low_win_rate = low_conf_wins / len(low_conf_bets) if low_conf_bets else 0
        close_edge_win_rate = close_edge_wins / len(close_edge_bets) if close_edge_bets else 0
        
        old_min_probability = self.config.strategy_min_hit_probability
        old_min_expected_value = self.config.strategy_min_expected_value
        old_min_probability_edge = self.config.strategy_min_probability_edge

        if high_win_rate > 0.62 and med_win_rate > 0.52:
            self.config.strategy_min_hit_probability = min(0.70, self.config.strategy_min_hit_probability + 0.01)
            logger.info(
                " Increasing min_hit_probability to %.2f (high-confidence bets performing well)",
                self.config.strategy_min_hit_probability,
            )
        elif high_win_rate < 0.45 and med_win_rate < 0.42:
            self.config.strategy_min_hit_probability = min(0.75, self.config.strategy_min_hit_probability + 0.02)
            logger.info(
                " Tightening min_hit_probability to %.2f (high-confidence bets underperform)",
                self.config.strategy_min_hit_probability,
            )
        elif skip_rate > 0.72 and high_win_rate > 0.58:
            self.config.strategy_min_hit_probability = max(0.32, self.config.strategy_min_hit_probability - 0.01)
            logger.info(
                " Relaxing min_hit_probability to %.2f (skip rate %.1f%%)",
                self.config.strategy_min_hit_probability,
                skip_rate * 100,
            )

        if len(close_edge_bets) >= 12:
            if close_edge_win_rate < 0.45:
                self.config.strategy_min_probability_edge = min(0.18, self.config.strategy_min_probability_edge + 0.01)
                logger.info(
                    " Increasing min_probability_edge to %.2f (close probability races underperform)",
                    self.config.strategy_min_probability_edge,
                )
            elif close_edge_win_rate > 0.58 and skip_rate > 0.35:
                self.config.strategy_min_probability_edge = max(0.01, self.config.strategy_min_probability_edge - 0.01)
                logger.info(
                    " Decreasing min_probability_edge to %.2f (close races holding up)",
                    self.config.strategy_min_probability_edge,
                )

        average_expected_value = statistics.mean(d.expected_value for d in recent_bets) if recent_bets else 0.0
        if average_expected_value < 0.02 and high_win_rate < 0.48:
            self.config.strategy_min_expected_value = min(0.40, self.config.strategy_min_expected_value + 0.02)
            logger.info(
                " Increasing min_expected_value to %.2f (expected edge not converting)",
                self.config.strategy_min_expected_value,
            )
        elif average_expected_value > 0.18 and skip_rate > 0.70:
            self.config.strategy_min_expected_value = max(-0.05, self.config.strategy_min_expected_value - 0.01)
            logger.info(
                " Decreasing min_expected_value to %.2f (strong edge with heavy skipping)",
                self.config.strategy_min_expected_value,
            )

        self.config.strategy_min_hit_probability = max(0.30, min(0.75, self.config.strategy_min_hit_probability))
        self.config.strategy_min_expected_value = max(-0.05, min(0.40, self.config.strategy_min_expected_value))
        self.config.strategy_min_probability_edge = max(0.01, min(0.18, self.config.strategy_min_probability_edge))

        changes = []
        if abs(old_min_probability - self.config.strategy_min_hit_probability) > 0.001:
            changes.append(
                f"min_hit_prob {old_min_probability:.2f} -> {self.config.strategy_min_hit_probability:.2f}"
            )
        if abs(old_min_expected_value - self.config.strategy_min_expected_value) > 0.001:
            changes.append(
                f"min_ev {old_min_expected_value:.2f} -> {self.config.strategy_min_expected_value:.2f}"
            )
        if abs(old_min_probability_edge - self.config.strategy_min_probability_edge) > 0.001:
            changes.append(
                f"min_edge {old_min_probability_edge:.2f} -> {self.config.strategy_min_probability_edge:.2f}"
            )

        if changes:
            self.state.update_status(" Adaptive threshold adjusted: " + ", ".join(changes))
            if self.app:
                self.app.config_dirty = True
    
    def _update_decision_outcome(self, round_number: int, c1_boxes: List[str], 
                                  multiplier: int, profit_change: float):
        for decision in self.decision_history:
            if decision.round_number == round_number:
                decision.outcome = "WIN" if multiplier > 0 else "LOSE"
                decision.multiplier = multiplier
                decision.profit_change = profit_change
                decision.actual_c1_boxes = c1_boxes
                
                if decision.confidence_level == "HIGH" and decision.outcome == "WIN":
                    self.decision_stats.high_conf_wins += 1
                elif decision.confidence_level == "MEDIUM" and decision.outcome == "WIN":
                    self.decision_stats.medium_conf_wins += 1
                elif decision.confidence_level == "LOW" and decision.outcome == "WIN":
                    self.decision_stats.low_conf_wins += 1
                break

    def _write_round_decision_log(
        self,
        round_number: int,
        outcome_override: Optional[str] = None,
        skip_reason: str = "",
        result_boxes_override: Optional[List[str]] = None,
    ):
        if not self.app or not hasattr(self.app, "event_logger"):
            return

        decision = next((d for d in self.decision_history if d.round_number == round_number), None)
        result_boxes = list(result_boxes_override) if result_boxes_override is not None else list(self.last_c1_boxes)
        payload = {
            "round_number": round_number,
            "mode": self.state.state.value if self.state.state else "unknown",
            "prediction": self.last_prediction,
            "decision_reason": self.last_decision_reason,
            "decision_csv_reason": self.last_decision_csv_reason,
            "skip_reason": skip_reason or self.last_skip_csv_reason,
            "regime": self.last_regime,
            "regime_reason": self.last_regime_reason,
            "decision_probability": self.last_decision_probability,
            "expected_value": self.last_expected_value,
            "probability_edge": self.last_probability_edge,
            "probability_samples": self.last_probability_samples,
            "last_result_boxes": result_boxes,
            "pending_bet_color": self.last_bet_color,
            "pending_bet_amount": self.last_bet_amount,
            "profit_total": self.profit_total,
            "loss_streak": self.loss_streak,
            "outcome": outcome_override or (decision.outcome if decision else "UNKNOWN"),
        }

        if decision:
            payload.update({
                "predicted_color": decision.predicted_color,
                "bet_amount": decision.bet_amount,
                "decision_score": decision.decision_score,
                "decision_gap": decision.decision_gap,
                "confidence_level": decision.confidence_level,
                "actual_c1_boxes": list(decision.actual_c1_boxes),
                "multiplier": decision.multiplier,
                "profit_change": decision.profit_change,
            })

        self.app.event_logger.log_decision(**payload)
    
    def _predict_color_from_game_state(
        self,
        game_state: Optional[GameState],
        amount: Optional[int] = None,
    ) -> Tuple[Optional[str], str, Dict, Dict[str, object]]:
        if not game_state or not game_state.columns:
            regime_info = {
                "mode": "DATA",
                "reason": "Data-driven model only",
                "dominant_ratio": 0.0,
                "change_rate": 0.0,
                "entropy": 0.0,
            }
            self.last_regime = regime_info["mode"]
            self.last_regime_reason = regime_info["reason"]
            return None, "No stable board data available", regime_info, {"sample_size": 0, "ranked": [], "board_snapshot": {}}

        snapshot = self._build_board_signal_snapshot(game_state)
        if not snapshot["scores"]:
            regime_info = self._build_regime_info(game_state, snapshot)
            self.last_regime = regime_info["mode"]
            self.last_regime_reason = regime_info["reason"]
            return None, "No color data available", regime_info, {"sample_size": 0, "ranked": [], "board_snapshot": snapshot}

        regime_info = self._build_regime_info(game_state, snapshot)
        self.last_regime = regime_info["mode"]
        self.last_regime_reason = regime_info["reason"]
        thresholds = self._get_data_driven_thresholds(
            regime_info=regime_info,
            amount=amount,
            cycle_counter=self.display_round_counter,
        )
        probability_snapshot = self._build_probability_snapshot(
            game_state,
            regime_info,
            board_weight_override=float(
                thresholds.get("board_weight", self.config.strategy_probability_board_weight)
            ),
        )
        top_model = probability_snapshot.get("top_model")
        if not top_model:
            return None, "No probability model available", regime_info, probability_snapshot

        if thresholds.get("amount_band_blocked", False):
            block_reason = thresholds.get("amount_block_reason", "")
            return None, (
                f"Data model: {regime_info['mode']} skips "
                f"{thresholds.get('amount_band', 'UNKNOWN')} amount "
                f"{thresholds.get('amount_value', 0)} and above"
            ), regime_info, probability_snapshot

        if probability_snapshot["sample_size"] < thresholds["min_probability_samples"]:
            return None, (
                "Data model: need more samples "
                f"({probability_snapshot['sample_size']}/{thresholds['min_probability_samples']})"
            ), regime_info, probability_snapshot

        if top_model["hit_probability"] < thresholds["min_hit_probability"]:
            return None, (
                f"Data model: {top_model['color']} hit probability "
                f"{top_model['hit_probability']:.2f} below {thresholds['min_hit_probability']:.2f}"
            ), regime_info, probability_snapshot

        if top_model["expected_value"] < thresholds["min_expected_value"]:
            return None, (
                f"Data model: {top_model['color']} expected value "
                f"{top_model['expected_value']:.2f} below {thresholds['min_expected_value']:.2f}"
            ), regime_info, probability_snapshot

        if probability_snapshot["probability_edge"] < thresholds["min_probability_edge"]:
            return None, (
                f"Data model: {top_model['color']} edge "
                f"{probability_snapshot['probability_edge']:.2f} below {thresholds['min_probability_edge']:.2f}"
            ), regime_info, probability_snapshot

        return top_model["color"], (
            f"Data-driven probability pick {top_model['color']}: "
            f"hit {top_model['hit_probability']:.2f}, EV {top_model['expected_value']:.2f}, "
            f"edge {probability_snapshot['probability_edge']:.2f}"
        ), regime_info, probability_snapshot

    def _apply_history_model_gate(
        self,
        chosen_color: Optional[str],
        reason: str,
        regime_info: Dict[str, object],
        probability_snapshot: Dict[str, object],
        amount: int,
        game_state: Optional[GameState] = None,
    ) -> Tuple[Optional[str], str, Dict[str, object]]:
        model = self.history_strategy_model
        if not model:
            self.last_history_gate_reason = "History model not ready yet"
            return chosen_color, reason, {
                "allow": True,
                "message": self.last_history_gate_reason,
                "support": 0.0,
                "estimated_profit": 0.0,
                "estimated_win_rate": 0.0,
            }

        if not chosen_color:
            self.last_history_gate_reason = "Base data model skipped before history gate"
            return chosen_color, reason, {
                "allow": False,
                "message": self.last_history_gate_reason,
                "support": 0.0,
                "estimated_profit": 0.0,
                "estimated_win_rate": 0.0,
            }

        top_model = probability_snapshot.get("top_model") or {}
        current_result_boxes: Optional[Tuple[str, ...]] = None
        if game_state and game_state.columns and game_state.columns[0].boxes:
            current_result_boxes = tuple(game_state.columns[0].boxes)
        recommendation = model.recommend_positive_color(
            regime=str(regime_info.get("mode", "DATA")),
            amount=amount,
            candidate_colors=tuple(self.config.color_labels),
            preferred_color=chosen_color,
            result_boxes=current_result_boxes,
            cycle_counter=self.display_round_counter,
        )
        selected_color = recommendation.get("selected_color") or chosen_color
        evaluation = recommendation.get("selected_evaluation") or {}
        self.last_history_gate_reason = str(recommendation.get("message", evaluation.get("message", "")))
        if recommendation.get("override", False):
            self.last_history_swap_hint = f"HISTORY_SWAP: {chosen_color} -> {selected_color}"
            return selected_color, f"{reason} | HISTORY_SWAP: {self.last_history_gate_reason}", {
                **evaluation,
                "override": True,
                "base_color": chosen_color,
                "selected_color": selected_color,
                "message": self.last_history_gate_reason,
            }
        self.last_history_swap_hint = ""
        if not evaluation.get("allow", False):
            return None, f"{reason} | HISTORY_BLOCK: {self.last_history_gate_reason}", {
                **evaluation,
                "override": False,
                "base_color": chosen_color,
                "selected_color": None,
                "message": self.last_history_gate_reason,
            }
        return selected_color, f"{reason} | HISTORY_OK: {self.last_history_gate_reason}", {
            **evaluation,
            "override": False,
            "base_color": chosen_color,
            "selected_color": selected_color,
            "message": self.last_history_gate_reason,
        }
    

    def _choose_next_bet(self, game_state: Optional[GameState] = None) -> Tuple[Optional[str], int, Dict]:
        start_time = time.time()
        amount = self._get_current_target_amount()
        self.last_history_swap_hint = ""

        # Data-driven flow:
        # 1) live probability gate picks or skips from recent board history
        # 2) history gate can positively reroute that pick using complete-window
        #    Result Boxes transitions, regime/phase behavior, and amount recovery
        if self.config.strategy_randomize_enabled:
            chosen_color = random.choice(self.config.color_labels)
            reason = f"Randomize mode: chose {chosen_color} with no analysis"
            regime_info = {"mode": "RANDOM", "reason": "Randomize mode enabled"}
            probability_snapshot = {
                "sample_size": 0,
                "probability_edge": 0.0,
                "top_model": {
                    "color": chosen_color,
                    "hit_probability": 0.0,
                    "expected_value": 0.0,
                },
                "second_model": None,
                "board_snapshot": self._build_board_signal_snapshot(game_state),
            }
        else:
            chosen_color, reason, regime_info, probability_snapshot = self._predict_color_from_game_state(
                game_state,
                amount=amount,
            )

        snapshot = probability_snapshot.get("board_snapshot") or self._build_board_signal_snapshot(game_state)
        scores = snapshot["scores"]
        top_score = snapshot["top_score"]
        gap = snapshot["gap"]
        top_model = probability_snapshot.get("top_model") or {}
        decision_probability = float(top_model.get("hit_probability", 0.0))
        expected_value = float(top_model.get("expected_value", 0.0))
        probability_edge = float(probability_snapshot.get("probability_edge", 0.0))
        confidence = self._get_confidence_level(
            top_score,
            gap,
            decision_probability=decision_probability,
            expected_value=expected_value,
        )
        if self.config.strategy_randomize_enabled:
            confidence = "RANDOM"
        
        alternatives = []
        if len(snapshot["ranked"]) > 1:
            for color, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)[1:4]:
                alternatives.append(f"{color}:{score:.1f}")

        active_thresholds = self._get_data_driven_thresholds(
            regime_info=regime_info,
            amount=amount,
            cycle_counter=self.display_round_counter,
        )
        if self.config.strategy_randomize_enabled:
            active_thresholds = {
                **active_thresholds,
                "allow_only_strong_signal": False,
            }
        if active_thresholds.get("amount_band_blocked", False) and not self.config.strategy_randomize_enabled:
            chosen_color = None
            reason = (
                f"{regime_info['mode']} skips "
                f"{active_thresholds.get('amount_band', 'UNKNOWN')} amount "
                f"{active_thresholds.get('amount_value', amount)} and above"
            )
        history_gate = {
            "allow": True,
            "message": "Random mode bypasses history gate" if self.config.strategy_randomize_enabled else "History model not applied",
            "support": 0.0,
            "estimated_profit": 0.0,
            "estimated_win_rate": 0.0,
        }
        if chosen_color and not self.config.strategy_randomize_enabled:
            chosen_color, reason, history_gate = self._apply_history_model_gate(
                chosen_color=chosen_color,
                reason=reason,
                regime_info=regime_info,
                probability_snapshot=probability_snapshot,
                amount=amount,
                game_state=game_state,
            )
        compact_reason = self._build_compact_decision_reason(
            chosen_color=chosen_color,
            regime=regime_info["mode"],
            score=top_score,
            gap=gap,
            decision_probability=decision_probability,
            expected_value=expected_value,
            probability_edge=probability_edge,
            probability_samples=int(probability_snapshot.get("sample_size", 0)),
            thresholds=active_thresholds,
            runner_up=(probability_snapshot.get("second_model") or {}).get("color"),
        )
        if active_thresholds.get("skip_relief_active"):
            compact_reason = f"{compact_reason}|RELIEF{active_thresholds.get('skip_relief_steps', 0)}"
        
        decision_metadata = {
            "score": top_score,
            "gap": gap,
            "decision_probability": decision_probability,
            "expected_value": expected_value,
            "probability_edge": probability_edge,
            "probability_samples": int(probability_snapshot.get("sample_size", 0)),
            "confidence": confidence,
            "alternatives": alternatives,
            "martingale_step": self.martingale_index + 1,
            "bet_mode": self.bet_mode,
            "loss_streak": self.loss_streak,
            "recent_win_rate": self.win_count / self.round_count if self.round_count > 0 else 0,
            "regime": regime_info["mode"],
            "regime_reason": regime_info["reason"],
            "active_min_score": active_thresholds["min_score"],
            "active_min_gap": active_thresholds["min_gap"],
            "active_recent_columns": active_thresholds["recent_columns"],
            "regime_strong_signal_only": active_thresholds["allow_only_strong_signal"],
            "compact_reason": compact_reason,
            "history_gate": history_gate,
        }
        if not self.config.strategy_randomize_enabled:
            logger.info(
                "Thresholds: regime=%s amount=%s/%s hit=%.2f ev=%.2f edge=%.2f samples>=%s board_weight=%.2f blocked=%s reason=%s history_support=%.0f",
                regime_info["mode"],
                active_thresholds.get("amount_band", "UNKNOWN"),
                active_thresholds.get("amount_value", amount),
                active_thresholds["min_hit_probability"],
                active_thresholds["min_expected_value"],
                active_thresholds["min_probability_edge"],
                active_thresholds["min_probability_samples"],
                float(active_thresholds.get("board_weight", self.config.strategy_probability_board_weight)),
                active_thresholds.get("amount_band_blocked", False),
                active_thresholds.get("amount_block_reason", "") or "NONE",
                float(history_gate.get("support", 0.0)),
            )
        
        reaction_time = (time.time() - start_time) * 1000
        
        if game_state:
            self._record_decision(
                game_state,
                chosen_color,
                amount,
                reason,
                reaction_time,
                regime=regime_info["mode"],
                regime_reason=regime_info["reason"],
                active_thresholds=active_thresholds,
                probability_snapshot=probability_snapshot,
            )
        
        if chosen_color:
            if self.config.strategy_randomize_enabled:
                self.last_decision_reason = f"[RANDOM] {reason}"
            else:
                history_hint = f"{self.last_history_swap_hint} | " if self.last_history_swap_hint else ""
                self.last_decision_reason = (
                    f"[{regime_info['mode']}] {history_hint}{reason} | Conf:{confidence} "
                    f"P:{decision_probability:.2f} EV:{expected_value:.2f} "
                    f"| Score:{top_score:.1f} Gap:{gap:.1f} | Alt: {', '.join(alternatives[:2])}"
                )
        else:
            self.last_decision_reason = (
                f"[{regime_info['mode']}] {reason} "
                f"(P:{decision_probability:.2f} EV:{expected_value:.2f} "
                f"Score:{top_score:.1f} Gap:{gap:.1f})"
            )
        self.last_decision_csv_reason = compact_reason
        self.last_skip_csv_reason = compact_reason if not chosen_color else ""
        
        self.last_decision_score = top_score
        self.last_decision_gap = gap
        self.last_decision_probability = decision_probability
        self.last_expected_value = expected_value
        self.last_probability_edge = probability_edge
        self.last_probability_samples = int(probability_snapshot.get("sample_size", 0))
        
        return chosen_color, amount, decision_metadata

    def _record_result_boxes(self, result_boxes: List[str], prefix: str) -> bool:
        self.last_result = "/".join(result_boxes)
        self.last_c1_boxes = list(result_boxes)
        self.c1_history.append(list(result_boxes))
        self.c1_history = self.c1_history[-30:]
        pending_idle_shutdown_message: Optional[str] = None
        
        if not self.last_bet_color:
            return False
        
        self.round_count += 1
        base_stake = self._current_stake_value()
        
        matches = sum(1 for box in result_boxes if box == self.last_bet_color)
        
        if matches == 3:
            multiplier = 3
        elif matches == 2:
            multiplier = 2
        elif matches == 1:
            multiplier = 1
        else:
            multiplier = 0
        
        if multiplier > 0:
            profit_change = base_stake * multiplier
            self.win_count += 1
            self.loss_streak = 0
            self._reset_progression()
            self.profit_total += profit_change
            result = "WIN"
        else:
            profit_change = -base_stake
            self.lose_count += 1
            self.loss_streak += 1
            self.profit_total += profit_change
            result = "LOSE"
            
            effective_loss_limit = self._effective_loss_limit()
            effective_max_loss_streak = self._effective_max_loss_streak()
            if effective_loss_limit is not None and self.profit_total <= -effective_loss_limit:
                pending_idle_shutdown_message = (
                    f"{prefix}: loss limit reached "
                    f"{self.profit_total:.2f} <= -{effective_loss_limit:.2f}. Status set to Idle."
                )
                self._simulate_halted = True
                self._stop_event.set()
            elif self.loss_streak >= effective_max_loss_streak:
                if self._scheduled_idle_waiting_for_profit:
                    pending_idle_shutdown_message = (
                        f"{prefix}: max loss streak reached while waiting for soft idle recovery. Status set to Idle."
                    )
                elif self._soft_idle_waiting_for_win:
                    pending_idle_shutdown_message = (
                        f"{prefix}: max loss streak reached while waiting for soft idle win-clear extension. Status set to Idle."
                    )
                else:
                    self.state.update_status(
                        f"HALTED: max loss streak reached after {self.loss_streak} consecutive losses"
                    )
                    self._simulate_halted = True
                    self._stop_event.set()
            else:
                self.martingale_index = min(self.martingale_index + 1, len(self.bet_amount_steps) - 1)
        
        self._update_decision_outcome(self.round_count, result_boxes, multiplier, profit_change)
        click_time_text = f" | Clicked: {self.last_bet_clicked_at}" if self.last_bet_clicked_at else ""
        
        decision = next((d for d in self.decision_history if d.round_number == self.round_count), None)
        if decision:
            status_msg = (
                f"{prefix} {result}: {self.last_bet_color} vs {result_boxes} "
                f"| {multiplier}x | {decision.confidence_level} conf "
                f"(score:{decision.decision_score:.1f}) | "
                f"Profit: {profit_change:+.2f} (Total: {self.profit_total:.2f})"
                f"{click_time_text}"
            )
        else:
            status_msg = (
                f"{prefix} {result}: {self.last_bet_color} vs {result_boxes} "
                f"| Profit: {profit_change:+.2f}{click_time_text}"
            )
        
        self.state.update_status(status_msg)
        
        self._append_simulation_csv(
            mode=prefix,
            round_number=self.round_count,
            c1_result=self.last_result,
            color_betted=self.last_bet_color,
            amount=self.last_bet_amount or self.last_bet_value or "",
            result=f"{result} (x{multiplier})" if multiplier > 1 else result,
            lose_streak=self.loss_streak if result == "LOSE" else 0,
            multiplier=multiplier,
            profit_change=profit_change,
            total_profit=self.profit_total,
            decision_score=decision.decision_score if decision else 0,
            score_gap=decision.decision_gap if decision else 0,
            confidence=decision.confidence_level if decision else "UNKNOWN",
            regime=decision.regime if decision else self.last_regime,
            regime_reason=decision.regime_reason if decision else self.last_regime_reason,
            active_min_score=decision.active_min_score if decision else self.config.strategy_min_score,
            active_min_gap=decision.active_min_gap if decision else self.config.strategy_min_gap,
            active_recent_columns=decision.active_recent_columns if decision else self.config.strategy_recent_columns_required,
            regime_strong_signal_only=decision.strong_signal_only if decision else False,
            decision_probability=decision.decision_probability if decision else self.last_decision_probability,
            expected_value=decision.expected_value if decision else self.last_expected_value,
            probability_edge=decision.probability_edge if decision else self.last_probability_edge,
            probability_samples=decision.probability_samples if decision else self.last_probability_samples,
            decision_reason=self.last_decision_csv_reason,
            skip_reason=self.last_skip_csv_reason,
        )
        self._write_round_decision_log(self.round_count)
        
        self._save_session_state()
        self.pending_round_number = self.round_count + 1

        if pending_idle_shutdown_message:
            self._request_idle_shutdown(pending_idle_shutdown_message)
        elif self._scheduled_idle_waiting_for_win and self.loss_streak == 0:
            self._request_idle_shutdown(
                f"{prefix}: Wednesday 03:00 mandatory idle extension completed after a win cleared the loss streak. Auto-resume is armed after 05:00 AM.",
                preserve_weekly_auto_resume=True,
            )
        elif self._soft_idle_waiting_for_win and self.loss_streak <= 1:
            if self.profit_total > 0:
                self._request_idle_shutdown(
                    f"{prefix}: soft idle extension completed after loss streak fell to {self.loss_streak} and profit recovered to {self.profit_total:.2f}. Status set to Idle."
                )
            else:
                self._soft_idle_waiting_for_win = False
                self._scheduled_idle_waiting_for_profit = True
                self.state.update_status(
                    f"{prefix}: soft idle streak extension completed, but profit is still {self.profit_total:.2f}. Extending until profit becomes positive, max loss streak hits {self.max_loss_streak}, or Wednesday 03:00 mandatory idle."
                )
                self._save_session_state()
        elif self._scheduled_idle_waiting_for_profit and self.profit_total > 0:
            if self.loss_streak <= 1:
                self._request_idle_shutdown(
                    f"{prefix}: soft idle recovery completed at profit {self.profit_total:.2f} with loss streak {self.loss_streak}. Status set to Idle."
                )
            else:
                self._scheduled_idle_waiting_for_profit = False
                self._soft_idle_waiting_for_win = True
                self.state.update_status(
                    f"{prefix}: soft idle profit recovery completed, but loss streak is still {self.loss_streak}. Extending until a win clears the streak to 1 or lower, then status will switch to Idle."
                )
                self._save_session_state()
        
        if self.round_count % 50 == 0:
            self._log_decision_metrics()
        
        return True
    
    def _log_decision_metrics(self):
        if self.decision_stats.total_decisions == 0:
            return
        
        logger.info("=" * 60)
        logger.info("DECISION QUALITY METRICS")
        logger.info("=" * 60)
        logger.info(f"Total Decisions: {self.decision_stats.total_decisions}")
        logger.info(f"Bets Placed: {self.decision_stats.bets_placed} ({self.decision_stats.bets_placed/self.decision_stats.total_decisions*100:.1f}%)")
        logger.info(f"Bets Skipped: {self.decision_stats.bets_skipped} ({self.decision_stats.bets_skipped/self.decision_stats.total_decisions*100:.1f}%)")
        logger.info(f"")
        logger.info(f"High Confidence: {self.decision_stats.high_conf_bets} bets, {self.decision_stats.high_conf_wins} wins ({self.decision_stats.high_conf_win_rate*100:.1f}%)")
        logger.info(f"Medium Confidence: {self.decision_stats.medium_conf_bets} bets, {self.decision_stats.medium_conf_wins} wins ({self.decision_stats.medium_conf_win_rate*100:.1f}%)")
        logger.info(f"Low Confidence: {self.decision_stats.low_conf_bets} bets, {self.decision_stats.low_conf_wins} wins ({self.decision_stats.low_conf_win_rate*100:.1f}%)")
        logger.info("=" * 60)
        
        if self.app and hasattr(self.app, 'event_logger'):
            self.app.event_logger.log_event(
                "DECISION_METRICS",
                f"Decision quality report at round {self.round_count}",
                total_decisions=self.decision_stats.total_decisions,
                bets_placed=self.decision_stats.bets_placed,
                high_conf_win_rate=f"{self.decision_stats.high_conf_win_rate*100:.1f}%",
                medium_conf_win_rate=f"{self.decision_stats.medium_conf_win_rate*100:.1f}%",
                current_min_hit_probability=self.config.strategy_min_hit_probability,
                current_min_expected_value=self.config.strategy_min_expected_value,
                current_min_probability_edge=self.config.strategy_min_probability_edge,
            )
    
    def get_decision_analytics(self) -> Dict:
        if len(self.decision_history) == 0:
            return {"status": "No decisions recorded yet"}
        
        recent = self.decision_history[-100:]
        recent_bets = [d for d in recent if d.predicted_color]
        reaction_times = [d.reaction_time_ms for d in recent if d.reaction_time_ms > 0]
        regime_counts = Counter(d.regime for d in recent if d.regime)
        active_thresholds = self._get_data_driven_thresholds()
        
        analytics = {
            "total_decisions": self.decision_stats.total_decisions,
            "bets_placed": self.decision_stats.bets_placed,
            "skip_rate": self.decision_stats.bets_skipped / self.decision_stats.total_decisions if self.decision_stats.total_decisions > 0 else 0,
            "high_conf_win_rate": self.decision_stats.high_conf_win_rate,
            "medium_conf_win_rate": self.decision_stats.medium_conf_win_rate,
            "low_conf_win_rate": self.decision_stats.low_conf_win_rate,
            "avg_decision_time_ms": statistics.mean(reaction_times) if reaction_times else 0,
            "avg_decision_probability": statistics.mean(
                d.decision_probability for d in recent_bets
            ) if recent_bets else 0,
            "avg_expected_value": statistics.mean(
                d.expected_value for d in recent_bets
            ) if recent_bets else 0,
            "current_thresholds": {
                "min_score": self.config.strategy_min_score,
                "min_gap": self.config.strategy_min_gap,
                "recent_columns": self.config.strategy_recent_columns_required,
                "min_hit_probability": self.config.strategy_min_hit_probability,
                "min_expected_value": self.config.strategy_min_expected_value,
                "min_probability_edge": self.config.strategy_min_probability_edge,
                "min_probability_samples": self.config.strategy_probability_min_samples,
            },
            "active_regime_thresholds": {
                "min_score": active_thresholds["min_score"],
                "min_gap": active_thresholds["min_gap"],
                "recent_columns": active_thresholds["recent_columns"],
                "min_hit_probability": active_thresholds["min_hit_probability"],
                "min_expected_value": active_thresholds["min_expected_value"],
                "min_probability_edge": active_thresholds["min_probability_edge"],
                "min_probability_samples": active_thresholds["min_probability_samples"],
            },
            "learning_mode": self.learning_mode,
            "active_regime": self.last_regime,
            "active_regime_reason": self.last_regime_reason,
            "regime_counts": dict(regime_counts),
            "consecutive_strategy_skips": self.consecutive_strategy_skips,
            "skip_relief_active": bool(active_thresholds.get("skip_relief_active", False)),
            "skip_relief_steps": int(active_thresholds.get("skip_relief_steps", 0)),
        }
        
        if self.decision_stats.high_conf_win_rate < 0.45 and self.decision_stats.high_conf_bets > 20:
            analytics["suggestion"] = " High confidence bets are weak - tighten min_hit_probability or min_expected_value"
        elif self.decision_stats.high_conf_win_rate > 0.65 and self.decision_stats.high_conf_bets > 20:
            analytics["suggestion"] = " High confidence bets are holding up - probability filter is behaving well"
        elif self.decision_stats.bets_skipped / self.decision_stats.total_decisions > 0.7:
            analytics["suggestion"] = " Skipping many rounds - consider easing min_hit_probability or min_expected_value slightly"
        
        return analytics

    def _current_stake_value(self) -> float:
        if not self.last_bet_amount:
            return 0.0

        amount_text = "".join(ch if ch.isdigit() or ch == "." else " " for ch in self.last_bet_amount)
        try:
            amount = float(amount_text.split()[0])
        except (IndexError, ValueError):
            return 0.0

        return amount

    def _is_100_confidence_round(self, game_state: GameState) -> bool:
        return (
            bool(game_state.columns)
            and not game_state.any_unknown
            and game_state.all_columns_full
            and game_state.confidence_score >= 100
        )

    def _get_simulation_image_path(self) -> Path:
        base_dir = self.app.config_dir if self.app else Path(__file__).resolve().parent
        return base_dir / "simulate_round_detector.png"

    def _get_simulation_csv_path(self) -> Path:
        base_dir = self.app.config_dir if self.app else Path(__file__).resolve().parent
        return base_dir / "simulate_results.csv"

    def _capture_detectable_round(self, save_image: bool, prefix: str = "CAPTURE") -> Optional[GameState]:
        with self.capture_mgr.capture_region() as (screenshot, bbox):
            if screenshot is None:
                return None
                
            game_state = self.game_analyzer.analyze_game_state(screenshot, bbox)
            self._refresh_blank_reference_allowance(game_state)
            self._normalize_confidence_for_valid_detection(game_state)
            self.state.set_game_state(game_state)
            self._update_display_round_counter(game_state)
            if self._handle_unchanged_board_idle(prefix, game_state):
                return None

            if self._is_cycle_reset_state(game_state):
                return game_state

            if not self._get_active_round_info(game_state):
                return None

            if save_image:
                screenshot.save(self._get_simulation_image_path())

            return game_state

    def _all_columns_blank_or_unknown(self, game_state: Optional[GameState]) -> bool:
        if not game_state or not game_state.columns:
            return False

        for column in game_state.columns[:self.config.total_columns]:
            if not column.boxes:
                return False
            if any(box not in {"Blank", "Unknown"} for box in column.boxes):
                return False
        return True

    def _is_cycle_reset_candidate(self, game_state: Optional[GameState]) -> bool:
        if not game_state:
            return False

        if game_state.confidence_score > 0:
            return False

        # Hard reset only when confidence is 0, the calibrated blank witness
        # area is visible, and the board is fully blank-like.
        return (
            game_state.blank_reference_visible
            and (
                game_state.blank_detected
                or self._all_columns_blank_or_unknown(game_state)
            )
        )

    def _is_cycle_reset_state(self, game_state: Optional[GameState]) -> bool:
        if not self._is_cycle_reset_candidate(game_state):
            self._cycle_reset_candidate_since = None
            return False

        current_timestamp = float(game_state.timestamp) if game_state else time.time()
        if self._cycle_reset_candidate_since is None:
            self._cycle_reset_candidate_since = current_timestamp
            return False

        if current_timestamp < self._cycle_reset_candidate_since:
            self._cycle_reset_candidate_since = current_timestamp
            return False

        return (
            current_timestamp - self._cycle_reset_candidate_since
            >= float(self.config.cycle_reset_confirm_seconds)
        )

    def _handle_cycle_reset_cooldown(self, prefix: str) -> bool:
        # A fresh cycle starts from counter 0, but stake progression is preserved.
        self._clear_pending_bet()
        message = (
            f"{prefix} SYNCED: all columns blank with confidence 0 held for "
            f"{self.config.cycle_reset_confirm_seconds:.1f}s, cycle reset confirmed."
        )
        self.state.update_status(message)
        return True

    def _is_cycle_warmup_active(self) -> bool:
        return 1 <= self.display_round_counter <= 6

    def _is_cycle_bet_cooldown_active(self) -> bool:
        return self.display_round_counter >= 91

    def _record_cycle_warmup_skip(self, prefix: str, result_boxes: List[str], record_history: bool = False):
        c1_result = "/".join(result_boxes)
        self.last_result = c1_result
        self.last_c1_boxes = list(result_boxes)
        if record_history:
            self.c1_history.append(list(result_boxes))
            self.c1_history = self.c1_history[-30:]

        warmup_reason = f"WARMUP_CYCLE_1_6|counter={self.display_round_counter}"
        self.last_skip_reason = "Cycle warmup active for counters 1-6; no new bet placed."
        self.last_skip_csv_reason = warmup_reason
        self.state.update_status(
            f"{prefix} WARMUP C{self.display_round_counter}: recording only, betting starts at cycle 7"
        )

        self._append_simulation_csv(
            mode=prefix,
            round_number=f"WU-{self.detected_round_count}",
            c1_result=c1_result,
            color_betted="",
            amount="",
            result="WARMUP",
            lose_streak=self.loss_streak,
            multiplier=0,
            profit_change=0.0,
            total_profit=self.profit_total,
            decision_score=self.last_decision_score,
            score_gap=self.last_decision_gap,
            confidence="WARMUP",
            regime=self.last_regime,
            regime_reason=self.last_regime_reason,
            active_min_score=self.config.strategy_min_score,
            active_min_gap=self.config.strategy_min_gap,
            active_recent_columns=self.config.strategy_recent_columns_required,
            regime_strong_signal_only=False,
            decision_probability=self.last_decision_probability,
            expected_value=self.last_expected_value,
            probability_edge=self.last_probability_edge,
            probability_samples=self.last_probability_samples,
            decision_reason="Cycle warmup active; betting resumes at cycle 7",
            skip_reason=warmup_reason,
        )
        self._write_round_decision_log(
            self.detected_round_count,
            outcome_override="WARMUP",
            skip_reason=warmup_reason,
            result_boxes_override=result_boxes,
        )
        self._save_session_state()

    def _record_cycle_bet_cooldown(self, prefix: str, result_boxes: List[str], record_history: bool = False):
        c1_result = "/".join(result_boxes)
        self.last_result = c1_result
        self.last_c1_boxes = list(result_boxes)
        self._clear_pending_bet()
        if record_history:
            self.c1_history.append(list(result_boxes))
            self.c1_history = self.c1_history[-30:]

        cooldown_reason = f"COOLDOWN_CYCLE_91|counter={self.display_round_counter}"
        self.last_skip_reason = "Cycle cooldown active from counter 91; Martingale state preserved."
        self.last_skip_csv_reason = cooldown_reason
        self.state.update_status(
            f"{prefix} COOLDOWN C{self.display_round_counter}: recording only, next bet waits for cycle reset"
        )

        self._append_simulation_csv(
            mode=prefix,
            round_number=f"CD-{self.detected_round_count}",
            c1_result=c1_result,
            color_betted="",
            amount="",
            result="COOLDOWN",
            lose_streak=self.loss_streak,
            multiplier=0,
            profit_change=0.0,
            total_profit=self.profit_total,
            decision_score=self.last_decision_score,
            score_gap=self.last_decision_gap,
            confidence="COOLDOWN",
            regime=self.last_regime,
            regime_reason=self.last_regime_reason,
            active_min_score=self.config.strategy_min_score,
            active_min_gap=self.config.strategy_min_gap,
            active_recent_columns=self.config.strategy_recent_columns_required,
            regime_strong_signal_only=False,
            decision_probability=self.last_decision_probability,
            expected_value=self.last_expected_value,
            probability_edge=self.last_probability_edge,
            probability_samples=self.last_probability_samples,
            decision_reason="Cycle cooldown active; no new bet placed",
            skip_reason=cooldown_reason,
        )
        self._write_round_decision_log(
            self.detected_round_count,
            outcome_override="COOLDOWN",
            skip_reason=cooldown_reason,
            result_boxes_override=result_boxes,
        )
        self._save_session_state()

    def _append_simulation_csv(self, **kwargs):
        csv_path = self._get_simulation_csv_path()
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "Timestamp", "Mode", "Round", "Result Boxes", "Color Bet", "Amount",
            "Result", "Loss Streak", "Profit Change", "Total Profit",
            "Regime", "Regime Reason", "Decision Probability", "Expected Value",
            "Probability Edge", "Probability Samples", "Decision Reason", "Skip Reason"
        ]

        try:
            if csv_path.exists() and csv_path.stat().st_size > 0:
                with open(csv_path, newline="", encoding="utf-8-sig") as existing_file:
                    reader = csv.DictReader(existing_file)
                    existing_fieldnames = reader.fieldnames or []
                    if existing_fieldnames != fieldnames:
                        existing_rows = list(reader)

                        with open(csv_path, "w", newline="", encoding="utf-8") as rewritten_file:
                            writer = csv.DictWriter(rewritten_file, fieldnames=fieldnames)
                            writer.writeheader()
                            for row in existing_rows:
                                writer.writerow({name: row.get(name, "") for name in fieldnames})

            needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if needs_header:
                    writer.writerow(fieldnames)
                writer.writerow([
                    timestamp,
                    kwargs.get("mode", ""),
                    kwargs.get("round_number", ""),
                    kwargs.get("c1_result", ""),
                    kwargs.get("color_betted", ""),
                    kwargs.get("amount", ""),
                    kwargs.get("result", ""),
                    kwargs.get("lose_streak", 0),
                    f"{kwargs.get('profit_change', 0):.2f}",
                    f"{kwargs.get('total_profit', 0):.2f}",
                    kwargs.get("regime", ""),
                    kwargs.get("regime_reason", "")[:200],
                    f"{kwargs.get('decision_probability', 0):.2f}",
                    f"{kwargs.get('expected_value', 0):.2f}",
                    f"{kwargs.get('probability_edge', 0):.2f}",
                    int(kwargs.get("probability_samples", 0)),
                    kwargs.get("decision_reason", "")[:200],
                    kwargs.get("skip_reason", "")[:100]
                ])
        except Exception as e:
            logger.warning(f"CSV write error: {e}")

    def _record_simulated_bet(self, chosen_color: str, bet_amount_label: str, use_x2: bool):
        self.last_bet_color = chosen_color
        amount = bet_amount_label.replace("Bet ", "")
        self.last_bet_amount = amount
        self.last_bet_value = f"{bet_amount_label}{' + X2' if use_x2 else ''}"
        self.last_bet_clicked_at = datetime.now().strftime("%H:%M:%S")
        self.state.update_status(
            f"SIMULATED BET R{self.pending_round_number}: {chosen_color} / "
            f"{self.last_bet_value} @ {self.last_bet_clicked_at}"
        )

    def _execute_autoclick_bet(self, chosen_color: str, target_amount: int) -> bool:
        extra_points = self.capture_mgr.get_extra_points()

        try:
            placements = build_bet_plan(target_amount)
            actions = create_click_actions(extra_points, chosen_color, placements)
            
            self.total_clicks += len(actions)
            
            completed = perform_click_actions(
                actions,
                marker_callback=self.app.show_click_marker if self.app else None,
                stop_requested=self._stop_event.is_set,
                move_duration_range=(0.04, 0.09),
                settle_range=(0.02, 0.05),
                pause_range=(0.07, 0.12),
                click_hold_range=(0.02, 0.05),
                temporary_pause_override=0.0,
                fixed_click_interval=self.config.click_interval_seconds,
                x2_click_interval=self.config.x2_click_interval_seconds,
                click_hold_duration=self.config.click_hold_seconds,
            )
            if not completed:
                self.state.update_status("AutoClick stopped before the bet finished.")
                return False

            self.last_bet_color = chosen_color
            self.last_bet_amount = str(target_amount)
            self.last_bet_value = format_bet_plan(placements)
            self.last_bet_clicked_at = datetime.now().strftime("%H:%M:%S")
            status_message = (
                f"AUTOCLICK BET R{self.pending_round_number}: {chosen_color} / "
                f"{target_amount} [{self.last_bet_value}] @ {self.last_bet_clicked_at}"
            )
            self.state.update_status(status_message)

            if self.app and hasattr(self.app, "event_logger"):
                self.app.event_logger.log_event(
                    "AUTOCLICK_BET",
                    status_message,
                    round_number=self.pending_round_number,
                    bet_color=chosen_color,
                    bet_amount=target_amount,
                    click_plan=self.last_bet_value,
                )

            self._log_performance()
            self._save_session_state()
            
            return True
        except ClickPlanError as exc:
            self.state.update_status(f"AutoClick planning error: {exc}")
            logger.error("AutoClick planning error: %s", exc)
            return False
        except Exception as exc:
            self.state.update_status(f"AutoClick click error: {exc}")
            logger.error("AutoClick click error: %s", exc)
            return False

    def _execute_autosim_bet(self, chosen_color: str, target_amount: int) -> bool:
        extra_points = self.capture_mgr.get_extra_points()

        try:
            placements = build_bet_plan(target_amount)
            actions = create_click_actions(extra_points, chosen_color, placements)
            self.total_clicks += len(actions)

            if self.app:
                self.app.show_click_sequence_markers(actions)

            self.last_bet_color = chosen_color
            self.last_bet_amount = str(target_amount)
            self.last_bet_value = format_bet_plan(placements)
            self.last_bet_clicked_at = datetime.now().strftime("%H:%M:%S")
            status_message = (
                f"AUTOSIM BET R{self.pending_round_number}: {chosen_color} / "
                f"{target_amount} [{self.last_bet_value}] @ {self.last_bet_clicked_at}"
            )
            self.state.update_status(status_message)
            self._log_performance()
            self._save_session_state()
            return True
        except ClickPlanError as exc:
            self.state.update_status(f"AutoSim planning error: {exc}")
            logger.error("AutoSim planning error: %s", exc)
            return False
        except Exception as exc:
            self.state.update_status(f"AutoSim marker error: {exc}")
            logger.error("AutoSim marker error: %s", exc)
            return False

    def _clear_pending_bet(self):
        self.last_bet_color = None
        self.last_bet_amount = None
        self.last_bet_value = None
        self.last_bet_clicked_at = None

    def _skip_pending_bet(self, prefix: str):
        skipped_round_number = self.pending_round_number
        self._clear_pending_bet()
        self.last_skip_reason = self.last_decision_reason
        self.last_skip_csv_reason = self.last_decision_csv_reason
        self.state.update_status(f"{prefix} SKIP R{skipped_round_number}: {self.last_decision_reason}")
        
        self._append_simulation_csv(
            mode=prefix,
            round_number=skipped_round_number,
            c1_result=self.last_result or "",
            color_betted="",
            amount="",
            result="SKIP",
            lose_streak=self.loss_streak,
            multiplier=0,
            profit_change=0.0,
            total_profit=self.profit_total,
            decision_score=self.last_decision_score,
            score_gap=self.last_decision_gap,
            decision_probability=self.last_decision_probability,
            expected_value=self.last_expected_value,
            probability_edge=self.last_probability_edge,
            probability_samples=self.last_probability_samples,
            regime=self.last_regime,
            regime_reason=self.last_regime_reason,
            active_min_score=self._get_data_driven_thresholds()["min_score"],
            active_min_gap=self._get_data_driven_thresholds()["min_gap"],
            active_recent_columns=self._get_data_driven_thresholds()["recent_columns"],
            regime_strong_signal_only=self._get_data_driven_thresholds()["allow_only_strong_signal"],
            decision_reason=self.last_decision_csv_reason,
            skip_reason=self.last_skip_csv_reason,
        )
        self._write_round_decision_log(
            skipped_round_number,
            outcome_override="SKIP",
            skip_reason=self.last_skip_csv_reason,
        )
        
        self.round_count = skipped_round_number
        self.pending_round_number = skipped_round_number + 1

    def _monitor_loop(self):
        logger.info("Monitor loop started")
        last_round_signature = self._get_resume_round_signature()

        while not self._stop_event.is_set():
            idle_remaining = self.state.get_monitor_idle_remaining()
            if idle_remaining > 0:
                time.sleep(1)
                continue

            self._enforce_brave_window_lock("monitor")

            with self.capture_mgr.capture_region() as (screenshot, bbox):
                if screenshot is None:
                    time.sleep(self.config.column_check_interval)
                    continue
                    
                game_state = self.game_analyzer.analyze_game_state(screenshot, bbox)
                self._refresh_blank_reference_allowance(game_state)
                self._normalize_confidence_for_valid_detection(game_state)
                self.state.set_game_state(game_state)
                self._update_display_round_counter(game_state)
                if self._handle_unchanged_board_idle("LIVE", game_state):
                    break

                if self._is_cycle_reset_state(game_state):
                    self._handle_cycle_reset_cooldown("LIVE")
                    last_round_signature = None
                    continue

                active_round = self._get_active_round_info(game_state)
                if not active_round:
                    continue

                signature = active_round["signature"]
                if signature == last_round_signature:
                    continue

                last_round_signature = signature
                self._advance_detected_round_count()
                self.state.update_status(
                    f" LIVE ACTIVE C{active_round['basis_index']} ({active_round['valid_count']} valid columns)"
                )

                if self.last_bet_color:
                    try:
                        self._record_result_boxes(active_round["boxes"], prefix="LIVE")
                    except Exception:
                        logger.exception("Failed to record LIVE round result")

                chosen_color, target_amount, _ = self._choose_next_bet(game_state)

                if chosen_color:
                    self.state.update_status(
                        f"LIVE READY R{self.pending_round_number}: {chosen_color} / {target_amount}"
                    )
                else:
                    self.state.update_status(
                        f"LIVE SKIP R{self.pending_round_number}: {self.last_decision_reason}"
                    )

                self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)

            time.sleep(self.config.column_check_interval)

        logger.info("Monitor loop stopped")

    def _simulate_round(self):
        try:
            first_round = True
            last_round_signature = self._get_resume_round_signature()

            while not self._stop_event.is_set():
                if self.config.lock_brave_window_enabled:
                    self._enforce_brave_window_lock("simulate", bring_to_front=True)
                else:
                    self._ensure_brave_on_top("simulate")
                game_state = self._capture_detectable_round(save_image=False, prefix="SIMULATE")

                if game_state is None:
                    time.sleep(self.config.column_check_interval)
                    continue

                if self._is_cycle_reset_state(game_state):
                    self._handle_cycle_reset_cooldown("SIMULATE")
                    last_round_signature = None
                    first_round = True
                    self._awaiting_cycle_reset_sync = False
                    self.state.update_status("Simulate SYNCED: waiting for next fresh active round...")
                    continue

                if self._awaiting_cycle_reset_sync:
                    if self._should_release_sync_wait_from_visible_suffix(game_state):
                        self._awaiting_cycle_reset_sync = False
                        self.state.update_status("Simulate SYNCED: visible C7..C2 suffix detected, continuing cycle.")
                    else:
                        if self._handle_daily_idle_cutoff("SIMULATE", first_round=first_round):
                            break
                        if not (
                            self._is_any_idle_waiting_for_win()
                            or self._scheduled_idle_waiting_for_profit
                        ):
                            self.state.update_status("Simulate SYNC WAIT: started mid-cycle, waiting for blank reset before betting...")
                        last_round_signature = None
                        time.sleep(self.config.column_check_interval)
                        continue

                active_round = self._get_active_round_info(game_state)
                if self._is_waiting_for_stable_match_state(game_state, active_round):
                    self.state.update_status("Simulate: waiting for stable match...")
                    last_round_signature = None
                    time.sleep(self.config.column_check_interval)
                    continue
                if not active_round:
                    time.sleep(self.config.column_check_interval)
                    continue

                signature = active_round["signature"]
                cycle_restart = self._is_cycle_restart_active_round(active_round, last_round_signature)
                if signature == last_round_signature:
                    time.sleep(self.config.column_check_interval)
                    continue

                last_round_signature = signature
                self._advance_detected_round_count()
                was_first_round = first_round or cycle_restart

                if was_first_round:
                    self.state.update_status(
                        f"SIMULATE: first active round detected at C{active_round['basis_index']}, placing virtual round 1 bet"
                    )
                    first_round = False
                else:
                    self._record_result_boxes(active_round["boxes"], prefix="SIMULATE")
                    if self._stop_event.is_set():
                        break

                if self._handle_daily_idle_cutoff("SIMULATE", first_round=was_first_round):
                    break

                if self._is_cycle_warmup_active():
                    self._record_cycle_warmup_skip("SIMULATE", active_round["boxes"], record_history=True)
                    time.sleep(1)
                    self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                    self.state.update_status(f"Simulate warmup countdown: {self.config.monitor_idle_seconds:.0f}s")

                    while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                        time.sleep(1)

                    self.state.update_status("Simulate: waiting for next active round...")
                    continue

                if self._is_cycle_bet_cooldown_active():
                    self._record_cycle_bet_cooldown("SIMULATE", active_round["boxes"], record_history=True)
                    time.sleep(1)
                    self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                    self.state.update_status(f"Simulate cooldown countdown: {self.config.monitor_idle_seconds:.0f}s")

                    while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                        time.sleep(1)

                    self.state.update_status("Simulate: waiting for next active round...")
                    continue

                chosen_color, target_amount, _ = self._choose_next_bet(game_state)
                if chosen_color:
                    if not self._check_bet_limits(target_amount):
                        self.state.state = AutomationState.IDLE
                        break
                    self._record_simulated_bet(chosen_color, f"Bet {target_amount}", False)
                else:
                    self._skip_pending_bet("SIMULATE")

                time.sleep(1)
                self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                self.state.update_status(f"Simulate countdown: {self.config.monitor_idle_seconds:.0f}s")

                while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                    time.sleep(1)

                self.state.update_status("Simulate: waiting for next active round...")

            if self._simulate_halted:
                self.state.state = AutomationState.IDLE
            elif self._stop_event.is_set():
                self.state.update_status("SIMULATE STOPPED")
                self.state.state = AutomationState.IDLE
        except Exception as e:
            logger.error(f"Simulation error: {e}")
            self.state.update_status(f"Simulation error: {e}")
            self.state.state = AutomationState.IDLE

    def _autoclick_loop(self):
        try:
            first_round = True
            last_round_signature = self._get_resume_round_signature()

            while not self._stop_event.is_set():
                if self.config.lock_brave_window_enabled:
                    self._enforce_brave_window_lock("autoclick", bring_to_front=True)
                else:
                    self._ensure_brave_on_top("autoclick")
                game_state = self._capture_detectable_round(save_image=False, prefix="AUTOCLICK")

                if game_state is None:
                    time.sleep(self.config.column_check_interval)
                    continue

                if self._is_cycle_reset_state(game_state):
                    self._handle_cycle_reset_cooldown("AUTOCLICK")
                    last_round_signature = None
                    first_round = True
                    self._awaiting_cycle_reset_sync = False
                    self.state.update_status("AutoClick SYNCED: waiting for next fresh active round...")
                    continue

                if self._awaiting_cycle_reset_sync:
                    if self._should_release_sync_wait_from_visible_suffix(game_state):
                        self._awaiting_cycle_reset_sync = False
                        self.state.update_status("AutoClick SYNCED: visible C7..C2 suffix detected, continuing cycle.")
                    else:
                        if self._handle_daily_idle_cutoff("AUTOCLICK", first_round=first_round):
                            break
                        if not (
                            self._is_any_idle_waiting_for_win()
                            or self._scheduled_idle_waiting_for_profit
                        ):
                            self.state.update_status("AutoClick SYNC WAIT: started mid-cycle, waiting for blank reset before betting...")
                        last_round_signature = None
                        time.sleep(self.config.column_check_interval)
                        continue

                active_round = self._get_active_round_info(game_state)
                if self._is_waiting_for_stable_match_state(game_state, active_round):
                    self.state.update_status("AutoClick: waiting for stable match...")
                    last_round_signature = None
                    time.sleep(self.config.column_check_interval)
                    continue
                if not active_round:
                    time.sleep(self.config.column_check_interval)
                    continue

                signature = active_round["signature"]
                cycle_restart = self._is_cycle_restart_active_round(active_round, last_round_signature)
                if signature == last_round_signature:
                    time.sleep(self.config.column_check_interval)
                    continue

                last_round_signature = signature
                self._advance_detected_round_count()
                was_first_round = first_round or cycle_restart

                if was_first_round:
                    self.state.update_status(
                        f"AUTOCLICK: first active round detected at C{active_round['basis_index']}, placing round 1 bet"
                    )
                    first_round = False
                else:
                    self._record_result_boxes(active_round["boxes"], prefix="AUTOCLICK")
                    if self._stop_event.is_set():
                        break

                if self._handle_daily_idle_cutoff("AUTOCLICK", first_round=was_first_round):
                    break

                if self._is_cycle_warmup_active():
                    self._record_cycle_warmup_skip("AUTOCLICK", active_round["boxes"], record_history=True)
                    time.sleep(1)
                    self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                    self.state.update_status(f"AutoClick warmup countdown: {self.config.monitor_idle_seconds:.0f}s")

                    while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                        time.sleep(1)

                    self.state.update_status("AutoClick: waiting for next active round...")
                    continue

                if self._is_cycle_bet_cooldown_active():
                    self._record_cycle_bet_cooldown("AUTOCLICK", active_round["boxes"], record_history=True)
                    time.sleep(1)
                    self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                    self.state.update_status(f"AutoClick cooldown countdown: {self.config.monitor_idle_seconds:.0f}s")

                    while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                        time.sleep(1)

                    self.state.update_status("AutoClick: waiting for next active round...")
                    continue

                chosen_color, target_amount, decision_meta = self._choose_next_bet(game_state)
                
                if chosen_color:
                    if not self._check_bet_limits(target_amount):
                        self._stop_event.set()
                        self.state.state = AutomationState.IDLE
                        break
                    
                    logger.info(f"Decision: {chosen_color} @ {target_amount} | "
                               f"Regime:{decision_meta['regime']} "
                               f"Conf:{decision_meta['confidence']} Score:{decision_meta['score']:.1f} "
                               f"Gap:{decision_meta['gap']:.1f} | Alt:{decision_meta['alternatives'][:2]}")
                    
                    if not self._execute_autoclick_bet(chosen_color, target_amount):
                        self._stop_event.set()
                        self.state.state = AutomationState.IDLE
                        break
                else:
                    self._skip_pending_bet("AUTOCLICK")

                time.sleep(1)
                self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                self.state.update_status(f"AutoClick countdown: {self.config.monitor_idle_seconds:.0f}s")

                while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                    time.sleep(1)

                self.state.update_status("AutoClick: waiting for next active round...")

            if self._simulate_halted:
                self.state.state = AutomationState.IDLE
            elif self._stop_event.is_set():
                self.state.update_status("AUTOCLICK STOPPED")
                self.state.state = AutomationState.IDLE

            self._log_decision_metrics()
            
        except Exception as e:
            logger.error(f"AutoClick error: {e}")
            self.state.update_status(f"AutoClick error: {e}")
            self.state.state = AutomationState.IDLE

    def _autosim_loop(self):
        try:
            first_round = True
            last_round_signature = self._get_resume_round_signature()

            while not self._stop_event.is_set():
                if self.config.lock_brave_window_enabled:
                    self._enforce_brave_window_lock("autosim", bring_to_front=True)
                else:
                    self._ensure_brave_on_top("autosim")
                game_state = self._capture_detectable_round(save_image=False, prefix="AUTOSIM")

                if game_state is None:
                    time.sleep(self.config.column_check_interval)
                    continue

                if self._is_cycle_reset_state(game_state):
                    self._handle_cycle_reset_cooldown("AUTOSIM")
                    last_round_signature = None
                    first_round = True
                    self._awaiting_cycle_reset_sync = False
                    self.state.update_status("AutoSim SYNCED: waiting for next fresh active round...")
                    continue

                if self._awaiting_cycle_reset_sync:
                    if self._should_release_sync_wait_from_visible_suffix(game_state):
                        self._awaiting_cycle_reset_sync = False
                        self.state.update_status("AutoSim SYNCED: visible C7..C2 suffix detected, continuing cycle.")
                    else:
                        if self._handle_daily_idle_cutoff("AUTOSIM", first_round=first_round):
                            break
                        if not (
                            self._is_any_idle_waiting_for_win()
                            or self._scheduled_idle_waiting_for_profit
                        ):
                            self.state.update_status("AutoSim SYNC WAIT: started mid-cycle, waiting for blank reset before betting...")
                        last_round_signature = None
                        time.sleep(self.config.column_check_interval)
                        continue

                active_round = self._get_active_round_info(game_state)
                if self._is_waiting_for_stable_match_state(game_state, active_round):
                    self.state.update_status("AutoSim: waiting for stable match...")
                    last_round_signature = None
                    time.sleep(self.config.column_check_interval)
                    continue
                if not active_round:
                    time.sleep(self.config.column_check_interval)
                    continue

                signature = active_round["signature"]
                cycle_restart = self._is_cycle_restart_active_round(active_round, last_round_signature)
                if signature == last_round_signature:
                    time.sleep(self.config.column_check_interval)
                    continue

                last_round_signature = signature
                self._advance_detected_round_count()
                was_first_round = first_round or cycle_restart

                if was_first_round:
                    self.state.update_status(
                        f"AUTOSIM: first active round detected at C{active_round['basis_index']}, placing round 1 markers"
                    )
                    first_round = False
                else:
                    self._record_result_boxes(active_round["boxes"], prefix="AUTOSIM")
                    if self._stop_event.is_set():
                        break

                if self._handle_daily_idle_cutoff("AUTOSIM", first_round=was_first_round):
                    break

                if self._is_cycle_warmup_active():
                    self._record_cycle_warmup_skip("AUTOSIM", active_round["boxes"], record_history=True)
                    time.sleep(1)
                    self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                    self.state.update_status(f"AutoSim warmup countdown: {self.config.monitor_idle_seconds:.0f}s")

                    while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                        time.sleep(1)

                    self.state.update_status("AutoSim: waiting for next active round...")
                    continue

                if self._is_cycle_bet_cooldown_active():
                    self._record_cycle_bet_cooldown("AUTOSIM", active_round["boxes"], record_history=True)
                    time.sleep(1)
                    self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                    self.state.update_status(f"AutoSim cooldown countdown: {self.config.monitor_idle_seconds:.0f}s")

                    while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                        time.sleep(1)

                    self.state.update_status("AutoSim: waiting for next active round...")
                    continue

                chosen_color, target_amount, _ = self._choose_next_bet(game_state)

                if chosen_color:
                    if not self._check_bet_limits(target_amount):
                        self._stop_event.set()
                        self.state.state = AutomationState.IDLE
                        break

                    if not self._execute_autosim_bet(chosen_color, target_amount):
                        self._stop_event.set()
                        self.state.state = AutomationState.IDLE
                        break
                else:
                    self._skip_pending_bet("AUTOSIM")

                time.sleep(1)
                self.state.set_monitor_idle_until(self.config.monitor_idle_seconds)
                self.state.update_status(f"AutoSim countdown: {self.config.monitor_idle_seconds:.0f}s")

                while not self._stop_event.is_set() and self.state.get_monitor_idle_remaining() > 0:
                    time.sleep(1)

                self.state.update_status("AutoSim: waiting for next active round...")

            if self._simulate_halted:
                self.state.state = AutomationState.IDLE
            elif self._stop_event.is_set():
                self.state.update_status("AUTOSIM STOPPED")
                self.state.state = AutomationState.IDLE

            self._log_decision_metrics()

        except Exception as e:
            logger.error(f"AutoSim error: {e}")
            self.state.update_status(f"AutoSim error: {e}")
            self.state.state = AutomationState.IDLE


class AutoClickerPro:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config_dir = ensure_runtime_data_dir()
        migrate_legacy_runtime_files(self.config_dir)
        configure_file_logging(self.config_dir)
        self.event_logger = EventLogger(self.config_dir)
        logger.info(f"Runtime data directory: {self.config_dir}")
        
        self.config_path = self.config_dir / "app_config.json"
        self.calibration_path = self.config_dir / "calibration.json"
        
        self.config = self._load_config()
        self.config_dirty = False
        self.capture_mgr = ScreenCaptureManager(self.config)
        self.color_matcher = ColorMatcher(self.config)
        self.game_analyzer = GameAnalyzer(self.config, self.capture_mgr, self.color_matcher)
        self.engine = AutomationEngine(self.config, self.capture_mgr, self.color_matcher, self.game_analyzer, self)
        
        self._calibration_window: Optional[tk.Toplevel] = None
        self._calibration_lock_after_id: Optional[str] = None
        self._calibration_brave_hwnd: Optional[int] = None
        self._calibration_brave_rect: Optional[Tuple[int, int, int, int]] = None
        self._saved_brave_rect: Optional[Tuple[int, int, int, int]] = None
        self._status_update_id = None
        self.status_mode_label = "Idle"
        self._history_analysis_thread: Optional[threading.Thread] = None
        self._history_analysis_running = False
        self._history_analysis_requested_while_unsynced = False
        self._history_analysis_last_csv_mtime: Optional[float] = None
        self._history_analysis_last_summary = "History Analysis: Waiting for CSV scan"
        self._history_analysis_last_basis = "History Basis: Complete 99-row windows"
        self._history_strategy_model: Optional[HistoryStrategyModel] = None
        self._history_strategy_applied_mtime: Optional[float] = None
        self._strategy_custom_fields_force_hidden = False
        self._auto_cache_clear_done_this_warmup = False
        self._auto_cache_clear_status_text = "Cache: idle"
        self._ui_shutting_down = False
        
        self._setup_ui()
        self._populate_strategy_vars_from_config()
        self._load_calibration()
        self._schedule_ui_update()
    
    def _load_config(self) -> AppConfig:
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r') as f:
                    data = json.load(f)
                    app_config_data = data.get("app_config", {})
                    return AppConfig.from_dict(app_config_data)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
        return AppConfig()
    
    def _save_config(self, force: bool = False):
        if not force and self.config_path.exists() and not self.config_dirty:
            return
        try:
            app_config = self.config.to_dict()
            app_config.pop("strategy_history_window_limit", None)
            with open(self.config_path, 'w') as f:
                json.dump({"app_config": app_config, "version": "2.3.0", "updated": datetime.now().isoformat()}, f, indent=2)
            self.config_dirty = False
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def _get_history_csv_path(self) -> Path:
        return self.config_dir / "simulate_results.csv"

    def _set_history_analysis_text(self, summary: str, basis: str):
        self._history_analysis_last_summary = summary
        self._history_analysis_last_basis = basis
        if hasattr(self, "history_analysis_status_var"):
            self.history_analysis_status_var.set(summary)
        if hasattr(self, "history_analysis_basis_var"):
            self.history_analysis_basis_var.set(basis)

    def _trim_history_csv_to_complete_windows(self):
        if not self._is_history_trim_allowed():
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: CSV trim is disabled while Simulate, AutoClick, or AutoSim is running"
                )
            return
        if self._history_analysis_running:
            if hasattr(self, "status_var"):
                self.status_var.set("Status: Wait for History Analysis to finish before trimming CSV")
            return

        csv_path = self._get_history_csv_path()
        if not csv_path.exists():
            messagebox.showinfo("Trim History CSV", "No simulate_results.csv found yet.")
            return

        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as handle:
                raw_rows = list(csv.DictReader(handle))
                fieldnames = list(raw_rows[0].keys()) if raw_rows else []
        except Exception as exc:
            messagebox.showerror("Trim History CSV", f"Could not read CSV: {exc}")
            return

        if not raw_rows or not fieldnames:
            messagebox.showinfo("Trim History CSV", "simulate_results.csv is empty.")
            return

        parsed_rows = _parse_history_rows(raw_rows)
        start_indexes = [index for index, row in enumerate(parsed_rows) if _is_window_start(row)]
        last_complete_end = 0
        complete_windows = 0

        for position, start_index in enumerate(start_indexes):
            end_index = start_indexes[position + 1] if position + 1 < len(start_indexes) else len(parsed_rows)
            candidate = parsed_rows[start_index:end_index]
            if not _is_complete_window(candidate):
                continue
            complete_windows += 1
            last_complete_end = end_index

        if complete_windows <= 0 or last_complete_end <= 0:
            messagebox.showwarning("Trim History CSV", "No complete valid history windows were found to retain.")
            return

        trailing_rows = len(raw_rows) - last_complete_end
        if trailing_rows <= 0:
            if hasattr(self, "status_var"):
                self.status_var.set(
                    f"Status: History CSV already clean with {complete_windows} complete windows"
                )
            messagebox.showinfo(
                "Trim History CSV",
                f"simulate_results.csv is already clean.\n\nComplete windows: {complete_windows}\nTrailing rows removed: 0",
            )
            return

        backup_path = csv_path.with_name(
            f"simulate_results.before_trim_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        trimmed_rows = raw_rows[:last_complete_end]

        try:
            shutil.copy2(csv_path, backup_path)
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(trimmed_rows)
        except Exception as exc:
            messagebox.showerror("Trim History CSV", f"Could not trim CSV: {exc}")
            return

        self._history_analysis_last_csv_mtime = None
        self._history_strategy_applied_mtime = None
        self._set_history_analysis_text(
            f"History Analysis: Trimmed CSV to {complete_windows} complete windows",
            f"History Basis: Removed {trailing_rows} incomplete trailing row(s); backup saved as {backup_path.name}",
        )
        self._ensure_lazy_history_analysis()
        if hasattr(self, "status_var"):
            self.status_var.set(
                f"Status: Trimmed history CSV; removed {trailing_rows} incomplete trailing row(s)"
            )
        logger.info(
            "Trimmed history CSV to %s complete windows; removed %s trailing rows; backup=%s",
            complete_windows,
            trailing_rows,
            backup_path,
        )
        messagebox.showinfo(
            "Trim History CSV",
            "\n".join(
                [
                    "simulate_results.csv trimmed successfully.",
                    "",
                    f"Complete windows retained: {complete_windows}",
                    f"Incomplete trailing rows removed: {trailing_rows}",
                    f"Backup: {backup_path.name}",
                ]
            ),
        )

    def _set_auto_cache_clear_status(self, text: str):
        self._auto_cache_clear_status_text = text
        if hasattr(self, "strategy_cache_auto_clear_var"):
            self.strategy_cache_auto_clear_var.set(text)

    def _schedule_on_ui_thread(self, callback) -> bool:
        if self._ui_shutting_down:
            return False
        try:
            self.root.after(0, callback)
            return True
        except (tk.TclError, RuntimeError):
            logger.debug("UI callback skipped because Tk is shutting down")
            self._ui_shutting_down = True
            return False

    def _ensure_lazy_history_analysis(self):
        csv_path = self._get_history_csv_path()
        if not csv_path.exists():
            self._set_history_analysis_text(
                "History Analysis: No simulate_results.csv found yet",
                "History Basis: Generate CSV history first",
            )
            self._history_analysis_requested_while_unsynced = False
            return

        try:
            csv_mtime = csv_path.stat().st_mtime
        except OSError:
            return

        if self._history_analysis_running:
            return

        if (
            self._history_analysis_requested_while_unsynced
            and self._history_analysis_last_csv_mtime == csv_mtime
        ):
            return

        self._history_analysis_requested_while_unsynced = True
        self._history_analysis_running = True
        self._set_history_analysis_text(
            "History Analysis: Analyzing CSV during warmup...",
            "History Basis: Scanning complete 99-row windows",
        )
        self._history_analysis_thread = threading.Thread(
            target=self._run_lazy_history_analysis,
            args=(csv_path, csv_mtime),
            name="HistoryAnalysisThread",
            daemon=True,
        )
        self._history_analysis_thread.start()

    def _should_auto_clear_color_cache(self, game_state: Optional[GameState]) -> Tuple[bool, str]:
        if not game_state or not self.engine._is_cycle_warmup_active():
            return False, ""
        if self._auto_cache_clear_done_this_warmup:
            return False, ""

        cache_stats = self.color_matcher.get_cache_stats()
        total_samples = int(cache_stats.get("hits", 0)) + int(cache_stats.get("misses", 0))
        cache_size = int(cache_stats.get("cache_size", 0))
        hit_rate = float(cache_stats.get("hit_rate", 0.0))

        # Require enough live evidence before treating the cache as suspect.
        if total_samples < 1000 or cache_size < 256:
            return False, ""

        if game_state.has_invalid_board_pattern and hit_rate < 85.0:
            return True, (
                f"invalid board pattern with cache hit rate {hit_rate:.1f}% and size {cache_size}"
            )

        if game_state.any_unknown and game_state.confidence_score < 50.0 and hit_rate < 75.0:
            return True, (
                f"low-confidence unknown board with cache hit rate {hit_rate:.1f}% and size {cache_size}"
            )

        if hit_rate < 65.0 and cache_size >= 1024:
            return True, (
                f"low cache hit rate {hit_rate:.1f}% with large cache size {cache_size}"
            )

        return False, ""

    def _maybe_auto_clear_color_cache(self, game_state: Optional[GameState]):
        if self._auto_cache_clear_done_this_warmup:
            self._set_auto_cache_clear_status("Cache: clear")
            return

        should_clear, reason = self._should_auto_clear_color_cache(game_state)
        if not should_clear:
            self._set_auto_cache_clear_status("Cache: skip")
            return

        cache_stats = self.color_matcher.get_cache_stats()
        self.color_matcher.clear_cache()
        self._auto_cache_clear_done_this_warmup = True
        self._set_auto_cache_clear_status("Cache: clear")
        logger.info(
            "Auto-cleared color cache during warmup: %s (hits=%s misses=%s size=%s/%s)",
            reason,
            cache_stats.get("hits", 0),
            cache_stats.get("misses", 0),
            cache_stats.get("cache_size", 0),
            cache_stats.get("cache_maxsize", 0),
        )

    def _run_lazy_history_analysis(self, csv_path: Path, csv_mtime: float):
        try:
            model = build_history_strategy_model(
                csv_path,
                window_limit=self.config.strategy_history_window_limit,
            )
            if model.complete_windows <= 0 or model.settled_bets <= 0:
                if not self._schedule_on_ui_thread(
                    lambda: self._finish_lazy_history_analysis(
                        csv_mtime,
                        model.summary,
                        model.basis,
                        None,
                    )
                ):
                    self._history_analysis_running = False
                return

            if not self._schedule_on_ui_thread(
                lambda: self._finish_lazy_history_analysis(
                    csv_mtime,
                    model.summary,
                    model.basis,
                    model,
                )
            ):
                self._history_analysis_running = False
        except Exception as exc:
            logger.exception("Lazy history analysis failed")
            error_message = f"History Analysis: Failed to analyze CSV ({exc})"
            if not self._schedule_on_ui_thread(
                lambda error_message=error_message: self._finish_lazy_history_analysis(
                    csv_mtime,
                    error_message,
                    "History Basis: Check CSV format",
                    None,
                )
            ):
                self._history_analysis_running = False

    def _apply_history_strategy_guardrails(
        self,
        updates: Dict[str, object],
        model: HistoryStrategyModel,
    ) -> Tuple[Dict[str, object], List[str]]:
        guarded = dict(updates)
        reasons: List[str] = []

        # Avoid repeated warmup auto-tunes from choking bet flow by tightening
        # thresholds too aggressively in one step.
        strict_caps = {
            "strategy_probability_window": 4,
            "strategy_probability_min_samples": 1,
            "strategy_min_hit_probability": 0.03,
            "strategy_min_expected_value": 0.02,
            "strategy_min_probability_edge": 0.01,
            "strategy_probability_board_weight": 0.08,
        }
        stricter_when_higher = {
            "strategy_probability_window",
            "strategy_probability_min_samples",
            "strategy_min_hit_probability",
            "strategy_min_expected_value",
            "strategy_min_probability_edge",
        }
        stricter_when_lower = {"strategy_probability_board_weight"}

        for field_name, cap in strict_caps.items():
            if field_name not in guarded:
                continue
            current_value = getattr(self.config, field_name)
            proposed_value = guarded[field_name]
            if field_name in stricter_when_higher and proposed_value > current_value:
                capped_value = min(proposed_value, current_value + cap)
                if capped_value != proposed_value:
                    reasons.append(f"cap {field_name}")
                guarded[field_name] = capped_value
            elif field_name in stricter_when_lower and proposed_value < current_value:
                capped_value = max(proposed_value, current_value - cap)
                if capped_value != proposed_value:
                    reasons.append(f"cap {field_name}")
                guarded[field_name] = capped_value

        current_limit = int(getattr(self.config, "strategy_history_window_limit", 0))
        proposed_limit = int(guarded.get("strategy_history_window_limit", current_limit))
        if current_limit > 0 and proposed_limit > 0 and proposed_limit < current_limit:
            capped_limit = max(proposed_limit, current_limit - 2)
            if capped_limit != proposed_limit:
                reasons.append("cap history window limit")
            guarded["strategy_history_window_limit"] = capped_limit

        # When recent history is still profitable enough, don't let auto-tune
        # keep ratcheting to stricter values that can starve betting.
        if model.summed_profit > 0 and model.hit_rate >= 0.52:
            profitable_guard_triggered = False
            for field_name in stricter_when_higher:
                if field_name in guarded:
                    relaxed_value = min(guarded[field_name], getattr(self.config, field_name))
                    if relaxed_value != guarded[field_name]:
                        profitable_guard_triggered = True
                    guarded[field_name] = relaxed_value
            if "strategy_probability_board_weight" in guarded:
                relaxed_value = max(
                    guarded["strategy_probability_board_weight"],
                    self.config.strategy_probability_board_weight,
                )
                if relaxed_value != guarded["strategy_probability_board_weight"]:
                    profitable_guard_triggered = True
                guarded["strategy_probability_board_weight"] = relaxed_value
            if current_limit > 0 and "strategy_history_window_limit" in guarded:
                relaxed_value = max(
                    int(guarded["strategy_history_window_limit"]),
                    current_limit,
                )
                if relaxed_value != int(guarded["strategy_history_window_limit"]):
                    profitable_guard_triggered = True
                guarded["strategy_history_window_limit"] = relaxed_value
            if profitable_guard_triggered:
                reasons.append("profit guard")

        return guarded, reasons

    def _build_history_stage_strategy_updates(self, model: HistoryStrategyModel) -> Tuple[Dict[str, object], List[str]]:
        updates: Dict[str, object] = {}
        reasons: List[str] = []
        if model.settled_bets < 80:
            return updates, reasons

        has_weak_deep = bool(model.dangerous_amounts)
        losing_history = model.summed_profit < 0 or model.hit_rate < 0.47
        if not has_weak_deep and not losing_history:
            return updates, reasons

        updates["strategy_probability_window"] = min(40, int(self.config.strategy_probability_window) + (4 if has_weak_deep else 2))
        updates["strategy_probability_min_samples"] = min(12, int(self.config.strategy_probability_min_samples) + 1)
        updates["strategy_min_hit_probability"] = min(
            0.68,
            float(self.config.strategy_min_hit_probability) + (0.035 if has_weak_deep else 0.02),
        )
        updates["strategy_min_expected_value"] = min(
            0.20,
            float(self.config.strategy_min_expected_value) + (0.025 if has_weak_deep else 0.015),
        )
        updates["strategy_min_probability_edge"] = min(
            0.08,
            float(self.config.strategy_min_probability_edge) + (0.012 if has_weak_deep else 0.006),
        )
        updates["strategy_probability_board_weight"] = max(
            0.35,
            float(self.config.strategy_probability_board_weight) - (0.05 if has_weak_deep else 0.025),
        )

        if has_weak_deep:
            reasons.append(
                "strategy builder weak deep stages "
                + ", ".join(str(amount) for amount in model.dangerous_amounts[:4])
            )
        if losing_history:
            reasons.append(
                f"strategy builder losing history hit={model.hit_rate * 100:.1f}% profit={model.summed_profit:+.0f}"
            )
        return updates, reasons

    def _apply_history_strategy_model(self, model: HistoryStrategyModel):
        self._history_strategy_model = model
        self.engine.history_strategy_model = model

        if self._history_strategy_applied_mtime == model.source_mtime:
            return

        updates = model.recommendation.to_config_updates()
        builder_updates, builder_reasons = self._build_history_stage_strategy_updates(model)
        updates.update(builder_updates)
        if not updates:
            self._history_strategy_applied_mtime = model.source_mtime
            return
        updates.pop("strategy_history_window_limit", None)
        if not updates:
            self._history_strategy_applied_mtime = model.source_mtime
            return
        updates, guardrail_reasons = self._apply_history_strategy_guardrails(updates, model)
        guardrail_reasons.extend(builder_reasons)

        candidate_data = self.config.to_dict()
        candidate_data.update(updates)

        try:
            validated = AppConfig.from_dict(candidate_data)
        except Exception as exc:
            logger.error("Failed to validate history strategy recommendation: %s", exc)
            return

        for field_name, _field_value in updates.items():
            setattr(self.config, field_name, getattr(validated, field_name))

        self._history_strategy_applied_mtime = model.source_mtime
        self.config_dirty = True
        self._save_config()
        self._populate_strategy_vars_from_config()
        if guardrail_reasons and hasattr(self, "status_var"):
            unique_reasons = ", ".join(dict.fromkeys(guardrail_reasons))
            self.status_var.set(f"Status: Auto-tune guarded - {unique_reasons}")
        if guardrail_reasons:
            logger.info("History auto-tune guardrails applied: %s", ", ".join(dict.fromkeys(guardrail_reasons)))

    def _finish_lazy_history_analysis(
        self,
        csv_mtime: float,
        summary: str,
        basis: str,
        model: Optional[HistoryStrategyModel],
    ):
        self._history_analysis_running = False
        self._history_analysis_last_csv_mtime = csv_mtime
        self._set_history_analysis_text(summary, basis)
        if model:
            self._apply_history_strategy_model(model)

    @staticmethod
    def _strategy_profile_definitions() -> Dict[str, Dict[str, object]]:
        default_config = AppConfig()
        return {
            "Default": {
                "strategy_randomize_enabled": False,
                "strategy_probability_window": default_config.strategy_probability_window,
                "strategy_probability_min_samples": default_config.strategy_probability_min_samples,
                "strategy_min_hit_probability": default_config.strategy_min_hit_probability,
                "strategy_min_expected_value": default_config.strategy_min_expected_value,
                "strategy_min_probability_edge": default_config.strategy_min_probability_edge,
                "strategy_probability_board_weight": default_config.strategy_probability_board_weight,
                "strategy_history_window_limit": default_config.strategy_history_window_limit,
            },
            "Aggressive": {
                "strategy_randomize_enabled": False,
                "strategy_probability_window": 24,
                "strategy_probability_min_samples": 6,
                "strategy_min_hit_probability": 0.54,
                "strategy_min_expected_value": 0.02,
                "strategy_min_probability_edge": 0.02,
                "strategy_probability_board_weight": 0.50,
                "strategy_history_window_limit": default_config.strategy_history_window_limit,
            },
            "Moderate": {
                "strategy_randomize_enabled": False,
                "strategy_probability_window": 28,
                "strategy_probability_min_samples": 7,
                "strategy_min_hit_probability": 0.58,
                "strategy_min_expected_value": 0.04,
                "strategy_min_probability_edge": 0.025,
                "strategy_probability_board_weight": 0.48,
                "strategy_history_window_limit": default_config.strategy_history_window_limit,
            },
            "Conservative": {
                "strategy_randomize_enabled": False,
                "strategy_probability_window": 36,
                "strategy_probability_min_samples": 10,
                "strategy_min_hit_probability": 0.66,
                "strategy_min_expected_value": 0.12,
                "strategy_min_probability_edge": 0.05,
                "strategy_probability_board_weight": 0.38,
                "strategy_history_window_limit": default_config.strategy_history_window_limit,
            },
            "Random": {
                "strategy_randomize_enabled": True,
                "strategy_probability_window": default_config.strategy_probability_window,
                "strategy_probability_min_samples": default_config.strategy_probability_min_samples,
                "strategy_min_hit_probability": default_config.strategy_min_hit_probability,
                "strategy_min_expected_value": default_config.strategy_min_expected_value,
                "strategy_min_probability_edge": default_config.strategy_min_probability_edge,
                "strategy_probability_board_weight": default_config.strategy_probability_board_weight,
                "strategy_history_window_limit": default_config.strategy_history_window_limit,
            },
        }

    @staticmethod
    def _progression_profile_names() -> Tuple[str, ...]:
        return tuple(PROGRESSION_PROFILES.keys())

    def _normalized_progression_profile_name(self) -> str:
        profile_name = (self.config.strategy_progression_profile or "").strip()
        if profile_name in PROGRESSION_PROFILES:
            return profile_name
        return "Slow Progress"

    def _rebuild_engine_progression_steps(self):
        if not hasattr(self, "engine"):
            return
        self.engine.bet_amount_steps = self.engine._build_virtual_bet_steps(
            start=self.config.martingale_start,
            max_steps=self.config.martingale_max_steps,
            flat_cap=self.engine._effective_max_bet_per_round() or None,
            profile_name=self._normalized_progression_profile_name(),
        )
        self.engine.max_loss_streak = len(self.engine.bet_amount_steps)
        self.engine.martingale_index = max(0, min(self.engine.martingale_index, len(self.engine.bet_amount_steps) - 1))

    def _detect_strategy_profile_name(self) -> str:
        profiles = self._strategy_profile_definitions()
        compare_fields = (
            "strategy_randomize_enabled",
            "strategy_probability_window",
            "strategy_probability_min_samples",
            "strategy_min_hit_probability",
            "strategy_min_expected_value",
            "strategy_min_probability_edge",
            "strategy_probability_board_weight",
            "strategy_history_window_limit",
        )

        for profile_name, values in profiles.items():
            matches = True
            for field_name in compare_fields:
                current_value = getattr(self.config, field_name)
                target_value = values[field_name]
                if isinstance(target_value, float):
                    if abs(float(current_value) - float(target_value)) > 1e-9:
                        matches = False
                        break
                else:
                    if current_value != target_value:
                        matches = False
                        break
            if matches:
                return profile_name

        return "Custom"

    def _detect_strategy_base_profile_name(self) -> str:
        profiles = self._strategy_profile_definitions()
        compare_fields = (
            "strategy_randomize_enabled",
            "strategy_probability_window",
            "strategy_probability_min_samples",
            "strategy_min_hit_probability",
            "strategy_min_expected_value",
            "strategy_min_probability_edge",
            "strategy_probability_board_weight",
        )

        for profile_name, values in profiles.items():
            matches = True
            for field_name in compare_fields:
                current_value = getattr(self.config, field_name)
                target_value = values[field_name]
                if isinstance(target_value, float):
                    if abs(float(current_value) - float(target_value)) > 1e-9:
                        matches = False
                        break
                else:
                    if current_value != target_value:
                        matches = False
                        break
            if matches:
                return profile_name

        return "Custom"

    def _apply_strategy_profile(self, profile_name: str):
        if profile_name == "Custom":
            self._strategy_custom_fields_force_hidden = False
            if hasattr(self, "status_var"):
                self.status_var.set("Status: Custom profile selected - edit fields below and apply")
            self._refresh_strategy_custom_fields_visibility()
            self._refresh_strategy_summary()
            return

        profile = self._strategy_profile_definitions().get(profile_name)
        if not profile:
            return

        current_randomize = bool(self.config.strategy_randomize_enabled)
        next_randomize = bool(profile.get("strategy_randomize_enabled", current_randomize))
        if current_randomize != next_randomize and not self._is_random_mode_edit_allowed():
            self._populate_strategy_vars_from_config()
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: Random mode can only be changed during warmup while Simulate, AutoClick, or AutoSim is running"
                )
            return

        for field_name, value in profile.items():
            setattr(self.config, field_name, value)

        self._strategy_custom_fields_force_hidden = True
        self._history_analysis_last_csv_mtime = None
        self.config_dirty = True
        self._save_config()
        self._populate_strategy_vars_from_config()
        if hasattr(self, "status_var"):
            self.status_var.set(f"Status: Strategy profile {profile_name}")
        logger.info("Strategy profile applied: %s", profile_name)

    def _on_strategy_profile_selected(self, _event=None):
        if getattr(self, "_strategy_profile_syncing", False):
            return
        selected = self.strategy_profile_var.get().strip() or "Custom"
        self._apply_strategy_profile(selected)

    def _populate_strategy_vars_from_config(self):
        self._strategy_profile_syncing = True
        try:
            if hasattr(self, "strategy_profile_var"):
                self.strategy_profile_var.set(self._detect_strategy_profile_name())
            if hasattr(self, "strategy_progression_profile_var"):
                self.strategy_progression_profile_var.set(self._normalized_progression_profile_name())
            self.strategy_randomize_enabled_var.set(bool(self.config.strategy_randomize_enabled))
            self.strategy_probability_window_var.set(str(int(self.config.strategy_probability_window)))
            self.strategy_probability_samples_var.set(str(int(self.config.strategy_probability_min_samples)))
            self.strategy_min_hit_probability_var.set(f"{float(self.config.strategy_min_hit_probability):.2f}")
            self.strategy_min_expected_value_var.set(f"{float(self.config.strategy_min_expected_value):.2f}")
            self.strategy_min_probability_edge_var.set(f"{float(self.config.strategy_min_probability_edge):.2f}")
            self.strategy_board_weight_var.set(f"{float(self.config.strategy_probability_board_weight):.2f}")
            self.strategy_history_window_limit_var.set(str(int(self.config.strategy_history_window_limit)))
            self.strategy_scheduled_idle_lead_minutes_var.set(str(int(self.config.scheduled_idle_lead_minutes)))
            if hasattr(self, "strategy_loss_streak_limit_var"):
                self.strategy_loss_streak_limit_var.set(str(int(self.config.martingale_max_steps)))
        finally:
            self._strategy_profile_syncing = False
        self._refresh_strategy_custom_fields_visibility()
        self._refresh_strategy_summary()

    def _build_strategy_summary_text(self) -> str:
        profile_name = self._detect_strategy_profile_name()
        soft_idle_text = (
            "Off" if self.config.scheduled_idle_lead_minutes <= 0 else f"{self.config.scheduled_idle_lead_minutes}m"
        )
        if profile_name == "Custom":
            base_profile_name = self._detect_strategy_base_profile_name()
            if base_profile_name != "Custom":
                profile_name = f"Custom ({base_profile_name} base)"
        if self.strategy_randomize_enabled_var.get():
            return (
                f"Profile: {profile_name} | Random ON | Every active round picks a random color with no analysis or skip\n"
                f"SoftIdle={soft_idle_text} | Wednesday 03:00 mandatory idle retained"
            )
        return (
            f"Profile: {profile_name} | Data-driven | "
            f"Hit>={self.config.strategy_min_hit_probability:.2f} | "
            f"EV>={self.config.strategy_min_expected_value:.2f} | "
            f"Edge>={self.config.strategy_min_probability_edge:.2f} | "
            f"Samples>={self.config.strategy_probability_min_samples}\n"
            f"W={self.config.strategy_probability_window} | "
            f"HWin={'All' if self.config.strategy_history_window_limit <= 0 else self.config.strategy_history_window_limit} | "
            f"Prog={self._normalized_progression_profile_name()} | "
            f"MaxLoss={self.config.martingale_max_steps} | SoftIdle={soft_idle_text}"
        )

    def _build_soft_idle_countdown_text(self) -> str:
        if self.config.scheduled_idle_lead_minutes <= 0:
            return "Due: off"
        if not hasattr(self, "engine"):
            return "Due: --"

        if self.engine._is_any_idle_waiting_for_win():
            return "Due: win wait"
        if self.engine._scheduled_idle_waiting_for_profit:
            return "Due: extend"

        live_betting_states = (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )
        if self.engine.state.state not in live_betting_states:
            return "Due: idle"

        started_at = self.engine._soft_idle_runtime_started_at
        if started_at is None:
            return "Due: --"

        total_seconds = max(0, int(self.config.scheduled_idle_lead_minutes) * 60)
        elapsed_seconds = max(0, int(time.time() - started_at))
        remaining_seconds = max(0, total_seconds - elapsed_seconds)
        minutes, seconds = divmod(remaining_seconds, 60)
        return f"Due: {minutes:02d}m {seconds:02d}s"

    def _refresh_soft_idle_countdown(self):
        if hasattr(self, "strategy_soft_idle_due_var"):
            self.strategy_soft_idle_due_var.set(self._build_soft_idle_countdown_text())

    def _build_strategy_progression_preview(self) -> List[int]:
        return list(self.engine.bet_amount_steps)

    def _build_strategy_info_text(self) -> str:
        profile_name = self._detect_strategy_profile_name()
        base_profile_name = self._detect_strategy_base_profile_name()
        current_profile = (
            f"{profile_name} ({base_profile_name} base)"
            if profile_name == "Custom" and base_profile_name != "Custom"
            else profile_name
        )

        lines = [
            "Strategy Controls Help",
            "",
            "Current Setup",
            f"- Profile: {current_profile}",
            f"- Hit >= {self.config.strategy_min_hit_probability:.2f}",
            f"- EV >= {self.config.strategy_min_expected_value:.2f}",
            f"- Edge >= {self.config.strategy_min_probability_edge:.3f}",
            f"- Samples >= {self.config.strategy_probability_min_samples}",
            f"- Window = {self.config.strategy_probability_window}",
            f"- Board Weight = {self.config.strategy_probability_board_weight:.2f}",
            f"- Progression Profile = {self._normalized_progression_profile_name()}",
            f"- Max Loss Streak = {self.config.martingale_max_steps}",
            f"- History Windows = {'all complete windows' if self.config.strategy_history_window_limit <= 0 else self.config.strategy_history_window_limit}",
            f"- Soft Idle Minutes = {'off' if self.config.scheduled_idle_lead_minutes <= 0 else f'{self.config.scheduled_idle_lead_minutes} minutes'}",
            "- Soft idle starts after that many elapsed runtime minutes in the active live mode.",
            "- Wednesday 03:00 mandatory idle remains separate and can still stop the run later.",
            "",
            "Decision Flow",
            "1. The live data-driven model scans recent board history and scores colors.",
            "2. The live probability gate checks Samples, Hit, EV, and Edge.",
            "3. If a color passes, history analysis reviews Result Boxes behavior.",
            "4. History can keep the pick, warn about it, or positively swap to a stronger color.",
            "5. The active progression picks the amount, then the result is logged to CSV.",
            "",
            "How It Analyzes",
            "- Live model uses a rolling recent window from the board history.",
            "- History analysis reads complete 99-row windows from simulate_results.csv.",
            "- WARMUP, COOLDOWN, and SKIP rows are included for Result Boxes transitions.",
            "- Settled WIN/LOSE rows drive amount recovery and profit-side analysis.",
            "- During warmup, the CSV is re-analyzed only when the file changed.",
            "- If requested history windows exceed valid complete windows, analysis falls back to the available complete windows and reports that in History Analysis.",
            "- With enough history, CSV analysis can apply small temporary live threshold adjustments; base config remains the anchor.",
            "- Those live adjustments are bounded, regime-aware, amount-aware, and do not rewrite app_config.json on each warmup.",
            "- If Learning mode is ON, those history-driven live threshold adjustments are skipped to avoid double-tuning.",
            "",
            "Progression Notes",
            f"- Selected profile: {self._normalized_progression_profile_name()}.",
            f"- Base Martingale start setting remains {self.config.martingale_start}.",
            f"- Max steps before halt/reset logic: {self.config.martingale_max_steps}.",
            f"- Max bet limit: {self.config.max_bet_per_round}.",
            "- The fixed progression is used as-is with no Fibonacci switching.",
            "",
            "Progression Reference",
            "Format: consecutive-loss bet amounts",
        ]
        lines.append(
            "- Sequence: " + ", ".join(str(amount) for amount in self._build_strategy_progression_preview())
        )

        lines.extend([
            "",
            "Interpretation",
            "- Profiles tune only the live probability and history gates.",
            "- Progression stays fixed unless the hardcoded sequence is edited in code.",
        ])

        return "\n".join(lines)

    def _show_strategy_info_dialog(self):
        info_window = tk.Toplevel(self.root)
        info_window.title("Strategy Controls Help")
        info_window.geometry("760x640")
        info_window.transient(self.root)
        info_window.attributes("-topmost", True)

        container = ttk.Frame(info_window, padding=10)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        text_widget = tk.Text(
            container,
            wrap="word",
            font=("Consolas", 9),
            padx=8,
            pady=8,
        )
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=text_widget.yview)
        text_widget.configure(yscrollcommand=scrollbar.set)
        text_widget.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        text_widget.insert("1.0", self._build_strategy_info_text())
        text_widget.configure(state="disabled")

        ttk.Button(container, text="Close", command=info_window.destroy).grid(
            row=1, column=0, sticky="e", pady=(10, 0)
        )

    def _refresh_strategy_summary(self):
        if hasattr(self, "strategy_summary_var"):
            self.strategy_summary_var.set(self._build_strategy_summary_text())

    def _toggle_strategy_custom_fields(self):
        if not self._is_strategy_custom_toggle_allowed():
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: Show Fields is disabled while Simulate, AutoClick, or AutoSim is running"
                )
            return
        selected = (self.strategy_profile_var.get().strip() if hasattr(self, "strategy_profile_var") else "") or "Custom"
        if selected != "Custom":
            return
        self._strategy_custom_fields_force_hidden = not self._strategy_custom_fields_force_hidden
        self._refresh_strategy_custom_fields_visibility()

    def _is_scheduled_idle_lead_edit_allowed(self) -> bool:
        if not hasattr(self, "engine"):
            return True

        live_betting_states = (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )
        if self.engine.state.state in live_betting_states:
            return (
                self.engine._is_cycle_warmup_active()
                and not self.engine._has_unresolved_pending_bet()
            )

        return True

    def _is_history_window_limit_edit_allowed(self) -> bool:
        if getattr(self, "strategy_randomize_enabled_var", None) and self.strategy_randomize_enabled_var.get():
            return False

        if not hasattr(self, "engine"):
            return True

        live_betting_states = (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )
        if self.engine.state.state in live_betting_states:
            return self.engine._is_cycle_warmup_active()

        return True

    def _is_random_mode_edit_allowed(self) -> bool:
        if not hasattr(self, "engine"):
            return True

        live_betting_states = (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )
        if self.engine.state.state in live_betting_states:
            return self.engine._is_cycle_warmup_active()

        return True

    def _is_live_betting_mode_active(self) -> bool:
        if not hasattr(self, "engine"):
            return False
        return self.engine.state.state in (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )

    def _is_strategy_custom_toggle_allowed(self) -> bool:
        return not self._is_live_betting_mode_active()

    def _is_history_trim_allowed(self) -> bool:
        return not self._is_live_betting_mode_active()

    def _is_progression_profile_edit_allowed(self) -> bool:
        if not hasattr(self, "engine"):
            return True

        live_betting_states = (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )
        if self.engine.state.state in live_betting_states:
            return (
                self.engine._is_cycle_warmup_active()
                or self.engine._is_cycle_bet_cooldown_active()
            )

        return True

    def _refresh_history_window_limit_entry_state(self):
        history_window_entry = getattr(self, "strategy_history_window_limit_entry", None)
        if not history_window_entry:
            return

        if self._is_history_window_limit_edit_allowed():
            history_window_entry.state(["!disabled"])
        else:
            history_window_entry.state(["disabled"])

    def _refresh_progression_profile_control_state(self):
        progression_combo = getattr(self, "strategy_progression_profile_combo", None)
        if not progression_combo:
            return
        if self._is_progression_profile_edit_allowed():
            progression_combo.state(["!disabled", "readonly"])
        else:
            progression_combo.state(["disabled"])

    def _refresh_scheduled_idle_lead_entry_state(self):
        scheduled_idle_entry = getattr(self, "strategy_scheduled_idle_lead_entry", None)
        if not scheduled_idle_entry:
            return

        if self._is_scheduled_idle_lead_edit_allowed():
            scheduled_idle_entry.state(["!disabled"])
        else:
            scheduled_idle_entry.state(["disabled"])

    def _refresh_loss_streak_limit_entry_state(self):
        loss_streak_entry = getattr(self, "strategy_loss_streak_limit_entry", None)
        if not loss_streak_entry:
            return

        if self._is_progression_profile_edit_allowed():
            loss_streak_entry.state(["!disabled"])
        else:
            loss_streak_entry.state(["disabled"])

    def _refresh_history_trim_button_state(self):
        trim_canvas = getattr(self, "info_trim_canvas", None)
        trim_oval = getattr(self, "_info_trim_oval_id", None)
        trim_text = getattr(self, "_info_trim_text_id", None)
        if not trim_canvas or trim_oval is None or trim_text is None:
            return

        if self._is_history_trim_allowed():
            trim_canvas.configure(cursor="hand2")
            trim_canvas.itemconfigure(trim_oval, fill="#16a34a", outline="#15803d")
            trim_canvas.itemconfigure(trim_text, fill="white")
        else:
            trim_canvas.configure(cursor="arrow")
            trim_canvas.itemconfigure(trim_oval, fill="#9ca3af", outline="#6b7280")
            trim_canvas.itemconfigure(trim_text, fill="#f9fafb")

    def _refresh_strategy_custom_fields_visibility(self):
        frame = getattr(self, "strategy_custom_fields_frame", None)
        button = getattr(self, "strategy_custom_toggle_btn", None)
        if not frame:
            return
        selected = (self.strategy_profile_var.get().strip() if hasattr(self, "strategy_profile_var") else "") or "Custom"
        should_show = selected == "Custom" and not self._strategy_custom_fields_force_hidden
        if should_show:
            frame.grid()
        else:
            frame.grid_remove()
        if button:
            if selected == "Custom" and self._is_strategy_custom_toggle_allowed():
                button.state(["!disabled"])
                button.configure(text="Hide Fields" if should_show else "Show Fields")
            else:
                button.state(["disabled"])
                button.configure(text="Show Fields")
        self._refresh_history_window_limit_entry_state()
        self._refresh_progression_profile_control_state()
        self._refresh_scheduled_idle_lead_entry_state()
        self._refresh_loss_streak_limit_entry_state()
        self._refresh_history_trim_button_state()

    def _apply_custom_strategy_fields(self):
        try:
            candidate_data = self.config.to_dict()
            candidate_data.update({
                "strategy_randomize_enabled": bool(self.config.strategy_randomize_enabled),
                "strategy_probability_window": int(self.strategy_probability_window_var.get().strip()),
                "strategy_probability_min_samples": int(self.strategy_probability_samples_var.get().strip()),
                "strategy_min_hit_probability": float(self.strategy_min_hit_probability_var.get().strip()),
                "strategy_min_expected_value": float(self.strategy_min_expected_value_var.get().strip()),
                "strategy_min_probability_edge": float(self.strategy_min_probability_edge_var.get().strip()),
                "strategy_probability_board_weight": float(self.strategy_board_weight_var.get().strip()),
                "strategy_history_window_limit": int(self.strategy_history_window_limit_var.get().strip()),
            })
            validated = AppConfig.from_dict(candidate_data)
        except ValueError as exc:
            messagebox.showerror("Invalid Custom Profile", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Invalid Custom Profile", f"Could not apply custom values: {exc}")
            return

        editable_fields = (
            "strategy_probability_window",
            "strategy_probability_min_samples",
            "strategy_min_hit_probability",
            "strategy_min_expected_value",
            "strategy_min_probability_edge",
            "strategy_probability_board_weight",
            "strategy_history_window_limit",
        )
        for field_name in editable_fields:
            setattr(self.config, field_name, getattr(validated, field_name))

        self._history_analysis_last_csv_mtime = None
        self._strategy_custom_fields_force_hidden = True
        self.config_dirty = True
        self._save_config()
        self._populate_strategy_vars_from_config()
        if hasattr(self, "status_var"):
            profile_name = self._detect_strategy_profile_name()
            if profile_name == "Custom":
                self.status_var.set("Status: Custom strategy applied - select Custom again to edit")
            else:
                self.status_var.set(f"Status: Strategy now matches profile {profile_name}")
        logger.info(
            "Custom strategy fields applied: window=%s samples=%s hit=%.2f ev=%.2f edge=%.2f board_weight=%.2f history_windows=%s",
            self.config.strategy_probability_window,
            self.config.strategy_probability_min_samples,
            self.config.strategy_min_hit_probability,
            self.config.strategy_min_expected_value,
            self.config.strategy_min_probability_edge,
            self.config.strategy_probability_board_weight,
            self.config.strategy_history_window_limit,
        )

    def _apply_history_window_limit_selection(self):
        if getattr(self, "_strategy_profile_syncing", False):
            return
        if not self._is_history_window_limit_edit_allowed():
            self._populate_strategy_vars_from_config()
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: Hist Windows can only be edited during warmup while Simulate, AutoClick, or AutoSim is running"
                )
            return
        try:
            candidate_data = self.config.to_dict()
            candidate_data["strategy_history_window_limit"] = int(self.strategy_history_window_limit_var.get().strip())
            validated = AppConfig.from_dict(candidate_data)
        except ValueError as exc:
            messagebox.showerror("Invalid History Window Limit", str(exc))
            self._populate_strategy_vars_from_config()
            return
        except Exception as exc:
            messagebox.showerror("Invalid History Window Limit", f"Could not apply history window limit: {exc}")
            self._populate_strategy_vars_from_config()
            return

        self.config.strategy_history_window_limit = validated.strategy_history_window_limit
        self._history_analysis_last_csv_mtime = None
        self._populate_strategy_vars_from_config()
        if hasattr(self, "status_var"):
            if self.config.strategy_history_window_limit <= 0:
                self.status_var.set("Status: History windows set to all complete windows")
            else:
                self.status_var.set(
                    f"Status: History windows set to latest {self.config.strategy_history_window_limit} complete windows"
                )
        logger.info("Runtime history window limit set to %s", self.config.strategy_history_window_limit)

    def _apply_progression_profile_selection(self):
        if getattr(self, "_strategy_profile_syncing", False):
            return
        if not self._is_progression_profile_edit_allowed():
            self._populate_strategy_vars_from_config()
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: Progression can only be changed while idle, warmup, or cooldown"
                )
            return

        selected = (self.strategy_progression_profile_var.get().strip() if hasattr(self, "strategy_progression_profile_var") else "")
        if selected not in PROGRESSION_PROFILES:
            self._populate_strategy_vars_from_config()
            return

        self.config.strategy_progression_profile = selected
        self.config_dirty = True
        self._save_config()
        self._rebuild_engine_progression_steps()
        self._populate_strategy_vars_from_config()
        if hasattr(self, "status_var"):
            self.status_var.set(f"Status: Progression profile {selected}")
        logger.info("Progression profile set to %s", selected)

    def _apply_loss_streak_limit_selection(self):
        if getattr(self, "_strategy_profile_syncing", False):
            return
        if not self._is_progression_profile_edit_allowed():
            self._populate_strategy_vars_from_config()
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: Max loss streak can only be changed while idle, warmup, or cooldown"
                )
            return
        try:
            candidate_data = self.config.to_dict()
            candidate_data["martingale_max_steps"] = int(self.strategy_loss_streak_limit_var.get().strip())
            validated = AppConfig.from_dict(candidate_data)
        except ValueError as exc:
            messagebox.showerror("Invalid Max Loss Streak", str(exc))
            self._populate_strategy_vars_from_config()
            return
        except Exception as exc:
            messagebox.showerror("Invalid Max Loss Streak", f"Could not apply max loss streak: {exc}")
            self._populate_strategy_vars_from_config()
            return

        self.config.martingale_max_steps = validated.martingale_max_steps
        self.config_dirty = True
        self._save_config()
        self._rebuild_engine_progression_steps()
        self.engine._save_session_state()
        self._populate_strategy_vars_from_config()
        if hasattr(self, "status_var"):
            self.status_var.set(f"Status: Max loss streak set to {self.config.martingale_max_steps}")
        logger.info("Max loss streak set to %s", self.config.martingale_max_steps)

    def _apply_scheduled_idle_lead_minutes_selection(self):
        if getattr(self, "_strategy_profile_syncing", False):
            return
        if not self._is_scheduled_idle_lead_edit_allowed():
            self._populate_strategy_vars_from_config()
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: Soft idle minutes can only be edited during warmup with no pending bet while Simulate, AutoClick, or AutoSim is running"
                )
            return
        try:
            candidate_data = self.config.to_dict()
            candidate_data["scheduled_idle_lead_minutes"] = int(self.strategy_scheduled_idle_lead_minutes_var.get().strip())
            validated = AppConfig.from_dict(candidate_data)
        except ValueError as exc:
            messagebox.showerror("Invalid Soft Idle Minutes", str(exc))
            self._populate_strategy_vars_from_config()
            return
        except Exception as exc:
            messagebox.showerror("Invalid Soft Idle Minutes", f"Could not apply soft idle minutes: {exc}")
            self._populate_strategy_vars_from_config()
            return

        preserve_active_extension = bool(
            self.engine._scheduled_idle_waiting_for_profit or self.engine._soft_idle_waiting_for_win
        )
        self.config.scheduled_idle_lead_minutes = validated.scheduled_idle_lead_minutes
        if not preserve_active_extension:
            self.engine._soft_idle_triggered_for_run = False
            self.engine._soft_idle_waiting_for_win = False
            self.engine._scheduled_idle_waiting_for_profit = False
        self.config_dirty = True
        self._save_config()
        self.engine._save_session_state()
        self._populate_strategy_vars_from_config()
        if hasattr(self, "status_var"):
            if self.config.scheduled_idle_lead_minutes <= 0:
                if preserve_active_extension:
                    self.status_var.set(
                        "Status: Soft idle minutes disabled; active soft-idle extension remains in effect until it resolves"
                    )
                else:
                    self.status_var.set("Status: Soft idle minutes disabled; only Wednesday 03:00 mandatory idle remains")
            else:
                if preserve_active_extension:
                    self.status_var.set(
                        f"Status: Soft idle minutes set to {self.config.scheduled_idle_lead_minutes}; active soft-idle extension preserved"
                    )
                else:
                    self.status_var.set(
                        f"Status: Soft idle minutes set to {self.config.scheduled_idle_lead_minutes}"
                    )
        logger.info("Scheduled idle lead minutes set to %s", self.config.scheduled_idle_lead_minutes)

    def _apply_random_toggle(self):
        if getattr(self, "_strategy_profile_syncing", False):
            return
        if not self._is_random_mode_edit_allowed():
            self._populate_strategy_vars_from_config()
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Status: Random mode can only be changed during warmup while Simulate, AutoClick, or AutoSim is running"
                )
            return
        enabled = bool(self.strategy_randomize_enabled_var.get())
        self.config.strategy_randomize_enabled = enabled
        self.config_dirty = True
        self._save_config()
        self._populate_strategy_vars_from_config()
        if hasattr(self, "status_var"):
            self.status_var.set(f"Status: Random {'ON' if enabled else 'OFF'}")
        logger.info("Random mode %s", "ENABLED" if enabled else "DISABLED")

    def _load_calibration(self):
        if not self.calibration_path.exists():
            return
        try:
            with open(self.calibration_path, 'r') as f:
                data = json.load(f)

            brave_rect = data.get("brave_window_rect")
            if (
                isinstance(brave_rect, list)
                and len(brave_rect) == 4
                and all(isinstance(value, int) for value in brave_rect)
            ):
                self._saved_brave_rect = tuple(brave_rect)
            else:
                self._saved_brave_rect = None
            
            self.capture_mgr.clear_calibration()

            point_entries = [
                point_data
                for point_data in data.get("points", [])
                if point_data.get("name") != "Gray"
            ]

            for i, point_data in enumerate(point_entries):
                point = CalibrationPoint.from_dict(i, point_data)
                self.capture_mgr.add_calibration_point(point)
                if point.rgb_sample and point.name in self.config.sampled_reference_labels:
                    self.color_matcher.set_reference(point.name, point.rgb_sample)
            
            loaded_points = len(self.capture_mgr.calibration_points)
            expected_points = self.config.total_calibration_points
            has_blank_calibration = any(
                point.name == self.config.blank_color_label
                for point in self.capture_mgr.calibration_points.values()
            )
            if loaded_points >= expected_points:
                self.status_var.set(f"Status: Calibration loaded ({loaded_points}/{expected_points} points)")
            elif loaded_points == expected_points - 1 and not has_blank_calibration:
                self.status_var.set(
                    f"Status: Partial calibration ({loaded_points}/{expected_points} points) - blank calibration missing"
                )
            else:
                self.status_var.set(f"Status: Partial calibration ({loaded_points}/{expected_points} points)")
            logger.info(f"Loaded {loaded_points} calibration points")
        except Exception as e:
            logger.error(f"Failed to load calibration: {e}")
    
    def _save_calibration(self):
        brave_rect = self._calibration_brave_rect or self._saved_brave_rect
        if brave_rect is not None:
            self._saved_brave_rect = brave_rect

        data = {
            "brave_window_rect": list(brave_rect) if brave_rect is not None else None,
            "points": [
                {"name": p.name, "x": p.x, "y": p.y, "rgb_sample": list(p.rgb_sample) if p.rgb_sample else None} 
                for p in sorted(self.capture_mgr.calibration_points.values(), key=lambda p: p.index)
            ],
        }
        try:
            with open(self.calibration_path, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info("Calibration saved")
        except Exception as e:
            logger.error(f"Failed to save calibration: {e}")

    def get_saved_brave_rect(self) -> Optional[Tuple[int, int, int, int]]:
        return self._saved_brave_rect

    def _stop_calibration_window_lock(self):
        if self._calibration_lock_after_id:
            try:
                self.root.after_cancel(self._calibration_lock_after_id)
            except Exception:
                pass
        self._calibration_lock_after_id = None
        self._calibration_brave_hwnd = None
        self._calibration_brave_rect = None

    def _schedule_calibration_window_lock(self):
        self._calibration_lock_after_id = None

        if not self._calibration_window or not self._calibration_window.winfo_exists():
            self._stop_calibration_window_lock()
            return

        hwnd = self._calibration_brave_hwnd
        target_rect = self._calibration_brave_rect
        if os.name != "nt" or not hwnd or not target_rect:
            return

        try:
            current_rect = get_window_rect(hwnd)
            if current_rect and current_rect != target_rect:
                if restore_window_rect(hwnd, target_rect):
                    logger.info("Calibration lock restored Brave window to %s", target_rect)
        except Exception:
            logger.exception("Failed to enforce Brave calibration window lock")

        self._calibration_lock_after_id = self.root.after(250, self._schedule_calibration_window_lock)

    def _start_calibration_window_lock(self):
        self._stop_calibration_window_lock()

        if os.name != "nt":
            return

        preferred_hwnd = int(user32.GetForegroundWindow() or 0) if os.name == "nt" else 0
        hwnd = find_brave_window(preferred_hwnd=preferred_hwnd)
        if not hwnd:
            logger.warning("Calibration lock skipped: Brave window not found")
            return

        rect = get_window_rect(hwnd)
        if not rect:
            logger.warning("Calibration lock skipped: could not read Brave window bounds")
            return

        self._calibration_brave_hwnd = hwnd
        self._calibration_brave_rect = rect
        restore_window_rect(hwnd, rect, bring_to_front=True)
        logger.info("Calibration lock pinned Brave window at %s", rect)
        self._schedule_calibration_window_lock()

    def _close_calibration_window(self):
        self._stop_calibration_window_lock()
        if self._calibration_window:
            try:
                self._calibration_window.destroy()
            except Exception:
                pass
            self._calibration_window = None

    def _on_main_panel_configure(self, _event=None):
        if hasattr(self, "_main_canvas"):
            self._main_canvas.configure(scrollregion=self._main_canvas.bbox("all"))

    def _on_main_canvas_configure(self, event):
        if hasattr(self, "_main_canvas_window"):
            self._main_canvas.itemconfigure(self._main_canvas_window, width=event.width)

    def _on_main_panel_mousewheel(self, event):
        if not hasattr(self, "_main_canvas"):
            return

        delta = 0
        if getattr(event, "delta", 0):
            delta = int(-event.delta / 120)
        elif getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1

        if delta:
            self._main_canvas.yview_scroll(delta, "units")

    def _setup_ui(self):
        self.root.title("Booty Bot with Analytics v5.43")
        window_width = 560
        desired_window_height = 636
        min_window_height = 540
        screen_margin = 20
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        available_height = max(min_window_height, screen_height - (screen_margin * 2))
        window_height = min(desired_window_height, available_height)
        x_offset = max(screen_margin, screen_width - window_width - screen_margin)
        y_offset = max(screen_margin, (screen_height - window_height) // 2)
        self.root.geometry(f"{window_width}x{window_height}+{x_offset}+{y_offset}")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, True)

        style = ttk.Style()
        style.theme_use('clam')
        app_bg = "#f2f2f2"
        self.root.configure(bg=app_bg)
        style.configure("TFrame", background=app_bg)
        style.configure("TLabel", background=app_bg)
        style.configure("TLabelframe", background=app_bg)
        style.configure("TLabelframe.Label", background=app_bg)
        style.configure("TNotebook", background=app_bg)
        style.configure("TNotebook.Tab", background=app_bg)

        self._main_container = ttk.Frame(self.root)
        self._main_container.pack(fill="both", expand=True)

        self._main_canvas = tk.Canvas(
            self._main_container,
            highlightthickness=0,
            borderwidth=0,
            bg=app_bg,
        )
        self._main_scrollbar = ttk.Scrollbar(self._main_container, orient="vertical", command=self._main_canvas.yview)
        self._main_canvas.configure(yscrollcommand=self._main_scrollbar.set)
        self._main_scrollbar.pack(side="right", fill="y")
        self._main_canvas.pack(side="left", fill="both", expand=True)

        self._main_panel = ttk.Frame(self._main_canvas)
        self._main_canvas_window = self._main_canvas.create_window((0, 0), window=self._main_panel, anchor="nw")
        self._main_panel.bind("<Configure>", self._on_main_panel_configure)
        self._main_canvas.bind("<Configure>", self._on_main_canvas_configure)
        self.root.bind_all("<MouseWheel>", self._on_main_panel_mousewheel)
        self.root.bind_all("<Button-4>", self._on_main_panel_mousewheel)
        self.root.bind_all("<Button-5>", self._on_main_panel_mousewheel)

        panel_parent = self._main_panel

        button_frame = ttk.Frame(panel_parent)
        button_frame.pack(pady=10)
        
        self.calibrate_btn = None
        self.monitor_btn = None
        self.simulate_btn = None
        self.autoclick_btn = None
        self.autosim_btn = None
        self.reset_btn = None
        self.learning_btn = None
        self.lock_brave_btn = None

        button_specs = [
            ("Calibrate", self.start_calibration, "#f0ad4e", 0, 0, "calibrate"),
            ("Monitor", self.start_monitoring, "#5cb85c", 0, 1, "monitor"),
            ("Simulate", self.start_simulation, "#0275d8", 0, 2, "simulate"),
            ("AutoClick", self.start_autoclick, "#8e44ad", 0, 3, "autoclick"),
            ("Autosim", self.start_autosim, "#16a085", 0, 4, "autosim"),
            ("Reset", self.reset_records, "#f39c12", 1, 0, None),
            ("Analytics", self.show_decision_analytics, "#6c757d", 1, 1, None),
            ("Exit", self.exit_app, "#d9534f", 1, 4, None),
        ]
        
        for text, command, color, row, column, stop_action_name in button_specs:
            btn = tk.Button(
                button_frame,
                text=text,
                command=command,
                width=12,
                bg=color,
                fg="white",
                relief="raised",
                bd=1,
            )
            btn.grid(row=row, column=column, padx=2, pady=5)

            if stop_action_name:
                btn.bind("<Double-Button-1>", lambda e, action=stop_action_name: self.stop_action(action))

            if text == "Calibrate":
                self.calibrate_btn = btn
            elif text == "Monitor":
                self.monitor_btn = btn
            elif text == "Simulate":
                self.simulate_btn = btn
            elif text == "AutoClick":
                self.autoclick_btn = btn
            elif text == "Autosim":
                self.autosim_btn = btn
            elif text == "Reset":
                self.reset_btn = btn

        self.learning_mode_var = tk.BooleanVar(value=self.config.adaptive_learning_enabled)
        self.learning_btn = tk.Button(
            button_frame,
            command=self.toggle_learning_mode,
            width=12,
            fg="white",
            relief="raised",
            bd=1,
        )
        self.learning_btn.grid(row=1, column=2, padx=2, pady=5)
        self._refresh_learning_button()

        self.lock_brave_var = tk.BooleanVar(value=self.config.lock_brave_window_enabled)
        self.lock_brave_btn = tk.Button(
            button_frame,
            command=self.toggle_brave_window_lock,
            width=12,
            fg="white",
            relief="raised",
            bd=1,
        )
        self.lock_brave_btn.grid(row=1, column=3, padx=2, pady=5)
        self._refresh_lock_brave_button()

        self._strategy_profile_syncing = False
        self.strategy_profile_var = tk.StringVar(value=self._detect_strategy_profile_name())
        self.strategy_progression_profile_var = tk.StringVar(value=self._normalized_progression_profile_name())
        self.strategy_randomize_enabled_var = tk.BooleanVar(value=self.config.strategy_randomize_enabled)
        self.strategy_probability_window_var = tk.StringVar(value=str(self.config.strategy_probability_window))
        self.strategy_probability_samples_var = tk.StringVar(value=str(self.config.strategy_probability_min_samples))
        self.strategy_min_hit_probability_var = tk.StringVar(value=f"{self.config.strategy_min_hit_probability:.2f}")
        self.strategy_min_expected_value_var = tk.StringVar(value=f"{self.config.strategy_min_expected_value:.2f}")
        self.strategy_min_probability_edge_var = tk.StringVar(value=f"{self.config.strategy_min_probability_edge:.2f}")
        self.strategy_board_weight_var = tk.StringVar(value=f"{self.config.strategy_probability_board_weight:.2f}")
        self.strategy_history_window_limit_var = tk.StringVar(value=str(self.config.strategy_history_window_limit))
        self.strategy_scheduled_idle_lead_minutes_var = tk.StringVar(value=str(self.config.scheduled_idle_lead_minutes))
        self.strategy_loss_streak_limit_var = tk.StringVar(value=str(self.config.martingale_max_steps))
        self.strategy_soft_idle_due_var = tk.StringVar(value=self._build_soft_idle_countdown_text())
        self.strategy_cache_auto_clear_var = tk.StringVar(value=self._auto_cache_clear_status_text)

        strategy_frame = tk.LabelFrame(
            panel_parent,
            padx=10,
            pady=5,
            bd=1,
            relief="solid",
            bg=app_bg,
        )
        strategy_frame.pack(fill="x", padx=10, pady=(5, 5))
        strategy_legend_bg = strategy_frame.cget("bg")
        strategy_legend = tk.Frame(strategy_frame, bg=strategy_legend_bg)
        tk.Label(
            strategy_legend,
            text="Strategy Controls",
            font=("Arial", 9),
            bg=strategy_legend_bg,
        ).pack(side="left", padx=(4, 0), pady=(0, 1))
        strategy_info_canvas = tk.Canvas(
            strategy_legend,
            width=18,
            height=18,
            bg=strategy_legend_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        strategy_info_canvas.pack(side="left", padx=(5, 2), pady=(0, 1))
        strategy_info_canvas.create_oval(1, 1, 17, 17, fill="#2563eb", outline="#1d4ed8", width=1)
        strategy_info_canvas.create_text(9, 9, text="i", fill="white", font=("Arial", 8, "bold"))
        strategy_info_canvas.bind("<Button-1>", lambda _event: self._show_strategy_info_dialog())
        strategy_frame.configure(labelwidget=strategy_legend)
        strategy_frame.columnconfigure(0, weight=0)
        strategy_frame.columnconfigure(1, weight=1)
        strategy_frame.columnconfigure(2, weight=0)
        strategy_frame.columnconfigure(3, weight=1)
        strategy_frame.columnconfigure(4, weight=0)
        strategy_frame.columnconfigure(5, weight=1)
        strategy_frame.columnconfigure(6, weight=1)

        ttk.Label(strategy_frame, text="Profile:").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.strategy_profile_combo = ttk.Combobox(
            strategy_frame,
            textvariable=self.strategy_profile_var,
            values=("Default", "Aggressive", "Moderate", "Conservative", "Random", "Custom"),
            state="readonly",
            width=18,
        )
        self.strategy_profile_combo.grid(row=0, column=1, sticky="w", pady=(0, 6))
        self.strategy_profile_combo.bind("<<ComboboxSelected>>", self._on_strategy_profile_selected)

        ttk.Label(strategy_frame, text="Hist Windows:").grid(row=0, column=2, sticky="e", padx=(12, 6), pady=(0, 6))
        self.strategy_history_window_limit_entry = ttk.Entry(
            strategy_frame,
            textvariable=self.strategy_history_window_limit_var,
            width=10,
            justify="center",
        )
        self.strategy_history_window_limit_entry.grid(row=0, column=3, sticky="w", pady=(0, 6))
        self.strategy_history_window_limit_entry.bind(
            "<Return>",
            lambda _event: self._apply_history_window_limit_selection(),
        )
        self.strategy_history_window_limit_entry.bind(
            "<FocusOut>",
            lambda _event: self._apply_history_window_limit_selection(),
        )

        ttk.Label(strategy_frame, text="Progression:").grid(row=0, column=4, sticky="e", padx=(12, 6), pady=(0, 6))
        self.strategy_progression_profile_combo = ttk.Combobox(
            strategy_frame,
            textvariable=self.strategy_progression_profile_var,
            values=self._progression_profile_names(),
            state="readonly",
            width=16,
        )
        self.strategy_progression_profile_combo.grid(row=0, column=5, sticky="w", pady=(0, 6))
        self.strategy_progression_profile_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._apply_progression_profile_selection(),
        )

        controls_row = ttk.Frame(strategy_frame)
        controls_row.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(0, 6))
        controls_row.columnconfigure(0, weight=0)
        controls_row.columnconfigure(1, weight=0)
        controls_row.columnconfigure(2, weight=0)
        controls_row.columnconfigure(3, weight=0)
        controls_row.columnconfigure(4, weight=0)
        controls_row.columnconfigure(5, weight=0)
        controls_row.columnconfigure(6, weight=0)
        controls_row.columnconfigure(7, weight=0)
        controls_row.columnconfigure(8, weight=0)
        controls_row.columnconfigure(9, weight=1)
        self.strategy_custom_toggle_btn = ttk.Button(
            controls_row,
            text="Show Fields",
            command=self._toggle_strategy_custom_fields,
            width=10,
        )
        self.strategy_custom_toggle_btn.grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Label(controls_row, text="Idle:").grid(row=0, column=1, sticky="w", padx=(2, 4))
        self.strategy_scheduled_idle_lead_entry = ttk.Entry(
            controls_row,
            textvariable=self.strategy_scheduled_idle_lead_minutes_var,
            width=5,
            justify="center",
        )
        self.strategy_scheduled_idle_lead_entry.grid(row=0, column=2, sticky="w")
        self.strategy_scheduled_idle_lead_entry.bind(
            "<Return>",
            lambda _event: self._apply_scheduled_idle_lead_minutes_selection(),
        )
        self.strategy_scheduled_idle_lead_entry.bind(
            "<FocusOut>",
            lambda _event: self._apply_scheduled_idle_lead_minutes_selection(),
        )
        ttk.Label(controls_row, text="0=off |").grid(row=0, column=3, sticky="w", padx=(4, 4))
        ttk.Label(controls_row, text="MaxLoss:").grid(row=0, column=4, sticky="w", padx=(0, 4))
        self.strategy_loss_streak_limit_entry = ttk.Entry(
            controls_row,
            textvariable=self.strategy_loss_streak_limit_var,
            width=4,
            justify="center",
        )
        self.strategy_loss_streak_limit_entry.grid(row=0, column=5, sticky="w")
        self.strategy_loss_streak_limit_entry.bind(
            "<Return>",
            lambda _event: self._apply_loss_streak_limit_selection(),
        )
        self.strategy_loss_streak_limit_entry.bind(
            "<FocusOut>",
            lambda _event: self._apply_loss_streak_limit_selection(),
        )
        ttk.Label(controls_row, text="|").grid(row=0, column=6, sticky="w", padx=(6, 4))
        ttk.Label(
            controls_row,
            textvariable=self.strategy_soft_idle_due_var,
            width=12,
        ).grid(row=0, column=7, sticky="w", padx=(0, 0))
        ttk.Label(controls_row, text="|").grid(row=0, column=8, sticky="w", padx=(4, 4))
        ttk.Label(
            controls_row,
            textvariable=self.strategy_cache_auto_clear_var,
            width=12,
        ).grid(row=0, column=9, sticky="w", padx=(0, 0))

        self.strategy_summary_var = tk.StringVar(value=self._build_strategy_summary_text())
        ttk.Label(
            strategy_frame,
            textvariable=self.strategy_summary_var,
            font=("Arial", 8),
            wraplength=610,
            justify="left",
        ).grid(row=2, column=0, columnspan=7, sticky="ew", pady=(0, 4))

        self.strategy_custom_fields_frame = tk.LabelFrame(
            strategy_frame,
            text="Custom Fields",
            padx=8,
            pady=6,
            bd=1,
            relief="solid",
            bg=app_bg,
        )
        self.strategy_custom_fields_frame.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(6, 0))
        self.strategy_custom_fields_frame.columnconfigure(0, weight=1)

        self.strategy_custom_fields_inner = ttk.Frame(self.strategy_custom_fields_frame)
        self.strategy_custom_fields_inner.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        self.strategy_custom_fields_inner.columnconfigure(0, weight=0, minsize=110)
        self.strategy_custom_fields_inner.columnconfigure(1, weight=0, minsize=130)
        self.strategy_custom_fields_inner.columnconfigure(2, weight=0, minsize=130)
        self.strategy_custom_fields_inner.columnconfigure(3, weight=0, minsize=130)
        for column_index in range(4):
            self.strategy_custom_fields_inner.columnconfigure(column_index, weight=0)

        custom_fields = (
            ("Window", self.strategy_probability_window_var),
            ("Samples", self.strategy_probability_samples_var),
            ("Min Hit", self.strategy_min_hit_probability_var),
            ("Min EV", self.strategy_min_expected_value_var),
            ("Min Edge", self.strategy_min_probability_edge_var),
            ("Board Weight", self.strategy_board_weight_var),
        )
        self._strategy_custom_entries = []
        for index, (label_text, variable) in enumerate(custom_fields):
            row = index // 2
            column = (index % 2) * 2
            ttk.Label(self.strategy_custom_fields_inner, text=f"{label_text}:").grid(
                row=row,
                column=column,
                sticky="e",
                padx=(0, 10),
                pady=(0, 6),
            )
            entry = ttk.Entry(self.strategy_custom_fields_inner, textvariable=variable, width=12, justify="center")
            entry.grid(row=row, column=column + 1, sticky="w", padx=(0, 24), pady=(0, 6))
            entry.bind("<Return>", lambda _event: self._apply_custom_strategy_fields())
            self._strategy_custom_entries.append(entry)

        ttk.Button(
            self.strategy_custom_fields_inner,
            text="Apply Custom",
            command=self._apply_custom_strategy_fields,
        ).grid(row=3, column=0, columnspan=4, pady=(6, 0))
        ttk.Label(
            self.strategy_custom_fields_inner,
            text="Custom edits only the threshold fields below. History Windows stays above. Matching a preset relabels the profile automatically.",
            font=("Arial", 8),
            justify="center",
            wraplength=440,
        ).grid(row=4, column=0, columnspan=4, pady=(6, 0))
        self._refresh_strategy_custom_fields_visibility()

        status_frame = tk.LabelFrame(
            panel_parent,
            padx=10,
            pady=5,
            bd=1,
            relief="solid",
            bg=app_bg,
        )
        status_frame.pack(fill="x", padx=10, pady=(5, 5))
        status_legend_bg = status_frame.cget("bg")
        status_legend = tk.Frame(status_frame, bg=status_legend_bg)
        tk.Label(status_legend, text="Status", font=("Arial", 9), bg=status_legend_bg).pack(side="left")
        status_frame.configure(labelwidget=status_legend)
        self.status_var = tk.StringVar(value="Ready - Not calibrated")
        ttk.Label(status_frame, textvariable=self.status_var, font=("Arial", 9)).pack(anchor="w")
        self.display_round_var = tk.StringVar(value="Cycle Counter: 0")
        ttk.Label(status_frame, textvariable=self.display_round_var, font=("Arial", 9)).pack(anchor="w")
        
        game_frame = tk.LabelFrame(
            panel_parent,
            padx=10,
            pady=5,
            bd=1,
            relief="solid",
            bg=app_bg,
        )
        game_frame.pack(fill="x", padx=10, pady=(5, 5))
        game_legend_bg = game_frame.cget("bg")
        game_legend = tk.Frame(game_frame, bg=game_legend_bg)
        tk.Label(game_legend, text="Game State", font=("Arial", 9), bg=game_legend_bg).pack(side="left")
        game_frame.configure(labelwidget=game_legend)
        self.columns_var = tk.StringVar(value="Columns: Not scanned")
        ttk.Label(game_frame, textvariable=self.columns_var, font=("Arial", 8)).pack(anchor="w")
        self.columns_var_row2 = tk.StringVar(value="")
        ttk.Label(game_frame, textvariable=self.columns_var_row2, font=("Arial", 8)).pack(anchor="w")
        self.columns_var_row3 = tk.StringVar(value="")
        ttk.Label(game_frame, textvariable=self.columns_var_row3, font=("Arial", 8)).pack(anchor="w")
        self.blank_area_var = tk.StringVar(value="Blank Area: Not scanned")
        ttk.Label(game_frame, textvariable=self.blank_area_var, font=("Arial", 8)).pack(anchor="w")
        self.confidence_var = tk.StringVar(value="Confidence: 0%")
        ttk.Label(game_frame, textvariable=self.confidence_var, font=("Arial", 8)).pack(anchor="w")

        stats_frame = tk.LabelFrame(
            panel_parent,
            padx=10,
            pady=5,
            bd=1,
            relief="solid",
            bg=app_bg,
        )
        stats_frame.pack(fill="x", padx=10, pady=(5, 5))
        stats_legend_bg = stats_frame.cget("bg")
        stats_legend = tk.Frame(stats_frame, bg=stats_legend_bg)
        tk.Label(stats_legend, text="Statistics", font=("Arial", 9), bg=stats_legend_bg).pack(side="left")
        stats_frame.configure(labelwidget=stats_legend)
        self.rounds_var = tk.StringVar(value="Rounds: 0")
        self.win_lose_var = tk.StringVar(value="Wins: 0 | Loses: 0")
        self.profit_var = tk.StringVar(value="Profit: 0.00")
        self.martingale_var = tk.StringVar(value="Martingale: 5")
        self.current_bet_var = tk.StringVar(value="Current Bet: None")
        self.pending_bet_var = tk.StringVar(value="Pending Bet: None")
        self.last_c1_var = tk.StringVar(value="Last Result Boxes: None")
        self.decision_var = tk.StringVar(value="Decision: Waiting for C1 history")
        stats_grid = ttk.Frame(stats_frame)
        stats_grid.pack(fill="x", expand=True)
        stats_grid.columnconfigure(0, weight=1)
        stats_grid.columnconfigure(1, weight=2)

        stats_left = ttk.Frame(stats_grid)
        stats_left.grid(row=0, column=0, sticky="nw", padx=(0, 12))

        stats_right = ttk.Frame(stats_grid)
        stats_right.grid(row=0, column=1, sticky="nsew")
        stats_right.columnconfigure(0, weight=1)

        ttk.Label(stats_left, textvariable=self.rounds_var, font=("Arial", 8), justify="left").pack(anchor="w")
        ttk.Label(stats_left, textvariable=self.win_lose_var, font=("Arial", 8), justify="left").pack(anchor="w")
        ttk.Label(stats_left, textvariable=self.profit_var, font=("Arial", 8), justify="left").pack(anchor="w")
        ttk.Label(stats_left, textvariable=self.martingale_var, font=("Arial", 8), justify="left", wraplength=175).pack(anchor="w")

        ttk.Label(stats_right, textvariable=self.current_bet_var, font=("Arial", 8), justify="left", wraplength=320).grid(row=0, column=0, sticky="w")
        ttk.Label(stats_right, textvariable=self.pending_bet_var, font=("Arial", 8), justify="left", wraplength=320).grid(row=1, column=0, sticky="w")
        ttk.Label(stats_right, textvariable=self.last_c1_var, font=("Arial", 8), justify="left", wraplength=320).grid(row=2, column=0, sticky="w")
        ttk.Label(
            stats_frame,
            textvariable=self.decision_var,
            font=("Arial", 8),
            justify="left",
            wraplength=500,
        ).pack(anchor="w", fill="x", pady=(4, 0))

        info_frame = tk.LabelFrame(
            panel_parent,
            padx=10,
            pady=5,
            bd=1,
            relief="solid",
            bg=app_bg,
        )
        info_frame.pack(fill="x", expand=False, padx=10, pady=(5, 10))
        info_legend_bg = info_frame.cget("bg")
        info_legend = tk.Frame(info_frame, bg=info_legend_bg)
        tk.Label(info_legend, text="Information", font=("Arial", 9), bg=info_legend_bg).pack(side="left")
        self.info_trim_canvas = tk.Canvas(
            info_legend,
            width=18,
            height=18,
            bg=info_legend_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.info_trim_canvas.pack(side="left", padx=(5, 2), pady=(0, 1))
        self._info_trim_oval_id = self.info_trim_canvas.create_oval(
            1, 1, 17, 17, fill="#16a34a", outline="#15803d", width=1
        )
        self._info_trim_text_id = self.info_trim_canvas.create_text(
            9, 9, text="C", fill="white", font=("Arial", 8, "bold")
        )
        self.info_trim_canvas.bind("<Button-1>", lambda _event: self._trim_history_csv_to_complete_windows())
        info_frame.configure(labelwidget=info_legend)
        self.calib_info_var = tk.StringVar(value=f"Calibration: 0/{self.config.total_columns * self.config.boxes_per_column} grid + 0/{len(self.config.extra_calibration_labels)} extra")
        ttk.Label(info_frame, textvariable=self.calib_info_var, font=("Arial", 8)).pack(anchor="w")
        self.history_analysis_status_var = tk.StringVar(value=self._history_analysis_last_summary)
        self.history_analysis_basis_var = tk.StringVar(value=self._history_analysis_last_basis)
        ttk.Label(
            info_frame,
            textvariable=self.history_analysis_status_var,
            font=("Arial", 8),
            justify="left",
            wraplength=520,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            info_frame,
            textvariable=self.history_analysis_basis_var,
            font=("Arial", 8),
            justify="left",
            wraplength=520,
        ).pack(anchor="w")
    
    def _refresh_learning_button(self):
        if not self.learning_btn:
            return

        enabled = self.learning_mode_var.get()
        label = "Learning: ON" if enabled else "Learning: OFF"
        color = "#16a34a" if enabled else "#6b7280"
        self.learning_btn.config(text=label, bg=color, activebackground=color)

    def _refresh_lock_brave_button(self):
        if not self.lock_brave_btn:
            return

        enabled = self.lock_brave_var.get()
        label = "Lock Brave: ON" if enabled else "Lock Brave: OFF"
        color = "#0f766e" if enabled else "#6b7280"
        self.lock_brave_btn.config(text=label, bg=color, activebackground=color)

    def toggle_learning_mode(self):
        enabled = not self.learning_mode_var.get()
        self.learning_mode_var.set(enabled)
        self.engine.learning_mode = enabled
        self.config.adaptive_learning_enabled = enabled
        self.config_dirty = True
        self._save_config()
        self._refresh_learning_button()
        status = "ENABLED" if self.engine.learning_mode else "DISABLED"
        self.status_var.set(f"Status: Adaptive Learning {status}")
        logger.info(f"Adaptive learning mode {status}")

    def toggle_brave_window_lock(self):
        enabled = not self.lock_brave_var.get()
        self.lock_brave_var.set(enabled)
        self.config.lock_brave_window_enabled = enabled
        self.config_dirty = True
        self._save_config()
        self._refresh_lock_brave_button()

        if enabled:
            armed = self.engine._arm_brave_window_lock("manual toggle")
            message = "Status: Brave window lock ENABLED" if armed else "Status: Brave lock enabled (Brave not found)"
        else:
            self.engine._clear_brave_window_lock()
            message = "Status: Brave window lock DISABLED"

        self.status_var.set(message)
        logger.info("Brave window lock %s", "ENABLED" if enabled else "DISABLED")
    
    def show_decision_analytics(self):
        analytics = self.engine.get_decision_analytics()
        
        dashboard = tk.Toplevel(self.root)
        dashboard.title("Decision Analytics Dashboard")
        dashboard.geometry("800x600")
        dashboard.attributes("-topmost", True)
        
        notebook = ttk.Notebook(dashboard)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Tab 1: Performance
        perf_frame = ttk.Frame(notebook)
        notebook.add(perf_frame, text="Performance")
        
        metrics_frame = ttk.LabelFrame(perf_frame, text="Real-time Metrics", padding=10)
        metrics_frame.pack(fill="x", padx=10, pady=5)
        
        metrics = [
            ("Total Decisions", analytics.get("total_decisions", 0)),
            ("Bets Placed", analytics.get("bets_placed", 0)),
            ("Skip Rate", f"{analytics.get('skip_rate', 0)*100:.1f}%"),
            ("Avg Decision Time", f"{analytics.get('avg_decision_time_ms', 0):.0f}ms"),
            ("Active Regime", analytics.get("active_regime", "RANGE")),
        ]
        
        for i, (label, value) in enumerate(metrics):
            ttk.Label(metrics_frame, text=f"{label}:", font=("Arial", 10, "bold")).grid(row=i, column=0, sticky="e", padx=5, pady=2)
            ttk.Label(metrics_frame, text=str(value), font=("Arial", 10)).grid(row=i, column=1, sticky="w", padx=5, pady=2)
        
        # Confidence breakdown
        conf_frame = ttk.LabelFrame(perf_frame, text="Win Rate by Confidence Level", padding=10)
        conf_frame.pack(fill="x", padx=10, pady=5)
        
        conf_data = [
            ("HIGH", analytics.get("high_conf_win_rate", 0), self.engine.decision_stats.high_conf_bets),
            ("MEDIUM", analytics.get("medium_conf_win_rate", 0), self.engine.decision_stats.medium_conf_bets),
            ("LOW", analytics.get("low_conf_win_rate", 0), self.engine.decision_stats.low_conf_bets),
        ]
        
        for level, rate, count in conf_data:
            color = "#28a745" if rate > 0.55 else ("#ffc107" if rate > 0.45 else "#dc3545")
            ttk.Label(conf_frame, text=f"{level}:", font=("Arial", 10, "bold")).pack(anchor="w")
            ttk.Label(conf_frame, text=f"  {rate*100:.1f}% win rate ({count} bets)", foreground=color).pack(anchor="w")
        
        # Tab 2: Thresholds
        thresh_frame = ttk.Frame(notebook)
        notebook.add(thresh_frame, text="Thresholds")
        
        thresh_display = tk.Text(thresh_frame, height=15, font=("Courier", 10))
        thresh_display.pack(fill="both", expand=True, padx=10, pady=10)
        
        thresholds = analytics.get("current_thresholds", {})
        active_regime_thresholds = analytics.get("active_regime_thresholds", {})
        thresh_text = f"""
Current Decision Thresholds:
{'='*40}

Base Legacy Min Score:   {thresholds.get('min_score', 0):.2f} (not live-gating)
Base Legacy Score Gap:   {thresholds.get('min_gap', 0):.2f} (not live-gating)
Base Legacy Recent Cols: {thresholds.get('recent_columns', 0)} (not live-gating)
Base Min Hit Prob:       {thresholds.get('min_hit_probability', 0):.2f}
Base Min Expected Value: {thresholds.get('min_expected_value', 0):.2f}
Base Min Edge:           {thresholds.get('min_probability_edge', 0):.2f}
Base Board Weight:       {thresholds.get('board_weight', 0):.2f}

Active Regime:           {analytics.get('active_regime', 'RANGE')}
Regime Reason:           {analytics.get('active_regime_reason', 'n/a')}
Active Legacy Min Score: {active_regime_thresholds.get('min_score', 0):.2f} (not live-gating)
Active Legacy Score Gap: {active_regime_thresholds.get('min_gap', 0):.2f} (not live-gating)
Active Legacy Recent:    {active_regime_thresholds.get('recent_columns', 0)} (not live-gating)
Active Min Hit Prob:     {active_regime_thresholds.get('min_hit_probability', 0):.2f}
Active Min Expected Val: {active_regime_thresholds.get('min_expected_value', 0):.2f}
Active Min Edge:         {active_regime_thresholds.get('min_probability_edge', 0):.2f}
Active Board Weight:     {active_regime_thresholds.get('board_weight', 0):.2f}

Adaptive Learning:      {'ENABLED' if analytics.get('learning_mode') else 'DISABLED'}

{'='*40}

Interpretation:
- Higher min_hit_probability = fewer, safer entries
- Higher min_expected_value = stronger positive edge required
- Higher min_probability_edge = avoids thin races between top colors
"""
        thresh_display.insert("1.0", thresh_text)
        thresh_display.configure(state="disabled")
        
        # Tab 3: Suggestions
        suggest_frame = ttk.Frame(notebook)
        notebook.add(suggest_frame, text="Suggestions")
        
        suggestion = analytics.get("suggestion", "No suggestions at this time. Continue monitoring.")
        ttk.Label(suggest_frame, text="AI Recommendations:", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=10)
        ttk.Label(suggest_frame, text=suggestion, font=("Arial", 10), wraplength=700).pack(anchor="w", padx=10, pady=5)
        
        # Export button
        def export_analytics():
            export_path = self.config_dir / f"analytics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(export_path, 'w') as f:
                json.dump(analytics, f, indent=2)
            messagebox.showinfo("Exported", f"Analytics saved to {export_path}")
        
        ttk.Button(dashboard, text="Export Analytics", command=export_analytics).pack(pady=10)
    
    def _schedule_ui_update(self):
        self._maybe_resume_after_weekly_idle()
        self._update_ui()
        self._status_update_id = self.root.after(500, self._schedule_ui_update)

    def _maybe_resume_after_weekly_idle(self):
        if self._calibration_window:
            return
        if self.engine.state.state != AutomationState.IDLE:
            return
        if not self.engine._is_weekly_idle_auto_resume_due():
            return

        mode = self.engine._get_weekly_idle_auto_resume_mode()
        if mode not in {"simulate", "autoclick", "autosim"}:
            self.engine._clear_weekly_idle_auto_resume()
            return

        self.engine._ensure_brave_on_top(f"resume {mode} after weekly idle")

        if mode == "simulate":
            started = self.engine.start_simulation(
                preserve_sync_handoff=False,
                inherited_sync_wait=True,
            )
            next_status = "Simulating"
        elif mode == "autoclick":
            started = self.engine.start_autoclick(
                preserve_sync_handoff=False,
                inherited_sync_wait=True,
            )
            next_status = "AutoClick"
        else:
            started = self.engine.start_autosim(
                preserve_sync_handoff=False,
                inherited_sync_wait=True,
            )
            next_status = "AutoSim"

        if started:
            self.status_mode_label = next_status
            self.status_var.set(
                f"Status: {next_status} auto-resumed after Wednesday hard idle; waiting to sync"
            )
            logger.info("Auto-resumed %s after Wednesday hard idle", mode)
        else:
            self.engine._clear_weekly_idle_auto_resume()
            self.status_var.set(
                f"Status: Could not auto-resume {mode} after Wednesday hard idle"
            )

    def show_click_marker(self, x: int, y: int, label: str):
        self.root.after(0, lambda: self._create_click_marker(x, y, label))

    def show_click_sequence_markers(self, actions):
        self.root.after(0, lambda: self._create_click_sequence_markers(actions))

    def _create_click_marker(self, x: int, y: int, label: str):
        marker = tk.Toplevel(self.root)
        marker.overrideredirect(True)
        marker.attributes("-topmost", True)
        marker.configure(bg="#ff00ff")

        try:
            marker.wm_attributes("-transparentcolor", "#ff00ff")
        except tk.TclError:
            pass

        marker.geometry(f"80x80+{max(0, x - 40)}+{max(0, y - 40)}")

        canvas = tk.Canvas(marker, width=80, height=80, bg="#ff00ff", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_oval(18, 18, 62, 62, outline="#ff3b30", width=3)
        canvas.create_text(40, 11, text=label.replace("Bet ", ""), fill="#ff3b30", font=("Arial", 8, "bold"))
        marker.after(2000, marker.destroy)

    def _create_click_sequence_markers(self, actions):
        marker_windows = []
        step_delay_ms = self.config.autosim_marker_step_delay_ms
        remove_after_ms = self.config.autosim_marker_remove_after_ms
        marker_color = "#ff0000"

        def show_marker(index: int, action):
            marker = tk.Toplevel(self.root)
            marker.overrideredirect(True)
            marker.attributes("-topmost", True)
            marker.configure(bg="#ff00ff")

            try:
                marker.wm_attributes("-transparentcolor", "#ff00ff")
            except tk.TclError:
                pass

            marker.geometry(f"36x36+{max(0, action.x - 18)}+{max(0, action.y - 18)}")
            label = tk.Label(
                marker,
                text=str(index + 1),
                font=("Arial", 18, "bold"),
                fg=marker_color,
                bg="#ff00ff",
            )
            label.pack(fill="both", expand=True)
            marker_windows.append(marker)

        for index, action in enumerate(actions):
            self.root.after(index * step_delay_ms, lambda idx=index, act=action: show_marker(idx, act))

        last_marker_delay = max(0, len(actions) - 1) * step_delay_ms
        total_delay = last_marker_delay + remove_after_ms

        def destroy_markers():
            for marker in marker_windows:
                try:
                    marker.destroy()
                except Exception:
                    pass

        self.root.after(total_delay, destroy_markers)

    @staticmethod
    def _format_panel_bet_summary(color: Optional[str], amount: Optional[str]) -> str:
        if not color:
            return "None"
        if amount:
            return f"{color} / {amount}"
        return color

    def _get_cycle_phase_label(self, game_state: Optional[GameState]) -> str:
        if self.engine._awaiting_cycle_reset_sync:
            return "SYNC WAIT (waiting for blank reset)"
        if self.engine._is_cycle_reset_state(game_state):
            return "SYNCED (waiting for fresh round)"
        if self.engine._is_cycle_warmup_active():
            return f"WARMUP (cycle {self.engine.display_round_counter}/6)"
        if self.engine._is_cycle_bet_cooldown_active():
            return f"COOLDOWN (cycle {self.engine.display_round_counter}+)"
        if self.engine.display_round_counter >= 7:
            return f"BETTING WINDOW (cycle {self.engine.display_round_counter})"
        return self.status_mode_label
    
    def _update_ui(self):
        self._refresh_history_window_limit_entry_state()
        self._refresh_scheduled_idle_lead_entry_state()
        self._refresh_soft_idle_countdown()
        self._refresh_strategy_custom_fields_visibility()
        current = len(self.capture_mgr.calibration_points)
        grid_needed = self.config.total_columns * self.config.boxes_per_column
        extra_needed = len(self.config.extra_calibration_labels)
        self.calib_info_var.set(f"Calibration: {min(current, grid_needed)}/{grid_needed} grid + {max(0, current - grid_needed)}/{extra_needed} extra")
        
        game_state = self.engine.state.get_game_state()
        if self.engine._is_cycle_warmup_active():
            self._maybe_auto_clear_color_cache(game_state)
        else:
            self._auto_cache_clear_done_this_warmup = False
            self._set_auto_cache_clear_status("Cache: idle")
        if game_state and game_state.columns:
            formatted_columns = [f"C{col.column_index}[{':'.join(col.boxes)}]" for col in game_state.columns]
            top_row = " | ".join(formatted_columns[:4])
            bottom_row = " | ".join(formatted_columns[4:7])
            active_round = self.engine._get_active_round_info(game_state)
            if active_round:
                self.columns_var.set(
                    f"Columns: Active suffix from C{active_round['basis_index']} "
                    f"({active_round['valid_count']} valid)"
                )
                self.columns_var_row2.set("         " + top_row)
                self.columns_var_row3.set("         " + bottom_row)
            elif game_state.has_invalid_board_pattern:
                repeated_color = game_state.invalid_repeated_color_label or "repeated"
                self.columns_var.set(f"Columns: Invalid {repeated_color} cluster detected")
                self.columns_var_row2.set("         " + top_row)
                self.columns_var_row3.set("         " + bottom_row)
            else:
                if not game_state.any_unknown:
                    self.columns_var.set("Columns: Stable board")
                    self.columns_var_row2.set("         " + top_row)
                    self.columns_var_row3.set("         " + bottom_row)
                else:
                    self.columns_var.set("Columns: Waiting for stable match...")
                    self.columns_var_row2.set("         " + top_row)
                    self.columns_var_row3.set("         " + bottom_row)
            blank_area_status = "DETECTED" if game_state.blank_reference_visible else "NOT DETECTED"
            self.blank_area_var.set(f"Blank Area: {blank_area_status}")
            self.confidence_var.set(f"Confidence: {game_state.confidence_score:.1f}%")
        else:
            self.columns_var.set("Columns: Waiting for data...")
            self.columns_var_row2.set("")
            self.columns_var_row3.set("")
            self.blank_area_var.set("Blank Area: Not scanned")
            self.confidence_var.set("Confidence: 0%")

        current_bet = self._format_panel_bet_summary(
            self.engine.last_bet_color,
            self.engine.last_bet_amount,
        )

        last_c1 = "None"
        if self.engine.last_c1_boxes:
            last_c1 = " / ".join(self.engine.last_c1_boxes)

        self.rounds_var.set(f"Rounds: {self.engine.detected_round_count}")
        self.win_lose_var.set(f"Wins: {self.engine.win_count} | Loses: {self.engine.lose_count}")
        self.profit_var.set(f"Profit: {self.engine.profit_total:.2f}")
        current_martingale = self.engine._get_current_target_amount()
        self.martingale_var.set(
            f"Progression: {current_martingale} | Loss streak: {self.engine.loss_streak}/{self.engine._effective_max_loss_streak()}"
        )
        self.current_bet_var.set(f"Current Bet: {current_bet}")
        self.pending_bet_var.set(f"Pending Bet: R{self.engine.pending_round_number} - {current_bet}")
        self.last_c1_var.set(f"Last Result Boxes: {last_c1}")
        self.decision_var.set(f"Decision: {self.engine.last_decision_reason}")
        
        idle_remaining = self.engine.state.get_monitor_idle_remaining()
        is_betting_mode = self.engine.state.state in (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )
        self.display_round_var.set(f"Cycle Counter: {self.engine.display_round_counter}")
        if self._calibration_window:
            self.status_var.set("Status: Calibrating")
        elif self.engine.state.state == AutomationState.IDLE:
            self.status_var.set("Status: Idle")
        elif is_betting_mode and self.engine._awaiting_cycle_reset_sync:
            phase_label = self._get_cycle_phase_label(game_state)
            self.status_var.set(f"Status: {self.status_mode_label} - {phase_label}")
        elif is_betting_mode and self.engine._is_cycle_reset_state(game_state):
            self.status_var.set(f"Status: {self.status_mode_label} - SYNCED (waiting for fresh round)")
        elif is_betting_mode and self.engine._is_cycle_warmup_active():
            # Only rescan history during warmup, when a new cycle has just
            # started and the latest complete 99-row window is ready to use.
            self._ensure_lazy_history_analysis()
            phase_label = self._get_cycle_phase_label(game_state)
            if idle_remaining > 0:
                self.status_var.set(
                    f"Status: {self.status_mode_label} - {phase_label} - Countdown {idle_remaining:.0f}s"
                )
            else:
                self.status_var.set(f"Status: {self.status_mode_label} - {phase_label}")
        elif idle_remaining > 0:
            phase_label = self._get_cycle_phase_label(game_state)
            self.status_var.set(f"Status: {self.status_mode_label} - {phase_label} - Countdown {idle_remaining:.0f}s")
        elif self.engine.state.state in (
            AutomationState.MONITORING,
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        ):
            phase_label = self._get_cycle_phase_label(game_state)
            self.status_var.set(f"Status: {self.status_mode_label} - {phase_label}")
    
    def stop_action(self, action: str):
        logger.info(f"Double-click detected on {action} - stopping")
        self.engine.stop_all()
        
        if self._calibration_window:
            self._close_calibration_window()
        
        self.engine.state.state = AutomationState.IDLE
        
        self.status_mode_label = "Idle"
        self.status_var.set("Status: Idle (stopped)")
        
        btn = None
        if action == "calibrate":
            btn = self.calibrate_btn
        elif action == "monitor":
            btn = self.monitor_btn
        elif action == "simulate":
            btn = self.simulate_btn
        elif action == "autoclick":
            btn = self.autoclick_btn
        elif action == "autosim":
            btn = self.autosim_btn
        
        if btn:
            original_bg = btn.cget("bg")
            btn.config(bg="#ff6666")
            self.root.after(500, lambda: btn.config(bg=original_bg))
    
    def start_calibration(self):
        if self._calibration_window:
            return
        
        self.engine.stop_all()
        self._start_calibration_window_lock()
        
        self._calibration_window = tk.Toplevel(self.root)
        self._calibration_window.title("Calibration")
        self._calibration_window.attributes("-fullscreen", True)
        
        screenshot = ImageGrab.grab().convert("RGB")
        screenshot_path = self.config_dir / "calibration_frame.png"
        screenshot.save(screenshot_path)
        
        bg_image = ImageTk.PhotoImage(screenshot)
        canvas = tk.Canvas(self._calibration_window, cursor="cross")
        canvas.pack(fill="both", expand=True)
        canvas.create_image(0, 0, image=bg_image, anchor="nw")
        canvas.bg_image = bg_image
        canvas.screenshot = screenshot
        
        temp_points = []
        
        def get_point_name(index: int) -> str:
            grid_count = self.config.total_columns * self.config.boxes_per_column
            if index < grid_count:
                col = (index // self.config.boxes_per_column) + 1
                box = (index % self.config.boxes_per_column) + 1
                return f"Column {col}, Box {box}"
            extra_idx = index - grid_count
            if extra_idx < len(self.config.extra_calibration_labels):
                return self.config.extra_calibration_labels[extra_idx]
            return f"Extra {extra_idx + 1}"
        
        instruction_var = tk.StringVar()
        
        def update_instruction_banner():
            current_index = len(temp_points)
            if current_index >= self.config.total_calibration_points:
                instruction_var.set(f"Complete: {len(temp_points)}/{self.config.total_calibration_points} points")
                return
            point_name = get_point_name(current_index)
            instruction_var.set(f"Click: {point_name} ({current_index + 1}/{self.config.total_calibration_points})")
        
        def on_click(event):
            idx = len(temp_points)
            if idx >= self.config.total_calibration_points:
                return
            
            point_name = get_point_name(idx)
            rgb_sample = None
            if point_name in self.config.sampled_reference_labels:
                rgb_sample = self.capture_mgr.get_pixel_rgb(screenshot, (event.x, event.y), None)
            
            point = CalibrationPoint(
                index=idx,
                name=point_name,
                x=event.x,
                y=event.y,
                rgb_sample=rgb_sample,
            )
            
            temp_points.append(point)
            self.capture_mgr.add_calibration_point(point)
            
            if rgb_sample:
                self.color_matcher.set_reference(point_name, rgb_sample)
            
            color = "red" if point_name in self.config.sampled_reference_labels else "yellow"
            canvas.create_oval(event.x - 5, event.y - 5, event.x + 5, event.y + 5, fill=color, outline="white")
            canvas.create_text(event.x + 10, event.y - 10, text=f"{idx + 1}", fill="white", anchor="w")
            
            remaining = self.config.total_calibration_points - len(temp_points)
            self._calibration_window.title(f"Calibration - {remaining} left")
            update_instruction_banner()
            
            if len(temp_points) >= self.config.total_calibration_points:
                self._save_calibration()
                self._close_calibration_window()
                messagebox.showinfo("Success", f"Calibration complete! {len(temp_points)} points saved.")
        
        def on_escape(event):
            if len(temp_points) > 0:
                if messagebox.askyesno("Cancel", f"Save {len(temp_points)} points?"):
                    self._save_calibration()
            self._close_calibration_window()
        
        canvas.bind("<Button-1>", on_click)
        self._calibration_window.bind("<Escape>", on_escape)
        self._calibration_window.protocol("WM_DELETE_WINDOW", lambda: on_escape(None))
        
        instruction_frame = tk.Frame(self._calibration_window, bg="black", bd=1, relief="solid")
        instruction_frame.place(x=10, y=10)
        
        tk.Label(instruction_frame, textvariable=instruction_var, bg="black", fg="#ffd966",
                 font=("Arial", 14, "bold"), padx=12, pady=8).pack(anchor="w")
        
        tk.Label(self._calibration_window, text="Click points in order. Press ESC to cancel.",
                 bg="black", fg="white", font=("Arial", 11), padx=12, pady=8).place(x=10, y=58)
        update_instruction_banner()
    
    def start_monitoring(self):
        if not self.capture_mgr.is_fully_calibrated():
            messagebox.showwarning("Not Calibrated", "Please complete calibration first.")
            return

        monitor_running = (
            self.engine._monitor_thread
            and self.engine._monitor_thread.is_alive()
            and not self.engine._stop_event.is_set()
        )

        if monitor_running:
            self.engine.stop_all()
            self.status_mode_label = "Idle"
            self.status_var.set("Status: Idle")
            return

        if self.engine.state.state != AutomationState.IDLE:
            self.engine.stop_all()
        
        if self.engine.start_monitoring():
            self.status_mode_label = "Monitoring"
            self.status_var.set("Status: Monitoring")
        else:
            messagebox.showerror("Error", "Failed to start monitoring")

    def _prepare_betting_mode_handoff(self, target_label: str) -> Optional[Tuple[bool, Optional[bool]]]:
        current_mode = self.engine.state.state
        switching_between_betting_modes = current_mode in (
            AutomationState.SIMULATING,
            AutomationState.AUTOCLICKING,
            AutomationState.AUTOSIMULATING,
        )

        if not switching_between_betting_modes:
            if current_mode != AutomationState.IDLE:
                self.engine.stop_all()
            return (False, None)

        if self.engine._has_unresolved_pending_bet():
            messagebox.showwarning(
                "Switch Blocked",
                (
                    f"Cannot switch to {target_label} yet because the current round still has an "
                    "unresolved pending bet/result.\n\nWait for the current round to settle first."
                ),
            )
            return None

        inherited_sync_wait = self.engine._awaiting_cycle_reset_sync
        preserve_sync_handoff = not inherited_sync_wait and self.engine._can_preserve_betting_handoff_sync()

        if current_mode != AutomationState.IDLE:
            self.engine.stop_all(preserve_soft_idle_runtime=True)

        if inherited_sync_wait:
            logger.info("Betting mode handoff keeps unsynced state while switching to %s", target_label)
        elif not preserve_sync_handoff:
            logger.info("Betting mode handoff requires resync before next bet")

        return (preserve_sync_handoff, inherited_sync_wait)

    def start_simulation(self):
        if not self.capture_mgr.is_fully_calibrated():
            messagebox.showwarning("Not Calibrated", "Please complete calibration first.")
            return

        simulate_running = (
            self.engine._simulate_thread
            and self.engine._simulate_thread.is_alive()
            and not self.engine._stop_event.is_set()
        )

        if simulate_running:
            self.engine.stop_all()
            self.status_mode_label = "Idle"
            self.status_var.set("Status: Idle")
            return

        handoff_state = self._prepare_betting_mode_handoff("Simulate")
        if handoff_state is None:
            return
        preserve_sync_handoff, inherited_sync_wait = handoff_state

        self.engine._ensure_brave_on_top("start simulation")

        if self.engine.start_simulation(
            preserve_sync_handoff=preserve_sync_handoff,
            inherited_sync_wait=inherited_sync_wait,
        ):
            self.status_mode_label = "Simulating"
            self.status_var.set("Status: Simulating")
        else:
            messagebox.showerror("Error", "Failed to start simulation")

    def start_autoclick(self):
        if not self.capture_mgr.is_fully_calibrated():
            messagebox.showwarning("Not Calibrated", "Please complete calibration first.")
            return

        autoclick_running = (
            self.engine._autoclick_thread
            and self.engine._autoclick_thread.is_alive()
            and not self.engine._stop_event.is_set()
        )

        if autoclick_running:
            self.engine.stop_all()
            self.status_mode_label = "Idle"
            self.status_var.set("Status: Idle")
            return

        handoff_state = self._prepare_betting_mode_handoff("AutoClick")
        if handoff_state is None:
            return
        preserve_sync_handoff, inherited_sync_wait = handoff_state

        self.engine._ensure_brave_on_top("start autoclick")

        if self.engine.start_autoclick(
            preserve_sync_handoff=preserve_sync_handoff,
            inherited_sync_wait=inherited_sync_wait,
        ):
            self.status_mode_label = "AutoClick"
            self.status_var.set("Status: AutoClick")
        else:
            messagebox.showerror("Error", "Failed to start AutoClick")

    def start_autosim(self):
        if not self.capture_mgr.is_fully_calibrated():
            messagebox.showwarning("Not Calibrated", "Please complete calibration first.")
            return

        autosim_running = (
            self.engine._autosim_thread
            and self.engine._autosim_thread.is_alive()
            and not self.engine._stop_event.is_set()
        )

        if autosim_running:
            self.engine.stop_all()
            self.status_mode_label = "Idle"
            self.status_var.set("Status: Idle")
            return

        handoff_state = self._prepare_betting_mode_handoff("AutoSim")
        if handoff_state is None:
            return
        preserve_sync_handoff, inherited_sync_wait = handoff_state

        self.engine._ensure_brave_on_top("start autosim")

        if self.engine.start_autosim(
            preserve_sync_handoff=preserve_sync_handoff,
            inherited_sync_wait=inherited_sync_wait,
        ):
            self.status_mode_label = "AutoSim"
            self.status_var.set("Status: AutoSim")
        else:
            messagebox.showerror("Error", "Failed to start AutoSim")

    def reset_records(self):
        if self._calibration_window:
            self._close_calibration_window()

        self.engine.reset_records()
        self.status_mode_label = "Idle"
        self.status_var.set("Status: Idle (records reset)")
    
    def exit_app(self):
        self._ui_shutting_down = True
        self._history_analysis_running = False
        if self._status_update_id:
            self.root.after_cancel(self._status_update_id)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.root.unbind_all(sequence)
            except Exception:
                pass
        self._stop_calibration_window_lock()
        self.engine.stop_all()
        self._save_config()
        self.root.quit()
        self.root.destroy()


def main():
    try:
        import PIL
        import pyautogui
    except ImportError as e:
        print(f"Missing dependency: {e}")
        print("Please install: pip install pillow pyautogui")
        return
    
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1
    
    root = tk.Tk()
    app = AutoClickerPro(root)
    
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\n Shutting down gracefully...")
        app.exit_app()


if __name__ == "__main__":
    main()
