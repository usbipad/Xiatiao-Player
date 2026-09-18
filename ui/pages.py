"""本地曲库页面。

- LocalLibraryPage：本地曲库 ColumnView（歌名/歌手/时长/封面）。

所有页面只消费 TrackItem 列表，不直接接触 Provider 内部实现。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from gi.repository import Adw, Gdk, GLib, Gio, GObject, Gtk

from core.i18n import _

from core.tasks import run_async
from models import TrackItem, extract_cover, make_square_cover_bytes
from ui.widgets.playing_indicator import (
    PlayingIndicator,
    set_current_key as set_playing_key,
)

#: 列表小封面缓存：filepath -> Gdk.Texture（None 表示无封面）。
#  用 OrderedDict 实现 LRU 上限：长列表滚动时不会无限积累纹理（纹理占内存/显存）。
from collections import OrderedDict as _OrderedDict

_COVER_CACHE: "_OrderedDict" = _OrderedDict()
_COVER_CACHE_MAX = 512
_COVER_SIZE = 40
#: 播放指示器尺寸（正方形）
_INDICATOR_SIZE = 14
#: 正在读取中的 filepath → 等待该结果的 list_item 列表。
#  同一首歌可能同时出现在多个列表（曲库/历史/歌单），若只用集合去重，
#  后续列表会因 "已在加载中" 直接 return，永远拿不到封面。
#  这里登记所有等待者，加载完成后统一回填。
_COVER_LOADING: dict = {}


class _CoverActivity(GObject.Object):
    """封面加载活动通知器（模块级单例）。

    启动遮罩（splash）需要等首屏封面都加载完再淡出。这里在「是否有封面
    正在加载」状态翻转时发 busy-changed 信号，window 据此判断是否就绪。
    """

    __gtype_name__ = "CoverActivity"

    __gsignals__ = {
        # 是否忙（有封面在加载）变化，param: bool
        "busy-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def _refresh(self) -> None:
        busy = bool(_COVER_LOADING)
        if busy != self._busy:
            self._busy = busy
            try:
                self.emit("busy-changed", busy)
            except Exception:
                pass

    def mark_busy(self) -> None:
        self._refresh()

    def mark_idle(self) -> None:
        self._refresh()


#: 全局封面活动单例
cover_activity = _CoverActivity()


def _add_hwheel_scroll(scroll: Gtk.ScrolledWindow) -> None:
    """给横向 ScrolledWindow 挂滚轮：普通滚轮（竖向）也驱动横向滚动。

    GTK4 默认滚轮只驱动竖向滚动；横向容器需要手动把 dy 映射到 hadjustment。
    """
    try:
        def _on_scroll(_ctrl, _dx, dy):
            try:
                adj = scroll.get_hadjustment()
                if adj is None:
                    return False
                step = adj.get_step_increment() or 40.0
                adj.set_value(adj.get_value() + (dy * -step if dy else 0))
                return True
            except Exception:
                return False

        ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        ctrl.connect("scroll", _on_scroll)
        scroll.add_controller(ctrl)
    except Exception:
        pass


def _cache_get(key):
    """取缓存并把命中的键移到末尾（LRU）。未命中返回 _MISS。"""
    if key in _COVER_CACHE:
        try:
            _COVER_CACHE.move_to_end(key)
        except Exception:
            pass
        return _COVER_CACHE[key]
    return _MISS


def _cache_put(key, value) -> None:
    """写缓存并淘汰最久未用的项。"""
    _COVER_CACHE[key] = value
    try:
        _COVER_CACHE.move_to_end(key)
    except Exception:
        pass
    while len(_COVER_CACHE) > _COVER_CACHE_MAX:
        try:
            _COVER_CACHE.popitem(last=False)
        except Exception:
            break


class _Miss:
    """缓存未命中的哨兵（区别于缓存值 None=无封面）。"""


_MISS = _Miss()


#: 视图模式
VIEW_SONGS = "songs"
VIEW_ALBUMS = "albums"
VIEW_ARTISTS = "artists"


def _load_cover_bytes(filepath: str):
    """后台线程：读取并缩放封面，返回 PNG bytes 或 None（不碰 UI）。"""
    raw = extract_cover(filepath)
    if not raw:
        return None
    try:
        return make_square_cover_bytes(raw, _COVER_SIZE, radius=6)
    except Exception:
        return None


# ================================================================
# 通用工具
# ================================================================
def _section_title(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("heading")
    label.set_halign(Gtk.Align.START)
    return label


def _h_scroll_cards(items) -> Gtk.ScrolledWindow:
    """items: [(标题, 宽, 高, 封面字节 or None), ...]"""
    scroll = Gtk.ScrolledWindow(hexpand=True)
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    h = (items[0][2] + 50) if items else 220
    scroll.set_size_request(-1, h)
    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    for title, w, height, cover in items:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        frame = Gtk.Frame()
        frame.add_css_class("card")
        frame.set_size_request(w, height)
        if cover:
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(cover))
                pic = Gtk.Picture()
                pic.set_paintable(texture)
                pic.set_content_fit(Gtk.ContentFit.COVER)
                frame.set_child(pic)
            except Exception:
                pass
        card.append(frame)
        lbl = Gtk.Label(label=title)
        lbl.add_css_class("caption")
        lbl.set_ellipsize(3)
        card.append(lbl)
        hbox.append(card)
    scroll.set_child(hbox)
    return scroll


# ================================================================
# Home 页面（静态占位）
# ================================================================
def _hint_label(text: str) -> Gtk.Label:
    """板块内的浅色提示标签（加载中 / 空态）。"""
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    label.add_css_class("caption")
    label.set_halign(Gtk.Align.START)
    label.set_margin_start(4)
    return label


class LocalLibraryPage(Gtk.Box):
    """展示曲目列表，点击触发回调。本地/在线页面共用。"""

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
        # 统一在此翻译：调用方传中文原文（或空串），避免默认参数里调 _()
        self._title = _(title) if title else ""
        self._empty_text = _(empty_text) if empty_text else ""
        self._track_actions = track_actions
        self._store = Gio.ListStore(item_type=TrackItem)
        # 排序模型：ColumnView 的列头 sorter 会设到这里，真正生效排序
        self._sort_model = Gtk.SortListModel(model=self._store)
        self._selection = Gtk.SingleSelection(model=self._sort_model)
        self._selection.connect("selection-changed", self._on_selection_changed)

        # 原始完整列表（未过滤），用于搜索时还原/过滤
        self._all_tracks: List[TrackItem] = []
        self._filter_text = ""
        # 多选模式：选中键集合 + 复选框列引用
        self._multi_mode = False
        self._selected_keys: set = set()
        self._check_col = None
        # 当前视图：songs / albums / artists（由 initial_view 决定；导航控制）
        self._view_mode = initial_view if initial_view in (VIEW_SONGS, VIEW_ALBUMS, VIEW_ARTISTS) else VIEW_SONGS
        # 专辑/艺术家二次过滤（点卡片进入后，限定该专辑/艺术家）
        self._group_filter = ""

        # 标题行：标题 + 右侧刷新按钮 + 计数
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(_section_title(self._title))
        # 进入某专辑/艺术家后显示「返回」图标按钮
        self._back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._back_btn.add_css_class("flat")
        self._back_btn.set_valign(Gtk.Align.CENTER)
        self._back_btn.set_tooltip_text(_("返回"))
        self._back_btn.set_visible(False)
        self._back_btn.connect("clicked", lambda *_: self._clear_group_filter())
        header.append(self._back_btn)
        header.append(Gtk.Box(hexpand=True))   # 占位把右侧推远
        self._count_label = Gtk.Label(label="")
        self._count_label.add_css_class("dim-label")
        self._count_label.add_css_class("caption")
        header.append(self._count_label)
        # 多选模式切换按钮
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
        # 「加入歌单」按钮（多选且有选中时显示）
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
        self.append(header)

        self._empty_label = Gtk.Label(label=empty_text)
        self._empty_label.add_css_class("dim-label")
        self.append(self._empty_label)

        # 内容区：用 Stack 在「歌曲列表」和「专辑/艺术家网格」之间切换
        self._content_stack = Gtk.Stack()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        # 关键：非均匀高度 → Stack 高度取「当前可见页」而非最大页。
        # 否则折叠切到矮的横向页后，Stack 仍按高网格撑高，把下方区块推远。
        try:
            self._content_stack.set_vhomogeneous(False)
            self._content_stack.set_hhomogeneous(False)
        except Exception:
            pass
        # 内嵌模式（主页）：不 vexpand，高度按内容，由外层滚动；
        # 独立页：vexpand 撑满，自带滚动。
        self._content_stack.set_vexpand(not embedded)

        # 当前播放引用：指示器列据此显示跳动指示器（含已登记实例表）
        self._now_playing_ref = {"key": "", "indicators": []}
        self._column_view = _build_track_columnview(
            self._selection, track_actions=track_actions,
            on_row_click=self._on_row_clicked,
            now_playing_ref=self._now_playing_ref,
            hide_header=hide_header,
            no_sort=no_sort,
            compact_cols=compact_cols,
            embedded=embedded,
        )
        self._content_stack.add_named(self._column_view, "songs")

        # 专辑/艺术家网格（FlowBox）
        self._grid_flow = Gtk.FlowBox()
        self._grid_flow.set_valign(Gtk.Align.START)
        self._grid_flow.set_max_children_per_line(9999)  # 按窗口宽度自适应列数
        self._grid_flow.set_min_children_per_line(2)
        self._grid_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        # homogeneous=False：卡片保持固定宽（150px），由容器宽度决定每行放几个
        self._grid_flow.set_homogeneous(False)
        self._grid_flow.add_css_class("media-flow")
        self._grid_flow.set_row_spacing(8)
        self._grid_flow.set_column_spacing(8)
        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_child(self._grid_flow)
        grid_scroll.set_vexpand(not embedded)
        if embedded:
            # 内嵌模式：不滚动、按内容高度（由外层滚动）
            grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
            self._grid_flow.set_valign(Gtk.Align.START)
        else:
            grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._grid_scroll = grid_scroll
        # 折叠状态（网格视图折叠时显示横向一行）
        self._collapsed = False
        # 网格卡片随机轮播定时器（专辑/艺术家块）
        self._grid_rotate_timers = []
        self._grid_groups = {}
        self._rotate_guard_id = None
        self._content_stack.add_named(grid_scroll, "grid")

        # 折叠用：横向一行 + 横向滚动（滚轮左右滑）
        self._hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._hbox.set_valign(Gtk.Align.START)
        h_scroll = Gtk.ScrolledWindow()
        h_scroll.set_child(self._hbox)
        h_scroll.set_vexpand(False)
        h_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self._h_scroll = h_scroll
        self._content_stack.add_named(h_scroll, "grid_h")

        # 当前播放曲目（用于「定位当前播放」按钮）
        self._now_playing_key = ""

        # 用 Overlay 承载内容区，右下角叠加「定位当前播放」悬浮按钮
        self._content_overlay = Gtk.Overlay()
        # 内嵌模式（主页）不 vexpand，按内容高度，避免被窗口拉伸
        self._content_overlay.set_vexpand(not embedded)
        self._content_overlay.set_child(self._content_stack)
        self._locate_btn = Gtk.Button(icon_name="find-location-symbolic")
        self._locate_btn.add_css_class("locate-fab")
        self._locate_btn.set_tooltip_text(_("定位当前播放"))
        self._locate_btn.set_halign(Gtk.Align.END)
        self._locate_btn.set_valign(Gtk.Align.END)
        self._locate_btn.set_margin_end(18)
        self._locate_btn.set_margin_bottom(18)
        # 放大内部图标（默认 symbolic 图标偏小）
        try:
            _img = self._locate_btn.get_child()
            if _img is not None:
                _img.set_pixel_size(20)
        except Exception:
            pass
        self._locate_btn.connect("clicked", lambda *_: self._locate_now_playing())
        # 定位按钮仅曲库/收藏页需要；主页等不显示
        self._locate_btn.set_visible(bool(show_locate))
        self._content_overlay.add_overlay(self._locate_btn)

        self.append(self._content_overlay)
        # 让 initial_view 立即生效（否则 content_stack 默认显示第一个加的 songs）
        self._apply_filter()

    # ------------------------------------------------------------
    # 数据入口
    # ------------------------------------------------------------
    def set_tracks(self, tracks: List[TrackItem]) -> None:
        # 记录完整列表，set_tracks 始终视为“新数据源”，重按当前关键词过滤
        self._all_tracks = list(tracks or [])
        self._apply_filter()

    def set_filter_text(self, text: str) -> None:
        """外部（HeaderBar 搜索框）设置过滤关键词。"""
        self._filter_text = (text or "").strip()
        self._apply_filter()

    # ------------------------------------------------------------
    # 多选模式
    # ------------------------------------------------------------
    def _on_multi_toggled(self, btn) -> None:
        """多选模式开/关：显示/隐藏复选框列，清空选中。"""
        on = bool(btn.get_active())
        self._multi_mode = on
        self._selected_keys.clear()
        try:
            cv = getattr(self._column_view, "_column_view", None)
            if cv is not None:
                if on and self._check_col is None:
                    # 在最前插入复选框列
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
        """构造复选框列（多选模式用）。"""
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
            cb.handler_block_by_func(self._on_check_toggled) if False else None
            try:
                cb.set_active(key in self._selected_keys)
            except Exception:
                pass
            # 断开旧连接（避免复用行时重复）
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
        """复选框勾选变化。"""
        try:
            if cb.get_active():
                self._selected_keys.add(key)
            else:
                self._selected_keys.discard(key)
            self._update_multi_count()
        except Exception:
            pass

    def _update_multi_count(self) -> None:
        """更新标题计数与「加入歌单」按钮显隐（多选模式）。"""
        try:
            if self._multi_mode:
                n = len(self._selected_keys)
                import sys as _sys
                print(f"[multi] mode={self._multi_mode} n={n} cb={callable(self._on_add_to_playlist)}", file=_sys.stderr, flush=True)
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
                # 恢复普通计数
                self._apply_filter()
        except Exception:
            pass

    def _on_add_pl_clicked(self, _btn) -> None:
        """点击「加入歌单」：把选中曲目交给回调。"""
        try:
            tracks = self.selected_tracks()
            if tracks and callable(self._on_add_to_playlist):
                self._on_add_to_playlist(tracks)
        except Exception:
            pass

    def selected_tracks(self) -> list:
        """返回当前选中的曲目列表（多选模式）。"""
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
        """设置歌曲列表（历史等）滚动窗高度（px<=0 表示放开）。"""
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
        """折叠/展开内容区。

        collapsed=True：卡片横向排成一行（不换行）+ 横向滚动（滚轮左右滑），
                       固定一行高度；
        collapsed=False：恢复正常网格（每行 cols 个），竖向由外层滚动看全部。
        """
        try:
            self._collapsed = bool(collapsed)
            h = int(row_h) if collapsed else -1
            gs = getattr(self, "_grid_scroll", None)
            flow = getattr(self, "_grid_flow", None)
            hs = getattr(self, "_h_scroll", None)
            if collapsed:
                # 折叠：切到横向页（一行 + 横向滚动），固定一行高
                try:
                    if self._view_mode in (VIEW_ALBUMS, VIEW_ARTISTS):
                        self._content_stack.set_visible_child_name("grid_h")
                except Exception:
                    pass
                if hs is not None:
                    hs.set_size_request(-1, h)
            else:
                # 展开：切回网格页，高度放开。
                # 不限制每行数量 → FlowBox 按窗口宽度自适应列数。
                if flow is not None:
                    try:
                        flow.set_max_children_per_line(9999)
                        flow.set_min_children_per_line(1)
                    except Exception:
                        pass
                try:
                    if self._view_mode in (VIEW_ALBUMS, VIEW_ARTISTS):
                        self._content_stack.set_visible_child_name("grid")
                except Exception:
                    pass
                if gs is not None:
                    gs.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
                    gs.set_size_request(-1, -1)
            # 歌曲列表（历史）：折叠时保持竖向，仅限制高度
            ls = getattr(self, "_column_view", None)
            if ls is not None:
                if collapsed:
                    ls.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                    ls.set_size_request(-1, h)
                else:
                    ls.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
                    ls.set_size_request(-1, -1)
            # 强制重新布局：切页/改高度后 Stack 可能仍按旧尺寸，
            # 导致下方区块被挤没。queue_resize 让本控件重算高度。
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
        """记录当前正在播放的曲目。

        用于：
        - 「定位当前播放」按钮；
        - 列表行前的跳动播放指示器。
        更新后触发列表重绑定，使指示器即时切换。
        """
        if track is None:
            key = ""
        else:
            key = (getattr(track, "filepath", "") or getattr(track, "source_id", "")
                   or getattr(track, "title", ""))
            key = key or ""
        self._now_playing_key = key
        # 同步到指示器读取的引用 + 模块级当前键（指示器每帧自比对）
        try:
            self._now_playing_ref["key"] = key
        except Exception:
            pass
        try:
            set_playing_key(key)
        except Exception:
            pass

    def _locate_now_playing(self) -> None:
        """滚动列表到当前播放行，并短暂高亮。

        - 仅在「歌曲列表」视图且当前播放曲目存在于过滤后列表时有效；
        - 找不到时给出轻提示（不弹窗）。
        """
        # 专辑/艺术家网格视图下无「行」概念，先切回歌曲视图
        try:
            if self._view_mode != VIEW_SONGS:
                self._content_stack.set_visible_child_name("songs")
        except Exception:
            pass
        key = getattr(self, "_now_playing_key", "") or ""
        if not key:
            return
        # 在当前列表模型里找 position
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
        # 滚动到该行：ColumnView 继承 ListView.scroll_to
        try:
            cv = getattr(self._column_view, "_column_view", None)
            if cv is not None:
                try:
                    cv.scroll_to(position, None, Gtk.ListScrollFlags.NONE, None)
                except Exception:
                    # 旧签名兜底
                    cv.scroll_to(position, None, Gtk.ListScrollFlags.NONE)
        except Exception:
            pass
        # 短暂高亮：给该行加 locate-flash 类，600ms 后移除
        try:
            row = None
            n = self._store.get_n_items()
            # 通过 selection 模型取不到行控件，改为遍历 ColumnView 可见子行不易；
            # 简化处理：仅滚动定位，不强制闪烁（避免依赖内部结构）。
        except Exception:
            pass

    # ------------------------------------------------------------
    # 视图切换
    # ------------------------------------------------------------
    def set_view_mode(self, mode: str) -> None:
        """外部（导航）切换视图：songs / albums / artists。"""
        if mode not in (VIEW_SONGS, VIEW_ALBUMS, VIEW_ARTISTS):
            return
        self._view_mode = mode
        self._group_filter = ""
        self._back_btn.set_visible(False)
        self._apply_filter()

    def _clear_group_filter(self) -> None:
        """返回全部：清过滤，并恢复进组前的视图（网格）。"""
        self._group_filter = ""
        # 恢复进组前的视图（专辑/艺术家网格）
        self._view_mode = getattr(self, "_view_before_group", VIEW_SONGS) or VIEW_SONGS
        self._back_btn.set_visible(False)
        self._apply_filter()

    def _enter_group(self, group_name: str) -> None:
        """点专辑/艺术家卡片：切到歌曲视图并过滤该组（记住原视图以便返回）。"""
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
            # 空值在网格里显示为「未知专辑 / 未知艺术家」，过滤时映射回去
            album_key = album or "未知专辑"
            artist_key = artist or "未知艺术家"
            if kw and not (kw in title or kw in artist or kw in album):
                continue
            if gf and not (gf == artist_key or gf == album_key):
                continue
            out.append(t)
        return out

    def remove_track(self, track) -> bool:
        """从当前列表数据中移除某曲目并刷新显示（不影响曲库/文件）。

        返回是否成功移除。基于 source_id / filepath 匹配。
        """
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
        """按当前关键词/视图刷新。"""
        tracks = self._filtered_tracks()
        if self._view_mode == VIEW_SONGS:
            self._content_stack.set_visible_child_name("songs")
            self._render_tracks(tracks)
        else:
            # 折叠时显示横向一行（grid_h），否则网格（grid）
            self._content_stack.set_visible_child_name(
                "grid_h" if getattr(self, "_collapsed", False) else "grid")
            self._render_grid(tracks)
        # 计数
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
        """把给定曲目写入列表模型（屏蔽 selection 副作用）。"""
        self._suppress_selection = True
        try:
            # 用 splice 一次批量替换：逐个 append 会每项触发一次列表变化 →
            # ColumnView 反复 relayout（几百首 → 几百次重排，最大化/恢复被拖慢）。
            self._store.splice(0, self._store.get_n_items(), tracks)
            self._selection.set_selected(Gtk.INVALID_LIST_POSITION)
        finally:
            self._suppress_selection = False

        self._empty_label.set_visible(len(tracks) == 0)

    def _render_grid(self, tracks: List[TrackItem]) -> None:
        """按专辑/艺术家分组，渲染网格卡片（同时填网格与横向容器）。"""
        # 重建期间隐藏容器：GTK 在隐藏时跳过部分 layout，减少中间 relayout。
        try:
            self._grid_flow.set_visible(False)
            self._hbox.set_visible(False)
        except Exception:
            pass
        # 清空网格
        c = self._grid_flow.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._grid_flow.remove(c)
            c = nxt
        # 清空横向容器
        try:
            hc = self._hbox.get_first_child()
            while hc is not None:
                nxt = hc.get_next_sibling()
                self._hbox.remove(hc)
                hc = nxt
        except Exception:
            pass
        # 分组
        groups: dict = {}
        for t in tracks:
            if self._view_mode == VIEW_ALBUMS:
                key = (getattr(t, "album", "") or "未知专辑").strip() or "未知专辑"
            else:
                key = (getattr(t, "artist", "") or "未知艺术家").strip() or "未知艺术家"
            groups.setdefault(key, []).append(t)
        self._empty_label.set_visible(len(groups) == 0)
        # 清理上一次的轮播定时器（重建后旧定时器失效）
        self._clear_grid_rotate_timers()
        self._grid_groups = dict(groups)
        group_items = list(groups.items())
        idx = 0
        for name in sorted(groups.keys(), key=lambda s: s.lower()):
            items = groups[name]
            card = self._make_group_card(name, items)
            self._grid_flow.append(card)
            hcard = None
            try:
                # 横向容器里放同款卡片（折叠时横向一行滚动）
                hcard = self._make_group_card(name, items)
                self._hbox.append(hcard)
            except Exception:
                hcard = None
            # 仅给折叠态（横向容器）前若干个卡片启动随机轮播（控制性能）
            # 展开态网格卡片不轮播；受设置开关控制。
            if idx < 12 and hcard is not None and self._card_rotate_enabled():
                self._schedule_card_rotate(hcard, group_items)
            idx += 1
        # 重建完成：显示容器（一次性 layout）
        try:
            self._grid_flow.set_visible(True)
            self._hbox.set_visible(True)
        except Exception:
            pass
        # 守护定时器：开关关闭时立即停轮播
        self._start_rotate_guard()

    def _clear_grid_rotate_timers(self) -> None:
        """清理网格卡片轮播定时器。"""
        try:
            for tid in getattr(self, "_grid_rotate_timers", []):
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
        except Exception:
            pass
        self._grid_rotate_timers = []
        # 同时清守护定时器
        try:
            gid = getattr(self, "_rotate_guard_id", None)
            if gid is not None:
                GLib.source_remove(gid)
        except Exception:
            pass
        self._rotate_guard_id = None

    def _start_rotate_guard(self) -> None:
        """守护定时器：每秒检查轮播开关。

        - 关：立即清所有轮播定时器（停止轮播）；
        - 开（从关变开）：重新启动折叠卡片的轮播。
        守护本身持续运行，不随开关关闭而退出。
        """
        if getattr(self, "_rotate_guard_id", None) is not None:
            return
        self._guard_last_on = self._card_rotate_enabled()
        self._guard_last_speed = self._card_rotate_speed()

        def _tick():
            try:
                on = self._card_rotate_enabled()
                was = getattr(self, "_guard_last_on", on)
                spd = self._card_rotate_speed()
                spd_was = getattr(self, "_guard_last_speed", spd)
                if not on and was:
                    # 关：停轮播
                    self._clear_grid_rotate_timers()
                    self._rotate_guard_id = GLib.timeout_add(1000, _tick)
                    self._guard_last_on = False
                    self._guard_last_speed = spd
                    return False
                if on and not was:
                    # 开：重启轮播
                    self._restart_card_rotate()
                elif on and spd != spd_was:
                    # 速度变化：清旧定时器 + 重启（用新速度）
                    self._clear_grid_rotate_timers()
                    self._restart_card_rotate()
                    self._rotate_guard_id = GLib.timeout_add(1000, _tick)
                    self._guard_last_on = True
                    self._guard_last_speed = spd
                    return False
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
        """重新启动折叠卡片的随机轮播（开关从关变开时调用）。"""
        try:
            group_items = list(getattr(self, "_grid_groups", {}).items())
            if not group_items:
                return
            idx = 0
            c = self._hbox.get_first_child()
            while c is not None and idx < 12:
                self._schedule_card_rotate(c, group_items)
                c = c.get_next_sibling()
                idx += 1
        except Exception:
            pass

    @staticmethod
    def _card_rotate_enabled() -> bool:
        """是否启用主页卡片轮播（读设置）。"""
        try:
            from config.settings import get_config
            return get_config().get_bool("card_rotate_enabled", True)
        except Exception:
            return True

    @staticmethod
    def _card_rotate_speed() -> str:
        """读取当前轮播速度档（slow/medium/fast）。"""
        try:
            from config.settings import get_config
            return get_config().get_str("card_rotate_speed", "medium")
        except Exception:
            return "medium"

    @staticmethod
    def _card_rotate_range() -> tuple:
        """轮播延时范围（毫秒），按设置的速度档。

        slow: 15~30 秒 / medium: 10~20 秒 / fast: 5~15 秒
        """
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
        """给单个卡片安排一次随机延时的换组（范围按设置速度档）。"""
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
        """把一个卡片随机换成另一个组的内容，并重新安排下一次。

        每次执行前检查开关；关闭则不换、也不再安排下一次。
        """
        if not self._card_rotate_enabled():
            return
        try:
            import random as _r
            if not group_items:
                return
            cur = getattr(card, "_group_name", None)
            # 随机选一个不同的组
            candidates = [g for g in group_items if g[0] != cur] or group_items
            name, items = _r.choice(candidates)
            apply_fn = getattr(card, "_apply_group", None)
            if callable(apply_fn):
                apply_fn(name, items)
        except Exception:
            pass
        # 继续下一次（仍受开关控制）
        try:
            self._schedule_card_rotate(card, group_items)
        except Exception:
            pass

    def _make_group_card(self, name: str, items: List[TrackItem]) -> Gtk.Widget:
        """专辑/艺术家卡片：封面 + 名称 + 曲目数。固定尺寸，不随窗口拉伸。"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(150, -1)
        box.set_valign(Gtk.Align.START)
        box.set_halign(Gtk.Align.START)
        # 禁止卡片随容器拉伸：宽固定、不 hexpand
        try:
            box.set_hexpand(False)
            box.set_vexpand(False)
        except Exception:
            pass
        # 封面：取该组第一首的封面
        cover_frame = Gtk.Frame()
        cover_frame.add_css_class("media-card-cover")
        cover_frame.set_size_request(150, 150)
        cover_frame.set_halign(Gtk.Align.START)
        try:
            cover_frame.set_hexpand(False)
        except Exception:
            pass
        first = items[0]
        # 封面异步加载：先占位，后台读完填（避免 320 个专辑同步读封面卡主线程）
        pic = Gtk.Picture()
        pic.set_content_fit(Gtk.ContentFit.COVER)
        pic.set_size_request(150, 150)
        _path = getattr(first, "filepath", "") or ""
        _cached = _cache_get(f"{_path}@150") if _path else _MISS
        if _cached is not _MISS and _cached is not None:
            pic.set_paintable(_cached)
            cover_frame.set_child(pic)
        else:
            ph = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
            ph.set_pixel_size(48)
            ph.add_css_class("media-card-placeholder")
            cover_frame.set_child(ph)
            if _path:
                self._load_grid_cover_async(f"{_path}@150", cover_frame, pic)
        # 两页 Stack：换内容时旧页滑出、新页滑入（无缝）
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(600)
        # 页 A（首屏显示当前内容）
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
        # 页 B（隐藏，换内容时填它再切过去）
        inner_b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        pic_b = Gtk.Picture()
        pic_b.set_content_fit(Gtk.ContentFit.COVER)
        pic_b.set_size_request(150, 150)
        cover_b = Gtk.Frame()
        cover_b.add_css_class("media-card-cover")
        cover_b.set_size_request(150, 150)
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
        stack.add_named(inner_a, "a")
        stack.add_named(inner_b, "b")
        stack.set_visible_child_name("a")
        box.append(stack)
        box._stack = stack
        # 卡片元数据 + 两页控件引用
        box._group_name = name
        box._items = list(items)
        box._pages = {
            "a": {"cover": cover_frame, "pic": pic, "name": lbl, "sub": sub},
            "b": {"cover": cover_b, "pic": pic_b, "name": lbl_b, "sub": sub_b},
        }
        box._cur_page = "a"
        # 点击卡片 → 进入「当前」组（可被 _rotate_card 更新）
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a, b=box: self._enter_group(getattr(b, "_group_name", "")))
        box.add_controller(click)
        box.set_cursor(Gdk.Cursor.new_from_name("pointer", None))

        def _fill_page(page_key: str, new_name: str, new_items) -> None:
            """把指定页填成新分组内容。"""
            try:
                page = box._pages.get(page_key)
                if page is None or not new_items:
                    return
                page["name"].set_text(new_name)
                page["sub"].set_text(f"{len(new_items)} {_('首')}")
                first_t = new_items[0]
                path = getattr(first_t, "filepath", "") or ""
                # 先占位
                try:
                    ph = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
                    ph.set_pixel_size(48)
                    ph.add_css_class("media-card-placeholder")
                    page["cover"].set_child(ph)
                except Exception:
                    pass
                if path:
                    _c = _cache_get(f"{path}@150")
                    if _c is not _MISS and _c is not None:
                        try:
                            page["pic"].set_paintable(_c)
                            page["cover"].set_child(page["pic"])
                        except Exception:
                            pass
                    else:
                        self._load_grid_cover_async(f"{path}@150", page["cover"], page["pic"])
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
                # 随机过渡方向（左右 / 上下）
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

    def _load_grid_cover_async(self, path: str, frame, pic, cache_key: str = None) -> None:
        """后台读大封面（150px），读完在主线程填进 frame。

        cache_key 为空时用 f"{path}@150"（与列表小封面 40px 缓存区分开，
        否则网格会复用 40px 缓存导致封面糊）。
        """
        real_path = path
        if path and path.endswith("@150"):
            real_path = path[:-4]
        ckey = cache_key or f"{real_path}@150"

        def _work():
            try:
                raw = extract_cover(real_path)
                if not raw:
                    return None
                return make_square_cover_bytes(raw, 150, radius=10)
            except Exception:
                return None

        def _done(png):
            if not png:
                return
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
                _cache_put(ckey, tex)
                # 占位换成图片
                if frame.get_child() is not pic:
                    pic.set_paintable(tex)
                    frame.set_child(pic)
            except Exception:
                pass

        try:
            run_async(work=_work, on_done=_done)
        except Exception:
            pass

    def _cover_texture_for(self, track: TrackItem):
        """取曲目封面纹理（同步读本地内嵌封面；无则 None）。"""
        path = getattr(track, "filepath", "") or ""
        if not path:
            return None
        cached = _cache_get(path)
        if cached is not _MISS:
            return cached
        raw = extract_cover(path)
        if not raw:
            _cache_put(path, None)
            return None
        try:
            png = make_square_cover_bytes(raw, 150, radius=10)
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
            _cache_put(path, tex)
            return tex
        except Exception:
            _cache_put(path, None)
            return None

    def _do_refresh(self) -> None:
        if callable(self._on_refresh):
            self._on_refresh()

    def _on_selection_changed(self, selection, _position, _n_items) -> None:
        # 选中不再触发播放（避免自动选中/重复点击问题），仅用于高亮。
        return

    def _on_row_clicked(self, item) -> None:
        """单击某行：触发播放回调（本地/在线列表通用）。"""
        if item is not None and callable(self._on_activated):
            self._on_activated(item)


