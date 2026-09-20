"""Xiatiao（虾条播放器）应用入口。"""
import logging
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ui import MainWindow  # noqa: E402


# ---- 过滤 GTK 层噪音警告 ----
# 窗口允许缩到很窄时，GtkOverlay 会周期性打印
#   "GtkOverlay ... exceeds MainWindow width: requested N px, M px available"
# 界面显示正常，仅属日志噪音，故在此静默，避免刷屏。
# 只过滤这一类消息，其它 GTK/Adwaita 警告照常输出。
_GTK_NOISE_MARKERS = (
    "exceeds MainWindow width",
    "exceeds ui+window+MainWindow width",
)


def _gtk_log_filter(domain, level, message, user_data):
    try:
        msg = str(message)
        if any(marker in msg for marker in _GTK_NOISE_MARKERS):
            return
    except Exception:
        pass
    # 非噪音：交回默认处理器（输出到 stderr）。
    if level & (GLib.LogLevelFlags.LEVEL_ERROR | GLib.LogLevelFlags.LEVEL_CRITICAL):
        sys.stderr.write(f"{domain or 'Gtk'}: {message}\n")
    elif level & GLib.LogLevelFlags.LEVEL_WARNING:
        sys.stderr.write(f"{domain or 'Gtk'}-WARNING: {message}\n")
    else:
        sys.stderr.write(f"{domain or 'Gtk'}: {message}\n")


try:
    GLib.log_set_handler("Gtk", GLib.LogLevelFlags.LEVEL_WARNING, _gtk_log_filter, None)
    GLib.log_set_handler("Adwaita", GLib.LogLevelFlags.LEVEL_WARNING, _gtk_log_filter, None)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/xiatiao-app.log", mode="w", encoding="utf-8"),
    ],
)

APP_ID = "com.xiatiao.player"
ICON_NAME = "xiatiao"

#: 项目内图标目录，作为运行时回退搜索路径
_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "icons")


def _register_icon_path() -> None:
    """把项目图标目录加入图标主题搜索路径。

    这样即使未执行系统安装，任务栏也能找到图标。
    """
    try:
        display = Gdk.Display.get_default()
        if display is None or not os.path.isdir(_ICON_DIR):
            return
        theme = Gtk.IconTheme.get_for_display(display)
        theme.add_search_path(_ICON_DIR)
    except Exception:
        logging.getLogger(__name__).debug("注册图标路径失败", exc_info=True)


def on_activate(app: Adw.Application) -> None:
    # 单实例：已有窗口则唤起（隐藏到后台后再启动程序可恢复）
    win = getattr(app, "_main_window", None)
    if win is None:
        win = MainWindow(app)
        app._main_window = win
    win.present()
    # 从隐藏状态恢复：重新显示
    if not win.get_visible():
        win.set_visible(True)
    win.present()


def main() -> None:
    app = Adw.Application(application_id=APP_ID)
    app.connect("activate", on_activate)

    # 启动后注册图标搜索路径并设置窗口默认图标
    def _on_startup(_app):
        _register_icon_path()
        Gtk.Window.set_default_icon_name(ICON_NAME)
        # 应用用户选择的主题
        try:
            from config.settings import get_config
            theme = get_config().get_str("theme", "system")
            mgr = Adw.StyleManager.get_default()
            if theme == "light":
                mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            elif theme == "dark":
                mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            else:
                mgr.set_color_scheme(Adw.ColorScheme.DEFAULT)
        except Exception:
            logging.getLogger(__name__).debug("应用主题失败", exc_info=True)

    app.connect("startup", _on_startup)
    # 应用级图标（影响任务栏/应用列表）
    if hasattr(app, "set_icon_name"):
        app.set_icon_name(ICON_NAME)

    app.run(sys.argv)


if __name__ == "__main__":
    main()
