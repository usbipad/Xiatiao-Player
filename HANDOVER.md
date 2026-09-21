# 虾条播放器 Xiatiao Player — 交接文档

最后更新：2026-09-20

本文档面向接手项目的开发者，覆盖架构、模块、构建、运行、测试、打包与已知问题。

> 说明：本文档已按**当前代码实际状态**校对（不再依赖早期设计描述）。
> 若与代码有出入，以代码为准。

---

## 0. 最近更新（2026-09-20）

本轮聚焦 **DSD 直通/软解** 与 **UI 响应式** 两大块。要点：

### DSD
- **直通路径按标准重写**：
  - DSF 数据区是「每声道 block_size（典型 4096）字节、块间交错」布局，
    `DsdReader::read_group()` 按此解交错（旧代码当字节流直读 → 左右错乱/杂音）。
  - Native（DSD_U32_LE）：每 4 字节→1 个 32-bit 采样，**ALSA 设备采样率 = dsd_rate/32**
    （旧代码用 dsd_rate，导致能力探测恒失败）。
  - DoP v1.0：每 2 字节→1 个 24-bit 样本（S32_LE / **S24_3LE** 承载），marker 每帧交替。
  - 打包纯函数 `dsd::pack_native_group` / `pack_dop_group` 有单元测试。
- **软解路径（ffmpeg）seek 修复**：清 carry / clear_abort / 重置 DSP+重建 Camilla /
  EOF 有限重试；`just_seeked` 移到 loop 外（跨迭代保持）。
- seek 换算统一到 ALSA 采样帧（native=/32, dop=/16），与 `played_frames` 单位一致。

### UI / 响应式
- 左侧面板改为 **`Adw.OverlaySplitView`**：宽度**线性**跟随窗口（fraction 0.28，
  钳制 280~520），**不随窗口自动折叠**（仅 headerbar 按钮手动显隐）。
- **窗口最小宽度 = 296**（= 左侧面板完整宽度 280 + margin 16），缩到最窄只剩播放面板。
  关键：`Adw.ApplicationWindow` 自带 360px 默认最小宽度，需 `set_size_request` 覆盖。
- **启动 splash 修复**：超时兜底强制生效（此前被封面轮询挡住），MAX_MS 5s→1.5s。
- **全屏页响应式**：`_apply_responsive()` 按可用宽高缩放封面/间距/歌词。
  注意：`Gtk.Widget.add_tick_callback` 回调签名是 **(widget, clock) 仅 2 参**
  （曾误写 3 参 → TypeError 静默失败 → 响应式不生效）。
- 播放/暂停按钮改**扁平**（去掉强调色实心圆），图标尺寸用 CSS `-gtk-icon-size`
  （避免 `set_pixel_size` 使 symbolic 图标变形）。
- 过滤 GTK 层 `GtkOverlay ... exceeds ... width` 噪音警告（`main.py`，保留其它警告）。

### 工程化
- 依赖分级：必需留 `Depends`，增强项（numpy/mutagen/yaml/ffmpeg/gstreamer）移到 `Recommends`。
- **打包策略转向**：放弃 AppImage 跨发行版分发（半自包含，glibc/gi 问题无解），
  改为**按 glibc 基线的原生 `.deb`**。新增 `tools/prepare_debian12_chroot.sh`
  （建 Debian 12 chroot，含 rustup 新版 Rust + libclang-dev）与
  `tools/build_deb_debian12.sh`（在 Debian 12 基线构建 .deb）。
  产物最高只需 **GLIBC_2.34**，覆盖 Debian 12/13 + Ubuntu 22.04/24.04+。
  详见第 9 节。
- 抽取 SQLite store 公共基类 `core/_base_store.py`。
- 新增 `ruff.toml`、`audio_backend_rs/rustfmt.toml`、`requirements.txt`。
- 新增 `tools/build_appimage.sh`（半自包含：打包代码+后端，GTK 依赖系统）。

### UI：主界面背景跟随封面（新增功能）
- 设置 → 外观新增开关 `main_bg_follow_cover`（默认关）。开启后整个主界面
  （窗口根 / 左侧播放面板 / 右侧内容区 / 顶部 headerbar / 列表 / 网格）
  统一用当前封面主色调着色。
