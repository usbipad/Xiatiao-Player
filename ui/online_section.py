"""在线页的通用「横滑卡片区块」组件。

- 标题行：标题 + 「更多」按钮（点→跳转该区块的完整列表页）；
- 内容：横向一排卡片（横滑看其余），不再折叠/展开。

数据源平台无关：每张卡片由 {name, cover_url, subtitle, click} 描述。
"""
from __future__ import annotations

from typing import List

from gi.repository import Gdk, Gtk

from core.i18n import _
from core.tasks import run_cover_async

#: 卡片封面尺寸
CARD_COVER_PX = 150
#: 封面纹理的最大边长。超过此值的图先缩放再建纹理，
#: 防止大封面（如 3000px）按原尺寸常驻内存。
MAX_COVER_DIM = 512
#: 容器四周阴影留白
_PAD = 14
#: 封面到容器边缘的距离
_GAP = 10
#: 卡片容器宽度（比封面宽一圈，给阴影/浮起留空间）
_CARD_W = CARD_COVER_PX + _PAD * 2
#: 一行高度
_ROW_H = 230


def _load_url_cover_async(url: str, on_done) -> None:
    """后台下载封面图，原始字节交给 GTK 解码建纹理。"""
    import urllib.request

    def _work():
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
        except Exception:
            return None
        if not raw:
            return None
        # 解码/缩放/裁剪/建纹理都在后台线程完成：
        # 若留到 on_done（主线程 idle），首屏几十张封面同时回来会堵死主循环。
        return _raw_to_texture(raw)

    # 缓存键：与 song_list / player_panel 保持一致（"url::" 前缀，进同一个 LRU）。
    ckey = "url::" + url
    try:
        from .pages.common import cache_get, cache_put, MISS
    except Exception:
        cache_get = cache_put = None
        MISS = object()
    if cache_get is not None:
        cached = cache_get(ckey)
        if cached is not MISS:
            on_done(cached)   # 命中：直接回填（Gdk.Texture 或 None）
            return

    def _done(tex):
        if not tex:
            try:
                cache_put(ckey, None)
            except Exception:
                pass
            on_done(None)
            return
        try:
            cache_put(ckey, tex)
        except Exception:
            pass
        on_done(tex)

    run_cover_async(work=_work, on_done=_done)


def _raw_to_texture(raw: bytes):
    """原始图片字节 -> 正方形 Gdk.Texture。

    居中裁剪成正方形（避免长图/宽图撑坏卡片布局），大于 MAX_COVER_DIM 的
    先缩放再裁剪（避免大封面常驻内存）。
    """
    from gi.repository import Gdk, GdkPixbuf, GLib, Gio

    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(raw))
    pb = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
    w, h = pb.get_width(), pb.get_height()
    if w <= 0 or h <= 0:
        return None

    # 先按最大边缩放（如果需要）
    if max(w, h) > MAX_COVER_DIM:
        scale = MAX_COVER_DIM / max(w, h)
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        pb = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
        w, h = pb.get_width(), pb.get_height()

    # 居中裁剪成正方形
    side = min(w, h)
    if w != h:
        pb = pb.new_subpixbuf((w - side) // 2, (h - side) // 2, side, side)

    ok, png = pb.save_to_bufferv("png", [], [])
    if not ok:
        return None
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(bytes(png)))


