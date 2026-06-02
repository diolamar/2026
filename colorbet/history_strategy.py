from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import csv
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from models import AppConfig


SETTLED_RESULTS = {"WIN", "WIN (x2)", "WIN (x3)", "LOSE"}
WINDOW_SIZE = 99
WARMUP_COUNT = 6
VALID_COOLDOWN_COUNTS = (9, 10)
PHASE_LABELS = (
    ("WARMUP", 1, 6),
    ("EARLY", 7, 30),
    ("MID", 31, 60),
    ("LATE", 61, 90),
    ("COOLDOWN", 91, 99),
)
MIN_TUNING_COMPLETE_WINDOWS = 3
MIN_TUNING_SETTLED_BETS = 80


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _amount_band(value: int) -> str:
    if value <= 15:
        return "LOW"
    if value <= 155:
        return "MID"
    if value <= 315:
        return "HIGH"
    return "DEEP"


def _extract_cycle_counter(text: object) -> Optional[int]:
    raw_text = str(text or "")
    marker = "counter="
    start = raw_text.rfind(marker)
    if start < 0:
        return None

    start += len(marker)
    digits: List[str] = []
    while start < len(raw_text) and raw_text[start].isdigit():
        digits.append(raw_text[start])
        start += 1

    if not digits:
        return None
    return int("".join(digits))


def _parse_result_boxes(value: object) -> Tuple[str, ...]:
    raw_boxes = [part.strip() for part in str(value or "").split("/") if part.strip()]
    return tuple(raw_boxes[:3])


def _boxes_signature(boxes: Tuple[str, ...]) -> str:
    return "/".join(boxes)


def _boxes_mix_signature(boxes: Tuple[str, ...]) -> str:
    counts = Counter(boxes)
    return "|".join(f"{color}x{counts[color]}" for color in sorted(counts))


def _phase_for_counter(counter: Optional[int]) -> str:
    if counter is None:
        return "UNKNOWN"
    for label, start, end in PHASE_LABELS:
        if start <= counter <= end:
            return label
    return "UNKNOWN"


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _round_probability(value: float) -> float:
    return round(value, 3)


def _round_metric(value: float) -> float:
    return round(value, 2)


def _skip_reason_family(text: object) -> str:
    raw_text = str(text or "").strip()
    if not raw_text:
        return "NONE"

    parts = raw_text.split("|")
    if not parts:
        return raw_text
    if parts[0] == "SKIP":
        return "|".join(parts[:3])
    return parts[0]


@dataclass
class HistoryCsvRow:
    result: str
    result_boxes: Tuple[str, ...]
    regime: str
    amount: int
    profit_change: float
    is_win: bool
    round_label: str
    skip_reason: str
    cycle_counter: Optional[int]
    cycle_phase: str = "UNKNOWN"
    window_index: int = 0


@dataclass(frozen=True)
class HistoryRecord:
    regime: str
    cycle_phase: str
    amount: int
    profit_change: float
    is_win: bool


@dataclass(frozen=True)
class AggregateStats:
    count: int
    wins: int
    profit: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.count if self.count else 0.0

    @property
    def avg_profit(self) -> float:
        return self.profit / self.count if self.count else 0.0


@dataclass(frozen=True)
class TransitionStats:
    count: int
    presence_counts: Dict[str, int]
    total_hits: Dict[str, int]

    def hit_rate(self, color: str) -> float:
        return self.presence_counts.get(color, 0) / self.count if self.count else 0.0

    def avg_hits(self, color: str) -> float:
        return self.total_hits.get(color, 0) / self.count if self.count else 0.0

    def top_color(self) -> Optional[str]:
        if not self.total_hits:
            return None
        return max(
            self.total_hits.items(),
            key=lambda item: (item[1], self.presence_counts.get(item[0], 0), item[0]),
        )[0]


@dataclass(frozen=True)
class SkipReasonStats:
    count: int
    repeated_next_count: int
    unique_color_total: int

    @property
    def repeated_next_rate(self) -> float:
        return self.repeated_next_count / self.count if self.count else 0.0

    @property
    def avg_unique_colors(self) -> float:
        return self.unique_color_total / self.count if self.count else 0.0


