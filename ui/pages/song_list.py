"""歌曲列表：ColumnView 构建、单元格工厂、封面懒加载、右键菜单。

列表只消费 TrackItem，不接触 Provider 内部。
"""
from __future__ import annotations

import weakref

from gi.repository import Gdk, GLib, Gio, Gtk

from core.i18n import _
from core.tasks import run_cover_async
from ui.widgets.playing_indicator import PlayingIndicator

from .common import (
    COVER_SIZE,
    INDICATOR_SIZE,
    MISS,
    cache_get,
    cache_put,
    cover_activity,
    cover_loading_map,
    load_cover_bytes as _load_cover_bytes,
    make_sort_func,
)


# ================================================================
# 右键菜单
# ================================================================
def _build_track_menu(column_view: Gtk.ColumnView, actions: dict):
    """构建歌曲右键菜单模型与动作，返回 (menu_model, ctx)。

    ctx 用于在右键时记录当前曲目，菜单动作作用于它。
    """
    # 菜单项列表：(label, action_key)。
    # 直接回调，不走 Gio 动作——ColumnView 单元格的 popover 无法可靠解析
    # 挂在其祖先上的 Gio 动作组（菜单项会灰显 / 点击无反应）。
    entries = []

    def _add(key, label):
        if callable(actions.get(key)):
            entries.append((label, key))

    _add("play", _("播放"))
    _add("play_next", _("下一首播放"))
    _add("append", _("添加到播放列表"))
    _add("add_to_playlist", _("添加到歌单…"))
    _add("like", _("喜欢 / 取消喜欢"))
    _add("copy", _("复制歌名"))
    _add("search_artist", _("查看歌手"))
    _add("info", _("查看歌曲信息"))
    _add("copy_path", _("复制文件路径"))
    _add("reveal", _("在文件管理器中显示"))
    _add("remove_from_list", _("从列表移除"))
    _add("delete_file", _("删除…"))

    state = {"track": None, "actions": actions, "entries": entries}
    return entries, state


def _attach_cell_right_click(_factory, list_item, menu_entries, ctx) -> None:
    """给单元格挂右键：在鼠标处弹出菜单（bind 阶段 child 已存在）。"""
    child = list_item.get_child()
    if child is None:
        return
    if getattr(child, "_rclick", None) is not None:
        return

    def on_right_click(gesture, n_press, x, y):
        item = list_item.get_item()
        if item is None:
            return
        ctx["track"] = item
        _popup_track_menu(child, x, y, ctx)

    g = Gtk.GestureClick()
    g.set_button(3)
    g.connect("pressed", on_right_click)
    child.add_controller(g)
    child._rclick = g


def _detach_cell_right_click(_factory, list_item) -> None:
    """unbind 时移除右键 controller。

    内存泄漏修复（实测：不移除 → 100/100 泄漏；移除 → 1/100）：
    add_controller(g) 让 child（cell 内容）持有 g；g 的 "pressed" 信号持有
    on_right_click 闭包，闭包又捕获 list_item。形成「child → g → 闭包 →
    list_item」的跨 C/Python 引用环——C 层不参与 Python gc，cell 无法回收。
    在 factory 的 unbind（cell 从列表解绑）时移除 controller，断开环。
    """
    child = list_item.get_child()
    if child is None:
        return
    g = getattr(child, "_rclick", None)
    if g is None:
        return
    try:
        child.remove_controller(g)
    except Exception:
        pass
    try:
        child._rclick = None
    except Exception:
        pass


