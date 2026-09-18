"""可视化独立窗口（非模态）。

- 大尺寸频谱显示，可切换样式（柱状 / 波形 / 平滑曲线）；
- 数据由主窗口的可视化定时器喂入（set_data），与面板底部共用同一份数据；
- 非模态：开着不影响操作主窗口。

窗口基类用普通 Gtk.Window（而非 Adw.Window）：
Adw.Window 与 Adw.PreferencesWindow（设置窗口）同属 Adwaita 家族，
模态/层级处理较复杂，曾导致频谱窗口被模态设置窗口阻断（点不动、关不掉）。
Gtk.Window 的 transient/模态行为更简单可预测。内部仍用 Adw 的控件。
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from core.i18n import _

from .widgets.viz_renderer import VizRenderer


class VizWindow(Gtk.Window):
    """独立的可视化窗口（普通 Gtk.Window）。"""

    def __init__(self, parent=None, style: str = "bars") -> None:
        super().__init__()
        if parent is not None:
            self.set_transient_for(parent)
        self.set_modal(False)  # 非模态
        self.set_title(_("音频频谱"))
        self.set_default_size(820, 420)

        # 用 Adw.HeaderBar 作为标题栏（set_titlebar），而不是塞进内容区——
        # 否则 Gtk.Window 默认装饰 + 内容里的 HeaderBar 会形成两个标题栏。
        header = Adw.HeaderBar()
        # 样式切换
        self._style_dd = Gtk.DropDown.new_from_strings([_("柱状"), _("波形"), _("平滑曲线")])
        self._style_map = ["bars", "wave", "curve"]
        self._style_dd.set_selected(self._style_map.index(style) if style in self._style_map else 0)
        self._style_dd.connect("notify::selected-item", self._on_style_changed)
        header.pack_end(self._style_dd)
        try:
            self.set_titlebar(header)
        except Exception:
            pass

        self.renderer = VizRenderer(style=style)
        self.renderer.set_vexpand(True)
        self.renderer.set_size_request(-1, 360)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(12)
        box.append(self.renderer)

        # Gtk.Window 用 set_child 承载内容
        self.set_child(box)

    def _on_style_changed(self, dd, _pspec) -> None:
        idx = dd.get_selected()
        if 0 <= idx < len(self._style_map):
            self.renderer.set_style(self._style_map[idx])

    def set_data(self, data) -> None:
        self.renderer.set_data(data)
