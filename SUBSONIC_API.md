# Subsonic 接口使用指南（后端开发契约）

本文档是虾条播放器在线播放模块与后端服务的接口契约。

播放器是通用 Subsonic 客户端：标准部分遵循 Subsonic API，任何兼容服务端都能连；扩展部分是私有端点（头像、扫码登录、用户资料），其他客户端会忽略。

---

## 认证

所有请求走 Subsonic 标准认证参数：

| 参数 | 含义 |
|------|------|
| `u` | 用户名 |
| `t` | 令牌 = md5(密码 + salt) |
| `s` | 随机 salt |
| `p` | 明文密码（与 t/s 二选一） |
| `v` | API 版本，固定 `1.16.1` |
| `c` | 客户端标识，固定 `xiatiao` |
| `f` | 返回格式，固定 `json` |

播放器默认用 `t`/`s` 令牌认证；设置里可关闭，改用明文 `p`。

---

## 请求约定

- 基地址由用户在设置填写，如 `http://127.0.0.1:4533`。
- 端点格式：`基地址/rest/端点.view?参数`。
- 返回统一 `f=json`，顶层为 `subsonic-response`，成功 `status=ok`，失败 `status=failed` 并带 `error`。
- 建议同时接受**不带 `.view` 后缀**的路径（部分现代服务端如此）。

---

## 标准端点（必须实现）

### ping — 连通性

`GET /rest/ping.view`

返回顶层附加字段：`type`（服务端类型，如 navidrome）、`serverVersion`、`version`（API 版本）、`openSubsonic`。
播放器用它判断在线服务是否连通、显示服务端信息。

### search3 — 搜索

`GET /rest/search3.view`

参数：`query`、`songCount`（默认 50）、`songOffset`、`artistCount`、`albumCount`。

返回 `searchResult3.song` 数组。歌曲字段映射见文末「歌曲字段映射」。

> **非标准行为（重要）**：播放器的「在线曲库」用 `search3` **空查询**（`query=`）来分页列出全部歌曲，依赖服务端把空查询当作「列出」。Navidrome 支持此行为；若你的服务端返回空，则曲库浏览为空。

### stream — 音频流（播放核心）

`GET /rest/stream.view`

参数：`id`、`maxBitRate`（0 = 不限制）、`format`（可选，指定转码格式）。

关键要求：

1. **必须支持 HTTP Range**（返回 206 + Content-Range），否则拖动进度条异常。
2. `maxBitRate=0` 表示不限制，原文件直传，支持无损。
3. 返回头含 `Content-Type`（如 audio/flac）和 `Content-Length`。

播放器提供音质档位选择，与 `maxBitRate` 取值一一对应：

| 档位 key | 显示名 | `maxBitRate` |
|----------|--------|--------------|
| `standard` | 标准 | `128` |
| `high` | 高品 | `320` |
| `lossless` | 无损 | `999` |
| `hires` | Hi-Res | `1400` |
| `master` | 母带 | `2000` |

例：`stream?id=xxx&maxBitRate=999` → 请求无损音质流。

> 服务端按自身能力响应；实际音质可能与所选档位不同（如服务端只能提供较低档）。
> 播放器以解码器实测参数判定并显示实际档位，无需服务端返回实际音质。

### getCoverArt — 封面

`GET /rest/getCoverArt.view`

参数：`id`、`size`（可选，像素）。返回图片二进制。

### getSong — 单曲信息

`GET /rest/getSong.view`

参数：`id`，返回 `song` 对象。

### getAlbum — 专辑详情

`GET /rest/getAlbum.view`

参数：`id`，返回 `album` 对象，其中 `album.song` 为歌曲数组。

### getArtist — 艺术家详情

`GET /rest/getArtist.view`

参数：`id`，返回 `artist` 对象，其中 `artist.album` 为专辑数组（播放器再逐张取 `getAlbum` 拿歌曲）。

### getArtists — 艺术家列表

`GET /rest/getArtists.view`

返回 `artists.index[].artist[]`，每个 artist 字段：`id`、`name`、`albumCount`，**可选** `coverArt`（见「扩展字段」）。

### getAlbumList2 — 专辑列表

`GET /rest/getAlbumList2.view`

参数：`type`（如 `newest` / `alphabeticalByArtist` / `random`）、`size`、`offset`。
返回 `albumList2.album` 数组。

### getRandomSongs — 随机歌曲

`GET /rest/getRandomSongs.view`

参数：`size`，返回 `randomSongs.song` 数组。

### getStarred2 — 收藏

`GET /rest/getStarred2.view`

返回 `starred2.song` 数组（播放器「收藏」页用）。

### star / unstar — 收藏 / 取消收藏

