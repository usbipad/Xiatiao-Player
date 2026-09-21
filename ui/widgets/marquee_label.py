"""跑马灯标签：文本超出可用宽度时，从右往左循环滚动。

用途：左侧播放面板的歌名 / 艺术家名。名字很长时不再截断成省略号，
而是像走马灯一样循环滚动，完整展示内容。

实现要点：
- 两段相同文本首尾相接（中间留固定间隔），滚过一整段后无缝回到起点。
- **水平方向 natural width 恒为 0**，绝不撑宽父容器——
  这正是修复「长艺术家名把左侧面板顶宽、正方形封面被连带顶大」的关键。
- 用 GLib.timeout（约 30fps）驱动，不用 add_tick_callback：
  不可见或无需滚动时立即停表，不空耗 CPU（与 playing_indicator 同一取舍）。
"""
from __future__ import annotations

from typing import Iterable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gsk, Gtk  # noqa: E402


class MarqueeLabel(Gtk.Widget):
    """超长文本自动循环滚动的标签。

    用法::

        lbl = MarqueeLabel("未播放", css_classes=["heading"])
        lbl.set_hexpand(True)      # 占满可用宽度
        lbl.set_text("很长的歌名…")
    """

    __gtype_name__ = "MarqueeLabel"

    #: 滚动速度（像素 / 秒）
    _SPEED = 32.0
    #: 定时器间隔（毫秒），约 30fps
    _INTERVAL_MS = 33
    #: 两段文本之间的间隔（像素）
    _GAP = 56
    #: 开始滚动前的停留（毫秒），让用户先看清开头
    _START_DELAY_MS = 1200

    def __init__(self, text: str = "",
                 css_classes: Iterable[str] | None = None) -> None:
        super().__init__()
        self.set_overflow(Gtk.Overflow.HIDDEN)

        # 两段等宽文本：第一段滚出视野时，第二段正好补上 → 无缝循环
        self._l1 = Gtk.Label(label=text or "", single_line_mode=True)
        self._l2 = Gtk.Label(label=text or "", single_line_mode=True)
        for lbl in (self._l1, self._l2):
            lbl.set_parent(self)
            lbl.set_valign(Gtk.Align.CENTER)
            if css_classes:
                for cls in css_classes:
                    lbl.add_css_class(cls)

        self._offset = 0.0
        self._text_w = 0
        self._scrolling = False
        self._pause_left = self._START_DELAY_MS
        self._timer_id = 0

    # ------------------------------------------------------------------
    # 公共 API（与 Gtk.Label 对齐的常用子集）
    # ------------------------------------------------------------------
    def set_text(self, text: str) -> None:
        """更新文本；从起点重新开始滚动。"""
        text = text or ""
        if text == self._l1.get_text():
            return
        self._l1.set_text(text)
        self._l2.set_text(text)
        self._offset = 0.0
        self._pause_left = self._START_DELAY_MS
        self.queue_allocate()
        self._sync_timer()

    def get_text(self) -> str:
        return self._l1.get_text()

    # ------------------------------------------------------------------
    # GTK 布局
    # ------------------------------------------------------------------
    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            # 关键：minimum 和 natural 都报 0 → 永远不会撑宽父容器。
            # 实际宽度由父容器（hexpand）分配。
            return (0, 0, -1, -1)
        _, nat_h, _, _ = self._l1.measure(Gtk.Orientation.VERTICAL, -1)
        return (0, nat_h, -1, -1)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        _, text_w, _, _ = self._l1.measure(Gtk.Orientation.HORIZONTAL, -1)
        self._text_w = text_w
        self._scrolling = text_w > width

        if not self._scrolling:
            # 放得下：单段居中显示，不滚动
            self._offset = 0.0
            self._place(self._l1, 0.0, width, height, baseline)
            self._place(self._l2, 0.0, 0, 0, baseline)
        else:
            span = text_w + self._GAP
            if self._offset >= span:
                self._offset -= span
            x1 = -self._offset
            self._place(self._l1, x1, text_w, height, baseline)
            self._place(self._l2, x1 + span, text_w, height, baseline)

        self._sync_timer()

    def _place(self, child, x: float, w: int, h: int, baseline: int) -> None:
        """把子控件放到 (x, 0)，尺寸 (w, h)；w/h 为 0 时隐藏。"""
        if w <= 0 or h <= 0:
            child.set_visible(False)
            return
        child.set_visible(True)
        pt = Graphene.Point().init(float(x), 0.0)
        child.allocate(w, h, baseline, Gsk.Transform.new().translate(pt))

    def do_snapshot(self, snapshot) -> None:
        self.snapshot_child(self._l1, snapshot)
        if self._scrolling:
            self.snapshot_child(self._l2, snapshot)

    # ------------------------------------------------------------------
    # 滚动驱动
    # ------------------------------------------------------------------
    def _sync_timer(self) -> None:
        """按需启停定时器：只在「需要滚动 + 已映射 + 可见」时运行。"""
        want = self._scrolling and self.get_mapped() and self.get_visible()
        if want and not self._timer_id:
            self._timer_id = GLib.timeout_add(self._INTERVAL_MS, self._tick)
        elif not want and self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    def _tick(self) -> bool:
        if not (self._scrolling and self.get_mapped() and self.get_visible()):
            self._timer_id = 0
            return False
        if self._pause_left > 0:
            self._pause_left -= self._INTERVAL_MS
            return True
        span = self._text_w + self._GAP
        if span <= 0:
            self._timer_id = 0
            return False
        self._offset += self._SPEED * self._INTERVAL_MS / 1000.0
        if self._offset >= span:
            self._offset -= span
        self.queue_allocate()
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def do_map(self) -> None:
        Gtk.Widget.do_map(self)
        self._sync_timer()

    def do_unmap(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        Gtk.Widget.do_unmap(self)

    def do_dispose(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        for lbl in (self._l1, self._l2):
            if lbl is not None:
                lbl.unparent()
        # 清空引用：widget 若一直存活到解释器关闭阶段，
        # 此时 GTK 已部分拆解，GC 再 finalize 会报
        # “still has children left”。清引用可避免该噪音。
        self._l1 = None
        self._l2 = None
        Gtk.Widget.do_dispose(self)
