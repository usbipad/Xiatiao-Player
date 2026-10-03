"""音质规格判定（单一职责，纯函数，可单测）。

用途：本地曲库与在线播放统一使用「物理规格」标签，供音质徽章显示。

规格标签（互斥，取最高）：
  DSD  —— DSD 音频（.dsf/.dff）
  DXD  —— 超高采样率 PCM（≥ 352.8kHz）
  HR   —— Hi-Res（采样率 > 48kHz）
  CD   —— CD 级无损（采样率 ≤ 48kHz 的无损）
  （有损音频不返回标签 → 不显示徽章）

多声道为独立维度（见 is_multichannel），由调用方叠加显示（如 "HR 5.1"）。

判定以「采样率优先」为准：44.1k/24bit 视为 CD（采样率是 CD 级）；
仅当采样率 > 48kHz 才算 HR。这与业界主流 Hi-Res 定义一致。
"""
from __future__ import annotations

#: 规格从低到高（用于比较、降级提示）。有损不在其列。
SPEC_ORDER = ["mp3", "hq", "cd", "hr", "dxd", "dsd"]

#: 无损 codec / 扩展名（小写）。
LOSSLESS_CODECS = frozenset({
    "flac", "alac", "wav", "ape", "wv", "aiff", "aif",
    "dsd", "dsf", "dff",
})

#: DSD 扩展名。
DSD_EXTS = frozenset({"dsf", "dff"})

#: 超高采样率（DXD）门槛。
_DXD_MIN_RATE = 352_800


def _to_int(v) -> int:
    """尽力转正整数；失败返回 0。"""
    try:
        n = int(v)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def spec_from_info(info: dict) -> str:
    """从实测参数判定「物理规格」。

    参数 info（播放内核 audio-info）：
      codec:       编码名（如 "flac"/"mp3"）
      bitrate:     码率 bps
      bit_depth:   位深
      sample_rate: 采样率 Hz

    返回：'dsd' / 'dxd' / 'hr' / 'cd' / ''（有损或信息不足 → 空）。
    """
    if not isinstance(info, dict):
        return ""
    codec = str(info.get("codec", "") or "").strip().lower()
    sample_rate = _to_int(info.get("sample_rate"))
    bit_depth = _to_int(info.get("bit_depth"))

    # DSD：codec 以 dsd 开头（dsd / dsd_lsbf_planar / …）。
    # 软解时 ffprobe 报的是 PCM 等效采样率（= DSD 原始率 / 8）：
    #   DSD64 原始 2822400 → 等效 352800；DSD512 → 等效 2822400。
    # 故 ×8 还原原始率再判倍数。
    if codec.startswith("dsd"):
        return dsd_spec_from_rate(sample_rate * 8)
    # codec 缺失：按采样率/位深兜底（无法判有损，宁可显示规格）。
    # 用于音频后端未上报 codec 的路径（如某些本地格式）。
    if not codec:
        if sample_rate >= _DXD_MIN_RATE:
            return "dxd"
        if sample_rate > 48_000:
            return "hr"
        if sample_rate > 44_100 and bit_depth > 16:
            return "hr"
        if sample_rate > 0:
            return "cd"
        return ""
    # 无损才继续判规格；有损直接返回空（不显示徽章）。
    if codec not in LOSSLESS_CODECS:
        return ""
    if sample_rate >= _DXD_MIN_RATE:
        return "dxd"
    if sample_rate > 48_000:
        return "hr"
    # 采样率 <= 48k：
    #   44.1k（任意位深）→ CD；48k/16bit → CD；
    #   48k/24bit → HR（唯一特例，比 CD 高一档）。
    if sample_rate > 44_100 and bit_depth > 16:
        return "hr"
    return "cd"


#: DSD 倍数基准 = CD 采样率 44.1kHz。
#: DSD64 = 64×44100 = 2822400；DSD256 = 256×44100 = 11289600。
_DSD_MULT_BASE = 44_100


def dsd_spec_from_rate(sample_rate: int) -> str:
    """按 DSD 采样率返回细分规格 key（dsd64/dsd128/…/dsd1024）。

    倍数 = round(rate / 44100)（DSD64=64×44.1k=2822400）。
    无法判定倍数（rate 为 0 或异常）时返回 'dsd'。
    """
    rate = _to_int(sample_rate)
    if rate <= 0:
        return "dsd"
    mult = round(rate / _DSD_MULT_BASE)
    if mult in (64, 128, 256, 512, 1024):
        return f"dsd{mult}"
    # 非常规倍数：就近归入最接近的标准档，或退回通用 DSD。
    if mult <= 0:
        return "dsd"
    nearest = min((64, 128, 256, 512, 1024), key=lambda m: abs(m - mult))
    return f"dsd{nearest}" if abs(nearest - mult) <= 8 else "dsd"


def is_multichannel(info: dict) -> bool:
    """是否多声道（>2 声道）。"""
    if not isinstance(info, dict):
        return False
    return _to_int(info.get("channels")) > 2


def multichannel_label(info: dict) -> str:
    """多声道标签（如 '5.1' / '4.0' / '6ch'）；非多声道返回空串。"""
    if not isinstance(info, dict):
        return ""
    ch = _to_int(info.get("channels"))
    if ch <= 2:
        return ""
    return {6: "5.1", 8: "7.1", 4: "4.0"}.get(ch, f"{ch}ch")


def spec_rank(spec: str) -> int:
    """规格序号（0=未知/有损）。用于比较。"""
    try:
        return SPEC_ORDER.index(str(spec or "").lower()) + 1
    except ValueError:
        return 0


def is_degraded(requested_spec: str, actual_spec: str) -> bool:
    """actual 是否低于 requested（规格维度）。"""
    r = spec_rank(requested_spec)
    a = spec_rank(actual_spec)
    return r > 0 and a > 0 and a < r