def _on_cover_unbind(factory, list_item) -> None:
    """unbind：清理封面加载表里本 cell 的登记，并断开右键 controller。

    治本（内存）：封面回填表 loading[ckey] 存 cell 强引用；若 cell 被回收/
    复用（unbind）时不清理，已回收的 cell 会永久滞留表中 → 无限累积。
    这里按 bind 时记下的 ckey 把自身从表中移除。
    """
    try:
        ckey = getattr(list_item, "_cover_loading_ckey", None)
        if ckey:
            loading = cover_loading_map()
            lst = loading.get(ckey)
            if lst:
                new = [t for t in lst if t[0] is not list_item]
                if new:
                    loading[ckey] = new
                else:
                    loading.pop(ckey, None)
            list_item._cover_loading_ckey = None
    except Exception:
        pass
    _detach_cell_right_click(factory, list_item)


def _popup_track_menu(parent, x, y, ctx) -> None:
    """在 (x,y) 处弹出曲目菜单。

    用 Gtk.Popover + 按钮直接连回调，不经 Gio 动作解析——ColumnView 单元格
    的 popover 无法可靠解析挂在其祖先上的 Gio 动作组（菜单项点击无反应）。
    """
    entries = ctx.get("entries") or []
    actions = ctx.get("actions") or {}
    pop = Gtk.Popover()
    pop.add_css_class("media-menu")
    pop.set_parent(parent)
    pop.set_has_arrow(False)
    pop.set_halign(Gtk.Align.START)
    rect = Gdk.Rectangle()
    rect.x = int(x)
    rect.y = int(y)
    rect.width = 1
    rect.height = 1
    pop.set_pointing_to(rect)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)
    for label, key in entries:
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.set_halign(Gtk.Align.FILL)
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        btn.set_child(lbl)

        def _cb(_b, _k=key):
            pop.popdown()
            fn = actions.get(_k)
            if callable(fn) and ctx.get("track"):
                fn(ctx["track"])

        btn.connect("clicked", _cb)
        box.append(btn)
    pop.set_child(box)
    # 关闭即解父：临时 popover 若不 unparent，父控件（列表行）销毁时
    # GTK 会访问已释放的 popover → SIGSEGV in gtk_widget_unparent()。
    pop.connect("closed", lambda p: p.unparent())
    pop.popup()


# ================================================================
# 单元格工厂
# ================================================================
def _on_factory_setup(_factory, list_item) -> None:
    label = Gtk.Label(xalign=0)
    label.set_ellipsize(3)
    label.set_margin_start(8)
    label.set_margin_end(8)
    label.set_vexpand(True)
    label.set_valign(Gtk.Align.FILL)
    list_item.set_child(label)


def _on_factory_bind(_factory, list_item, prop: str) -> None:
    item = list_item.get_item()
    label = list_item.get_child()
    label.set_text(getattr(item, prop, ""))


def _on_cover_setup(_factory, list_item) -> None:
    pic = Gtk.Picture()
    pic.set_content_fit(Gtk.ContentFit.COVER)
    pic.set_size_request(COVER_SIZE, COVER_SIZE)
    pic.set_can_shrink(True)
    pic.add_css_class("cover-img")
    # 无内嵌封面时露出这层圆角底色（配合中央音符占位图标）
    pic.add_css_class("track-cover")
    pic.set_vexpand(True)
    pic.set_valign(Gtk.Align.FILL)

    # 用 Overlay 在封面右下角叠加音质徽标（默认隐藏，bind 时按需显示）
    overlay = Gtk.Overlay()
    overlay.set_size_request(COVER_SIZE, COVER_SIZE)
    overlay.set_margin_start(8)
    overlay.set_margin_top(4)
    overlay.set_margin_bottom(4)
    overlay.set_vexpand(True)
    overlay.set_valign(Gtk.Align.FILL)
    overlay.set_child(pic)
    badge = Gtk.Label(label="HR")
    badge.add_css_class("hires-badge")
    badge.set_halign(Gtk.Align.END)
    badge.set_valign(Gtk.Align.END)
    badge.set_visible(False)
    badge.set_tooltip_text(_("Hi-Res 高解析音频"))
    overlay.add_overlay(badge)
    # 无封面时的占位音符（居中，默认显示，bind 时按需隐藏）
    ph_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
    ph_icon.set_pixel_size(18)
    ph_icon.add_css_class("track-cover-placeholder")
    ph_icon.set_halign(Gtk.Align.CENTER)
    ph_icon.set_valign(Gtk.Align.CENTER)
    overlay.add_overlay(ph_icon)

    # 封面 cell = 水平 Box：[跳动音符(默认隐藏)] [封面 Overlay]。
    # 只有「正在播放」那一行会显示出左侧跳动音符，其他行封面直接在最前。
    ind = PlayingIndicator(width=INDICATOR_SIZE, height=INDICATOR_SIZE)
    ind.set_valign(Gtk.Align.CENTER)
    ind.set_margin_end(8)
    ind.set_visible(False)

    cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    cell.set_valign(Gtk.Align.CENTER)
    cell.append(ind)
    cell.append(overlay)
    list_item.set_child(cell)
    list_item._cover_cell = cell
    list_item._cover_indicator = ind
    list_item._cover_overlay = overlay
    list_item._cover_badge = badge
    list_item._cover_ph = ph_icon


