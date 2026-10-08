"""回归：EffectPage(basic) 反复创建销毁后应被回收。

basic 模式是设置页用的（含 stereo 的 lambda、master 的 reset lambda、
advanced_entry 的 activated lambda）。修复后应 0 残留。
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import weakref
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="xiatiao-basic-"))


def main() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
    try:
        Gtk.init()
    except Exception:
        pass

    from ui.effect_page import EffectPage

    refs = []
    for i in range(10):
        p = EffectPage(mode="basic")
        p.disconnect_global_refs()
        refs.append(weakref.ref(p))
        del p

    gc.collect()
    alive = sum(1 for r in refs if r() is not None)
    print(f"10 个 EffectPage(basic) 中仍存活: {alive}")
    if alive > 2:
        print("RESULT: LEAK")
        return 1
    print("RESULT: NO LEAK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
