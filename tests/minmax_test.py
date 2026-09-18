"""最小 GTK4 窗口：测最大化/恢复端到端时间，对比项目窗口。"""
import sys
import time
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib


class W(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="min")
        self.set_default_size(800, 600)
        self._click_t = None
        btn = Gtk.Button(label="max")
        btn.connect("clicked", self._on)
        self.set_child(btn)

    def _on(self, _b):
        self._click_t = time.monotonic()
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def do_size_allocate(self, w, h, b):
        if getattr(self, "_click_t", None):
            if getattr(self, "_settle", None):
                try:
                    GLib.source_remove(self._settle)
                except Exception:
                    pass
            self._settle = GLib.timeout_add(100, self._done)
        Gtk.ApplicationWindow.do_size_allocate(self, w, h, b)

    def _done(self):
        t = (time.monotonic() - self._click_t) * 1000
        print(f"min-window 最大化/恢复总用时={t:.0f}ms", flush=True)
        self._click_t = None
        self._settle = None
        return False


app = Gtk.Application(application_id="test.minwin")
def on_act(a):
    W(a).present()
app.connect("activate", on_act)
app.run()
