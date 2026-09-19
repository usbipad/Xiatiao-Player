"""本地歌单存储（SQLite）。

位置：$XDG_DATA_HOME/xiatiao/playlists.db
表：
  - playlists(id, name, created_at)
  - playlist_tracks(playlist_id, key, title, artist, album, duration,
                    duration_seconds, filepath, source_type, source_id,
                    cover_url, position)

曲目以 key（source_id / filepath）标识，同一歌单内去重；
删除歌单时级联删除其曲目。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

from ._base_store import SqliteStore, track_key

log = logging.getLogger(__name__)

DB_FILENAME = "playlists.db"


def _track_key(track) -> str:
    """兼容旧调用点的别名。"""
    return track_key(track)


class PlaylistStore(SqliteStore):
    """本地歌单存储。"""

    _db_filename = DB_FILENAME
    _foreign_keys = True

    def _init_db(self) -> None:
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS playlists (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        created_at REAL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS playlist_tracks (
                        playlist_id INTEGER NOT NULL,
                        key TEXT NOT NULL,
                        title TEXT,
                        artist TEXT,
                        album TEXT,
                        duration TEXT,
                        duration_seconds REAL,
                        filepath TEXT,
                        source_type TEXT,
                        source_id TEXT,
                        cover_url TEXT,
                        position INTEGER,
                        PRIMARY KEY (playlist_id, key)
                    )"""
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("初始化歌单库失败: %s", exc)

    # ---- 歌单增删改查 ----
    def create(self, name: str) -> Optional[int]:
        """新建歌单，返回 id；失败返回 None。"""
        try:
            with self._lock:
                conn = self._connect()
                cur = conn.execute(
                    "INSERT INTO playlists(name, created_at) VALUES (?, ?)",
                    ((name or "新歌单").strip() or "新歌单", time.time()),
                )
                conn.commit()
                return int(cur.lastrowid)
        except sqlite3.Error as exc:
            log.warning("新建歌单失败: %s", exc)
            return None

    def rename(self, playlist_id: int, name: str) -> bool:
        try:
            with self._lock:
                conn = self._connect()
                cur = conn.execute(
                    "UPDATE playlists SET name=? WHERE id=?",
                    ((name or "新歌单").strip() or "新歌单", int(playlist_id)),
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            log.warning("重命名歌单失败: %s", exc)
            return False

    def delete(self, playlist_id: int) -> bool:
        try:
            with self._lock:
                conn = self._connect()
                conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (int(playlist_id),))
                cur = conn.execute("DELETE FROM playlists WHERE id=?", (int(playlist_id),))
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            log.warning("删除歌单失败: %s", exc)
            return False

    def all_playlists(self) -> List[dict]:
        """全部歌单（含曲目数），按创建时间倒序。"""
        try:
            with self._lock:
                conn = self._connect()
                cur = conn.execute(
                    """SELECT p.id, p.name, p.created_at,
                              (SELECT COUNT(*) FROM playlist_tracks t WHERE t.playlist_id = p.id) AS cnt
                       FROM playlists p ORDER BY p.created_at DESC"""
                )
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error:
            return []

    def count(self) -> int:
        try:
            with self._lock:
                cur = self._connect().execute("SELECT COUNT(*) FROM playlists")
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    # ---- 歌单内曲目 ----
    def add_tracks(self, playlist_id: int, tracks) -> int:
        """把曲目加入歌单（去重），返回新增条数。"""
        if not tracks:
            return 0
        added = 0
        try:
            with self._lock:
                conn = self._connect()
                # 当前最大 position
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) FROM playlist_tracks WHERE playlist_id=?",
                    (int(playlist_id),),
                ).fetchone()
                pos = int(row[0]) + 1 if row else 0
                for t in tracks:
                    try:
                        cur = conn.execute(
                            """INSERT OR IGNORE INTO playlist_tracks
                            (playlist_id,key,title,artist,album,duration,duration_seconds,
                             filepath,source_type,source_id,cover_url,position)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                int(playlist_id),
                                _track_key(t),
                                getattr(t, "title", "") or "",
                                getattr(t, "artist", "") or "",
                                getattr(t, "album", "") or "",
                                getattr(t, "duration", "") or "",
                                float(getattr(t, "duration_seconds", 0.0) or 0.0),
                                getattr(t, "filepath", "") or "",
                                getattr(t, "source_type", "") or "",
                                getattr(t, "source_id", "") or "",
                                getattr(t, "cover_url", "") or "",
                                pos,
                            ),
                        )
                        if cur.rowcount > 0:
                            added += 1
                            pos += 1
                    except sqlite3.Error:
                        continue
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("加入歌单失败: %s", exc)
        return added

    def remove_track(self, playlist_id: int, key: str) -> bool:
        try:
            with self._lock:
                conn = self._connect()
                cur = conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id=? AND key=?",
                    (int(playlist_id), key),
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def rows_of(self, playlist_id: int) -> List[dict]:
        """歌单内全部曲目（按 position）。"""
        try:
            with self._lock:
                cur = self._connect().execute(
                    "SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
                    (int(playlist_id),),
                )
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error:
            return []


_holder: dict = {"instance": None}
_holder_lock = threading.Lock()


def get_playlist_store() -> PlaylistStore:
    with _holder_lock:
        if _holder["instance"] is None:
            _holder["instance"] = PlaylistStore()
        return _holder["instance"]