# ================================================================
# ColumnView 构建（统一入口）
# ================================================================
def _build_track_columnview(model, on_activate=None,
                            track_actions=None,
                            on_row_click=None,
                            now_playing_ref=None,
                            hide_header=False,
                            no_sort=False,
                            compact_cols=None,
                            embedded=False) -> Gtk.ScrolledWindow:
    """model 可为 Gio.ListStore 或 Gtk.SelectionModel。

    track_actions: 可选 dict，提供歌曲右键菜单动作：
        {play, play_next, append, copy, search_artist}
    on_row_click: 可选回调，单击某行时以该行 TrackItem 调用。
    now_playing_ref: 可选 dict（如 {"key": "..."}），记录当前播放曲目键；
        封面列据此显示跳动指示器。
    hide_header: 隐藏列头（精简列表模式）。
    no_sort: 禁用列头排序。
    compact_cols: 指定显示的文本列，如 [("歌名","title",True,"title"), ...]；
        None 时用默认完整列。
    """
    if isinstance(model, Gio.ListStore):
        model = Gtk.SingleSelection(model=Gtk.SortListModel(model=model))
    column_view = Gtk.ColumnView(model=model)
    column_view.set_vexpand(not embedded)
    # 列头点击排序：GTK4 的 ColumnView 自带一个 ColumnViewSorter（cv.get_sorter()）。
    # 点击列头时该 sorter 变化并 emit "changed"；把它同步给底层 SortListModel 即生效。
    try:
        _sel_model = model
        _sort_model = _sel_model.get_model() if hasattr(_sel_model, "get_model") else None
        if isinstance(_sort_model, Gtk.SortListModel):
            _cv_sorter = column_view.get_sorter()

            def _sync_sorter(*_a):
                # 1) 清除选中：否则排序后 GTK 会把「选中行」滚到可见区（导致跳到末尾）
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
    # 单击即触发 activate（默认是双击才 activate、单击仅选中）。
    # 这样点一下歌曲就播放，不必双击。
    try:
        column_view.set_single_click_activate(True)
    except Exception:
        pass
    # 不再加 .card：避免列表区域套一层与外部不同色的"框"。
    # 列表整体透明，仅靠行级 hover/选中高亮区分（现代媒体库风格）。
    column_view.add_css_class("track-list")
    # 整行单击：用 ColumnView 自带的 activate（整行级、命中准确），
    # 替代过去"逐单元格挂手势"的做法——后者在行边缘会跨行错位，
    # 导致"高亮到 A 行、实际播放 B 行"的 bug。
    if on_row_click is not None:
        def _on_row_activate(_cv, position):
            try:
                item = model.get_item(position)
            except Exception:
                item = None
            if item is not None:
                on_row_click(item)
        column_view.connect("activate", _on_row_activate)

    # 歌曲右键菜单：动作挂 ColumnView，菜单在单元格右键处弹出
    menu_model = None
    ctx = None
    if track_actions:
        menu_model, ctx = _build_track_menu(column_view, track_actions)

    # 封面列（最前，懒加载 + 缓存）
    cover_factory = Gtk.SignalListItemFactory()

    def _cover_setup_cb(_f, list_item):
        _on_cover_setup(_f, list_item)
        # 把当前播放引用挂到 list_item，供 bind 判断
        if now_playing_ref is not None:
            list_item._now_playing_ref = now_playing_ref

    cover_factory.connect("setup", _cover_setup_cb)
    cover_factory.connect("bind", _on_cover_bind)
    if menu_model is not None:
        cover_factory.connect("bind", _attach_cell_right_click, menu_model, ctx)
    cover_col = Gtk.ColumnViewColumn(title="", factory=cover_factory)
    cover_col.set_fixed_width(_COVER_SIZE + 16)
    column_view.append_column(cover_col)

    def _append_text_column(title, prop, expand, sort_key, numeric=False, width=0, sortable=True):
        """追加一个普通文本列（歌名 / 歌手 / 格式 / 时长）。

        排序用 GTK4 内置 StringSorter / NumericSorter（签名确定、可靠），
        避免 CustomSorter 回调签名不匹配导致点列头排序失效。
        """
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", _on_factory_setup)
        factory.connect("bind", _on_factory_bind, prop)
        if menu_model is not None:
            factory.connect("bind", _attach_cell_right_click, menu_model, ctx)
        col = Gtk.ColumnViewColumn(title=title, factory=factory)
        col.set_expand(expand)
        if width > 0:
            col.set_fixed_width(width)
        # 排序：统一用 CustomSorter（支持拼音/格式分类/数值逻辑）
        if sortable:
            try:
                col.set_sorter(Gtk.CustomSorter.new(_make_sort_func(sort_key), None))
            except Exception:
                pass
        column_view.append_column(col)

    # 列顺序：默认 [封面] [歌名] [歌手] [播放指示器] [格式] [采样率]
    # compact_cols 指定时只显示这些文本列（精简模式，如历史块）。
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
        ind_col.set_fixed_width(_INDICATOR_SIZE + 16)
        column_view.append_column(ind_col)

    if compact_cols is not None:
        # 精简模式：按指定列构建（不可排序）；在「歌手」之后插入指示器列，
        # 使其位置与曲库列表一致。
        for i, (title, prop, expand, sort_key) in enumerate(compact_cols):
            _append_text_column(title, prop, expand, sort_key, sortable=False)
            if i == 1:
                _append_indicator_column()
        # 若列数不足 2，则追加到末尾
        if len(compact_cols) < 2:
            _append_indicator_column()
    else:
        _append_text_column(_("歌名"), "title", True, "title", sortable=_sort_on)
        _append_text_column(_("歌手"), "artist", False, "artist", sortable=_sort_on)
        # 播放指示器列（位于「歌手」与「采样率」之间）
        _append_indicator_column()
        # 音频格式列（只显示扩展名，如 FLAC / MP3 / APE）
        _append_text_column(_("格式"), "format_ext", False, "format_ext", width=90, sortable=_sort_on)
        # 采样率列（如 44.1kHz / 96kHz）
        _append_text_column(_("采样率"), "sample_rate_label", False, "sample_rate", numeric=True, width=100, sortable=_sort_on)

    # 隐藏表头（精简模式）
    if hide_header:
        try:
            column_view.add_css_class("no-header")
        except Exception:
            pass

    scroll = Gtk.ScrolledWindow()
    scroll.set_child(column_view)
    if embedded:
        # 内嵌模式：不滚动、按内容高度（由外层滚动）
        scroll.set_vexpand(False)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
    else:
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    # 供外部（定位当前播放按钮）拿到 ColumnView 以滚动
    scroll._column_view = column_view
    return scroll


