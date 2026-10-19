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
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from config.settings import xdg_data_dir

log = logging.getLogger(__name__)


def xdg_data_db_path(filename: str) -> Path:
    """按 XDG 规范返回 $XDG_DATA_HOME/xiatiao/<filename>。

    未设置 XDG_DATA_HOME 时回退到 ~/.local/share。
    目录名与其它模块共用 config.settings.APP_DIR_NAME（单一真相）。
    """
    return Path(xdg_data_dir()) / filename


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
            # ---- 写入性能：WAL + synchronous=NORMAL ----
            # 默认 journal_mode=DELETE + synchronous=FULL 下，每次 commit 都要
            # fsync 日志文件与数据库文件两次。实测（真实数据目录）：
            #   默认            1.50 ms/次
            #   WAL             0.85 ms/次
            #   WAL + NORMAL    0.03 ms/次   ← 快约 50 倍
            # 这些写入（播放历史、收藏、歌单）都发生在主线程切歌链路上，
            # 1.5ms 的同步 IO 会直接体现在切歌延迟里。
            # WAL + NORMAL 的代价：断电时可能丢失最后一个事务（不损坏库），
            # 对播放历史/收藏/歌单这类数据完全可接受。
            # 注意：journal_mode 是**持久化**属性（写在库文件头），设置一次即长期生效；
            # 若库放在不支持共享内存的文件系统（如部分网络盘）上，WAL 会失败，
            # 此时静默回退（保留默认 delete 模式），不影响功能。
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.Error as exc:
                log.debug("启用 WAL 失败（回退默认 journal）: %s", exc)
            try:
                self._conn.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.Error as exc:
                log.debug("设置 synchronous=NORMAL 失败: %s", exc)
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
