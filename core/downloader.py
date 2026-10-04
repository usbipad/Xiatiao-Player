"""在线曲目下载：音频流 + 封面 / 歌词 / 标签内嵌。

设计原则：
- 与 UI 解耦：只依赖传入的 provider、track 信息与回调，不碰 GTK 控件。
- 兼容标准 Subsonic：音质走标准 maxBitRate；歌词私有端点失败时回退
  标准 getLyrics；再失败则跳过。任何一步失败都不影响音频文件本身。
- 格式判定用文件头魔数（不信 Content-Type，服务端常标错 MIME）。
"""
from __future__ import annotations

import logging
import os
import socket
import urllib.error
import urllib.request

from core.quality import ONLINE_QUALITY_BITRATE, ONLINE_QUALITY_LABELS

log = logging.getLogger(__name__)

#: 音质档位 → maxBitRate（标准 Subsonic 参数）。单一真相见 core.quality。
QUALITY_MAX_BITRATE = dict(ONLINE_QUALITY_BITRATE)

#: 下拉框展示名（key, 显示名）。单一真相见 core.quality。
QUALITY_LABELS = list(ONLINE_QUALITY_LABELS)

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 60.0
MAX_TRIES = 5


def ext_from_magic(path: str) -> str:
    """由文件头魔数判定音频真实格式，返回扩展名（无法识别回退 flac）。"""
    try:
        with open(path, "rb") as fp:
            head = fp.read(16)
    except Exception:
        return "flac"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"MAC ":
        return "ape"
    if head[:4] == b"wvpk":
        return "wv"
    if head[:4] == b"DSD ":
        return "dsf"
    if head[:4] == b"FRM8":
        return "dff"
    if head[:4] == b"FORM" and head[8:12] == b"AIFF":
        return "aiff"
    return "flac"


def safe_filename(name: str) -> str:
    """替换文件名里的非法字符。"""
    import re
    s = re.sub(r'[\\/:*?"<>|]', "_", name or "")
    return s.strip() or "download"


