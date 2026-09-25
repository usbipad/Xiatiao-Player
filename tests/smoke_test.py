"""重构冒烟测试基线。

目标：在不依赖图形显示（headless）的前提下，验证应用的核心逻辑
没有被重构破坏。每个重构阶段完成后都应能通过本脚本。

覆盖范围：
1. 核心模块可导入（models / core / config / providers）；
2. TrackItem 数据模型与音质判定逻辑；
3. Playlist 队列状态机（随机/循环/增删/移动）；
4. 配置读写；
5. 缓存序列化往返。

运行：
    python3 tests/smoke_test.py
退出码 0 表示全部通过。
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

# 保证能 import 项目根目录模块
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# GTK 类型注册需要 GI；headless 下不初始化显示即可。
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def test_imports() -> None:
    section("模块导入")
    try:
        from models import TrackItem, SOURCE_LOCAL  # noqa: F401
        check("models 导入", True)
    except Exception as exc:
        check("models 导入", False, repr(exc))
    try:
        from core import Playlist, PlayerCore  # noqa: F401
        check("core 导入", True)
    except Exception as exc:
        check("core 导入", False, repr(exc))
    try:
        from config.settings import get_config  # noqa: F401
        check("config 导入", True)
    except Exception as exc:
        check("config 导入", False, repr(exc))


def test_track_model() -> None:
    section("TrackItem 数据模型")
    from models import TrackItem

    t = TrackItem(title="歌", artist="手", album="辑",
                  duration="3:21", duration_seconds=201.0,
                  filepath="/x/a.flac", sample_rate=96000,
                  bit_depth=24, channels=2)
    check("字段赋值", t.title == "歌" and t.artist == "手")
    check("is_local", t.is_local is True)
    check("Hi-Res 判定(96k/24bit)", t.is_hires is True)
    check("quality_badge=HR", t.quality_badge == "HR", f"got={t.quality_badge!r}")

    cd = TrackItem(filepath="/x/b.flac", sample_rate=44100, bit_depth=16)
    check("CD 级判定", cd.is_cd_quality is True)
    check("quality_badge=CD", cd.quality_badge == "CD", f"got={cd.quality_badge!r}")

    dsd = TrackItem(filepath="/x/c.dsf")
    check("DSD 判定", dsd.is_dsd is True)
    check("quality_badge=DSD", dsd.quality_badge == "DSD", f"got={dsd.quality_badge!r}")

    mp3 = TrackItem(filepath="/x/d.mp3", sample_rate=44100, bit_depth=16)
    check("有损不算 CD 级", mp3.is_cd_quality is False)

    check("format_seconds", TrackItem.format_seconds(201) == "3:21")


def test_playlist() -> None:
    section("Playlist 队列状态机")
    from core import Playlist
    from core.playlist import REPEAT_ALL, REPEAT_OFF, REPEAT_ONE
    from models import TrackItem

    def mk(i: int) -> TrackItem:
        return TrackItem(title=f"t{i}", filepath=f"/x/{i}.flac")

    pl = Playlist()
    tracks = [mk(i) for i in range(5)]
    pl.set_tracks(tracks, autoplay_index=0)
    check("set_tracks 长度", len(pl) == 5)
    check("当前索引 0", pl.current_index() == 0)
    check("current_track", pl.current_track().title == "t0")

    pl.next()
    check("next → 1", pl.current_index() == 1)
    pl.previous()
    check("previous → 0", pl.current_index() == 0)

    # 单曲循环：自动切应停在当前
    pl.set_repeat_mode(REPEAT_ONE)
    pl.set_current_index(2)
    pl.next(auto=True)
    check("单曲循环 auto next 不动", pl.current_index() == 2)

    # 列表循环：末尾绕回
    pl.set_repeat_mode(REPEAT_ALL)
    pl.set_current_index(4)
    pl.next(auto=True)
    check("列表循环末尾绕回 0", pl.current_index() == 0)

    # 不循环：末尾停止
    pl.set_repeat_mode(REPEAT_OFF)
    pl.set_current_index(4)
    r = pl.next(auto=True)
    check("不循环末尾停止", r is None)

    # remove_at 修正索引
    pl2 = Playlist()
    pl2.set_tracks([mk(i) for i in range(4)], autoplay_index=2)
    pl2.remove_at(0)
    check("移除当前项之前 → 索引前移", pl2.current_index() == 1)
    check("移除后长度 3", len(pl2) == 3)

    # insert_next
    pl3 = Playlist()
    pl3.set_tracks([mk(0), mk(1), mk(2)], autoplay_index=0)
    pl3.insert_next(mk(9))
    check("insert_next 位置", pl3.tracks()[1].title == "t9")

    # 洗牌一轮不重复
    pl4 = Playlist()
    pl4.set_tracks([mk(i) for i in range(6)], autoplay_index=0)
    pl4.set_shuffle(True)
    seen = [pl4.current_index()]
    for _ in range(5):
        pl4.next()
        seen.append(pl4.current_index())
    check("洗牌一轮覆盖全部 6 首", len(set(seen)) == 6, f"seen={seen}")


def test_config() -> None:
    section("配置读写")
    from config.settings import AppConfig

    with tempfile.TemporaryDirectory() as d:
        from pathlib import Path
        p = Path(d) / "config.json"
        cfg = AppConfig(path=p)
        cfg.set_str("language", "en")
        cfg.set_int("window_width", 1234)
        cfg.set_bool("restore_playback", False)
        cfg.save()
        check("配置文件已写", p.is_file())
        cfg2 = AppConfig(path=p)
        check("字符串读回", cfg2.get_str("language") == "en")
        check("整数读回", cfg2.get_int("window_width") == 1234)
        check("布尔读回", cfg2.get_bool("restore_playback") is False)
        check("默认值兜底", cfg2.get_str("nonexist", "x") == "x")


def test_cache_roundtrip() -> None:
    section("缓存序列化往返")
    from models import TrackItem
    from core.cache import track_to_dict, dict_to_track, tracks_to_list, list_to_tracks

    t = TrackItem(title="A", artist="B", album="C",
                  filepath="/x/a.flac", duration="1:02",
                  duration_seconds=62.0, sample_rate=48000,
                  bit_depth=24, channels=2, bitrate=999000)
    d = track_to_dict(t)
    check("track_to_dict 类型", isinstance(d, dict))
    t2 = dict_to_track(d)
    check("往返 title", t2.title == "A")
    check("往返 sample_rate", t2.sample_rate == 48000)
    check("往返 bit_depth", t2.bit_depth == 24)
    check("往返 bitrate", t2.bitrate == 999000)

    lst = tracks_to_list([t, t])
    back = list_to_tracks(lst)
    check("列表往返长度", len(back) == 2)


def test_track_info_rows() -> None:
    section("歌曲信息行构建")
    try:
        from ui.window import MainWindow
    except Exception as exc:
        check("导入 MainWindow", False, repr(exc))
        return
    from models import TrackItem
    t = TrackItem(title="A", artist="B", album="C",
                  filepath="/x/a.flac", sample_rate=96000,
                  bit_depth=24, channels=2, bitrate=2304000)
    rows = MainWindow._build_track_info_rows(t)
    d = dict(rows)
    check("信息行非空", len(rows) >= 9, f"len={len(rows)}")
    check("歌名", d.get("歌名") == "A")
    check("采样率格式", d.get("采样率") == "96 kHz", f"got={d.get('采样率')!r}")
    check("位深格式", d.get("位深") == "24 bit")
    check("声道格式", d.get("声道") == "立体声 (2)")
    check("码率格式", d.get("码率") == "2304 kbps", f"got={d.get('码率')!r}")
    check("编码格式", d.get("Codec") == "FLAC")


def test_shortcuts() -> None:
    section("快捷键解析与匹配")
    from services.shortcuts import parse_shortcut, match_shortcut, SHORTCUT_ACTIONS

    p = parse_shortcut("space")
    check("space 可解析", p is not None)
    mods, kv = p if p else (frozenset(), 0)
    check("space 无修饰键", len(mods) == 0)

    p2 = parse_shortcut("Ctrl+Alt+Left")
    check("Ctrl+Alt+Left 可解析", p2 is not None)
    if p2:
        check("含 ctrl/alt", set(p2[0]) == {"ctrl", "alt"}, f"got={p2[0]}")

    check("空串返回 None", parse_shortcut("") is None)
    check("非法键返回 None", parse_shortcut("NotAKey!!!") is None)

    # 匹配
    cfg = {name: "" for name in SHORTCUT_ACTIONS}
    cfg["play_pause"] = "space"
    cfg["next"] = "Ctrl+Right"
    sp = parse_shortcut("space")
    check("匹配 play_pause", match_shortcut(cfg, sp[1], sp[0]) == "play_pause")
    cr = parse_shortcut("Ctrl+Right")
    check("匹配 next", match_shortcut(cfg, cr[1], cr[0]) == "next")
    check("无匹配返回 None", match_shortcut(cfg, 999999, set()) is None)


def main() -> int:
    print("=" * 56)
    print("夏条播放器 重构冒烟测试")
    print("=" * 56)
    tests = [test_imports, test_track_model, test_playlist,
             test_config, test_cache_roundtrip, test_track_info_rows,
             test_shortcuts]
    for fn in tests:
        try:
            fn()
        except Exception:
            global _failed
            _failed += 1
            print(f"  ERROR in {fn.__name__}:")
            traceback.print_exc()
    print("\n" + "=" * 56)
    print(f"结果: {_passed} 通过, {_failed} 失败")
    print("=" * 56)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
