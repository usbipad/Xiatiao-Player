"""文本/数值格式化辅助（纯函数，从 ui/window.py 抽出）。

这些函数不依赖任何 GTK 控件或窗口状态，是通用的小工具，
放 window（上帝对象）里既臃肿又难复用。
"""
from __future__ import annotations


def human_size(n: float) -> str:
    """把字节数格式化成可读大小（如 1.2 MB）。失败返回「—」。"""
    try:
        n = float(n)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def glib_escape(text: str) -> str:
    """转义 Pango markup 特殊字符（& < > ' "）。"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;"))
