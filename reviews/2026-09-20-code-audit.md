# 代码审查报告 — 2026-09-20

全项目体检：引用扫描 + 死代码清理 + 架构梳理。

- 审查范围：Python（core/ui/models/providers/services/config）+ Rust（audio_backend_rs/src）
- 验证方式：全项目 import 引用扫描、grep 交叉验证、冒烟测试、cargo test、实机启动

---

## 一、已完成的清理（已提交）

### Python 死代码

| 文件 | 规模 | 依据 | 提交 |
|---|---|---|---|
| core/gst_backend.py | 473 行 | 零 import，player_core 已用 RustBackend | ee956f7 |
| core/dsp_presets.py | 203 行 | 零引用，旧 effects.json 存储，已被 dsp_store.py 取代 | ee956f7 |
| ui/dsp_page.py | 50 行 | DspPage 类零引用，主窗口用 EffectPage | ee956f7 |
| tools/dev/（9 脚本） | - | 开发期一次性调试脚本 | ee956f7 |
| tools/dump_preset_yamls.py | - | 一次性工具 | ee956f7 |

### 资源与实验项目

| 项目 | 规模 | 依据 | 提交 |
|---|---|---|---|
| tools/pwrs-check/ | 152K | 仅含 Hello, world! 的实验项目 | deb2a75 |
| data/hrtf/（3 wav） | 88K | 旧 HRTF 功能遗留，代码零引用 | deb2a75 |
| Xiatiao-Player-x86_64.AppImage | 113M | 已放弃方案的产物 | 未跟踪（本地删） |

### 注释与日志

- 清理 player_core.py / rust_backend.py 中过时的「GstBackend」注释。
- advanced_dsp_window.py 裸 print 改 log.debug。
- 移除主界面背景功能的诊断日志（84d1b0c）。

### 引用同步

- .gitignore / ruff.toml / debian/rules / README.md / HANDOVER.md 中相关条目一并更新。

---

## 二、明确保留（勿删，已核实）

| 文件 | 保留原因 |
|---|---|
| core/audio_backend.py | RustBackend 的基类（PlayerState / AudioBackend 在此） |
| core/effect_state.py | 音效预设单一真相源，4 处 UI 在用 |
| core/viz.py | VizPipeline / VIZ_RATE 在用 |
| core/camilla.py | 经 from core import camilla 动态引用 |
| providers/local.py | 经 providers/__init__.py 动态发现导入 |
| image.png | splash 启动页 logo（非纯图标源图） |

---

## 三、待办 / 未处理项

### 1. Rust dead_code 警告（17 个）

多为连锁预留扩展点，HANDOVER 已说明有意保留：

- camilla_engine.rs: channels 字段、has_pipeline
- dsp/mod.rs: process_split / process_interleaved（对应 DspStage）
- dsp/params.rs: DspStage、Owner 枚举
- shared.rs: OUTPUT_LATENCY_SECS、dsp_enabled、position_secs
- output.rs / output_alsa.rs: latency、available、is_failed、MAX_CHANNELS
- ir_resample.rs: read_wav_info、resample（仅测试用）
- protocol.rs: camilla 字段、Ready/AudioInfo 变体

建议：保留。若要零警告，需逐个确认是否真为死代码，收益低。

### 2. 个别可能未使用的 Python 导入

粗检发现若干（如 core/tasks.py 的 threading、ui/settings_dialog.py 的 Gio 等），但检测脚本对类型注解有误报，未逐一改动。

建议：装 ruff 后跑一次 ruff check . 精准清理（项目已有 ruff.toml 配置）。

### 3. HANDOVER_DSD_SEEK.md（134 行）

内容已被主 HANDOVER.md 第 0 节的 DSD 部分吸收，但保留了开发过程细节。

建议：保留为过程记录，或合并进 reviews/。

### 4. DSD 直通仍未真机验证

HANDOVER_DSD_SEEK.md 记录的遗留：DSD 直通 seek、DoP/Native 实机音质需在支持 DSD 的 DAC 上验证（用户 DAC 的 DoP 有问题）。

---

## 四、项目架构逻辑

    主界面 MainWindow (GTK4 + Adw)
      ├─ 左：PlayerPanel（封面/歌词/进度/控制）
      ├─ 右：ContentArea（主页/曲库/歌单/收藏 + headerbar）
      └─ 沉浸页 NowPlayingPage（独立，封面模糊背景）

    播放链路：
      PlayerCore(壳) → RustBackend(Unix socket IPC) → Rust 后端进程
                         ↓ 信号转发
                       UI 订阅

    数据层：
      providers/local.py(扫描) → core/cache.py(缓存)
      core/*_store.py(SQLite) → core/_base_store.py(公共基类)
      config/settings.py(JSON)

    DSP：
      core/dsp_state.py(参数单一真相) → Rust dsp/ + 内嵌 camillalib
      core/effect_state.py(音效预设单一真相)

---

## 五、验证结果

| 检查 | 结果 |
|---|---|
| Python 冒烟测试 tests/smoke_test.py | 52 通过 / 0 失败 |
| Rust 单元测试 cargo test | 51 通过 / 0 失败 |
| 应用启动 | 正常，无 error/traceback |
| 托盘 + MPRIS | 正常 |
