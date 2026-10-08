"""回归：AdvancedDspWindow 反复创建关闭后应被回收。

覆盖：DspState 订阅、窗口自身控件树、以及「页→窗口」回边
（功能页持有 _on_dsp_changed 闭包，捕获窗口 self）。
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

os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="xiatiao-adv-"))


def main() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
    try:
        Gtk.init()
    except Exception:
        pass

    from ui.advanced_dsp_window import AdvancedDspWindow

    refs = []
    for i in range(8):
        w = AdvancedDspWindow()
        w.disconnect_global_refs()
        refs.append(weakref.ref(w))
        del w

    gc.collect()
    alive = sum(1 for r in refs if r() is not None)
    print(f"8 个 AdvancedDspWindow 中仍存活: {alive}")
    if alive > 2:
        print("RESULT: LEAK")
        return 1
    print("RESULT: NO LEAK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
