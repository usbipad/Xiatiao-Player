"""pages 包公共设施：封面缓存、封面活动通知、通用小工具、排序。

从原 ui/pages.py 抽出，保持逻辑不变，仅做物理拆分。
"""
from __future__ import annotations

from collections import OrderedDict as _OrderedDict

from gi.repository import GObject, Gtk


# ================================================================
# 列表封面缓存（LRU）
# ================================================================
#: filepath -> Gdk.Texture（None 表示无封面）。
#: 用 OrderedDict 实现 LRU 上限：长列表滚动时不会无限积累纹理。
_COVER_CACHE: "_OrderedDict" = _OrderedDict()
_COVER_CACHE_MAX = 512
COVER_SIZE = 40
#: 播放指示器尺寸（正方形）
INDICATOR_SIZE = 14
#: 正在读取中的 key → 等待该结果的 (list_item, pic, kind) 列表。
#: 同一首歌可能同时出现在多个列表（曲库/历史/歌单），需登记所有等待者。
_COVER_LOADING: dict = {}


#: 音质规格徽章的所有 CSS 类（统一在此维护）。
_BADGE_CLASSES = (
    "hires-badge", "cd-badge", "dsd-badge", "dxd-badge", "mc-badge",
    "dsd64-badge", "dsd128-badge", "dsd256-badge", "dsd512-badge", "dsd1024-badge",
)


def badge_css_for(label: str, multichannel: str = "") -> str:
    """按规格标签 + 多声道返回徽章 CSS 类名。

    多声道优先用青灰（mc-badge）；否则按规格：DSD 紫 / DXD 青绿 /
    CD 蓝 / 其余（HR 等）金。空标签返回空串。
    """
    if not label:
        return ""
    if multichannel:
        return "mc-badge"
    if label.startswith("DSD"):
        # DSD 细分：DSD64→dsd64-badge…；通用 DSD 用 dsd-badge。
        suffix = label[3:].strip()
        if suffix in ("64", "128", "256", "512", "1024"):
            return f"dsd{suffix}-badge"
        return "dsd-badge"
    if label.startswith("DXD"):
        return "dxd-badge"
    if label.startswith("CD"):
        return "cd-badge"
    return "hires-badge"


def apply_quality_badge(badge, track) -> None:
    """按 track 的规格 + 多声道设置徽章文字与 CSS 类（三处徽章共用）。

    - 文字：quality_badge（如 'HR'）+ 多声道（如 '5.1'）→ 'HR 5.1'。
    - 空标签（有损 / 未知）→ 隐藏徽章。
    """
    try:
        label = getattr(track, "quality_badge", "") or ""
        mc = getattr(track, "multichannel_label", "") or ""
        if mc:
            label = f"{label} {mc}".strip()
        badge.set_text(label)
        badge.set_visible(bool(label))
        for c in _BADGE_CLASSES:
            badge.remove_css_class(c)
        cls = badge_css_for(getattr(track, "quality_badge", "") or "", mc)
        if cls:
            badge.add_css_class(cls)
    except Exception:
        pass


def track_from_row(r: dict, stream_url: str = ""):
    """从 DB 行构造 TrackItem（歌单/历史/收藏共用）。

    - 本地文件：补读音频技术参数（DB 表未存），供音质徽章显示。
    - 在线歌：stream_url 由调用方传入（token 有时效，动态重建）。
    失败返回 None。
    """
    try:
        from models import TrackItem, SOURCE_LOCAL
        source_type = r.get("source_type") or SOURCE_LOCAL
        filepath = r.get("filepath") or ""
        # 先取 DB 行里的技术参数（在线歌的关键：stream_url 无扩展名，
        # 只能靠这些实测值 + format_hint 判 DSD/DXD/HR 徽章）。
        rate = int(r.get("sample_rate") or 0)
        depth = int(r.get("bit_depth") or 0)
        channels = int(r.get("channels") or 0)
        bitrate = int(r.get("bitrate") or 0)
        # 本地文件：再用 mutagen 实时读取覆盖（DB 值可能缺失/过时）。
        if source_type == SOURCE_LOCAL and filepath:
            try:
                from providers.local import LocalProvider
                tech = LocalProvider._read_tech_mutagen(filepath)
                if tech:
                    rate = tech.get("sample_rate", 0) or rate
                    depth = tech.get("bit_depth", 0) or depth
                    channels = tech.get("channels", 0) or channels
                    bitrate = tech.get("bitrate", 0) or bitrate
            except Exception:
                pass
        return TrackItem(
            title=r.get("title") or "未知歌曲",
            artist=r.get("artist") or "未知歌手",
            album=r.get("album") or "",
            duration=r.get("duration") or "0:00",
            duration_seconds=float(r.get("duration_seconds") or 0.0),
            filepath=filepath,
            source_type=source_type,
            source_id=r.get("source_id") or "",
            stream_url=stream_url or r.get("stream_url") or "",
            cover_url=r.get("cover_url") or "",
            sample_rate=rate,
            bit_depth=depth,
            channels=channels,
            bitrate=bitrate,
            format_hint=r.get("format_hint") or "",
        )
    except Exception:
        return None


