"""验证：调用 disconnect_global_refs 后 EffectPage 能被回收。

运行：python3 tests/test_dsp_leak_fixed.py
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="xiatiao-dspfix-"))


def _count(cls) -> int:
    gc.collect()
    n = 0
    for o in gc.get_objects():
        try:
            if isinstance(o, cls):
                n += 1
        except Exception:
            pass
    return n


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

    base = _count(EffectPage)
    print(f"基线 EffectPage: {base}")

    ROUNDS = 20
    for i in range(ROUNDS):
        page = EffectPage(mode="single", only="master")
        page.disconnect_global_refs()   # 修复：显式断开
        del page

    n = _count(EffectPage)
    print(f"{ROUNDS} 轮（断开后）存活 EffectPage: {n}")
    growth = n - base
    if growth > 2:
        print(f"RESULT: STILL LEAK（增长 {growth}）")
        return 1
    print("RESULT: FIXED（断开后正常回收）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