# ================================================================
# 排序
# ================================================================
#: 中文拼音首字母近似映射（常用字；未覆盖的按 Unicode 排）
#  说明：不引入 pypinyin 依赖，用 Unicode 序作为稳定回退。

def _text_sort_key(s: str) -> str:
    """文本排序键：小写；中文按 Unicode（与英文分别聚拢）。"""
    return (s or "").strip().lower()


def _make_sort_func(key: str):
    """返回 Gtk.CustomSorter 的比较函数（签名 (a, b, user_data) -> int）。

    - duration_seconds / sample_rate：按数值
    - format_ext：按格式名（同格式聚拢，再按字母）
    - title / artist：按文本（小写；中文按 Unicode）
    """
    def _cmp(a, b, _user_data=None):
        if a is None and b is None:
            return 0
        if a is None:
            return -1
        if b is None:
            return 1
        # 数值列
        if key in ("duration_seconds", "sample_rate", "bitrate", "bit_depth", "channels"):
            va = getattr(a, key, 0) or 0
            vb = getattr(b, key, 0) or 0
            try:
                fa, fb = float(va), float(vb)
            except (TypeError, ValueError):
                fa, fb = 0.0, 0.0
            return -1 if fa < fb else (1 if fa > fb else 0)
        # 文本列
        if key == "format_ext":
            va = (getattr(a, "format_ext", "") or "")
            vb = (getattr(b, "format_ext", "") or "")
        else:
            va = str(getattr(a, key, "") or "")
            vb = str(getattr(b, key, "") or "")
        sa, sb = _text_sort_key(va), _text_sort_key(vb)
        return -1 if sa < sb else (1 if sa > sb else 0)
    return _cmp


