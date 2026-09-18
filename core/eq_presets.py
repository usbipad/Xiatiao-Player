"""内置音效预设（声学设计版）。

设计依据（专业声学参考）：
- Harman 耳机/音箱目标（Sean Olive）：低频相对扩散场 +4~5dB@100Hz，
  2-4kHz 略降防刺，8-12kHz 微提空气感。
- 人耳中频最敏感（300Hz-3kHz）：此区染色严格控制在 ±2dB，避免刺耳。
- 低频下潜与紧致：60-120Hz 提升，200-400Hz 适当衰减去浑浊。
- 限幅保护：任何提升都留 headroom + 限幅，防削顶。

分组：忠实向 / 增强向 / 空间向 / 场景向。
每个预设的"实际频响"已用 Rust 频响测试工具核验（见 camilla_engine 测试）。
"""
from __future__ import annotations

from typing import Any, Dict, List


def _base() -> Dict[str, Any]:
    """基础参数（与 ui/effect_page.py 的 _default_params 对齐）。"""
    return {
        "enabled": True,
        "pre_gain_db": 0.0,
        "replaygain_enabled": False,
        "replaygain_mode": "track",
        "replaygain_db": 0.0,
        "replaygain_preamp_db": 0.0,
        "peq_enabled": False,
        "peq_bands": [],
        "convolution_enabled": False,
        "convolution_ir": "",
        "phase_invert": False,
        "eq_enabled": True,
        "eq_gains": [0.0] * 10,
        "bass_enabled": False,
        "bass_gain_db": 0.0,
        "bass_freq": 100.0,
        "treble_gain_db": 0.0,
        "treble_freq": 8000.0,
        "loudness_enabled": False,
        "loudness_amount": 0.0,
        "stereo_width": 1.0,
        "width_enabled": False,
        "balance": 0.0,
        "channel_matrix": "off",
        "crossfeed_enabled": False,
        "crossfeed_amount": 0.3,
        "crossfeed_delay_ms": 0.3,
        "compressor_enabled": False,
        "compressor_threshold_db": -18.0,
        "compressor_ratio": 2.0,
        "compressor_makeup_db": 0.0,
        "limiter_enabled": False,
        "limiter_threshold_db": 0.0,
    }


