# 虾条播放器 Xiatiao Player — 交接文档

最后更新：2026-09-19

本文档面向接手项目的开发者，覆盖架构、模块、构建、运行、测试、打包与已知问题。

---

## 1. 项目简介

虾条播放器（Xiatiao Player）是 GTK4 界面的本地音乐播放器，音频后端用 Rust 实现。

- 主要特性：本地曲库扫描、bit-perfect 直通、采样率跟随、DSD 支持、内嵌 CamillaDSP、MPRIS2、系统托盘。
- 许可证：GPL-3.0（因内嵌 CamillaDSP）。
- 应用 ID：com.xiatiao.player
- 当前版本：1.0.0

---

## 2. 架构总览

分两个进程：

- UI 与控制：Python + GTK4/Libadwaita。
- 音频：独立 Rust 进程（xiatiao-audio-backend），通过 Unix socket + JSON Lines 通信。

数据流：

    Python UI  --(IPC: Unix socket + JSON)-->  Rust 后端
    Rust 后端：解码(symphonia/ffmpeg) -> DSP(EQ/PEQ/Loudness/Camilla) -> 输出(PipeWire/ALSA)

双进程理由：音频实时性与 DSP 性能用 Rust；UI 迭代用 Python。

---

## 3. 目录结构

顶层：

- main.py：应用入口（GTK4 Application）
- config/settings.py：配置读写（~/.config/xiatiao/config.json）
- core/：核心逻辑（rust_backend / player_core / playlist / camilla / dsp_presets / 各 store / viz / i18n）
- models/：数据模型（track / coverart / lyrics / replaygain）
- providers/：音乐来源（base / local 本地曲库）
- services/：系统集成（mpris / tray / shortcuts / track_assets）
- ui/：GTK4 界面（window / player_panel / settings_dialog / 各页面 / widgets）
- data/：hrtf（空间音频 IR）/ icons（应用图标）/ desktop 文件
- audio_backend_rs/：Rust 音频后端（见第 5 节）
- debian/：Debian 打包配置
- tests/：Python 测试
- tools/：gen_icons.py + 内嵌 CamillaDSP 源码

---

## 4. Python 侧关键模块

### 4.1 IPC 客户端 core/rust_backend.py

- 启动后端：_find_binary() 在 audio_backend_rs/target/{debug,release}/ 找可执行文件，Popen 启动。
- 通信：Unix socket（默认 /tmp/xiatiao-audio-backend.sock，可用 XIATIAO_BACKEND_SOCKET 覆盖）+ JSON Lines。
- 事件读取：后台线程 _read_loop，再经 GLib.idle_add(_dispatch) 派发到主线程信号。
- 日志：后端输出写 /tmp/xiatiao-backend.log；应用日志 /tmp/xiatiao-app.log。
- 方法：play_file / pause / resume / stop / seek_seconds / set_volume / set_effect / set_dsp / set_camilla_yaml / set_engine / set_coloring / set_dsd_mode / set_output_device / list_output_devices。

注意：list_output_devices 用独立短连接直读，避免与主线程事件队列死锁。

### 4.2 播放门面 core/player_core.py

对 UI 暴露统一接口，并转发后端 GTK 信号（position-update / duration-changed / state / error-occur / audio-info 等）。

### 4.3 配置与数据位置

- 配置：~/.config/xiatiao/config.json
- 历史：~/.local/share/xiatiao/history.db
- 收藏：~/.local/share/xiatiao/liked.db
- 歌单：~/.local/share/xiatiao/playlists.db
- 缓存：~/.cache/xiatiao/cache.json

---

## 5. Rust 后端模块（audio_backend_rs/src/）

- main.rs：进程入口，Unix socket 监听，每连接一线程，IPC 循环。
- engine.rs：播放引擎，请求分发、后端切换、解码线程管理、死锁防护。
- protocol.rs：IPC 协议（Request / Event / DeviceInfo）。
- shared.rs：解码线程与输出层共享状态。
- decode.rs：symphonia 解码路径 + DSD 分流。
- decode_ffmpeg.rs：ffmpeg 子进程解码（APE/WavPack/DSD 软解等）。
- dsd.rs：DSD 原生解析（DSF/DFF 流式）+ DoP 封装 + Native 打包。
- output.rs：PipeWire 输出 + AudioOut 统一后端枚举。
- output_alsa.rs：ALSA 独占输出 + 硬件设备枚举 + 格式协商。
- dsp/：DSP 链（biquad / loudness / params / mod）。
- camilla_engine.rs：内嵌 CamillaDSP 引擎。
- viz.rs：可视化旁路（FIFO 喂数据给 UI）。
- bbe.rs / tube.rs / reverb.rs / oversample.rs：音色/混响/超采样。
- deps.rs：外部程序查找（ffmpeg/pw-cat）。
- ipc.rs：IPC 辅助。

### 5.1 输出后端选择

- 输出设备为空（自动）：走 PipeWire（系统默认）。
- 指定 hw:CARD=...,DEV=...：走 ALSA 独占（绕过音频服务）。

### 5.2 DSD 输出模式

- auto：优先直通（设备支持则 native/dop），失败降级 PCM 软解。
- native：原生 DSD 直通（需 DAC 支持 DSD_U32/U16/U8）。
- dop：DoP 封装（需 DAC 支持 DoP）。
- pcm：ffmpeg 软解为 PCM（兼容性最好）。

---

## 6. IPC 协议

传输：Unix socket，JSON Lines（每行一个 JSON）。

