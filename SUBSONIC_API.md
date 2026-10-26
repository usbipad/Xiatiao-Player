# Subsonic 接口契约（播放器 ↔ 服务端）

本文档是虾条播放器在线播放模块与服务端之间的接口契约，**以两侧实际实现为准**：

- 客户端：`providers/subsonic.py`、`ui/window.py`、`core/quality.py`
- 服务端参考实现：`Xiatiao-SubsonicAPI/subsonic-adapter-go`（下称「适配器」）

播放器是通用 Subsonic 客户端：标准部分遵循 Subsonic API，任何兼容服务端都能连；
扩展部分是私有端点（推荐内容、用户资料、音质档位），**服务端不实现时播放器自动降级**，
不报错、不阻塞基本播放。

标记约定：**✅** 适配器已实现 ｜ **⚠️** 实现但与标准/期望有差异 ｜ **❌** 未实现

---

## 0. 端点支持矩阵

| 端点 | 播放器用途 | 适配器 | 备注 |
|---|---|:---:|---|
| `ping` | 连通检测 + 后端类型探测 | ✅ | 决定是否启用音质档位 |
| `search3` | 搜索 + 在线曲库分页 | ⚠️ | 空 query 返回空；单页上限 20 |
| `stream` | 播放（必须支持 Range） | ✅ | 代理上游，透传 Range |
| `getCoverArt` | 封面 | ⚠️ | 302 跳外链，不直接返回字节 |
| `getSong` | 单曲信息 | ✅ | |
| `getAlbum` | 专辑详情（含曲目） | ✅ | |
| `getArtist` | 艺术家详情 | ⚠️ | 桩实现，无专辑列表 |
| `getArtists` | 艺术家列表 | ⚠️ | 固定返回空索引 → 区块隐藏 |
| `getAlbumList2` | 专辑列表（最新/随机区块） | ⚠️ | 忽略 `type`/`offset`，上限 20 |
| `getRandomSongs` | 随机歌曲区块 | ⚠️ | 非随机（固定关键词） |
| `getStarred2` | 在线收藏（小红心/收藏镜像） | ✅ | 上限 100 首 |
| `star` / `unstar` | 收藏 / 取消收藏 | ✅ | 需上游数字 id |
| `getPlaylists` | 歌单列表 | ✅ | |
| `getPlaylist` | 歌单详情 + 虚拟歌单 | ✅ | 支持 `exclude` |
| `getLyricsBySongId` | 结构化歌词（首选） | ✅ | |
| `getLyrics` | 传统歌词（回退） | ⚠️ | 恒返回空 |
| `getUser` | 账号角色 | ✅ | 固定角色集 |
| `getUserProfile` | 昵称 / 头像 URL（扩展） | ✅ | |
| `getAvatar` | 头像图片（扩展） | ✅ | 302 跳外链 |
| `getRecommendations` | 排行榜 / 每日推荐（扩展） | ✅ | |
| `scrobble` | 播放上报（可选） | ✅ | 空实现 |
| `qrLogin` | 扫码登录（扩展） | ❌ | 播放器侧亦无 UI 入口 |

适配器未识别的端点统一返回 `status=failed`、`error.code=0`、
`message="unsupported endpoint: <名字>"`。

---

## 1. 连接与认证

### 1.1 基地址

用户在设置里填写**完整地址**（含 `http://` 或 `https://`），客户端只做去尾斜杠处理，
例如 `http://127.0.0.1:4535`。

适配器默认监听 `0.0.0.0`、端口 `4535`（`HOST` / `PORT` 可覆盖，被占用时自动顺延），
启动日志会打印可直接填进播放器的地址，并提示「用户名、密码任意」——
但**不能留空**（见 1.4）。

### 1.2 请求形态

客户端**固定**使用带 `.view` 的路径：

    {基地址}/rest/{端点名}.view?{认证参数}&{业务参数}

适配器的 `normalizeEndpoint` 会剥离 `.view`，因此带不带后缀都接受；
但客户端从不尝试无后缀形式，服务端若**只**接受无后缀则无法连接。

所有请求均为 `GET`，返回 `f=json`。

### 1.3 认证参数

客户端**始终**发送下列参数（顺序即实际拼接顺序）：