# ================================================================
# 列表单元格 / 右键菜单 / 封面懒加载
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
    # ---- 现代播放器常见功能 ----
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


def _on_factory_setup(_factory, list_item) -> None:
    label = Gtk.Label(xalign=0)
    label.set_ellipsize(3)
    label.set_margin_start(8)
    label.set_margin_end(8)
    # 撑满单元格高度：命中区覆盖整行（文字靠自身基线，label 填满单元格）
    label.set_vexpand(True)
    label.set_valign(Gtk.Align.FILL)
    list_item.set_child(label)


def _on_factory_bind(_factory, list_item, prop: str) -> None:
    item = list_item.get_item()
    label = list_item.get_child()
    label.set_text(getattr(item, prop, ""))


def _attach_cell_click(_factory, list_item, on_row_click) -> None:
    """给单元格挂单击手势：点击时用该行 TrackItem 触发回调。

    手势挂在「单元格容器」(list_item) 而非其 child 上：child 常有内边距/
    内容高度小于行高，导致行的上下边缘点击落不到手势上（点击不生效）。
    挂到容器可覆盖整行高度。
    """
    # 让 child 撑满单元格高度：child 撑满后，手势挂在 child 上即可覆盖整行，
    # 解决行上下边缘（选中高亮区）点击不生效的问题。
    child = list_item.get_child()
    if child is None:
        return
    try:
        child.set_vexpand(True)
        child.set_valign(Gtk.Align.FILL)
    except Exception:
        pass
    if getattr(child, "_lclick", None) is not None:
        return

    def on_click(gesture, n_press, x, y):
        item = list_item.get_item()
        if item is not None:
            on_row_click(item)

    g = Gtk.GestureClick()
    g.set_button(1)
    # 用 pressed（按下即触发），避免 Gtk.GestureClick 等双击判定造成延迟。
    try:
        g.set_exclusive(True)
    except Exception:
        pass
    g.connect("pressed", on_click)
    child.add_controller(g)
    child._lclick = g


