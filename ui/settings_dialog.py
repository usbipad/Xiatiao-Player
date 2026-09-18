"""设置弹窗（Adw.PreferencesWindow）。"""
from __future__ import annotations

import logging
import os
from typing import List

from gi.repository import Adw, Gdk, Gio, Gtk

from config.settings import get_config
from core.i18n import _, set_language

log = logging.getLogger(__name__)


class SettingsWindow(Adw.PreferencesWindow):
    """应用设置。"""

    def __init__(self, parent, on_dirs_changed=None, on_dsp_changed=None,
                 on_convolution_ir=None, on_convolution_cleared=None,
                 on_viz_changed=None, on_open_viz_window=None,
                 on_coloring=None) -> None:
        super().__init__()
        # 保存父窗口：构建期 self.get_root() 可能为 None，
        # 因此统一通过 _get_window() 获取主窗口（用于访问 player）。
        self._parent = parent
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title(_("设置"))
        self.set_default_size(620, 560)
        self._on_dirs_changed = on_dirs_changed
        self._on_dsp_changed = on_dsp_changed
        self._on_convolution_ir = on_convolution_ir
        self._on_convolution_cleared = on_convolution_cleared
        self._on_viz_changed = on_viz_changed
        self._on_open_viz_window = on_open_viz_window
        self._on_coloring = on_coloring

        self._build_local_group()
        self._build_playback_page()
        self._build_shortcuts_page()
        self._build_effect_page()
        self._build_visualization_page()
        self._build_appearance_page()
        self._build_about_page()
        self._reload_dir_rows()

    def _get_window(self):
        """获取主窗口（用于访问 player）。

        构建期 self.get_root() 可能为 None，故优先用构造时保存的 parent。
        """
        return self._parent if self._parent is not None else self.get_root()

    # ------------------------------------------------------------
    # 快捷键页
    # ------------------------------------------------------------
    #: 动作键 → 显示名
    _SHORTCUT_LABELS = (
        ("play_pause", "播放 / 暂停"),
        ("prev", "上一首"),
        ("next", "下一首"),
        ("seek_back", "快退 5 秒"),
        ("seek_fwd", "快进 5 秒"),
        ("vol_up", "音量 +5%"),
        ("vol_down", "音量 -5%"),
    )
    #: 默认全部为空（未绑定），由用户自行录制
    _SHORTCUT_DEFAULTS = {
        "play_pause": "", "prev": "", "next": "",
        "seek_back": "", "seek_fwd": "",
        "vol_up": "", "vol_down": "",
    }

    def _build_shortcuts_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title(_("快捷键"))
        page.set_icon_name("preferences-desktop-keyboard-symbolic")
        group = Adw.PreferencesGroup()
        group.set_title(_("播放快捷键"))
        group.set_description(_("点击右侧按钮后按下新按键即可修改"))
        # 标题右侧：重置按钮
        try:
            reset_btn = Gtk.Button(label=_("重置"))
            reset_btn.add_css_class("flat")
            reset_btn.set_valign(Gtk.Align.CENTER)
            reset_btn.set_tooltip_text(_("恢复默认快捷键"))
            reset_btn.connect("clicked", lambda *_: self._reset_shortcuts())
            group.set_header_suffix(reset_btn)
        except Exception:
            pass
        page.add(group)

        cfg = get_config()
        saved = cfg.get("shortcuts")
        cur = dict(self._SHORTCUT_DEFAULTS)
        if isinstance(saved, dict):
            cur.update({k: v for k, v in saved.items() if isinstance(v, str)})
        self._shortcut_rows = {}

        for action, label in self._SHORTCUT_LABELS:
            row = Adw.ActionRow()
            row.set_title(_(label))
            # 键位按钮：未设置时显示占位框（有边框，提示可点击录制）
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_valign(Gtk.Align.CENTER)
            btn.set_size_request(96, -1)
            self._apply_key_btn_label(btn, cur.get(action, ""))
            btn.set_tooltip_text(_("点击后按下新按键录制"))
            btn.connect("clicked", self._on_capture_shortcut, action, row)
            row.add_suffix(btn)
            row._key_btn = btn
            # 删除按钮：清除本项快捷键
            del_btn = Gtk.Button(icon_name="edit-clear-symbolic")
            del_btn.add_css_class("flat")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.set_tooltip_text(_("清除此快捷键"))
            del_btn.connect("clicked", lambda *_a, act=action: self._clear_shortcut(act))
            row.add_suffix(del_btn)
            row._del_btn = del_btn
            group.add(row)
            self._shortcut_rows[action] = row

        self.add(page)
        self._shortcuts_page = page

    def _apply_key_btn_label(self, btn, spec: str) -> None:
        """按是否已设置，设置键位按钮的文案与样式（未设置显示占位框）。"""
        try:
            if spec:
                btn.set_label(self._pretty_key(spec))
                btn.remove_css_class("unset-key")
            else:
                btn.set_label(_("未设置"))
                btn.add_css_class("unset-key")
        except Exception:
            pass

    def _clear_shortcut(self, action: str) -> None:
        """清除单个功能的快捷键。"""
        try:
            cfg = get_config()
            saved = cfg.get("shortcuts")
            cur = dict(self._SHORTCUT_DEFAULTS)
            if isinstance(saved, dict):
                cur.update({k: v for k, v in saved.items() if isinstance(v, str)})
            cur[action] = ""
            cfg.set("shortcuts", cur)
            r = getattr(self, "_shortcut_rows", {}).get(action)
            if r is not None:
                self._apply_key_btn_label(r._key_btn, "")
        except Exception:
            pass

    @staticmethod
    def _pretty_key(spec: str) -> str:
        """键位字符串 → 可读显示（如 Ctrl+Left → Ctrl+←）。"""
        m = {
            "Left": "←", "Right": "→", "Up": "↑", "Down": "↓",
            "space": "空格", "Return": "回车", "Escape": "Esc",
            "comma": ",", "period": ".", "slash": "/",
            "Ctrl": "Ctrl", "Control": "Ctrl",
            "Alt": "Alt", "Shift": "Shift", "Super": "Super",
        }
        parts = (spec or "").split("+")
        out = []
        for p in parts:
            out.append(m.get(p, p))
        return "+".join(out) if out else "（未设置）"

    def _on_capture_shortcut(self, btn, action: str, row) -> None:
        """点击改键：弹出对话框捕获按键。"""
        dlg = Adw.Dialog()
        dlg.set_title(_("按下新快捷键"))
        dlg.set_content_width(360)
        dlg.set_content_height(160)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        lbl = Gtk.Label(label=_("请按下要绑定的按键…"))
        lbl.add_css_class("title-3")
        box.append(lbl)
        dlg.set_child(box)

        # 纯修饰键：按下时先忽略，等真正的按键（如 ←）再保存
        _MODIFIER_KEYS = {
            Gdk.KEY_Control_L, Gdk.KEY_Control_R,
            Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
            Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
            Gdk.KEY_Super_L, Gdk.KEY_Super_R,
            Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
            Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
            Gdk.KEY_Caps_Lock, Gdk.KEY_Shift_Lock,
        }

        def _on_key(_c, keyval, _code, state):
            # Esc 取消
            if keyval == Gdk.KEY_Escape:
                dlg.close()
                return True
            # 修饰键本身不保存（等真正的按键）
            if keyval in _MODIFIER_KEYS:
                return True
            name = Gdk.keyval_name(keyval) or ""
            if not name:
                return True
            # 组合修饰键：Ctrl / Alt / Shift / Super（按固定顺序拼接）
            mods = []
            try:
                if state & Gdk.ModifierType.CONTROL_MASK:
                    mods.append("Ctrl")
                if state & Gdk.ModifierType.ALT_MASK:
                    mods.append("Alt")
                if state & Gdk.ModifierType.SHIFT_MASK:
                    mods.append("Shift")
                if state & Gdk.ModifierType.SUPER_MASK:
                    mods.append("Super")
            except Exception:
                pass
            spec = ("+".join(mods) + "+" + name) if mods else name
            # 冲突检测：该键位已被其它功能使用 → 提示且不保存
            conflict = self._find_conflict(action, spec)
            if conflict is not None:
                try:
                    lbl.set_text(_("此键位已被「{conflict}」使用").format(conflict=conflict))
                    lbl.add_css_class("error")
                except Exception:
                    pass
                return True
            self._save_shortcut(action, spec)
            self._apply_key_btn_label(row._key_btn, spec)
            dlg.close()
            return True

        kc = Gtk.EventControllerKey()
        kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        kc.connect("key-pressed", _on_key)
        dlg.add_controller(kc)
        dlg.present(self)

    def _find_conflict(self, action: str, spec: str):
        """检查该键位是否已被其它动作使用；返回冲突功能的显示名，无冲突返回 None。"""
        try:
            if not spec:
                return None
            cfg = get_config()
            saved = cfg.get("shortcuts")
            cur = dict(self._SHORTCUT_DEFAULTS)
            if isinstance(saved, dict):
                cur.update({k: v for k, v in saved.items() if isinstance(v, str)})
            label_map = dict(self._SHORTCUT_LABELS)
            for other, v in cur.items():
                if other != action and v == spec:
                    return label_map.get(other, other)
        except Exception:
            pass
        return None

    def _save_shortcut(self, action: str, spec: str) -> None:
        """保存单个快捷键到配置（调用前应已通过冲突检测）。"""
        try:
            cfg = get_config()
            saved = cfg.get("shortcuts")
            cur = dict(self._SHORTCUT_DEFAULTS)
            if isinstance(saved, dict):
                cur.update({k: v for k, v in saved.items() if isinstance(v, str)})
            cur[action] = spec
            cfg.set("shortcuts", cur)
        except Exception:
            pass

    def _reset_shortcuts(self) -> None:
        """恢复默认快捷键。"""
        try:
            cfg = get_config()
            cfg.set("shortcuts", dict(self._SHORTCUT_DEFAULTS))
            for action, row in getattr(self, "_shortcut_rows", {}).items():
                self._apply_key_btn_label(row._key_btn, self._SHORTCUT_DEFAULTS.get(action, ""))
        except Exception:
            pass

    # ------------------------------------------------------------
    # 关于页
    # ------------------------------------------------------------
    #: 关于信息（占位符：TODO 待替换）
    _ABOUT = {
        "name": "虾条播放器",
        "name_en": "Xiatiao",
        "version": "0.1.0",
        "developer": "usbipad",
        "website": "TODO: 项目主页 / 仓库地址",
        "issue": "TODO: 问题反馈地址",
        "copyright": "© 2026 usbipad",
    }

    def _build_about_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title(_("关于"))
        page.set_icon_name("help-about-symbolic")

        group = Adw.PreferencesGroup()
        # 点击弹出标准「关于」对话框
        row = Adw.ActionRow()
        row.set_title(_("关于") + " " + _(self._ABOUT["name"]))
        row.set_subtitle(_("版本") + " " + self._ABOUT["version"])
        try:
            row.add_prefix(Gtk.Image.new_from_icon_name("xiatiao"))
        except Exception:
            pass
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.set_activatable(True)
        row.connect("activated", lambda *_: self._show_about_dialog())
        group.add(row)
        page.add(group)

        # 信息组：开发者 / 网站 / 反馈 / 许可
        info = Adw.PreferencesGroup()
        info.set_title(_("详细信息"))
        info.add(self._make_info_row(_("开发者"), self._ABOUT["developer"]))
        info.add(self._make_info_row(_("网站"), self._ABOUT["website"]))
        info.add(self._make_info_row(_("问题反馈"), self._ABOUT["issue"]))
        info.add(self._make_info_row(_("版权"), self._ABOUT["copyright"]))
        info.add(self._make_info_row(_("许可"), "MIT"))
        page.add(info)

        self.add(page)

    def _make_info_row(self, title: str, value: str) -> Adw.ActionRow:
        """构造一条只读信息行（可复制文本）。"""
        row = Adw.ActionRow()
        row.set_title(title)
        lbl = Gtk.Label(label=value)
        lbl.add_css_class("dim-label")
        lbl.set_selectable(True)
        lbl.set_ellipsize(3)
        row.add_suffix(lbl)
        return row

    def _show_about_dialog(self) -> None:
        """标准「关于」对话框（Adw.AboutDialog）。

        parent 用本设置窗口（self），使对话框叠在设置窗口之上；
        并复用同一个对话框实例，避免连续点击反复弹出、反复加遮罩。
        """
        try:
            dlg = getattr(self, "_about_dlg", None)
            if dlg is None:
                dlg = Adw.AboutDialog()
                dlg.set_application_name(_(self._ABOUT["name"]))
                dlg.set_application_icon("xiatiao")
                dlg.set_version(self._ABOUT["version"])
                dlg.set_developer_name(self._ABOUT["developer"])
                dlg.set_developers([self._ABOUT["developer"]])
                dlg.set_copyright(self._ABOUT["copyright"])
                dlg.set_comments(_("本地音乐播放器（GTK4 / libadwaita）"))
                dlg.set_license_type(Gtk.License.MIT_X11)
                # TODO: 补 website / issue_url 后取消注释
                # dlg.set_website(self._ABOUT["website"])
                # dlg.set_issue_url(self._ABOUT["issue"])
                self._about_dlg = dlg
            dlg.present(self)
        except Exception:
            log.debug("显示关于对话框失败", exc_info=True)

    # ------------------------------------------------------------
    # 可视化页
    # ------------------------------------------------------------
    def _build_visualization_page(self) -> None:
        from .viz_settings_page import VizSettingsPage
        self._viz_page = VizSettingsPage(
            on_viz_changed=self._on_viz_changed,
            on_open_viz_window=self._open_viz_from_settings,
        )
        self.add(self._viz_page)

    def _open_viz_from_settings(self) -> None:
        """从设置页打开频谱窗口。

        不再取消自身模态：让频谱窗口作为本窗口的 transient 子窗口
        （在模态链内，可交互），从而既保持「主窗口被模态遮罩锁定」的
        预期行为，又让频谱窗口能正常操作 / 关闭。
        """
        if self._on_open_viz_window is not None:
            try:
                self._on_open_viz_window()
            except Exception:
                pass

    # ------------------------------------------------------------
    # 音效页
    # ------------------------------------------------------------
    def _build_effect_page(self) -> None:
        from .effect_page import EffectPage
        from config.settings import get_config
        # 读取已保存的 DSP 参数，同步到界面（避免开关状态不一致）
        initial = None
        try:
            saved = get_config().get("dsp_params")
            if isinstance(saved, dict):
                initial = saved
        except Exception:
            pass
        self._effect_page = EffectPage(
            on_dsp_changed=self._on_dsp_changed,
            initial=initial,
            on_convolution_ir=self._on_convolution_ir,
            on_convolution_cleared=self._on_convolution_cleared,
            mode="basic",
            on_open_advanced=self._open_advanced_dsp,
        )
        # 回填已保存的 IR 文件名，避免显示「未加载」造成误导
        try:
            ir_path = get_config().get("convolution_ir") or ""
            if ir_path:
                self._effect_page.set_convolution_ir_label(ir_path)
        except Exception:
            pass
        self.add(self._effect_page)

    def _open_advanced_dsp(self) -> None:
        """打开高级 DSP 设置窗口（音效页底部入口）。

        先关闭本设置窗口：避免「模态 PreferencesWindow 里再开 PreferencesWindow」
        的嵌套导致卡死/无响应。
        """
        try:
            from .advanced_dsp_window import AdvancedDspWindow
            # 优先用「完整」的 dsp_params（从 config 读），而非基础页的子集；
            # 否则高级窗口 _params 缺 convolution_*/tube_*/bbe_*/peq_* 等字段，
            # 改这些功能下发时会缺字段而被后端忽略（搬迁后新增路径的回归）。
            params = None
            try:
                from config.settings import get_config
                _saved = get_config().get("dsp_params")
                if isinstance(_saved, dict):
                    params = dict(_saved)
            except Exception:
                params = None
            if params is None:
                try:
                    params = self._effect_page.params()
                except Exception:
                    params = None
            parent = self.get_transient_for() or self
            win = AdvancedDspWindow(
                parent=parent,
                params=params,
                on_dsp_changed=self._on_dsp_changed,
                on_coloring=self._on_coloring,
                on_viz_changed=self._on_viz_changed,
                on_open_viz_window=self._on_open_viz_window,
                on_convolution_ir=self._on_convolution_ir,
                on_convolution_cleared=self._on_convolution_cleared,
            )
            # 设置窗口是模态的，会连带阻塞高级窗口的交互。
            # 开高级前临时取消模态，关闭高级后恢复。
            was_modal = self.get_modal()
            try:
                self.set_modal(False)
            except Exception:
                pass

            def _on_adv_closed(_w=None, _self=self, _m=was_modal):
                try:
                    _self.set_modal(_m)
                except Exception:
                    pass
            try:
                win.connect("close-request", _on_adv_closed)
            except Exception:
                pass

            win.present()
            self._advanced_win = win
        except Exception:
            pass

    def show_effect_page(self) -> None:
        """跳到音效页（供音效菜单"详细配置"调用）。"""
        try:
            self.set_visible_page(self._effect_page)
        except Exception:
            pass

    # ------------------------------------------------------------
    # 播放设置页
    # ------------------------------------------------------------
    def _build_playback_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title(_("播放"))
        page.set_icon_name("media-playback-start-symbolic")
        self.add(page)
        cfg = get_config()

        group = Adw.PreferencesGroup()
        group.set_title(_("播放行为"))
        page.add(group)

        # 启动恢复播放会话
        row = Adw.SwitchRow()
        row.set_title(_("启动时恢复上次播放会话"))
        row.set_subtitle(_("恢复上次的播放队列、当前歌曲、进度、音量与播放模式"))
        row.set_active(cfg.get_bool("restore_playback", True))
        row.connect("notify::active", lambda r, _p: cfg.set_bool("restore_playback", r.get_active()))
        group.add(row)

        # 记忆音量
        row = Adw.SwitchRow()
        row.set_title(_("记忆音量"))
        row.set_subtitle(_("下次启动沿用上次的音量"))
        row.set_active(cfg.get_bool("remember_volume", True))
        row.connect("notify::active", lambda r, _p: cfg.set_bool("remember_volume", r.get_active()))
        group.add(row)

        # 切歌淡入淡出
        row = Adw.SwitchRow()
        row.set_title(_("切歌淡入淡出"))
        row.set_subtitle(_("切换歌曲时平滑过渡，避免突兀"))
        row.set_active(cfg.get_bool("fade_on_switch", False))
        row.connect("notify::active", lambda r, _p: cfg.set_bool("fade_on_switch", r.get_active()))
        group.add(row)

        # 默认播放模式
        mode_row = Adw.ComboRow()
        mode_row.set_title(_("默认播放模式"))
        mode_row.set_subtitle(_("新队列开始时采用的循环方式"))
        model = Gtk.StringList()
        for label in (_("顺序播放"), _("随机播放"), _("单曲循环")):
            model.append(label)
        mode_row.set_model(model)
        mode_row.set_selected(cfg.get_int("default_play_mode", 0) % 3)
        mode_row.connect("notify::selected",
                         lambda r, _p: cfg.set_int("default_play_mode", r.get_selected()))
        group.add(mode_row)

        # ---- 输出 / DSD ----
        out_group = Adw.PreferencesGroup()
        out_group.set_title(_("输出"))
        page.add(out_group)

        # DSD 输出模式
        dsd_row = Adw.ComboRow()
        dsd_row.set_title(_("DSD 输出模式"))
        dsd_row.set_subtitle(_("DSD 文件的输出方式；自动模式按 DAC 能力选择"))
        self._dsd_mode_values = ["auto", "native", "dop", "pcm"]
        dsd_model = Gtk.StringList()
        for label in (_("自动"), _("DSD 直通 (Native)"), _("DoP"), _("转 PCM (软解)")):
            dsd_model.append(label)
        dsd_row.set_model(dsd_model)
        try:
            dsd_row.set_selected(self._dsd_mode_values.index(cfg.get_str("dsd_output_mode", "auto")))
        except ValueError:
            dsd_row.set_selected(0)

        def _on_dsd_changed(r, _p):
            idx = r.get_selected()
            if 0 <= idx < len(self._dsd_mode_values):
                cfg.set_str("dsd_output_mode", self._dsd_mode_values[idx])
                # 下发到后端
                try:
                    win = self._get_window()
                    if win is not None and hasattr(win, "apply_dsd_mode"):
                        win.apply_dsd_mode(self._dsd_mode_values[idx])
                except Exception:
                    pass
        dsd_row.connect("notify::selected", _on_dsd_changed)
        out_group.add(dsd_row)

        # 输出设备
        dev_row = Adw.ComboRow()
        dev_row.set_title(_("输出设备"))
        dev_row.set_subtitle(_("自动=系统默认；选择具体设备则以 ALSA 独占直连硬件"))
        self._dev_values = [""]
        dev_model = Gtk.StringList()
        dev_model.append(_("自动（系统默认）"))
        try:
            win = self._get_window()
            player = getattr(win, "player", None) if win is not None else None
            if player is not None and hasattr(player, "list_output_devices"):
                for dev in player.list_output_devices():
                    dev_id = dev.get("id", "")
                    desc = dev.get("description", dev_id)
                    if dev_id:
                        self._dev_values.append(dev_id)
                        dev_model.append(desc)
        except Exception:
            pass
        dev_row.set_model(dev_model)
        _cur_dev = cfg.get_str("output_device", "")
        try:
            dev_row.set_selected(self._dev_values.index(_cur_dev))
        except ValueError:
            dev_row.set_selected(0)

        def _on_dev_changed(r, _p):
            idx = r.get_selected()
            if 0 <= idx < len(self._dev_values):
                cfg.set_str("output_device", self._dev_values[idx])
                try:
                    win = self._get_window()
                    if win is not None and hasattr(win, "apply_output_device"):
                        win.apply_output_device(self._dev_values[idx])
                except Exception:
                    pass
        dev_row.connect("notify::selected", _on_dev_changed)
        out_group.add(dev_row)

        # ---- 主页卡片轮播 ----
        rotate_group = Adw.PreferencesGroup()
        rotate_group.set_title(_("主页卡片"))
        rotate_group.set_description(_("主页专辑 / 艺术家卡片的随机轮播"))
        page.add(rotate_group)

        rot_row = Adw.SwitchRow()
        rot_row.set_title(_("主页卡片随机轮播"))
        rot_row.set_subtitle(_("折叠时卡片自动随机切换展示的专辑 / 艺术家"))
        rot_row.set_active(cfg.get_bool("card_rotate_enabled", True))

        def _on_rot_toggled(r, _p):
            cfg.set_bool("card_rotate_enabled", r.get_active())
            # 开启时提示需重启生效
            if r.get_active():
                try:
                    self.add_toast(Adw.Toast.new(_("已启用，重启应用后生效")))
                except Exception:
                    pass

        rot_row.connect("notify::active", _on_rot_toggled)
        rotate_group.add(rot_row)

        spd_row = Adw.ComboRow()
        spd_row.set_title(_("轮播速度"))
        spd_model = Gtk.StringList()
        for label in (_("慢速"), _("中等"), _("快速")):
            spd_model.append(label)
        spd_row.set_model(spd_model)
        self._rotate_speed_values = ["slow", "medium", "fast"]
        _cur_spd = cfg.get_str("card_rotate_speed", "medium")
        _idx = self._rotate_speed_values.index(_cur_spd) if _cur_spd in self._rotate_speed_values else 1
        spd_row.set_selected(_idx)
        spd_row.connect("notify::selected",
                        lambda r, _p: cfg.set_str("card_rotate_speed",
                                                  self._rotate_speed_values[r.get_selected()]
                                                  if 0 <= r.get_selected() < 3 else "medium"))
        rotate_group.add(spd_row)

    # ------------------------------------------------------------
    # 外观设置页
    # ------------------------------------------------------------
    def _build_appearance_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title(_("外观"))
        page.set_icon_name("applications-graphics-symbolic")
        self.add(page)
        cfg = get_config()

        group = Adw.PreferencesGroup()
        group.set_title(_("界面"))
        page.add(group)

        # 主题
        theme_row = Adw.ComboRow()
        theme_row.set_title(_("主题"))
        theme_model = Gtk.StringList()
        self._theme_values = ["system", "light", "dark"]
        for label in (_("跟随系统"), _("浅色"), _("深色")):
            theme_model.append(label)
        theme_row.set_model(theme_model)
        try:
            theme_row.set_selected(self._theme_values.index(cfg.get_str("theme", "system")))
        except ValueError:
            theme_row.set_selected(0)
        theme_row.connect("notify::selected", self._on_theme_changed)
        group.add(theme_row)

        # 列表行高
        density_row = Adw.ComboRow()
        density_row.set_title(_("列表行高"))
        density_model = Gtk.StringList()
        self._density_values = ["compact", "normal", "relaxed"]
        for label in (_("紧凑"), _("标准"), _("宽松")):
            density_model.append(label)
        density_row.set_model(density_model)
        try:
            density_row.set_selected(self._density_values.index(cfg.get_str("row_density", "normal")))
        except ValueError:
            density_row.set_selected(1)
        density_row.connect("notify::selected", self._on_density_changed)
        group.add(density_row)

        # 语言
        lang_row = Adw.ComboRow()
        lang_row.set_title(_("语言"))
        lang_model = Gtk.StringList()
        self._lang_values = ["system", "zh", "en"]
        for label in (_("跟随系统"), _("中文"), _("英文")):
            lang_model.append(label)
        lang_row.set_model(lang_model)
        try:
            lang_row.set_selected(self._lang_values.index(cfg.get_str("language", "system")))
        except ValueError:
            lang_row.set_selected(0)
        lang_row.connect("notify::selected", self._on_language_changed)
        group.add(lang_row)

        # 沉浸页背景模糊
        blur_row = Adw.SwitchRow()
        blur_row.set_title(_("沉浸页背景模糊"))
        blur_row.set_subtitle(_("用当前封面生成模糊背景；关闭则用纯色背景"))
        blur_row.set_active(cfg.get_bool("nowplaying_blur_bg", True))
        blur_row.connect("notify::active", self._on_blur_bg_toggled)
        group.add(blur_row)

        # 行为分组
        behave = Adw.PreferencesGroup()
        behave.set_title(_("行为"))
        page.add(behave)

        row = Adw.SwitchRow()
        row.set_title(_("启动时自动扫描本地曲库"))
        row.set_active(cfg.get_bool("auto_scan_on_start", True))
        row.connect("notify::active", lambda r, _p: cfg.set_bool("auto_scan_on_start", r.get_active()))
        behave.add(row)

        row = Adw.SwitchRow()
        row.set_title(_("关闭时最小化到后台"))
        row.set_subtitle(_("关闭窗口不退出程序"))
        row.set_active(cfg.get_bool("close_to_tray", False))
        row.connect("notify::active", lambda r, _p: cfg.set_bool("close_to_tray", r.get_active()))
        behave.add(row)

    def _on_theme_changed(self, row, _pspec) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(self._theme_values):
            get_config().set_str("theme", self._theme_values[idx])
            self._apply_theme(self._theme_values[idx])

    def _on_density_changed(self, row, _pspec) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(self._density_values):
            get_config().set_str("row_density", self._density_values[idx])

    def _on_language_changed(self, row, _pspec) -> None:
        """切换界面语言：持久化并提示重启生效。"""
        idx = row.get_selected()
        if 0 <= idx < len(getattr(self, "_lang_values", [])):
            set_language(self._lang_values[idx])
            try:
                win = self.get_root()
                if win is not None and hasattr(win, "add_toast"):
                    win.add_toast(Adw.Toast.new(_("语言已切换，重启应用后生效")))
            except Exception:
                pass

    def _on_blur_bg_toggled(self, row, _pspec) -> None:
        """切换沉浸页背景模糊，持久化并即时通知主窗口重应用。"""
        enabled = bool(row.get_active())
        get_config().set_bool("nowplaying_blur_bg", enabled)
        # 即时生效：通知主窗口重新应用当前曲目的背景
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "reapply_nowplaying_bg"):
                win.reapply_nowplaying_bg()
        except Exception:
            pass

    @staticmethod
    def _apply_theme(theme: str) -> None:
        """切换 libadwaita 配色方案（即时生效）。"""
        try:
            style_mgr = Adw.StyleManager.get_default()
            if theme == "light":
                style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            elif theme == "dark":
                style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            else:
                style_mgr.set_color_scheme(Adw.ColorScheme.DEFAULT)
        except Exception:
            pass

    # ------------------------------------------------------------
    # ------------------------------------------------------------
    # 本地音源分组
    # ------------------------------------------------------------
    def _build_local_group(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title(_("音源"))
        page.set_icon_name("audio-x-generic-symbolic")
        self.add(page)
        self._page = page  # 供后续分组复用

        group = Adw.PreferencesGroup()
        group.set_title(_("本地音源"))
        group.set_description(_("添加多个音乐目录，程序启动时会自动扫描"))
        page.add(group)
        self._local_group = group

        # 已添加目录的行容器
        self._dir_rows: List[Adw.ActionRow] = []

        # 添加按钮
        add_row = Adw.ActionRow()
        add_row.set_title(_("添加音乐目录"))
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_dir)
        add_row.add_suffix(add_btn)
        group.add(add_row)

    def _reload_dir_rows(self) -> None:
        # 清空旧行（保留“添加”行）
        for row in self._dir_rows:
            self._local_group.remove(row)
        self._dir_rows.clear()

        for directory in get_config().get_music_dirs():
            row = Adw.ActionRow()
            row.set_title(os.path.basename(directory) or directory)
            row.set_subtitle(directory)
            rm_btn = Gtk.Button(icon_name="user-trash-symbolic")
            rm_btn.set_valign(Gtk.Align.CENTER)
            rm_btn.add_css_class("flat")
            rm_btn.connect("clicked", self._on_remove_dir, directory)
            row.add_suffix(rm_btn)
            # 插到“添加”行之前
            self._local_group.add(row)
            self._dir_rows.append(row)

    # ------------------------------------------------------------
    # 目录增删
    # ------------------------------------------------------------
    def _on_add_dir(self, _btn) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("选择音乐目录"))
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return  # 用户取消或出错，忽略
        if folder is None:
            return
        path = folder.get_path()
        if not path:
            return
        if get_config().add_music_dir(path):
            self._reload_dir_rows()
            self._notify_changed()

    def _on_remove_dir(self, _btn, directory: str) -> None:
        if get_config().remove_music_dir(directory):
            self._reload_dir_rows()
            self._notify_changed()

    def _notify_changed(self) -> None:
        if callable(self._on_dirs_changed):
            self._on_dirs_changed()