请求（cmd 字段）：

    ping / play{path} / pause / resume / stop / seek{seconds}
    set_volume{value} / set_effect{preset} / set_dsp{params}
    set_camilla_yaml{yaml} / set_engine{camilla} / set_coloring{tube_drive,bbe_amount}
    reset_dsp / set_dsd_mode{mode} / set_output_device{name}
    list_output_devices / query_state / shutdown

事件（event 字段）：

    ready / pong / ack{cmd} / error{message}
    position{sec} / duration{sec} / state{state} / end_of_stream
    audio_info{info} / effect{preset} / dsp{params}
    output_devices{devices:[{id,description}]}

---

## 7. 构建与运行

### 7.1 依赖（Debian/Ubuntu）

    sudo apt install python3 python3-gi python3-gi-cairo python3-numpy python3-yaml python3-mutagen \
      gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0 \
      gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      ffmpeg pipewire pipewire-bin \
      libasound2t64 libpipewire-0.3-0t64 \
      cargo rustc pkg-config libasound2-dev libpipewire-0.3-dev

### 7.2 编译 Rust 后端

    cd audio_backend_rs
    cargo build --release
    # 产物：target/release/xiatiao-audio-backend

### 7.3 运行

    python3 main.py

应用会自动在 audio_backend_rs/target/{debug,release}/ 查找后端二进制并启动。

### 7.4 环境变量

- XIATIAO_BACKEND_SOCKET：后端 socket 路径（默认 /tmp/xiatiao-audio-backend.sock）
- XIATIAO_AUDIO_BACKEND：设为 pwcat 强制用 pw-cat 子进程输出
- XIATIAO_DSP_DEBUG：非空时打印 DSP 调试信息
- XIATIAO_VIZ_FIFO：可视化 FIFO 路径覆盖

---

## 8. 测试

- Python 冒烟：python3 tests/smoke_test.py（模块导入、TrackItem、Playlist、配置、缓存，52 项）
- Python minmax：python3 tests/minmax_test.py（窗口最大化/恢复性能）
- Rust 单元：cd audio_backend_rs && cargo test（deps、DSP loudness、camilla_engine，14 项）
- Rust 构建：cargo build --release

当前状态：全部通过（Rust 14/14、Python 52/52）。

---

## 9. Debian 打包（.deb）

### 9.1 构建

    cd xiatiao-player
    dpkg-buildpackage -us -uc -b
    # 产物在上级目录：xiatiao-player_1.0.0_amd64.deb

### 9.2 打包配置（debian/）

- control：包元数据 + 依赖（含 t64 兼容）
- rules：编译 Rust 后端 + 组装安装树
- changelog / copyright / postinst / source-format
- xiatiao-player.lintian-overrides：抑制 Rust 二进制固有提示

### 9.3 安装

    sudo apt install ./xiatiao-player_1.0.0_amd64.deb

安装后：程序在 /usr/lib/xiatiao-player/，图标在 /usr/share/icons/hicolor/，启动器在 /usr/share/applications/。

---

## 10. 图标生成

源图为项目根目录 image.png（1024x1024，圆角透明）。

    python3 tools/gen_icons.py
    # 生成 data/icons/xiatiao-{16,24,32,48,64,128,256}.png + xiatiao.png

注意：gen_icons.py 对正方形源图直接缩放（不裁剪），保留圆角透明；非正方形才走裁剪居中。请勿对已处理好的圆角图做二次裁剪。

---

## 11. 已知问题与注意事项

### 11.1 硬件限制

- DoP / Native DSD 需要 DAC 支持。若 DAC 不支持（USB 描述符无 DSD 声明），DoP/Native 会输出白噪音或无声。
  - 验证方法：用成熟播放器（如 mpd 配 dop yes）对比。
  - 不支持时请用 pcm 模式（软解）。
- 固定采样率设备（如部分内置声卡固定 48kHz）无法独占播放其它采样率素材，会报错——改用“自动（系统默认）”走 PipeWire 重采样。

### 11.2 输出后端

- ALSA 独占：同一 hw 设备同一时刻只能被一个进程打开。若被 PipeWire 占用，会尝试自动释放；仍失败则报错。
- 停止释放：stop 会停止输出后端、释放设备（避免残留占用）。
- 切设备/切模式/切歌：通过 abort_write 中断卡在 write 的解码线程，避免 join 死锁。

### 11.3 图标缓存（GNOME Wayland）

更换图标后 GNOME Shell 可能缓存旧图标。注销重登是 Wayland 下刷新图标缓存的可靠方法。

### 11.4 Rust 编译警告

后端编译有约 25 个 dead-code 警告（多为 dsd.rs 的非流式辅助函数、native 路径、旧 API 保留），不影响功能。

---

## 12. 开发约定

- 改动聚焦：保持改动范围最小，遵循现有结构/命名/风格。
- IPC 兼容：新增命令/事件时，protocol.rs 与 rust_backend.py 需同步。
- 实时安全：输出 RT 回调中不加锁、不分配、不做 IO。
- 资源释放：切换/停止时确保线程 join、设备释放、子进程清理。

---

## 13. 快速上手清单

    # 1. 装依赖（见 7.1）
    # 2. 编译后端
    cd audio_backend_rs && cargo build --release && cd ..
    # 3. 跑测试
    python3 tests/smoke_test.py
    cd audio_backend_rs && cargo test && cd ..
    # 4. 运行
    python3 main.py
    # 5. 打包（可选）
    dpkg-buildpackage -us -uc -b

---

文档结束。如有疑问，请参考代码注释（各模块头部有详细说明）。