def _on_cover_setup(_factory, list_item) -> None:
    pic = Gtk.Picture()
    pic.set_content_fit(Gtk.ContentFit.COVER)
    pic.set_size_request(_COVER_SIZE, _COVER_SIZE)
    pic.set_can_shrink(True)
    pic.set_margin_start(8)
    pic.set_margin_top(4)
    pic.set_margin_bottom(4)
    pic.add_css_class("cover-img")
    # 撑满单元格高度，命中区覆盖整行
    pic.set_vexpand(True)
    pic.set_valign(Gtk.Align.FILL)

    # 用 Overlay 在封面右下角叠加音质徽标（默认隐藏，bind 时按需显示）
    overlay = Gtk.Overlay()
    overlay.set_size_request(_COVER_SIZE, _COVER_SIZE)
    overlay.set_margin_start(8)
    overlay.set_margin_top(4)
    overlay.set_margin_bottom(4)
    overlay.set_vexpand(True)
    overlay.set_valign(Gtk.Align.FILL)
    # 图片作为主 child；徽标作为 overlay child 定位右下角
    pic.set_margin_start(0)
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
    indicator = PlayingIndicator(width=_INDICATOR_SIZE, height=_INDICATOR_SIZE)
    indicator.set_halign(Gtk.Align.CENTER)
    indicator.set_valign(Gtk.Align.CENTER)
    indicator.set_visible(False)
    list_item.set_child(indicator)
    list_item._indicator = indicator


