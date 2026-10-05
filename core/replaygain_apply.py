"""ReplayGain 读取与应用（从 ui/window.py 抽出）。

抽出理由：ReplayGain 的读取是纯逻辑（读 config 的开关/模式 + 读文件标签），
应用逻辑边界也清晰（只改 dsp_params.replaygain_db 一个字段）。放 window
（上帝对象）里既臃肿又难测。

关键历史（勿删）：应用 ReplayGain 曾用「重读 config + 直接 set_dsp」，
会与总开关切换竞态——切歌的 ReplayGain 是后台线程算的，回到主线程时用户
可能已切总开关；重读 config 可能拿到旧值（enabled=false），直接 set_dsp
会把刚打开的总开关打回去，且绕过统一下发（不发 YAML），导致 Camilla 卷积
等不恢复。现改为：只改 replaygain_db 一个字段，走 DspState 统一下发。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)


def read_track_gain(track, params: Dict[str, Any]) -> Optional[float]:
    """读当前曲目的 ReplayGain 增益值（返回 dB 或 None，不碰 UI/后端）。

    params: DSP 参数字典（需含 replaygain_enabled / replaygain_mode）。
    仅在开启 ReplayGain 且曲目有本地文件路径时读取；否则返回 None。
    """
    try:
        if not isinstance(params, dict) or not params.get("replaygain_enabled"):
            return None
        from models.replaygain import read_replaygain
        mode = params.get("replaygain_mode", "track")
        path = getattr(track, "filepath", "") or ""
        return read_replaygain(path, mode) if path else None
    except Exception as exc:
        log.debug("计算 ReplayGain 失败: %s", exc)
        return None


def apply_gain(
    gain: Optional[float],
    *,
    dsp_state,
    push_dsp: Callable[[Dict[str, Any]], None],
    fallback_params: Optional[Dict[str, Any]] = None,
    persist: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    """应用 ReplayGain 增益：只改 replaygain_db，走 DspState 统一下发。

    dsp_state:    DspState 实例（有 patch 方法）；None 时走 push_dsp 降级。
    push_dsp:     降级下发函数（无 DspState 时用）。
    fallback_params: 无 DspState 时用于构造完整参数（从 config 读的 dsp_params）。
    persist:      持久化回调（可选，通常写 config.dsp_params）。

    只改 replaygain_db，不碰 enabled 等其它字段（避免覆盖总开关）。
    """
    val = float(gain) if gain is not None else 0.0
    wrote = False
    if dsp_state is not None:
        try:
            dsp_state.patch({"replaygain_db": val}, source="replaygain")
            wrote = True
        except Exception:
            wrote = False
    if not wrote and isinstance(fallback_params, dict):
        params = dict(fallback_params)
        params["replaygain_db"] = val
        try:
            push_dsp(params)
        except Exception:
            log.debug("降级下发 ReplayGain 失败", exc_info=True)
        if persist is not None:
            try:
                persist(params)
            except Exception:
                pass
