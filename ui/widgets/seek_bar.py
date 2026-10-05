"""进度条组件（自绘）：点击轨道 seek、拖动滑块 seek、外部位置跟随。

用 Gtk.DrawingArea 自绘而非 Gtk.Scale —— 因为 GTK 主题（如 Adwaita）
会给 Scale 的已播段强加蓝色，CSS 无法覆盖。自绘后颜色完全由代码控制。

交互（Gtk.GestureDrag，CAPTURE 阶段先于父级滚动容器）：
- drag-begin：记录按下位置，视觉跟手移动。
- drag-update：按位移实时移动（跟手），不 seek。
- drag-end：松手时 seek 一次。

接口与旧 SeekBar 兼容：set_duration / set_position / reset / on_seek；
另加 on_drag（拖动中回调，替代 Gtk.Scale 的 change-value 用于同步时间标签）。
"""
from __future__ import annotations

from typing import Callable

from gi.repository import GLib, Gtk


#: 颜色（0..1）——浅背景版：已播段半透明深灰（近黑），不用白/蓝
TRACK_RGBA = (0.0, 0.0, 0.0, 0.12)      # 未播轨道：极淡灰
PLAYED_RGBA = (0.0, 0.0, 0.0, 0.55)     # 已播段：半透明深灰
THUMB_RGBA = (0.0, 0.0, 0.0, 0.55)      # 滑块：同色小圆（与已播段融合）
THUMB_SHADOW_RGBA = (0.0, 0.0, 0.0, 0.0)  # 无阴影（Apple 进度条干净）

#: 暗背景版：用白色系，保证在深色背景上可见
TRACK_RGBA_DARK = (1.0, 1.0, 1.0, 0.25)   # 未播轨道：半透明白
PLAYED_RGBA_DARK = (1.0, 1.0, 1.0, 0.85)  # 已播段：白色
THUMB_RGBA_DARK = (1.0, 1.0, 1.0, 0.95)   # 滑块：白色

BAR_H = 5          # 轨道高度
THUMB_R = 7        # 滑块半径


