<div align="center">

<img src="data/icons/xiatiao-128.png" alt="Xiatiao Player" width="120">

# 虾条播放器 · Xiatiao Player

**注重音质的 GTK4 本地音乐播放器** · Rust 音频后端

名字谐音「瞎调」与「虾条」

[![GTK4](https://img.shields.io/badge/GTK-4-51A2DA?logo=gtk&logoColor=white)](https://gtk.org/)
[![Rust](https://img.shields.io/badge/Rust-backend-DEA584?logo=rust&logoColor=white)](https://www.rust-lang.org/)
[![License](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Debian%20%7C%20Ubuntu-A81D33?logo=debian&logoColor=white)](#-安装)

**语言 / Language：** [中文](#中文) · [English](#english)

</div>

---

<a id="中文"></a>

## 📖 中文说明

## 📸 界面预览

<div align="center">

| 主页 · 暗色 | 主页 · 浅色 |
| :---: | :---: |
| <img src="docs/screenshots/01-home-dark.webp" width="420"> | <img src="docs/screenshots/02-home-light.webp" width="420"> |

| 背景跟随封面 | 沉浸式播放页 |
| :---: | :---: |
| <img src="docs/screenshots/03-bg-follow-cover.webp" width="420"> | <img src="docs/screenshots/04-immersive.webp" width="420"> |

| 设置 | 高级 DSP |
| :---: | :---: |
| <img src="docs/screenshots/05-settings.webp" width="420"> | <img src="docs/screenshots/06-dsp-advanced.webp" width="420"> |

</div>

### ✨ 功能特性

#### 🎧 音质优先

| 特性 | 说明 |
| --- | --- |
| **不重采样** | 保持源采样率，DSP 关闭时为 bit-perfect 直通 |
| **采样率跟随** | 原生 PipeWire 输出，DAC 跟随源文件采样率 |
| **DSD 支持** | 原生直通（Native DSD_U32）/ DoP 封装 / PCM 软解三种模式 |
| **ALSA 独占** | 可选绕过音频服务，直连 `hw:` 设备 |

#### 🎛️ 音效与 DSP

- 内嵌 **CamillaDSP** 引擎，支持参数实时调整
- 效果链：10 段图形 EQ、PEQ、低音 / 高音、动态等响度（ISO 226）、压缩器、限幅器、立体声宽度、声道平衡、Crossfeed、卷积混响、电子管染色、BBE
- 卷积 IR 按播放采样率重采样

#### 📚 播放与管理

- 本地曲库扫描：`mp3` `flac` `ape` `wv` `m4a` `ogg` `wav` `dsf` `dff`
- 解码：symphonia（原生）+ ffmpeg（高压缩格式与 DSD）
- 播放列表、歌单、收藏（我喜欢）、播放历史
- 沉浸式全屏页、滚动歌词、频谱可视化
- **主界面背景跟随封面** —— 可选让整个界面用当前封面主色调着色
- **DLNA 投送** —— 发现局域网 DLNA 渲染器，把本地曲目推送到设备播放（投送时本机不播放）
- **MPRIS2** 媒体控制、系统托盘

### 🏗️ 架构

前端 Python + PyGObject（GTK4 / libadwaita），后端 Rust 独立进程，通过 Unix domain socket + JSON Lines 通信。

    ┌─────────────────────────┐        ┌──────────────────────────┐
    │   Python (GTK4 UI)      │        │   Rust 音频后端           │
    │   main.py / ui/         │◀──IPC─▶│   audio_backend_rs/      │
    │   core/ models/ ...     │  Unix  │   decode / dsp / output  │
    │  · 界面与交互            │ socket │  · 解码（symphonia+ffmpeg）│
    │  · 曲库/歌单/收藏管理    │ + JSON │  · DSP 链（含 CamillaDSP） │
    │  · MPRIS2 / 托盘        │  Lines │  · 输出（PipeWire / ALSA） │
    └─────────────────────────┘        └──────────────────────────┘

- **音频输出**：默认 PipeWire（原生接口，采样率跟随）；可选 ALSA 独占
- **DSP**：Rust 侧实现 + 内嵌 CamillaDSP（camillalib）

### 📦 安装

#### 方式一：Debian 包（推荐）

从 Releases 下载 .deb 后：

    apt install ./xiatiao-player_1.0.3_amd64.deb

（需要管理员权限；安装后程序在 /usr/lib/xiatiao-player/，启动器 /usr/bin/xiatiao-player，也可在应用菜单中找到「虾条播放器」。）

#### 方式二：从源码运行

系统依赖（Debian / Ubuntu）：

    apt install python3-gi python3-gi-cairo python3-cairo python3-numpy \
         python3-yaml python3-mutagen gir1.2-gtk-4.0 gir1.2-adw-1 \
         gir1.2-gdkpixbuf-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
         librsvg2-common webp-pixbuf-loader \
         ffmpeg pipewire pipewire-bin pulseaudio-utils \
         iproute2 libglib2.0-bin xdg-utils dbus-bin

构建并运行：

    git clone https://github.com/usbipad/Xiatiao-Player.git
    cd Xiatiao-Player/audio_backend_rs && cargo build --release && cd ..
    python3 main.py

### 🔨 从源码打包 .deb

推荐用 **Debian 12 基线**构建。产物最高只需 GLIBC_2.34，一个包即可覆盖 Debian 12 / 13 / 14 与 Ubuntu 22.04 / 24.04 及以上：

    # 一次性：生成构建 chroot（含新版 Rust + libclang）
    bash tools/prepare_debian12_chroot.sh

    # 构建 .deb（产物输出到 release/）
    # TMPDIR 须为大磁盘目录（用 /var/tmp）
    TMPDIR=/var/tmp bash tools/build_deb_debian12.sh

为何不在本机直接 dpkg-buildpackage：较新系统（如 Debian sid，glibc 2.43）编译出的二进制要求 GLIBC_2.43，无法在 Debian 12 / Ubuntu 22.04 运行。必须在目标最低版本环境里构建，glibc 需求才会降下来。

打包配置位于 debian/：control（元信息与依赖）、rules（编译 + 组装）、changelog（版本历史）、postinst（刷新图标 / desktop 缓存）。

### 🧪 测试

    # Rust 单元测试（DSP / DSD / 输出 / Camilla 等）
    cd audio_backend_rs && cargo test --release

    # Python 冒烟测试
    python3 tests/smoke_test.py

### 🐛 调试

出问题时可通过调试入口开启诊断日志：

    bash tools/debug_run.sh            # DEBUG 级别
    bash tools/debug_run.sh verbose    # 更啰嗦（含 GTK 噪音）

详见 [docs/DEBUG.md](docs/DEBUG.md)。

### 📁 目录结构

    main.py              应用入口
    config/              配置与设置
    core/                核心逻辑（audio_backend / rust_backend / player_core / playlist / *_store / camilla）
    models/              数据模型（曲目/歌词/封面/ReplayGain）
    providers/           音源提供者
    services/            MPRIS2 / 系统托盘 / 快捷键 / 资源加载
    ui/                  GTK4 界面（window.py 主窗口）
    data/                运行时资源（图标 / desktop）
    audio_backend_rs/    Rust 音频后端
    debian/              Debian 打包配置
    docs/                文档与界面截图
    tests/               冒烟测试
    release/             发布产物（.deb，不入库）
    tools/               构建脚本（打包 / 图标生成 / 调试入口）

### 📄 许可证

**GPL-3.0**（因内嵌 CamillaDSP）。详见 [LICENSE](LICENSE)。

### 🙏 鸣谢

- [CamillaDSP](https://github.com/HEnquist/camilladsp) —— 内嵌 DSP 引擎
- [symphonia](https://github.com/pdeljanov/Symphonia) —— Rust 原生音频解码
- [FFmpeg](https://ffmpeg.org/) —— 高压缩格式与 DSD 解码
- [GTK](https://gtk.org/) / [libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) —— 界面框架

---

<a id="english"></a>

## 📖 English

**Xiatiao Player** — a GTK4 local music player focused on sound quality, with a Rust audio backend. The name is a pun on "瞎调" (messing around) and "虾条" (shrimp sticks).

### 📸 Screenshots

<div align="center">

| Home · Dark | Home · Light |
| :---: | :---: |
| <img src="docs/screenshots/01-home-dark.webp" width="420"> | <img src="docs/screenshots/02-home-light.webp" width="420"> |

| Background Follows Cover | Now Playing |
| :---: | :---: |
| <img src="docs/screenshots/03-bg-follow-cover.webp" width="420"> | <img src="docs/screenshots/04-immersive.webp" width="420"> |

| Settings | Advanced DSP |
| :---: | :---: |
| <img src="docs/screenshots/05-settings.webp" width="420"> | <img src="docs/screenshots/06-dsp-advanced.webp" width="420"> |

</div>

### ✨ Features

#### 🎧 Sound Quality First

| Feature | Description |
| --- | --- |
| **No Resampling** | Keeps the source sample rate; bit-perfect passthrough when DSP is off |
| **Sample Rate Following** | Native PipeWire output; DAC follows the source file's sample rate |
| **DSD Support** | Native passthrough (Native DSD_U32) / DoP / PCM software decode |
| **ALSA Exclusive** | Optionally bypass the audio service and connect directly to `hw:` devices |

#### 🎛️ Effects & DSP

- Embedded **CamillaDSP** engine with real-time parameter adjustment
- Effect chain: 10-band graphic EQ, PEQ, bass/treble, dynamic loudness (ISO 226), compressor, limiter, stereo width, channel balance, crossfeed, convolution reverb, tube coloration, BBE
- Convolution IR resampled to the playback sample rate

#### 📚 Playback & Management

- Local library scanning: `mp3` `flac` `ape` `wv` `m4a` `ogg` `wav` `dsf` `dff`
- Decoding: symphonia (native) + ffmpeg (high-compression formats and DSD)
- Play queue, playlists, favorites, play history
- Immersive full-screen page, scrolling lyrics, spectrum visualization
- **Background follows cover** — optionally tint the entire UI with the current cover's dominant color
- **DLNA casting** — discover LAN DLNA renderers and push local tracks to them (local playback is paused while casting)
- **MPRIS2** media control, system tray

### 🏗️ Architecture

Frontend in Python + PyGObject (GTK4 / libadwaita); backend is a separate Rust process, communicating over a Unix domain socket with JSON Lines.

    ┌─────────────────────────┐        ┌──────────────────────────┐
    │   Python (GTK4 UI)      │        │   Rust audio backend     │
    │   main.py / ui/         │◀──IPC─▶│   audio_backend_rs/      │
    │   core/ models/ ...     │  Unix  │   decode / dsp / output  │
    │  · UI & interaction     │ socket │  · decode (symphonia+ffmpeg)│
    │  · library/playlists    │ + JSON │  · DSP chain (+ CamillaDSP)│
    │  · MPRIS2 / tray        │  Lines │  · output (PipeWire/ALSA) │
    └─────────────────────────┘        └──────────────────────────┘

- **Audio output**: PipeWire by default (native interface, sample-rate following); ALSA exclusive optional
- **DSP**: implemented in Rust + embedded CamillaDSP (camillalib)

### 📦 Installation

#### Option 1: Debian package (recommended)

Download the `.deb` from Releases, then:

    sudo apt install ./xiatiao-player_1.0.3_amd64.deb

(Requires admin rights. Installed to /usr/lib/xiatiao-player/, launcher at /usr/bin/xiatiao-player, also available in the app menu as "Xiatiao Player".)

#### Option 2: Run from source

System dependencies (Debian / Ubuntu):

    sudo apt install python3-gi python3-gi-cairo python3-cairo python3-numpy \
         python3-yaml python3-mutagen gir1.2-gtk-4.0 gir1.2-adw-1 \
         gir1.2-gdkpixbuf-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
         librsvg2-common webp-pixbuf-loader \
         ffmpeg pipewire pipewire-bin pulseaudio-utils \
         iproute2 libglib2.0-bin xdg-utils dbus-bin

Build and run:

    git clone https://github.com/usbipad/Xiatiao-Player.git
    cd Xiatiao-Player/audio_backend_rs && cargo build --release && cd ..
    python3 main.py

### 🔨 Building the .deb from source

Recommended: build on a **Debian 12 baseline**. The resulting binary requires only GLIBC_2.34, so a single package covers Debian 12 / 13 / 14 and Ubuntu 22.04 / 24.04+:

    # One-time: create the build chroot (with newer Rust + libclang)
    bash tools/prepare_debian12_chroot.sh

    # Build the .deb (output to release/)
    # TMPDIR must be a large, world-executable directory (use /var/tmp)
    TMPDIR=/var/tmp bash tools/build_deb_debian12.sh

Why not build directly with dpkg-buildpackage on the host: on newer systems (e.g. Debian sid, glibc 2.43) the binary requires GLIBC_2.43 and cannot run on Debian 12 / Ubuntu 22.04. Building in the lowest target environment lowers the glibc requirement.

Packaging config lives in debian/: control (metadata & dependencies), rules (build + install tree), changelog (version history), postinst (refresh icon/desktop cache).

### 🧪 Testing

    # Rust unit tests (DSP / DSD / output / Camilla, etc.)
    cd audio_backend_rs && cargo test --release

    # Python smoke test
    python3 tests/smoke_test.py

### 🐛 Debugging

When issues occur, enable diagnostic logging via the debug entry point:

    bash tools/debug_run.sh            # DEBUG level
    bash tools/debug_run.sh verbose    # more verbose (includes GTK noise)

See [docs/DEBUG.md](docs/DEBUG.md).

### 📁 Directory Layout

    main.py              app entry point
    config/              configuration & settings
    core/                core logic (audio_backend / rust_backend / player_core / playlist / *_store / camilla)
    models/              data models (track/lyrics/cover/ReplayGain)
    providers/           music providers
    services/            MPRIS2 / system tray / shortcuts / asset loading
    ui/                  GTK4 interface (window.py main window)
    data/                runtime resources (icons / desktop)
    audio_backend_rs/    Rust audio backend
    debian/              Debian packaging config
    docs/                docs & screenshots
    tests/               smoke tests
    release/             release artifacts (.deb, not tracked)
    tools/               build scripts (packaging / icon gen / debug entry)

### 📄 License

**GPL-3.0** (due to embedded CamillaDSP). See [LICENSE](LICENSE).

### 🙏 Credits

- [CamillaDSP](https://github.com/HEnquist/camilladsp) — embedded DSP engine
- [symphonia](https://github.com/pdeljanov/Symphonia) — native Rust audio decoding
- [FFmpeg](https://ffmpeg.org/) — high-compression formats and DSD decoding
- [GTK](https://gtk.org/) / [libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) — UI framework
