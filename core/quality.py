"""在线音源音质判定（单一职责，纯函数，可单测）。

背景：
  在线播放走 ffmpeg 解码，播放内核会上报 ffprobe 实测的
  `codec / bit_rate / sample_rate / bits_per_sample`。这些是对
  真实音频流的事实观测，比「后端回传的档位」「按配置显示」都准确。

  本模块把「实测参数」映射为播放器音质档位 key，供徽章显示真实音质，
  从而消除「选了母带却实际拿到 320k」这类显示与实际不一致。

档位 key（与 ui/player_panel.py 的 _QUALITY_LABELS 保持一致）：
  standard / high / lossless / hires / master

判定策略（依据实测数据，各档可区分）：
  - 有损（mp3/aac/ogg 等）：按 bit_rate 分 standard / high
  - 无损（flac/alac/wav/ape/dsd 等）：按位深 / 采样率分
      lossless / hires / master
"""
from __future__ import annotations

#: 档位从低到高（用于比较、降级提示）。
QUALITY_ORDER = ["standard", "high", "lossless", "hires", "master"]

#: 无损音频 codec 名（ffprobe codec_name，小写）。
LOSSLESS_CODECS = frozenset({
    "flac", "alac", "wav", "ape", "wv", "aiff", "aif",
    "dsd", "dsf", "dff",
})

#: 有损 codec 名（仅供参考，未列出的按有损处理）。
LOSSY_CODECS = frozenset({
    "mp3", "aac", "ogg", "vorbis", "opus", "m4a", "wma", "ac3",
})

#: 有损档位判定阈值（bps）。
_HIGH_BITRATE_THRESHOLD = 256_000  # 256k 以上算「高品」，以下算「标准」


def quality_rank(key: str) -> int:
    """返回档位序号（0=未知，1=standard ... 5=master）。用于比较高低。"""
    try:
        return QUALITY_ORDER.index(str(key or "").lower()) + 1
    except ValueError:
        return 0


def _to_int(v) -> int:
    """尽力把值转成正整数；失败返回 0。"""
    try:
        n = int(v)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def actual_quality_from_info(info: dict) -> str:
    """从解码器实测参数判定「实际音质档位」。

    参数 info（来自播放内核 audio-info 事件）：
      codec:       音频编码名（如 "flac"/"mp3"），可能缺失
      bitrate:     码率（bps），有损档通常有值，无损档可能缺失
      bit_depth:   位深（如 16/24），无损档通常有值
      sample_rate: 采样率（Hz）
      channels:    声道数（本判定不使用）

    返回：档位 key（standard/high/lossless/hires/master）；
          信息不足无法判定时返回空串 ""（调用方保留原显示）。
    """
    if not isinstance(info, dict):
        return ""
    codec = str(info.get("codec", "") or "").strip().lower()
    bitrate = _to_int(info.get("bitrate"))
    bit_depth = _to_int(info.get("bit_depth"))
    sample_rate = _to_int(info.get("sample_rate"))

    # codec 缺失时无法可靠分流；若连码率也没有则放弃判定。
    if not codec:
        if bitrate >= _HIGH_BITRATE_THRESHOLD:
            return "high"
        if bitrate > 0:
            return "standard"
        return ""

    if codec in LOSSLESS_CODECS:
        return _lossless_quality(bit_depth, sample_rate)

    # 其余（含显式有损、未知 codec）按有损处理。
    if bitrate >= _HIGH_BITRATE_THRESHOLD:
        return "high"
    if bitrate > 0:
        return "standard"
    # 有损但无码率：拿不到更细信息，归为标准档。
    return "standard"


def _lossless_quality(bit_depth: int, sample_rate: int) -> str:
    """无损音频按位深/采样率细分档位。

    约定：
      master：24bit+ 且采样率 > 96kHz（母带级）
      hires ：位深 > 16bit 或采样率 > 48kHz（高解析）
      lossless：其余（CD 级或未知参数的无损）
    """
    if bit_depth >= 24 and sample_rate > 96_000:
        return "master"
    if bit_depth > 16 or sample_rate > 48_000:
        return "hires"
    return "lossless"


def is_degraded(requested: str, actual: str) -> bool:
    """actual 是否低于 requested（用于降级提示）。"""
    r = quality_rank(requested)
    a = quality_rank(actual)
    return r > 0 and a > 0 and a < r
