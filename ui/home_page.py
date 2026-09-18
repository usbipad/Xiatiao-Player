"""主页：三个可折叠区块——专辑 / 艺术家 / 历史。

折叠：内容区固定为一行高度（网格每行 4 个），内部可滚动看其余；
展开：高度放开，主页整体滚动看全部。点标题后的按钮切换。

每块复用 LocalLibraryPage，只负责聚合、折叠与数据分发。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from gi.repository import Gtk

from core.i18n import _

from models import TrackItem
from .pages import LocalLibraryPage, VIEW_ALBUMS, VIEW_ARTISTS, VIEW_SONGS

#: 展开后网格每行卡片数
_GRID_COLS = 4
#: 折叠时显示的行数（网格：一行）
_COLLAPSED_ROWS = 1
#: 单行高度估算（卡片 + 标题 + 间距）
_ROW_H = 210


class _CollapsibleSection(Gtk.Box):
    """可折叠区块：标题行（标题 + 按钮）+ 内容。

    折叠：内容固定为一行高度（内部可滚）；展开：放开高度。
    """

    def __init__(self, title: str, content, expanded: bool = False,
                 on_toggle=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._content = content
        self._on_toggle = on_toggle

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label=title)
        lbl.add_css_class("heading")
        lbl.set_halign(Gtk.Align.START)
        header.append(lbl)
        self._toggle_btn = Gtk.Button()
        self._toggle_btn.add_css_class("flat")
        self._toggle_btn.set_valign(Gtk.Align.CENTER)
        self._toggle_btn.set_child(Gtk.Image.new_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"))
        self._toggle_btn.connect("clicked", lambda *_: self.toggle())
        header.append(self._toggle_btn)
        self.append(header)

        self.append(content)
        # 初始状态
        self._expanded = bool(expanded)
        self._apply()

    def _apply(self) -> None:
        """按当前展开状态设置内容折叠高度与按钮图标。"""
        try:
            self._content.set_collapsed(not self._expanded, _COLLAPSED_ROWS, _ROW_H)
        except Exception:
            pass
        self._toggle_btn.set_child(Gtk.Image.new_from_icon_name(
            "pan-down-symbolic" if self._expanded else "pan-end-symbolic"))

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._apply()
        if callable(self._on_toggle):
            try:
                self._on_toggle()
            except Exception:
                pass


class HomePage(Gtk.Box):
    """主页：专辑 + 艺术家 + 历史（三个可折叠区块）。"""

    def __init__(self,
                 on_track_activated: Optional[Callable[[TrackItem], None]] = None,
                 track_actions: Optional[dict] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.set_margin_top(4)

        # ---- 1) 专辑 ----
        self.albums_page = LocalLibraryPage(
            on_track_activated=on_track_activated,
            title="",
            empty_text="曲库为空",
            track_actions=track_actions,
            initial_view=VIEW_ALBUMS,
            show_locate=False,
            hide_header=True,
        )
        self.albums_section = _CollapsibleSection(_("专辑"), self.albums_page, expanded=False)
        self.append(self.albums_section)

        # ---- 2) 艺术家 ----
        self.artists_page = LocalLibraryPage(
            on_track_activated=on_track_activated,
            title="",
            empty_text="曲库为空",
            track_actions=track_actions,
            initial_view=VIEW_ARTISTS,
            show_locate=False,
            hide_header=True,
        )
        self.artists_section = _CollapsibleSection(_("艺术家"), self.artists_page, expanded=False)
        self.append(self.artists_section)

        # ---- 3) 历史（精简：无表头、不可排序，歌名/歌手/格式/采样率）----
        self.history_page = LocalLibraryPage(
            on_track_activated=on_track_activated,
            title="",
            empty_text="还没有播放记录",
            track_actions=track_actions,
            initial_view=VIEW_SONGS,
            hide_header=True,
            no_sort=True,
            show_locate=False,
            compact_cols=[
                (_("歌名"), "title", True, "title"),
                (_("歌手"), "artist", False, "artist"),
                (_("格式"), "format_ext", False, "format_ext"),
                (_("采样率"), "sample_rate_label", False, "sample_rate"),
            ],
        )
        self.history_section = _CollapsibleSection(_("历史播放"), self.history_page, expanded=True)
        self.append(self.history_section)

    # ---- 数据入口 ----
    def set_library(self, tracks: List[TrackItem]) -> None:
        """把曲库喂给专辑/艺术家两块。"""
        try:
            self.albums_page.set_tracks(tracks)
            self.artists_page.set_tracks(tracks)
            # 网格列数按窗口宽度自适应（不固定）
            try:
                self.albums_page._grid_flow.set_max_children_per_line(9999)
                self.artists_page._grid_flow.set_max_children_per_line(9999)
            except Exception:
                pass
        except Exception:
            pass

    def set_history(self, tracks: List[TrackItem]) -> None:
        """把播放历史喂给历史块。"""
        try:
            self.history_page.set_tracks(tracks)
        except Exception:
            pass
