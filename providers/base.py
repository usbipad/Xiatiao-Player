"""音源抽象基类。

UI 层只通过本接口访问数据，不直接接触任何底层实现（GStreamer / 网络等）。
"""
from __future__ import annotations

from typing import List

from gi.repository import GObject

from models import TrackItem


class BaseMusicProvider(GObject.Object):
    """所有音源 Provider 的公共接口。"""

    __gtype_name__ = "BaseMusicProvider"

    __gsignals__ = {
        # 曲库扫描/加载进度，param: float 0.0~1.0
        "library-progress": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        # 曲库发生变化（新增曲目等）
        "library-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # 发生错误，param: str 错误信息
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # 异步搜索完成，param: (str 查询词, object 结果列表)
        "search-finished": (GObject.SignalFlags.RUN_LAST, None, (str, object)),
    }

    #: Provider 唯一标识，如 "local" / "online"
    source_type: str = "base"
    #: 展示名称
    display_name: str = "Base"
    #: 能力声明（供 UI 判断：显示哪些图标、首页展示哪些板块）
    #: 取值示例：library / playlists / recommendations / mixes / favourite / share
    capabilities: set[str] = set()

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities

    def search(self, query: str) -> List[TrackItem]:
        """按关键字搜索（同步，适合本地内存数据）。

        在线音源应覆盖 search_async 走异步，不要阻塞 UI。
        """
        raise NotImplementedError

    def search_async(self, query: str) -> None:
        """异步搜索，结果通过 search-finished 信号返回。

        默认实现：同步搜索后发信号。在线音源应覆盖此方法，
        用 core.tasks.run_async 在后台请求，完成后 emit 信号。
        """
        try:
            results = self.search(query)
        except Exception as exc:
            self.emit("error", f"搜索失败: {exc}")
            results = []
        self.emit("search-finished", query, results)

    def get_library(self) -> List[TrackItem]:
        """返回全部曲库曲目。"""
        raise NotImplementedError

    def get_playlist_tracks(self) -> List[TrackItem]:
        """返回该音源的默认播放列表（本期可等同曲库）。"""
        return self.get_library()

    def refresh(self) -> None:
        """触发重新扫描 / 重新加载。"""
        raise NotImplementedError
