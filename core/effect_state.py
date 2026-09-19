"""当前音效预设的单一状态源（单一真相 + 变更广播）。

背景：此前「当前音效」以 config 里的 effect_preset 字符串为准，
但读取分散、且各视图（主界面音效弹窗 / 设置页 / 高级窗口）各自缓存，
导致任一处改动后其它视图不自动同步（需手动刷新，易漏）。

本模块提供唯一真相 + GObject 信号广播：任何一处 set_current 后，
所有订阅者（如音效弹窗）都会收到 changed 信号并自动刷新。

值语义：
  - ""       表示「当前不是任何预设」（如手动调参数后）
  - "关闭"   作为特殊预设名（对应内置预设「关闭」）
  - 其它字符串 = 具体预设名
"""
from __future__ import annotations

import logging

from gi.repository import GObject

log = logging.getLogger(__name__)


class EffectState(GObject.Object):
    """当前音效预设名的单一真相。"""

    __gtype_name__ = "EffectState"

    __gsignals__ = {
        # 当前音效变更，param: str（新名称，可能为空串）。
        "changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # DSP 参数被重置（重置按钮 / 重置本页）。无参。
        # 各视图（设置页 / 高级窗口 / 主界面参数宿主）订阅后从 config
        # 重读 dsp_params 刷新自身，保证多实例 UI 与后端一致。
        "reset": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        # 从 config 初始化（兼容旧数据）。
        self._current = ""
        try:
            from config.settings import get_config
            val = get_config().get("effect_preset")
            if isinstance(val, str):
                self._current = val
        except Exception:
            log.debug("初始化 EffectState 读取 effect_preset 失败", exc_info=True)

    def current(self) -> str:
        """当前音效预设名（可能为空串）。"""
        return self._current

    def set_current(self, name: str, *, persist: bool = True) -> None:
        """设置当前音效并广播。

        总是广播（即使与旧值相同）：让所有视图都有机会把 UI 纠正到与
        单一源一致，避免「UI 与实际状态不一致但值相等不广播」导致的不同步。
        persist=True 时写入 config.effect_preset。
        """
        name = name or ""
        self._current = name
        if persist:
            try:
                from config.settings import get_config
                get_config().set("effect_preset", name)
            except Exception:
                log.debug("持久化 effect_preset 失败", exc_info=True)
        try:
            self.emit("changed", name)
        except Exception:
            log.debug("广播 changed 失败", exc_info=True)

    def notify_reset(self) -> None:
        """广播「DSP 参数已被重置」。

        重置不改变「当前音效」预设名语义（由调用方另行 set_current("")），
        但所有持有 DSP 参数副本的视图都需要重读 config 刷新 UI，
        故单独发一个无参信号，避免与 changed 的语义（预设名）混淆。
        """
        try:
            self.emit("reset")
        except Exception:
            log.debug("广播 reset 失败", exc_info=True)


_instance: EffectState | None = None


def get_effect_state() -> EffectState:
    global _instance
    if _instance is None:
        _instance = EffectState()
    return _instance
