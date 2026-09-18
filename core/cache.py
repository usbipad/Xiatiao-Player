"""本地内容缓存（XDG 缓存目录 + JSON）。

用途：退出时把各页面数据落盘，下次启动秒显，再后台刷新。
位置：$XDG_CACHE_HOME/xiatiao/cache.json（默认 ~/.cache/xiatiao/）。

设计：
- 单例 get_cache()，线程安全（锁）。
- 分块存储：home / explore / collection / local / user。
- 写入原子（临时文件 + replace），失败不影响主流程。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

APP_DIR_NAME = "xiatiao"
CACHE_FILENAME = "cache.json"

#: 允许缓存的块名（防止误写大对象）
CACHE_KEYS = ("home", "explore", "collection", "local", "user", "queue")


def _default_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / APP_DIR_NAME / CACHE_FILENAME


class AppCache:
    """应用内容缓存。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_cache_path()
        self._data: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        with self._lock:
            if not self._path.is_file():
                self._data = {}
                return
            try:
                with self._path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                self._data = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("读取缓存失败，忽略: %s", exc)
                self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def set(self, key: str, value: Any) -> None:
        """仅内存更新，不落盘（退出时统一 save）。"""
        if key not in CACHE_KEYS:
            return
        with self._lock:
            self._data[key] = value

    def save(self) -> None:
        """原子写入缓存文件。"""
        with self._lock:
            data = dict(self._data)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False)
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("保存缓存失败: %s", exc)

    def clear(self) -> None:
        with self._lock:
            self._data = {}
        try:
            if self._path.is_file():
                self._path.unlink()
        except OSError as exc:
            log.warning("清除缓存失败: %s", exc)


_instance: Optional[AppCache] = None
_cache_lock = threading.Lock()


def track_to_dict(track) -> dict:
    """TrackItem -> 可 JSON 化的 dict（缓存用）。"""
    return {
        "title": getattr(track, "title", ""),
        "artist": getattr(track, "artist", ""),
        "album": getattr(track, "album", ""),
        "duration": getattr(track, "duration", ""),
        "duration_seconds": float(getattr(track, "duration_seconds", 0.0) or 0.0),
        "filepath": getattr(track, "filepath", ""),
        "source_type": getattr(track, "source_type", "local"),
        "source_id": getattr(track, "source_id", ""),
        "cover_url": getattr(track, "cover_url", "") or "",
        # 音频技术参数：一并缓存，避免重启后丢失（弹窗显示"未知"）
        "sample_rate": int(getattr(track, "sample_rate", 0) or 0),
        "bit_depth": int(getattr(track, "bit_depth", 0) or 0),
        "channels": int(getattr(track, "channels", 0) or 0),
        "bitrate": int(getattr(track, "bitrate", 0) or 0),
    }


def dict_to_track(data: dict):
    """dict -> TrackItem（从缓存恢复用）。"""
    from models import TrackItem
    if not isinstance(data, dict):
        return None
    try:
        return TrackItem(
            title=data.get("title") or "未知歌曲",
            artist=data.get("artist") or "未知歌手",
            album=data.get("album") or "",
            duration=data.get("duration") or "0:00",
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            filepath=data.get("filepath") or "",
            source_type=data.get("source_type") or "local",
            source_id=data.get("source_id") or "",
            cover_url=data.get("cover_url") or "",
            sample_rate=int(data.get("sample_rate") or 0),
            bit_depth=int(data.get("bit_depth") or 0),
            channels=int(data.get("channels") or 0),
            bitrate=int(data.get("bitrate") or 0),
        )
    except Exception:
        return None


def tracks_to_list(tracks) -> list:
    return [track_to_dict(t) for t in (tracks or [])]


def list_to_tracks(items) -> list:
    out = []
    for it in (items or []):
        t = dict_to_track(it)
        if t is not None:
            out.append(t)
    return out


def get_cache() -> AppCache:
    global _instance
    with _cache_lock:
        if _instance is None:
            _instance = AppCache()
        return _instance
