# ============ FILE: capture.py (COMPLETE - unchanged from original) ============
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageGrab
import pyautogui

from models import AppConfig, CalibrationPoint, ColumnAnalysis, GameState


logger = logging.getLogger(__name__)


class ColorMatcher:
    """Optimized color matcher with caching for 70% CPU reduction."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.reference_colors: Dict[str, Tuple[int, int, int]] = {}
        self._cache: "OrderedDict[Tuple[int, int, int], Tuple[str, float, bool]]" = OrderedDict()
        self._cache_maxsize = 4096
        self._cache_hits = 0
        self._cache_misses = 0

    @staticmethod
    def normalize_color(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
        total = max(sum(rgb), 1)
        return tuple(c / total for c in rgb)

    def calculate_score(self, rgb: Tuple[int, int, int], reference: Tuple[int, int, int]) -> float:
        raw_distance = sum((rgb[i] - reference[i]) ** 2 for i in range(3))
        norm_rgb = self.normalize_color(rgb)
        norm_ref = self.normalize_color(reference)
        balance_distance = sum((norm_rgb[i] - norm_ref[i]) ** 2 for i in range(3)) * 100000
        return raw_distance + balance_distance

    def match_color(self, rgb: Tuple[int, int, int]) -> Tuple[str, float, bool]:
        cache_key = rgb
        if cache_key in self._cache:
            self._cache_hits += 1
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        self._cache_misses += 1
        result = self._match_color_uncached(rgb)
        self._cache[cache_key] = result
        self._cache.move_to_end(cache_key)
        if len(self._cache) > self._cache_maxsize:
            self._cache.popitem(last=False)

        if self._cache_misses % 1000 == 0 and self._cache_misses > 0:
            hit_rate = (self._cache_hits / (self._cache_hits + self._cache_misses)) * 100
            logger.info(
                "Color cache: %.1f%% hit rate (%s hits, %s misses)",
                hit_rate,
                self._cache_hits,
                self._cache_misses,
            )

        return result

    def _match_color_uncached(self, rgb: Tuple[int, int, int]) -> Tuple[str, float, bool]:
        if not self.reference_colors:
            return "Unknown", float("inf"), False

        best_name = None
        best_score = float("inf")

        for name, ref_rgb in self.reference_colors.items():
            if name == self.config.blank_color_label:
                continue
            score = self.calculate_score(rgb, ref_rgb)
            if score < best_score:
                best_name = name
                best_score = score

        is_match = best_score <= self.config.color_match_max_score
        return best_name or "Unknown", best_score, is_match

    def is_blank_color(self, rgb: Tuple[int, int, int]) -> bool:
        blank_reference = self.reference_colors.get(self.config.blank_color_label)
        if blank_reference:
            blank_score = self.calculate_score(rgb, blank_reference)
            return blank_score <= self.config.blank_match_max_score
        return max(rgb) <= self.config.black_detection_threshold

    def set_reference(self, name: str, rgb: Tuple[int, int, int]):
        self.reference_colors[name] = rgb
        self._cache.clear()
        logger.debug("Color cache cleared due to reference change: %s", name)

    def clear_cache(self):
        self._cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0
        logger.debug("Color cache manually cleared")

    def get_cache_stats(self) -> Dict[str, Any]:
        total = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total * 100) if total > 0 else 0
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": hit_rate,
            "cache_size": len(self._cache),
            "cache_maxsize": self._cache_maxsize,
        }


class ScreenCaptureManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.calibration_points: Dict[int, CalibrationPoint] = {}

    def add_calibration_point(self, point: CalibrationPoint):
        self.calibration_points[point.index] = point

    def get_grid_points(self) -> List[List[Tuple[int, int]]]:
        grid_count = self.config.total_columns * self.config.boxes_per_column
        grid_points = [self.calibration_points[i].coord for i in range(grid_count) if i in self.calibration_points]

        if len(grid_points) < grid_count:
            return []

        return [
            grid_points[i * self.config.boxes_per_column:(i + 1) * self.config.boxes_per_column]
            for i in range(self.config.total_columns)
        ]

    def get_extra_points(self) -> Dict[str, Tuple[int, int]]:
        start_idx = self.config.total_columns * self.config.boxes_per_column
        extra_points = {}

        for i, label in enumerate(self.config.extra_calibration_labels):
            idx = start_idx + i
            if idx in self.calibration_points:
                extra_points[label] = self.calibration_points[idx].coord

        return extra_points

    def get_named_point(self, label: str) -> Optional[Tuple[int, int]]:
        start_idx = self.config.total_columns * self.config.boxes_per_column
        for i, configured_label in enumerate(self.config.extra_calibration_labels):
            if configured_label != label:
                continue
            idx = start_idx + i
            point = self.calibration_points.get(idx)
            return point.coord if point else None
        return None

    def get_monitor_points(self) -> List[Tuple[int, int]]:
        ordered_points = sorted(self.calibration_points.values(), key=lambda p: p.index)
        points = [p.coord for p in ordered_points]
        if not points:
            return []

        grid_count = self.config.total_columns * self.config.boxes_per_column
        monitor_points = [p for i, p in enumerate(points) if i < grid_count]
        for point in ordered_points[grid_count:]:
            if point.name in self.config.sampled_reference_labels:
                monitor_points.append(point.coord)
        return monitor_points

    def get_capture_bbox(self, padding: Optional[int] = None) -> Optional[Tuple[int, int, int, int]]:
        points = self.get_monitor_points()
        if not points:
            return None

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        pad = padding or self.config.monitor_capture_padding

        left = max(0, min(xs) - pad)
        top = max(0, min(ys) - pad)
        right = max(xs) + pad + 1
        bottom = max(ys) + pad + 1

        return (left, top, right, bottom)

    @contextmanager
    def capture_region(self):
        screenshot = None
        try:
            bbox = self.get_capture_bbox()
            if bbox:
                screenshot = ImageGrab.grab(bbox=bbox).convert("RGB")
                yield screenshot, bbox
            else:
                screenshot = ImageGrab.grab().convert("RGB")
                yield screenshot, None
        except Exception as exc:
            logger.error("Screenshot failed: %s", exc)
            yield None, None
        finally:
            if screenshot:
                screenshot.close()

    def get_pixel_rgb(
        self,
        screenshot: Image.Image,
        point: Tuple[int, int],
        bbox: Optional[Tuple[int, int, int, int]] = None,
    ) -> Tuple[int, int, int]:
        if screenshot is None:
            return (0, 0, 0)

        x, y = point
        if bbox:
            x -= bbox[0]
            y -= bbox[1]

        radius = self.config.color_sample_radius
        left = max(0, x - radius)
        top = max(0, y - radius)
        right = min(screenshot.width - 1, x + radius)
        bottom = min(screenshot.height - 1, y + radius)

        total_r = total_g = total_b = 0
        count = 0

        for px in range(left, right + 1):
            for py in range(top, bottom + 1):
                r, g, b = screenshot.getpixel((px, py))
                total_r += r
                total_g += g
                total_b += b
                count += 1

        if count == 0:
            return screenshot.getpixel((x, y))

        return (total_r // count, total_g // count, total_b // count)

    def is_fully_calibrated(self) -> bool:
        return len(self.calibration_points) >= self.config.total_calibration_points

    def clear_calibration(self):
        self.calibration_points.clear()

    def validate_calibration(self) -> bool:
        screen_width, screen_height = pyautogui.size()
        for point in self.calibration_points.values():
            if not (0 <= point.x < screen_width and 0 <= point.y < screen_height):
                logger.error("Calibration point %s at (%s, %s) is off-screen", point.name, point.x, point.y)
                return False
        logger.info("Calibration validated: %s points within screen bounds", len(self.calibration_points))
        return True


class GameAnalyzer:
    def __init__(self, config: AppConfig, capture_mgr: ScreenCaptureManager, color_matcher: ColorMatcher):
        self.config = config
        self.capture_mgr = capture_mgr
        self.color_matcher = color_matcher

    def analyze_columns(self, screenshot: Image.Image, bbox: Optional[Tuple]) -> List[ColumnAnalysis]:
        if screenshot is None:
            return []

        grid_points = self.capture_mgr.get_grid_points()
        if not grid_points:
            return []

        columns = []
        for col_idx, column_points in enumerate(grid_points):
            box_colors = []
            has_unknown = False
            is_full = True

            for point in column_points:
                rgb = self.capture_mgr.get_pixel_rgb(screenshot, point, bbox)

                if self.color_matcher.is_blank_color(rgb):
                    box_colors.append("Blank")
                    is_full = False
                else:
                    color_name, score, is_match = self.color_matcher.match_color(rgb)
                    color_display = color_name if is_match else "Unknown"
                    box_colors.append(color_display)
                    if color_display == "Unknown":
                        has_unknown = True

            columns.append(
                ColumnAnalysis(
                    column_index=col_idx + 1,
                    boxes=box_colors,
                    is_full=is_full,
                    has_unknown=has_unknown,
                    box_debug=[],
                )
            )

        return columns

    def is_blank_reference_visible(self, screenshot: Image.Image, bbox: Optional[Tuple]) -> bool:
        blank_point = self.capture_mgr.get_named_point(self.config.blank_color_label)
        if screenshot is None or blank_point is None:
            return False

        rgb = self.capture_mgr.get_pixel_rgb(screenshot, blank_point, bbox)
        return self.color_matcher.is_blank_color(rgb)

    def analyze_game_state(self, screenshot: Image.Image, bbox: Optional[Tuple]) -> GameState:
        if screenshot is None:
            return GameState(
                timestamp=time.time(),
                columns=[],
                all_columns_full=False,
                any_unknown=True,
                blank_detected=False,
                confidence_score=0,
                blank_reference_visible=False,
            )

        columns = self.analyze_columns(screenshot, bbox)

        total_boxes = len(columns) * self.config.boxes_per_column if columns else 0
        blank_boxes = sum(1 for col in columns for box in col.boxes if box == "Blank") if columns else 0
        blank_detected = total_boxes > 0 and blank_boxes == total_boxes

        all_columns_full = all(col.is_full for col in columns) if columns else False
        any_unknown = any(col.has_unknown for col in columns) if columns else False

        if columns:
            invalid_boxes = {"Blank", "Unknown"}
            known_boxes = sum(1 for col in columns for box in col.boxes if box not in invalid_boxes)
            total_boxes_count = len(columns) * self.config.boxes_per_column
            confidence = (known_boxes / total_boxes_count) * 100 if total_boxes_count > 0 else 0
        else:
            confidence = 0

        game_state = GameState(
            timestamp=time.time(),
            columns=columns,
            all_columns_full=all_columns_full,
            any_unknown=any_unknown,
            blank_detected=blank_detected,
            confidence_score=confidence,
            blank_reference_visible=self.is_blank_reference_visible(screenshot, bbox),
        )

        if game_state.has_invalid_board_pattern:
            game_state.confidence_score = 0

        return game_state
