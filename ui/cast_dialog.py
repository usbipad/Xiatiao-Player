"""投送对话框：扫描并选择局域网 DLNA 设备，把当前曲目推过去播放。"""
from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from core.dlna_push import discover_renderers, get_dlna_pusher
from core.i18n import _

log = logging.getLogger(__name__)


class CastDialog(Adw.Dialog):
    """投送设备选择对话框。"""

    def __init__(self, parent, current_track, on_toast=None,
                 on_cast_started=None, on_cast_stopped=None) -> None:
        super().__init__()
        self._parent = parent
        self._track = current_track
        self._on_toast = on_toast
        self._on_cast_started = on_cast_started
        self._on_cast_stopped = on_cast_stopped
        self._devices = []
        self._rows = []

        self.set_title(_("投送到设备"))
        self.set_content_width(420)
        self.set_content_height(480)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        # 扫描按钮
        scan_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        scan_btn.set_tooltip_text(_("重新扫描"))
        scan_btn.connect("clicked", lambda *_: self._scan())
        header.pack_start(scan_btn)
        toolbar.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)

        # 当前推送状态提示
        self._status = Gtk.Label(label=_("正在扫描…"))
        self._status.add_css_class("dim-label")
        self._status.set_halign(Gtk.Align.CENTER)
        body.append(self._status)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        body.append(self._list)
        toolbar.set_content(body)
        self.set_child(toolbar)

        # 若已选设备，先显示停止按钮
        self._stop_btn = Gtk.Button(label=_("停止投送"))
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.connect("clicked", lambda *_: self._stop())
        self._stop_btn.set_visible(False)
        body.append(self._stop_btn)

        self._scan()

    # ---- 扫描 ----
    def _scan(self) -> None:
        self._status.set_text(_("正在扫描…"))
        self._clear_rows()
        import threading
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        devices = discover_renderers(timeout=3.0)
        GLib.idle_add(self._on_scan_done, devices)

    def _on_scan_done(self, devices: list) -> bool:
        self._devices = devices or []
        self._clear_rows()
        if not devices:
            self._status.set_text(_("未发现设备，请确认音箱已开机并与本机同一网络"))
            return False
        self._status.set_text(_("发现 {n} 个设备").format(n=len(devices)))
        cur = get_dlna_pusher().current_device()
        cur_udn = (cur or {}).get("udn", "")
        for dev in devices:
            row = Adw.ActionRow()
            row.set_title(dev.get("name", _("未知设备")))
            row.set_subtitle(dev.get("ip", ""))
            row.set_activatable(True)
            if dev.get("udn") and dev.get("udn") == cur_udn:
                row.add_suffix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            else:
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, d=dev: self._on_pick(d))
            self._list.append(row)
            self._rows.append(row)
        # 已选设备时显示停止按钮
        self._stop_btn.set_visible(bool(cur_udn))
        return False

    def _clear_rows(self) -> None:
        for r in self._rows:
            self._list.remove(r)
        self._rows = []

    # ---- 推送 ----
    def _on_pick(self, dev: dict) -> None:
        if self._track is None:
            self._toast(_("当前没有可投送的曲目"))
            return
        filepath = getattr(self._track, "filepath", "") or ""
        if not filepath:
            self._toast(_("仅支持投送本地曲目"))
            return
        pusher = get_dlna_pusher()
        pusher.set_device(dev)
        title = getattr(self._track, "title", "") or ""
        artist = getattr(self._track, "artist", "") or ""
        ok = pusher.push(filepath, title, artist)
        if ok:
            if callable(self._on_cast_started):
                self._on_cast_started()
            self._toast(_("已投送到「{name}」").format(name=dev.get("name", "")))
            self.close()
        else:
            self._toast(_("投送失败，请重试"))

    def _stop(self) -> None:
        pusher = get_dlna_pusher()
        pusher.stop()
        pusher.set_device({})
        if callable(self._on_cast_stopped):
            self._on_cast_stopped()
        self._toast(_("已停止投送"))
        self.close()

    def _toast(self, msg: str) -> None:
        if callable(self._on_toast):
            try:
                self._on_toast(msg)
            except Exception:
                pass