| 参数 | 值 | 说明 |
|---|---|---|
| `u` | 用户名 | 必填（设置为空则视为未配置） |
| `v` | `1.16.1` | API 版本 |
| `c` | `xiatiao` | 客户端标识 |
| `f` | `json` | 固定 |
| `t` `s` | 令牌 / 盐 | 勾选「使用令牌认证」且密码非空时发送 |
| `p` | 明文密码 | 关闭令牌认证且密码非空时发送 |

令牌算法：

```
salt  = 16 位十六进制随机串（secrets.token_hex(8)）
token = md5(password + salt) 的十六进制小写串
```

### 1.4 密码为空会导致所有请求失败（重要）

客户端在**密码为空时不发送任何认证参数**（既无 `t`/`s` 也无 `p`）。
而适配器的 `hasAuthParams` 要求「有 `u` 且（`t`+`s` 或 `p` 存在）」，否则直接返回：

```json
{"subsonic-response":{"status":"failed","error":{"code":40,"message":"authentication required"}}}
```

因此接适配器时**密码必须填一个非空值**（内容任意）。
标准服务端通常要求真实凭据，同理会失败。

### 1.5 自定义额外参数

配置项 `subsonic_extra_params`（形如 `k1=v1&k2=v2`，仅配置文件、无 UI）会被
**追加**在认证参数之后。它可能产生重复键（例如再传一次 `v`）：
服务端应容忍重复参数（适配器按 `url.Values` 取第一个值）。

### 1.6 适配器的鉴权范围

适配器**只校验认证参数的形态，不校验口令内容**（用户名、密码任意）。
管理页（`/admin`）有独立的会话鉴权（bcrypt + HMAC Cookie），与 `/rest/*` 完全分离，
不影响播放器连接。

---

## 2. 响应外壳与客户端容错

成功：

```json
{"subsonic-response": {"status": "ok", "version": "1.16.1",
  "type": "xiatiao-api", "serverVersion": "0.1.0", "openSubsonic": true}}
```

失败：

```json
{"subsonic-response": {"status": "failed", "error": {"code": 70, "message": "not found"}}}
```

客户端行为：

- 必须能解析出 `subsonic-response` 对象，否则抛错；
- `status == "failed"` 时**只读取 `error.message`** 展示给用户，不按 `code` 分支；
- HTTP 状态码非 2xx 直接视为异常（`stream` 除外，见 3.2）；
- `openSubsonic` **不做判定**，仅作信息字段；是否私有后端只看 `type`（见第 4 节）。

端点超时：默认 **15 秒**；`getPlaylists` 与 `getPlaylist` 放宽到 **60 秒**
（大歌单序列化慢、部分服务端后台刷新时会抖动）。

适配器使用的错误码：`0` 通用、`10` 缺参数、`40` 未认证、`70` 未找到。

---

## 3. 标准端点

### 3.1 ping — 连通性与后端类型探测

`GET /rest/ping.view`

```json
{"subsonic-response": {"status":"ok","version":"1.16.1",
  "type":"xiatiao-api","serverVersion":"0.1.0","openSubsonic":true}}
```

客户端在**每次 ping 成功后**把 `type` 写入全局状态（`set_backend_type`），
因此任何一次 ping（设置页「连接」按钮、启动探测、加载账号）都会刷新后端类型。

适配器 ✅：`type` 取环境变量 `SUBSONIC_TYPE`，默认 **`xiatiao-api`**；
`serverVersion` 取 `SUBSONIC_SERVER_VERSION`，默认 `0.1.0`。

### 3.2 stream — 音频流（播放核心）

`GET /rest/stream.view`

客户端发送的参数：

| 后端类型 | 参数 |
|---|---|
| 私有（`type=xiatiao-api`） | `id`、`quality`、`maxBitRate` |
| 标准 | `id`、`maxBitRate=0` |

`quality` 取值与 `maxBitRate` 的对应（客户端 `core/quality.py`）：

| `quality` | 显示名 | `maxBitRate` |
|---|---|---|
| `standard` | 标准 | 128 |
| `high` | 高品 | 320 |
| `lossless` | 无损（默认） | 999 |
| `hires` | Hi-Res | 1400 |
| `master` | 母带 | 2000 |

客户端**显式**发送 `maxBitRate=0` 表示「不限制、原文件直传」
（省略该参数可能触发服务端默认转码）。`format` 参数仅在下行场景使用，当前不发送。

#### Range 是硬要求

