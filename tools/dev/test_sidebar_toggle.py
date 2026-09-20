#!/usr/bin/env python3
"""验证侧栏按钮手动切换（不随窗口宽度自动折叠）。"""
import os
import sys

# 从 tools/dev/ 上溯到项目根，便于 import ui/*
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import gi  # noqa: E402

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib  # noqa: E402

app = Adw.Application(application_id='dev.x.toggle')


def on_activate(a):
    from ui.window import MainWindow
    win = MainWindow(a)
    sv = win._split_view
    btn = win._sidebar_toggle_btn
    win.set_default_size(1200, 700)
    win.present()

    def s1():
        print('初始: show_sidebar=', sv.get_show_sidebar(), 'btn_active=', btn.get_active())
        btn.set_active(False)
        GLib.timeout_add(400, s2)
        return False

    def s2():
        print('点按钮隐藏后: show_sidebar=', sv.get_show_sidebar(), 'btn_active=', btn.get_active())
        btn.set_active(True)
        GLib.timeout_add(400, s3)
        return False

    def s3():
        print('再点显示后: show_sidebar=', sv.get_show_sidebar(), 'btn_active=', btn.get_active())
        a.quit()
        return False

    GLib.timeout_add(700, s1)


app.connect('activate', on_activate)
app.run([])
