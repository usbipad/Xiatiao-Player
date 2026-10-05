"""Python 侧冒烟自检（无需启动 GUI）。

覆盖：
  1) 关键模块可导入（含 GI / 依赖可用性）；
  2) provider 注册表可用（内置 local + subsonic）；
  3) 外部插件环境变量解析不崩；
  4) 核心纯逻辑快速自检。

运行：
    python3 tests/smoke_test.py

退出码 0=通过 / 1=失败。
"""
from __future__ import annotations

import os
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


def test_imports() -> None:
    # GI / GTK4 / Adw
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw  # noqa: F401
        check("import gi/gtk4/adw", True)
    except Exception as exc:
        check("import gi/gtk4/adw", False, str(exc))
    # 核心模块
    for mod in ("config.settings", "core.quality", "core.playlist", "models.track"):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as exc:
            check(f"import {mod}", False, str(exc))


def test_providers() -> None:
    try:
        import providers
        sources = providers.available_source_types()
        check("provider local present", "local" in sources, str(sources))
        check("provider subsonic present", "subsonic" in sources, str(sources))
    except Exception as exc:
        check("provider registry", False, str(exc))
    # 外部插件环境变量不应导致崩溃（指向不存在模块）。
    try:
        import importlib
        import providers
        os.environ["XIATIAO_PROVIDER_MODULES"] = "nonexistent.mod:Foo;broken"
        importlib.reload(providers)
        sources = providers.available_source_types()
        check("bad external env tolerated", "local" in sources, str(sources))
    except Exception as exc:
        check("bad external env tolerated", False, str(exc))
    finally:
        os.environ.pop("XIATIAO_PROVIDER_MODULES", None)


def test_core_logic() -> None:
    try:
        from core.quality import quality_to_bitrate
        check("quality map", quality_to_bitrate("lossless") == 999)
    except Exception as exc:
        check("quality map", False, str(exc))
    try:
        from models.track import TrackItem
        check("track badge online",
              TrackItem(stream_url="http://h/rest/stream.view?id=1",
                        sample_rate=96000, bit_depth=24).quality_badge == "HR")
    except Exception as exc:
        check("track badge online", False, str(exc))


def main() -> int:
    test_imports()
    test_providers()
    test_core_logic()
    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}):", _FAILS)
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
