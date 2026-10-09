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
#: 说明：
#:   - ffprobe 与 symphonia 报的名字可能不同（如 WavPack ffprobe 报
#:     `wavpack`、WAV 报 `pcm_*`），都需覆盖；
#:   - `wv`/`wavpack` 是 WavPack；`tta`/`tak`/`shorten` 等为无损压缩；
#:   - `mlp`/`truehd` 为无损多声道。
LOSSLESS_CODECS = frozenset({
    "flac", "alac", "wav", "ape", "wv", "wavpack", "aiff", "aif",
    "dsd", "dsf", "dff",
    "tta", "tak", "shorten", "wmalossless", "mlp", "truehd",
})

#: DSD 扩展名。
DSD_EXTS = frozenset({"dsf", "dff"})

#: 超高采样率（DXD）门槛。
_DXD_MIN_RATE = 352_800

# ============================================================
# 在线音质档位（单一真相）
# ============================================================
#
# 档位 → Subsonic `maxBitRate` 约定值（通用参数；由服务端按自身能力解释，
# 不支持该档位的标准服务端不会报错，最多原样返回）。
# 0 表示不限制（原文件直传）。
#
# 说明：这里只是「客户端请求的通用档位」，不含任何平台私有语义；
# 标准 Subsonic 与用户自备的兼容服务端都按此约定响应。
ONLINE_QUALITY_BITRATE: dict[str, int] = {
    "standard": 128,
    "high": 320,
    "lossless": 999,
    "hires": 1400,
    "master": 2000,
}

#: 档位显示名（key, 展示名），供下拉框/UI 复用。
ONLINE_QUALITY_LABELS: list[tuple[str, str]] = [
    ("standard", "标准 128k"),
    ("high", "高品 320k"),
    ("lossless", "无损"),
    ("hires", "Hi-Res"),
    ("master", "母带"),
]

#: 默认档位（配置缺省）。
DEFAULT_ONLINE_QUALITY = "lossless"

#: 档位 → 短显示名（不含码率），供按钮/菜单等窄空间复用。
#: 从 ONLINE_QUALITY_LABELS 派生（去掉尾部「码率」），保证单一数据源——
#: 改档位清单只需改上面一处，这里自动跟随。
ONLINE_QUALITY_SHORT: dict[str, str] = {
    key: (label.split()[0] if label else key)
    for key, label in ONLINE_QUALITY_LABELS
}

#: 档位 → 期望「物理规格」（用于「所选音质不可用」降级提示的比对）。
#: 语义：该档位理论上应达到的规格门槛；实际 < 期望即视为被服务端降级。
#: standard/high 为有损（mp3），lossless 期望 CD，hires/master 期望 HR。
ONLINE_QUALITY_EXPECT_SPEC: dict[str, str] = {
    "standard": "mp3",
    "high": "mp3",
    "lossless": "cd",
    "hires": "hr",
    "master": "hr",
}


def quality_to_bitrate(key: str) -> int:
    """档位 key → maxBitRate；未知/空返回 0（不限制）。"""
    return ONLINE_QUALITY_BITRATE.get(str(key or "").strip().lower(), 0)


def quality_short(key: str) -> str:
    """档位 key → 短显示名；未知返回原 key。"""
    k = str(key or "").strip().lower()
    return ONLINE_QUALITY_SHORT.get(k, key or "")


def quality_expect_spec(key: str) -> str:
    """档位 key → 期望物理规格（用于降级比对）；未知返回空串。"""
    return ONLINE_QUALITY_EXPECT_SPEC.get(str(key or "").strip().lower(), "")


#: 物理规格 → 简短显示名（用于「所选 X 不可用，实际为 Y」提示）。
#: 口径：cd 显示「无损」（用户语言），非「CD」（规格术语）。
#: 注：与 models.track.quality_badge（用「CD」）语义不同，勿混用。
SPEC_DISPLAY_NAME: dict[str, str] = {
    "dsd": "DSD",
    "dxd": "DXD",
    "hr": "Hi-Res",
    "cd": "无损",
}


def spec_display_name(spec: str) -> str:
    """物理规格 → 简短显示名；未知返回原 spec。"""
    k = str(spec or "").strip().lower()
    return SPEC_DISPLAY_NAME.get(k, spec or "")


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

    # DSD：codec 以 dsd 开头（dsd / dsd_lsbf_planar / …），或扩展名
    # dsf/dff（symphonia 路径用扩展名当 codec）。采样率口径由
    # dsd_spec_from_rate 内部对「原值 / ×8 / ÷8」容错。
    if codec.startswith("dsd") or codec in DSD_EXTS:
        return dsd_spec_from_rate(sample_rate)
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
    # PCM 变体（WAV 文件 ffprobe 常报 pcm_s16le / pcm_s24le / pcm_f32le 等，
    # 而非 "wav"）本质是无损，一并按无损处理，否则 WAV 无损歌不显示徽标。
    if codec not in LOSSLESS_CODECS and not codec.startswith("pcm_"):
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

    倍数基准 = CD 采样率 44.1kHz（DSD64 = 64×44100 = 2822400）。

    采样率口径不确定：ffmpeg/ffprobe 常报「PCM 等效率」(= 原始率 ÷ 8，
    如 DSD64 → 352800)；也有后端直接报原始率。故对三种口径
    （原值 / ×8 / ÷8）都试，取最接近标准倍数(64/128/256/512/1024)的那个。
    无法判定时返回 'dsd'。
    """
    rate = _to_int(sample_rate)
    if rate <= 0:
        return "dsd"
    _std = (64, 128, 256, 512, 1024)
    best = None  # (偏差, 倍数)
    for cand in (rate, rate * 8, rate / 8):
        mult = round(cand / _DSD_MULT_BASE)
        if mult in _std:
            return f"dsd{mult}"
        if mult > 0:
            nearest = min(_std, key=lambda m: abs(m - mult))
            dev = abs(nearest - mult)
            if best is None or dev < best[0]:
                best = (dev, nearest)
    if best is not None and best[0] <= 8:
        return f"dsd{best[1]}"
    return "dsd"


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
