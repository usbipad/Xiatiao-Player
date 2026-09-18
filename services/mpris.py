"""MPRIS2 D-Bus 服务：让桌面媒体控件 / 媒体键 / 锁屏控制本播放器。

实现 org.mpris.MediaPlayer2 与 org.mpris.MediaPlayer2.Player 两个接口。
用 Gio.DBus（GLib 自带，无额外依赖）。

- 方法：Play/Pause/PlayPause/Stop/Next/Previous/Seek/SetPosition
- 属性：PlaybackStatus/Metadata/Position/CanPlay/CanPause/CanGoNext/...
- 信号：PropertiesChanged（状态 / 元数据变化时发）、Seeked

用法（在 window 里）：
    self._mpris = MprisService(self.player, self.playlist)
    self._mpris.set_raise_callback(self._mpris_raise)
    self._mpris.set_quit_callback(self._mpris_quit)
    self._mpris.start()
"""
from __future__ import annotations

import hashlib
import logging
from typing import Callable, Optional

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

#: D-Bus 总线名（org.mpris.MediaPlayer2.<name>）
BUS_NAME = "org.mpris.MediaPlayer2.xiatiao"
#: 对象路径
OBJ_PATH = "/org/mpris/MediaPlayer2"
#: 根接口
IFACE_ROOT = "org.mpris.MediaPlayer2"
#: 播放接口
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
#: Properties 接口
IFACE_PROPS = "org.freedesktop.DBus.Properties"

#: 无曲目时的假 trackid
_NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
#: 本应用的桌面入口名（与 .desktop 文件名 / APP_ID 对应）
DESKTOP_ENTRY = "com.xiatiao.player"
IDENTITY = "Xiatiao"


# ---- D-Bus 接口 XML ----
_INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" type="x" name="Offset"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" type="o" name="TrackId"/>
      <arg direction="in" type="x" name="Position"/>
    </method>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
    <signal name="Seeked">
      <arg type="x" name="Position"/>
    </signal>
  </interface>
