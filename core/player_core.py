"""播放内核 PlayerCore（壳）。

职责：
- 持有一个音频后端（当前为独立 Rust 进程 + IPC，见 core/rust_backend.py）；
- 把后端信号转发为自己的同名信号，UI 只依赖 PlayerCore；
- 对外方法与信号保持稳定，切换后端时 UI 无需改动。

设计要点：
- 不使用 playbin / playbin3，具体管道由后端负责；
- 对外只发 GObject 信号，绝不直接操作 GTK 控件；
- 后端不可用时降级为 null 内核：接口/信号保持一致，只不出声。
"""
from __future__ import annotations

import logging

from gi.repository import GObject

log = logging.getLogger(__name__)

#: 当前使用的后端工厂：独立 Rust 进程（core/rust_backend.py）。
def _create_default_backend():
    from .rust_backend import RustBackend
    return RustBackend()


class PlayerCore(GObject.Object):
    """播放内核壳。"""

    __gtype_name__ = "PlayerCore"

    __gsignals__ = {
        # 播放位置更新（秒）
        "position-update": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        # 曲目总时长变化（秒）
        "duration-changed": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        # 播放到末尾
        "end-of-stream": (GObject.SignalFlags.RUN_LAST, None, ()),
        # 播放状态变化，param: str（stopped/playing/paused）
        "play-state-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # 错误，param: str
        "error-occur": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # 音频技术信息就绪，param: dict
        "audio-info": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        # 音效变化，param: str
        "effect-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # 音量变化，param: float（0.0..1.0）。任何来源（UI / MPRIS / 恢复）
        # 改动音量都会发，供 UI 与 MPRIS 双向同步。
        "volume-changed": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        # 后端连接丢失，param: str（描述）。
        "backend-lost": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._backend = _create_default_backend()
        # 每个信号单独转发到自己的同名信号（参数一致）
        self._backend.connect("position-update", self._fwd_position)
        self._backend.connect("duration-changed", self._fwd_duration)
        self._backend.connect("end-of-stream", self._fwd_eos)
        self._backend.connect("play-state-changed", self._fwd_state)
        self._backend.connect("error-occur", self._fwd_error)
        self._backend.connect("audio-info", self._fwd_audio_info)
        self._backend.connect("effect-changed", self._fwd_effect)
        # backend-lost 是可选信号（老后端可能没有），失败属预期，仅记录。
        try:
            self._backend.connect("backend-lost", self._fwd_backend_lost)
        except Exception as exc:
            log.debug("后端不支持 backend-lost 信号（可忽略）: %s", exc)

    # ---- 信号转发 ----
    def _fwd_position(self, _b, seconds: float) -> None:
        self.emit("position-update", seconds)

    def _fwd_duration(self, _b, seconds: float) -> None:
        self.emit("duration-changed", seconds)

    def _fwd_eos(self, _b) -> None:
        self.emit("end-of-stream")

    def _fwd_state(self, _b, state: str) -> None:
        self.emit("play-state-changed", state)

    def _fwd_error(self, _b, message: str) -> None:
        self.emit("error-occur", message)

    def _fwd_audio_info(self, _b, info) -> None:
        self.emit("audio-info", info)

    def _fwd_effect(self, _b, preset: str) -> None:
        self.emit("effect-changed", preset)

    def _fwd_backend_lost(self, _b, message: str) -> None:
        self.emit("backend-lost", message)


    # ---- 播放控制（转发后端） ----
    def play_file(self, path: str) -> bool:
        return self._backend.play_file(path)

    def pause(self) -> None:
        self._backend.pause()

    def resume(self) -> None:
        self._backend.resume()

    def stop(self) -> None:
        self._backend.stop()

    def seek_seconds(self, seconds: float) -> None:
        self._backend.seek_seconds(seconds)

    def set_volume(self, v: float) -> None:
        try:
            v = max(0.0, min(1.0, float(v)))
        except Exception:
            return
        self._backend.set_volume(v)
        # 主动广播：后端不推 volume 事件，音量变化只能由发起方在此上报。
        self.emit("volume-changed", v)

    def volume(self) -> float:
        """当前音量（0.0..1.0）。"""
        try:
            return max(0.0, min(1.0, float(self._backend._volume)))
        except Exception:
            return 1.0

    def set_effect(self, preset: str) -> None:
        self._backend.set_effect(preset)

    def set_dsp(self, params: dict) -> None:
        self._backend.set_dsp(params)

    def reset_dsp(self) -> None:
        self._backend.reset_dsp()

    def set_camilla_yaml(self, yaml_str: str) -> None:
        """下发嵌入式 camillalib 配置（YAML）。"""
        fn = getattr(self._backend, "set_camilla_yaml", None)
        if callable(fn):
            fn(yaml_str)

    def set_engine(self, camilla: bool = False) -> None:
        """兼容接口：Camilla 是否参与由后端自动推导。"""
        fn = getattr(self._backend, "set_engine", None)
        if callable(fn):
            fn(camilla)

    def set_coloring(self, tube_drive: float, bbe_amount: float) -> None:
        """下发音色染色参数。"""
        fn = getattr(self._backend, "set_coloring", None)
        if callable(fn):
            fn(tube_drive, bbe_amount)

    def set_dsd_mode(self, mode: str) -> None:
        """下发 DSD 输出模式（auto/native/dop/pcm）。"""
        fn = getattr(self._backend, "set_dsd_mode", None)
        if callable(fn):
            fn(mode)

    def set_output_device(self, name: str) -> None:
        """下发输出设备（ALSA hw 设备名；空=默认走 PipeWire）。"""
        fn = getattr(self._backend, "set_output_device", None)
        if callable(fn):
            fn(name)


    def list_output_devices(self) -> list:
        """枚举可用的 ALSA 硬件输出设备。

        返回 [{"id": "hw:...", "description": "..."}, ...]。
        后端不支持时返回空列表。
        """
        fn = getattr(self._backend, "list_output_devices", None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                return []
        return []

    # ---- 查询 ----
    def state(self) -> str:
        return self._backend.state()

    def position(self) -> float:
        return self._backend.position()

    def effect(self) -> str:
        return self._backend.effect()

    def audio_info(self) -> dict:
        return self._backend.audio_info()

    # ---- 兼容旧属性访问 ----
    @property
    def _volume(self):
        """兼容 window.py 对 player._volume 的读取。"""
        return getattr(self._backend, "_volume", 1.0)

    # ---- 生命周期 ----
    def shutdown(self) -> None:
        self._backend.shutdown()
