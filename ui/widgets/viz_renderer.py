"""可视化渲染组件（可复用）。

接收幅度数组（来自 core.viz.VizPipeline），用 Cairo 画频谱柱。
尺寸自适应：同一组件可挂到左栏底部（小）或独立窗口（大）。
"""
from __future__ import annotations

import math
from typing import List

import cairo
from gi.repository import Gtk


def _smooth(prev: List[float], cur: List[float],
            fall_alpha: float = 0.35, rise_alpha: float = 0.6) -> List[float]:
    """帧间平滑：
    - 上升：按 rise_alpha 缓动趋近峰值（1.0=瞬间到位，越小越柔和）；
    - 下落：按 fall_alpha 衰减（越小落得越慢）。

    注意：alpha 是「每帧趋近比例」，其视觉速度与帧率强相关。
    调用方应通过 _alpha_from_tau 按实际帧间隔换算，保证与 fps 解耦。
    """
    if not prev:
        # 首次：从 0 起步，避免第一帧直接满高
        return [c * rise_alpha for c in cur]
    if len(prev) != len(cur):
        # 长度变化（如 bin 数变了）：按新长度对齐，从 0 起步平滑升起，
        # 而不是直接返回目标值（后者会造成「啪」一下跳变）。
        return [c * rise_alpha for c in cur]
    out = []
    for p, c in zip(prev, cur):
        if c > p:
            out.append(p + (c - p) * rise_alpha)
        else:
            out.append(p + (c - p) * fall_alpha)
    return out


def _alpha_from_tau(tau_s: float, dt_s: float) -> float:
    """把时间常数 τ（秒）换算为「每帧趋近比例」alpha。

    连续一阶低通：alpha = 1 - exp(-dt/τ)。
    dt 为帧间隔（秒）。这样相同 τ 在任何帧率下视觉速度一致：
      - τ 越小 → 越快（越硬）；τ 越大 → 越慢（越柔和）。
    """
    if tau_s <= 1e-6:
        return 1.0
    if dt_s <= 1e-6:
        return 1.0
    import math as _m
    return 1.0 - _m.exp(-dt_s / tau_s)


