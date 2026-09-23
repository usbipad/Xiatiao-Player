"""左侧播放器面板（Tonearm 风格）。

- 顶部封面 + 曲目信息 + 进度 + 控制按钮。
- 底部 Tab：Player / Lyrics / Queue，切换下方内容栈。
- 本面板只发 UI 事件回调，不直接操作 PlayerCore 内部。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from core.i18n import _

from .widgets.cover_art import CoverArt
from .widgets.lyrics_view import LyricsView
from .widgets.marquee_label import MarqueeLabel
from .widgets.seek_bar import SeekBar

log = logging.getLogger(__name__)


def _fmt_seconds(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


class PlayerPanel(Gtk.Box):
    """左侧播放器面板。

    宽度由 OverlaySplitView 线性跟随窗口，封面（正方形）随之平滑缩放；
    控件尺寸固定（见 style.css），不再随窗口分档缩放（避免拖动卡顿）。
    """

    __gtype_name__ = "PlayerPanel"

    def __init__(
        self,
        on_play_pause: Callable[[], None],
        on_prev: Callable[[], None],
        on_next: Callable[[], None],
        on_seek: Callable[[float], None],
        on_cover_clicked: Callable[[], None] | None = None,
        on_volume: Callable[[float], None] | None = None,
        on_shuffle: Callable[[bool], None] | None = None,
        on_repeat: Callable[[int], None] | None = None,
        on_func_toast: Callable[[str], None] | None = None,
        on_tab: Callable[[str], None] | None = None,
        on_open_settings: Callable[[], None] | None = None,
        on_like: Callable[[], None] | None = None,
        on_effect: Callable[[str], None] | None = None,
        on_effect_settings: Callable[[], None] | None = None,
        on_queue_activate: Callable[[object], None] | None = None,
        on_queue_action: Callable[[str, object], None] | None = None,
        on_add_queue: Callable[[], None] | None = None,
    ) -> None:
        self._on_add_queue = on_add_queue
        self._on_queue_activate = on_queue_activate
        self._on_queue_action = on_queue_action
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("player-panel")  # 浅色背景，区分右侧内容区
        self.set_size_request(280, -1)  # 最小宽度（避免拖太窄导致内容溢出）
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(14)
        self.set_margin_bottom(14)

        # ---- 顶部工具栏（固定面板顶部，标题居中 + 🔔 ☰ 右侧）----
        topbar = Gtk.CenterBox()
        topbar.set_valign(Gtk.Align.CENTER)
        topbar.set_margin_bottom(12)
        # 中间：头像 + 昵称；默认应用名，登录后由 set_user_info 改为账号昵称
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.set_valign(Gtk.Align.CENTER)
        # 头像：位于昵称左侧，登录后由 window 填充图片
        self.user_avatar = Gtk.Picture()
        self.user_avatar.set_size_request(30, 30)
        self.user_avatar.set_halign(Gtk.Align.CENTER)
        self.user_avatar.set_valign(Gtk.Align.CENTER)
        self.user_avatar.set_can_shrink(True)
        self.user_avatar.set_content_fit(Gtk.ContentFit.COVER)
        self.user_avatar.add_css_class("user-avatar")
        self.user_avatar.set_tooltip_text(_("未登录"))
        self.user_avatar.set_visible(False)
        title_box.append(self.user_avatar)
        self._title_lbl = Gtk.Label(label="Xiatiao Player")
        self._title_lbl.add_css_class("heading")
        self._title_lbl.set_ellipsize(3)
        title_box.append(self._title_lbl)
        topbar.set_center_widget(title_box)
        # 顶部工具栏右侧留空（菜单已移到右侧 HeaderBar，避免设置入口重复）
        topbar.set_end_widget(Gtk.Box(spacing=8))
        # 套 WindowHandle：拖动顶部工具栏（标题/空白）可移动窗口。
        # 按钮仍可点击（WindowHandle 只在拖动时接管）。
        topbar_handle = Gtk.WindowHandle()
        topbar_handle.set_child(topbar)
        self.append(topbar_handle)

        # 内层容器：优雅留白呼吸感排列
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_halign(Gtk.Align.FILL)

        # CenterBox：内容比视图矮时垂直居中；比视图高时由外层滚动
        center = Gtk.CenterBox()
        center.set_orientation(Gtk.Orientation.VERTICAL)
        center.set_center_widget(inner)
        center.set_valign(Gtk.Align.CENTER)
        self._center = center

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(center)
        self.append(scroll)
        self._inner = inner

        self._on_play_pause = on_play_pause
        self._on_prev = on_prev
        self._on_next = on_next
        self._on_seek = on_seek
        self._on_volume = on_volume
        self._on_shuffle = on_shuffle
        self._on_repeat = on_repeat
        self._on_func_toast = on_func_toast
        self._on_tab = on_tab
        self._on_like = on_like
        self._on_effect = on_effect
        self._on_effect_settings = on_effect_settings

        # ---- 封面 / 歌词 切换区（底部 Tab：Player 显示封面，Lyrics 显示歌词）----
        self._cover_area = Gtk.Stack()
        self._cover_area.set_size_request(280, -1)   # 封面区最小宽度
        # 左右滑入：与本地曲库卡片切换动画一致（点入口从左滑入新页）
        self._cover_area.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._cover_area.set_transition_duration(260)
        # 非均匀高度：Stack 高度取当前可见页（封面页=正方形），
        # 否则会被较高的歌词/队列页撑高，封面上下留白。
        try:
            self._cover_area.set_vhomogeneous(False)
            self._cover_area.set_hhomogeneous(False)
        except Exception:
            pass
        self.cover = CoverArt(on_clicked=on_cover_clicked)
        self._panel_lyrics = LyricsView(on_seek=self._on_seek, compact=True)
        self._cover_area.add_named(self.cover, "cover")
        self._cover_area.add_named(self._panel_lyrics, "lyrics")
        # 队列视图：当前播放列表（点击跳播）
        self._queue_view = self._build_queue_view()
        self._cover_area.add_named(self._queue_view, "queue")
        self._cover_area.set_visible_child_name("cover")
        inner.append(self._cover_area)

        # ---- 曲目信息 ----
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        info_box.set_margin_top(4)
        info_box.set_margin_bottom(2)
        info_box.set_vexpand(False)
        # 歌名 / 艺术家用跑马灯标签：文本超长时循环滚动完整展示，
        # 且水平 natural 宽度恒为 0，绝不撑宽面板（此前长艺术家名
        # 会把面板顶宽，正方形封面被连带顶大）。
        self.label_track = MarqueeLabel(_("未播放"), css_classes=["heading"])
        self.label_track.set_hexpand(True)
        self.label_artist = MarqueeLabel("—", css_classes=["dim-label"])
        self.label_artist.set_hexpand(True)
        info_box.append(self.label_track)
        info_box.append(self.label_artist)
        # 套 WindowHandle：拖动歌名/歌手区域也能移动窗口（纯文字，无交互冲突）
        info_handle = Gtk.WindowHandle()
        info_handle.set_child(info_box)
        inner.append(info_handle)

        # ---- 进度条（独立组件：点击/拖动 seek）----
        self.progress = SeekBar(on_seek=self._on_seek, on_drag=self._on_progress_drag)
        inner.append(self.progress)

        self.label_time_left = Gtk.Label(label="0:00")
        self.label_time_left.add_css_class("caption")
        # 音频技术信息独立一行（格式 · 采样率 · 位深 · 声道 · 码率）
        self.label_format = Gtk.Label(label="")
        self.label_format.add_css_class("caption")
        self.label_format.add_css_class("dim-label")
        self.label_format.set_ellipsize(3)
        self.label_format.set_halign(Gtk.Align.CENTER)
        self.label_time_right = Gtk.Label(label="0:00")
        self.label_time_right.add_css_class("caption")
        # CenterBox：中间留空，两侧时间对齐
        time_box = Gtk.CenterBox()
        time_box.set_start_widget(self.label_time_left)
        time_box.set_end_widget(self.label_time_right)
        inner.append(time_box)
        inner.append(self.label_format)

        # ---- 控制行（Apple Music 风格：随机 / 上 / 播 / 下 / 循环）----
        ctrl_box = Gtk.Box(spacing=12)
        ctrl_box.set_margin_top(2)
        ctrl_box.set_margin_bottom(2)
        ctrl_box.set_halign(Gtk.Align.CENTER)
        ctrl_box.set_valign(Gtk.Align.CENTER)

        self.btn_shuffle = Gtk.Button(icon_name="media-playlist-shuffle-symbolic")
        self.btn_shuffle.add_css_class("np-skip-btn")
        self.btn_shuffle.add_css_class("np-toggle")
        self.btn_shuffle.set_tooltip_text(_("随机播放"))
        self.btn_shuffle.connect("clicked", lambda *_: self._toggle_shuffle())

        btn_prev = Gtk.Button(icon_name="media-skip-backward-symbolic")
        btn_prev.add_css_class("np-skip-btn")
        self.btn_play = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.btn_play.add_css_class("np-play-btn")
        btn_next = Gtk.Button(icon_name="media-skip-forward-symbolic")
        btn_next.add_css_class("np-skip-btn")

        # 循环按钮：图标 + 可选数字（单曲循环显示"1"），用组合 child 实现三态区分
        self.btn_repeat = Gtk.Button()
        self.btn_repeat.add_css_class("np-skip-btn")
        self.btn_repeat.add_css_class("np-toggle")
        self.btn_repeat.set_tooltip_text(_("不循环"))
        repeat_box = Gtk.Box(spacing=1)
        repeat_box.set_valign(Gtk.Align.CENTER)
        self._repeat_icon = Gtk.Image.new_from_icon_name(
            "media-playlist-consecutive-symbolic"
        )
        self._repeat_num = Gtk.Label(label="")
        self._repeat_num.add_css_class("caption")
        repeat_box.append(self._repeat_icon)
        repeat_box.append(self._repeat_num)
        self.btn_repeat.set_child(repeat_box)
        self.btn_repeat.connect("clicked", lambda *_: self._cycle_repeat())

        btn_prev.connect("clicked", lambda *_: self._on_prev())
        btn_next.connect("clicked", lambda *_: self._on_next())
        self.btn_play.connect("clicked", lambda *_: self._on_play_pause())

        ctrl_box.append(self.btn_shuffle)
        ctrl_box.append(btn_prev)
        ctrl_box.append(self.btn_play)
        ctrl_box.append(btn_next)
        ctrl_box.append(self.btn_repeat)
        self._ctrl_box = ctrl_box
        self._btn_prev = btn_prev
        self._btn_next = btn_next
        inner.append(ctrl_box)

        # ---- 功能图标行（本地：音量；在线功能预留）----
        func_box = Gtk.Box(spacing=8)
        func_box.set_halign(Gtk.Align.CENTER)
        func_box.set_valign(Gtk.Align.CENTER)

        # 音量：点击弹出竖向滑块
        self._vol_btn = Gtk.MenuButton()
        self._vol_btn.set_icon_name("audio-volume-high-symbolic")
        self._vol_btn.add_css_class("flat")
        self._vol_btn.add_css_class("np-skip-btn")
        self._vol_btn.set_tooltip_text(_("音量"))

        vol_popover = Gtk.Popover()
        vol_popover.set_position(Gtk.PositionType.TOP)
        vol_slider = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL)
        vol_slider.set_range(0, 1)
        vol_slider.set_value(1.0)
        vol_slider.set_draw_value(False)
        vol_slider.set_inverted(True)  # 上大下小
        vol_slider.set_size_request(-1, 140)
        vol_slider.connect("value-changed", self._on_volume_changed)
        self.volume = vol_slider
        vol_popover.set_child(vol_slider)
        self._vol_btn.set_popover(vol_popover)
        func_box.append(self._vol_btn)

        # 喜欢按钮（接通本地喜欢）
        self.btn_like = Gtk.Button(icon_name="xiatiao-heart-outline-symbolic")
        self.btn_like.add_css_class("flat")
        self.btn_like.add_css_class("np-skip-btn")
        self.btn_like.set_tooltip_text(_("喜欢"))
        self.btn_like.connect("clicked", lambda *_: self._on_like and self._on_like())
        # 图标控件单独挂 CSS 类，便于直接改颜色
        _like_img = self.btn_like.get_child()
        if _like_img is not None:
            _like_img.add_css_class("like-icon")
        func_box.append(self.btn_like)

        # 音效：点击弹出模态对话框（预设列表 + DSP 设置入口）
        # 音效：自定义均衡器 symbolic 图标（律动条造型，跟随主题色）。
        self._effect_btn = Gtk.Button(icon_name="xiatiao-equalizer-symbolic")
        self._effect_btn.add_css_class("flat")
        self._effect_btn.add_css_class("np-skip-btn")
        self._effect_btn.set_tooltip_text(_("音效"))
        self._effect_btn.connect("clicked", self._on_effect_button_clicked)
        func_box.append(self._effect_btn)
        self._effect_buttons: dict = {}
        self._effect_current = ""
        self._effect_dialog = None
        self._effect_dialog_rows: dict = {}
        # 订阅「当前音效」单一状态源：任何一处改动都会广播到这里，
        # 若音效弹窗正开着则自动刷新高亮（无需手动同步）。
        try:
            from core.effect_state import get_effect_state
            self._effect_state = get_effect_state()
            self._effect_state.connect("changed", self._on_effect_state_changed)
        except Exception:
            self._effect_state = None

        # 添加到歌单：把当前播放曲目加入歌单（弹出选择对话框）
        self._add_queue_btn = Gtk.Button(icon_name="xiatiao-queue-add-symbolic")
        self._add_queue_btn.add_css_class("flat")
        self._add_queue_btn.add_css_class("np-skip-btn")
        self._add_queue_btn.set_tooltip_text(_("添加到歌单"))
        self._add_queue_btn.connect("clicked", lambda *_: self._on_add_queue and self._on_add_queue())
        func_box.append(self._add_queue_btn)

        self._func_box = func_box
        inner.append(func_box)

        # ---- 底部 Tab（Tonearm 风格：Player / Lyrics / Queue）----
        tab_box = Gtk.Box(spacing=4)
        tab_box.add_css_class("player-tab-bar")
        tab_box.set_halign(Gtk.Align.CENTER)
        tab_box.set_margin_top(4)
        self._tab_box = tab_box
        self._tab_buttons = []
        for label, icon, key in (
            ("Player", "audio-x-generic-symbolic", "player"),
            ("Lyrics", "xiatiao-lyrics-symbolic", "lyrics"),
            ("Queue", "view-list-symbolic", "queue"),
        ):
            btn = Gtk.Button()
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=label))
            btn.set_child(box)
            btn.add_css_class("flat")
            btn.add_css_class("nav-pill")
            btn.connect("clicked", self._on_tab_clicked, key)
            tab_box.append(btn)
            self._tab_buttons.append(btn)
        inner.append(tab_box)
        self._active_tab = "player"

        # 播放模式状态
        self._shuffle_on = False
        self._repeat_mode = 0  # 0=不循环 1=列表循环 2=单曲循环

    def _build_queue_view(self) -> Gtk.Widget:
        """当前播放队列（ListBox：序号 + 封面 + 歌名 + 歌手；点击跳播）。

        列表上下边缘加渐隐遮罩：滚动时内容淡出到背景，视觉更柔和。
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_size_request(240, -1)
        self._queue_list = Gtk.ListBox()
        self._queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._queue_list.add_css_class("queue-list")
        self._queue_list.connect("row-activated", self._on_queue_row_activated)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_min_content_height(360)
        scroll.set_child(self._queue_list)
        box.append(scroll)
        return box

    def set_queue(self, tracks: list, current_index: int = -1) -> None:
        """刷新队列列表；current_index 高亮当前播放项。"""
        try:
            # 清空
            c = self._queue_list.get_first_child()
            while c is not None:
                nxt = c.get_next_sibling()
                self._queue_list.remove(c)
                c = nxt
            for i, t in enumerate(tracks or []):
                row = Gtk.ListBoxRow()
                hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                hb.set_margin_start(8)
                hb.set_margin_end(8)
                hb.set_margin_top(4)
                hb.set_margin_bottom(4)
                num = Gtk.Label(label=str(i + 1))
                num.add_css_class("dim-label")
                num.add_css_class("caption")
                num.set_size_request(24, -1)
                hb.append(num)
                # 小封面（含 Hi-Res 徽标叠加）
                cover_widget = self._build_queue_cover(t)
                hb.append(cover_widget)
                info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                info.set_hexpand(True)
                title = Gtk.Label(label=getattr(t, "title", "") or _("未知"), xalign=0)
                title.set_ellipsize(3)
                artist = Gtk.Label(label=getattr(t, "artist", "") or "", xalign=0)
                artist.add_css_class("dim-label")
                artist.add_css_class("caption")
                artist.set_ellipsize(3)
                info.append(title)
                info.append(artist)
                hb.append(info)
                row.set_child(hb)
                row._track = t
                # 右键菜单：提升到下一首 / 从列表丢弃
                self._attach_queue_row_menu(row, t)
                if i == current_index:
                    row.add_css_class("queue-current")
                self._queue_list.append(row)
        except Exception:
            # 队列重建期间任一行出错会中断后续行；仅记 debug 日志保持容错，
            # 排查时（DEBUG 级别）可定位到失败点。
            log.debug("刷新队列列表失败", exc_info=True)

    def _attach_queue_row_menu(self, row, track) -> None:
        """给队列行挂右键菜单：提升到下一首 / 从列表丢弃。"""
        if getattr(row, "_rclick", None) is not None:
            return
        if getattr(self, "_on_queue_action", None) is None:
            return

        def on_right_click(gesture, n_press, x, y):
            menu = Gio.Menu()
            menu.append(_("提升到下一首"), "qrow.promote")
            menu.append(_("从列表丢弃"), "qrow.discard")
            popover = Gtk.PopoverMenu.new_from_model(menu)
            popover.add_css_class("media-menu")
            popover.set_parent(row)
            popover.set_has_arrow(False)
            popover.set_halign(Gtk.Align.START)
            rect = Gdk.Rectangle()
            rect.x = int(x)
            rect.y = int(y)
            rect.width = 1
            rect.height = 1
            popover.set_pointing_to(rect)
            # 动作组：作用于本行曲目
            ag = Gio.SimpleActionGroup()

            def _mk(name, action_name):
                a = Gio.SimpleAction.new(name, None)
                a.connect("activate", lambda *_: self._on_queue_action(action_name, track))
                ag.add_action(a)

            _mk("promote", "promote")
            _mk("discard", "discard")
            row.insert_action_group("qrow", ag)
            popover.popup()

        g = Gtk.GestureClick()
        g.set_button(3)
        g.connect("pressed", on_right_click)
        row.add_controller(g)
        row._rclick = g

    def _build_queue_cover(self, track) -> Gtk.Widget:
        """队列行小封面：32px 封面图 + 右下角 Hi-Res 徽标。

        封面异步加载（复用 ui.pages 的缓存与工具），加载完成前显示占位图标。
        """
        _QCOVER = 32
        overlay = Gtk.Overlay()
        overlay.set_size_request(_QCOVER, _QCOVER)
        overlay.set_valign(Gtk.Align.CENTER)

        pic = Gtk.Picture()
        pic.set_content_fit(Gtk.ContentFit.COVER)
        pic.set_size_request(_QCOVER, _QCOVER)
        pic.set_can_shrink(True)
        pic.add_css_class("queue-row-cover")
        overlay.set_child(pic)

        badge = Gtk.Label(label="HR")
        badge.add_css_class("hires-badge")
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.END)
        badge.set_visible(False)
        overlay.add_overlay(badge)

        # 徽标：按曲目规格决定显示 DSD / HR / CD
        try:
            label = getattr(track, "quality_badge", "") or ""
            badge.set_text(label)
            badge.set_visible(bool(label))
            badge.remove_css_class("cd-badge")
            badge.remove_css_class("dsd-badge")
            if label == "DSD":
                badge.remove_css_class("hires-badge")
                badge.add_css_class("dsd-badge")
            elif label == "CD":
                badge.remove_css_class("hires-badge")
                badge.add_css_class("cd-badge")
        except Exception:
            pass

        # 封面：仅本地曲目按 filepath 读取；在线曲目（有 cover_bytes/url）暂用占位
        filepath = getattr(track, "filepath", "") or ""
        if not filepath:
            return overlay
        try:
            from ui.pages import _cache_get, _cache_put, _load_cover_bytes, _MISS
        except Exception:
            return overlay
        cached = _cache_get(filepath)
        if cached is not _MISS and cached is not None:
            pic.set_paintable(cached)
            return overlay
        if cached is _MISS:
            # 未缓存：异步加载（复用 pages 的后台读图 + 缓存）
            from core.tasks import run_async

            def _done(png_bytes) -> None:
                tex = None
                if png_bytes:
                    try:
                        tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_bytes))
                    except Exception:
                        tex = None
                _cache_put(filepath, tex)
                if tex is not None:
                    try:
                        pic.set_paintable(tex)
                    except Exception:
                        pass

            try:
                run_async(work=lambda: _load_cover_bytes(filepath), on_done=_done)
            except Exception:
                pass
        return overlay

    def set_queue_current(self, current_index: int) -> None:
        """仅更新队列中「当前播放」高亮，不重建任何行。

        切歌时队列组成通常不变，整表重建会明显卡顿；此方法只切 CSS 类。
        """
        try:
            i = 0
            c = self._queue_list.get_first_child()
            while c is not None:
                if i == current_index:
                    c.add_css_class("queue-current")
                else:
                    c.remove_css_class("queue-current")
                c = c.get_next_sibling()
                i += 1
        except Exception:
            pass

    def _on_queue_row_activated(self, _listbox, row) -> None:
        """点队列某行 → 跳播（通过 on_next/外部回调）。"""
        track = getattr(row, "_track", None)
        if track is not None and getattr(self, "_on_queue_activate", None) is not None:
            try:
                self._on_queue_activate(track)
            except Exception:
                pass

    def _on_tab_clicked(self, btn, key: str) -> None:
        """底部 Tab 切换：Player 封面 / Lyrics 歌词 / Queue 队列。"""
        self._active_tab = key
        if key == "lyrics":
            self._cover_area.set_visible_child_name("lyrics")
            self._cover_area.set_vexpand(True)
            self._center.set_valign(Gtk.Align.FILL)
        elif key == "queue":
            self._cover_area.set_visible_child_name("queue")
            self._cover_area.set_vexpand(True)
            self._center.set_valign(Gtk.Align.FILL)
        else:
            self._cover_area.set_visible_child_name("cover")
            self._cover_area.set_vexpand(False)
            self._center.set_valign(Gtk.Align.CENTER)
        if self._on_tab is not None:
            self._on_tab(key)

    def set_lyrics(self, lyrics) -> None:
        """更新面板内的歌词（供窗口同步）。"""
        self._panel_lyrics.set_lyrics(lyrics)

    def set_viz_data(self, data) -> None:
        """喂入一帧可视化幅度数组（由窗口定时器调用）。"""
        self.viz.set_data(data)

    def set_user_info(self, info) -> None:
        """已登录时顶部显示账号昵称，未登录显示应用名。

        info 为 {nick, avatar_url} 或 None；头像显示在右侧 HeaderBar，
        这里只更新文字。
        """
        nick = info.get("nick") if isinstance(info, dict) else ""
        self._title_lbl.set_text(nick or "Xiatiao Player")
        # 有头像时显示头像，未登录/无头像时隐藏
        url = info.get("avatar_url") if isinstance(info, dict) else ""
        if nick:
            self.user_avatar.set_tooltip_text(nick)
        self.user_avatar.set_visible(bool(url))

    #: 音效预设展示名（键与 PlayerCore 一致）
    _EFFECT_LABELS = [
        ("off", "关闭"),
        ("pop", "流行"),
        ("rock", "摇滚"),
        ("classical", "古典"),
        ("jazz", "爵士"),
        ("bass", "低音增强"),
    ]

    #: 是否启用「主页音效按钮 → 模态对话框」模式（由 window 打开）
    use_effect_dialog: bool = False

    def set_effect_presets(self, names: list, current: str) -> None:
        """由 window 设置预设列表（模态对话框模式下仅记录当前选中）。"""
        self._effect_presets = list(names or [])
        self._effect_current = current or ""

    # ------------------------------------------------------------
    # 音效模态对话框
    # ------------------------------------------------------------
    def _on_effect_button_clicked(self, _btn) -> None:
        """点击音效按钮：弹出模态对话框。"""
        if not getattr(self, "use_effect_dialog", False):
            # 未启用对话框模式时退回旧行为（打开设置页）
            if self._on_effect_settings is not None:
                self._on_effect_settings()
            return
        self._open_effect_dialog()

    def _open_effect_dialog(self) -> None:
        """构建并显示音效选择对话框。"""
        # 已开着则直接置前，避免重复点击叠出多个对话框
        existing = getattr(self, "_effect_dialog", None)
        if existing is not None:
            try:
                existing.present(self)
                return
            except Exception:
                self._effect_dialog = None

        from core.eq_presets import BUILTIN_PRESETS

        dialog = Adw.Dialog()
        dialog.set_title(_("音效"))
        dialog.set_content_width(420)
        dialog.set_content_height(560)
        self._effect_dialog = dialog

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)

        group = Adw.PreferencesGroup()
        group.set_title(_("内置音效"))

        # 选中态用「行高亮」表达（不再用勾选框）：
        # 勾选框可被手动取消，造成「勾没了但音效还开着」的矛盾。
        # 高亮统一在构建完所有行后由 _refresh_effect_dialog_highlight()
        # 按单一源设置，这里不逐个判断，避免与单一源不一致。
        self._effect_dialog_rows = {}
        for preset in BUILTIN_PRESETS:
            name = preset["name"]
            row = Adw.ActionRow()
            row.set_title(name)
            row.set_activatable(True)
            # 点整行 = 选中该音效（高亮 + 下发）
            row.connect("activated", self._on_effect_dialog_choice, name)
            group.add(row)
            self._effect_dialog_rows[name] = row
        body.append(group)

        # ---- 「我的预设」：DSP 设置页保存的自定义预设（可删除）----
        # 注意：勾选统一用 cur（= self._effect_current），与内置区共享同一
        # 「当前音效」状态；不能用 dsp_store.current() 单独判断，否则内置与
        # 自定义可能同时被勾选（显示冲突）。
        try:
            from core.dsp_store import get_dsp_preset_store
            store = get_dsp_preset_store()
            custom_names = store.names()
        except Exception:
            custom_names = []
        custom_group = Adw.PreferencesGroup()
        custom_group.set_title(_("我的预设"))
        if custom_names:
            for name in custom_names:
                row = Adw.ActionRow()
                row.set_title(name)
                # 删除按钮（仅自定义预设可删）
                del_btn = Gtk.Button(icon_name="user-trash-symbolic")
                del_btn.set_valign(Gtk.Align.CENTER)
                del_btn.add_css_class("flat")
                del_btn.set_tooltip_text(_("删除该预设"))
                del_btn.connect("clicked", self._on_effect_dialog_delete, name)
                row.add_suffix(del_btn)
                row.set_activatable(True)
                # 点整行 = 选中该音效（高亮 + 下发）
                row.connect("activated", self._on_effect_dialog_choice, name)
                custom_group.add(row)
                self._effect_dialog_rows[name] = row
        else:
            hint = Adw.ActionRow()
            hint.set_title(_("还没有自定义预设"))
            hint.set_subtitle(_("在「DSP 音效设置」中调好参数后保存"))
            hint.set_activatable(False)
            custom_group.add(hint)
        body.append(custom_group)

        # DSP 详细设置入口
        cfg_group = Adw.PreferencesGroup()
        cfg_row = Adw.ActionRow()
        cfg_row.set_title(_("DSP 音效设置…"))
        cfg_row.set_subtitle(_("手动微调 EQ / 低音 / 压缩等参数"))
        cfg_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        cfg_row.set_activatable(True)
        cfg_row.connect("activated", self._on_effect_dialog_settings)
        cfg_group.add(cfg_row)
        body.append(cfg_group)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(body)
        toolbar.set_content(scroll)
        dialog.set_child(toolbar)
        # 构建完各行后，统一按单一源刷新一次高亮（保证打开即正确）。
        self._refresh_effect_dialog_highlight()
        # 手动关闭时清理引用（否则 _effect_dialog 会悬空，
        # 且下次点击按钮时防重逻辑会误判为「已开着」）
        dialog.connect("closed", self._on_effect_dialog_closed)
        dialog.present(self)

    def _on_effect_dialog_closed(self, _dialog) -> None:
        """对话框关闭：清引用。"""
        self._effect_dialog = None

    def _on_effect_state_changed(self, _state, name: str) -> None:
        """「当前音效」变更：同步缓存 + 刷新已打开的弹窗高亮。"""
        self._effect_current = name or ""
        self._refresh_effect_dialog_highlight()

    def _refresh_effect_dialog_highlight(self) -> None:
        """把弹窗各行高亮刷新为「当前音效」（实时读单一源，幂等）。

        始终读 EffectState.current()，不依赖广播传参、不缓存 —— 保证任何
        时刻高亮都等于单一真相，不会因广播时机/值未变而漏刷。
        """
        rows = getattr(self, "_effect_dialog_rows", None)
        if not rows:
            return
        current = ""
        try:
            if getattr(self, "_effect_state", None) is not None:
                current = self._effect_state.current()
        except Exception:
            current = ""
        for name, row in rows.items():
            try:
                if name == current:
                    row.add_css_class("effect-selected")
                else:
                    row.remove_css_class("effect-selected")
            except Exception:
                pass

    def _on_effect_dialog_choice(self, _row, name: str) -> None:
        """点某行：选中该音效（高亮 + 下发）。

        高亮统一走「单一源 + 广播 → _refresh_effect_dialog_highlight」，
        不在此手动改 class，避免与单一源不一致。
        """
        self._select_effect(name)

    def _on_effect_dialog_delete(self, _btn, name: str) -> None:
        """删除自定义预设（仅「我的预设」有删除按钮；内置预设无）。"""
        try:
            from core.dsp_store import get_dsp_preset_store
            if get_dsp_preset_store().remove(name):
                if self._on_func_toast is not None:
                    self._on_func_toast(_("已删除预设：{name}").format(name=name))
        except Exception:
            pass
        # 刷新对话框（关闭重开，简单可靠）
        dlg = getattr(self, "_effect_dialog", None)
        if dlg is not None:
            try:
                dlg.close()
            except Exception:
                pass
            self._effect_dialog = None
        self._open_effect_dialog()

    def _select_effect(self, name: str) -> None:
        """选中预设：更新单一源 + 回调 window（下发音频）。

        高亮由 EffectState 广播统一刷新，此处不再直接改 UI。
        不自动关闭对话框：方便连续试听、对比不同预设。
        """
        self._effect_current = name
        # 更新单一源：广播 → _refresh_effect_dialog_highlight 统一刷新高亮。
        try:
            if getattr(self, "_effect_state", None) is not None:
                self._effect_state.set_current(name)
        except Exception:
            pass
        if self._on_effect is not None:
            self._on_effect(name)

    def _on_effect_dialog_settings(self, _row) -> None:
        dlg = getattr(self, "_effect_dialog", None)
        if dlg is not None:
            try:
                dlg.close()
            except Exception:
                pass
            self._effect_dialog = None
        if self._on_effect_settings is not None:
            self._on_effect_settings()

    def rebuild_effect_menu(self) -> None:
        """兼容旧调用：无 Popover，空实现。"""
        pass

    def _on_preset_clicked(self, _btn, name: str) -> None:
        self._select_effect(name)

    def _on_effect_settings_clicked(self, _btn) -> None:
        if self._on_effect_settings is not None:
            self._on_effect_settings()

    def _on_effect_selected(self, _btn, key: str, popover=None) -> None:
        """（保留旧接口）音效预设被点击：更新高亮、回调外部。"""
        self.set_effect_state(key)
        if popover is not None:
            try:
                popover.popdown()
            except Exception:
                pass
        if self._on_effect is not None:
            self._on_effect(key)

    def set_effect_state(self, key: str) -> None:
        """纯 UI 同步（不触发回调）：高亮当前音效预设。"""
        for k, btn in getattr(self, "_effect_buttons", {}).items():
            if k == key:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        # 非关闭状态时按钮高亮，便于一眼看出音效已开启
        if hasattr(self, "_effect_btn"):
            if key and key != "off":
                self._effect_btn.add_css_class("suggested-action")
            else:
                self._effect_btn.remove_css_class("suggested-action")

    def set_liked(self, liked: bool) -> None:
        """设置喜欢按钮状态。

        喜欢：实心爱心；未喜欢：空心爱心。
        颜色跟随主题前景色（浅色主题黑 / 暗色主题白）。
        """
        try:
            img = self.btn_like.get_child()
            if liked:
                self.btn_like.add_css_class("liked-active")
                self.btn_like.set_tooltip_text(_("取消喜欢"))
            else:
                self.btn_like.remove_css_class("liked-active")
                self.btn_like.set_tooltip_text(_("喜欢"))
            # 自定义爱心图标：已喜欢=实心，未喜欢=空心；
            # 颜色跟随主题前景色（currentColor）。
            if img is not None:
                img.set_from_icon_name(
                    "xiatiao-heart-filled-symbolic" if liked
                    else "xiatiao-heart-outline-symbolic")
        except Exception:
            pass

    # ---- 播放模式 ----
    def _toggle_shuffle(self) -> None:
        self._shuffle_on = not self._shuffle_on
        self.set_shuffle_state(self._shuffle_on)
        if self._on_shuffle is not None:
            self._on_shuffle(self._shuffle_on)

    def set_shuffle_state(self, on: bool) -> None:
        """纯 UI 同步（不触发回调），供外部保持状态一致。

        用 np-toggle-on：style.css 已为它定义了激活态（强调色前景 +
        半透明强调色背景 + 同色弥散阴影）。此前用 suggested-action，
        但本应用样式表未定义该类、系统主题里也只有 :not(.suggested-action)
        排除式规则，故开与关外观完全相同。
        """
        self._shuffle_on = bool(on)
        if self._shuffle_on:
            self.btn_shuffle.add_css_class("np-toggle-on")
        else:
            self.btn_shuffle.remove_css_class("np-toggle-on")

    def _cycle_repeat(self) -> None:
        self._repeat_mode = (self._repeat_mode + 1) % 3
        # 三态用三个可区分的图标：
        # 三态：
        #   0 不循环   -> 顺序箭头（consecutive），无数字
        #   1 列表循环 -> 循环箭头（repeat），无数字
        #   2 单曲循环 -> 循环箭头（repeat）+ 数字 "1"
        # 不再使用背景色，仅靠图标 + 数字区分。
        self.set_repeat_state(self._repeat_mode)
        if self._on_repeat is not None:
            self._on_repeat(self._repeat_mode)

    def set_repeat_state(self, mode: int) -> None:
        """纯 UI 同步（不触发回调）：更新循环图标、数字、提示。"""
        self._repeat_mode = mode % 3
        icons = {
            0: "media-playlist-consecutive-symbolic",
            1: "media-playlist-repeat-symbolic",
            2: "media-playlist-repeat-symbolic",
        }
        self._repeat_icon.set_from_icon_name(icons[self._repeat_mode])
        self._repeat_num.set_text("1" if self._repeat_mode == 2 else "")
        tips = {0: "不循环", 1: "列表循环", 2: "单曲循环"}
        self.btn_repeat.set_tooltip_text(tips[self._repeat_mode])

    # ---- 音量交互 ----
    def _update_volume_icon(self, vol: float) -> None:
        """按音量值更新喇叭图标。"""
        try:
            if vol <= 0.01:
                self._vol_btn.set_icon_name("audio-volume-muted-symbolic")
            elif vol < 0.4:
                self._vol_btn.set_icon_name("audio-volume-low-symbolic")
            elif vol < 0.7:
                self._vol_btn.set_icon_name("audio-volume-medium-symbolic")
            else:
                self._vol_btn.set_icon_name("audio-volume-high-symbolic")
        except Exception:
            pass

    def set_volume(self, value: float) -> None:
        """外部同步音量（不触发回调），用于 MPRIS 等外部来源改音量后刷新 UI。"""
        try:
            v = max(0.0, min(1.0, float(value)))
        except Exception:
            return
        try:
            self.volume.handler_block_by_func(self._on_volume_changed)
            self.volume.set_value(v)
            self.volume.handler_unblock_by_func(self._on_volume_changed)
        except Exception:
            pass
        self._update_volume_icon(v)

    def _on_volume_changed(self, scale) -> None:
        vol = scale.get_value()
        self._update_volume_icon(vol)
        if self._on_volume is not None:
            self._on_volume(vol)

    # ---- 进度条交互 ----
    def set_progress_color(self, rgb) -> None:
        """设置主界面进度条已播段颜色（跟随封面主色，加深版）；None 回退默认。"""
        try:
            self.progress.set_played_color(rgb)
        except Exception:
            pass

    def _on_progress_drag(self, value) -> None:
        """拖动进度条时同步左侧时间标签（seek 由 SeekBar 内部处理）。"""
        self.label_time_left.set_text(_fmt_seconds(value))

    # ---- 外部更新接口 ----
    def set_track_info(self, title: str, artist: str) -> None:
        self.label_track.set_text(title)
        self.label_artist.set_text(artist)

    def set_playing_from(self, name: str, cover_bytes: bytes | None = None) -> None:
        """Playing From 行已移除，保留空实现以兼容调用方。"""
        return None

    def set_available_qualities(self, qualities, current: str, on_select) -> None:
        """音质按钮已移除，保留空实现以兼容调用方。"""
        return None

    def set_format_info(self, text: str) -> None:
        """显示音频技术信息（格式 · 采样率 · 位深 · 声道 · 码率）。

        独立于音质按钮（后者只显示档位名），两者互不覆盖。
        """
        self.label_format.set_text(text or "")

    def set_cover(self, image_bytes: bytes | None) -> None:
        """更新封面。传入图片字节则显示，None 则回退占位符号。"""
        self.cover.set_cover(image_bytes)

    def set_duration(self, seconds: float) -> None:
        self.progress.set_duration(seconds)
        self.label_time_right.set_text(_fmt_seconds(seconds))

    def reset_position(self) -> None:
        """切歌时重置进度显示，避免沿用上一首的位置。"""
        self.progress.reset()
        self.label_time_left.set_text("0:00")

    def set_position(self, seconds: float) -> None:
        self.progress.set_position(seconds)
        # SeekBar 冻结期间（seek 后短暂）不更新已播时间，避免时间标签闪回
        frozen = bool(getattr(self.progress, "_seeking", False))
        if not frozen:
            self.label_time_left.set_text(_fmt_seconds(seconds))
        self._panel_lyrics.set_position(seconds)

    def set_playing(self, playing: bool) -> None:
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        self.btn_play.set_icon_name(icon)

