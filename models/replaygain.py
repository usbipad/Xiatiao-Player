"""ReplayGain 标签读取（基于 mutagen）。

支持的标签：
- ID3（MP3）：TXXX:replaygain_track_gain / replaygain_album_gain
- Vorbis Comment（FLAC/OGG）：REPLAYGAIN_TRACK_GAIN / REPLAYGAIN_ALBUM_GAIN
- MP4（M4A）：----:com.apple.iTunes:replaygain_track_gain 等

返回增益（dB，float），无标签返回 None。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

try:
    from mutagen import File as MutagenFile

    _HAS_MUTAGEN = True
except Exception:
    _HAS_MUTAGEN = False

#: 匹配 "-7.23 dB" 或 "-7.23dB"
_DB_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)


def _parse_db(value) -> Optional[float]:
    """从标签值解析 dB 数。"""
    if value is None:
        return None
    try:
        text = value[0] if isinstance(value, (list, tuple)) else value
        if isinstance(text, bytes):
            text = text.decode("utf-8", "ignore")
        m = _DB_RE.search(str(text))
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def read_replaygain(path: str, mode: str = "track") -> Optional[float]:
    """读取文件的 ReplayGain 增益（dB）。

    mode: "track" 或 "album"。
    无标签返回 None。
    """
    if not _HAS_MUTAGEN or not path or not os.path.isfile(path):
        return None
    try:
        audio = MutagenFile(path)
        if audio is None:
            return None
        tags = audio.tags
        if tags is None:
            return None
        keys = [f"replaygain_{mode}_gain", f"REPLAYGAIN_{mode.upper()}_GAIN"]
        # 1. Vorbis Comment（FLAC/OGG）：tags 是 dict-like
        for k in keys:
            if k in tags:
                v = _parse_db(tags[k])
                if v is not None:
                    return v
        # 2. ID3（MP3）：TXXX 帧
        try:
            from mutagen.id3 import TXXX
            for frame in tags.getall("TXXX"):
                desc = (frame.desc or "").lower()
                if desc == f"replaygain_{mode}_gain":
                    v = _parse_db(frame.text)
                    if v is not None:
                        return v
        except Exception:
            pass
        # 3. MP4（M4A）：----:com.apple.iTunes:replaygain_track_gain
        try:
            for k in list(tags.keys()):
                kl = k.lower()
                if "replaygain" in kl and mode in kl and "gain" in kl:
                    v = _parse_db(tags[k])
                    if v is not None:
                        return v
        except Exception:
            pass
    except Exception as exc:
        log.debug("读取 ReplayGain 失败 %s: %s", path, exc)
    return None
