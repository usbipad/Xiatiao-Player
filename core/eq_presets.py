"""内置均衡器音效预设（6 个常用、耐听向）。

全部基于 10 段图形 EQ（频率与 Rust 侧 EQ_FREQS 一致）：
    31 / 62 / 125 / 250 / 500 / 1000 / 2000 / 4000 / 8000 / 16000 Hz

原则：任一频段调整不超过 ±5dB，避免听感疲劳；
不依赖特定耳机，普适性较强。
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
    """用 PEQ 频段定义音效（PEQ-only 模式）。

    bands: [{"freq":, "gain":, "q":, "kind": "pk"/"ls"/"hs"}, ...]
    自动关闭图形 EQ / 压缩 / 宽度 / Loudness，只用 PEQ + 限幅保护。
    """
    p = _base()
    p["eq_enabled"] = False          # 不用 10 段图形 EQ
    p["peq_enabled"] = True          # 用 PEQ
    p["peq_bands"] = list(bands)
    p.update(extra)
    return {"name": name, "params": p}


#: 限幅保护（纯安全兜底，不改变音色）
def _pk(freq: float, gain: float, q: float = 1.0) -> Dict[str, Any]:
    return {"freq": freq, "gain": gain, "q": q, "kind": "pk"}


def _ls(freq: float, gain: float, q: float = 0.7) -> Dict[str, Any]:
    return {"freq": freq, "gain": gain, "q": q, "kind": "ls"}


def _hs(freq: float, gain: float, q: float = 0.7) -> Dict[str, Any]:
    return {"freq": freq, "gain": gain, "q": q, "kind": "hs"}


#: 全部音效只保留限幅（保护），不用压缩/宽度/Loudness
_PROTECT = dict(limiter_enabled=True, limiter_threshold_db=-1.0)

#: 内置音效（PEQ-only，顺序即展示顺序）
# 设计依据：人耳中频最敏感（300Hz-3kHz，控制在 ±3dB）；
# 人声清晰度核心 1k-2k；低频下潜 60-125Hz；高频只微调。
# 每个音效用 3-5 段 PEQ，段数少、相位失真小。
BUILTIN_PRESETS: List[Dict[str, Any]] = [
    # 关闭：整体旁路直通（BitPerfect）
    _preset("关闭", [], enabled=False, peq_enabled=False),

    # 现代流行：流媒体母带感——低频紧致、人声靠前、高频不刺耳
    # 设计：80Hz 量感、300Hz 去浑浊、1.8k 咬字、4k 临场、12k 空气感
    # 注：流行母带本身已高压缩，此处不再叠加压缩器，保留动态
    _preset("现代流行", [
        _ls(80, 2.0, 0.8), _pk(300, -1.0, 1.0), _pk(1800, 2.0, 1.2),
        _pk(4000, 1.5, 1.0), _hs(12000, 1.2, 0.8),
    ], stereo_width=1.12, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 清澈人声：人声突出、齿音顺滑、乐器退后（优先衰减，非一味提升）
    # 设计：200Hz 去浑浊、1k 人声核心、2.5k 咬字、6k 齿音控制（-2dB）
    _preset("清澈人声", [
        _pk(200, -2.0, 1.0), _pk(1000, 2.0, 1.5), _pk(2500, 3.0, 1.2),
        _pk(6000, -2.0, 1.4), _hs(10000, 0.8),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 微笑曲线：经典 V 型，低频厚、高频透、中频略退
    _preset("微笑曲线", [
        _ls(70, 2.0, 0.8), _pk(250, -1.5, 1.0), _pk(1500, 1.5, 1.0),
        _pk(4000, 2.0, 1.0), _hs(12000, 2.0, 0.8),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 低音增强：深下潜 + 紧致、不轰头（参考哈曼低频增益思路）
    # 修正：收敛总低频量（原 55+90+180 三段叠加偏大易轰），
    # 加强 400Hz 去浊，避免低频糊住人声
    _preset("低音增强", [
        _ls(60, 2.5, 0.8), _pk(100, 1.8, 1.1), _pk(200, 0.8, 1.0),
        _pk(400, -2.5, 1.0), _pk(1200, 0.5, 1.0),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 耳机空间：Crossfeed 减头中效应，微宽，声场宽松立体
    # 修正：width 收一点（1.08→1.05），避免 M/S 加宽削弱 crossfeed 的
    # 头中效应缓解；crossfeed 略降、delay 略增，更接近真实头部串扰
    _preset("耳机空间", [
        _ls(90, 1.5, 0.8), _pk(400, -1.0, 1.0), _hs(9000, 0.8, 0.7),
    ], crossfeed_enabled=True, crossfeed_amount=0.40, crossfeed_delay_ms=0.35,
       stereo_width=1.05, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 空间感：宽声场 + 轻微空间扩散（适合电影/现场录音）
    _preset("空间感", [
        _pk(200, -1.0, 1.0), _pk(3000, 1.0, 1.0), _hs(11000, 1.5, 0.7),
    ], crossfeed_enabled=True, crossfeed_amount=0.22, crossfeed_delay_ms=0.4,
       stereo_width=1.25, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 哈曼曲线：参考 Harman 头戴目标（低频 +4dB @105Hz，中高频微调）
    # 依据：Harman over-ear 目标相对扩散场，低频抬升约 4-5dB，
    # 2-4k 略降避免刺耳，8k 附近小幅提升增加空气感
    _preset("哈曼曲线", [
        _ls(105, 3.5, 0.7), _pk(250, -1.0, 1.0), _pk(1800, 1.0, 1.2),
        _pk(3200, -1.5, 1.4), _hs(8000, 1.5, 0.8),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 深夜聆听：低音量下等响补偿（Fletcher-Munson 曲线）
    # 小音量时人耳对低/高频迟钝，故轻推两端；中频不动避免吵
    _preset("深夜聆听", [
        _ls(90, 2.5, 0.7), _hs(10000, 1.5, 0.7),
    ], limiter_enabled=True, limiter_threshold_db=-1.5),

    # 摇滚：中频推进（吉他/军鼓）+ 低频紧致 + 高频延展
    # 400Hz 减一点去箱体浑浊，2k/4k 推吉他泛音，12k 增镲片空气感
    _preset("摇滚", [
        _ls(85, 1.5, 0.8), _pk(400, -1.5, 1.0), _pk(2000, 2.0, 1.2),
        _pk(4000, 1.5, 1.1), _hs(12000, 1.5, 0.8),
    ], stereo_width=1.08, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 古典：尽量平直，只做极轻的厅堂感与低频延展
    # 古典录音动态大，绝不过度 EQ；仅 60Hz 微展、10k 微增空气
    _preset("古典", [
        _ls(60, 1.2, 0.7), _pk(300, -0.8, 1.0), _hs(10000, 1.0, 0.7),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),

    # 爵士：温暖中低 + 顺滑高频 + 乐器分离
    # 200Hz 加暖、500Hz 略减去闷、3k 顺滑、8k 微增铜管/镲片光泽
    _preset("爵士", [
        _ls(120, 1.5, 0.8), _pk(500, -1.0, 1.0), _pk(2500, 0.8, 1.2),
        _hs(8000, 1.2, 0.8),
    ], stereo_width=1.05, width_enabled=True,
       limiter_enabled=True, limiter_threshold_db=-1.0),

    # 播客 / 人声：语音清晰度优先
    # 180Hz 去隆隆、800Hz 去鼻音、2.5k 提清晰度、5k 控齿音
    _preset("播客人声", [
        _pk(180, -2.5, 1.0), _pk(800, -1.5, 1.2), _pk(2500, 2.5, 1.2),
        _pk(5000, -1.5, 1.4), _hs(9000, 0.5),
    ], limiter_enabled=True, limiter_threshold_db=-1.0),
]


def preset_names() -> List[str]:
    return [p["name"] for p in BUILTIN_PRESETS]


def get_preset(name: str) -> Dict[str, Any] | None:
    for p in BUILTIN_PRESETS:
        if p["name"] == name:
            return dict(p["params"])
    return None
