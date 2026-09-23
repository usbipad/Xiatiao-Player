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


#: 网格卡片封面的「渲染」尺寸（像素）。
#: 取显示上限：卡片在 CARD_COVER_MIN_PX..CARD_COVER_MAX_PX 间缩放时，
#: 纹理始终被降采样 → 任何窗口尺寸都清晰；若按当前显示尺寸渲染，
#: 放大后会糊，且每换一档都要把所有封面重新解码一遍。
CARD_COVER_PX = 184
#: 卡片封面显示尺寸区间（由 ui/pages/__init__.py 按可用宽度动态计算）
CARD_COVER_MIN_PX = 132
CARD_COVER_MAX_PX = 184

#: 封面在卡片容器内的额外收进量（像素）。
#:
#: 容器宽度固定（= CARD_COVER_PX + CARD_SHADOW_PAD * 2，决定网格列数）。
#: 把封面在容器内部再收进一点，可腾出更多空间给阴影扩散，
#: 而**不影响列数**——代价仅是封面视觉上略小。
CARD_COVER_INSET = 8

#: 封面实际显示尺寸 = 渲染尺寸 - 两侧收进。
#: 纹理仍按 CARD_COVER_PX 渲染（降采样到显示尺寸，更清晰）。
CARD_COVER_DISPLAY_PX = CARD_COVER_PX - CARD_COVER_INSET * 2

#: 卡片容器的额外留白（像素）。
#:
#: 阴影画在封面控件的外侧。若容器宽度恰好等于封面宽度，
#: 左右与上方的阴影会被容器边界裁掉，只有下方（名字标签处）可见，
#: 表现为「底部一条直边阴影、四周没有悬浮感」。
#:
#: 关键约束：阴影的有效扩散范围（offset_y + blur）必须 ≤ 实际可用留白。
#: 否则阴影会在容器边界处被硬裁切 —— 容器内边缘仍带阴影残余、
#: 容器外已是纯背景色，两张卡片之间就会出现一条深浅突变的条带。
CARD_SHADOW_PAD = 16

#: 封面边缘到容器边缘的实际距离 = 阴影可用的扩散空间。
#: 容器宽 = CARD_COVER_PX + CARD_SHADOW_PAD*2，
#: 封面显示 = CARD_COVER_PX - CARD_COVER_INSET*2，
#: 故两侧各留 (CARD_SHADOW_PAD + CARD_COVER_INSET)。
#: 当前 = 16 + 8 = 24px，对应 CSS 阴影 0 6px 18px（6+18=24）。
CARD_EDGE_GAP = CARD_SHADOW_PAD + CARD_COVER_INSET


def _title_max_chars(px: int) -> int:
    """卡片标题的最大字符数：随卡片宽度缩放。

    卡片放大后标题拥有更宽的可用宽度，不再两三个字就被省略号截断。
    """
    return max(8, int(px / 8.5))


def _make_cover_content() -> Gtk.Box:
    """构建封面内容容器（承载 Picture 或占位控件）。

    注：曾用 Gtk.Overlay 在其上再叠一层「浮雕内阴影」，
    但实测效果对深浅封面的适配不一致（浅色明显、深色几乎不可见），
    故暂时移除内阴影，只保留卡片外部由 .media-card-cover 提供的悬浮阴影。
    """
    holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    holder.set_halign(Gtk.Align.FILL)
    holder.set_valign(Gtk.Align.FILL)
    holder.set_hexpand(True)
    holder.set_vexpand(True)
    return holder


def _set_cover_content(holder: Gtk.Box, widget: Gtk.Widget) -> None:
    """把内容设进 holder（Gtk.Box 没有 set_child，需清空后 append）。

    轮播换页会反复调用，故必须先清空旧子控件，否则会叠加。
    """
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
        widget.set_halign(Gtk.Align.FILL)
        widget.set_valign(Gtk.Align.FILL)
        holder.append(widget)
    except Exception:
        pass


def _make_placeholder() -> Gtk.Widget:
    """构建封面占位控件（圆角容器 + 居中图标）。

    不能把 Gtk.Image 直接设为 Frame 的子控件：Frame 会画一圈矩形边框，
    图标很小时该边框暴露，border-radius 只圆外框、内边界仍是直角，
    导致阴影沿直角下垂（轮播切换后的卡片会明显露出该问题）。
    故统一用带圆角的容器撑满封面区。

    轮播的第一页、第二页、以及运行时换页都复用本函数，避免样式不一致。
    """
    ph_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    ph_box.set_hexpand(True)
    ph_box.set_vexpand(True)
    ph_box.set_halign(Gtk.Align.FILL)
    ph_box.set_valign(Gtk.Align.FILL)
    ph_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
    ph_icon.set_pixel_size(48)
    ph_icon.add_css_class("media-card-placeholder")
    ph_icon.set_halign(Gtk.Align.CENTER)
    ph_icon.set_valign(Gtk.Align.CENTER)
    ph_icon.set_vexpand(True)
    ph_box.append(ph_icon)
    return ph_box