def _on_cover_bind(_factory, list_item) -> None:
    item = list_item.get_item()
    # 封面 cell 现在是 Box[指示器 + Overlay]，用存的属性取 overlay。
    overlay = getattr(list_item, "_cover_overlay", None)
    if overlay is None:
        child = list_item.get_child()
        if isinstance(child, Gtk.Overlay):
            overlay = child
    pic = overlay.get_child() if isinstance(overlay, Gtk.Overlay) else None
    if pic is None:
        return
    pic.set_paintable(None)
    ph = getattr(list_item, "_cover_ph", None)
    if ph is not None:
        ph.set_visible(True)
    # 跳动音符：按当前行键决定是否在本行封面左侧显示（切歌时自动刷新）。
    ind = getattr(list_item, "_cover_indicator", None)
    if ind is not None:
        row_key = ""
        if item is not None:
            row_key = (getattr(item, "filepath", "") or getattr(item, "source_id", "")
                       or getattr(item, "title", ""))
        ind.set_row_key(row_key)
        try:
            ind.sync_now()
        except Exception:
            pass

    # 音质规格徽标（DSD / DXD / HR / CD + 多声道）：统一由公共函数处理
    try:
        badge = getattr(list_item, "_cover_badge", None)
        if badge is not None:
            from ui.pages.common import apply_quality_badge
            apply_quality_badge(badge, item)
    except Exception:
        pass

    if item is None:
        return

    # 决定封面来源：本地按 filepath 读内嵌封面；在线按 cover_url 下载
    filepath = getattr(item, "filepath", "")
    cover_url = getattr(item, "cover_url", "")
    if not filepath and not cover_url:
        return
    # 缓存键：本地用 filepath，在线用 "url::<cover_url>"（避免与本地键冲突）
    ckey = filepath if filepath else ("url::" + cover_url)
    cached = cache_get(ckey)
    if cached is not MISS:
        pic.set_paintable(cached)
        if ph is not None:
            ph.set_visible(cached is None)
        return
    loading = cover_loading_map()
    # 治本（内存 + 正确性）：
    #  - 回填表存 **强引用**：PyGObject 的 Python 包装会被 GC（底层 C widget
    #    仍在），若存弱引用，异步完成时取到 None → 封面永不回填（表现为
    #    「封面不显示 + 反复触发加载而变慢」）。故必须强引用才能可靠回填。
    #  - 表不无限累积：在 factory 的 unbind 里按 list_item._cover_loading_ckey
    #    把已回收/复用的 cell 从表中移除（见 _on_cover_unbind）。
    #  - on_done 闭包**不捕获任何 cell**（只捕获 ckey/loading 等），故异步任务
    #    不会钉住 cell；回填靠遍历表里的等待者完成。
    first = ckey not in loading
    loading.setdefault(ckey, [])
    loading[ckey].append((list_item, pic))
    list_item._cover_loading_ckey = ckey
    if not first:
        # 已有同 key 的加载在进行，登记等待即可，不重复起任务。
        return
    cover_activity.mark_busy()

    def _item_matches(t) -> bool:
        if t is None:
            return False
        if filepath:
            return getattr(t, "filepath", "") == filepath
        return getattr(t, "cover_url", "") == cover_url

    def on_done(png_bytes) -> None:
        waiters = loading.pop(ckey, [])
        cover_activity.mark_idle()
        tex = None
        if png_bytes:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_bytes))
            except Exception:
                tex = None
        cache_put(ckey, tex)
        # 回填所有等待同一封面的列表项（曲库/历史/歌单可能同屏）。
        # 逐个校验其当前绑定项仍匹配（复用的 cell 可能已换曲目）→ 不匹配则跳过。
        for w_item, w_pic in waiters:
            try:
                wc = w_item.get_item()
                if _item_matches(wc):
                    w_pic.set_paintable(tex)
                    _wph = getattr(w_item, "_cover_ph", None)
                    if _wph is not None:
                        _wph.set_visible(tex is None)
            except Exception:
                pass

    # 封面一律走封面专用高并发池（run_cover_async）：避免与在线流代理等
    # 长任务共用全局池（仅 4 线程）而排队，导致封面延迟数十秒才显示。
    if filepath:
        run_cover_async(work=lambda: _load_cover_bytes(filepath), on_done=on_done)
    else:
        from .common import load_cover_from_url as _load_url_cover
        run_cover_async(work=lambda: _load_url_cover(cover_url), on_done=on_done)


