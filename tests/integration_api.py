"""播放器 ↔ Subsonic API 服务端 联动测试（真实网络，可指定地址）。

覆盖三类：
  1) 端点功能：播放器实际调用的每个端点能否正常返回；
  2) 时序/负载：慢端点是否在超时内、负载下是否仍可用；
  3) 错误路径：坏参数/不存在的 id 不崩、不挂起。

用法：
    # 默认读播放器配置的 subsonic_url；不可达则整脚本跳过（退出码 0）
    python3 tests/integration_api.py
    # 显式指定
    XIATIAO_TEST_API=http://127.0.0.1:4535 \
    XIATIAO_TEST_USER=admin XIATIAO_TEST_PASS=admin123 \
        python3 tests/integration_api.py

退出码：0=通过/跳过；1=有失败。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), name, detail)
    if not ok:
        _FAILS.append(name)


def _make_provider():
    """优先用环境变量覆盖的地址/账号，否则用播放器配置。"""
    env_url = os.environ.get("XIATIAO_TEST_API")
    env_user = os.environ.get("XIATIAO_TEST_USER")
    env_pass = os.environ.get("XIATIAO_TEST_PASS")
    if env_url or env_user:
        from config.settings import get_config
        c = get_config()
        if env_url:
            c.set_str("subsonic_url", env_url)
        if env_user:
            c.set_str("subsonic_user", env_user)
        if env_pass is not None:
            c.set_str("subsonic_password", env_pass)
            c.set_bool("subsonic_use_token", False)
    from providers.subsonic import SubsonicProvider
    return SubsonicProvider()


def _reachable(p) -> bool:
    try:
        return p.ping().get("status") == "ok"
    except Exception:
        return False


def test_endpoints(p) -> None:
    print("=== 1. 端点功能 ===")
    ck("ping", p.ping().get("status") == "ok")
    ck("is_configured", p.is_configured())
    s = p.search("周杰伦")
    ck("search3", isinstance(s, list) and len(s) > 0, f"{len(s)}")
    ck("search(空)", isinstance(p.search(""), list))
    ck("search_page", isinstance(p.search_page("周杰伦", 0, 10), list))
    pls = p.fetch_playlists()
    ck("getPlaylists", isinstance(pls, list) and len(pls) > 0, f"{len(pls)}")
    ck("getArtists", isinstance(p.all_artists(), list))
    ck("getRandomSongs", isinstance(p.random_songs(5), list))
    ck("getAlbumList2", isinstance(p.get_album_list2("newest", size=5), dict))
    ck("getUserProfile", isinstance(p.get_user_profile(), dict))
    ck("getRecommendations", isinstance(p.recommendation_sections("", "daily"), list))
    if s:
        t = s[0]
        ck("cover_art_url", "getCoverArt" in p.cover_art_url("x"))
        ck("best_lyrics_lrc", isinstance(p.best_lyrics_lrc(t.source_id, t.artist, t.title), str))


def test_timing(p) -> None:
    print("=== 2. 时序/负载 ===")
    # 播放器对 getPlaylists / getPlaylist 用 PLAYLIST_TIMEOUT（已放宽）。
    pls = p.fetch_playlists()
    # 大歌单在放宽超时内应成功。
    if pls:
        big = max(pls, key=lambda x: x.song_count)
        t0 = time.time()
        tracks, err = p.playlist_tracks_safe(big.id)
        dt = time.time() - t0
        ck(f"大歌单({big.song_count}) 在超时内加载", not err and len(tracks) > 0,
           f"{dt:.1f}s")
    # 负载下 getPlaylists 仍可用。
    stop = False

    def load():
        lp = _make_provider()
        while not stop:
            try:
                lp.search("a")
            except Exception:
                pass
    ths = [threading.Thread(target=load, daemon=True) for _ in range(4)]
    for t in ths:
        t.start()
    time.sleep(0.8)
    ok = 0
    for _ in range(5):
        try:
            if len(p.fetch_playlists()) > 0:
                ok += 1
        except Exception:
            pass
    stop = True
    ck("负载下 getPlaylists 可用", ok >= 4, f"{ok}/5")


def test_errors(p) -> None:
    print("=== 3. 错误路径 ===")
    # 坏 id 应抛错或返回错误标记，但绝不挂起（< 超时）。
    for name, fn in (
        ("getSong(坏id)", lambda: p.get_song("no_such_id_xyz")),
        ("getPlaylist(坏id)", lambda: p.get_playlist("no_such_pl_xyz")),
    ):
        t0 = time.time()
        try:
            fn()
        except Exception:
            pass
        dt = time.time() - t0
        ck(f"{name} 不挂起", dt < 10, f"{dt:.1f}s")
    tracks, err = p.playlist_tracks_safe("no_such_pl_xyz")
    ck("playlist_tracks_safe 坏id 返回 err", err is not None)


def main() -> int:
    p = _make_provider()
    if not _reachable(p):
        print("SKIP: Subsonic API 不可达（未配置或未启动），跳过联动测试。")
        return 0
    test_endpoints(p)
    test_timing(p)
    test_errors(p)
    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}):", _FAILS)
        return 1
    print("INTEGRATION TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