class VizRenderer(Gtk.DrawingArea):
    """频谱柱渲染器。样式后续可扩展（波形 / 平滑曲线）。"""

    def __init__(self, style: str = "bars") -> None:
        super().__init__()
        self.set_content_height(64)
        self.set_vexpand(False)
        self.set_draw_func(self._draw, None)
        self._style = style
        self._data: List[float] = []
        self._smoothed: List[float] = []
        # 幅度归一化参考（自适应峰值）
        self._peak = 1e-6
        # 下落衰减系数：每帧下落时衰减的比例（越小落得越慢）
        self._fall_alpha = 0.35
        # 上升缓动系数：每帧上升时趋近的比例（1.0=瞬间到位，越小越柔和）
        self._rise_alpha = 0.6
        # 上升/下落的时间常数（秒）：真正的速度定义，与帧率无关
        self._rise_tau = 0.4 - 0.6 * 0.395
        self._fall_tau = 0.5 - 0.35 * 0.48
        # 上次 set_data 的时间戳，用于计算帧间隔 dt
        self._last_data_time = 0.0
        # 响度映射下限（dB）：只显示该值以上的信号，越小动态范围越大
        self._db_floor = -60.0
        # 氛围色（r,g,b 0..1）；None 用默认冷色
        self._ambient_color = None
        # 柔和淡色模式（浅背景上更搭）
        self._soft = False
        # 数据源：无参函数，返回最新幅度数组；由 tick callback 每帧拉取
        self._data_source = None
        # tick 是否运行（默认停：主界面不跑，进沉浸页才 start）
        self._tick_running = False
        self._tick_id = None
        # 目标刷新率（Hz）：0/None 表示不节流（跟 vsync 走）。
        # 由 viz_fps 配置驱动；节流保证画面节拍与用户设置一致。
        self._fps = 0
        self._min_interval = 0.0      # 秒；两次 set_data 的最小间隔
        self._last_tick = 0.0         # 上次拉取数据的时间戳

    def start(self) -> None:
        """启动帧回调（进入沉浸页时调用）。"""
        if self._tick_running:
            return
        self._tick_running = True
        try:
            self._tick_id = self.add_tick_callback(self._on_tick)
        except Exception:
            self._tick_running = False

    def stop(self) -> None:
        """停止帧回调（退出沉浸页时调用），避免后台空转。"""
        self._tick_running = False
        if self._tick_id is not None:
            try:
                self.remove_tick_callback(self._tick_id)
            except Exception:
                pass
            self._tick_id = None

    def set_data_source(self, fn) -> None:
        """设置数据源（无参函数，返回 List[float]）。None 则停止拉取。"""
        self._data_source = fn

    def set_fps(self, fps) -> None:
        """设置目标刷新率（Hz）。<=0 表示不节流（跟显示器 vsync 走）。

        节流在 tick 里按时间戳判断：距上次拉取不足 1/fps 秒就跳过本帧，
        避免渲染快于数据产出导致重复帧（视觉卡顿）。
        """
        try:
            f = float(fps)
        except Exception:
            f = 0.0
        self._fps = f
        self._min_interval = (1.0 / f) if f > 0 else 0.0

    def _on_tick(self, _widget, _clock) -> bool:
        """GTK 每帧回调：从数据源拉最新数据并更新（触发重绘）。返回 True 持续监听。

        按 set_fps 设置的间隔节流：未到间隔直接返回，不拉数据也不重绘。
        """
        if not self._tick_running:
            return False
        # 节流：不足最小间隔则跳过本帧
        if self._min_interval > 0.0:
            import time as _t
            now = _t.monotonic()
            if now - self._last_tick < self._min_interval:
                return True
            self._last_tick = now
        if self._data_source is not None:
            try:
                data = self._data_source()
                if data:
                    self.set_data(data)
            except Exception:
                pass
        return True

    def set_style(self, style: str) -> None:
        """样式：bars（柱状）/ wave（波形）/ curve（平滑曲线）。"""
        self._style = style
        self.queue_draw()

    def set_soft(self, soft: bool = True) -> None:
        """柔和淡色模式：柱子用低饱和浅色，适合浅背景。"""
        self._soft = bool(soft)
        self.queue_draw()

    def set_fall_alpha(self, alpha: float) -> None:
        """设置下落衰减系数（0..1，越小下落越慢）。"""
        self._fall_alpha = max(0.02, min(1.0, float(alpha)))

    def set_fall_speed(self, speed: float) -> None:
        """设置下落速度（0..1，越大落得越快）。

        内部换算为时间常数 τ（秒），再由帧间隔换算每帧 alpha，
        使视觉效果与 fps 解耦。speed 0→τ 大（拖尾），1→τ 小（快落）。
        """
        s = max(0.0, min(1.0, float(speed)))
        # speed 0 → τ=0.5s（很慢）；speed 1 → τ=0.03s（较快）。
        # 用指数分布，让滑块全程都有可感变化（线性时高端变化太小）。
        self._fall_tau = 0.5 * (0.06 ** s)  # s=0→0.5, s=1→0.03
        self._fall_alpha = _alpha_from_tau(self._fall_tau, 1.0 / 60.0)
        import os as _os
        if _os.environ.get("XIATIAO_VIZ_DEBUG"):
            print(f"[viz-set] set_fall_speed({speed}) -> fall_tau={self._fall_tau:.3f}", flush=True)

    def set_rise_speed(self, speed: float) -> None:
        """设置上升速度（0..1，越大上升越快/越硬）。

        内部换算为时间常数 τ（秒），再由帧间隔换算每帧 alpha，
        使视觉效果与 fps 解耦。speed 0→τ 大（柔和），1→τ 小（瞬间）。
        """
        s = max(0.0, min(1.0, float(speed)))
        # speed 0 → τ=0.4s（很柔和）；speed 1 → τ=0.02s（较快）。
        # 用指数分布，让滑块全程都有可感变化（线性时高端变化太小）。
        self._rise_tau = 0.4 * (0.05 ** s)  # s=0→0.4, s=1→0.02
        self._rise_alpha = _alpha_from_tau(self._rise_tau, 1.0 / 60.0)
        import os as _os
        if _os.environ.get("XIATIAO_VIZ_DEBUG"):
            print(f"[viz-set] set_rise_speed({speed}) -> rise_tau={self._rise_tau:.3f}", flush=True)

    def set_db_floor(self, db_floor: float) -> None:
        """设置响度映射下限（dB）：越小动态范围越大、小信号越矮。"""
        self._db_floor = min(-6.0, float(db_floor))
        self.queue_draw()
        import os as _os
        if _os.environ.get("XIATIAO_VIZ_DEBUG"):
            print(f"[viz-set] set_db_floor({db_floor}) -> {self._db_floor:.1f}", flush=True)

    def set_data(self, data: List[float], redraw: bool = True) -> None:
        """更新幅度数组；redraw=True 时请求重绘（tick 场景传 False）。

        每次更新按实际帧间隔重算 alpha（时间常数法），保证上升/下落
        速度与 fps 解耦：改 fps 后同样的滑块值视觉速度不变。
        """
        import time as _t
        now = _t.monotonic()
        dt = (now - self._last_data_time) if self._last_data_time > 0 else (1.0 / 60.0)
        # 限制 dt 范围，避免卡顿/切后台后 dt 过大导致 alpha 突变为 1
        dt = max(1.0 / 240.0, min(dt, 0.2))
        self._last_data_time = now
        rise_alpha = _alpha_from_tau(self._rise_tau, dt)
        fall_alpha = _alpha_from_tau(self._fall_tau, dt)
        # ---- 临时诊断 ----
        import os as _os
        if _os.environ.get("XIATIAO_VIZ_DEBUG"):
            self._dbg_n = getattr(self, "_dbg_n", 0) + 1
            if self._dbg_n % 60 == 0:
                _d = list(data)
                _mx = max(_d) if _d else 0.0
                _mn = min(_d) if _d else 0.0
                print(f"[viz-renderer] dt={dt*1000:.1f}ms rise_tau={self._rise_tau:.3f} fall_tau={self._fall_tau:.3f} "
                      f"rise_a={rise_alpha:.3f} fall_a={fall_alpha:.3f} db_floor={self._db_floor:.1f} "
                      f"data[{_mn:.4f},{_mx:.4f}] peak={self._peak:.4f}", flush=True)
        self._data = list(data)
        self._smoothed = _smooth(self._smoothed, self._data, fall_alpha, rise_alpha)
        # 自适应峰值：慢速跟随，避免 peak 剧烈波动淹没 db_floor/rise/fall 设置。
        # 上升：立即跟上（防止削顶）；下落：缓慢衰减（视觉稳定）。
        m = max(self._smoothed) if self._smoothed else 0.0
        if m >= self._peak:
            self._peak = m
        else:
            # 缓慢下落（每帧衰减 0.5%），让参考稳定
            self._peak = self._peak * 0.995 + m * 0.005
        # 地板：避免小信号时参考过小导致归一化乱跳
        if self._peak < 1e-3:
            self._peak = 1e-3
        if redraw:
            self.queue_draw()

    def has_signal(self) -> bool:
        """是否有非零数据（静音时可用于跳过重绘）。"""
        return any(v > 1e-6 for v in self._data)

    def _draw(self, _area, cr, w: int, h: int, _data) -> None:
        # 透明背景（让父容器样式透出）
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()

        data = self._smoothed
        n = len(data)
        if n == 0 or w <= 0 or h <= 0:
            return
        # 归一化到 0..1：dB 域映射。
        # r = v/peak（相对峰值），db = 20log10(r) ∈ (-∞, 0]；
        # floor_db（如 -60）表示「相对峰值衰减该 dB 以下的信号贴地」。
        # x = (db - floor_db) / (0 - floor_db)：floor_db→0, 0dB→1。
        # 配合稳定的 peak，动态范围滑块效果直观。
        floor_db = self._db_floor          # 如 -60dB（负）
        if floor_db >= 0:
            floor_db = -60.0
        span = -floor_db                   # 正
        ref = self._peak if self._peak > 0 else 1e-9
        norm = []
        for v in data:
            if v <= 1e-9:
                norm.append(0.0)
                continue
            r = v / ref
            if r <= 1e-9:
                norm.append(0.0)
                continue
            db = 20.0 * math.log10(r)      # ≤ 0
            x = (db - floor_db) / span     # floor_db→0, 0dB→1
            norm.append(max(0.0, min(1.0, x)))

        if self._style == "bars":
            self._draw_bars(cr, w, h, norm)
        elif self._style == "wave":
            self._draw_wave(cr, w, h, norm)
        elif self._style == "curve":
            self._draw_curve(cr, w, h, norm)
        else:  # ambient：氛围光晕（用于背景）
            self._draw_ambient(cr, w, h, norm)

    def set_ambient_color(self, rgb) -> None:
        """设置氛围色（r,g,b ∈ 0..1）；None 用默认冷色。"""
        self._ambient_color = rgb

    def _draw_ambient(self, cr, w, h, norm) -> None:
        """氛围模式：粗色块 + 低透明 + 纵向渐变，模拟「背景模糊透出的律动光晕」。

        不做真模糊（GTK4 实时模糊不可靠），而是用宽柱、柔和渐变、低透明度
        在视觉上营造模糊透出的氛围感。
        """
        n = len(norm)
        if n == 0:
            return
        # 颜色：默认冷蓝，可被封面主色覆盖
        if self._ambient_color is not None:
            r0, g0, b0 = self._ambient_color
        else:
            r0, g0, b0 = 0.45, 0.62, 0.95
        # 柱子很宽（相互融合 → 柔和），只画下半部
        bar_w = (w / n) * 1.6  # 重叠，制造融合感
        base_y = h * 0.98
        max_h = h * 0.85       # 最高到 h 的 85%
        for i, v in enumerate(norm):
            cx = (i + 0.5) * (w / n)
            bh = max(2.0, v * max_h)
            top_y = base_y - bh
            # 纵向渐变：顶部透明 → 底部稍浓（光晕感）
            grad = cairo.LinearGradient(cx, top_y, cx, base_y)
            grad.add_color_stop_rgba(0.0, r0, g0, b0, 0.0)     # 顶端完全透明
            grad.add_color_stop_rgba(0.6, r0, g0, b0, 0.10)    # 中段淡
            grad.add_color_stop_rgba(1.0, r0, g0, b0, 0.28)    # 底部稍浓
            cr.set_source(grad)
            cr.rectangle(cx - bar_w / 2, top_y, bar_w, bh)
            cr.fill()

    def _draw_bars(self, cr, w, h, norm) -> None:
        n = len(norm)
        gap = 1.0
        bar_w = max(1.0, (w - gap * (n - 1)) / n)
        soft = self._soft
        for i, v in enumerate(norm):
            x = i * (bar_w + gap)
            bh = max(1.0, v * h)
            y = h - bh
            t = i / max(1, n - 1)
            if soft:
                # 柔和淡色：低饱和灰蓝，透明度低，跟浅背景搭
                r = 0.62 + 0.18 * (1.0 - t)
                g = 0.68 + 0.15 * (1.0 - t)
                b = 0.80
                a = 0.55
            else:
                r = 0.30 + 0.50 * (1.0 - t)
                g = 0.55 + 0.30 * t
                b = 1.00
                a = 0.9
            cr.set_source_rgba(r, g, b, a)
            cr.rectangle(x, y, bar_w, bh)
            cr.fill()

    def _draw_wave(self, cr, w, h, norm) -> None:
        """波形：以中线为基准的镜像波形。"""
        n = len(norm)
        cr.set_source_rgba(0.4, 0.75, 1.0, 0.9)
        cr.set_line_width(2.0)
        mid = h / 2.0
        for i, v in enumerate(norm):
            x = i * (w / max(1, n - 1))
            amp = v * mid
            cr.move_to(x, mid - amp)
            cr.line_to(x, mid + amp)
        cr.stroke()

    def _draw_curve(self, cr, w, h, norm) -> None:
        """平滑曲线：Catmull-Rom 风格，填充曲线下方区域。"""
        n = len(norm)
        if n < 2:
            return
        pts = [(i * (w / (n - 1)), h - norm[i] * h) for i in range(n)]
        cr.set_source_rgba(0.4, 0.75, 1.0, 0.25)
        cr.move_to(pts[0][0], h)
        for (x, y) in pts:
            cr.line_to(x, y)
        cr.line_to(pts[-1][0], h)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(0.5, 0.85, 1.0, 0.95)
        cr.set_line_width(2.0)
        cr.move_to(pts[0][0], pts[0][1])
        for (x, y) in pts[1:]:
            cr.line_to(x, y)
        cr.stroke()
