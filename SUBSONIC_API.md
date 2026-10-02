# Subsonic 接口使用指南（后端开发契约）

本文档是虾条播放器在线音源模块与后端服务的接口契约。

播放器是通用 Subsonic 客户端：标准部分遵循 Subsonic API，任何合规服务端都能连；扩展部分是私有端点（头像、扫码登录），其他客户端会忽略。

## 认证

所有请求走 Subsonic 标准认证参数：u 用户名，t 令牌=md5(密码+salt)，s 随机salt，p 明文密码（与t/s二选一），v 固定1.16.1，c 固定fenda，f 固定json。

## 请求约定

基地址由用户在设置填写，如 http://127.0.0.1:4533。
端点格式：基地址/rest/端点.view?参数。
返回统一 f=json，顶层为 subsonic-response，成功 status=ok，失败 status=failed 并带 error。

## 标准端点（必须实现）

**ping** — 连通性。GET /rest/ping.view

**search3** — 搜索。GET /rest/search3.view 参数 query、songCount=50、songOffset=0、artistCount=0、albumCount=0。
返回 searchResult3.song 数组，字段：id、title、artist、album、duration、coverArt、suffix、bitRate、contentType。
映射：id 到 source_id；title 到 title；artist 到 artist；album 到 album；duration 到 duration_seconds；coverArt 用于封面URL；bitRate（kbps）乘1000 到 bitrate（bps）。

**stream** — 音频流（播放核心）。GET /rest/stream.view 参数 id、maxBitRate=0。
关键：1) 必须支持 HTTP Range（返回 206 和 Content-Range），否则拖动进度条异常；2) maxBitRate=0 表示不限制，原文件直传，支持无损；3) 返回头含 Content-Type（如 audio/flac）和 Content-Length。

**getCoverArt** — 封面。GET /rest/getCoverArt.view 参数 id、size（可选），返回图片二进制。

**getSong** — 单曲信息。GET /rest/getSong.view 参数 id，返回 song 对象。

**歌词** — GET /rest/getLyrics.view 参数 artist、title；或 GET /rest/getLyricsBySongId.view 参数 id，返回 lyricsList.structuredLyrics 数组，每项含 line 数组（value、start）。

**歌单** — GET /rest/getPlaylists.view 返回 playlists.playlist 数组（id、name、songCount、coverArt）；GET /rest/getPlaylist.view 参数 id 返回 entry 数组。

**收藏** — GET /rest/getStarred2.view 返回 starred2.song 数组；GET /rest/star.view 参数 id；GET /rest/unstar.view 参数 id。

**浏览** — GET /rest/getAlbumList2.view 参数 type、size、offset；GET /rest/getArtists.view。

**scrobble（可选）** — GET /rest/scrobble.view 参数 id、submission=true。

## 私有扩展端点（可选实现）

以下为非 Subsonic 标准，供播放器增强。后端可实现可不实现（播放器会降级）。路径同样用 /rest/名字.view。

**getUserProfile** — 用户资料。GET /rest/getUserProfile.view 参数 username（可选）。
返回 subsonic-response 内 userProfile 对象，字段：username、nickname、avatarUrl、isVip、source。
播放器用于主页顶部显示头像和昵称。

**getAvatar** — 用户头像。GET /rest/getAvatar.view 参数 username（可选），直接返回图片二进制，或 302 跳转到图片URL。

**qrLogin 创建** — GET /rest/qrLogin.view 参数 action=create、source（平台标识）。
返回 subsonic-response 内 qrLogin 对象，字段：source、key、url、imageUrl（可选）、expiresAt。
url 与 imageUrl 至少一个：url 是二维码内容文本（播放器本地生成二维码）；imageUrl 是现成图片（http链接或 data:image 开头的base64），播放器直接显示。

**qrLogin 轮询** — GET /rest/qrLogin.view 参数 action=status、source、key。
返回 subsonic-response 内 qrLoginStatus 对象，字段：status、message、cookie（成功时）。
status 取值：waiting（等待扫码）、scanned（已扫待确认）、confirmed（成功，应带 cookie）、expired（过期）、failed（失败）。

## 扩展字段（可选）

后端可在标准端点的返回里**附加字段**，供播放器增强显示。播放器遇缺失时降级。

**getArtists 的 artist 节点可附加：**

- `coverArt`：艺术家封面 id（播放器会拼成封面 URL 显示）。
  标准 Subsonic 无此字段；支持的实现（如国内流媒体中转）可返回，
  让艺术家列表显示头像/封面。

**getUserProfile / getAvatar**：见扩展端点章节。

## 兼容性建议

.view 后缀：播放器请求 /rest/端点.view；后端建议同时接受不带后缀。
JSON 优先：播放器只用 f=json；建议后端同时支持 f=xml 以兼容其他客户端。
Range 支持：stream 端点务必支持 Range。
无损直传：maxBitRate=0 时尽量原文件直传。
错误码：遵循 Subsonic 标准（40=认证失败，70=未找到）。

## 最小实现清单

能搜能播（必须）：认证（u/t/s 或 u/p）、ping、search3、stream（支持 Range）、getCoverArt。

完整功能（可选）：getSong、getLyricsBySongId、getPlaylists、getPlaylist、getStarred2、star、unstar、getAlbumList2、getArtists、getUserProfile、getAvatar、qrLogin。

## 播放器侧实现

客户端代码见 providers/subsonic.py。方法对照：ping、search3/search、stream_url、cover_art_url、get_song、get_lyrics_by_song_id、get_playlists/get_playlist、get_starred/star/unstar、get_album_list2/get_artists、get_user_profile、get_avatar_url、qr_login_create、qr_login_status。

## 说明

本文档描述播放器与后端的接口契约。后端如何获取音乐（各平台解析、Cookie、音质档位）不在本契约范围，由后端自行实现。
播放器不含任何音乐平台解析逻辑，是干净的通用 Subsonic 客户端。