- **取色算法**：主界面用新增的 `models.coverart.tint_for_background` ——
  **保留封面原始饱和度**（按比例压制 + 封顶），使淡封面→淡背景、艳封面→柔和背景。
  区别于沉浸页的 `lighten_for_background`（强制固定饱和度，会把淡色拉成纯色）。
- **实现要点（含多处踩坑修复）**：
  - 背景色在切歌后台线程算好（`services/track_assets.py` 的 `main_bg_rgb`，
    独立于沉浸页模糊开关），主线程只注入 CSS，零额外解码。
  - 用**动态 CssProvider + `PRIORITY_USER`**（`ui/window.py` 的 `_apply_main_bg`）
    注入，规则全部限定在 `.content-area` 内，避免污染沉浸页/菜单。
  - **不要覆盖 `--view-bg-color` / `--window-bg-color` 全局变量**：它们被所有
    后代继承，会导致沉浸页歌词、右键菜单、设置按钮异常变白。
  - libadwaita 默认给 `listview/list` 设了不透明 `view-bg-color`，须显式覆盖
    `columnview/listview/gridview/flowbox` 及 `columnview > header` 各状态
    （含 `:hover/:active/:checked`）。
  - **popover 是独立顶层窗口**，不受 `window .xxx` 祖先选择器影响，须用全局
    `popover.media-menu > contents`（`.media-menu` 为本项目菜单专用类）。
  - 左上歌词区 `.np-fade-top/.np-fade-bottom` 渐隐原用 `@window_bg_color`（白），
    跟随模式下改用封面色渐变。

### 工程化（续）
- **死代码清理**：经全项目引用扫描后删除 `core/gst_backend.py`（GStreamer 后端）、
  `core/dsp_presets.py`（旧 effects.json 存储）、`ui/dsp_page.py`、`tools/dev/`、
  `tools/dump_preset_yamls.py`。清理过时注释与裸 print。
- 保留：`core/audio_backend.py`（`RustBackend` 的基类，勿删）、`core/viz.py`、
  `core/effect_state.py`。Rust 侧 dead_code 警告多为**预留扩展点**，保留。
- 完整 `README.md`（含 5 张界面截图，存 `docs/screenshots/`）。


---

## 1. 项目简介

虾条播放器（Xiatiao Player）是 GTK4 界面的本地音乐播放器，音频后端用 Rust 实现。

- 主要特性：本地曲库扫描、bit-perfect 直通、采样率跟随、DSD 支持、内嵌 CamillaDSP、MPRIS2、系统托盘。
- 许可证：GPL-3.0（因内嵌 CamillaDSP）。
- 应用 ID：com.xiatiao.player
- 当前版本：1.0.1

---

## 2. 架构总览

分两个进程：

- UI 与控制：Python + GTK4/Libadwaita。
- 音频：独立 Rust 进程（xiatiao-audio-backend），通过 Unix socket + JSON Lines 通信。

数据流：

    Python UI  --(IPC: Unix socket + JSON)-->  Rust 后端
    Rust 后端：解码(symphonia/ffmpeg) -> DSP(Rust 内置链 + 内嵌 CamillaDSP) -> 输出(PipeWire 或 ALSA 独占)

双进程理由：音频实时性与 DSP 性能用 Rust；UI 迭代用 Python。

---

## 3. 目录结构

顶层：

- main.py：应用入口（GTK4 Application）
- config/settings.py：配置读写（~/.config/xiatiao/config.json）
- core/：核心逻辑（rust_backend / player_core / playlist / camilla / dsp_store / eq_presets / 各 store / viz / i18n）
- models/：数据模型（track / coverart / lyrics / replaygain）
- providers/：音乐来源（base / local 本地曲库）
- services/：系统集成（mpris / tray / shortcuts / track_assets）
- ui/：GTK4 界面（window / player_panel / settings_dialog / advanced_dsp_window / effect_page / 各页面 / widgets）
- data/：icons（应用图标）/ desktop 文件
- audio_backend_rs/：Rust 音频后端（见第 5 节）
- debian/：Debian 打包配置
- tests/：Python 测试
- tools/：gen_icons.py + 打包脚本（build_deb_debian12.sh / prepare_debian12_chroot.sh / build_appimage.sh）+ camilladsp-src（内嵌 CamillaDSP 源码，编译进 Rust 后端）