# ================================================================
# ColumnView 构建（统一入口）
# ================================================================
def build_track_columnview(model, on_activate=None,
                           track_actions=None,
                           on_row_click=None,
                           now_playing_ref=None,
                           hide_header=False,
                           no_sort=False,
                           compact_cols=None,
                           embedded=False) -> Gtk.ScrolledWindow:
    """构建歌曲列表 ScrolledWindow（内含 ColumnView）。

    model 可为 Gio.ListStore 或 Gtk.SelectionModel。
    track_actions: 可选 dict，提供歌曲右键菜单动作。
    on_row_click: 可选回调，单击某行时以该行 TrackItem 调用。
    now_playing_ref: 可选 dict（如 {"key": "..."}），记录当前播放曲目键。
    hide_header: 隐藏列头（精简列表模式）。
    no_sort: 禁用列头排序。
    compact_cols: 指定显示的文本列，如 [("歌名","title",True,"title"), ...]。
    """
    if isinstance(model, Gio.ListStore):
        model = Gtk.SingleSelection(model=Gtk.SortListModel(model=model))
    column_view = Gtk.ColumnView(model=model)
    column_view.set_vexpand(not embedded)
    # 列头点击排序：把 ColumnViewSorter 同步给底层 SortListModel
    try:
        _sel_model = model
        _sort_model = _sel_model.get_model() if hasattr(_sel_model, "get_model") else None
        if isinstance(_sort_model, Gtk.SortListModel):
            _cv_sorter = column_view.get_sorter()
            # 内存泄漏修复（关键）：_sync_sorter 若直接引用 column_view，会形成
            #   column_view → sorter → "changed"信号 → _sync_sorter闭包 → column_view
            # 的跨 C/Python 引用环。C 层不参与 Python gc，column_view 永不释放，
            # 其内部 ColumnView 的 cell 复用池（每次 splice 建的一批 ColumnViewCell）
            # 也随之永不回收——表现为「反复切列表/切专辑 → cell 无限累积 → 内存持续涨」。
            # 用 weakref 引用 column_view 断环：信号仍能回调，但不再强持有视图。
            _cv_ref = weakref.ref(column_view)

            def _sync_sorter(*_a):
                cv = _cv_ref()
                if cv is None:
                    return
                # 1) 清除选中：否则排序后 GTK 会把「选中行」滚到可见区
                try:
                    if hasattr(model, "set_selected"):
                        model.set_selected(Gtk.INVALID_LIST_POSITION)
                except Exception:
                    pass
                # 2) 应用新排序
                try:
                    _sort_model.set_sorter(cv.get_sorter())
                except Exception:
                    pass

            if _cv_sorter is not None:
                _cv_sorter.connect("changed", _sync_sorter)
    except Exception:
        pass
    # 单击即触发 activate
    try:
        column_view.set_single_click_activate(True)
    except Exception:
        pass
    column_view.add_css_class("track-list")
    # 整行单击：用 ColumnView 自带的 activate（整行级、命中准确）
    if on_row_click is not None:
        def _on_row_activate(_cv, position):
            try:
                item = model.get_item(position)
            except Exception:
                item = None
            if item is not None:
                on_row_click(item)
        column_view.connect("activate", _on_row_activate)

    # 歌曲右键菜单
    menu_model = None
    ctx = None
    if track_actions:
        menu_model, ctx = _build_track_menu(column_view, track_actions)

    # 封面列（最前，懒加载 + 缓存）
    cover_factory = Gtk.SignalListItemFactory()

    def _cover_setup_cb(_f, list_item):
        _on_cover_setup(_f, list_item)
        if now_playing_ref is not None:
            list_item._now_playing_ref = now_playing_ref

    cover_factory.connect("setup", _cover_setup_cb)
    cover_factory.connect("bind", _on_cover_bind)
    cover_factory.connect("unbind", _on_cover_unbind)
    if menu_model is not None:
        cover_factory.connect("bind", _attach_cell_right_click, menu_model, ctx)
    cover_col = Gtk.ColumnViewColumn(title="", factory=cover_factory)
    # 封面列变宽：左侧要容纳「正在播放」跳动音符（约 INDICATOR_SIZE + 间距）。
    cover_col.set_fixed_width(COVER_SIZE + INDICATOR_SIZE + 24)

    def _append_text_column(title, prop, expand, sort_key, numeric=False, width=0, sortable=True):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", _on_factory_setup)
        factory.connect("bind", _on_factory_bind, prop)
        factory.connect("unbind", _on_cover_unbind)
        if menu_model is not None:
            factory.connect("bind", _attach_cell_right_click, menu_model, ctx)
        col = Gtk.ColumnViewColumn(title=title, factory=factory)
        col.set_expand(expand)
        if width > 0:
            col.set_fixed_width(width)
        if sortable:
            try:
                col.set_sorter(Gtk.CustomSorter.new(make_sort_func(sort_key), None))
            except Exception:
                pass
        column_view.append_column(col)

    _sort_on = not no_sort

    # 封面列（首列）。跳动音符已内嵌进封面 cell，仅当前播放行显示，
    # 所以不再需要单独的指示器列。
    column_view.append_column(cover_col)

    if compact_cols is not None:
        for (title, prop, expand, sort_key) in compact_cols:
            _append_text_column(title, prop, expand, sort_key, sortable=False)
    else:
        _append_text_column(_("歌名"), "title", True, "title", sortable=_sort_on)
        _append_text_column(_("歌手"), "artist", False, "artist", sortable=_sort_on)
        _append_text_column(_("格式"), "format_ext", False, "format_ext", width=90, sortable=_sort_on)
        _append_text_column(_("采样率"), "sample_rate_label", False, "sample_rate", numeric=True, width=100, sortable=_sort_on)

    if hide_header:
        try:
            column_view.add_css_class("no-header")
        except Exception:
            pass

    scroll = Gtk.ScrolledWindow()
    scroll.set_child(column_view)
    if embedded:
        scroll.set_vexpand(False)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
    else:
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    # 供外部（定位当前播放按钮）拿到 ColumnView 以滚动
    scroll._column_view = column_view
    return scroll
