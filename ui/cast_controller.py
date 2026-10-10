"""DLNA 投送控制器：投送模式的状态机与远端设备控制。

从 ui/window.py 抽出（原 _on_cast_* / _cast_* / _start_cast_poll 等约 300 行）。
抽出目的：window.py 是「上帝对象」，投送状态机边界清晰、状态集中，是最
适合先剥离的一块。

设计：组合而非继承。MainWindow 持有一个 CastController 实例，把投送
相关调用委托给它；Controller 反向通过注入的回调访问 UI/播放器，避免
直接依赖 MainWindow 内部细节（降低耦合）。

状态（原先散在 window 的 self._dlna_* / _cast_*）：
  - casting           是否处于投送模式
  - remote_playing    远端播放/暂停（本机意图权威，不由轮询改）
  - remote_pos        远端最近上报的位置（秒，供 MPRIS 进度）
  - poll_stop         轮询线程停止标志（threading.Event）
  - ended_handled     「已处理播完」标志（防重复触发自动下一首）

调用方（window）保留同名薄委托，行为对外不变。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from gi.repository import GLib

from core.i18n import _

log = logging.getLogger(__name__)


class CastController:
    """DLNA 投送状态机。

    通过构造注入的回调与宿主交互（不直接依赖 MainWindow 类型）：
      get_current_track()   -> 当前曲目（或 None）
      player_stop()         -> 停止本机播放（进入投送时调用）
      ui_set_playing(bool)  -> 同步两侧 UI 播放按钮
      ui_set_position(sec)  -> 同步两侧 UI 进度
      ui_set_duration(sec)  -> 同步两侧 UI 时长
      toast(msg)            -> 提示
      next_track()          -> 队列推进到下一首（返回新曲目或 None）
      current_index()       -> 当前索引
      playlist_next_auto()  -> playlist.next(auto=True)
      notify_mpris()        -> 通知 MPRIS 投送状态变化
    """

    def __init__(
        self,
        *,
        get_current_track: Callable[[], object],
        player_stop: Callable[[], None],
        ui_set_playing: Callable[[bool], None],
        ui_set_position: Callable[[float], None],
        ui_set_duration: Callable[[float], None],
        toast: Callable[[str], None],
        playlist_next_auto: Callable[[], object],
        notify_mpris: Optional[Callable[[], None]] = None,
    ) -> None:
        self._get_current_track = get_current_track
        self._player_stop = player_stop
        self._ui_set_playing = ui_set_playing
        self._ui_set_position = ui_set_position
        self._ui_set_duration = ui_set_duration
        self._toast = toast
        self._playlist_next_auto = playlist_next_auto
        self._notify_mpris = notify_mpris

        # ---- 状态 ----
        self.casting = False
        self.remote_playing = True
        self.remote_pos = 0.0
        self._poll_stop: Optional[threading.Event] = None
        self._ended_handled = False
        #: 投送选择对话框（由 window 持有展示，这里仅存引用避免 GC）
        self.dialog = None

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def is_casting(self) -> bool:
        """当前是否处于 DLNA 投送模式（控制发给远端设备）。"""
        return bool(self.casting)

    def is_remote_playing(self) -> bool:
        return bool(self.remote_playing)

    def get_remote_position(self) -> float:
        return float(self.remote_pos or 0.0)

    def _mpris(self) -> None:
        if self._notify_mpris is not None:
            try:
                self._notify_mpris()
            except Exception:
                pass

    # ------------------------------------------------------------
    # 进入 / 退出投送
    # ------------------------------------------------------------
    def on_started(self) -> None:
        """投送成功：进入投送模式，播放控制改道到远端设备。"""
        self.casting = True
        self.remote_playing = True
        # 本机若正在播放，停掉（B1：本机不放）。
        try:
            self._player_stop()
        except Exception:
            pass
        try:
            self._ui_set_playing(True)
        except Exception:
            pass
        self.start_poll()
        self._toast(_("已进入投送模式：播放控制将发送到设备"))

    def on_stopped(self) -> None:
        """停止投送：退出投送模式，控制恢复本机。"""
        self.casting = False
        self.remote_playing = False
        self.stop_poll()
        try:
            self._ui_set_playing(False)
        except Exception:
            pass
        self._toast(_("已退出投送模式"))

    # ------------------------------------------------------------
    # 播放控制（投送模式）
    # ------------------------------------------------------------
    def play_pause(self) -> None:
        """投送模式播放/暂停：状态由本机意图维护，方向可靠。"""
        try:
            from core.dlna_push import get_dlna_pusher
            pusher = get_dlna_pusher()
            is_playing = bool(self.remote_playing)
            log.debug("[投送] 播放键: remote_playing=%s → 发送 %s",
                      is_playing, "Pause" if is_playing else "Play")
            if is_playing:
                pusher.pause()
                new_state = False
            else:
                pusher.resume()
                new_state = True
            self.remote_playing = new_state
            self._ui_set_playing(new_state)
            self._mpris()
        except Exception:
            log.debug("DLNA 投送播放/暂停失败", exc_info=True)

    def seek(self, seconds: float) -> None:
        """投送模式 seek：发给远端设备（AVTransport Seek）。"""
        try:
            from core.dlna_push import get_dlna_pusher
            pusher = get_dlna_pusher()
            pusher.seek(seconds)
            # seek 后远端会（继续/开始）播放。统一把本机意图与按钮同步为
            # 「播放中」，避免按钮仍显示暂停与实际不符。
            self.remote_playing = True
            try:
                self._ui_set_playing(True)
            except Exception:
                pass
            # 远端进度无法实时回读，本地进度条先跳到目标位置。
            self._ui_set_position(seconds)
        except Exception:
            log.debug("DLNA 投送 seek 失败", exc_info=True)

    def stop_remote(self) -> None:
        """停止远端设备播放（MPRIS stop）。"""
        try:
            from core.dlna_push import get_dlna_pusher
            get_dlna_pusher().stop()
            self.remote_playing = False
            self._ui_set_playing(False)
        except Exception:
            log.debug("投送停止远端失败", exc_info=True)

    def set_remote_position(self, sec: float) -> None:
        """记录远端位置（MPRIS seek 后调用）。"""
        self.remote_pos = float(sec or 0.0)

    # ------------------------------------------------------------
    # 曲目投送
    # ------------------------------------------------------------
    def push_track(self, track) -> None:
        """把指定曲目推送到当前 DLNA 设备，保持切歌前的播放/暂停状态。"""
        fp = (getattr(track, "filepath", "")
              or getattr(track, "stream_url", "")) or ""
        if not fp:
            return
        was_playing = bool(self.remote_playing)
        # 切歌：清除「已播完」标志，让新曲目的轮询重新可触发自动下一首。
        self._ended_handled = False
        try:
            from core.dlna_push import get_dlna_pusher
            pusher = get_dlna_pusher()
            if pusher.current_device() is None:
                return
            # push() 内含 Play；暂停态时随后立即 Pause 收回。
            pusher.push(fp, getattr(track, "title", "") or "",
                        getattr(track, "artist", "") or "")
            if not was_playing:
                pusher.pause()
            # 同步按钮：保持切歌前的播放/暂停状态。
            try:
                self._ui_set_playing(was_playing)
            except Exception:
                pass
        except Exception:
            log.debug("DLNA 投送切歌失败", exc_info=True)

    def re_push_for_quality(self, track) -> None:
        """投送模式切音质：把新音质 URL 重推给远端设备，从头播放。

        本机不出声（保持投送语义）。切音质 = 重新加载，直接从头播最简单
        可靠：SetAVTransportURI 本身即从 0 开始，push 后无需 seek。
        """
        fp = (getattr(track, "stream_url", "")
              or getattr(track, "filepath", "")) or ""
        if not fp:
            return
        try:
            from core.dlna_push import get_dlna_pusher
            pusher = get_dlna_pusher()
            if pusher.current_device() is None:
                return
            was_playing = bool(self.remote_playing)
            self._ended_handled = False
            pusher.push(fp, getattr(track, "title", "") or "",
                        getattr(track, "artist", "") or "")
            if not was_playing:
                pusher.pause()
        except Exception:
            log.debug("投送切音质重推失败", exc_info=True)

    # ------------------------------------------------------------
    # 位置轮询
    # ------------------------------------------------------------
    def start_poll(self) -> None:
        """启动投送位置轮询：每秒查询远端位置，更新进度条。"""
        self.stop_poll()
        stop_flag = threading.Event()
        self._poll_stop = stop_flag

        def _loop():
            while not stop_flag.is_set():
                if not self.casting:
                    break
                try:
                    from core.dlna_push import get_dlna_pusher
                    pusher = get_dlna_pusher()
                    # 仅同步进度；播放/暂停状态以本机操作为准，
                    # 不让轮询覆盖（否则设备状态上报延迟会与本机意图
                    # 打架，导致「点播放却发了暂停」的错乱）。
                    info = pusher.get_position()
                    if info:
                        GLib.idle_add(self._apply_position, info)
                except Exception:
                    pass
                stop_flag.wait(1.0)

        threading.Thread(target=_loop, daemon=True).start()

    def stop_poll(self) -> None:
        flag = self._poll_stop
        if flag is not None:
            try:
                flag.set()
            except Exception:
                pass
        self._poll_stop = None

    def _apply_position(self, info: dict) -> bool:
        """主线程：用远端查询到的位置/时长更新进度条。"""
        if not self.casting:
            return False
        try:
            pos = float(info.get("position", 0.0))
            dur = float(info.get("duration", 0.0))
        except Exception:
            return False
        # 记录远端位置：供 MPRIS 进度上报使用（投送时本机 player 不动）。
        self.remote_pos = pos
        self._mpris()
        # 标准 DLNA：进度直接采用设备上报的位置/时长（渲染器是权威）。
        try:
            self._ui_set_position(pos)
            if dur > 0:
                self._ui_set_duration(dur)
        except Exception:
            pass
        # 自动下一首：投送模式下本机后端不在播放，收不到 end-of-stream，
        # 只能靠轮询到的远端位置判断是否播完。留 1.5s 容差（轮询 1s + 网络
        # 延迟），并用标志防重复触发；dur<=0（设备未上报时长）时不判断。
        if dur > 0 and pos >= dur - 1.5:
            if not self._ended_handled:
                self._ended_handled = True
                GLib.idle_add(self._on_track_ended)
        return False

    def _on_track_ended(self) -> bool:
        """投送曲目播完：按播放列表推进到下一首（投送模式）。"""
        if not self.casting:
            return False
        try:
            before = self._get_current_track()
            nxt = self._playlist_next_auto()
            if nxt is None:
                # 播放列表结束：停止远端设备。
                from core.dlna_push import get_dlna_pusher
                get_dlna_pusher().stop()
                self.remote_playing = False
                try:
                    self._ui_set_playing(False)
                except Exception:
                    pass
            elif nxt is before:
                # 单曲循环：曲目未变，_on_playlist_current_changed 不会触发，
                # 这里手动重推同一首（并复位「已播完」标志）。
                self.push_track(nxt)
        except Exception:
            log.debug("投送自动下一首失败", exc_info=True)
        return False
