"""播放历史存储（SQLite）。

记录每次播放的曲目，按最近播放时间倒序；同一曲目重复播放时更新时间戳
（不重复堆积），并可限制保留条数。

位置：$XDG_DATA_HOME/xiatiao/history.db
表 history_tracks：以 key（source_id / filepath）为主键，存曲目快照 + 最后播放时间。
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
DB_FILENAME = "history.db"
#: 最多保留条数（防止无限增长）
MAX_ROWS = 30


def _default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / APP_DIR_NAME / DB_FILENAME


class HistoryStore:
    """播放历史存储。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_db_path()
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

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
                    """CREATE TABLE IF NOT EXISTS history_tracks (
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
                        played_at REAL
                    )"""
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("初始化历史库失败: %s", exc)

    @staticmethod
    def _key(track) -> str:
        sid = getattr(track, "source_id", "") or getattr(track, "filepath", "")
        if sid:
            return sid
        return f"{getattr(track, 'title', '')}|{getattr(track, 'artist', '')}"

    def record(self, track) -> bool:
        """记录一次播放：同曲目更新时间戳（不重复堆积）。"""
        if track is None:
            return False
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    """INSERT OR REPLACE INTO history_tracks
                    (key,title,artist,album,duration,duration_seconds,filepath,
                     source_type,source_id,cover_url,played_at)
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
                # 修剪：只保留最近 MAX_ROWS 条
                conn.execute(
                    """DELETE FROM history_tracks WHERE key NOT IN
                    (SELECT key FROM history_tracks ORDER BY played_at DESC LIMIT ?)""",
                    (MAX_ROWS,),
                )
                conn.commit()
                return True
        except sqlite3.Error as exc:
            log.warning("记录播放历史失败: %s", exc)
            return False

    def all_rows(self) -> List[dict]:
        """全部历史（按播放时间倒序）。"""
        try:
            with self._lock:
                cur = self._connect().execute(
                    "SELECT * FROM history_tracks ORDER BY played_at DESC"
                )
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error:
            return []

    def clear(self) -> None:
        try:
            with self._lock:
                conn = self._connect()
                conn.execute("DELETE FROM history_tracks")
                conn.commit()
        except sqlite3.Error:
            pass

    def count(self) -> int:
        try:
            with self._lock:
                cur = self._connect().execute("SELECT COUNT(*) FROM history_tracks")
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0


_instance: Optional[HistoryStore] = None
_store_lock = threading.Lock()


def get_history_store() -> HistoryStore:
    global _instance
    with _store_lock:
        if _instance is None:
            _instance = HistoryStore()
        return _instance
