"""本地「我喜欢」存储（SQLite）。

说明：上游 qq-music-api 没有写接口（微信登录也算不出 g_tk），
无法同步到 QQ 音乐账号，因此本地点喜欢存到本地库。

位置：$XDG_DATA_HOME/xiatiao/liked.db（默认 ~/.local/share/xiatiao/）。
表 liked_tracks：以 source_id（song_mid / filepath）为主键，存曲目快照 + 喜欢时间。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

APP_DIR_NAME = "xiatiao"
DB_FILENAME = "liked.db"


def _default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / APP_DIR_NAME / DB_FILENAME


class LikedStore:
    """本地「我喜欢」曲目存储。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_db_path()
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ---- 基础 ----
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS liked_tracks (
                        key TEXT PRIMARY KEY,
                        title TEXT,
                        artist TEXT,
                        album TEXT,
                        duration TEXT,
                        duration_seconds REAL,
                        filepath TEXT,
                        source_type TEXT,
                        source_id TEXT,
                        cover_url TEXT,
                        liked_at REAL
                    )"""
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("初始化喜欢库失败: %s", exc)

    @staticmethod
    def _key(track) -> str:
        sid = getattr(track, "source_id", "") or getattr(track, "filepath", "")
        if sid:
            return sid
        return f"{getattr(track, 'title', '')}|{getattr(track, 'artist', '')}"

    # ---- 增删查 ----
    def is_liked(self, track) -> bool:
        try:
            with self._lock:
                cur = self._connect().execute(
                    "SELECT 1 FROM liked_tracks WHERE key=? LIMIT 1", (self._key(track),)
                )
                return cur.fetchone() is not None
        except sqlite3.Error:
            return False

    def add(self, track) -> bool:
        """加入喜欢，返回是否新增（已存在返回 False）。"""
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    """INSERT OR REPLACE INTO liked_tracks
                    (key,title,artist,album,duration,duration_seconds,filepath,
                     source_type,source_id,cover_url,liked_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self._key(track),
                        getattr(track, "title", ""),
                        getattr(track, "artist", ""),
                        getattr(track, "album", ""),
                        getattr(track, "duration", ""),
                        float(getattr(track, "duration_seconds", 0.0) or 0.0),
                        getattr(track, "filepath", "") or "",
                        getattr(track, "source_type", "") or "",
                        getattr(track, "source_id", "") or "",
                        getattr(track, "cover_url", "") or "",
                        time.time(),
                    ),
                )
                conn.commit()
                return True
        except sqlite3.Error as exc:
            log.warning("加入喜欢失败: %s", exc)
            return False

    def remove(self, track) -> bool:
        try:
            with self._lock:
                conn = self._connect()
                cur = conn.execute(
                    "DELETE FROM liked_tracks WHERE key=?", (self._key(track),)
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            log.warning("取消喜欢失败: %s", exc)
            return False

    def toggle(self, track) -> bool:
        """切换喜欢状态，返回切换后是否已喜欢。"""
        if self.is_liked(track):
            self.remove(track)
            return False
        self.add(track)
        return True

    def all_rows(self) -> List[dict]:
        """全部喜欢曲目（按喜欢时间倒序）。"""
        try:
            with self._lock:
                cur = self._connect().execute(
                    "SELECT * FROM liked_tracks ORDER BY liked_at DESC"
                )
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error:
            return []

    def count(self) -> int:
        try:
            with self._lock:
                cur = self._connect().execute("SELECT COUNT(*) FROM liked_tracks")
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0


_instance: Optional[LikedStore] = None
_store_lock = threading.Lock()


def get_liked_store() -> LikedStore:
    global _instance
    with _store_lock:
        if _instance is None:
            _instance = LikedStore()
        return _instance
