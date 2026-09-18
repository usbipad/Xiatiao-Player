"""全屏 Now Playing 页：封面背景 + 大封面 + 滚动歌词 + 控制。

背景：当前封面拉伸铺满整页，上覆暗色遮罩让前景可读。
前景：左大封面 + 右歌词 + 底部进度条与播放控制。
"""
from __future__ import annotations

from gi.repository import Gdk, GLib, Gtk

from core.i18n import _

from models import extract_dominant_color, make_square_cover_bytes

from .widgets.lyrics_view import LyricsView
from .widgets.seek_bar import SeekBar


def _fmt(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


class NowPlayingPage(Gtk.Overlay):
    """全屏播放页。"""

    def __init__(self, on_exit, on_seek=None, on_play_pause=None,
                 on_prev=None, on_next=None, on_shuffle=None,
                 on_repeat=None, on_volume=None) -> None:
        super().__init__()
        self.add_css_class("now-playing-root")
        self._on_exit = on_exit
        self._on_seek = on_seek
        self._on_play_pause = on_play_pause
        self._on_prev = on_prev
        self._on_next = on_next
        self._on_shuffle = on_shuffle
        self._on_repeat = on_repeat
        self._on_volume = on_volume
        # 播放模式状态（与左侧面板同步）
        self._shuffle_on = False
        self._repeat_mode = 0
        # 复用的背景色 CSS provider（避免每次切歌新建导致累积泄漏）
        self._bg_provider = Gtk.CssProvider()
        self._bg_provider_installed = False

        # ============ 背景：封面模糊图拉伸铺满（Apple Music 风格）============
        # 用 Picture 铺满整页；模糊+暗化在后台线程生成，主线程只设纹理。
        # 无封面时清空纹理，露出下面由动态 CSS 设的纯色背景。
        self._bg_picture = Gtk.Picture()
        self._bg_picture.add_css_class("np-bg")
        self._bg_picture.set_content_fit(Gtk.ContentFit.COVER)   # 拉伸铺满裁剪
        self._bg_picture.set_can_shrink(True)
        self._bg_picture.set_hexpand(True)
        self._bg_picture.set_vexpand(True)
        self._bg_picture.set_halign(Gtk.Align.FILL)
        self._bg_picture.set_valign(Gtk.Align.FILL)
        self.set_child(self._bg_picture)

        # ============ 前景层 ============
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.add_css_class("np-content")
        self.add_overlay(content)

        # ---- 顶部栏 ----
        top = Gtk.Box(spacing=12)
        top.set_margin_start(16)
        top.set_margin_end(16)
        top.set_margin_top(12)
        top.set_margin_bottom(12)

        back_btn = Gtk.Button(icon_name="go-down-symbolic")
        back_btn.set_tooltip_text(_("返回（Esc）"))
        back_btn.add_css_class("circular")
        back_btn.set_can_focus(False)   # 避免空格键误激活该按钮
        back_btn.connect("clicked", lambda *_: self._on_exit())
        top.append(back_btn)

        # 中间留空，把最大化按钮推到右侧
        spacer = Gtk.Box(hexpand=True)
        top.append(spacer)

        self._max_btn = Gtk.Button(icon_name="window-maximize-symbolic")
        self._max_btn.add_css_class("circular")
        self._max_btn.set_can_focus(False)   # 避免空格键误激活
        self._max_btn.set_tooltip_text(_("最大化"))
        self._max_btn.connect("clicked", self._on_toggle_maximize)
        top.append(self._max_btn)
        # 顶部栏套 WindowHandle：拖顶部空白区可移动窗口（沉浸式页没有系统标题栏）
        handle = Gtk.WindowHandle()
        handle.set_child(top)
        content.append(handle)

        # ---- 主体：左封面 + 右歌词 ----
        # 两侧弹性 spacer 对称，使整组内容水平居中，不随窗口宽度散开。
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_margin_start(48)
        body.set_margin_end(48)
        body.set_margin_top(8)
        body.set_margin_bottom(8)
        body.set_vexpand(True)
        content.append(body)

        body.append(Gtk.Box(hexpand=True))

        # 左列：封面 + 歌曲信息（与封面同宽，居中对齐）
        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        left_col.set_valign(Gtk.Align.CENTER)
        left_col.set_halign(Gtk.Align.CENTER)
        left_col.set_size_request(self._COVER_PX, -1)
        left_col.set_hexpand(False)
        left_col.set_vexpand(False)
        body.append(left_col)

        # 封面固定为正方形，避免随图片大小变化导致布局跳动。
        # 用 AspectFrame(obey_child=False)：忽略图片 natural size，
        # 强制占用给定空间；Picture 用 COVER 填充裁剪。
        self._cover = Gtk.Picture()
        self._cover.set_content_fit(Gtk.ContentFit.COVER)
        self._cover.set_can_shrink(True)
        self._cover.set_size_request(self._COVER_PX, self._COVER_PX)
        # 占位页（无封面时显示）：柔和渐变背景 + 居中音符图标
        self._np_placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._np_placeholder.add_css_class("cover-placeholder-box")
        # 沉浸页专属：用不透明背景，避免半透明渐变叠在动态封面主色背景上
        # 显得比主界面暗（主界面底下是主题亮色，沉浸页底下是封面主色）。
        self._np_placeholder.add_css_class("np-cover-placeholder")
        self._np_placeholder.set_size_request(self._COVER_PX, self._COVER_PX)
        _np_ph_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        try:
            _np_ph_icon.set_pixel_size(96)
        except Exception:
            pass
        _np_ph_icon.add_css_class("cover-placeholder-icon")
        _np_ph_icon.set_halign(Gtk.Align.CENTER)
        _np_ph_icon.set_valign(Gtk.Align.CENTER)
        _np_ph_icon.set_vexpand(True)
        self._np_placeholder.append(_np_ph_icon)
        # Stack：cover(图片) / placeholder(占位)
        self._cover_stack = Gtk.Stack()
        self._cover_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._cover_stack.add_named(self._cover, "cover")
        self._cover_stack.add_named(self._np_placeholder, "placeholder")
        self._cover_stack.set_visible_child_name("placeholder")
        # 圆角用容器遮罩实现（CSS border-radius + overflow hidden）。
        cover_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cover_frame.add_css_class("np-cover")
        cover_frame.append(self._cover_stack)
        cover_frame.set_size_request(self._COVER_PX, self._COVER_PX)
        cover_frame.set_halign(Gtk.Align.CENTER)
        cover_frame.set_valign(Gtk.Align.CENTER)
        cover_frame.set_overflow(Gtk.Overflow.HIDDEN)
        left_col.append(cover_frame)

        # 封面下方：歌名 / 歌手
        # 固定高度 + 顶部对齐：避免不同歌名换行数不同导致封面位置上下跳动。
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        info.set_halign(Gtk.Align.FILL)
        info.set_valign(Gtk.Align.START)
        info.set_size_request(self._COVER_PX, 120)
        self._track_label = Gtk.Label(label="")
        self._track_label.add_css_class("np-track-name")
        self._track_label.set_justify(Gtk.Justification.CENTER)
        self._track_label.set_wrap(True)
        self._track_label.set_lines(2)          # 最多 2 行，超出省略
        self._track_label.set_ellipsize(3)      # Pango.EllipsizeMode.END
        self._track_label.set_valign(Gtk.Align.START)
        self._artist_label = Gtk.Label(label="")
        self._artist_label.add_css_class("np-secondary")
        self._artist_label.set_justify(Gtk.Justification.CENTER)
        self._artist_label.set_ellipsize(3)
        self._artist_label.set_valign(Gtk.Align.START)
        # 音频技术信息（格式 / 采样率 / 位深 / 声道 / 码率）
        self._format_label = Gtk.Label(label="")
        self._format_label.add_css_class("np-secondary")
        self._format_label.add_css_class("caption")
        self._format_label.set_justify(Gtk.Justification.CENTER)
        self._format_label.set_ellipsize(3)
        self._format_label.set_valign(Gtk.Align.START)
        info.append(self._track_label)
        info.append(self._artist_label)
        info.append(self._format_label)
        left_col.append(info)

        # 频谱：宽度与封面对齐，位于歌名/信息下方
        from .widgets.viz_renderer import VizRenderer
        self.viz = VizRenderer(style="bars")
        self.viz.set_size_request(self._COVER_PX, 48)
        self.viz.set_halign(Gtk.Align.CENTER)
        self.viz.set_valign(Gtk.Align.START)
        self.viz.set_soft(True)          # 柔和淡色（跟背景搭）
        left_col.append(self.viz)

        # 封面与歌词之间固定间距
        gap = Gtk.Box()
        gap.set_size_request(300, -1)
        body.append(gap)

        # 右列：歌词（独立组件：高亮 + 缓动滚动 + 点击跳转 + 首尾渐隐）
        self.lyrics = LyricsView(on_seek=on_seek)
        self.lyrics.set_size_request(480, -1)   # 歌词固定宽度
        self.lyrics.set_hexpand(False)
        body.append(self.lyrics)

        # 右侧弹性 spacer：与左侧对称，整组居中
        body.append(Gtk.Box(hexpand=True))

        # ---- 底部：控制按钮 + 进度条 ----
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        bottom.set_margin_start(56)
        bottom.set_margin_end(56)
        bottom.set_margin_top(12)
        bottom.set_margin_bottom(36)

        # 播放控制（Apple Music 风格分组：辅助组变大距离、核心组紧凑）
        ctrl = Gtk.Box(spacing=40)
        ctrl.set_halign(Gtk.Align.CENTER)
        ctrl.set_valign(Gtk.Align.CENTER)

        # 音量：与控制按钮同一排，点击向上弹出竖向滑块
        self._vol_btn = Gtk.MenuButton()
        self._vol_btn.set_icon_name("audio-volume-high-symbolic")
        self._vol_btn.add_css_class("flat")
        self._vol_btn.add_css_class("np-skip-btn")
        self._vol_btn.add_css_class("np-aux-btn")
        self._vol_btn.set_tooltip_text(_("音量"))
        self._vol_btn.set_valign(Gtk.Align.CENTER)

        vol_popover = Gtk.Popover()
        vol_popover.set_position(Gtk.PositionType.TOP)
        self._vol_slider = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL)
        self._vol_slider.set_range(0, 1)
        self._vol_slider.set_value(1.0)
        self._vol_slider.set_draw_value(False)
        self._vol_slider.set_inverted(True)   # 上大下小
        self._vol_slider.set_size_request(-1, 140)
        self._vol_slider.connect("value-changed", self._on_volume_changed)
        vol_popover.set_child(self._vol_slider)
        self._vol_btn.set_popover(vol_popover)

        # 左辅助：随机（小号）
        self._btn_shuffle = Gtk.Button(icon_name="media-playlist-shuffle-symbolic")
        self._btn_shuffle.add_css_class("np-skip-btn")
        self._btn_shuffle.add_css_class("np-aux-btn")
        self._btn_shuffle.set_tooltip_text(_("随机播放"))
        self._btn_shuffle.set_valign(Gtk.Align.CENTER)
        self._btn_shuffle.connect("clicked", lambda *_: self._toggle_shuffle())
        ctrl.append(self._btn_shuffle)

        # 核心组：上一曲 / 播放 / 下一曲（紧凑）
        core = Gtk.Box(spacing=20)
        core.set_valign(Gtk.Align.CENTER)
        btn_prev = Gtk.Button(icon_name="media-skip-backward-symbolic")
        btn_prev.add_css_class("np-skip-btn")
        btn_prev.connect("clicked", lambda *_: self._on_prev and self._on_prev())
        self._btn_play = Gtk.Button(icon_name="media-playback-start-symbolic")
        self._btn_play.add_css_class("np-play-btn")
        self._btn_play.set_can_focus(False)
        self._btn_play.connect("clicked", lambda *_: self._on_play_pause and self._on_play_pause())
        btn_next = Gtk.Button(icon_name="media-skip-forward-symbolic")
        btn_next.add_css_class("np-skip-btn")
        btn_next.connect("clicked", lambda *_: self._on_next and self._on_next())
        core.append(btn_prev)
        core.append(self._btn_play)
        core.append(btn_next)
        ctrl.append(core)

        # 右辅助：循环（小号，三态图标 + 数字 1）
        self._btn_repeat = Gtk.Button()
        self._btn_repeat.add_css_class("np-skip-btn")
        self._btn_repeat.add_css_class("np-aux-btn")
        self._btn_repeat.set_valign(Gtk.Align.CENTER)
        self._btn_repeat.set_tooltip_text(_("不循环"))
        repeat_box = Gtk.Box(spacing=1)
        repeat_box.set_valign(Gtk.Align.CENTER)
        self._repeat_icon = Gtk.Image.new_from_icon_name(
            "media-playlist-consecutive-symbolic"
        )
        self._repeat_num = Gtk.Label(label="")
        self._repeat_num.add_css_class("caption")
        repeat_box.append(self._repeat_icon)
        repeat_box.append(self._repeat_num)
        self._btn_repeat.set_child(repeat_box)
        self._btn_repeat.connect("clicked", lambda *_: self._cycle_repeat())
        ctrl.append(self._btn_repeat)

        # 音量放最右
        ctrl.append(self._vol_btn)

        bottom.append(ctrl)

        # 进度条（独立组件：点击/拖动 seek）
        self._progress = SeekBar(on_seek=on_seek, on_drag=self._on_progress_drag)
        bottom.append(self._progress)

        time_box = Gtk.Box()
        self._time_left = Gtk.Label(label="0:00")
        self._time_left.add_css_class("np-secondary")
        self._time_right = Gtk.Label(label="0:00")
        self._time_right.add_css_class("np-secondary")
        time_box.append(self._time_left)
        time_box.append(Gtk.Box(hexpand=True))
        time_box.append(self._time_right)
        bottom.append(time_box)
        content.append(bottom)

        # 键盘：Esc 退出、空格播放/暂停。
        # 用 CAPTURE 阶段优先处理，避免空格触发获得焦点的按钮。
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    # ------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------
    def set_track(self, title: str, artist: str) -> None:
        self._track_label.set_text(title)
        self._artist_label.set_text(artist)

    def set_format_info(self, text: str) -> None:
        """显示音频技术信息（格式 · 采样率 · 位深 · 声道 · 码率）。"""
        try:
            self._format_label.set_text(text or "")
        except Exception:
            pass

    #: 封面显示尺寸与圆角（固定，避免随图片 natural size 变化导致布局跳动）
    _COVER_PX = 420
    _COVER_RADIUS = 28

    def set_cover(self, image_bytes: bytes | None) -> None:
        """设置封面（兼容旧调用：会在主线程做缩放+主色提取）。

        新代码应优先用 set_cover_ready：由后台线程预缩放并算好主色，
        避免主线程做图片缩放/主色提取导致切歌封面延迟。
        """
        if not image_bytes:
            self._cover.set_paintable(None)
            self._apply_bg_color(None)
            try:
                self._progress.set_played_color(None)
            except Exception:
                pass
            return
        fixed = make_square_cover_bytes(
            image_bytes, self._COVER_PX, radius=self._COVER_RADIUS
        )
        raw = extract_dominant_color(image_bytes, lighten=0.0)
        bg = None
        seek = None
        if raw:
            r, g, b = raw
            bg = (int(r + (255 - r) * 0.72),
                  int(g + (255 - g) * 0.72),
                  int(b + (255 - b) * 0.72))
            f = 0.72
            seek = (r / 255.0 * f, g / 255.0 * f, b / 255.0 * f)
        self.set_cover_ready(fixed, bg, seek)

    def set_bg_colors(self, bg_rgb=None, seekbar_rgb=None) -> None:
        """只更新沉浸页背景色与进度条色（不动封面纹理）。"""
        self._apply_bg_color(bg_rgb)
        try:
            self._progress.set_played_color(seekbar_rgb)
        except Exception:
            pass

    def set_bg_texture(self, texture, dark=None) -> None:
        """设置背景层的模糊封面图（None 时清空，露出纯色背景）。

        纹理由后台线程用 make_blurred_bg 生成好，主线程只 set_paintable。
        dark: True/False 显式指定背景明暗（切换前景色）；None 时不动。
        """
        try:
            self._bg_picture.set_paintable(texture)
        except Exception:
            pass
        if texture is None:
            # 清空背景图时，纯色回退由 _apply_bg_color 决定明暗
            return
        if dark is not None:
            self._set_dark_bg(bool(dark))

    def set_cover_texture(self, texture, bg_rgb=None, seekbar_rgb=None) -> None:
        """直接设置已解码的 GdkTexture（后台线程建好），主线程零解码。

        bg_rgb / seekbar_rgb 为 None 时「不改动」现有背景色（避免主界面切歌
        误重置沉浸页已设好的颜色）；只有显式传入颜色才更新。
        """
        if texture is not None:
            try:
                self._cover.set_paintable(texture)
                self._cover.set_size_request(self._COVER_PX, self._COVER_PX)
                self._cover_stack.set_visible_child_name("cover")
            except Exception:
                pass
        else:
            # 无封面：切到占位页，避免沿用上一首封面
            try:
                self._cover.set_paintable(None)
                self._cover_stack.set_visible_child_name("placeholder")
            except Exception:
                pass
            # 无封面：清空背景模糊图，回退到纯色背景
            try:
                self._bg_picture.set_paintable(None)
            except Exception:
                pass
            # 无封面时强制重置背景为主题色，避免残留上一首封面的主色导致
            # 背景偏暗（bg_rgb/seekbar_rgb 都是 None 时下方 if 不会触发）。
            self._apply_bg_color(None)
            try:
                self._progress.set_played_color(None)
            except Exception:
                pass
            return
        # 进度条颜色总是设置：None 时回退默认黑白，避免残留上一首封面色。
        # （关闭背景功能时 bg_rgb/seekbar_rgb 都是 None，若跳过会残留封面色。）
        if bg_rgb is not None or seekbar_rgb is not None:
            self._apply_bg_color(bg_rgb)
        try:
            self._progress.set_played_color(seekbar_rgb)
        except Exception:
            pass

    def set_cover_ready(self, cover_png: bytes | None,
                        bg_rgb=None, seekbar_rgb=None) -> None:
        """主线程：只设纹理与「已算好」的背景色/进度条色（不做缩放/主色提取）。

        cover_png: 已按 _COVER_PX 缩放并带圆角的 PNG 字节。
        bg_rgb:    沉浸页背景色 (r,g,b) 或 None。
        seekbar_rgb: 进度条已播段颜色 (0..1) 或 None。
        """
        if not cover_png:
            self._cover.set_paintable(None)
            try:
                self._cover_stack.set_visible_child_name("placeholder")
            except Exception:
                pass
            self._apply_bg_color(None)
            try:
                self._progress.set_played_color(None)
            except Exception:
                pass
            return
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(cover_png))
        except Exception:
            self._cover.set_paintable(None)
            self._apply_bg_color(None)
            return
        self._cover.set_paintable(texture)
        self._cover.set_size_request(self._COVER_PX, self._COVER_PX)
        try:
            self._cover_stack.set_visible_child_name("cover")
        except Exception:
            pass
        self._apply_bg_color(bg_rgb)
        try:
            self._progress.set_played_color(seekbar_rgb)
        except Exception:
            pass

    def _apply_seekbar_color(self, rgb) -> None:
        """把已提取的封面主色设给进度条已播段（压暗，保证浅背景上有对比）。"""
        try:
            if not rgb:
                self._progress.set_played_color(None)
                return
            r, g, b = rgb
            f = 0.72
            self._progress.set_played_color((r / 255.0 * f, g / 255.0 * f, b / 255.0 * f))
        except Exception:
            try:
                self._progress.set_played_color(None)
            except Exception:
                pass

    def _apply_bg_color(self, color: tuple[int, int, int] | None) -> None:
        """用封面主色调设置全屏页背景色（纯色回退用）。

        color 为 None（无封面）时：背景回退到主题色 @window_bg_color，
        与主界面一致。渐隐层已改为透明（见 style.css），此处不再注入渐隐色。
        """
        if color:
            r, g, b = color
            bg_css = f"rgb({r}, {g}, {b})"
            self._set_dark_bg(self._is_dark(r, g, b))
        else:
            # 无封面：回退主题色，与主界面一致
            bg_css = "@window_bg_color"
            self._set_dark_bg(False)
        css = f".now-playing-root {{ background-color: {bg_css}; }}"
        try:
            self._bg_provider.load_from_data(css.encode("utf-8"))
            if not self._bg_provider_installed:
                display = Gdk.Display.get_default()
                if display is not None:
                    Gtk.StyleContext.add_provider_for_display(
                        display, self._bg_provider,
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                    )
                    self._bg_provider_installed = True
        except Exception:
            pass

    @staticmethod
    def _is_dark(r: int, g: int, b: int, threshold: float = 0.65) -> bool:
        """按感知亮度判断颜色是否偏暗（暗则前景需切亮色）。"""
        lum = (r * 299 + g * 587 + b * 114) / 255000.0
        return lum < threshold

    def _set_dark_bg(self, dark: bool) -> None:
        """切换沉浸页明暗前景：背景暗时给根节点加 np-dark-bg 类。

        进度条是自绘的（CSS 管不到），需单独通知它切白色系。
        """
        try:
            if dark:
                self.add_css_class("np-dark-bg")
            else:
                self.remove_css_class("np-dark-bg")
        except Exception:
            pass
        try:
            self._progress.set_dark(bool(dark))
        except Exception:
            pass

    def set_playing(self, playing: bool) -> None:
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        self._btn_play.set_icon_name(icon)

    # ---- 播放模式（与左侧面板同步）----
    def _toggle_shuffle(self) -> None:
        self.set_shuffle(not self._shuffle_on)
        if self._on_shuffle is not None:
            self._on_shuffle(self._shuffle_on)

    def _cycle_repeat(self) -> None:
        self.set_repeat_mode((self._repeat_mode + 1) % 3)
        if self._on_repeat is not None:
            self._on_repeat(self._repeat_mode)

    def set_shuffle(self, on: bool) -> None:
        self._shuffle_on = bool(on)
        # 用 Adwaita 内置的 suggested-action（和主界面随机按钮完全一致），
        # 自带激活态背景填充；自定义 np-toggle-on 会被 image-button 主题规则压住。
        if self._shuffle_on:
            self._btn_shuffle.add_css_class("suggested-action")
        else:
            self._btn_shuffle.remove_css_class("suggested-action")

    def set_repeat_mode(self, mode: int) -> None:
        self._repeat_mode = mode % 3
        icons = {
            0: "media-playlist-consecutive-symbolic",
            1: "media-playlist-repeat-symbolic",
            2: "media-playlist-repeat-symbolic",
        }
        self._repeat_icon.set_from_icon_name(icons[self._repeat_mode])
        self._repeat_num.set_text("1" if self._repeat_mode == 2 else "")
        tips = {0: "不循环", 1: "列表循环", 2: "单曲循环"}
        self._btn_repeat.set_tooltip_text(tips[self._repeat_mode])

    def set_volume(self, value: float) -> None:
        """外部同步音量（不触发回调）。"""
        self._vol_slider.handler_block_by_func(self._on_volume_changed)
        self._vol_slider.set_value(value)
        self._vol_slider.handler_unblock_by_func(self._on_volume_changed)

    def _on_volume_changed(self, scale) -> None:
        if self._on_volume is not None:
            self._on_volume(scale.get_value())

    def set_lyrics(self, lyrics: list[tuple[float, str]]) -> None:
        self.lyrics.set_lyrics(lyrics)

    def set_duration(self, seconds: float) -> None:
        self._progress.set_duration(seconds)
        self._time_right.set_text(_fmt(seconds))

    def reset_position(self) -> None:
        """切歌时重置进度显示，避免沿用上一首的位置或卡住 seeking。"""
        self._progress.reset()
        self._time_left.set_text("0:00")

    def set_position(self, seconds: float) -> None:
        self._progress.set_position(seconds)
        # SeekBar 冻结期间（拖动中 / seek 后短暂）不更新已播时间，
        # 否则会被实际播放位置覆盖，导致时间标签来回抖动。
        frozen = bool(getattr(self._progress, "_seeking", False))
        if not frozen:
            self._time_left.set_text(_fmt(seconds))
        self.lyrics.set_position(seconds)

    # ---- 进度条交互 ----
    def _on_progress_drag(self, value) -> None:
        """拖动进度条时同步左侧时间标签（seek 由 SeekBar 内部处理）。"""
        self._time_left.set_text(_fmt(value))

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._on_exit()
            return True
        if keyval == Gdk.KEY_space:
            # 空格键：播放 / 暂停
            if self._on_play_pause is not None:
                self._on_play_pause()
            return True
        return False

    # ---- 最大化 / 还原 ----
    def bind_window(self, window: Gtk.Window) -> None:
        self._window = window
        window.connect("notify::maximized", self._on_maximized_changed)
        self._sync_max_button()

    def _on_toggle_maximize(self, _btn) -> None:
        window = getattr(self, "_window", None)
        if window is None:
            return
        if window.is_maximized():
            window.unmaximize()
        else:
            window.maximize()

    def _on_maximized_changed(self, window, _pspec) -> None:
        self._sync_max_button()

    def _sync_max_button(self) -> None:
        window = getattr(self, "_window", None)
        maximized = bool(window and window.is_maximized())
        self._max_btn.set_icon_name(
            "window-restore-symbolic" if maximized else "window-maximize-symbolic"
        )
        self._max_btn.set_tooltip_text("还原" if maximized else "最大化")
