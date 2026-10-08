"""虚拟化卡片网格（Gtk.GridView）：只建可视区卡片，滚动复用。

用于在线页「更多」（section）网格：
- 数据：CardItem（name / subtitle / cover_url / click），存在 Gio.ListStore；
- 渲染：Gtk.GridView + Gtk.SignalListItemFactory，卡片 widget 复用；
- 封面：bind 时异步下载，unbind 时按 generation 丢弃过期回填，避免串图。

与旧的 FlowBox（一次性建全部卡片）不同：几百张也流畅。
样式沿用 .media-card 系列，与 online_section._make_card 视觉一致。
"""
from __future__ import annotations

import urllib.request
from typing import Callable, List, Optional

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk

from core.tasks import run_cover_async

#: 封面渲染尺寸（与 online_section.CARD_COVER_PX 一致）
CARD_COVER_PX = 150
#: 封面在卡片容器内的收进量（阴影留白）
_PAD = 14
_GAP = 10
#: 卡片容器宽度（比封面宽一圈，给阴影/浮起留空间）
_CARD_W = CARD_COVER_PX + _PAD * 2


class CardItem(GObject.Object):
    """网格里的一个卡片项（轻量模型，不是 TrackItem）。"""

    __gtype_name__ = "OnlineCardItem"

    def __init__(self, name: str = "", subtitle: str = "",
                 cover_url: str = "", click: Optional[Callable] = None) -> None:
        super().__init__()
        self.name = name
        self.subtitle = subtitle
        self.cover_url = cover_url
        self.click = click


