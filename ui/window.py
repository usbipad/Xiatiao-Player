"""主窗口（本地曲库版）：UI 与本地播放后端的缝合。

- 左侧固定 PlayerPanel（封面/曲目/进度/控制）。
- 右侧本地曲库列表 + 沉浸式播放页。
- 仅本地音源，无任何在线功能。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from core.i18n import _

from config.settings import get_config
from core import Playlist, PlayerCore, get_cache, tracks_to_list, list_to_tracks
from core.liked_store import get_liked_store
from models import (
    TrackItem,
    SOURCE_LOCAL,
    extract_cover,
    load_lyrics,
    lighten_for_background,
)
from providers import create_provider
from providers.base import BaseMusicProvider
from services.shortcuts import (
    SHORTCUT_ACTIONS,
    get_shortcuts,
    match_shortcut,
    mods_from_state,
    parse_shortcut,
)

from .now_playing import NowPlayingPage
from .pages import LocalLibraryPage
from .player_panel import PANEL_MIN_W, PlayerPanel
from .settings_dialog import SettingsWindow

log = logging.getLogger(__name__)


def _human_size(n: float) -> str:
    """把字节数格式化成可读大小。"""
    try:
        n = float(n)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _glib_escape(text: str) -> str:
    """转义 Pango markup 特殊字符。"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;"))


def _mpris_cover_cache_dir() -> str:
    """MPRIS 封面落盘目录（XDG 缓存目录下）。"""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "xiatiao")


def _cover_ext(image_bytes: bytes) -> str:
    """按图片魔数判定扩展名。"""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _write_mpris_cover(image_bytes: bytes, token: int) -> Optional[str]:
    """把封面字节写成缓存文件，返回路径；失败返回 None。

    关键：文件名带 token（唯一），原因有二——
    1. 避免快速连切时旧线程覆盖新封面（即使有 token 校验，文件层面也隔离）；
    2. mpris:artUrl 每次变化，KDE Connect / Android 端不会命中旧缓存，
       否则固定 URL 会导致一直显示第一张封面。
    同时清理本目录下其它过期封面文件（保留当前这张）。
    """
    if not image_bytes:
        return None
    try:
        ext = _cover_ext(image_bytes)
        d = _mpris_cover_cache_dir()
        os.makedirs(d, exist_ok=True)
        name = f"mpris_cover_{token}{ext}"
        path = os.path.join(d, name)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fp:
            fp.write(image_bytes)
        os.replace(tmp, path)
        # 清理本目录下其它 mpris_cover_* 旧文件，避免无限堆积
        try:
            for fn in os.listdir(d):
                if fn == name or not fn.startswith("mpris_cover_"):
                    continue
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:
                    pass
        except OSError:
            pass
        return path
    except OSError as exc:
        log.debug("写入 MPRIS 封面失败: %s", exc)
        return None


#: 导航项：(显示名 key, 页面 key)；显示名经 _() 翻译
NAV_ITEMS = [
    ("主页", "home"),
    ("歌单", "playlists"),
    ("曲库", "local"),
    ("收藏", "liked"),
]


