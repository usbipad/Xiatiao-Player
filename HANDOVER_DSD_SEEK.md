# 交接：DSD 直通 seek 修复

新 AI 读完本文档即可接手。

## 项目
- 路径：/home/faith/项目/xiatiao-player
- 架构：Python GTK4 前端 + Rust 后端（audio_backend_rs/），Unix socket IPC
- 二进制：audio_backend_rs/target/release/xiatiao-audio-backend
- 输出：PipeWire（默认 PCM）+ ALSA 独占（PCM + DSD 直通）
- 分支：backup/pre-refactor-20260919-153633
- 用户：中文，务实，要求不迁就 bug、找根因

## 已完成（实机验证）
1. PCM 开音效 seek「记忆音频」—已修复：decode.rs seek 分支在 camilla_engine.trigger_fade() 后加 camilla_engine.clear() + set_yaml(&yaml)（重建 Camilla pipeline 丢弃卷积尾）。
2. PCM 暂停中 seek 不生效—已修复：engine.rs Seek handler 置 output.abort_write()；decode.rs seek 后 clear_abort()。
3. PCM seek 错位/空洞—已修复：输出状态机（PLAYING/DRAINING/REFILLING）+ 10ms 淡变。

## 待修：DSD 直通 seek
现象：开音效 seek → 加速；关音效 seek → 直接跳下一首。正常播放不 seek 没问题。

根因怀疑：played_frames 单位不一致。
- 写线程 output_alsa.rs:~903：played_frames.fetch_add(got)，got=f32帧数（DSD每4字节打包1个f32）
- seek 时 decode.rs:~456：alsa.reset_played_frames(out_frames)，out_frames=DSD帧
- in_rate（decode.rs:~387）：native=dsd_rate，DoP=pcm_rate
- position = played_frames / in_rate（engine.rs:~132）
- 怀疑 got（f32帧）与 in_rate（DSD帧率）单位不匹配 → position 跳变 → 跳歌

注意：A2 曾给 DSD 接 PCM 状态机，引发加速，已回退。DSD 当前不接状态机。
write_native_chunk（output_alsa.rs:543）：4字节→1f32；frames_out += (usable*8)/channels。
write_dop_chunk（output_alsa.rs:567）：每2字节→1个24bit DoP样本；marker_phase 跨块翻转。

## 诊断方法（无需用户操作）
Python 脚本：启动后端 → set_dsd_mode + set_output_device（ALSA hw）→ play DSD → seek → 录 stderr + pw-record monitor 波形 → 分析 played_frames/position/eof。

DSD 测试文件：/mnt/D1333F9E1DE59718/音乐/DANA WINNER - Unforgettable (2001) [SACD]/*.dsf
monitor source：alsa_output.usb-Headset_Meizu_Corp_Meizu_HiFi_DAC_Headphone_Amplifier_PRO-00.analog-stereo.monitor

IPC 命令：play/seek/pause/resume/set_dsd_mode/set_output_device/set_dsp/set_camilla_yaml/shutdown（JSON 行协议）

Camilla YAML 格式：capture/playback 在 devices 下，缩进 2 空格（用 python3 -c "from core import camilla; ..." 生成最稳）。

## 环境
Debian, PipeWire 1.6.8。默认 sink：USB DAC（Meizu HiFi）。工具：pw-record/ffprobe/aplay/pactl/pw-cli。

## 规范
- 改前备份：git branch backup/xxx-$(date +%Y%m%d-%H%M%S) + tag + /tmp/*.patch
- 每步：cd audio_backend_rs && cargo build --release && cargo test --release；python3 tests/smoke_test.py
- 不擅自删用户文件
- 终端用 execute_command；不要用 pkill -f（误杀 webcode shell）

## 备份
分支 backup/a1a2-20260920-0044, backup/a1a2-20260920-004425
tag backup-a1a2-20260920-0044, backup-a1a2-20260920-004425
patch /tmp/xiatiao-a1a2-20260920-004427.patch

## 未提交改动
M Cargo.lock/Cargo.toml/decode.rs/decode_ffmpeg.rs/engine.rs/output.rs/output_alsa.rs
?? diag_mem.py, tools/analyze_mem.py（诊断脚本，可删）

建议：新对话先 git add -A && git commit 固化 PCM 修复，再查 DSD。