def _make_card(card: dict) -> Gtk.Widget:
    """构建单张卡片（封面 + 名称 + 副标题）。

    若卡片带 play_on_hover=True（实时流歌单，如每日推荐），
    封面中央叠一个常驻播放按钮，点击直接播放（等同点卡片）。
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_size_request(_CARD_W, -1)
    box.add_css_class("media-card")
    box.set_valign(Gtk.Align.START)

    frame = Gtk.Frame()
    frame.add_css_class("media-card-cover")
    frame.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
    frame.set_halign(Gtk.Align.CENTER)
    frame.set_valign(Gtk.Align.START)
    frame.set_margin_start(_GAP)
    frame.set_margin_end(_GAP)
    frame.set_margin_top(6)
    holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    ph = Gtk.Image.new_from_icon_name("media-optical-symbolic")
    ph.set_pixel_size(42)
    ph.set_halign(Gtk.Align.CENTER)
    ph.set_valign(Gtk.Align.CENTER)
    ph.set_vexpand(True)
    holder.append(ph)

    # 播放按钮放在 frame 内部（Overlay 包住 holder）：
    # frame 的 CSS transform:scale 会带着按钮一起缩放，视觉上「贴在封面上」。
    # 按钮常驻显示，不依赖悬停。
    click = card.get("click")
    if card.get("play_on_hover") and callable(click):
        cover_overlay = Gtk.Overlay()
        cover_overlay.set_child(holder)
        play_btn = Gtk.Button()
        play_btn.add_css_class("card-play-btn")
        play_btn.set_tooltip_text(_("播放"))
        _play_img = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        _play_img.set_pixel_size(22)
        play_btn.set_child(_play_img)
        play_btn.set_halign(Gtk.Align.CENTER)
        play_btn.set_valign(Gtk.Align.CENTER)
        play_btn.connect("clicked", lambda *_a: click())
        cover_overlay.add_overlay(play_btn)
        frame.set_child(cover_overlay)
    else:
        frame.set_child(holder)
    box.append(frame)

    name_lbl = Gtk.Label(label=str(card.get("name", "")))
    name_lbl.set_ellipsize(3)
    name_lbl.set_max_width_chars(13)
    name_lbl.set_size_request(CARD_COVER_PX, -1)
    name_lbl.set_xalign(0.5)
    name_lbl.set_margin_start(_GAP)
    name_lbl.set_margin_end(_GAP)
    name_lbl.add_css_class("media-card-title")
    box.append(name_lbl)

    sub = str(card.get("subtitle", "") or "")
    if sub:
        sub_lbl = Gtk.Label(label=sub)
        sub_lbl.set_ellipsize(3)
        sub_lbl.set_max_width_chars(13)
        sub_lbl.set_size_request(CARD_COVER_PX, -1)
        sub_lbl.set_margin_start(_GAP)
        sub_lbl.set_margin_end(_GAP)
        sub_lbl.add_css_class("media-card-subtitle")
        box.append(sub_lbl)

    url = card.get("cover_url", "") or ""
    if url:
        try:
            from .pages.common import cover_activity as _ca
            _ca.mark_busy()
        except Exception:
            _ca = None

        def _on_cover(tex):
            try:
                if tex is not None:
                    pic = Gtk.Picture()
                    # 用 set_paintable（纹理）：不再让 Picture 各持一份 GdkPixbuf。
                    pic.set_paintable(tex)
                    pic.set_content_fit(Gtk.ContentFit.COVER)
                    pic.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
                    # 锁死尺寸：不随容器拉伸，也不被纹理固有尺寸带偏。
                    # 之前 hexpand/vexpand=True 会让宽图把卡片撑成长方形。
                    pic.set_hexpand(False)
                    pic.set_vexpand(False)
                    pic.set_halign(Gtk.Align.CENTER)
                    pic.set_valign(Gtk.Align.CENTER)
                    child = holder.get_first_child()
                    while child is not None:
                        nxt = child.get_next_sibling()
                        holder.remove(child)
                        child = nxt
                    holder.append(pic)
            except Exception:
                pass
            # 关键：设图后「下一帧」再 mark_idle，让图先渲染，
            # 避免 show_content 切过去时还是占位。
            try:
                if _ca is not None:
                    from gi.repository import GLib as _GLib
                    _GLib.idle_add(lambda: (_ca.mark_idle(), False)[1])
            except Exception:
                pass
        _load_url_cover_async(url, _on_cover)

    if callable(click):
        g = Gtk.GestureClick()
        g.connect("released", lambda *_a: click())
        box.add_controller(g)
        box.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        # 存引用，供 set_cards 移除卡片时断开（内存泄漏修复）。
        # 环：box → g → lambda → click(页面方法) → 页面 → hbox → box，
        # C 层持有 g，不显式 remove_controller 则卡片整棵树永不回收
        # （与 media_grid.detach_card_click 同一模式）。
        box._click_ctrl = g

    return box


class CardSection(Gtk.Box):
    """标题行（标题 + 「更多」）+ 横向一排卡片。空数据时整体隐藏。"""

    def __init__(self, title: str, on_more=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._on_more = on_more

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label=_(title))
        lbl.add_css_class("heading")
        lbl.set_halign(Gtk.Align.START)
        header.append(lbl)
        if callable(on_more):
            more_btn = Gtk.Button(label=_("更多"))
            more_btn.add_css_class("flat")
            more_btn.set_valign(Gtk.Align.CENTER)
            more_btn.connect("clicked", lambda *_: self._fire_more())
            header.append(more_btn)
        self.append(header)

        self._hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._hbox.set_valign(Gtk.Align.START)
        # 内容比视口窄时，ScrolledWindow 默认会把子项居中 → 单个/少量卡片
        # 会「飘在中间」。强制 _hbox 靠左，卡片从左边排起。
        self._hbox.set_halign(Gtk.Align.START)
        self._h_scroll = Gtk.ScrolledWindow()
        self._h_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self._h_scroll.set_size_request(-1, _ROW_H)
        self._h_scroll.set_halign(Gtk.Align.FILL)
        self._h_scroll.set_child(self._hbox)
        self.append(self._h_scroll)
        self.set_visible(False)

    def _fire_more(self) -> None:
        if callable(self._on_more):
            try:
                self._on_more()
            except Exception:
                pass

    def set_cards(self, cards: List[dict]) -> None:
        """填充卡片；空则隐藏整个区块。"""
        try:
            child = self._hbox.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                # 移除前断开点击 controller（内存泄漏修复，见 _make_card）。
                _c = getattr(child, "_click_ctrl", None)
                if _c is not None:
                    try:
                        child.remove_controller(_c)
                    except Exception:
                        pass
                    try:
                        child._click_ctrl = None
                    except Exception:
                        pass
                self._hbox.remove(child)
                child = nxt
        except Exception:
            pass
        cards = list(cards or [])
        if not cards:
            self.set_visible(False)
            return
        for c in cards:
            if isinstance(c, dict):
                self._hbox.append(_make_card(c))
        self.set_visible(True)
