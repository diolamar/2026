from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import random
import time
from typing import Callable, Dict, List, Optional, Tuple

import pyautogui


CHIP_VALUES: Dict[str, int] = {
    "Bet 5": 5,
    "Bet 10": 10,
    "Bet 20": 20,
    "Bet 50": 50,
}
COLOR_CLICK_JITTER_PX = 3


class ClickPlanError(Exception):
    """Raised when the requested bet cannot be expressed with calibrated controls."""


@dataclass(frozen=True)
class BetPlacement:
    chip_label: str
    added_amount: int
    resulting_total: int
    x2_clicks: int = 0

    @property
    def click_cost(self) -> int:
        return 2 + self.x2_clicks

    @property
    def description(self) -> str:
        if self.x2_clicks <= 0:
            return f"+{self.added_amount} via {self.chip_label} -> {self.resulting_total}"
        return f"+{self.added_amount} via {self.chip_label} + X2x{self.x2_clicks} -> {self.resulting_total}"


@dataclass(frozen=True)
class ClickAction:
    label: str
    x: int
    y: int


def build_bet_plan(target_amount: int) -> List[BetPlacement]:
    if target_amount <= 0:
        raise ClickPlanError("Bet amount must be greater than zero.")

    largest_base = max(CHIP_VALUES.values())
    prefer_largest_chip_first = target_amount >= 1000
    chip_candidates = sorted(CHIP_VALUES.items(), key=lambda item: -item[1])

    # DP state: best (click_cost, non_largest_penalty, steps) and the plan list
    # We store for each total the best plan as a tuple (key, plan_list)
    INF = 10**9
    # dp[total] = (best_key, plan_list) where best_key = (click_cost, non_largest_penalty, steps)
    dp = [None] * (target_amount + 1)
    dp[0] = ((0, 0, 0), [])

    for current in range(target_amount + 1):
        if dp[current] is None:
            continue
        current_key, current_plan = dp[current]

        for chip_label, chip_value in chip_candidates:
            after_chip = current + chip_value
            if after_chip > target_amount:
                continue

            # Determine possible X2 multipliers
            max_x2 = 0
            projected = after_chip
            while projected * 2 <= target_amount:
                projected *= 2
                max_x2 += 1

            for x2 in range(max_x2, -1, -1):
                if x2 > 0 and prefer_largest_chip_first and chip_value != largest_base:
                    continue

                next_total = after_chip * (2 ** x2)
                if next_total > target_amount:
                    continue

                step = BetPlacement(
                    chip_label=chip_label,
                    added_amount=chip_value,
                    resulting_total=next_total,
                    x2_clicks=x2,
                )
                new_key = (
                    current_key[0] + step.click_cost,
                    current_key[1] + (1 if chip_value != largest_base else 0),
                    current_key[2] + 1,
                )
                new_plan = current_plan + [step]

                if dp[next_total] is None or new_key < dp[next_total][0]:
                    dp[next_total] = (new_key, new_plan)

    if dp[target_amount] is None:
        raise ClickPlanError(f"Unable to build a click plan for amount {target_amount}.")

    return dp[target_amount][1]


def create_click_actions(
    extra_points: Dict[str, Tuple[int, int]],
    chosen_color: str,
    placements: List[BetPlacement],
) -> List[ClickAction]:
    color_label = f"Bet {chosen_color}"
    if color_label not in extra_points:
        raise ClickPlanError(f"Missing calibration for {color_label}")

    if any(item.x2_clicks > 0 for item in placements) and "X2" not in extra_points:
        raise ClickPlanError("Missing calibration for X2")

    color_x, color_y = extra_points[color_label]
    actions: List[ClickAction] = []

    for placement in placements:
        if placement.chip_label not in extra_points:
            raise ClickPlanError(f"Missing calibration for {placement.chip_label}")

        chip_x, chip_y = extra_points[placement.chip_label]
        actions.append(ClickAction(label=placement.chip_label, x=chip_x, y=chip_y))

        if placement.x2_clicks > 0:
            # On this game UI, X2 multiplies the current board total after the
            # chip has been placed on the chosen color.
            actions.append(ClickAction(label=color_label, x=color_x, y=color_y))
            x2_x, x2_y = extra_points["X2"]
            for _ in range(placement.x2_clicks):
                actions.append(ClickAction(label="X2", x=x2_x, y=x2_y))
        else:
            actions.append(ClickAction(label=color_label, x=color_x, y=color_y))

    return actions


def perform_click_actions(
    actions: List[ClickAction],
    marker_callback: Optional[Callable[[int, int, str], None]] = None,
    stop_requested: Optional[Callable[[], bool]] = None,
    move_duration_range: Tuple[float, float] = (0.12, 0.28),
    settle_range: Tuple[float, float] = (0.06, 0.14),
    pause_range: Tuple[float, float] = (0.28, 0.55),
    click_hold_range: Tuple[float, float] = (0.03, 0.08),
    temporary_pause_override: Optional[float] = None,
    fixed_click_interval: float = 0.25,
    x2_click_interval: float = 0.32,
    click_hold_duration: float = 0.10,
):
    previous_pause = pyautogui.PAUSE
    if temporary_pause_override is not None:
        pyautogui.PAUSE = temporary_pause_override

    try:
        for action in actions:
            if stop_requested and stop_requested():
                return False

            if action.label in CHIP_VALUES:
                target_x, target_y = action.x, action.y
                action_settle_range = (max(settle_range[0], 0.03), max(settle_range[1], 0.06))
                action_pause_range = (max(pause_range[0], 0.10), max(pause_range[1], 0.16))
            elif action.label == "X2":
                target_x, target_y = action.x, action.y
                action_settle_range = (max(settle_range[0], 0.05), max(settle_range[1], 0.09))
                action_pause_range = (max(pause_range[0], 0.12), max(pause_range[1], 0.18))
            else:
                target_x = action.x + random.randint(-COLOR_CLICK_JITTER_PX, COLOR_CLICK_JITTER_PX)
                target_y = action.y + random.randint(-COLOR_CLICK_JITTER_PX, COLOR_CLICK_JITTER_PX)
                action_settle_range = (max(settle_range[0], 0.03), max(settle_range[1], 0.06))
                action_pause_range = (max(pause_range[0], 0.11), max(pause_range[1], 0.18))
            pyautogui.moveTo(target_x, target_y, duration=random.uniform(*move_duration_range))
            time.sleep(random.uniform(*action_settle_range))
            pyautogui.mouseDown(target_x, target_y)
            hold_min = max(0.01, min(click_hold_range))
            hold_max = max(hold_min, max(click_hold_range))
            effective_hold = max(click_hold_duration, random.uniform(hold_min, hold_max))
            time.sleep(effective_hold)
            pyautogui.mouseUp(target_x, target_y)
            if marker_callback:
                marker_callback(target_x, target_y, action.label)

            post_click_delay = x2_click_interval if action.label == "X2" else fixed_click_interval
            post_click_delay = max(post_click_delay, random.uniform(*action_pause_range))
            time.sleep(post_click_delay)

        return True
    finally:
        pyautogui.PAUSE = previous_pause


def format_bet_plan(placements: List[BetPlacement]) -> str:
    return " + ".join(item.description for item in placements)
