#!/usr/bin/env python3
"""尝试复现 gtk_list_base_update_adjustments 崩溃。

场景：ColumnView + PlayingIndicator；频繁切歌（set_current_key）+ 列表 splice。
"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GLib, GObject

from ui.widgets.playing_indicator import PlayingIndicator, set_current_key


class Track(GObject.Object):
    __gtype_name__ = 'ReproTrack'
    filepath = GObject.Property(type=str, default='')
    title = GObject.Property(type=str, default='')

    def __init__(self, fp, title):
        super().__init__()
        self.filepath = fp
        self.title = title


app = Gtk.Application(application_id='dev.x.repro')


def on_activate(a):
    win = Gtk.ApplicationWindow(application=a)
    win.set_default_size(700, 500)

    store = Gio.ListStore(item_type=Track)
    for i in range(50):
        store.append(Track(f'/m/{i}.flac', f't{i}'))
    sel = Gtk.SingleSelection(model=Gtk.SortListModel(model=store))
    cv = Gtk.ColumnView(model=sel)

    ind_factory = Gtk.SignalListItemFactory()

    def ind_setup(_f, li):
        ind = PlayingIndicator(width=14, height=14)
        li.set_child(ind)
        li._ind = ind

    def ind_bind(_f, li):
        item = li.get_item()
        ind = getattr(li, '_ind', None)
        if ind is None or item is None:
            return
        ind.set_row_key(getattr(item, 'filepath', ''))
        ind.sync_now()

    ind_factory.connect('setup', ind_setup)
    ind_factory.connect('bind', ind_bind)
    col = Gtk.ColumnViewColumn(title='', factory=ind_factory)
    col.set_fixed_width(30)
    cv.append_column(col)

    tf = Gtk.SignalListItemFactory()
    def t_setup(_f, li): li.set_child(Gtk.Label(label='x'))
    def t_bind(_f, li):
        it = li.get_item()
        c = li.get_child()
        if it and c: c.set_text(it.title)
    tf.connect('setup', t_setup)
    tf.connect('bind', t_bind)
    c2 = Gtk.ColumnViewColumn(title='t', factory=tf)
    c2.set_expand(True)
    cv.append_column(c2)

    sw = Gtk.ScrolledWindow()
    sw.set_child(cv)
    win.set_child(sw)
    win.present()

    state = {'n': 0}

    def churn():
        """交替：切歌 + 重建列表，模拟真实操作。"""
        state['n'] += 1
        n = state['n']
        # 切歌
        set_current_key(f'/m/{n % 50}.flac')
        # 重建列表（splice）
        newlist = [Track(f'/m/{(n + i) % 50}.flac', f't{(n + i) % 50}') for i in range(50)]
        store.splice(0, store.get_n_items(), newlist)
        if n >= 30:
            print('churn 完成，无崩溃')
            a.quit()
            return False
        return True

    GLib.timeout_add(50, churn)


app.connect('activate', on_activate)
app.run([])