@dataclass(frozen=True)
class ThresholdRecommendation:
    strategy_probability_window: int = 0
    strategy_probability_min_samples: int = 0
    strategy_min_hit_probability: float = 0.0
    strategy_min_expected_value: float = 0.0
    strategy_min_probability_edge: float = 0.0
    strategy_probability_board_weight: float = 0.0
    strategy_history_window_limit: int = 0
    settled_bets: int = 0
    estimated_profit: float = 0.0

    def to_config_updates(self) -> Dict[str, object]:
        if (
            self.settled_bets < MIN_TUNING_SETTLED_BETS
            or self.strategy_probability_window <= 0
            or self.strategy_probability_min_samples <= 0
        ):
            return {}

        return {
            "strategy_probability_window": int(self.strategy_probability_window),
            "strategy_probability_min_samples": int(self.strategy_probability_min_samples),
            "strategy_min_hit_probability": float(self.strategy_min_hit_probability),
            "strategy_min_expected_value": float(self.strategy_min_expected_value),
            "strategy_min_probability_edge": float(self.strategy_min_probability_edge),
            "strategy_probability_board_weight": float(self.strategy_probability_board_weight),
            "strategy_history_window_limit": int(self.strategy_history_window_limit),
        }


@dataclass(frozen=True)
class HistoryStrategyModel:
    source_path: str
    source_mtime: float
    settled_bets: int
    hit_rate: float
    summed_profit: float
    recommendation: ThresholdRecommendation
    summary: str
    basis: str
    dangerous_amounts: Tuple[int, ...]
    complete_windows: int
    transition_pairs: int
    transition_exact: Dict[Tuple[str, str, str], TransitionStats]
    transition_mix: Dict[Tuple[str, str, str], TransitionStats]
    transition_phase: Dict[Tuple[str, str], TransitionStats]
    amount_recovery: Dict[Tuple[str, str, str], AggregateStats]
    skip_reason_outcomes: Dict[str, SkipReasonStats]

    def coordinated_probability_thresholds(
        self,
        base_thresholds: Dict[str, float],
        regime: str,
        amount: int,
        cycle_counter: Optional[int] = None,
    ) -> Dict[str, float]:
        tuned = dict(base_thresholds)
        if (
            self.complete_windows < MIN_TUNING_COMPLETE_WINDOWS
            or self.settled_bets < MIN_TUNING_SETTLED_BETS
        ):
            return tuned

        evidence_strength = min(
            1.0,
            min(self.complete_windows / 6.0, self.settled_bets / 240.0),
        )
        phase = _phase_for_counter(cycle_counter)
        amount_band = _amount_band(amount)
        amount_specs = [
            (regime, phase, amount_band),
            ("*", phase, amount_band),
            ("*", "ANY", amount_band),
        ]
        amount_stats = next(
            (self.amount_recovery.get(key) for key in amount_specs if self.amount_recovery.get(key)),
            None,
        )

        hit_delta = 0.0
        ev_delta = 0.0
        edge_delta = 0.0
        sample_delta = 0
        board_weight_delta = 0.0
        skip_amount_bands = set(tuned.get("skip_amount_bands") or set())
        block_reason = str(tuned.get("amount_block_reason", "") or "")

        overall_negative = self.summed_profit < 0 and self.hit_rate < 0.50
        overall_positive = self.summed_profit > 0 and self.hit_rate > 0.56

        if overall_negative:
            hit_delta += 0.015
            ev_delta += 0.015
            edge_delta += 0.007
            sample_delta += 1
        elif overall_positive:
            hit_delta -= 0.010
            ev_delta -= 0.010
            edge_delta -= 0.004

        if regime == "CHAOS":
            hit_delta += 0.015
            ev_delta += 0.015
            edge_delta += 0.010
            sample_delta += 1
            board_weight_delta -= 0.050
        elif regime == "TREND" and overall_positive:
            hit_delta -= 0.008
            ev_delta -= 0.006
            edge_delta -= 0.003
            board_weight_delta += 0.050
        elif regime == "RANGE" and phase in {"EARLY", "MID"} and overall_negative:
            hit_delta += 0.008
            ev_delta += 0.010
            edge_delta += 0.004

        if amount_stats and amount_stats.count >= 12:
            if amount_stats.avg_profit < 0 and amount_stats.win_rate < 0.40:
                if amount_band == "MID":
                    hit_delta += 0.006
                    ev_delta += 0.010
                elif amount_band == "HIGH":
                    hit_delta += 0.012
                    ev_delta += 0.018
                    edge_delta += 0.006
                    sample_delta += 1
                elif amount_band == "DEEP":
                    hit_delta += 0.020
                    ev_delta += 0.026
                    edge_delta += 0.010
                    sample_delta += 2
                    board_weight_delta -= 0.020
                    if (
                        amount_stats.count >= 18
                        and amount_stats.win_rate < 0.34
                        and regime == "CHAOS"
                    ):
                        skip_amount_bands.add("DEEP")
                        block_reason = block_reason or "HISTORY_DEEP_RISK"
            elif (
                overall_positive
                and amount_stats.avg_profit > 0
                and amount_stats.win_rate > 0.58
            ):
                if amount_band in {"LOW", "MID"}:
                    hit_delta -= 0.005
                    ev_delta -= 0.005
                    edge_delta -= 0.002

        if amount in self.dangerous_amounts:
            hit_delta += 0.008
            ev_delta += 0.012
            edge_delta += 0.005
            if amount_band == "DEEP":
                board_weight_delta -= 0.020
                if regime == "CHAOS":
                    skip_amount_bands.add("DEEP")
                    block_reason = block_reason or "HISTORY_DANGEROUS_DEEP"

        hit_delta = _clamp(hit_delta * evidence_strength, -0.04, 0.04)
        ev_delta = _clamp(ev_delta * evidence_strength, -0.05, 0.05)
        edge_delta = _clamp(edge_delta * evidence_strength, -0.02, 0.02)
        board_weight_delta = _clamp(board_weight_delta * evidence_strength, -0.08, 0.08)
        sample_delta = max(-2, min(2, int(round(sample_delta * evidence_strength))))

        tuned["min_hit_probability"] = _clamp(
            float(base_thresholds["min_hit_probability"]) + hit_delta,
            0.30,
            0.75,
        )
        tuned["min_expected_value"] = _clamp(
            float(base_thresholds["min_expected_value"]) + ev_delta,
            -0.05,
            0.40,
        )
        tuned["min_probability_edge"] = _clamp(
            float(base_thresholds["min_probability_edge"]) + edge_delta,
            0.01,
            0.18,
        )
        tuned["min_probability_samples"] = max(
            4,
            min(12, int(base_thresholds["min_probability_samples"]) + sample_delta),
        )
        if "board_weight" in tuned:
            tuned["board_weight"] = _clamp(
                float(tuned["board_weight"]) + board_weight_delta,
                0.20,
                0.75,
            )
        tuned["skip_amount_bands"] = skip_amount_bands
        if block_reason:
            tuned["amount_block_reason"] = block_reason
        return tuned

    def evaluate(
        self,
        regime: str,
        decision_probability: float,
        expected_value: float,
        probability_edge: float,
        probability_samples: int,
        amount: int,
        candidate_color: Optional[str] = None,
        result_boxes: Optional[Tuple[str, ...]] = None,
        cycle_counter: Optional[int] = None,
    ) -> Dict[str, object]:
        del decision_probability, expected_value, probability_edge, probability_samples

        phase = _phase_for_counter(cycle_counter)
        amount_band = _amount_band(amount)

        if not candidate_color or not result_boxes:
            return {
                "allow": True,
                "support": 0.0,
                "estimated_profit": 0.0,
                "estimated_win_rate": 0.0,
                "transition_support": 0.0,
                "transition_hit_rate": 0.0,
                "transition_avg_hits": 0.0,
                "message": "history transition gate missing candidate color or Result Boxes",
                "fallback": True,
            }

        signature = _boxes_signature(result_boxes)
        mix_signature = _boxes_mix_signature(result_boxes)
        transition_specs = [
            ((regime, phase, signature), self.transition_exact, 1.80, 5, "exact-regime"),
            ((regime, phase, mix_signature), self.transition_mix, 1.45, 8, "mix-regime"),
            (("*", phase, signature), self.transition_exact, 1.15, 12, "exact-phase"),
            (("*", phase, mix_signature), self.transition_mix, 0.95, 16, "mix-phase"),
            ((regime, phase), self.transition_phase, 0.75, 20, "phase-regime"),
            (("*", phase), self.transition_phase, 0.55, 30, "phase-all"),
        ]

        weighted_hit_rate = 0.0
        weighted_avg_hits = 0.0
        total_weight = 0.0
        support = 0.0
        transition_sources: List[str] = []

        for key, source_map, weight, min_support, label in transition_specs:
            stats = source_map.get(key)
            if not stats or stats.count < min_support:
                continue

            confidence_weight = min(1.55, stats.count / 20.0)
            effective_weight = weight * confidence_weight
            hit_rate = stats.hit_rate(candidate_color)
            avg_hits = stats.avg_hits(candidate_color)
            weighted_hit_rate += hit_rate * effective_weight
            weighted_avg_hits += avg_hits * effective_weight
            total_weight += effective_weight
            support += stats.count * weight
            transition_sources.append(f"{label}:{stats.count}")

        transition_hit_rate = weighted_hit_rate / total_weight if total_weight else 0.0
        transition_avg_hits = weighted_avg_hits / total_weight if total_weight else 0.0

        amount_specs = [
            (regime, phase, amount_band),
            ("*", phase, amount_band),
            ("*", "ANY", amount_band),
        ]
        amount_stats = next((self.amount_recovery.get(key) for key in amount_specs if self.amount_recovery.get(key)), None)

        amount_message = "amount recovery unavailable"
        amount_block = False
        amount_profit = 0.0
        amount_win_rate = 0.0
        if amount_stats:
            amount_profit = amount_stats.avg_profit
            amount_win_rate = amount_stats.win_rate
            amount_message = (
                f"amount {amount_band} {amount_stats.count} bets, "
                f"avg {amount_profit:+.0f}, win {amount_win_rate * 100:.1f}%"
            )
            if amount_stats.count >= 6 and amount_profit < 0 and amount_win_rate < 0.38:
                amount_block = True

        min_support = 25.0
        min_hit_rate = 0.36
        min_avg_hits = 0.48
        weak_block_support = 40.0
        weak_block_hit = 0.34
        weak_block_avg = 0.45

        if regime == "CHAOS":
            min_support = 18.0
            min_hit_rate = 0.34
            min_avg_hits = 0.46
            weak_block_support = 28.0
            weak_block_hit = 0.33
            weak_block_avg = 0.43
        elif regime == "RANGE":
            min_support = 40.0
            min_hit_rate = 0.42
            min_avg_hits = 0.52
            weak_block_support = 55.0
            weak_block_hit = 0.40
            weak_block_avg = 0.50

            if phase in {"EARLY", "MID"}:
                min_support = 55.0
                min_hit_rate = 0.45
                min_avg_hits = 0.55
                weak_block_support = 70.0
                weak_block_hit = 0.43
                weak_block_avg = 0.52

        if total_weight <= 0:
            fallback_message = (
                f"transition support too thin for {candidate_color} at {phase} from {signature}; {amount_message}"
            )
            return {
                "allow": not amount_block,
                "support": 0.0,
                "estimated_profit": amount_profit,
                "estimated_win_rate": amount_win_rate,
                "transition_support": 0.0,
                "transition_hit_rate": 0.0,
                "transition_avg_hits": 0.0,
                "message": fallback_message,
                "fallback": True,
            }

        estimated_profit = amount_profit
        estimated_win_rate = amount_win_rate
        allow = support >= min_support and transition_hit_rate >= min_hit_rate and transition_avg_hits >= min_avg_hits

        if support < min_support:
            allow = not amount_block
        if amount_block:
            allow = False
        if support >= weak_block_support and transition_hit_rate < weak_block_hit and transition_avg_hits < weak_block_avg:
            allow = False

        message = (
            f"transition support {support:.0f}, next-hit {transition_hit_rate * 100:.1f}%, "
            f"avg hits {transition_avg_hits:.2f}, sources {', '.join(transition_sources[:3]) or 'none'}, "
            f"{amount_message}"
        )

        return {
            "allow": allow,
            "support": support,
            "estimated_profit": estimated_profit,
            "estimated_win_rate": estimated_win_rate,
            "transition_support": support,
            "transition_hit_rate": transition_hit_rate,
            "transition_avg_hits": transition_avg_hits,
            "message": message,
            "fallback": False,
        }

    def recommend_positive_color(
        self,
        regime: str,
        amount: int,
        candidate_colors: Tuple[str, ...],
        preferred_color: Optional[str] = None,
        result_boxes: Optional[Tuple[str, ...]] = None,
        cycle_counter: Optional[int] = None,
    ) -> Dict[str, object]:
        evaluations: Dict[str, Dict[str, object]] = {}
        for color in candidate_colors:
            evaluations[color] = self.evaluate(
                regime=regime,
                decision_probability=0.0,
                expected_value=0.0,
                probability_edge=0.0,
                probability_samples=0,
                amount=amount,
                candidate_color=color,
                result_boxes=result_boxes,
                cycle_counter=cycle_counter,
            )

        preferred_evaluation = evaluations.get(preferred_color or "")
        strong_alternatives: List[Tuple[float, float, float, str, Dict[str, object]]] = []
        for color, evaluation in evaluations.items():
            if color == preferred_color:
                continue
            if not evaluation.get("allow", False):
                continue
            support = float(evaluation.get("support", 0.0))
            avg_hits = float(evaluation.get("transition_avg_hits", 0.0))
            hit_rate = float(evaluation.get("transition_hit_rate", 0.0))
            if support < 60.0 or avg_hits < 0.55 or hit_rate < 0.42:
                continue
            strong_alternatives.append((avg_hits, hit_rate, support, color, evaluation))

        strong_alternatives.sort(reverse=True)
        if preferred_evaluation and preferred_evaluation.get("allow", False):
            return {
                "selected_color": preferred_color,
                "selected_evaluation": preferred_evaluation,
                "preferred_evaluation": preferred_evaluation,
                "override": False,
                "message": str(preferred_evaluation.get("message", "")),
            }

        if strong_alternatives:
            _, _, _, selected_color, selected_evaluation = strong_alternatives[0]
            return {
                "selected_color": selected_color,
                "selected_evaluation": selected_evaluation,
                "preferred_evaluation": preferred_evaluation,
                "override": selected_color != preferred_color,
                "message": (
                    f"history positive override {preferred_color or 'None'} -> {selected_color}; "
                    f"{selected_evaluation.get('message', '')}"
                ),
            }

        fallback_evaluation = preferred_evaluation or {
            "allow": False,
            "message": "history positive override unavailable",
            "support": 0.0,
            "estimated_profit": 0.0,
            "estimated_win_rate": 0.0,
            "transition_support": 0.0,
            "transition_hit_rate": 0.0,
            "transition_avg_hits": 0.0,
            "fallback": True,
        }
        return {
            "selected_color": preferred_color,
            "selected_evaluation": fallback_evaluation,
            "preferred_evaluation": preferred_evaluation,
            "override": False,
            "message": str(fallback_evaluation.get("message", "")),
        }


