"""实测/回归：VizWindow 反复创建销毁是否回收。"""
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

os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="xiatiao-vizw-"))


def main() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
    try:
        Gtk.init()
    except Exception:
        pass

    from ui.viz_window import VizWindow

    refs = []
    for i in range(8):
        w = VizWindow()
        # 模拟关闭：调 renderer 清理 + 若窗口有 disconnect 则调
        dc = getattr(w, "disconnect_global_refs", None)
        if callable(dc):
            dc()
        else:
            try:
                w.renderer.disconnect_global_refs()
            except Exception:
                pass
        refs.append(weakref.ref(w))
        del w

    gc.collect()
    alive = sum(1 for r in refs if r() is not None)
    print(f"8 个 VizWindow 中仍存活: {alive}")
    if alive > 2:
        print("RESULT: LEAK")
        return 1
    print("RESULT: NO LEAK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
