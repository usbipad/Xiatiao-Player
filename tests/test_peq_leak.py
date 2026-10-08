"""验证：调用 PeqCurve.disconnect_global_refs 后能否回收。"""
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

os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="xiatiao-peq3-"))


def main() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    try:
        Gtk.init()
    except Exception:
        pass

    from ui.widgets.peq_curve import PeqCurve

    refs = []
    for i in range(20):
        c = PeqCurve()
        c.disconnect_global_refs()
        refs.append(weakref.ref(c))
        del c

    gc.collect()
    alive = sum(1 for r in refs if r() is not None)
    print(f"20 个 PeqCurve 中仍存活: {alive}")
    if alive > 3:
        print("RESULT: LEAK")
        return 1
    print("RESULT: NO LEAK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