播放器网络流走 Rust 侧 `HttpMediaSource`：先用 `Range: bytes=0-0` 探测，
**要求返回 206 且带 `Content-Range`**，否则判定「不支持 Range」并回退 ffmpeg 子进程
（仍能播放，但切歌/seek 变慢，且失去断流自动续传能力）。

适配器 ✅：`proxyAudio` 透传客户端 `Range`，回传上游状态码与
`Content-Type` / `Content-Length` / `Content-Range` / `Accept-Ranges`。

#### 上游直链失效与 404

适配器缓存「id + 质量 → 上游直链」。上游返回 401 / 403 / 5xx 时换链重试一次；
416 / 404 不重试（换链拿到的还是同一资源）。上游拿不到播放地址时适配器返回
**HTTP 404**（而不是 200 + JSON），客户端在 HTTP 层即可判定失败并**立即跳下一首**，
不必等卡死检测超时。

#### 适配器的质量映射

```
quality: standard → 128
         high     → 320
         lossless / hires / master → flac
quality 缺失时按 maxBitRate：<=128 → 128，<=320 → 320，其余 → flac
```

注意：适配器**不做转码**，只是向上游请求不同规格的文件；
`hires` / `master` 与 `lossless` 走同一条 flac 路径，实际音质取决于上游提供的文件。

### 3.3 getCoverArt — 封面

`GET /rest/getCoverArt.view?id=...&size=...`

- `size` 仅在大于 0 时发送；播放器默认不带 `size`。
- 响应可以是图片字节，也可以是 **302 跳转**；客户端用 `urllib` 下载并自动跟随跳转。
- 歌曲无 `coverArt` 时，客户端回退用歌曲 `id` 作为封面 id。

适配器 ⚠️：固定 **302 到 `https://y.gtimg.cn/...`**（`size` 默认 300），并支持前缀：

| id 前缀 | 图片类型 | 上游前缀 |
|---|---|---|
| （无） | 专辑封面 | `T002` |
| `top_` | 榜单封面 | `T003` |
| `singer_` | 歌手头像 | `T001` |

### 3.4 search3 — 搜索与在线曲库

`GET /rest/search3.view?query=&songCount=&songOffset=&artistCount=&albumCount=`

客户端**始终**发送全部 5 个参数（`artistCount` / `albumCount` 通常为 `0`），
服务端应容忍 `0`，不要因「传了但没用到」而报错。

响应解析顺序（取第一个存在 `song` 的层级）：

1. `searchResult3.song`
2. `searchResult2.song`
3. `searchResult.song`

每层 `song` **可以是数组，也可以是单个对象**（客户端两种都接受），
字段映射见第 7 节。

用途一：搜索结果（`songCount=50`，翻页用 `songOffset`）。
用途二：**在线曲库**——客户端发**空 query** 分页列出全部歌曲
（`all_songs`，首批 `count=100`）。这不是 Subsonic 标准行为。

适配器 ⚠️ 三点差异：

1. **空 query 直接返回 `searchResult3: {}`** → 在线曲库为空。
2. 上游单页 `limit` 被压到 **≤20**，而分页号按 `page = songOffset/songCount + 1` 计算。
   客户端按 `count=100` 翻页时，第 2 页 `offset=100` → `page=2`、返回第 20–39 条，
   **与请求区间不符**（会漏歌/重复）。服务端应让 `songOffset` 与实际返回条数一致。
3. 适配器忽略 `artistCount` / `albumCount`（只返回歌曲）。

### 3.5 getSong / getAlbum / getArtist / getArtists / getAlbumList2

#### getSong

`GET /rest/getSong.view?id=...` → `song` 对象。适配器 ✅：查不到返回 `failed`（`code=70`）。

#### getAlbum

`GET /rest/getAlbum.view?id=...` → `album` 对象，`album.song` 为曲目数组
（客户端接受数组或单对象）。适配器 ✅，并为缺 `coverArt` 的曲目补上专辑 mid。

#### getArtist

`GET /rest/getArtist.view?id=...` → `artist` 对象，`artist.album` 为专辑数组
（客户端再逐张 `getAlbum` 取歌）。适配器 ⚠️ **仅桩实现**：
返回 `{id, name: id, albumCount: 0}` 且无 `album`，因此取该艺术家歌曲为空。

#### getArtists

`GET /rest/getArtists.view` → `artists.index[].artist[]`，字段 `id`、`name`、`albumCount`，
以及可选的封面字段（见第 6 节）。适配器 ⚠️ **固定返回空索引** `{"index": []}`，
播放器在线页的「艺术家」区块因此不显示。