---

## 4. Python 侧关键模块

### 4.1 IPC 客户端 core/rust_backend.py

- 启动后端：_find_binary() 在 audio_backend_rs/target/{debug,release}/ 找可执行文件，Popen 启动。
- 通信：Unix socket（默认 /tmp/xiatiao-audio-backend.sock，可用 XIATIAO_BACKEND_SOCKET 覆盖）+ JSON Lines。
- 事件读取：后台线程 _read_loop，再经 GLib.idle_add(_dispatch) 派发到主线程信号。
- 日志：后端输出写 /tmp/xiatiao-backend.log；应用日志 /tmp/xiatiao-app.log。
- 方法：play_file / pause / resume / stop / seek_seconds / set_volume / set_effect / set_dsp / set_camilla_yaml / set_engine / set_coloring / set_dsd_mode / set_output_device / list_output_devices。

注意：list_output_devices 用**独立一次性短连接**直读响应，绕开主线程事件队列。
若复用主连接 + 等 event，在 UI 主线程调用会与 GLib.idle_add 派发互相阻塞，导致下拉永远为空。

### 4.2 播放门面 core/player_core.py

对 UI 暴露统一接口，并转发后端 GTK 信号（position-update / duration-changed / state / error-occur / audio-info 等）。

`_create_default_backend()` 返回 `RustBackend`（core/rust_backend.py）。
`core/audio_backend.py` 提供 `AudioBackend` 抽象基类与 `PlayerState` 常量，
`RustBackend` 继承它——**该文件勿删**。

### 4.3 配置与数据位置

- 配置：~/.config/xiatiao/config.json
- DSP 自定义预设：~/.config/xiatiao/dsp_presets.json
- 历史：~/.local/share/xiatiao/history.db
- 收藏：~/.local/share/xiatiao/liked.db
- 歌单：~/.local/share/xiatiao/playlists.db
- 缓存：~/.cache/xiatiao/cache.json

config.json 中与音频相关的关键项：

- `dsp_params`：完整 DSP 参数 dict（持久化副本）
- `dsp_enabled`：全局 DSP 开关（Rust 内置链）
- `camilla_enabled`：Camilla 引擎开关（注意：Rust 侧实际参与与否由归属表推导，见 5.3）
- `audio_effect`：当前音效键
- `effect_preset`：当前选中音效预设名（仅用于对话框高亮）
- `dsd_output_enabled`：DSD 输出总开关（关=走老逻辑，DSD 经 ffmpeg 软解为 PCM + PipeWire）
- `dsd_output_mode`：DSD 输出模式（auto/native/dop/pcm）
- `output_device`：输出设备名（**ALSA hw 设备名**；空=系统默认走 PipeWire）
- `convolution_ir`：已加载的卷积 IR 路径（UI 侧记录）

---

## 5. Rust 后端模块（audio_backend_rs/src/）

- main.rs：进程入口，Unix socket 监听，每连接一线程，IPC 循环。
- engine.rs：播放引擎，请求分发、后端切换、解码线程管理、死锁防护。
- protocol.rs：IPC 协议（Request / Event / DeviceInfo）。
- shared.rs：解码线程与输出层共享状态。
- decode.rs：symphonia 解码路径 + DSD 分流（读取 dsd_mode 决定 native/dop/pcm）。
- decode_ffmpeg.rs：ffmpeg 子进程解码（APE/WavPack/DSD 软解等）。
- dsd.rs：DSD 原生解析（DSF/DFF 流式）+ DoP 封装 + Native 打包。
- output.rs：PipeWire 输出 + AudioOut 统一后端枚举。
- output_alsa.rs：ALSA 独占输出 + 硬件设备枚举 + 格式协商 + DSD(DoP/Native) 模式。
- dsp/：Rust 内置 DSP 链（biquad / loudness / params / mod）。
- camilla_engine.rs：内嵌 CamillaDSP 引擎。
- viz.rs：可视化旁路（FIFO 喂数据给 UI）。
- bbe.rs / tube.rs / reverb.rs / oversample.rs：音色/混响/超采样。
- deps.rs：外部程序查找（ffmpeg/pw-cat）。
- ipc.rs：IPC 辅助。

