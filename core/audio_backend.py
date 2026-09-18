"""音频后端抽象接口。

设计目的：把「播放内核对外契约」与「具体实现」解耦，便于替换后端
（当前是 GStreamer，后续计划换成独立 Rust 进程 + IPC）。

约定：
- 后端不直接操作任何 GTK 控件，只通过 GObject 信号对外通报状态。
- PlayerCore 作为壳持有后端，把后端信号转发为自己的同名信号，
  因此 UI 层只依赖 PlayerCore，不关心后端是谁。
- 所有后端实现必须提供与本模块同名的方法与信号。
"""
from __future__ import annotations

from gi.repository import GObject


class PlayerState:
    """播放状态常量（前后端共用）。"""

    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class AudioBackend(GObject.Object):
    """音频后端接口基类。

    子类需实现全部方法，并发出下列同名信号：
      position-update(float)
      duration-changed(float)
      end-of-stream()
      play-state-changed(str)
      error-occur(str)
      audio-info(object)
      effect-changed(str)
    """

    __gtype_name__ = "AudioBackend"

    __gsignals__ = {
        "position-update": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        "duration-changed": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        "end-of-stream": (GObject.SignalFlags.RUN_LAST, None, ()),
        "play-state-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "error-occur": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "audio-info": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "effect-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    # ---- 播放控制 ----
    def play_file(self, path: str) -> bool:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def seek_seconds(self, seconds: float) -> None:
        raise NotImplementedError

    def set_volume(self, v: float) -> None:
        raise NotImplementedError

    def set_effect(self, preset: str) -> None:
        raise NotImplementedError

    # ---- 查询 ----
    def state(self) -> str:
        raise NotImplementedError

    def position(self) -> float:
        raise NotImplementedError

    def effect(self) -> str:
        raise NotImplementedError

    def audio_info(self) -> dict:
        raise NotImplementedError

    # ---- 生命周期 ----
    def shutdown(self) -> None:
        raise NotImplementedError
