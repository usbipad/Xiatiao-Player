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

    @property
    def busy(self) -> bool:
        return self._busy

    def _refresh(self) -> None:
        busy = bool(_COVER_LOADING)
        if busy != self._busy:
            self._busy = busy
            try:
                self.emit("busy-changed", busy)
            except Exception:
                pass

    def mark_busy(self) -> None:
        self._refresh()

    def mark_idle(self) -> None:
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


def add_hwheel_scroll(scroll: Gtk.ScrolledWindow) -> None:
    """给横向 ScrolledWindow 挂滚轮：普通滚轮（竖向）也驱动横向滚动。"""
    try:
        def _on_scroll(_ctrl, _dx, dy):
            try:
                adj = scroll.get_hadjustment()
                if adj is None:
                    return False
                step = adj.get_step_increment() or 40.0
                adj.set_value(adj.get_value() + (dy * -step if dy else 0))
                return True
            except Exception:
                return False

        ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        ctrl.connect("scroll", _on_scroll)
        scroll.add_controller(ctrl)
    except Exception:
        pass


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
