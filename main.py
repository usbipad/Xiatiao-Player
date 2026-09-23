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


# ---- 调试入口 ----
# XIATIAO_DEBUG 控制日志级别与输出（未设置=保持常规 INFO 行为）：
#   XIATIAO_DEBUG=1        -> DEBUG 级别，日志写到 /tmp/xiatiao-debug.log
#   XIATIAO_DEBUG=verbose  -> 同上，且不再过滤 GTK 噪音警告
# 目的：出问题时一条命令开启全量诊断日志，无需改代码。
_DEBUG = os.environ.get("XIATIAO_DEBUG", "").strip().lower()
_DEBUG_ON = _DEBUG not in ("", "0", "false", "no", "off")
_DEBUG_VERBOSE = _DEBUG in ("verbose", "2", "all")

# verbose 调试模式下不过滤 GTK 噪音，保留原始警告便于排查。
if not _DEBUG_VERBOSE:
    try:
        GLib.log_set_handler("Gtk", GLib.LogLevelFlags.LEVEL_WARNING, _gtk_log_filter, None)
        GLib.log_set_handler("Adwaita", GLib.LogLevelFlags.LEVEL_WARNING, _gtk_log_filter, None)
    except Exception:
        pass

_log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s" if _DEBUG_ON else \
    "%(levelname)s %(name)s: %(message)s"
_log_path = "/tmp/xiatiao-debug.log" if _DEBUG_ON else "/tmp/xiatiao-app.log"
logging.basicConfig(
    level=logging.DEBUG if _DEBUG_ON else logging.INFO,
    format=_log_format,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_path, mode="w", encoding="utf-8"),
    ],
)
if _DEBUG_ON:
    logging.getLogger(__name__).info(
        "调试模式已开启（XIATIAO_DEBUG=%s）：级别=DEBUG，日志=%s%s",
        _DEBUG, _log_path, "，GTK 噪音不再过滤" if _DEBUG_VERBOSE else "")

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


def _set_process_name(name: str) -> None:
    """设置进程名，改善系统监视器 / htop / top 的显示。

    两层：
      1. /proc/PID/comm —— prctl(PR_SET_NAME)（15 字符上限）；
      2. /proc/PID/cmdline —— setproctitle（htop 默认显示的是这个）。
    两者都失败也无妨（非关键）。
    """
    # 1) comm（进程名）
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(15, name.encode("utf-8"), 0, 0, 0)  # PR_SET_NAME = 15
    except Exception:
        logging.getLogger(__name__).debug("设置进程名失败", exc_info=True)
    # 2) cmdline（命令行；htop 默认显示此项）。
    #    setproctitle 为可选依赖，缺失则跳过（comm 已改，够用）。
    try:
        import setproctitle  # type: ignore
        setproctitle.setproctitle(name)
    except Exception:
        logging.getLogger(__name__).debug("setproctitle 不可用，cmdline 保持原样", exc_info=True)


def main() -> None:
    # 显式设置程序名与应用名。
    # Wayland 下 GTK4 用 GLib prgname 作为窗口 app_id 的 fallback；
    # 桌面环境据此匹配 .desktop（图标 / 名称）。设成 APP_ID 与
    # com.xiatiao.player.desktop 对上。
    try:
        GLib.set_prgname(APP_ID)
        GLib.set_application_name("Xiatiao Player")
    except Exception:
        pass
    # 改 /proc/PID/comm，让系统监视器 / htop 显示应用名而非 "python3"。
    _set_process_name("xiatiao-player")

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
