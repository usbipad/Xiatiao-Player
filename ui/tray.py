"""系统托盘（StatusNotifierItem / AppIndicator），纯 D-Bus 实现。

不依赖 GTK3（Ayatana 需 GTK3，与应用的 GTK4 冲突）。
直接实现两个 D-Bus 接口，由桌面环境（GNOME 的 AppIndicator 扩展 /
KDE Plasma / XFCE 等）接管显示：

- org.kde.StatusNotifierItem：托盘图标与状态；
- com.canonical.dbusmenu：右键菜单。

注册流程：连接会话总线 → 导出上述两个对象 → 调用
org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem 让面板接管。

所有 GTK4 侧回调由外部注入（见 Tray.__init__）。
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional, Tuple

from gi.repository import Gio, GLib

from core.i18n import _

log = logging.getLogger(__name__)

IFACE_SNI = "org.kde.StatusNotifierItem"
IFACE_MENU = "com.canonical.dbusmenu"
IFACE_WATCHER = "org.kde.StatusNotifierWatcher"

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <method name="Activate">
      <arg direction="in" type="i" name="x"/>
      <arg direction="in" type="i" name="y"/>
    </method>
    <method name="SecondaryActivate">
      <arg direction="in" type="i" name="x"/>
      <arg direction="in" type="i" name="y"/>
    </method>
    <method name="ContextMenu">
      <arg direction="in" type="i" name="x"/>
      <arg direction="in" type="i" name="y"/>
    </method>
    <method name="Scroll">
      <arg direction="in" type="i" name="delta"/>
      <arg direction="in" type="s" name="orientation"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewStatus">
      <arg type="s" name="status"/>
    </signal>
    <signal name="NewToolTip"/>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg direction="in" type="i" name="parentId"/>
      <arg direction="in" type="i" name="recursionDepth"/>
      <arg direction="in" type="as" name="propertyNames"/>
      <arg direction="out" type="u" name="revision"/>
      <arg direction="out" type="(ia{sv}av)" name="layout"/>
    </method>
    <method name="GetGroupProperties">
      <arg direction="in" type="ai" name="ids"/>
      <arg direction="in" type="as" name="propertyNames"/>
      <arg direction="out" type="a(ia{sv})" name="properties"/>
    </method>
    <method name="GetProperty">
      <arg direction="in" type="i" name="id"/>
      <arg direction="in" type="s" name="name"/>
      <arg direction="out" type="v" name="value"/>
    </method>
    <method name="Event">
      <arg direction="in" type="i" name="id"/>
      <arg direction="in" type="s" name="eventId"/>
      <arg direction="in" type="v" name="data"/>
      <arg direction="in" type="u" name="timestamp"/>
    </method>
    <method name="EventGroup">
      <arg direction="in" type="a(isvu)" name="events"/>
      <arg direction="out" type="ai" name="idErrors"/>
    </method>
    <method name="AboutToShow">
      <arg direction="in" type="i" name="id"/>
      <arg direction="out" type="b" name="needUpdate"/>
    </method>
    <method name="AboutToShowGroup">
      <arg direction="in" type="ai" name="ids"/>
      <arg direction="out" type="ai" name="updatesNeeded"/>
      <arg direction="out" type="ai" name="idErrors"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
  </interface>
</node>
"""


