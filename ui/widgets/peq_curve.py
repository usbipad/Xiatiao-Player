"""参数均衡器（PEQ）可拖曲线控件。

基于 Gtk.DrawingArea：
- 绘制频响曲线 + 频段控制点；
- 拖动控制点：水平=频率，垂直=增益；
- 滚轮/Shift滚轮：调 Q。

频响曲线用每个频段的 Biquad 复频响应相乘计算。
"""
from __future__ import annotations

import math
from typing import Callable, List, Optional

from gi.repository import Gtk

#: 频率范围
F_MIN = 20.0
F_MAX = 20000.0
#: 增益范围（dB）
G_MIN = -24.0
G_MAX = 24.0


def _freq_to_x(f: float, w: float) -> float:
    """频率 → 屏幕 x（对数映射）。"""
    f = max(F_MIN, min(F_MAX, f))
    lr = math.log10(f / F_MIN) / math.log10(F_MAX / F_MIN)
    return lr * w


def _x_to_freq(x: float, w: float) -> float:
    """屏幕 x → 频率。"""
    if w <= 0:
        return F_MIN
    lr = max(0.0, min(1.0, x / w))
    return F_MIN * (F_MAX / F_MIN) ** lr


def _gain_to_y(g: float, h: float) -> float:
    """增益 → 屏幕 y（0 在顶部）。"""
    g = max(G_MIN, min(G_MAX, g))
    return (G_MAX - g) / (G_MAX - G_MIN) * h


def _y_to_gain(y: float, h: float) -> float:
    if h <= 0:
        return 0.0
    return G_MAX - (y / h) * (G_MAX - G_MIN)


