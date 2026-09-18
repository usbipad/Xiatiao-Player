"""本地音源 Provider：扫描配置目录，读取元数据。"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from gi.repository import GLib, GObject

from config.settings import SUPPORTED_EXTENSIONS, get_config
from core.tasks import run_async
from models import TrackItem, SOURCE_LOCAL

from .base import BaseMusicProvider

log = logging.getLogger(__name__)

# 尝试启用 GStreamer Discoverer 读取元数据；不可用则回退文件名。
try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import Gst, GstPbutils

    _HAS_GST = True
except Exception:  # pragma: no cover - 环境相关
    Gst = None  # type: ignore
    GstPbutils = None  # type: ignore
    _HAS_GST = False


class LocalProvider(BaseMusicProvider):
    """读取本地音乐目录列表中的音频文件。"""

    __gtype_name__ = "LocalProvider"

    source_type = SOURCE_LOCAL
    display_name = "本地曲库"
    # 本地：有曲库/播放列表；无在线推荐/mixes（在线音源才提供）
    capabilities = {"library", "playlists"}

    def __init__(self) -> None:
        super().__init__()
        self._tracks: List[TrackItem] = []
        self._refresh_token = None
        self._discoverer = None
        self._init_discoverer()

    # ---- 元数据读取 ----

    def _init_discoverer(self) -> None:
        if not _HAS_GST or GstPbutils is None:
            log.info("GStreamer 不可用，本地元数据将仅从文件名推断")
            return
        try:
            if not Gst.is_initialized():
                Gst.init(None)
            self._discoverer = GstPbutils.Discoverer.new(GLib.MAXINT)
        except Exception as exc:  # pragma: no cover
            log.warning("初始化 Discoverer 失败: %s", exc)
            self._discoverer = None

    @staticmethod
    def _tag_string(tags, tag_name: str) -> str:
        """从 Gst.TagList 安全读取字符串标签。"""
        if tags is None:
            return ""
        try:
            ok, value = tags.get_string(tag_name)
            return value if ok and value else ""
        except Exception:
            return ""

    @staticmethod
    def _read_tech_mutagen(path: str):
        """用 mutagen 读音频技术参数（采样率/位深/声道/码率）。

        GStreamer Discoverer 对部分 FLAC 读不出这些字段（返回 0），
        mutagen 更可靠。返回 dict 或 None。
        """
        try:
            from mutagen import File as MFile
        except ImportError:
            return None
        try:
            m = MFile(path)
            info = getattr(m, "info", None) if m is not None else None
            if info is None:
                return None
            return {
                "sample_rate": int(getattr(info, "sample_rate", 0) or 0),
                "bit_depth": int(getattr(info, "bits_per_sample", 0) or 0),
                "channels": int(getattr(info, "channels", 0) or 0),
                "bitrate": int(getattr(info, "bitrate", 0) or 0),
            }
        except Exception:
            return None

    @staticmethod
    def _read_tags_mutagen(path: str):
        """用 mutagen 读标签（比 Gst 可靠，尤其 FLAC/MP3 的 album）。

        返回 (title, artist, album)；读不到返回 None。
        """
        try:
            from mutagen import File as MFile
        except ImportError:
            return None
        try:
            m = MFile(path)
            if m is None or m.tags is None:
                return None

            def _first(*keys):
                for k in keys:
                    v = m.tags.get(k)
                    if v is None:
                        continue
                    if isinstance(v, (list, tuple)):
                        if v:
                            return str(v[0]).strip()
                    else:
                        s = str(v).strip()
                        if s:
                            return s
                return ""

            return (_first("title", "TIT2"), _first("artist", "TPE1"), _first("album", "TALB"))
        except Exception:
            return None

    def _read_metadata(self, path: str) -> TrackItem:
        fallback = self._from_filename(path)
        # 优先 mutagen 读标签（Gst 常读不到 FLAC 的 album）
        meta = self._read_tags_mutagen(path)
        if self._discoverer is None:
            tech = self._read_tech_mutagen(path) or {}
            if meta is not None:
                t, a, al = meta
                return TrackItem(
                    title=t or fallback.title, artist=a or fallback.artist, album=al,
                    duration=fallback.duration, duration_seconds=fallback.duration_seconds,
                    filepath=path, source_type=SOURCE_LOCAL, source_id=path,
                    sample_rate=tech.get("sample_rate", 0),
                    bit_depth=tech.get("bit_depth", 0),
                    channels=tech.get("channels", 0),
                    bitrate=tech.get("bitrate", 0),
                )
            return fallback
        try:
            uri = GLib.filename_to_uri(path, None)
            info = self._discoverer.discover_uri(uri)
            if info is None:
                return fallback

            tags = info.get_tags()
            title = self._tag_string(tags, Gst.TAG_TITLE) or fallback.title
            artist = self._tag_string(tags, Gst.TAG_ARTIST) or fallback.artist
            album = self._tag_string(tags, Gst.TAG_ALBUM)
            # mutagen 结果优先（更可靠）
            if meta is not None:
                mt, ma, mal = meta
                title = mt or title or fallback.title
                artist = ma or artist or fallback.artist
                album = mal or album

            dur_ns = info.get_duration()
            dur_sec = (dur_ns / Gst.SECOND) if dur_ns and dur_ns > 0 else 0.0

            # 音频流技术参数：优先 mutagen（Gst 对部分 FLAC 读不全）
            rate = depth = channels = bitrate = 0
            tech = self._read_tech_mutagen(path)
            if tech:
                rate = tech.get("sample_rate", 0) or 0
                depth = tech.get("bit_depth", 0) or 0
                channels = tech.get("channels", 0) or 0
                bitrate = tech.get("bitrate", 0) or 0
            # mutagen 缺失的字段再用 GStreamer 补
            streams = info.get_audio_streams()
            if streams:
                s = streams[0]
                if not rate:
                    rate = s.get_sample_rate() or 0
                if not depth:
                    depth = s.get_depth() or 0
                if not channels:
                    channels = s.get_channels() or 0
                if not bitrate:
                    bitrate = s.get_bitrate() or 0
            # 仍无码率：用文件大小/时长估算
            if bitrate <= 0 and dur_sec > 0:
                try:
                    size_bits = os.path.getsize(path) * 8
                    bitrate = int(size_bits / dur_sec)
                except OSError:
                    bitrate = 0

            return TrackItem(
                title=title,
                artist=artist,
                album=album,
                duration=TrackItem.format_seconds(dur_sec),
                duration_seconds=dur_sec,
                filepath=path,
                source_type=SOURCE_LOCAL,
                source_id=path,
                sample_rate=rate,
                bit_depth=depth,
                channels=channels,
                bitrate=bitrate,
            )
        except Exception as exc:  # 单个文件失败不影响整体
            log.debug("读取元数据失败 %s: %s", path, exc)
            return fallback

    @staticmethod
    def _from_filename(path: str) -> TrackItem:
        name = os.path.splitext(os.path.basename(path))[0]
        title, artist = name, "Unknown"
        if " - " in name:
            parts = name.split(" - ", 1)
            artist, title = parts[0].strip(), parts[1].strip()
        return TrackItem(
            title=title or name,
            artist=artist,
            duration="0:00",
            duration_seconds=0.0,
            filepath=path,
            source_type=SOURCE_LOCAL,
            source_id=path,
        )

    # ---- 扫描 ----

    def _scan_files(self) -> tuple[List[str], List[str]]:
        """扫描音乐目录，返回 (文件列表, 错误信息列表)。

        注意：本方法在后台线程执行，绝不 emit 信号（GTK 信号须主线程发）。
        错误信息收集后由主线程统一发。
        """
        found: List[str] = []
        errors: List[str] = []
        for directory in get_config().get_music_dirs():
            if not os.path.isdir(directory):
                errors.append(f"目录不存在: {directory}")
                continue
            try:
                # followlinks=True：进入目录软链接（用户常把曲库软链到其他磁盘）。
                # 为防软链接成环导致无限递归，用已访问真实路径做去重。
                seen_dirs: set[str] = set()
                for root, dirs, files in os.walk(directory, followlinks=True):
                    try:
                        real = os.path.realpath(root)
                    except OSError:
                        real = root
                    if real in seen_dirs:
                        # 该目录已访问过（软链接回环），剪枝
                        dirs[:] = []
                        continue
                    seen_dirs.add(real)
                    for fname in files:
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in SUPPORTED_EXTENSIONS:
                            found.append(os.path.join(root, fname))
            except PermissionError:
                errors.append(f"目录权限不足: {directory}")
            except OSError as exc:
                errors.append(f"扫描目录失败 {directory}: {exc}")
        return found, errors

    def refresh(self) -> None:
        """异步刷新曲库：后台扫描 + 读元数据，主线程发信号。

        避免阻塞 UI；重复调用会取消上一次未完成的任务。
        """
        # 取消上一次未完成的刷新
        if self._refresh_token is not None:
            self._refresh_token.cancel()
        self._refresh_token = run_async(
            work=self._scan_and_read,
            on_done=self._on_refresh_done,
            on_error=self._on_refresh_error,
        )

    def _scan_and_read(self) -> tuple[List[TrackItem], List[str]]:
        """后台线程执行：扫描文件并逐个读元数据（不发信号）。"""
        files, errors = self._scan_files()
        tracks: List[TrackItem] = []
        for path in files:
            tracks.append(self._read_metadata(path))
        return tracks, errors

    def _on_refresh_done(self, result) -> None:
        """主线程：保存结果、发错误与变更信号。"""
        tracks, errors = result
        self._tracks = tracks
        for msg in errors:
            self.emit("error", msg)
        self.emit("library-changed")

    def _on_refresh_error(self, exc: Exception) -> None:
        self.emit("error", f"扫描失败: {exc}")

    # ---- 接口实现 ----

    def get_library(self) -> List[TrackItem]:
        return list(self._tracks)

    def search(self, query: str) -> List[TrackItem]:
        q = (query or "").strip().lower()
        if not q:
            return self.get_library()
        return [
            t
            for t in self._tracks
            if q in t.title.lower() or q in t.artist.lower() or q in t.album.lower()
        ]
