"""播放队列管理器。

只维护“播放什么、当前第几首”，不负责解码播放（那是 PlayerCore 的职责）。
仅 local 类型曲目会被交给播放内核。
"""
from __future__ import annotations

import random
from typing import List, Optional

from gi.repository import GObject

from models import TrackItem

# 循环模式
REPEAT_OFF = 0       # 不循环：到末尾停
REPEAT_ALL = 1       # 列表循环
REPEAT_ONE = 2       # 单曲循环


class Playlist(GObject.Object):
    """播放队列。"""

    __gtype_name__ = "Playlist"

    __gsignals__ = {
        # 队列内容变化
        "changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # 当前曲目变化，param: int 索引（-1 表示无）
        "current-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        # 随机播放开关变化，param: bool
        "shuffle-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        # 循环模式变化，param: int（0=不循环 1=列表 2=单曲）
        "repeat-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._tracks: List[TrackItem] = []
        self._current_index: int = -1
        self._shuffle = False
        self._repeat_mode = REPEAT_OFF
        # 洗牌队列：本轮随机的播放顺序（存索引）
        self._shuffle_order: List[int] = []
        self._shuffle_pos: int = -1

    # ---- 播放模式 ----

    def set_shuffle(self, on: bool) -> None:
        on = bool(on)
        if on == self._shuffle:
            return
        self._shuffle = on
        self._rebuild_shuffle()
        self.emit("shuffle-changed", on)

    def is_shuffle(self) -> bool:
        return self._shuffle

    def _rebuild_shuffle(self) -> None:
        """重建洗牌顺序（不含当前曲，当前曲放最前）。"""
        n = len(self._tracks)
        self._shuffle_order = []
        self._shuffle_pos = -1
        if not self._shuffle or n == 0:
            return
        order = list(range(n))
        # 当前曲放在本轮开头，保证接着播下一首不重复
        cur = self._current_index
        if 0 <= cur < n:
            order.remove(cur)
            random.shuffle(order)
            order.insert(0, cur)
        else:
            random.shuffle(order)
        self._shuffle_order = order
        self._shuffle_pos = 0

    def set_repeat_mode(self, mode: int) -> None:
        mode = int(mode) % 3
        if mode == self._repeat_mode:
            return
        self._repeat_mode = mode
        self.emit("repeat-changed", mode)

    def repeat_mode(self) -> int:
        return self._repeat_mode

    # ---- 队列内容 ----

    def tracks(self) -> List[TrackItem]:
        return list(self._tracks)

    def __len__(self) -> int:
        return len(self._tracks)

    def is_empty(self) -> bool:
        return not self._tracks

    def current_index(self) -> int:
        return self._current_index

    def current_track(self) -> Optional[TrackItem]:
        if 0 <= self._current_index < len(self._tracks):
            return self._tracks[self._current_index]
        return None

    # ---- 修改队列 ----

    def set_tracks(self, tracks: List[TrackItem], autoplay_index: int = 0) -> None:
        """整体替换队列。"""
        self._tracks = list(tracks)
        self._current_index = -1
        self._shuffle_order = []
        self._shuffle_pos = -1
        self.emit("changed")
        if self._tracks and autoplay_index >= 0:
            self.set_current_index(min(autoplay_index, len(self._tracks) - 1))
        self._rebuild_shuffle()

    def append(self, track: TrackItem) -> None:
        self._tracks.append(track)
        self.emit("changed")

    def insert_next(self, track: TrackItem) -> None:
        """插入到当前曲目之后（下一首播放）。

        若队列为空或没有当前曲目，则直接追加。
        """
        if not self._tracks or self._current_index < 0:
            self._tracks.append(track)
        else:
            pos = self._current_index + 1
            self._tracks.insert(pos, track)
        self.emit("changed")

    def move_after_current(self, index: int) -> None:
        """把队列中第 index 项移到当前播放项之后（设为下一首播放）。

        - 若 index 就是当前项，或队列为空/无当前项，则不改动；
        - 移动后修正 _current_index，保持仍指向原来的当前曲目。
        """
        n = len(self._tracks)
        if index < 0 or index >= n:
            return
        cur = self._current_index
        if index == cur or cur < 0:
            return
        track = self._tracks.pop(index)
        # 修正当前索引：移除点若在当前项之前，当前索引前移一位
        if index < cur:
            cur -= 1
        # 插到当前项之后
        insert_at = cur + 1
        self._tracks.insert(insert_at, track)
        self._current_index = cur
        self._rebuild_shuffle()
        self.emit("changed")

    def remove_at(self, index: int) -> None:
        """从队列移除第 index 项（不影响曲库/文件），并修正当前索引。

        - 移除的是当前项：当前索引保持，指向原位置的后一项（若末尾则前移）；
        - 移除项在当前项之前：当前索引前移一位。
        """
        n = len(self._tracks)
        if index < 0 or index >= n:
            return
        self._tracks.pop(index)
        cur = self._current_index
        if n - 1 == 0:
            # 队列空了
            self._current_index = -1
            self._rebuild_shuffle()
            self.emit("changed")
            self.emit("current-changed", -1)
            return
        if index < cur:
            self._current_index = cur - 1
        elif index == cur:
            # 当前项被移除：保持索引（若越界则前移），不改动播放
            if cur >= len(self._tracks):
                self._current_index = len(self._tracks) - 1
        self._rebuild_shuffle()
        self.emit("changed")

    # ---- 当前曲目切换 ----

    def set_current_index(self, index: int) -> Optional[TrackItem]:
        if not self._tracks:
            self._current_index = -1
            self.emit("current-changed", -1)
            return None
        index = max(0, min(index, len(self._tracks) - 1))
        self._current_index = index
        self.emit("current-changed", index)
        return self._tracks[index]

    def next(self, auto: bool = False) -> Optional[TrackItem]:
        """下一首。auto=True 表示播放结束自动切（受循环/随机影响）。"""
        if not self._tracks:
            return None
        n = len(self._tracks)
        # 单曲循环：自动切时重播当前
        if auto and self._repeat_mode == REPEAT_ONE:
            return self.set_current_index(self._current_index)
        # 随机：按洗牌顺序播放，一轮不重复
        if self._shuffle:
            return self._next_shuffle(auto)
        nxt = self._current_index + 1
        if nxt >= n:
            # 到末尾
            if auto and self._repeat_mode == REPEAT_OFF:
                return None  # 不循环：停止
            nxt = 0  # 列表循环：绕回
        return self.set_current_index(nxt)

    def _next_shuffle(self, auto: bool) -> Optional[TrackItem]:
        """洗牌式下一首：本轮播完则重洗（循环）或停止（不循环）。"""
        if not self._shuffle_order:
            self._rebuild_shuffle()
        if not self._shuffle_order:
            return None
        self._shuffle_pos += 1
        if self._shuffle_pos >= len(self._shuffle_order):
            # 一轮播完
            if auto and self._repeat_mode == REPEAT_OFF:
                return None  # 不循环：停止
            self._rebuild_shuffle()
            self._shuffle_pos = 1 if len(self._shuffle_order) > 1 else 0
        idx = self._shuffle_order[self._shuffle_pos]
        # 同步洗牌位置到当前索引变化（若用户手动选曲，后面会重建）
        return self.set_current_index(idx)

    def previous(self) -> Optional[TrackItem]:
        if not self._tracks:
            return None
        n = len(self._tracks)
        if self._shuffle and self._shuffle_order:
            self._shuffle_pos -= 1
            if self._shuffle_pos < 0:
                self._shuffle_pos = len(self._shuffle_order) - 1
            idx = self._shuffle_order[self._shuffle_pos]
            return self.set_current_index(idx)
        prev = self._current_index - 1
        if prev < 0:
            prev = n - 1  # 绕回（手动切歌总是绕回）
        return self.set_current_index(prev)

    # ---- 交给播放内核的判断 ----

    @staticmethod
    def is_playable(track: Optional[TrackItem]) -> bool:
        """可播判断：本地文件存在，或在线曲目带流地址。"""
        return bool(track) and track.playable