def fmt_seconds(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


class SeekBar(Gtk.DrawingArea):
    """带 seek 交互的横向进度条（自绘）。"""

    def __init__(self, on_seek: Callable[[float], None] | None = None,
                 on_drag: Callable[[float], None] | None = None) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_content_height(24)
        self.set_draw_func(self._draw, None)

        self._on_seek = on_seek
        self._on_drag = on_drag
        self._value = 0.0          # 当前值（秒）
        self._duration = 0.0
        self._seeking = False
        #: 是否处于「拖动中」（drag-begin 到 drag-end 之间）。
        #: 用于让 _freeze_after_seek 的定时解冻不打断正在进行的拖动。
        self._dragging = False
        self._start_x = 0.0
        # seek 后冻结倒计时句柄：期间忽略外部位置更新，避免进度条闪回
        self._seek_freeze_id = None
        # 已播段颜色（r,g,b 0..1）；None 回退默认深灰
        self._played_rgb = None
        # 暗背景模式：True 时用白色系绘制
        self._dark = False

        drag = Gtk.GestureDrag()
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    # ---- 外部接口（与旧版兼容）----
    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self.queue_draw()

    def reset(self) -> None:
        self._seeking = False
        self._value = 0.0
        self.queue_draw()

    def freeze(self, ms: int = 400) -> None:
        """外部冻结位置跟随 ms 毫秒：期间忽略 set_position。

        用于切歌 / 切音质：旧曲目残留的 position 事件（异步派发、
        可能在路上）会到达并把进度条拉回旧值；冻结可忽略这些过期
        事件，等新曲目的稳定位置到来再解冻。

        注意：本方法不改动当前值——是否清零由调用方决定（切歌走
        reset()+freeze()，切音质走 set_value(当前)+freeze()）。
        """
        self._freeze_after_seek(ms)

    def set_position(self, seconds: float) -> None:
        """外部（播放器）更新位置：交互期间不覆盖用户操作。"""
        if not self._seeking:
            self._value = max(0.0, float(seconds))
            self.queue_draw()

    def get_value(self) -> float:
        return self._value

    def set_value(self, seconds: float) -> None:
        self._value = max(0.0, min(self._duration, float(seconds)))
        self.queue_draw()

    def set_played_color(self, rgb) -> None:
        """设置已播段颜色（r,g,b ∈ 0..1）；None 回退默认深灰。"""
        self._played_rgb = rgb
        self.queue_draw()

    def set_dark(self, dark: bool) -> None:
        """切换暗背景模式：暗时轨道/已播段/滑块改用白色系，保证可见。"""
        self._dark = bool(dark)
        self.queue_draw()

    # ---- 交互 ----
    def _value_for_x(self, x: float) -> float:
        width = self.get_width()
        if width <= 0 or self._duration <= 0:
            return self._value
        ratio = max(0.0, min(1.0, x / width))
        return ratio * self._duration

    def _on_drag_begin(self, gesture, start_x, _start_y) -> None:
        self._seeking = True
        self._dragging = True
        self._start_x = start_x
        self._value = self._value_for_x(start_x)
        self.queue_draw()
        if self._on_drag is not None:
            self._on_drag(self._value)
        try:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except Exception:
            pass

    def _on_drag_update(self, _gesture, offset_x, _offset_y) -> None:
        # 关键修复：拖动过程中**持续**确保处于 seek 态。
        #
        # 真凶（由实机日志定位）：拖动期间若有一个「上次 seek/切歌」遗留的
        # _freeze_after_seek 定时器（1.5s）到期，其 _unfreeze 会把 _seeking
        # 置回 False；此后 position-update 到达时 set_position 的守卫失效，
        # 把进度条拉回实际播放位置，下一帧 drag-update 又跳回——表现为
        # 「一弹一弹」（200ms 一次，随 position 事件频率）。
        # 这里每次拖动更新都重申 _seeking=True，且 _unfreeze 感知拖动（见下）。
        self._seeking = True
        current_x = self._start_x + offset_x
        self._value = self._value_for_x(current_x)
        self.queue_draw()
        if self._on_drag is not None:
            self._on_drag(self._value)

    def _on_drag_end(self, _gesture, offset_x, _offset_y) -> None:
        current_x = self._start_x + offset_x
        self._value = self._value_for_x(current_x)
        self.queue_draw()
        self._dragging = False
        if self._on_seek is not None:
            self._on_seek(self._value)
        # seek 是异步的：保持“冻结”一小段时间，期间忽略外部位置更新，
        # 否则下一帧轮询会用 seek 前的旧位置把进度条拉回（视觉闪一下）。
        self._freeze_after_seek()

    def _freeze_after_seek(self, ms: int = 1500) -> None:
        """seek 后冻结位置跟随 ms 毫秒，之后恢复。"""
        self._seeking = True
        if self._seek_freeze_id is not None:
            try:
                GLib.source_remove(self._seek_freeze_id)
            except Exception:
                pass
            self._seek_freeze_id = None

        def _unfreeze():
            self._seek_freeze_id = None
            # 若用户正在拖动（drag-begin 已触发、drag-end 未到），不要解冻——
            # 否则会打断拖动，导致 position 把进度条拉回（「一弹一弹」）。
            # 拖动的解冻由 drag-end 的 _freeze_after_seek 负责。
            if self._dragging:
                return False
            self._seeking = False
            return False

        try:
            self._seek_freeze_id = GLib.timeout_add(ms, _unfreeze)
        except Exception:
            self._seeking = False

    # ---- 绘制 ----
    def _draw(self, _area, cr, w: int, h: int, _data) -> None:
        if w <= 0:
            return
        ratio = 0.0
        if self._duration > 0:
            ratio = max(0.0, min(1.0, self._value / self._duration))
        cy = h / 2.0
        x0 = THUMB_R
        x1 = w - THUMB_R
        track_w = max(0.0, x1 - x0)

        # 轨道（未播）：暗背景用半透明白，浅背景用淡灰
        track_rgba = TRACK_RGBA_DARK if self._dark else TRACK_RGBA
        cr.set_source_rgba(*track_rgba)
        self._rounded_bar(cr, x0, cy, track_w)
        cr.fill()

        # 已播段颜色：优先使用封面主色（保证与封面色调统一）；仅未设封面色时按暗/明背景回退默认色
        if self._played_rgb is not None:
            pr, pg, pb = self._played_rgb
            played_rgba = (pr, pg, pb, 0.95 if self._dark else 0.85)
            thumb_rgba = (min(1.0, pr * 1.15), min(1.0, pg * 1.15), min(1.0, pb * 1.15), 1.0)
        elif self._dark:
            played_rgba = PLAYED_RGBA_DARK
            thumb_rgba = THUMB_RGBA_DARK
        else:
            played_rgba = PLAYED_RGBA
            thumb_rgba = THUMB_RGBA

        # 已播段长度
        played_len = track_w * ratio
        tx = x0 + played_len

        # 已播段（干净圆角条，无光晕）
        cr.set_source_rgba(*played_rgba)
        self._rounded_bar(cr, x0, cy, played_len)
        cr.fill()

        # 滑块：纯色小圆 + 极淡柔光（一层放大的半透明圆，非硬描边）
        cr.set_source_rgba(thumb_rgba[0], thumb_rgba[1], thumb_rgba[2], 0.18)
        cr.arc(tx, cy, THUMB_R + 2, 0, 2 * 3.141592653589793)
        cr.fill()
        cr.set_source_rgba(*thumb_rgba)
        cr.arc(tx, cy, THUMB_R, 0, 2 * 3.141592653589793)
        cr.fill()

    @staticmethod
    def _rounded_bar(cr, x: float, cy: float, length: float,
                     height: float | None = None) -> None:
        """圆角横条路径（length<=0 不画）。height 默认用 BAR_H。"""
        if length <= 0.5:
            cr.new_path()
            return
        h = float(height) if height is not None else float(BAR_H)
        r = h / 2.0
        cr.new_path()
        cr.move_to(x + r, cy - r)
        cr.line_to(x + length - r, cy - r)
        cr.arc(x + length - r, cy, r, -3.141592653589793 / 2, 3.141592653589793 / 2)
        cr.line_to(x + r, cy + r)
        cr.arc(x + r, cy, r, 3.141592653589793 / 2, 3 * 3.141592653589793 / 2)
        cr.close_path()