def _parse_history_rows(rows: Iterable[Dict[str, str]]) -> List[HistoryCsvRow]:
    parsed_rows: List[HistoryCsvRow] = []
    for row in rows:
        result = str(row.get("Result") or "").strip()
        result_boxes = _parse_result_boxes(row.get("Result Boxes") or row.get("C1 Result") or "")
        parsed_rows.append(
            HistoryCsvRow(
                result=result,
                result_boxes=result_boxes,
                regime=str(row.get("Regime") or "DATA"),
                amount=_to_int(row.get("Amount")),
                profit_change=_to_float(row.get("Profit Change")),
                is_win=result.startswith("WIN"),
                round_label=str(row.get("Round") or ""),
                skip_reason=str(row.get("Skip Reason") or ""),
                cycle_counter=_extract_cycle_counter(row.get("Skip Reason")),
            )
        )
    return parsed_rows


def _is_window_start(row: HistoryCsvRow) -> bool:
    return row.result == "WARMUP" and row.cycle_counter == 1


def _is_complete_window(window_rows: List[HistoryCsvRow]) -> bool:
    if len(window_rows) != WINDOW_SIZE:
        return False

    if any(not row.result_boxes for row in window_rows):
        return False

    first_rows = window_rows[:WARMUP_COUNT]
    if any(row.result != "WARMUP" for row in first_rows):
        return False
    if [row.cycle_counter for row in first_rows] != list(range(1, WARMUP_COUNT + 1)):
        return False

    if sum(1 for row in window_rows if row.result == "WARMUP") != WARMUP_COUNT:
        return False

    cooldown_start = next((index for index, row in enumerate(window_rows) if row.result == "COOLDOWN"), None)
    if cooldown_start is None:
        return False

    cooldown_rows = window_rows[cooldown_start:]
    if any(row.result != "COOLDOWN" for row in cooldown_rows):
        return False
    # FIX: A valid 99‑row window must have exactly 9 cooldown rows (counters 91–99)
    if len(cooldown_rows) != 9:
        return False

    expected_counters = list(range(91, 91 + len(cooldown_rows)))
    if [row.cycle_counter for row in cooldown_rows] != expected_counters:
        return False

    return True


