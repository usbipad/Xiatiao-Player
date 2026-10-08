"""回归：VizPipeline 反复 start/stop 不应累积读线程。

修复前实测：10 轮 start/stop → 10 个 viz-fifo 线程（读线程阻塞在 FIFO
open() 无法退出，每次 start 又新建）。修复后复用存活线程，恒为 1 个。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="xiatiao-vizth-"))


def _viz_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "viz-fifo")


def main() -> int:
    from core.viz import VizPipeline

    print(f"初始 viz-fifo 线程: {_viz_threads()}")
    p = VizPipeline()
    for i in range(10):
        p.start()
        time.sleep(0.02)
        p.stop()
    time.sleep(0.3)
    n = _viz_threads()
    print(f"10 轮 start/stop 后 viz-fifo 线程: {n}")
    if n > 2:
        print("RESULT: LEAK（读线程累积）")
        return 1
    print("RESULT: NO LEAK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
