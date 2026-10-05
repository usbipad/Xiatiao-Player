"""边界/单元测试（无 GUI、无网络、可随时复跑）。

覆盖：音质规格判定、在线档位映射、TrackItem 徽章（含在线流）、配置读写、
      三个 SQLite 存储、播放队列（含随机播放覆盖）。

运行：
    python3 tests/test_boundary.py

设计：不依赖 pytest；纯 assert + 计数，退出码 0=全过 / 1=有失败。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 允许从仓库根直接运行。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(("PASS" if ok else "FAIL"), name, "=>", repr(got), "" if ok else f"(want {want!r})")
    if not ok:
        _FAILS.append(name)


def test_quality() -> None:
    from core.quality import (
        spec_from_info, spec_rank, is_degraded,
        quality_to_bitrate, dsd_spec_from_rate, multichannel_label,
    )
    # 见文件末尾 test_quality_constants：集中化常量一致性
    check("spec empty", spec_from_info({}), "")
    check("spec None", spec_from_info(None), "")
    check("flac 44.1/16 cd", spec_from_info({"codec": "flac", "sample_rate": 44100, "bit_depth": 16}), "cd")
    check("flac 48/24 hr", spec_from_info({"codec": "flac", "sample_rate": 48000, "bit_depth": 24}), "hr")
    check("96k hr", spec_from_info({"codec": "flac", "sample_rate": 96000, "bit_depth": 24}), "hr")
    check("352800 dxd", spec_from_info({"codec": "flac", "sample_rate": 352800, "bit_depth": 24}), "dxd")
    check("mp3 lossy empty", spec_from_info({"codec": "mp3", "sample_rate": 44100}), "")
    check("dsd soft dsd64", spec_from_info({"codec": "dsd", "sample_rate": 352800}), "dsd64")
    check("no codec 96k hr", spec_from_info({"sample_rate": 96000}), "hr")
    check("dsd rate 0", dsd_spec_from_rate(0), "dsd")
    check("dsd 2822400", dsd_spec_from_rate(2822400), "dsd64")
    check("dsd 11289600", dsd_spec_from_rate(11289600), "dsd256")
    check("rank empty", spec_rank(""), 0)
    check("rank dsd", spec_rank("dsd"), 6)
    check("degraded hr->cd", is_degraded("hr", "cd"), True)
    check("degraded cd->hr", is_degraded("cd", "hr"), False)
    check("q lossless", quality_to_bitrate("lossless"), 999)
    check("q hires", quality_to_bitrate("hires"), 1400)
    check("q empty", quality_to_bitrate(""), 0)
    check("q unknown", quality_to_bitrate("zzz"), 0)
    check("mc 6ch", multichannel_label({"channels": 6}), "5.1")
    check("mc 2ch", multichannel_label({"channels": 2}), "")


def test_quality_constants() -> None:
    """音质常量集中化：短名 / 期望规格 / 显示名 派生一致。"""
    from core.quality import (
        ONLINE_QUALITY_LABELS, ONLINE_QUALITY_SHORT,
        quality_short, quality_expect_spec, spec_display_name,
    )
    # 短名派生自 LABELS（去掉码率），键集合一致
    check("short keys == labels keys",
          set(ONLINE_QUALITY_SHORT), {k for k, _ in ONLINE_QUALITY_LABELS})
    check("short lossless", quality_short("lossless"), "无损")
    check("short hires", quality_short("hires"), "Hi-Res")
    check("short 未知回退", quality_short("zzz"), "zzz")
    check("expect lossless cd", quality_expect_spec("lossless"), "cd")
    check("expect hires hr", quality_expect_spec("hires"), "hr")
    check("expect 未知空", quality_expect_spec("zzz"), "")
    check("display cd 无损", spec_display_name("cd"), "无损")
    check("display hr Hi-Res", spec_display_name("hr"), "Hi-Res")
    check("display 未知回退", spec_display_name("zz"), "zz")


def test_track_badge() -> None:
    """TrackItem 音质徽章：重点覆盖「在线流」（stream.view）路径。"""
    from models.track import TrackItem
    # 在线流：扩展名是 view（不可辨识），应按参数判定。
    check("online 96k/24 HR",
          TrackItem(stream_url="http://h/rest/stream.view?id=1", sample_rate=96000, bit_depth=24).quality_badge, "HR")
    check("online 44.1/16 CD",
          TrackItem(stream_url="http://h/rest/stream.view?id=2", sample_rate=44100, bit_depth=16).quality_badge, "CD")
    check("online 352.8k DXD",
          TrackItem(stream_url="http://h/rest/stream.view?id=3", sample_rate=352800, bit_depth=24).quality_badge, "DXD")
    check("online no-info empty",
          TrackItem(stream_url="http://h/rest/stream.view?id=4").quality_badge, "")
    # 本地：行为不变。
    check("local flac 96k HR", TrackItem(filepath="/x.flac", sample_rate=96000, bit_depth=24).quality_badge, "HR")
    check("local flac 44.1 CD", TrackItem(filepath="/y.flac", sample_rate=44100, bit_depth=16).quality_badge, "CD")
    check("local mp3 lossy empty", TrackItem(filepath="/z.mp3", sample_rate=96000).quality_badge, "")
    check("local dsf DSD64", TrackItem(filepath="/a.dsf", sample_rate=2822400).quality_badge, "DSD64")
    # playable/play_url/exists
    check("missing not exists", TrackItem(filepath="/no/x.flac").exists, False)
    check("stream playable", TrackItem(stream_url="http://h/s?id=1").playable, True)


def test_config() -> None:
    from config.settings import AppConfig, SUPPORTED_EXTENSIONS
    d = tempfile.mkdtemp()
    p = Path(d) / "config.json"
    c = AppConfig(path=p)
    check("default music_dirs", c.get_music_dirs(), [])
    check("int fallback", c.get_int("nope", 42), 42)
    check("bool fallback", c.get_bool("nope", True), True)
    # 坏 JSON → 回退默认，不崩。
    p.write_text("{ not json")
    check("corrupt load ok", AppConfig(path=p).get_str("theme", "system"), "system")
    # 往返。
    c2 = AppConfig(path=p)
    c2.set_str("theme", "dark")
    c2.set_int("vx", 5)
    c3 = AppConfig(path=p)
    check("roundtrip str", c3.get_str("theme"), "dark")
    check("roundtrip int", c3.get_int("vx"), 5)
    # 目录去重。
    c3.set_music_dirs([])
    check("add dir", c3.add_music_dir("/m"), True)
    check("add dup", c3.add_music_dir("/m"), False)
    check("remove dir", c3.remove_music_dir("/m"), True)
    check("ext flac", ".flac" in SUPPORTED_EXTENSIONS, True)
    check("ext txt", ".txt" in SUPPORTED_EXTENSIONS, False)


def test_stores() -> None:
    from core.liked_store import LikedStore
    from core.playlist_store import PlaylistStore
    from core.history_store import HistoryStore

    class T:
        def __init__(self, **k):
            self.__dict__.update(k)

    d = Path(tempfile.mkdtemp())
    lk = LikedStore(path=d / "l.db")
    t = T(source_id="s1", title="T1", filepath="/f1")
    check("liked add", lk.add(t), True)
    check("liked is", lk.is_liked(t), True)
    check("liked toggle off", lk.toggle(t), False)
    check("liked count 0", lk.count(), 0)

    ps = PlaylistStore(path=d / "p.db")
    pid = ps.create("P")
    check("pl add", ps.add_tracks(pid, [t, T(source_id="s2")]), 2)
    check("pl dedupe", ps.add_tracks(pid, [t]), 0)
    check("pl rows", len(ps.rows_of(pid)), 2)
    check("pl delete", ps.delete(pid), True)

    hs = HistoryStore(path=d / "h.db")
    for i in range(40):
        hs.record(T(source_id=f"h{i}"))
    check("history cap<=30", hs.count() <= 30, True)
    check("history clear", (hs.clear(), hs.count())[1], 0)


def test_playlist() -> None:
    from core.playlist import Playlist, REPEAT_OFF, REPEAT_ALL, REPEAT_ONE
    from models import TrackItem

    pl = Playlist()
    pl.set_tracks([TrackItem(title=f"T{i}", source_id=f"s{i}") for i in range(5)], autoplay_index=0)
    check("len", len(pl), 5)
    check("next", pl.next().title, "T1")
    check("prev", pl.previous().title, "T0")
    pl.set_current_index(4)
    check("eof repeat off stops", pl.next(auto=True), None)
    pl.set_repeat_mode(REPEAT_ALL)
    check("repeat all wraps", pl.next(auto=True).title, "T0")
    pl.set_repeat_mode(REPEAT_ONE)
    pl.set_current_index(2)
    check("repeat one same", pl.next(auto=True).title, "T2")

    # 随机播放：本轮应覆盖「除当前曲外的全部」，一轮恰好 n-1 首。
    pl2 = Playlist()
    pl2.set_tracks([TrackItem(title=f"S{i}", source_id=f"q{i}") for i in range(5)], autoplay_index=0)
    pl2.set_repeat_mode(REPEAT_ALL)
    pl2.set_shuffle(True)
    seen = set()
    for _ in range(4):
        tt = pl2.next(auto=True)
        if tt:
            seen.add(tt.title)
    check("shuffle covers others", seen, {"S1", "S2", "S3", "S4"})

    # 队列编辑。
    pl3 = Playlist()
    pl3.set_tracks([TrackItem(title=f"X{i}", source_id=f"x{i}") for i in range(4)], 0)
    pl3.move_after_current(3)
    check("move after current", pl3.tracks()[1].title, "X3")
    check("move keeps current", pl3.current_track().title, "X0")


def main() -> int:
    test_quality()
    test_quality_constants()
    test_track_badge()
    test_config()
    test_stores()
    test_playlist()
    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}):", _FAILS)
        return 1
    print("ALL BOUNDARY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
