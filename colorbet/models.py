# ============ FILE: models.py (COMPLETE) ============
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from enum import Enum
from collections import Counter
import logging
import threading
import time
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    """Centralized configuration with validation."""

    # Board layout and color sampling for the 7 columns x 3 boxes game grid.
    total_columns: int = 7
    boxes_per_column: int = 3
    color_sample_radius: int = 3
    color_match_max_score: float = 18000.0
    blank_match_max_score: float = 12000.0

    # Timing for blank/reset handling and round pacing.
    monitor_blank_allowance_seconds: float = 20.0
    cycle_reset_confirm_seconds: float = 3.0
    monitor_idle_seconds: float = 25.0
    monitor_capture_padding: int = 30
    calibration_timeout_seconds: float = 300.0
    column_check_interval: float = 0.5

    # Click timings for AutoClick and marker timings for AutoSim.
    click_hold_seconds: float = 0.13
    click_interval_seconds: float = 0.28
    x2_click_interval_seconds: float = 0.28
    autosim_marker_step_delay_ms: int = 250
    autosim_marker_remove_after_ms: int = 13000

    # Fallback dark-pixel blank detection used if no blank color sample exists.
    black_detection_threshold: int = 45

    # Decision thresholds used by the betting strategy.
    strategy_min_score: float = 3.40
    strategy_min_gap: float = 0.80
    strategy_recent_columns_required: int = 1
    strategy_regime_window: int = 12
    strategy_regime_enabled: bool = True
    strategy_probability_window: int = 24
    strategy_probability_min_samples: int = 8
    strategy_min_hit_probability: float = 0.44
    strategy_min_expected_value: float = 0.05
    strategy_min_probability_edge: float = 0.03
    strategy_probability_board_weight: float = 0.55
    strategy_progression_profile: str = "Slow Progress"
    strategy_history_window_limit: int = 0
    strategy_randomize_enabled: bool = False
    adaptive_learning_enabled: bool = False
    lock_brave_window_enabled: bool = False
    scheduled_idle_lead_minutes: int = 0
    board_idle_timeout_minutes: int = 30

    # Base progression settings for bet sizing.
    martingale_start: int = 5
    martingale_max_steps: int = 13

    # Session-level bankroll and safety limits.
    max_bet_per_round: int = 16600
    profit_target: Optional[float] = 10000.0
    loss_limit: Optional[float] = 20000.0

    # Labels used for board colors, betting buttons, and extra actions.
    color_labels: Tuple[str, ...] = ("Yellow", "White", "Pink", "Blue", "Red", "Green")
    blank_color_label: str = "Blank Color"
    bet_color_labels: Tuple[str, ...] = (
        "Bet Yellow", "Bet White", "Bet Pink",
        "Bet Blue", "Bet Red", "Bet Green",
    )
    bet_labels: Tuple[str, ...] = ("Bet 5", "Bet 10", "Bet 20", "Bet 50")
    action_labels: Tuple[str, ...] = ("X2",)

    def __post_init__(self):
        if self.martingale_start <= 0:
            raise ValueError("Martingale start must be positive")
        if self.martingale_max_steps < 1:
            raise ValueError("Martingale max steps must be at least 1")
        if not 0 <= self.strategy_min_score <= 10:
            raise ValueError("Strategy min score must be between 0 and 10")
        if self.strategy_regime_window < 4:
            raise ValueError("Regime window must be at least 4")
        if self.strategy_probability_window < 6:
            raise ValueError("Probability window must be at least 6")
        if self.strategy_probability_min_samples < 4:
            raise ValueError("Probability min samples must be at least 4")
        if not 0 <= self.strategy_min_hit_probability <= 1:
            raise ValueError("Strategy min hit probability must be between 0 and 1")
        if not -1 <= self.strategy_min_expected_value <= 3:
            raise ValueError("Strategy min expected value must be between -1 and 3")
        if not 0 <= self.strategy_min_probability_edge <= 1:
            raise ValueError("Strategy min probability edge must be between 0 and 1")
        if not 0 <= self.strategy_probability_board_weight <= 1:
            raise ValueError("Strategy probability board weight must be between 0 and 1")
        if self.strategy_history_window_limit < 0:
            raise ValueError("History window limit must be 0 or greater")
        if self.scheduled_idle_lead_minutes < 0:
            raise ValueError("Scheduled idle lead minutes must be 0 or greater")
        if self.board_idle_timeout_minutes < 0:
            raise ValueError("Board idle timeout minutes must be 0 or greater")
        if not 0.0 <= self.cycle_reset_confirm_seconds <= 10.0:
            raise ValueError("Cycle reset confirm seconds must be between 0 and 10")
        if not 0.01 <= self.click_hold_seconds <= 1.0:
            raise ValueError("Click hold seconds must be between 0.01 and 1.0")
        if not 0.01 <= self.click_interval_seconds <= 2.0:
            raise ValueError("Click interval seconds must be between 0.01 and 2.0")
        if not 0.01 <= self.x2_click_interval_seconds <= 2.0:
            raise ValueError("X2 click interval seconds must be between 0.01 and 2.0")
        if not 0 <= self.autosim_marker_step_delay_ms <= 10000:
            raise ValueError("Autosim marker step delay must be between 0 and 10000 ms")
        if not 100 <= self.autosim_marker_remove_after_ms <= 60000:
            raise ValueError("Autosim marker remove-after must be between 100 and 60000 ms")

        self.reference_color_labels = self.color_labels
        self.sampled_reference_labels = self.color_labels + (self.blank_color_label,)
        self.extra_calibration_labels = (
            self.color_labels
            + self.bet_color_labels
            + self.bet_labels
            + self.action_labels
            + (self.blank_color_label,)
        )
        self.total_calibration_points = (
            (self.total_columns * self.boxes_per_column)
            + len(self.extra_calibration_labels)
        )

    def get_bet_color_label(self, color_name: str) -> str:
        return f"Bet {color_name}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        normalized = dict(data)
        tuple_fields = ("color_labels", "bet_color_labels", "bet_labels", "action_labels")
        valid_field_names = {item.name for item in dataclass_fields(cls)}

        for field_name in tuple_fields:
            field_value = normalized.get(field_name)
            if isinstance(field_value, list):
                normalized[field_name] = tuple(field_value)

        filtered = {key: value for key, value in normalized.items() if key in valid_field_names}
        return cls(**filtered)


