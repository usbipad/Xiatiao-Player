"""歌词读取与解析。

- 优先读取同名 .lrc 文件
- 回退读取音频内嵌歌词（ID3 USLT / Vorbis LYRICS）
- 解析 LRC 时间标签，返回按时间排序的 (秒, 文本) 列表
"""
from __future__ import annotations

import html
import logging
import os
import re

log = logging.getLogger(__name__)


def _unescape(text: str) -> str:
    """解码歌词里的 HTML 实体（&apos; &amp; &quot; 等）。

    歌词文本常带 HTML 转义（在线/内嵌/本地都可能），
    统一解码成正常字符，避免显示成 &apos; 这种。
    """
    if not text:
        return text
    try:
        return html.unescape(text)
    except Exception:
        return text

# [mm:ss.xx] 或 [mm:ss] 或 [mm:ss.xxx]
_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def _parse_lrc(text: str) -> list[tuple[float, str]]:
    """解析 LRC 文本，返回 [(秒, 文本)] 按时间升序。"""
    lines: list[tuple[float, str]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        # 跳过元信息标签 [ar:] [ti:] 等（非时间标签）
        matches = list(_TIME_RE.finditer(raw))
        if not matches:
            continue
        # 去掉时间标签后的正文，并解码 HTML 实体
        content = _unescape(_TIME_RE.sub("", raw).strip())
        for m in matches:
            mm = int(m.group(1))
            ss = int(m.group(2))
            frac = m.group(3)
            if frac:
                # 1~3 位小数，统一成秒的小数
                sec = int(frac) / (10 ** len(frac))
            else:
                sec = 0.0
            total = mm * 60 + ss + sec
            lines.append((total, content))
    lines.sort(key=lambda x: x[0])
    return lines


def _read_lrc_file(path: str) -> str | None:
    """读取与音频同名的 .lrc 文件内容。"""
    base = os.path.splitext(path)[0]
    for ext in (".lrc", ".LRC"):
        lrc_path = base + ext
        if os.path.isfile(lrc_path):
            for enc in ("utf-8", "gbk", "utf-16"):
                try:
                    with open(lrc_path, "r", encoding=enc) as fp:
                        return fp.read()
                except (UnicodeDecodeError, OSError):
                    continue
    return None


def _read_embedded_lyrics(path: str) -> str | None:
    """读取音频内嵌歌词（需要 mutagen）。"""
    try:
        from mutagen import File as MutagenFile
    except Exception:
        return None
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3

            tags = ID3(path)
            for key in tags.keys():
                if key.startswith("USLT"):
                    return str(tags[key].text)
        audio = MutagenFile(path)
        if audio is not None and audio.tags:
            for key in ("LYRICS", "UNSYNCEDLYRICS", "\xa9lyr"):
                if key in audio.tags:
                    val = audio.tags[key]
                    if isinstance(val, list):
                        val = val[0]
                    return str(val)
    except Exception as exc:
        log.debug("读取内嵌歌词失败 %s: %s", path, exc)
    return None


def parse_lrc_text(text: str) -> list[tuple[float, str]]:
    """解析 LRC 文本（在线歌词），返回 [(秒, 文本)] 升序。

    无时间标签时按纯文本逐行处理（时间 0）。
    """
    if not text:
        return []
    parsed = _parse_lrc(text)
    if parsed:
        return parsed
    stripped = text.strip()
    if stripped:
        return [(0.0, _unescape(line)) for line in stripped.splitlines() if line.strip()]
    return []


def load_lyrics(path: str) -> list[tuple[float, str]]:
    """读取并解析歌词，返回 [(秒, 文本)]；无歌词返回空列表。

    优先同名 .lrc，其次内嵌歌词。
    """
    if not path:
        return []
    text = _read_lrc_file(path)
    if not text:
        text = _read_embedded_lyrics(path)
    if not text:
        return []
    # 内嵌歌词可能是纯文本（无时间标签）
    parsed = _parse_lrc(text)
    if parsed:
        return parsed
    # 无时间标签：整段作为一行（0 秒）
    stripped = text.strip()
    if stripped:
        return [(0.0, _unescape(line)) for line in stripped.splitlines() if line.strip()]
    return []
