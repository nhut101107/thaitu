"""Screen-color auto clicker for Windows desktop.

This utility scans a small box around the center of the primary display,
finds sufficiently large red regions, and clicks the selected target.

Controls:
    F10 - toggle clicking on/off
    F12 - stop and exit

The program needs a real graphical desktop session. It will not work on a
normal headless VPS without a display server/RDP session.
"""

from __future__ import annotations

import math
import platform
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional

import cv2
import keyboard
import mss
import numpy as np
import pyautogui


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCAN_BOX_SIZE = 400
MIN_CONTOUR_AREA = 20
MAX_TARGET_DISTANCE = 220
CLICK_INTERVAL = 0.15
IDLE_LOOP_INTERVAL = 0.03
ACTIVE_LOOP_INTERVAL = 0.005
STATUS_INTERVAL = 1.0

LOWER_RED1 = np.array([0, 120, 70], dtype=np.uint8)
UPPER_RED1 = np.array([10, 255, 255], dtype=np.uint8)
LOWER_RED2 = np.array([170, 120, 70], dtype=np.uint8)
UPPER_RED2 = np.array([180, 255, 255], dtype=np.uint8)

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02


@dataclass(frozen=True)
class MonitorBox:
    """A screen region and its center point."""

    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (
            self.left + self.width // 2,
            self.top + self.height // 2,
        )


@dataclass(frozen=True)
class Target:
    """A detected target in absolute screen coordinates."""

    x: int
    y: int
    area: float


state_lock = Lock()
is_running = False
should_exit = False


def create_monitor() -> MonitorBox:
    """Create a centered scan box using the current primary display size."""

    screen_width, screen_height = pyautogui.size()
    if screen_width < SCAN_BOX_SIZE or screen_height < SCAN_BOX_SIZE:
        raise RuntimeError(
            f"Màn hình {screen_width}x{screen_height} nhỏ hơn vùng quét "
            f"{SCAN_BOX_SIZE}x{SCAN_BOX_SIZE}."
        )

    return MonitorBox(
        left=int(screen_width / 2 - SCAN_BOX_SIZE / 2),
        top=int(screen_height / 2 - SCAN_BOX_SIZE / 2),
        width=SCAN_BOX_SIZE,
        height=SCAN_BOX_SIZE,
    )


def toggle_macro() -> None:
    """Toggle the active state from the F10 global hotkey."""

    global is_running
    with state_lock:
        is_running = not is_running
        current_state = is_running

    if current_state:
        print("[ON] Đã bật auto, đang quét điểm đỏ...")
    else:
        print("[OFF] Đã tắt auto.")


def request_exit() -> None:
    """Ask the main loop to stop from the F12 global hotkey."""

    global should_exit, is_running
    with state_lock:
        should_exit = True
        is_running = False
    print("[EXIT] Đang thoát tool...")


def get_state() -> tuple[bool, bool]:
    with state_lock:
        return is_running, should_exit


def register_hotkeys() -> None:
    """Register one toggle hotkey and one emergency exit hotkey."""

    try:
        keyboard.add_hotkey("f10", toggle_macro)
        keyboard.add_hotkey("f12", request_exit)
    except Exception as exc:
        if platform.system() != "Windows":
            raise RuntimeError(
                "Không đăng ký được hotkey. Trên Linux/VPS, thư viện "
                "keyboard thường cần quyền phù hợp và một phiên desktop."
            ) from exc
        raise RuntimeError(
            "Không đăng ký được F10/F12. Hãy thử chạy terminal bằng quyền "
            "Administrator trên Windows."
        ) from exc


def build_red_mask(image: np.ndarray) -> np.ndarray:
    """Convert an MSS screenshot to HSV and return a cleaned red mask."""

    if image.ndim != 3:
        raise ValueError(f"Ảnh chụp không hợp lệ: shape={image.shape}")

    if image.shape[2] == 4:
        bgr = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    elif image.shape[2] == 3:
        bgr = image
    else:
        raise ValueError(f"Ảnh chụp không có 3 hoặc 4 kênh: shape={image.shape}")

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask1 = cv2.inRange(hsv, LOWER_RED1, UPPER_RED1)
    mask2 = cv2.inRange(hsv, LOWER_RED2, UPPER_RED2)
    mask = cv2.bitwise_or(mask1, mask2)

    # Remove isolated pixels and close tiny holes in a detected region.
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    return mask


