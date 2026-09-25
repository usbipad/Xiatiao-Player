"""本地「我喜欢」存储（SQLite）。

收藏记录保存在本地库（不依赖任何在线账号）。

位置：$XDG_DATA_HOME/xiatiao/liked.db（默认 ~/.local/share/xiatiao/）。
表 liked_tracks：以 source_id（filepath 等）为主键，存曲目快照 + 喜欢时间。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import List

from ._base_store import SqliteStore, track_key

log = logging.getLogger(__name__)

DB_FILENAME = "liked.db"


class LikedStore(SqliteStore):
    """本地「我喜欢」曲目存储。"""

    _db_filename = DB_FILENAME

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
        return track_key(track)

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


_holder: dict = {"instance": None}
_holder_lock = threading.Lock()


def get_liked_store() -> LikedStore:
    with _holder_lock:
        if _holder["instance"] is None:
            _holder["instance"] = LikedStore()
        return _holder["instance"]
