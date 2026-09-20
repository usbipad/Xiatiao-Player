"""高级 DSP 配置窗口（独立弹窗，非模态）。

页面：
- 「DSP 链路」：按音频链路顺序的完整 DSP 排布（复用 EffectPage）。
- 「音色染色」：电子管 / BBE（独立于 YAML 的染色路径）。
- 「预设管理」：整套 DSP 配置的保存 / 加载 / 删除。

注意：Adw.PreferencesWindow（AdwWindow）不支持 set_titlebar()，
调用会触发 "gtk_window_set_titlebar() is not supported for AdwWindow" 并 abort。
它自带搜索栏 + Esc 关闭，非模态下也能直接关。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from gi.repository import Adw, GLib, Gtk

from core.i18n import _

log = logging.getLogger(__name__)


class AdvancedDspWindow(Adw.PreferencesWindow):
    """高级 DSP 配置窗口。"""

    def __init__(self, parent=None, params: Optional[dict] = None,
                 on_dsp_changed: Optional[Callable[[dict], None]] = None,
                 on_coloring: Optional[Callable[[float, float], None]] = None,
                 on_viz_changed: Optional[Callable[[], None]] = None,
                 on_open_viz_window: Optional[Callable[[], None]] = None,
                 on_convolution_ir: Optional[Callable[[str], None]] = None,
                 on_convolution_cleared: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        if parent is not None:
            self.set_transient_for(parent)
        self.set_modal(False)
        self.set_title(_("高级 DSP 设置"))
        self.set_default_size(760, 640)
        self._params = dict(params or {})
        self._on_dsp_changed = on_dsp_changed
        self._on_coloring = on_coloring
        self._on_viz_changed = on_viz_changed
        self._on_open_viz_window = on_open_viz_window
        self._on_convolution_ir = on_convolution_ir
        self._on_convolution_cleared = on_convolution_cleared
        self._emit_timer = None
        # 刷新 UI（_on_dsp_state_changed）期间为 True：抑制控件回调触发下发。
        self._syncing = False
        # ---- DSP 单一真相源（S4）----
        # 各功能页 / 染色页不再各持独立 _params 并手工同步；统一走 DspState：
        # 改参数 → DspState.patch/replace → 广播 → 各页自动刷新 + window 统一下发。
        self._dsp_state = None
        try:
            from core.dsp_state import get_dsp_state
            self._dsp_state = get_dsp_state()
            self._dsp_state.connect("changed", self._on_dsp_state_changed)
            self._dsp_state.connect("replaced", self._on_dsp_state_changed)
        except Exception:
            log.debug("订阅 DspState 失败", exc_info=True)
            self._dsp_state = None

        # 每个功能一个独立页，按音频链路顺序（标题单列在顶部）
        self._build_feature_pages()
        # 染色（电子管 / BBE）：独立于 YAML 的染色路径，单独一页
        self._build_coloring_page()
        # 预设管理
        self._build_presets_page()

        # 注：不再订阅 EffectState.reset —— DspState 广播已覆盖所有变更来源
        # （预设 / 重置 / 其它视图），_on_dsp_state_changed 统一刷新。

    # ------------------------------------------------------------
    # 每功能一页（按音频链路顺序）
    # ------------------------------------------------------------
    #: (页标题, [only 键...], 图标)
    _FEATURE_PAGES = (
        ("ReplayGain / 增益", ["replaygain", "gain"], "audio-volume-high-symbolic"),
        ("均衡器（图形 EQ / 参数 EQ）", ["eq", "peq"], "multimedia-equalizer-symbolic"),
        ("卷积 / 音调", ["convolution", "bass_treble"], "audio-input-microphone-symbolic"),
        ("响度 / 动态", ["loudness", "compressor", "limiter"], "audio-volume-high-symbolic"),
        ("空间 / 声道", ["stereo", "crossfeed", "routing", "reverb"], "audio-stereo-symbolic"),
    )

    def _build_feature_pages(self) -> None:
        """按分组构建功能页（一个页可含多个组）。"""
        from .effect_page import EffectPage
        self._feature_pages = []
        for title, only_list, icon in self._FEATURE_PAGES:
            try:
                page = EffectPage(
                    on_dsp_changed=self._make_page_handler(list(only_list)),
                    initial=dict(self._params),
                    on_convolution_ir=self._on_convolution_ir,
                    on_convolution_cleared=self._on_convolution_cleared,
                    mode="single",
                    only=list(only_list),
                )
                page.set_title(_(title))
                page.set_icon_name(icon)
                self.add(page)
                self._feature_pages.append(page)
            except Exception as exc:
                log.debug("构建功能页 %s 失败: %s", title, exc, exc_info=True)
        # 总开关在第一个功能页里；它只刷新本页各组，不会通知染色页。
        # 这里额外挂一个回调，让染色组随总开关一起置灰 / 取消灰。
        try:
            if self._feature_pages:
                master = getattr(self._feature_pages[0], "_master_switch", None)
                if master is not None:
                    master.connect("notify::active", self._on_master_active_changed)
        except Exception:
            log.debug("挂载染色组联动失败", exc_info=True)

    def _on_master_active_changed(self, switch, _pspec) -> None:
        """总开关切换：同步 advanced 窗口侧的染色组置灰。"""
        try:
            self._params["enabled"] = bool(switch.get_active())
        except Exception:
            pass
        self._update_coloring_sensitivity()

    def _make_page_handler(self, groups: list):
        """给某功能页生成回调：只合并该页负责字段，避免其它页字段被空值覆盖。

        各功能页各持一份 _params 快照，未涉字段默认空；若整份合并，
        会把别的页刚设好的值（如卷积 IR 路径）覆盖成空。故按组过滤。
        """
        from .effect_page import EffectPage as _EP
        fields = set()
        for g in groups:
            fields.update(_EP._GROUP_FIELDS.get(g, []))

        def _handler(params: dict) -> None:
            for k in fields:
                if k in params:
                    self._params[k] = params[k]
            self._emit()
        return _handler

    def _on_chain_changed(self, params: dict) -> None:
        """兼容旧回调：整份合并（仅染色等非分页场景用）。"""
        self._params.update(params)
        self._emit()

    def _emit(self, *, immediate: bool = False, clear_mark: bool = True) -> None:
        """下发参数。

        默认防抖（拖滑块时合并，避免频繁重建 Pipeline 导致音频颤动）；
        immediate=True 时立即下发（选预设等一次性操作，需保证下发顺序）。
        clear_mark=True（默认，手动改）：清空音效标记；加载预设传 False。
        """
        self._pending_clear_mark = clear_mark
        if self._emit_timer is not None:
            try:
                GLib.source_remove(self._emit_timer)
            except Exception:
                pass
            self._emit_timer = None
        if immediate:
            self._emit_now()
            return
        self._emit_timer = GLib.timeout_add(120, self._emit_now)

    def _emit_now(self) -> bool:
        self._emit_timer = None
        # 重构后：统一写 DspState（唯一真相源）→ 广播 → window 统一下发。
        if getattr(self, "_dsp_state", None) is not None:
            try:
                self._dsp_state.replace(dict(self._params), source=self)
                return False
            except Exception:
                log.debug("写 DspState 失败，回退旧路径", exc_info=True)
        # 回退：无 DspState 时走旧直调回调（兼容/降级）
        if self._on_dsp_changed is not None:
            clear = getattr(self, "_pending_clear_mark", True)
            try:
                self._on_dsp_changed(dict(self._params), clear_mark=clear, immediate=True)
            except TypeError:
                try:
                    self._on_dsp_changed(dict(self._params), clear_mark=clear)
                except TypeError:
                    self._on_dsp_changed(dict(self._params))
        return False

    def _on_dsp_state_changed(self, _state, params) -> None:
        """DspState 变更 → 从单一源刷新本窗口各功能页 + 染色页（只读）。

        替代旧的 apply_external_params 手工同步：无论变更来自本窗口、
        主界面选音效、设置页还是重置，都会到达这里统一刷新。
        """
        if not isinstance(params, dict):
            return
        from .effect_page import _default_params as _ep_defaults
        defaults = _ep_defaults()
        base = dict(defaults)
        base.update(params)
        self._params = base
        self._syncing = True
        try:
            for page in getattr(self, "_feature_pages", []) or []:
                try:
                    p = dict(defaults)
                    p.update(params)
                    page._params = p
                    page.refresh_from_params()
                except Exception:
                    log.debug("刷新功能页失败", exc_info=True)
            try:
                if getattr(self, "_tube_switch", None) is not None:
                    self._tube_switch.set_active(bool(self._params.get("tube_enabled", False)))
                if getattr(self, "_bbe_switch", None) is not None:
                    self._bbe_switch.set_active(bool(self._params.get("bbe_enabled", False)))
                for key, sc in (getattr(self, "_coloring_scales", {}) or {}).items():
                    try:
                        sc.set_value(float(self._params.get(key, 0.0)))
                    except Exception:
                        pass
                self._update_coloring_sensitivity()
            except Exception:
                log.debug("刷新染色页失败", exc_info=True)
        finally:
            self._syncing = False

    def _patch_state(self, changes: dict) -> None:
        """写 DspState（唯一真相源）。染色参数也写进去，保持单一源完整。"""
        if getattr(self, "_dsp_state", None) is not None:
            try:
                self._dsp_state.patch(changes, source=self)
                return
            except Exception:
                log.debug("patch DspState 失败", exc_info=True)
        # 回退：直接改本地 _params 并下发
        self._params.update(changes)
        self._emit()

    # ------------------------------------------------------------
    # 音色染色（电子管 / BBE）
    # ------------------------------------------------------------
    def _build_coloring_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title(_("音色染色"))
        page.set_icon_name("applications-multimedia-symbolic")

        # 电子管（自研 DSP）
        g2 = Adw.PreferencesGroup()
        g2.set_title(_("电子管偶次谐波"))
        g2.set_description(_("非对称软饱和，温暖音色（过采样）"))
        sw2 = Adw.SwitchRow()
        sw2.set_title(_("启用电子管"))
        sw2.set_active(bool(self._params.get("tube_enabled", False)))
        sw2.connect("notify::active", self._on_tube_toggled)
        g2.add(sw2)
        self._tube_switch = sw2
        r2 = Adw.ActionRow()
        r2.set_title(_("驱动量"))
        r2.add_suffix(self._make_scale(1.0, 3.0, 0.1, "tube_drive"))
        g2.add(r2)
        page.add(g2)

        # BBE（自研 DSP）
        g3 = Adw.PreferencesGroup()
        g3.set_title(_("BBE Sonic Maximizer"))
        g3.set_description(_("分频 + 高频相位超前，提升清晰度（过采样）"))
        sw3 = Adw.SwitchRow()
        sw3.set_title(_("启用 BBE"))
        sw3.set_active(bool(self._params.get("bbe_enabled", False)))
        sw3.connect("notify::active", self._on_bbe_toggled)
        g3.add(sw3)
        self._bbe_switch = sw3
        r3 = Adw.ActionRow()
        r3.set_title(_("强度"))
        r3.add_suffix(self._make_scale(0.0, 1.0, 0.05, "bbe_amount"))
        g3.add(r3)
        page.add(g3)

        self.add(page)
        # 染色归 Rust DSP 链（受「启用音频处理」总开关控制）：
        # 总开关关闭时置灰。存下组引用，供加载预设后刷新置灰状态。
        self._coloring_groups = [g2, g3]
        self._update_coloring_sensitivity()

    def _update_coloring_sensitivity(self) -> None:
        """按「启用音频处理」总开关刷新染色组置灰状态。"""
        enabled = bool(self._params.get("enabled", False))
        for g in getattr(self, "_coloring_groups", []) or []:
            try:
                g.set_sensitive(enabled)
            except Exception:
                pass

    def _make_scale(self, lo: float, hi: float, step: float, key: str):
        sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        sc.set_range(lo, hi)
        sc.set_increments(step, step * 4)
        sc.set_draw_value(True)
        sc.set_digits(2)
        sc.set_size_request(220, -1)
        sc.set_valign(Gtk.Align.CENTER)
        sc.set_value(float(self._params.get(key, 0.0)))
        sc.connect("value-changed", self._on_scale_changed, key)
        # 存引用：重置广播后需要把滑块拉回默认值
        if not hasattr(self, "_coloring_scales"):
            self._coloring_scales = {}
        self._coloring_scales[key] = sc
        return sc

    def _on_scale_changed(self, scale, key: str) -> None:
        if getattr(self, "_syncing", False):
            return
        self._params[key] = float(scale.get_value())
        # 染色参数：写单一源（保留在 dsp_params 内），并走独立命令下发。
        if key in ("tube_drive", "bbe_amount"):
            self._patch_state({key: self._params[key]})
            self._emit_coloring()
        else:
            self._emit()

    def _on_tube_toggled(self, row, _pspec) -> None:
        if getattr(self, "_syncing", False):
            return
        self._params["tube_enabled"] = bool(row.get_active())
        self._patch_state({"tube_enabled": self._params["tube_enabled"]})
        self._emit_coloring()

    def _on_bbe_toggled(self, row, _pspec) -> None:
        if getattr(self, "_syncing", False):
            return
        self._params["bbe_enabled"] = bool(row.get_active())
        self._patch_state({"bbe_enabled": self._params["bbe_enabled"]})
        self._emit_coloring()

    def _emit_coloring(self) -> None:
        """染色参数走独立回调（不经 YAML）。"""
        td = float(self._params.get("tube_drive", 1.0)) if self._params.get("tube_enabled") else 0.0
        ba = float(self._params.get("bbe_amount", 0.5)) if self._params.get("bbe_enabled") else 0.0
        if self._on_coloring is not None:
            self._on_coloring(td, ba)

    # ------------------------------------------------------------
    # 预设管理
    # ------------------------------------------------------------
    def _build_presets_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title(_("预设管理"))
        page.set_icon_name("document-save-symbolic")

        group = Adw.PreferencesGroup()
        group.set_title(_("DSP 预设"))
        group.set_description(_("保存/加载整套 DSP 配置（EQ / 卷积 / 压缩 / 染色…）"))

        row = Adw.ActionRow()
        row.set_title(_("预设"))
        self._preset_dropdown = Gtk.DropDown()
        self._preset_dropdown.set_valign(Gtk.Align.CENTER)
        self._reload_presets()
        row.add_suffix(self._preset_dropdown)
        # 保存
        save_btn = Gtk.Button(icon_name="document-save-symbolic")
        save_btn.set_valign(Gtk.Align.CENTER)
        save_btn.add_css_class("flat")
        save_btn.set_tooltip_text(_("保存当前配置为预设"))
        save_btn.connect("clicked", self._on_preset_save)
        row.add_suffix(save_btn)
        # 加载
        load_btn = Gtk.Button(icon_name="document-open-symbolic")
        load_btn.set_valign(Gtk.Align.CENTER)
        load_btn.add_css_class("flat")
        load_btn.set_tooltip_text(_("加载选中预设"))
        load_btn.connect("clicked", self._on_preset_load)
        row.add_suffix(load_btn)
        # 删除
        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        del_btn.set_tooltip_text(_("删除选中预设"))
        del_btn.connect("clicked", self._on_preset_delete)
        row.add_suffix(del_btn)
        group.add(row)
        page.add(group)
        self.add(page)

    def _reload_presets(self) -> None:
        from core.dsp_store import get_dsp_preset_store
        store = get_dsp_preset_store()
        names = store.names()
        self._preset_names = names
        model = Gtk.StringList()
        for n in names:
            model.append(n)
        self._preset_dropdown.set_model(model)
        if names:
            self._preset_dropdown.set_selected(0)

    def _on_preset_save(self, _btn) -> None:
        """弹命名框，保存当前 params。"""
        dialog = Adw.MessageDialog(
            transient_for=self, heading="保存预设",
            body="输入预设名称（保存当前整套 DSP 配置）",
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("预设名称"))
        entry.set_margin_top(6)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "取消")
        dialog.add_response("ok", "保存")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.connect("response", self._on_preset_save_response, entry)
        dialog.present()

    def _on_preset_save_response(self, dialog, response, entry) -> None:
        if response == "ok":
            name = entry.get_text().strip()
            if name:
                from core.dsp_store import get_dsp_preset_store
                get_dsp_preset_store().put(name, dict(self._params))
                self._reload_presets()
        dialog.destroy()

    def _on_preset_load(self, _btn) -> None:
        from core.dsp_store import get_dsp_preset_store
        idx = self._preset_dropdown.get_selected()
        if not (hasattr(self, "_preset_names") and 0 <= idx < len(self._preset_names)):
            return
        name = self._preset_names[idx]
        params = get_dsp_preset_store().get(name)
        if params:
            # 加载预设采用「默认基底 + 预设覆盖」语义：
            # 预设里没有的功能回到默认（关闭），而不是保留当前旧值，
            # 避免预设未启用却显示为开启。
            from .effect_page import _default_params as _ep_defaults
            defaults = _ep_defaults()
            base = dict(self._params)   # 保留窗口其它字段
            base.update(defaults)       # 功能字段先归默认
            # 染色字段不在一级默认里，单独归默认（关闭），
            # 保证预设未含染色时也回到关闭，而非沿用旧状态。
            base["tube_enabled"] = False
            base["bbe_enabled"] = False
            base.update(params)         # 再套预设
            self._params = base
            # 立即下发且不清空音效标记（clear_mark=False）：因为这次是「加载
            # 预设」，参数即预设内容，随后由 set_current(name) 记录预设名。
            self._emit(immediate=True, clear_mark=False)
            # 同步到各功能页并刷新 UI：开关勾选、置灰状态、滑块值。
            for page in getattr(self, "_feature_pages", []) or []:
                try:
                    p = dict(defaults)      # 默认基底：未启用功能归关闭
                    p.update(params)        # 套预设
                    page._params = p
                    page.refresh_from_params()
                except Exception:
                    log.debug("同步功能页失败", exc_info=True)
            # 染色页（电子管 / BBE）开关 + 置灰同步。
            try:
                if getattr(self, "_tube_switch", None) is not None:
                    self._tube_switch.set_active(bool(self._params.get("tube_enabled", False)))
                if getattr(self, "_bbe_switch", None) is not None:
                    self._bbe_switch.set_active(bool(self._params.get("bbe_enabled", False)))
                self._update_coloring_sensitivity()
            except Exception:
                log.debug("同步染色页失败", exc_info=True)
            # 最后一步：记录「当前音效预设」为该名称（走单一状态源，广播给
            # 所有视图，主界面音效弹窗自动高亮）。必须放在所有会触发下发
            # （_emit → _on_dsp_changed → 清空标记）的操作之后，否则会被
            # 随后的 refresh 清掉，表现为高亮「闪一下就不见」。
            try:
                from core.effect_state import get_effect_state
                get_effect_state().set_current(name)
            except Exception:
                log.debug("记录预设名失败", exc_info=True)

    def _on_preset_delete(self, _btn) -> None:
        from core.dsp_store import get_dsp_preset_store
        idx = self._preset_dropdown.get_selected()
        if not (hasattr(self, "_preset_names") and 0 <= idx < len(self._preset_names)):
            return
        name = self._preset_names[idx]
        if get_dsp_preset_store().remove(name):
            self._reload_presets()
