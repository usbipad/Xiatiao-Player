"""主页「我的歌单」区块（在线音源）。

数据来源：SubsonicProvider.get_playlists_info() → PlaylistInfo 列表。
平台无关：后端接 QQ/汽水等，只要返回 PlaylistInfo 结构，本区块都能渲染。

无数据（未配置 / 连不上 / 空）时，整个区块隐藏。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from core.i18n import _

from .online_section import CardSection


class OnlinePlaylistSection(CardSection):
    """主页「我的歌单」区块。

    直接复用 CardSection（专辑/艺术家同款）：标题 + 横向一排卡片 + 横滑，
    布局 / 滚动 / 外观与其它在线区块完全一致。
    set_playlists(playlists)：有数据 → 显示；空 → 隐藏整个区块。
    """

    def __init__(self, on_playlist_click: Optional[Callable] = None) -> None:
        super().__init__(_("我的歌单"))
        self._on_click = on_playlist_click

    def set_playlists(self, playlists: List) -> None:
        """歌单（PlaylistInfo）→ 卡片 dict，喂给 CardSection。"""
        cards = []
        for pl in (playlists or []):
            try:
                cnt = int(getattr(pl, "song_count", 0) or 0)
                cards.append({
                    "name": getattr(pl, "name", "") or "",
                    "subtitle": ("%d %s" % (cnt, _("首"))) if cnt else "",
                    "cover_url": getattr(pl, "cover_url", "") or "",
                    "click": (lambda _pl=pl: self._on_click(_pl))
                             if callable(self._on_click) else None,
                })
            except Exception:
                continue
        self.set_cards(cards)
