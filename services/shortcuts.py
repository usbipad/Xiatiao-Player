"""快捷键服务：键位解析、匹配、配置读取。

从 ui/window.py 抽出。解析与匹配为**纯逻辑**（可单测），
不依赖 GTK 控件；仅 mods_from_state 需要 Gdk.ModifierType 常量。

动作名与语义：
    play_pause  播放 / 暂停
    prev        上一首
    next        下一首
    seek_back   快退 5 秒
    seek_fwd    快进 5 秒
    vol_up      音量 +
    vol_down    音量 -
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: 支持的动作名（顺序即匹配顺序）
SHORTCUT_ACTIONS = (
    "play_pause", "prev", "next", "seek_back", "seek_fwd", "vol_up", "vol_down",
)

#: 动作默认绑定（全空 = 未绑定，由用户在设置里录制）
_DEFAULT_SHORTCUTS = {name: "" for name in SHORTCUT_ACTIONS}


def parse_shortcut(spec: str):
    """把 'Ctrl+Alt+Left' / 'space' 解析为 (mods: frozenset, keyval: int)。

    mods 为修饰键集合（'ctrl'/'alt'/'shift'/'super'）；解析失败返回 None。
    """
    try:
        from gi.repository import Gdk
        s = (spec or "").strip()
        if not s:
            return None
        mods = set()
        parts = s.split("+")
        keyname = parts[-1]
        for p in parts[:-1]:
            pl = p.strip().lower()
            if pl in ("ctrl", "control"):
                mods.add("ctrl")
            elif pl == "alt":
                mods.add("alt")
            elif pl == "shift":
                mods.add("shift")
            elif pl in ("super", "meta"):
                mods.add("super")
        keyval = Gdk.keyval_from_name(keyname)
        if not keyval:
            keyval = Gdk.keyval_from_name(keyname.lower())
        # Gdk 对未知键名返回 GDK_KEY_VoidSymbol(0xFFFFFF) 而非 0，需显式过滤
        # （否则非法键位会被当成一个真实键，可能误匹配）。
        if not keyval or int(keyval) == 0xFFFFFF:
            return None
        return (frozenset(mods), int(keyval))
    except Exception:
        return None


def get_shortcuts() -> dict:
    """读取快捷键配置（键位字符串），带默认兜底。"""
    defaults = dict(_DEFAULT_SHORTCUTS)
    try:
        from config.settings import get_config
        saved = get_config().get("shortcuts")
        if isinstance(saved, dict):
            defaults.update({k: v for k, v in saved.items() if isinstance(v, str)})
    except Exception:
        pass
    return defaults


def mods_from_state(state) -> set:
    """从 Gdk 修饰键状态位提取修饰键集合。"""
    mods = set()
    try:
        from gi.repository import Gdk
        if state & Gdk.ModifierType.CONTROL_MASK:
            mods.add("ctrl")
        if state & Gdk.ModifierType.ALT_MASK:
            mods.add("alt")
        if state & Gdk.ModifierType.SHIFT_MASK:
            mods.add("shift")
        if state & Gdk.ModifierType.SUPER_MASK:
            mods.add("super")
    except Exception:
        pass
    return mods


def match_shortcut(shortcuts: dict, keyval: int, mods: set):
    """在配置中查找匹配 (keyval, mods) 的动作名；无匹配返回 None。"""
    for action in SHORTCUT_ACTIONS:
        parsed = parse_shortcut(shortcuts.get(action, ""))
        if not parsed:
            continue
        exp_mods, exp_keyval = parsed
        if int(keyval) == exp_keyval and set(mods) == set(exp_mods):
            return action
    return None
