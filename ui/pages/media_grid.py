"""专辑/艺术家网格卡片构建。

从原 ui/pages.py 抽出：卡片是纯构建逻辑（异步封面 + 两页 Stack 轮播），
不依赖页面其余状态，只需外部注入 on_enter_group 回调。
"""
from __future__ import annotations

from gi.repository import Gdk, GLib, Gtk

from core.i18n import _
from core.tasks import run_async
from models import TrackItem, extract_cover, make_square_cover_bytes

from .common import MISS, cache_get, cache_put


#: 网格卡片封面尺寸（像素）
CARD_COVER_PX = 150


def load_grid_cover_async(path: str, frame, pic, cache_key: str = None) -> None:
    """后台读大封面（150px），读完在主线程填进 frame。

    cache_key 为空时用 f"{path}@150"（与列表小封面区分开，否则会糊）。
    """
    real_path = path
    if path and path.endswith("@150"):
        real_path = path[:-4]
    ckey = cache_key or f"{real_path}@{CARD_COVER_PX}"

    def _work():
        try:
            raw = extract_cover(real_path)
            if not raw:
                return None
            return make_square_cover_bytes(raw, CARD_COVER_PX, radius=10)
        except Exception:
            return None

    def _done(png):
        if not png:
            return
        try:
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
            cache_put(ckey, tex)
            if frame.get_child() is not pic:
                pic.set_paintable(tex)
                frame.set_child(pic)
        except Exception:
            pass

    try:
        run_async(work=_work, on_done=_done)
    except Exception:
        pass