def _on_indicator_bind(_factory, list_item) -> None:
    """指示器列：记录本行键，显隐由指示器自身每帧比对（见 PlayingIndicator）。"""
    item = list_item.get_item()
    indicator = getattr(list_item, "_indicator", None)
    if indicator is None:
        return
    row_key = ""
    if item is not None:
        row_key = (getattr(item, "filepath", "") or getattr(item, "source_id", "")
                   or getattr(item, "title", ""))
    # 设置行键，并立即同步一次显隐（定时器之外，bind 后即时正确）
    indicator.set_row_key(row_key)
    try:
        indicator.sync_now()
    except Exception:
        pass


def _on_cover_bind(_factory, list_item) -> None:
    item = list_item.get_item()
    overlay = list_item.get_child()
    # child 是 Overlay；主 child 为 Gtk.Picture。
    if isinstance(overlay, Gtk.Overlay):
        pic = overlay.get_child()
    else:
        pic = overlay
        overlay = None
    if pic is None:
        return
    pic.set_paintable(None)

    # 音质徽标（Hi-Res / CD）：按当前曲目音频规格显示/隐藏
    try:
        badge = getattr(list_item, "_cover_badge", None)
        if badge is not None:
            label = ""
            if item is not None:
                label = getattr(item, "quality_badge", "") or ""
            badge.set_text(label)
            badge.set_visible(bool(label))
            # 按级别切换配色：DSD 紫色 / HR 金色 / CD 蓝色
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

    # 在线曲目：自带封面字节，直接显示（缓存 key 用 source_id）
    raw = getattr(item, "cover_bytes", None)
    filepath = getattr(item, "filepath", "")
    if raw:
        key = getattr(item, "source_id", "") or str(id(item))
        cached = _cache_get(key)
        if cached is not _MISS:
            pic.set_paintable(cached)
            return
        try:
            fixed = make_square_cover_bytes(raw, _COVER_SIZE, radius=6)
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(fixed))
            _cache_put(key, tex)
            pic.set_paintable(tex)
        except Exception:
            pass
        return

    # 在线曲目：有 cover_url 则异步下载（缓存 key 用 URL）
    cover_url = getattr(item, "cover_url", "")
    if cover_url:
        cached = _cache_get(cover_url)
        if cached is not _MISS:
            pic.set_paintable(cached)
            return
        if cover_url in _COVER_LOADING:
            _COVER_LOADING[cover_url].append((list_item, pic, "url"))
            return
        _COVER_LOADING[cover_url] = []
        cover_activity.mark_busy()

        def on_img(raw) -> None:
            waiters = _COVER_LOADING.pop(cover_url, [])
            cover_activity.mark_idle()
            tex = None
            if raw:
                try:
                    fixed = make_square_cover_bytes(raw, _COVER_SIZE, radius=6)
                    tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(fixed))
                except Exception:
                    tex = None
            _cache_put(cover_url, tex)
            cur = list_item.get_item()
            if cur is not None and getattr(cur, "cover_url", "") == cover_url:
                pic.set_paintable(tex)
            # 回填所有等待同一 URL 的其它列表项
            for w_item, w_pic, _kind in waiters:
                try:
                    wc = w_item.get_item()
                    if wc is not None and getattr(wc, "cover_url", "") == cover_url:
                        w_pic.set_paintable(tex)
                except Exception:
                    pass

        def work_img():
            from providers.qq_api import fetch_image_bytes
            return fetch_image_bytes(cover_url)

        run_async(work=work_img, on_done=on_img)
        return

    # 本地曲目：按 filepath 后台读内嵌封面
    if not filepath:
        return
    cached = _cache_get(filepath)
    if cached is not _MISS:
        pic.set_paintable(cached)
        return
    if filepath in _COVER_LOADING:
        _COVER_LOADING[filepath].append((list_item, pic, "file"))
        return
    _COVER_LOADING[filepath] = []
    cover_activity.mark_busy()

    def on_done(png_bytes) -> None:
        waiters = _COVER_LOADING.pop(filepath, [])
        cover_activity.mark_idle()
        tex = None
        if png_bytes:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_bytes))
            except Exception:
                tex = None
        _cache_put(filepath, tex)
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
