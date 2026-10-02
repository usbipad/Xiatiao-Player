"""主页「我的歌单」区块（在线音源）。

数据来源：SubsonicProvider.get_playlists_info() → PlaylistInfo 列表。
平台无关：后端接 QQ/汽水等，只要返回 PlaylistInfo 结构，本区块都能渲染。

无数据（未配置 / 连不上 / 空）时，整个区块隐藏。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from gi.repository import Gdk, GLib, Gtk

from core.i18n import _
from core.tasks import run_async

#: 卡片封面显示尺寸
CARD_COVER_PX = 160
#: 封面到容器边缘的距离（给 hover 浮起/阴影留空间）
_GAP = 12
#: 卡片容器宽度（封面 + 阴影留白）
_CARD_W = CARD_COVER_PX + _GAP * 2
#: 折叠时显示的行数
_COLLAPSED_ROWS = 1
#: 单行高度估算
_ROW_H = 200


def _load_url_cover_async(url: str, on_done) -> None:
    """后台下载封面图片，回主线程回调 (pixbuf_or_none)。"""
    import urllib.request

    def _work():
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except Exception:
            return None

    def _done(raw):
        if not raw:
            on_done(None)
            return
        try:
            from gi.repository import GdkPixbuf
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(raw)
            loader.close()
            pb = loader.get_pixbuf()
            if pb is None:
                on_done(None)
                return
            # 居中裁成正方形
            w, h = pb.get_width(), pb.get_height()
            side = min(w, h)
            x = (w - side) // 2
            y = (h - side) // 2
            if w != h:
                pb = pb.new_subpixbuf(x, y, side, side)
            # 缩放到封面尺寸
            pb = pb.scale_simple(
                CARD_COVER_PX, CARD_COVER_PX, GdkPixbuf.InterpType.BILINEAR)
            on_done(pb)
        except Exception:
            on_done(None)

    run_async(work=_work, on_done=_done)


def _make_playlist_card(pl, on_click: Optional[Callable] = None) -> Gtk.Widget:
    """构建单个歌单卡片（封面 + 名字 + 歌曲数）。"""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_size_request(_CARD_W, -1)
    box.add_css_class("media-card")
    box.set_valign(Gtk.Align.START)

    # 封面容器
    cover_frame = Gtk.Frame()
    cover_frame.add_css_class("media-card-cover")
    cover_frame.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
    cover_frame.set_halign(Gtk.Align.CENTER)
    cover_frame.set_valign(Gtk.Align.START)
    # 四周留白：给 hover 浮起 / 阴影扩散留空间
    cover_frame.set_margin_start(_GAP)
    cover_frame.set_margin_end(_GAP)
    cover_frame.set_margin_top(6)

    holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    # 占位图标
    ph = Gtk.Image.new_from_icon_name("media-optical-symbolic")
    ph.set_pixel_size(48)
    ph.set_halign(Gtk.Align.CENTER)
    ph.set_valign(Gtk.Align.CENTER)
    ph.set_vexpand(True)
    holder.append(ph)
    cover_frame.set_child(holder)
    box.append(cover_frame)

    # 名字
    name_lbl = Gtk.Label(label=pl.name)
    name_lbl.set_ellipsize(3)
    name_lbl.set_max_width_chars(14)
    name_lbl.set_size_request(CARD_COVER_PX, -1)
    name_lbl.set_margin_start(_GAP)
    name_lbl.set_margin_end(_GAP)
    name_lbl.add_css_class("media-card-title")
    box.append(name_lbl)

    # 歌曲数（有平台标签时附带）
    sub_text = f"{pl.song_count} {_('首')}" if pl.song_count else ""
    sub_lbl = Gtk.Label(label=sub_text)
    sub_lbl.set_size_request(CARD_COVER_PX, -1)
    sub_lbl.set_margin_start(_GAP)
    sub_lbl.set_margin_end(_GAP)
    sub_lbl.add_css_class("media-card-subtitle")
    box.append(sub_lbl)

    # 异步加载封面
    if pl.cover_url:
        try:
            from .pages.common import cover_activity as _ca
            _ca.mark_busy()
        except Exception:
            _ca = None

        def _on_cover(pb):
            try:
                if pb is not None:
                    pic = Gtk.Picture()
                    pic.set_pixbuf(pb)
                    pic.set_content_fit(Gtk.ContentFit.COVER)
                    pic.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
                    pic.set_hexpand(True)
                    pic.set_vexpand(True)
                    # 替换占位
                    child = holder.get_first_child()
                    while child is not None:
                        nxt = child.get_next_sibling()
                        holder.remove(child)
                        child = nxt
                    holder.append(pic)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("歌单封面渲染失败: %s", exc)
            # 设图后「下一帧」再 mark_idle，让图先渲染。
            try:
                if _ca is not None:
                    from gi.repository import GLib as _GLib
                    _GLib.idle_add(lambda: (_ca.mark_idle(), False)[1])
            except Exception:
                pass

        _load_url_cover_async(pl.cover_url, _on_cover)

    # 点击
    if on_click is not None:
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: on_click(pl))
        box.add_controller(click)
        box.set_cursor(Gdk.Cursor.new_from_name("pointer", None))

    return box


class OnlinePlaylistSection(Gtk.Box):
    """主页「我的歌单」区块：标题 + 可折叠歌单网格。

    set_playlists(playlists)：有数据 → 显示；空 → 隐藏整个区块。
    """

    def __init__(self, on_playlist_click: Optional[Callable] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._on_click = on_playlist_click
        self._expanded = False

        # 标题行
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._title_lbl = Gtk.Label(label=_("我的歌单"))
        self._title_lbl.add_css_class("heading")
        self._title_lbl.set_halign(Gtk.Align.START)
        header.append(self._title_lbl)
        self._toggle_btn = Gtk.Button()
        self._toggle_btn.add_css_class("flat")
        self._toggle_btn.set_valign(Gtk.Align.CENTER)
        self._toggle_btn.set_child(Gtk.Image.new_from_icon_name("pan-end-symbolic"))
        self._toggle_btn.connect("clicked", lambda *_: self._toggle())
        header.append(self._toggle_btn)
        self.append(header)

        # 内容：横向滚动的 FlowBox（一行）
        self._flow = Gtk.FlowBox()
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_halign(Gtk.Align.START)
        self._flow.set_row_spacing(12)
        self._flow.set_column_spacing(12)
        self._flow.set_max_children_per_line(9999)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self._scroll.set_child(self._flow)
        self._scroll.set_size_request(-1, _ROW_H)
        self.append(self._scroll)

        # 初始隐藏
        self.set_visible(False)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._scroll.set_size_request(-1, _ROW_H if self._expanded else 200)
        self._toggle_btn.set_child(Gtk.Image.new_from_icon_name(
            "pan-down-symbolic" if self._expanded else "pan-end-symbolic"))

    def set_playlists(self, playlists: List) -> None:
        """填充歌单；空则隐藏区块。"""
        # 清空旧卡片
        try:
            child = self._flow.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self._flow.remove(child)
                child = nxt
        except Exception:
            pass

        if not playlists:
            self.set_visible(False)
            return

        for pl in playlists:
            card = _make_playlist_card(pl, self._on_click)
            self._flow.append(card)
        self.set_visible(True)
