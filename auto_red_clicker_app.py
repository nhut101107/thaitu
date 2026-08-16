"""GUI launcher cho Auto Red Clicker.

Giao diện này chỉ điều khiển phiên chạy; logic nhận diện/click vẫn nằm trong
auto_red_clicker.py để CLI và app .exe dùng cùng một code.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

# Các import tĩnh này giúp PyInstaller tự phát hiện dependency được nạp trễ
# trong auto_red_clicker.load_dependencies(). Chúng không được dùng trực tiếp.
try:  # pragma: no cover - chỉ phục vụ đóng gói
    import cv2 as _pyinstaller_cv2
    import keyboard as _pyinstaller_keyboard
    import mss as _pyinstaller_mss
    import numpy as _pyinstaller_numpy
    import pyautogui as _pyinstaller_pyautogui

    _PYINSTALLER_IMPORTS = (
        _pyinstaller_cv2,
        _pyinstaller_keyboard,
        _pyinstaller_mss,
        _pyinstaller_numpy,
        _pyinstaller_pyautogui,
    )
except ImportError:
    _PYINSTALLER_IMPORTS = ()

import auto_red_clicker as core


class QueueLogHandler(logging.Handler):
    """Đưa log từ worker thread về thread Tkinter."""

    def __init__(self, messages: queue.Queue[tuple[str, Any]]) -> None:
        super().__init__()
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


class AutoRedClickerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Auto Red Clicker")
        self.root.geometry("720x500")
        self.root.minsize(620, 400)

        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_requested: threading.Event | None = None
        self.enabled: threading.Event | None = None

        self.mode = tk.StringVar(value="auto")
        self.start_enabled = tk.BooleanVar(value=False)
        self.interval = tk.StringVar(value=f"{core.CLICK_INTERVAL_SECONDS:g}")
        self.max_clicks = tk.StringVar(value="0")
        self.max_duration_minutes = tk.StringVar(
            value=f"{core.DEFAULT_MAX_RUN_DURATION_SECONDS / 60:g}"
        )
        self.foreground_lock = tk.BooleanVar(value=True)
        self.foreground_title = tk.StringVar(value=core.DEFAULT_FOREGROUND_TITLE)
        self.status = tk.StringVar(value="Sẵn sàng")
        self.help_text = tk.StringVar()

        self.log_handler = QueueLogHandler(self.messages)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        core.LOGGER.setLevel(core.LOG_LEVEL)
        core.LOGGER.propagate = False
        core.LOGGER.addHandler(self.log_handler)

        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_messages)
        self.root.after(250, self._refresh_window_list)

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Auto Red Clicker", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Chọn chức năng rồi bấm Bắt đầu. Auto click mặc định khởi động ở trạng thái TẮT.",
        ).pack(anchor="w", pady=(2, 12))

        mode_frame = ttk.LabelFrame(outer, text="Chức năng", padding=10)
        mode_frame.pack(fill="x")
        ttk.Radiobutton(
            mode_frame,
            text="Auto click (F10 bật/tắt, F12 thoát khẩn cấp)",
            variable=self.mode,
            value="auto",
            command=self._update_mode_controls,
        ).pack(anchor="w")
        ttk.Radiobutton(
            mode_frame,
            text="Dry-run (quét một lần, không di chuyển/click chuột)",
            variable=self.mode,
            value="dry",
            command=self._update_mode_controls,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(
            mode_frame,
            text="Cưa gỗ (nhận diện prompt A-Z và tự nhấn phím)",
            variable=self.mode,
            value="wood",
            command=self._update_mode_controls,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(
            mode_frame,
            text="Đập đá (nhận diện prompt A-Z và tự nhấn phím)",
            variable=self.mode,
            value="stone",
            command=self._update_mode_controls,
        ).pack(anchor="w", pady=(6, 0))
        self.start_enabled_check = ttk.Checkbutton(
            mode_frame,
            text="Bật chức năng — tích là chạy ngay, bỏ tích là dừng",
            variable=self.start_enabled,
            command=self._toggle_auto_click,
        )
        self.start_enabled_check.pack(anchor="w", padx=24, pady=(8, 0))

        settings_frame = ttk.LabelFrame(outer, text="Thông số an toàn", padding=10)
        settings_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(settings_frame, text="Khoảng nghỉ giữa click/nhấn (giây):").grid(
            row=0, column=0, sticky="w"
        )
        self.interval_entry = ttk.Entry(settings_frame, textvariable=self.interval, width=10)
        self.interval_entry.grid(row=0, column=1, sticky="w", padx=(8, 20))
        ttk.Label(settings_frame, text="Giới hạn số click (0 = không giới hạn):").grid(
            row=0, column=2, sticky="w"
        )
        self.max_clicks_entry = ttk.Entry(settings_frame, textvariable=self.max_clicks, width=10)
        self.max_clicks_entry.grid(row=0, column=3, sticky="w", padx=(8, 0))
        self.foreground_lock_check = ttk.Checkbutton(
            settings_frame,
            text="Chỉ chạy khi cửa sổ đang active:",
            variable=self.foreground_lock,
            command=self._update_window_lock_controls,
        )
        self.foreground_lock_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.foreground_title_entry = ttk.Combobox(
            settings_frame, textvariable=self.foreground_title, width=24
        )
        self.foreground_title_entry.grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.refresh_title_button = ttk.Button(
            settings_frame, text="Quét cửa sổ", command=self._refresh_window_list
        )
        self.refresh_title_button.grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0))
        self.apply_title_button = ttk.Button(
            settings_frame, text="Áp dụng", command=self._apply_window_title
        )
        self.apply_title_button.grid(row=2, column=3, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(settings_frame, text="Thời gian tối đa (phút, 0 = không giới hạn):").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self.max_duration_entry = ttk.Entry(
            settings_frame, textvariable=self.max_duration_minutes, width=10
        )
        self.max_duration_entry.grid(row=2, column=2, sticky="w", pady=(8, 0))

        help_frame = ttk.LabelFrame(outer, text="Hướng dẫn sử dụng", padding=10)
        help_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(
            help_frame,
            textvariable=self.help_text,
            justify="left",
            anchor="w",
            wraplength=660,
        ).pack(fill="x")

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=12)
        self.start_button = ttk.Button(controls, text="Bắt đầu", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Dừng", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(controls, textvariable=self.status).pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="Log trạng thái", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=14, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._update_help_text()
        self._update_window_lock_controls()

    def _refresh_window_list(self) -> None:
        """Quét lại cửa sổ Windows và ưu tiên từ khóa GTA5X nếu có."""

        titles = core.list_visible_window_titles()
        if hasattr(self, "foreground_title_entry"):
            self.foreground_title_entry["values"] = titles
        current = self.foreground_title.get().strip()
        preferred = next(
            (title for title in titles if core.DEFAULT_FOREGROUND_TITLE.casefold() in title.casefold()),
            None,
        )
        if preferred and (not current or current == core.DEFAULT_FOREGROUND_TITLE):
            self.foreground_title.set(core.DEFAULT_FOREGROUND_TITLE)
        elif current and current in titles:
            self.foreground_title.set(current)
        self.status.set(f"Đã quét {len(titles)} cửa sổ; chọn title rồi bấm Áp dụng.")
        core.LOGGER.info("Đã quét %d cửa sổ hiển thị.", len(titles))

    def _apply_window_title(self) -> bool:
        title_fragment = self.foreground_title.get().strip()
        if not title_fragment:
            self.status.set("Thiếu mã/title cửa sổ")
            core.LOGGER.error("Mã/title cửa sổ không được để trống khi bật khóa cửa sổ.")
            return False
        self.foreground_title.set(title_fragment)
        self.foreground_lock.set(True)
        self._update_window_lock_controls()
        self.status.set(f"Đã áp dụng cửa sổ: {title_fragment}")
        core.LOGGER.info("Đã áp dụng mã/title cửa sổ: '%s'.", title_fragment)
        return True

    def _update_mode_controls(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if self.mode.get() == "dry":
            self.start_enabled.set(False)
        state = "normal" if self.mode.get() in {"auto", "wood", "stone"} else "disabled"
        self.start_enabled_check.configure(state=state)
        self._update_help_text()
        self._update_window_lock_controls()

    def _update_window_lock_controls(self) -> None:
        # Hai mini-game tự quét nội dung mọi cửa sổ, không cần nhập/ưu tiên title.
        if self.mode.get() in {"wood", "stone"}:
            state = "disabled"
        else:
            state = "normal" if self.foreground_lock.get() else "disabled"
        self.foreground_lock_check.configure(
            state="disabled" if self.mode.get() in {"wood", "stone"} else "normal"
        )
        if hasattr(self, "foreground_title_entry"):
            self.foreground_title_entry.configure(state=state)
        if hasattr(self, "refresh_title_button"):
            self.refresh_title_button.configure(state=state)
        if hasattr(self, "apply_title_button"):
            self.apply_title_button.configure(state=state)

    def _update_help_text(self) -> None:
        if self.mode.get() == "dry":
            self.help_text.set(
                "DRY-RUN — 1) Chọn Dry-run. 2) Bấm Bắt đầu. 3) App chụp vùng 400x400 "
                "và ghi tọa độ mục tiêu màu đỏ vào Log. Chế độ này không di chuyển và không click chuột. "
                "Dùng để kiểm tra trước khi chạy thật."
            )
        elif self.mode.get() == "wood":
            self.help_text.set(
                "CƯA GỖ — 1) Chọn Cưa gỗ. "
                "2) Tích 'Bật chức năng'; app tự quét kỹ tất cả cửa sổ đang hiển thị, "
                "tìm prompt A-Z, đưa đúng cửa sổ lên trước, đếm ngược 3 giây rồi nhấn phím. "
                "và tự nhấn phím cho tới khi prompt biến mất/thanh đầy. "
                "Bỏ tích để dừng; F12 thoát khẩn cấp. Không dùng chế độ này nếu prompt không phải chữ A-Z."
            )
        elif self.mode.get() == "stone":
            self.help_text.set(
                "ĐẬP ĐÁ — 1) Chọn Đập đá. "
                "2) Tích 'Bật chức năng'; app tự quét nội dung các cửa sổ đang hiển thị, "
                "tìm thanh tiến trình và ô phím A-Z (kể cả kiểu ô xanh), đưa đúng cửa sổ lên trước, "
                "đếm ngược 3 giây rồi tự nhấn phím cho tới khi thanh đầy. "
                "Không cần nhập title hay mã tab; không tự tìm đường/NPC. Bỏ tích để dừng; F12 thoát khẩn cấp."
            )
        else:
            self.help_text.set(
                "AUTO CLICK — 1) Chọn Auto click, đặt khoảng nghỉ và giới hạn click. "
                "2) Tích 'Bật chức năng'; app tự quét cửa sổ, đếm ngược 3 giây rồi tự nhận diện/click; bỏ tích là dừng. "
                f"Chỉ chạy khi cửa sổ có title chứa '{self.foreground_title.get()}' active và tự dừng sau 10 phút. "
                "F10 vẫn bật/tắt được; nút Dừng hoặc F12 thoát. Khi không thấy đỏ, app sẽ click tâm vùng quét."
            )

    def _toggle_auto_click(self) -> None:
        """Checkbox chính: bật là khởi động phiên và bắt đầu click ngay."""

        if self.mode.get() not in {"auto", "wood", "stone"}:
            self.start_enabled.set(False)
            return
        if self.start_enabled.get():
            if self.mode.get() in {"wood", "stone"}:
                label = "Cưa gỗ" if self.mode.get() == "wood" else "Đập đá"
                core.LOGGER.info("%s: bật quét nội dung tất cả cửa sổ, không dùng ưu tiên title.", label)
            else:
                self._auto_select_window()
            self._start(force_enable=True)
        else:
            self._stop()

    def _auto_select_window(self) -> None:
        """Tự quét và chọn từ khóa title trước khi bắt đầu macro."""

        titles = core.list_visible_window_titles()
        preferred = next(
            (title for title in titles if core.DEFAULT_FOREGROUND_TITLE.casefold() in title.casefold()),
            None,
        )
        current = self.foreground_title.get().strip()
        if preferred and (not current or current == core.DEFAULT_FOREGROUND_TITLE):
            self.foreground_title.set(core.DEFAULT_FOREGROUND_TITLE)
            self._apply_window_title()
            core.LOGGER.info("Tự chọn cửa sổ theo từ khóa '%s'.", core.DEFAULT_FOREGROUND_TITLE)
        elif current:
            self._apply_window_title()
        else:
            foreground = core.get_foreground_window_title()
            if foreground and "AutoRedClicker" not in foreground:
                self.foreground_title.set(foreground)
                self._apply_window_title()
            else:
                core.LOGGER.warning(
                    "Chưa tự tìm thấy title game; macro sẽ chờ cửa sổ chứa '%s'.",
                    core.DEFAULT_FOREGROUND_TITLE,
                )

    def _start(self, force_enable: bool = False) -> None:
        if self.worker is not None and self.worker.is_alive():
            return

        try:
            interval = float(self.interval.get().strip())
            max_clicks = int(self.max_clicks.get().strip())
            duration_minutes = float(self.max_duration_minutes.get().strip())
            if interval < core.MIN_CLICK_INTERVAL_SECONDS:
                raise ValueError(
                    f"khoảng nghỉ không được nhỏ hơn {core.MIN_CLICK_INTERVAL_SECONDS:g} giây"
                )
            if max_clicks < 0:
                raise ValueError("giới hạn click không được âm")
            if duration_minutes < 0:
                raise ValueError("thời gian tối đa không được âm")
            foreground_title = self.foreground_title.get().strip()
            if (
                self.foreground_lock.get()
                and not foreground_title
                and self.mode.get() not in {"wood", "stone"}
            ):
                raise ValueError("tên cửa sổ cần khóa không được để trống")
        except ValueError as exc:
            self.start_enabled.set(False)
            self.status.set("Thông số không hợp lệ")
            core.LOGGER.error("Không thể bắt đầu: %s.", exc)
            return

        self.stop_requested = threading.Event()
        self.enabled = threading.Event()
        dry_run = self.mode.get() == "dry"
        action_mode = (
            "wood"
            if self.mode.get() == "wood"
            else "stone"
            if self.mode.get() == "stone"
            else "click"
        )
        if not dry_run and (force_enable or self.start_enabled.get()):
            self.enabled.set()

        self.worker = threading.Thread(
            target=self._worker_main,
            args=(
                dry_run,
                self.enabled,
                self.stop_requested,
                interval,
                max_clicks,
                self._queue_emergency,
                duration_minutes * 60,
                self.foreground_lock.get(),
                foreground_title,
                action_mode,
                action_mode in {"wood", "stone"},
            ),
            name="auto-red-clicker-worker",
            daemon=True,
        )
        self.worker.start()
        if dry_run:
            self.status.set("Đang chạy dry-run...")
        elif action_mode in {"wood", "stone"}:
            label = "cưa gỗ" if action_mode == "wood" else "đập đá"
            self.status.set(f"Đang chạy {label}; chờ prompt A-Z...")
        else:
            self.status.set("Đang chạy; chờ F10 hoặc đã bật sẵn")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.start_enabled_check.configure(state="disabled")

    def _worker_main(
        self,
        dry_run: bool,
        enabled: threading.Event,
        stop_requested: threading.Event,
        interval: float,
        max_clicks: int,
        on_emergency: Any,
        max_duration_seconds: float,
        foreground_lock_enabled: bool,
        foreground_title: str,
        action_mode: str,
        auto_find_prompt_window: bool,
    ) -> None:
        result = 1
        try:
            result = core.run_session(
                dry_run=dry_run,
                enabled=enabled,
                stop_requested=stop_requested,
                click_interval_seconds=interval,
                max_clicks=max_clicks,
                on_emergency=on_emergency,
                max_run_duration_seconds=max_duration_seconds,
                foreground_lock_enabled=foreground_lock_enabled,
                foreground_title_contains=foreground_title,
                action_mode=action_mode,
                auto_find_prompt_window=auto_find_prompt_window,
            )
        except KeyboardInterrupt:
            core.LOGGER.info("Đã nhận Ctrl+C; thoát an toàn.")
            result = 0
        except core.AutoClickerError as exc:
            core.LOGGER.error("%s", exc)
        except Exception:
            core.LOGGER.exception("Lỗi không mong đợi trong app.")
        finally:
            self.messages.put(("done", result))

    def _queue_emergency(self) -> None:
        """Đưa tín hiệu F12 từ hotkey thread về thread Tkinter."""

        self.messages.put(("emergency", None))

    def _stop(self) -> None:
        self.start_enabled.set(False)
        if self.stop_requested is not None:
            self.stop_requested.set()
            if self.enabled is not None:
                self.enabled.clear()
            self.status.set("Đang dừng...")

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", f"{value}\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "done":
                    self.status.set("Đã dừng" if value == 0 else "Kết thúc với lỗi; xem log")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self._update_mode_controls()
                elif kind == "emergency":
                    self.status.set("F12: đang thoát khẩn cấp...")
                    self.root.after(0, self._on_close)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_messages)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self._stop()
            self.status.set("Đang dừng trước khi đóng cửa sổ...")
            self.root.after(100, self._close_when_stopped)
            return
        self._close()

    def _close_when_stopped(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self._close_when_stopped)
        else:
            self._close()

    def _close(self) -> None:
        core.LOGGER.removeHandler(self.log_handler)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    AutoRedClickerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
