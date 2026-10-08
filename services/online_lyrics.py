"""在线（Subsonic）歌词加载与解析。

从 ui/window.py 的 _load_online_lyrics / _dedupe_lyrics 抽出。
抽出理由：这是**纯逻辑**（网络请求 + 文本解析），不碰任何 GTK 控件，
放 window（上帝对象）里既臃肿又难测。抽出后：
  - window 只调用 load_online_lyrics(track, provider)；
  - 逻辑可独立单测（见 tests/test_online_lyrics.py）。

依赖：仅需一个能提供 get_lyrics_by_song_id / get_lyrics 的 provider
（providers.subsonic.SubsonicProvider）。
"""
from __future__ import annotations

import logging
from typing import List, Tuple

log = logging.getLogger(__name__)


#: 歌词项类型：(秒, 文本)
LyricItem = Tuple[float, str]


def dedupe_lyrics(items: List[LyricItem]) -> List[LyricItem]:
    """去掉「连续重复」的歌词行。

    与后端 xiatiao-api 的 dedupTimed 一致：只做「相邻相同才合并」兜底。
    **绝不跨时间全局去重**——否则会把正常重复的副歌（Chorus）删掉，
    导致歌曲后半段无歌词（如《相思》副歌重复三次，全局去重会删掉第二、三次）。
    """
    out: List[LyricItem] = []
    last_key = None
    for sec, text in (items or []):
        key = (text or "").strip()
        if key == last_key:
            # 与上一行完全相同（逐字/逐句平铺特征）→ 跳过
            continue
        last_key = key
        out.append((sec, text))
    return out


def load_online_lyrics(track, provider) -> List[LyricItem]:
    """在线歌歌词：调 Subsonic getLyricsBySongId，转成 [(秒, 文本)]。

    track:    TrackItem（需 source_type=='subsonic' 且有 source_id）
    provider: 具备 get_lyrics_by_song_id / get_lyrics 的在线音源 provider；
              None 时返回空。

    在后台线程调用（网络请求，不碰 UI）。失败返回 []。
    兼容：结构化歌词（line[].value/start）；无结构化则回退纯文本歌词。
    """
    try:
        sid = getattr(track, "source_id", "") or ""
        if not sid or getattr(track, "source_type", "") != "subsonic":
            return []
        if provider is None:
            return []
        from models import parse_lrc_text
        # 优先结构化歌词
        try:
            body = provider.get_lyrics_by_song_id(sid)
            node = body.get("lyricsList")
            items: List[LyricItem] = []
            raw_lrc_chunks: List[str] = []
            if isinstance(node, dict):
                sl = node.get("structuredLyrics")
                if isinstance(sl, dict):
                    sl = [sl]
                if isinstance(sl, list):
                    for one in sl:
                        lines = one.get("line") if isinstance(one, dict) else None
                        if isinstance(lines, dict):
                            lines = [lines]
                        if not isinstance(lines, list):
                            continue
                        for ln in lines:
                            if not isinstance(ln, dict):
                                continue
                            val = str(ln.get("value", "") or "")
                            # 兼容：value 里若自带 [mm:ss] 时间标签，
                            # 交给 parse_lrc_text 解析，忽略可能单位不一致的 start。
                            if "[" in val and "]" in val:
                                raw_lrc_chunks.append(val)
                                continue
                            start = ln.get("start", 0) or 0
                            try:
                                sec = float(start)
                            except Exception:
                                sec = 0.0
                            # OpenSubsonic 规定 start 为**毫秒**，直接归一。
                            # （旧实现用「>10000 才算毫秒」的启发式，会把
                            #  6.12s=6120ms 这类 <10000 的毫秒值误当秒，
                            #  导致开头几句时间轴错乱、歌词不动。）
                            sec = sec / 1000.0
                            items.append((sec, val))
            # 结构化里含 LRC 原文 → 统一解析后合并
            if raw_lrc_chunks:
                parsed = parse_lrc_text("\n".join(raw_lrc_chunks))
                if parsed:
                    items = parsed
            if items:
                return dedupe_lyrics(items)
        except Exception:
            pass
        # 回退：传统 getLyrics。
        # 后端常返回带 [mm:ss.xx] 时间标签的 LRC 文本，
        # 必须用 parse_lrc_text 解析时间轴；无标签时它按纯文本处理。
        try:
            body = provider.get_lyrics(
                artist=getattr(track, "artist", "") or "",
                title=getattr(track, "title", "") or "")
            lyr = body.get("lyrics")
            if isinstance(lyr, dict):
                lyr = lyr.get("value", "") or ""
            if isinstance(lyr, str) and lyr.strip():
                parsed = parse_lrc_text(lyr)
                if parsed:
                    return dedupe_lyrics(parsed)
        except Exception:
            pass
    except Exception as exc:
        log.debug("在线歌词加载失败: %s", exc)
    return []
