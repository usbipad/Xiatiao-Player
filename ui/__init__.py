"""UI 包入口。

在导入任何子模块前，先声明 GTK/Adw 等库的版本。

原因：PyGObject 要求 `gi.require_version` 必须在**首次** `from gi.repository
import Adw/Gtk/...` 之前调用；而本包入口先执行 `from .window import ...`，
window 等子模块顶部又直接 `from gi.repository import Adw`。若不在此处先声明，
会触发 `PyGIWarning: Adw was imported without specifying a version first`，
且依赖调用方（如 main.py）是否恰好先声明过——属隐式的导入顺序依赖。
在此集中声明后，任何 `import ui.xxx` 都会先经过本入口，子模块无需各自重复。
"""
import gi as _gi

_gi.require_version("Gtk", "4.0")
_gi.require_version("Adw", "1")
_gi.require_version("Gdk", "4.0")
try:
    _gi.require_version("GdkPixbuf", "2.0")
except ValueError:
    # 版本已声明过（重复 require 同版本 OK，异版本会抛错）：忽略。
    pass

from .window import MainWindow  # noqa: E402

__all__ = ["MainWindow"]
