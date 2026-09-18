"""启动闪屏控制器（Splash）。

从 window.py 抽出，职责单一：管理启动闪屏的显示、就绪检测与淡出。
通过依赖注入与主窗口解耦：
- overlay_provider：返回承载 splash 的 Gtk.Overlay；
- has_library_to_load：是否配置了音乐目录（决定是否值得等封面）；
- is_library_loaded：曲库是否已就绪（缓存已填 / 扫描完成）；
- is_cover_busy：首屏封面是否仍在加载。

用法：
    ctrl = SplashController(
        overlay_provider=lambda: self._root_overlay,
        has_library_to_load=...,
        is_library_loaded=...,
        is_cover_busy=...,
    )
    ctrl.build()          # 构建并返回 splash 控件（调用方 add_overlay）
    ctrl.on_window_mapped()   # 窗口首次显示后调用
    ctrl.request_fade()       # 数据就绪时调用（如扫描完成）
    ctrl.on_cover_activity_changed(busy)  # 封面忙闲变化时调用
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

from gi.repository import Adw, GLib, Gtk

from core.i18n import _

log = logging.getLogger(__name__)


class SplashController:
    """启动闪屏控制器。"""

    MIN_MS = 400        # 最短显示时长（避免一闪而过）
    FADE_MS = 400       # 淡出时长
    MAX_MS = 5000       # 兜底：最长等待，超时强制淡出
    COVER_POLL_MS = 200  # 轮询封面是否空闲的间隔
    COVER_IDLE_TICKS = 3  # 连续多少次空闲才认为稳定（3×200=600ms 静默）

    def __init__(self,
                 overlay_provider: Callable[[], Optional[Gtk.Overlay]],
                 has_library_to_load: Callable[[], bool],
                 is_library_loaded: Callable[[], bool],
                 is_cover_busy: Callable[[], bool]) -> None:
        self._overlay_provider = overlay_provider
        self._has_library_to_load = has_library_to_load
        self._is_library_loaded = is_library_loaded
        self._is_cover_busy = is_cover_busy

        self._widget: Optional[Gtk.Widget] = None
        self._t0 = time.monotonic()
        self._fade_id = 0
        self._timeout_id = 0
        self._want_fade = False
        self._idle_ticks = 0

    # ------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------
    def build(self) -> Gtk.Widget:
        """构建闪屏控件（不自动加入 overlay，由调用方 add_overlay）。"""
        self._t0 = time.monotonic()
        self._fade_id = 0
        self._timeout_id = 0
        self._want_fade = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        # logo：优先用项目根目录的 image.png，其次内置 256px 图标
        logo = Gtk.Image()
        logo.set_pixel_size(128)
        logo.add_css_class("splash-logo")
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            candidates = [
                os.path.join(project_root, "image.png"),
                os.path.join(project_root, "data", "icons", "xiatiao-256.png"),
            ]
            logo_path = next((p for p in candidates if os.path.isfile(p)), None)
            if logo_path:
                logo.set_from_file(logo_path)
            else:
                logo.set_from_icon_name("xiatiao")
        except Exception:
            logo.set_from_icon_name("xiatiao")
        box.append(logo)

        title = Gtk.Label(label="Xiatiao")
        title.add_css_class("splash-title")
        box.append(title)

        subtitle = Gtk.Label(label=_("虾条播放器"))
        subtitle.add_css_class("splash-subtitle")
        box.append(subtitle)

        spinner = Adw.Spinner()
        spinner.set_size_request(24, 24)
        spinner.set_halign(Gtk.Align.CENTER)
        box.append(spinner)

        overlay = Gtk.Box()
        overlay.add_css_class("splash-overlay")
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.append(box)
        self._widget = overlay
        return overlay

    @property
    def widget(self) -> Optional[Gtk.Widget]:
        return self._widget

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    def on_window_mapped(self) -> None:
        """窗口首次 map（显示）后决定何时淡出。"""
        if not self._safe(self._has_library_to_load):
            # 没有内容可等：直接按最短显示时长淡出
            self._want_fade = True
            delay = self._delay_to_min()
            self._fade_id = GLib.timeout_add(delay, self._start_fade)
            return
        # 就绪条件可能已满足，先检查一次
        self.mark_ready_if_loaded()
        # 兜底超时
        if not self._timeout_id:
            self._timeout_id = GLib.timeout_add(self.MAX_MS, self._on_timeout)

    def mark_ready_if_loaded(self) -> None:
        """若首屏数据已就绪，请求淡出。"""
        if self._safe(self._is_library_loaded):
            self.request_fade()

    def request_fade(self) -> None:
        """请求淡出：进入「等首屏封面加载完成」阶段。"""
        if self._widget is None:
            return
        if self._want_fade:
            return
        self._want_fade = True
        self._idle_ticks = 0
        if self._fade_id:
            return
        self._fade_id = GLib.timeout_add(self.COVER_POLL_MS, self._poll_ready)

    def on_cover_activity_changed(self, busy: bool) -> None:
        """封面加载忙闲变化：变空闲时立即检查一次，加快淡出。"""
        if busy:
            return
        if self._widget is None:
            return
        if not self._want_fade:
            return
        if not self._fade_id:
            self._idle_ticks = 0
            self._fade_id = GLib.timeout_add(self.COVER_POLL_MS, self._poll_ready)

    def cancel(self) -> None:
        """取消所有定时器（窗口关闭时调用）。"""
        for attr in ("_fade_id", "_timeout_id"):
            tid = getattr(self, attr, 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                setattr(self, attr, 0)

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------
    def _on_timeout(self) -> bool:
        """兜底超时：强制淡出。"""
        self._timeout_id = 0
        self._want_fade = True
        if self._fade_id:
            return False
        delay = self._delay_to_min()
        self._fade_id = GLib.timeout_add(delay, self._start_fade)
        return False

    def _poll_ready(self) -> bool:
        """轮询首屏是否稳定：封面连续空闲 COVER_IDLE_TICKS 次就淡出。"""
        if self._widget is None:
            return False
        if self._safe(self._is_cover_busy):
            self._idle_ticks = 0
            return True
        self._idle_ticks += 1
        if self._idle_ticks < self.COVER_IDLE_TICKS:
            return True
        # 稳定：满足最短显示时长后淡出
        self._fade_id = 0
        delay = self._delay_to_min()
        if delay > 0:
            self._fade_id = GLib.timeout_add(delay, self._start_fade)
        else:
            self._start_fade()
        return False

    def _start_fade(self) -> bool:
        """开始淡出；结束后移除。"""
        self._fade_id = 0
        splash = self._widget
        if splash is None:
            return False
        try:
            anim = Adw.TimedAnimation.new(
                splash, 1.0, 0.0, self.FADE_MS,
                Adw.PropertyAnimationTarget.new(splash, "opacity"),
            )
            anim.connect("done", self._on_faded)
            anim.play()
        except Exception:
            log.debug("启动闪屏淡出失败，直接移除", exc_info=True)
            self._on_faded(None)
        return False

    def _on_faded(self, *_args) -> None:
        """淡出完成：从 overlay 移除 splash。"""
        try:
            if self._timeout_id:
                GLib.source_remove(self._timeout_id)
                self._timeout_id = 0
        except Exception:
            pass
        splash = self._widget
        overlay = self._safe_get(self._overlay_provider)
        if splash is not None and overlay is not None:
            try:
                overlay.remove_overlay(splash)
            except Exception:
                log.debug("移除启动闪屏失败", exc_info=True)
        self._widget = None

    def _delay_to_min(self) -> int:
        """距最短显示时长的剩余毫秒（>=0）。"""
        elapsed_ms = (time.monotonic() - self._t0) * 1000.0
        return max(0, int(self.MIN_MS - elapsed_ms))

    @staticmethod
    def _safe(fn: Callable[[], bool]) -> bool:
        try:
            return bool(fn())
        except Exception:
            return False

    @staticmethod
    def _safe_get(fn: Callable[[], object]):
        try:
            return fn()
        except Exception:
            return None