</node>
"""


def _valid_object_path_segment(text: str) -> str:
    """把任意字符串转成合法的 D-Bus object path 段。

    规范：object path 各段只能含 [A-Za-z0-9_]，且不能以数字开头。
    """
    raw = "".join(
        c if (c.isascii() and (c.isalnum() or c == "_")) else "_"
        for c in str(text or "")
    )
    if not raw:
        raw = "track"
    # 段首不能是数字
    if raw[0].isdigit():
        raw = "t_" + raw
    return raw[:120]


class MprisService:
    """把 PlayerCore / Playlist 暴露为 MPRIS2 服务。

    不持有 GUI；只转发控制与状态。信号在主线程（GTK 主循环）里发。
    """

    def __init__(self, player, playlist) -> None:
        self._player = player
        self._playlist = playlist
        self._conn: Optional[Gio.DBusConnection] = None
        self._reg_id = 0
        self._duration = 0.0  # 当前曲目时长（秒）
        self._last_pos = 0.0
        # 当前封面的本地文件路径（用于 mpris:artUrl）
        self._cover_path: Optional[str] = None
        # 外部回调（可选）：窗口 Raise / 应用 Quit
        self._raise_cb: Optional[Callable[[], None]] = None
        self._quit_cb: Optional[Callable[[], None]] = None
        self._started = False
        # 定时广播 Position 的 source id（0 表示未启动）
        self._pos_timer_id = 0

    def set_cover_path(self, path: Optional[str]) -> None:
        """设置当前曲目封面的本地文件路径，并广播 Metadata 更新。

        MPRIS 的 mpris:artUrl 只接受 file:// 或 http(s):// URI；
        KDE Connect 等客户端不支持 data: base64。因此调用方需先把封面
        字节落盘成真实文件，再把路径传进来。

        path 为 None 或空字符串表示当前无封面（清空 artUrl）。
        """
        new_path = str(path) if path else None
        if new_path == self._cover_path:
            return
        self._cover_path = new_path
        self._emit_metadata()

    # ------------------------------------------------------------
    # 外部回调注册（替代直接赋值下划线属性的脆弱写法）
    # ------------------------------------------------------------
    def set_raise_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """设置 Raise 方法回调（把窗口置前）。"""
        self._raise_cb = cb

    def set_quit_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """设置 Quit 方法回调（退出应用）。"""
        self._quit_cb = cb

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    def start(self) -> bool:
        """连接会话总线、注册对象、接管总线名。成功返回 True。"""
        if self._started:
            return True
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as exc:
            log.debug("MPRIS: 连接会话总线失败: %s", exc)
            return False
        try:
            node = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML)
            iface_root = node.lookup_interface(IFACE_ROOT)
            iface_player = node.lookup_interface(IFACE_PLAYER)
        except Exception as exc:
            log.debug("MPRIS: 解析接口 XML 失败: %s", exc)
            return False
        # 注册对象：两个接口各自带 get/set 回调。
        # 关键修复：根接口也必须提供 get_property 回调，否则对根接口属性的
        # Get/GetAll 会因缺少回调而产生类型错误警告。
        try:
            self._conn.register_object(
                OBJ_PATH, iface_root,
                self._on_root_call, self._on_root_get, None)
            self._conn.register_object(
                OBJ_PATH, iface_player,
                self._on_player_call, self._on_player_get, self._on_player_set)
        except Exception as exc:
            log.debug("MPRIS: 注册对象失败: %s", exc)
            return False
        # 接管总线名
        try:
            self._reg_id = Gio.bus_own_name_on_connection(
                self._conn, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
        except Exception as exc:
            log.debug("MPRIS: 接管总线名失败: %s", exc)
        # 连接 player / playlist 信号，驱动属性变化
        self._connect_signals()
        # 定时广播 Position：MPRIS 客户端（KDE Connect / GNOME Shell 等）
        # 只在收到 PropertiesChanged 时同步一次位置，之后靠客户端本地估算，
        # 会逐渐漂移。这里定期把真实位置广播回去，校正进度条。
        # 1 秒一次：足够消除漂移，又不至于刷爆 D-Bus。
        try:
            self._pos_timer_id = GLib.timeout_add_seconds(1, self._tick_position)
        except Exception:
            log.debug("MPRIS: 启动 Position 定时器失败", exc_info=True)
        self._started = True
        log.info("MPRIS: 已注册 %s", BUS_NAME)
        return True

    def stop(self) -> None:
        try:
            if self._pos_timer_id:
                GLib.source_remove(self._pos_timer_id)
                self._pos_timer_id = 0
        except Exception:
            log.debug("MPRIS: 停止 Position 定时器失败", exc_info=True)
        try:
            if self._reg_id:
                Gio.bus_unown_name(self._reg_id)
                self._reg_id = 0
        except Exception:
            log.debug("MPRIS: 释放总线名失败", exc_info=True)
        self._started = False

    def _tick_position(self) -> bool:
        """定时器：播放中时广播当前真实位置。返回 True 保持定时器。

        MPRIS 客户端只在收到 PropertiesChanged 时同步一次位置，之后靠
        本地估算，会逐渐漂移；这里定期把真实位置广播回去校正进度条。
        Seeked 仍只在用户/程序真正 seek 时发（持续播放时发不符合语义）。
        """
        try:
            if self._status() == "Playing":
                self._emit_props(IFACE_PLAYER, {
                    "Position": GLib.Variant("x", self._position_us()),
                })
        except Exception:
            log.debug("MPRIS: 定时广播 Position 失败", exc_info=True)
        return True

    def _connect_signals(self) -> None:
        for sig, cb in (
            ("play-state-changed", self._on_state),
            ("duration-changed", self._on_duration),
            ("position-update", self._on_position),
            ("volume-changed", self._on_volume_changed),
        ):
            try:
                self._player.connect(sig, cb)
            except Exception:
                log.debug("MPRIS: 连接 player 信号 %s 失败", sig, exc_info=True)
        try:
            self._playlist.connect("current-changed", self._on_current)
            self._playlist.connect("changed", self._on_queue_changed)
            self._playlist.connect("shuffle-changed", self._on_shuffle_changed)
            self._playlist.connect("repeat-changed", self._on_repeat_changed)
        except Exception:
            log.debug("MPRIS: 连接 playlist 信号失败", exc_info=True)

    # ------------------------------------------------------------
    # 信号处理 → PropertiesChanged
    # ------------------------------------------------------------
    def _on_state(self, _p, _state: str) -> None:
        self._emit_props(IFACE_PLAYER, {
            "PlaybackStatus": GLib.Variant("s", self._status()),
        })

    def _on_duration(self, _p, seconds: float) -> None:
        try:
            self._duration = float(seconds or 0.0)
        except Exception:
            self._duration = 0.0
        self._emit_metadata()

    def _on_position(self, _p, seconds: float) -> None:
        try:
            self._last_pos = float(seconds or 0.0)
        except Exception:
            self._last_pos = 0.0

    def _on_volume_changed(self, _p, value: float) -> None:
        """音量变化（来自 UI 或 MPRIS 本身）→ 广播给客户端。

        电脑端调音量时，手机端能实时同步；手机端调音量时，这会广播一次
        相同值（客户端通常忽略无变化的值），不构成回环。
        """
        try:
            v = max(0.0, min(1.0, float(value)))
        except Exception:
            return
        self._emit_props(IFACE_PLAYER, {
            "Volume": GLib.Variant("d", v),
        })

    def _on_current(self, _pl, _index: int) -> None:
        self._emit_metadata()
        self._emit_props(IFACE_PLAYER, {
            "PlaybackStatus": GLib.Variant("s", self._status()),
            "CanGoNext": GLib.Variant("b", self._can_go_next()),
            "CanGoPrevious": GLib.Variant("b", self._can_go_previous()),
        })

    def _on_queue_changed(self, _pl) -> None:
        """队列内容变化：刷新 CanGoNext / CanGoPrevious。"""
        self._emit_props(IFACE_PLAYER, {
            "CanGoNext": GLib.Variant("b", self._can_go_next()),
            "CanGoPrevious": GLib.Variant("b", self._can_go_previous()),
        })

    def _on_shuffle_changed(self, _pl, on: bool) -> None:
        """随机播放开关变化（来自 UI 或 MPRIS）→ 广播给客户端。"""
        self._emit_props(IFACE_PLAYER, {
            "Shuffle": GLib.Variant("b", bool(on)),
        })

    def _on_repeat_changed(self, _pl, mode: int) -> None:
        """循环模式变化（来自 UI 或 MPRIS）→ 广播给客户端。"""
        self._emit_props(IFACE_PLAYER, {
            "LoopStatus": GLib.Variant("s", self._loop_status()),
            "CanGoNext": GLib.Variant("b", self._can_go_next()),
            "CanGoPrevious": GLib.Variant("b", self._can_go_previous()),
        })

    def _emit_metadata(self) -> None:
        try:
            self._emit_props(IFACE_PLAYER, {
                "Metadata": GLib.Variant("a{sv}", self._metadata()),
            })
        except Exception:
            log.debug("MPRIS: 发 Metadata 失败", exc_info=True)

    def _emit_props(self, iface: str, changed: dict) -> None:
        if self._conn is None:
            return
        try:
            params = GLib.Variant("(sa{sv}as)", (iface, changed, []))
            self._conn.emit_signal(
                None, OBJ_PATH, IFACE_PROPS, "PropertiesChanged", params)
        except Exception:
            log.debug("MPRIS: 发 PropertiesChanged 失败", exc_info=True)

    def _emit_seeked(self, position_us: int) -> None:
        if self._conn is None:
            return
        try:
            self._conn.emit_signal(
                None, OBJ_PATH, IFACE_PLAYER, "Seeked",
                GLib.Variant("(x)", (int(position_us),)))
        except Exception:
            log.debug("MPRIS: 发 Seeked 失败", exc_info=True)

    # ------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------
    def _status(self) -> str:
        try:
            st = self._player.state()
        except Exception:
            st = "stopped"
        if st == "playing":
            return "Playing"
        if st == "paused":
            return "Paused"
        return "Stopped"

    def _track_id(self) -> str:
        """生成稳定的、合法的 mpris:trackid object path。

        D-Bus object path 每段只能含 [A-Za-z0-9_] 且不能以数字开头。
        用源标识做 hash，保证同一曲目每次得到的 trackid 稳定一致。
        """
        try:
            track = self._playlist.current_track()
        except Exception:
            track = None
        if track is None:
            return _NO_TRACK
        key = (
            getattr(track, "source_id", "")
            or getattr(track, "filepath", "")
            or getattr(track, "stream_url", "")
            or getattr(track, "title", "")
        )
        digest = hashlib.sha1(str(key).encode("utf-8", "ignore")).hexdigest()[:16]
        return f"/org/mpris/MediaPlayer2/TrackList/{_valid_object_path_segment('track_' + digest)}"

    def _metadata(self) -> dict:
        try:
            track = self._playlist.current_track()
        except Exception:
            track = None
        meta = {
            "mpris:trackid": GLib.Variant("o", self._track_id()),
        }
        if track is None:
            return meta
        title = getattr(track, "title", "") or ""
        artist = getattr(track, "artist", "") or ""
        album = getattr(track, "album", "") or ""
        dur = self._duration or float(getattr(track, "duration_seconds", 0.0) or 0.0)
        meta["xesam:title"] = GLib.Variant("s", title)
        meta["xesam:artist"] = GLib.Variant("as", [artist] if artist else [])
        meta["xesam:album"] = GLib.Variant("s", album)
        if dur > 0:
            meta["mpris:length"] = GLib.Variant("x", int(dur * 1_000_000))
        if getattr(track, "stream_url", ""):
            meta["xesam:url"] = GLib.Variant("s", track.stream_url)
        elif getattr(track, "filepath", ""):
            meta["xesam:url"] = GLib.Variant("s", "file://" + track.filepath)
        # 封面：MPRIS 客户端（KDE Connect / GNOME Shell 等）通过 mpris:artUrl
        # 获取封面。只接受 file:// 或 http(s):// —— base64 data: 不被支持。
        art_url = self._art_url()
        if art_url:
            meta["mpris:artUrl"] = GLib.Variant("s", art_url)
        return meta

    def _art_url(self) -> Optional[str]:
        """当前封面的 mpris:artUrl（file:// URI）；无封面返回 None。"""
        path = self._cover_path
        if not path:
            return None
        try:
            import os
            if not os.path.isfile(path):
                return None
            from urllib.parse import quote
            # 转成合法的 file:// URI（对路径各段做百分号编码）
            return "file://" + quote(os.path.abspath(path))
        except Exception:
            log.debug("MPRIS: 构造 artUrl 失败", exc_info=True)
            return None

    def _position_us(self) -> int:
        try:
            pos = float(self._player.position() or 0.0)
        except Exception:
            pos = self._last_pos
        return int(pos * 1_000_000)

    def _can_go_next(self) -> bool:
        try:
            if self._playlist.is_empty():
                return False
            # 单曲循环 / 列表循环 / 队列长度 > 1 时可下一首
            if self._playlist.repeat_mode() != 0:
                return True
            return len(self._playlist) > 1
        except Exception:
            return True

    def _can_go_previous(self) -> bool:
        try:
            if self._playlist.is_empty():
                return False
            if self._playlist.repeat_mode() != 0:
                return True
            return len(self._playlist) > 1
        except Exception:
            return True

    def _loop_status(self) -> str:
        try:
            mode = self._playlist.repeat_mode()
        except Exception:
            mode = 0
        return {0: "None", 1: "Playlist", 2: "Track"}.get(mode, "None")

    def _volume(self) -> float:
        try:
            fn = getattr(self._player, "volume", None)
            if callable(fn):
                return max(0.0, min(1.0, float(fn())))
            return max(0.0, min(1.0, float(self._player._volume)))
        except Exception:
            return 1.0

    def _shuffle(self) -> bool:
        try:
            return bool(self._playlist.is_shuffle())
        except Exception:
            return False

    def _has_track(self) -> bool:
        """是否存在当前曲目。"""
        try:
            return self._playlist.current_track() is not None
        except Exception:
            return False

    # ------------------------------------------------------------
    # D-Bus 回调（根接口方法）
    # ------------------------------------------------------------
    def _on_root_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        try:
            if method == "Raise":
                cb = self._raise_cb
                if callable(cb):
                    cb()
            elif method == "Quit":
                cb = self._quit_cb
                if callable(cb):
                    cb()
        except Exception:
            log.debug("MPRIS: 根方法 %s 失败", method, exc_info=True)
        invocation.return_value(None)

    # ------------------------------------------------------------
    # D-Bus 回调（根接口属性）—— 修复 Get/GetAll 返回类型警告
    # ------------------------------------------------------------
    def _on_root_get(self, _conn, _sender, _path, _iface, prop):
        if prop == "CanQuit":
            return GLib.Variant("b", True)
        if prop == "CanRaise":
            return GLib.Variant("b", True)
        if prop == "HasTrackList":
            return GLib.Variant("b", False)
        if prop == "Identity":
            return GLib.Variant("s", IDENTITY)
        if prop == "DesktopEntry":
            return GLib.Variant("s", DESKTOP_ENTRY)
        if prop == "SupportedUriSchemes":
            return GLib.Variant("as", ["file", "http", "https"])
        if prop == "SupportedMimeTypes":
            return GLib.Variant("as", [
                "audio/mpeg", "audio/flac", "audio/x-flac", "audio/ogg",
                "audio/mp4", "audio/aac", "audio/wav", "audio/x-wav",
            ])
        return None

    # ------------------------------------------------------------
    # D-Bus 回调（播放接口方法）
    # ------------------------------------------------------------
    def _on_player_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        try:
            if method == "Play":
                self._do_play()
            elif method == "Pause":
                self._player.pause()
            elif method == "PlayPause":
                self._do_play_pause()
            elif method == "Stop":
                self._player.stop()
            elif method == "Next":
                self._playlist.next()
            elif method == "Previous":
                self._playlist.previous()
            elif method == "Seek":
                offset_us = params.unpack()[0]
                self._do_seek(offset_us / 1_000_000.0)
            elif method == "SetPosition":
                _tid, pos_us = params.unpack()
                self._player.seek_seconds(pos_us / 1_000_000.0)
                self._emit_seeked(int(pos_us))
            else:
                log.debug("MPRIS: 未知方法 %s", method)
        except Exception as exc:
            log.debug("MPRIS: 方法 %s 失败: %s", method, exc)
        invocation.return_value(None)

    def _do_play(self) -> None:
        st = self._player.state()
        if st == "paused":
            self._player.resume()
        elif st != "playing":
            track = self._playlist.current_track()
            if track is not None and getattr(track, "playable", False):
                self._player.play_file(track.play_url)

    def _do_play_pause(self) -> None:
        st = self._player.state()
        if st == "playing":
            self._player.pause()
        elif st == "paused":
            self._player.resume()
        else:
            self._do_play()

    def _do_seek(self, offset_sec: float) -> None:
        cur = float(self._player.position() or 0.0)
        target = max(0.0, cur + offset_sec)
        if self._duration > 0:
            target = min(target, self._duration)
        self._player.seek_seconds(target)
        self._emit_seeked(int(target * 1_000_000))

    # ------------------------------------------------------------
    # D-Bus 回调（播放接口属性 get/set）
    # ------------------------------------------------------------
    def _on_player_get(self, _conn, _sender, _path, _iface, prop):
        if prop == "PlaybackStatus":
            return GLib.Variant("s", self._status())
        if prop == "Metadata":
            return GLib.Variant("a{sv}", self._metadata())
        if prop == "Position":
            return GLib.Variant("x", self._position_us())
        if prop == "CanGoNext":
            return GLib.Variant("b", self._can_go_next())
        if prop == "CanGoPrevious":
            return GLib.Variant("b", self._can_go_previous())
        if prop in ("CanPlay", "CanPause", "CanSeek", "CanControl"):
            # 恒为 True：客户端（GSConnect 等）据此显示播放/暂停按钮与进度条。
            # 若返回 False，客户端会直接灰掉/隐藏控件，体验变差。
            return GLib.Variant("b", True)
        if prop == "LoopStatus":
            return GLib.Variant("s", self._loop_status())
        if prop == "Shuffle":
            return GLib.Variant("b", self._shuffle())
        if prop == "Volume":
            return GLib.Variant("d", self._volume())
        if prop in ("Rate", "MinimumRate", "MaximumRate"):
            return GLib.Variant("d", 1.0)
        log.debug("MPRIS: 未知属性读取 %s", prop)
        return None

    def _on_player_set(self, _conn, _sender, _path, _iface, prop, value):
        try:
            if prop == "Volume":
                self._player.set_volume(max(0.0, min(1.0, float(value.unpack()))))
                return True
            if prop == "Shuffle":
                on = bool(value.unpack())
                # 只改状态；广播由 playlist 的 shuffle-changed 信号统一处理。
                try:
                    self._playlist.set_shuffle(on)
                except Exception:
                    log.debug("MPRIS: 设置 Shuffle 失败", exc_info=True)
                return True
            if prop == "LoopStatus":
                s = str(value.unpack())
                mode = {"None": 0, "Playlist": 1, "Track": 2}.get(s, 0)
                # 只改状态；广播由 playlist 的 repeat-changed 信号统一处理。
                try:
                    self._playlist.set_repeat_mode(mode)
                except Exception:
                    log.debug("MPRIS: 设置 LoopStatus 失败", exc_info=True)
                return True
            if prop in ("Rate", "MinimumRate", "MaximumRate"):
                # 暂不支持变速：接受但不改变
                return True
        except Exception:
            log.debug("MPRIS: 设置属性 %s 失败", prop, exc_info=True)
        return False
