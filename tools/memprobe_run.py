"""一键内存诊断启动器：带探针启动 Xiatiao Player。

用法（在项目根目录）::

    python3 tools/memprobe_run.py
    # 可选环境变量：
    XIATIAO_MEMPROBE_INTERVAL=10 python3 tools/memprobe_run.py
    XIATIAO_MEMPROBE_LOG=/tmp/my.log python3 tools/memprobe_run.py

原理：导入 main 模块 -> 包装其 on_activate（原逻辑照跑，额外启动探针）
-> 调用 main.main()。**不修改 main.py**，探针只在此临时进程内生效。

启动后复现疑似泄漏的操作（切页 / 切歌 / 滚动），日志：

    cat /tmp/xiatiao-memprobe.log

停止：正常关窗口或 Ctrl+C。
"""
from __future__ import annotations

import os
import sys


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)  # 保证相对路径（配置 / 图标）与直接运行一致

    import main as app_main
    from tools.memprobe import start as _probe_start

    _orig_activate = app_main.on_activate

    def _patched_activate(app):
        _orig_activate(app)          # 先正常建窗口
        try:
            _probe_start()           # 再挂探针
        except Exception as exc:
            sys.stderr.write(f"[memprobe_run] 探针启动失败: {exc}\n")

    app_main.on_activate = _patched_activate
    sys.stderr.write("[memprobe_run] 已注入内存探针，启动应用…\n")
    app_main.main()


if __name__ == "__main__":
    main()