### 5.1 输出后端选择

- `output_device` 为空（自动）：走 **PipeWire**（系统默认）。
- `output_device` 非空：切到 **ALSA 独占**（绕过音频服务）。
  - 设备名格式为 ALSA hw 名，如 `hw:CARD=Amplif,DEV=0`（见 engine.rs 单元测试）。
  - 注意：`shared.rs` 中该字段注释写的是「PipeWire sink 名」，属**注释过时**；以 engine.rs 的
    `apply_output_backend` 实际行为为准（ALSA hw 名）。
- 设备枚举：后端 `ListOutputDevices` 返回的是 **ALSA 硬件设备**（`output_alsa::list_devices()`），
  而非 PipeWire sink。UI 侧若用 `pactl list sinks` 枚举会与后端期望不一致。

### 5.2 DSD 输出模式

`shared.dsd_mode` 会被 **decode.rs 与 output_alsa.rs 实际消费**（不是只存不用）：

- auto：优先直通（设备支持则 native/dop），失败降级 PCM 软解。
- native：原生 DSD 直通（需 DAC 支持 DSD_U32/U16/U8）。
- dop：DoP 封装（需 DAC 支持 DoP）。
- pcm：ffmpeg 软解为 PCM（兼容性最好）。

**切换行为**：切模式时若当前正在播放 DSD 文件，会**重载当前文件并从原位置续播**，
使新模式对已播放的流真正生效（否则用户会以为「没生效」）。见 engine.rs 的 SetDsdMode 分支。

### 5.3 DSP 功能归属（Camilla vs Rust）—— 重要机制

Rust 与内嵌 CamillaDSP 是**两条并行 DSP 路径**。某功能由谁处理，由归属表决定：

- 定义位置：`dsp/params.rs` 的 `camilla_should_engage()` / `camilla_features()`。
- 规则：**功能重叠时优先 Camilla**；Rust 只做 Camilla 没有的。
- 归 Camilla 的功能：pre_gain、eq、peq、convolution、bass、treble、compressor、channel_matrix、phase_invert。
- 归 Rust 独有的功能：混响(reverb)、tube、BBE、宽度(width)、平衡(balance)、crossfeed、
  ReplayGain、**Loudness（动态等响度，依赖播放器音量，Camilla 拿不到音量）**。

**Camilla 是否参与 = `camilla_should_engage(params)`**（「启用 DSP」总开关关闭时一律不参与），
**不是**靠 params 里的 `camilla_enabled` 字段。

`Request::SetEngine` 命令在 engine.rs 中**仅占位（Ack 后不做实际切换）**；Camilla 参与由上述归属表自动推导。

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

**命令语义要点（对齐代码）：**

- `set_output_device{name}`：name 为 ALSA hw 设备名；空=默认走 PipeWire。
- `set_engine{camilla}`：仅占位，实际参与由后端按归属表推导（见 5.3）。
- `set_dsd_mode{mode}`：会重载当前 DSD 文件以生效。
- **卷积 IR 没有独立 IPC 命令**：卷积通过 `set_dsp` 里的 `convolution_*` 参数生效；
  参数由 `core/camilla.py` 的 `build_config()` 生成 Camilla YAML。
  （protocol.rs 中残留的「加载/清除卷积 IR」空注释无对应 variant，属历史遗留。）
- `reset_dsp`：后端重置为默认参数并回发 `dsp{params}`。

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
- Rust 单元：cd audio_backend_rs && cargo test（deps、DSP loudness、camilla_engine、engine 的 output_device_changed 等）
- Rust 构建：cargo build --release

---

## 9. Debian 打包（.deb）

> **跨发行版策略（2026-09-20 确立）**：放弃 AppImage 跨发行版分发，改为**按
> glibc 基线构建原生 `.deb`**。用 Debian 12（glibc 2.36）作为最低基线，一个包即可
> 覆盖 Debian 12/13 + Ubuntu 22.04/24.04 及更新的 Debian 系。
> 原因：AppImage 内嵌的 ffmpeg/Rust 后端仍需宿主 glibc，无法真正自包含；而原生 `.deb`
> 依赖交给 apt，包小且无 glibc 问题。

