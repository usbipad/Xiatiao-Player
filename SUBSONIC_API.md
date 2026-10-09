# Subsonic 接口使用指南（后端开发契约）

本文档是虾条播放器在线音源模块与后端服务的接口契约。

播放器是通用 Subsonic 客户端：标准部分遵循 Subsonic API，任何合规服务端都能连；扩展部分是私有端点（头像、扫码登录、用户资料），其他客户端会忽略。

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
播放器用它判断在线音源是否连通、显示服务端信息。

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

**创建**：`GET /rest/qrLogin.view` 参数 `action=create`、`source`（平台标识，如 netease/qq/bilibili）。
返回 `qrLogin` 对象，字段：`source`、`key`、`url`（二维码内容文本）、`imageUrl`（可选，现成图片 http 链接或 data:image base64）、`expiresAt`。
`url` 与 `imageUrl` 至少一个。

**轮询**：`GET /rest/qrLogin.view` 参数 `action=status`、`source`、`key`。
返回 `qrLoginStatus` 对象，字段：`status`、`message`、`cookie`（成功时）。
`status` 取值：`waiting` / `scanned` / `confirmed`（应带 cookie）/ `expired` / `failed`。

### 推荐内容扩展 getRecommendations

标准 Subsonic **没有**「排行榜 / 每日推荐 / 私人电台 / 猜你喜欢 / 歌单分类」的概念，
因此这类内容只能走私有扩展。播放器的在线主页**固定预留**了对应区块，
但区块只在「后端返回非空数据」时显示；未实现的后端返回失败 → **自动隐藏**，
不影响合规与其它客户端。

> **降级约定（重要）**：前端对此端点一律「失败即隐藏」——
> 端点不存在、返回 `status=failed`、或返回空 sections，都视为「本后端不支持该功能」，
> 静默隐藏对应区块，绝不报错、不占位。

#### 端点

`GET /rest/getRecommendations.view`

参数：

- `source`：音源，`qq` / `netease` / `kugou` / `kuwo` / `migu`
- `type`：推荐类型

`type` 取值：

- `daily`：每日推荐
- `guess`：猜你喜欢
- `rank`：排行榜
- `radio`：私人电台
- `categories`：歌单分类

#### 返回结构

`subsonic-response` 内 `recommendations` 对象：

- `source`：音源
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

## 架构边界原则（后端与插件解耦）

后端是**通用 Subsonic 网关**，不是某个音乐平台的客户端。它只负责：

1. 加载插件（通用机制）；
2. 调用插件的通用接口（如解析直链）；
3. 把结果规范化成 Subsonic 协议返回。

**后端代码中不得出现任何具体音源的业务知识**，包括但不限于：

- 平台名分支（`qq` / `netease` / `kugou` …）；
- 平台特定的 HTTP 头（各家的 `Referer` / `User-Agent` 规则）；
- 平台特定的 ID 字段（`songmid` / `hash` / `copyrightId` …）；
- 平台名到插件平台码的映射（`qq→tx` …）。

这些平台适配知识属于**插件**或**显式适配层**，不散落在业务逻辑里。

> **现状**：后端存在两套并存的音源命名体系——music-lib 名（`qq`/`netease`/…，
> 用于榜单/登录/账号/浏览）与插件平台码（`tx`/`wy`/…，用于解析直链）。
> 二者之间的翻译已**集中**到 `source_adapter.go`（唯一翻译点，纯函数、可单测），
> 业务代码不再散落映射。该适配层的存在是合理的：它是两个既有体系之间的桥，
> 而非平台业务知识泄漏。新增平台只改适配层一处。

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
私有扩展端点（头像、扫码登录、推荐类：榜单/每日推荐/私人FM）。

---

## 后端待改清单（配合播放器界面改造）

> 以下各项是**后端（QQ 音乐私有实现）**需要修改/新增的部分。
> 播放器前端已完成兼容；后端按此改完后，可被任意标准 Subsonic 客户端正常使用。

### 1. 歌词格式规范化（P0，已发现 bug）

**问题**：`getLyricsBySongId` 返回的歌词带有 LRC 原始标签（`[00:00.00]`、
`[ti:]`、`[ar:]`、`[offset:0]`），且同一句因多时间标签/逐字平铺而重复多次。

**改法**：服务端解析 LRC，输出干净结构——

- 剥掉所有时间标签与元信息标签，`line[].value` 只放纯正文；
- 同一句合并为**一个** `line` 项（去掉重复）；
- `line[].start` 用**毫秒**整数；
- 翻译歌词放**独立的** `structuredLyrics` 项，用 `lang` 区分。

详见上文「歌词」章节的「标准返回格式」与「常见错误」。

### 2. 排行榜区块（P1，走 getRecommendations）

播放器在线主页已预留「排行榜」区块，数据源为**私有扩展**
`getRecommendations(type=rank)`（契约见上文「推荐内容扩展 getRecommendations」）。
返回 `sections` 分组；点分组卡片用 `playlistId` 调 `getPlaylist` 取歌。
不实现则区块自动隐藏。

### 3. 每日推荐区块（P1，走 getRecommendations）

播放器已预留「每日推荐」区块，数据源为**私有扩展**
`getRecommendations(type=daily)`。同上，不实现则自动隐藏。

### 4. 推荐内容（getRecommendations，已并入上文契约）

