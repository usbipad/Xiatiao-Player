"""进程内内存诊断探针（Xiatiao Player 专用）。

用途
----
在**真实运行的应用**里定位「内存持续增长」。原理：周期性对比
`gc` 对象计数，找出持续增长的类，并沿引用链向上追踪「具名持有者」
（模块 / 我们的类 / 属性），从而把「跨 C/Python 引用环」点名到具体对象。

为什么需要它
------------
GTK4/PyGObject 的泄漏多为「C 层持有 Python 回调 → 回调引用 widget →
widget 属于 C 层控件树」，Python 的 gc 看不出是谁在持有。静态读代码
收益有限；在真实进程里抓增长对象 + 引用链才是可靠路径。

用法
----
**最简单：用一键启动器（推荐）**。它会注入探针并启动应用，
无需改动 main.py：

    python3 tools/memprobe_run.py
    XIATIAO_MEMPROBE_INTERVAL=10 python3 tools/memprobe_run.py
    cat /tmp/xiatiao-memprobe.log

**手动方式（备用）**：本工具是独立脚本，不常驻在产品入口
（main.py 不引用它）。需要时临时挂到应用里：

1) 在 `main.py` 的 `on_activate` 里、创建窗口后临时加两行：:

       from tools.memprobe import start as _mp
       _mp()

   然后正常运行应用（间隔 / 日志可用环境变量控制）：:

       XIATIAO_MEMPROBE_INTERVAL=10 python3 main.py

   诊断完删掉这两行（不要提交）。

2) 复现疑似泄漏的操作（切页 / 切歌 / 滚动 / 开关窗口）。

3) 查看日志（同时输出到 stderr 与该文件）：:

       cat /tmp/xiatiao-memprobe.log

4) 也可独立运行做快速自测（只统计本进程，参考用）：:

       python3 tools/memprobe.py

如何判读
--------
- 「无增长」连续出现 → 有界工作集 / 缓存，正常。
- 某类**单调上升、从不回落** → 真泄漏；看其下 `<- ...` 具名持有者定位。
- 关注内置的全局表：`_COVER_LOADING` / `_COVER_CACHE` /
  `PlayingIndicator._INSTANCES`，它们只增不减即为泄漏。

信号触发
--------
- ``kill -USR1 <pid>``：抓一次「深度快照」= gc 对象内容 + 持有者链
  + 关键容器大小 + tracemalloc 分配点（最有用）。
- ``kill -USR2 <pid>``：只抓 tracemalloc 差异（轻量）。

环境变量
--------
- ``XIATIAO_MEMPROBE_INTERVAL`` 采样间隔秒数，默认 8。
- ``XIATIAO_MEMPROBE_LOG``      日志路径，默认 /tmp/xiatiao-memprobe.log。
- ``XIATIAO_MEMPROBE_TRACEMALLOC`` 设为 1 启用 tracemalloc（**默认关**）。
  **关键**：gc 不追踪 str/bytes/int；若泄漏是字符串/字节（歌词/响应/
  原始图），只看 gc 会漏。tracemalloc 追踪全部分配并给「文件:行号」。
  ⚠️ **开启会明显卡顿**（每次分配都记调用栈，GTK 下会卡成无响应）——
  仅短时定位用；定位完就不带该变量重启即可。

注意
----
- 采样在主线程执行 ``gc.collect()``，瞬间可能有轻微卡顿；间隔可调大。
- 本工具仅用于诊断，不参与发布逻辑；关闭时对应用零影响。
"""
from __future__ import annotations

import gc
import os
import sys
from collections import Counter

_INTERVAL = float(os.environ.get("XIATIAO_MEMPROBE_INTERVAL", "8"))
_LOG = os.environ.get("XIATIAO_MEMPROBE_LOG", "/tmp/xiatiao-memprobe.log")
_STATE: dict = {"prev": None, "n": 0}