def _load_url_cover_async(url: str, on_done: Callable) -> None:
    """后台下载 + 解码 + 缩放封面，回主线程回调 pixbuf 或 None。

    关键：解码/缩放（CPU 密集）也放在后台线程，主线程只做
    set_pixbuf。否则一批 48 张的解码 idle 回调会挤在主线程，
    追加时明显卡顿。
    """
    def _work():
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
        except Exception:
            return None
        if not raw:
            return None
        try:
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(raw)
            loader.close()
            pb = loader.get_pixbuf()
            if pb is None:
                return None
            w, h = pb.get_width(), pb.get_height()
            side = min(w, h)
            if w != h:
                pb = pb.new_subpixbuf((w - side) // 2, (h - side) // 2, side, side)
            # 原图已小于目标则不放大（省一次无谓缩放）
            if side > CARD_COVER_PX:
                pb = pb.scale_simple(CARD_COVER_PX, CARD_COVER_PX,
                                     GdkPixbuf.InterpType.BILINEAR)
            return pb
        except Exception:
            return None

    # 缓存键：与 song_list / player_panel 一致（"url::" 前缀，同一 LRU）。
    ckey = "url::" + url
    try:
        from .pages.common import cache_get, cache_put, MISS
    except Exception:
        cache_get = cache_put = None
        MISS = object()
    if cache_get is not None:
        cached = cache_get(ckey)
        if cached is not MISS:
            on_done(cached)
            return

    def _done(pb):
        if pb is None:
            try:
                cache_put(ckey, None)
            except Exception:
                pass
            on_done(None)
            return
        try:
            from .online_section import _pixbuf_to_texture
            tex = _pixbuf_to_texture(pb)
        except Exception:
            tex = None
        try:
            cache_put(ckey, tex)
        except Exception:
            pass
        on_done(tex)

    run_cover_async(work=_work, on_done=_done)


def _set_holder_child(holder: Gtk.Box, widget: Gtk.Widget) -> None:
    """清空 holder 并放入 widget。"""
    try:
        child = holder.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            holder.remove(child)
            child = nxt
    except Exception:
        pass
    try:
        widget.set_hexpand(True)
        widget.set_vexpand(True)
        holder.append(widget)
    except Exception:
        pass


def _make_placeholder() -> Gtk.Widget:
    ph = Gtk.Image.new_from_icon_name("media-optical-symbolic")
    ph.set_pixel_size(42)
    ph.set_halign(Gtk.Align.CENTER)
    ph.set_valign(Gtk.Align.CENTER)
    ph.set_vexpand(True)
    return ph


class OnlineCardGrid(Gtk.ScrolledWindow):
    """虚拟化卡片网格（自带滚动）。

    set_cards(list[dict]) / append_cards(list[dict]) 接口与旧 FlowBox 用法兼容：
    dict 字段 {name, subtitle, cover_url, click}。
    """

    def __init__(self) -> None:
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)

        self._store = Gio.ListStore.new(CardItem)
        self._grid = Gtk.GridView(model=Gtk.NoSelection(model=self._store))
        self._grid.set_valign(Gtk.Align.START)
        self._grid.set_halign(Gtk.Align.START)
        self._grid.set_min_columns(2)
        self._grid.set_max_columns(12)
        self._grid.set_hexpand(True)
        # 网格单元间距
        try:
            self._grid.set_row_spacing(8)
            self._grid.set_column_spacing(8)
        except Exception:
            pass
        # 卡片外围留白，贴近原 FlowBox 观感
        try:
            self._grid.set_margin_top(4)
            self._grid.set_margin_bottom(4)
            self._grid.set_margin_start(4)
            self._grid.set_margin_end(4)
        except Exception:
            pass

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", self._on_unbind)
        factory.connect("teardown", self._on_teardown)
        self._grid.set_factory(factory)
        self.set_child(self._grid)

        #: 接近底部回调（滚动自动加载）
        self._near_bottom_cb = None
        self._near_bottom_armed = True
        vadj = self.get_vadjustment()
        if vadj is not None:
            vadj.connect("value-changed", self._on_scroll_changed)

    # ---- factory ----
    def _on_setup(self, _factory, list_item) -> None:
        """建卡片骨架（不含封面图，bind 时再加载）。"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_size_request(_CARD_W, -1)
        box.add_css_class("media-card")
        box.set_valign(Gtk.Align.START)
        box.set_halign(Gtk.Align.CENTER)

        frame = Gtk.Frame()
        frame.add_css_class("media-card-cover")
        frame.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
        frame.set_halign(Gtk.Align.CENTER)
        frame.set_valign(Gtk.Align.START)
        frame.set_margin_start(_GAP)
        frame.set_margin_end(_GAP)
        frame.set_margin_top(6)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        holder.set_halign(Gtk.Align.FILL)
        holder.set_valign(Gtk.Align.FILL)
        holder.set_hexpand(True)
        holder.set_vexpand(True)
        holder.append(_make_placeholder())
        frame.set_child(holder)
        box.append(frame)

        name_lbl = Gtk.Label()
        name_lbl.set_ellipsize(3)
        name_lbl.set_max_width_chars(13)
        name_lbl.set_size_request(CARD_COVER_PX, -1)
        name_lbl.set_xalign(0.5)
        name_lbl.set_margin_start(_GAP)
        name_lbl.set_margin_end(_GAP)
        name_lbl.add_css_class("media-card-title")
        box.append(name_lbl)

        sub_lbl = Gtk.Label()
        sub_lbl.set_ellipsize(3)
        sub_lbl.set_max_width_chars(13)
        sub_lbl.set_size_request(CARD_COVER_PX, -1)
        sub_lbl.set_xalign(0.5)
        sub_lbl.set_margin_start(_GAP)
        sub_lbl.set_margin_end(_GAP)
        sub_lbl.add_css_class("media-card-subtitle")
        sub_lbl.set_visible(False)
        box.append(sub_lbl)

        # 点击：gesture 只连一次，读 box 上的 _click
        gesture = Gtk.GestureClick()
        gesture.connect("released", self._on_card_clicked)
        box.add_controller(gesture)
        box.set_cursor(Gdk.Cursor.new_from_name("pointer", None))

        box._holder = holder
        box._name_lbl = name_lbl
        box._sub_lbl = sub_lbl
        box._click = None
        box._gen = 0
        box._busy = False
        list_item.set_child(box)

    def _on_bind(self, _factory, list_item) -> None:
        item = list_item.get_item()
        box = list_item.get_child()
        if item is None or box is None:
            return
        box._gen = getattr(box, "_gen", 0) + 1
        gen = box._gen
        box._name_lbl.set_text(item.name or "")
        sub = item.subtitle or ""
        box._sub_lbl.set_text(sub)
        box._sub_lbl.set_visible(bool(sub))
        box._click = item.click
        # 先复位为占位，再异步加载封面（复用时避免显示上一张的图）
        _set_holder_child(box._holder, _make_placeholder())
        url = item.cover_url or ""
        if not url:
            return
        try:
            from .pages.common import cover_activity as _ca
            _ca.mark_busy()
            box._busy = True
        except Exception:
            _ca = None
        def _on_cover(tex, _box=box, _gen=gen):
            # 过期回填（已被复用/解绑）→ 丢弃
            if getattr(_box, "_gen", 0) != _gen:
                self._release_busy(_box)
                return
            try:
                if tex is not None:
                    pic = Gtk.Picture()
                    pic.set_paintable(tex)
                    pic.set_content_fit(Gtk.ContentFit.COVER)
                    pic.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
                    _set_holder_child(_box._holder, pic)
            except Exception:
                pass
            self._release_busy(_box)
        try:
            _load_url_cover_async(url, _on_cover)
        except Exception:
            self._release_busy(box)

    def _release_busy(self, box) -> None:
        """配对释放 cover_activity 计数（每个 bind 至多一次）。"""
        if not getattr(box, "_busy", False):
            return
        box._busy = False
        try:
            from .pages.common import cover_activity as _ca
            GLib.idle_add(lambda: (_ca.mark_idle(), False)[1])
        except Exception:
            pass

    def _on_unbind(self, _factory, list_item) -> None:
        box = list_item.get_child()
        if box is None:
            return
        # 令 pending 回填失效，并释放计数
        box._gen = getattr(box, "_gen", 0) + 1
        box._click = None
        self._release_busy(box)
        _set_holder_child(box._holder, _make_placeholder())

    def _on_teardown(self, _factory, list_item) -> None:
        box = list_item.get_child()
        if box is not None:
            box._click = None
        try:
            list_item.set_child(None)
        except Exception:
            pass

    def _on_card_clicked(self, _gesture, _n_press, _x, _y) -> None:
        box = _gesture.get_widget()
        click = getattr(box, "_click", None)
        if callable(click):
            try:
                click()
            except Exception:
                pass

    # ---- 滚动自动加载 ----
    def connect_near_bottom(self, callback: Callable) -> None:
        """注册「接近底部」回调（滚动到距底 < 400px 时触发一次）。

        回调方负责去重（如加载中/已耗尽时忽略）。
        """
        self._near_bottom_cb = callback

    def _on_scroll_changed(self, adj) -> None:
        if self._near_bottom_cb is None:
            return
        try:
            remaining = adj.get_upper() - adj.get_page_size() - adj.get_value()
        except Exception:
            return
        if remaining <= 400:
            if self._near_bottom_armed:
                self._near_bottom_armed = False
                try:
                    self._near_bottom_cb()
                except Exception:
                    pass
        else:
            # 离开底部区域 → 重新武装，允许下次再触发
            self._near_bottom_armed = True

    def arm_near_bottom(self) -> None:
        """允许下一次接近底部再次触发（数据追加后调用）。"""
        self._near_bottom_armed = True

    # ---- 数据接口 ----
    def set_cards(self, cards: List[dict]) -> None:
        """整体替换（offset=0）。"""
        self._store.remove_all()
        self.append_cards(cards)

    def append_cards(self, cards: List[dict]) -> None:
        """追加一批。"""
        for c in (cards or []):
            if not isinstance(c, dict):
                continue
            self._store.append(CardItem(
                name=str(c.get("name", "") or ""),
                subtitle=str(c.get("subtitle", "") or ""),
                cover_url=str(c.get("cover_url", "") or ""),
                click=c.get("click"),
            ))

    def clear(self) -> None:
        self._store.remove_all()

    def count(self) -> int:
        return int(self._store.get_n_items())
