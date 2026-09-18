"""DSP 音效设置页（设置窗口内）。

模块：
- 全局开关（旁路）
- 10 段图形 EQ
- 低音增强（增益 + 频率）
- 高音增强（增益 + 频率）
- 立体声宽度
- 左右平衡
- 预增益
- 限幅器（开关 + 阈值）

拖动滑块实时下发到播放器（立刻生效）。
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from gi.repository import Adw, Gio, Gtk

from core.dsp_store import get_dsp_preset_store
from core.i18n import _

#: EQ 频段（与 Rust 侧 EQ_FREQS 一致）
EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def _default_params() -> dict:
    return {
        "enabled": False,
        "camilla_enabled": False,
        "pre_gain_db": 0.0,
        "replaygain_enabled": False,
        "replaygain_mode": "track",
        "replaygain_db": 0.0,
        "replaygain_preamp_db": 0.0,
        "convolution_enabled": False,
        "convolution_ir": "",
        "convolution_channel": 0,
        "convolution_stereo_ir": False,
        "convolution_dry": 1.0,
        "convolution_wet": 1.0,
        "peq_enabled": False,
        "peq_bands": [],
        "eq_enabled": False,
        "eq_gains": [0.0] * 10,
        "eq_q": 1.0,
        "bass_enabled": False,
        "bass_gain_db": 0.0,
        "bass_freq": 100.0,
        "loudness_enabled": False,
        "loudness_amount": 0.0,
        "treble_gain_db": 0.0,
        "treble_freq": 8000.0,
        "crossfeed_enabled": False,
        "crossfeed_amount": 0.3,
        "crossfeed_delay_ms": 0.3,
        "stereo_width": 1.0,
        "width_enabled": False,
        "balance": 0.0,
        "compressor_enabled": False,
        "compressor_threshold_db": -18.0,
        "compressor_ratio": 2.0,
        "compressor_makeup_db": 0.0,
        "compressor_attack": 0.01,
        "compressor_release": 0.2,
        "limiter_threshold_db": 0.0,
        "limiter_enabled": False,
        "limiter_soft_clip": False,
        "phase_invert": False,
        "channel_matrix": "off",
        "reverb_enabled": False,
        "reverb_mix": 0.25,
        "reverb_pre_delay_ms": 20.0,
        "reverb_decay": 0.5,
        "reverb_damping": 0.5,
        "reverb_width": 1.0,
        "reverb_low_cut_hz": 100.0,
        "reverb_mod_depth": 0.5,
    }


class EffectPage(Adw.PreferencesPage):
    """DSP 音效设置页。"""

    def __init__(self, on_dsp_changed: Optional[Callable[[dict], None]] = None,
                 initial: Optional[dict] = None,
                 on_convolution_ir: Optional[Callable[[str], None]] = None,
                 on_convolution_cleared: Optional[Callable[[], None]] = None,
                 mode: str = "full",
                 only=None,
                 on_open_advanced: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        self.set_title(_("DSP 音效"))
        self.set_icon_name("multimedia-equalizer-symbolic")
        self._only = only
        self._on_dsp_changed = on_dsp_changed
        # 注意：回调属性名不能与方法名同名，否则会遮蔽方法
        self._on_convolution_ir = on_convolution_ir
        self._on_convolution_cleared = on_convolution_cleared
        self._on_open_advanced = on_open_advanced
        self._mode = mode
        self._params = _default_params()
        if initial:
            self._params.update(initial)
        self._sliders: dict = {}
        # 归 Camilla 负责的控件组（Camilla 关闭时置灰）
        self._camilla_groups: list = []
        # 恢复默认值期间为 True：抑制滑块回调，避免旧值被写回
        self._restoring: bool = False
        # 防抖定时器（滑块拖动合并下发）
        self._emit_timer = None

        #: 本页包含的组（single 模式用；决定「重置本页」的范围）
        self._page_groups: list = []

        # 单功能页（高级窗口用）：只建指定组（only 可为字符串或列表）
        if mode == "single" and only:
            builders = {
                "master": self._build_master_group,
                "replaygain": self._build_replaygain_group,
                "gain": self._build_gain_group,
                "eq": self._build_eq_group,
                "peq": self._build_peq_group,
                "convolution": self._build_convolution_group,
                "bass_treble": self._build_bass_treble_group,
                "loudness": self._build_loudness_group,
                "compressor": self._build_compressor_group,
                "limiter": self._build_limiter_group,
                "stereo": self._build_stereo_group,
                "crossfeed": self._build_crossfeed_group,
                "routing": self._build_routing_group,
                "reverb": self._build_reverb_group,
            }
            keys = [only] if isinstance(only, str) else list(only)
            self._page_groups = list(keys)
            for key in keys:
                fn = builders.get(key)
                if fn is not None:
                    fn()
            self._sync_sliders()
            self._update_camilla_sensitivity()
            return

        # 一级页（basic）：总开关 → 常用（宽度/平衡/低音高音）→ 高级入口
        if mode == "basic":
            self._page_groups = ["master", "stereo", "bass_treble"]
            self._build_master_group()
            self._build_stereo_group()
            self._build_bass_treble_group()
            self._build_advanced_entry()

        # 完整链路排布（full/advanced/advanced_no_peq）：按音频链路顺序
        if mode in ("advanced", "full", "advanced_no_peq"):
            self._page_groups = list(self._GROUP_FIELDS.keys())
            self._build_page_reset_row()
            self._build_master_group()           # 总开关
            self._build_replaygain_group()       # 前置：ReplayGain
            self._build_gain_group()             # 预增益 / 余量
            self._build_eq_group()               # 10 段图形 EQ
            if mode != "advanced_no_peq":
                self._build_peq_group()          # 参数均衡器
            self._build_convolution_group()      # 卷积 IR
            self._build_bass_treble_group()      # 低音 / 高音
            self._build_loudness_group()         # Loudness
            self._build_compressor_group()       # 压缩
            self._build_limiter_group()          # 限幅
            self._build_stereo_group()           # 宽度 / 平衡
            self._build_crossfeed_group()        # Crossfeed
            self._build_routing_group()          # 相位 / 通道矩阵
            self._build_reverb_group()           # 混响
            self._build_preset_group()           # 预设（整套配置）

        self._sync_sliders()
        # 初始置灰状态
        self._update_camilla_sensitivity()

    def _build_advanced_entry(self) -> None:
        """基础模式底部：打开高级 DSP 设置的入口。"""
        group = Adw.PreferencesGroup()
        row = Adw.ActionRow()
        row.set_title(_("高级设置"))
        row.set_subtitle(_("EQ / 限幅 / 压缩 / 响度 / 卷积 等"))
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.set_activatable(True)
        row.connect("activated", lambda *_: self._on_open_advanced and self._on_open_advanced())
        group.add(row)
        self.add(group)

    # ------------------------------------------------------------
    # 预设
    # ------------------------------------------------------------
    def _build_preset_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("预设"))
        self.add(group)
        self._preset_store = get_dsp_preset_store()

        row = Adw.ActionRow()
        row.set_title(_("预设"))
        # 下拉：选中即加载
        self._preset_dropdown = Gtk.DropDown()
        self._preset_dropdown.set_valign(Gtk.Align.CENTER)
        self._reload_preset_dropdown()
        self._preset_dropdown.connect("notify::selected", self._on_preset_selected)
        row.add_suffix(self._preset_dropdown)
        # 新建预设
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.set_tooltip_text(_("新建预设（输入名字，保存当前参数）"))
        add_btn.connect("clicked", self._on_preset_new)
        row.add_suffix(add_btn)
        # 保存（覆盖下拉选中的预设）
        save_btn = Gtk.Button(icon_name="document-save-symbolic")
        save_btn.set_valign(Gtk.Align.CENTER)
        save_btn.add_css_class("flat")
        save_btn.set_tooltip_text(_("保存：覆盖当前选中的预设"))
        save_btn.connect("clicked", self._on_preset_save)
        row.add_suffix(save_btn)
        # 删除（下拉选中的预设）
        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        del_btn.set_tooltip_text(_("删除当前选中的预设"))
        del_btn.connect("clicked", self._on_preset_delete)
        row.add_suffix(del_btn)
        group.add(row)
        # 重置按钮：放在「预设」标题右侧（header suffix）
        reset_btn = Gtk.Button(label=_("重置"))
        reset_btn.set_valign(Gtk.Align.CENTER)
        reset_btn.add_css_class("destructive-action")
        reset_btn.set_tooltip_text(_("重置所有参数到默认"))
        reset_btn.connect("clicked", self._on_reset)
        try:
            group.set_header_suffix(reset_btn)
        except Exception:
            pass

    def _reload_preset_dropdown(self) -> None:
        names = self._preset_store.names()
        model = Gtk.StringList()
        for n in names:
            model.append(n)
        self._preset_names = names
        self._preset_dropdown.set_model(model)
        cur = self._preset_store.current()
        if cur in names:
            self._preset_dropdown.set_selected(names.index(cur))
        elif names:
            self._preset_dropdown.set_selected(0)

    def _on_preset_selected(self, dropdown, _pspec) -> None:
        """下拉选中预设 → 立即加载。"""
        idx = dropdown.get_selected()
        if not (hasattr(self, "_preset_names") and 0 <= idx < len(self._preset_names)):
            return
        name = self._preset_names[idx]
        params = self._preset_store.get(name)
        if params:
            self._preset_store.set_current(name)
            merged = _default_params()
            merged.update(params)
            self._params = merged
            self._sync_all_switches()
            self._sync_sliders()
            self._update_camilla_sensitivity()
            # 刷新 PEQ 曲线
            if getattr(self, "_peq_curve", None) is not None:
                self._peq_curve.set_bands(self._params.get("peq_bands", []))
            self._emit()

    def _on_preset_new(self, _btn) -> None:
        """新建预设：弹命名框，用当前参数创建。"""
        dialog = Adw.MessageDialog(
            transient_for=self.get_root(),
            heading="新建预设",
            body="输入预设名称（保存当前参数）",
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("预设名称"))
        entry.set_margin_top(6)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "取消")
        dialog.add_response("ok", "创建")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.connect("response", self._on_new_response, entry)
        dialog.present()

    def _on_new_response(self, dialog, response, entry) -> None:
        if response == "ok":
            name = entry.get_text().strip()
            if name:
                self._preset_store.put(name, dict(self._params))
                self._reload_preset_dropdown()
        dialog.destroy()

    def _on_preset_save(self, _btn) -> None:
        """保存：覆盖保存到下拉当前选中的预设。"""
        idx = self._preset_dropdown.get_selected()
        if not (hasattr(self, "_preset_names") and 0 <= idx < len(self._preset_names)):
            return
        name = self._preset_names[idx]
        self._preset_store.put(name, dict(self._params))

    def _on_preset_delete(self, _btn) -> None:
        """删除：删除下拉当前选中的预设。"""
        idx = self._preset_dropdown.get_selected()
        if not (hasattr(self, "_preset_names") and 0 <= idx < len(self._preset_names)):
            return
        name = self._preset_names[idx]
        if self._preset_store.remove(name):
            self._reload_preset_dropdown()

    def _sync_all_switches(self) -> None:
        """把当前 params 同步到所有开关（不触发回调）。

        mode 只建部分开关，用属性名 + getattr 取，未建的跳过，避免 AttributeError。
        """
        pairs = (
            ("_master_switch", "enabled", "_on_master_toggled"),
            ("_peq_switch", "peq_enabled", "_on_peq_enabled_toggled"),
            ("_bass_switch", "bass_enabled", "_on_bass_enabled_toggled"),
            ("_loudness_switch", "loudness_enabled", "_on_loudness_toggled"),
            ("_headroom_switch", "headroom_enabled", "_on_headroom_toggled"),
            ("_crossfeed_switch", "crossfeed_enabled", "_on_crossfeed_toggled"),
            ("_compressor_switch", "compressor_enabled", "_on_compressor_toggled"),
            ("_limiter_row", "limiter_enabled", "_on_limiter_toggled"),
        )
        for sw_name, key, cb_name in pairs:
            sw = getattr(self, sw_name, None)
            cb = getattr(self, cb_name, None)
            if sw is None or cb is None:
                continue
            try:
                sw.handler_block_by_func(cb)
                sw.set_active(bool(self._params.get(key, False)))
                sw.handler_unblock_by_func(cb)
            except Exception:
                pass

    # ------------------------------------------------------------
    # 重置（单组 / 整页）
    # ------------------------------------------------------------
    #: 组标识 → 该组负责的参数字段（重置用）
    _GROUP_FIELDS = {
        "master": ["enabled"],
        "gain": ["pre_gain_db", "headroom_enabled", "headroom_db"],
        "replaygain": ["replaygain_enabled", "replaygain_mode", "replaygain_db", "replaygain_preamp_db"],
        "eq": ["eq_enabled", "eq_gains", "eq_q"],
        "peq": ["peq_enabled", "peq_bands"],
        "convolution": ["convolution_enabled", "convolution_ir", "convolution_channel", "convolution_stereo_ir"],
        "bass_treble": ["bass_enabled", "bass_gain_db", "bass_freq", "treble_gain_db", "treble_freq"],
        "loudness": ["loudness_enabled", "loudness_amount"],
        "stereo": ["width_enabled", "stereo_width", "balance"],
        "crossfeed": ["crossfeed_enabled", "crossfeed_amount", "crossfeed_delay_ms"],
        "routing": ["phase_invert", "channel_matrix"],
        "reverb": ["reverb_enabled", "reverb_mix", "reverb_pre_delay_ms", "reverb_decay", "reverb_damping", "reverb_width", "reverb_low_cut_hz", "reverb_mod_depth"],
        "compressor": ["compressor_enabled", "compressor_threshold_db", "compressor_ratio", "compressor_makeup_db", "compressor_attack", "compressor_release"],
        "limiter": ["limiter_enabled", "limiter_threshold_db", "limiter_soft_clip"],
    }

    def _add_group_reset(self, group, fields: list) -> None:
        """给组标题右侧加「重置」按钮：只重置该组字段并下发。"""
        try:
            btn = Gtk.Button(label=_("重置"))
            btn.set_valign(Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.set_tooltip_text(_("重置本组为默认值"))
            btn.connect("clicked", lambda _b, fs=list(fields): self._reset_fields(fs))
            group.set_header_suffix(btn)
        except Exception:
            pass

    def _reset_fields(self, fields: list, emit: bool = True) -> None:
        """把指定字段重置为默认值，刷新 UI 并（可选）下发。"""
        defaults = _default_params()
        for k in fields:
            if k in defaults:
                # 列表/字典类要深拷贝，避免与默认值共享引用
                v = defaults[k]
                self._params[k] = list(v) if isinstance(v, list) else (
                    dict(v) if isinstance(v, dict) else v)
        self._sync_sliders()
        self._sync_all_switches()
        self._sync_extra_switches()
        self._update_camilla_sensitivity()
        # 刷新 PEQ 曲线
        if getattr(self, "_peq_curve", None) is not None:
            self._peq_curve.set_bands(self._params.get("peq_bands", []))
        # 刷新卷积 IR 标签
        self._sync_convolution_label()
        if emit:
            self._emit()

    def _sync_extra_switches(self) -> None:
        """同步未纳入 _sync_all_switches 的开关（卷积 / 图形EQ / 宽度等）。"""
        pairs = (
            ("_convolution_switch", "convolution_enabled", "_on_convolution_toggled"),
            ("_eq_switch", "eq_enabled", None),
        )
        for sw_name, key, cb_name in pairs:
            sw = getattr(self, sw_name, None)
            if sw is None:
                continue
            try:
                cb = getattr(self, cb_name, None) if cb_name else None
                if cb is not None:
                    sw.handler_block_by_func(cb)
                sw.set_active(bool(self._params.get(key, False)))
                if cb is not None:
                    sw.handler_unblock_by_func(cb)
            except Exception:
                pass

    def _sync_convolution_label(self) -> None:
        """刷新卷积 IR 文件名标签（无 IR 时显示「未加载」）。"""
        lbl = getattr(self, "_conv_ir_label", None)
        if lbl is None:
            return
        try:
            path = str(self._params.get("convolution_ir") or "")
            lbl.set_text(os.path.basename(path) if path else _("未加载"))
        except Exception:
            pass

    def _reset_all(self) -> None:
        """重置所有 DSP 功能为默认值（总开关状态保持不变）。"""
        fields = []
        for fs in self._GROUP_FIELDS.values():
            fields.extend(fs)
        # 总开关不参与重置：全局旁路状态不应被「重置功能」改动
        fields = [f for f in fields if f != "enabled"]
        # 一次性重置并下发（emit 只调一次，避免中间态发出去）
        self._reset_fields(fields, emit=True)

    def _build_page_reset_row(self) -> None:
        """页面顶部右侧：圆形图标「重置本页」。

        Adw.PreferencesPage 只接受 PreferencesGroup，故用无标题组承载一行。
        """
        group = Adw.PreferencesGroup()
        # 去卡片背景，让按钮像页面右上角的浮动圆钮
        group.add_css_class("reset-bar")
        row = Adw.ActionRow()
        row.set_activatable(False)
        row.add_css_class("reset-bar-row")
        btn = Gtk.Button(icon_name="edit-undo-symbolic")
        btn.add_css_class("circular")
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text(_("重置本页所有功能为默认值"))
        btn.connect("clicked", lambda *_: self._reset_all())
        row.add_suffix(btn)
        group.add(row)
        self.add(group)

    # ------------------------------------------------------------
    # 全局
    # ------------------------------------------------------------
    def _build_master_group(self) -> None:
        group = Adw.PreferencesGroup()
        self.add(group)

        row = Adw.ActionRow()
        row.set_title(_("启用 DSP"))
        master_switch = Gtk.Switch()
        master_switch.set_valign(Gtk.Align.CENTER)
        master_switch.set_active(bool(self._params.get("enabled", False)))
        master_switch.connect("notify::active", self._on_master_toggled)
        self._master_switch = master_switch
        row.add_suffix(master_switch)
        # 一级页：开关旁放红色「重置所有」
        if self._mode == "basic":
            reset_btn = Gtk.Button(label=_("重置所有"))
            reset_btn.set_valign(Gtk.Align.CENTER)
            reset_btn.add_css_class("destructive-action")
            reset_btn.set_tooltip_text(_("把 DSP 音效所有功能恢复为默认值"))
            reset_btn.connect("clicked", lambda *_: self._reset_all())
            row.add_suffix(reset_btn)
        group.add(row)
        # 无「启用 Camilla 引擎」开关：Camilla 是否参与由各功能开关自动决定
        self._update_camilla_sensitivity()

    # ------------------------------------------------------------
    # 增益 / 余量
    # ------------------------------------------------------------
    def _build_gain_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("增益 / 余量"))
        self.add(group)
        # 预增益归 Camilla → 整组随 Camilla 开关置灰
        self._camilla_groups.append(group)

        # 预增益
        row = Adw.ActionRow()
        row.set_title(_("预增益 (dB)"))
        scale = self._make_scale(-12.0, 12.0, 0.5, "pre_gain_db")
        row.add_suffix(scale)
        group.add(row)

        # Headroom 开关 + 衰减
        hsw = Adw.SwitchRow()
        hsw.set_title(_("启用余量管理 (Headroom)"))
        hsw.set_subtitle(_("预留峰值空间，防止削波"))
        hsw.set_active(bool(self._params.get("headroom_enabled", False)))
        hsw.connect("notify::active", self._on_headroom_toggled)
        group.add(hsw)
        self._headroom_switch = hsw

        row = Adw.ActionRow()
        row.set_title(_("余量 (dB)"))
        scale = self._make_scale(-12.0, 0.0, 0.5, "headroom_db")
        row.add_suffix(scale)
        group.add(row)

    # ------------------------------------------------------------
    # ReplayGain
    # ------------------------------------------------------------
    def _build_replaygain_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("ReplayGain"))
        group.set_description(_("按文件标签拉平响度（需音频带 ReplayGain 标签）"))
        self.add(group)

        sw = Adw.SwitchRow()
        sw.set_title(_("启用 ReplayGain"))
        sw.set_active(bool(self._params.get("replaygain_enabled", False)))
        sw.connect("notify::active", self._on_replaygain_toggled)
        group.add(sw)
        self._replaygain_switch = sw

        # 模式：曲目 / 专辑
        mode_row = Adw.ComboRow()
        mode_row.set_title(_("模式"))
        model = Gtk.StringList()
        model.append(_("曲目 (Track)"))
        model.append(_("专辑 (Album)"))
        mode_row.set_model(model)
        mode_row.set_selected(0 if self._params.get("replaygain_mode", "track") == "track" else 1)
        mode_row.connect("notify::selected", self._on_replaygain_mode)
        group.add(mode_row)
        self._replaygain_mode_row = mode_row

        row = Adw.ActionRow()
        row.set_title(_("前置增益 (dB)"))
        row.set_subtitle(_("负值衰减（防削波）"))
        scale = self._make_scale(-12.0, 0.0, 0.5, "replaygain_preamp_db")
        row.add_suffix(scale)
        group.add(row)

    def _on_replaygain_toggled(self, row, _pspec) -> None:
        self._params["replaygain_enabled"] = bool(row.get_active())
        self._emit()

    def _on_replaygain_mode(self, row, _pspec) -> None:
        self._params["replaygain_mode"] = "album" if row.get_selected() == 1 else "track"
        self._emit()

    # ------------------------------------------------------------
    # 10 段图形 EQ
    # ------------------------------------------------------------
    def _build_eq_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("图形 EQ（10 段）"))
        group.set_description(_("固定中心频率的图示均衡"))
        self.add(group)
        self._camilla_groups.append(group)

        sw = Adw.SwitchRow()
        sw.set_title(_("启用图形 EQ"))
        sw.set_active(bool(self._params.get("eq_enabled", False)))
        sw.connect("notify::active", lambda r, _p: self._on_simple_toggle(r, "eq_enabled"))
        group.add(sw)
        self._eq_switch = sw

        row = Adw.ActionRow()
        row.set_title(_("Q 值"))
        row.set_subtitle(_("各段带宽，越大越窄"))
        row.add_suffix(self._make_scale(0.3, 4.0, 0.1, "eq_q"))
        group.add(row)

        # 竖向滑块横排：每列 = 竖滑块 + 频率标签
        gains = self._params.get("eq_gains", [0.0] * len(EQ_FREQS))
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row_box.set_homogeneous(True)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)
        for i, f in enumerate(EQ_FREQS):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.set_hexpand(True)
            scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL)
            scale.set_range(-12.0, 12.0)
            scale.set_increments(0.5, 3.0)
            scale.set_draw_value(True)
            scale.set_digits(1)
            scale.set_size_request(-1, 180)
            scale.set_valign(Gtk.Align.CENTER)
            scale.set_halign(Gtk.Align.CENTER)
            # 反转方向：上=大（增益）、下=小（衰减）
            scale.set_inverted(True)
            scale.set_value(float(gains[i]) if i < len(gains) else 0.0)
            scale.connect("value-changed", self._on_eq_band_changed, i)
            self._sliders[f"eq_{i}"] = scale
            # 双击恢复该段默认值（0.0）
            self._attach_eq_double_click_reset(scale, i)
            col.append(scale)
            lbl = Gtk.Label(label=(f"{int(f)}" if f < 1000 else f"{int(f / 1000)}k"))
            lbl.add_css_class("dim-label")
            lbl.set_halign(Gtk.Align.CENTER)
            col.append(lbl)
            row_box.append(col)
        group.add(row_box)

    def _on_eq_band_changed(self, scale, i: int) -> None:
        if self._restoring:
            return
        gains = list(self._params.get("eq_gains", [0.0] * len(EQ_FREQS)))
        while len(gains) <= i:
            gains.append(0.0)
        gains[i] = float(scale.get_value())
        self._params["eq_gains"] = gains
        self._emit(debounce=True)

    def _attach_eq_double_click_reset(self, scale, i: int) -> None:
        """图形 EQ 滑块左键双击恢复默认（0.0）。"""
        try:
            gesture = Gtk.GestureClick()
            gesture.set_button(1)
            gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            gesture.connect("pressed", self._on_eq_double_click, scale, i)
            scale.add_controller(gesture)
        except Exception:
            pass

    def _on_eq_double_click(self, gesture, n_press, _x, _y, scale, i: int) -> bool:
        """图形 EQ 滑块双击恢复默认值。"""
        if n_press < 2:
            return False
        try:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except Exception:
            pass
        self._reset_eq_band_to_default(scale, i)
        return True

    def _reset_eq_band_to_default(self, scale, i: int) -> None:
        self._restoring = True
        try:
            gains = list(self._params.get("eq_gains", [0.0] * len(EQ_FREQS)))
            while len(gains) <= i:
                gains.append(0.0)
            gains[i] = 0.0
            self._params["eq_gains"] = gains
            try:
                scale.handler_block_by_func(self._on_eq_band_changed)
                scale.set_value(0.0)
                scale.handler_unblock_by_func(self._on_eq_band_changed)
            except Exception:
                scale.set_value(0.0)
            self._emit()
        finally:
            self._restoring = False

    # ------------------------------------------------------------
    # 参数均衡器（PEQ）
    # ------------------------------------------------------------
    def _build_peq_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("参数均衡器 (PEQ)"))
        group.set_description(_("每段可调频率 / 增益 / Q 值"))
        self.add(group)
        self._peq_group = group
        # PEQ 归 Camilla → 整组随 Camilla 开关置灰
        self._camilla_groups.append(group)
        self._peq_curve = None

        # 开关
        sw_row = Adw.SwitchRow()
        sw_row.set_title(_("启用 PEQ"))
        sw_row.set_active(bool(self._params.get("peq_enabled", False)))
        sw_row.connect("notify::active", self._on_peq_enabled_toggled)
        group.add(sw_row)
        self._peq_switch = sw_row

        # 曲线控件（直接加入 group，避免 ActionRow 拦截鼠标事件）
        from .widgets.peq_curve import PeqCurve
        self._peq_curve = PeqCurve(on_changed=self._on_peq_curve_changed)
        self._peq_curve.set_bands(self._params.get("peq_bands", []))
        self._peq_curve.set_margin_top(4)
        self._peq_curve.set_margin_bottom(4)
        group.add(self._peq_curve)

        # 添加 / 删除频段按钮
        btn_row = Adw.ActionRow()
        btn_row.set_title(_("频段"))
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.set_tooltip_text(_("添加频段"))
        add_btn.connect("clicked", self._on_peq_add_band)
        btn_row.add_suffix(add_btn)
        del_btn = Gtk.Button(icon_name="list-remove-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        del_btn.set_tooltip_text(_("删除最后添加的频段"))
        del_btn.connect("clicked", self._on_peq_del_last_band)
        btn_row.add_suffix(del_btn)
        group.add(btn_row)

        self._peq_band_box = None  # 不再用列表

    def _rebuild_peq_bands(self) -> None:
        """重建 PEQ 频段 UI。"""
        # 清空
        c = self._peq_band_box.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._peq_band_box.remove(c)
            c = nxt
        self._peq_band_rows = []
        bands = self._params.get("peq_bands", [])
        for idx, band in enumerate(bands):
            self._peq_band_box.append(self._make_peq_band_row(idx, band))

    def _make_peq_band_row(self, idx: int, band: dict) -> Gtk.Widget:
        row = Adw.ActionRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        # 类型
        type_dd = Gtk.DropDown()
        tmodel = Gtk.StringList()
        for t in (_("峰值"), _("低架"), _("高架")):
            tmodel.append(t)
        type_dd.set_model(tmodel)
        type_dd.set_selected({"pk": 0, "ls": 1, "hs": 2}.get(band.get("kind", "pk"), 0))
        type_dd.connect("notify::selected", self._on_peq_band_changed, idx, "kind")
        box.append(type_dd)
        # 频率
        f_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        f_scale.set_range(20.0, 20000.0)
        f_scale.set_increments(10.0, 100.0)
        f_scale.set_draw_value(True)
        f_scale.set_digits(0)
        f_scale.set_size_request(140, -1)
        f_scale.set_value(float(band.get("freq", 1000.0)))
        f_scale.connect("value-changed", self._on_peq_band_changed, idx, "freq")
        box.append(f_scale)
        # 增益
        g_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        g_scale.set_range(-18.0, 18.0)
        g_scale.set_increments(0.5, 3.0)
        g_scale.set_draw_value(True)
        g_scale.set_digits(1)
        g_scale.set_size_request(120, -1)
        g_scale.set_value(float(band.get("gain", 0.0)))
        g_scale.connect("value-changed", self._on_peq_band_changed, idx, "gain")
        box.append(g_scale)
        # Q
        q_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        q_scale.set_range(0.1, 10.0)
        q_scale.set_increments(0.1, 1.0)
        q_scale.set_draw_value(True)
        q_scale.set_digits(2)
        q_scale.set_size_request(100, -1)
        q_scale.set_value(float(band.get("q", 1.0)))
        q_scale.connect("value-changed", self._on_peq_band_changed, idx, "q")
        box.append(q_scale)
        # 删除
        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.connect("clicked", self._on_peq_del_band, idx)
        box.append(del_btn)
        row.set_child(box) if hasattr(row, "set_child") else row.add_suffix(box)
        return row

    def _on_peq_band_changed(self, widget, idx: int, field: str) -> None:
        bands = self._params.get("peq_bands", [])
        if 0 <= idx < len(bands):
            if field == "kind":
                bands[idx]["kind"] = ["pk", "ls", "hs"][widget.get_selected()]
            else:
                bands[idx][field] = float(widget.get_value())
            self._params["peq_bands"] = bands
            self._emit()

    def _on_peq_add_band(self, _btn) -> None:
        bands = self._params.get("peq_bands", [])
        bands.append({"freq": 1000.0, "gain": 0.0, "q": 1.0, "kind": "pk"})
        self._params["peq_bands"] = bands
        if self._peq_curve is not None:
            self._peq_curve.set_bands(bands)
        self._emit()

    def _on_peq_del_last_band(self, _btn) -> None:
        bands = self._params.get("peq_bands", [])
        if bands:
            bands.pop()
            self._params["peq_bands"] = bands
            if self._peq_curve is not None:
                self._peq_curve.set_bands(bands)
            self._emit()

    def _on_peq_curve_changed(self, bands: list) -> None:
        """曲线拖动/滚轮后，更新参数并下发。"""
        self._params["peq_bands"] = bands
        self._emit()

    def _on_peq_enabled_toggled(self, row, _pspec) -> None:
        self._params["peq_enabled"] = bool(row.get_active())
        self._emit()

    def _on_bass_enabled_toggled(self, row, _pspec) -> None:
        self._params["bass_enabled"] = bool(row.get_active())
        self._emit()

    def _on_loudness_toggled(self, row, _pspec) -> None:
        self._params["loudness_enabled"] = bool(row.get_active())
        self._emit()

    def _on_headroom_toggled(self, row, _pspec) -> None:
        self._params["headroom_enabled"] = bool(row.get_active())
        self._emit()

    # ------------------------------------------------------------
    # 卷积（IR）
    # ------------------------------------------------------------
    def _build_convolution_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("卷积 (Convolution)"))
        group.set_description(_("加载脉冲响应（IR）文件，如耳机校正 / 房间混响"))
        self.add(group)
        self._convolution_group = group

        # 开关
        sw = Adw.SwitchRow()
        sw.set_title(_("启用卷积"))
        sw.set_active(bool(self._params.get("convolution_enabled", False)))
        sw.connect("notify::active", self._on_convolution_toggled)
        group.add(sw)
        self._convolution_switch = sw

        # 加载 / 清除 IR
        row = Adw.ActionRow()
        row.set_title(_("IR 文件"))
        _init_ir = str(self._params.get("convolution_ir") or "")
        self._conv_ir_label = Gtk.Label(label=(os.path.basename(_init_ir) if _init_ir else _("未加载")))
        self._conv_ir_label.add_css_class("dim-label")
        self._conv_ir_label.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self._conv_ir_label)
        load_btn = Gtk.Button(label=_("加载…"))
        load_btn.set_valign(Gtk.Align.CENTER)
        load_btn.add_css_class("flat")
        load_btn.connect("clicked", self._on_convolution_load)
        row.add_suffix(load_btn)
        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.add_css_class("flat")
        clear_btn.set_tooltip_text(_("清除已加载的 IR"))
        clear_btn.connect("clicked", self._on_convolution_clear)
        row.add_suffix(clear_btn)
        group.add(row)

        # IR 模式：单路（左右用同一路）/ 立体声（左用左、右用右）
        mode_row = Adw.ComboRow()
        mode_row.set_title(_("IR 模式"))
        mode_row.set_subtitle(_("单路：左右用同一路；立体声：左用左、右用右"))
        mode_model = Gtk.StringList()
        for label in (_("单路"), _("立体声（左右分开）")):
            mode_model.append(label)
        mode_row.set_model(mode_model)
        mode_row.set_selected(1 if self._params.get("convolution_stereo_ir", False) else 0)
        mode_row.connect("notify::selected", self._on_convolution_mode)
        group.add(mode_row)
        self._conv_mode_row = mode_row

        # IR 声道（单路模式下有效；官方 ConvParametersWav.channel，0-based）
        ch_row = Adw.ComboRow()
        ch_row.set_title(_("IR 声道"))
        ch_row.set_subtitle(_("单路模式下选哪一路"))
        ch_model = Gtk.StringList()
        for label in (_("左 (0)"), _("右 (1)")):
            ch_model.append(label)
        ch_row.set_model(ch_model)
        _cur_ch = int(self._params.get("convolution_channel", 0) or 0)
        ch_row.set_selected(1 if _cur_ch == 1 else 0)
        ch_row.connect("notify::selected", self._on_convolution_channel)
        group.add(ch_row)
        self._conv_channel_row = ch_row
        # 立体声模式时 IR 声道置灰
        try:
            ch_row.set_sensitive(not self._params.get("convolution_stereo_ir", False))
        except Exception:
            pass

    def _on_convolution_mode(self, row, _pspec) -> None:
        stereo = (row.get_selected() == 1)
        self._params["convolution_stereo_ir"] = bool(stereo)
        # 立体声模式时 IR 声道置灰
        try:
            self._conv_channel_row.set_sensitive(not stereo)
        except Exception:
            pass
        self._emit()

    def _on_convolution_channel(self, row, _pspec) -> None:
        self._params["convolution_channel"] = int(row.get_selected())
        self._emit()

    def _on_convolution_toggled(self, row, _pspec) -> None:
        self._params["convolution_enabled"] = bool(row.get_active())
        self._emit()

    def _on_convolution_load(self, _btn) -> None:
        """选文件 → 通知外部加载 IR（IR 不经 SetDsp，走单独命令）。"""
        dialog = Gtk.FileDialog()
        dialog.set_title(_("选择 IR 文件"))
        # IR 常见后缀：wav / irs；再给一个「所有文件」兜底，
        # 便于加载后缀不标准的 IR（能否解析取决于文件是否为 RIFF/WAV 结构）
        filt_ir = Gtk.FileFilter()
        filt_ir.set_name(_("脉冲响应 (wav, irs)"))
        filt_ir.add_pattern("*.wav")
        filt_ir.add_pattern("*.irs")
        filt_all = Gtk.FileFilter()
        filt_all.set_name(_("所有文件"))
        filt_all.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt_ir)
        filters.append(filt_all)
        dialog.set_filters(filters)
        dialog.set_default_filter(filt_ir)
        dialog.open(self.get_root(), None, self._on_ir_selected)

    def _on_ir_selected(self, dialog, result) -> None:
        try:
            f = dialog.open_finish(result)
        except Exception:
            return  # 用户取消
        if f is None:
            return
        path = f.get_path()
        if not path:
            return
        # 路径写进 params（Camilla YAML 从这里取 convolution_ir）
        self._params["convolution_ir"] = path
        self.set_convolution_ir_label(path)
        # 选了 IR 就自动启用卷积开关（否则装了 IR 也不会生效）
        if not self._params.get("convolution_enabled", False):
            self._params["convolution_enabled"] = True
            try:
                self._convolution_switch.handler_block_by_func(self._on_convolution_toggled)
                self._convolution_switch.set_active(True)
                self._convolution_switch.handler_unblock_by_func(self._on_convolution_toggled)
            except Exception:
                pass
        # 通知外部（写入 config），并下发一次让 Camilla 重建管线
        if self._on_convolution_ir is not None:
            try:
                self._on_convolution_ir(path)
            except Exception:
                pass
        self._emit()

    def set_convolution_ir_label(self, path: str) -> None:
        """外部（如启动恢复）同步 IR 文件名显示。"""
        name = os.path.basename(path) if path else _("未加载")
        self._conv_ir_label.set_text(name or _("未加载"))

    def _on_convolution_clear(self, _btn) -> None:
        self._conv_ir_label.set_text(_("未加载"))
        # 清 params 里的路径，避免 YAML 仍引用旧 IR
        self._params["convolution_ir"] = ""
        self._params["convolution_enabled"] = False
        try:
            self._convolution_switch.handler_block_by_func(self._on_convolution_toggled)
            self._convolution_switch.set_active(False)
            self._convolution_switch.handler_unblock_by_func(self._on_convolution_toggled)
        except Exception:
            pass
        if self._on_convolution_cleared is not None:
            self._on_convolution_cleared()
        self._emit()

    # ------------------------------------------------------------
    # 低音 / 高音
    # ------------------------------------------------------------
    def _build_bass_treble_group(self) -> None:
        """低音 / 高音增强（基础）。"""
        group = Adw.PreferencesGroup()
        group.set_title(_("低音 / 高音"))
        self.add(group)
        # 低音/高音归 Camilla → 整组随 Camilla 开关置灰
        self._camilla_groups.append(group)

        # 低音开关
        sw = Adw.SwitchRow()
        sw.set_title(_("启用低音增强"))
        sw.set_active(bool(self._params.get("bass_enabled", False)))
        sw.connect("notify::active", self._on_bass_enabled_toggled)
        group.add(sw)
        self._bass_switch = sw

        row = Adw.ActionRow()
        row.set_title(_("低音增益 (dB)"))
        scale = self._make_scale(-12.0, 12.0, 0.5, "bass_gain_db")
        row.add_suffix(scale)
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("低音频率 (Hz)"))
        scale = self._make_scale(40.0, 400.0, 10.0, "bass_freq")
        row.add_suffix(scale)
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("高音增益 (dB)"))
        scale = self._make_scale(-12.0, 12.0, 0.5, "treble_gain_db")
        row.add_suffix(scale)
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("高音频率 (Hz)"))
        scale = self._make_scale(2000.0, 16000.0, 500.0, "treble_freq")
        row.add_suffix(scale)
        group.add(row)

    def _build_loudness_group(self) -> None:
        """Loudness 等响度补偿（高级）。"""
        group = Adw.PreferencesGroup()
        group.set_title(_("响度补偿 (Loudness)"))
        self.add(group)
        # 注：Loudness 由 Rust 动态实现（随播放器音量补偿两端），
        # 不归 Camilla，故不随 Camilla 开关置灰。

        lsw = Adw.SwitchRow()
        lsw.set_title(_("启用响度补偿"))
        lsw.set_subtitle(_("随音量动态补偿低/高频（小音量时更明显）"))
        lsw.set_active(bool(self._params.get("loudness_enabled", False)))
        lsw.connect("notify::active", self._on_loudness_toggled)
        group.add(lsw)
        self._loudness_switch = lsw

        row = Adw.ActionRow()
        row.set_title(_("响度强度"))
        scale = self._make_scale(0.0, 1.0, 0.05, "loudness_amount")
        row.add_suffix(scale)
        group.add(row)

    # ------------------------------------------------------------
    # 立体声
    # ------------------------------------------------------------
    def _build_stereo_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("立体声"))
        self.add(group)

        # 立体声宽度（独立开关）
        sw = Adw.SwitchRow()
        sw.set_title(_("启用立体声宽度"))
        sw.set_active(bool(self._params.get("width_enabled", False)))
        sw.connect("notify::active", lambda r, _p: self._on_simple_toggle(r, "width_enabled"))
        group.add(sw)

        row = Adw.ActionRow()
        row.set_title(_("宽度"))
        row.set_subtitle(_("0=单声道, 1=原始, 2=加宽"))
        scale = self._make_scale(0.0, 2.0, 0.05, "stereo_width")
        row.add_suffix(scale)
        group.add(row)

        # 左右平衡：无独立开关，滑块直接生效
        row = Adw.ActionRow()
        row.set_title(_("平衡"))
        row.set_subtitle(_("-1=全左, 0=居中, 1=全右"))
        scale = self._make_scale(-1.0, 1.0, 0.05, "balance")
        row.add_suffix(scale)
        group.add(row)

    def _on_simple_toggle(self, row, key: str) -> None:
        """通用布尔开关回调。"""
        self._params[key] = bool(row.get_active())
        self._emit()

    # ------------------------------------------------------------
    # Crossfeed（耳机串扰）
    # ------------------------------------------------------------
    def _build_reverb_group(self) -> None:
        """混响（Rust 独有：Freeverb + 预延迟 + 低切）。"""
        group = Adw.PreferencesGroup()
        group.set_title(_("混响 (Reverb)"))
        group.set_description(_("高质量 Freeverb：预延迟 / 衰减 / 阻尼 / 低切"))
        self.add(group)

        sw = Adw.SwitchRow()
        sw.set_title(_("启用混响"))
        sw.set_active(bool(self._params.get("reverb_enabled", False)))
        sw.connect("notify::active", lambda r, _p: self._on_simple_toggle(r, "reverb_enabled"))
        group.add(sw)
        self._reverb_switch = sw

        row = Adw.ActionRow()
        row.set_title(_("干湿比"))
        row.set_subtitle(_("0=全干, 1=全湿"))
        row.add_suffix(self._make_scale(0.0, 1.0, 0.05, "reverb_mix"))
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("预延迟 (ms)"))
        row.set_subtitle(_("干声与混响的间隔，越大越清晰"))
        row.add_suffix(self._make_scale(0.0, 100.0, 1.0, "reverb_pre_delay_ms"))
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("衰减"))
        row.set_subtitle(_("房间大小感，越大混响越长"))
        row.add_suffix(self._make_scale(0.0, 1.0, 0.05, "reverb_decay"))
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("阻尼"))
        row.set_subtitle(_("高频衰减，越大越闷"))
        row.add_suffix(self._make_scale(0.0, 1.0, 0.05, "reverb_damping"))
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("立体声宽度"))
        row.add_suffix(self._make_scale(0.0, 1.0, 0.05, "reverb_width"))
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("低切 (Hz)"))
        row.set_subtitle(_("去掉混响低频浑浊"))
        row.add_suffix(self._make_scale(20.0, 500.0, 10.0, "reverb_low_cut_hz"))
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("调制"))
        row.set_subtitle(_("打散金属感，让混响更自然"))
        row.add_suffix(self._make_scale(0.0, 1.0, 0.05, "reverb_mod_depth"))
        group.add(row)

    def _build_crossfeed_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("耳机串扰 (Crossfeed)"))
        group.set_description(_("模拟声音绕过头部，减少「头中效应」"))
        self.add(group)

        sw = Adw.SwitchRow()
        sw.set_title(_("启用 Crossfeed"))
        sw.set_active(bool(self._params.get("crossfeed_enabled", False)))
        sw.connect("notify::active", self._on_crossfeed_toggled)
        group.add(sw)
        self._crossfeed_switch = sw

        row = Adw.ActionRow()
        row.set_title(_("强度"))
        scale = self._make_scale(0.0, 1.0, 0.05, "crossfeed_amount")
        row.add_suffix(scale)
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("延迟 (ms)"))
        scale = self._make_scale(0.1, 1.0, 0.05, "crossfeed_delay_ms")
        row.add_suffix(scale)
        group.add(row)

    def _on_crossfeed_toggled(self, row, _pspec) -> None:
        self._params["crossfeed_enabled"] = bool(row.get_active())
        self._emit()

    # ------------------------------------------------------------
    # 相位 / 通道矩阵（后置路由）
    # ------------------------------------------------------------
    def _build_routing_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("相位 / 通道"))
        self.add(group)

        # 相位翻转
        sw = Adw.SwitchRow()
        sw.set_title(_("全局相位翻转"))
        sw.set_subtitle(_("整段反相（180°）"))
        sw.set_active(bool(self._params.get("phase_invert", False)))
        sw.connect("notify::active", lambda r, _p: self._on_simple_toggle(r, "phase_invert"))
        group.add(sw)

        # 通道矩阵
        row = Adw.ComboRow()
        row.set_title(_("通道矩阵"))
        row.set_subtitle(_("声道混音 / 交换 / 映射"))
        model = Gtk.StringList()
        for label in (_("关闭"), _("左右交换"), _("单声道"), _("左→双声道"), _("右→双声道")):
            model.append(label)
        row.set_model(model)
        cm_map = ["off", "swap", "mono", "left_both", "right_both"]
        cur = str(self._params.get("channel_matrix", "off"))
        row.set_selected(cm_map.index(cur) if cur in cm_map else 0)
        row.connect("notify::selected", self._on_matrix_changed)
        group.add(row)

    def _on_matrix_changed(self, row, _pspec) -> None:
        cm = ["off", "swap", "mono", "left_both", "right_both"]
        idx = row.get_selected()
        self._params["channel_matrix"] = cm[idx] if 0 <= idx < len(cm) else "off"
        self._emit()

    # ------------------------------------------------------------
    # 压缩器
    # ------------------------------------------------------------
    def _build_compressor_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("压缩器 (Compressor)"))
        group.set_description(_("压大声、提小声，让响度更平稳"))
        self.add(group)

        sw = Adw.SwitchRow()
        sw.set_title(_("启用压缩器"))
        sw.set_active(bool(self._params.get("compressor_enabled", False)))
        sw.connect("notify::active", self._on_compressor_toggled)
        group.add(sw)
        self._compressor_switch = sw

        row = Adw.ActionRow()
        row.set_title(_("阈值 (dBFS)"))
        scale = self._make_scale(-40.0, 0.0, 1.0, "compressor_threshold_db")
        row.add_suffix(scale)
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("压缩比"))
        scale = self._make_scale(1.0, 20.0, 0.5, "compressor_ratio")
        row.add_suffix(scale)
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("增益补偿 (dB)"))
        scale = self._make_scale(0.0, 12.0, 0.5, "compressor_makeup_db")
        row.add_suffix(scale)
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("启动 (s)"))
        row.set_subtitle(_("越小反应越快"))
        row.add_suffix(self._make_scale(0.001, 0.2, 0.001, "compressor_attack"))
        group.add(row)

        row = Adw.ActionRow()
        row.set_title(_("释放 (s)"))
        row.set_subtitle(_("越大恢复越慢"))
        row.add_suffix(self._make_scale(0.01, 1.0, 0.01, "compressor_release"))
        group.add(row)

    def _on_compressor_toggled(self, row, _pspec) -> None:
        self._params["compressor_enabled"] = bool(row.get_active())
        self._emit()

    # ------------------------------------------------------------
    # 限幅器
    # ------------------------------------------------------------
    def _build_limiter_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title(_("限幅器"))
        group.set_description(_("防止削波爆音"))
        self.add(group)

        row = Adw.SwitchRow()
        row.set_title(_("启用限幅器"))
        row.set_active(bool(self._params.get("limiter_enabled", False)))
        row.connect("notify::active", self._on_limiter_toggled)
        group.add(row)
        self._limiter_row = row

        row = Adw.ActionRow()
        row.set_title(_("限幅阈值 (dBFS)"))
        scale = self._make_scale(-12.0, 0.0, 0.5, "limiter_threshold_db")
        row.add_suffix(scale)
        group.add(row)

        sw_soft = Adw.SwitchRow()
        sw_soft.set_title(_("软削波 (soft clip)"))
        sw_soft.set_subtitle(_("峰值平滑过渡，减少高频毛刺（会轻微染色）"))
        sw_soft.set_active(bool(self._params.get("limiter_soft_clip", False)))
        sw_soft.connect("notify::active", lambda r, _p: self._on_simple_toggle(r, "limiter_soft_clip"))
        group.add(sw_soft)

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    def _make_scale(self, lo: float, hi: float, step: float, key: str) -> Gtk.Scale:
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        scale.set_range(lo, hi)
        scale.set_increments(step, step * 4)
        scale.set_draw_value(True)
        scale.set_digits(2)
        scale.set_size_request(220, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_value(float(self._params.get(key, 0.0)))
        scale.connect("value-changed", self._on_param_changed, key)
        self._sliders[key] = scale
        # 双击滑块：恢复该参数默认值
        self._attach_double_click_reset(scale, key)
        return scale

    def _attach_double_click_reset(self, scale, key: str) -> None:
        """给滑块加左键双击恢复默认。

        Gtk.Scale 会吞掉普通阶段的手势，故用 CAPTURE 阶段抢先拿到
        双击事件；双击不触发拖动定位，能可靠恢复默认值。
        """
        try:
            gesture = Gtk.GestureClick()
            gesture.set_button(1)
            gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            gesture.connect("pressed", self._on_scale_double_click, scale, key)
            scale.add_controller(gesture)
        except Exception:
            pass

    def _on_scale_double_click(self, gesture, n_press, _x, _y, scale, key: str) -> bool:
        """双击（n_press>=2）恢复滑块默认值。"""
        if n_press < 2:
            return False
        try:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except Exception:
            pass
        self._reset_scale_to_default(scale, key)
        return True

    def _reset_scale_to_default(self, scale, key: str) -> None:
        """把滑块恢复为该参数默认值并下发。"""
        defaults = _default_params()
        default = defaults.get(key, 0.0)
        try:
            default = float(default)
        except (TypeError, ValueError):
            return
        self._restoring = True
        try:
            self._params[key] = default
            try:
                scale.handler_block_by_func(self._on_param_changed)
                scale.set_value(default)
                scale.handler_unblock_by_func(self._on_param_changed)
            except Exception:
                scale.set_value(default)
        finally:
            self._restoring = False
        # 在 restoring 结束后再下发，避免下游同步打断恢复
        self._emit()

    def _sync_sliders(self) -> None:
        """把当前参数刷新到滑块（不触发回调）。"""
        for key, scale in self._sliders.items():
            if key.startswith("eq_") and key[3:].isdigit():
                # 图形 EQ 各段（eq_0..eq_9）；eq_q 等非数字后缀走普通参数
                i = int(key[3:])
                gains = self._params.get("eq_gains", [])
                val = float(gains[i]) if i < len(gains) else 0.0
                cb = self._on_eq_band_changed
            else:
                val = float(self._params.get(key, 0.0))
                cb = self._on_param_changed
            try:
                scale.handler_block_by_func(cb)
                scale.set_value(val)
                scale.handler_unblock_by_func(cb)
            except Exception:
                # 回调未连接或参数越界：直接设值，不让 UI 崩溃
                try:
                    scale.set_value(val)
                except Exception:
                    pass

    # ------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------
    def _on_master_toggled(self, switch, _pspec) -> None:
        self._params["enabled"] = bool(switch.get_active())
        self._update_camilla_sensitivity()
        self._emit()

    def _update_camilla_sensitivity(self) -> None:
        """刷新置灰状态：DSP 总开关关闭时，所有功能组置灰。"""
        enabled = bool(self._params.get("enabled", False))
        for g in getattr(self, "_camilla_groups", []) or []:
            try:
                g.set_sensitive(enabled)
            except Exception:
                pass

    def _on_reset(self, _btn) -> None:
        """重置：所有模块开关关闭、参数归默认（总开关状态不变）。"""
        keep_enabled = bool(self._params.get("enabled", False))
        self._params = _default_params()
        # 所有模块开关关闭
        self._params["peq_enabled"] = False
        self._params["convolution_enabled"] = False
        self._params["bass_enabled"] = False
        self._params["loudness_enabled"] = False
        self._params["headroom_enabled"] = False
        self._params["crossfeed_enabled"] = False
        self._params["compressor_enabled"] = False
        self._params["limiter_enabled"] = False
        self._params["camilla_enabled"] = False
        # 总开关保持
        self._params["enabled"] = keep_enabled
        self._sync_sliders()
        self._update_camilla_sensitivity()
        # 刷新 PEQ 曲线
        if getattr(self, "_peq_curve", None) is not None:
            self._peq_curve.set_bands(self._params.get("peq_bands", []))
        # 同步所有模块开关到界面（关闭），总开关不动
        # mode 只建部分开关，用属性名 + getattr 取，未建的跳过。
        for sw_name, key, cb_name in (
            ("_peq_switch", "peq_enabled", "_on_peq_enabled_toggled"),
            ("_bass_switch", "bass_enabled", "_on_bass_enabled_toggled"),
            ("_loudness_switch", "loudness_enabled", "_on_loudness_toggled"),
            ("_headroom_switch", "headroom_enabled", "_on_headroom_toggled"),
            ("_crossfeed_switch", "crossfeed_enabled", "_on_crossfeed_toggled"),
            ("_compressor_switch", "compressor_enabled", "_on_compressor_toggled"),
            ("_limiter_row", "limiter_enabled", "_on_limiter_toggled"),
        ):
            sw = getattr(self, sw_name, None)
            cb = getattr(self, cb_name, None)
            if sw is None or cb is None:
                continue
            try:
                sw.handler_block_by_func(cb)
                sw.set_active(False)
                sw.handler_unblock_by_func(cb)
            except Exception:
                pass
        self._emit()
        # 同步清空「当前音效预设」记录：重置后实际已是默认状态，
        # 若保留旧的 effect_preset，音效对话框会假高亮上次选的预设，
        # 与实际听感不一致。此处清掉，高亮回到「关闭」。
        try:
            from config.settings import get_config
            get_config().set("effect_preset", "")
        except Exception:
            pass
        # 提示已重置
        try:
            dlg = Adw.MessageDialog(
                transient_for=self.get_root(),
                heading="已重置",
                body="所有参数已重置为初始设置。",
            )
            dlg.add_response("ok", "知道了")
            dlg.set_default_response("ok")
            dlg.present()
        except Exception:
            pass

    def _on_limiter_toggled(self, row, _pspec) -> None:
        self._params["limiter_enabled"] = bool(row.get_active())
        self._emit()

    def _on_param_changed(self, scale, key: str) -> None:
        if self._restoring:
            return
        self._params[key] = float(scale.get_value())
        # 滑块连续拖动：防抖下发，避免每动一下都重建管线（声音一顿一顿）
        self._emit(debounce=True)

    def _emit(self, debounce: bool = False) -> None:
        """下发参数。

        debounce=True：延迟 80ms 合并多次拖动（滑块用），避免频繁重建管线；
        debounce=False：立即下发（开关/按钮用）。
        """
        if self._on_dsp_changed is None:
            return
        if not debounce:
            # 立即：若有挂起的定时器先取消，避免稍后又发一次旧值
            if getattr(self, "_emit_timer", None) is not None:
                try:
                    from gi.repository import GLib
                    GLib.source_remove(self._emit_timer)
                except Exception:
                    pass
                self._emit_timer = None
            self._on_dsp_changed(dict(self._params))
            return
        # 防抖
        try:
            from gi.repository import GLib
            if getattr(self, "_emit_timer", None) is not None:
                GLib.source_remove(self._emit_timer)
            self._emit_timer = GLib.timeout_add(80, self._emit_now)
        except Exception:
            # 无 GLib：退化为立即
            self._on_dsp_changed(dict(self._params))

    def _emit_now(self) -> bool:
        self._emit_timer = None
        if self._on_dsp_changed is not None:
            self._on_dsp_changed(dict(self._params))
        return False

    def params(self) -> dict:
        return dict(self._params)
