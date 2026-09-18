"""异步任务基础设施。

GTK 不是线程安全的：所有 UI 更新必须在主线程（GLib 主循环）。
本模块提供统一的"后台执行 + 回主线程"模式，避免耗时操作阻塞界面。

用法：
    token = run_async(
        work=lambda: 耗时计算(),
        on_done=lambda result: 更新UI(result),
        on_error=lambda exc: 提示(exc),
    )
    token.cancel()   # 需要时取消（结果会被丢弃）

设计要点：
- 后台线程执行 work，完成后用 GLib.idle_add 把回调排到主线程。
- 任务可取消：取消后即使 work 跑完，也不再回调 UI。
- 不做线程池（避免复杂），每个任务一个 daemon 线程；
  本地扫描/网络请求这类低频任务足够。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from gi.repository import GLib

log = logging.getLogger(__name__)

#: 全局有界线程池：封面读取/元数据解析/网络等低频后台任务共用。
#  用固定容量避免快速连切歌时无限起线程（线程创建/销毁本身也是开销）。
#  max_workers 取较小值：这些任务多为 IO 密集，且并发过高会争抢 GIL。
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="xiatiao-async")


class TaskToken:
    """异步任务句柄，用于取消与查询状态。"""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def run_async(
    work: Callable[[], Any],
    on_done: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> TaskToken:
    """在后台线程执行 work，完成后在主线程回调。

    work:     后台执行的函数，返回值传给 on_done。
    on_done:  成功回调，在主线程执行，参数为 work 的返回值。
    on_error: 异常回调，在主线程执行，参数为异常对象。
    返回 TaskToken，可用于取消。
    """
    token = TaskToken()

    def worker() -> None:
        if token.cancelled:
            return
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001
            log.debug("异步任务异常: %s", exc)
            if not token.cancelled and on_error is not None:
                GLib.idle_add(_safe_call, on_error, exc)
            return
        if not token.cancelled and on_done is not None:
            GLib.idle_add(_safe_call, on_done, result)

    try:
        _EXECUTOR.submit(worker)
    except Exception as exc:  # 线程池关闭等极端情况：降级为同步执行，不丢任务
        log.debug("提交线程池失败，降级同步执行: %s", exc)
        worker()
    return token


def _safe_call(fn: Callable, arg: Any) -> bool:
    """在主线程调用回调，吞掉回调自身异常，避免中断主循环。"""
    try:
        fn(arg)
    except Exception as exc:  # noqa: BLE001
        log.warning("异步回调异常: %s", exc)
    return False


def post_to_main(fn: Callable, *args: Any) -> None:
    """从任意线程把一个调用排到主线程执行。"""
    GLib.idle_add(lambda: (fn(*args), False)[1])