class AutomationState(Enum):
    IDLE = "idle"
    MONITORING = "monitoring"
    SIMULATING = "simulating"
    AUTOCLICKING = "autoclicking"
    AUTOSIMULATING = "autosimulating"
    CALIBRATING = "calibrating"


@dataclass
class ColumnAnalysis:
    column_index: int
    boxes: List[str]
    is_full: bool
    has_unknown: bool
    box_debug: List[str] = field(default_factory=list)

    @property
    def all_boxes_known(self) -> bool:
        return not self.has_unknown and self.is_full

    @property
    def display_text(self) -> str:
        return f"C{self.column_index}: {'/'.join(self.boxes)}"


@dataclass
class GameState:
    timestamp: float
    columns: List[ColumnAnalysis]
    all_columns_full: bool
    any_unknown: bool
    blank_detected: bool
    confidence_score: float
    blank_reference_visible: bool = False

    @property
    def winning_column_boxes(self) -> List[str]:
        if self.columns and self.columns[0].all_boxes_known:
            return list(self.columns[0].boxes)
        return []

    @property
    def has_uniform_column_pattern(self) -> bool:
        if not self.columns:
            return False
        if any(not col.all_boxes_known for col in self.columns):
            return False
        first_pattern = tuple(self.columns[0].boxes)
        return all(tuple(col.boxes) == first_pattern for col in self.columns[1:])

    @property
    def has_excess_all_white_columns(self) -> bool:
        return self.invalid_repeated_color_label == "White"

    @property
    def excess_uniform_color_label(self) -> Optional[str]:
        return self.invalid_repeated_color_label

    @property
    def has_excess_uniform_color_columns(self) -> bool:
        return self.invalid_repeated_color_label is not None

    @property
    def invalid_repeated_color_label(self) -> Optional[str]:
        repeated_counts: Counter[str] = Counter()
        unresolved_counts: Counter[str] = Counter()
        invalid_boxes = {"Blank", "Unknown"}

        for col in self.columns:
            known_boxes = [box for box in col.boxes if box not in invalid_boxes]
            if len(known_boxes) < 1:
                continue
            unresolved = len(known_boxes) < len(col.boxes) or not col.all_boxes_known

            for color in set(known_boxes):
                repeated_counts[color] += 1
                if unresolved or len(set(known_boxes)) > 1:
                    unresolved_counts[color] += 1

        for color, count in repeated_counts.items():
            if count >= 3 and unresolved_counts.get(color, 0) > 0:
                return color
        return None

    @property
    def has_invalid_board_pattern(self) -> bool:
        return self.invalid_repeated_color_label is not None


