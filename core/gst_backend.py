"""GStreamer 音频后端实现。

逻辑自原 PlayerCore 迁移而来，行为保持一致：
- 手动构建 uridecodebin -> audioconvert -> volume -> equalizer -> sink 管道；
- DSD（.dsf/.dff）走解码后 PCM 输出；
- 预留 DSP 扩展点（EQ）；
- GStreamer 不可用时降级为 null 后端（接口/信号一致，只不出声）。
"""
from __future__ import annotations

import logging
import os
import threading

from gi.repository import GLib

from .audio_backend import AudioBackend, PlayerState

log = logging.getLogger(__name__)

DSD_EXTENSIONS = {".dsf", ".dff"}

# ----------------------------------------------------------------
# 尝试加载 GStreamer
# ----------------------------------------------------------------
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)
    _HAS_GST = True
except Exception as exc:  # pragma: no cover - 环境相关
    Gst = None  # type: ignore
    _HAS_GST = False
    log.info("GStreamer 不可用，GstBackend 将降级为 null 内核: %s", exc)


class GstBackend(AudioBackend):
    """基于 GStreamer 的音频后端。"""

    __gtype_name__ = "GstBackend"

    #: 音效预设：预设键 -> {频段(Hz): 增益(dB)}。
    EFFECT_PRESETS = {
        "off": {},
        "pop": {60: 3.0, 170: 1.5, 350: -1.0, 1000: 0.0, 3500: 2.0, 10000: 2.5},
        "rock": {60: 4.5, 170: 2.5, 350: -1.5, 1000: -1.0, 3500: 2.5, 10000: 4.0},
        "classical": {60: 3.5, 170: 2.0, 350: 0.0, 1000: 0.0, 3500: 1.5, 10000: 3.0},
        "jazz": {60: 3.0, 170: 1.5, 350: 0.5, 1000: -0.5, 3500: 1.5, 10000: 2.5},
        "bass": {60: 8.0, 170: 5.0, 350: 2.0, 1000: 0.0, 3500: 0.0, 10000: 0.0},
    }

    def __init__(self) -> None:
        super().__init__()
        self._pipeline = None
        self._uridecodebin = None
        self._sink = None
        self._pre_convert = None
        self._volume_elem = None
        self._audioconvert = None
        self._audioresample = None
        self._eq_pre = None
        self._eq_bands: list = []
        self._effect = "off"
        self._state = PlayerState.STOPPED
        self._duration = 0.0
        self._position = 0.0
        self._volume = 1.0
        self._lock = threading.Lock()
        self._bus = None
        self._poll_id = 0
        self._last_pos_emit = -1.0
        self._audio_info: dict = {}
        self._info_probe_id = 0

        if _HAS_GST:
            self._build_pipeline()
        else:
            log.warning("GstBackend 运行在 null 内核模式（无音频输出）")

        self._poll_id = GLib.timeout_add(100, self._poll_position)

    # ------------------------------------------------------------
    # 管道构建
    # ------------------------------------------------------------
    def _build_pipeline(self) -> None:
        try:
            self._pipeline = Gst.Pipeline.new("player")
            self._uridecodebin = Gst.ElementFactory.make("uridecodebin", "decode")
            self._pre_convert = Gst.ElementFactory.make("audioconvert", "preconv")
            self._eq_pre = Gst.ElementFactory.make("audioconvert", "eqpre")
            self._volume_elem = Gst.ElementFactory.make("volume", "vol")
            self._audioconvert = Gst.ElementFactory.make("audioconvert", "conv")
            self._audioresample = Gst.ElementFactory.make("audioresample", "resample")
            self._sink = Gst.ElementFactory.make("autoaudiosink", "sink")

            self._eq_bands = self._build_equalizer_bands()

            if not all([self._pipeline, self._uridecodebin,
                        self._pre_convert, self._eq_pre, self._volume_elem,
                        self._audioconvert, self._audioresample, self._sink]):
                raise RuntimeError("GStreamer 元素创建失败（缺少插件？）")

            self._pipeline.add(self._uridecodebin)
            self._pipeline.add(self._pre_convert)
            self._pipeline.add(self._eq_pre)
            for _freq, eq in self._eq_bands:
                self._pipeline.add(eq)
            self._pipeline.add(self._volume_elem)
            self._pipeline.add(self._audioconvert)
            self._pipeline.add(self._audioresample)
            self._pipeline.add(self._sink)

            chain = [self._pre_convert, self._eq_pre]
            chain += [eq for _freq, eq in self._eq_bands]
            chain += [self._volume_elem, self._audioconvert,
                      self._audioresample, self._sink]
            for a, b in zip(chain, chain[1:]):
                if not a.link(b):
                    raise RuntimeError(f"{a.get_name()} -> {b.get_name()} 连接失败")

            self._uridecodebin.connect("pad-added", self._on_pad_added)

            self._bus = self._pipeline.get_bus()
            self._bus.add_signal_watch()
            self._bus.connect("message::eos", self._on_bus_eos)
            self._bus.connect("message::error", self._on_bus_error)
            self._bus.connect("message::state-changed", self._on_bus_state_changed)
        except Exception as exc:
            log.error("构建 GStreamer 管道失败，降级 null 内核: %s", exc)
            self._pipeline = None

    def _build_equalizer_bands(self) -> list:
        bands = []
        freqs = sorted({f for p in self.EFFECT_PRESETS.values() for f in p})
        for freq in freqs:
            try:
                eq = Gst.ElementFactory.make("equalizer", f"eq{freq}")
            except Exception as exc:
                log.warning("创建均衡器元素失败（freq=%s）: %s", freq, exc)
                return []
            if eq is None:
                log.warning("缺少 equalizer 插件，音效功能不可用")
                return []
            self._config_band(eq, freq, 0.0)
            bands.append((freq, eq))
        return bands

    @staticmethod
    def _config_band(eq, freq: int, gain_db: float) -> None:
        try:
            band_freqs = [29, 59, 119, 237, 474, 947, 1893, 3785, 7570, 15140]
            best = min(range(len(band_freqs)), key=lambda i: abs(band_freqs[i] - freq))
            for i in range(len(band_freqs)):
                val = float(gain_db) if i == best else 0.0
                eq.set_property(f"band{i}", val)
        except Exception as exc:
            log.debug("设置均衡器频段失败: %s", exc)

    def set_dsp(self, params: dict) -> None:
        """GStreamer 后端暂不支持完整 DSP 链，忽略。"""
        return None

    def reset_dsp(self) -> None:
        """GStreamer 后端暂不支持，忽略。"""
        return None

    def set_effect(self, preset: str) -> None:
        preset = preset if preset in self.EFFECT_PRESETS else "off"
        self._effect = preset
        gains = self.EFFECT_PRESETS.get(preset, {})
        for freq, eq in self._eq_bands:
            self._config_band(eq, freq, gains.get(freq, 0.0))
        self.emit("effect-changed", preset)

    def effect(self) -> str:
        return self._effect

    def _on_pad_added(self, _decodebin, pad) -> None:
        target = self._pre_convert if self._pre_convert is not None else self._volume_elem
        if target is None:
            return
        try:
            caps = pad.get_current_caps() or pad.query_caps(None)
        except Exception as exc:
            log.debug("查询 pad caps 失败: %s", exc)
            return
        if caps is None or caps.get_size() == 0:
            return
        struct_name = caps.get_structure(0).get_name()
        if not struct_name.startswith("audio/"):
            log.debug("忽略非音频 pad: %s", struct_name)
            return

        sink_pad = target.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return
        if pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            log.warning("pad 链接失败: %s -> %s", struct_name, target.get_name())
            return
        target.sync_state_with_parent()
        self._schedule_audio_info_probe()

    # ------------------------------------------------------------
    # 音频技术信息探测
    # ------------------------------------------------------------
    def _schedule_audio_info_probe(self) -> None:
        if self._info_probe_id:
            try:
                GLib.source_remove(self._info_probe_id)
            except Exception:
                pass
        self._info_probe_id = GLib.timeout_add(300, self._probe_audio_info)

    def _probe_audio_info(self) -> bool:
        self._info_probe_id = 0
        if not _HAS_GST or self._pre_convert is None:
            return False
        info = self._read_caps_info()
        if info:
            self._audio_info = info
            self.emit("audio-info", info)
            return False
        if getattr(self, "_probe_retry", 0) < 10:
            self._probe_retry = getattr(self, "_probe_retry", 0) + 1
            self._info_probe_id = GLib.timeout_add(200, self._probe_audio_info)
        else:
            self._probe_retry = 0
        return False

    def _read_caps_info(self) -> dict:
        result: dict = {}
        try:
            pad = self._pre_convert.get_static_pad("sink")
            caps = pad.get_current_caps() if pad is not None else None
            if caps is None or caps.get_size() == 0:
                return {}
            s = caps.get_structure(0)
            name = s.get_name()
            fmt = {
                "audio/x-raw": "PCM",
                "audio/mpeg": "MP3",
                "audio/x-flac": "FLAC",
                "audio/x-ape": "APE",
                "audio/x-m4a": "M4A",
                "audio/x-aac": "AAC",
                "audio/x-wav": "WAV",
                "audio/x-vorbis": "OGG",
            }.get(name, name.replace("audio/", "").replace("x-", "").upper())
            result["format"] = fmt
            ok, rate = s.get_int("rate")
            if ok and rate:
                result["sample_rate"] = int(rate)
            ok, ch = s.get_int("channels") or s.get_int("channel-mask")
            if ok and ch:
                result["channels"] = int(ch)
            for key in ("depth", "bits", "bit-depth-lookahead", "width"):
                ok, v = s.get_int(key)
                if ok and v:
                    result["bit_depth"] = int(v)
                    break
            if not result.get("bit_depth"):
                fmtname = self._caps_string(s, "format")
                if fmtname and fmtname[:1] in ("S", "U"):
                    import re as _re
                    m = _re.search(r"(\d+)", fmtname)
                    if m:
                        result["bit_depth"] = int(m.group(1))
        except Exception as exc:
            log.debug("读取音频 caps 失败: %s", exc)
        return result

    @staticmethod
    def _caps_string(struct, field: str) -> str:
        try:
            ok, val = struct.get_string(field)
        except Exception:
            return ""
        if not ok:
            return ""
        if isinstance(val, str):
            return val
        try:
            gv = val
            if hasattr(gv, "get_string"):
                return gv.get_string() or ""
        except Exception:
            pass
        return str(val or "")

    def audio_info(self) -> dict:
        return dict(self._audio_info)

    @staticmethod
    def _is_dsd(path: str) -> bool:
        return os.path.splitext(path)[1].lower() in DSD_EXTENSIONS

    # ------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------
    def play_file(self, path: str) -> bool:
        if not path:
            self.emit("error-occur", "播放地址为空")
            return False
        is_url = "://" in path
        if not is_url and not os.path.isfile(path):
            self.emit("error-occur", f"文件不存在: {path}")
            return False

        if self._pipeline is None:
            self._set_state(PlayerState.PLAYING)
            self._position = 0.0
            self.emit("position-update", 0.0)
            return True

        try:
            self.stop()
            uri = path if is_url else GLib.filename_to_uri(path, None)
            self._uridecodebin.set_property("uri", uri)
            self._apply_volume()
            self._apply_effect()
            self._pipeline.set_state(Gst.State.PLAYING)
            self._set_state(PlayerState.PLAYING)
            return True
        except Exception as exc:
            self.emit("error-occur", f"播放失败: {exc}")
            return False

    def set_volume(self, v: float) -> None:
        v = max(0.0, min(1.0, float(v)))
        self._volume = v
        self._apply_volume()
        if self._pipeline is not None and self._state == PlayerState.PAUSED:
            try:
                pos = self._position
                self._pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    int(max(0.0, pos) * Gst.SECOND),
                )
            except Exception as exc:
                log.debug("调音量后 flush 失败: %s", exc)

    def _apply_volume(self) -> None:
        if self._volume_elem is not None:
            try:
                self._volume_elem.set_property("volume", self._volume)
            except Exception as exc:
                log.debug("应用音量失败: %s", exc)

    def _apply_effect(self) -> None:
        gains = self.EFFECT_PRESETS.get(self._effect, {})
        for freq, eq in self._eq_bands:
            self._config_band(eq, freq, gains.get(freq, 0.0))

    def pause(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.PAUSED)
        self._set_state(PlayerState.PAUSED)

    def resume(self) -> None:
        if self._pipeline is not None:
            self._apply_volume()
            self._pipeline.set_state(Gst.State.PLAYING)
        self._set_state(PlayerState.PLAYING)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self._position = 0.0
        self._last_pos_emit = -1.0
        self._set_state(PlayerState.STOPPED)

    def seek_seconds(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        if self._pipeline is None:
            self._position = seconds
            self.emit("position-update", seconds)
            return
        if self._duration > 0:
            seconds = min(seconds, self._duration)
        try:
            ok = self._pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                int(seconds * Gst.SECOND),
            )
            if ok:
                self._position = seconds
                self._last_pos_emit = seconds
                self.emit("position-update", seconds)
            else:
                log.debug("seek 未生效（可能流未就绪）: %.2fs", seconds)
        except Exception as exc:
            log.debug("跳转失败: %s", exc)

    def shutdown(self) -> None:
        try:
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
            if self._bus is not None:
                self._bus.remove_signal_watch()
        except Exception as exc:  # pragma: no cover
            log.warning("释放播放资源异常: %s", exc)
        finally:
            if self._poll_id:
                GLib.source_remove(self._poll_id)
                self._poll_id = 0
            self._pipeline = None
            self._bus = None
            self._set_state(PlayerState.STOPPED)

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        if self._state != state:
            self._state = state
            self.emit("play-state-changed", state)

    def state(self) -> str:
        return self._state

    def position(self) -> float:
        return self._position

    def _poll_position(self) -> bool:
        if self._pipeline is None or self._state != PlayerState.PLAYING:
            return True
        try:
            ok, pos = self._pipeline.query_position(Gst.Format.TIME)
            ok_d, dur = self._pipeline.query_duration(Gst.Format.TIME)
            if ok_d and dur > 0:
                dur_sec = dur / Gst.SECOND
                if abs(dur_sec - self._duration) > 0.5:
                    self._duration = dur_sec
                    self.emit("duration-changed", dur_sec)
            if ok:
                pos_sec = pos / Gst.SECOND
                if abs(pos_sec - self._last_pos_emit) >= 0.05:
                    self._position = pos_sec
                    self._last_pos_emit = pos_sec
                    self.emit("position-update", pos_sec)
        except Exception as exc:
            log.debug("查询位置失败: %s", exc)
        return True

    # ------------------------------------------------------------
    # 总线回调
    # ------------------------------------------------------------
    def _on_bus_eos(self, _bus, _msg) -> None:
        self._set_state(PlayerState.STOPPED)
        self.emit("end-of-stream")

    def _on_bus_error(self, _bus, msg) -> None:
        err, debug = msg.parse_error()
        self.emit("error-occur", f"{err.message} ({debug})")
        self._set_state(PlayerState.STOPPED)

    def _on_bus_state_changed(self, _bus, msg) -> None:
        try:
            if msg.src is not self._pipeline:
                return
            _old, new, pending = msg.parse_state_changed()
            if pending:
                return
            if new in (Gst.State.PAUSED, Gst.State.PLAYING):
                self._apply_volume()
        except Exception as exc:
            log.debug("处理状态变化失败: %s", exc)
