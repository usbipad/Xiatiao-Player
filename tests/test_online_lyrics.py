"""在线歌词加载与去重单元测试（无网络，用假 provider）。

覆盖 services/online_lyrics.py：
  - 连续去重（不跨时间全局去重）；
  - 结构化歌词：start 毫秒归一；
  - value 自带 LRC 标签 → 走 parse_lrc_text；
  - 回退传统 getLyrics；
  - provider/字段缺失 → 空列表不崩。

运行：python3 tests/test_online_lyrics.py
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
    def __init__(self, sid="1", source_type="subsonic", artist="A", title="T"):
        self.source_id = sid
        self.source_type = source_type
        self.artist = artist
        self.title = title


class _FakeProvider:
    def __init__(self, structured=None, legacy=None):
        self._structured = structured
        self._legacy = legacy

    def get_lyrics_by_song_id(self, sid):
        return self._structured or {}

    def get_lyrics(self, artist="", title=""):
        return self._legacy or {}


def test_dedupe() -> None:
    from services.online_lyrics import dedupe_lyrics
    # 连续重复合并
    items = [(0.0, "a"), (1.0, "a"), (2.0, "b"), (3.0, "b"), (4.0, "c")]
    out = dedupe_lyrics(items)
    check("连续去重", [t for _, t in out] == ["a", "b", "c"])
    # 不跨时间全局去重（副歌重复保留）
    chorus = [(0.0, "x"), (1.0, "y"), (2.0, "x"), (3.0, "y"), (4.0, "x")]
    out2 = dedupe_lyrics(chorus)
    check("不跨时间全局去重", len(out2) == 5)
    check("空输入", dedupe_lyrics([]) == [])


def test_structured_ms() -> None:
    from services.online_lyrics import load_online_lyrics
    # start 为毫秒：6120ms -> 6.12s
    prov = _FakeProvider(structured={
        "lyricsList": {"structuredLyrics": [
            {"line": [{"value": "line1", "start": 0},
                      {"value": "line2", "start": 6120}]}
        ]}
    })
    out = load_online_lyrics(_FakeTrack(), prov)
    check("结构化条数", len(out) == 2)
    check("毫秒归一 6120->6.12", abs(out[1][0] - 6.12) < 1e-6, str(out))


def test_value_with_lrc_tag() -> None:
    from services.online_lyrics import load_online_lyrics
    # value 自带 [mm:ss] → 交给 parse_lrc_text
    prov = _FakeProvider(structured={
        "lyricsList": {"structuredLyrics": [
            {"line": [{"value": "[00:01.00]hello"},
                      {"value": "[00:02.50]world"}]}
        ]}
    })
    out = load_online_lyrics(_FakeTrack(), prov)
    check("LRC 标签解析条数", len(out) == 2, str(out))
    if len(out) == 2:
        check("LRC 时间轴", abs(out[0][0] - 1.0) < 1e-6 and abs(out[1][0] - 2.5) < 1e-6, str(out))


def test_legacy_fallback() -> None:
    from services.online_lyrics import load_online_lyrics
    prov = _FakeProvider(structured={}, legacy={"lyrics": "[00:03.00]fallback"})
    out = load_online_lyrics(_FakeTrack(), prov)
    check("回退 getLyrics", len(out) == 1 and abs(out[0][0] - 3.0) < 1e-6, str(out))


def test_edge_cases() -> None:
    from services.online_lyrics import load_online_lyrics
    check("provider None", load_online_lyrics(_FakeTrack(), None) == [])
    check("非 subsonic", load_online_lyrics(_FakeTrack(source_type="local"), _FakeProvider()) == [])
    check("无 source_id", load_online_lyrics(_FakeTrack(sid=""), _FakeProvider()) == [])
    check("空 provider", load_online_lyrics(_FakeTrack(), _FakeProvider()) == [])


def main() -> int:
    test_dedupe()
    test_structured_ms()
    test_value_with_lrc_tag()
    test_legacy_fallback()
    test_edge_cases()
    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 -> {_FAILS}")
        return 1
    print("ONLINE LYRICS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
