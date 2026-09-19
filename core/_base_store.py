"""SQLite store 公共基类。

liked / history / playlist 三个 store 共用同一套模式：
  - 按 XDG 规范定位数据库路径；
  - 懒连接（check_same_thread=False + Row 工厂）；
  - 线程锁保护；
  - 统一的 key 提取（source_id / filepath / title|artist 兜底）。

抽取基类只为消除重复；各子类的表结构、SQL、业务方法保持在各自文件中。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: 应用数据目录名（$XDG_DATA_HOME 下）。
APP_DIR_NAME = "xiatiao"


def xdg_data_db_path(filename: str) -> Path:
    """按 XDG 规范返回 $XDG_DATA_HOME/xiatiao/<filename>。

    未设置 XDG_DATA_HOME 时回退到 ~/.local/share。
    """
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / APP_DIR_NAME / filename


def track_key(track) -> str:
    """曲目唯一键：优先 source_id，其次 filepath，最后 title|artist 兜底。"""
    sid = getattr(track, "source_id", "") or getattr(track, "filepath", "")
    if sid:
        return sid
    return f"{getattr(track, 'title', '')}|{getattr(track, 'artist', '')}"


class SqliteStore:
    """SQLite store 基类：连接管理 + 线程锁 + 路径。

    子类在 __init__ 中设置 self._db_filename，并调用 super().__init__(path)，
    然后在 _init_db 中建表。
    """

    #: 数据库文件名（子类覆盖）。
    _db_filename: str = ""
    #: 是否启用外键（子类可覆盖）。
    _foreign_keys: bool = False

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or xdg_data_db_path(self._db_filename)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """懒连接：首次调用时创建连接并建父目录。"""
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            if self._foreign_keys:
                try:
                    self._conn.execute("PRAGMA foreign_keys = ON")
                except sqlite3.Error as exc:
                    log.debug("启用外键失败: %s", exc)
        return self._conn

    def _init_db(self) -> None:
        """建表（子类覆盖）。基类默认无操作。"""

    @staticmethod
    def _key(track) -> str:
        return track_key(track)
