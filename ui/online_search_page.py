"""在线页：三视图切换。

- browse ：多区块首页（我的歌单 / 最新添加 / 随机专辑）
- detail ：歌单详情（返回 + 歌单名 + 歌曲列表）
- result ：搜索结果（歌曲列表）

搜索框用顶栏那个（本页不重复放）。
"""
from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Gtk

from core.i18n import _

from .online_grid import OnlineCardGrid
from .online_home import OnlinePlaylistSection
from .online_section import CardSection


class OnlineSearchPage(Gtk.Box):
    """在线页（多区块首页 + 歌单详情 + 搜索结果）。"""

    def __init__(self,
                 on_playlist_click: Optional[Callable] = None,
                 on_track_activated: Optional[Callable] = None,
                 on_album_click: Optional[Callable] = None,
                 track_actions: Optional[dict] = None,
                 on_load_more: Optional[Callable] = None,
                 on_section_more: Optional[Callable] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_vexpand(True)

        from .pages import LocalLibraryPage, VIEW_SONGS

        self._on_album_click = on_album_click
        self._on_load_more_ext = on_load_more
        self._on_section_more = on_section_more
        # 供 section 列表视图按类型重建（专辑/艺术家列头不同）
        self._on_track_activated = on_track_activated
        self._track_actions = track_actions
        # section 列表行点击 → 打开详情：按 source_id 索引卡片的 click
        self._section_click_map = {}
        self._section_name = ""
        self._section_loading = False
        self._section_exhausted = False
        # 详情页来源视图（返回时回退目标）
        self._detail_from = "browse"
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        try:
            self._stack.set_transition_duration(200)
        except Exception:
            pass
        try:
            self._stack.set_vhomogeneous(False)
            self._stack.set_hhomogeneous(False)
        except Exception:
            pass
        # vexpand：各视图内部自滚（section 滚动加载需要内部滚）。
        self._stack.set_vexpand(True)
        self.append(self._stack)

        # ---- browse：多区块首页（内滚）----
        browse = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        browse.set_margin_top(4)

        self.playlist_section = OnlinePlaylistSection(on_playlist_click=on_playlist_click)
        browse.append(self.playlist_section)

        self.random_section = CardSection(_("专辑"), on_more=lambda: self._open_section("random"))
        browse.append(self.random_section)

        self.artists_section = CardSection(_("艺术家"), on_more=lambda: self._open_section("artists"))
        browse.append(self.artists_section)

        self.random_songs_section = CardSection(_("随机歌曲"))
        browse.append(self.random_songs_section)

        self.newest_section = CardSection(_("最新添加"))
        browse.append(self.newest_section)

        browse_scroll = Gtk.ScrolledWindow()
        browse_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        browse_scroll.set_vexpand(True)
        browse_scroll.set_child(browse)
        # browse 外层 Stack：loading（spinner）/ content（区块）
        self._browse_stack = Gtk.Stack()
        try:
            self._browse_stack.set_vhomogeneous(False)
            self._browse_stack.set_hhomogeneous(False)
        except Exception:
            pass
        # loading 视图：居中 spinner + 文字
        _loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        _loading.set_valign(Gtk.Align.CENTER)
        _loading.set_halign(Gtk.Align.CENTER)
        _loading.set_vexpand(True)
        _spin = Gtk.Spinner()
        _spin.set_size_request(40, 40)
        try:
            _spin.start()
        except Exception:
            pass
        _spin.set_halign(Gtk.Align.CENTER)
        _loading.append(_spin)
        _lbl = Gtk.Label(label=_("加载中…"))
        _lbl.add_css_class("dim-label")
        _loading.append(_lbl)
        self._browse_stack.add_named(_loading, "loading")
        self._browse_stack.add_named(browse_scroll, "content")
        self._browse_stack.set_visible_child_name("loading")
        self._stack.add_named(self._browse_stack, "browse")

        # ---- detail：歌单详情 ----
        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        d_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.add_css_class("flat")
        back_btn.set_tooltip_text(_("返回"))
        back_btn.connect("clicked", lambda *_: self._detail_back())
        d_header.append(back_btn)
        self._detail_title = Gtk.Label(label="")
        self._detail_title.add_css_class("heading")
        self._detail_title.set_halign(Gtk.Align.START)
        d_header.append(self._detail_title)
        detail.append(d_header)
        self.detail_list = LocalLibraryPage(
            on_track_activated=on_track_activated,
            title="",
            empty_text="歌单为空",
            track_actions=track_actions,
            initial_view=VIEW_SONGS,
            hide_header=True,
            show_locate=False,
        )
        self.detail_list.set_vexpand(True)
        detail.append(self.detail_list)
        self._stack.add_named(detail, "detail")

        # ---- result：搜索结果 ----
        result = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.status_lbl = Gtk.Label(label="")
        self.status_lbl.add_css_class("dim-label")
        self.status_lbl.set_halign(Gtk.Align.START)
        result.append(self.status_lbl)
        self.result_page = LocalLibraryPage(
            on_track_activated=on_track_activated,
            title="",
            empty_text="",
            track_actions=track_actions,
            initial_view=VIEW_SONGS,
            hide_header=True,
            show_locate=False,
        )
        self.result_page.set_vexpand(True)
        result.append(self.result_page)
        self._stack.add_named(result, "result")

        # ---- section：完整列表（网格 + 滚动加载）----
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section.set_vexpand(True)
        s_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        s_back = Gtk.Button(icon_name="go-previous-symbolic")
        s_back.add_css_class("flat")
        s_back.set_tooltip_text(_("返回"))
        s_back.connect("clicked", lambda *_: self._stack.set_visible_child_name("browse"))
        s_header.append(s_back)
        self._section_title = Gtk.Label(label="")
        self._section_title.add_css_class("heading")
        self._section_title.set_halign(Gtk.Align.START)
        self._section_title.set_hexpand(True)
        s_header.append(self._section_title)
        # 标题旁的加载指示：转圈 Spinner（放标题右边，出现/消失不顶动网格）
        self._section_more_hint = Gtk.Spinner()
        self._section_more_hint.set_size_request(18, 18)
        self._section_more_hint.set_valign(Gtk.Align.CENTER)
        self._section_more_hint.set_margin_start(8)
        self._section_more_hint.set_visible(False)
        s_header.append(self._section_more_hint)
        # 视图切换按钮（网格 / 列表）
        self._section_view = "grid"
        self._section_view_btn = Gtk.Button(icon_name="view-list-symbolic")
        self._section_view_btn.add_css_class("flat")
        self._section_view_btn.set_tooltip_text(_("切换网格 / 列表"))
        self._section_view_btn.connect("clicked", lambda *_: self._toggle_section_view())
        s_header.append(self._section_view_btn)
        section.append(s_header)
        # 虚拟化网格（GridView）：只建可视区卡片，滚动复用，
        # 根本解决「更多」页每次重建全部卡片的卡顿。
        self._section_grid = OnlineCardGrid()
        self._section_scroll = self._section_grid  # 兼容旧引用（自滚）
        # 滚动到底自动拉下一批
        try:
            self._section_grid.connect_near_bottom(self._section_do_more)
        except Exception:
            pass
        # 网格 / 列表两种容器，用 Stack 切
        self._section_grid_stack = Gtk.Stack()
        self._section_grid_stack.set_vexpand(True)
        try:
            self._section_grid_stack.set_transition_type(
                Gtk.StackTransitionType.CROSSFADE)
            self._section_grid_stack.set_transition_duration(200)
        except Exception:
            pass
        self._section_grid_stack.add_named(self._section_grid, "grid")
        # loading 视图（首批加载时显示）
        _sec_loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        _sec_loading.set_valign(Gtk.Align.CENTER)
        _sec_loading.set_halign(Gtk.Align.CENTER)
        _sec_loading.set_vexpand(True)
        _sec_spin = Gtk.Spinner()
        _sec_spin.set_size_request(36, 36)
        try:
            _sec_spin.start()
        except Exception:
            pass
        _sec_spin.set_halign(Gtk.Align.CENTER)
        _sec_loading.append(_sec_spin)
        _sec_lbl = Gtk.Label(label=_("加载中…"))
        _sec_lbl.add_css_class("dim-label")
        _sec_loading.append(_sec_lbl)
        self._section_grid_stack.add_named(_sec_loading, "loading")
        # 列表视图（复用 LocalLibraryPage）
        self._section_list_page = LocalLibraryPage(
            on_track_activated=on_track_activated,
            title="",
            empty_text="",
            track_actions=track_actions,
            initial_view=VIEW_SONGS,
            hide_header=True,
            show_locate=False,
        )
        self._section_list_scroll = Gtk.ScrolledWindow()
        self._section_list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._section_list_scroll.set_child(self._section_list_page)
        self._section_list_scroll.set_vexpand(True)
        # 列表模式也支持滚动到底自动加载。
        # 注意：LocalLibraryPage 内部自带滚动容器（ColumnView 的 ScrolledWindow），
        # 外层 _section_list_scroll 实际不会滚动，真正滚的是 _column_view。
        # 所以两个都挂（回调自带去重，哪个动都能触发）。
        self._wire_scroll_autoload(self._section_list_scroll)
        try:
            self._wire_scroll_autoload(self._section_list_page._column_view)
        except Exception:
            pass
        self._section_grid_stack.add_named(self._section_list_scroll, "list")
        self._section_grid_stack.set_visible_child_name("loading")
        section.append(self._section_grid_stack)
        self._stack.add_named(section, "section")

        self._stack.set_visible_child_name("browse")

    def _open_section(self, name: str) -> None:
        """点区块「更多」：切到 section 视图，请求外部拉该区块完整列表。"""
        self._section_name = name
        self._section_loading = False
        self._section_exhausted = False
        self._section_title.set_text(_(self._section_titles().get(name, name)))
        self._section_flow_clear()
        # 列表视图按 section 类型重建（专辑/艺术家列头不同）
        self._rebuild_section_list(name)
        # 恢复该 section 上次的视图模式（网格/列表），默认网格。
        # 同时把图标与状态同步，避免状态和显示不一致导致切换失灵。
        memo = getattr(self, "_section_view_memo", None)
        if memo is None:
            memo = {}
            self._section_view_memo = memo
        self._section_view = memo.get(name, "grid")
        try:
            icon = "view-grid-symbolic" if self._section_view == "list" else "view-list-symbolic"
            self._section_view_btn.set_icon_name(icon)
        except Exception:
            pass
        # 首批加载指示：显示 spinner
        try:
            self._section_grid_stack.set_visible_child_name("loading")
        except Exception:
            pass
        self._section_hint_hide()
        self._stack.set_visible_child_name("section")
        if callable(self._on_section_more):
            try:
                self._on_section_more(name, 0)   # offset=0，首批
            except Exception:
                pass

    def _rebuild_section_list(self, name: str) -> None:
        """按 section 类型重建列表视图（列头随类型变化）。

        - 专辑（random/newest）：专辑 | 歌手
        - 艺术家（artists）：艺术家
        重建后重新挂『接近底部自动加载』（旧滚动容器已丢弃）。
        """
        from .pages import LocalLibraryPage, VIEW_SONGS
        try:
            # 重建即换 section，清空行点击映射
            self._section_click_map = {}
            if name == "artists":
                cols = [(_("艺术家"), "title", True, "title")]
            else:
                cols = [(_("专辑"), "title", True, "title"),
                        (_("歌手"), "artist", False, "artist")]
            page = LocalLibraryPage(
                on_track_activated=self._section_list_activated,
                title="",
                empty_text="",
                track_actions=self._track_actions,
                initial_view=VIEW_SONGS,
                hide_header=True,
                no_sort=True,
                show_locate=False,
                compact_cols=cols,
            )
            self._section_list_page = page
            self._section_list_scroll.set_child(page)
            # 重新挂滚动自动加载（内部 ColumnView 的滚动容器）
            try:
                self._wire_scroll_autoload(page._column_view)
            except Exception:
                pass
        except Exception:
            pass

    def _toggle_section_view(self) -> None:
        """切换 section 的网格 / 列表视图（并记忆该 section 的选择）。"""
        try:
            if self._section_view == "grid":
                self._section_view = "list"
                self._section_view_btn.set_icon_name("view-grid-symbolic")
                self._section_grid_stack.set_visible_child_name("list")
            else:
                self._section_view = "grid"
                self._section_view_btn.set_icon_name("view-list-symbolic")
                self._section_grid_stack.set_visible_child_name("grid")
            # 写入记忆（按 section 名）
            memo = getattr(self, "_section_view_memo", None)
            if memo is None:
                memo = {}
                self._section_view_memo = memo
            if self._section_name:
                memo[self._section_name] = self._section_view
        except Exception:
            pass

    @staticmethod
    def _section_titles() -> dict:
        return {"newest": "最新添加", "random": "专辑",
                "artists": "艺术家"}

    def _wire_scroll_autoload(self, scroll: Gtk.ScrolledWindow) -> None:
        """给任意 ScrolledWindow 挂『接近底部自动加载』。

        与网格的 connect_near_bottom 行为一致：距底 < 400px 触发一次，
        离开底部区域后重新武装。回调方（_section_do_more）自带去重。
        """
        state = {"armed": True}

        def _on_value_changed(adj):
            try:
                remaining = adj.get_upper() - adj.get_page_size() - adj.get_value()
            except Exception:
                return
            if remaining <= 400:
                if state["armed"]:
                    state["armed"] = False
                    self._section_do_more()
            else:
                state["armed"] = True

        try:
            adj = scroll.get_vadjustment()
            if adj is not None:
                adj.connect("value-changed", _on_value_changed)
        except Exception:
            pass

    def _section_hint_show(self) -> None:
        """显示标题旁的加载转圈。"""
        try:
            self._section_more_hint.set_visible(True)
            self._section_more_hint.start()
        except Exception:
            pass

    def _section_hint_hide(self) -> None:
        """隐藏标题旁的加载转圈。"""
        try:
            self._section_more_hint.stop()
            self._section_more_hint.set_visible(False)
        except Exception:
            pass

    def _section_flow_clear(self) -> None:
        try:
            self._section_grid.clear()
        except Exception:
            pass

    def show_section_cards(self, name: str, cards, offset: int = 0) -> None:
        """填充 section 视图卡片（offset=0 重建，否则追加）。"""
        if name != self._section_name:
            return
        cards = list(cards or [])
        if offset == 0:
            self._section_flow_clear()
            self._section_exhausted = False
        self._section_loading = False
        # 隐藏标题旁加载指示
        self._section_hint_hide()
        if not cards:
            self._section_exhausted = True
            return
        if offset == 0:
            self._section_grid.set_cards(cards)
        else:
            self._section_grid.append_cards(cards)
        # 追加后重新武装滚动自动加载
        try:
            self._section_grid.arm_near_bottom()
        except Exception:
            pass
        # 首批到达 → 从 loading 切到当前视图（网格或列表，尊重记忆）
        try:
            if offset == 0:
                self._section_grid_stack.set_visible_child_name(self._section_view)
        except Exception:
            pass
        # 列表视图也要填充（转成 TrackItem）
        try:
            self._section_fill_list(cards, offset)
        except Exception:
            pass

    def _section_list_activated(self, tr) -> None:
        """section 列表行点击：打开专辑/艺术家详情（不播放）。

        列表里装的是专辑/艺术家卡片，不是歌曲；点行应打开详情。
        用 source_id 反查卡片原 click 回调；查不到则回退到普通激活。
        """
        try:
            key = getattr(tr, "source_id", "") or getattr(tr, "title", "")
            click = self._section_click_map.get(key)
            if callable(click):
                click()
                return
        except Exception:
            pass
        if callable(self._on_track_activated):
            try:
                self._on_track_activated(tr)
            except Exception:
                pass

    def _section_fill_list(self, cards, offset: int) -> None:
        """把卡片也填到列表视图（TrackItem）。"""
        from models import TrackItem
        cur = list(getattr(self._section_list_page, "_all_tracks", []) or [])
        if offset == 0:
            cur = []
        for c in (cards or []):
            if not isinstance(c, dict):
                continue
            nm = str(c.get("name", ""))
            # 登记 click（列表行点击 → 打开详情）
            click = c.get("click")
            if callable(click) and nm:
                self._section_click_map[nm] = click
            cur.append(TrackItem(
                title=nm,
                artist=str(c.get("subtitle", "") or ""),
                cover_url=str(c.get("cover_url", "") or ""),
                source_type="online",
                source_id=nm,
            ))
        self._section_list_page.set_tracks(cur)

    def _section_count(self) -> int:
        """section 网格里已有的卡片数。"""
        try:
            return self._section_grid.count()
        except Exception:
            return 0

    def _section_do_more(self) -> None:
        """滚动到底自动拉下一批（显示「正在加载更多…」提示）。"""
        if self._section_loading or self._section_exhausted:
            return
        if not callable(self._on_section_more):
            return
        self._section_loading = True
        self._section_hint_show()
        n = self._section_count()
        self._on_section_more(self._section_name, n)

    def section(self, name: str):
        """按名字取 CardSection（供外部 append）。"""
        return {
            "newest": self.newest_section,
            "random": self.random_section,
            "artists": self.artists_section,
            "rand_songs": self.random_songs_section,
        }.get(name)

    # ---- 供 window 调用 ----
    def set_playlists(self, playlists) -> None:
        try:
            self.playlist_section.set_playlists(playlists or [])
        except Exception:
            pass

    def set_newest(self, cards) -> None:
        try:
            self.newest_section.set_cards(cards or [])
        except Exception:
            pass

    def set_random(self, cards) -> None:
        try:
            self.random_section.set_cards(cards or [])
        except Exception:
            pass

    def set_artists(self, cards) -> None:
        try:
            self.artists_section.set_cards(cards or [])
        except Exception:
            pass

    def set_random_songs(self, cards) -> None:
        try:
            self.random_songs_section.set_cards(cards or [])
        except Exception:
            pass

    def show_playlist_detail(self, name: str, tracks) -> None:
        # 记住来源视图（browse / section），返回时回退到它
        try:
            cur = self._stack.get_visible_child_name()
            if cur in ("browse", "section", "result", "detail"):
                self._detail_from = cur
        except Exception:
            pass
        self._detail_title.set_text(name or "")
        self.detail_list.set_tracks(list(tracks or []))
        self._stack.set_visible_child_name("detail")

    def _detail_back(self) -> None:
        """详情返回：回到来源视图（从「更多」进来的就回「更多」）。"""
        target = getattr(self, "_detail_from", "browse") or "browse"
        if target == "detail":
            target = "browse"
        try:
            self._stack.set_visible_child_name(target)
        except Exception:
            self.show_browse()

    def show_results(self, query: str, tracks) -> None:
        tracks = list(tracks or [])
        self.result_page.set_tracks(tracks)
        self.status_lbl.set_text(
            ("%d " % len(tracks)) + _("首") if tracks else _("无结果"))
        self._stack.set_visible_child_name("result")

    def show_browse(self) -> None:
        self._stack.set_visible_child_name("browse")

    def show_loading(self) -> None:
        """显示加载中（首次进在线页/刷新时）。"""
        try:
            self._browse_stack.set_visible_child_name("loading")
            self._stack.set_visible_child_name("browse")
        except Exception:
            pass

    def show_content(self) -> None:
        """显示内容（加载完成）。"""
        try:
            self._browse_stack.set_visible_child_name("content")
        except Exception:
            pass

    def show_status(self, text: str) -> None:
        self.status_lbl.set_text(text or "")
        self._stack.set_visible_child_name("result")
