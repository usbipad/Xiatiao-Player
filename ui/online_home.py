"""主页「我的歌单」区块（在线音源）。

数据来源：SubsonicProvider.get_playlists_info() → PlaylistInfo 列表。
平台无关：后端接 QQ/汽水等，只要返回 PlaylistInfo 结构，本区块都能渲染。

无数据（未配置 / 连不上 / 空）时，整个区块隐藏。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from core.i18n import _
from core.tasks import run_cover_async

from .online_section import CardSection

#: 卡片封面显示尺寸
CARD_COVER_PX = 160
#: 封面到容器边缘的距离（给 hover 浮起/阴影留空间）
_GAP = 12
#: 卡片容器宽度（封面 + 阴影留白）
_CARD_W = CARD_COVER_PX + _GAP * 2
#: 折叠时显示的行数
_COLLAPSED_ROWS = 1
#: 折叠态高度（一行卡片）
_COLLAPSED_H = 200
#: 展开态高度上限（-1 = 放开，由外层主页滚动接管）
_EXPANDED_H = -1


def _load_url_cover_async(url: str, on_done) -> None:
    """后台下载封面图片，回主线程回调 (pixbuf_or_none)。"""
    import urllib.request

    def _work():
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except Exception:
            return None

    # 缓存键：与 song_list / player_panel 一致（"url::" 前缀，同一 LRU）。
    ckey = "url::" + url
    try:
        from .pages.common import cache_get, cache_put, MISS
    except Exception:
        cache_get = cache_put = None
        MISS = object()
    if cache_get is not None:
        cached = cache_get(ckey)
        if cached is not MISS:
            on_done(cached)
            return

    def _done(raw):
        if not raw:
            try:
                cache_put(ckey, None)
            except Exception:
                pass
            on_done(None)
            return
        try:
            from gi.repository import GdkPixbuf
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(raw)
            loader.close()
            pb = loader.get_pixbuf()
            if pb is None:
                on_done(None)
                return
            # 居中裁成正方形
            w, h = pb.get_width(), pb.get_height()
            side = min(w, h)
            x = (w - side) // 2
            y = (h - side) // 2
            if w != h:
                pb = pb.new_subpixbuf(x, y, side, side)
            # 缩放到封面尺寸
            pb = pb.scale_simple(
                CARD_COVER_PX, CARD_COVER_PX, GdkPixbuf.InterpType.BILINEAR)
            from .online_section import _pixbuf_to_texture
            tex = _pixbuf_to_texture(pb)
            try:
                cache_put(ckey, tex)
            except Exception:
                pass
            on_done(tex)
        except Exception:
            on_done(None)

    run_cover_async(work=_work, on_done=_done)


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
