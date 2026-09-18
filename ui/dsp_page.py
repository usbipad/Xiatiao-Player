"""主界面 DSP 音效页（基础控件）。

继承 EffectPage，白得全部基础 DSP 控件；
底部加「高级 DSP 设置 >>」入口，打开独立高级窗口。

按需求：主界面只放基础控件，高级功能在独立窗口。
"""
from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Adw, Gtk

from core.i18n import _

from .effect_page import EffectPage


class DspPage(EffectPage):
    """主界面 DSP 页（基础控件 + 高级入口）。"""

    def __init__(self, on_advanced: Optional[Callable[[], None]] = None,
                 on_dsp_changed: Optional[Callable[[dict], None]] = None,
                 initial: Optional[dict] = None,
                 on_convolution_ir: Optional[Callable[[str], None]] = None,
                 on_convolution_cleared: Optional[Callable[[], None]] = None) -> None:
        super().__init__(
            on_dsp_changed=on_dsp_changed,
            initial=initial,
            on_convolution_ir=on_convolution_ir,
            on_convolution_cleared=on_convolution_cleared,
        )
        self.set_title(_("DSP 音效"))
        self.set_icon_name("multimedia-equalizer-symbolic")
        self._on_advanced = on_advanced

        # 底部：高级 DSP 设置入口
        adv_group = Adw.PreferencesGroup()
        adv_row = Adw.ActionRow()
        adv_row.set_title(_("高级 DSP 设置"))
        adv_row.set_subtitle(_("卷积 IR / 动态 &amp; 响度 / 可视化 / 音色染色 / 采样 &amp; 通道 / 预设管理"))
        adv_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        adv_row.set_activatable(True)
        adv_row.connect("activated", self._on_advanced_clicked)
        adv_group.add(adv_row)
        self.add(adv_group)

    def _on_advanced_clicked(self, _row) -> None:
        if self._on_advanced is not None:
            self._on_advanced()