客户端解析细节：`index` 与 `artist` 均接受数组或单对象。

#### 艺术家封面：客户端四级择优（`all_artists`）

按「越靠前越真实」排列，逐级尝试：

1. **`artistImageUrl`** —— 完整图片 URL。**必须先探测真的能下载**
   （200 且 `Content-Type: image/*`）才采用；
2. **第一首歌的专辑封面** —— `getArtist` → 首张 `getAlbum` → 首曲 `coverArt`。
   服务端没有艺术家图时的最佳替代，拿到的是**真实**专辑封面；
3. **`coverArt`** —— 封面 id 拼 `getCoverArt`。服务端保证有图，
   但「无艺术家图」时返回的是接近纯白的通用占位图（视觉上等同没有封面），故排最后；
4. 都没有 → 空串（前端显示占位）。

> ⚠️ **Navidrome 陷阱**：艺术家没有图片时，`getArtists` **仍会返回格式合法的
> `artistImageUrl`**（签名 URL），但请求结果是 `404 Artwork not found`。
> 客户端因此会先发一次轻量请求（只读 1 字节）验证，失败则走第 2 级。
> 后端若能直接给出可下载的地址，可省掉这次探测。
>
> 第 1、2 级合计最多 3 次请求 / 位艺术家，故只对**前 30 位**做择优
> （`_ARTIST_FALLBACK_MAX`，区块首屏显示数量）；其余仅用 `coverArt`，
> 避免大曲库拖慢加载。

#### getAlbumList2

`GET /rest/getAlbumList2.view?type=&size=&offset=`

客户端用于「最新」区块（`type=newest`，`size=30`）与「随机」区块
（实际传 `type=alphabeticalByArtist`，`size=30`），滚动加载时递增 `offset`。

适配器 ⚠️：**忽略 `type` 与 `offset`**，固定返回上游「新专辑」，`size` 上限 20。
两个区块内容会重复，且无法真正翻页。

### 3.6 getPlaylists / getPlaylist

#### getPlaylists

`GET /rest/getPlaylists.view`（超时 60s）

响应 `playlists.playlist`，**数组或单对象**；无歌单时 `playlists` 为空对象。
每个歌单字段：`id`、`name`、`songCount`、`coverArt`（可选）、`comment`（描述，可选）。

客户端兜底：`coverArt` 为空时用歌单 `id` 拼封面 URL（可能 404，前端有占位图）。
适配器 ✅（在 `coverArt` 缺失时用「我喜欢的音乐」首曲封面补齐第一个歌单）。

#### getPlaylist

`GET /rest/getPlaylist.view?id=...`（超时 60s）

响应 `playlist.entry`，**数组或单对象**。

**扩展参数 `exclude`**（仅「每日推荐」续拉使用）：

    GET /rest/getPlaylist.view?id=virtual:daily&exclude=id1,id2,...,idN

- 值为逗号分隔的已播歌曲 id；
- 客户端维护最多 **500** 个近期已播 id（`DAILY_EXCLUDE_MAX`，控制 URL 长度）；
- 服务端应返回**排除这些 id 后**的内容；返回空数组表示「没有新内容」，
  客户端据此停止续拉（不会无限请求）。

#### 虚拟歌单（适配器扩展）

`id` 形如 `virtual:<kind>[:<arg>]`：

| id 示例 | 含义 | 内容来源 | 支持 `exclude` |
|---|---|---|:---:|
| `virtual:daily` | 每日推荐（雷达） | `/recommend/radar` | ✅ |
| `virtual:radar` | 雷达模式 | `/recommend/radar` | ✅ |
| `virtual:daily30` | 每日30首 | `/user/daily-songs` | ❌ |
| `virtual:guess` | 猜你喜欢 | `/user/guess-you-like` | ❌ |
| `virtual:toplist:<id>` | 榜单 | `/getRanks` | ❌ |
| `virtual:radio:<id>` | 电台 | `/radio/tracks` | ❌ |
| `virtual:category:<id>` | 歌单分类 | `/getSongLists` | ❌ |

播放器**只把 `virtual:daily`** 视为「实时流」：点卡片即播、队列快播完时自动带
`exclude` 续拉（`STREAM_PLAYLIST_IDS`）。其余虚拟歌单按普通静态歌单处理
（先进详情页再点歌）。「每日30首」内容同样会变，但客户端不续拉。
新增实时流需同时改客户端白名单。

