"""可视化设置页（独立组件，可挂到设置窗口等任意 PreferencesWindow）。

从高级 DSP 窗口抽取而来：调整频谱 FPS / FFT 窗口 / bin 数 / 样式 / 排列 /
音画同步 / 动态范围 / 上升下落速度，并可打开独立频谱窗口。
"""
from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Adw, Gtk

from core.i18n import _


class VizSettingsPage(Adw.PreferencesPage):
    """可视化设置页。

    on_viz_changed: 参数变更时回调（通知主窗口刷新 pipeline / 渲染器）
    on_open_viz_window: 点「打开频谱窗口」时回调
    """

    def __init__(self,
                 on_viz_changed: Optional[Callable[[], None]] = None,
                 on_open_viz_window: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        self.set_title(_("可视化"))
        self.set_icon_name("utilities-system-monitor-symbolic")
        self._on_viz_changed = on_viz_changed
        self._on_open_viz_window = on_open_viz_window

        # 总开关
        from config.settings import get_config
        g_sw = Adw.PreferencesGroup()
        sw = Adw.SwitchRow()
        sw.set_title(_("启用可视化"))
        sw.set_subtitle(_("显示沉浸式页面的频谱；关闭可省 CPU"))
        try:
            sw.set_active(bool(get_config().get_bool("viz_enabled", True)))
        except Exception:
            sw.set_active(True)
        sw.connect("notify::active", self._on_viz_enabled_toggled)
        g_sw.add(sw)
        self.add(g_sw)

        # 数据管线参数（FPS / FFT 窗口 / bin 数）
        g = Adw.PreferencesGroup()
        g.set_title(_("频谱参数"))
        g.set_description(_("调整可视化刷新率与频率分辨率"))

        r_fps = Adw.ActionRow()
        r_fps.set_title(_("采样 FPS"))
        r_fps.set_subtitle(_("越高越流畅，但更耗电/占 CPU"))
        r_fps.add_suffix(self._make_viz_scale(15, 120, 5, "viz_fps"))
        g.add(r_fps)

        r_win = Adw.ActionRow()
        r_win.set_title(_("FFT 窗口大小"))
        r_win.set_subtitle(_("越大，频率分得越细，但计算更耗 CPU"))
        r_win.add_suffix(self._make_viz_window_dropdown())
        g.add(r_win)

        r_bin = Adw.ActionRow()
        r_bin.set_title(_("频谱柱数量"))
        r_bin.set_subtitle(_("影响横向颗粒度"))
        r_bin.add_suffix(self._make_viz_scale(8, 64, 1, "viz_bin_count"))
        g.add(r_bin)

        # 显示
        g2 = Adw.PreferencesGroup()
        g2.set_title(_("显示"))
        r_style = Adw.ActionRow()
        r_style.set_title(_("样式"))
        r_style.add_suffix(self._make_viz_style_dropdown())
        g2.add(r_style)

        r_scale = Adw.ActionRow()
        r_scale.set_title(_("排列方式"))
        r_scale.set_subtitle(_("频率（对数均分）/ 音阶（按音高）"))
        r_scale.add_suffix(self._make_viz_scale_mode_dropdown())
        g2.add(r_scale)

        r_sync = Adw.ActionRow()
        r_sync.set_title(_("音画同步"))
        r_sync.set_subtitle(_("频谱整体延后（ms），与耳朵听到的声音对齐"))
        r_sync.add_suffix(self._make_viz_sync_scale())
        g2.add(r_sync)

        r_db = Adw.ActionRow()
        r_db.set_title(_("动态范围"))
        r_db.set_subtitle(_("响度映射下限（dB）；越负动态越大、小信号越矮"))
        r_db.add_suffix(self._make_viz_db_scale())
        g2.add(r_db)

        r_rise = Adw.ActionRow()
        r_rise.set_title(_("上升速度"))
        r_rise.set_subtitle(_("频谱柱上升快慢；越小越柔和（过小会迟滞）"))
        r_rise.add_suffix(self._make_viz_speed_scale("viz_rise_speed", 0.6, self._on_viz_rise_changed))
        g2.add(r_rise)

        r_fall = Adw.ActionRow()
        r_fall.set_title(_("下落速度"))
        r_fall.set_subtitle(_("频谱柱回落快慢；越小越拖尾"))
        r_fall.add_suffix(self._make_viz_speed_scale("viz_fall_speed", 0.35, self._on_viz_fall_changed))
        g2.add(r_fall)

        # 打开大窗口
        g3 = Adw.PreferencesGroup()
        r_open = Adw.ActionRow()
        r_open.set_title(_("打开频谱窗口"))
        r_open.set_subtitle(_("大尺寸独立窗口，可切换样式"))
        r_open.add_suffix(Gtk.Image.new_from_icon_name("window-new-symbolic"))
        r_open.set_activatable(True)
        r_open.connect("activated", lambda *_: self._on_open_viz_clicked())
        g3.add(r_open)

        self.add(g)
        self.add(g2)
        self.add(g3)

    def _on_viz_enabled_toggled(self, row, _pspec) -> None:
        from config.settings import get_config
        get_config().set_bool("viz_enabled", bool(row.get_active()))
        self._notify_viz()

    # ---- 辅助控件 ----
    def _make_viz_scale_mode_dropdown(self):
        from config.settings import get_config
        cfg = get_config()
        options = [_("频率"), _("音阶")]
        dd = Gtk.DropDown.new_from_strings(options)
        cur = cfg.get_str("viz_scale_mode", "freq")
        dd.set_selected(1 if cur == "musical" else 0)
        dd.connect("notify::selected-item", self._on_viz_scale_mode_changed, ["freq", "musical"])
        return dd

    def _on_viz_scale_mode_changed(self, dd, _pspec, values) -> None:
        from config.settings import get_config
        idx = dd.get_selected()
        if 0 <= idx < len(values):
            get_config().set_str("viz_scale_mode", values[idx])
            self._notify_viz()

    def _make_viz_sync_scale(self):
        from config.settings import get_config
        cfg = get_config()
        sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        sc.set_range(0.0, 500.0)
        sc.set_increments(10.0, 50.0)
        sc.set_draw_value(True)
        sc.set_digits(0)
        sc.set_size_request(220, -1)
        sc.set_valign(Gtk.Align.CENTER)
        sc.set_value(float(cfg.get("viz_sync_delay_ms", 100.0)))
        sc.connect("value-changed", self._on_viz_sync_changed)
        return sc

    def _on_viz_sync_changed(self, scale, _pspec=None) -> None:
        from config.settings import get_config
        get_config().set("viz_sync_delay_ms", float(scale.get_value()))
        self._notify_viz()

    def _make_viz_db_scale(self):
        from config.settings import get_config
        cfg = get_config()
        sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        sc.set_range(-90.0, -20.0)
        sc.set_increments(5.0, 10.0)
        sc.set_draw_value(True)
        sc.set_digits(0)
        sc.set_size_request(220, -1)
        sc.set_valign(Gtk.Align.CENTER)
        sc.set_value(float(cfg.get("viz_db_floor", -60.0)))
        sc.connect("value-changed", self._on_viz_db_changed)
        return sc

    def _on_viz_db_changed(self, scale, _pspec=None) -> None:
        from config.settings import get_config
        get_config().set("viz_db_floor", float(scale.get_value()))
        self._notify_viz()

    def _make_viz_speed_scale(self, key: str, default: float, handler):
        """通用 0..1 速度滑块（上升/下落共用）。"""
        from config.settings import get_config
        cfg = get_config()
        sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        sc.set_range(0.0, 1.0)
        sc.set_increments(0.05, 0.2)
        sc.set_draw_value(True)
        sc.set_digits(2)
        sc.set_size_request(220, -1)
        sc.set_valign(Gtk.Align.CENTER)
        sc.set_value(float(cfg.get(key, default)))
        sc.connect("value-changed", handler, key)
        return sc

    def _on_viz_fall_changed(self, scale, _pspec=None, key: str = "viz_fall_speed") -> None:
        from config.settings import get_config
        get_config().set(key, float(scale.get_value()))
        self._notify_viz()

    def _on_viz_rise_changed(self, scale, _pspec=None, key: str = "viz_rise_speed") -> None:
        from config.settings import get_config
        get_config().set(key, float(scale.get_value()))
        self._notify_viz()

    def _make_viz_scale(self, lo: float, hi: float, step: float, key: str):
        from config.settings import get_config
        cfg = get_config()
        sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        sc.set_range(lo, hi)
        sc.set_increments(step, step * 4)
        sc.set_draw_value(True)
        sc.set_digits(0)
        sc.set_size_request(220, -1)
        sc.set_valign(Gtk.Align.CENTER)
        default = 60 if key == "viz_fps" else 32
        sc.set_value(float(cfg.get_int(key, default)))
        sc.connect("value-changed", self._on_viz_scale_changed, key)
        return sc

    def _make_viz_window_dropdown(self):
        from config.settings import get_config
        cfg = get_config()
        options = ["256", "512", "1024", "2048", "4096"]
        dd = Gtk.DropDown.new_from_strings(options)
        cur = str(cfg.get_int("viz_fft_window", 1024))
        if cur in options:
            dd.set_selected(options.index(cur))
        else:
            dd.set_selected(options.index("1024"))
        dd.connect("notify::selected", self._on_viz_window_changed, options)
        return dd

    def _make_viz_style_dropdown(self):
        from config.settings import get_config
        cfg = get_config()
        options = [_("柱状"), _("波形"), _("平滑曲线")]
        dd = Gtk.DropDown.new_from_strings(options)
        cur = cfg.get_str("viz_style", "bars")
        idx = {"bars": 0, "wave": 1, "curve": 2}.get(cur, 0)
        dd.set_selected(idx)
        dd.connect("notify::selected", self._on_viz_style_changed, ["bars", "wave", "curve"])
        return dd

    def _on_viz_scale_changed(self, scale, key: str) -> None:
        from config.settings import get_config
        val = int(scale.get_value())
        cfg = get_config()
        cfg.set_int(key, val)
        # fps 变更：同步按新 fps 重算 hop（VIZ_RATE/fps 取 2 的幂），
        # 保证 FFT 产帧率 ≥ 渲染率，否则画面会一卡一卡。
        if key == "viz_fps" and val > 0:
            try:
                from core.viz import VIZ_RATE
                target = max(64, min(4096, int(VIZ_RATE / max(1, val))))
                hop = 1 << (target.bit_length() - 1)
                cfg.set_int("viz_hop", hop)
            except Exception:
                pass
        self._notify_viz()

    def _on_viz_window_changed(self, dd, _pspec, options) -> None:
        from config.settings import get_config
        idx = dd.get_selected()
        if 0 <= idx < len(options):
            get_config().set_int("viz_fft_window", int(options[idx]))
            self._notify_viz()

    def _on_viz_style_changed(self, dd, _pspec, values) -> None:
        from config.settings import get_config
        idx = dd.get_selected()
        if 0 <= idx < len(values):
            get_config().set_str("viz_style", values[idx])
            self._notify_viz()

    def _notify_viz(self) -> None:
        if self._on_viz_changed is not None:
            self._on_viz_changed()

    def _on_open_viz_clicked(self) -> None:
        if self._on_open_viz_window is not None:
            self._on_open_viz_window()