def load_grid_cover_async(path: str, holder, pic, cache_key: str = None) -> None:
    """后台读封面，读完在主线程把 Picture 设进 holder。

    holder 是封面内容的承载容器（Gtk.Box，见 _make_cover_content）。
    cache_key 为空时用 f"{path}@{CARD_COVER_PX}"。
    """
    real_path = path
    if path and "@" in path:
        # 只剥掉形如 "@184" 的尺寸后缀；文件名本身含 @ 时不受影响。
        # 此前硬编码 endswith("@150")，渲染尺寸一变就剥不掉后缀 → 封面加载失败。
        head, _, tail = path.rpartition("@")
        if head and tail.isdigit():
            real_path = head
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
            pic.set_paintable(tex)
            _set_cover_content(holder, pic)
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
    # 容器比封面宽 2*PAD：左右各留出阴影扩散空间，
    # 否则阴影会被容器边界裁掉（只有底部有标签留白，故只底部可见）。
    box.set_size_request(CARD_COVER_PX + CARD_SHADOW_PAD * 2, -1)
    #: 当前显示尺寸（由 _apply_size 更新；懒构建的第二页据此取尺寸）
    box._card_px = CARD_COVER_PX
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
    # 用「显示尺寸」而非渲染尺寸：容器不变、封面内收，
    # 腾出的空间全部给阴影扩散（见 CARD_COVER_INSET 说明）。
    cover_frame.set_size_request(CARD_COVER_DISPLAY_PX, CARD_COVER_DISPLAY_PX)
    cover_frame.set_halign(Gtk.Align.START)
    # valign=START：不被父容器纵向拉伸。
    # 否则 Frame 会被拉成长方形，box-shadow 沿矩形边缘绘制，
    # 表现为「底部阴影是直直下来的一块」而非柔和弥散。
    cover_frame.set_valign(Gtk.Align.START)
    # 四周留白：让阴影在左右与上方也有扩散空间（见 CARD_EDGE_GAP 说明）
    cover_frame.set_margin_start(CARD_EDGE_GAP)
    cover_frame.set_margin_end(CARD_EDGE_GAP)
    cover_frame.set_margin_top(CARD_EDGE_GAP)
    try:
        cover_frame.set_hexpand(False)
        cover_frame.set_vexpand(False)
    except Exception:
        pass
    first = items[0]
    pic = Gtk.Picture()
    pic.set_content_fit(Gtk.ContentFit.COVER)
    pic.set_size_request(CARD_COVER_DISPLAY_PX, CARD_COVER_DISPLAY_PX)
    # 内容容器：承载 Picture 或占位控件
    cover_holder = _make_cover_content()
    cover_frame.set_child(cover_holder)
    _path = getattr(first, "filepath", "") or ""
    _cached = cache_get(f"{_path}@{CARD_COVER_PX}") if _path else MISS
    if _cached is not MISS and _cached is not None:
        pic.set_paintable(_cached)
        _set_cover_content(cover_holder, pic)
    else:
        _set_cover_content(cover_holder, _make_placeholder())
        if _path:
            load_grid_cover_async(f"{_path}@{CARD_COVER_PX}", cover_holder, pic)

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
    lbl.set_max_width_chars(_title_max_chars(CARD_COVER_PX))
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
        "a": {"cover": cover_holder, "pic": pic, "name": lbl, "sub": sub},
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
        dp = CARD_COVER_DISPLAY_PX
        pic_b = Gtk.Picture()
        pic_b.set_content_fit(Gtk.ContentFit.COVER)
        pic_b.set_size_request(dp, dp)
        cover_b = Gtk.Frame()
        cover_b.add_css_class("media-card-cover")
        cover_b.set_size_request(dp, dp)
        cover_b.set_halign(Gtk.Align.START)
        # 同第一页：防止被拉伸成长方形（见上面说明）
        cover_b.set_valign(Gtk.Align.START)
        # 同第一页：四周留白供阴影扩散，否则轮播切过来的卡片没有悬浮感
        cover_b.set_margin_start(CARD_EDGE_GAP)
        cover_b.set_margin_end(CARD_EDGE_GAP)
        cover_b.set_margin_top(CARD_EDGE_GAP)
        try:
            cover_b.set_hexpand(False)
            cover_b.set_vexpand(False)
        except Exception:
            pass
        holder_b = _make_cover_content()
        _set_cover_content(holder_b, _make_placeholder())
        cover_b.set_child(holder_b)
        inner_b.append(cover_b)
        lbl_b = Gtk.Label(label=name)
        lbl_b.set_ellipsize(3)
        lbl_b.set_max_width_chars(_title_max_chars(CARD_COVER_DISPLAY_PX))
        lbl_b.add_css_class("media-card-title")
        inner_b.append(lbl_b)
        sub_b = Gtk.Label(label=f"{len(items)} {_('首')}")
        sub_b.add_css_class("media-card-subtitle")
        inner_b.append(sub_b)
        stack.add_named(inner_b, "b")
        box._pages["b"] = {
            "cover": holder_b, "pic": pic_b, "name": lbl_b, "sub": sub_b,
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
                _set_cover_content(page["cover"], _make_placeholder())
            except Exception:
                pass
            if path:
                _c = cache_get(f"{path}@{CARD_COVER_PX}")
                if _c is not MISS and _c is not None:
                    try:
                        page["pic"].set_paintable(_c)
                        _set_cover_content(page["cover"], page["pic"])
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

    def _apply_size(px: int) -> None:
        """就地更新卡片尺寸（不重建卡片、不重新解码封面）。

        响应式只在「可用宽度跨档」时调用。重建整网格会让所有封面
        重走一遍解码，宽屏下拖动窗口会明显卡顿。
        """
        try:
            px = int(px)
        except Exception:
            return
        box._card_px = px
        try:
            box.set_size_request(px, -1)
        except Exception:
            pass
        chars = _title_max_chars(px)
        for page in getattr(box, "_pages", {}).values():
            for key in ("cover", "pic"):
                w = page.get(key)
                if w is None:
                    continue
                try:
                    w.set_size_request(px, px)
                except Exception:
                    pass
            lbl_p = page.get("name")
            if lbl_p is not None:
                try:
                    lbl_p.set_max_width_chars(chars)
                except Exception:
                    pass

    box._fill_page = _fill_page
    box._apply_group = _apply_group
    box._apply_size = _apply_size
    return box