@dataclass
class DecisionRecord:
    """Track each betting decision for analysis"""
    timestamp: float
    round_number: int
    predicted_color: Optional[str]
    actual_c1_boxes: List[str]
    bet_amount: int
    decision_score: float
    decision_gap: float
    confidence_level: str
    decision_reason: str
    regime: str = "UNKNOWN"
    regime_reason: str = ""
    active_min_score: float = 0.0
    active_min_gap: float = 0.0
    active_recent_columns: int = 0
    strong_signal_only: bool = False
    decision_probability: float = 0.0
    expected_value: float = 0.0
    probability_edge: float = 0.0
    probability_samples: int = 0
    outcome: str = "PENDING"
    multiplier: int = 0
    profit_change: float = 0.0
    reaction_time_ms: float = 0.0


@dataclass
class DecisionStats:
    """Real-time decision statistics"""
    total_decisions: int = 0
    bets_placed: int = 0
    bets_skipped: int = 0
    high_conf_bets: int = 0
    high_conf_wins: int = 0
    medium_conf_bets: int = 0
    medium_conf_wins: int = 0
    low_conf_bets: int = 0
    low_conf_wins: int = 0
    
    @property
    def high_conf_win_rate(self) -> float:
        return self.high_conf_wins / self.high_conf_bets if self.high_conf_bets > 0 else 0
    
    @property
    def medium_conf_win_rate(self) -> float:
        return self.medium_conf_wins / self.medium_conf_bets if self.medium_conf_bets > 0 else 0
    
    @property
    def low_conf_win_rate(self) -> float:
        return self.low_conf_wins / self.low_conf_bets if self.low_conf_bets > 0 else 0


class ThreadSafeState:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutomationState.IDLE
        self._monitor_idle_until: float = 0.0
        self._current_game_state: Optional[GameState] = None
        self._match_output_enabled = False

    @property
    def state(self) -> AutomationState:
        with self._lock:
            return self._state

    @state.setter
    def state(self, value: AutomationState):
        with self._lock:
            self._state = value
            logger.info(f"State changed to: {value.value}")

    def set_monitor_idle_until(self, seconds: float):
        with self._lock:
            self._monitor_idle_until = time.time() + seconds

    def get_monitor_idle_remaining(self) -> float:
        with self._lock:
            remaining = self._monitor_idle_until - time.time()
            return max(0, remaining)

    def update_status(self, status: str):
        with self._lock:
            logger.info(status)

    def set_game_state(self, state: GameState):
        with self._lock:
            self._current_game_state = state

    def get_game_state(self) -> Optional[GameState]:
        with self._lock:
            return self._current_game_state

    def enable_match_output(self):
        with self._lock:
            self._match_output_enabled = True

    def disable_match_output(self):
        with self._lock:
            self._match_output_enabled = False

    def is_match_output_enabled(self) -> bool:
        with self._lock:
            return self._match_output_enabled


@dataclass
class CalibrationPoint:
    index: int
    name: str
    x: int
    y: int
    rgb_sample: Optional[Tuple[int, int, int]] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "x": self.x, "y": self.y, "rgb_sample": self.rgb_sample}

    @classmethod
    def from_dict(cls, index: int, data: dict) -> "CalibrationPoint":
        return cls(
            index=index,
            name=data["name"],
            x=data["x"],
            y=data["y"],
            rgb_sample=tuple(data["rgb_sample"]) if data.get("rgb_sample") else None,
        )

    @property
    def coord(self) -> Tuple[int, int]:
        return (self.x, self.y)
