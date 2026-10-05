"""Subsonic 通用客户端 Provider。

设计原则（合规）：
- 本模块只实现 Subsonic 协议【客户端】，不含任何音乐平台解析逻辑；
- 所有音乐内容均由用户自备的 Subsonic 服务端提供；
- 用户自行填写服务端地址与账号。

支持的接口：
  【Subsonic 标准】ping / search3 / getSong / stream / getCoverArt /
                    getLyrics / getPlaylists / getPlaylist / getStarred /
                    star / unstar / getAlbumList2 / getArtists / scrobble
  【可选扩展端点】（非 Subsonic 标准；本模块只提供通用调用入口，
                    不含任何具体平台的解析逻辑。服务端未实现时调用失败，
                    调用方据此隐藏对应入口即可）
                    getAvatar / getUserProfile / qrLogin / qrLoginStatus

合规边界：
- 本 Provider 是通用 Subsonic 客户端，不对接、不内置任何国内音乐平台；
- 若用户希望接入某平台，应由用户自行提供符合 Subsonic 协议的第三方
  服务端（如自建网关），本软件仅作为标准协议客户端与之通信；
- 本模块不实现：登录态伪造、Cookie 注入、绕过版权/DRM、平台私有接口解析。

认证：Subsonic 标准 u/t/s/v/c/f 参数（token=md5(password+salt)），
      同时支持明文 p=（兼容部分服务端）。
返回：统一请求 f=json，解析 subsonic-response。
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from config.settings import get_config
from core.tasks import run_async
from models import TrackItem

from .base import BaseMusicProvider

log = logging.getLogger(__name__)

#: Subsonic API 版本
API_VERSION = "1.16.1"
#: 客户端标识（中性名，避免与任何第三方聚合客户端关联）。
CLIENT_NAME = "xiatiao"


@dataclass
class PlaylistInfo:
    """平台无关的歌单信息。

    后端无论对接哪个平台（Subsonic 标准 / QQ音乐 / 汽水音乐 / ...），
    只要把歌单统一映射成这个结构，前端即可渲染。

    字段：
      id:          歌单唯一标识（前端用于请求详情）
      name:        歌单名
      cover_url:   封面图片 URL（可为空）
      song_count:  歌曲数（0=未知）
      source:      来源平台标识（如 "subsonic"/"qq"/"soda"/...），用于显示标签
      description: 歌单描述（可选）
    """
    id: str
    name: str
    cover_url: str = ""
    song_count: int = 0
    source: str = ""
    description: str = ""


@dataclass
class UserProfile:
    """平台无关的用户资料（扩展端点 getUserProfile 返回）。"""
    username: str = ""
    nickname: str = ""
    avatar_url: str = ""
    is_vip: bool = False
    source: str = ""


class SubsonicError(Exception):
    """Subsonic 请求/解析错误。"""


#: 运行时音质档位（内存，不持久化）。母带/Hi-Res 只设这里，
#: 当前会话生效；重启后回落到配置档位（最高无损）。
_RUNTIME_QUALITY = ""


def set_runtime_quality(key: str) -> None:
    """设置运行时音质档位（不写配置）。空串 = 回落配置。"""
    global _RUNTIME_QUALITY
    _RUNTIME_QUALITY = str(key or "")


#: 当前后端类型标识（来自 ping 的 type 字段；连接时探测一次）。
#: 标准 Subsonic 服务端返回 navidrome / subsonic / airsonic 等；
#: 私有协议后端返回自定义标识（如 xiatiao-api）。
_BACKEND_TYPE = ""

#: 私有协议后端的 type 白名单。只有明确列在这里的 type 才算「私有后端」
#: （支持音质档位）；其它一律视为「非私有」（不显示档位）——
#: 这样未知的标准 Subsonic 服务端也不会被误判为私有。
_PRIVATE_BACKEND_TYPES = frozenset({
    "xiatiao-api",
})


def set_backend_type(t: str) -> None:
    """记录后端类型（连接/探测时调用）。"""
    global _BACKEND_TYPE
    _BACKEND_TYPE = str(t or "").strip()


def get_backend_type() -> str:
    """当前后端类型标识（空 = 未探测）。"""
    return _BACKEND_TYPE


def is_private_backend() -> bool:
    """当前后端是否为「私有协议后端」（相对标准 Subsonic）。

    判据（白名单，单一真相，基于 ping 的 type 字段）：
      - type 在 _PRIVATE_BACKEND_TYPES 白名单里 → 私有（支持音质档位）；
      - 其它一切（标准 Subsonic、未知服务端、type 为空）→ 非私有。

    用白名单而非黑名单：只有明确认识的后端才启用私有增强，
    避免未知的标准服务端被误判。与曲目无关；所有分支都应调本函数。
    """
    t = _BACKEND_TYPE.strip().lower()
    return bool(t) and t in _PRIVATE_BACKEND_TYPES


class SubsonicProvider(BaseMusicProvider):
    """通用 Subsonic 客户端。"""

    __gtype_name__ = "SubsonicProvider"

    source_type = "subsonic"
    display_name = "Subsonic"
    capabilities = {
        "library", "playlists", "search", "favourite", "lyrics", "recommendations",
    }

    def __init__(self) -> None:
        super().__init__()
        self._tracks: List[TrackItem] = []

    # ============================================================
    # 配置
    # ============================================================
    @staticmethod
    def _cfg() -> dict:
        c = get_config()
        return {
            "url": c.get_str("subsonic_url", "").rstrip("/"),
            "user": c.get_str("subsonic_user", ""),
            "password": c.get_str("subsonic_password", ""),
            "use_token": c.get_bool("subsonic_use_token", True),
            "extra_params": c.get_str("subsonic_extra_params", ""),
        }

    @staticmethod
    def is_configured() -> bool:
        c = SubsonicProvider._cfg()
        return bool(c["url"] and c["user"])

    # ============================================================
    # 底层请求
    # ============================================================
    def _auth_params(self) -> List[tuple]:
        """构造 Subsonic 认证参数（标准 u/t/s/v/c/f）。"""
        c = self._cfg()
        user = c["user"]
        pwd = c["password"]
        params = [("u", user), ("v", API_VERSION), ("c", CLIENT_NAME), ("f", "json")]
        if c["use_token"] and pwd:
            salt = secrets.token_hex(8)
            token = hashlib.md5((pwd + salt).encode("utf-8")).hexdigest()
            params.append(("t", token))
            params.append(("s", salt))
        elif pwd:
            # 兼容支持明文密码的服务端
            params.append(("p", pwd))
        # 用户自定义额外参数（如某些服务端的特殊开关）
        if c["extra_params"]:
            for pair in c["extra_params"].split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params.append((k.strip(), v.strip()))
        return params

    def _url(self, endpoint: str, extra: Optional[list] = None) -> str:
        """构造完整请求 URL。

        endpoint: Subsonic 端点名（不带 .view）
        extra: 额外查询参数 [(k, v), ...]
        返回形如 {url}/rest/{endpoint}.view?u=...&t=...&...
        注意：Subsonic 传统是 /rest/{endpoint}.view，部分现代服务
              （如 Navidrome）同时支持不带 .view。这里用 .view。
        """
        base = self._cfg()["url"]
        params = list(self._auth_params())
        if extra:
            params.extend(extra)
        return base + "/rest/" + endpoint + ".view?" + urllib.parse.urlencode(params)

    def _request(self, endpoint: str, extra: Optional[list] = None,
                 timeout: float = 15.0) -> dict:
        """发起请求并返回 subsonic-response 内容（dict）。

        成功返回 response 内部的 dict；服务端返回 status=failed 时抛 SubsonicError。
        """
        url = self._url(endpoint, extra)
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise SubsonicError(f"响应不是合法 JSON: {exc}") from exc
        body = data.get("subsonic-response")
        if not isinstance(body, dict):
            raise SubsonicError("缺少 subsonic-response 字段")
        if body.get("status") == "failed":
            err = body.get("error") or {}
            raise SubsonicError(f"服务端错误: {err.get('message', err)}")
        return body

    # ============================================================
    # Subsonic 标准端点
    # ============================================================
    def ping(self) -> dict:
        """连通性 + 服务端信息。"""
        return self._request("ping")

    def get_song(self, song_id: str) -> dict:
        """单曲信息。"""
        return self._request("getSong", [("id", song_id)])

    def search3(self, query: str, song_count: int = 50,
                song_offset: int = 0, artist_count: int = 0,
                album_count: int = 0) -> dict:
        """搜索（Subsonic 1.16+ 的 search3，返回 artist/album/song）。"""
        return self._request("search3", [
            ("query", query),
            ("songCount", str(song_count)),
            ("songOffset", str(song_offset)),
            ("artistCount", str(artist_count)),
            ("albumCount", str(album_count)),
        ])

    def get_starred(self) -> dict:
        """收藏（starred 歌曲/专辑/艺术家）。"""
        return self._request("getStarred2")

    def star(self, song_id: str) -> dict:
        return self._request("star", [("id", song_id)])

    def unstar(self, song_id: str) -> dict:
        return self._request("unstar", [("id", song_id)])

    def get_playlists(self) -> dict:
        """全部歌单（原始 subsonic-response）。

        超时放宽（同 PLAYLIST_TIMEOUT）：某些兼容服务端在后台刷新时
        getPlaylists 可能从 <1s 波动到十几秒；统一的 15s 会偶发超时，
        导致在线页歌单区块变空。放宽到 60s 覆盖该波动。
        标准 Subsonic（Navidrome 等）此请求通常很快，不受影响。
        """
        return self._request("getPlaylists", timeout=self.PLAYLIST_TIMEOUT)

    def get_playlists_info(self) -> List[PlaylistInfo]:
        """全部歌单 → PlaylistInfo 列表（平台无关）。

        兼容两种结构：
          playlists.playlist = [{...}]  （有歌单）
          playlists = {}                （无歌单）
        每个歌单字段：id / name / songCount / coverArt / comment(描述)。
        """
        body = self.get_playlists()
        node = body.get("playlists")
        raw = []
        if isinstance(node, dict):
            pl = node.get("playlist")
            if isinstance(pl, list):
                raw = pl
            elif isinstance(pl, dict):
                raw = [pl]
        result = []
        for p in raw:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id", ""))
            if not pid:
                continue
            # 封面：优先 coverArt；为空则回退用歌单 id（与歌曲卡片一致的兜底策略）。
            cover_id = str(p.get("coverArt", "") or "") or pid
            result.append(PlaylistInfo(
                id=pid,
                name=str(p.get("name", "") or "未命名歌单"),
                cover_url=self.cover_art_url(cover_id),
                song_count=int(p.get("songCount", 0) or 0),
                source="subsonic",
                description=str(p.get("comment", "") or ""),
            ))
        return result

    #: getPlaylist 的超时（秒）。
    #
    # 大歌单（数千首）服务端需一次性序列化全部条目，耗时可达十几秒；
    # 用统一的 15s 会对超大歌单超时 → 详情页误显示为空。故单独放宽。
    # 标准 Subsonic 服务端（Navidrome 等）此请求通常很快，不受影响。
    PLAYLIST_TIMEOUT = 60.0

    def get_playlist(self, playlist_id: str) -> dict:
        """单个歌单详情（含歌曲）。

        使用放宽后的超时（见 PLAYLIST_TIMEOUT），避免超大歌单超时失败。
        """
        return self._request("getPlaylist", [("id", playlist_id)],
                             timeout=self.PLAYLIST_TIMEOUT)

    def playlist_tracks(self, playlist_id: str) -> List[TrackItem]:
        """歌单详情 → TrackItem 列表（带音质信息）。失败抛异常（不吞）。

        注意：不用 get_playlist_tracks 之名——基类已有同名的无参方法
        （返回默认播放列表），子类覆盖会因签名不符而报错。

        异常语义：加载失败（网络/超时/服务端错误）会**向上抛**，
        由调用方决定提示；避免"失败即当空歌单"导致用户误以为歌单为空。
        """
        body = self.get_playlist(playlist_id)
        node = body.get("playlist")
        if not isinstance(node, dict):
            return []
        entries = node.get("entry")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return []
        return [self._song_to_track(s) for s in entries if isinstance(s, dict)]

    def playlist_tracks_safe(self, playlist_id: str) -> tuple:
        """歌单详情，返回 (tracks, error)。

        error 为 None 表示成功（tracks 可能为空 = 真空歌单）；
        error 为 str 表示加载失败（网络/超时/服务端错误）。
        调用方据此区分「空歌单」与「加载失败」，给出正确提示。
        """
        try:
            return (self.playlist_tracks(playlist_id), None)
        except Exception as exc:
            return ([], str(exc) or exc.__class__.__name__)

    def all_artists(self) -> list:
        """艺术家列表 → [{"id", "name", "album_count"}, ...]。"""
        if not self.is_configured():
            return []
        body = self.get_artists()
        node = body.get("artists") or {}
        idx = node.get("index")
        if isinstance(idx, dict):
            idx = [idx]
        out = []
        for one in (idx or []):
            arts = one.get("artist") if isinstance(one, dict) else None
            if isinstance(arts, dict):
                arts = [arts]
            for a in (arts or []):
                if not isinstance(a, dict):
                    continue
                aid = str(a.get("id", ""))
                if aid:
                    # 艺术家封面来源（按可靠性排序）：
                    #   1) artistImageUrl —— Navidrome 直接给完整图片 URL（可含 token）
                    #   2) coverArt —— 取封面 id，再拼 getCoverArt URL
                    # 两者都无 → 空串（前端占位；该艺术家在服务端本就无图）。
                    _img = str(a.get("artistImageUrl", "") or "")
                    if _img:
                        cover_url = _img
                    else:
                        cover_id = str(a.get("coverArt", "") or "")
                        cover_url = self.cover_art_url(cover_id) if cover_id else ""
                    out.append({
                        "id": aid,
                        "name": str(a.get("name", "") or ""),
                        "album_count": int(a.get("albumCount", 0) or 0),
                        "cover_url": cover_url,
                    })
        # 无封面艺术家：用其「第一首歌封面」兜底（主流客户端做法）。
        # 代价：每个要 getArtist → getAlbum 两次请求，故只补前 _ARTIST_FALLBACK_MAX
        # 个（艺术家区块本来就只显示前 30），避免艺术家多时拖慢加载。
        _missing = [x for x in out if not x.get("cover_url")][:self._ARTIST_FALLBACK_MAX]
        for x in _missing:
            try:
                url = self._artist_first_song_cover(x["id"])
                if url:
                    x["cover_url"] = url
            except Exception:
                continue
        return out

    #: 无封面艺术家补图的最大数量（避免大量额外请求拖慢加载）。
    _ARTIST_FALLBACK_MAX = 30

    def _artist_first_song_cover(self, artist_id: str) -> str:
        """取艺术家「第一首歌」的封面 URL（艺术家无图时的兜底）。

        路径：getArtist（专辑列表）→ 首张 getAlbum（歌曲列表）→ 首曲 coverArt。
        失败返回空串。
        """
        try:
            body = self._request("getArtist", [("id", artist_id)])
        except Exception:
            return ""
        node = body.get("artist") or {}
        albums = node.get("album")
        if isinstance(albums, dict):
            albums = [albums]
        if not isinstance(albums, list) or not albums:
            return ""
        aid = str((albums[0] or {}).get("id", "") or "")
        if not aid:
            return ""
        try:
            abody = self._request("getAlbum", [("id", aid)])
        except Exception:
            return ""
        anode = abody.get("album") or {}
        songs = anode.get("song")
        if isinstance(songs, dict):
            songs = [songs]
        if not isinstance(songs, list) or not songs:
            return ""
        s0 = songs[0] or {}
        cover_id = str(s0.get("coverArt", "") or s0.get("id", "") or "")
        return self.cover_art_url(cover_id) if cover_id else ""

    def artist_tracks(self, artist_id: str) -> List[TrackItem]:
        """艺术家的歌曲（getArtist → 各专辑的歌曲）。"""
        if not self.is_configured():
            return []
        body = self._request("getArtist", [("id", artist_id)])
        node = body.get("artist") or {}
        albums = node.get("album")
        if isinstance(albums, dict):
            albums = [albums]
        tracks = []
        for alb in (albums or []):
            if not isinstance(alb, dict):
                continue
            aid = str(alb.get("id", ""))
            if aid:
                try:
                    tracks.extend(self.album_tracks(aid))
                except Exception:
                    pass
        return tracks

    def random_songs(self, size: int = 50) -> List[TrackItem]:
        """随机歌曲。"""
        if not self.is_configured():
            return []
        body = self._request("getRandomSongs", [("size", str(size))])
        songs = (body.get("randomSongs") or {}).get("song")
        if isinstance(songs, dict):
            songs = [songs]
        if not isinstance(songs, list):
            songs = []
        return [self._song_to_track(s) for s in songs if isinstance(s, dict)]

    def album_tracks(self, album_id: str) -> List[TrackItem]:
        """专辑的歌曲列表（getAlbum）。"""
        if not self.is_configured():
            return []
        body = self._request("getAlbum", [("id", album_id)])
        node = body.get("album") or {}
        songs = node.get("song")
        if isinstance(songs, dict):
            songs = [songs]
        if not isinstance(songs, list):
            songs = []
        return [self._song_to_track(s) for s in songs if isinstance(s, dict)]

    def all_songs(self, offset: int = 0, count: int = 100) -> List[TrackItem]:
        """分页列出歌曲（用于在线曲库浏览）。

        实现：search3 空查询（Navidrome 等把空查询当"列出"）。
        注意：这不是 Subsonic 标准行为，服务端若返回空则结果为空。
        返回 TrackItem 列表（已转换），供 UI 直接使用。
        """
        if not self.is_configured():
            return []
        body = self.search3("", song_count=count, song_offset=offset)
        songs = self._extract_songs(body, "searchResult3", "searchResult2", "searchResult")
        return [self._song_to_track(s) for s in songs if isinstance(s, dict)]

    def get_album_list2(self, list_type: str = "alphabeticalByArtist",
                        size: int = 50, offset: int = 0) -> dict:
        """专辑列表。list_type: newest/alphabeticalByArtist/random/..."""
        return self._request("getAlbumList2", [
            ("type", list_type), ("size", str(size)), ("offset", str(offset)),
        ])

    def get_artists(self) -> dict:
        """艺术家列表。"""
        return self._request("getArtists")

    def scrobble(self, song_id: str, submission: bool = True) -> dict:
        """上报播放（用于服务端统计）。"""
        return self._request("scrobble", [
            ("id", song_id), ("submission", "true" if submission else "false"),
        ])

    def get_lyrics(self, artist: str = "", title: str = "") -> dict:
        """歌词（传统 getLyrics，按 artist+title）。"""
        p = []
        if artist:
            p.append(("artist", artist))
        if title:
            p.append(("title", title))
        return self._request("getLyrics", p)

    def get_lyrics_by_song_id(self, song_id: str) -> dict:
        """结构化歌词（Subsonic 1.16.1+ getLyricsBySongId）。"""
        return self._request("getLyricsBySongId", [("id", song_id)])

    # ---- 下载辅助（同时兼容标准 Subsonic）----
    def download_url(self, song_id: str, quality: str = "") -> str:
        """按指定音质档位构造下载 URL（面向私有协议后端）。

        - 私有后端：识别 maxBitRate 档位语义（128/320/999/1400/2000）
          与 quality 参数（master/hires…），精确命中。
        - 标准 Subsonic：不按档位转码——调用方应改用
          stream_url(song_id, max_bit_rate=0) 原文件直传，不要走本方法。
        - quality 为空 → 用配置/运行时档位（沿用 stream_url 默认行为）。
        """
        from core.quality import quality_to_bitrate
        q = str(quality or "").strip().lower()
        if not q:
            return self.stream_url(song_id)
        br = quality_to_bitrate(q)
        extra = [("id", song_id), ("quality", q)]
        if br:
            extra.append(("maxBitRate", str(br)))
        return self._url("stream", extra)

    def best_lyrics_lrc(self, song_id: str, artist: str = "",
                        title: str = "") -> str:
        """尽力取得 LRC 文本歌词，失败返回空串（不抛异常）。

        顺序：
          1) getLyricsBySongId（OpenSubsonic 结构化）→ LRC；
          2) 传统 getLyrics(artist,title) → 纯文本。
        标准 Subsonic 未必支持 1，回退 2；都失败则空串（下载照常）。
        """
        from core.downloader import structured_lyrics_to_lrc
        # 1) 结构化
        try:
            body = self.get_lyrics_by_song_id(song_id)
            lrc = structured_lyrics_to_lrc(body)
            if lrc.strip():
                return lrc
        except Exception:
            pass
        # 2) 传统端点（标准 Subsonic）
        try:
            body = self.get_lyrics(artist=artist, title=title)
            lrc = structured_lyrics_to_lrc(body)
            if lrc.strip():
                return lrc
        except Exception:
            pass
        return ""

    # ---- URL 构造（交给播放器/图片用）----
    def stream_url(self, song_id: str, max_bit_rate: int | None = None,
                   fmt: str = "") -> str:
        """音频流 URL。

        max_bit_rate=None（默认）：读用户配置/运行时音质档位
            —— 私有后端用档位语义（999/1400/2000）。
        max_bit_rate=0：不限制，原文件直传
            —— 标准 Subsonic 用（有什么歌播什么歌，不按档位转码）。
        max_bit_rate>0：指定码率。
        fmt 可指定转码格式（如 mp3），留空=不转码。
        """
        if max_bit_rate is None:
            try:
                max_bit_rate = self._configured_quality_br()
            except Exception:
                max_bit_rate = 0
        extra = [("id", song_id)]
        if max_bit_rate:
            extra.append(("maxBitRate", str(max_bit_rate)))
        if fmt:
            extra.append(("format", fmt))
        return self._url("stream", extra)

    @staticmethod
    def _configured_quality_br() -> int:
        """把当前音质档位映射成 maxBitRate 值（后端按此值返回对应音质）。

        档位 → maxBitRate 约定值（后端需对齐）：
          standard 标准 128 / high 高品 320 / lossless 无损 999 /
          hires Hi-Res 1400 / master 母带 2000。
        0 表示不限制（原文件直传）。

        优先用「运行时档位」（母带/Hi-Res 临时试听，不持久化）；
        无运行时档位时读配置（持久化，最高无损）。
        """
        from core.quality import DEFAULT_ONLINE_QUALITY, quality_to_bitrate
        q = _RUNTIME_QUALITY or None
        if not q:
            try:
                from config.settings import get_config
                q = get_config().get_str("online_quality", DEFAULT_ONLINE_QUALITY)
            except Exception:
                q = DEFAULT_ONLINE_QUALITY
        return quality_to_bitrate(str(q or ""))

    def cover_art_url(self, cover_id: str, size: int = 0) -> str:
        """封面 URL（cover_id 通常是专辑/歌曲 id）。"""
        extra = [("id", cover_id)]
        if size:
            extra.append(("size", str(size)))
        return self._url("getCoverArt", extra)

    # ============================================================
    # 私有扩展端点（非 Subsonic 标准；其他客户端可忽略）
    # 路径约定：/rest/{name}.view（与标准端点同构）
    # ============================================================
    def get_avatar_url(self, username: str = "") -> str:
        """用户头像 URL（扩展）。

        后端约定：返回图片（image/*），或 302 跳转到图片。
        参数 username 可选（默认当前登录用户）。
        """
        extra = []
        if username:
            extra.append(("username", username))
        return self._url("getAvatar", extra)

    def get_user(self, username: str = "") -> dict:
        """标准 getUser：用户名 + 角色权限（Navidrome 等支持）。

        返回 { "user": { "username":..., "adminRole":..., ... } }。
        """
        extra = []
        if not username:
            try:
                username = self._cfg()["user"]
            except Exception:
                username = ""
        if username:
            extra.append(("username", username))
        return self._request("getUser", extra)

    def get_user_profile(self, username: str = "") -> dict:
        """用户资料（扩展）：昵称、头像、会员状态等。

        约定返回 JSON（subsonic-response 包裹）：
          { "userProfile": { "username":..., "nickname":...,
                             "avatarUrl":..., "isVip":..., "source":... } }
        """
        extra = []
        if username:
            extra.append(("username", username))
        return self._request("getUserProfile", extra)

    def qr_login_create(self, source: str) -> dict:
        """创建扫码登录会话（扩展）。

        source: 平台标识（由后端定义，如 "netease"/"qq"/"qq_wx"/"bilibili"）。
        约定返回：
          { "qrLogin": { "source":..., "key":...,
                         "url":...,        # 二维码内容（文本），可选
                         "imageUrl":...,    # 二维码图片地址，可选
                         "expiresAt":... } }
        """
        return self._request("qrLogin", [("action", "create"), ("source", source)])

    def qr_login_status(self, source: str, key: str) -> dict:
        """轮询扫码状态（扩展）。

        约定返回：
          { "qrLoginStatus": { "status": "waiting|scanned|confirmed|expired|failed",
                               "message":..., "cookie":...(成功后) } }
        """
        return self._request("qrLogin", [
            ("action", "status"), ("source", source), ("key", key),
        ])

    # ---- 推荐内容扩展（非标准；未实现的后端返回失败 → 前端隐藏入口）----
    def get_recommendations(self, source: str = "qq", rec_type: str = "daily") -> dict:
        """推荐内容（私有扩展 getRecommendations，见 PLAYER_EXTENSION.md）。

        参数：source（音源 qq/netease/...）、type（daily/guess/rank/radio/categories）。
        返回整个 recommendations 对象（含 sections 分组）；未实现 / 出错 → {}。
        """
        try:
            body = self._request("getRecommendations", [
                ("source", source), ("type", rec_type),
            ])
        except Exception:
            return {}
        node = body.get("recommendations")
        return node if isinstance(node, dict) else {}

    def recommendation_sections(self, source: str = "qq", rec_type: str = "daily") -> list:
        """取推荐分组列表（sections 数组）。未实现 → []。

        每项：{id, title, kind(playlist|songs), playlistId, coverArt, children}。
        """
        rec = self.get_recommendations(source, rec_type)
        secs = rec.get("sections") if isinstance(rec, dict) else None
        return secs if isinstance(secs, list) else []

    def recommendation_section_cards(self, source: str, rec_type: str) -> list:
        """推荐分组 → 卡片 dict 列表（name/subtitle/cover_url/click）。

        kind=playlist：点击后用 playlistId 调 getPlaylist 取歌（虚拟歌单，
        后端动态返回内容）。未实现 → []，前端隐藏区块。
        """
        out = []
        for sec in self.recommendation_sections(source, rec_type):
            if not isinstance(sec, dict):
                continue
            pid = str(sec.get("playlistId", "") or "")
            title = str(sec.get("title", "") or "")
            if not pid and not title:
                continue
            cover = str(sec.get("coverArt", "") or "")
            out.append({
                "name": title,
                "subtitle": str(sec.get("id", "") or ""),
                "cover_url": self.cover_art_url(cover) if cover else "",
                "playlist_id": pid,
            })
        return out

    # ============================================================
    # 数据转换：Subsonic 结构 → TrackItem
    # ============================================================
    def _song_to_track(self, song: dict) -> TrackItem:
        """Subsonic child（歌曲）→ TrackItem（带音质信息）。"""
        sid = str(song.get("id", ""))
        dur = float(song.get("duration", 0) or 0)
        bitrate = int(song.get("bitRate", 0) or 0) * 1000  # kbps → bps
        cover_id = str(song.get("coverArt", "") or sid)
        # 该歌支持的音质档位（后端扩展字段 qualityLevels）；无 → None，前端回退默认五档
        qlevels = song.get("qualityLevels")
        if not isinstance(qlevels, list):
            qlevels = None
        # 技术参数（OpenSubsonic 提供；普通 Subsonic 可能缺）
        sample_rate = int(song.get("samplingRate", 0) or 0)
        bit_depth = int(song.get("bitDepth", 0) or 0)
        channels = int(song.get("channelCount", 0) or 0)
        # 真实音频格式：Navidrome/OpenSubsonic 返回 suffix（flac/dsf/dff/mp3…）。
        # 在线流的 stream_url 扩展名是 `view`，无法判格式，故用 suffix。
        suffix = str(song.get("suffix", "") or "").strip().lower()
        # 私有协议后端才按音质档位请求；标准 Subsonic 不按档位转码
        # ——有什么歌播什么歌（maxBitRate=0 直传）。
        # 注意：判据用「全局后端类型标志」（连接时 ping 探测），
        # 不能只看本曲的 qualityLevels——私有后端未必逐曲返回该字段。
        _surl = (self.stream_url(sid) if is_private_backend()
                 else self.stream_url(sid, max_bit_rate=0))
        return TrackItem(
            title=str(song.get("title", "Unknown") or "Unknown"),
            artist=str(song.get("artist", "Unknown") or "Unknown"),
            album=str(song.get("album", "") or ""),
            duration=TrackItem.format_seconds(dur),
            duration_seconds=dur,
            filepath="",
            source_type="subsonic",
            source_id=sid,
            stream_url=_surl,
            cover_url=self.cover_art_url(cover_id),
            bitrate=bitrate,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=channels,
            quality_levels=qlevels,
            format_hint=suffix,
        )

    @staticmethod
    def _extract_songs(body: dict, *keys: str) -> list:
        """从 subsonic-response 里提取歌曲列表（兼容 searchResult3/playlist/...）。"""
        for k in keys:
            node = body.get(k)
            if isinstance(node, dict):
                songs = node.get("song")
                if isinstance(songs, list):
                    return songs
                if isinstance(songs, dict):
                    return [songs]
        return []

    # ============================================================
    # BaseMusicProvider 接口
    # ============================================================
    def search(self, query: str) -> List[TrackItem]:
        """同步搜索（内部用；UI 走 search_async）。"""
        if not self.is_configured():
            return []
        body = self.search3(query, song_count=50)
        songs = self._extract_songs(body, "searchResult3", "searchResult2", "searchResult")
        return [self._song_to_track(s) for s in songs]

    def search_async(self, query: str) -> None:
        log.info("[search_async] 进入 query=%r configured=%s", query, self.is_configured())
        if not self.is_configured():
            self.emit("search-finished", query, [])
            return

        def _work():
            log.info("[search_async] 后台请求 query=%r", query)
            r = self.search(query)
            log.info("[search_async] 后台完成 query=%r 结果数=%d", query, len(r))
            return r

        def _done(result):
            log.info("[search_async] emit search-finished query=%r 数=%d", query, len(result))
            self._tracks = list(result)
            self.emit("search-finished", query, result)

        def _err(exc):
            log.warning("Subsonic 搜索失败: %s", exc)
            self.emit("error", f"在线搜索失败: {exc}")
            self.emit("search-finished", query, [])

        run_async(work=_work, on_done=_done, on_error=_err)

    def search_page(self, query: str, offset: int = 0,
                    count: int = 50) -> List[TrackItem]:
        """搜索某一页（支持翻页）；返回 TrackItem 列表。

        与 search() 的区别：可指定 offset，用于「滚动加载更多」。
        """
        if not self.is_configured():
            return []
        body = self.search3(query, song_count=count, song_offset=offset)
        songs = self._extract_songs(
            body, "searchResult3", "searchResult2", "searchResult")
        return [self._song_to_track(s) for s in songs]

    def get_library(self) -> List[TrackItem]:
        return list(self._tracks)

    def get_playlist_tracks(self) -> List[TrackItem]:
        return list(self._tracks)

    def refresh(self) -> None:
        """Subsonic 无「扫描」概念，空实现。"""
        return

    # ============================================================
    # 统一内容拉取接口（BaseMusicProvider 可选实现）
    # ============================================================
    #
    # 这些方法把「标准 Subsonic 端点」适配成平台无关的中间结构，
    # 使 UI 无需区分后端是标准 Subsonic 还是用户自备的兼容服务端。
    # 纯转发：内部仍走既有标准端点，行为与之前完全一致。

    def fetch_playlists(self):
        """歌单列表（PlaylistInfo）；未配置/出错返回空列表。"""
        if not self.is_configured():
            return []
        try:
            return self.get_playlists_info()
        except Exception as exc:
            log.debug("获取歌单列表失败: %s", exc)
            return []

    def fetch_library_page(self, offset: int = 0, count: int = 100):
        """在线曲库分页（TrackItem）；未配置/出错返回空列表。

        走标准 search3 空查询（部分服务端将其视为「列出全部」）。
        """
        try:
            return self.all_songs(offset=offset, count=count)
        except Exception as exc:
            log.debug("获取在线曲库分页失败: %s", exc)
            return []

    def fetch_stream_url(self, source_id: str, quality: str = "") -> str:
        """按 song id 构造流 URL（通用档位）。空 id 返回空串。"""
        if not source_id:
            return ""
        try:
            if quality:
                return self.download_url(source_id, quality=quality)
            return self.stream_url(source_id)
        except Exception as exc:
            log.debug("构造在线流 URL 失败: %s", exc)
            return ""

    def fetch_recommendations(self, source: str = "", rec_type: str = "daily"):
        """推荐内容（可选能力，统一入口）。

        转发到既有扩展端点 getRecommendations。
        - 标准 Subsonic 未实现该端点 → _request 抛错 → 内部捕获返回 []，
          UI 据此隐藏推荐区块（优雅降级）。
        - 用户自备的兼容服务端 / 外挂插件实现该端点后，自动可用。

        返回 recommendation_section_cards 风格的卡片列表
        （name/subtitle/cover_url/playlist_id），供 UI 直接渲染区块。

        合规：此处只调用「通用扩展端点」，不含任何具体平台的接口解析逻辑；
        是否返回内容完全取决于服务端/插件自身。
        """
        try:
            if not self.is_configured():
                return []
            # source 为空时用中性默认（仅为端点参数，不代表任何平台）。
            src = str(source or "").strip()
            return self.recommendation_section_cards(src, rec_type)
        except Exception as exc:
            log.debug("获取推荐失败: %s", exc)
            return []

    def fetch_user_profile(self, username: str = ""):
        """用户资料（可选能力，统一入口）。未实现/失败返回 {}。

        转发到扩展端点 getUserProfile；标准 Subsonic 未实现则返回 {}。
        """
        try:
            if not self.is_configured():
                return {}
            body = self.get_user_profile(username)
            node = body.get("userProfile") if isinstance(body, dict) else None
            return node if isinstance(node, dict) else {}
        except Exception as exc:
            log.debug("获取用户资料失败: %s", exc)
            return {}

    def fetch_avatar_url(self, username: str = "") -> str:
        """用户头像 URL（可选能力，统一入口）。未配置返回空串。"""
        try:
            if not self.is_configured():
                return ""
            return self.get_avatar_url(username)
        except Exception as exc:
            log.debug("获取头像 URL 失败: %s", exc)
            return ""