### 9.1 构建（推荐：跨版本基线）

    # 一次性准备：生成 Debian 12 chroot（含新版 Rust + libclang）
    bash tools/prepare_debian12_chroot.sh

    # 构建 .deb（在 Debian 12 基线里编，产物最高只需 GLIBC_2.34）
    bash tools/build_deb_debian12.sh
    # 产物：xiatiao-player_1.0.1_amd64.deb（覆盖 Debian 12+ / Ubuntu 22.04+）

**为什么不能直接用 `dpkg-buildpackage`**：本机（Debian sid）glibc 2.43，直接编出的
Rust 后端要求 GLIBC_2.43，装到 Debian 12/Ubuntu 22.04 会因缺符号启动失败。
必须在**目标最低版本的环境**里编，glibc 需求才会降下来。

**关键实现点**（`tools/` 脚本 + `debian/` 配置）：
- 用 `sbuild --chroot-mode=unshare`（无需 root）+ `mmdebstrap` 建 chroot。
- chroot 内用 **rustup** 装新版 Rust（Debian 12 自带的 1.63 无法解析 `Cargo.lock` v4，
  且 `tools/camilladsp-src` 要求 rustc ≥ 1.90）。Rust 版本只影响能否编译，
  **产物 glibc 由 chroot 的 libc 决定**。
- `cargo vendor` 把依赖固化进 `audio_backend_rs/vendor/`，构建离线可复现。
- chroot 需含 `libclang-dev`（`libspa-sys` 的 bindgen 依赖 libclang）。
- `debian/rules` 三处关键改动：
  - 注入 `PATH=/usr/local/cargo/bin:...` 使用 rustup 的 Rust；
  - `override_dh_clean` 用 `dh_clean -d`，避免误删 vendor 的 `Cargo.toml.orig`；
  - 声明 `binary-arch: build-arch` 依赖（否则 `dpkg-buildpackage` 只跑 binary，不编译）。
- `debian/source/options` 用 `tar-ignore` 排除构建产物/AppImage，源码包约 28 MB（含 vendor）。

**sbuild 临时目录要求（踩过的坑）**：
- sbuild 的 unshare 模式用 `$TMPDIR/tmp.sbuild.XXXXXXXXXX` 解包 chroot，
  **必须在足够大的磁盘 + 路径各层 world-executable**。
- 本机 `/tmp` 是 tmpfs（6.7G 内存盘），解 1.8G chroot 会满；
  `$HOME` 是 700，unshared user（uid 100000）进不去 → session 创建失败。
- **正解**：用 `/var/tmp`（根盘 + 1777 world-writable）：

      TMPDIR=/var/tmp bash tools/build_deb_debian12.sh

**tar-ignore 陷阱（务必注意）**：
- 不要写裸的 `tar-ignore = release`——tar 的 `--exclude` 会匹配**任意路径中**
  名为 `release` 的目录，误伤 `vendor/yaml_serde/util/release`（crate 自带的
  发版脚本，在 `.cargo-checksum.json` 中登记，漏了会导致 cargo 校验失败）。
- `release/` 产物里的 `*.deb` 已由 `tar-ignore = *.deb` 排除，无需额外规则。

### 9.1b 构建（本机快速，仅限本机/最新系统）

    cd xiatiao-player
    dpkg-buildpackage -us -uc -b
    # 产物要求 GLIBC_2.43，只能在 Debian sid / 最新系统运行

### 9.2 打包配置（debian/）

- control：包元数据 + 依赖（含 t64 兼容）。`Build-Depends` 含 `libclang-dev`。
- rules：编译 Rust 后端 + 组装安装树（含 rustup PATH、dh_clean -d、target 依赖）。
- source/options：`tar-ignore` 排除规则（native 格式下 `.gitignore` 不生效）。
- changelog / copyright / postinst。
- xiatiao-player.lintian-overrides：抑制 Rust 二进制固有提示。

### 9.3 安装

    sudo apt install ./xiatiao-player_1.0.1_amd64.deb

安装后：程序在 /usr/lib/xiatiao-player/，图标在 /usr/share/icons/hicolor/，启动器在 /usr/share/applications/。

### 9.4 依赖分级（control）

