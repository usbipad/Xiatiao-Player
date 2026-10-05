"""DspState 单一真相源单元测试。

背景：DspState 是 DSP 参数重构后的唯一真相源，各视图只 patch/replace，
由 window 统一出口下发。本次重构（批 1）收口了双写，必须有测试锁住语义：
  - patch 只改传入字段；
  - replace 用默认基底重建；
  - reset 可保留字段；
  - 变更广播 changed/replaced；
  - 广播回调里再 patch 被防御（防递归）；
  - get() 返回深拷贝（外部改不影响内部）。

运行：
    python3 tests/test_dsp_state.py

退出码 0=通过 / 1=失败。不依赖 pytest、不写真实配置
（用临时 XDG_CONFIG_HOME 隔离）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 用临时配置目录隔离，避免污染用户真实 config.json。
_TMP = tempfile.mkdtemp(prefix="xiatiao-dspstate-")
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["XDG_DATA_HOME"] = _TMP

_FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), name, detail)
    if not ok:
        _FAILS.append(name)


def _fresh_state():
    """每次构造全新 DspState 实例，保证测试相互独立。

    注意：不能用 importlib.reload —— PyGObject 不允许重复注册同名 GType
    （DspState），reload 会抛 RuntimeError。直接实例化即可（实例各自独立）。
    """
    from core.dsp_state import DspState
    return DspState()


def test_patch_merges() -> None:
    s = _fresh_state()
    s.patch({"enabled": True, "pre_gain_db": -3.0}, persist=False)
    cur = s.get()
    check("patch enabled", cur["enabled"] is True)
    check("patch pre_gain_db", cur["pre_gain_db"] == -3.0)
    # 未传字段保持默认（不丢字段）
    check("patch 未传字段保留", "eq_gains" in cur and len(cur["eq_gains"]) == 10)
    # 再 patch 只改一个字段，其它不变
    s.patch({"pre_gain_db": 6.0}, persist=False)
    cur2 = s.get()
    check("patch 二次只改一个", cur2["pre_gain_db"] == 6.0 and cur2["enabled"] is True)


def test_replace_rebuilds() -> None:
    s = _fresh_state()
    s.patch({"enabled": True, "eq_enabled": True}, persist=False)
    # replace 用默认基底重建：只保留传入字段，其余回默认
    s.replace({"enabled": True}, persist=False)
    cur = s.get()
    check("replace 保留传入", cur["enabled"] is True)
    check("replace 其余回默认", cur["eq_enabled"] is False)


def test_reset_keep() -> None:
    s = _fresh_state()
    s.patch({"enabled": True, "pre_gain_db": 9.0}, persist=False)
    s.reset(keep=["enabled"], persist=False)
    cur = s.get()
    check("reset 保留 enabled", cur["enabled"] is True)
    check("reset 清掉 pre_gain_db", cur["pre_gain_db"] == 0.0)


def test_get_is_deepcopy() -> None:
    s = _fresh_state()
    s.patch({"eq_gains": [1.0] * 10}, persist=False)
    a = s.get()
    a["eq_gains"][0] = 999.0
    b = s.get()
    check("get 深拷贝（外部改不影响内部）", b["eq_gains"][0] == 1.0)


def test_unknown_keys_ignored() -> None:
    s = _fresh_state()
    before = s.get()
    s.patch({"nonexistent_field_xyz": 123}, persist=False)
    after = s.get()
    check("未知字段不进入状态", "nonexistent_field_xyz" not in after)
    check("patch 未知字段不改已有", len(before) == len(after))


def test_signals() -> None:
    s = _fresh_state()
    got = {"changed": 0, "replaced": 0}
    s.connect("changed", lambda *_: got.__setitem__("changed", got["changed"] + 1))
    s.connect("replaced", lambda *_: got.__setitem__("replaced", got["replaced"] + 1))
    s.patch({"enabled": True}, persist=False)
    check("patch 触发 changed", got["changed"] == 1)
    s.replace({"enabled": False}, persist=False)
    check("replace 触发 replaced", got["replaced"] == 1)
    s.reset(persist=False)
    check("reset 触发 replaced", got["replaced"] == 2)


def test_recursion_guard() -> None:
    """广播回调里再 patch 应被忽略（防无限递归）。"""
    s = _fresh_state()
    fired = {"n": 0}

    def _on_changed(_state, _params):
        fired["n"] += 1
        # 试图在回调里再 patch —— 应被 _in_patch 防御忽略
        s.patch({"enabled": True}, persist=False)

    s.connect("changed", _on_changed)
    s.patch({"enabled": True}, persist=False)
    check("递归被防（回调只触发一次）", fired["n"] == 1)


def main() -> int:
    test_patch_merges()
    test_replace_rebuilds()
    test_reset_keep()
    test_get_is_deepcopy()
    test_unknown_keys_ignored()
    test_signals()
    test_recursion_guard()
    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 -> {_FAILS}")
        return 1
    print("DSP STATE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