def _collect_complete_windows(rows: List[HistoryCsvRow]) -> List[Tuple[HistoryCsvRow, ...]]:
    start_indexes = [index for index, row in enumerate(rows) if _is_window_start(row)]
    windows: List[Tuple[HistoryCsvRow, ...]] = []

    for position, start_index in enumerate(start_indexes):
        end_index = start_indexes[position + 1] if position + 1 < len(start_indexes) else len(rows)
        candidate = rows[start_index:end_index]
        if not _is_complete_window(candidate):
            continue

        window_number = len(windows) + 1
        for cycle_position, row in enumerate(candidate, start=1):
            row.cycle_counter = cycle_position
            row.cycle_phase = _phase_for_counter(cycle_position)
            row.window_index = window_number

        windows.append(tuple(candidate))

    return windows


def _parse_settled_rows(rows: Iterable[HistoryCsvRow]) -> List[HistoryRecord]:
    records: List[HistoryRecord] = []
    for row in rows:
        if row.result not in SETTLED_RESULTS:
            continue
        records.append(
            HistoryRecord(
                regime=row.regime,
                cycle_phase=row.cycle_phase,
                amount=row.amount,
                profit_change=row.profit_change,
                is_win=row.is_win,
            )
        )
    return records


def _freeze_transition_stats(
    raw: Dict[Tuple[str, ...], Dict[str, object]],
) -> Dict[Tuple[str, ...], TransitionStats]:
    frozen: Dict[Tuple[str, ...], TransitionStats] = {}
    for key, bucket in raw.items():
        frozen[key] = TransitionStats(
            count=int(bucket["count"]),
            presence_counts={color: int(value) for color, value in bucket["presence"].items()},
            total_hits={color: int(value) for color, value in bucket["hits"].items()},
        )
    return frozen