> **2026-09-21 起：全部依赖并入 `Depends`**（不再分 Recommends）。
> 目的：一条 `apt install` 即装齐所有功能依赖，杜绝「装了主包但缺增强项
> 导致功能不全」。代价：apt 列表略长，但保证开箱即用。

- `Depends`（全部必需）：python3 (>=3.10)、python3-gi、python3-gi-cairo、
  gir1.2-gtk-4.0、gir1.2-adw-1、gir1.2-gdkpixbuf-2.0、
  libasound2t64 | libasound2、libpipewire-0.3-0t64 | libpipewire-0.3-0、
  python3-mutagen、python3-numpy、python3-yaml、
  gir1.2-gstreamer-1.0、gir1.2-gst-plugins-base-1.0、
  ffmpeg、pipewire、pipewire-bin、pulseaudio-utils、python3-setproctitle。
- `Build-Depends`：debhelper-compat、cargo、rustc、pkg-config、libasound2-dev、
  libpipewire-0.3-dev、libclang-dev。

**各依赖用途**：

| 依赖 | 用途 |
|---|---|
| python3 / -gi / -gi-cairo | 解释器 + PyGObject + cairo 绑定 |
| gir1.2-gtk-4.0 / -adw-1 / -gdkpixbuf-2.0 | GTK4 / libadwaita / 图像 typelib |
| libasound2 / libpipewire-0.3-0 | Rust 后端链接的系统库 |
| python3-mutagen | 音频标签 + 内嵌封面 |
| python3-numpy | 频谱 FFT 加速 |
| python3-yaml | Camilla YAML 生成 |
| gir1.2-gstreamer-1.0 / -gst-plugins-base-1.0 | 元数据补全 |
| ffmpeg | DSD / APE / WavPack 软解 |
| pipewire / pipewire-bin | 音频服务 + `pw-cat` 子进程输出 |
| pulseaudio-utils | `pactl`（切 ALSA 独占前释放设备）|
| python3-setproctitle | 改 cmdline（htop 显示应用名）|

#### 9.4.0 图形栈自动展开（GTK 本体不单独声明）

`control` 只声明直接依赖 `gir1.2-gtk-4.0`，GTK 本体由 apt 递归解析自动装：

    我声明                  自动拉入
    gir1.2-gtk-4.0      →   libgtk-4-1（GTK4 本体）→ libpango/libharfbuzz/
                            libepoxy/libgdk-pixbuf… 一整套图形栈
    gir1.2-adw-1        →   libadwaita-1-0
    gir1.2-gdkpixbuf-2.0→   libgdk-pixbuf-2.0-0
    python3-gi-cairo    →   libcairo2 / python3-cairo

- **不写 `libgtk-4-1`**：Debian 规范要求声明直接依赖，本体由 apt 递归解析。
- **不内嵌 GTK**：依赖图形驱动层（libGL/dri），与宿主内核死绑，内嵌会花屏/崩。
- **Rust 后端不链接任何 GTK/图形库**（仅 libasound / libpipewire / libc）。

#### 9.4.1 t64 命名差异（重要，勿改错）

2024 年起 Debian/Ubuntu 做 **64 位 time_t 过渡**，同名库分两代命名：

| 库 | 旧命名（≤2023） | 新命名（t64） |
|---|---|---|
| ALSA | `libasound2` | `libasound2t64` |
| PipeWire | `libpipewire-0.3-0` | `libpipewire-0.3-0t64` |

**是同一份库，ABI 从 32 位时间戳换 64 位，故包名加 t64 后缀。**

各发行版实际命名：

| 发行版 | ALSA | PipeWire |
|---|---|---|
| Debian 12 (bookworm) | libasound2 | libpipewire-0.3-0 |
| Debian 13 (trixie) | libasound2t64 | libpipewire-0.3-0t64 |
| Debian forky | libasound2t64 | libpipewire-0.3-0t64 |
| Ubuntu 22.04 (jammy) | libasound2 | libpipewire-0.3-0 |
| Ubuntu 24.04 (noble) | libasound2t64 | libpipewire-0.3-0t64 |

`control` 用 **`A | B`（或）语法**同时声明两代，apt 会自动选存在的那个：

    libasound2t64 | libasound2,
    libpipewire-0.3-0t64 | libpipewire-0.3-0

