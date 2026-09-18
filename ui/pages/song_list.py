"""歌曲列表：ColumnView 构建、单元格工厂、封面懒加载、右键菜单。

从原 ui/pages.py 抽出。列表只消费 TrackItem，不接触 Provider 内部。
注：原代码中的在线音源封面分支（cover_url / cover_bytes / qq_api）
    已随在线音源功能移除而清理。
"""
from __future__ import annotations

from gi.repository import Gdk, GLib, Gio, Gtk

from core.i18n import _
from core.tasks import run_async
from models import TrackItem, extract_cover, make_square_cover_bytes
from ui.widgets.playing_indicator import PlayingIndicator

from .common import (
    COVER_SIZE,
    INDICATOR_SIZE,
    MISS,
    cache_get,
    cache_put,
    cover_activity,
    cover_loading_map,
    make_sort_func,
)


def _load_cover_bytes(filepath: str):
    """后台线程：读取并缩放封面，返回 PNG bytes 或 None（不碰 UI）。"""
    raw = extract_cover(filepath)
    if not raw:
        return None
    try:
        return make_square_cover_bytes(raw, COVER_SIZE, radius=6)
    except Exception:
        return None


# ================================================================
# 右键菜单
# ================================================================
def _build_track_menu(column_view: Gtk.ColumnView, actions: dict):
    """构建歌曲右键菜单模型与动作，返回 (menu_model, ctx)。

    ctx 用于在右键时记录当前曲目，菜单动作作用于它。
    """
    menu = Gio.Menu()
    menu.append(_("播放"), "row.play")
    menu.append(_("下一首播放"), "row.play_next")
    menu.append(_("添加到播放列表"), "row.append")
    if actions.get("add_to_playlist"):
        menu.append(_("添加到歌单…"), "row.add_to_playlist")
    if actions.get("like"):
        menu.append(_("喜欢 / 取消喜欢"), "row.like")
    menu.append(_("复制歌名"), "row.copy")
    if actions.get("search_artist"):
        menu.append(_("查看歌手"), "row.search_artist")
    menu.append(_("查看歌曲信息"), "row.info")
    menu.append(_("复制文件路径"), "row.copy_path")
    menu.append(_("在文件管理器中显示"), "row.reveal")
    if actions.get("remove_from_list"):
        menu.append(_("从列表移除"), "row.remove_from_list")
    if actions.get("delete_file"):
        menu.append(_("删除…"), "row.delete_file")

    state = {"track": None}
    column_view._ctx_state = state

    ag = Gio.SimpleActionGroup()

    def _mk(name, fn):
        if not callable(fn):
            return
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", lambda *_: fn(state["track"]) if state["track"] else None)
        ag.add_action(a)

    _mk("play", actions.get("play"))
    _mk("play_next", actions.get("play_next"))
    _mk("append", actions.get("append"))
    _mk("add_to_playlist", actions.get("add_to_playlist"))
    _mk("copy", actions.get("copy"))
    _mk("like", actions.get("like"))
    _mk("search_artist", actions.get("search_artist"))
    _mk("info", actions.get("info"))
    _mk("copy_path", actions.get("copy_path"))
    _mk("reveal", actions.get("reveal"))
    _mk("remove_from_list", actions.get("remove_from_list"))
    _mk("delete_file", actions.get("delete_file"))
    column_view.insert_action_group("row", ag)
    return menu, state


def _attach_cell_right_click(_factory, list_item, menu_model, ctx) -> None:
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
        popover = Gtk.PopoverMenu.new_from_model(menu_model)
        popover.set_parent(child)
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        popover.popup()

    g = Gtk.GestureClick()
    g.set_button(3)
    g.connect("pressed", on_right_click)
    child.add_controller(g)
    child._rclick = g


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
    list_item.set_child(overlay)
    list_item._cover_overlay = overlay
    list_item._cover_badge = badge


def _on_indicator_setup(_factory, list_item) -> None:
    """指示器列单元格：一个播放跳动指示器（默认隐藏）。"""
    indicator = PlayingIndicator(width=INDICATOR_SIZE, height=INDICATOR_SIZE)
    indicator.set_halign(Gtk.Align.CENTER)
    indicator.set_valign(Gtk.Align.CENTER)
    indicator.set_visible(False)
    list_item.set_child(indicator)
    list_item._indicator = indicator


def _on_indicator_bind(_factory, list_item) -> None:
    """指示器列：记录本行键，显隐由指示器自身每帧比对。"""
    item = list_item.get_item()
    indicator = getattr(list_item, "_indicator", None)
    if indicator is None:
        return
    row_key = ""
    if item is not None:
        row_key = (getattr(item, "filepath", "") or getattr(item, "source_id", "")
                   or getattr(item, "title", ""))
    indicator.set_row_key(row_key)
    try:
        indicator.sync_now()
    except Exception:
        pass


