"""曲库视图（songs / albums / artists 三种模式组合）。

对外导出：
- LocalLibraryPage：本地曲库页面（保留原类名以兼容现有导入）；
- VIEW_SONGS / VIEW_ALBUMS / VIEW_ARTISTS；
- cover_activity：封面加载活动通知单例；
- 兼容旧导入的 _cache_get / _cache_put / _MISS。

本类只消费 TrackItem 列表，不接触 Provider 内部实现。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from gi.repository import Gio, GLib, Gtk

from core.i18n import _
from models import TrackItem
from ui.widgets.playing_indicator import set_current_key as set_playing_key

from .common import (
    MISS,
    VIEW_ALBUMS,
    VIEW_ARTISTS,
    VIEW_SONGS,
    cache_get,
    cache_put,
    cover_activity,
    load_cover_bytes,
    section_title,
)
from .media_grid import CARD_COVER_PX, make_group_card
from .song_list import build_track_columnview

#: 展开后网格每行卡片数（保留旧常量名以兼容）
_GRID_COLS = 4
#: 折叠时显示的行数
_COLLAPSED_ROWS = 1
#: 单行高度估算
_ROW_H = 210

# 兼容旧私有名（window/player_panel 里有 from ui.pages import _cache_get, _MISS）
_cache_get = cache_get
_cache_put = cache_put
_MISS = MISS
# 队列（player_panel）按旧私有名导入封面加载函数；此前未导出 → ImportError
# 被静默吞掉 → 队列封面永远不显示。此处补齐导出。
_load_cover_bytes = load_cover_bytes

__all__ = [
    "LocalLibraryPage",
    "VIEW_SONGS",
    "VIEW_ALBUMS",
    "VIEW_ARTISTS",
    "cover_activity",
]


class LocalLibraryPage(Gtk.Box):
    """展示曲目列表（歌曲列表 / 专辑网格 / 艺术家网格），点击触发回调。"""

    def __init__(self, on_track_activated: Optional[Callable[[TrackItem], None]] = None,
                 title: str = "本地曲库",
                 empty_text: str = "尚未添加音乐目录，请在设置中添加",
                 track_actions: Optional[dict] = None,
                 on_refresh: Optional[Callable[[], None]] = None,
                 initial_view: str = VIEW_SONGS,
                 hide_header: bool = False,
                 no_sort: bool = False,
                 compact_cols=None,
                 show_locate: bool = True,
                 embedded: bool = False,
                 on_add_to_playlist=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_activated = on_track_activated
        self._on_refresh = on_refresh
        self._title = _(title) if title else ""
        self._empty_text = _(empty_text) if empty_text else ""
        self._track_actions = track_actions
        self._store = Gio.ListStore(item_type=TrackItem)
        self._sort_model = Gtk.SortListModel(model=self._store)
        self._selection = Gtk.SingleSelection(model=self._sort_model)
        self._selection.connect("selection-changed", self._on_selection_changed)

        self._all_tracks: List[TrackItem] = []
        self._filter_text = ""
        self._multi_mode = False
        self._selected_keys: set = set()
        self._check_col = None
        self._view_mode = initial_view if initial_view in (VIEW_SONGS, VIEW_ALBUMS, VIEW_ARTISTS) else VIEW_SONGS
        self._group_filter = ""

        header = self._build_header(title, on_refresh, on_add_to_playlist)
        self.append(header)

        self._empty_label = Gtk.Label(label=empty_text)
        self._empty_label.add_css_class("dim-label")
        self.append(self._empty_label)

        self._content_stack = Gtk.Stack()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        try:
            self._content_stack.set_vhomogeneous(False)
            self._content_stack.set_hhomogeneous(False)
        except Exception:
            pass
        self._content_stack.set_vexpand(not embedded)

        self._now_playing_ref = {"key": "", "indicators": []}
        self._column_view = build_track_columnview(
            self._selection, track_actions=track_actions,
            on_row_click=self._on_row_clicked,
            now_playing_ref=self._now_playing_ref,
            hide_header=hide_header,
            no_sort=no_sort,
            compact_cols=compact_cols,
            embedded=embedded,
        )
        self._content_stack.add_named(self._column_view, "songs")

        self._build_grid(embedded)

        self._now_playing_key = ""
        self._content_overlay = Gtk.Overlay()
        self._content_overlay.set_vexpand(not embedded)
        self._content_overlay.set_child(self._content_stack)
        self._locate_btn = self._build_locate_button(show_locate)
        self._content_overlay.add_overlay(self._locate_btn)
        self.append(self._content_overlay)
        self._apply_filter()

    # ------------------------------------------------------------
    # 构建辅助
    # ------------------------------------------------------------
    def _build_header(self, title, on_refresh, on_add_to_playlist) -> Gtk.Box:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(section_title(self._title))
        self._back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._back_btn.add_css_class("flat")
        self._back_btn.set_valign(Gtk.Align.CENTER)
        self._back_btn.set_tooltip_text(_("返回"))
        self._back_btn.set_visible(False)
        self._back_btn.connect("clicked", lambda *_: self._clear_group_filter())
        header.append(self._back_btn)
        header.append(Gtk.Box(hexpand=True))
        self._count_label = Gtk.Label(label="")
        self._count_label.add_css_class("dim-label")
        self._count_label.add_css_class("caption")
        header.append(self._count_label)
        self._multi_btn = Gtk.ToggleButton()
        self._multi_btn.add_css_class("flat")
        self._multi_btn.set_valign(Gtk.Align.CENTER)
        self._multi_btn.set_tooltip_text(_("多选模式"))
        try:
            self._multi_btn.set_child(Gtk.Image.new_from_icon_name("object-select-symbolic"))
        except Exception:
            self._multi_btn.set_label(_("多选"))
        self._multi_btn.connect("toggled", self._on_multi_toggled)
        header.append(self._multi_btn)
        self._on_add_to_playlist = on_add_to_playlist
        self._add_pl_btn = Gtk.Button(label=_("加入歌单"))
        self._add_pl_btn.add_css_class("flat")
        self._add_pl_btn.set_valign(Gtk.Align.CENTER)
        self._add_pl_btn.set_visible(False)
        self._add_pl_btn.connect("clicked", self._on_add_pl_clicked)
        header.append(self._add_pl_btn)
        if on_refresh is not None:
            refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
            refresh_btn.add_css_class("flat")
            refresh_btn.set_tooltip_text(_("重新扫描本地曲库（仅新增/删除会更新）"))
            refresh_btn.set_valign(Gtk.Align.CENTER)
            refresh_btn.connect("clicked", lambda *_: self._do_refresh())
            self._refresh_btn = refresh_btn
            header.append(refresh_btn)
        return header

    def _build_grid(self, embedded: bool) -> None:
        self._grid_flow = Gtk.FlowBox()
        self._grid_flow.set_valign(Gtk.Align.START)
        self._grid_flow.set_max_children_per_line(9999)
        self._grid_flow.set_min_children_per_line(2)
        self._grid_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._grid_flow.set_homogeneous(False)
        self._grid_flow.add_css_class("media-flow")
        self._grid_flow.set_row_spacing(8)
        self._grid_flow.set_column_spacing(8)
        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_child(self._grid_flow)
        grid_scroll.set_vexpand(not embedded)
        if embedded:
            grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
            self._grid_flow.set_valign(Gtk.Align.START)
        else:
            grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._grid_scroll = grid_scroll
        self._collapsed = False
        self._grid_rotate_timers = []
        self._grid_groups = {}
        #: 展开网格 / 折叠横排各自的卡片是否已构建。
        #: 只构建当前可见的那一份，避免同一批封面被解码两次、控件翻倍。
        self._grid_built = False
        self._hbox_built = False
        self._rotate_guard_id = None
        self._content_stack.add_named(grid_scroll, "grid")

        self._hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._hbox.set_valign(Gtk.Align.START)
        h_scroll = Gtk.ScrolledWindow()
        h_scroll.set_child(self._hbox)
        h_scroll.set_vexpand(False)
        h_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self._h_scroll = h_scroll
        self._content_stack.add_named(h_scroll, "grid_h")

    def _build_locate_button(self, show_locate: bool) -> Gtk.Button:
        btn = Gtk.Button(icon_name="find-location-symbolic")
        btn.add_css_class("locate-fab")
        btn.set_tooltip_text(_("定位当前播放"))
        btn.set_halign(Gtk.Align.END)
        btn.set_valign(Gtk.Align.END)
        btn.set_margin_end(18)
        btn.set_margin_bottom(18)
        try:
            _img = btn.get_child()
            if _img is not None:
                _img.set_pixel_size(20)
        except Exception:
            pass
        btn.connect("clicked", lambda *_: self._locate_now_playing())
        btn.set_visible(bool(show_locate))
        return btn

    # ------------------------------------------------------------
    # 数据入口
    # ------------------------------------------------------------
    def set_tracks(self, tracks: List[TrackItem]) -> None:
        self._all_tracks = list(tracks or [])
        self._apply_filter()

    def set_filter_text(self, text: str) -> None:
        text = (text or "").strip()
        # 关键词未变则不重渲染：切页时 window 会把搜索框内容同步到各页，
        # 无脑重渲染会让每次切回主页都白重建一遍网格。
        if text == self._filter_text:
            return
        self._filter_text = text
        self._apply_filter()

    # ------------------------------------------------------------
    # 多选模式
    # ------------------------------------------------------------
    def _on_multi_toggled(self, btn) -> None:
        on = bool(btn.get_active())
        self._multi_mode = on
        self._selected_keys.clear()
        try:
            cv = getattr(self._column_view, "_column_view", None)
            if cv is not None:
                if on and self._check_col is None:
                    self._check_col = self._make_check_column()
                    cv.insert_column(0, self._check_col)
                elif not on and self._check_col is not None:
                    try:
                        cv.remove_column(self._check_col)
                    except Exception:
                        pass
                    self._check_col = None
        except Exception:
            pass
        self._update_multi_count()

    def _make_check_column(self):
        factory = Gtk.SignalListItemFactory()

        def _setup(_f, item):
            cb = Gtk.CheckButton()
            cb.set_valign(Gtk.Align.CENTER)
            cb.set_halign(Gtk.Align.CENTER)
            cb.set_margin_start(4)
            cb.set_margin_end(4)
            item.set_child(cb)

        def _bind(_f, item):
            track = item.get_item()
            cb = item.get_child()
            if track is None or cb is None:
                return
            key = (getattr(track, "filepath", "") or getattr(track, "source_id", ""))
            try:
                cb.set_active(key in self._selected_keys)
            except Exception:
                pass
            try:
                if getattr(cb, "_sig", None) is not None:
                    cb.disconnect(cb._sig)
            except Exception:
                pass
            cb._sig = cb.connect("toggled", self._on_check_toggled, key)

        factory.connect("setup", _setup)
        factory.connect("bind", _bind)
        col = Gtk.ColumnViewColumn(title="", factory=factory)
        col.set_fixed_width(40)
        return col

    def _on_check_toggled(self, cb, key: str) -> None:
        try:
            if cb.get_active():
                self._selected_keys.add(key)
            else:
                self._selected_keys.discard(key)
            self._update_multi_count()
        except Exception:
            pass

    def _update_multi_count(self) -> None:
        try:
            if self._multi_mode:
                n = len(self._selected_keys)
                self._count_label.set_text(f"{_('已选')} {n} {_('首')}")
                try:
                    self._add_pl_btn.set_visible(n > 0 and callable(self._on_add_to_playlist))
                except Exception:
                    pass
            else:
                try:
                    self._add_pl_btn.set_visible(False)
                except Exception:
                    pass
                self._apply_filter()
        except Exception:
            pass

    def _on_add_pl_clicked(self, _btn) -> None:
        try:
            tracks = self.selected_tracks()
            if tracks and callable(self._on_add_to_playlist):
                self._on_add_to_playlist(tracks)
        except Exception:
            pass

    def selected_tracks(self) -> list:
        out = []
        try:
            for t in self._all_tracks:
                k = (getattr(t, "filepath", "") or getattr(t, "source_id", ""))
                if k in self._selected_keys:
                    out.append(t)
        except Exception:
            pass
        return out

    def set_list_height(self, px: int) -> None:
        try:
            ls = getattr(self, "_column_view", None)
            if ls is None:
                return
            if px and px > 0:
                ls.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                ls.set_size_request(-1, int(px))
            else:
                ls.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
                ls.set_size_request(-1, -1)
        except Exception:
            pass

    def set_collapsed(self, collapsed: bool, rows: int = 1, row_h: int = 210,
                      cols: int = 4) -> None:
        """折叠/展开内容区（网格模式）。"""
        try:
            self._collapsed = bool(collapsed)
            h = int(row_h) if collapsed else -1
            gs = getattr(self, "_grid_scroll", None)
            flow = getattr(self, "_grid_flow", None)
            hs = getattr(self, "_h_scroll", None)
            if collapsed:
                # 折叠视图的卡片按需构建（展开时不再白建一份）
                self._ensure_hbox_cards()
                try:
                    if self._view_mode in (VIEW_ALBUMS, VIEW_ARTISTS):
                        self._content_stack.set_visible_child_name("grid_h")
                except Exception:
                    pass
                if hs is not None:
                    hs.set_size_request(-1, h)
            else:
                if flow is not None:
                    try:
                        flow.set_max_children_per_line(9999)
                        flow.set_min_children_per_line(1)
                    except Exception:
                        pass
                # 展开时按需构建网格卡片（折叠态下不再白建一份）
                self._ensure_grid_cards()
                try:
                    if self._view_mode in (VIEW_ALBUMS, VIEW_ARTISTS):
                        self._content_stack.set_visible_child_name("grid")
                except Exception:
                    pass
                if gs is not None:
                    gs.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
                    gs.set_size_request(-1, -1)
            ls = getattr(self, "_column_view", None)
            if ls is not None:
                if collapsed:
                    ls.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                    ls.set_size_request(-1, h)
                else:
                    ls.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
                    ls.set_size_request(-1, -1)
            try:
                self._content_stack.queue_resize()
                self._content_overlay.queue_resize()
                self.queue_resize()
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------
    # 定位当前播放
    # ------------------------------------------------------------
    def set_now_playing(self, track) -> None:
        if track is None:
            key = ""
        else:
            key = (getattr(track, "filepath", "") or getattr(track, "source_id", "")
                   or getattr(track, "title", ""))
            key = key or ""
        self._now_playing_key = key
        try:
            self._now_playing_ref["key"] = key
        except Exception:
            pass
        try:
            set_playing_key(key)
        except Exception:
            pass

    def _locate_now_playing(self) -> None:
        try:
            if self._view_mode != VIEW_SONGS:
                self._content_stack.set_visible_child_name("songs")
        except Exception:
            pass
        key = getattr(self, "_now_playing_key", "") or ""
        if not key:
            return
        position = -1
        try:
            n = self._store.get_n_items()
            for i in range(n):
                t = self._store.get_item(i)
                k = (getattr(t, "filepath", "") or getattr(t, "source_id", "")
                     or getattr(t, "title", ""))
                if k == key:
                    position = i
                    break
        except Exception:
            position = -1
        if position < 0:
            return
        try:
            cv = getattr(self._column_view, "_column_view", None)
            if cv is not None:
                try:
                    cv.scroll_to(position, None, Gtk.ListScrollFlags.NONE, None)
                except Exception:
                    cv.scroll_to(position, None, Gtk.ListScrollFlags.NONE)
        except Exception:
            pass

    # ------------------------------------------------------------
    # 视图切换
    # ------------------------------------------------------------
    def set_view_mode(self, mode: str) -> None:
        if mode not in (VIEW_SONGS, VIEW_ALBUMS, VIEW_ARTISTS):
            return
        # 模式未变且不在分组内：无需重渲染
        if mode == self._view_mode and not self._group_filter:
            return
        self._view_mode = mode
        self._group_filter = ""
        self._back_btn.set_visible(False)
        self._apply_filter()

    def _clear_group_filter(self) -> None:
        self._group_filter = ""
        self._view_mode = getattr(self, "_view_before_group", VIEW_SONGS) or VIEW_SONGS
        self._back_btn.set_visible(False)
        self._apply_filter()

    def _enter_group(self, group_name: str) -> None:
        self._view_before_group = self._view_mode
        self._group_filter = group_name
        self._view_mode = VIEW_SONGS
        self._back_btn.set_visible(True)
        self._apply_filter()

    # ------------------------------------------------------------
    # 过滤 / 渲染
    # ------------------------------------------------------------
    def _filtered_tracks(self) -> List[TrackItem]:
        kw = self._filter_text.lower()
        gf = self._group_filter.lower()
        out = []
        for t in self._all_tracks:
            title = (getattr(t, "title", "") or "").lower()
            artist = (getattr(t, "artist", "") or "").lower()
            album = (getattr(t, "album", "") or "").lower()
            album_key = album or "未知专辑"
            artist_key = artist or "未知艺术家"
            if kw and not (kw in title or kw in artist or kw in album):
                continue
            if gf and not (gf == artist_key or gf == album_key):
                continue
            out.append(t)
        return out

    def remove_track(self, track) -> bool:
        if track is None:
            return False
        key = (getattr(track, "filepath", "") or getattr(track, "source_id", "") or "")
        title = getattr(track, "title", "")
        artist = getattr(track, "artist", "")
        new_list = []
        removed = False
        for t in self._all_tracks:
            k = (getattr(t, "filepath", "") or getattr(t, "source_id", "") or "")
            same = (k and k == key) if key else (getattr(t, "title", "") == title and getattr(t, "artist", "") == artist)
            if same:
                removed = True
                continue
            new_list.append(t)
        if removed:
            self._all_tracks = new_list
            self._apply_filter()
        return removed

    def _apply_filter(self) -> None:
        tracks = self._filtered_tracks()
        if self._view_mode == VIEW_SONGS:
            self._content_stack.set_visible_child_name("songs")
            self._render_tracks(tracks)
        else:
            self._content_stack.set_visible_child_name(
                "grid_h" if getattr(self, "_collapsed", False) else "grid")
            self._render_grid(tracks)
        try:
            total = len(self._all_tracks)
            shown = len(tracks)
            if (self._filter_text or self._group_filter) and total:
                self._count_label.set_text(f"{shown} / {total} {_('首')}")
            else:
                self._count_label.set_text(f"{total} {_('首')}" if total else "")
        except Exception:
            pass

    def _render_tracks(self, tracks: List[TrackItem]) -> None:
        self._suppress_selection = True
        try:
            self._store.splice(0, self._store.get_n_items(), tracks)
            self._selection.set_selected(Gtk.INVALID_LIST_POSITION)
        finally:
            self._suppress_selection = False
        self._empty_label.set_visible(len(tracks) == 0)

    def _render_grid(self, tracks: List[TrackItem]) -> None:
        try:
            self._grid_flow.set_visible(False)
            self._hbox.set_visible(False)
        except Exception:
            pass
        c = self._grid_flow.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._grid_flow.remove(c)
            c = nxt
        try:
            hc = self._hbox.get_first_child()
            while hc is not None:
                nxt = hc.get_next_sibling()
                self._hbox.remove(hc)
                hc = nxt
        except Exception:
            pass
        groups: dict = {}
        for t in tracks:
            if self._view_mode == VIEW_ALBUMS:
                key = (getattr(t, "album", "") or "未知专辑").strip() or "未知专辑"
            else:
                key = (getattr(t, "artist", "") or "未知艺术家").strip() or "未知艺术家"
            groups.setdefault(key, []).append(t)
        self._empty_label.set_visible(len(groups) == 0)
        self._clear_grid_rotate_timers()
        self._grid_groups = dict(groups)
        # 只构建「当前可见」的那一份卡片：
        #   折叠 → 横向一排（grid_h）；展开 → 网格（grid）。
        # 此前无论展开还是折叠都建两份，封面被解码两次、控件数量翻倍，
        # 既抬高内存峰值，也让每次重渲染慢一倍。
        self._grid_built = False
        self._hbox_built = False
        if getattr(self, "_collapsed", False):
            self._ensure_hbox_cards()
        else:
            self._ensure_grid_cards()
        try:
            self._grid_flow.set_visible(True)
            self._hbox.set_visible(True)
        except Exception:
            pass
        self._start_rotate_guard()

    def _ensure_grid_cards(self) -> None:
        """懒构建展开视图（网格）的卡片。与折叠视图互斥，只建可见的那份。"""
        if getattr(self, "_grid_built", False):
            return
        self._grid_built = True
        try:
            groups = getattr(self, "_grid_groups", {}) or {}
            for name in sorted(groups.keys(), key=lambda s: s.lower()):
                items = groups[name]
                try:
                    card = make_group_card(name, items, self._enter_group)
                    self._grid_flow.append(card)
                except Exception:
                    pass
        except Exception:
            pass

    def _ensure_hbox_cards(self) -> None:
        """懒构建折叠视图（横向一排）的卡片。

        折叠视图此前与展开网格各建一份卡片，每组封面被解码两次、
        控件数量翻倍。改为仅在需要折叠时构建，展开时不产生这份开销。
        """
        if getattr(self, "_hbox_built", False):
            return
        self._hbox_built = True
        try:
            groups = getattr(self, "_grid_groups", {}) or {}
            group_items = list(groups.items())
            if not group_items:
                return
            rotate_on = self._card_rotate_enabled()
            for name in sorted(groups.keys(), key=lambda s: s.lower()):
                items = groups[name]
                hcard = None
                try:
                    hcard = make_group_card(name, items, self._enter_group)
                    self._hbox.append(hcard)
                except Exception:
                    hcard = None
                # 全部卡片都参与轮播：此前只调度前 12 张，横向滚动到
                # 中后段的卡片不会轮播。不可见卡片由 _rotate_card 内部节流跳过。
                if hcard is not None and rotate_on:
                    self._schedule_card_rotate(hcard, group_items)
        except Exception:
            pass

    # ------------------------------------------------------------
    # 卡片轮播
    # ------------------------------------------------------------
    def _clear_grid_rotate_timers(self) -> None:
        try:
            for tid in getattr(self, "_grid_rotate_timers", []):
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
        except Exception:
            pass
        self._grid_rotate_timers = []
        try:
            gid = getattr(self, "_rotate_guard_id", None)
            if gid is not None:
                GLib.source_remove(gid)
        except Exception:
            pass
        self._rotate_guard_id = None

    def _start_rotate_guard(self) -> None:
        if getattr(self, "_rotate_guard_id", None) is not None:
            return
        self._guard_last_on = self._card_rotate_enabled()
        self._guard_last_speed = self._card_rotate_speed()
        try:
            self._guard_last_mapped = bool(self.get_mapped())
        except Exception:
            self._guard_last_mapped = True

        def _tick():
            try:
                on = self._card_rotate_enabled()
                was = getattr(self, "_guard_last_on", on)
                spd = self._card_rotate_speed()
                spd_was = getattr(self, "_guard_last_speed", spd)
                try:
                    mapped = bool(self.get_mapped())
                except Exception:
                    mapped = True
                was_mapped = getattr(self, "_guard_last_mapped", mapped)
                self._guard_last_mapped = mapped
                if not on and was:
                    self._clear_grid_rotate_timers()
                    self._rotate_guard_id = GLib.timeout_add(1000, _tick)
                    self._guard_last_on = False
                    self._guard_last_speed = spd
                    return False
                if on and not was:
                    self._restart_card_rotate()
                elif on and spd != spd_was:
                    self._clear_grid_rotate_timers()
                    self._restart_card_rotate()
                    self._rotate_guard_id = GLib.timeout_add(1000, _tick)
                    self._guard_last_on = True
                    self._guard_last_speed = spd
                    return False
                elif on and mapped and not was_mapped:
                    # 页面从不可见恢复：不可见期间各卡片的定时器已在 _rotate_card
                    # 中提前返回且未重新调度，需要重新拉起，否则轮播永久失效。
                    self._restart_card_rotate()
                self._guard_last_on = on
                self._guard_last_speed = spd
            except Exception:
                pass
            return True

        try:
            self._rotate_guard_id = GLib.timeout_add(1000, _tick)
        except Exception:
            self._rotate_guard_id = None

    def _restart_card_rotate(self) -> None:
        try:
            group_items = list(getattr(self, "_grid_groups", {}).items())
            if not group_items:
                return
            # 先清掉残留的卡片定时器，避免重复调度（guard 轮询不受影响）
            self._cancel_card_timers()
            # 与 _ensure_hbox_cards 一致：全部卡片都重新调度（此前限 12 张）
            c = self._hbox.get_first_child()
            while c is not None:
                self._schedule_card_rotate(c, group_items)
                c = c.get_next_sibling()
        except Exception:
            pass

    def _cancel_card_timers(self) -> None:
        """只取消卡片轮播定时器，保留 guard 轮询定时器。"""
        try:
            for tid in getattr(self, "_grid_rotate_timers", []):
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
        except Exception:
            pass
        self._grid_rotate_timers = []

    @staticmethod
    def _card_rotate_enabled() -> bool:
        try:
            from config.settings import get_config
            return get_config().get_bool("card_rotate_enabled", True)
        except Exception:
            return True

    @staticmethod
    def _card_rotate_speed() -> str:
        try:
            from config.settings import get_config
            return get_config().get_str("card_rotate_speed", "medium")
        except Exception:
            return "medium"

    @staticmethod
    def _card_rotate_range() -> tuple:
        try:
            from config.settings import get_config
            spd = get_config().get_str("card_rotate_speed", "medium")
        except Exception:
            spd = "medium"
        if spd == "slow":
            return (15000, 30000)
        if spd == "fast":
            return (5000, 15000)
        return (10000, 20000)

    def _schedule_card_rotate(self, card, group_items, first: bool = False) -> None:
        import random as _r
        lo, hi = self._card_rotate_range()
        delay = _r.randint(lo, hi)

        def _fire():
            try:
                self._grid_rotate_timers = [
                    t for t in getattr(self, "_grid_rotate_timers", []) if t != tid
                ]
            except Exception:
                pass
            self._rotate_card(card, group_items)
            return False

        try:
            tid = GLib.timeout_add(delay, _fire)
            self._grid_rotate_timers.append(tid)
        except Exception:
            pass

    def _rotate_card(self, card, group_items) -> None:
        if not self._card_rotate_enabled():
            return
        # 折叠视图未构建 / 页面不可见时不再轮换，避免无谓的封面解码与定时器堆积。
        if not getattr(self, "_hbox_built", False):
            return
        try:
            if not self.get_mapped():
                return
        except Exception:
            pass
        # 滚出横向视口的卡片不切换：全部卡片都带定时器后，若不做这层节流，
        # 几百张卡片会各自跑 600ms 转场 + 封面解码。仍重新调度自己，
        # 滚回来即可继续轮播。
        if not self._is_card_in_view(card):
            try:
                self._schedule_card_rotate(card, group_items)
            except Exception:
                pass
            return
        try:
            import random as _r
            if not group_items:
                return
            cur = getattr(card, "_group_name", None)
            candidates = [g for g in group_items if g[0] != cur] or group_items
            name, items = _r.choice(candidates)
            apply_fn = getattr(card, "_apply_group", None)
            if callable(apply_fn):
                apply_fn(name, items)
        except Exception:
            pass
        try:
            self._schedule_card_rotate(card, group_items)
        except Exception:
            pass

    def _is_card_in_view(self, card) -> bool:
        """卡片是否在折叠视图的横向视口内（轮播节流用）。

        滚出视口的卡片仍处于 mapped 状态，轮播动画看不到、封面解码也是白费。
        算不出边界时保守返回 True（宁可多轮播，不可漏轮播）。
        """
        try:
            hs = getattr(self, "_h_scroll", None)
            if hs is None:
                return True
            ok, rect = card.compute_bounds(hs)
            if not ok:
                return True
            w = hs.get_width()
            if w <= 0:
                return True
            return (rect.origin.x + rect.size.width) > 0 and rect.origin.x < w
        except Exception:
            return True

    # ------------------------------------------------------------
    # 杂项回调
    # ------------------------------------------------------------
    def _do_refresh(self) -> None:
        if callable(self._on_refresh):
            self._on_refresh()

    def _on_selection_changed(self, selection, _position, _n_items) -> None:
        return

    def _on_row_clicked(self, item) -> None:
        if item is not None and callable(self._on_activated):
            self._on_activated(item)
