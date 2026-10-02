"""Subsonic 通用客户端 Provider。

设计原则（合规）：
- 本模块只实现 Subsonic 协议【客户端】，不含任何音乐平台解析逻辑；
- 所有音乐内容均由用户自备的 Subsonic 服务端提供；
- 用户自行填写服务端地址与账号。

支持的接口：
  【Subsonic 标准】ping / search3 / getSong / stream / getCoverArt /
                    getLyrics / getPlaylists / getPlaylist / getStarred /
                    star / unstar / getAlbumList2 / getArtists / scrobble
  【私有扩展】（非 Subsonic 标准，供 Fenda-Player 增强体验，其他客户端可忽略）
                    getAvatar / getUserProfile / qrLogin / qrLoginStatus

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
#: 客户端标识
CLIENT_NAME = "fenda"


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
        """全部歌单（原始 subsonic-response）。"""
        return self._request("getPlaylists")

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
            cover_id = str(p.get("coverArt", "") or "")
            result.append(PlaylistInfo(
                id=pid,
                name=str(p.get("name", "") or "未命名歌单"),
                cover_url=self.cover_art_url(cover_id) if cover_id else "",
                song_count=int(p.get("songCount", 0) or 0),
                source="subsonic",
                description=str(p.get("comment", "") or ""),
            ))
        return result

    def get_playlist(self, playlist_id: str) -> dict:
        """单个歌单详情（含歌曲）。"""
        return self._request("getPlaylist", [("id", playlist_id)])

    def playlist_tracks(self, playlist_id: str) -> List[TrackItem]:
        """歌单详情 → TrackItem 列表（带音质信息）。

        注意：不用 get_playlist_tracks 之名——基类已有同名的无参方法
        （返回默认播放列表），子类覆盖会因签名不符而报错。
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
                    # coverArt 非 Subsonic 标准字段；
                    # 标准服务端不返回（前端占位），
                    # 支持的后端（含扩展的国内流媒体中转）可返回封面 id/URL。
                    cover_id = str(a.get("coverArt", "") or "")
                    cover_url = self.cover_art_url(cover_id) if cover_id else ""
                    out.append({
                        "id": aid,
                        "name": str(a.get("name", "") or ""),
                        "album_count": int(a.get("albumCount", 0) or 0),
                        "cover_url": cover_url,
                    })
        return out

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

    # ---- URL 构造（交给播放器/图片用）----
    def stream_url(self, song_id: str, max_bit_rate: int = 0,
                   fmt: str = "") -> str:
        """音频流 URL。

        max_bit_rate=0 表示不限制（原文件直传，支持无损/Range seek）。
        fmt 可指定转码格式（如 mp3），留空=不转码。
        """
        extra = [("id", song_id)]
        if max_bit_rate:
            extra.append(("maxBitRate", str(max_bit_rate)))
        if fmt:
            extra.append(("format", fmt))
        return self._url("stream", extra)

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

    # ============================================================
    # 数据转换：Subsonic 结构 → TrackItem
    # ============================================================
    def _song_to_track(self, song: dict) -> TrackItem:
        """Subsonic child（歌曲）→ TrackItem（带音质信息）。"""
        sid = str(song.get("id", ""))
        dur = float(song.get("duration", 0) or 0)
        bitrate = int(song.get("bitRate", 0) or 0) * 1000  # kbps → bps
        cover_id = str(song.get("coverArt", "") or sid)
        # 技术参数（OpenSubsonic 提供；普通 Subsonic 可能缺）
        sample_rate = int(song.get("samplingRate", 0) or 0)
        bit_depth = int(song.get("bitDepth", 0) or 0)
        channels = int(song.get("channelCount", 0) or 0)
        return TrackItem(
            title=str(song.get("title", "Unknown") or "Unknown"),
            artist=str(song.get("artist", "Unknown") or "Unknown"),
            album=str(song.get("album", "") or ""),
            duration=TrackItem.format_seconds(dur),
            duration_seconds=dur,
            filepath="",
            source_type="subsonic",
            source_id=sid,
            stream_url=self.stream_url(sid),
            cover_url=self.cover_art_url(cover_id),
            bitrate=bitrate,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=channels,
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
        if not self.is_configured():
            self.emit("search-finished", query, [])
            return

        def _work():
            return self.search(query)

        def _done(result):
            self._tracks = list(result)
            self.emit("search-finished", query, result)

        def _err(exc):
            log.warning("Subsonic 搜索失败: %s", exc)
            self.emit("error", f"在线搜索失败: {exc}")
            self.emit("search-finished", query, [])

        run_async(work=_work, on_done=_done, on_error=_err)

    def get_library(self) -> List[TrackItem]:
        return list(self._tracks)

    def get_playlist_tracks(self) -> List[TrackItem]:
        return list(self._tracks)

    def refresh(self) -> None:
        """Subsonic 无「扫描」概念，空实现。"""
        return
