"""曲库页：本地 / 在线 切换。

- 本地：复用现有 LocalLibraryPage（数据由 window 喂）。
- 在线：切到时拉 search3('') 分页；滚动接近底部自动加载下一页。

搜索/加载逻辑由 window 注入回调，本页只管 UI 与滚动触发。
"""
from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Gtk

from core.i18n import _

from .pages import LocalLibraryPage, VIEW_SONGS

#: 在线每页拉取数量
PAGE_SIZE = 100
#: 距底部多少像素触发加载下一页
_LOAD_THRESHOLD = 300


class LibraryPage(Gtk.Box):
    """曲库页（本地 / 在线）。"""

    def __init__(self,
                 on_local_track: Optional[Callable] = None,
                 on_online_track: Optional[Callable] = None,
                 on_local_refresh: Optional[Callable] = None,
                 on_add_to_playlist: Optional[Callable] = None,
                 on_online_load_more: Optional[Callable[[int, int], None]] = None,
                 track_actions: Optional[dict] = None,
                 on_tab_changed: Optional[Callable[[str], None]] = None,
                 on_online_track_search: Optional[Callable] = None,
                 on_search_back: Optional[Callable] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_online_load_more = on_online_load_more
        self._on_tab_changed = on_tab_changed
        self._on_search_back = on_search_back
        self._online_loaded = 0
        self._online_total_hint = -1
        self._loading = False
        self._online_active = False

        # ---- 切换按钮 ----
        switch = Gtk.Box(spacing=6)
        switch.set_halign(Gtk.Align.START)
        self._btn_local = Gtk.Button(label=_("本地"))
        self._btn_online = Gtk.Button(label=_("在线"))
        for b in (self._btn_local, self._btn_online):
            b.add_css_class("flat")
            b.add_css_class("nav-pill")
        self._btn_local.connect("clicked", lambda *_: self._switch("local"))
        self._btn_online.connect("clicked", lambda *_: self._switch("online"))
        switch.append(self._btn_local)
        switch.append(self._btn_online)
        self.append(switch)

        # ---- Stack：本地 / 在线 ----
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        try:
            self._stack.set_vhomogeneous(False)
            self._stack.set_hhomogeneous(False)
        except Exception:
            pass
        self.append(self._stack)

        # 本地列表（复用组件）
        self.local_list = LocalLibraryPage(
            on_track_activated=on_local_track,
            track_actions=track_actions,
            on_refresh=on_local_refresh,
            on_add_to_playlist=on_add_to_playlist,
        )
        self._stack.add_named(self.local_list, "local")

        # 在线列表（复用组件；点歌走在线回调）。
        #
        # 空提示说明：部分兼容服务端（如自建网关）**不实现**「search3 空查询
        # 列出全部歌曲」这一非标准行为（在线曲库太大，全量返回不现实）。
        # 此时在线曲库页会为空——给一条明确指引，而不是留空白让用户困惑。
        # 对支持该行为的服务端（如 Navidrome），一旦有数据此提示自然隐藏。
        self.online_list = LocalLibraryPage(
            on_track_activated=on_online_track,
            track_actions=track_actions,
            initial_view=VIEW_SONGS,
            empty_text="该服务端不支持全量曲库浏览，请使用搜索、歌单或推荐",
        )
        self._stack.add_named(self.online_list, "online")

        # 搜索结果视图（本地/在线 tab 之外的第三视图）：
        # 顶栏搜索非空时切到它显示结果；带「返回」按钮回上一页。
        self.search_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _s_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._search_back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._search_back_btn.add_css_class("flat")
        self._search_back_btn.set_tooltip_text(_("返回"))
        self._search_back_btn.connect("clicked", lambda *_: self._fire_search_back())
        _s_head.append(self._search_back_btn)
        self._search_title = Gtk.Label(label=_("搜索结果"))
        self._search_title.add_css_class("heading")
        self._search_title.set_halign(Gtk.Align.START)
        self._search_title.set_hexpand(True)
        _s_head.append(self._search_title)
        self.search_view.append(_s_head)
        # 搜索结果列表（复用 LocalLibraryPage 渲染）
        self.search_list = LocalLibraryPage(
            on_track_activated=on_online_track_search,
            track_actions=track_actions,
            initial_view=VIEW_SONGS,
            empty_text="无结果",
        )
        self.search_view.append(self.search_list)
        self._stack.add_named(self.search_view, "search")

        # 在线滚动到底 → 加载更多
        self._setup_scroll_hook()

        self._switch("local")

    def _fire_search_back(self) -> None:
        if callable(self._on_search_back):
            try:
                self._on_search_back()
            except Exception:
                pass

    def show_search_results(self, query: str, tracks) -> None:
        """显示搜索结果（切到 search 视图）。"""
        try:
            self._search_title.set_text(
                (_("搜索：{q}") .format(q=query)) if query else _("搜索结果"))
        except Exception:
            pass
        try:
            self.search_list.set_tracks(list(tracks or []))
        except Exception:
            pass
        self._stack.set_visible_child_name("search")
        # 结果视图无 tab 高亮（不属本地/在线）
        self._btn_local.remove_css_class("nav-active")
        self._btn_online.remove_css_class("nav-active")
        self._online_active = False

    def is_search_view(self) -> bool:
        try:
            return self._stack.get_visible_child_name() == "search"
        except Exception:
            return False

    def _setup_scroll_hook(self) -> None:
        """给在线列表的滚动窗口挂"接近底部"检测。"""
        try:
            sc = getattr(self.online_list, "_column_view", None)
            # _column_view 是 build_track_columnview 返回的 ScrolledWindow
            if sc is None:
                # 兜底：遍历找 ScrolledWindow
                return
            vadj = sc.get_vadjustment()
            if vadj is not None:
                vadj.connect("value-changed", self._on_scroll)
        except Exception:
            pass

    def _on_scroll(self, adj) -> None:
        if not self._online_active or self._loading:
            return
        try:
            val = adj.get_value()
            upper = adj.get_upper()
            page = adj.get_page_size()
            if upper - (val + page) < _LOAD_THRESHOLD:
                self._request_more()
        except Exception:
            pass

    def _request_more(self) -> None:
        if self._loading:
            return
        # 已知总数且已拉满 → 不再拉
        if self._online_total_hint >= 0 and self._online_loaded >= self._online_total_hint:
            return
        if not callable(self._on_online_load_more):
            return
        self._loading = True
        try:
            self._on_online_load_more(self._online_loaded, PAGE_SIZE)
        except Exception:
            self._loading = False

    def _switch(self, which: str) -> None:
        self._online_active = (which == "online")
        self._stack.set_visible_child_name(which)
        # 高亮
        self._btn_local.remove_css_class("nav-active")
        self._btn_online.remove_css_class("nav-active")
        (self._btn_online if which == "online" else self._btn_local).add_css_class("nav-active")
        # 通知外部（window）：用于区分「曲库页当前在本地还是在线 tab」，
        # 决定搜索框搜索走本地过滤还是在线搜索。
        if callable(self._on_tab_changed):
            try:
                self._on_tab_changed(which)
            except Exception:
                pass
        # 首次切到在线 → 拉第一页
        if which == "online" and self._online_loaded == 0 and not self._loading:
            self._request_more()

    # ---- 供 window 回调 ----
    def append_online(self, tracks, total_hint: int = -1) -> None:
        """追加在线歌曲（分页）。"""
        self._loading = False
        tracks = list(tracks or [])
        if not tracks:
            if total_hint >= 0:
                self._online_total_hint = total_hint
            # 首次加载即为空 → 显式 set_tracks([]) 触发空提示可见（
            # 说明该后端不支持全量列出）。否则空提示不会出现，用户只见空白。
            if self._online_loaded == 0:
                try:
                    self.online_list.set_tracks([])
                except Exception:
                    pass
            return
        # 现有 + 新增
        cur = list(getattr(self.online_list, "_all_tracks", []) or [])
        cur.extend(tracks)
        self.online_list.set_tracks(cur)
        self._online_loaded = len(cur)
        if total_hint >= 0:
            self._online_total_hint = total_hint

    def reset_online(self) -> None:
        """清空在线列表（重新拉取时）。"""
        self._online_loaded = 0
        self._online_total_hint = -1
        self._loading = False
        self.online_list.set_tracks([])
