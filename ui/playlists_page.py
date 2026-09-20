"""歌单页：卡片网格 + 点卡片展开歌单内曲目。

- 页1：歌单卡片（FlowBox），卡片显示第一首封面 / 图标 + 名称 + 曲目数
- 页2：点卡片后展开，用 LocalLibraryPage（与曲库同款列表）显示曲目
- 空白右键 / 右上 +：新建歌单；卡片右键：播放 / 重命名 / 删除
"""
from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from core.i18n import _

from core.playlist_store import get_playlist_store
from models import TrackItem, SOURCE_LOCAL, extract_cover, make_square_cover_bytes
from ui.pages import LocalLibraryPage


def _section_title(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("heading")
    lbl.set_halign(Gtk.Align.START)
    return lbl


def _row_to_track(r: dict) -> Optional[TrackItem]:
    try:
        return TrackItem(
            title=r.get("title") or "未知歌曲",
            artist=r.get("artist") or "未知歌手",
            album=r.get("album") or "",
            duration=r.get("duration") or "0:00",
            duration_seconds=float(r.get("duration_seconds") or 0.0),
            filepath=r.get("filepath") or "",
            source_type=r.get("source_type") or SOURCE_LOCAL,
            source_id=r.get("source_id") or "",
            cover_url=r.get("cover_url") or "",
        )
    except Exception:
        return None


class PlaylistsPage(Gtk.Box):
    """歌单页（卡片网格 + 内嵌曲目列表）。"""

    def __init__(self,
                 on_playlist_play: Optional[Callable[[int], None]] = None,
                 on_track_activated: Optional[Callable[[TrackItem], None]] = None,
                 on_toast: Optional[Callable[[str], None]] = None,
                 track_actions: Optional[dict] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_play = on_playlist_play
        self._on_activated = on_track_activated
        self._toast = on_toast
        self._track_actions = track_actions
        self._current_pid = None

        # 标题行
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._back_btn.add_css_class("flat")
        self._back_btn.set_valign(Gtk.Align.CENTER)
        self._back_btn.set_visible(False)
        self._back_btn.set_tooltip_text(_("返回歌单"))
        self._back_btn.connect("clicked", lambda *_: self.show_list())
        header.append(self._back_btn)
        self._title = _section_title("歌单")
        header.append(self._title)
        header.append(Gtk.Box(hexpand=True))
        self._count_label = Gtk.Label(label="")
        self._count_label.add_css_class("dim-label")
        self._count_label.add_css_class("caption")
        header.append(self._count_label)
        self._play_all_btn = Gtk.Button(icon_name="media-playback-start-symbolic")
        self._play_all_btn.add_css_class("flat")
        self._play_all_btn.set_tooltip_text(_("播放整个歌单"))
        self._play_all_btn.set_valign(Gtk.Align.CENTER)
        self._play_all_btn.set_visible(False)
        self._play_all_btn.connect("clicked", lambda *_: self._play_all())
        header.append(self._play_all_btn)
        self._new_btn = Gtk.Button(icon_name="list-add-symbolic")
        self._new_btn.add_css_class("flat")
        self._new_btn.set_tooltip_text(_("新建歌单"))
        self._new_btn.set_valign(Gtk.Align.CENTER)
        self._new_btn.connect("clicked", lambda *_: self._create_playlist())
        header.append(self._new_btn)
        self.append(header)

        self._empty_label = Gtk.Label(label=_("还没有歌单，点右上角 + 或右键新建"))
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_halign(Gtk.Align.START)
        self._empty_label.set_margin_top(12)
        self.append(self._empty_label)

        # Stack：页1 卡片网格 / 页2 曲目列表
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)
        try:
            self._stack.set_vhomogeneous(False)
            self._stack.set_hhomogeneous(False)
        except Exception:
            pass

        # 页1：卡片网格
        self._flow = Gtk.FlowBox()
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_max_children_per_line(9999)
        self._flow.set_min_children_per_line(2)
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(False)
        self._flow.set_row_spacing(12)
        self._flow.set_column_spacing(12)
        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        grid_scroll.set_child(self._flow)
        self._stack.add_named(grid_scroll, "cards")

        # 歌单专属 actions：从列表移除 = 从歌单删除；且不含「删除文件」
        page_actions = dict(track_actions or {})
        page_actions["remove_from_list"] = self._remove_track_from_playlist
        page_actions.pop("delete_file", None)

        # 页2：曲目列表（曲库同款）
        self._track_page = LocalLibraryPage(
            on_track_activated=on_track_activated,
            title="",
            empty_text="此歌单暂无歌曲",
            hide_header=True,
            track_actions=page_actions,
        )
        self._stack.add_named(self._track_page, "tracks")
        self._stack.set_visible_child_name("cards")
        self.append(self._stack)

        # 空白右键（卡片页）：新建
        try:
            rclick = Gtk.GestureClick()
            rclick.set_button(3)
            rclick.connect("pressed", self._on_blank_right_click)
            self._flow.add_controller(rclick)
        except Exception:
            pass

        self.refresh()

    # ---- 数据 ----
    def refresh(self) -> None:
        if self._current_pid is None:
            self.show_list()
        else:
            self._open_playlist(self._current_pid, self._title.get_text())

    def show_list(self) -> None:
        """显示歌单卡片网格。"""
        self._current_pid = None
        self._back_btn.set_visible(False)
        self._play_all_btn.set_visible(False)
        self._new_btn.set_visible(True)
        self._title.set_text(_("歌单"))
        self._stack.set_visible_child_name("cards")
        # 清空卡片
        c = self._flow.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._flow.remove(c)
            c = nxt
        try:
            playlists = get_playlist_store().all_playlists()
        except Exception:
            playlists = []
        self._empty_label.set_text(_("还没有歌单，点右上角 + 或右键新建"))
        self._empty_label.set_visible(len(playlists) == 0)
        self._count_label.set_text(f"{len(playlists)} {_('个')}" if playlists else "")
        for pl in playlists:
            self._flow.append(self._make_card(pl))

    def _open_playlist(self, pid: int, name: str) -> None:
        """展开歌单：切到曲目列表页。"""
        self._current_pid = pid
        self._back_btn.set_visible(True)
        self._play_all_btn.set_visible(True)
        self._new_btn.set_visible(False)
        self._title.set_text(name or _("未命名歌单"))
        self._empty_label.set_visible(False)
        try:
            rows = get_playlist_store().rows_of(pid)
        except Exception:
            rows = []
        tracks = []
        for r in rows:
            t = _row_to_track(r)
            if t is not None:
                tracks.append(t)
        self._count_label.set_text(f"{len(tracks)} {_('首')}" if tracks else "")
        try:
            self._track_page.set_tracks(tracks)
        except Exception:
            pass
        self._stack.set_visible_child_name("tracks")

    def _remove_track_from_playlist(self, track) -> None:
        """从当前歌单中移除某曲目（真正删库），并刷新视图。"""
        try:
            pid = self._current_pid
            if pid is None or track is None:
                return
            key = (getattr(track, "filepath", "") or getattr(track, "source_id", ""))
            if not key:
                return
            get_playlist_store().remove_track(pid, key)
            if self._toast:
                self._toast(_("已从歌单移除：{title}").format(title=getattr(track, 'title', '')))
            # 刷新歌单详情
            self._open_playlist(pid, self._title.get_text())
        except Exception:
            pass

    # ---- 卡片 ----
    def _make_card(self, pl: dict) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(150, -1)
        box.set_valign(Gtk.Align.START)
        box.set_halign(Gtk.Align.START)
        try:
            box.set_hexpand(False)
        except Exception:
            pass
        # 封面框
        frame = Gtk.Frame()
        frame.add_css_class("media-card-cover")
        frame.set_size_request(150, 150)
        pic = Gtk.Picture()
        pic.set_content_fit(Gtk.ContentFit.COVER)
        pic.set_size_request(150, 150)
        tex = self._first_cover_texture(pl.get("id"))
        if tex is not None:
            pic.set_paintable(tex)
            frame.set_child(pic)
        else:
            ph = Gtk.Image.new_from_icon_name("view-list-symbolic")
            ph.set_pixel_size(48)
            ph.add_css_class("media-card-placeholder")
            frame.set_child(ph)
        box.append(frame)
        lbl = Gtk.Label(label=pl.get("name") or _("未命名歌单"))
        lbl.set_ellipsize(3)
        lbl.set_max_width_chars(18)
        lbl.add_css_class("media-card-title")
        box.append(lbl)
        sub = Gtk.Label(label=f"{pl.get('cnt', 0)} {_('首')}")
        sub.add_css_class("media-card-subtitle")
        box.append(sub)
        # 点击 → 展开
        click = Gtk.GestureClick()
        click.connect("released", lambda _g, _n, _x, _y, p=pl: self._open_playlist(p["id"], p.get("name", "")))
        box.add_controller(click)
        box.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        # 右键 → 菜单
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", lambda g, n, x, y, p=pl: self._card_menu(box, p, x, y))
        box.add_controller(rclick)
        return box

    def _first_cover_texture(self, pid):
        """取歌单第一首的封面纹理（无则 None）。"""
        try:
            rows = get_playlist_store().rows_of(pid)
            if not rows:
                return None
            fp = rows[0].get("filepath") or ""
            if not fp:
                return None
            raw = extract_cover(fp)
            if not raw:
                return None
            png = make_square_cover_bytes(raw, 150, radius=10)
            return Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
        except Exception:
            return None

    def _card_menu(self, box, pl: dict, x: float, y: float) -> None:
        menu = Gio.Menu()
        menu.append(_("播放"), "card.play")
        menu.append(_("重命名…"), "card.rename")
        menu.append(_("删除歌单"), "card.delete")
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.add_css_class("media-menu")
        pop.set_parent(box)
        pop.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        ag = Gio.SimpleActionGroup()
        a0 = Gio.SimpleAction.new("play", None)
        a0.connect("activate", lambda *_: self._play_playlist(pl.get("id")))
        a1 = Gio.SimpleAction.new("rename", None)
        a1.connect("activate", lambda *_: self._rename_playlist(pl.get("id"), pl.get("name")))
        a2 = Gio.SimpleAction.new("delete", None)
        a2.connect("activate", lambda *_: self._delete_playlist(pl.get("id"), pl.get("name")))
        ag.add_action(a0)
        ag.add_action(a1)
        ag.add_action(a2)
        box.insert_action_group("card", ag)
        pop.popup()

    # ---- 交互 ----
    def _play_all(self) -> None:
        try:
            if self._current_pid is not None:
                self._play_playlist(self._current_pid)
        except Exception:
            pass

    def _play_playlist(self, pid) -> None:
        try:
            if callable(self._on_play) and pid is not None:
                self._on_play(int(pid))
        except Exception:
            pass

    def _on_blank_right_click(self, gesture, _n, x, y) -> None:
        if self._stack.get_visible_child_name() != "cards":
            return
        menu = Gio.Menu()
        menu.append(_("新建歌单"), "pl.new")
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.add_css_class("media-menu")
        pop.set_parent(self._flow)
        pop.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)
        ag = Gio.SimpleActionGroup()
        a = Gio.SimpleAction.new("new", None)
        a.connect("activate", lambda *_: self._create_playlist())
        ag.add_action(a)
        self._flow.insert_action_group("pl", ag)
        pop.popup()

    # ---- 操作 ----
    def _create_playlist(self) -> None:
        self._prompt_name("新建歌单", "新歌单", lambda name: self._do_create(name))

    def _do_create(self, name: str) -> None:
        try:
            pid = get_playlist_store().create(name)
            if pid is not None and self._toast:
                self._toast(_("已创建歌单：{name}").format(name=name))
        except Exception:
            pass
        self.show_list()

    def _rename_playlist(self, pid, old_name) -> None:
        self._prompt_name("重命名歌单", old_name or "", lambda name: self._do_rename(pid, name))

    def _do_rename(self, pid, name: str) -> None:
        try:
            get_playlist_store().rename(pid, name)
        except Exception:
            pass
        self.show_list()

    def _delete_playlist(self, pid, name) -> None:
        try:
            get_playlist_store().delete(pid)
            if self._toast:
                self._toast(_("已删除歌单：{name}").format(name=name))
        except Exception:
            pass
        self.show_list()

    def _prompt_name(self, title: str, default: str, on_ok) -> None:
        dlg = Adw.Dialog()
        dlg.set_title(title)
        dlg.set_content_width(360)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        entry = Gtk.Entry()
        entry.set_text(default)
        entry.set_activates_default(True)
        box.append(entry)
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label=_("取消"))
        cancel.connect("clicked", lambda *_: dlg.close())
        ok = Gtk.Button(label=_("确定"))
        ok.add_css_class("suggested-action")

        def _ok(*_a):
            name = entry.get_text().strip()
            dlg.close()
            if name:
                on_ok(name)
        ok.connect("clicked", _ok)
        btns.append(cancel)
        btns.append(ok)
        box.append(btns)
        dlg.set_child(box)
        dlg.present(self)