def make_group_card(name: str, items, on_enter_group) -> Gtk.Widget:
    """专辑/艺术家卡片：封面 + 名称 + 曲目数。固定尺寸，不随窗口拉伸。

    on_enter_group(group_name) 在点击卡片时调用。
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_size_request(CARD_COVER_PX, -1)
    box.set_valign(Gtk.Align.START)
    box.set_halign(Gtk.Align.START)
    try:
        box.set_hexpand(False)
        box.set_vexpand(False)
    except Exception:
        pass

    # 封面：取该组第一首的封面
    cover_frame = Gtk.Frame()
    cover_frame.add_css_class("media-card-cover")
    cover_frame.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
    cover_frame.set_halign(Gtk.Align.START)
    try:
        cover_frame.set_hexpand(False)
    except Exception:
        pass
    first = items[0]
    pic = Gtk.Picture()
    pic.set_content_fit(Gtk.ContentFit.COVER)
    pic.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
    _path = getattr(first, "filepath", "") or ""
    _cached = cache_get(f"{_path}@{CARD_COVER_PX}") if _path else MISS
    if _cached is not MISS and _cached is not None:
        pic.set_paintable(_cached)
        cover_frame.set_child(pic)
    else:
        ph = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        ph.set_pixel_size(48)
        ph.add_css_class("media-card-placeholder")
        cover_frame.set_child(ph)
        if _path:
            load_grid_cover_async(f"{_path}@{CARD_COVER_PX}", cover_frame, pic)

    # 两页 Stack：换内容时旧页滑出、新页滑入（无缝）
    stack = Gtk.Stack()
    stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
    stack.set_transition_duration(600)
    # 非均匀：只测量「可见」页。
    # 默认 True 时每张卡片每次布局都要量两页；首页 FlowBox 必须测量全部子项
    # 才能分行，几百张卡片就是上千棵子树，窗口 resize（最大化/拖拽）会明显变慢。
    try:
        stack.set_hhomogeneous(False)
        stack.set_vhomogeneous(False)
    except Exception:
        pass

    inner_a = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    inner_a.append(cover_frame)
    lbl = Gtk.Label(label=name)
    lbl.set_ellipsize(3)
    lbl.set_max_width_chars(18)
    lbl.add_css_class("media-card-title")
    inner_a.append(lbl)
    sub = Gtk.Label(label=f"{len(items)} {_('首')}")
    sub.add_css_class("media-card-subtitle")
    inner_a.append(sub)

    stack.add_named(inner_a, "a")
    stack.set_visible_child_name("a")
    box.append(stack)
    box._stack = stack
    box._group_name = name
    box._items = list(items)
    box._pages = {
        "a": {"cover": cover_frame, "pic": pic, "name": lbl, "sub": sub},
    }
    box._cur_page = "a"
    #: 轮播用的第二页是否已构建。轮播只调度前 12 张卡片，
    #: 其余卡片不该白建一套永不显示的第二页控件。
    box._second_built = False

    def _ensure_page_b() -> None:
        """懒构建轮播用的第二页（只有需要轮播的卡片才会走到这里）。"""
        if box._second_built:
            return
        box._second_built = True
        inner_b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        pic_b = Gtk.Picture()
        pic_b.set_content_fit(Gtk.ContentFit.COVER)
        pic_b.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
        cover_b = Gtk.Frame()
        cover_b.add_css_class("media-card-cover")
        cover_b.set_size_request(CARD_COVER_PX, CARD_COVER_PX)
        cover_b.set_halign(Gtk.Align.START)
        try:
            cover_b.set_hexpand(False)
        except Exception:
            pass
        ph_b = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        ph_b.set_pixel_size(48)
        ph_b.add_css_class("media-card-placeholder")
        cover_b.set_child(ph_b)
        inner_b.append(cover_b)
        lbl_b = Gtk.Label(label=name)
        lbl_b.set_ellipsize(3)
        lbl_b.set_max_width_chars(18)
        lbl_b.add_css_class("media-card-title")
        inner_b.append(lbl_b)
        sub_b = Gtk.Label(label=f"{len(items)} {_('首')}")
        sub_b.add_css_class("media-card-subtitle")
        inner_b.append(sub_b)
        stack.add_named(inner_b, "b")
        box._pages["b"] = {
            "cover": cover_b, "pic": pic_b, "name": lbl_b, "sub": sub_b,
        }

    click = Gtk.GestureClick()
    click.connect("released", lambda *_a, b=box: on_enter_group(getattr(b, "_group_name", "")))
    box.add_controller(click)
    box.set_cursor(Gdk.Cursor.new_from_name("pointer", None))

    def _fill_page(page_key: str, new_name: str, new_items) -> None:
        try:
            # 第二页按需构建（只有轮播会填充它）
            _ensure_page_b()
            page = box._pages.get(page_key)
            if page is None or not new_items:
                return
            page["name"].set_text(new_name)
            page["sub"].set_text(f"{len(new_items)} {_('首')}")
            first_t = new_items[0]
            path = getattr(first_t, "filepath", "") or ""
            try:
                ph = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
                ph.set_pixel_size(48)
                ph.add_css_class("media-card-placeholder")
                page["cover"].set_child(ph)
            except Exception:
                pass
            if path:
                _c = cache_get(f"{path}@{CARD_COVER_PX}")
                if _c is not MISS and _c is not None:
                    try:
                        page["pic"].set_paintable(_c)
                        page["cover"].set_child(page["pic"])
                    except Exception:
                        pass
                else:
                    load_grid_cover_async(f"{path}@{CARD_COVER_PX}", page["cover"], page["pic"])
        except Exception:
            pass

    def _apply_group(new_name: str, new_items) -> None:
        """换内容：填隐藏页，再切过去（随机方向滑动）。"""
        try:
            if not new_items:
                return
            box._group_name = new_name
            box._items = list(new_items)
            cur = getattr(box, "_cur_page", "a")
            other = "b" if cur == "a" else "a"
            _fill_page(other, new_name, new_items)
            box._cur_page = other
            try:
                import random as _r
                trans = _r.choice([
                    Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                    Gtk.StackTransitionType.SLIDE_UP_DOWN,
                ])
                box._stack.set_transition_type(trans)
            except Exception:
                pass
            try:
                box._stack.set_visible_child_name(other)
            except Exception:
                pass
        except Exception:
            pass

    box._fill_page = _fill_page
    box._apply_group = _apply_group
    return box