class _Miss:
    """缓存未命中的哨兵（区别于缓存值 None=无封面）。"""


MISS = _Miss()


def cache_get(key):
    """取缓存并把命中的键移到末尾（LRU）。未命中返回 MISS。"""
    if key in _COVER_CACHE:
        try:
            _COVER_CACHE.move_to_end(key)
        except Exception:
            pass
        return _COVER_CACHE[key]
    return MISS


def cache_put(key, value) -> None:
    """写缓存并淘汰最久未用的项。"""
    _COVER_CACHE[key] = value
    try:
        _COVER_CACHE.move_to_end(key)
    except Exception:
        pass
    while len(_COVER_CACHE) > _COVER_CACHE_MAX:
        try:
            _COVER_CACHE.popitem(last=False)
        except Exception:
            break


def cover_loading_map() -> dict:
    """返回「正在加载封面」的登记表（供单元格工厂读写）。"""
    return _COVER_LOADING


def load_cover_from_url(url: str, size: int = COVER_SIZE, radius: int = 6):
    """后台线程：从 URL 下载封面并缩放为正方形 PNG bytes（不碰 UI）。

    用于在线曲目（filepath 为空、cover_url 有值）。
    下载失败返回 None。
    """
    if not url:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        from models import make_square_cover_bytes
        return make_square_cover_bytes(raw, size, radius=radius)
    except Exception:
        return None


def load_cover_bytes(filepath: str, size: int = COVER_SIZE, radius: int = 6):
    """后台线程：读取内嵌封面并缩放为正方形 PNG bytes（不碰 UI）。

    无封面 / 解析失败返回 None。供歌曲列表、队列等所有列表共用，
    保证缩放参数与缓存一致。
    """
    try:
        from models import extract_cover, make_square_cover_bytes
    except Exception:
        return None
    raw = extract_cover(filepath)
    if not raw:
        return None
    try:
        return make_square_cover_bytes(raw, size, radius=radius)
    except Exception:
        return None


# ================================================================
# 封面加载活动通知器（模块级单例）
# ================================================================
class _CoverActivity(GObject.Object):
    """封面加载活动通知器。

    启动遮罩（splash）需要等首屏封面都加载完再淡出。这里在「是否有封面
    正在加载」状态翻转时发 busy-changed 信号，window 据此判断是否就绪。
    """

    __gtype_name__ = "CoverActivity"

    __gsignals__ = {
        # 是否忙（有封面在加载）变化，param: bool
        "busy-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._busy = False
        #: 手动引用计数：调用方 mark_busy/mark_idle 配对（如在线卡片封面）。
        self._manual = 0

    @property
    def busy(self) -> bool:
        return self._busy

    def _refresh(self) -> None:
        busy = bool(_COVER_LOADING) or self._manual > 0
        if busy != self._busy:
            self._busy = busy
            try:
                self.emit("busy-changed", busy)
            except Exception:
                pass

    def mark_busy(self) -> None:
        self._manual += 1
        self._refresh()

    def mark_idle(self) -> None:
        self._manual = max(0, self._manual - 1)
        self._refresh()


#: 全局封面活动单例
cover_activity = _CoverActivity()


# ================================================================
# 视图模式常量
# ================================================================
VIEW_SONGS = "songs"
VIEW_ALBUMS = "albums"
VIEW_ARTISTS = "artists"


# ================================================================
# 通用小工具
# ================================================================
def section_title(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("heading")
    label.set_halign(Gtk.Align.START)
    return label


# ================================================================
# 排序
# ================================================================
def _text_sort_key(s: str) -> str:
    """文本排序键：小写；中文按 Unicode（与英文分别聚拢）。"""
    return (s or "").strip().lower()


def make_sort_func(key: str):
    """返回 Gtk.CustomSorter 的比较函数（签名 (a, b, user_data) -> int）。

    - duration_seconds / sample_rate 等：按数值
    - format_ext：按格式名（同格式聚拢，再按字母）
    - title / artist：按文本（小写；中文按 Unicode）
    """
    def _cmp(a, b, _user_data=None):
        if a is None and b is None:
            return 0
        if a is None:
            return -1
        if b is None:
            return 1
        # 数值列
        if key in ("duration_seconds", "sample_rate", "bitrate", "bit_depth", "channels"):
            va = getattr(a, key, 0) or 0
            vb = getattr(b, key, 0) or 0
            try:
                fa, fb = float(va), float(vb)
            except (TypeError, ValueError):
                fa, fb = 0.0, 0.0
            return -1 if fa < fb else (1 if fa > fb else 0)
        # 文本列
        if key == "format_ext":
            va = (getattr(a, "format_ext", "") or "")
            vb = (getattr(b, "format_ext", "") or "")
        else:
            va = str(getattr(a, key, "") or "")
            vb = str(getattr(b, key, "") or "")
        sa, sb = _text_sort_key(va), _text_sort_key(vb)
        return -1 if sa < sb else (1 if sa > sb else 0)
    return _cmp