def _build_transition_models(
    windows: Iterable[Tuple[HistoryCsvRow, ...]],
) -> Tuple[
    Dict[Tuple[str, str, str], TransitionStats],
    Dict[Tuple[str, str, str], TransitionStats],
    Dict[Tuple[str, str], TransitionStats],
    int,
]:
    exact_raw: Dict[Tuple[str, ...], Dict[str, object]] = defaultdict(
        lambda: {"count": 0, "presence": defaultdict(int), "hits": defaultdict(int)}
    )
    mix_raw: Dict[Tuple[str, ...], Dict[str, object]] = defaultdict(
        lambda: {"count": 0, "presence": defaultdict(int), "hits": defaultdict(int)}
    )
    phase_raw: Dict[Tuple[str, ...], Dict[str, object]] = defaultdict(
        lambda: {"count": 0, "presence": defaultdict(int), "hits": defaultdict(int)}
    )
    pair_count = 0

    for window in windows:
        for current_row, next_row in zip(window, window[1:]):
            if not current_row.result_boxes or not next_row.result_boxes:
                continue

            pair_count += 1
            next_counts = Counter(next_row.result_boxes)
            exact_key = _boxes_signature(current_row.result_boxes)
            mix_key = _boxes_mix_signature(current_row.result_boxes)
            keyed_targets = (
                (exact_raw, (current_row.regime, current_row.cycle_phase, exact_key)),
                (exact_raw, ("*", current_row.cycle_phase, exact_key)),
                (mix_raw, (current_row.regime, current_row.cycle_phase, mix_key)),
                (mix_raw, ("*", current_row.cycle_phase, mix_key)),
                (phase_raw, (current_row.regime, current_row.cycle_phase)),
                (phase_raw, ("*", current_row.cycle_phase)),
            )

            for target, key in keyed_targets:
                bucket = target[key]
                bucket["count"] += 1
                for color, hits in next_counts.items():
                    bucket["presence"][color] += 1
                    bucket["hits"][color] += hits

    return (
        _freeze_transition_stats(exact_raw),
        _freeze_transition_stats(mix_raw),
        _freeze_transition_stats(phase_raw),
        pair_count,
    )