def _preset(name: str, bands: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    """用 PEQ 频段定义音效。自动：开 PEQ、关图形 EQ。"""
    p = _base()
    p["eq_enabled"] = False
    p["peq_enabled"] = True
    p["peq_bands"] = list(bands)
    p.update(extra)
    return {"name": name, "params": p}


def _pk(freq: float, gain: float, q: float = 1.0) -> Dict[str, Any]:
    return {"freq": freq, "gain": gain, "q": q, "kind": "pk"}


def _ls(freq: float, gain: float, q: float = 0.7) -> Dict[str, Any]:
    return {"freq": freq, "gain": gain, "q": q, "kind": "ls"}


def _hs(freq: float, gain: float, q: float = 0.7) -> Dict[str, Any]:
    return {"freq": freq, "gain": gain, "q": q, "kind": "hs"}


#: 内置音效（顺序即展示顺序）
BUILTIN_PRESETS: List[Dict[str, Any]] = [
    # ============ 关闭 ============
    _preset("关闭", [], enabled=False, peq_enabled=False),

    # ============ 忠实向（中性，少染） ============
    # 参考平直：几乎不染，仅轻微修正（低频微展 + 高频微提）
    _preset("参考平直", [
        _ls(60, 1.0, 0.7), _hs(10000, 0.8, 0.7),
    ], limiter_enabled=True, limiter_threshold_db=-0.5),

    # 古典：保留动态，轻厅堂感（低频微展 + 中频平 + 高频微提）
    _preset("古典", [
        _ls(50, 1.2, 0.7), _pk(300, -0.8, 1.0), _hs(12000, 1.0, 0.7),
    ], limiter_enabled=True, limiter_threshold_db=-0.5),

    # 播客人声：语音清晰（去隆隆 + 提清晰 + 控齿音）
    _preset("播客人声", [
        _pk(150, -2.5, 1.0), _pk(800, -1.5, 1.2),
        _pk(2500, 2.0, 1.2), _pk(5500, -1.5, 1.4),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # ============ 增强向（Harman 式，好听） ============
    # 哈曼曲线：标准 Harman 耳机目标。
    # 注：Lowshelf 的 freq 是「转折点」，满增益在更低频；要让 100Hz 达到
    # +4dB 平台，freq 取 ~200Hz（经频响测试校准）。2-4k 微降防刺，8k 微提。
    _preset("哈曼曲线", [
        _ls(150, 4.5, 1.0), _pk(3000, -1.0, 1.4), _hs(8000, 1.5, 0.8),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 现代流行：流媒体母带感（低频紧致 + 人声靠前 + 空气感；1-4k 不抬防刺）
    _preset("现代流行", [
        _ls(80, 2.0, 0.8), _pk(300, -1.0, 1.0),
        _pk(1800, 1.5, 1.2), _hs(12000, 1.5, 0.8),
    ], stereo_width=1.10, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 清澈人声：人声突出（重点抬 3k 咬字，相对 1k 才明显）+ 控齿音
    _preset("清澈人声", [
        _pk(200, -1.0, 1.0), _pk(3000, 2.0, 1.2),
        _pk(5500, -1.5, 1.4), _hs(10000, 0.5),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 微笑曲线：经典 V 型（低频厚 + 中频略退 + 高频透）
    _preset("微笑曲线", [
        _ls(70, 2.0, 0.8), _pk(1500, -1.5, 1.0),
        _pk(4000, 1.5, 1.0), _hs(12000, 2.0, 0.8),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 低音增强：深潜 + 紧致不轰头（Harman 低频 + 去浊）
    _preset("低音增强", [
        _ls(60, 3.0, 0.8), _pk(150, 1.0, 1.0),
        _pk(400, -2.5, 1.0),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 摇滚：中频推进（吉他/军鼓）+ 低频紧致 + 镲片空气感
    _preset("摇滚", [
        _ls(85, 1.5, 0.8), _pk(400, -1.5, 1.0),
        _pk(2500, 1.5, 1.2), _pk(4000, 1.0, 1.1), _hs(12000, 1.5, 0.8),
    ], stereo_width=1.08, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 爵士：温暖中低 + 顺滑高频 + 乐器分离
    _preset("爵士", [
        _ls(120, 1.5, 0.8), _pk(500, -1.0, 1.0),
        _pk(2500, 0.8, 1.2), _hs(8000, 1.2, 0.8),
    ], stereo_width=1.05, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # ============ 空间向（声场，PEQ 近中性，效果靠 Crossfeed/宽度） ============
    # 耳机空间：Crossfeed 减头中效应（近中性频响）
    _preset("耳机空间", [
        _ls(80, 1.0, 0.8),
    ], crossfeed_enabled=True, crossfeed_amount=0.42, crossfeed_delay_ms=0.35,
       stereo_width=1.05, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 音箱空间：宽声场（近中性频响）
    _preset("音箱空间", [
        _pk(2000, -0.8, 1.0),
    ], stereo_width=1.25, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 现场感：宽 + 轻微空间扩散（近中性频响）
    _preset("现场感", [
        _pk(3000, 0.8, 1.0), _hs(11000, 1.0, 0.7),
    ], crossfeed_enabled=True, crossfeed_amount=0.22, crossfeed_delay_ms=0.4,
       stereo_width=1.20, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # ============ 场景向 ============
    # 深夜聆听：等响度补偿（用 Camilla Loudness 模块，随音量动态补偿）。
    # 配极轻静态 EQ 兜底。
    _preset("深夜聆听", [
        _ls(80, 1.0, 0.7), _hs(10000, 0.8, 0.7),
    ], loudness_enabled=True, loudness_amount=0.5,
       limiter_enabled=True, limiter_threshold_db=-1.5),
]


def preset_names() -> List[str]:
    return [p["name"] for p in BUILTIN_PRESETS]


def get_preset(name: str) -> Dict[str, Any] | None:
    for p in BUILTIN_PRESETS:
        if p["name"] == name:
            return dict(p["params"])
    return None