class Tray:
    """系统托盘（StatusNotifierItem）。

    通过回调与主窗口交互（不直接持有 GTK 控件）。
    """

    #: 菜单项：(id, 中文标签, 回调 key)
    def __init__(
        self,
        on_play_pause: Callable[[], None],
        on_prev: Callable[[], None],
        on_next: Callable[[], None],
        on_show_window: Callable[[], None],
        on_settings: Callable[[], None],
        on_about: Callable[[], None],
        on_quit: Callable[[], None],
        title: str = "Xiatiao",
        icon_name: str = "xiatiao",
    ) -> None:
        self._cb = {
            "play_pause": on_play_pause,
            "prev": on_prev,
            "next": on_next,
            "show": on_show_window,
            "settings": on_settings,
            "about": on_about,
            "quit": on_quit,
        }
        self._title = title
        self._icon_name = icon_name
        self._conn: Optional[Gio.DBusConnection] = None
        self._reg_ids: List[int] = []
        self._bus_id = 0
        self._playing = False
        self._revision = 1

    def start(self) -> bool:
        """连接总线、导出对象、注册到 watcher。成功返回 True。"""
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as exc:
            log.warning("托盘：连接会话总线失败: %s", exc)
            return False
        try:
            sni_info = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).lookup_interface(IFACE_SNI)
            menu_info = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).lookup_interface(IFACE_MENU)
            self._reg_ids.append(self._conn.register_object(
                SNI_PATH, sni_info, self._on_sni_call, self._on_sni_get, None))
            self._reg_ids.append(self._conn.register_object(
                MENU_PATH, menu_info, self._on_menu_call, self._on_menu_get, None))
        except Exception as exc:
            log.warning("托盘：导出 D-Bus 对象失败: %s", exc)
            return False
        # own 一个唯一总线名（SNI 标准：面板按此名找到本项）
        import os
        bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        try:
            self._bus_id = Gio.bus_own_name_on_connection(
                self._conn, bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        except Exception as exc:
            log.info("托盘：own 总线名失败: %s", exc)
        # 注册到 watcher（面板据此接管显示）
        try:
            self._conn.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                IFACE_WATCHER,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (bus_name,)),
                None, Gio.DBusCallFlags.NONE, 2000, None,
            )
            log.info("托盘：已注册 StatusNotifierItem（%s）", bus_name)
        except Exception as exc:
            log.info("托盘：未找到 StatusNotifierWatcher（%s），图标可能不显示", exc)
        return True

    def stop(self) -> None:
        for rid in self._reg_ids:
            try:
                self._conn.unregister_object(rid)
            except Exception:
                pass
        self._reg_ids = []
        self._conn = None

    def set_playing(self, playing: bool) -> None:
        """更新播放状态（影响菜单「播放/暂停」文本）。"""
        self._playing = bool(playing)
        self._revision += 1
        self._emit_layout_updated()

    def _emit_layout_updated(self) -> None:
        try:
            if self._conn is not None:
                self._conn.emit_signal(
                    None, MENU_PATH, IFACE_MENU, "LayoutUpdated",
                    GLib.Variant("(ui)", (self._revision, 0)))
        except Exception:
            pass

    # ------------------------------------------------------------
    # SNI：方法 / 属性
    # ------------------------------------------------------------
    def _on_sni_call(self, _c, _s, _p, _i, method, _params, inv):
        try:
            if method in ("Activate", "SecondaryActivate"):
                self._fire("show")
            elif method == "ContextMenu":
                # 由面板自行弹出菜单，无需处理
                pass
            elif method == "Scroll":
                delta, orientation = _params.unpack()
                self._fire("next" if delta < 0 else "prev")
        except Exception:
            log.debug("托盘：SNI 方法 %s 失败", method, exc_info=True)
        inv.return_value(None)

    def _on_sni_get(self, _c, _s, _p, _i, prop):
        if prop == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if prop == "Id":
            return GLib.Variant("s", "xiatiao")
        if prop == "Title":
            return GLib.Variant("s", self._title)
        if prop == "Status":
            return GLib.Variant("s", "Active")
        if prop == "IconName":
            return GLib.Variant("s", self._icon_name)
        if prop == "IconThemePath":
            return GLib.Variant("s", "")
        if prop == "Menu":
            return GLib.Variant("o", MENU_PATH)
        if prop == "ItemIsMenu":
            return GLib.Variant("b", False)
        if prop == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", (self._icon_name, [], self._title, ""))
        return None

    # ------------------------------------------------------------
    # dbusmenu
    # ------------------------------------------------------------
    #: 菜单布局：id → (标签, 回调 key, 是否分隔符)
    def _menu_items(self) -> List[Tuple[int, str, Optional[str], bool]]:
        play_label = _("暂停") if self._playing else _("播放")
        return [
            (1, play_label, "play_pause", False),
            (2, _("上一首"), "prev", False),
            (3, _("下一首"), "next", False),
            (4, "", None, True),
            (5, _("显示主窗口"), "show", False),
            (6, _("设置"), "settings", False),
            (7, _("关于"), "about", False),
            (8, "", None, True),
            (9, _("退出"), "quit", False),
        ]

    def _layout(self) -> GLib.Variant:
        """构造 dbusmenu 布局 variant，类型严格为 (ia{sv}av)。"""
        children = []
        for mid, label, _key, sep in self._menu_items():
            if sep:
                props = {"type": GLib.Variant("s", "separator")}
            else:
                props = {
                    "label": GLib.Variant("s", label),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True),
                }
            # 每个子项：id + 属性字典 + 空子菜单(av)
            children.append(GLib.Variant("(ia{sv}av)", (mid, props, [])))
        root_props = {"children-display": GLib.Variant("s", "submenu")}
        return GLib.Variant("(ia{sv}av)", (0, root_props, children))

    def _on_menu_call(self, _c, _s, _p, _i, method, params, inv):
        try:
            if method == "GetLayout":
                log.info("托盘：GetLayout 被调用")
                # 用 new_tuple 显式组合，避免把已打包的 variant 当普通值再序列化
                layout = GLib.Variant.new_tuple(
                    GLib.Variant("u", self._revision),
                    self._layout(),
                )
                inv.return_value(layout)
                return
            if method == "GetGroupProperties":
                inv.return_value(GLib.Variant("a(ia{sv})", []))
                return
            if method == "GetProperty":
                inv.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
                return
            if method == "Event":
                mid, event_id, _data, _ts = params.unpack()
                if event_id == "clicked":
                    self._handle_menu_event(int(mid))
                inv.return_value(None)
                return
            if method == "EventGroup":
                inv.return_value(GLib.Variant("(ai)", ([],)))
                return
            if method == "AboutToShow":
                inv.return_value(GLib.Variant("(b)", (False,)))
                return
            if method == "AboutToShowGroup":
                inv.return_value(GLib.Variant("(aiai)", ([], [])))
                return
        except Exception:
            log.debug("托盘：dbusmenu 方法 %s 失败", method, exc_info=True)
        inv.return_value(None)

    def _on_menu_get(self, _c, _s, _p, _i, prop):
        if prop == "Version":
            return GLib.Variant("u", 3)
        if prop == "TextDirection":
            return GLib.Variant("s", "ltr")
        if prop == "Status":
            return GLib.Variant("s", "normal")
        if prop == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def _handle_menu_event(self, mid: int) -> None:
        for iid, _label, key, sep in self._menu_items():
            if iid == mid and not sep and key:
                self._fire(key)
                return

    def _fire(self, key: str) -> None:
        cb = self._cb.get(key)
        if callable(cb):
            try:
                cb()
            except Exception:
                log.debug("托盘：回调 %s 失败", key, exc_info=True)
