"""IPC 契约测试：校验 core/ipc_protocol.py 与 Rust protocol.rs 对齐。

目的：Python 侧命令字/事件名集中在 ipc_protocol.py，Rust 侧在
protocol.rs 的 enum（serde rename_all="snake_case"）。本测试：
  1) 校验 Python 契约集合自洽（无重复、命名规范）；
  2) 从 protocol.rs 源码里**提取** enum 变体名，转成 snake_case，
     与 Python 契约集合比对——若两侧漂移（加命令忘同步）即失败。

运行：
    python3 tests/test_ipc_protocol.py

退出码 0=通过 / 1=失败。不依赖 pytest。
"""
from __future__ import annotations

import re
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


def _snake(name: str) -> str:
    """Rust 变体名 -> serde snake_case（如 EndOfStream -> end_of_stream）。"""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _extract_rust_variants(src: str, enum_name: str) -> set[str]:
    """从 protocol.rs 文本里提取指定 enum 的变体名（首个词元）。"""
    m = re.search(rf"pub enum {enum_name} \{{(.*?)\n\}}", src, re.S)
    if not m:
        return set()
    body = m.group(1)
    variants = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        mm = re.match(r"([A-Z][A-Za-z0-9]*)\b", line)
        if mm:
            variants.add(mm.group(1))
    return variants


def test_python_contract() -> None:
    from core.ipc_protocol import Cmd, Evt, Field
    # 集合自洽：ALL 与逐个常量一致（防止加了常量忘了进 ALL）
    check("Cmd.ALL == 逐项", Cmd.ALL == frozenset({
        Cmd.PING, Cmd.PLAY, Cmd.PAUSE, Cmd.RESUME, Cmd.STOP, Cmd.SEEK,
        Cmd.SET_VOLUME, Cmd.SET_EFFECT, Cmd.SET_DSP, Cmd.SET_CAMILLA_YAML,
        Cmd.SET_COLORING, Cmd.RESET_DSP, Cmd.SET_DSD_MODE,
        Cmd.SET_OUTPUT_DEVICE, Cmd.LIST_OUTPUT_DEVICES, Cmd.QUERY_STATE,
        Cmd.SHUTDOWN,
    }))
    check("Evt.ALL == 逐项", Evt.ALL == frozenset({
        Evt.READY, Evt.PONG, Evt.ACK, Evt.ERROR, Evt.POSITION, Evt.DURATION,
        Evt.STATE, Evt.END_OF_STREAM, Evt.AUDIO_INFO, Evt.EFFECT, Evt.DSP,
        Evt.OUTPUT_DEVICES,
    }))
    # 命名规范：全小写 + 下划线
    bad_cmd = [c for c in Cmd.ALL if not re.fullmatch(r"[a-z0-9_]+", c)]
    bad_evt = [e for e in Evt.ALL if not re.fullmatch(r"[a-z0-9_]+", e)]
    check("Cmd 命名规范", not bad_cmd, str(bad_cmd))
    check("Evt 命名规范", not bad_evt, str(bad_evt))
    # 字段名常量非空
    check("Field 常量非空", all(
        isinstance(getattr(Field, k), str) and getattr(Field, k)
        for k in dir(Field) if not k.startswith("_")))


def test_rust_alignment() -> None:
    """与 Rust protocol.rs 比对（文件不存在则跳过，便于纯 Python 环境）。"""
    proto = _ROOT / "audio_backend_rs" / "src" / "protocol.rs"
    if not proto.is_file():
        print("SKIP rust alignment (protocol.rs not found)")
        return
    src = proto.read_text(encoding="utf-8")
    from core.ipc_protocol import Cmd, Evt

    rust_req = {_snake(v) for v in _extract_rust_variants(src, "Request")}
    rust_evt = {_snake(v) for v in _extract_rust_variants(src, "Event")}

    # Request 与 Cmd 应完全一致（含顺序无关的集合相等）
    only_py = Cmd.ALL - rust_req
    only_rs = rust_req - Cmd.ALL
    check("Cmd ⊆ Rust Request", not only_py, f"Python 多出: {sorted(only_py)}")
    check("Rust Request ⊆ Cmd", not only_rs, f"Rust 多出: {sorted(only_rs)}")

    only_py_e = Evt.ALL - rust_evt
    only_rs_e = rust_evt - Evt.ALL
    check("Evt ⊆ Rust Event", not only_py_e, f"Python 多出: {sorted(only_py_e)}")
    check("Rust Event ⊆ Evt", not only_rs_e, f"Rust 多出: {sorted(only_rs_e)}")


def main() -> int:
    test_python_contract()
    test_rust_alignment()
    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 -> {_FAILS}")
        return 1
    print("IPC PROTOCOL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