### 3.7 getStarred2 / star / unstar

`GET /rest/getStarred2.view` → `starred2.song`（客户端也兼容旧字段 `starred.song`；
`song` 可为数组或单对象）。适配器 ✅，上限 100 首。

`GET /rest/star.view?id=...` / `GET /rest/unstar.view?id=...` → 成功即可，客户端不读业务字段。
适配器 ✅：先按 mid 查上游数字 id 再写入；失败返回 `failed`（`code=0`）。

### 3.8 getRandomSongs — 随机歌曲

`GET /rest/getRandomSongs.view?size=...` → `randomSongs.song`（数组或单对象）。

适配器 ⚠️：并非随机，固定用关键词「热门」搜索，`size` 上限 20。

### 3.9 歌词

客户端策略（`services/online_lyrics.py`）：

1. 首选 `getLyricsBySongId`（结构化）；
2. 失败或无内容 → 回退 `getLyrics`（`artist` + `title`）；
3. 两者都空 → 无歌词（不影响播放）。

#### getLyricsBySongId

`GET /rest/getLyricsBySongId.view?id=...`

```json
{"subsonic-response": {"status":"ok","lyricsList":{"structuredLyrics":[
  {"lang":"zh","synced":true,"line":[
    {"value":"第一句","start":13010},
    {"value":"第二句","start":17520}
  ]}
]}}}
```

| 字段 | 要求 |
|---|---|
| `structuredLyrics` | 数组（客户端也接受单对象） |
| `line` | 数组（客户端也接受单对象） |
| `line[].value` | **纯正文**，理想情况不含 `[mm:ss]` 标签 |
| `line[].start` | **毫秒整数** |
| `synced` | 有时间轴 `true`；纯文本 `false` |
| `lang` | 多语言时每种语言一项，不要把翻译混进同一条 |

客户端解析规则（务必对齐）：

- `start` **一律除以 1000** 当作毫秒处理（已移除「大于 10000 才算毫秒」的旧启发式，
  否则 6.12s = 6120ms 这类小于 10000 的毫秒值会被误当秒，导致开头几句错位）；
- 只要有一条 `value` 含 `[` 与 `]`，客户端就**丢弃整个结构化结果**、
  把这些行当 LRC 原文统一重新解析时间轴；
- 相邻完全重复的行会被合并（`dedupe_lyrics`），**不做跨时间全局去重**
  （否则重复的副歌会被误删，后半段无歌词）。

适配器 ✅：从上游 `/getLyric` 取 LRC 后自行解析为「毫秒 + 纯正文」；多时间标签行会展开成
多条并按时间排序；无时间轴时 `synced=false`、`start=0`；无歌词时
`structuredLyrics` 为 `null`。

#### getLyrics

`GET /rest/getLyrics.view?artist=&title=` → `lyrics.value` 为字符串，
可含 `[mm:ss.xx]` 标签（客户端用 LRC 解析；无标签按纯文本处理）。

适配器 ⚠️：**恒返回空 `value`**（桩实现），因此回退路径对适配器无效。

### 3.10 getUser — 用户信息（标准）

`GET /rest/getUser.view?username=...`（缺省时用当前登录用户名）→ `user` 对象。

客户端只读取**键名以 `Role` 结尾**的布尔字段，对应 UI 标签：
`adminRole`、`downloadRole`、`streamRole`、`coverArtRole`、`shareRole`、
`playlistRole`、`uploadRole`。

> **已知限制**：UI 里还定义了 `scrobblingEnabled` 标签，但客户端只扫描 `*Role` 键，
> 该标签实际不会显示。适配器虽返回 `scrobblingEnabled: true`，同样不显示。
> 若要展示，需改用 `scrobblingEnabledRole` 之类键名或调整客户端取值规则。

适配器 ✅：返回固定角色集（`adminRole`/`downloadRole`/`playlistRole`/`coverArtRole`/
`streamRole`/`shareRole` 为 `true`，`uploadRole`/`jukeboxRole` 为 `false`）。

### 3.11 scrobble（可选） — 播放上报

`GET /rest/scrobble.view?id=...&submission=true`。客户端当前**未调用**（保留接口）。
适配器 ✅：空实现，直接返回 `ok`。

---

## 4. 后端类型：`type` 字段的实际作用

`ping` 返回的 `type` 是**功能开关**，不只是展示信息。

客户端判定（白名单，大小写不敏感）：

