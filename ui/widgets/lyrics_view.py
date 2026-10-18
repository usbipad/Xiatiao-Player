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
        #: 上一次应用的 (top_m, bottom_m)，避免每帧重复 set_margin。
        self._margin_last = (-1, -1)
        # 用 tick 轮询尺寸：GTK4 的 Overlay 在 vexpand 时 notify::height
        # 不触发，tick 是唯一可靠的时机。尺寸不变时开销可忽略。
        self._fit_tick_id = self.add_tick_callback(self._on_fit_tick)

        # 紧凑模式（左侧面板）：保留固定的面板底色渐隐，让首尾歌词淡出。
        # 沉浸页不设遮罩——背景是动态模糊封面，任何盖上去的色块都会
        # 在背景渐变处露出色差；歌词直接硬切反而干净。
        if compact:
            _fade_h = 30
            self._fade_top = Gtk.Box()
            self._fade_top.add_css_class("np-fade-top")
            self._fade_top.set_halign(Gtk.Align.FILL)
            self._fade_top.set_valign(Gtk.Align.START)
            self._fade_top.set_size_request(-1, _fade_h)
            self._fade_top.set_can_target(False)
            self.add_overlay(self._fade_top)

            self._fade_bottom = Gtk.Box()
            self._fade_bottom.add_css_class("np-fade-bottom")
            self._fade_bottom.set_halign(Gtk.Align.FILL)
            self._fade_bottom.set_valign(Gtk.Align.END)
            self._fade_bottom.set_size_request(-1, _fade_h)
            self._fade_bottom.set_can_target(False)
            self.add_overlay(self._fade_bottom)

        self._empty_label = Gtk.Label(label=_("暂无歌词"))
        self._empty_label.add_css_class("dim-label")
        self._empty_label.add_css_class("title-2")
        # 插在首尾 spacer 之间
        self._lyrics_box.insert_child_after(self._empty_label, self._lyrics_top_spacer)

    def set_fade_colors(self, top_rgb=None, bottom_rgb=None,
                        dark: bool | None = None) -> None:
        """兼容空实现：沉浸页不再使用遮罩。

        保留方法签名，避免调用方改动（now_playing 仍会调）。
        歌词硬切——背景是动态模糊封面，任何盖上去的色块都会露色差。
        """
        return None

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

    def _on_fit_tick(self, _w, _clock) -> bool:
        """轮询：把滚动区上下 margin 调到使可视高度 = 整数行。

        为什么要用 tick 而非 notify::height：实测 GTK4 的 Overlay
        在 vexpand 时尺寸变化不触发 notify::height；tick 每帧跑，
        用 get_height() 差值判断即可（尺寸不变时开销可忽略）。

        原理：
          行占高 unit = 行高 + 间距；
          可见行数 N = floor(可用高 / unit)；
          目标高度 = N × unit - 间距（最后一行后无间距）；
          多余 = 可用高 - 目标高度，上下各分一半作 margin。
        这样滚动视口高度正好是整数行，上下都不会切到歌词。
        """
        try:
            if not self._row_widgets or not self.get_mapped():
                return True
            avail = self.get_height()
            if avail <= 0:
                return True
            ok, r = self._row_widgets[0].compute_bounds(self._lyrics_box)
            if not ok or r.size.height <= 0:
                return True
            row_h = r.size.height
            spacing = 12 if self._compact else 26
            unit = row_h + spacing
            n = int(avail // unit)
            if n < 1:
                n = 1
            target_h = int(n * unit - spacing)
            extra = max(0, int(avail - target_h))
            top_m = extra // 2
            bot_m = extra - top_m
            if (top_m, bot_m) == self._margin_last:
                return True
            self._margin_last = (top_m, bot_m)
            self._lyrics_scroll.set_margin_top(top_m)
            self._lyrics_scroll.set_margin_bottom(bot_m)
        except Exception:
            pass
        return True

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
        # 遍历全部歌词行逐行 add/remove_css_class，长歌词（上百行）时
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
        """滚动到当前行，并吸附到行边界，避免上下露出半行歌词。

        思路：
          1) 让当前行落在视口内一个「完整行区间」的中心；
          2) 把这个目标值再吸附到「某行顶部对齐视口顶部」的最近位置，
             使视口顶部永远从整行开始，不会被切一半。

        注：底部仍可能有半行（视口高度 ÷ 行高不整除），但顶部固定
        完整——视觉上比上下都切干净得多。
        """
        adj = self._lyrics_scroll.get_vadjustment()
        if adj is None:
            return False
        ok, rect = widget.compute_bounds(self._lyrics_box)
        if not ok:
            return False
        page = adj.get_page_size()
        line_top = rect.origin.y
        line_h = rect.size.height
        # 理想位置：当前行居中
        ideal = line_top + line_h / 2 - page / 2
        # 吸附：找最近的行顶部（含 spacer 偏移）。
        snapped = self._snap_to_row_top(ideal)
        target = snapped if snapped is not None else ideal
        target = max(adj.get_lower(), min(target, adj.get_upper() - page))
        self._animate_scroll(adj, target)
        return False

    def _snap_to_row_top(self, ideal: float):
        """把滚动目标吸附到最近的行顶部（使视口顶部对齐整行）。

        行顶部集合 = [每行在 _lyrics_box 内的 y]。用二分找最近的。
        行高不固定（长歌词换行更高），故逐行取 compute_bounds。
        返回吸附后的值；无可用行时返回 None。
        """
        tops: list[float] = []
        for w in self._row_widgets:
            ok, r = w.compute_bounds(self._lyrics_box)
            if ok:
                tops.append(r.origin.y)
        if not tops:
            return None
        # 二分：找 <= ideal 的最后一个（即「当前应停在哪个行顶」）
        i = bisect.bisect_right(tops, ideal) - 1
        if i < 0:
            i = 0
        # 比较 i 与 i+1 哪个更接近 ideal，取近者
        best = tops[i]
        if i + 1 < len(tops) and abs(tops[i + 1] - ideal) < abs(best - ideal):
            best = tops[i + 1]
        return best

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
