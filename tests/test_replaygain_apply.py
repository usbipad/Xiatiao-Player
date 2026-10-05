"""ReplayGain 读取与应用单元测试（无网络、无真实音频文件）。

覆盖 core/replaygain_apply.py：
  - read_track_gain：开关关闭返回 None；无路径返回 None；开启且有路径才读。
  - apply_gain：有 DspState 时只 patch replaygain_db；无 DspState 时降级 push。

运行：python3 tests/test_replaygain_apply.py
退出码 0=通过 / 1=失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), name, detail)
    if not ok:
        _FAILS.append(name)


class _FakeTrack:
    def __init__(self, filepath="/nonexistent/x.flac"):
        self.filepath = filepath


class _FakeDspState:
    """记录 patch 调用，模拟 DspState。"""
    def __init__(self):
        self.patched = []

    def patch(self, changes, source=None):
        self.patched.append(dict(changes))


def test_read_disabled() -> None:
    from core.replaygain_apply import read_track_gain
    # 关闭 ReplayGain → None（不读文件）
    check("开关关闭→None", read_track_gain(_FakeTrack(), {"replaygain_enabled": False}) is None)
    # 非 dict → None
    check("params 非 dict→None", read_track_gain(_FakeTrack(), None) is None)


def test_read_no_path() -> None:
    from core.replaygain_apply import read_track_gain
    # 开启但无路径 → None
    r = read_track_gain(_FakeTrack(filepath=""), {"replaygain_enabled": True, "replaygain_mode": "track"})
    check("无路径→None", r is None)


def test_read_with_path() -> None:
    from core.replaygain_apply import read_track_gain
    # 开启 + 有路径：文件不存在 → read_replaygain 返回 None（不崩）
    r = read_track_gain(_FakeTrack(filepath="/tmp/definitely-missing-xyz.flac"),
                        {"replaygain_enabled": True, "replaygain_mode": "track"})
    check("缺失文件→None 不崩", r is None)


def test_apply_with_state() -> None:
    from core.replaygain_apply import apply_gain
    st = _FakeDspState()
    pushed = []
    apply_gain(-7.5, dsp_state=st, push_dsp=lambda p: pushed.append(p))
    check("patch 一次", len(st.patched) == 1)
    check("只改 replaygain_db", st.patched[0] == {"replaygain_db": -7.5}, str(st.patched))
    check("有 state 时不降级 push", not pushed)


def test_apply_none_gain() -> None:
    from core.replaygain_apply import apply_gain
    st = _FakeDspState()
    apply_gain(None, dsp_state=st, push_dsp=lambda p: None)
    check("None 增益→0.0", st.patched[0] == {"replaygain_db": 0.0}, str(st.patched))


def test_apply_fallback() -> None:
    from core.replaygain_apply import apply_gain
    pushed = []
    persisted = []
    apply_gain(3.0, dsp_state=None,
               push_dsp=lambda p: pushed.append(p),
               fallback_params={"enabled": True, "replaygain_db": 0.0},
               persist=lambda p: persisted.append(p))
    check("无 state→降级 push", len(pushed) == 1)
    check("降级保留其它字段", pushed and pushed[0].get("enabled") is True and pushed[0].get("replaygain_db") == 3.0, str(pushed))
    check("降级持久化", len(persisted) == 1)


def main() -> int:
    test_read_disabled()
    test_read_no_path()
    test_read_with_path()
    test_apply_with_state()
    test_apply_none_gain()
    test_apply_fallback()
    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 -> {_FAILS}")
        return 1
    print("REPLAYGAIN APPLY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