`GET /rest/star.view` / `GET /rest/unstar.view`，参数 `id`。

### getPlaylists — 歌单列表

`GET /rest/getPlaylists.view`

返回 `playlists.playlist` 数组，每个歌单字段：`id`、`name`、`songCount`、`coverArt`、`comment`（描述）。

### getPlaylist — 歌单详情

`GET /rest/getPlaylist.view`

参数：`id`，返回 `playlist.entry` 数组（歌曲）。

### 歌词

- `GET /rest/getLyrics.view` 参数 `artist`、`title`（传统按名查，返回纯文本 `lyrics.value`）。
- `GET /rest/getLyricsBySongId.view` 参数 `id`，返回结构化歌词（**推荐**）。

播放器优先用 `getLyricsBySongId`（结构化），降级 `getLyrics`。

#### getLyricsBySongId 标准返回格式（务必遵守）

返回 `lyricsList.structuredLyrics` 数组，每项含 `lang`、`synced`、`line`。
`line` 是数组，每项含 `value`（纯正文）和 `start`（**毫秒**整数）。

示例：`start` 为 13010 表示 13.01 秒；`value` 为纯歌词正文，不带任何标签。

字段要求：

| 字段 | 要求 |
|------|------|
| `line[].value` | **纯正文**，不含任何 `[mm:ss.xx]` 时间标签、`[ti:]`/`[ar:]`/`[offset:]` 元信息标签 |
| `line[].start` | **毫秒**整数（OpenSubsonic 规定），如 13010 = 13.01 秒 |
| `synced` | `true` 表示有时间轴；纯文本歌词用 `false` |
| 多语言 | 每种语言一个 `structuredLyrics` 项，用 `lang` 区分（如 `zh`/`en`），**不要把翻译混进同一条** |

#### 常见错误（会导致歌词显示异常）

1. **把 LRC 原文塞进 `value`**：如 `"[00:13.01]第一句"`。
   播放器虽做了兼容解析，但**其它标准客户端会显示错乱**。服务端应自行解析成纯正文 + 毫秒 `start`。
2. **`start` 用秒而非毫秒**：如 `13.01`。标准要求毫秒，用秒会导致时间轴错位。
3. **同一句打多个时间标签 / 逐字平铺**：如 `[00:13.01][00:13.67]同一句...`。
   解析后会整句重复多行，应合并为一项。
4. **元信息标签未剥离**：`[ti:]`、`[ar:]`、`[offset:0]` 等应丢弃，不要作为歌词行返回。

> **服务端应输出「已解析、无标签、无重复、毫秒时间」的干净结构**，
> 这样任意标准 Subsonic 客户端都能正确显示；播放器的兼容逻辑仅作保底。

### scrobble（可选） — 播放上报

`GET /rest/scrobble.view` 参数 `id`、`submission=true`。用于服务端统计。

### getUser — 用户信息（标准）

`GET /rest/getUser.view` 参数 `username`（默认当前登录用户）。

返回 `user` 对象，字段：`username`、以及一组 `*Role` 布尔（`adminRole`、`downloadRole`、`uploadRole`、`playlistRole`、`coverArtRole`、`streamRole`、`shareRole`、`jukeboxRole` 等）、`scrobblingEnabled`。
播放器用它显示账号角色权限（个人中心卡片）。Navidrome 支持。

---

## 私有扩展端点（可选实现）

以下为非 Subsonic 标准，供播放器增强。后端可实现可不实现（播放器会降级）。
路径同样用 `/rest/名字.view`。

### getUserProfile — 用户资料

`GET /rest/getUserProfile.view` 参数 `username`（可选）。

返回 `subsonic-response` 内 `userProfile` 对象，字段：`username`、`nickname`、`avatarUrl`、`isVip`、`source`。
播放器用于显示头像和昵称。

> 注：标准 Navidrome 不支持此端点，播放器会降级到标准 `getUser` + `getAvatar`。

### getAvatar — 用户头像

`GET /rest/getAvatar.view` 参数 `username`（可选），直接返回图片二进制，或 302 跳转到图片 URL。

> 注：Navidrome 标准也支持 `getAvatar`。

### qrLogin — 扫码登录

**创建**：`GET /rest/qrLogin.view` 参数 `action=create`、`source`（平台标识，由后端定义）。
返回 `qrLogin` 对象，字段：`source`、`key`、`url`（二维码内容文本）、`imageUrl`（可选，现成图片 http 链接或 data:image base64）、`expiresAt`。
`url` 与 `imageUrl` 至少一个。

**轮询**：`GET /rest/qrLogin.view` 参数 `action=status`、`source`、`key`。
返回 `qrLoginStatus` 对象，字段：`status`、`message`、`cookie`（成功时）。
`status` 取值：`waiting` / `scanned` / `confirmed`（应带 cookie）/ `expired` / `failed`。