```python
_PRIVATE_BACKEND_TYPES = {"xiatiao-api"}
is_private = type.strip().lower() in _PRIVATE_BACKEND_TYPES
```

- 命中 → **私有后端**：显示音质下拉、`stream` 带 `quality`、小红心写入在线收藏、
  启用「所选音质被降级」提示；
- 未命中（`navidrome` / `subsonic` / 空 / 未知）→ **标准后端**：不显示档位、
  `stream` 只传 `maxBitRate=0`、收藏落本地库。

必须用白名单而非黑名单：未知标准服务端不应被误判为私有。
若你的后端要实现音质档位，**必须让 `ping` 返回 `"type": "xiatiao-api"`**。

适配器默认即 `xiatiao-api`（`SUBSONIC_TYPE` 可控），因此天然启用档位。

---

## 5. 私有扩展端点（可选实现）

以下为非 Subsonic 标准，供播放器增强。后端可实现可不实现（播放器会降级）。
路径同样用 `/rest/名字.view`。

### 5.1 getUserProfile — 用户资料

`GET /rest/getUserProfile.view?username=...`

| 返回字段 | 客户端用途 |
|---|---|
| `nickname` | 顶部昵称 |
| `avatarUrl` | 头像图片 URL，**优先于** `getAvatar` |
| `username` / `isVip` / `source` | 当前未使用（保留） |

调用时机：启动探测、开关在线音源、刷新在线页；失败静默降级到标准 `getUser`。

适配器 ✅：从上游 `/user/detail` 取 `nick` / `logo`，`source` 固定 `upstream`。

> 注：标准 Navidrome 不支持此端点，播放器会降级到 `getUser` + `getAvatar`。

### 5.2 getAvatar — 用户头像

`GET /rest/getAvatar.view?username=...` → 图片字节，或 302 跳到图片。
客户端用 `urllib` 下载并自行解码为 `GdkPixbuf`，**跟随跳转**。

仅当 `getUserProfile.avatarUrl` 为空才使用。适配器 ✅（302；无头像返回 `code=70`）。

### 5.3 getRecommendations — 推荐内容

`GET /rest/getRecommendations.view?source=&type=`

| 参数 | 说明 |
|---|---|
| `source` | 平台标识；播放器**传空串**（由后端决定默认来源） |
| `type` | `daily` / `rank` / `guess` / `radio` / `categories` |

播放器**实际只请求两种**：`rank`（排行榜区块）与 `daily`（每日推荐区块）。

响应：

```json
{"subsonic-response":{"status":"ok","recommendations":{
  "source":"upstream","type":"daily",
  "sections":[
    {"id":"daily","title":"每日推荐","kind":"playlist",
     "playlistId":"virtual:daily","coverArt":"000xxx"}
  ]}}}
```

客户端对 `sections` 各字段的**实际用法**：

| 字段 | 用法 |
|---|---|
| `title` | 卡片主标题 |
| `id` | **卡片副标题**（直接显示给用户，建议给可读文案或留空） |
| `playlistId` | 点击后用它调 `getPlaylist`；为空则卡片不可点 |
| `coverArt` | 封面 id，拼 `getCoverArt` |
| `kind` | **未读取**（客户端只支持歌单型） |
| `children` | **未读取**（分类树未实现） |

因此服务端只需保证 `kind=playlist` 语义；`kind=songs` 与 `children` 目前不会被消费。

`playlistId` 是**虚拟歌单 id**，播放器不解析，原样回传给 `getPlaylist`，
后端动态返回内容。点卡片 → `getPlaylist(playlistId)` → 播放。

适配器 ✅：`sections` 全部为 `kind:"playlist"`，`id` 形如 `daily` / `daily30` /
`rank-<id>` / `radio-<id>` / `cat-<id>` / `official-<id>` / `guess`；
未知 `type` 返回空 `sections`（客户端据此隐藏区块）。

### 5.4 qrLogin — 扫码登录（两侧均未落地）

协议曾预留如下形态：

**创建**：`GET /rest/qrLogin.view?action=create&source=...` → `qrLogin` 对象
（`source`、`key`、`url` 或 `imageUrl`、`expiresAt`）。
**轮询**：`GET /rest/qrLogin.view?action=status&source=&key=` → `qrLoginStatus`
（`status`：`waiting` / `scanned` / `confirmed`（带 `cookie`）/ `expired` / `failed`）。

