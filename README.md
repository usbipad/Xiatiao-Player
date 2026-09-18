# QQMusic Tonearm Style

一个 GTK4 / libadwaita 编写的本地音乐播放器，界面参考 Tonearm 与 Apple Music 风格。

> 本地播放：本地曲库扫描、真实音频解码播放、封面与音频技术信息、沉浸式全屏播放页。
> QQ 音乐在线音源：对接本地自部署 qq-music-api（默认 127.0.0.1:3200），
> 支持搜索、扫码登录、实时播放地址、在线歌词与歌单。

## 功能

- 本地曲库：扫描多个音乐目录，识别 .mp3 .flac .ape .wv .m4a .ogg .wav .dsf .dff
- 元数据：读取歌名 / 歌手 / 专辑 / 时长，以及采样率 / 位深 / 声道 / 码率
- 内嵌封面：从音频文件读取封面图显示（无封面时回退占位符号）
- 播放内核：基于 GStreamer 手动管道（filesrc -> decodebin -> volume -> audiosink），非 playbin
- 播放控制：播放 / 暂停、上一首 / 下一首、自动切歌
- 播放模式：随机（洗牌式，一轮内不重复）、循环（不循环 / 列表循环 / 单曲循环）
- 音量：点击喇叭图标弹出竖向滑块调节
- 进度条：实时跟随播放进度，支持点击跳转与拖动 seek
- 沉浸式全屏播放页：封面背景（由封面提取主色）、滚动歌词（当前行高亮、平滑缓动、首尾渐隐）、播放控制
- 设置：多音乐目录管理（持久化到 ~/.config/qqmusic-gtk/config.json）
- 搜索：本地曲库内按歌名 / 歌手 / 专辑过滤
- QQ 音乐在线音源：关键词搜索、扫码登录（Cookie 本地持久化）、点击即播、在线 LRC 歌词滚动
- 我的歌单：浏览收藏歌单，点击歌单加载曲目列表并可播放

## 依赖

### 系统包（Debian / Ubuntu，需管理员权限）

    apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly python3-mutagen

- gir1.2-adw-1：libadwaita 绑定
- gir1.2-gstreamer-1.0 + plugins：音频解码播放
- python3-mutagen：读取内嵌封面与歌词（缺失时相关功能自动降级，不影响播放）

### 可选

- ffmpeg：仅测试脚本用它生成测试音频

## 运行

    python3 main.py

首次运行后，点击右上角设置图标添加音乐目录，切到「本地曲库」即可看到扫描结果。

## 操作说明

- 点击左侧封面右下角的悬浮按钮进入沉浸式全屏播放页（Esc 或顶部返回按钮退出）
- 全屏页：点击进度条跳转、拖动滑块 seek；点击歌词行跳到对应时间
- 音量：点击左侧面板的喇叭图标，弹出竖向滑块
- 随机 / 循环：控制行左右两端的按钮

## QQ 音乐在线音源

依赖本地自部署的 [@sansenjian/qq-music-api](https://github.com/sansenjian/qq-music-api)（社区逆向 Fork，2.6.0），
API 服务独立于 GUI 进程，默认监听 `127.0.0.1:3200`。

### 启动 API 服务

克隆仓库后依次执行 npm install、npm run build、npm run start，
服务即监听 127.0.0.1:3200。首页 `/` 提供测试页，`/login` 提供网页扫码登录。

### GUI 使用

1. 先启动本地 API 服务（本程序不负责拉起）。
2. 运行本程序，切到顶部「QQ音乐」导航，输入关键词搜索。
3. 点击左侧面板的登录入口，用手机 QQ 扫码登录；Cookie 仅保存在本机
   `~/.config/qqmusic-gtk/qq_cookie.json`，不会上传。
4. 切到顶部「我的歌单」导航，可浏览收藏歌单；点击某个歌单，其曲目会显示在
   「QQ音乐」页面，点击任意曲目即可播放。
5. 在「设置 → 在线音源」可修改 API 地址、查看登录状态、退出登录。

### 对接的接口契约（基于 2.6.0 源码核对）

- 搜索 GET /getSearchByKey，参数 key/limit/page/remoteplace=song，无需登录
- 播放地址 GET /getMusicPlay，参数 songmid/quality=128/resType=play，**需登录**
- 歌词 GET /getLyric，参数 songmid/isFormat=1，无需登录
- 我的歌单 GET /user/getUserPlaylists，参数 uin/offset/limit，**需登录**
- 歌单详情 GET /getSongListDetail，参数 disstid，无需登录
- 二维码 GET /getQQLoginQr，无参数
- 查扫码 POST /checkQQLoginQr，body 含 ptqrtoken 与 qrsig

Cookie 通过请求头 Cookie 传递；getUserPlaylists 还需从 Cookie 提取 uin。
播放地址在响应 data.playUrl.<songmid>.url；未登录时该字段给出明确错误提示。

约定与限制：
- 播放地址（vkey）有时效，**每次播放前实时请求，绝不缓存**。
- VIP 音源以登录账号自身权限为准，API 不破解会员。
- 若接口临时失效，通常是上游加密逻辑变更，需跟进 qq-music-api 仓库更新。
- 高频请求可能触发风控，请控制搜索频率。
- 仅访问 `127.0.0.1` 本地接口，不直连公网。

## 桌面集成（可选）

安装应用图标和 .desktop 文件，使任务栏 / 应用列表显示正确图标：

    python3 tools/install_desktop.py

图标缓存可能需要重新登录或重启桌面后才刷新。

## 项目结构

    qqmusic-gtk/
    ├── main.py                  应用入口
    ├── models/                  TrackItem / coverart（封面、主色）/ lyrics（歌词）
    ├── config/                  配置持久化
    ├── providers/               音源抽象与本地实现
    ├── core/                    Playlist / PlayerCore
    ├── ui/                      窗口、面板、全屏页、设置
    ├── data/icons/              应用图标
    ├── tools/                   图标生成与桌面集成
    └── tests/                   测试脚本

## 测试

    python3 tests/smoke_test.py   # 后端链路（隔离临时目录）
    python3 tests/gui_probe.py    # GUI 构造与控件检查（需图形环境）
    python3 tests/paned_probe.py  # 左右分栏可调宽度
    python3 tests/poll_test.py    # 位置轮询递增
    python3 tests/seek_test.py    # seek 后位置信号
    python3 tests/qq_api_test.py  # QQ音乐字段归一化与信号链路（离线，不连真实服务）

smoke_test.py 使用隔离临时目录，不会影响真实配置。

## 架构约定

1. UI 层只调用 Provider 抽象接口，不直接接触 GStreamer。
2. PlayerCore 只发送 GObject 信号，绝不直接操作 GTK 控件。
3. qqmusic.py 与本地功能完全解耦，删除该模块不影响本地播放。
4. 配置异常、单文件元数据失败、封面读取失败均不导致崩溃。
5. 功能图标按音源能力区分：本地仅显示音量，在线功能（喜欢 / 分享等）预留接口。

## 已知限制

- DSD（.dsf/.dff）当前解码到 PCM 输出，尚未实现 DoP 优先 / 硬件降级策略。
- ReplayGain / EQ 等 DSP 未实现（PlayerCore 中预留扩展点）。
- 专辑封面不做网络抓取，仅读取文件内嵌封面。
- QQ 音乐在线音源依赖本地 qq-music-api；服务未启动时搜索会提示连接失败。
- 在线曲目封面暂用占位图（未接入在线封面抓取）。
- 歌词读取本地 .lrc 或内嵌歌词，无则显示占位。