#: gc 内部容器/描述符/迭代器：爬链时穿过它们，不算「具名持有者」。
#: 特别包含 list_iterator —— gc.get_objects()/get_referrers() 自身迭代
#: 产生的临时迭代器会引用一切，若不排除会刷屏（本项目实测踩过）。
_GC_INTERNAL = {
    "list", "dict", "tuple", "set", "frozenset", "cell", "frame",
    "method", "function", "wrapper_descriptor", "method_descriptor",
    "getset_descriptor", "member_descriptor", "builtin_function_or_method",
    "type", "classmethod", "staticmethod", "property",
    "list_iterator", "list_reverseiterator", "tuple_iterator",
    "dict_keyiterator", "dict_valueiterator", "dict_itemiterator",
    "set_iterator", "enumerate", "zip", "reversed", "generator",
    "list_iterator", "range_iterator", "callable_iterator",
}


def _snapshot() -> Counter:
    """GC 一次后统计各类对象数量。"""
    gc.collect()
    c: Counter = Counter()
    for o in gc.get_objects():
        try:
            c[type(o).__name__] += 1
        except Exception:
            pass
    return c


def _walk_holders(obj, depth=0, maxdepth=7, seen=None, out=None):
    """沿 referrer 链向上，直到找到「具名持有者」（模块 / 我们的类 / 属性）。"""
    if seen is None:
        seen = set()
    if out is None:
        out = []
    if depth > maxdepth or len(out) >= 30:
        return out
    if id(obj) in seen:
        return out
    seen.add(id(obj))
    try:
        refs = gc.get_referrers(obj)
    except Exception:
        return out
    for r in refs:
        tn = type(r).__name__
        if tn == "frame":
            continue
        if tn in _GC_INTERNAL:
            _walk_holders(r, depth, maxdepth, seen, out)
            continue
        hint = ""
        try:
            if tn == "module":
                hint = f" module={getattr(r, '__name__', '?')}"
            else:
                hint = f" class={type(r).__name__}"
                d = getattr(r, "__dict__", None)
                if isinstance(d, dict):
                    names = [k for k, v in d.items() if v is obj]
                    if names:
                        hint += f" attr={names[0]}"
        except Exception:
            pass
        out.append(f"{'  ' * depth}<- {tn}{hint}")
    return out


def _trace(name: str, sample: int = 2, max_lines: int = 10):
    """对指定类型抽样，打印其「具名持有者」链。"""
    lines = []
    objs = []
    for o in gc.get_objects():
        try:
            if type(o).__name__ == name:
                objs.append(o)
        except Exception:
            pass
    for o in objs[:sample]:
        lines.append(f"    [{name}] id={id(o)}")
        try:
            chain = _walk_holders(o)
        except Exception:
            chain = []
        lines.extend(chain[:max_lines])
    return lines


#: 深度快照时关注的重点类型（按堆积证据排序）。
_FOCUS_TYPES = (
    "TrackItem", "ColumnViewCell", "GestureClick", "PlayingIndicator",
    "MemoryTexture", "GdkTexture", "Picture", "Label", "Box", "Overlay",
    "NowPlayingIcon",
)


def _brief(o, limit: int = 90) -> str:
    """对象的简短可读描述（尽量取出歌名等关键字段）。"""
    try:
        t = type(o).__name__
        for attr in ("title", "name", "_group_name"):
            v = getattr(o, attr, None)
            if isinstance(v, str) and v:
                return f"{t}(title={v!r})"
        r = repr(o)
        return r[:limit]
    except Exception:
        return type(o).__name__