**当前状态**：适配器未实现该端点（返回 `unsupported endpoint`）；
播放器虽有 `qr_login_create` / `qr_login_status` 方法，但**没有任何 UI 入口**调用它们。
字段定义仅为将来预留，不作为契约要求。

---

## 6. 扩展字段

后端可在标准端点返回里**附加字段**供播放器增强显示；缺失时自动降级。

### 6.1 歌曲节点

| 字段 | 客户端用途 | 适配器 |
|---|---|---|
| `suffix` | **真实格式**（`flac`/`mp3`/`m4a`/`ape`/`dsf`…，小写无点）。在线流 URL 是 `...view`，无法从 URL 判格式，DSD/DXD/HR/CD 徽章判定依赖它 | ✅ 由 `size_flac`/`size_320mp3`/`size_128mp3`/`size_m4a`/`size_ape` 推断 |
| `qualityLevels` | 该曲支持的档位（见 8.2） | ✅ 仅 `standard`/`high`/`lossless` |
| `samplingRate` / `bitDepth` / `channelCount` | OpenSubsonic 技术参数，用于列表静态徽章 | ❌ **未提供** |
| `bitRate` | kbps，客户端 ×1000 存 bps | ❌ 未提供（恒 0） |

> 因技术参数缺失，播放器对适配器曲目的实际规格判定**完全依赖解码器实测**
> （`audio-info` 事件里的采样率/位深 + `suffix` 兜底 codec）；
> 列表里的静态徽章只能用 `suffix` 推断，精度较低。

### 6.2 艺术家节点

`getArtists` 的 artist 可附 `artistImageUrl`（完整 URL）或 `coverArt`（封面 id），
用于避免「四级择优」的额外请求。适配器返回空索引，故未使用。

> 注意：`artistImageUrl` **必须真的能下载**，否则客户端要额外探测一次再回落；
> 无图时不要返回「看似合法但 404」的地址（Navidrome 就是这样做的）。

### 6.3 歌单节点

`getPlaylists` / `getPlaylist` 的 `comment` 会作为歌单描述显示。适配器未提供。

---

## 7. 歌曲字段映射

| Subsonic 字段 | TrackItem 字段 | 说明 |
|---|---|---|
| `id` | `source_id` | 必填；缺失则曲目被丢弃 |
| `title` | `title` | 缺失时用 `Unknown` |
| `artist` | `artist` | 同上 |
| `album` | `album` | |
| `duration` | `duration_seconds` | **秒**（整数/浮点均可） |
| `coverArt` | `cover_url` | 缺失回退用 `id` |
| `bitRate` | `bitrate` | kbps → ×1000 |
| `samplingRate` / `bitDepth` / `channelCount` | 同名字段 | 可选 |
| `suffix` | `format_hint` | 推荐返回（小写无点） |
| `qualityLevels` | `quality_levels` | 私有后端用 |

---

## 8. 音质档位体系（客户端行为）

### 8.1 档位与默认值

- 全局默认档位：**`lossless`**（配置项 `online_quality`）。
- 用户选择**全部持久化**（含 `hires` / `master`），重启后沿用，不擅自回落。
- 运行时档位 `_RUNTIME_QUALITY`：仅用于「当前歌不支持偏好档」时的**临时降级**，
  不写配置；切到支持偏好档的歌时清空，自动升回偏好档。

### 8.2 `qualityLevels` 的清洗与兜底

客户端 `normalize_quality_levels`：

- 非数组 / 空数组 / 全部非法 → **兜底为三档**：`standard`、`high`、`lossless`
  （**不是五档**；`hires` / `master` 只有服务端显式声明才会出现）；
- `key` 去空白、转小写、同 key 去重（保留首次出现）；
- `name` 缺省 → 短显示名 → 再缺用 `key`；
- `br` 缺省/非法 → 按 3.2 的档位表补，仍取不到则 0；
- 保持服务端数组顺序。

格式：

    "qualityLevels": [
      {"key": "lossless", "name": "无损", "br": 999},
      {"key": "hires", "name": "Hi-Res", "br": 1400}
    ]

选择逻辑（`effective_quality`）：偏好档在该歌列表里 → 用它；否则用该歌**最高 `br`** 的档
（不改全局偏好）。

仅私有后端才显示音质下拉框；标准后端整块隐藏。

### 8.3 播放 URL 的构造（单一入口 `playable_url`）