def detect_red_target(image: np.ndarray, monitor: MonitorBox) -> Optional[Target]:
    """Find the largest useful red contour near the scan-box center."""

    mask = build_red_mask(image)
    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    center_x = monitor.width / 2
    center_y = monitor.height / 2
    candidates: list[tuple[float, float, int, int, int, int]] = []

    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < MIN_CONTOUR_AREA:
            continue

        x, y, width, height = cv2.boundingRect(contour)
        contour_center_x = x + width / 2
        contour_center_y = y + height / 2
        distance = math.hypot(
            contour_center_x - center_x,
            contour_center_y - center_y,
        )
        candidates.append((area, distance, x, y, width, height))

    if not candidates:
        return None

    nearby = [item for item in candidates if item[1] <= MAX_TARGET_DISTANCE]
    pool = nearby or candidates
    area, _distance, x, y, width, height = max(pool, key=lambda item: item[0])

    target_x = monitor.left + x + width // 2
    target_y = monitor.top + y + height // 2
    return Target(target_x, target_y, area)


def clamp_to_screen(x: int, y: int) -> tuple[int, int]:
    """Keep a click coordinate inside the primary screen bounds."""

    screen_width, screen_height = pyautogui.size()
    return (
        max(0, min(int(x), screen_width - 1)),
        max(0, min(int(y), screen_height - 1)),
    )


def click_target(target: Target) -> None:
    x, y = clamp_to_screen(target.x, target.y)
    pyautogui.moveTo(x, y, duration=0.02)
    pyautogui.click()


def click_center(monitor: MonitorBox) -> None:
    """Fallback click: always click the center of the scan box."""

    x, y = clamp_to_screen(*monitor.center)
    pyautogui.moveTo(x, y, duration=0.02)
    pyautogui.click()


def main() -> None:
    monitor = create_monitor()
    register_hotkeys()

    print("=== AUTO RED CLICKER ===")
    print(f"Vùng quét: {monitor.width}x{monitor.height} tại tâm màn hình")
    print("F10: bật/tắt | F12: thoát hoàn toàn")

    last_click_time = 0.0
    last_status_time = 0.0

    try:
        with mss.mss() as screen_capture:
            while True:
                running, exiting = get_state()
                if exiting:
                    break

                if not running:
                    time.sleep(IDLE_LOOP_INTERVAL)
                    continue

                try:
                    screenshot = np.asarray(screen_capture.grab({
                        "top": monitor.top,
                        "left": monitor.left,
                        "width": monitor.width,
                        "height": monitor.height,
                    }))
                    target = detect_red_target(screenshot, monitor)
                except Exception as exc:
                    now = time.monotonic()
                    if now - last_status_time >= STATUS_INTERVAL:
                        print(f"[ERROR] Lỗi quét màn hình: {exc}")
                        last_status_time = now
                    time.sleep(ACTIVE_LOOP_INTERVAL)
                    continue

                now = time.monotonic()
                if now - last_click_time < CLICK_INTERVAL:
                    time.sleep(ACTIVE_LOOP_INTERVAL)
                    continue

                if target is not None:
                    click_target(target)
                else:
                    click_center(monitor)
                    if now - last_status_time >= STATUS_INTERVAL:
                        print("[SCAN] Không thấy điểm đỏ, đang click tại tâm...")
                        last_status_time = now

                last_click_time = time.monotonic()

    except KeyboardInterrupt:
        print("\n[EXIT] Đã thoát bằng Ctrl+C.")
    except Exception as exc:
        print(f"[FATAL] {exc}")
    finally:
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        print("Đã dọn dẹp hotkey và kết thúc chương trình.")


if __name__ == "__main__":
    main()