**勿改成单一命名**，否则另一半发行版装不上。

#### 9.4.2 依赖存在性审计（2026-09-21）

逐包核对 Debian 12/13/forky 与 Ubuntu 22.04/24.04 的仓库：

- **全部依赖均存在，零缺口**（含 `python3-setproctitle`、`pulseaudio-utils`）。
- 命名差异仅上表两个库，已由或语法覆盖。
- 其余依赖（gtk/gi/mutagen/numpy/yaml/gstreamer/ffmpeg/pipewire/pactl）在各版本命名一致。

#### 9.4.3 各依赖的用途

- `pulseaudio-utils`（pactl）：切 ALSA 独占前释放被 PipeWire 占用的设备
  （`output_alsa.rs`）。缺失不崩，但独占可能打不开。
- `python3-setproctitle`：改 `/proc/PID/cmdline`，htop/top 显示应用名而非 python3。
- `ffmpeg`：DSD/APE/WavPack 软解（子进程调用）。缺失则这些格式放不了。

### 9.5 跨发行版说明（AppImage 已放弃）

- **AppImage 方案已放弃**（`tools/build_appimage.sh` 保留但不再发布）：
  - 内嵌的 ffmpeg / Rust 后端**仍需宿主 glibc**（实测内嵌 ffmpeg 要求 GLIBC_2.38、
    后端 2.43），旧系统照样崩；
  - gi/GTK 路径写死 Debian（`/usr/lib/python3/dist-packages`），Fedora/Arch 上 `import gi` 失败；
  - 内嵌库闭包不全（169 个 vs 实际 171+，且 dlopen 插件 ldd 看不到）。
- **非 Debian 系（Fedora/Arch/openSUSE）**：当前不提供原生包。若将来需要，
  建议用 **Flatpak**（一套 runtime 通吃，且不碰驱动层），而非 AppImage。
- **`.deb` 覆盖范围**：GLIBC_2.34 → Debian 12/13、Ubuntu 22.04/24.04 及更新。
  若要覆盖更老（如 Ubuntu 20.04），需换更低基线（Debian 11，glibc 2.31）重建 chroot。

---

## 10. 图标生成

源图为项目根目录 image.png（1024x1024，圆角透明）。

    python3 tools/gen_icons.py
    # 生成 data/icons/xiatiao-{16,24,32,48,64,128,256}.png + xiatiao.png

注意：gen_icons.py 对正方形源图直接缩放（不裁剪），保留圆角透明；非正方形才走裁剪居中。请勿对已处理好的圆角图做二次裁剪。

---

## 11. DSP 参数流转与预设（当前实现现状）

> 本节记录当前实现，供理解与后续优化参考。

### 11.1 参数的多个副本

DSP 参数（扁平 dict）在内存/磁盘中存在**多个副本**，改动时需手动同步：

- `MainWindow.dsp_page._params`：主窗口内一个「隐形」EffectPage（不当页面显示，仅作参数宿主）。
- `SettingsWindow._effect_page._params`：设置窗口的音效页。
- `AdvancedDspWindow._params`：高级窗口的总参数。
- `AdvancedDspWindow._feature_pages[*]._params`：高级窗口内每个功能页各持一份快照。
- `config.json` 的 `dsp_params`：持久化副本。

各视图通过散落的 `_sync_*` 方法刷新（`_sync_all_switches` / `_sync_extra_switches` /
`_sync_sliders` / `_sync_convolution_ui` / `_update_camilla_sensitivity` /
`_update_coloring_sensitivity`）。**漏调其中任何一个就会导致 UI 与参数不一致**，
这是多起「加载预设后 UI 不同步」问题的共同根因。

### 11.2 三套「预设」概念

1. **内置音效预设**：`core/eq_presets.py` 的 `BUILTIN_PRESETS`（硬编码声学调音）。
2. **自定义预设**：`core/dsp_store.py` 的 `DspPresetStore`（存 dsp_presets.json）。
3. **`effect_preset` 标记**：config 中一个字符串，仅用于音效对话框高亮。

主界面音效对话框选择 → `MainWindow._on_effect()`：先查内置预设，再查自定义预设。
高级窗口「预设管理」→ 加载/保存自定义预设（`AdvancedDspWindow._on_preset_load/save`）。