```
私有后端 + 指定档位 → stream?id=..&quality=<key>&maxBitRate=<br>
私有后端 + 未指定   → 用「运行时档位 → 配置档位」
标准后端           → stream?id=..&maxBitRate=0
```

> 私有后端必须**同时**带 `quality` 与 `maxBitRate`（早期实测只传 `maxBitRate`
> 会被部分实现忽略）。适配器在缺 `quality` 时也看 `maxBitRate`，但客户端仍按上述规则发送。

### 8.4 降级提示

仅当「私有后端 + 在线歌 + 非多声道」且实测规格低于所选档位的期望规格时，
toast 提示「所选 X 不可用，实际为 Y」。标准后端无「所选档位」概念，不提示。

---

## 9. 已知差异清单（接适配器时的实际表现）

| 现象 | 原因 | 影响 |
|---|---|---|
| 在线曲库为空 | `search3` 空 query 固定返回空 | 「曲库 → 在线」tab 无内容 |
| 曲库翻页错位 | 上游 limit ≤20，但分页号按客户端 `songCount=100` 算 | 漏歌 / 重复 |
| 艺术家区块不显示 | `getArtists` 返回空索引 | 无艺术家浏览 |
| 艺术家详情为空 | `getArtist` 桩实现，无 `album` | 点进去无歌曲 |
| 「最新」「随机」区块相同 | `getAlbumList2` 忽略 `type`/`offset` | 两区块重复，翻页无效 |
| 随机歌曲不随机 | `getRandomSongs` 固定关键词「热门」 | 每次内容相同 |
| 传统歌词回退为空 | `getLyrics` 恒返回空 `value` | 只有结构化歌词可用 |
| 列表徽章精度低 | 未返回 `samplingRate`/`bitDepth`/`channelCount`/`bitRate` | 靠 `suffix` 推断，播放后由实测校正 |
| 卡片副标题是内部 id | `sections[].id` 被客户端当副标题 | 显示 `rank-123` 之类 |
| 扫码登录不可用 | 适配器无 `qrLogin`，播放器亦无 UI | 功能缺失（不影响连接） |
| 密码为空时全部失败 | 适配器要求认证参数存在，客户端空密码不发 | **必须填非空密码** |

---

## 10. 最小实现清单

要让播放器核心可用，服务端至少实现：

1. `ping`（连通 + `type`）
2. `search3`（搜索；若要「在线曲库」，需支持空 query 列出）
3. `stream`（**必须支持 Range**，理想是 206 + `Content-Range`）
4. `getCoverArt`
5. `getAlbumList2`（专辑区块）
6. `getPlaylists` / `getPlaylist`
7. `getLyricsBySongId` 或 `getLyrics`（至少一个）

可选增强（客户端会自动降级）：

- `getAlbum` / `getArtists` / `getArtist`（专辑与艺术家浏览）
- `getStarred2` / `star` / `unstar`（收藏）
- `getRandomSongs`（随机区块）
- `getUser`（账号角色）
- 私有扩展：`getUserProfile` / `getAvatar` / `getRecommendations`

> 客户端**没有**实现 `getGenres` / `getSongsByGenre` / `getTopSongs` / `getSimilarSongs2`，
> 适配器也未提供，无需实现。

---

## 11. 兼容性建议

- **`.view` 后缀**：客户端固定请求 `/rest/{端点}.view`；服务端最好同时接受不带后缀。
- **数组/单对象**：`song` / `entry` / `playlist` / `album` / `artist` / `index` / `line` /
  `structuredLyrics`，客户端都能容忍单对象；服务端保持标准数组即可。
- **JSON 优先**：客户端只用 `f=json`；如需兼容其他客户端可同时支持 `f=xml`。
- **Range**：`stream` 必须支持；返回 200（无 Range）会导致回退 ffmpeg，切歌与 seek 变慢。
- **无损直传**：`maxBitRate=0` 时应原文件直传，不要按默认档位转码。
- **错误码**：遵循标准（40 认证失败、70 未找到）；客户端只展示 `message`，
  但 `stream` 的 HTTP 状态码很重要（404 会被立即判失败并跳下一首）。
- **超时**：单请求尽量 15 秒内返回；大歌单（`getPlaylist`）放宽到 60 秒。

---

*本文档按 2026-10-26 的两侧实现校对（客户端 `providers/subsonic.py` +
适配器 `subsonic-adapter-go`）。代码变动时请同步更新本文件。*

