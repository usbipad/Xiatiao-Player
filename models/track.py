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

        判定标准（超过 CD 规格 44.1kHz / 16bit 即视为 Hi-Res）：
        - 采样率 > 48kHz（如 88.2 / 96 / 176.4 / 192kHz），或
        - 位深 > 16bit（如 24bit / 32bit）
        信息未知（0）时不显示徽标。
        """
        try:
            if self.sample_rate and self.sample_rate > 48000:
                return True
            if self.bit_depth and self.bit_depth > 16:
                return True
        except Exception:
            return False
        return False

    @property
    def is_cd_quality(self) -> bool:
        """是否为 CD 级音质（无损 + 16bit + <= 48kHz）。

        判定标准：
        - 位深 == 16bit（CD 规格），且
        - 采样率 <= 48kHz（44.1kHz 为标准 CD，48kHz 亦归入 CD 级），且
        - 文件格式为无损（flac / ape / wav / wv / alac / aiff / aif）
        有损格式（mp3 / aac / ogg / m4a 等）即使参数相同也不算 CD 级。
        信息未知（0）时不显示。
        """
        try:
            # 位深必须已知且为 16bit
            if not self.bit_depth or self.bit_depth != 16:
                return False
            # 采样率必须已知且 <= 48kHz
            if not self.sample_rate or self.sample_rate > 48000:
                return False
            # 必须是无损格式
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
    def quality_badge(self) -> str:
        """返回音质徽标文案：'DSD' / 'HR' / 'CD' / ''（无徽标）。

        互斥，优先级 DSD > Hi-Res > CD 级。
        """
        if self.is_dsd:
            return "DSD"
        if self.is_hires:
            return "HR"
        if self.is_cd_quality and self._lossless_ext() in self._LOSSLESS_EXTS:
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
        # 本地用文件扩展名；在线用 stream_url 扩展名
        src = self.filepath or self.stream_url or ""
        ext = ""
        if "." in src:
            # 去掉 query string 后取扩展名
            path_part = src.split("?", 1)[0]
            if "." in path_part:
                ext = path_part.rsplit(".", 1)[-1].upper()
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
