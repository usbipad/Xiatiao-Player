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

## 已修：DSD 软解（pcm 模式）seek —— 已验证
用户实际配置为 `set_dsd_mode=pcm`（手动选，因 DAC 的 DoP 有问题），
所有现象都走 decode_ffmpeg.rs 的 ffmpeg 路径，**不是** decode.rs 的直通路径。
现象：seek → 加速 / 没声音 / 跳下一首（开音效更明显）。

根因（decode_ffmpeg.rs，共 4 处）：
1. seek 后未清 `carry`：旧位置残留的非 4 字节尾部拼到新数据开头 → 样本错位
   （听起来像加速/杂音）。
2. seek 后未重置 DSP 状态 / 未重建 Camilla pipeline：卷积尾残留 →
   「记忆音频」（开音效时出现，与之前 decode.rs 修的是同一类问题）。
3. seek 后未 `output.clear_abort()`：engine Seek handler 置了 abort_write 后，
   ffmpeg 路径不清 → output.write 立即返回 → 新数据写不进去 → 静音。
4. `just_seeked` 声明在 loop 内 + mark_playing 只在 output.write 分支：
   EOF 重试失效 / pwcat 分支状态机永久停在 Refilling。

修复：对齐 decode.rs 的 PCM seek 做法，并加 seek 后 EOF 有限重试
（SEEK_EOF_MAX_RETRIES=40，避免子进程启动延迟误判跳歌，且不死循环）。

验证（tools/diag_dsd_seek*.py，实机）：
- 关音效：seek 前速率 1.0006 → 后 1.002，position 60.0→65.9，无跳歌。
- 开音效(Camilla EQ/PEQ/bass)：前 1.001 → 后 1.0024，position 60.0→66.0，无跳歌。
- 边界：seek 263s（时长 265.9s）→ 正确播到结尾再 EOF，不提前跳歌。
- cargo build/test 通过（47 passed），smoke_test 52 passed。

## 以下为旧记录（DSD 直通路径，decode.rs）——尚未实机复现
现象（理论）：开音效 seek → 加速；关音效 seek → 跳下一首。
注：用户 DoP 有问题未用直通，此路径暂未实测，保留供后续 Native 直通验证。

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
无（PCM 修复与 DSD 软解 seek 修复均已提交）。

## 已提交
- 7409d2a fix(audio): PCM seek 记忆音频 + 暂停中seek响应 + 交接文档
- de2b976 fix(dsd): 软解路径 seek 修复（清 carry/abort、重置 DSP+Camilla、EOF 重试、pwcat 状态机）

## 遗留 / 待办
1. DSD **直通**（decode.rs run_playback_dsd）seek 尚未实机验证——
   用户 DoP 有问题，需先解决 DAC 的 DoP 支持，或验证 Native 直通。
   怀疑点（未证实）：output_alsa.rs flush() 只清 ring 不清 ALSA 内核缓冲；
   DSD 不走状态机，seek 后写线程可能卡在旧内核数据。
2. 诊断脚本 tools/diag_dsd_seek*.py 可复用/可删。
