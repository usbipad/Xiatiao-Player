"""歌词视图：滚动歌词、当前行高亮、点击跳转、平滑缓动、首尾渐隐。

从 now_playing 抽出，外观与交互保持原样。
"""
from __future__ import annotations

import bisect
from typing import Callable

from gi.repository import Gdk, GLib, Gtk

from core.i18n import _


class LyricsView(Gtk.Overlay):
    """歌词区（含顶/底渐隐遮罩）。

    on_seek(seconds) 在点击歌词行时回调。
    """

    def __init__(self, on_seek: Callable[[float], None] | None = None,
                 compact: bool = False, card: bool = False) -> None:
        super().__init__()
        self._on_seek = on_seek
        self._compact = compact
        if card:
            self.add_css_class("np-lyrics-card")
        self._lyrics: list[tuple[float, str]] = []
        self._times: list[float] = []
        self._current_line = -1
        self._row_widgets: list[Gtk.Label] = []
        self._scroll_anim_id = 0

        if compact:
            self.add_css_class("np-lyrics-compact")

        # 紧凑模式（左侧窄面板）行距更小、首尾 spacer 更矮
        spacing = 12 if compact else 26
        spacer_h = 60 if compact else 200
        self._lyrics_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        self._lyrics_box.set_valign(Gtk.Align.CENTER)

        # 首尾 spacer：让第一句/最后一句也能滚到视图中间
        self._lyrics_top_spacer = Gtk.Box()
        self._lyrics_top_spacer.set_size_request(-1, spacer_h)
        self._lyrics_bottom_spacer = Gtk.Box()
        self._lyrics_bottom_spacer.set_size_request(-1, spacer_h)
        self._lyrics_box.append(self._lyrics_top_spacer)
        self._lyrics_box.append(self._lyrics_bottom_spacer)

        self._lyrics_scroll = Gtk.ScrolledWindow()
        self._lyrics_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # 隐藏滚动条：歌词自动滚动，右侧竖条纯属干扰（见 style.css）
        self._lyrics_scroll.add_css_class("np-lyrics-scroll")
        self._lyrics_scroll.set_child(self._lyrics_box)
        self._lyrics_scroll.set_hexpand(True)
        self._lyrics_scroll.set_vexpand(True)

        self.set_child(self._lyrics_scroll)
        self.set_hexpand(True)
        self.set_vexpand(True)

        # 顶/底渐隐遮罩（覆盖在滚动区上下，让首尾歌词淡出）
        self._fade_top = Gtk.Box()
        self._fade_top.add_css_class("np-fade-top")
        self._fade_top.set_halign(Gtk.Align.FILL)
        self._fade_top.set_valign(Gtk.Align.START)
        self._fade_top.set_size_request(-1, 80)
        self._fade_top.set_can_target(False)  # 不挡点击
        self.add_overlay(self._fade_top)

        self._fade_bottom = Gtk.Box()
        self._fade_bottom.add_css_class("np-fade-bottom")
        self._fade_bottom.set_halign(Gtk.Align.FILL)
        self._fade_bottom.set_valign(Gtk.Align.END)
        self._fade_bottom.set_size_request(-1, 80)
        self._fade_bottom.set_can_target(False)
        self.add_overlay(self._fade_bottom)

        self._empty_label = Gtk.Label(label=_("暂无歌词"))
        self._empty_label.add_css_class("dim-label")
        self._empty_label.add_css_class("title-2")
        # 插在首尾 spacer 之间
        self._lyrics_box.insert_child_after(self._empty_label, self._lyrics_top_spacer)

    # ---- 外部接口 ----
    def set_lyrics(self, lyrics: list[tuple[float, str]]) -> None:
        self._lyrics = lyrics
        self._times = [t for t, _ in lyrics]
        self._current_line = -1
        self._hl_idx = -1
        self._row_widgets.clear()

        # 只删除歌词行，保留首尾 spacer 和 empty_label
        child = self._lyrics_top_spacer.get_next_sibling()
        while child is not None and child is not self._lyrics_bottom_spacer:
            nxt = child.get_next_sibling()
            if child is not self._empty_label:
                self._lyrics_box.remove(child)
            child = nxt

        if not lyrics:
            self._lyrics_box.set_valign(Gtk.Align.CENTER)
            self._empty_label.set_visible(True)
            return

        self._empty_label.set_visible(False)
        self._lyrics_box.set_valign(Gtk.Align.START)
        for t, text in lyrics:
            label = Gtk.Label(label=text)
            label.set_wrap(True)
            label.set_justify(Gtk.Justification.CENTER)
            label.set_xalign(0.5)
            label.add_css_class("lyric-line")
            if self._on_seek is not None:
                label.set_can_target(True)
                gesture = Gtk.GestureClick()
                gesture.connect("released", lambda _g, _n, _x, _y, tt=t: self._on_seek(tt))
                label.add_controller(gesture)
                label.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
            self._lyrics_box.insert_child_after(
                label,
                self._row_widgets[-1] if self._row_widgets else self._lyrics_top_spacer,
            )
            self._row_widgets.append(label)

    def set_position(self, seconds: float) -> None:
        """根据播放位置更新当前高亮行。"""
        if not self._times:
            return
        idx = bisect.bisect_right(self._times, seconds) - 1
        if idx < 0:
            idx = 0
        if idx == self._current_line:
            return
        self._current_line = idx
        self._apply_highlight(idx)

    # ---- 内部 ----
    def _apply_highlight(self, idx: int) -> None:
        # 只改「上一行」和「当前行」两个控件。
        # 此前遍历全部歌词行逐行 add/remove_css_class，长歌词（上百行）时
        # 每次切行都会产生大量样式失效；而该方法在进入沉浸页的
        # set_position 链路上也会被调用，直接拖慢首帧。
        prev = getattr(self, "_hl_idx", -1)
        if prev == idx:
            return
        self._hl_idx = idx
        try:
            if 0 <= prev < len(self._row_widgets):
                self._row_widgets[prev].remove_css_class("lyric-active")
        except Exception:
            pass
        if 0 <= idx < len(self._row_widgets):
            self._row_widgets[idx].add_css_class("lyric-active")
            GLib.idle_add(self._scroll_to, self._row_widgets[idx])

    def _scroll_to(self, widget: Gtk.Widget) -> bool:
        adj = self._lyrics_scroll.get_vadjustment()
        if adj is None:
            return False
        ok, rect = widget.compute_bounds(self._lyrics_box)
        if not ok:
            return False
        line_top = rect.origin.y
        line_h = rect.size.height
        page = adj.get_page_size()
        target = line_top + line_h / 2 - page / 2
        target = max(adj.get_lower(), min(target, adj.get_upper() - page))
        self._animate_scroll(adj, target)
        return False

    def _animate_scroll(self, adj, target: float) -> None:
        """缓动滚动到目标值（用 frame clock 驱动，平滑且跟随刷新率）。"""
        start = adj.get_value()
        if abs(target - start) < 1.0:
            adj.set_value(target)
            return
        if self._scroll_anim_id:
            self.remove_tick_callback(self._scroll_anim_id)
            self._scroll_anim_id = 0

        duration_us = 420_000  # 420ms
        state = {"t0": None}
        start_val = start

        def tick(_widget, clock) -> bool:
            now = clock.get_frame_time()
            if state["t0"] is None:
                state["t0"] = now
            p = min(1.0, (now - state["t0"]) / duration_us)
            eased = 1 - (1 - p) ** 5
            adj.set_value(start_val + (target - start_val) * eased)
            if p >= 1.0:
                self._scroll_anim_id = 0
                return False
            return True

        self._scroll_anim_id = self.add_tick_callback(tick)