排行榜 / 每日推荐 / 私人电台 / 猜你喜欢 / 歌单分类，统一走 `getRecommendations`。
详见「推荐内容扩展 getRecommendations」章节。

### 5. 在线歌技术字段（P1，用于显示格式/采样率/码率）

**问题**：播放器左侧面板会显示音频技术信息（如 `FLAC · 96kHz · 24bit · 立体声 · 999kbps`）。
本地歌有这些数据，但**在线歌的 `song` 节点未返回技术字段**，导致在线歌这一行几乎为空。

**改法**：后端在 `song` 节点返回以下字段（标准 Subsonic + OpenSubsonic 扩展）：

| 字段 | 含义 | 示例 |
|------|------|------|
| `suffix` | 音频格式 | `flac` / `mp3` / `m4a` |
| `bitRate` | 码率（kbps） | `999` |
| `samplingRate` | 采样率（Hz） | `96000` |
| `bitDepth` | 位深 | `24` |
| `channelCount` | 声道数 | `2` |

播放器**有多少显示多少**，缺的字段自动跳过；建议尽量返回 `suffix`、`bitRate`、
`samplingRate`、`bitDepth`。

> 注：这些字段还同时用于「歌曲信息」弹窗与列表；返回后各处一并受益。

### 6. 音质选择对接（私有增强）

播放器面板提供音质档位选择（歌名旁徽章）。档位与 `stream` 请求的
`maxBitRate` 参数一一对应：

| 档位 key | 显示名 | `maxBitRate` |
|----------|--------|--------------|
| `standard` | 标准 | `128` |
| `high` | 高品 | `320` |
| `lossless` | 无损 | `999` |
| `hires` | Hi-Res | `1400` |
| `master` | 母带 | `2000` |

例：`stream?id=xxx&maxBitRate=999` → 请求无损音质流。
不带 `maxBitRate` 时后端用默认档位（无损 flac）。

#### 6.1 职责边界（重要）

音质这条链路上，**没有一方能独自知道「实际拿到了什么」**，因此按能力划分职责：

| 角色 | 知道什么 | 职责 |
|------|----------|------|
| 前端 | 用户选的档位；解码器实测参数 | 传 `maxBitRate`；用实测参数判定实际档位 |
| 后端 | 回退链停在哪一档 | 按档位解析、逐级降级；**不返回实际档位** |
| 插件 | 平台能力 | 按档位解析直链，失败则返回空 |

**为什么后端不返回实际档位**：插件接口只返回一个 URL 字符串，不含格式/码率；
后端无法在不实际拉流的前提下预知音质。强行返回只会是猜测。

#### 6.2 后端行为：惰性解析 + 自动降级

后端收到某档位后，**惰性解析**：从该档位往下逐档、逐插件
「解析 → 立即代理」，**成功即停**（不预先解析整条链）。若某档拿不到，
自动降级到下一档（母带 → Hi-Res → 无损 → 高品 → 标准），
保证只要有声就一定返回。

> 惰性解析的意义：避免「为取一个档位而解析整条链」的浪费
> （旧实现解析 5 档 × N 插件，即便只用 1 个，白花数秒）。

前端无需处理失败，也**不需要 `qualityLevels`**。

> **注意**：实际拿到的音质取决于插件能力与档位映射，可能与所选不符
> （插件可能「降级」或「超发」）。前端**以播放器实测为准**显示实际档位。

#### 6.3 前端行为：实测判定实际档位

在线播放走 ffmpeg 解码，播放内核通过 `audio-info` 事件上报 ffprobe 的
**实测参数**（`codec` / `bitrate` / `sample_rate` / `bit_depth`）。
这些是对真实音频流的事实观测，比任何猜测都准确。

前端据此判定实际档位（见 `core/quality.py`）：

- **有损**（`mp3`/`aac`/…）：按 `bitrate` 分 `standard` / `high`
- **无损**（`flac`/`wav`/…）：按 `bit_depth`、`sample_rate` 分
  `lossless` / `hires` / `master`

徽章随后显示**实测档位**；若低于所选，提示「已降级」。
判定失败时回退显示所选档位，不报错。

> 实测参考（同一首 QQ 歌，`ffprobe` 对在线流可直接读出）：
> `maxBitRate=320` → `mp3 / 320000bps`；`maxBitRate=2000` → `flac / 24bit / 192kHz`。

### 7. 推荐卡片封面（P1，已发现缺失）

**问题**：`getRecommendations` 返回的 `sections` 项**没有 `coverArt` 字段**；
且 `getPlaylist` 返回的 `playlist.coverArt` **也为空**。导致「排行榜」「每日推荐」的卡片
无封面（灰色占位）。

实测：`sections[].coverArt` 缺失；`getPlaylist` 的 `playlist.coverArt` = null；
但歌曲 `entry[].coverArt` **有值**。

**改法**（二选一即可）：

1. 在 `getRecommendations` 的 `sections[].coverArt` 返回歌单封面 id（推荐，卡片直接有图）；
2. 或在 `getPlaylist` 的 `playlist.coverArt` 返回歌单封面 id。

`coverArt` 的值用标准 `getCoverArt` 能取到的格式（与歌曲 `coverArt` 同构即可）。
QQ 侧歌单有封面图，取到填入即可。

> 播放器已兼容「有就用、没有就占位」，后端补上即显示，无需前端改动。
