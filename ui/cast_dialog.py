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
        # UDN → ActionRow 映射：刷新时按设备身份合并，而非清空重建。
        self._rows_by_udn = {}

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
        self._scan_btn = scan_btn
        self._scanning = False
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

        # 立即显示「当前已连接设备」（若正在投送），不等扫描完成——
        # 否则开着投送时每次打开都要等 3 秒才看到已连接设备。
        self._seed_current_device()
        self._scan()

    def _seed_current_device(self) -> None:
        """把当前正在投送的设备立即填入列表（不等 SSDP 扫描）。"""
        try:
            cur = get_dlna_pusher().current_device()
            cur_udn = (cur or {}).get("udn", "")
            if not cur or not cur_udn:
                return
            udn = self._udn_of(cur)
            if udn in self._rows_by_udn:
                return
            row = self._make_row(cur, cur_udn)
            self._rows_by_udn[udn] = row
            self._list.append(row)
            self._stop_btn.set_visible(True)
        except Exception:
            log.debug("立即显示当前设备失败", exc_info=True)

    # ---- 扫描 ----
    def _scan(self) -> None:
        # 防重复触发：扫描期间禁用刷新按钮。
        if getattr(self, "_scanning", False):
            return
        self._scanning = True
        try:
            self._scan_btn.set_sensitive(False)
        except Exception:
            pass
        # 关键：不清空已有列表——扫描完成后才无缝替换。
        # 否则点刷新会先看到「已发现的设备全部消失」，等扫描结束才回来。
        self._status.set_text(_("正在扫描…"))
        import threading
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        devices = discover_renderers(timeout=3.0)
        GLib.idle_add(self._on_scan_done, devices)

    def _on_scan_done(self, devices: list) -> bool:
        self._scanning = False
        try:
            self._scan_btn.set_sensitive(True)
        except Exception:
            pass
        devices = devices or []
        self._devices = devices
        cur = get_dlna_pusher().current_device()
        cur_udn = (cur or {}).get("udn", "")

        # 按 UDN 合并：已存在的行原位更新，新设备插入，消失的设备移除。
        # 不清空重建，避免刷新时列表闪烁。
        seen = set()
        for dev in devices:
            udn = self._udn_of(dev)
            seen.add(udn)
            row = self._rows_by_udn.get(udn)
            if row is not None:
                # 已存在：更新（IP 可能变、选中态可能变）
                row._dev = dev
                row.set_title(dev.get("name", _("未知设备")))
                row.set_subtitle(dev.get("ip", ""))
                self._update_row_icon(row, dev, cur_udn)
            else:
                row = self._make_row(dev, cur_udn)
                self._rows_by_udn[udn] = row
                self._list.append(row)
                self._fade_in(row)

        # 移除本轮未出现的设备——但保留「当前正在投送的设备」：
        # 它在播放中可能不响应 SSDP，若被移除会导致开着投送却看不到设备。
        cur_key = self._udn_of(cur) if cur else ""
        for udn in list(self._rows_by_udn.keys()):
            if udn not in seen and udn != cur_key:
                row = self._rows_by_udn.pop(udn)
                try:
                    self._list.remove(row)
                except Exception:
                    pass

        if not devices:
            self._status.set_text(_("未发现设备，请确认音箱已开机并与本机同一网络"))
        else:
            self._status.set_text(_("发现 {n} 个设备").format(n=len(devices)))
        # 已选设备时显示停止按钮
        self._stop_btn.set_visible(bool(cur_udn))
        return False

    @staticmethod
    def _udn_of(dev: dict) -> str:
        """设备身份键：优先 UDN，退化为 location / ip。"""
        return (dev.get("udn") or dev.get("location")
                or dev.get("ip") or dev.get("name") or "")

    def _make_row(self, dev: dict, cur_udn: str) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(dev.get("name", _("未知设备")))
        row.set_subtitle(dev.get("ip", ""))
        row.set_activatable(True)
        row._dev = dev
        img = Gtk.Image()
        row.add_suffix(img)
        row._img = img
        self._update_row_icon(row, dev, cur_udn)
        row.connect("activated", lambda r: self._on_pick(getattr(r, "_dev", {})))
        return row

    @staticmethod
    def _update_row_icon(row, dev: dict, cur_udn: str) -> None:
        """按是否为当前选中设备，更新行尾图标。"""
        try:
            selected = bool(dev.get("udn")) and dev.get("udn") == cur_udn
            row._img.set_from_icon_name(
                "object-select-symbolic" if selected else "go-next-symbolic")
        except Exception:
            pass

    @staticmethod
    def _fade_in(row) -> None:
        """新设备淡入（adw 动画不可用时退化为直接显示）。"""
        try:
            row.set_opacity(0.0)
            anim = Adw.TimedAnimation.new(
                row, 0.0, 1.0, 160,
                Adw.PropertyAnimationTarget.new(row, "opacity"))
            anim.play()
        except Exception:
            try:
                row.set_opacity(1.0)
            except Exception:
                pass

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
