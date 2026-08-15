"""Auto clicker nhận diện mục tiêu màu đỏ trên Windows desktop.

Chạy ở trạng thái tắt. Nhấn F10 để bật/tắt auto click và F12 để thoát.
Chế độ --dry-run chỉ chụp/nhận diện một lần, không di chuyển hoặc click chuột.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import logging
import math
import os
import sys
import time
import threading
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from types import ModuleType
from typing import Any, Callable, Sequence


# ============================== CẤU HÌNH ==============================
# MSS dùng hệ tọa độ pixel của virtual desktop. Vì vậy các tọa độ bên dưới
# được cộng với left/top của vùng quét để click đúng trên toàn màn hình.
SCAN_WIDTH = 400
SCAN_HEIGHT = 400

# OpenCV biểu diễn Hue trong khoảng 0..179. Màu đỏ nằm ở hai đầu dải Hue.
RED_HSV_RANGES = (
    ((0, 100, 80), (10, 255, 255)),
    ((170, 100, 80), (179, 255, 255)),
)

MIN_CONTOUR_AREA = 20
MORPHOLOGY_KERNEL_SIZE = 3

# Khoảng nghỉ sau mỗi lần click, tính bằng giây.
CLICK_INTERVAL_SECONDS = 0.15
MIN_CLICK_INTERVAL_SECONDS = 0.05
DISABLED_POLL_INTERVAL_SECONDS = 0.10
START_COUNTDOWN_SECONDS = 3.0
DEFAULT_MAX_RUN_DURATION_SECONDS = 10 * 60
DEFAULT_FOREGROUND_TITLE = "GTA5X"
MINIGAME_ACTION_MODES = {"wood", "stone"}

# Vùng tìm prompt cưa gỗ, tính tương đối với vùng quét 400x400. Prompt trong
# ảnh mẫu nằm gần chính giữa; các hằng số này để đầu file cho dễ chỉnh nếu UI scale khác.
PROMPT_REGION_LEFT = 80
PROMPT_REGION_TOP = 80
PROMPT_REGION_WIDTH = 240
PROMPT_REGION_HEIGHT = 200
PROMPT_MIN_CONFIDENCE = 0.35
PROMPT_MIN_SCORE_MARGIN = 0.02
PROMPT_LOST_GRACE_SECONDS = 1.0
PROMPT_POLL_INTERVAL_SECONDS = 0.05
WINDOW_SCAN_INTERVAL_SECONDS = 0.15

# Một số mini-game dùng ô phím xanh thay cho ô đen; vẫn nhận chữ A-Z màu sáng bên trong.
BLUE_PROMPT_HSV_LOWER = (85, 45, 25)
BLUE_PROMPT_HSV_UPPER = (145, 255, 255)

# Trọng số ưu tiên mục tiêu gần tâm. Diện tích vẫn là yếu tố chính.
CENTER_PROXIMITY_WEIGHT = 1.50

LOG_LEVEL = logging.INFO
# ======================================================================


LOGGER = logging.getLogger("auto_red_clicker")


class AutoClickerError(RuntimeError):
    """Lỗi có thể hiển thị trực tiếp cho người dùng."""


class DependencyError(AutoClickerError):
    """Một hoặc nhiều dependency chưa được cài."""


class DesktopUnavailableError(AutoClickerError):
    """Không thể truy cập Windows desktop để chụp màn hình."""


def configure_console_encoding() -> None:
    """Giúp log/--help tiếng Việt không lỗi trên console Windows CP1252."""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def get_foreground_window_title() -> str:
    """Lấy title cửa sổ foreground để tránh click nhầm sang ứng dụng khác."""

    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        title_buffer = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        return title_buffer.value
    except (AttributeError, OSError):
        return ""


def list_visible_windows() -> list["WindowInfo"]:
    """Liệt kê cửa sổ Windows hiển thị cùng tọa độ/kích thước."""

    if os.name != "nt":
        return []
    try:
        user32 = ctypes.windll.user32
        windows: list[WindowInfo] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.strip()
            if not title:
                return True
            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                if rect.right <= rect.left or rect.bottom <= rect.top:
                    return True
            windows.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    title=title,
                    left=int(rect.left),
                    top=int(rect.top),
                    width=int(rect.right - rect.left),
                    height=int(rect.bottom - rect.top),
                )
            )
            return True

        user32.EnumWindows(enum_callback, 0)
        unique: dict[tuple[str, int, int, int, int], WindowInfo] = {}
        for window in windows:
            key = (window.title, window.left, window.top, window.width, window.height)
            unique.setdefault(key, window)
        return list(unique.values())
    except (AttributeError, OSError):
        return []


def list_visible_window_titles() -> list[str]:
    """Liệt kê title các cửa sổ Windows đang hiển thị để chọn trong GUI."""

    return list(dict.fromkeys(window.title for window in list_visible_windows()))


def activate_window(hwnd: int) -> bool:
    """Đưa cửa sổ đã tìm thấy lên foreground trước khi nhấn phím."""

    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 5)
        return bool(user32.SetForegroundWindow(hwnd))
    except (AttributeError, OSError):
        return False


def foreground_window_matches(title_fragment: str) -> tuple[bool, str]:
    """So khớp không phân biệt hoa thường với title cửa sổ foreground."""

    expected = title_fragment.strip()
    if not expected:
        return True, get_foreground_window_title()
    current_title = get_foreground_window_title()
    return expected.casefold() in current_title.casefold(), current_title


@dataclass(frozen=True)
class Dependencies:
    """Các module được nạp trễ để import file vẫn cho lỗi cài đặt rõ ràng."""

    mss: ModuleType
    cv2: ModuleType
    numpy: ModuleType
    pyautogui: ModuleType
    keyboard: ModuleType


@dataclass(frozen=True)
class Target:
    """Mục tiêu đỏ trong vùng quét, dùng tọa độ tương đối với vùng quét."""

    x: int
    y: int
    area: float
    distance_from_center: float
    score: float


@dataclass(frozen=True)
class WindowInfo:
    """Cửa sổ Windows hiển thị được dùng khi quét prompt."""

    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int


def load_dependencies() -> Dependencies:
    """Nạp các dependency và báo đúng tên package còn thiếu."""

    modules: dict[str, ModuleType] = {}
    package_names = {
        "mss": "mss",
        "cv2": "opencv-python",
        "numpy": "numpy",
        "pyautogui": "pyautogui",
        "keyboard": "keyboard",
    }
    missing: list[str] = []

    for module_name, package_name in package_names.items():
        try:
            modules[module_name] = importlib.import_module(module_name)
        except ImportError:
            missing.append(package_name)

    if missing:
        missing_text = ", ".join(missing)
        raise DependencyError(
            f"Thiếu dependency: {missing_text}. "
            "Cài bằng: python -m pip install -r requirements_auto_clicker.txt"
        )

    return Dependencies(
        mss=modules["mss"],
        cv2=modules["cv2"],
        numpy=modules["numpy"],
        pyautogui=modules["pyautogui"],
        keyboard=modules["keyboard"],
    )


def configure_windows_dpi_awareness() -> None:
    """Đồng bộ hệ tọa độ MSS và PyAutoGUI trên Windows khi DPI scaling bật."""

    if os.name != "nt":
        return

    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            LOGGER.warning("Không thể bật DPI awareness; tọa độ có thể bị ảnh hưởng bởi scaling.")


def _read_size(size: Any) -> tuple[int, int]:
    """Đọc kích thước từ Size của PyAutoGUI hoặc tuple tương thích."""

    try:
        return int(size.width), int(size.height)
    except AttributeError:
        return int(size[0]), int(size[1])


def get_virtual_monitor(capture: Any) -> dict[str, int]:
    """Lấy virtual desktop (monitors[0]) để hỗ trợ cả tọa độ âm đa màn hình."""

    try:
        monitors = capture.monitors
        monitor = monitors[0]
        left = int(monitor["left"])
        top = int(monitor["top"])
        width = int(monitor["width"])
        height = int(monitor["height"])
    except (IndexError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DesktopUnavailableError(
            "Không thể đọc virtual desktop. Tool không thể chạy nếu máy không có giao diện màn hình."
        ) from exc

    if width <= 0 or height <= 0:
        raise DesktopUnavailableError(
            "Không phát hiện giao diện màn hình hoạt động (kích thước desktop bằng 0). Tool không thể chạy."
        )

    return {"left": left, "top": top, "width": width, "height": height}


def ensure_desktop_available(capture: Any, pyautogui_module: ModuleType) -> dict[str, int]:
    """Xác thực Windows desktop và vùng 400x400 trước khi chạy."""

    if os.name != "nt":
        raise DesktopUnavailableError("Tool này chỉ chạy trên Windows desktop có giao diện màn hình.")

    monitor = get_virtual_monitor(capture)
    if monitor["width"] < SCAN_WIDTH or monitor["height"] < SCAN_HEIGHT:
        raise DesktopUnavailableError(
            f"Màn hình ({monitor['width']}x{monitor['height']}) nhỏ hơn vùng quét "
            f"{SCAN_WIDTH}x{SCAN_HEIGHT}; tool không thể chạy."
        )

    try:
        screen_width, screen_height = _read_size(pyautogui_module.size())
    except Exception as exc:
        raise DesktopUnavailableError(
            "Không thể đọc kích thước màn hình qua PyAutoGUI. "
            "Tool không thể chạy khi không có giao diện desktop."
        ) from exc

    if screen_width <= 0 or screen_height <= 0:
        raise DesktopUnavailableError(
            "PyAutoGUI báo màn hình không hoạt động (kích thước bằng 0). Tool không thể chạy."
        )

    return monitor


def build_scan_region(monitor: dict[str, int]) -> dict[str, int]:
    """Tạo vùng 400x400 tại chính giữa virtual desktop."""

    return {
        "left": monitor["left"] + (monitor["width"] - SCAN_WIDTH) // 2,
        "top": monitor["top"] + (monitor["height"] - SCAN_HEIGHT) // 2,
        "width": SCAN_WIDTH,
        "height": SCAN_HEIGHT,
    }


def detect_red_target(frame: Any, cv2_module: ModuleType, numpy_module: ModuleType) -> Target | None:
    """Lọc hai dải đỏ HSV và chọn contour lớn, gần tâm nhất theo điểm số."""

    if getattr(frame, "ndim", 0) != 3 or frame.shape[2] not in (3, 4):
        raise AutoClickerError("Ảnh chụp màn hình có định dạng không được hỗ trợ.")

    if frame.shape[2] == 4:
        # MSS trả BGRA; OpenCV không cung cấp mã BGRA2HSV ở mọi phiên bản.
        bgr = cv2_module.cvtColor(frame, cv2_module.COLOR_BGRA2BGR)
        hsv = cv2_module.cvtColor(bgr, cv2_module.COLOR_BGR2HSV)
    else:
        hsv = cv2_module.cvtColor(frame, cv2_module.COLOR_BGR2HSV)

    mask = numpy_module.zeros(hsv.shape[:2], dtype=numpy_module.uint8)
    for lower, upper in RED_HSV_RANGES:
        red_part = cv2_module.inRange(
            hsv,
            numpy_module.array(lower, dtype=numpy_module.uint8),
            numpy_module.array(upper, dtype=numpy_module.uint8),
        )
        mask = cv2_module.bitwise_or(mask, red_part)

    kernel_size = max(1, int(MORPHOLOGY_KERNEL_SIZE))
    kernel = numpy_module.ones((kernel_size, kernel_size), dtype=numpy_module.uint8)
    mask = cv2_module.morphologyEx(mask, cv2_module.MORPH_OPEN, kernel)
    mask = cv2_module.morphologyEx(mask, cv2_module.MORPH_CLOSE, kernel)

    contours_result = cv2_module.findContours(
        mask, cv2_module.RETR_EXTERNAL, cv2_module.CHAIN_APPROX_SIMPLE
    )
    contours = contours_result[-2]

    scan_center_x = (SCAN_WIDTH - 1) / 2.0
    scan_center_y = (SCAN_HEIGHT - 1) / 2.0
    max_center_distance = math.hypot(scan_center_x, scan_center_y)
    candidates: list[Target] = []

    for contour in contours:
        area = float(cv2_module.contourArea(contour))
        if area < MIN_CONTOUR_AREA:
            continue

        moments = cv2_module.moments(contour)
        if moments["m00"]:
            center_x = float(moments["m10"] / moments["m00"])
            center_y = float(moments["m01"] / moments["m00"])
        else:
            x, y, width, height = cv2_module.boundingRect(contour)
            center_x = x + width / 2.0
            center_y = y + height / 2.0

        distance = math.hypot(center_x - scan_center_x, center_y - scan_center_y)
        proximity = max(0.0, 1.0 - distance / max_center_distance)
        score = area * (1.0 + CENTER_PROXIMITY_WEIGHT * proximity)
        candidates.append(
            Target(
                x=int(round(center_x)),
                y=int(round(center_y)),
                area=area,
                distance_from_center=distance,
                score=score,
            )
        )

    if not candidates:
        return None

    return max(candidates, key=lambda candidate: candidate.score)


@lru_cache(maxsize=4)
def build_letter_templates(cv2_module: ModuleType, numpy_module: ModuleType) -> dict[str, Any]:
    """Tạo mẫu chữ A-Z bằng OpenCV để nhận diện prompt mà không cần OCR ngoài."""

    templates: dict[str, Any] = {}
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        canvas = numpy_module.zeros((72, 72), dtype=numpy_module.uint8)
        font = cv2_module.FONT_HERSHEY_SIMPLEX
        font_scale = 1.7
        thickness = 3
        text_size, _ = cv2_module.getTextSize(letter, font, font_scale, thickness)
        text_x = (canvas.shape[1] - text_size[0]) // 2
        text_y = (canvas.shape[0] + text_size[1]) // 2
        cv2_module.putText(
            canvas,
            letter,
            (text_x, text_y),
            font,
            font_scale,
            255,
            thickness,
            cv2_module.LINE_AA,
        )
        templates[letter] = normalize_glyph(canvas, cv2_module, numpy_module)
    return templates


def normalize_glyph(mask: Any, cv2_module: ModuleType, numpy_module: ModuleType) -> Any:
    """Cắt viền và đưa glyph về kích thước chuẩn để so mẫu."""

    points = numpy_module.argwhere(mask > 0)
    if points.size == 0:
        return None
    top, left = points.min(axis=0)
    bottom, right = points.max(axis=0) + 1
    cropped = mask[top:bottom, left:right]
    return cv2_module.resize(cropped, (32, 48), interpolation=cv2_module.INTER_AREA)


def _find_prompt_glyph(mask: Any, cv2_module: ModuleType, numpy_module: ModuleType) -> Any:
    """Tìm glyph trắng có kích thước phù hợp trong vùng prompt."""

    contours_result = cv2_module.findContours(
        mask, cv2_module.RETR_EXTERNAL, cv2_module.CHAIN_APPROX_SIMPLE
    )
    contours = contours_result[-2]
    expected_x = PROMPT_REGION_WIDTH / 2.0
    expected_y = PROMPT_REGION_HEIGHT * 0.45
    candidates: list[tuple[float, Any]] = []

    for contour in contours:
        area = float(cv2_module.contourArea(contour))
        x, y, width, height = cv2_module.boundingRect(contour)
        if area < 8 or width < 2 or height < 8 or width > 55 or height > 65:
            continue
        center_x = x + width / 2.0
        center_y = y + height / 2.0
        distance = math.hypot(center_x - expected_x, center_y - expected_y)
        # Ưu tiên glyph gần tâm vùng prompt, sau đó ưu tiên hình đủ lớn.
        score = distance - min(area, 250.0) / 1000.0
        candidates.append((score, (x, y, width, height)))

    if not candidates:
        return None
    _, (x, y, width, height) = min(candidates, key=lambda item: item[0])
    return mask[max(0, y - 2) : y + height + 2, max(0, x - 2) : x + width + 2]


def _classify_prompt_glyph(
    glyph: Any,
    cv2_module: ModuleType,
    numpy_module: ModuleType,
) -> tuple[str, float] | None:
    """Classify one normalized A-Z glyph against the local templates."""

    if glyph is None:
        return None
    normalized = normalize_glyph(glyph, cv2_module, numpy_module)
    if normalized is None:
        return None

    scores: list[tuple[float, str]] = []
    for letter, template in build_letter_templates(cv2_module, numpy_module).items():
        score = float(
            cv2_module.matchTemplate(normalized, template, cv2_module.TM_CCOEFF_NORMED)[0, 0]
        )
        scores.append((score, letter))
    scores.sort(reverse=True)
    best_score, best_letter = scores[0]
    second_score = scores[1][0]
    if best_score < PROMPT_MIN_CONFIDENCE or best_score - second_score < PROMPT_MIN_SCORE_MARGIN:
        return None
    return best_letter, best_score


def _find_blue_prompt_glyph(
    roi: Any,
    hsv: Any,
    cv2_module: ModuleType,
    numpy_module: ModuleType,
) -> Any:
    """Extract a light A-Z glyph from a blue key button, if present."""

    blue_mask = cv2_module.inRange(
        hsv,
        numpy_module.array(BLUE_PROMPT_HSV_LOWER, dtype=numpy_module.uint8),
        numpy_module.array(BLUE_PROMPT_HSV_UPPER, dtype=numpy_module.uint8),
    )
    kernel = numpy_module.ones((3, 3), dtype=numpy_module.uint8)
    blue_mask = cv2_module.morphologyEx(blue_mask, cv2_module.MORPH_CLOSE, kernel)

    contours_result = cv2_module.findContours(
        blue_mask, cv2_module.RETR_EXTERNAL, cv2_module.CHAIN_APPROX_SIMPLE
    )
    contours = contours_result[-2]
    expected_x = PROMPT_REGION_WIDTH / 2.0
    expected_y = PROMPT_REGION_HEIGHT * 0.72
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []

    for contour in contours:
        area = float(cv2_module.contourArea(contour))
        x, y, width, height = cv2_module.boundingRect(contour)
        if area < 30 or width < 10 or height < 10 or width > 100 or height > 100:
            continue
        aspect = width / max(1.0, float(height))
        if aspect < 0.45 or aspect > 2.2:
            continue
        center_x = x + width / 2.0
        center_y = y + height / 2.0
        distance = math.hypot(center_x - expected_x, center_y - expected_y)
        score = distance - min(area, 1200.0) / 1000.0
        candidates.append((score, (x, y, width, height)))

    for _, (x, y, width, height) in sorted(candidates, key=lambda item: item[0]):
        margin_x = max(2, width // 6)
        margin_y = max(2, height // 6)
        left = min(roi.shape[1], x + margin_x)
        top = min(roi.shape[0], y + margin_y)
        right = max(left, min(roi.shape[1], x + width - margin_x))
        bottom = max(top, min(roi.shape[0], y + height - margin_y))
        button_hsv = hsv[top:bottom, left:right]
        if button_hsv.size == 0:
            continue
        # A blue button normally contains a light glyph; low saturation removes the blue background.
        glyph_mask = cv2_module.inRange(
            button_hsv,
            numpy_module.array((0, 0, 120), dtype=numpy_module.uint8),
            numpy_module.array((179, 160, 255), dtype=numpy_module.uint8),
        )
        glyph_mask = cv2_module.morphologyEx(glyph_mask, cv2_module.MORPH_OPEN, kernel)
        glyph = normalize_glyph(glyph_mask, cv2_module, numpy_module)
        if glyph is None:
            # Fallback for a dark glyph on a bright/blue button.
            dark_mask = cv2_module.inRange(
                button_hsv,
                numpy_module.array((0, 0, 0), dtype=numpy_module.uint8),
                numpy_module.array((179, 160, 105), dtype=numpy_module.uint8),
            )
            dark_mask = cv2_module.morphologyEx(dark_mask, cv2_module.MORPH_OPEN, kernel)
            glyph = normalize_glyph(dark_mask, cv2_module, numpy_module)
        if glyph is not None:
            return glyph
    return None


def detect_prompt_key(frame: Any, cv2_module: ModuleType, numpy_module: ModuleType) -> tuple[str, float] | None:
    """Nhận diện prompt A-Z ở kiểu ô đen/trắng hoặc ô xanh."""

    if getattr(frame, "ndim", 0) != 3 or frame.shape[2] not in (3, 4):
        return None
    if frame.shape[2] == 4:
        bgr = cv2_module.cvtColor(frame, cv2_module.COLOR_BGRA2BGR)
    else:
        bgr = frame

    left = max(0, PROMPT_REGION_LEFT)
    top = max(0, PROMPT_REGION_TOP)
    right = min(frame.shape[1], left + PROMPT_REGION_WIDTH)
    bottom = min(frame.shape[0], top + PROMPT_REGION_HEIGHT)
    roi = bgr[top:bottom, left:right]
    if roi.size == 0:
        return None

    hsv = cv2_module.cvtColor(roi, cv2_module.COLOR_BGR2HSV)
    # Chữ prompt là trắng; saturation thấp và value cao giúp bỏ phần nền màu.
    white_mask = cv2_module.inRange(
        hsv,
        numpy_module.array((0, 0, 160), dtype=numpy_module.uint8),
        numpy_module.array((179, 100, 255), dtype=numpy_module.uint8),
    )
    kernel = numpy_module.ones((2, 2), dtype=numpy_module.uint8)
    white_mask = cv2_module.morphologyEx(white_mask, cv2_module.MORPH_OPEN, kernel)
    white_glyph = _find_prompt_glyph(white_mask, cv2_module, numpy_module)
    white_result = _classify_prompt_glyph(white_glyph, cv2_module, numpy_module)

    # Prefer a detected blue button so other white HUD text cannot win by accident.
    blue_glyph = _find_blue_prompt_glyph(roi, hsv, cv2_module, numpy_module)
    blue_result = _classify_prompt_glyph(blue_glyph, cv2_module, numpy_module)
    return blue_result or white_result


def scan_visible_windows_for_prompt(
    capture: Any,
    cv2_module: ModuleType,
    numpy_module: ModuleType,
) -> tuple[WindowInfo, dict[str, int], Any, tuple[str, float]] | None:
    """Quét tất cả cửa sổ đủ lớn và trả về cửa sổ có prompt A-Z đầu tiên."""

    for window in list_visible_windows():
        if "autoredclicker" in window.title.casefold().replace(" ", ""):
            continue
        if window.width < SCAN_WIDTH or window.height < SCAN_HEIGHT:
            continue
        region = {
            "left": window.left + (window.width - SCAN_WIDTH) // 2,
            "top": window.top + (window.height - SCAN_HEIGHT) // 2,
            "width": SCAN_WIDTH,
            "height": SCAN_HEIGHT,
        }
        try:
            frame = numpy_module.asarray(capture.grab(region))
            prompt = detect_prompt_key(frame, cv2_module, numpy_module)
        except Exception:
            continue
        if prompt is not None:
            return window, region, frame, prompt
    return None


def close_capture(capture: Any) -> None:
    """Đóng MSS an toàn, không che mất lỗi chính."""

    try:
        capture.close()
    except Exception:
        LOGGER.debug("Không thể đóng MSS capture.", exc_info=True)


def capture_frame(capture: Any, region: dict[str, int], numpy_module: ModuleType) -> Any:
    """Chụp frame và chuyển lỗi capture thành thông báo desktop rõ ràng."""

    try:
        return numpy_module.asarray(capture.grab(region))
    except Exception as exc:
        raise DesktopUnavailableError(
            "MSS không thể chụp màn hình (Windows graphics/BitBlt thất bại). "
            "Có thể session không có desktop hoạt động, đang chạy dưới service/RDP bị hạn chế, "
            "hoặc quyền capture bị từ chối; tool không thể chạy."
        ) from exc


def run_dry_run(capture: Any, region: dict[str, int], dependencies: Dependencies) -> int:
    """Quét một lần và chỉ ghi nhận kết quả, tuyệt đối không chạm chuột."""

    LOGGER.info(
        "DRY-RUN: quét vùng x=%d y=%d w=%d h=%d; không di chuyển/click chuột.",
        region["left"],
        region["top"],
        region["width"],
        region["height"],
    )
    frame = capture_frame(capture, region, dependencies.numpy)
    target = detect_red_target(frame, dependencies.cv2, dependencies.numpy)

    if target is None:
        center_x = region["left"] + SCAN_WIDTH // 2
        center_y = region["top"] + SCAN_HEIGHT // 2
        LOGGER.info(
            "DRY-RUN: không thấy màu đỏ; tọa độ fallback sẽ là tâm vùng quét (%d, %d).",
            center_x,
            center_y,
        )
    else:
        screen_x = region["left"] + target.x
        screen_y = region["top"] + target.y
        LOGGER.info(
            "DRY-RUN: thấy mục tiêu đỏ area=%.1f, cách tâm=%.1f; "
            "tọa độ màn hình=(%d, %d), score=%.1f.",
            target.area,
            target.distance_from_center,
            screen_x,
            screen_y,
            target.score,
        )

    return 0


def register_hotkeys(
    keyboard_module: ModuleType,
    enabled: threading.Event,
    stop_requested: threading.Event,
    on_emergency: Callable[[], None] | None = None,
) -> list[Any]:
    """Đăng ký F10/F12 và trả về handle để dọn đúng các hotkey của tool."""

    handles: list[Any] = []

    def toggle_auto_click() -> None:
        if stop_requested.is_set():
            return
        if enabled.is_set():
            enabled.clear()
            LOGGER.info("F10: auto click TẮT.")
        else:
            enabled.set()
            LOGGER.info("F10: auto click BẬT.")

    def emergency_exit() -> None:
        enabled.clear()
        stop_requested.set()
        LOGGER.warning("F12: nhận lệnh thoát khẩn cấp; đang dừng và dọn hotkey.")
        if on_emergency is not None:
            try:
                on_emergency()
            except Exception:
                LOGGER.debug("Không thể gửi tín hiệu thoát khẩn cấp cho giao diện.", exc_info=True)

    try:
        handles.append(keyboard_module.add_hotkey("f10", toggle_auto_click))
        handles.append(keyboard_module.add_hotkey("f12", emergency_exit))
    except Exception as exc:
        for handle in handles:
            try:
                keyboard_module.remove_hotkey(handle)
            except Exception:
                LOGGER.debug("Không thể gỡ hotkey sau lỗi đăng ký.", exc_info=True)
        raise AutoClickerError(
            "Không đăng ký được F10/F12. Trên Windows, hãy thử chạy terminal với quyền phù hợp."
        ) from exc

    return handles


def cleanup_hotkeys(keyboard_module: ModuleType, handles: Sequence[Any]) -> None:
    """Gỡ riêng các hotkey đã đăng ký bởi tool."""

    for handle in handles:
        try:
            keyboard_module.remove_hotkey(handle)
        except Exception:
            LOGGER.debug("Không thể gỡ một hotkey.", exc_info=True)


def click_target(
    pyautogui_module: ModuleType,
    region: dict[str, int],
    target: Target | None,
) -> tuple[int, int]:
    """Tính tọa độ toàn màn hình rồi di chuyển và click đúng một lần."""

    if target is None:
        screen_x = region["left"] + SCAN_WIDTH // 2
        screen_y = region["top"] + SCAN_HEIGHT // 2
    else:
        screen_x = region["left"] + target.x
        screen_y = region["top"] + target.y

    pyautogui_module.moveTo(screen_x, screen_y, duration=0)
    pyautogui_module.click()
    return screen_x, screen_y


def run_interactive(
    capture: Any,
    region: dict[str, int],
    dependencies: Dependencies,
    enabled: threading.Event | None = None,
    stop_requested: threading.Event | None = None,
    click_interval_seconds: float = CLICK_INTERVAL_SECONDS,
    max_clicks: int = 0,
    on_emergency: Callable[[], None] | None = None,
    max_run_duration_seconds: float = DEFAULT_MAX_RUN_DURATION_SECONDS,
    foreground_lock_enabled: bool = True,
    foreground_title_contains: str = DEFAULT_FOREGROUND_TITLE,
    action_mode: str = "click",
    auto_find_prompt_window: bool = False,
) -> int:
    """Vòng lặp auto click không bận CPU khi đang tắt hoặc đang nghỉ."""

    if enabled is None:
        enabled = threading.Event()
    if stop_requested is None:
        stop_requested = threading.Event()
    if click_interval_seconds < MIN_CLICK_INTERVAL_SECONDS:
        raise AutoClickerError(
            f"Khoảng nghỉ giữa các click không được nhỏ hơn {MIN_CLICK_INTERVAL_SECONDS:g} giây."
        )
    if max_clicks < 0:
        raise AutoClickerError("Giới hạn số click không được là số âm.")
    if max_run_duration_seconds < 0:
        raise AutoClickerError("Thời gian chạy tối đa không được là số âm.")
    if (
        foreground_lock_enabled
        and not foreground_title_contains.strip()
        and not (action_mode in MINIGAME_ACTION_MODES and auto_find_prompt_window)
    ):
        raise AutoClickerError("Tên cửa sổ khóa không được để trống khi bật khóa cửa sổ.")
    if action_mode not in {"click", *MINIGAME_ACTION_MODES}:
        raise AutoClickerError(f"Chế độ thao tác không hợp lệ: {action_mode}.")
    handles: list[Any] = []
    click_count = 0
    run_started_at: float | None = None
    was_enabled = False
    last_window_allowed: bool | None = None
    last_prompt_seen: float | None = None

    def wait_for_start_countdown() -> bool:
        """Đếm ngược trước mỗi lần chuyển từ tắt sang bật."""

        countdown = max(0.0, float(START_COUNTDOWN_SECONDS))
        if countdown <= 0:
            return True
        LOGGER.info("Auto click sẽ bắt đầu sau %.0f giây...", countdown)
        deadline = time.monotonic() + countdown
        last_logged_second: int | None = None
        while True:
            if stop_requested.is_set() or not enabled.is_set():
                LOGGER.info("Đã hủy đếm ngược auto click.")
                return False
            remaining = max(0.0, deadline - time.monotonic())
            remaining_second = int(math.ceil(remaining))
            if remaining_second != last_logged_second and remaining_second > 0:
                LOGGER.info("Bắt đầu sau %d...", remaining_second)
                last_logged_second = remaining_second
            if remaining <= 0:
                return True
            stop_requested.wait(min(0.1, remaining))

    try:
        handles = register_hotkeys(dependencies.keyboard, enabled, stop_requested, on_emergency)
        LOGGER.info(
            "Sẵn sàng. Vùng quét giữa virtual desktop: x=%d y=%d w=%d h=%d.",
            region["left"],
            region["top"],
            region["width"],
            region["height"],
        )
        LOGGER.info("Auto click đang TẮT. F10 bật/tắt; F12 thoát khẩn cấp; Ctrl+C cũng thoát.")

        while not stop_requested.is_set():
            if not enabled.is_set():
                was_enabled = False
                run_started_at = None
                last_window_allowed = None
                last_prompt_seen = None
                # Event.wait nhường CPU, không quay vòng 100% khi đang tắt.
                stop_requested.wait(DISABLED_POLL_INTERVAL_SECONDS)
                continue

            if not was_enabled:
                if not wait_for_start_countdown():
                    continue
                was_enabled = True
                run_started_at = time.monotonic()

            if max_run_duration_seconds and run_started_at is not None:
                elapsed = time.monotonic() - run_started_at
                if elapsed >= max_run_duration_seconds:
                    LOGGER.info(
                        "Đã đạt giới hạn thời gian %.0f giây; tự động dừng.",
                        max_run_duration_seconds,
                    )
                    stop_requested.set()
                    continue

            if foreground_lock_enabled and not (action_mode in MINIGAME_ACTION_MODES and auto_find_prompt_window):
                window_allowed, current_title = foreground_window_matches(foreground_title_contains)
                if not window_allowed:
                    if last_window_allowed is not False:
                        LOGGER.warning(
                            "Tạm dừng: cửa sổ foreground '%s' không khớp '%s'.",
                            current_title or "(không có title)",
                            foreground_title_contains,
                        )
                    last_window_allowed = False
                    stop_requested.wait(DISABLED_POLL_INTERVAL_SECONDS)
                    continue
                if last_window_allowed is False:
                    LOGGER.info("Cửa sổ '%s' đã active; tiếp tục auto click.", current_title)
                last_window_allowed = True

            try:
                prompt: tuple[str, float] | None = None
                if action_mode in MINIGAME_ACTION_MODES and auto_find_prompt_window:
                    prompt_match = scan_visible_windows_for_prompt(
                        capture, dependencies.cv2, dependencies.numpy
                    )
                    if prompt_match is None:
                        if (
                            last_prompt_seen is not None
                            and time.monotonic() - last_prompt_seen >= PROMPT_LOST_GRACE_SECONDS
                        ):
                            LOGGER.info(
                                "Không còn prompt A-Z trong %.1f giây; thanh có thể đã đầy, "
                                "dừng chế độ mini-game.",
                                PROMPT_LOST_GRACE_SECONDS,
                            )
                            stop_requested.set()
                            continue
                        stop_requested.wait(WINDOW_SCAN_INTERVAL_SECONDS)
                        continue
                    matched_window, region, frame, prompt = prompt_match
                    if not activate_window(matched_window.hwnd):
                        LOGGER.warning(
                            "Đã tìm thấy prompt trong '%s' nhưng không thể đưa cửa sổ lên foreground; tạm dừng.",
                            matched_window.title,
                        )
                        stop_requested.wait(WINDOW_SCAN_INTERVAL_SECONDS)
                        continue
                else:
                    frame = capture_frame(capture, region, dependencies.numpy)
                if action_mode in MINIGAME_ACTION_MODES:
                    if prompt is None:
                        prompt = detect_prompt_key(frame, dependencies.cv2, dependencies.numpy)
                    if prompt is None:
                        if (
                            last_prompt_seen is not None
                            and time.monotonic() - last_prompt_seen >= PROMPT_LOST_GRACE_SECONDS
                        ):
                            LOGGER.info(
                                "Không còn prompt A-Z trong %.1f giây; thanh có thể đã đầy, "
                                "dừng chế độ mini-game.",
                                PROMPT_LOST_GRACE_SECONDS,
                            )
                            stop_requested.set()
                            continue
                        stop_requested.wait(PROMPT_POLL_INTERVAL_SECONDS)
                        continue

                    key, confidence = prompt
                    dependencies.pyautogui.press(key.lower())
                    last_prompt_seen = time.monotonic()
                    click_count += 1
                    mode_label = "Cưa gỗ" if action_mode == "wood" else "Đập đá"
                    LOGGER.info(
                        "%s: nhận diện phím %s (confidence=%.2f), đã nhấn %d lần.",
                        mode_label,
                        key,
                        confidence,
                        click_count,
                    )
                    if max_clicks and click_count >= max_clicks:
                        LOGGER.info("Đã đạt giới hạn %d lần nhấn; tự động dừng.", max_clicks)
                        stop_requested.set()
                    stop_requested.wait(click_interval_seconds)
                    continue

                target = detect_red_target(frame, dependencies.cv2, dependencies.numpy)
                screen_x, screen_y = click_target(dependencies.pyautogui, region, target)
            except AutoClickerError as exc:
                LOGGER.error("%s", exc)
                return 1
            except Exception as exc:
                LOGGER.exception("Lỗi trong vòng quét/click; dừng để an toàn: %s", exc)
                return 1

            if target is None:
                LOGGER.info(
                    "Không thấy màu đỏ; click tâm vùng quét tại (%d, %d).", screen_x, screen_y
                )
            else:
                LOGGER.info(
                    "Click mục tiêu đỏ tại (%d, %d), area=%.1f, cách tâm=%.1f.",
                    screen_x,
                    screen_y,
                    target.area,
                    target.distance_from_center,
                )

            click_count += 1
            if max_clicks and click_count >= max_clicks:
                LOGGER.info("Đã đạt giới hạn %d click; tự động dừng.", max_clicks)
                stop_requested.set()

            # Có khoảng nghỉ rõ ràng giữa các click và cho F12 cơ hội dừng nhanh.
            stop_requested.wait(click_interval_seconds)

    finally:
        cleanup_hotkeys(dependencies.keyboard, handles)
        LOGGER.info("Đã dọn dẹp hotkey và dừng auto clicker.")

    return 0


def run_session(
    dry_run: bool = False,
    enabled: threading.Event | None = None,
    stop_requested: threading.Event | None = None,
    click_interval_seconds: float = CLICK_INTERVAL_SECONDS,
    max_clicks: int = 0,
    on_emergency: Callable[[], None] | None = None,
    max_run_duration_seconds: float = DEFAULT_MAX_RUN_DURATION_SECONDS,
    foreground_lock_enabled: bool = True,
    foreground_title_contains: str = DEFAULT_FOREGROUND_TITLE,
    action_mode: str = "click",
    auto_find_prompt_window: bool = False,
) -> int:
    """Chạy một phiên độc lập, dùng chung cho CLI và giao diện GUI."""

    if click_interval_seconds < MIN_CLICK_INTERVAL_SECONDS:
        raise AutoClickerError(
            f"Khoảng nghỉ giữa các click không được nhỏ hơn {MIN_CLICK_INTERVAL_SECONDS:g} giây."
        )
    if max_clicks < 0:
        raise AutoClickerError("Giới hạn số click không được là số âm.")
    if max_run_duration_seconds < 0:
        raise AutoClickerError("Thời gian chạy tối đa không được là số âm.")
    if action_mode not in {"click", *MINIGAME_ACTION_MODES}:
        raise AutoClickerError(f"Chế độ thao tác không hợp lệ: {action_mode}.")

    capture: Any | None = None
    try:
        configure_windows_dpi_awareness()
        dependencies = load_dependencies()
        try:
            mss_factory = getattr(dependencies.mss, "MSS", None)
            if mss_factory is None:
                # Tương thích với bản mss cũ hơn 10.
                mss_factory = dependencies.mss.mss
            capture = mss_factory()
        except Exception as exc:
            raise DesktopUnavailableError(
                "Không thể khởi tạo chụp màn hình MSS. Máy không có giao diện desktop hoạt động "
                "hoặc quyền truy cập màn hình bị từ chối; tool không thể chạy."
            ) from exc

        monitor = ensure_desktop_available(capture, dependencies.pyautogui)
        region = build_scan_region(monitor)
        if dry_run:
            return run_dry_run(capture, region, dependencies)
        return run_interactive(
            capture,
            region,
            dependencies,
            enabled,
            stop_requested,
            click_interval_seconds,
            max_clicks,
            on_emergency,
            max_run_duration_seconds,
            foreground_lock_enabled,
            foreground_title_contains,
            action_mode,
            auto_find_prompt_window,
        )
    finally:
        if capture is not None:
            close_capture(capture)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="quét nhận diện một lần, không di chuyển hoặc click chuột",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=CLICK_INTERVAL_SECONDS,
        help=f"khoảng nghỉ giữa click, tối thiểu {MIN_CLICK_INTERVAL_SECONDS:g}, "
        f"mặc định {CLICK_INTERVAL_SECONDS:g} giây",
    )
    parser.add_argument(
        "--max-clicks",
        type=int,
        default=0,
        help="tự dừng sau N click; 0 là không giới hạn",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_MAX_RUN_DURATION_SECONDS,
        help="thời gian chạy tối đa tính bằng giây; 0 là không giới hạn",
    )
    parser.add_argument(
        "--window-title",
        default=DEFAULT_FOREGROUND_TITLE,
        help=f"title cửa sổ cần active, mặc định '{DEFAULT_FOREGROUND_TITLE}'",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--wood-mode",
        action="store_true",
        help="nhận diện prompt A-Z và nhấn phím cho mini-game cưa gỗ",
    )
    mode_group.add_argument(
        "--stone-mode",
        action="store_true",
        help="detect A-Z prompt and press keys for the stone mini-game",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_encoding()
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
    args = create_parser().parse_args(argv)

    try:
        return run_session(
            dry_run=args.dry_run,
            click_interval_seconds=args.interval,
            max_clicks=args.max_clicks,
            max_run_duration_seconds=args.duration,
            foreground_title_contains=args.window_title,
            action_mode="stone" if args.stone_mode else "wood" if args.wood_mode else "click",
            auto_find_prompt_window=args.wood_mode or args.stone_mode,
        )
    except KeyboardInterrupt:
        LOGGER.info("Đã nhận Ctrl+C; thoát an toàn.")
        return 0
    except AutoClickerError as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception:
        LOGGER.exception("Lỗi không mong đợi; tool không hoàn tất.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