def _build_amount_recovery(records: Iterable[HistoryRecord]) -> Dict[Tuple[str, str, str], AggregateStats]:
    raw: Dict[Tuple[str, ...], List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    def bump(key: Tuple[str, str, str], record: HistoryRecord):
        bucket = raw[key]
        bucket[0] += 1.0
        bucket[1] += 1.0 if record.is_win else 0.0
        bucket[2] += record.profit_change

    for record in records:
        band = _amount_band(record.amount)
        keys = (
            (record.regime, record.cycle_phase, band),
            ("*", record.cycle_phase, band),
            ("*", "ANY", band),
        )
        for key in keys:
            bump(key, record)

    return {
        key: AggregateStats(count=int(values[0]), wins=int(values[1]), profit=values[2])
        for key, values in raw.items()
    }


def _build_skip_reason_outcomes(windows: Iterable[Tuple[HistoryCsvRow, ...]]) -> Dict[str, SkipReasonStats]:
    raw: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])

    for window in windows:
        for current_row, next_row in zip(window, window[1:]):
            if current_row.result not in {"SKIP", "WARMUP", "COOLDOWN"}:
                continue

            family = _skip_reason_family(current_row.skip_reason)
            bucket = raw[family]
            bucket[0] += 1
            bucket[1] += 1 if len(set(next_row.result_boxes)) < len(next_row.result_boxes) else 0
            bucket[2] += len(set(next_row.result_boxes))

    return {
        key: SkipReasonStats(
            count=values[0],
            repeated_next_count=values[1],
            unique_color_total=values[2],
        )
        for key, values in raw.items()
    }