def download_audio(url: str, dest_dir: str, base_name: str,
                   on_progress=None) -> str:
    """下载音频到 dest_dir，返回最终文件路径（扩展名按魔数判定）。

    url:     音频流地址（已按所选音质构造好）。
    on_progress: 可选回调 (已下载字节, 总字节)；总字节未知时为 0。
    兼容 Range 断点续传、超时重试、完成后大小校验。
    """
    os.makedirs(dest_dir, exist_ok=True)
    safe = safe_filename(base_name)

    accept_ranges = False
    total = 0
    try:
        hreq = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(hreq, timeout=CONNECT_TIMEOUT) as hr:
            accept_ranges = (hr.headers.get("Accept-Ranges") or "").lower() == "bytes"
            try:
                total = int(hr.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
    except Exception:
        pass

    tmp_path = os.path.join(dest_dir, f".{safe}.part")
    got = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0

    last_err = None
    for _try in range(MAX_TRIES):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if got > 0 and accept_ranges:
                headers["Range"] = f"bytes={got}-"
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(
                    req, timeout=max(READ_TIMEOUT, CONNECT_TIMEOUT)) as resp:
                if (resp.headers.get("Accept-Ranges") or "").lower() == "bytes":
                    accept_ranges = True
                status = getattr(resp, "status", 200)
                if got > 0 and status == 200:
                    got = 0
                mode = "ab" if (got > 0 and status == 206) else "wb"
                if mode == "wb":
                    got = 0
                with open(tmp_path, mode) as fp:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fp.write(chunk)
                        got += len(chunk)
                        if on_progress:
                            try:
                                on_progress(got, total)
                            except Exception:
                                pass
            if total and got != total:
                last_err = f"大小不符（{got}/{total}）"
                continue
            if got <= 0:
                last_err = "文件为空"
                continue
            real_ext = ext_from_magic(tmp_path)
            final_path = os.path.join(dest_dir, f"{safe}.{real_ext}")
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
                os.replace(tmp_path, final_path)
            except Exception:
                final_path = tmp_path
            return final_path
        except (socket.timeout, TimeoutError) as exc:
            last_err = f"超时: {exc}"
            got = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            if not accept_ranges:
                break
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_err = exc
            got = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            if not accept_ranges:
                break
        except Exception as exc:
            last_err = exc
            got = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            if not accept_ranges:
                break
    raise RuntimeError(str(last_err) if last_err else "下载失败")


def fetch_bytes(url: str, timeout: float = 20.0) -> bytes | None:
    """下载小文件（封面等）为字节；失败返回 None。"""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        log.debug("下载字节失败 %s: %s", url, exc)
        return None


def write_metadata(path: str, *, title: str = "", artist: str = "",
                   album: str = "", cover_bytes: bytes | None = None,
                   lyrics_lrc: str = "") -> bool:
    """把标签 / 封面 / 歌词写入音频文件（尽力而为，失败返回 False）。

    支持 FLAC / MP3 / OGG / M4A；其他格式跳过。任一步失败仅记录，
    不影响音频文件可播放性。
    """
    try:
        from mutagen import File as MutagenFile
    except Exception:
        log.info("mutagen 不可用，跳过元数据写入")
        return False

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".flac":
            return _write_flac(path, title, artist, album, cover_bytes, lyrics_lrc)
        if ext == ".mp3":
            return _write_mp3(path, title, artist, album, cover_bytes, lyrics_lrc)
        if ext == ".ogg":
            return _write_ogg(path, title, artist, album, cover_bytes, lyrics_lrc)
        if ext in (".m4a", ".mp4"):
            return _write_m4a(path, title, artist, album, cover_bytes, lyrics_lrc)
    except Exception as exc:
        log.info("写入元数据失败（%s）: %s", ext, exc)
        return False

    # 其他格式：仅尝试通用标签（不改封面/歌词）。
    try:
        audio = MutagenFile(path)
        if audio is None:
            return False
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
        return True
    except Exception as exc:
        log.debug("通用标签写入失败: %s", exc)
        return False


def _write_flac(path, title, artist, album, cover_bytes, lyrics_lrc) -> bool:
    from mutagen.flac import FLAC, Picture
    audio = FLAC(path)
    if title:
        audio["title"] = title
    if artist:
        audio["artist"] = artist
    if album:
        audio["album"] = album
    if lyrics_lrc:
        audio["lyrics"] = lyrics_lrc
    if cover_bytes:
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = _guess_image_mime(cover_bytes)
        pic.desc = "Cover"
        pic.data = cover_bytes
        audio.add_picture(pic)
    audio.save()
    return True


def _write_mp3(path, title, artist, album, cover_bytes, lyrics_lrc) -> bool:
    from mutagen.id3 import (ID3, TIT2, TPE1, TALB, APIC, USLT, ID3NoHeaderError)
    try:
        audio = ID3(path)
    except ID3NoHeaderError:
        audio = ID3()
    if title:
        audio.add(TIT2(encoding=3, text=title))
    if artist:
        audio.add(TPE1(encoding=3, text=artist))
    if album:
        audio.add(TALB(encoding=3, text=album))
    if lyrics_lrc:
        audio.add(USLT(encoding=3, lang="eng", desc="", text=lyrics_lrc))
    if cover_bytes:
        audio.add(APIC(encoding=3, mime=_guess_image_mime(cover_bytes),
                       type=3, desc="Cover", data=cover_bytes))
    audio.save(path)
    return True


def _write_ogg(path, title, artist, album, cover_bytes, lyrics_lrc) -> bool:
    import base64
    from mutagen.flac import Picture
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(path)
    if title:
        audio["title"] = title
    if artist:
        audio["artist"] = artist
    if album:
        audio["album"] = album
    if lyrics_lrc:
        audio["lyrics"] = lyrics_lrc
    if cover_bytes:
        pic = Picture()
        pic.type = 3
        pic.mime = _guess_image_mime(cover_bytes)
        pic.desc = "Cover"
        pic.data = cover_bytes
        audio["metadata_block_picture"] = [
            base64.b64encode(pic.write()).decode("ascii")
        ]
    audio.save()
    return True


def _write_m4a(path, title, artist, album, cover_bytes, lyrics_lrc) -> bool:
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(path)
    if title:
        audio["\xa9nam"] = title
    if artist:
        audio["\xa9ART"] = artist
    if album:
        audio["\xa9alb"] = album
    if lyrics_lrc:
        audio["\xa9lyr"] = lyrics_lrc
    if cover_bytes:
        fmt = (MP4Cover.FORMAT_PNG
               if cover_bytes[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG)
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
    audio.save()
    return True


def _guess_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    return "image/jpeg"


def structured_lyrics_to_lrc(body: dict) -> str:
    """把 Subsonic 结构化歌词（getLyricsBySongId）转成 LRC 文本。

    兼容：value 自带 [mm:ss] 标签 → 直接用；否则按 start（毫秒/秒）生成。
    无结构化则回退 lyrics 纯文本字段。
    """
    try:
        node = body.get("lyricsList") if isinstance(body, dict) else None
        lines_out = []
        if isinstance(node, dict):
            sl = node.get("structuredLyrics")
            if isinstance(sl, dict):
                sl = [sl]
            if isinstance(sl, list):
                for one in sl:
                    if not isinstance(one, dict):
                        continue
                    lines = one.get("line")
                    if isinstance(lines, dict):
                        lines = [lines]
                    if not isinstance(lines, list):
                        continue
                    for ln in lines:
                        if not isinstance(ln, dict):
                            continue
                        val = str(ln.get("value", "") or "")
                        if "[" in val and "]" in val:
                            lines_out.append(val)
                            continue
                        sec = ln.get("start", 0) or 0
                        try:
                            sec = float(sec)
                        except Exception:
                            sec = 0.0
                        if sec > 10000:
                            sec = sec / 1000.0
                        m = int(sec // 60)
                        s = sec - m * 60
                        lines_out.append(f"[{m:02d}:{s:05.2f}]{val}")
        if lines_out:
            return "\n".join(lines_out)
        # 回退纯文本歌词
        if isinstance(body, dict):
            lyrics = body.get("lyrics")
            if isinstance(lyrics, str) and lyrics.strip():
                return lyrics
            if isinstance(lyrics, dict):
                v = lyrics.get("value")
                if isinstance(v, str):
                    return v
    except Exception:
        pass
    return ""