class PeqCurve(Gtk.DrawingArea):
    """PEQ 频响曲线 + 可拖控制点。"""

    def __init__(self, on_changed: Optional[Callable[[List[dict]], None]] = None) -> None:
        super().__init__()
        self.set_content_width(520)
        self.set_content_height(220)
        self.set_draw_func(self._draw, None)
        self._bands: List[dict] = []
        self._sample_rate = 48000.0
        self._on_changed = on_changed
        self._drag_idx = -1
        self._drag_mode = "freq_gain"

        # 拖拽手势
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        # 滚轮调 Q
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        # 点击选择（用于滚轮调 Q）
        self._selected = -1

    # ---- 外部接口 ----
    def set_bands(self, bands: List[dict]) -> None:
        self._bands = [dict(b) for b in (bands or [])]
        self.queue_draw()

    def set_sample_rate(self, rate: float) -> None:
        self._sample_rate = max(1000.0, float(rate))
        self.queue_draw()

    # ---- 频响计算 ----
    def _band_response_db(self, band: dict, freqs) -> List[float]:
        """计算单个频段的频响（dB）。"""
        kind = band.get("kind", "pk")
        gain = float(band.get("gain", 0.0))
        q = max(0.1, float(band.get("q", 1.0)))
        f0 = max(1.0, float(band.get("freq", 1000.0)))
        sr = self._sample_rate
        out = []
        for f in freqs:
            w0 = 2.0 * math.pi * f0 / sr
            w = 2.0 * math.pi * f / sr
            if kind == "ls":
                # 低架（近似：用 gain 的全通-低通混合）
                a = 10 ** (gain / 40.0)
                cw = math.cos(w0)
                sw = math.sin(w0)
                alpha = sw / 2.0 * math.sqrt(2.0)
                b0 = a * ((a + 1) - (a - 1) * cw + 2 * math.sqrt(a) * alpha)
                b1 = 2 * a * ((a - 1) - (a + 1) * cw)
                b2 = a * ((a + 1) - (a - 1) * cw - 2 * math.sqrt(a) * alpha)
                a0 = (a + 1) + (a - 1) * cw + 2 * math.sqrt(a) * alpha
                a1 = -2 * ((a - 1) + (a + 1) * cw)
                a2 = (a + 1) + (a - 1) * cw - 2 * math.sqrt(a) * alpha
            elif kind == "hs":
                a = 10 ** (gain / 40.0)
                cw = math.cos(w0)
                sw = math.sin(w0)
                alpha = sw / 2.0 * math.sqrt(2.0)
                b0 = a * ((a + 1) + (a - 1) * cw + 2 * math.sqrt(a) * alpha)
                b1 = -2 * a * ((a - 1) + (a + 1) * cw)
                b2 = a * ((a + 1) + (a - 1) * cw - 2 * math.sqrt(a) * alpha)
                a0 = (a + 1) - (a - 1) * cw + 2 * math.sqrt(a) * alpha
                a1 = 2 * ((a - 1) - (a + 1) * cw)
                a2 = (a + 1) - (a - 1) * cw - 2 * math.sqrt(a) * alpha
            else:  # pk
                a = 10 ** (gain / 40.0)
                cw = math.cos(w0)
                sw = math.sin(w0)
                alpha = sw / (2.0 * q)
                b0 = 1 + alpha * a
                b1 = -2 * cw
                b2 = 1 - alpha * a
                a0 = 1 + alpha / a
                a1 = -2 * cw
                a2 = 1 - alpha / a
            # 复频响应 H(e^jw) = (b0 + b1 z^-1 + b2 z^-2)/(a0 + a1 z^-1 + a2 z^-2)
            z1 = complex(math.cos(-w), math.sin(-w))
            z2 = complex(math.cos(-2 * w), math.sin(-2 * w))
            num = b0 + b1 * z1 + b2 * z2
            den = a0 + a1 * z1 + a2 * z2
            h = num / den if den != 0 else 1.0
            mag = abs(h)
            out.append(20.0 * math.log10(mag) if mag > 1e-9 else -120.0)
        return out

    def _total_response_db(self, freqs) -> List[float]:
        """所有频段叠加的频响（dB）。"""
        total = [0.0] * len(freqs)
        for band in self._bands:
            resp = self._band_response_db(band, freqs)
            for i, v in enumerate(resp):
                total[i] += v
        return total

    # ---- 绘制 ----
    def _draw(self, area, cr, w, h, _data) -> None:
        # 背景
        cr.set_source_rgb(0.12, 0.12, 0.14)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        # 网格（频率）
        cr.set_line_width(1)
        cr.set_source_rgba(0.3, 0.3, 0.35, 0.5)
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = _freq_to_x(f, w)
            cr.move_to(x, 0)
            cr.line_to(x, h)
            cr.stroke()
        # 网格（增益）
        for g in (-18, -12, -6, 0, 6, 12, 18):
            y = _gain_to_y(g, h)
            cr.move_to(0, y)
            cr.line_to(w, y)
            cr.stroke()
        # 0dB 线加粗
        cr.set_source_rgba(0.5, 0.5, 0.55, 0.8)
        y0 = _gain_to_y(0.0, h)
        cr.move_to(0, y0)
        cr.line_to(w, y0)
        cr.stroke()

        # 频响曲线
        n = 200
        freqs = [F_MIN * (F_MAX / F_MIN) ** (i / (n - 1)) for i in range(n)]
        resp = self._total_response_db(freqs)
        cr.set_source_rgba(0.4, 0.7, 1.0, 0.9)
        cr.set_line_width(2)
        for i, f in enumerate(freqs):
            x = _freq_to_x(f, w)
            y = _gain_to_y(resp[i], h)
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()

        # 控制点
        for idx, band in enumerate(self._bands):
            f = float(band.get("freq", 1000.0))
            g = float(band.get("gain", 0.0))
            x = _freq_to_x(f, w)
            y = _gain_to_y(g, h)
            cr.set_source_rgba(1.0, 0.6, 0.2, 0.95)
            cr.arc(x, y, 7, 0, 2 * math.pi)
            cr.fill()
            if idx == self._selected or idx == self._drag_idx:
                cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
                cr.set_line_width(2)
                cr.arc(x, y, 9, 0, 2 * math.pi)
                cr.stroke()

    # ---- 交互 ----
    def _hit_test(self, x: float, y: float, w: float, h: float) -> int:
        """返回命中的控制点索引（-1=无）。"""
        best = -1
        best_d = 16.0
        for idx, band in enumerate(self._bands):
            bx = _freq_to_x(float(band.get("freq", 1000.0)), w)
            by = _gain_to_y(float(band.get("gain", 0.0)), h)
            d = math.hypot(x - bx, y - by)
            if d < best_d:
                best_d = d
                best = idx
        return best

    def _on_drag_begin(self, gesture, sx, sy) -> None:
        w = self.get_width()
        h = self.get_height()
        idx = self._hit_test(sx, sy, w, h)
        self._drag_idx = idx
        self._selected = idx
        if idx >= 0:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.queue_draw()

    def _on_drag_update(self, gesture, ox, oy) -> None:
        idx = self._drag_idx
        if idx < 0 or idx >= len(self._bands):
            return
        w = self.get_width()
        h = self.get_height()
        start = gesture.get_start_point()
        # GTK4 返回 (found, x, y)
        if isinstance(start, tuple):
            if len(start) == 3:
                _found, sx, sy = start
            elif len(start) == 2:
                sx, sy = start
            else:
                return
        else:
            return
        x = sx + ox
        y = sy + oy
        # 钳制到画面范围内
        x = max(0.0, min(float(w), x))
        y = max(0.0, min(float(h), y))
        f = _x_to_freq(x, w)
        g = _y_to_gain(y, h)
        f = max(F_MIN, min(F_MAX, f))
        g = max(G_MIN, min(G_MAX, g))
        self._bands[idx]["freq"] = round(f, 1)
        self._bands[idx]["gain"] = round(g, 2)
        self.queue_draw()

    def _on_drag_end(self, _gesture, _ox, _oy) -> None:
        if self._drag_idx >= 0 and self._on_changed is not None:
            self._on_changed([dict(b) for b in self._bands])
        self._drag_idx = -1

    def _on_scroll(self, _ctrl, _dx, dy) -> bool:
        idx = self._selected
        if idx < 0 or idx >= len(self._bands):
            return False
        q = float(self._bands[idx].get("q", 1.0))
        factor = 1.2 if dy < 0 else 1 / 1.2
        self._bands[idx]["q"] = round(max(0.1, min(10.0, q * factor)), 3)
        self.queue_draw()
        if self._on_changed is not None:
            self._on_changed([dict(b) for b in self._bands])
        return True
