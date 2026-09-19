"""DSP 参数的单一真相源（单一真相 + 变更广播 + 唯一出口触发）。

背景：此前 dsp_params 在多个视图各存一份副本（主窗口 dsp_page / 设置页
音效页 / 高级窗口 5 个功能页 / 高级窗口染色页），靠手工 update + 手工
refresh 同步，导致：
  - 重置后部分开关不回归；
  - 总开关切换被晚到的异步下发覆盖（ReplayGain 竞态）；
  - 加一个视图就要补一处同步，必漏。

本模块提供唯一真相 + GObject 信号广播：任何一处 patch() 后，所有订阅者
收到 changed 信号并从本状态读取最新参数刷新 UI；下发由 window 统一出口
完成（set_dsp + Camilla YAML）。

用法：
  state = get_dsp_state()
  state.patch({"enabled": True})          # 改一个/多个字段，广播
  state.patch({"enabled": True}, source=self)  # 标注发起者（用于跳过自身刷新）
  cur = state.get()                         # 取当前参数副本（安全，不含内部引用）

信号：
  changed(dict)  —— param 为变更后的完整参数副本（dict）。
                    订阅者据此刷新 UI（只读，不要写回 state）。
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Iterable, Optional

from gi.repository import GObject

log = logging.getLogger(__name__)


#: DSP 参数默认值。与 Rust `DspParams` 字段保持一致。
#: 注：此处是唯一一份默认定义；EffectPage 通过导入本函数复用，
#: 避免两处清单漂移。
def default_params() -> Dict[str, Any]:
    return {
        "enabled": False,
        "camilla_enabled": False,
        "pre_gain_db": 0.0,
        "gain_enabled": False,
        "headroom_enabled": False,
        "headroom_db": 0.0,
        "replaygain_enabled": False,
        "replaygain_mode": "track",
        "replaygain_db": 0.0,
        "replaygain_preamp_db": 0.0,
        "convolution_enabled": False,
        "convolution_ir": "",
        "convolution_channel": 0,
        "convolution_stereo_ir": False,
        "convolution_dry": 1.0,
        "convolution_wet": 1.0,
        "peq_enabled": False,
        "peq_bands": [],
        "eq_enabled": False,
        "eq_gains": [0.0] * 10,
        "eq_q": 1.0,
        "bass_enabled": False,
        "bass_gain_db": 0.0,
        "bass_freq": 100.0,
        "loudness_enabled": False,
        "loudness_amount": 0.0,
        "treble_gain_db": 0.0,
        "treble_freq": 8000.0,
        "crossfeed_enabled": False,
        "crossfeed_amount": 0.3,
        "crossfeed_delay_ms": 0.3,
        "stereo_width": 1.0,
        "width_enabled": False,
        "balance": 0.0,
        "compressor_enabled": False,
        "compressor_threshold_db": -18.0,
        "compressor_ratio": 2.0,
        "compressor_makeup_db": 0.0,
        "compressor_attack": 0.01,
        "compressor_release": 0.2,
        "limiter_threshold_db": 0.0,
        "limiter_enabled": False,
        "limiter_soft_clip": False,
        "phase_invert": False,
        "channel_matrix": "off",
        "reverb_enabled": False,
        "reverb_mix": 0.25,
        "reverb_pre_delay_ms": 20.0,
        "reverb_decay": 0.5,
        "reverb_damping": 0.5,
        "reverb_width": 1.0,
        "reverb_low_cut_hz": 100.0,
        "reverb_mod_depth": 0.5,
        # 音色染色（电子管 / BBE）：归「启用音频处理」总开关管，纳入默认体系
        "tube_enabled": False,
        "tube_drive": 1.0,
        "bbe_enabled": False,
        "bbe_amount": 0.5,
    }


class DspState(GObject.Object):
    """DSP 参数的单一真相。"""

    __gtype_name__ = "DspState"

    __gsignals__ = {
        # 参数变更。param: object（变更后的完整参数副本 dict）。
        # 订阅者刷新 UI，禁止在回调里再次 patch（避免递归广播）。
        "changed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        # 参数被整体替换（加载预设 / 重置）。param: object（新参数副本）。
        # 与 changed 的区别仅为语义标注，处理方式相同（刷新 UI）。
        "replaced": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__()
        # 唯一真相：从 config 初始化，缺失字段用默认值补齐。
        self._params: Dict[str, Any] = default_params()
        try:
            from config.settings import get_config
            saved = get_config().get("dsp_params")
            if isinstance(saved, dict):
                # 只接受已知字段，避免旧数据带入未知键污染
                for k, v in saved.items():
                    if k in self._params:
                        self._params[k] = copy.deepcopy(v)
        except Exception:
            log.debug("初始化 DspState 读取 dsp_params 失败", exc_info=True)
        # 防止递归广播：patch 期间置位，订阅者回调里若再 patch 直接忽略。
        self._in_patch: bool = False

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------
    def get(self) -> Dict[str, Any]:
        """当前参数的深拷贝（调用方可安全修改，不影响内部状态）。"""
        return copy.deepcopy(self._params)

    def get_value(self, key: str, default: Any = None) -> Any:
        """取单个字段值（深拷贝，避免外部拿到内部可变对象引用）。"""
        v = self._params.get(key, default)
        return copy.deepcopy(v)

    # ------------------------------------------------------------
    # 写（唯一变更入口）
    # ------------------------------------------------------------
    def patch(self, changes: Dict[str, Any], *, source: Any = None,
              persist: bool = True) -> None:
        """合并若干字段并广播。

        changes: 要变更的字段字典（只改传入的键，其余保持不变）。
        source:  发起者（可选，仅用于日志/调试；当前不做跳过自身，
                 订阅者刷新是幂等的，跳过反而易漏）。
        persist: 是否写入 config.dsp_params。

        总是广播（即使值与旧值相同）：保证所有视图都有机会把 UI 纠正到
        与单一源一致（避免「值没变不广播」导致的不同步）。
        """
        if not isinstance(changes, dict) or not changes:
            return
        if self._in_patch:
            # 防御：订阅者回调里不应再 patch。忽略并记录，避免无限递归。
            log.warning("DspState.patch 在广播回调中被再次调用，已忽略: %r",
                        list(changes.keys()))
            return
        # 合并（深拷贝，隔离外部引用）
        for k, v in changes.items():
            self._params[k] = copy.deepcopy(v)
        if persist:
            self._persist()
        self._emit_changed("changed", source)

    def replace(self, params: Dict[str, Any], *, source: Any = None,
                persist: bool = True) -> None:
        """整体替换参数（加载预设 / 重置）。缺失字段用默认补齐。

        与 patch 的区别：patch 只改传入键；replace 用「默认基底 + 传入覆盖」
        重建整份参数，未传的字段回到默认值（预设语义）。
        """
        if not isinstance(params, dict):
            return
        if self._in_patch:
            log.warning("DspState.replace 在广播回调中被再次调用，已忽略")
            return
        merged = default_params()
        for k, v in params.items():
            if k in merged:
                merged[k] = copy.deepcopy(v)
        self._params = merged
        if persist:
            self._persist()
        self._emit_changed("replaced", source)

    def reset(self, *, keep: Optional[Iterable[str]] = None,
              source: Any = None, persist: bool = True) -> None:
        """重置为默认值，可保留若干字段（如总开关 enabled）。"""
        keep_set = set(keep or [])
        merged = default_params()
        for k in keep_set:
            if k in self._params:
                merged[k] = copy.deepcopy(self._params[k])
        self._params = merged
        if persist:
            self._persist()
        self._emit_changed("replaced", source)

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------
    def _persist(self) -> None:
        try:
            from config.settings import get_config
            cfg = get_config()
            cfg.set("dsp_params", copy.deepcopy(self._params))
            cfg.set_bool("dsp_enabled", bool(self._params.get("enabled", False)))
        except Exception:
            log.debug("持久化 dsp_params 失败", exc_info=True)

    def _emit_changed(self, signal: str, source: Any) -> None:
        self._in_patch = True
        try:
            # 传副本，订阅者拿到的是快照，无法回写内部状态
            self.emit(signal, copy.deepcopy(self._params))
        except Exception:
            log.debug("广播 %s 失败", signal, exc_info=True)
        finally:
            self._in_patch = False


_instance: Optional[DspState] = None


def get_dsp_state() -> DspState:
    global _instance
    if _instance is None:
        _instance = DspState()
    return _instance
