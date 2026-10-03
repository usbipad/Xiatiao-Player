"""曲目数据模型。"""
from __future__ import annotations

import os

from gi.repository import GObject


SOURCE_LOCAL = "local"


class TrackItem(GObject.Object):
    """统一曲目实体。

    UI 只依赖本类字段，不关心数据来自哪个 Provider。
    """

    __gtype_name__ = "TrackItem"

    title = GObject.Property(type=str, default="Unknown")
    artist = GObject.Property(type=str, default="Unknown")
    album = GObject.Property(type=str, default="")
    duration = GObject.Property(type=str, default="0:00")
    duration_seconds = GObject.Property(type=float, default=0.0)

    filepath = GObject.Property(type=str, default="")
    source_type = GObject.Property(type=str, default=SOURCE_LOCAL)
    source_id = GObject.Property(type=str, default="")
    #: 在线音源的流地址（http/https 等）；本地曲目为空
    stream_url = GObject.Property(type=str, default="")
    #: 在线曲目的封面图字节（PNG/JPEG）；本地曲目用 filepath 读内嵌封面
    cover_bytes = GObject.Property(type=object, default=None)
    #: 在线曲目的封面图 URL（用于异步下载）
    cover_url = GObject.Property(type=str, default="")

    # 音频技术信息（0 表示未知）
    sample_rate = GObject.Property(type=int, default=0)
    bit_depth = GObject.Property(type=int, default=0)
    channels = GObject.Property(type=int, default=0)
    bitrate = GObject.Property(type=int, default=0)  # bps
    #: 该曲目支持的音质档位列表（在线歌，来自后端扩展字段）。
    #: 形如 [{"key":"lossless","name":"无损","br":999}, ...]；空=未知→回退默认五档。
    quality_levels = GObject.Property(type=object, default=None)

    def __init__(
        self,
        title: str = "Unknown",
        artist: str = "Unknown",
        album: str = "",
        duration: str = "0:00",
        duration_seconds: float = 0.0,
        filepath: str = "",
        source_type: str = SOURCE_LOCAL,
        source_id: str = "",
        stream_url: str = "",
        cover_bytes: bytes | None = None,
        cover_url: str = "",
        sample_rate: int = 0,
        bit_depth: int = 0,
        channels: int = 0,
        bitrate: int = 0,
        quality_levels: list | None = None,
    ) -> None:
        super().__init__()
        self.title = title
        self.artist = artist
        self.album = album
        self.duration = duration
        self.duration_seconds = duration_seconds
        self.filepath = filepath
        self.source_type = source_type
        self.source_id = source_id or filepath
        self.stream_url = stream_url
        self.cover_bytes = cover_bytes
        self.cover_url = cover_url
        self.sample_rate = sample_rate
        self.bit_depth = bit_depth
        self.channels = channels
        self.bitrate = bitrate
        self.quality_levels = quality_levels or None

    # ---- 便捷方法 ----

    @property
    def is_local(self) -> bool:
        return self.source_type == SOURCE_LOCAL

    @property
    def is_stream(self) -> bool:
        """是否为在线流曲目（有 stream_url）。"""
        return bool(self.stream_url)

    @property
    def is_hires(self) -> bool:
        """是否为 Hi-Res 高解析音频。

        判定：
        - 采样率 > 48kHz（88.2 / 96 / 176.4 / 192kHz…），或
        - 48kHz 且 24bit（特例：比 CD 高一档）。
        注：44.1k（任意位深）与 48k/16bit 归 CD 级。
        信息未知（0）时不显示徽标。
        """
        try:
            sr = int(self.sample_rate or 0)
            bd = int(self.bit_depth or 0)
            if sr > 48000:
                return True
            # 48kHz/24bit 特例 → HR。
            return bool(sr > 44100 and bd > 16)
        except Exception:
            return False

    @property
    def is_dxd(self) -> bool:
        """是否为 DXD（超高采样率 PCM，>= 352.8kHz）。"""
        try:
            return bool(self.sample_rate and self.sample_rate >= 352800)
        except Exception:
            return False

    @property
    def is_multichannel(self) -> bool:
        """是否多声道（>2 声道）。"""
        try:
            return bool(self.channels and self.channels > 2)
        except Exception:
            return False

    @property
    def multichannel_label(self) -> str:
        """多声道标签（如 '5.1' / '7.1' / 'Nch'）；非多声道返回空串。"""
        try:
            ch = int(self.channels or 0)
            if ch <= 2:
                return ""
            return {6: "5.1", 8: "7.1", 4: "4.0"}.get(ch, f"{ch}ch")
        except Exception:
            return ""

    @property
    def is_cd_quality(self) -> bool:
        """是否为 CD 级音质（无损 + 采样率 <= 48kHz）。

        判定（采样率优先）：
        - 采样率 <= 48kHz（44.1kHz 为标准 CD，48kHz 亦归入），且
        - 文件格式为无损（flac / ape / wav / wv / alac / aiff / aif）。
        注：24bit/48k 亦归 CD（采样率是 CD 规格）；有损格式不算。
        信息未知（0）时不显示。
        """
        try:
            if not self.sample_rate or self.sample_rate > 48000:
                return False
            ext = self._lossless_ext()
            if not ext or ext not in self._LOSSLESS_EXTS:
                return False
            return True
        except Exception:
            return False

    def _lossless_ext(self) -> str:
        """从 filepath 提取扩展名（小写，不含点）；无则返回空串。

        用于判断是否无损格式。在线曲目用 stream_url 兜底。
        """
        src = self.filepath or self.stream_url or ""
        if "." not in src:
            return ""
        path_part = src.split("?", 1)[0]
        if "." not in path_part:
            return ""
        return path_part.rsplit(".", 1)[-1].lower()

    @property
    def format_ext(self) -> str:
        """音频格式扩展名（大写，如 FLAC / MP3 / APE）；未知返回空串。"""
        try:
            return self._lossless_ext().upper()
        except Exception:
            return ""

    @property
    def sample_rate_label(self) -> str:
        """采样率文本（如 44.1kHz / 96kHz）；未知返回空串。"""
        try:
            if not self.sample_rate:
                return ""
            khz = self.sample_rate / 1000.0
            if abs(khz - round(khz)) < 0.001:
                return f"{int(round(khz))}kHz"
            return f"{khz:.1f}kHz"
        except Exception:
            return ""

    #: 无损音频格式扩展名集合
    _LOSSLESS_EXTS = frozenset(
        {"flac", "ape", "wav", "wv", "alac", "aiff", "aif", "dsf", "dff"}
    )

    @property
    def is_dsd(self) -> bool:
        """是否为 DSD 音频（Direct Stream Digital）。

        判定依据：文件扩展名为 dsf / dff。
        DSD 无 PCM 式采样率/位深，故以格式为准；优先级高于 Hi-Res。
        """
        try:
            return self._lossless_ext() in ("dsf", "dff")
        except Exception:
            return False

    @property
    def dsd_label(self) -> str:
        """DSD 细分标签（DSD64/DSD128/…）；非 DSD 返回空串。

        按采样率 / 2822400 取倍数；无法判定时退回 'DSD'。
        """
        try:
            if not self.is_dsd:
                return ""
            sr = int(self.sample_rate or 0)
            if sr <= 0:
                return "DSD"
            # DSD 倍数基准 = 44.1kHz（DSD64=64×44100=2822400）。
            mult = round(sr / 44100)
            if mult in (64, 128, 256, 512, 1024):
                return f"DSD{mult}"
            if mult > 0 and abs(mult - min((64, 128, 256, 512, 1024),
                                           key=lambda m: abs(m - mult))) <= 8:
                nearest = min((64, 128, 256, 512, 1024), key=lambda m: abs(m - mult))
                return f"DSD{nearest}"
            return "DSD"
        except Exception:
            return "DSD"

    @property
    def quality_badge(self) -> str:
        """返回音质规格徽标文案：DSD/DSD64…/DXD/HR/CD/''。

        互斥，优先级 DSD > DXD > HR > CD。
        仅无损规格才有徽标（有损不标）；多声道另由 multichannel_label 叠加。
        """
        if self.is_dsd:
            return self.dsd_label
        # 仅无损格式参与规格判定。
        if self._lossless_ext() not in self._LOSSLESS_EXTS:
            return ""
        if self.is_dxd:
            return "DXD"
        if self.is_hires:
            return "HR"
        if self.is_cd_quality:
            return "CD"
        return ""

    @property
    def exists(self) -> bool:
        return bool(self.filepath) and os.path.isfile(self.filepath)

    @property
    def play_url(self) -> str:
        """交给播放内核的地址：优先在线流，其次本地路径。"""
        return self.stream_url or self.filepath

    @property
    def playable(self) -> bool:
        """是否可交给播放内核：在线流，或本地文件存在。"""
        return self.is_stream or self.exists

    @staticmethod
    def format_seconds(seconds: float) -> str:
        seconds = int(seconds or 0)
        return f"{seconds // 60}:{seconds % 60:02d}"

    def apply_audio_info(self, info: dict) -> None:
        """应用解码器探测到的真实音频技术信息（覆盖未知字段）。"""
        if not isinstance(info, dict):
            return
        if info.get("sample_rate"):
            self.sample_rate = int(info["sample_rate"])
        if info.get("bit_depth"):
            self.bit_depth = int(info["bit_depth"])
        if info.get("channels"):
            self.channels = int(info["channels"])
        if info.get("bitrate"):
            self.bitrate = int(info["bitrate"])

    @property
    def format_label(self) -> str:
        """返回可读的音频格式描述，如 'FLAC · 44.1kHz · 16bit · 2ch'。"""
        parts = []
        # 本地用文件扩展名；在线用 stream_url 扩展名。
        # 注意：在线 stream_url 指向 Subsonic 的 `.view` 端点，
        # 其「扩展名」是 view 而非音频格式，须排除（否则显示 VIEW）。
        src = self.filepath or self.stream_url or ""
        ext = ""
        if "." in src:
            path_part = src.split("?", 1)[0]
            if "." in path_part:
                cand = path_part.rsplit(".", 1)[-1].upper()
                if cand not in _NON_AUDIO_EXTS:
                    ext = cand
        if ext:
            parts.append(ext)
        if self.sample_rate:
            khz = self.sample_rate / 1000
            parts.append(f"{khz:.1f}kHz".replace(".0kHz", "kHz"))
        if self.bit_depth:
            parts.append(f"{self.bit_depth}bit")
        if self.channels:
            from core.i18n import _ as _t
            ch = {1: _t("单声道"), 2: _t("立体声")}.get(self.channels, f"{self.channels}ch")
            parts.append(ch)
        if self.bitrate:
            parts.append(f"{self.bitrate // 1000}kbps")
        return " · ".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TrackItem {self.title!r} by {self.artist!r} [{self.source_type}]>"


#: 非音频的 URL「扩展名」（Subsonic 端点等），不应当作音频格式显示。
_NON_AUDIO_EXTS = frozenset({"VIEW", "JSON", "XML", "HTML", "PHP", "ASP"})
