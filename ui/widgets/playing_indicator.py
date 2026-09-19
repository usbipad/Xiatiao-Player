"""当前播放指示器：动态跳动的音符频谱柱（轻量实现）。

设计要点（性能优先）：
- 不使用 add_tick_callback（每帧回调、且不可见时也挂着）。
- 使用单个 GLib.timeout（约 15fps），仅在「正在播放」时运行；
  停止时立刻移除定时器，不可见时不消耗 CPU。
- 通过模块级全局键标识「当前播放行」，外部切歌时调用 set_current_key()。
"""
from __future__ import annotations

import math
import random

import cairo
from gi.repository import GLib, Gtk

#: 全局「当前播放行键」；由播放列表页在切歌时设置。
_CURRENT_KEY = ""
#: 存活指示器实例（弱引用），仅用于切歌时快速通知显隐。
import weakref as _weakref
_INSTANCES = _weakref.WeakSet()


def set_current_key(key: str) -> None:
    """设置当前播放曲目的匹配键，并通知所有存活指示器刷新显隐。"""
    global _CURRENT_KEY
    _CURRENT_KEY = key or ""
    for ind in list(_INSTANCES):
        try:
            ind.sync_now()
        except Exception:
            pass


def get_current_key() -> str:
    return _CURRENT_KEY


class NowPlayingIcon(Gtk.Image):
    """当前播放图标：一个醒目的小图标，仅对当前播放行显示。

    与 PlayingIndicator 共用模块级当前键与实例集合；
    切歌时由 set_current_key() 通知显隐。
    """

    def __init__(self, icon_name: str = "media-playback-start-symbolic",
                 size: int = 16, row_key: str = "") -> None:
        super().__init__()
        self.set_from_icon_name(icon_name)
        try:
            self.set_pixel_size(size)
        except Exception:
            pass
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.add_css_class("now-playing-icon")
        self._row_key = row_key or ""
        self.set_visible(False)
        try:
            _INSTANCES.add(self)
        except Exception:
            pass

    def set_row_key(self, key: str) -> None:
        self._row_key = key or ""

    def sync_now(self, *_args) -> None:
        should = bool(_CURRENT_KEY) and self._row_key == _CURRENT_KEY
        try:
            if self.get_visible() != should:
                self.set_visible(should)
        except Exception:
            pass


class PlayingIndicator(Gtk.DrawingArea):
    """跳动音符指示器（当前播放标识）。"""

    #: 竖条数量
    _BARS = 4
    #: 动画帧间隔（毫秒）；约 30fps，流畅且开销低（仅重绘 14px 小图标）
    _FRAME_MS = 33

    def __init__(self, width: int = 14, height: int = 14, row_key: str = "") -> None:
        super().__init__()
        self.set_content_width(width)
        self.set_content_height(height)
        self.set_size_request(width, height)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw, None)
        self._row_key = row_key or ""
        # 每根柱的当前高度（0..1）
        self._vals = [0.3, 0.6, 0.4, 0.7]
        self._phase = [random.random() * math.tau for _ in range(self._BARS)]
        self._speed = [1.6 + random.random() * 1.2 for _ in range(self._BARS)]
        # 动画定时器 id（None = 未运行）
        self._timer = None
        self._rgb = (0.20, 0.55, 0.95)
        self.set_visible(False)
        # 注册到全局集合（切歌时通知）
        try:
            _INSTANCES.add(self)
        except Exception:
            pass

    def set_color(self, r: float, g: float, b: float) -> None:
        self._rgb = (r, g, b)
        self.queue_draw()

    def set_row_key(self, key: str) -> None:
        self._row_key = key or ""

    def sync_now(self, *_args) -> None:
        """按全局当前键决定显隐、动画，并给所在行加/去高亮类。"""
        should = bool(_CURRENT_KEY) and self._row_key == _CURRENT_KEY
        try:
            if self.get_visible() != should:
                self.set_visible(should)
        except Exception:
            pass
        if should:
            self._start_timer()
        else:
            self._stop_timer()
        self._sync_row_highlight(should)

    def _sync_row_highlight(self, on: bool) -> None:
        """给所在行加/去 `effect-selected` 类（与音效弹窗选中样式一致）。

        本指示器位于 ColumnView 单元格内，向上找到行级祖先
        （ColumnViewRow / ListBoxRow / FlowBoxChild）再切换类。
        """
        try:
            row = self._find_row_widget()
            if row is None:
                return
            if on:
                row.add_css_class("effect-selected")
            else:
                row.remove_css_class("effect-selected")
        except Exception:
            pass

    def _find_row_widget(self):
        """向上遍历父链，返回第一个行级控件（或 None）。

        不用 `isinstance(.., Gtk.ColumnViewRow)`：GTK4 的 ColumnViewRow 是
        interface，对 C 实现的 widget 判定不可靠。改用 CSS 节点名识别：
        ColumnView 行 / ListBoxRow / FlowBoxChild 的 css_name 都是 "row"
        （ColumnView 的表头是 "header"，可据此排除）。
        """
        w = self.get_parent()
        while w is not None:
            try:
                if w.get_css_name() == "row":
                    return w
            except Exception:
                pass
            w = w.get_parent()
        return None

    # ---- 动画定时器 ----
    def _start_timer(self) -> None:
        if self._timer is not None:
            return
        self._timer = GLib.timeout_add(self._FRAME_MS, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                GLib.source_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        self.queue_draw()

    def _tick(self) -> bool:
        """更新柱高并重绘。返回 True 继续。"""
        # 平滑趋近目标：避免每帧直接赋值造成生硬跳动
        for i in range(self._BARS):
            self._phase[i] += self._speed[i] * (self._FRAME_MS / 1000.0)
            base = 0.5 + 0.5 * math.sin(self._phase[i])
            jitter = (random.random() - 0.5) * 0.12
            target = max(0.08, min(1.0, base + jitter))
            cur = self._vals[i]
            # 每帧按固定比例趋近目标，形成柔和的上下摆动
            self._vals[i] = cur + (target - cur) * 0.5
        self.queue_draw()
        return True

    def _draw(self, _area, cr, w: int, h: int, _data) -> None:
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        if w <= 0 or h <= 0:
            return
        r, g, b = self._rgb
        n = self._BARS
        gap = max(1.0, w * 0.08)
        bar_w = max(1.5, (w - gap * (n - 1)) / n)
        min_h = h * 0.18
        cr.set_source_rgba(r, g, b, 1.0)
        for i in range(n):
            bh = min_h + (h - min_h) * self._vals[i]
            x = i * (bar_w + gap)
            y = h - bh
            radius = min(bar_w / 2.0, 1.5)
            self._rounded_rect(cr, x, y, bar_w, bh, radius)
            cr.fill()

    @staticmethod
    def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
        if r <= 0:
            cr.rectangle(x, y, w, h)
            return
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
