"""封面组件：显示内嵌封面，无封面时回退占位符号；可选右下角全屏按钮。

从 player_panel 抽出，外观与交互保持原样。
"""
from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, GLib, Gtk

from core.i18n import _
from models import make_square_cover_bytes


class CoverArt(Gtk.Box):
    """左侧面板的主封面。

    on_clicked 非空时，在封面右下角叠加"进入全屏"悬浮按钮。
    """

    def __init__(self, on_clicked: Callable[[], None] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._on_clicked = on_clicked

        # 封面固定尺寸与圆角（正方形）。固定边长避免随图片大小变化导致布局跳动。
        self._cover_size = 320
        self._cover_radius = 24
        # 供外部/后台预缩放用
        self.cover_size = self._cover_size
        self.cover_radius = self._cover_radius

        self._cover_stack = Gtk.Stack()
        # 自适应：不固定尺寸，由容器（AspectFrame）决定
        self._cover_stack.set_hexpand(True)
        self._cover_stack.set_vexpand(True)
        # 阴影/圆角加在封面图片上（正方形），而非外层矩形 AspectFrame
        self._cover_stack.add_css_class("cover-shadow")
        try:
            self._cover_stack.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:
            pass

        # Picture：填充裁剪（COVER），填满容器（消除上下白边）；尺寸随容器缩放
        self._cover_picture = Gtk.Picture()
        self._cover_picture.set_content_fit(Gtk.ContentFit.COVER)
        self._cover_picture.set_can_shrink(True)
        self._cover_picture.set_hexpand(True)
        self._cover_picture.set_vexpand(True)
        self._cover_picture.set_halign(Gtk.Align.FILL)
        self._cover_picture.set_valign(Gtk.Align.FILL)
        self._cover_picture.add_css_class("cover-img")

        # 占位封面：柔和渐变背景 + 居中音符图标（比单个 emoji 好看）
        self._cover_placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._cover_placeholder.add_css_class("cover-placeholder-box")
        self._cover_placeholder.set_halign(Gtk.Align.FILL)
        self._cover_placeholder.set_valign(Gtk.Align.FILL)
        _ph_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        try:
            _ph_icon.set_pixel_size(72)
        except Exception:
            pass
        _ph_icon.add_css_class("cover-placeholder-icon")
        _ph_icon.set_halign(Gtk.Align.CENTER)
        _ph_icon.set_valign(Gtk.Align.CENTER)
        _ph_icon.set_vexpand(True)
        self._cover_placeholder.append(_ph_icon)

        self._cover_stack.add_named(self._cover_placeholder, "placeholder")
        self._cover_stack.add_named(self._cover_picture, "cover")
        self._cover_stack.set_visible_child_name("placeholder")

        # 封面容器：固定正方形尺寸，居中，不随图片变化。
        # 用普通 Box 而非 Gtk.AspectFrame：AspectFrame 会画出 GTK 默认的方形
        # 边框/底色，在圆角阴影外形成"四方尖角框"。尺寸已由 size_request 固定，
        # 不需要 AspectFrame 的宽高比约束，Box 只承载阴影与裁剪即可。
        # 用 AspectFrame 约束为正方形：宽度跟随容器，高度=宽度（自适应缩放）
        cover_frame = Gtk.AspectFrame()
        cover_frame.set_ratio(1.0)
        cover_frame.set_obey_child(False)
        # 撑满：保证没播放时封面区也占满正方形（占位图标居中大）。
        # 阴影已加在封面图片上，AspectFrame 透明 → 不会露出白边。
        cover_frame.set_hexpand(True)
        cover_frame.set_valign(Gtk.Align.FILL)
        cover_frame.set_child(self._cover_stack)
        cover_frame.set_overflow(Gtk.Overflow.HIDDEN)

        # 用 Overlay 在封面右下角叠加"进入全屏"悬浮按钮
        if on_clicked is not None:
            overlay = Gtk.Overlay()
            overlay.set_halign(Gtk.Align.FILL)
            overlay.set_child(cover_frame)

            expand_btn = Gtk.Button(icon_name="pan-up-symbolic")
            expand_btn.add_css_class("circular")
            expand_btn.add_css_class("osd")
            expand_btn.set_tooltip_text(_("进入全屏播放"))
            expand_btn.set_halign(Gtk.Align.END)
            expand_btn.set_valign(Gtk.Align.END)
            expand_btn.set_margin_end(10)
            expand_btn.set_margin_bottom(10)
            expand_btn.connect("clicked", lambda *_: on_clicked())
            overlay.add_overlay(expand_btn)

            self.append(overlay)
        else:
            self.append(cover_frame)

    def set_cover_texture(self, texture) -> None:
        """直接设置已解码的 GdkTexture（主线程只 set_paintable，零解码）。

        纹理由后台线程建好，这样切歌封面和歌名一样即时出现。
        """
        if texture is None:
            # 无封面：保留当前显示（占位或旧图）由调用方决定；这里不动
            return
        try:
            self._cover_picture.set_paintable(texture)
            self._cover_stack.set_visible_child_name("cover")
        except Exception:
            pass

    def set_cover(self, image_bytes: bytes | None) -> None:
        """更新封面（兼容入口：接收 PNG 字节，主线程解码）。

        新代码应优先用 set_cover_texture（后台预解码）。
        """
        if not image_bytes:
            self._cover_stack.set_visible_child_name("placeholder")
            return
        try:
            gbytes = GLib.Bytes.new(image_bytes)
            texture = Gdk.Texture.new_from_bytes(gbytes)
            if texture.get_width() != self._cover_size or texture.get_height() != self._cover_size:
                fixed = make_square_cover_bytes(
                    image_bytes, self._cover_size, radius=self._cover_radius
                )
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(fixed))
            self._cover_picture.set_paintable(texture)
            self._cover_stack.set_visible_child_name("cover")
        except Exception:
            self._cover_stack.set_visible_child_name("placeholder")