def _on_cover_bind(_factory, list_item) -> None:
    item = list_item.get_item()
    overlay = list_item.get_child()
    if isinstance(overlay, Gtk.Overlay):
        pic = overlay.get_child()
    else:
        pic = overlay
        overlay = None
    if pic is None:
        return
    pic.set_paintable(None)

    # 音质徽标（DSD / Hi-Res / CD）：按当前曲目音频规格显示/隐藏
    try:
        badge = getattr(list_item, "_cover_badge", None)
        if badge is not None:
            label = ""
            if item is not None:
                label = getattr(item, "quality_badge", "") or ""
            badge.set_text(label)
            badge.set_visible(bool(label))
            badge.remove_css_class("hires-badge")
            badge.remove_css_class("cd-badge")
            badge.remove_css_class("dsd-badge")
            if label == "DSD":
                badge.add_css_class("dsd-badge")
            elif label == "CD":
                badge.add_css_class("cd-badge")
            else:
                badge.add_css_class("hires-badge")
    except Exception:
        pass

    if item is None:
        return

    # 本地曲目：按 filepath 后台读内嵌封面
    filepath = getattr(item, "filepath", "")
    if not filepath:
        return
    cached = cache_get(filepath)
    if cached is not MISS:
        pic.set_paintable(cached)
        return
    loading = cover_loading_map()
    if filepath in loading:
        loading[filepath].append((list_item, pic, "file"))
        return
    loading[filepath] = []
    cover_activity.mark_busy()

    def on_done(png_bytes) -> None:
        waiters = loading.pop(filepath, [])
        cover_activity.mark_idle()
        tex = None
        if png_bytes:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_bytes))
            except Exception:
                tex = None
        cache_put(filepath, tex)
        cur = list_item.get_item()
        if cur is not None and getattr(cur, "filepath", "") == filepath:
            pic.set_paintable(tex)
        # 回填所有等待同一 filepath 的其它列表项（曲库/历史/歌单可能同屏）
        for w_item, w_pic, _kind in waiters:
            try:
                wc = w_item.get_item()
                if wc is not None and getattr(wc, "filepath", "") == filepath:
                    w_pic.set_paintable(tex)
            except Exception:
                pass

    run_async(work=lambda: _load_cover_bytes(filepath), on_done=on_done)


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

            def _sync_sorter(*_a):
                # 1) 清除选中：否则排序后 GTK 会把「选中行」滚到可见区
                try:
                    if hasattr(model, "set_selected"):
                        model.set_selected(Gtk.INVALID_LIST_POSITION)
                except Exception:
                    pass
                # 2) 应用新排序
                try:
                    _sort_model.set_sorter(column_view.get_sorter())
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
    if menu_model is not None:
        cover_factory.connect("bind", _attach_cell_right_click, menu_model, ctx)
    cover_col = Gtk.ColumnViewColumn(title="", factory=cover_factory)
    cover_col.set_fixed_width(COVER_SIZE + 16)
    column_view.append_column(cover_col)

    def _append_text_column(title, prop, expand, sort_key, numeric=False, width=0, sortable=True):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", _on_factory_setup)
        factory.connect("bind", _on_factory_bind, prop)
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

    def _append_indicator_column():
        """追加播放指示器列（当前播放行显示跳动音符）。"""
        ind_factory = Gtk.SignalListItemFactory()

        def _ind_setup_cb(_f, list_item):
            _on_indicator_setup(_f, list_item)
            if now_playing_ref is not None:
                list_item._now_playing_ref = now_playing_ref

        ind_factory.connect("setup", _ind_setup_cb)
        ind_factory.connect("bind", _on_indicator_bind)
        ind_col = Gtk.ColumnViewColumn(title="", factory=ind_factory)
        ind_col.set_fixed_width(INDICATOR_SIZE + 16)
        column_view.append_column(ind_col)

    if compact_cols is not None:
        for i, (title, prop, expand, sort_key) in enumerate(compact_cols):
            _append_text_column(title, prop, expand, sort_key, sortable=False)
            if i == 1:
                _append_indicator_column()
        if len(compact_cols) < 2:
            _append_indicator_column()
    else:
        _append_text_column(_("歌名"), "title", True, "title", sortable=_sort_on)
        _append_text_column(_("歌手"), "artist", False, "artist", sortable=_sort_on)
        _append_indicator_column()
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