class MainWindow(Adw.ApplicationWindow):
    """主窗口。"""

    __gtype_name__ = "MainWindow"

    def _get_narrow_mode(self) -> bool:
        return getattr(self, "_narrow", False)

    def _set_narrow_mode(self, value: bool) -> None:
        self._narrow = bool(value)
        try:
            self._apply_narrow(bool(value))
        except Exception:
            pass

    #: 窄屏模式（由 Adw.Breakpoint 驱动）：收缩 headerbar 控件。
    narrow_mode = GObject.Property(
        type=bool, default=False, getter=_get_narrow_mode, setter=_set_narrow_mode)

    def _keep_expanded(self, sv, _pspec=None) -> None:
        """始终保持侧栏展开（不折叠），消除拖窗时的折叠抖动。"""
        try:
            if sv.get_collapsed():
                sv.set_collapsed(False)
        except Exception:
            pass

    def _apply_narrow(self, on_narrow: bool) -> None:
        """窄屏时隐藏导航文字、收缩搜索框，降低窗口最小宽度。"""
        for _lbl in getattr(self, "_nav_labels", {}).values():
            try:
                _lbl.set_visible(not on_narrow)
            except Exception:
                pass
        se = getattr(self, "_search_entry", None)
        if se is not None:
            try:
                se.set_size_request(0 if on_narrow else 150, -1)
            except Exception:
                pass

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=_("虾条播放器"))
        cfg0 = get_config()
        w = cfg0.get_int("window_width", 1400) or 1400
        h = cfg0.get_int("window_height", 900) or 900
        self.set_default_size(max(340, w), max(480, h))
        # 窗口最小尺寸 = 左侧播放面板的最小宽度（二者相等）。
        # 使窗口缩到最窄时恰好容纳面板、不被压窄。
        # 宽度取自 player_panel.PANEL_MIN_W（单一真相源，改面板宽度即自动跟随）。
        try:
            self.set_size_request(PANEL_MIN_W, 562)
        except Exception:
            log.debug("设置窗口最小尺寸失败", exc_info=True)

        self._load_css()

        # ---- 后端 ----
        self.playlist = Playlist()
        self.player = PlayerCore()
        self._providers: Dict[str, BaseMusicProvider] = {}
        self._active_source = "local"
        self._local_fingerprint = None
        self._restoring = False
        self._dsp_yaml_timer = None
        self._pending_dsp_params = None
        # ---- DSP 单一真相源（架构重构 S2：接线，过渡期双写）----
        # DspState 成为 dsp_params 的唯一权威；旧路径（各视图 _params +
        # _on_dsp_changed）在过渡期继续工作，DspState 并行维护。
        # 订阅其广播：一旦有变更，统一出口下发（S2 阶段先记录，
        # S3/S4 各视图改走 patch 后，旧路径删除，此出口接管）。
        self._dsp_state = None
        try:
            from core.dsp_state import get_dsp_state
            self._dsp_state = get_dsp_state()
            # 订阅：DspState 一变，走统一出口下发（set_dsp + Camilla YAML）。
            # 这是架构重构后的唯一「下发触发点」——各视图只 patch，不再各自
            # 下发，从根上消除多入口竞态（如 ReplayGain 晚到覆盖总开关）。
            self._dsp_state.connect("changed", self._on_dsp_state_changed)
            self._dsp_state.connect("replaced", self._on_dsp_state_changed)
            # 下发防抖合并（拖滑块）：DspState 广播可能很密集。
            self._state_push_timer = None
            self._state_push_pending = None
            self._state_push_immediate = False
        except Exception:
            log.debug("订阅 DspState 失败", exc_info=True)
            self._dsp_state = None

        # ---- 布局 ----
        # 侧栏（Adw.OverlaySplitView）：始终并排显示，宽度线性跟随窗口；
        # 仅由 headerbar 按钮手动隐藏/显示（不随窗口宽度自动折叠）。
        self._main_stack = Gtk.Stack()
        self._main_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        # 非均匀：尺寸变化时只测量「可见」那一页。
        # 默认 homogeneous=True 会把主界面和沉浸页两棵子树都量一遍，
        # 沉浸页含大封面/歌词/可视化，每次最大化或拖动窗口都要白量一整棵，
        # 是窗口缩放明显「慢半拍」的主因。（右侧 stack 与 _content_stack 同此处理。）
        try:
            self._main_stack.set_hhomogeneous(False)
            self._main_stack.set_vhomogeneous(False)
        except Exception:
            pass

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._main_stack)

        # 外层 Overlay：承载启动闪屏（叠在内容之上，淡出后移除）
        self._root_overlay = Gtk.Overlay()
        self._root_overlay.set_child(self._toast_overlay)
        # 闪屏控制器（见 ui/splash.py）：通过回调注入依赖，与窗口解耦
        from .splash import SplashController
        self._splash_ctrl = SplashController(
            overlay_provider=lambda: self._root_overlay,
            has_library_to_load=self._has_library_to_load,
            is_library_loaded=self._is_local_library_loaded,
            is_cover_busy=self._is_cover_busy,
        )
        self._root_overlay.add_overlay(self._splash_ctrl.build())
        self.set_content(self._root_overlay)

        # 左侧面板
        self.player_panel = PlayerPanel(
            on_play_pause=self._on_play_pause,
            on_prev=self._on_prev,
            on_next=self._on_next,
            on_seek=self._on_seek,
            on_cover_clicked=self._enter_fullscreen,
            on_volume=self._on_volume,
            on_shuffle=self._on_shuffle,
            on_repeat=self._on_repeat,
            on_func_toast=self._toast,
            on_tab=self._on_panel_tab,
            on_open_settings=self._on_open_settings,
            on_like=self._on_like_current,
            on_effect=self._on_effect,
            on_effect_settings=self._on_open_effect_settings,
            on_queue_activate=self._on_queue_activate,
            on_queue_action=self._on_queue_action,
            on_add_queue=self._on_add_current_to_playlist,
            on_cast=self._on_cast_current,
        )
        # 主页音效按钮改为弹出模态对话框（6 个内置预设 + DSP 设置入口）
        self.player_panel.use_effect_dialog = True

        # 右侧：顶部栏 + 内容
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_box.set_hexpand(True)
        right_box.add_css_class("content-area")
        right_box.append(self._build_headerbar())
        right_box.append(self._build_right_area())
        self._right_box = right_box
        # 用 ScrolledWindow 包住右侧内容区：ScrolledWindow 的最小宽度可为 0
        # （它会裁切内容，而非被内容撑开），使窗口缩到最窄时右侧被完全挤出，
        # 窗口最小宽度只由左侧播放面板决定。滚动条策略 NEVER（纯为收缩）。
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        right_scroll.set_propagate_natural_width(False)
        right_scroll.set_child(right_box)

        # 可折叠侧栏：宽窗口固定并排，窄窗口折叠为浮层。
        self._split_view = Adw.OverlaySplitView()
        self._split_view.set_sidebar(self.player_panel)
        self._split_view.set_content(right_scroll)
        # 侧栏宽度：**线性**跟随窗口（比例 0.28，钳制在 280~520）。
        # 拖动窗口时连续平滑变化，无跳档；封面（正方形）随之平滑放大/缩小。
        self._split_view.set_min_sidebar_width(280)
        self._split_view.set_max_sidebar_width(520)
        try:
            self._split_view.set_sidebar_width_unit(Adw.LengthUnit.SP)
            self._split_view.set_sidebar_width_fraction(0.28)
        except Exception:
            log.debug("设置侧栏宽度失败", exc_info=True)
        # 永不折叠（固定并排）：libadwaita 默认在窗口变窄时自动折叠侧栏，
        # 宽度在临界值附近抖动会「折叠↔展开」反复切换，每次重绘 border/shadow
        # → 拖动窗口时面板右侧竖线一闪一闪。强制 collapsed 恒 False 消除抖动。
        try:
            self._split_view.set_collapsed(False)
            self._split_view.connect("notify::collapsed", self._keep_expanded)
        except Exception:
            log.debug("设置侧栏折叠失败", exc_info=True)
        self._main_stack.add_named(self._split_view, "main")

        # 侧栏显隐：headerbar 切换按钮 ↔ show-sidebar 双向绑定。
        try:
            _btn = getattr(self, "_sidebar_toggle_btn", None)
            if _btn is not None:
                _btn.bind_property(
                    "active", self._split_view, "show-sidebar",
                    GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        except Exception:
            log.debug("绑定侧栏切换失败", exc_info=True)

        # 侧栏显隐**只由按钮控制**（不随窗口宽度自动折叠）：
        # 窗口缩窄时右侧区域收缩，播放器面板始终保留。
        # （collapsed 保持默认 False = 并排显示。）

        # ---- 窄屏响应式：收缩 headerbar 控件，降低窗口最小宽度 ----
        # 宽屏（> 900px）：导航显示图标+文字，搜索框 150px。
        # 窄屏（≤ 900px）：导航只留图标（隐藏文字），搜索框收窄到 0（可压缩）。
        # 这样窗口最小宽度由「导航图标 + 设置/窗口按钮」决定，能缩到更窄。
        try:
            _bp_narrow = Adw.Breakpoint.new(
                Adw.BreakpointCondition.parse("max-width: 900px"))
            _bp_narrow.add_setter(self, "narrow-mode", True)
            self.add_breakpoint(_bp_narrow)
            _bp_wide = Adw.Breakpoint.new(
                Adw.BreakpointCondition.parse("min-width: 901px"))
            _bp_wide.add_setter(self, "narrow-mode", False)
            self.add_breakpoint(_bp_wide)
        except Exception:
            log.debug("添加窄屏断点失败", exc_info=True)

        # 沉浸式播放页
        self.now_playing = NowPlayingPage(
            on_exit=self._exit_fullscreen,
            on_seek=self._on_seek,
            on_play_pause=self._on_play_pause,
            on_prev=self._on_prev,
            on_next=self._on_next,
            on_shuffle=self._on_shuffle,
            on_repeat=self._on_repeat,
            on_volume=self._on_volume,
            on_effect=self._open_effect_dialog,
        )
        self.now_playing.bind_window(self)
        self._main_stack.add_named(self.now_playing, "nowplaying")

        # ---- 信号 ----
        self._connect_player_signals()
        # ---- 系统托盘（GNOME 需 AppIndicator 扩展；失败则静默跳过）----
        self._tray = None
        try:
            from services.tray import Tray
            self._tray = Tray(
                on_play_pause=self._on_play_pause,
                on_prev=self._on_prev,
                on_next=self._on_next,
                on_show_window=self._tray_show_window,
                on_settings=self._on_open_settings,
                on_about=self._show_about,
                on_quit=self._quit_app,
            )
            if self._tray.start():
                log.info("托盘已启动")
        except Exception:
            log.debug("托盘初始化失败", exc_info=True)
            self._tray = None
        self.playlist.connect("current-changed", self._on_playlist_current_changed)
        self.playlist.connect("current-changed", self._on_current_changed_queue)
        # 队列内容变化（增删/移动）→ 实时刷新 Queue 视图
        self.playlist.connect("changed", self._on_playlist_changed_queue)
        # 播放模式变化 → 同步两侧 UI（MPRIS 也监听，实现双向同步）
        self.playlist.connect("shuffle-changed", self._on_playlist_shuffle_changed)
        self.playlist.connect("repeat-changed", self._on_playlist_repeat_changed)
        self.connect("close-request", self._on_close_request)
        # 窗口首次显示后安排启动闪屏淡出
        self.connect("map", self._on_window_mapped)
        # 封面加载忙闲变化 → 空闲时淡出 splash（等首屏封面都加载完）
        try:
            from ui.pages import cover_activity
            self._cover_activity = cover_activity
            cover_activity.connect("busy-changed", self._on_cover_activity_changed)
        except Exception:
            self._cover_activity = None
        # 全局快捷键：空格播放/暂停、←→切歌、Ctrl+←→ seek、↑↓音量
        # 用 CAPTURE 阶段，先于子控件拿到按键（否则列表/按钮会吞掉）
        try:
            _key = Gtk.EventControllerKey()
            _key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            _key.connect("key-pressed", self._on_window_key)
            self.add_controller(_key)
        except Exception:
            pass

        # 初始化音效菜单并应用当前预设
        self._init_effect_menu()
        self._init_effect_presets()

        # ---- 可视化管线（Rust 旁路 FIFO → FFT → 面板底部频谱柱）----
        self._viz_pipeline = None
        self._viz_timer = None
        self._init_visualizer()

        # ---- 应用持久化的音频输出设置到后端 ----
        # 后端启动时输出设备默认为空（PipeWire）、DSD 模式为 auto，
        # 需把上次保存的选择下发，避免 UI 显示与后端实际不一致。
        self._apply_audio_output_settings()

        # ---- 首屏：窗口先显示，重活丢到主循环空闲时做（避免启动卡半拍）----
        self._switch_page("home")
        GLib.idle_add(self._apply_cache_async)
        GLib.idle_add(self._initial_scan)
        if get_config().get_bool("restore_playback", False):
            # 延后到窗口显示、控件就绪后再恢复（避免恢复时 UI 未就绪被覆盖）
            GLib.timeout_add(600, self._restore_last_playback)
        if get_config().get_bool("remember_window_size", True) and get_config().get_bool("window_maximized", False):
            GLib.idle_add(lambda: (self.maximize(), False)[1])

        # ---- MPRIS2：桌面媒体控件 / 媒体键 / 锁屏控制 ----
        try:
            from services.mpris import MprisService
            self._mpris = MprisService(self.player, self.playlist)
            self._mpris.set_raise_callback(self._mpris_raise)
            self._mpris.set_quit_callback(self._mpris_quit)
            _ok = self._mpris.start()
            log.info("MPRIS 启动结果: %s", _ok)
        except Exception as exc:
            log.info("MPRIS 初始化失败: %s", exc, exc_info=True)

    def _mpris_raise(self) -> None:
        """MPRIS Raise：把窗口置前。"""
        try:
            self.present()
        except Exception:
            pass

    def _mpris_quit(self) -> None:
        """MPRIS Quit：退出应用。"""
        try:
            app = self.get_application()
            if app is not None:
                app.quit()
        except Exception:
            pass

    # ============================================================
    # 缓存
    # ============================================================
    def _apply_cache(self) -> None:
        cache = get_cache()
        local = cache.get("local")
        if isinstance(local, dict):
            tracks = list_to_tracks(local.get("tracks"))
            if tracks:
                # 关键：把这批（唯一的）TrackItem 也设给 provider，
                # 让列表页、播放队列、provider 共用同一批对象引用。
                # 否则列表是缓存对象、播放是 provider 对象，两份独立，
                # 参数更新/右键信息会不一致。
                try:
                    provider = self._get_provider(SOURCE_LOCAL)
                    if provider is not None and not provider.get_library():
                        provider._tracks = tracks  # noqa: SLF001
                except Exception:
                    pass
                self.local_page.set_tracks(tracks)
                self._local_fingerprint = self._tracks_fingerprint(tracks)

    def _apply_cache_async(self) -> bool:
        """idle 回调：窗口显示后再读缓存填列表。"""
        try:
            self._apply_cache()
        except Exception as exc:
            log.debug("应用缓存失败: %s", exc)
        # 缓存已填（有曲库）则首屏就绪，可隐藏 splash
        try:
            self._mark_splash_ready_if_loaded()
        except Exception:
            pass
        return False

    def _save_cache(self) -> None:
        try:
            get_cache().save()
        except Exception:
            pass

    # ============================================================
    # 样式
    # ============================================================
    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        css_path = os.path.join(os.path.dirname(__file__), "style.css")
        provider.load_from_path(css_path)
        display = Gdk.Display.get_default()
        if display is not None:
            # 使用 USER 级（800）优先级，确保本应用精心设计的样式能够完全压过
            # 用户系统中安装的任何第三方 GTK/Adwaita 主题（如 MacTahoe、WhiteSur 等）
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
            # 动态背景 provider：主界面「背景跟随封面」时注入封面主色。
            # 用 USER+1000 优先级：用户主题（MacTahoe 等）的 popover/listview
            # 规则与 USER 同级且后加载，会覆盖我们；提高到 +1000 确保胜出
            # （音效气泡/菜单背景即依赖此 provider）。
            self._dynamic_css = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                display, self._dynamic_css,
                Gtk.STYLE_PROVIDER_PRIORITY_USER + 1000,
            )
            # 侧栏描边/背景覆盖：libadwaita 给 overlay-split-view > border 画了
            # 1px 描边（浅色主题下成白线）、给 .sidebar-pane 设了 sidebar 底色。
            # 这些是库内置样式，style.css（USER 级）拼不过，故用 USER+1000 注入。
            _sv_prov = Gtk.CssProvider()
            _sv_prov.load_from_data(
                # 侧栏与内容区之间的分隔线（border 节点）：淡前景色，明暗自适应。
                b"overlay-split-view > border,"
                b"overlay-split-view > border:backdrop,"
                b"overlay-split-view > border:dir(ltr),"
                b"overlay-split-view > border:dir(rtl) {"
                b"background: alpha(@window_fg_color, 0.12);"
                b"background-image: none;"
                b"background-color: alpha(@window_fg_color, 0.12);"
                b"min-width: 1px; min-height: 1px; border: none; box-shadow: none;}"
                b"overlay-split-view > outline {"
                b"background: none; background-image: none;"
                b"border: none; box-shadow: none; min-width: 0; min-height: 0;}"
                # 侧栏与内容区之间有个无类名的 AdwGizmo widget（分隔装饰）。
                b"overlay-split-view > widget {"
                b"background: none; background-image: none; border: none;"
                b"box-shadow: none; min-width: 0; min-height: 0;}"
                # dimming（暗化层）也清零，避免残留。
                b"overlay-split-view > dimming {"
                b"background: none; border: none; box-shadow: none;}"

                # .sidebar-pane 右缘内描边（面板右边竖线）——含方向变体，彻底去掉。
                b"overlay-split-view .sidebar-pane,"
                b"overlay-split-view .sidebar-pane:dir(ltr),"
                b"overlay-split-view .sidebar-pane:dir(rtl),"
                b"overlay-split-view .sidebar-pane.end:dir(ltr),"
                b"overlay-split-view .sidebar-pane.end:dir(rtl) {"
                b"border: none; box-shadow: none;}"
                b"overlay-split-view > shadow,"
                b"overlay-split-view > shadow.left,"
                b"overlay-split-view > shadow.right {"
                b"background: none; background-image: none; box-shadow: none;}"
                # 注意：不给 .sidebar-pane 设背景色——它的背景由 _dynamic_css
                # （跟随封面时）或 style.css（兜底）负责。此处设了会因本 provider
                # 后注册而覆盖掉 _dynamic_css 的跟随色，导致圆角外露白框。
                b"overlay-split-view .sidebar-pane {"
                b"box-shadow: none;}"
            )
            Gtk.StyleContext.add_provider_for_display(
                display, _sv_prov,
                Gtk.STYLE_PROVIDER_PRIORITY_USER + 1000,
            )
            self._sv_css = _sv_prov

        # 订阅系统配色明暗变化：关闭「背景跟随封面」时，沉浸页前景
        # 需跟随系统明暗重算（开启时按封面，不受影响）。
        try:
            import gi
            gi.require_version("Adw", "1")
            from gi.repository import Adw
            _sm = Adw.StyleManager.get_default()
            _sm.connect("notify::dark", self._on_system_dark_changed)
        except Exception:
            log.debug("订阅系统配色变化失败", exc_info=True)

    def _on_system_dark_changed(self, *_args) -> None:
        """系统配色明暗变化：刷新沉浸页前景 + 重算主界面背景。"""
        try:
            np = getattr(self, "now_playing", None)
            if np is not None:
                np.refresh_dark_bg()
        except Exception:
            log.debug("刷新沉浸页前景失败", exc_info=True)
        # 主界面背景：用缓存的封面主色重算（暗色压暗、亮色提亮）。
        # 纯计算，不重新解码封面、不起后台线程。
        try:
            dom = getattr(self, "_current_dominant_rgb", None)
            if dom:
                from models import tint_for_background
                rgb = tint_for_background(dom, dark=self._current_theme_dark())
                self._current_bg_rgb = rgb
                self._apply_main_bg(rgb)
        except Exception:
            log.debug("重算主界面背景失败", exc_info=True)

    def _current_theme_dark(self) -> bool:
        """当前是否为暗色主题；查询失败按亮色处理。"""
        try:
            import gi
            gi.require_version("Adw", "1")
            from gi.repository import Adw
            return bool(Adw.StyleManager.get_default().get_dark())
        except Exception:
            return False

    # ============================================================
    # 启动闪屏（Splash）—— 逻辑已抽到 ui/splash.py，此处仅做转发
    # ============================================================
    def _has_library_to_load(self) -> bool:
        """是否有音乐目录需要加载（决定是否值得等封面）。"""
        try:
            return bool(get_config().get_music_dirs())
        except Exception:
            return False

    def _is_local_library_loaded(self) -> bool:
        """本地曲库是否已就绪（供闪屏判断可否淡出）。"""
        try:
            provider = self._get_provider(SOURCE_LOCAL)
            return bool(provider is not None and provider.get_library())
        except Exception:
            return False

    @staticmethod
    def _is_cover_busy() -> bool:
        """首屏是否有封面仍在加载。"""
        try:
            from ui.pages import cover_activity
            return bool(cover_activity.busy)
        except Exception:
            return False

    def _on_window_mapped(self, *_args) -> None:
        """窗口首次 map（显示）后交给闪屏控制器决定何时淡出。"""
        ctrl = getattr(self, "_splash_ctrl", None)
        if ctrl is not None:
            ctrl.on_window_mapped()

    def _on_cover_activity_changed(self, _obj, busy: bool) -> None:
        """封面加载忙闲变化：转发给闪屏控制器。"""
        ctrl = getattr(self, "_splash_ctrl", None)
        if ctrl is not None:
            ctrl.on_cover_activity_changed(busy)

    def _mark_splash_ready_if_loaded(self) -> None:
        """若首屏数据已就绪（缓存已填 / 曲库已有），请求淡出。"""
        ctrl = getattr(self, "_splash_ctrl", None)
        if ctrl is not None:
            ctrl.mark_ready_if_loaded()

    def _request_splash_fade(self) -> None:
        """请求淡出 splash（如扫描完成时调用）。"""
        ctrl = getattr(self, "_splash_ctrl", None)
        if ctrl is not None:
            ctrl.request_fade()

    # ============================================================
    # HeaderBar
    # ============================================================
    def _build_headerbar(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Box())

        left_box = Gtk.Box(spacing=12)
        left_box.set_valign(Gtk.Align.CENTER)
        # 侧栏显隐切换（窄窗口折叠时用于唤出浮层面板）
        self._sidebar_toggle_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic")
        self._sidebar_toggle_btn.add_css_class("flat")
        self._sidebar_toggle_btn.set_tooltip_text(_("显示 / 隐藏播放器面板"))
        self._sidebar_toggle_btn.set_active(True)
        left_box.append(self._sidebar_toggle_btn)
        # 导航项：显示名 → symbolic 图标（窄屏图标化用）。
        _nav_icons = {
            "home": "go-home-symbolic",
            "playlists": "view-list-symbolic",
            "local": "media-optical-symbolic",
            "liked": "xiatiao-heart-outline-symbolic",
        }
        self._nav_buttons: Dict[str, Gtk.Button] = {}
        self._nav_labels: Dict[str, Gtk.Label] = {}
        nav_container = Gtk.Box(spacing=2)
        nav_container.add_css_class("nav-segmented-bar")
        nav_container.set_valign(Gtk.Align.CENTER)
        for label, key in NAV_ITEMS:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("nav-pill")
            # 按钮内：图标 + 文字（窄屏时隐藏文字，只留图标）
            _inner = Gtk.Box(spacing=6)
            _icon = Gtk.Image.new_from_icon_name(_nav_icons.get(key, ""))
            _inner.append(_icon)
            _lbl = Gtk.Label(label=_(label))
            _inner.append(_lbl)
            btn.set_child(_inner)
            btn.set_tooltip_text(_(label))
            btn.connect("clicked", self._on_nav_clicked, key)
            nav_container.append(btn)
            self._nav_buttons[key] = btn
            self._nav_labels[key] = _lbl
        left_box.append(nav_container)
        # 导航按钮右侧：全局搜索框（搜当前页：本地曲库 / 我喜欢）
        left_box.append(Gtk.Box(spacing=8))
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text(_("搜索歌曲 / 歌手 / 专辑"))
        # 基准宽度 150；窄屏由断点收缩（见 _apply_responsive_breakpoints）。
        self._search_entry.set_size_request(150, -1)
        self._search_entry.set_valign(Gtk.Align.CENTER)
        self._search_entry.connect("search-changed", self._on_global_search)
        left_box.append(self._search_entry)
        header.pack_start(left_box)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_btn.add_css_class("flat")
        menu_btn.set_tooltip_text(_("菜单"))
        # 标准应用菜单（GNOME HIG：首选项 / 快捷键 / 关于 / 退出）
        menu = Gio.Menu()
        menu.append(_("设置"), "app.settings")
        menu.append(_("键盘快捷键"), "app.shortcuts")
        section = Gio.Menu()
        section.append(_("关于 Xiatiao"), "app.about")
        menu.append_section(None, section)
        section2 = Gio.Menu()
        section2.append(_("退出"), "app.quit")
        menu.append_section(None, section2)
        menu_btn.set_menu_model(menu)
        # 应用菜单（⋯）的 popover 也归入 media-menu，
        # 使其在「背景跟随封面」开启时用封面色、去白色描边。
        try:
            _pop = menu_btn.get_popover()
            if _pop is not None:
                _pop.add_css_class("media-menu")
        except Exception:
            log.debug("应用菜单 popover 加类失败", exc_info=True)
        act_settings = Gio.SimpleAction.new("settings", None)
        act_settings.connect("activate", lambda *_: self._on_open_settings())
        act_shortcuts = Gio.SimpleAction.new("shortcuts", None)
        # 跳到设置页的「快捷键」页（而非弹 GTK 快捷键窗口），
        # 与「音效 → 详细配置」跳到音效页同一模式。
        act_shortcuts.connect(
            "activate",
            lambda *_: self._on_open_settings(goto_shortcuts=True))
        act_about = Gio.SimpleAction.new("about", None)
        act_about.connect("activate", lambda *_: self._show_about())
        act_quit = Gio.SimpleAction.new("quit", None)
        act_quit.connect("activate", lambda *_: self._quit_app())
        act_group = Gio.SimpleActionGroup()
        act_group.add_action(act_settings)
        act_group.add_action(act_shortcuts)
        act_group.add_action(act_about)
        act_group.add_action(act_quit)
        self.insert_action_group("app", act_group)
        # 标准快捷键：Ctrl+, 设置 / Ctrl+? 快捷键 / Ctrl+Q 退出
        try:
            app = self.get_application()
            if app is not None:
                app.set_accels_for_action("app.settings", ["<Ctrl>comma"])
                app.set_accels_for_action("app.shortcuts", ["<Ctrl>question"])
                app.set_accels_for_action("app.quit", ["<Ctrl>q"])
        except Exception:
            log.debug("设置应用快捷键失败", exc_info=True)
        header.pack_end(menu_btn)

        self._user_avatar = self.player_panel.user_avatar
        return header

    # 左侧面板宽度由 Gtk.Paned 管理（set_resize_start_child(False)），
    # 无需 do_size_allocate 手动调宽（旧实现会触发布局循环 → 最大化卡顿）。

    # ============================================================
    # 右侧内容
    # ============================================================
    def _build_right_area(self) -> Gtk.ScrolledWindow:
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        right_panel.set_margin_start(12)
        right_panel.set_margin_end(12)
        right_panel.set_margin_top(12)
        right_panel.set_margin_bottom(12)
        right_panel.set_hexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        # 非均匀高度：Stack 高度取当前页，避免被高页撑开（导致主页被拉伸）
        try:
            self.stack.set_vhomogeneous(False)
            self.stack.set_hhomogeneous(False)
        except Exception:
            pass
        _track_actions = self._build_track_actions()
        # 主页：专辑 / 艺术家 / 历史（上下三块）
        from .home_page import HomePage
        self.home_page = HomePage(
            on_track_activated=self._on_local_track_activated,
            track_actions=_track_actions,
        )
        self.stack.add_named(self.home_page, "home")
        # 曲库（本地歌曲列表）
        self.local_page = LocalLibraryPage(
            on_track_activated=self._on_local_track_activated,
            track_actions=_track_actions,
            on_refresh=self._on_local_refresh,
            on_add_to_playlist=self._on_add_tracks_to_playlist,
        )
        self.stack.add_named(self.local_page, "local")
        # 收藏（原「我喜欢」）
        self.liked_page = LocalLibraryPage(
            on_track_activated=self._on_liked_track_activated,
            title="收藏",
            empty_text="还没有收藏的歌曲，点歌曲旁的小爱心即可收藏",
            track_actions=_track_actions,
        )
        self.stack.add_named(self.liked_page, "liked")
        # 歌单页
        from .playlists_page import PlaylistsPage
        self.playlists_page = PlaylistsPage(
            on_playlist_play=self._on_playlist_play,
            on_track_activated=self._on_local_track_activated,
            on_toast=self._toast,
            track_actions=_track_actions,
        )
        self.stack.add_named(self.playlists_page, "playlists")
        # 专辑 / 艺术家页（供主页复用其数据接口，不再单独作为导航页）
        self.albums_page = self.home_page.albums_page
        self.artists_page = self.home_page.artists_page
        # DSP 参数宿主：不再作为主界面页面展示（音效已并入设置），
        # 仅用 EffectPage 实例承载参数与同步接口（供高级窗口/设置使用）。
        from .effect_page import EffectPage
        _init_params = None
        try:
            _saved = get_config().get("dsp_params")
            if isinstance(_saved, dict):
                _init_params = _saved
        except Exception:
            _init_params = None
        self.dsp_page = EffectPage(
            on_dsp_changed=self._on_dsp_changed,
            initial=_init_params,
            on_coloring=self._on_coloring_changed,
        )
        right_panel.append(self.stack)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(right_panel)
        return scroll

    # ============================================================
    # 导航
    # ============================================================
    def _on_nav_clicked(self, _btn, key: str) -> None:
        self._switch_page(key)

    def _on_global_search(self, entry) -> None:
        """HeaderBar 搜索框：过滤当前页。"""
        text = entry.get_text().strip()
        page = None
        if self._active_source == "local":
            page = getattr(self, "local_page", None)
        elif self._active_source == "liked":
            page = getattr(self, "liked_page", None)
        elif self._active_source == "home":
            # 主页：过滤专辑 / 艺术家两块（历史不过滤）
            for pg in (getattr(self.home_page, "albums_page", None),
                       getattr(self.home_page, "artists_page", None)):
                if pg is not None and hasattr(pg, "set_filter_text"):
                    pg.set_filter_text(text)
            return
        elif self._active_source == "playlists":
            # 歌单页不支持搜索过滤
            return
        elif self._active_source == "albums":
            page = getattr(self, "albums_page", None)
        elif self._active_source == "artists":
            page = getattr(self, "artists_page", None)
        if page is not None and hasattr(page, "set_filter_text"):
            page.set_filter_text(text)

    def _switch_page(self, key: str) -> None:
        # 只做「立即」的事：切页 + 高亮导航（不阻塞）
        self._active_source = key
        self.stack.set_visible_child_name(key)
        for k, btn in self._nav_buttons.items():
            if k == key:
                btn.add_css_class("nav-active")
            else:
                btn.remove_css_class("nav-active")
        # 数据准备丢到主循环空闲时做（避免切页瞬间卡一下）
        GLib.idle_add(self._fill_page_data, key)

    def _fill_page_data(self, key: str) -> bool:
        """idle 回调：切页后异步填数据（读缓存/provider + 渲染）。"""
        try:
            # 同步搜索关键词到新页
            if getattr(self, "_search_entry", None) is not None:
                self._on_global_search(self._search_entry)
            if key == "local":
                self._ensure_local_provider()
            if key == "home":
                self._refresh_home_page()
            if key == "liked":
                self._refresh_liked_page()
            if key == "playlists":
                try:
                    self.playlists_page.refresh()
                except Exception:
                    pass
            if key in ("albums", "artists"):
                pg = getattr(self, f"{key}_page", None)
                provider = self._get_provider(SOURCE_LOCAL)
                tracks = None
                if provider is not None and provider.get_library():
                    tracks = provider.get_library()
                else:
                    from core.cache import get_cache
                    loc = get_cache().get("local")
                    if isinstance(loc, dict):
                        tracks = list_to_tracks(loc.get("tracks"))
                if pg is not None:
                    if tracks is not None:
                        # 复用全局曲库指纹（避免每次切页都重算 MD5：O(n log n)）
                        fp = getattr(self, "_local_fingerprint", None)
                        if fp is None:
                            fp = self._tracks_fingerprint(tracks)
                            self._local_fingerprint = fp
                        # 页面脏（扫描后未渲染）或数据指纹变化时才重设
                        dirty = getattr(self, "_page_dirty", {}).get(key, False)
                        if dirty or getattr(pg, "_data_fp", None) != fp:
                            pg._data_fp = fp
                            pg.set_tracks(tracks)
                            try:
                                self._page_dirty[key] = False
                            except Exception:
                                pass
                    pg.set_view_mode("albums" if key == "albums" else "artists")
        except Exception as exc:
            log.debug("切页填数据失败: %s", exc)
        return False

    # ============================================================
    # Provider
    # ============================================================
    def _get_provider(self, source_type: str):
        if source_type not in self._providers:
            provider = create_provider(source_type)
            if provider is None:
                return None
            provider.connect("library-changed", self._on_library_changed, source_type)
            provider.connect("error", self._on_provider_error)
            self._providers[source_type] = provider
        return self._providers[source_type]

    def _ensure_local_provider(self) -> None:
        provider = self._get_provider(SOURCE_LOCAL)
        if provider is None:
            return
        if not provider.get_library() and get_config().get_music_dirs():
            provider.refresh()

    def _initial_scan(self) -> None:
        if not get_config().get_music_dirs():
            return
        provider = self._get_provider(SOURCE_LOCAL)
        if provider is None:
            return
        if provider.get_library():
            return
        if get_config().get_bool("auto_scan_on_start", True):
            provider.refresh()

    def _on_local_refresh(self) -> None:
        provider = self._get_provider(SOURCE_LOCAL)
        if provider is None:
            return
        self._toast(_("正在重新扫描本地曲库…"))
        provider.refresh()

    def _on_library_changed(self, _provider, source_type: str) -> None:
        if source_type == SOURCE_LOCAL:
            provider = self._providers.get(SOURCE_LOCAL)
            if provider is not None:
                tracks = provider.get_library()
                fp = self._tracks_fingerprint(tracks)
                if fp != getattr(self, "_local_fingerprint", None):
                    self._local_fingerprint = fp
                    # 只重渲染「当前活动页」：非活动页标记为脏，切到该页时再渲染。
                    # 避免一次扫描后 local/albums/artists 三页同时重分组重渲染。
                    self._page_dirty = {
                        "local": True, "albums": True, "artists": True, "liked": False,
                    }
                    self._render_active_page(tracks)
                get_cache().set("local", {"tracks": tracks_to_list(tracks)})
                get_cache().save()
                # 扫描完成、曲库已就绪 → 隐藏 splash
                try:
                    self._request_splash_fade()
                except Exception:
                    pass

    def _render_active_page(self, tracks) -> None:
        """按当前活动页渲染曲库数据，并清除该页脏标记。"""
        key = self._active_source
        try:
            if key == "local":
                self.local_page.set_tracks(tracks)
            elif key == "albums":
                self.albums_page.set_tracks(tracks)
            elif key == "artists":
                self.artists_page.set_tracks(tracks)
            elif key == "liked":
                # 喜欢页不随曲库刷新重建（除非当前正在看）
                return
            self._page_dirty[key] = False
        except Exception:
            pass

    @staticmethod
    def _tracks_fingerprint(tracks) -> str:
        import hashlib
        paths = sorted(getattr(t, "filepath", "") for t in (tracks or []))
        return hashlib.md5("\n".join(paths).encode("utf-8")).hexdigest()

    def _on_provider_error(self, _provider, message: str) -> None:
        log.warning("Provider 错误: %s", message)
        self._toast(message)

    # ============================================================
    # 我喜欢
    # ============================================================
    def _refresh_home_history(self) -> None:
        """只刷新主页的历史块（切歌时调用，避免重建专辑/艺术家网格）。"""
        try:
            from core.history_store import get_history_store
            from models import TrackItem as _TI
            rows = get_history_store().all_rows()
            # 曲库索引（拿技术参数）
            lib_index = {}
            try:
                prov = self._get_provider(SOURCE_LOCAL)
                lib = prov.get_library() if prov is not None else None
                if not lib:
                    from core.cache import get_cache as _gc
                    _loc = _gc().get("local")
                    lib = list_to_tracks(_loc.get("tracks")) if isinstance(_loc, dict) else []
                for t in (lib or []):
                    k1 = getattr(t, "filepath", "") or ""
                    k2 = getattr(t, "source_id", "") or ""
                    if k1:
                        lib_index[k1] = t
                    if k2:
                        lib_index[k2] = t
            except Exception:
                lib_index = {}
            hist = []
            for r in rows:
                try:
                    fp = r.get("filepath") or ""
                    sid = r.get("source_id") or ""
                    hit = lib_index.get(fp) or lib_index.get(sid)
                    if hit is not None:
                        hist.append(hit)
                        continue
                    hist.append(_TI(
                        title=r.get("title") or "未知歌曲",
                        artist=r.get("artist") or "未知歌手",
                        album=r.get("album") or "",
                        duration=r.get("duration") or "0:00",
                        duration_seconds=float(r.get("duration_seconds") or 0.0),
                        filepath=fp,
                        source_type=r.get("source_type") or SOURCE_LOCAL,
                        source_id=sid,
                        cover_url=r.get("cover_url") or "",
                    ))
                except Exception:
                    continue
            self.home_page.set_history(hist)
            # 同步指纹：否则下次切回主页时 _refresh_home_page 会判定「变了」
            # 而再重建一次历史列表。
            try:
                self._home_hist_fp = tuple(
                    (r.get("filepath") or "", r.get("source_id") or "") for r in rows
                )
            except Exception:
                pass
        except Exception as exc:
            log.debug("刷新主页历史失败: %s", exc)

    def _refresh_home_page(self) -> None:
        """主页数据：曲库喂专辑/艺术家，播放历史喂历史块。

        带指纹去重：曲库/历史没变时不重渲染。此前每次切回主页都无条件
        重建专辑+艺术家两个网格（几百张卡片），是切页卡顿的主因。
        """
        try:
            provider = self._get_provider(SOURCE_LOCAL)
            tracks = None
            if provider is not None and provider.get_library():
                tracks = provider.get_library()
            else:
                from core.cache import get_cache
                loc = get_cache().get("local")
                if isinstance(loc, dict):
                    tracks = list_to_tracks(loc.get("tracks"))
            if tracks is not None:
                # 复用全局曲库指纹（扫描后由 _on_library_changed 更新），
                # 避免每次切页都重算 O(n log n) 的 MD5。
                fp = getattr(self, "_local_fingerprint", None)
                if fp is None:
                    fp = self._tracks_fingerprint(tracks)
                    self._local_fingerprint = fp
                if fp != getattr(self, "_home_lib_fp", None):
                    self._home_lib_fp = fp
                    self.home_page.set_library(tracks)
        except Exception as exc:
            log.debug("主页曲库填充失败: %s", exc)
        # 历史：优先用曲库里的同一对象（带格式/采样率等技术参数），
        # 避免在历史库重复存这些字段。匹配不到则回退历史快照。
        try:
            from core.history_store import get_history_store
            from models import TrackItem as _TI
            rows = get_history_store().all_rows()
            # 建曲库索引：filepath / source_id → TrackItem
            lib_index = {}
            try:
                prov = self._get_provider(SOURCE_LOCAL)
                lib = prov.get_library() if prov is not None else None
                if not lib:
                    from core.cache import get_cache as _gc
                    _loc = _gc().get("local")
                    lib = list_to_tracks(_loc.get("tracks")) if isinstance(_loc, dict) else []
                for t in (lib or []):
                    k1 = getattr(t, "filepath", "") or ""
                    k2 = getattr(t, "source_id", "") or ""
                    if k1:
                        lib_index[k1] = t
                    if k2:
                        lib_index[k2] = t
            except Exception:
                lib_index = {}
            hist = []
            for r in rows:
                try:
                    fp = r.get("filepath") or ""
                    sid = r.get("source_id") or ""
                    hit = lib_index.get(fp) or lib_index.get(sid)
                    if hit is not None:
                        # 用曲库对象：带完整技术参数
                        hist.append(hit)
                        continue
                    # 回退：历史快照（无技术参数）
                    hist.append(_TI(
                        title=r.get("title") or "未知歌曲",
                        artist=r.get("artist") or "未知歌手",
                        album=r.get("album") or "",
                        duration=r.get("duration") or "0:00",
                        duration_seconds=float(r.get("duration_seconds") or 0.0),
                        filepath=fp,
                        source_type=r.get("source_type") or SOURCE_LOCAL,
                        source_id=sid,
                        cover_url=r.get("cover_url") or "",
                    ))
                except Exception:
                    continue
            # 历史同样去重：rows 未变时不重建历史列表
            hfp = tuple((r.get("filepath") or "", r.get("source_id") or "") for r in rows)
            if hfp != getattr(self, "_home_hist_fp", None):
                self._home_hist_fp = hfp
                self.home_page.set_history(hist)
        except Exception as exc:
            log.debug("主页历史填充失败: %s", exc)

    def _refresh_liked_page(self) -> None:
        try:
            rows = get_liked_store().all_rows()
        except Exception:
            rows = []
        tracks = []
        for r in rows:
            try:
                tracks.append(TrackItem(
                    title=r.get("title") or "未知歌曲",
                    artist=r.get("artist") or "未知歌手",
                    album=r.get("album") or "",
                    duration=r.get("duration") or "0:00",
                    duration_seconds=float(r.get("duration_seconds") or 0.0),
                    filepath=r.get("filepath") or "",
                    source_type=r.get("source_type") or SOURCE_LOCAL,
                    source_id=r.get("source_id") or "",
                    cover_url=r.get("cover_url") or "",
                ))
            except Exception:
                continue
        self.liked_page.set_tracks(tracks)

    def _liked_rows_to_tracks(self):
        try:
            rows = get_liked_store().all_rows()
        except Exception:
            rows = []
        tracks = []
        for r in rows:
            try:
                tracks.append(TrackItem(
                    title=r.get("title") or "未知歌曲",
                    artist=r.get("artist") or "未知歌手",
                    album=r.get("album") or "",
                    duration=r.get("duration") or "0:00",
                    duration_seconds=float(r.get("duration_seconds") or 0.0),
                    filepath=r.get("filepath") or "",
                    source_type=r.get("source_type") or SOURCE_LOCAL,
                    source_id=r.get("source_id") or "",
                    cover_url=r.get("cover_url") or "",
                ))
            except Exception:
                continue
        return tracks

    def _on_liked_track_activated(self, track: TrackItem) -> None:
        tracks = self._liked_rows_to_tracks()
        if not tracks:
            return
        key = getattr(track, "filepath", "") or getattr(track, "source_id", "")
        # 喜欢页队列指纹：一致时直接切索引，避免重建
        import hashlib as _hl
        fp = "liked:" + _hl.md5(
            "\n".join(getattr(t, "filepath", "") or getattr(t, "source_id", "") for t in tracks).encode("utf-8")
        ).hexdigest()
        if fp == getattr(self, "_queue_source_fp", None):
            cur = self.playlist.tracks()
            for i, t in enumerate(cur):
                if (getattr(t, "filepath", "") or getattr(t, "source_id", "")) == key:
                    self.playlist.set_current_index(i)
                    return
        index = 0
        for i, t in enumerate(tracks):
            if (getattr(t, "filepath", "") or getattr(t, "source_id", "")) == key:
                index = i
                break
        self.playlist.set_tracks(tracks, autoplay_index=index)
        self._queue_source_fp = fp

    # ============================================================
    # 本地播放
    # ============================================================
    def _on_local_track_activated(self, track: TrackItem) -> None:
        provider = self._get_provider(SOURCE_LOCAL)
        if provider is None:
            return
        key = getattr(track, "filepath", "") or getattr(track, "source_id", "")
        lib = provider.get_library()
        fp = getattr(self, "_local_fingerprint", None) or self._tracks_fingerprint(lib)
        # 队列已是同一份曲库（指纹一致）时，直接切索引，避免重建整个队列
        # （set_tracks 会 emit changed + 重建洗牌顺序，歌多时明显耗时）。
        if fp == getattr(self, "_queue_source_fp", None):
            cur = self.playlist.tracks()
            for i, t in enumerate(cur):
                if (getattr(t, "filepath", "") or getattr(t, "source_id", "")) == key:
                    self.playlist.set_current_index(i)
                    return
        index = 0
        for i, t in enumerate(lib):
            if (getattr(t, "filepath", "") or getattr(t, "source_id", "")) == key:
                index = i
                break
        self.playlist.set_tracks(lib, autoplay_index=index)
        self._queue_source_fp = fp

    def _on_like_current(self) -> None:
        track = self.playlist.current_track()
        if track is None:
            self._toast("当前没有播放曲目")
            return
        liked = get_liked_store().toggle(track)
        self.player_panel.set_liked(liked)
        self._toast("已加入我喜欢" if liked else "已取消喜欢")
        if self._active_source == "liked":
            self._refresh_liked_page()

    def _on_add_current_to_playlist(self) -> None:
        """把当前播放曲目加入歌单（弹出歌单选择对话框）。"""
        track = self.playlist.current_track()
        if track is None:
            self._toast("当前没有播放曲目")
            return
        self._on_add_tracks_to_playlist([track])

    def _on_cast_current(self) -> None:
        """投送当前曲目到局域网 DLNA 设备（弹出设备选择对话框）。"""
        from .cast_dialog import CastDialog
        track = self.playlist.current_track()
        if track is None:
            self._toast("当前没有播放曲目")
            return
        if not getattr(track, "filepath", ""):
            self._toast("仅支持投送本地曲目")
            return
        try:
            dlg = CastDialog(
                self, track, on_toast=self._toast,
                on_cast_started=self._on_cast_started,
                on_cast_stopped=self._on_cast_stopped,
            )
            dlg.present(self)
            self._cast_dlg = dlg
        except Exception:
            log.debug("打开投送对话框失败", exc_info=True)

    def _on_cast_started(self) -> None:
        """投送成功：进入投送模式，播放控制改道到远端设备。"""
        self._dlna_casting = True
        self._dlna_remote_playing = True
        # 本机若正在播放，停掉（B1：本机不放）。
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.player_panel.set_playing(True)
            self.now_playing.set_playing(True)
        except Exception:
            pass
        self._start_cast_poll()
        self._toast("已进入投送模式：播放控制将发送到设备")

    def _on_cast_stopped(self) -> None:
        """停止投送：退出投送模式，控制恢复本机。"""
        self._dlna_casting = False
        self._dlna_remote_playing = False
        self._stop_cast_poll()
        try:
            self.player_panel.set_playing(False)
            self.now_playing.set_playing(False)
        except Exception:
            pass
        self._toast("已退出投送模式")

    def _start_cast_poll(self) -> None:
        """启动投送位置轮询：每秒查询远端播放位置，更新进度条。"""
        self._stop_cast_poll()
        # 单独后台线程查询（SOAP 是阻塞网络调用），结果经 GLib.idle_add 回主线程。
        import threading
        stop_flag = threading.Event()
        self._cast_poll_stop = stop_flag

        def _loop():
            while not stop_flag.is_set():
                if not getattr(self, "_dlna_casting", False):
                    break
                try:
                    from core.dlna_push import get_dlna_pusher
                    pusher = get_dlna_pusher()
                    # 仅同步进度；播放/暂停状态以本机操作为准，
                    # 不让轮询覆盖（否则设备状态上报延迟会与本机意图
                    # 打架，导致「点播放却发了暂停」的错乱）。
                    info = pusher.get_position()
                    if info:
                        GLib.idle_add(self._apply_cast_position, info)
                except Exception:
                    pass
                stop_flag.wait(1.0)

        threading.Thread(target=_loop, daemon=True).start()

    def _stop_cast_poll(self) -> None:
        flag = getattr(self, "_cast_poll_stop", None)
        if flag is not None:
            try:
                flag.set()
            except Exception:
                pass
        self._cast_poll_stop = None

    def _apply_cast_position(self, info: dict) -> bool:
        """主线程：用远端查询到的位置/时长更新进度条。"""
        if not getattr(self, "_dlna_casting", False):
            return False
        try:
            pos = float(info.get("position", 0.0))
            dur = float(info.get("duration", 0.0))
        except Exception:
            return False
        # 标准 DLNA：进度直接采用设备上报的位置/时长（渲染器是权威）。
        try:
            self.player_panel.set_position(pos)
            self.now_playing.set_position(pos)
            if dur > 0:
                self.player_panel.set_duration(dur)
                self.now_playing.set_duration(dur)
        except Exception:
            pass
        return False

    # 注：不再用轮询同步播放/暂停状态（曾导致本机意图被设备延迟覆盖）。
    # 播放/暂停/切歌状态一律由本机操作权威设置；轮询只负责进度。

    def _on_playlist_current_changed(self, _playlist, index: int) -> None:
        """当前曲目变化：协调 UI 更新、历史记录、播放启动、资产加载。"""
        track = self.playlist.current_track()
        if track is None:
            return
        restoring = getattr(self, "_restoring", False)

        self._update_now_playing_ui(track)
        self._record_play_history(track)
        # DLNA 投送模式：切歌时把新曲目推给远端设备，本机不播放。
        if self._is_casting():
            self._cast_track(track)
        else:
            self._start_playback_if_needed(track, restoring)
        self._dispatch_track_assets(track, restoring)

    def _cast_track(self, track) -> None:
        """把指定曲目推送到当前选中的 DLNA 设备。

        保持当前播放/暂停状态：若处于暂停态，推完 URI 后立即暂停，
        不强制出声；并同步播放按钮状态。
        """
        fp = getattr(track, "filepath", "") or ""
        if not fp:
            return
        was_playing = bool(getattr(self, "_dlna_remote_playing", True))
        try:
            from core.dlna_push import get_dlna_pusher
            pusher = get_dlna_pusher()
            if pusher.current_device() is None:
                return
            # push() 内含 Play；暂停态时随后立即 Pause 收回。
            pusher.push(fp, getattr(track, "title", "") or "",
                        getattr(track, "artist", "") or "")
            if not was_playing:
                pusher.pause()
            # 同步按钮：保持切歌前的播放/暂停状态。
            try:
                self.player_panel.set_playing(was_playing)
                self.now_playing.set_playing(was_playing)
            except Exception:
                pass
        except Exception:
            log.debug("DLNA 投送切歌失败", exc_info=True)

    def _update_now_playing_ui(self, track) -> None:
        """同步两侧 UI 的曲目信息（轻量、立即响应）。"""
        try:
            self.player_panel.set_liked(get_liked_store().is_liked(track))
        except Exception:
            pass
        try:
            self.player_panel.reset_position()
            self.now_playing.reset_position()
            self.player_panel.set_track_info(track.title, track.artist)
            self.player_panel.set_format_info(track.format_label)
            self.now_playing.set_track(track.title, track.artist)
        except Exception:
            pass
        # 通知本地曲库页记录当前播放曲目（供「定位当前播放」按钮使用）
        try:
            self.local_page.set_now_playing(track)
        except Exception:
            pass

    def _record_play_history(self, track) -> None:
        """记录播放历史；若主页正显示则刷新其历史块。"""
        try:
            from core.history_store import get_history_store
            get_history_store().record(track)
        except Exception:
            pass
        try:
            if getattr(self, "_active_source", None) == "home":
                self._refresh_home_history()
        except Exception:
            pass

    def _start_playback_if_needed(self, track, restoring: bool) -> None:
        """非恢复态且为本地曲目时，立即开始播放（不等封面加载）。"""
        if restoring or not track.is_local:
            return
        if self.playlist.is_playable(track):
            self.player.play_file(track.play_url)
            # 立即同步播放按钮为"播放中"：后端 state 事件是异步的，
            # 不主动更新会等解码启动才变图标。
            try:
                self.player_panel.set_playing(True)
                self.now_playing.set_playing(True)
            except Exception:
                pass

    def _dispatch_track_assets(self, track, restoring: bool) -> None:
        """在专用后台线程加载封面/歌词/ReplayGain（重活，避免切歌卡顿）。"""
        is_local = bool(track.is_local)
        filepath = track.filepath
        token = id(track)          # 用于丢弃过期结果（快速连切时）
        self._track_token = token
        # 记住当前曲目/文件路径，供设置页切换背景开关时即时重应用
        self._current_track = track
        self._current_filepath = filepath
        self._current_is_local = is_local
        import time as _tt
        self._track_click_t0 = _tt.monotonic()
        # 主线程判断当前是否在沉浸页（GTK 调用必须在主线程）
        want_color = False
        try:
            want_color = (self._main_stack.get_visible_child_name() == "nowplaying")
        except Exception:
            want_color = False
        # 用独立专用线程：不与其他批量任务争抢公共线程池，
        # 避免点击后封面迟迟不出现的等待感。
        import threading
        threading.Thread(
            target=self._load_track_assets_bg,
            args=(token, filepath, is_local, track, restoring, want_color),
            daemon=True,
            name="xiatiao-track-assets",
        ).start()

    def _apply_instant_cover_placeholder(self, filepath: str) -> None:
        """切歌瞬间：若列表封面缓存里已有这首歌的小图，先用它占位。

        目的：让封面区域和歌名一样"立即"有内容，不再空等后台线程。
        小图后续会被高清封面替换（后台线程完成后）。
        """
        if not filepath:
            return
        try:
            from ui.pages import _cache_get, _MISS
            tex = _cache_get(filepath)
            if tex is _MISS or tex is None:
                return
            # 列表小封面（40px）先顶上，避免空白/占位符
            self.player_panel.cover.set_cover_texture(tex)
        except Exception:
            pass

    def _load_track_assets_bg(self, token: int, filepath: str, is_local: bool,
                              track, restoring: bool, want_color: bool = False) -> None:
        """专用后台线程：加载封面 / 歌词 / ReplayGain 并预缩放封面（不碰 UI）。

        want_color 由主线程传入（是否在沉浸页，决定是否需要算主色）。
        """
        import time as _t
        _t0 = _t.monotonic()
        cover_raw = extract_cover(filepath) if is_local else None
        _t1 = _t.monotonic()
        # 封面处理（缩放/主色/背景模糊/亮度）委托给 services/track_assets.py
        from services.track_assets import load_cover_assets
        from config.settings import get_config as _get_cfg
        _cfg = _get_cfg()
        sz_panel = getattr(getattr(self, "player_panel", None), "cover", None)
        p_size = getattr(sz_panel, "cover_size", 320)
        n_size = getattr(getattr(self, "now_playing", None), "_COVER_PX", 320)
        # 主界面背景色需知道当前主题明暗（暗色要压暗而非提亮）。
        # 后台线程读 Adw.StyleManager 是只读查询，安全；失败则按亮色处理。
        try:
            import gi
            gi.require_version("Adw", "1")
            from gi.repository import Adw as _Adw
            _dark_theme = bool(_Adw.StyleManager.get_default().get_dark())
        except Exception:
            _dark_theme = False
        assets = load_cover_assets(
            cover_raw, p_size, n_size,
            blur_on=_cfg.get_bool("nowplaying_blur_bg", True),
            blur_px=_cfg.get_int("nowplaying_blur_px", 6),
            dark_threshold=_cfg.get("nowplaying_dark_threshold", 0.65),
            dark_theme=_dark_theme,
        )
        cover_panel_tex = assets["panel_tex"]
        cover_np_tex = assets["np_tex"]
        bg_tex = assets["bg_tex"]
        bg_dark = assets["bg_dark"]
        bg_rgb = assets["bg_rgb"]
        main_bg_rgb = assets.get("main_bg_rgb")
        seekbar_rgb = assets["seekbar_rgb"]
        try:
            self._pending_cover_raw = assets["pending_cover_raw"]
        except Exception:
            pass
        _t2 = _t.monotonic()
        lyrics = load_lyrics(filepath) if is_local else []
        _t3 = _t.monotonic()
        rg_gain = None
        if not restoring:
            try:
                rg_gain = self._compute_replaygain(track)
            except Exception:
                rg_gain = None
        _t4 = _t.monotonic()
        log.debug("[切歌] extract=%.0fms scale=%.0fms lyrics=%.0fms rg=%.0fms | %s",
                  (_t1 - _t0) * 1000, (_t2 - _t1) * 1000,
                  (_t3 - _t2) * 1000, (_t4 - _t3) * 1000,
                  filepath.split("/")[-1])
        self._track_click_t0 = getattr(self, "_track_click_t0", None)
        # MPRIS 封面：原始字节交给主线程，在 token 校验通过后再落盘。
        # 不能在后台线程落盘——快速切歌时旧线程晚到会覆盖新封面。
        GLib.idle_add(self._apply_track_assets, token, cover_panel_tex, cover_np_tex,
                      lyrics, rg_gain, bg_rgb, seekbar_rgb, bg_tex, bg_dark,
                      cover_raw, main_bg_rgb, assets.get("dominant_rgb"),
                      assets.get("bg_png_path"), assets.get("popover_bg_path"))

    def _apply_track_assets(self, token: int, cover_panel_tex, cover_np_tex, lyrics, rg_gain,
                            bg_rgb=None, seekbar_rgb=None, bg_tex=None, bg_dark=None,
                            mpris_cover_raw=None, main_bg_rgb=None, dominant_rgb=None,
                            bg_png_path=None, popover_bg_path=None) -> bool:
        """主线程：应用后台已建好的 GdkTexture / 歌词 / ReplayGain。

        封面纹理在后台线程已解码完成，这里只 set_paintable，几乎零耗时。
        """
        if getattr(self, "_track_token", None) != token:
            return False   # 已被更新的切歌请求取代，丢弃
        try:
            import time as _t
            _e2e = (_t.monotonic() - getattr(self, "_track_click_t0", _t.monotonic())) * 1000
            log.debug("[切歌] 点击到应用封面 = %.0fms", _e2e)
        except Exception:
            pass
        # 沉浸页背景色：已在后台线程算好并传入（bg_rgb/seekbar_rgb），
        # 主线程不做取色（取色解码大图会卡住 UI）。
        try:
            if cover_panel_tex is not None:
                self.player_panel.cover.set_cover_texture(cover_panel_tex)
            else:
                # 无封面：清空为占位，避免沿用上一首封面
                self.player_panel.cover.set_cover(None)
            self.now_playing.set_cover_texture(cover_np_tex, bg_rgb, seekbar_rgb)
            # 沉浸页背景：封面模糊图铺满（None 时清空 → 回退纯色背景）
            self.now_playing.set_bg_texture(bg_tex, bg_dark)
            # 记录模糊图路径/明暗，供沉浸页音效气泡作背景
            # 气泡用专用「超糊」图（比沉浸页背景更糊）。
            self._immersive_bg_path = popover_bg_path or bg_png_path
            self._immersive_bg_dark = bg_dark
            # 主界面背景跟随封面（独立开关，用 main_bg_rgb，不受沉浸页模糊开关影响）
            self._current_bg_rgb = main_bg_rgb
            # 缓存封面主色：系统切明暗时据此重算背景（纯计算，不重新解码封面）
            self._current_dominant_rgb = dominant_rgb
            self._apply_main_bg(main_bg_rgb)
            # 主界面左侧进度条也跟随封面主色
            self.player_panel.set_progress_color(seekbar_rgb)
            self.now_playing.set_lyrics(lyrics)
            self.player_panel.set_lyrics(lyrics)
        except Exception:
            # 本块包含封面/背景/歌词等多项 UI 更新，任一步失败都会中断其后
            # 调用（曾因未定义变量导致歌词无声消失）。此处仅记 debug 日志，
            # 保持容错不崩，但排查时（日志级别调 DEBUG）可定位到具体失败点。
            log.debug("应用曲目资产失败（封面/背景/歌词可能未更新）", exc_info=True)
        # 更新 MPRIS 封面（供 KDE Connect 等同步到手机通知栏）。
        # 落盘放在 token 校验之后的主线程，确保文件内容与当前曲目一致；
        # 文件名带 token，artUrl 每次变化，客户端不会命中旧缓存。
        try:
            mpris = getattr(self, "_mpris", None)
            if mpris is not None:
                cover_path = (_write_mpris_cover(mpris_cover_raw, token)
                              if mpris_cover_raw else None)
                mpris.set_cover_path(cover_path)
        except Exception:
            log.debug("更新 MPRIS 封面失败", exc_info=True)
        if rg_gain is not None:
            self._apply_replaygain_value(rg_gain)
        return False

    def _compute_replaygain(self, track):
        """读取当前曲目的 ReplayGain 增益值（返回 float 或 None，不碰 UI/后端）。"""
        try:
            cfg = get_config()
            params = cfg.get("dsp_params")
            if not isinstance(params, dict):
                return None
            if not params.get("replaygain_enabled"):
                return None
            from models.replaygain import read_replaygain
            mode = params.get("replaygain_mode", "track")
            path = getattr(track, "filepath", "") or ""
            return read_replaygain(path, mode) if path else None
        except Exception as exc:
            log.debug("计算 ReplayGain 失败: %s", exc)
            return None

    def _apply_replaygain_value(self, gain) -> None:
        """主线程：把 ReplayGain 增益下发 DSP 并持久化。

        修复（关键）：不再「重读 config + 直接 set_dsp」。
        旧实现会带来竞态：切歌的 ReplayGain 是后台线程算的，回到主线程时
        用户可能已切换总开关；此时重读 config 可能拿到旧值（enabled=false），
        直接 set_dsp 会把刚打开的总开关打回去，且绕过统一下发（不发 YAML），
        导致 Camilla 卷积等不恢复。

        现在改为：以主窗口参数宿主 dsp_page._params 为权威当前值，
        只改 replaygain_db，走 _push_dsp_to_engine 统一下发（含 Camilla YAML），
        不触碰 enabled 等其它字段。
        """
        try:
            params = None
            page = getattr(self, "dsp_page", None)
            if page is not None:
                try:
                    params = page.params()
                except Exception:
                    params = None
            if not isinstance(params, dict):
                # 回退：从 config 读（兼容无 dsp_page 的场景）
                cfg = get_config()
                _p = cfg.get("dsp_params")
                params = dict(_p) if isinstance(_p, dict) else None
            if not isinstance(params, dict):
                return
            params["replaygain_db"] = float(gain) if gain is not None else 0.0
            # 走单一真相源：写 DspState（广播 → 各视图刷新 + 统一下发）。
            # 旧实现直接 set_dsp 且重读 config，会与总开关切换竞态（已修）。
            wrote = False
            if getattr(self, "_dsp_state", None) is not None:
                try:
                    # 只改 replaygain_db，不碰其它字段（避免覆盖 enabled 等）
                    self._dsp_state.patch({"replaygain_db": params["replaygain_db"]}, source=self)
                    wrote = True
                except Exception:
                    wrote = False
            if not wrote:
                self._push_dsp_to_engine(params, debounce=False)
            get_config().set("dsp_params", params)
        except Exception as exc:
            log.debug("应用 ReplayGain 失败: %s", exc)

    def _apply_replaygain(self, track) -> None:
        """兼容旧调用：同步计算并应用（保留给其它调用点）。"""
        gain = self._compute_replaygain(track)
        if gain is not None:
            self._apply_replaygain_value(gain)

    # ============================================================
    # 播放控制
    # ============================================================
    def _is_casting(self) -> bool:
        """当前是否处于 DLNA 投送模式（控制发给远端设备）。"""
        return bool(getattr(self, "_dlna_casting", False))

    def _on_play_pause(self) -> None:
        # DLNA 投送模式：控制发给远端设备（本机不放）。
        if self._is_casting():
            try:
                from core.dlna_push import get_dlna_pusher
                pusher = get_dlna_pusher()
                # 状态完全由本机操作维护（轮询不再改它），方向判断可靠：
                # 正在播放 → 本次暂停；已暂停 → 本次恢复。
                is_playing = bool(getattr(self, "_dlna_remote_playing", True))
                log.debug("[投送] 播放键: _dlna_remote_playing=%s → 发送 %s",
                          is_playing, "Pause" if is_playing else "Play")
                if is_playing:
                    pusher.pause()
                    new_state = False
                else:
                    pusher.resume()
                    new_state = True
                self._dlna_remote_playing = new_state
                self.player_panel.set_playing(new_state)
                self.now_playing.set_playing(new_state)
            except Exception:
                log.debug("DLNA 投送播放/暂停失败", exc_info=True)
            return
        state = self.player.state()
        if state == "playing":
            self.player.pause()
        elif state == "paused":
            self.player.resume()
        else:
            track = self.playlist.current_track()
            if track is None:
                if not self.playlist.is_empty():
                    self.playlist.set_current_index(0)
            else:
                self._on_playlist_current_changed(self.playlist, self.playlist.current_index())

    def _on_prev(self) -> None:
        self.playlist.previous()

    def _on_next(self) -> None:
        self.playlist.next()

    def _on_seek(self, seconds: float) -> None:
        # DLNA 投送模式：seek 发给远端设备（AVTransport Seek）。
        if self._is_casting():
            try:
                from core.dlna_push import get_dlna_pusher
                get_dlna_pusher().seek(seconds)
                # 远端进度无法实时回读，本地进度条先跳到目标位置。
                self.player_panel.set_position(seconds)
                self.now_playing.set_position(seconds)
            except Exception:
                log.debug("DLNA 投送 seek 失败", exc_info=True)
            return
        self.player.seek_seconds(seconds)

    def _on_volume(self, value: float) -> None:
        self.player.set_volume(value)
        self.now_playing.set_volume(value)

    def _on_effect(self, key: str) -> None:
        """音效预设被选中：应用对应 DSP 参数并持久化。"""
        try:
            from core.eq_presets import get_preset
            params = get_preset(key)
            if params is None:
                # 不是内置预设 → 试「我的预设」（DSP 设置页保存的自定义预设）
                try:
                    from core.dsp_store import get_dsp_preset_store
                    params = get_dsp_preset_store().get(key)
                except Exception:
                    params = None
            if params is None:
                # 未知键（如旧的 off/pop 等）回退到设置页
                self._on_open_effect_settings()
                return
            # 重构后：统一写 DspState（唯一真相源）→ 广播 → 所有视图自动刷新，
            # 由 _on_dsp_state_changed 统一出口下发。不再手工逐个视图同步。
            _wrote = False
            if getattr(self, "_dsp_state", None) is not None:
                try:
                    self._dsp_state.replace(params, source=self)
                    _wrote = True
                except Exception:
                    log.debug("选预设写 DspState 失败", exc_info=True)
            if not _wrote:
                # 回退：无 DspState 时直接下发（兼容/降级）
                self._push_dsp_to_engine(params, debounce=False)
            cfg = get_config()
            cfg.set("dsp_params", params)
            # 当前音效走单一状态源：会广播到所有订阅者（音效弹窗自动高亮）。
            try:
                from core.effect_state import get_effect_state
                get_effect_state().set_current(key)
            except Exception:
                cfg.set("effect_preset", key)
            # 注：不再手工同步设置页 / 高级窗口 —— DspState 广播已覆盖。
        except Exception as exc:
            log.debug("应用音效预设失败: %s", exc)

    def _current_effect_preset(self) -> str:
        """读取当前音效预设名（单一状态源）。"""
        try:
            from core.effect_state import get_effect_state
            return get_effect_state().current()
        except Exception:
            try:
                return get_config().get("effect_preset") or ""
            except Exception:
                return ""

    def _init_effect_presets(self) -> None:
        """启动时把已选预设名同步给面板（仅用于高亮）。"""
        try:
            name = self._current_effect_preset()
            if name:
                self.player_panel.set_effect_presets([], name)
        except Exception:
            pass

    @staticmethod
    def _any_camilla_feature(params: dict) -> bool:
        """判断任一 Camilla 功能是否开着（决定 Camilla 引擎是否参与）。"""
        p = params or {}
        try:
            return (
                (bool(p.get("gain_enabled", False))
                 and abs(float(p.get("pre_gain_db", 0.0) or 0.0)) > 1e-6)
                or bool(p.get("eq_enabled", False))
                or bool(p.get("peq_enabled", False))
                or bool(p.get("convolution_enabled", False))
                or bool(p.get("bass_enabled", False))
                or abs(float(p.get("treble_gain_db", 0.0) or 0.0)) > 1e-6
                or bool(p.get("loudness_enabled", False))
            )
        except Exception:
            return False

    def _init_effect_menu(self) -> None:
        """初始化：应用已保存的 DSP 参数。"""
        try:
            from config.settings import get_config
            cfg = get_config()
            saved = cfg.get("dsp_params")
            if isinstance(saved, dict):
                self.player.set_dsp(saved)
            # 启动即下发一次 YAML（无条件，含直通；camilla.py 内部按 YAML 去重）
            if isinstance(saved, dict):
                try:
                    from core import camilla
                    cfg2 = camilla.build_config(saved, 48000)
                    self.player.set_camilla_yaml(camilla.to_yaml(cfg2))
                except Exception as exc:
                    log.debug("启动下发 camilla YAML 失败: %s", exc)
            # 记住供设置页显示（卷积由 CamillaDSP 的 Conv filter 处理）
            ir_path = cfg.get("convolution_ir") or ""
            self._convolution_ir_path = ir_path
        except Exception as exc:
            log.debug("初始化 DSP 失败: %s", exc)

    def _on_panel_tab(self, key: str) -> None:
        """面板 Tab 切换：queue 时刷新队列数据（切到队列时强制重建一次）。"""
        if key == "queue":
            self._queue_ids = None
            self._refresh_queue_view(force=True)

    def _on_current_changed_queue(self, *_args) -> None:
        """切歌时刷新队列列表（高亮当前项）。"""
        try:
            self._refresh_queue_view()
        except Exception:
            pass

    def _on_playlist_changed_queue(self, *_args) -> None:
        """队列内容变化（下一首播放/添加/移除/移动）→ 实时刷新 Queue 视图，并持久化队列。

        内容变化必须重建（组成变了），故 force=True。
        """
        try:
            self._queue_ids = None
            # 队列内容变化：立即重建（不再依赖 Queue 是否可见）
            self._refresh_queue_view(force=True)
        except Exception:
            pass
        # 持久化队列（重启恢复）
        self._save_queue()

    def _refresh_queue_view(self, force: bool = False) -> None:
        """把当前播放队列喂给左侧面板。

        节流 + 去重：切歌时队列内容通常不变，只有当前项高亮变；
        若整表重建会明显卡顿，因此仅当队列组成变化或强制刷新时才重建。
        """
        try:
            import time as _time
            now = _time.monotonic()
            # 节流：150ms 内的重复请求直接丢弃（拖进度/快速连切场景）
            if not force and now - getattr(self, "_last_queue_refresh", 0.0) < 0.15:
                return
            self._last_queue_refresh = now

            tracks = self.playlist.tracks()
            idx = self.playlist.current_index()
            # 队列指纹：组成不变则只更新高亮，不重建行
            ids = tuple(getattr(t, "source_id", "") or getattr(t, "filepath", "") for t in tracks)
            if not force and ids == getattr(self, "_queue_ids", None):
                self.player_panel.set_queue_current(idx)
                return
            self._queue_ids = ids
            self.player_panel.set_queue(tracks, idx)
        except Exception:
            pass

    def _on_queue_activate(self, track) -> None:
        """点队列某项 → 跳播该曲。"""
        try:
            tracks = self.playlist.tracks()
            for i, t in enumerate(tracks):
                if t is track or getattr(t, "source_id", "") == getattr(track, "source_id", ""):
                    self.playlist.set_current_index(i)
                    break
        except Exception:
            pass

    def _on_add_tracks_to_playlist(self, tracks: list) -> None:
        """多选后「加入歌单」：弹窗选歌单（或新建），把曲目加入。"""
        if not tracks:
            return
        try:
            from core.playlist_store import get_playlist_store
            store = get_playlist_store()
            playlists = store.all_playlists()
        except Exception:
            store = None
            playlists = []

        dlg = Adw.Dialog()
        dlg.set_title(_("加入歌单（{n} 首）").format(n=len(tracks)))
        dlg.set_content_width(400)
        dlg.set_content_height(460)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)

        # 新建歌单行
        new_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("新建歌单名称"))
        entry.set_hexpand(True)
        new_btn = Gtk.Button(label=_("新建并加入"))
        new_btn.add_css_class("suggested-action")

        def _new_and_add(*_a):
            name = entry.get_text().strip() or _("新歌单")
            try:
                if store is not None:
                    pid = store.create(name)
                    if pid is not None:
                        n = store.add_tracks(pid, tracks)
                        self._toast(_("已新建「{name}」并加入 {n} 首").format(name=name, n=n))
                        dlg.close()
                        # 刷新歌单页
                        try:
                            self.playlists_page.refresh()
                        except Exception:
                            pass
            except Exception:
                pass
        new_btn.connect("clicked", _new_and_add)
        new_row.append(entry)
        new_row.append(new_btn)
        body.append(new_row)

        # 现有歌单列表
        group = Adw.PreferencesGroup()
        group.set_title(_("选择已有歌单"))
        for pl in playlists:
            row = Adw.ActionRow()
            row.set_title(pl.get("name") or _("未命名歌单"))
            row.set_subtitle(f"{pl.get('cnt', 0)} {_('首')}")
            add_btn = Gtk.Button(label=_("加入"))
            add_btn.add_css_class("flat")
            add_btn.set_valign(Gtk.Align.CENTER)

            def _add(_b, pid=pl.get("id"), nm=pl.get("name")):
                try:
                    if store is not None:
                        n = store.add_tracks(pid, tracks)
                        self._toast(_("已加入「{name}」{n} 首").format(name=nm, n=n))
                        dlg.close()
                        try:
                            self.playlists_page.refresh()
                        except Exception:
                            pass
                except Exception:
                    pass
            add_btn.connect("clicked", _add)
            row.add_suffix(add_btn)
            group.add(row)
        if not playlists:
            hint = Gtk.Label(label=_("还没有歌单，可在上方新建"))
            hint.add_css_class("dim-label")
            group.add(hint)
        body.append(group)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(body)
        toolbar.set_content(scroll)
        dlg.set_child(toolbar)
        dlg.present(self)

    def _on_playlist_play(self, playlist_id: int) -> None:
        """播放某个歌单（读取其曲目并设为队列）。"""
        try:
            from core.playlist_store import get_playlist_store
            from models import TrackItem as _TI
            rows = get_playlist_store().rows_of(playlist_id)
            tracks = []
            for r in rows:
                try:
                    tracks.append(_TI(
                        title=r.get("title") or "未知歌曲",
                        artist=r.get("artist") or "未知歌手",
                        album=r.get("album") or "",
                        duration=r.get("duration") or "0:00",
                        duration_seconds=float(r.get("duration_seconds") or 0.0),
                        filepath=r.get("filepath") or "",
                        source_type=r.get("source_type") or SOURCE_LOCAL,
                        source_id=r.get("source_id") or "",
                        cover_url=r.get("cover_url") or "",
                    ))
                except Exception:
                    continue
            if tracks:
                self.playlist.set_tracks(tracks, autoplay_index=0)
        except Exception as exc:
            log.debug("播放歌单失败: %s", exc)

    def _on_queue_action(self, action: str, track) -> None:
        """队列行右键动作：promote=提升到下一首；discard=从列表丢弃。"""
        try:
            tracks = self.playlist.tracks()
            idx = -1
            for i, t in enumerate(tracks):
                if t is track or getattr(t, "source_id", "") == getattr(track, "source_id", ""):
                    idx = i
                    break
            if idx < 0:
                return
            if action == "promote":
                self.playlist.move_after_current(idx)
                self._toast(_("已设为下一首：{title}").format(title=getattr(track, 'title', '')))
            elif action == "discard":
                self.playlist.remove_at(idx)
                self._toast(_("已从播放列表移除：{title}").format(title=getattr(track, 'title', '')))
            # 强制刷新队列视图（内容已变，需要重建）
            self._queue_ids = None
            self._refresh_queue_view(force=True)
        except Exception as exc:
            log.debug("队列右键动作失败: %s", exc)

    def _on_shuffle(self, on: bool) -> None:
        """UI 随机按钮点击 → 只改队列状态；UI 同步由 shuffle-changed 信号统一处理。"""
        self.playlist.set_shuffle(on)

    def _on_repeat(self, mode: int) -> None:
        """UI 循环按钮点击 → 只改队列状态；UI 同步由 repeat-changed 信号统一处理。"""
        self.playlist.set_repeat_mode(mode)

    def _on_playlist_shuffle_changed(self, _playlist, on: bool) -> None:
        """随机播放状态变化（来自 UI / MPRIS / 恢复）→ 同步两侧 UI 按钮。"""
        try:
            self.player_panel.set_shuffle_state(on)
        except Exception:
            log.debug("同步面板随机状态失败", exc_info=True)
        try:
            self.now_playing.set_shuffle(on)
        except Exception:
            log.debug("同步沉浸页随机状态失败", exc_info=True)

    def _on_playlist_repeat_changed(self, _playlist, mode: int) -> None:
        """循环模式变化（来自 UI / MPRIS / 恢复）→ 同步两侧 UI 按钮。"""
        try:
            self.player_panel.set_repeat_state(mode)
        except Exception:
            log.debug("同步面板循环状态失败", exc_info=True)
        try:
            self.now_playing.set_repeat_mode(mode)
        except Exception:
            log.debug("同步沉浸页循环状态失败", exc_info=True)

    # ============================================================
    # 右键菜单
    # ============================================================
    def _build_track_actions(self) -> dict:
        return {
            "play": self._on_local_track_activated,
            "play_next": self._track_ctx_play_next,
            "append": self._track_ctx_append,
            "add_to_playlist": self._track_ctx_add_to_playlist,
            "copy": self._track_ctx_copy,
            "like": self._track_ctx_like,
            "info": self._track_ctx_info,
            "copy_path": self._track_ctx_copy_path,
            "reveal": self._track_ctx_reveal,
            "remove_from_list": self._track_ctx_remove_from_list,
            "delete_file": self._track_ctx_delete_file,
        }

    def _track_ctx_play_next(self, track) -> None:
        if not isinstance(track, TrackItem):
            return
        self.playlist.insert_next(track)
        self._toast(_("已加入下一首播放：{title}").format(title=track.title))

    def _track_ctx_append(self, track) -> None:
        if not isinstance(track, TrackItem):
            return
        self.playlist.append(track)
        self._toast(_("已添加到播放列表：{title}").format(title=track.title))

    def _track_ctx_add_to_playlist(self, track) -> None:
        """右键「添加到歌单」：多选模式下加所有选中，否则加右键那一首。"""
        try:
            page = getattr(self, "local_page", None)
            tracks = []
            if page is not None and getattr(page, "_multi_mode", False):
                tracks = page.selected_tracks()
            if not tracks and isinstance(track, TrackItem):
                tracks = [track]
            if not tracks:
                return
            self._on_add_tracks_to_playlist(tracks)
        except Exception as exc:
            log.debug("添加到歌单失败: %s", exc)

    def _track_ctx_copy(self, track) -> None:
        if not isinstance(track, TrackItem):
            return
        text = f"{track.title} - {track.artist}"
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                display.get_clipboard().set(text)
                self._toast(_("已复制：{text}").format(text=text))
        except Exception:
            self._toast(text)

    def _track_ctx_like(self, track) -> None:
        if not isinstance(track, TrackItem):
            return
        liked = get_liked_store().toggle(track)
        self._toast(_("已喜欢：{title}" if liked else "已取消喜欢：{title}").format(title=track.title))
        cur = self.playlist.current_track()
        if cur is not None and getattr(cur, "source_id", "") == getattr(track, "source_id", ""):
            self.player_panel.set_liked(liked)
        if self._active_source == "liked":
            self._refresh_liked_page()

    def _resolve_track(self, track):
        """用 filepath/source_id 从 provider 查"权威"对象。

        ColumnViewCell.get_item() 可能返回包装对象（属性默认值），
        导致弹窗读不到音频参数。这里按 key 回查 provider 的当前数据。
        """
        if track is None:
            return None
        key = getattr(track, "filepath", "") or getattr(track, "source_id", "")
        if not key:
            return track
        try:
            provider = self._get_provider(SOURCE_LOCAL)
            if provider is not None:
                for t in provider.get_library():
                    k = getattr(t, "filepath", "") or getattr(t, "source_id", "")
                    if k == key:
                        return t
        except Exception:
            pass
        return track

    def _track_ctx_info(self, track) -> None:
        """查看歌曲信息：弹窗显示详情。"""
        if not isinstance(track, TrackItem):
            return
        # 用权威对象（避免拿到包装对象导致参数为默认值）
        track = self._resolve_track(track) or track
        try:
            rows = self._build_track_info_rows(track)
            self._show_info_dialog(rows)
        except Exception as exc:
            log.debug("查看歌曲信息失败: %s", exc)

    @staticmethod
    def _build_track_info_rows(track) -> list:
        """收集曲目信息行 [(标签, 值), ...]（纯逻辑，不碰 UI）。"""
        import os as _os
        # 编码格式：从文件扩展名推断
        src = getattr(track, "filepath", "") or getattr(track, "stream_url", "") or ""
        fmt = ""
        if "." in src:
            pp = src.split("?", 1)[0]
            if "." in pp:
                fmt = pp.rsplit(".", 1)[-1].upper()
        sr = int(getattr(track, "sample_rate", 0) or 0)
        sr_txt = f"{sr / 1000:.1f} kHz".replace(".0 kHz", " kHz") if sr else "未知"
        bd = int(getattr(track, "bit_depth", 0) or 0)
        bd_txt = f"{bd} bit" if bd else "未知"
        ch = int(getattr(track, "channels", 0) or 0)
        ch_txt = {1: "单声道 (1)", 2: "立体声 (2)"}.get(ch, f"{ch} 声道") if ch else "未知"
        br = int(getattr(track, "bitrate", 0) or 0)
        br_txt = f"{br // 1000} kbps" if br else "未知"
        rows = [
            ("歌名", track.title or ""),
            ("歌手", track.artist or ""),
            ("专辑", track.album or ""),
            ("时长", track.duration or ""),
            (_("编码格式"), fmt or _("未知")),
            ("采样率", sr_txt),
            ("位深", bd_txt),
            ("声道", ch_txt),
            ("码率", br_txt),
        ]
        path = getattr(track, "filepath", "") or ""
        if path:
            rows.append(("文件路径", path))
            try:
                rows.append(("文件大小", _human_size(_os.path.getsize(path))))
            except OSError:
                pass
        return rows

    def _show_info_dialog(self, rows: list) -> None:
        """用信息行构建并显示歌曲信息弹窗。"""
        dialog = Adw.Dialog()
        dialog.set_title(_("歌曲信息"))
        dialog.set_content_width(520)
        dialog.set_content_height(400)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        for label, value in rows:
            lb = Gtk.Label()
            lb.set_markup(f"<b>{_glib_escape(label)}</b>")
            lb.set_halign(Gtk.Align.START)
            box.append(lb)
            vl = Gtk.Label(label=value or "—")
            vl.set_halign(Gtk.Align.START)
            vl.set_selectable(True)
            vl.set_wrap(True)
            vl.add_css_class("dim-label")
            box.append(vl)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar.set_content(scroll)
        dialog.set_child(toolbar)
        dialog.present(self)

    def _track_ctx_copy_path(self, track) -> None:
        """复制文件路径到剪贴板。"""
        if not isinstance(track, TrackItem):
            return
        path = getattr(track, "filepath", "") or ""
        if not path:
            self._toast("该曲目没有本地文件路径")
            return
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                display.get_clipboard().set(path)
                self._toast("已复制文件路径")
        except Exception:
            self._toast(path)

    def _track_ctx_reveal(self, track) -> None:
        """在文件管理器中显示（打开所在目录并选中文件）。"""
        if not isinstance(track, TrackItem):
            return
        path = getattr(track, "filepath", "") or ""
        if not path or not os.path.isfile(path):
            self._toast("该曲目没有本地文件")
            return
        try:
            import subprocess
            # 优先用文件管理器的"选中文件"能力（不同桌面环境支持不同）
            folder = os.path.dirname(path)
            try:
                subprocess.Popen(["dbus-send", "--session", "--dest=org.freedesktop.FileManager1",
                                  "--type=method_call", "/org/freedesktop/FileManager1",
                                  "org.freedesktop.FileManager1.ShowItems",
                                  f"array:string:file://{path}", "string:"])
            except Exception:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            self._toast(f"打开目录失败: {exc}")

    def _active_page(self):
        """返回当前活动页面对象（用于列表移除等操作）。"""
        key = getattr(self, "_active_source", "")
        return {
            "local": getattr(self, "local_page", None),
            "liked": getattr(self, "liked_page", None),
            "albums": getattr(self, "albums_page", None),
            "artists": getattr(self, "artists_page", None),
        }.get(key)

    def _track_ctx_remove_from_list(self, track) -> None:
        """从当前列表移除（仅 UI 列表，不动文件、不影响曲库）。"""
        if not isinstance(track, TrackItem):
            return
        page = self._active_page()
        if page is None or not hasattr(page, "remove_track"):
            self._toast("当前列表不支持移除")
            return
        try:
            ok = page.remove_track(track)
            self._toast("已从列表移除" if ok else "移除失败")
        except Exception as exc:
            self._toast(f"移除失败: {exc}")

    def _track_ctx_delete_file(self, track) -> None:
        """删除：弹确认框，询问是否同时删除本地文件（走回收站，可恢复）。"""
        if not isinstance(track, TrackItem):
            return
        path = getattr(track, "filepath", "") or ""
        has_file = bool(path) and os.path.isfile(path)
        try:
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="删除歌曲",
                body=(f"确定要从列表移除「{track.title}」吗？\n\n"
                      + ("勾选下方将同时删除本地文件（移入回收站，可恢复）。" if has_file else "该曲目没有本地文件。")),
            )
            dialog.add_response("cancel", "取消")
            dialog.add_response("remove", "仅移除")
            if has_file:
                dialog.add_response("delete", "删除文件")
                dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def _on_resp(_d, resp):
                if resp == "cancel":
                    return
                # 先移除列表项
                page = self._active_page()
                if page is not None and hasattr(page, "remove_track"):
                    try:
                        page.remove_track(track)
                    except Exception:
                        pass
                if resp == "delete" and has_file:
                    self._delete_to_trash(path)
            dialog.connect("response", _on_resp)
            dialog.present()
        except Exception as exc:
            log.debug("删除对话框失败: %s", exc)

    def _delete_to_trash(self, path: str) -> None:
        """把文件移入回收站（可恢复）；失败则提示。"""
        try:
            import subprocess
            r = subprocess.run(["gio", "trash", "--", path], capture_output=True, text=True)
            if r.returncode == 0:
                self._toast("已移入回收站")
            else:
                self._toast(f"删除失败: {r.stderr.strip() or '未知错误'}")
        except Exception as exc:
            self._toast(f"删除失败: {exc}")

    # ============================================================
    # 全屏播放页
    # ============================================================
    def _enter_fullscreen(self) -> None:
        track = self.playlist.current_track()
        if track is not None:
            self.now_playing.set_track(track.title, track.artist)
            self.now_playing.set_position(self.player.position())
        # 先切页：让切页动画立刻开始。
        # 背景色补算（解码 + 可能触发 CSS 重解析）与可视化启动都放到切页之后，
        # 否则这些同步开销会让「点击 → 动画开始」之间出现可感知的停顿。
        self._main_stack.set_visible_child_name("nowplaying")
        if track is not None:
            # 沉浸页背景/进度条色：切歌时已随封面设过；此处仅兜底补算，
            # 颜色未变时内部会直接返回，不再解码、不再重解析 CSS。
            self._apply_cover_color_for_now_playing()
        # 进入沉浸页：启动可视化采集 + 渲染（主界面期间不跑）
        try:
            if getattr(self, "_viz_pipeline", None) is not None and \
                    bool(get_config().get_bool("viz_enabled", True)):
                self._viz_pipeline.start()
                self.now_playing.viz.start()
        except Exception:
            pass
        self._was_maximized = self.is_maximized()
        if not self._was_maximized:
            # 用 idle_add 延后最大化：先让切页动画立即开始，
            # 最大化放到主循环空闲时执行，避免窗口最大化动画阻塞切页。
            try:
                GLib.idle_add(self.maximize)
            except Exception:
                self.maximize()

    def _apply_cover_color_for_now_playing(self) -> None:
        """进入沉浸页时：用缓存的封面原图算主色，设背景与进度条色。"""
        raw_cover = getattr(self, "_pending_cover_raw", None)
        if not raw_cover:
            return
        # 同一份封面原图只算一次：切歌时已随封面设过颜色，
        # 重复进出沉浸页不该再解码一遍原始封面字节。
        if raw_cover is getattr(self, "_np_color_src", None):
            return
        self._np_color_src = raw_cover
        try:
            from models.coverart import extract_dominant_color
            raw = extract_dominant_color(raw_cover, lighten=0.0)
            if not raw:
                return
            r, g, b = raw
            # 背景在关闭模糊开关时回退主题色；进度条颜色受
            # 「进度条跟随封面取色」开关控制（关闭则回退默认深灰/白）。
            from config.settings import get_config as _get_cfg
            _cfg = _get_cfg()
            _blur_on = _cfg.get_bool("nowplaying_blur_bg", True)
            bg_rgb = lighten_for_background((r, g, b)) if _blur_on else None
            try:
                _follow = _cfg.get_bool("progress_follow_cover", True)
            except Exception:
                _follow = True
            f = 0.72
            seek = ((r / 255.0 * f, g / 255.0 * f, b / 255.0 * f)
                    if _follow else None)
            self.now_playing.set_bg_colors(bg_rgb, seek)
        except Exception:
            pass

    def _apply_main_bg(self, bg_rgb) -> None:
        """主界面「背景跟随封面」：把封面主色注入右侧内容区背景。

        开关关闭或无颜色时清空动态 CSS，回退主题色（@view_bg_color）。
        """
        provider = getattr(self, "_dynamic_css", None)
        if provider is None:
            return
        try:
            enabled = get_config().get_bool("main_bg_follow_cover", False)
        except Exception:
            enabled = False
        try:
            if enabled and bg_rgb:
                r, g, b = bg_rgb
                col = f"rgb({r}, {g}, {b})"
                # 左侧面板与底同色（不暗一档），层级靠圆角/阴影/边界区分。
                col_panel = col
                # 给窗口加类，统一覆盖主界面所有背景层。
                # 必须显式覆盖 listview/columnview 等：libadwaita 默认给
                # `listview, list` 设了不透明的 --view-bg-color，会盖住底层色。
                self.add_css_class("main-bg-follow")
                # 精确限定在 .content-area 内。
                # 关键：不覆盖 --view-bg-color/--window-bg-color 全局变量——
                # 它们会被所有后代（含沉浸页、菜单、按钮）继承，导致
                # 沉浸页歌词/右键菜单/设置按钮出现异常白边或染色。
                # 沉浸页 .now-playing-root 是 _main_stack 的兄弟，不在
                # .content-area 内，因此下列选择器天然不会命中它。
                # 自定义属性：供 style.css 里的菜单等使用。
                # 与 view-bg-color 不同，这个属性没有任何内置控件引用，
                # 因此不会污染沉浸页/按钮等（仅显式引用它的规则受影响）。
                css = (
                    # 面板外的底（窗口根 + 侧栏容器）：原封面色。
                    "window.main-bg-follow,"
                    "window.main-bg-follow overlay-split-view .sidebar-pane {"
                    f"background-color: {col};"
                    "}"
                    # 左侧面板：略暗色，与面板外的底拉开对比。
                    # 同时清除 style.css 里 .player-panel 的 background-image 叠加层
                    # （那是系统默认色模式下的暗一档实现）；否则跟随封面时
                    # 会「col_panel × 再叠一层」双重变暗，与系统模式层级不一致。
                    "window.main-bg-follow .player-panel {"
                    f"background-color: {col_panel};"
                    "background-image: none;"
                    "}"
                    # 右侧内容区：原封面色
                    "window.main-bg-follow .content-area,"
                    "window.main-bg-follow .content-area scrolledwindow,"
                    "window.main-bg-follow .content-area scrolledwindow > viewport,"
                    "window.main-bg-follow .content-area columnview,"
                    "window.main-bg-follow .content-area listview,"
                    "window.main-bg-follow .content-area gridview,"
                    "window.main-bg-follow .content-area flowbox {"
                    f"background-color: {col};"
                    "}"
                    # headerbar 透明（去默认底边阴影/边框）
                    "window.main-bg-follow headerbar,"
                    "window.main-bg-follow headerbar:backdrop {"
                    "background-color: transparent; box-shadow: none; border: none;"
                    "}"
                    # 表头及其内部：彻底透明，覆盖 hover/active/checked 等所有状态，
                    # 消除在彩色背景上出现的白色高亮块。
                    "window.main-bg-follow .content-area columnview > header,"
                    "window.main-bg-follow .content-area columnview > header > button,"
                    "window.main-bg-follow .content-area columnview > header > button > box,"
                    "window.main-bg-follow .content-area columnview > header > button:hover,"
                    "window.main-bg-follow .content-area columnview > header > button:active,"
                    "window.main-bg-follow .content-area columnview > header > button:checked {"
                    "background: none; background-image: none;"
                    "box-shadow: none; border: none;"
                    "}"
                    # headerbar 扁平按钮（设置/侧栏/导航）hover/active 去白环
                    "window.main-bg-follow headerbar button.flat:hover,"
                    "window.main-bg-follow headerbar button.flat:active,"
                    "window.main-bg-follow headerbar button.flat:checked {"
                    "background-color: alpha(currentColor, 0.08); box-shadow: none;"
                    "}"
                    # 右键/媒体菜单：popover 是独立 CSS 根，不能用 window 祖先限定。
                    # .media-menu 仅本项目菜单使用，故全局选择器安全；
                    # 且仅在功能开启时注入，关闭即清空。
                    "popover.media-menu > contents,"
                    "popover.media-menu > arrow {"
                    f"background-color: {col}; border: none;"
                    "box-shadow: none;"
                    "}"
                    "popover.media-menu > contents {"
                    "box-shadow: 0 2px 10px 2px alpha(black, 0.18);"
                    "padding: 4px;"
                    "}"
                    # 菜单项更紧凑：减小最小宽度与左右内边距（主题默认 88px/6px 偏宽）
                    "popover.media-menu modelbutton {"
                    "min-width: 0; padding: 4px 8px;"
                    "}"
                    # 音效气泡：与菜单同色，跟随主界面背景色自动变化。
                    # 用 USER+1000 的 _dynamic_css 已能压过第三方主题。
                    # 注意：箭头（> arrow）与内容同属主题的 `popover > arrow` 规则，
                    # 必须一并覆盖，否则箭头仍是主题白底、与气泡背景不协调。
                    "popover.effect-popover > contents,"
                    "popover.effect-popover > arrow {"
                    f"background-color: {col}; border: none;"
                    "box-shadow: none;"
                    "}"
                    "popover.effect-popover > contents {"
                    "box-shadow: 0 2px 10px 2px alpha(black, 0.18);"
                    "}"
                    # 左侧面板歌词区顶/底渐隐：用面板底色 col_panel（×0.90，暗一档），
                    # 与加了阴影的浮起面板一致；用 col 会在面板上形成偏亮渐变块。
                    "window.main-bg-follow .np-lyrics-compact .np-fade-top {"
                    f"background-image: linear-gradient(to bottom, {col_panel}, alpha({col_panel}, 0));"
                    "background-color: transparent;"
                    "}"
                    "window.main-bg-follow .np-lyrics-compact .np-fade-bottom {"
                    f"background-image: linear-gradient(to top, {col_panel}, alpha({col_panel}, 0));"
                    "background-color: transparent;"
                    "}"
                )
            else:
                self.remove_css_class("main-bg-follow")
                css = ""
            provider.load_from_data(css.encode("utf-8"))
        except Exception:
            log.debug("应用主界面背景色失败", exc_info=True)

    def reapply_main_bg(self) -> None:
        """设置页切换「主界面背景跟随封面」后即时应用当前封面主色。"""
        self._apply_main_bg(getattr(self, "_current_bg_rgb", None))

    def reapply_progress_color(self) -> None:
        """设置页切换「进度条跟随封面取色」后即时重设进度条颜色。

        用缓存的封面主色重算（纯计算，不重新解码封面）：
        开关开启 → 主色 * 0.72；关闭 → None（进度条回退默认深灰/白）。
        同时应用到左侧面板进度条与沉浸页进度条。
        """
        try:
            enabled = get_config().get_bool("progress_follow_cover", True)
        except Exception:
            enabled = True
        rgb = None
        if enabled:
            dom = getattr(self, "_current_dominant_rgb", None)
            if dom:
                r, g, b = dom
                f = 0.72
                rgb = (r / 255.0 * f, g / 255.0 * f, b / 255.0 * f)
        try:
            self.player_panel.set_progress_color(rgb)
        except Exception:
            pass
        try:
            np = getattr(self, "now_playing", None)
            if np is not None:
                np._apply_seekbar_color(dom if enabled else None)
        except Exception:
            pass

    def reapply_nowplaying_bg(self) -> None:
        """设置页切换「背景模糊」后即时重应用当前曲目的背景。

        重新跑一遍后台资源加载（会重新按开关生成/清除模糊背景）。
        """
        try:
            token = getattr(self, "_track_token", None)
            track = getattr(self, "_current_track", None)
            if token is None or track is None:
                return
            filepath = getattr(track, "filepath", "") or ""
            is_local = getattr(track, "source", SOURCE_LOCAL) == SOURCE_LOCAL
            import threading
            threading.Thread(
                target=self._load_track_assets_bg,
                args=(token, filepath, is_local, track, True),
                daemon=True,
            ).start()
        except Exception:
            pass

    def _exit_fullscreen(self) -> None:
        self._main_stack.set_visible_child_name("main")
        # 退出沉浸页：停止可视化采集 + 渲染，释放 CPU
        try:
            self.now_playing.viz.stop()
            if getattr(self, "_viz_pipeline", None) is not None:
                self._viz_pipeline.stop()
        except Exception:
            pass
        if not getattr(self, "_was_maximized", False):
            self.unmaximize()

    # ============================================================
    def apply_dsd_mode(self, mode: str) -> None:
        """下发 DSD 输出模式到后端。"""
        try:
            self.player.set_dsd_mode(mode)
        except Exception:
            pass

    def apply_output_device(self, name: str) -> None:
        """下发输出设备到后端。"""
        try:
            self.player.set_output_device(name)
        except Exception:
            pass

    def _apply_audio_output_settings(self) -> None:
        """启动时把持久化的输出设备 / DSD 模式下发到后端。

        后端启动时输出设备为空（PipeWire）、DSD 模式为 auto，
        若用户上次选了外置 DAC，需在启动时同步，否则 UI 显示与
        实际输出不一致（显示选了设备，实际走默认）。
        """
        try:
            cfg = get_config()
            enabled = cfg.get_bool("dsd_output_enabled", False)
            if enabled:
                # 开关开：下发用户选择的 DSD 模式与输出设备。
                mode = cfg.get_str("dsd_output_mode", "auto")
                dev = cfg.get_str("output_device", "")
            else:
                # 开关关（默认）：走老逻辑——DSD 经 ffmpeg 软解 + PipeWire 默认输出。
                mode = "pcm"
                dev = ""
            # 先设 DSD 模式，再设输出设备（避免切设备后续播时用错模式）。
            self.apply_dsd_mode(mode)
            self.apply_output_device(dev)
            log.info("启动应用音频输出设置: enabled=%s device=%r dsd_mode=%r",
                     enabled, dev, mode)
        except Exception as exc:
            log.info("应用启动音频输出设置失败: %s", exc)

    # PlayerCore 信号
    # ============================================================
    def _connect_player_signals(self) -> None:
        self.player.connect("position-update", self._on_position_update)
        self.player.connect("duration-changed", self._on_duration_changed)
        self.player.connect("end-of-stream", self._on_end_of_stream)
        self.player.connect("play-state-changed", self._on_play_state_changed)
        self.player.connect("error-occur", self._on_player_error)
        self.player.connect("audio-info", self._on_audio_info)
        self.player.connect("volume-changed", self._on_volume_changed)
        # 后端断开（可选信号，用 try 兼容）。
        try:
            self.player.connect("backend-lost", self._on_backend_lost)
        except Exception:
            pass

    def _on_position_update(self, _player, seconds: float) -> None:
        # 位置信号可能很密集；节流到约 30fps，避免 SeekBar 反复重绘、
        # 歌词高亮/缓动滚动被高频触发而占满主线程。
        import time as _time
        now = _time.monotonic()
        last = getattr(self, "_last_pos_update", 0.0)
        if now - last < 0.033:
            return
        self._last_pos_update = now
        self.player_panel.set_position(seconds)
        self.now_playing.set_position(seconds)

    def _on_duration_changed(self, _player, seconds: float) -> None:
        self.player_panel.set_duration(seconds)
        self.now_playing.set_duration(seconds)

    def _on_end_of_stream(self, _player) -> None:
        nxt = self.playlist.next(auto=True)
        if nxt is None:
            self.player.stop()

    def _on_play_state_changed(self, _player, state: str) -> None:
        # 投送模式下，本机后端不是播放源（已 stop），其后端的 state 事件
        # （如 stop 触发的 stopped）不应覆盖投送按钮状态——否则设备在放、
        # 按钮却显示未播放。投送状态由 _cast_track/_on_play_pause 自行维护。
        if self._is_casting():
            return
        playing = state == "playing"
        self.player_panel.set_playing(playing)
        self.now_playing.set_playing(playing)
        try:
            if getattr(self, "_tray", None) is not None:
                self._tray.set_playing(playing)
        except Exception:
            pass

    def _on_player_error(self, _player, message: str) -> None:
        self._toast(message)

    def _on_backend_lost(self, _player, message: str) -> None:
        """后端连接丢失：提示用户（RustBackend 会在后台尝试自愈重启）。"""
        log.warning("后端连接丢失: %s", message)
        try:
            self._toast(message or "音频后端已断开，正在尝试恢复")
        except Exception:
            pass

    def _on_volume_changed(self, _player, value: float) -> None:
        """音量变化（来自 UI / MPRIS / 恢复）→ 同步两侧滑块。

        用不触发回调的 set_volume，避免与用户拖动滑块形成回环。
        """
        try:
            v = max(0.0, min(1.0, float(value)))
        except Exception:
            return
        try:
            self.player_panel.set_volume(v)
        except Exception:
            log.debug("同步面板音量失败", exc_info=True)
        try:
            self.now_playing.set_volume(v)
        except Exception:
            log.debug("同步沉浸页音量失败", exc_info=True)

    def _on_audio_info(self, _player, info: dict) -> None:
        track = self.playlist.current_track()
        if track is None:
            return
        try:
            track.apply_audio_info(info)
            self.player_panel.set_format_info(track.format_label)
            self.now_playing.set_format_info(track.format_label)
            # 同步到各列表页里的同名 track：列表 model 里的对象是独立副本，
            # 不更新的话右键"查看歌曲信息"读到的是无参数的旧对象。
            self._sync_audio_info_to_pages(track, info)
        except Exception:
            pass

    def _sync_audio_info_to_pages(self, track, info: dict) -> None:
        """把探测到的音频参数同步到所有列表页的同名 TrackItem。"""
        if not info:
            return
        key = getattr(track, "source_id", "") or getattr(track, "filepath", "")
        if not key:
            return
        for pg in (getattr(self, "local_page", None),
                   getattr(self, "liked_page", None),
                   getattr(self, "albums_page", None),
                   getattr(self, "artists_page", None)):
            if pg is None:
                continue
            try:
                for t in getattr(pg, "_all_tracks", []) or []:
                    k = getattr(t, "source_id", "") or getattr(t, "filepath", "")
                    if k == key:
                        t.apply_audio_info(info)
            except Exception:
                pass

    # ============================================================
    # 设置 / 关闭
    # ============================================================
    def _on_open_settings(self, _btn=None, goto_effect: bool = False,
                          goto_shortcuts: bool = False) -> None:
        win = SettingsWindow(
            self,
            on_dirs_changed=self._on_dirs_changed,
            on_dsp_changed=self._on_dsp_changed,
            on_convolution_ir=self._on_convolution_ir,
            on_convolution_cleared=self._on_convolution_cleared,
            on_viz_changed=self._on_viz_changed,
            on_open_viz_window=self._on_open_viz_window,
            on_coloring=self._on_coloring_changed,
        )
        win.present()
        if goto_effect:
            win.show_effect_page()
        if goto_shortcuts:
            win.show_shortcuts_page()
        self._settings_win = win

    def _on_open_effect_settings(self) -> None:
        """音效菜单"详细配置"：打开设置并跳到音效页。"""
        self._on_open_settings(goto_effect=True)

    def _open_effect_dialog(self, anchor=None) -> None:
        """打开音效列表气泡（与主页音效按钮同一入口）。

        沉浸页音效按钮调用此方法：复用 player_panel 的内容构建，气泡
        从传入的 anchor 按钮旁弹出（不传则用主页音效按钮）。
        """
        try:
            fn = getattr(self.player_panel, "_open_effect_dialog", None)
            if callable(fn):
                # 沉浸页打开时，用沉浸页的模糊封面图作气泡背景
                # （与沉浸页视觉一致；无模糊图时回退主界面背景色）。
                path = getattr(self, "_immersive_bg_path", None)
                dark = getattr(self, "_immersive_bg_dark", None)
                if path and anchor is not None:
                    fn(anchor=anchor, bg_image_path=path, bg_dark=dark)
                else:
                    fn(anchor=anchor)
        except Exception:
            log.debug("打开音效弹窗失败", exc_info=True)

    def _on_coloring_changed(self, tube_drive: float, bbe_amount: float) -> None:
        """音色染色参数：下发后端 + 持久化。"""
        log.debug("音色染色变化: td=%s ba=%s", tube_drive, bbe_amount)
        try:
            fn = getattr(self.player, "set_coloring", None)
            if callable(fn):
                fn(tube_drive, bbe_amount)
            from config.settings import get_config
            get_config().set("coloring", {"tube_drive": tube_drive, "bbe_amount": bbe_amount})
        except Exception as exc:
            log.debug("下发染色参数失败: %s", exc)

    def _on_open_advanced_dsp(self) -> None:
        """打开独立高级 DSP 窗口。"""
        from .advanced_dsp_window import AdvancedDspWindow
        win = AdvancedDspWindow(
            parent=self,
            params=self.dsp_page.params(),
            on_dsp_changed=self._on_dsp_changed,
            on_coloring=self._on_coloring_changed,
            on_viz_changed=self._on_viz_changed,
            on_open_viz_window=self._on_open_viz_window,
            on_convolution_ir=self._on_convolution_ir,
            on_convolution_cleared=self._on_convolution_cleared,
        )
        win.present()
        self._advanced_dsp_win = win

    def _on_dsp_state_changed(self, state, params: dict) -> None:
        """DspState 变更 → 统一出口下发。

        这是重构后的唯一「下发触发点」。各视图（EffectPage / 高级窗口 /
        ReplayGain）只调 DspState.patch()，由这里统一 set_dsp + Camilla YAML。

        防抖策略：
        - 总开关 enabled 变化 → 立即下发（关键切换，不能延迟）。
        - 其它（拖滑块）→ 80ms 防抖合并，避免频繁重建管线。
        """
        if not isinstance(params, dict):
            return
        enabled = bool(params.get("enabled", False))
        prev = getattr(self, "_last_state_enabled", None)
        enabled_changed = (prev is not None and prev != enabled)
        self._last_state_enabled = enabled
        immediate = bool(enabled_changed)
        # 记录待下发参数（后者覆盖前者，只发最新）
        self._state_push_pending = dict(params)
        if immediate:
            self._flush_state_push(immediate=True)
            return
        # 防抖
        try:
            if getattr(self, "_state_push_timer", None) is not None:
                GLib.source_remove(self._state_push_timer)
            self._state_push_timer = GLib.timeout_add(80, self._flush_state_push)
        except Exception:
            self._flush_state_push()

    def _flush_state_push(self, *, immediate: bool = False) -> bool:
        """把挂起的 DspState 参数下发到后端。"""
        self._state_push_timer = None
        params = getattr(self, "_state_push_pending", None)
        if not isinstance(params, dict):
            return False
        self._state_push_pending = None
        try:
            self._push_dsp_to_engine(params, debounce=False)
        except Exception:
            log.debug("统一出口下发失败", exc_info=True)
        return False

    def _push_dsp_to_engine(self, params: dict, *, debounce: bool = False) -> None:
        """把 DSP 参数下发到后端：Rust 内置链（set_dsp）+ 内嵌 Camilla YAML。

        这是「下发」的统一步骤，供 _on_effect / _on_dsp_changed 共用。
        - debounce=False：立即生成并下发 Camilla YAML（选预设等一次性操作）。
        - debounce=True：防抖 80ms 合并（拖滑块），避免频繁重建管线卡 UI。
        """
        try:
            self.player.set_dsp(params)
        except Exception as exc:
            log.debug("set_dsp 下发失败: %s", exc)
        if not debounce:
            try:
                from core import camilla
                cfg = camilla.build_config(params, 48000)
                self.player.set_camilla_yaml(camilla.to_yaml(cfg))
            except Exception as exc:
                log.debug("下发 camilla YAML 失败: %s", exc)
            return
        # 防抖：拖滑块时合并，避免每个中间值都生成 YAML。
        self._pending_dsp_params = dict(params)
        if getattr(self, "_dsp_yaml_timer", None) is not None:
            try:
                GLib.source_remove(self._dsp_yaml_timer)
            except Exception:
                pass
        self._dsp_yaml_timer = GLib.timeout_add(80, self._emit_camilla_yaml_now)

    def _on_dsp_changed(self, params: dict, *, clear_mark: bool = True,
                        immediate: bool = False) -> None:
        """下发 DSP 参数并持久化。

        - Rust 内置 DSP：通过 set_dsp 下发（camilla 模式下 Rust 会跳过，但无害）
        - CamillaDSP 模式：同时更新 camilladsp 配置并触发重载

        clear_mark=True（默认，手动改参数）：因为已不是任何预设，清空「当前
        音效」标记。clear_mark=False（加载预设等）：保留标记，由调用方随后
        显式 set_current(预设名)。这样「下发」与「音效标记」解耦，避免下发
        顺带清空标记导致的高亮丢失。

        immediate=True（开关/按钮等一次性操作）：立即下发 Camilla YAML，
        不走防抖。**总开关 enabled 变化时必须立即下发**——否则防抖延迟/覆盖
        会导致重开时 Camilla 管线没更新，卷积等 Camilla 功能不恢复。
        """
        # 总开关变化 → 强制立即下发（关键状态切换，不能防抖）。
        enabled = bool(params.get("enabled", False))
        prev_enabled = getattr(self, "_last_dsp_enabled", None)
        enabled_changed = (prev_enabled is not None and prev_enabled != enabled)
        self._last_dsp_enabled = enabled
        # 立即下发条件：上游要求立即（开关/按钮）或总开关变化。
        do_immediate = bool(immediate) or enabled_changed
        # 重构后：不再在此直接下发；统一写 DspState（唯一真相源），
        # 由订阅的 _on_dsp_state_changed 统一出口下发（含防抖）。
        wrote_state = False
        if getattr(self, "_dsp_state", None) is not None:
            try:
                self._dsp_state.replace(params, source=self)
                wrote_state = True
            except Exception:
                log.debug("写入 DspState 失败", exc_info=True)
        if not wrote_state:
            # 回退：无 DspState 时直接下发（兼容/降级）
            self._push_dsp_to_engine(params, debounce=not do_immediate)
        try:
            from config.settings import get_config
            _cfg = get_config()
            _cfg.set("dsp_params", params)
            _cfg.set_bool("dsp_enabled", bool(params.get("enabled", False)))
            if clear_mark:
                # 手动改参数：清空「当前音效」标记（走单一状态源，广播给所有视图）：
                # - DSP 关闭 → 高亮「关闭」
                # - DSP 开着但手动调了参数 → 已不是任何预设，清空标记
                try:
                    from core.effect_state import get_effect_state
                    get_effect_state().set_current(
                        "关闭" if not params.get("enabled", False) else "")
                except Exception:
                    if not params.get("enabled", False):
                        _cfg.set("effect_preset", "关闭")
                    else:
                        _cfg.set("effect_preset", "")
            # 注：不再手工同步 dsp_page —— DspState 广播已让所有视图自动刷新。
        except Exception:
            pass

    def _emit_camilla_yaml_now(self) -> bool:
        """防抖回调：生成并下发 camilla YAML。"""
        self._dsp_yaml_timer = None
        params = getattr(self, "_pending_dsp_params", None)
        if not isinstance(params, dict):
            return False
        try:
            from core import camilla
            cfg = camilla.build_config(params, 48000)
            yaml_str = camilla.to_yaml(cfg)
            self.player.set_camilla_yaml(yaml_str)
        except Exception as exc:
            log.debug("生成/下发 camilla YAML 失败: %s", exc)
        return False

    def _on_convolution_ir(self, path: str) -> None:
        """记录卷积 IR 路径（由 CamillaDSP 的 Conv filter 加载）。"""
        try:
            from config.settings import get_config
            get_config().set("convolution_ir", path)
            self._convolution_ir_path = path
            self._toast(f"已加载 IR：{os.path.basename(path)}")
        except Exception as exc:
            log.debug("记录卷积 IR 失败: %s", exc)

    def _on_convolution_cleared(self) -> None:
        """清除卷积 IR 记录。"""
        try:
            from config.settings import get_config
            get_config().set("convolution_ir", "")
            self._convolution_ir_path = ""
            self._toast("已清除 IR")
        except Exception as exc:
            log.debug("清除卷积 IR 记录失败: %s", exc)

    def _on_dirs_changed(self) -> None:
        provider = self._get_provider(SOURCE_LOCAL)
        if provider is not None:
            provider.refresh()

    def _init_visualizer(self) -> None:
        """创建可视化管线（不立即启动）。

        优化：可视化采集（Rust FIFO + FFT）与渲染开销只在沉浸页需要。
        主界面不启动，避免后台空转吃 CPU（尤其在其它程序占用资源时）。
        """
        try:
            from core.viz import VizPipeline
            self._viz_pipeline = VizPipeline()
            # 沉浸式页频谱：注册数据源（由 renderer 的 tick callback 每帧拉取）
            self.now_playing.viz.set_data_source(self._viz_pipeline.latest)
            from config.settings import get_config
            cfg = get_config()
            self.now_playing.viz.set_fall_speed(float(cfg.get("viz_fall_speed", 0.35)))
            self.now_playing.viz.set_rise_speed(float(cfg.get("viz_rise_speed", 0.6)))
            self.now_playing.viz.set_db_floor(float(cfg.get("viz_db_floor", -60.0)))
            self.now_playing.viz.set_fps(int(cfg.get_int("viz_fps", 60)))
            # 按总开关设初始显隐
            self.now_playing.viz.set_visible(bool(cfg.get_bool("viz_enabled", True)))
            # 启动时确保渲染器 tick 停止（主界面不跑）
            try:
                self.now_playing.viz.stop()
            except Exception:
                pass
            self._viz_window = None
        except Exception as exc:
            log.debug("可视化管线初始化失败: %s", exc)
            self._viz_window = None

    def _on_viz_changed(self) -> None:
        """可视化设置变更：更新 pipeline 参数与面板样式（渲染由帧时钟驱动，无需重建定时器）。"""
        import os as _os
        _dbg = bool(_os.environ.get("XIATIAO_VIZ_DEBUG"))
        if _dbg:
            print("[viz-changed] 进入 _on_viz_changed", flush=True)
        pipe = self._viz_pipeline
        if pipe is None:
            if _dbg:
                print("[viz-changed] pipe is None，直接返回", flush=True)
            return
        try:
            pipe.set_params()
            if _dbg:
                print("[viz-changed] pipe.set_params() 完成", flush=True)
            # 面板样式与下落速度同步
            from config.settings import get_config
            cfg = get_config()
            style = cfg.get_str("viz_style", "bars")
            self.now_playing.viz.set_style(style)
            self.now_playing.viz.set_fall_speed(float(cfg.get("viz_fall_speed", 0.35)))
            self.now_playing.viz.set_rise_speed(float(cfg.get("viz_rise_speed", 0.6)))
            self.now_playing.viz.set_db_floor(float(cfg.get("viz_db_floor", -60.0)))
            self.now_playing.viz.set_fps(int(cfg.get_int("viz_fps", 60)))
            # 大窗口渲染器同步全部可视化参数
            _vw = getattr(self, "_viz_window", None)
            if _dbg:
                print(f"[viz-changed] 大窗口 _viz_window={_vw}", flush=True)
            if _vw is not None and getattr(_vw, "renderer", None) is not None:
                # 注意：不同步 style——大窗口有自己的样式下拉框，
                # 若这里用 config 的 style 覆盖，调滑块会把大窗口样式改掉。
                _vw.renderer.set_fall_speed(float(cfg.get("viz_fall_speed", 0.35)))
                _vw.renderer.set_rise_speed(float(cfg.get("viz_rise_speed", 0.6)))
                _vw.renderer.set_db_floor(float(cfg.get("viz_db_floor", -60.0)))
                _vw.renderer.set_fps(int(cfg.get_int("viz_fps", 60)))
                if _dbg:
                    print("[viz-changed] 大窗口参数已同步", flush=True)
            # 总开关：显隐沉浸页频谱
            enabled = bool(cfg.get_bool("viz_enabled", True))
            self.now_playing.viz.set_visible(enabled)
        except Exception as exc:
            log.debug("可视化参数更新失败: %s", exc)

    def _on_open_viz_window(self) -> None:
        """打开/聚焦可视化独立窗口。"""
        try:
            from config.settings import get_config
            from .viz_window import VizWindow
            style = get_config().get_str("viz_style", "bars")
            win = getattr(self, "_viz_window", None)
            # 频谱窗口以「设置窗口」为 transient 父窗口：作为模态设置窗口的
            # 子窗口，处于其模态链内 → 可交互 / 可关闭；同时设置窗口保持模态，
            # 主窗口仍被模态遮罩锁定（不再取消模态）。
            settings_win = getattr(self, "_settings_win", None)
            parent = settings_win if settings_win is not None else self
            if win is None:
                win = VizWindow(parent=parent, style=style)
                # 大窗口渲染器同样由 GTK 帧时钟驱动，注册同一数据源
                if self._viz_pipeline is not None:
                    win.renderer.set_data_source(self._viz_pipeline.latest)
                _cfg = get_config()
                win.renderer.set_fall_speed(float(_cfg.get("viz_fall_speed", 0.35)))
                win.renderer.set_rise_speed(float(_cfg.get("viz_rise_speed", 0.6)))
                win.renderer.set_db_floor(float(_cfg.get("viz_db_floor", -60.0)))
                win.renderer.set_fps(int(_cfg.get_int("viz_fps", 60)))

                def _on_viz_closed(*_a):
                    # 关闭：停 tick + 不在沉浸页则停管线
                    try:
                        win.renderer.stop()
                    except Exception:
                        pass
                    try:
                        if self._main_stack.get_visible_child_name() != "nowplaying":
                            if getattr(self, "_viz_pipeline", None) is not None:
                                self._viz_pipeline.stop()
                    except Exception:
                        pass
                    self._viz_window = None
                    return False
                win.connect("close-request", _on_viz_closed)
                self._viz_window = win
            win.present()
            # 启动帧回调：不 start 则永远不拉数据（频谱不显示、样式切换无效果）
            try:
                win.renderer.start()
            except Exception:
                log.debug("启动频谱渲染 tick 失败", exc_info=True)
            # 启动可视化采集管线：主界面默认不跑（省电），但独立频谱窗口需要数据
            try:
                if getattr(self, "_viz_pipeline", None) is not None and \
                        bool(get_config().get_bool("viz_enabled", True)):
                    self._viz_pipeline.start()
            except Exception:
                log.debug("启动可视化管线失败", exc_info=True)
        except Exception as exc:
            log.warning("打开可视化窗口失败: %s", exc, exc_info=True)

    #: 快捷键动作名（供设置页展示；解析/匹配见 services/shortcuts.py）
    _SHORTCUT_ACTIONS = SHORTCUT_ACTIONS

    @staticmethod
    def _parse_shortcut(spec: str):
        """兼容入口：委托 services/shortcuts.parse_shortcut。"""
        return parse_shortcut(spec)

    def _get_shortcuts(self) -> dict:
        """兼容入口：委托 services/shortcuts.get_shortcuts。"""
        return get_shortcuts()

    def _on_window_key(self, _ctrl, keyval, _code, state) -> bool:
        """全局快捷键（主界面，非输入框焦点时）。键位来自配置，可自定义。"""
        try:
            focus = self.get_focus()
            if isinstance(focus, Gtk.Editable):
                return False
            mods = mods_from_state(state)
            action = match_shortcut(get_shortcuts(), int(keyval), mods)
            if action is not None:
                self._run_shortcut(action)
                return True
        except Exception:
            pass
        return False

    def _run_shortcut(self, action: str) -> None:
        """执行快捷键对应动作。"""
        try:
            if action == "play_pause":
                self._on_play_pause()
            elif action == "prev":
                self._on_prev()
            elif action == "next":
                self._on_next()
            elif action == "seek_back":
                self._seek_relative(-5.0)
            elif action == "seek_fwd":
                self._seek_relative(5.0)
            elif action == "vol_up":
                self._volume_relative(0.05)
            elif action == "vol_down":
                self._volume_relative(-0.05)
            elif action == "open_settings":
                self._on_open_settings()
        except Exception:
            pass

    def _seek_relative(self, delta: float) -> None:
        """相对当前位置 seek（秒）。"""
        try:
            cur = float(self.player.position() or 0.0)
            dur = 0.0
            try:
                tr = self.playlist.current_track()
                dur = float(getattr(tr, "duration_seconds", 0.0) or 0.0)
            except Exception:
                dur = 0.0
            target = cur + delta
            if target < 0:
                target = 0.0
            if dur > 0 and target > dur:
                target = dur
            self.player.seek_seconds(target)
            try:
                self.player_panel.set_position(target)
                self.now_playing.set_position(target)
            except Exception:
                pass
        except Exception:
            pass

    def _volume_relative(self, delta: float) -> None:
        """相对调节音量（0..1）。"""
        try:
            vs = getattr(self.player_panel, "volume", None)
            cur = float(vs.get_value()) if vs is not None else 1.0
            val = max(0.0, min(1.0, cur + delta))
            self.player.set_volume(val)
            if vs is not None:
                vs.set_value(val)
        except Exception:
            pass

    def _cancel_all_timers(self) -> None:
        """统一移除本窗口注册的所有 GLib 定时器。

        为什么集中管理：定时器散落各处（DSP 防抖、seek 恢复等），
        窗口销毁后若仍有定时器待触发，会在控件已析构后回调，属隐患。
        这里统一 source_remove，幂等、可重复调用。
        闪屏定时器由 SplashController 自行管理，这里调用其 cancel()。
        """
        # 闪屏定时器
        try:
            ctrl = getattr(self, "_splash_ctrl", None)
            if ctrl is not None:
                ctrl.cancel()
        except Exception:
            pass
        # 窗口自身定时器
        for attr in ("_dsp_yaml_timer",):
            tid = getattr(self, attr, 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                try:
                    setattr(self, attr, 0)
                except Exception:
                    pass

    def _on_close_request(self, *_args) -> bool:
        """窗口关闭请求。

        - 开启「关闭时最小化到后台」(close_to_tray)：隐藏窗口到托盘，
          不退出（返回 True 阻止默认关闭）。
        - 否则：取消定时器 → 保存状态 → 停止服务 → 退出。
        """
        # 主动退出（Ctrl+Q / 菜单 / 托盘退出）：设了标志则直接走退出流程，
        # 不被「关闭时最小化到后台」拦截。
        if getattr(self, "_force_quit", False):
            pass
        else:
            # 最小化到后台（托盘）模式。
            try:
                if get_config().get_bool("close_to_tray", False):
                    self.set_visible(False)
                    log.info("已最小化到后台（关闭窗口不退出）")
                    return True  # 阻止默认关闭 → 不退出
            except Exception:
                log.debug("close_to_tray 判断失败", exc_info=True)
        # 正常退出流程。
        try:
            self._cancel_all_timers()
        except Exception:
            log.debug("取消定时器失败", exc_info=True)
        self._persist_on_close()
        self._shutdown_services()
        return False

    def _persist_on_close(self) -> None:
        """关闭前持久化：队列会话 + 缓存 + 音量/窗口尺寸偏好。"""
        try:
            self._save_queue()
        except Exception:
            pass
        try:
            self._save_cache()
            cfg = get_config()
            if cfg.get_bool("remember_volume", True):
                cfg.set("volume", self.player._volume if hasattr(self.player, "_volume") else 1.0)
            if cfg.get_bool("remember_window_size", True):
                cfg.set_bool("window_maximized", self.is_maximized())
                if not self.is_maximized():
                    cfg.set_int("window_width", self.get_width())
                    cfg.set_int("window_height", self.get_height())
        except Exception:
            pass

    def _shutdown_services(self) -> None:
        """停止可视化 / MPRIS / 托盘 / 播放内核。"""
        if self._viz_pipeline is not None:
            try:
                self._viz_pipeline.stop()
            except Exception:
                pass
            self._viz_pipeline = None
        try:
            mpris = getattr(self, "_mpris", None)
            if mpris is not None:
                mpris.stop()
        except Exception:
            log.debug("停止 MPRIS 失败", exc_info=True)
        try:
            tray = getattr(self, "_tray", None)
            if tray is not None:
                tray.stop()
        except Exception:
            log.debug("停止托盘失败", exc_info=True)
        self.player.shutdown()

    def _show_shortcuts(self) -> None:
        """显示键盘快捷键窗口（列出当前应用内绑定的快捷键）。"""
        try:
            from core.i18n import _ as _t
            sc = Gtk.ShortcutsWindow()
            sc.set_transient_for(self)
            sc.set_modal(True)
            section = Gtk.ShortcutsSection()
            section.set_title(_("播放控制"))
            group = Gtk.ShortcutsGroup()
            group.set_title(_("应用"))
            items = (
                ("播放 / 暂停", "space"),
                ("上一首", "Left"),
                ("下一首", "Right"),
                ("快退 5 秒", "less"),
                ("快进 5 秒", "greater"),
                ("音量 +​", "Up"),
                ("音量 -​", "Down"),
                ("设置", "<Ctrl>comma"),
                ("退出", "<Ctrl>q"),
            )
            for title, accel in items:
                item = Gtk.ShortcutsShortcut()
                item.set_title(_(title))
                item.set_accelerator(accel)
                group.add_shortcut(item)
            section.add_group(group)
            sc.add_section(section)
            sc.present()
            self._shortcuts_win = sc
        except Exception:
            log.debug("显示快捷键窗口失败", exc_info=True)

    def _tray_show_window(self) -> None:
        """托盘「显示主窗口」：把窗口置前并恢复可见。"""
        try:
            if not self.get_visible():
                self.set_visible(True)
            self.present()
        except Exception:
            pass

    def _show_about(self) -> None:
        """显示「关于」对话框（Adw 标准，复用实例避免反复弹）。"""
        try:
            dlg = getattr(self, "_about_dlg", None)
            if dlg is None:
                dlg = Adw.AboutDialog()
                dlg.set_application_name(_("虾条播放器"))
                dlg.set_application_icon("xiatiao")
                dlg.set_version("1.0.2")
                dlg.set_comments(_("GTK4 本地音乐播放器，Rust 音频后端，支持 DSD 直通与 DSP"))
                dlg.set_copyright("© 2026 usbipad")
                # 法律信息：GTK 会据此提供 GPL-3.0 全文入口。
                dlg.set_license_type(Gtk.License.GPL_3_0)
                dlg.set_website("https://github.com/usbipad/Xiatiao-Player")
                dlg.set_issue_url("https://github.com/usbipad/Xiatiao-Player/issues")
                dlg.set_support_url("https://github.com/usbipad/Xiatiao-Player/issues")
                self._about_dlg = dlg
            dlg.present(self)
        except Exception:
            log.debug("显示关于对话框失败", exc_info=True)

    def _quit_app(self) -> None:
        """主动退出应用（Ctrl+Q / 菜单 / 托盘）。

        设 _force_quit 标志，确保即使开启「关闭时最小化到后台」也能真正退出。
        """
        self._force_quit = True
        app = self.get_application()
        if app is not None:
            app.quit()

    def _save_queue(self) -> None:
        """保存播放会话（队列 + 当前索引 + 位置 + 模式 + 音量），供重启恢复。"""
        try:
            tracks = self.playlist.tracks()
            pos = 0.0
            try:
                pos = float(self.player.position() or 0.0)
            except Exception:
                pos = 0.0
            data = {
                "tracks": tracks_to_list(tracks) if tracks else [],
                "index": int(self.playlist.current_index()) if tracks else -1,
                "position": pos,
                "shuffle": bool(self.playlist.is_shuffle()),
                "repeat": int(self.playlist.repeat_mode()),
                "volume": float(getattr(self.player_panel, "volume", None).get_value())
                if getattr(self.player_panel, "volume", None) is not None else 1.0,
            }
            get_cache().set("queue", data)
        except Exception as exc:
            log.debug("保存队列失败: %s", exc)

    def _restore_last_playback(self) -> bool:
        """启动时恢复上次的播放队列（可选，由 restore_playback 控制）。

        只恢复队列内容与当前索引，不自动播放（避免启动即出声）。
        """
        try:
            data = get_cache().get("queue")
            if not isinstance(data, dict):
                return False
            tracks = list_to_tracks(data.get("tracks"))
            if not tracks:
                return False
            idx = int(data.get("index", 0) or 0)
            if idx < 0:
                # 没有当前项：仅恢复队列，不设当前
                self._apply_restored_tracks(tracks, autoplay_index=-1)
                return False
            idx = max(0, min(idx, len(tracks) - 1))
            self._apply_restored_tracks(tracks, idx, data)
            self._refresh_ui_after_restore(idx)
            self._restore_volume(data)
            self._restore_position(data)
            return False
        except Exception as exc:
            log.debug("恢复队列失败: %s", exc)
            return False

    def _apply_restored_tracks(self, tracks, autoplay_index: int = -1, data=None) -> None:
        """在 _restoring 保护下设置队列与当前索引，并恢复播放模式。"""
        self._restoring = True
        try:
            if data is not None:
                try:
                    self.playlist.set_shuffle(bool(data.get("shuffle", False)))
                    self.playlist.set_repeat_mode(int(data.get("repeat", 0) or 0))
                except Exception:
                    pass
            self.playlist.set_tracks(tracks, autoplay_index=-1)
            if autoplay_index >= 0:
                self.playlist.set_current_index(autoplay_index)
        finally:
            self._restoring = False

    def _refresh_ui_after_restore(self, idx: int) -> None:
        """恢复后刷新播放界面（_restoring=True 包住，不触发播放）。"""
        try:
            self._restoring = True
            try:
                self._on_playlist_current_changed(self.playlist, idx)
            finally:
                self._restoring = False
        except Exception:
            pass

    def _restore_volume(self, data: dict) -> None:
        """恢复音量并同步滑块。"""
        try:
            vol = float(data.get("volume", 1.0) or 1.0)
            vol = max(0.0, min(1.0, vol))
            self.player.set_volume(vol)
            if getattr(self.player_panel, "volume", None) is not None:
                self.player_panel.volume.set_value(vol)
        except Exception:
            pass

    def _restore_position(self, data: dict) -> None:
        """延迟恢复播放位置（等当前曲目装载完）。"""
        try:
            pos = float(data.get("position", 0.0) or 0.0)
            if pos <= 0.5:
                return

            def _seek_restore():
                try:
                    cur = self.playlist.current_track()
                    dur = float(getattr(cur, "duration_seconds", 0.0) or 0.0)
                    if dur > 0:
                        self.player_panel.set_duration(dur)
                        self.now_playing.set_duration(dur)
                except Exception:
                    pass
                try:
                    self.player.seek_seconds(pos)
                except Exception:
                    pass
                try:
                    self.player_panel.set_position(pos)
                    self.now_playing.set_position(pos)
                except Exception:
                    pass
                return False

            GLib.timeout_add(800, _seek_restore)
        except Exception:
            pass

    # ============================================================
    # 工具
    # ============================================================
    def _toast(self, message: str) -> None:
        try:
            # 若存在可见的模态子窗口（如设置窗），把 toast 加到它上面，
            # 否则会被子窗口遮挡（用户看不到提示）。
            target = self._visible_child_window()
            if target is not None and hasattr(target, "add_toast"):
                target.add_toast(Adw.Toast.new(message))
            else:
                self._toast_overlay.add_toast(Adw.Toast.new(message))
        except Exception:
            log.info("提示: %s", message)

    def _visible_child_window(self):
        """返回当前可见的模态子窗口（设置窗等）；没有则返回 None。"""
        win = getattr(self, "_settings_win", None)
        try:
            if win is not None and win.get_visible():
                return win
        except Exception:
            pass
        return None