### 11.3 下发链路

    UI 改动 → EffectPage._emit()
      → MainWindow._on_dsp_changed(params)
          ├─ player.set_dsp(params)                # Rust 内置链参数
          ├─ cfg.set("dsp_params", params)         # 持久化
          ├─ cfg.set("effect_preset", ...)         # 高亮标记
          ├─ 防抖 80ms → camilla.build_config() → player.set_camilla_yaml()  # Camilla
          └─ 手动同步 dsp_page 的 UI

### 11.4 加载预设的语义（现状）

`AdvancedDspWindow._on_preset_load` 采用「**默认基底 + 预设覆盖**」：
预设里没有的功能回到默认（关闭），避免预设未启用却显示为开启。
随后手动刷新各功能页 UI（含卷积、染色）。

---

## 12. 已知问题与注意事项

### 12.1 硬件限制

- DoP / Native DSD 需要 DAC 支持。若 DAC 不支持（USB 描述符无 DSD 声明），DoP/Native 会输出白噪音或无声。
  - 验证方法：用成熟播放器（如 mpd 配 dop yes）对比。
  - 不支持时请用 pcm 模式（软解）。
- 固定采样率设备（如部分内置声卡固定 48kHz）无法独占播放其它采样率素材，会报错——改用「自动（系统默认）」走 PipeWire 重采样。

### 12.2 输出后端

- ALSA 独占：同一 hw 设备同一时刻只能被一个进程打开。若被 PipeWire 占用，会尝试自动释放；仍失败则报错。
- 停止释放：stop 会停止输出后端、释放设备（避免残留占用）。
- 切设备/切模式/切歌：通过 abort_write 中断卡在 write 的解码线程，避免 join 死锁。

### 12.3 文档/注释过时点（已按代码校对）

- `shared.rs` 中 `output_device` 注释写「PipeWire sink 名」，实际为 ALSA hw 名。
- `protocol.rs` 中卷积 IR 相关空注释无对应 variant。
- （`player_core.py` / `rust_backend.py` 的 GstBackend 过时注释已清理。）

### 12.4 图标缓存（GNOME Wayland）

更换图标后 GNOME Shell 可能缓存旧图标。注销重登是 Wayland 下刷新图标缓存的可靠方法。

### 12.6 GTK 噪音警告

缩窗口时 GTK 会打印 `GtkOverlay ... exceeds MainWindow width` 警告（界面正常，
仅日志噪音）。已在 `main.py` 用 `GLib.log_set_handler` 过滤该条，其它警告保留。

### 12.7 主题差异

界面用 libadwaita 变量（@accent_bg_color 等），实际观感随系统 GTK/Adw 主题变化。
播放按钮已改为扁平（不依赖强调色），跟随主题前景色。

### 12.5 Rust 编译警告

后端编译有若干 dead-code 警告（多为预留 API：latency/is_failed/has_pipeline、
DSP 分阶段处理、协议预留事件等）。已清理掉可清的（多余括号/未用 mut/未读赋值等），
剩余为有意保留的扩展点。dsd.rs 的内存读取路径已标 `#[allow(dead_code)]` 并注释。

---

## 13. 开发约定

- 改动聚焦：保持改动范围最小，遵循现有结构/命名/风格。
- IPC 兼容：新增命令/事件时，protocol.rs 与 rust_backend.py 需同步。
- 实时安全：输出 RT 回调中不加锁、不分配、不做 IO。
- 资源释放：切换/停止时确保线程 join、设备释放、子进程清理。
- 文档同步：本文件如与代码不符，以代码为准，并顺手更新本文件。

---

## 14. 快速上手清单

    # 1. 装依赖（见 7.1）
    # 2. 编译后端
    cd audio_backend_rs && cargo build --release && cd ..
    # 3. 跑测试
    python3 tests/smoke_test.py
    cd audio_backend_rs && cargo test && cd ..
    # 4. 运行
    python3 main.py
    # 5. 打包（可选）
    dpkg-buildpackage -b -us -uc            # deb
    bash tools/build_appimage.sh            # AppImage（半自包含）

---

文档结束。如有疑问，请参考代码注释（各模块头部有详细说明）。