def build_history_strategy_model(csv_path: Path, window_limit: int = 0) -> HistoryStrategyModel:
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        raw_rows = list(csv.DictReader(handle))

    parsed_rows = _parse_history_rows(raw_rows)
    all_complete_windows = _collect_complete_windows(parsed_rows)
    available_complete_windows = len(all_complete_windows)
    requested_window_limit = max(0, int(window_limit))
    if requested_window_limit > 0 and available_complete_windows > requested_window_limit:
        complete_windows = all_complete_windows[-requested_window_limit:]
    else:
        complete_windows = all_complete_windows
    window_rows = [row for window in complete_windows for row in window]
    records = _parse_settled_rows(window_rows)

    settled_bets = len(records)
    hit_rate = sum(1 for record in records if record.is_win) / settled_bets if settled_bets else 0.0
    summed_profit = sum(record.profit_change for record in records)
    transition_exact, transition_mix, transition_phase, transition_pairs = _build_transition_models(complete_windows)
    amount_recovery = _build_amount_recovery(records)
    skip_reason_outcomes = _build_skip_reason_outcomes(complete_windows)

    amount_profit: Dict[int, float] = defaultdict(float)
    amount_count: Dict[int, int] = defaultdict(int)
    regime_profit: Dict[str, float] = defaultdict(float)

    for record in records:
        amount_profit[record.amount] += record.profit_change
        amount_count[record.amount] += 1
        regime_profit[record.regime] += record.profit_change

    dangerous_amounts = tuple(
        amount
        for amount, profit in sorted(amount_profit.items())
        if amount >= 315 and amount_count[amount] >= 3 and profit < 0
    )
    worst_regime = min(regime_profit.items(), key=lambda item: item[1])[0] if regime_profit else "DATA"

    default_config = AppConfig()
    recommendation_window = 0
    recommendation_samples = 0
    recommendation_hit = 0.0
    recommendation_ev = 0.0
    recommendation_edge = 0.0
    recommendation_board_weight = 0.0
    recommendation_history_window_limit = 0

    if len(complete_windows) >= MIN_TUNING_COMPLETE_WINDOWS and settled_bets >= MIN_TUNING_SETTLED_BETS:
        evidence_strength = min(
            1.0,
            min(len(complete_windows) / 8.0, settled_bets / 320.0),
        )
        deep_risk = min(1.0, len(dangerous_amounts) / 4.0)
        profit_per_bet = (summed_profit / settled_bets) if settled_bets else 0.0

        recommendation_window = default_config.strategy_probability_window
        recommendation_samples = default_config.strategy_probability_min_samples
        recommendation_hit = default_config.strategy_min_hit_probability
        recommendation_ev = default_config.strategy_min_expected_value
        recommendation_edge = default_config.strategy_min_probability_edge
        recommendation_board_weight = default_config.strategy_probability_board_weight

        if hit_rate < 0.47 or summed_profit < 0:
            recommendation_window += 4 if evidence_strength < 0.75 else 6
            recommendation_samples += 1
            recommendation_hit += 0.05 + (0.03 * evidence_strength)
            recommendation_ev += 0.03 + (0.02 * evidence_strength)
            recommendation_edge += 0.010 + (0.006 * evidence_strength)
            recommendation_board_weight -= 0.08
        elif hit_rate > 0.56 and summed_profit > 0:
            recommendation_window -= 2 if evidence_strength < 0.75 else 4
            recommendation_samples -= 1
            recommendation_hit -= 0.02 + (0.01 * evidence_strength)
            recommendation_ev -= 0.01 + (0.01 * evidence_strength)
            recommendation_edge -= 0.004 + (0.003 * evidence_strength)
            recommendation_board_weight += 0.05

        if profit_per_bet < 0:
            recommendation_hit += 0.02
            recommendation_ev += 0.02
            recommendation_edge += 0.005
        elif profit_per_bet > 0.12:
            recommendation_hit -= 0.01
            recommendation_ev -= 0.01

        if worst_regime == "CHAOS":
            recommendation_window += 4
            recommendation_samples += 1
            recommendation_hit += 0.02
            recommendation_ev += 0.03
            recommendation_edge += 0.008
            recommendation_board_weight -= 0.10
        elif worst_regime == "TREND" and hit_rate > 0.54:
            recommendation_board_weight += 0.04

        if deep_risk > 0:
            recommendation_window += int(round(4 * deep_risk))
            recommendation_samples += int(round(2 * deep_risk))
            recommendation_hit += 0.01 * deep_risk
            recommendation_ev += 0.02 * deep_risk
            recommendation_edge += 0.005 * deep_risk
            recommendation_board_weight -= 0.06 * deep_risk

        if len(complete_windows) <= 4:
            recommendation_history_window_limit = len(complete_windows)
        elif len(complete_windows) <= 8:
            recommendation_history_window_limit = min(len(complete_windows), 6)
        else:
            recommendation_history_window_limit = min(len(complete_windows), 8)

        recommendation_window = int(max(6, min(40, recommendation_window)))
        recommendation_samples = int(max(4, min(12, recommendation_samples)))
        recommendation_hit = _round_probability(_clamp(recommendation_hit, 0.30, 0.75))
        recommendation_ev = _round_metric(_clamp(recommendation_ev, -0.05, 0.40))
        recommendation_edge = _round_probability(_clamp(recommendation_edge, 0.01, 0.18))
        recommendation_board_weight = _round_metric(_clamp(recommendation_board_weight, 0.25, 0.75))

    recommendation = ThresholdRecommendation(
        strategy_probability_window=recommendation_window,
        strategy_probability_min_samples=recommendation_samples,
        strategy_min_hit_probability=recommendation_hit,
        strategy_min_expected_value=recommendation_ev,
        strategy_min_probability_edge=recommendation_edge,
        strategy_probability_board_weight=recommendation_board_weight,
        strategy_history_window_limit=recommendation_history_window_limit,
        settled_bets=settled_bets,
        estimated_profit=summed_profit,
    )

    if not complete_windows:
        summary = (
            "History Analysis: No complete 99-row windows found yet."
        )
        basis = (
            "History Basis: Need complete 99-row windows"
        )
        if requested_window_limit > 0 and available_complete_windows <= 0:
            basis += f" | Requested {requested_window_limit}, available 0"
    else:
        window_scope_text = f"{len(complete_windows)} complete windows"
        if requested_window_limit > 0:
            if requested_window_limit > available_complete_windows:
                window_scope_text = (
                    f"requested {requested_window_limit}, using {len(complete_windows)} available complete windows"
                )
            else:
                window_scope_text = f"using last {len(complete_windows)} complete windows"
        summary = (
            f"History Analysis: {window_scope_text}, {settled_bets} settled bets, "
            f"{summed_profit:+.0f} summed profit, {transition_pairs} Result Boxes transitions. "
            f"Main leak: {worst_regime}, weak deep stages "
            f"{', '.join(str(value) for value in dangerous_amounts[:4]) if dangerous_amounts else 'none'}."
        )
        basis = (
            "History Basis: Result Boxes transitions, cycle phase, amount recovery, skip behavior"
        )
        if requested_window_limit > 0 and requested_window_limit > available_complete_windows:
            basis += (
                f" | Requested {requested_window_limit} windows exceeded valid complete windows; "
                f"using {len(complete_windows)}"
            )
        elif requested_window_limit > 0:
            basis += f" | Window cap: last {len(complete_windows)} complete windows"
        else:
            basis += " | Window cap: all complete windows"

    return HistoryStrategyModel(
        source_path=str(csv_path),
        source_mtime=csv_path.stat().st_mtime,
        settled_bets=settled_bets,
        hit_rate=hit_rate,
        summed_profit=summed_profit,
        recommendation=recommendation,
        summary=summary,
        basis=basis,
        dangerous_amounts=dangerous_amounts,
        complete_windows=len(complete_windows),
        transition_pairs=transition_pairs,
        transition_exact=transition_exact,
        transition_mix=transition_mix,
        transition_phase=transition_phase,
        amount_recovery=amount_recovery,
        skip_reason_outcomes=skip_reason_outcomes,
    )