### 推荐内容扩展 getRecommendations

标准 Subsonic 没有「排行榜 / 每日推荐 / 私人电台 / 猜你喜欢 / 歌单分类」的概念，
这类内容走私有扩展。播放器的在线主页预留了对应区块，**仅在**后端返回非空数据时显示；
端点不存在、返回 `status=failed` 或空 sections 时，静默隐藏对应区块，不报错、不占位。
后端可按需实现，未实现不影响其它功能。

#### 端点

`GET /rest/getRecommendations.view`

参数：

- `source`：平台标识，由后端定义；留空则由后端选择默认来源
- `type`：推荐类型

`type` 取值：

- `daily`：每日推荐
- `guess`：猜你喜欢
- `rank`：排行榜
- `radio`：私人电台
- `categories`：歌单分类

#### 返回结构

`subsonic-response` 内 `recommendations` 对象：

- `source`：平台标识
- `type`：类型
- `sections`：分组数组，每项含：
  - `id`：分组标识
  - `title`：标题
  - `kind`：`playlist` 或 `songs`
  - `playlistId`：当 `kind=playlist`，用它调 `getPlaylist` 取歌
  - `coverArt`：可选封面
  - `children`：可选，子项（分类用），每项含 `id` / `title` / `playlistId`

**关键**：`playlistId` 是**虚拟歌单 id**，播放器不解析，原样回传给 `getPlaylist` 即可。
后端动态返回内容。

取歌曲：`GET /rest/getPlaylist.view?id=<playlistId>`，返回标准 `playlist.entry`。

#### 播放器用法

- 「排行榜」区块 → `getRecommendations(type=rank)`
- 「每日推荐」区块 → `getRecommendations(type=daily)`
- 点分组卡片 → `getPlaylist(playlistId)` → 播放

#### 能力探测

连上后调一次 `getRecommendations`，返回 `ok` 就显示推荐入口，`failed` 就隐藏。

---

## 扩展字段（可选）

后端可在标准端点的返回里**附加字段**，供播放器增强显示；播放器遇缺失时降级。

- **`getArtists` 的 artist 节点**可附加 `coverArt`（艺术家封面 id）。标准 Subsonic 无此字段；支持的实现可返回，让艺术家列表显示头像/封面。
- **歌曲节点**可附加 OpenSubsonic 技术参数：`samplingRate`、`bitDepth`、`channelCount`（见下）。

---

## 歌曲字段映射

歌曲对象（`song`）→ 播放器 `TrackItem`：

| Subsonic 字段 | TrackItem 字段 | 说明 |
|---|---|---|
| `id` | `source_id` | 歌曲唯一标识 |
| `title` | `title` | 曲名 |
| `artist` | `artist` | 歌手 |
| `album` | `album` | 专辑 |
| `duration` | `duration_seconds` | 秒（浮点） |
| `coverArt` | （封面 URL） | 缺省时回退用 `id` |
| `bitRate` | `bitrate` | kbps → 播放器存 bps（×1000） |
| `samplingRate` | `sample_rate` | OpenSubsonic，可选 |
| `bitDepth` | `bit_depth` | OpenSubsonic，可选 |
| `channelCount` | `channels` | OpenSubsonic，可选 |

---

## 兼容性建议

- **`.view` 后缀**：播放器请求 `/rest/端点.view`；后端建议同时接受不带后缀。
- **JSON 优先**：播放器只用 `f=json`；建议后端同时支持 `f=xml` 以兼容其他客户端。
- **Range 支持**：`stream` 端点务必支持 Range。
- **无损直传**：`maxBitRate=0` 时尽量原文件直传。
- **错误码**：遵循 Subsonic 标准（40 = 认证失败，70 = 未找到）。

---

## 最小实现清单

要让播放器核心可用，后端至少实现：

1. `ping`（连通检测）
2. `search3`（搜索 + 曲库列出）
3. `stream`（播放，**必须支持 Range**）
4. `getCoverArt`（封面）
5. `getAlbum` / `getArtist` / `getArtists` / `getAlbumList2`（专辑、艺术家浏览）
6. `getPlaylists` / `getPlaylist`（歌单）
7. `getLyricsBySongId` 或 `getLyrics`（歌词）

可选增强：`getStarred2` / `star` / `unstar`（收藏）、`getRandomSongs`（随机）、
`getSongsByGenre` / `getGenres`（流派）、`getTopSongs`（艺术家热门）、
`getSimilarSongs2`（相似推荐）、`getUser`（账号角色）、
私有扩展端点（头像、扫码登录、推荐类：榜单/每日推荐/私人电台）。