def _deep_dump(reason: str = "manual") -> None:
    """深度快照：对重点类型各抽样，打印内容 + 完整持有者路径。

    由 SIGUSR1 或周期触发。用于「内存已涨」时抓现场，看清是哪些对象/
    哪些歌在堆积、被谁钉住。
    """
    gc.collect()
    lines = [f"\n########## DEEP DUMP ({reason}) ##########"]
    snap = _snapshot()
    for name in _FOCUS_TYPES:
        cnt = snap.get(name, 0)
        if cnt <= 0:
            continue
        lines.append(f"\n--- {name} × {cnt} ---")
        objs = []
        for o in gc.get_objects():
            try:
                if type(o).__name__ == name:
                    objs.append(o)
            except Exception:
                pass
        # 抽样：头部 + 尾部（新分配对象更可能是泄漏源）
        sample = objs[:3] + (objs[-2:] if len(objs) > 5 else [])
        for o in sample:
            lines.append(f"  * {_brief(o)}")
            try:
                chain = _walk_holders(o, maxdepth=8)
            except Exception:
                chain = []
            if not chain:
                lines.append("    <- (无 Python 侧具名持有者；疑 C 层强引用)")
            seen_line = None
            shown = 0
            for c in chain:
                if c == seen_line:
                    continue
                seen_line = c
                lines.append("    " + c)
                shown += 1
                if shown >= 8:
                    break
    lines.append(_proc_stats_line())
    lines.extend(_dump_known_globals())
    lines.extend(_dump_app_containers())
    lines.extend(_tm_diff(reason))
    text = "\n".join(lines)
    sys.stderr.write(text + "\n")
    try:
        with open(_LOG, "a", encoding="utf-8") as fp:
            fp.write(text + "\n")
    except Exception:
        pass


#: 信号处理器只置标志；真正的 dump 由后台采样线程执行。
#: 这样信号处理永远瞬时返回（不碰 gc / 文件 IO），且不依赖主循环。
_REQ = {"deep": 0, "tm": 0}


def _on_sigusr1(_signum, _frame) -> None:
    """SIGUSR1：请求一次完整深度快照（仅置标志，由后台线程执行）。"""
    _REQ["deep"] += 1


def _on_sigusr2(_signum, _frame) -> None:
    """SIGUSR2：请求一次 tracemalloc 差异（仅置标志）。"""
    _REQ["tm"] += 1


def _emit_tm_only() -> None:
    """仅输出 tracemalloc 差异（后台线程直接执行，不碰主循环 / GLib）。"""
    lines = _tm_diff("SIGUSR2")
    text = "\n".join(lines)
    sys.stderr.write(text + "\n")
    try:
        with open(_LOG, "a", encoding="utf-8") as fp:
            fp.write(text + "\n")
    except Exception:
        pass


