"""GObject / GTK4 控件树的「断环」清理工具。

背景（本项目多次踩坑，已实测）：
GTK4 的 C 层会持有 Python 回调（绑定方法 / lambda 闭包）。当回调捕获了
某个 Python 对象（如页面 self）时，形成跨 C/Python 引用环：

    widget(C层) → 回调(绑定方法/lambda) → self → 整棵控件树

C 层不参与 Python gc，环破不了 → 对象永不回收。PyGObject **不暴露**
"枚举某对象全部 handler"的实例方法，但提供了**模块级**函数：

    GObject.signal_handlers_disconnect_matched(
        widget, GObject.SignalMatchType.ID, signal_id, 0, None, None, None)

本工具用三层手段覆盖各类回调：
  1) `GObject.signal_lookup("notify", GObject.Object)` + 上述 matched 调用
     断开所有 `notify::xxx`（Gtk.Switch/ComboRow 等属性变更都走它，含
     lambda 闭包）；
  2) 对常见非 notify 信号（clicked / activated / changed / value-changed /
     toggled / pressed / ...）按**控件自身类型**查 signal id 后同样 matched
     断开——这样连在这些信号上的**匿名 lambda** 也能断（关键：
     signal_lookup 必须传具体类型，如 Gtk.Button，传 GObject.Object 查不到）；
  3) owner 的**绑定方法**用 `widget.disconnect_by_func(meth)` 逐控件断。

用法：
    from ui.gobject_cleanup import disconnect_widget_tree
    disconnect_widget_tree(page, owner=page)

注：若某信号的匿名 lambda 不在上述常见名单内，仍无法断——此时应把该
lambda 改为绑定方法（见 effect_page 的实践）。新增信号名可加入
`_common_sig_names`。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def iter_widget_tree(root):
    """深度优先遍历 root 及其所有子控件。"""
    yield root
    try:
        child = root.get_first_child()
    except Exception:
        child = None
    while child is not None:
        yield from iter_widget_tree(child)
        try:
            child = child.get_next_sibling()
        except Exception:
            break


def _collect_bound_methods(owner) -> list:
    """收集 owner 上所有「绑定方法」（method 且 __self__ 是 owner）。"""
    meths = []
    for name in dir(owner):
        try:
            v = getattr(owner, name)
        except Exception:
            continue
        if callable(v) and getattr(v, "__self__", None) is owner:
            meths.append(v)
    return meths


def disconnect_widget_tree(root, owner=None) -> None:
    """断开 root 及其子控件上的信号连接，破解跨 C/Python 引用环。

    root:  控件树根（通常是页面/窗口自身）。
    owner: 回调所属的 Python 对象（默认 root）；其绑定方法会被 disconnect_by_func
           逐控件断开。

    行为：
      1) 对每个控件断开所有 notify::xxx（覆盖 lambda 闭包）；
      2) 对每个控件用 owner 的绑定方法尝试 disconnect_by_func；
      3) 若子控件自身有 disconnect_global_refs，先调用它（递归清理内部环，
         如 PeqCurve 的 set_draw_func）。

    幂等、失败静默。
    """
    if owner is None:
        owner = root
    try:
        from gi.repository import GObject
        _notify_id = GObject.signal_lookup("notify", GObject.Object)
    except Exception:
        GObject = None
        _notify_id = 0
    # 常见「会挂 lambda 闭包」的非 notify 信号名。signal_lookup 必须用
    # **控件自身类型**（clicked 在 Gtk.Button 上，用 Object 查不到），
    # 故对每个控件按它的 type 逐个 lookup。
    _common_sig_names = ("clicked", "activated", "changed", "value-changed",
                         "toggled", "pressed", "released", "selected",
                         "activate", "state-set")
    meths = _collect_bound_methods(owner)
    for w in iter_widget_tree(root):
        # 先让「自身也持有引用环」的子控件清理（如 PeqCurve）
        if w is not owner:
            sub = getattr(w, "disconnect_global_refs", None)
            if callable(sub):
                try:
                    sub()
                except Exception:
                    pass
        # 1) 断 notify（含 lambda 闭包）
        if GObject is not None and _notify_id:
            try:
                GObject.signal_handlers_disconnect_matched(
                    w, GObject.SignalMatchType.ID, _notify_id, 0, None, None, None)
            except Exception:
                pass
        # 1b) 断常见非 notify 信号（clicked/changed/... 上的 lambda）：
        # 用控件自身类型查 signal id（clicked 等只在具体类型上存在）。
        if GObject is not None:
            wtype = type(w)
            for nm in _common_sig_names:
                try:
                    sid = GObject.signal_lookup(nm, wtype)
                except Exception:
                    sid = 0
                if not sid:
                    continue
                try:
                    GObject.signal_handlers_disconnect_matched(
                        w, GObject.SignalMatchType.ID, sid, 0, None, None, None)
                except Exception:
                    pass
        # 2) 断 owner 的绑定方法在该控件上的连接
        for m in meths:
            try:
                w.disconnect_by_func(m)
            except Exception:
                pass