def _proc_stats() -> dict:
    """从 /proc/self 读进程级指标：RSS、heap、anon、线程数。

    关键作用：gc 只能看 Python 对象；若「对象数没涨但 RSS 涨」，
    说明泄漏在 C 层（纹理 / cairo / glibc 堆 / 线程栈）——靠这几个
    指标区分，避免误判「无增长=正常」。
    """
    out = {"rss_mb": None, "heap_mb": None, "anon_mb": None, "threads": None}
    try:
        with open("/proc/self/status", encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = int(line.split()[1]) / 1024.0
                elif line.startswith("Threads:"):
                    out["threads"] = int(line.split()[1])
    except Exception:
        pass
    try:
        with open("/proc/self/smaps", encoding="utf-8") as fp:
            name = ""
            for line in fp:
                if line and line[0].isdigit():
                    parts = line.split()
                    name = parts[5] if len(parts) >= 6 else "[anon]"
                elif line.startswith("Rss:") and name in ("[heap]", "[anon]"):
                    kb = int(line.split()[1]) / 1024.0
                    key = "heap_mb" if name == "[heap]" else "anon_mb"
                    out[key] = (out.get(key) or 0) + kb
    except Exception:
        pass
    return out


def _proc_stats_line() -> str:
    s = _proc_stats()
    def _fmt(v):
        return f"{v:.0f}" if isinstance(v, (int, float)) else "?"
    return (f"    [proc] RSS={_fmt(s['rss_mb'])}MB heap={_fmt(s['heap_mb'])}MB "
            f"anon={_fmt(s['anon_mb'])}MB threads={s['threads']}")


def _dump_known_globals():
    """报告已知高危全局表的大小（只增不减即为泄漏）。

    ⚠️ 只用 `sys.modules.get()` 查找「已加载」的模块，**绝不主动 import**。
    实测：在后台线程里 `from ui.pages import ...` 会卡死（GTK 相关模块的
    导入不能在非主线程做），导致采样线程整个卡住、之后再不采样。
    """
    lines = []
    _c = sys.modules.get("ui.pages.common")
    if _c is not None:
        try:
            lines.append(
                f"    [globals] _COVER_LOADING={len(getattr(_c, '_COVER_LOADING', {}))}"
                f" _COVER_CACHE={len(getattr(_c, '_COVER_CACHE', {}))}"
            )
        except Exception:
            pass
    _pi = sys.modules.get("ui.widgets.playing_indicator")
    if _pi is not None:
        try:
            lines.append(
                f"    [globals] PlayingIndicator._INSTANCES={len(_pi._INSTANCES)}")
        except Exception:
            pass
    return lines


# ---- tracemalloc：追踪「全部」Python 分配（含 str/bytes）----
# ⚠️ 默认**关闭**：tracemalloc 开启后，每一次内存分配都要记录调用栈，
# 而 GTK 应用每秒成千上万次分配 → 主线程被拖死（实测：界面卡成
# 「无响应」，连 8s 的采样定时器都没机会跑）。故仅在需要定位
# 「分配点文件:行号」时，用 XIATIAO_MEMPROBE_TRACEMALLOC=1 显式开启，
# 且只宜短时使用（配合 kill -USR1/2 抓一次即可）。
_TRACEMALLOC_ON = os.environ.get(
    "XIATIAO_MEMPROBE_TRACEMALLOC", "0").strip().lower() not in (
    "", "0", "false", "no", "off")
_tm_state: dict = {"enabled": False, "baseline": None}


def _tm_start() -> None:
    """启动 tracemalloc 并记录基线（若启用）。"""
    if not _TRACEMALLOC_ON:
        return
    try:
        import tracemalloc
        tracemalloc.start(20)   # 保留 20 层调用栈
        _tm_state["enabled"] = True
        _tm_state["baseline"] = tracemalloc.take_snapshot()
    except Exception:
        _tm_state["enabled"] = False


def _tm_diff(reason: str = "", top: int = 25) -> list:
    """tracemalloc 差异：相对基线，列出增长最多的分配点（文件:行号）。"""
    if not _tm_state.get("enabled"):
        return ["    [tracemalloc] 未启用"]
    try:
        import tracemalloc
        snap = tracemalloc.take_snapshot()
        base = _tm_state.get("baseline")
        lines = [f"\n----- TRACEMALLOC 增长（vs 基线，{reason}）-----"]
        if base is None:
            _tm_state["baseline"] = snap
            return lines + ["    （已记录基线，下次对比）"]
        stats = snap.compare_to(base, "lineno")
        rows = [s for s in stats if s.size_diff > 0]
        rows.sort(key=lambda s: s.size_diff, reverse=True)
        if not rows:
            lines.append("    （无增长）")
        for s in rows[:top]:
            tb = str(s.traceback).strip().replace("\n", " | ")
            lines.append(
                f"    +{s.size_diff / 1024:9.1f} KB  ({s.count_diff:+d} 块)  {tb}")
        return lines
    except Exception as exc:
        return [f"    [tracemalloc] diff 失败: {exc}"]


def _dump_app_containers() -> list:
    """快照应用关键长生命周期容器的大小（只增不减即泄漏）。"""
    lines = ["    [containers]"]
    win = None
    for o in gc.get_objects():
        try:
            if type(o).__name__ == "MainWindow":
                win = o
                break
        except Exception:
            pass
    if win is None:
        lines.append("      （未找到 MainWindow）")
        return lines
    try:
        pl = getattr(win, "playlist", None)
        if pl is not None and hasattr(pl, "tracks"):
            lines.append(f"      playlist.tracks = {len(pl.tracks())}")
    except Exception:
        pass
    try:
        pp = getattr(win, "player_panel", None)
        st = getattr(pp, "_queue_store", None)
        if st is not None:
            lines.append(f"      queue_store = {st.get_n_items()}")
    except Exception:
        pass
    return lines


def _tick() -> bool:
    """一次采样：对比上一次，打印增长 top + 持有者 + 全局表。"""
    try:
        cur = _snapshot()
        prev = _STATE["prev"]
        _STATE["prev"] = cur
        _STATE["n"] += 1
        if prev is None:
            lines = [f"=== memprobe 基线 #{_STATE['n']}（对象总数 {sum(cur.values())}）==="]
            lines.append(_proc_stats_line())
            lines += [f"  {v:<8} {k}" for k, v in cur.most_common(10)]
        else:
            rows = []
            for k, v in cur.items():
                d = v - prev.get(k, 0)
                if d > 0:
                    rows.append((k, d, v))
            rows.sort(key=lambda r: r[1], reverse=True)
            lines = [f"=== memprobe #{_STATE['n']} 增长 top（间隔 {_INTERVAL:.0f}s）==="]
            if not rows:
                lines.append("  （无增长）")
            for k, d, v in rows[:15]:
                lines.append(f"  +{d:<8} = {v:<8} {k}")
            lines.append(_proc_stats_line())
            lines.extend(_dump_known_globals())
            for k, d, _v in rows[:3]:
                if d >= 5:
                    lines.extend(_trace(k))
        text = "\n".join(lines)
        sys.stderr.write(text + "\n")
        try:
            with open(_LOG, "a", encoding="utf-8") as fp:
                fp.write(text + "\n")
        except Exception:
            pass
    except Exception:
        pass
    return True


def _loop() -> None:
    """后台采样循环（daemon 线程）。

    ⚠️ 关键设计：**不用 GLib.timeout_add**。实测在真实应用里，主线程会被
    GTK 布局/样式重算长时间占满，主循环的 timeout source 得不到调度，
    导致采样一次都不跑（且表现为界面「无响应」）。改用独立后台线程后：
      - 不依赖主循环，主线程再卡也能采样；
      - 信号（SIGUSR1/2）只置标志，由本线程在下一轮执行 dump。
    gc.collect() 与 gc.get_objects() 在**非主线程**执行是安全的（GIL 保护）。
    """
    import threading
    import time
    while True:
        try:
            if _REQ.get("deep"):
                _REQ["deep"] = 0
                _deep_dump("SIGUSR1")
            elif _REQ.get("tm"):
                _REQ["tm"] = 0
                _emit_tm_only()
            else:
                _tick()
        except Exception:
            pass
        time.sleep(max(1.0, _INTERVAL))


def start() -> None:
    """启动探针（后台线程周期采样）。由启动器 / main.py 条件调用。"""
    try:
        with open(_LOG, "w", encoding="utf-8") as fp:
            fp.write(f"# xiatiao memprobe interval={_INTERVAL}s\n")
    except Exception:
        pass
    try:
        import signal
        import threading
        _tm_start()
        # SIGUSR1：完整深度快照；SIGUSR2：仅 tracemalloc（均只置标志）。
        try:
            signal.signal(signal.SIGUSR1, _on_sigusr1)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGUSR2, _on_sigusr2)
        except Exception:
            pass
        threading.Thread(target=_loop, name="memprobe", daemon=True).start()
        sys.stderr.write(
            f"[memprobe] 已启动（后台线程），间隔 {_INTERVAL:.0f}s，日志 {_LOG}\n"
            f"[memprobe] tracemalloc={'开' if _tm_state.get('enabled') else '关'}\n"
            f"[memprobe] 涨起来时：`kill -USR1 <pid>` 完整快照 / "
            f"`kill -USR2 <pid>` 仅 tracemalloc\n")
    except Exception as exc:
        sys.stderr.write(f"[memprobe] 启动失败: {exc}\n")


def main() -> None:
    """独立运行：对当前进程做一次快照（供快速自测）。"""
    snap = _snapshot()
    print(f"对象总数 = {sum(snap.values())}")
    for name, cnt in snap.most_common(30):
        print(f"  {cnt:>8}  {name}")


if __name__ == "__main__":
    main()
