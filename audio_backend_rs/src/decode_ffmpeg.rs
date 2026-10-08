//! FFmpeg 子进程解码路径。
//!
//! 用于 symphonia 不支持的格式（APE / WavPack / DSD / MPC / TTA 等）。
//! 原则：ffmpeg 解码出什么采样率，全程保持那个采样率，**不重采样**；
//! 采样率转换交给 PipeWire/DAC。

use std::io::{Read as _, Write};
use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::Arc;

use crate::dsp::DspParams;
use crate::shared::{viz_fifo_path, Shared};

/// 是否网络流（http/https）。仅这类输入需要 ffmpeg 重连，
/// 本地文件不需要（本地 read 返回 0 基本就是真 EOF）。
pub(crate) fn is_network_stream(path: &str) -> bool {
    path.starts_with("http://") || path.starts_with("https://")
}

/// 给 ffmpeg 输入参数追加「网络重连」选项（仅网络流）。
///
/// 标准做法：让 ffmpeg 自身处理断流重连，上层无需感知网络抖动。
///   -reconnect 1              允许 HTTP 重连
///   -reconnect_streamed 1     对流式输入也重连（我们的场景）
///   -reconnect_delay_max 2    每次重连最多等 2s，之后继续（勿设太大以免静默过久）
///   -reconnect_at_eof 0       真 EOF 时不重连（保证正常结束能触发 end_of_stream）
/// 注意：这些选项必须放在 `-i` 之前（作用于输入）。
fn push_input_args(cmd: &mut std::process::Command, path: &str) {
    if is_network_stream(path) {
        cmd.args([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            "-reconnect_at_eof", "0",
        ]);
    }
    cmd.arg("-i").arg(path);
}

/// 启动 pw-cat 子进程，声明原始采样率/声道，返回其 stdin 对应的子进程。
///
/// 用 PIPEWIRE_PROPS 覆盖 PipeWire 客户端属性，使媒体控件显示为播放器名。
pub(crate) fn spawn_pwcat(rate: u32, channels: u32) -> Result<std::process::Child, String> {
    use std::process::Stdio;
    // node.force-rate：让 PipeWire 把 graph 采样率切到本流采样率，
    // 使 DAC 跟随源采样率（而非固定 48k 重采样）。这是「采样率跟随」的关键。
    let props = format!(
        "{{ application.name = \"Xiatiao Player\" application.process.binary = \"xiatiao-player\" media.role = \"Music\" node.name = \"Xiatiao Player\" node.force-rate = {rate} }}"
    );
    let pw_env_props = "{ application.name = \"Xiatiao Player\" application.process.binary = \"xiatiao-player\" node.name = \"Xiatiao Player\" }";
    eprintln!("[output][rate] spawn pw-cat: --rate {rate} --channels {channels} (node.force-rate={rate})");
    let child = crate::deps::command("pw-cat")
        .env("PIPEWIRE_PROPS", pw_env_props)
        .args([
            "--playback",
            "--raw",
            "--format", "f32",
            "--rate", &rate.to_string(),
            "--channels", &channels.to_string(),
            "--properties", &props,
            "-",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("spawn pw-cat: {e}"))?;
    Ok(child)
}

/// 是否需要 ffmpeg：按扩展名判断 symphonia 不支持的格式。
pub(crate) fn prefer_ffmpeg(path: &str) -> bool {
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    matches!(ext.as_str(), "ape" | "wv" | "dsf" | "dff" | "mpc" | "tta")
}

/// 用 ffprobe 读取 (采样率, 声道, 时长秒, 位深, 码率bps, 编码格式, 是否有音频流)。
///
/// 末位 `has_audio` 很关键：ffprobe 解析不到音频流时（如服务器返回的
/// JSON 错误体、损坏文件），其余字段会静默停留在默认值，无法据此区分
/// 「时长未知的合法流」与「根本不是音频的输入」。故显式返回该判据，
/// 由调用方在入口处拦截，避免把垃圾输入当合法流一路播到假 EOF。
fn ffprobe_info(path: &str) -> (u32, u32, f64, u32, u64, String, bool) {
    let out = crate::deps::command("ffprobe")
        .args([
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels,bits_per_raw_sample,bits_per_sample,bit_rate,codec_name:format=duration,bit_rate",
            "-of", "default=noprint_wrappers=1",
            path,
        ])
        .output();
    let mut rate = 44100u32;
    let mut channels = 2u32;
    let mut dur = 0.0f64;
    let mut bits = 0u32;
    let mut bitrate = 0u64;
    let mut codec = String::new();
    // 只要解析到 codec/sample_rate/channels 任一，即认为存在音频流。
    // 合法格式（含 DSD 软解、DXD、APE/WV/MPC/TTA）都会命中；
    // JSON 错误体 / 非音频文件则一个都解析不到。
    let mut has_audio = false;
    if let Ok(o) = out {
        let text = String::from_utf8_lossy(&o.stdout);
        for line in text.lines() {
            if let Some(v) = line.strip_prefix("sample_rate=") {
                rate = v.trim().parse().unwrap_or(rate);
                has_audio = true;
            } else if let Some(v) = line.strip_prefix("channels=") {
                channels = v.trim().parse().unwrap_or(channels);
                has_audio = true;
            } else if let Some(v) = line.strip_prefix("duration=") {
                dur = v.trim().parse().unwrap_or(0.0);
            } else if let Some(v) = line.strip_prefix("bits_per_raw_sample=") {
                bits = v.trim().parse().unwrap_or(bits);
            } else if let Some(v) = line.strip_prefix("bits_per_sample=") {
                if bits == 0 {
                    bits = v.trim().parse().unwrap_or(0);
                }
            } else if let Some(v) = line.strip_prefix("bit_rate=") {
                if bitrate == 0 {
                    bitrate = v.trim().parse().unwrap_or(0);
                }
            } else if let Some(v) = line.strip_prefix("codec_name=") {
                if codec.is_empty() {
                    codec = v.trim().to_string();
                }
                has_audio = true;
            }
        }
    }
    (rate, channels, dur, bits, bitrate, codec, has_audio)
}

/// 用 ffmpeg 解码到原始采样率 PCM，再转交输出层。
pub(crate) fn run_playback_ffmpeg(path: &str, shared: Arc<Shared>,
                        output: Arc<crate::output::AudioOut>) -> Result<(), String> {
    use std::process::Stdio;

    // 优雅降级：需要 ffmpeg/ffprobe 时先探测，缺失则明确报错（而非静默失败）。
    if !crate::deps::has_executable("ffmpeg") || !crate::deps::has_executable("ffprobe") {
        let msg = format!(
            "该格式需要 ffmpeg 解码，但未找到 ffmpeg/ffprobe。请安装 ffmpeg（如 apt install ffmpeg）。文件：{}",
            path.rsplit('/').next().unwrap_or(path)
        );
        eprintln!("[engine/ffmpeg] {msg}");
        shared.report_error(msg.clone());
        return Err(msg);
    }

    let (src_rate, in_channels, dur, bits, bitrate, codec, has_audio) = ffprobe_info(path);

    // 【关键】入口校验：ffprobe 探测不到音频流 → 输入不是可播放的音频。
    // 最常见场景：Subsonic 服务器对无法解析的曲目返回「HTTP 200 + JSON
    // 错误体」，HTTP 层不报错、ffmpeg 也解不出内容；若不在此拦截，后续
    // 会因 duration=0 走「未知时长重试超限 → 当作正常播完」，把错误伪装
    // 成 end_of_stream，并让前端兜底成笼统的「已跳过」。这里直接给出明确
    // 错误（经 Event::Error → error-occur），让用户看到真实原因。
    //
    // 兼容性：合法输入（DSD 软解 dsf/dff、DXD、APE/WV/MPC/TTA、网络流）
    // 均能被 ffprobe 探测到音频流；只有真正的非音频输入会命中此处。
    if !has_audio {
        // 曲目标识：不要把完整 URL 放进提示。
        // 原因：URL 含 & 等字符，而 Adw.Toast 的标题按 Pango markup 解析，
        // 非法的 &xxx 会被判为实体错误 → 整个提示显示为空白框（实测）。
        // 故网络流只取 id 参数（纯标识），本地文件取文件名。
        let short = if is_network_stream(path) {
            path.split("id=")
                .nth(1)
                .and_then(|s| s.split('&').next())
                .map(|s| format!("曲目 {s}"))
                .unwrap_or_else(|| "在线曲目".to_string())
        } else {
            path.rsplit('/').next().unwrap_or(path).to_string()
        };
        let msg = format!(
            "无法播放：服务器未返回有效音频流（曲目可能无法解析或地址失效）。{short}"
        );
        eprintln!("[engine/ffmpeg] {msg}");
        shared.report_error(msg.clone());
        return Err(msg);
    }

    // 原则：ffmpeg 解码出什么采样率，就全程保持那个采样率，
    // 不做任何重采样/限幅/滤波。采样率转换交给 PipeWire/DAC。
    let in_rate = src_rate;
    crate::logts!("[engine/ffmpeg] ffprobe 完成: {in_rate}Hz {in_channels}ch {bits}bit {bitrate}bps dur={dur:.1}s codec={codec}");

    shared.in_rate.store(in_rate as u64, Ordering::SeqCst);
    shared.in_channels.store(in_channels as u64, Ordering::SeqCst);
    shared.duration_ms.store((dur * 1000.0) as u64, Ordering::SeqCst);
    // 上报音频技术信息（前端显示格式/采样率/位深/码率）
    {
        let mut info = serde_json::Map::new();
        info.insert("sample_rate".into(), serde_json::json!(in_rate));
        info.insert("channels".into(), serde_json::json!(in_channels));
        if bits > 0 {
            info.insert("bit_depth".into(), serde_json::json!(bits));
        }
        if bitrate > 0 {
            info.insert("bitrate".into(), serde_json::json!(bitrate));
        }
        if !codec.is_empty() {
            info.insert("codec".into(), serde_json::json!(codec));
        }
        shared.report_audio_info(serde_json::Value::Object(info));
    }
    let ch = in_channels.max(1) as u32;

    // 可视化旁路 + DSP 链 + Camilla 引擎
    let mut viz = crate::viz::VizTap::open(&viz_fifo_path(), in_rate);
    let mut dsp = crate::dsp::DspChain::new(in_rate);
    let mut camilla_engine = crate::camilla_engine::CamillaEngine::new(in_rate);
    // 输出方式：默认**原生 PipeWire**；仅显式 XIATIAO_AUDIO_BACKEND=pwcat 时回退子进程。
    let use_pwcat = std::env::var("XIATIAO_AUDIO_BACKEND")
        .map(|v| v.to_ascii_lowercase() == "pwcat")
        .unwrap_or(false);
    let mut pwcat: Option<std::process::Child> = if use_pwcat {
        match spawn_pwcat(in_rate, ch) {
            Ok(c) => { eprintln!("[engine/ffmpeg] 使用 pw-cat 子进程输出"); Some(c) }
            Err(e) => { eprintln!("[engine/ffmpeg] pw-cat 启动失败: {e}"); None }
        }
    } else {
        crate::logts!("[engine/ffmpeg] 开始解码 + 输出");
        None
    };
    // 启动 ffmpeg：不加 -ar / -af，用 ffmpeg 默认采样率输出（保持原样）。
    // 网络流额外启用重连（标准做法），本地文件不加。
    let mut cmd = crate::deps::command("ffmpeg");
    cmd.args(["-v", "error", "-nostdin"]);
    push_input_args(&mut cmd, path);
    // stderr 继承后端进程（写入后端日志），而非丢弃：ffmpeg 的报错
    // （如 Invalid data found）是定位「解不出音频」的关键证据，丢弃后
    // 只剩一个笼统的假 EOF，难以排查。ffmpeg 用 -v error，仅错误才输出，
    // 不会刷屏。
    let mut ff = cmd
        .args(["-f", "f32le", "-ac", &ch.to_string(), "-"])
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("spawn ffmpeg: {e}"))?;
    let mut ff_out = ff.stdout.take().ok_or("no ffmpeg stdout")?;

    let mut frames_written: u64 = 0;
    // 持久读取缓冲：一次读大块，并用 carry 保存跨块残留的非 4 字节尾部，
    // 保证每次处理的都是完整 f32 样本，避免字节错位导致杂音。
    let mut buf = vec![0u8; 131072];
    let mut carry: Vec<u8> = Vec::with_capacity(4);
    // seek 后若刚重启的 ffmpeg 尚未吐数据就被读到 EOF，允许有限次重试
    // （子进程启动延迟），超过阈值才认定为真 EOF，避免误跳下一首，
    // 同时避免无限重试死循环。声明在 loop 外，否则 continue 会被重置。
    let mut seek_eof_retries: u32 = 0;
    const SEEK_EOF_MAX_RETRIES: u32 = 150;
    // 「已播时长 vs 总时长」判定用：允许已播比总时长略短仍算播完
    // （编码器尾部 padding / 时长元数据误差）。取 2 秒。
    const EOF_DURATION_TOLERANCE_SECS: f64 = 2.0;
    // seek 后待恢复 Playing 的标志：必须跨迭代保持（读到第一块新数据才清），
    // 否则 EOF 重试时 just_seeked 被重置为 false，重试逻辑失效。
    let mut just_seeked = false;
    loop {
        if shared.stop.load(Ordering::SeqCst) {
            break;
        }
        while !shared.playing.load(Ordering::SeqCst) && !shared.stop.load(Ordering::SeqCst) {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        if shared.stop.load(Ordering::SeqCst) {
            break;
        }

        // seek：重启 ffmpeg；输出缓冲清空
        let seek_ms = shared.seek_target_ms.swap(u64::MAX, Ordering::SeqCst);
        if seek_ms != u64::MAX {
            drop(ff_out);
            let _ = ff.kill();
            let _ = ff.wait();
            // 状态机（与 symphonia 路径一致）：Draining → Refilling，
            // 期间输出静音；第一块新数据写入后 mark_playing → Playing。
            output.begin_seek();
            output.mark_refilling();
            just_seeked = true;

            // 【关键】清 seek 时置的 abort_write，否则 output.write 会立即
            // 返回（解码线程以为要中断），新数据写不进去 → seek 后静音。
            // 与 symphonia 路径（decode.rs）保持一致。
            output.clear_abort();

            // 【关键】丢弃跨块残留字节：seek 后 ffmpeg 从新位置输出全新字节流，
            // 旧 carry 是旧位置的非 4 字节尾部，拼到新数据开头会导致样本错位
            // （听起来像加速 / 杂音）。
            carry.clear();

            let ss = seek_ms as f64 / 1000.0;
            let mut scmd = crate::deps::command("ffmpeg");
            scmd.args(["-v", "error", "-nostdin", "-ss", &format!("{ss}")]);
            push_input_args(&mut scmd, path);
            ff = scmd
                .args(["-f", "f32le", "-ac", &ch.to_string(), "-"])
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .map_err(|e| format!("respawn ffmpeg: {e}"))?;
            ff_out = ff.stdout.take().ok_or("no ffmpeg stdout")?;
            frames_written = (ss * in_rate as f64) as u64;
            shared.frames_out.store(frames_written, Ordering::SeqCst);
            // 关键：重置输出层的进度基准（engine.position 用 played_frames），
            // 否则 seek 后进度条会回弹到 0。
            output.reset_played_frames(frames_written);
            shared.eof.store(false, Ordering::SeqCst);
            // 新一次 seek：重置 EOF 重试计数。
            seek_eof_retries = 0;

            // seek 后重置 DSP 运行时状态（滤波器延迟 / 包络 / 平滑器），
            // 否则新位置的信号会与旧位置的滤波器状态不连续 → 衔接不自然 / 爆音。
            // 与 symphonia 路径（decode.rs）保持一致。
            dsp.reset_state();
            // Camilla 触发短淡入，避免跳转硬切。
            camilla_engine.trigger_fade();
            // 【关键】seek 后重建 Camilla pipeline：camillalib 没有滤波器状态
            // 的 reset API（FIR 卷积历史无法单独清），只能重建管线来丢弃卷积尾。
            // 否则 seek 后新数据接旧卷积尾 → 「记忆音频」（开音效时出现）。
            if let Some(yaml) = shared.camilla_yaml.lock().ok().and_then(|g| g.clone()) {
                camilla_engine.clear();
                if let Err(e) = camilla_engine.set_yaml(&yaml) {
                    eprintln!("[camilla/ffmpeg] seek 后重建失败: {e}");
                }
            }
        }

        let n = match ff_out.read(&mut buf) {
            Ok(n) => n,
            // 被信号打断：非错误，重试。
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            // 其它读取错误（管道断裂等）：不是「播完」，当作「暂时无数据」
            // 交给下方证据判定，避免把真错误伪装成正常结束。
            Err(_) => 0,
        };
        if n == 0 {
            // 【关键】read 返回 0 不等于「整首播完」。以下情况都会返回 0
            // 但并非真结束：
            //   - seek 后刚重启的 ffmpeg 尚未吐出数据；
            //   - 在线流的 CDN 暂时无数据 / 网络抖动；
            //   - ffmpeg 内部缓冲尚未就绪。
            //
            // 若直接把 0 判为 EOF，会向客户端误报 end_of_stream，导致
            // 播放器「自动下一首」并陷入无限切歌。
            //
            // 【证据判定】用「已播时长 vs 总时长」作为决定性依据：
            //   只要已播时长明显不足总时长，就绝不判 EOF，继续等待
            //   （ffmpeg 已启用 -reconnect，网络抖动由其内部重连消化）。
            //   只有「已播 ≈ 总时长」才是真结束。未知时长（total==0）
            //   才退回固定重试次数兜底。
            let played_secs = frames_written as f64 / in_rate.max(1) as f64;
            let total_secs = shared.duration_ms.load(Ordering::SeqCst) as f64 / 1000.0;
            let known_duration = total_secs > 0.0;
            let near_end = known_duration
                && played_secs >= (total_secs - EOF_DURATION_TOLERANCE_SECS);

            if known_duration && !near_end {
                // 铁证：远没播完 → 绝不 EOF。继续等待/重试（不设判 EOF 上限）。
                std::thread::sleep(std::time::Duration::from_millis(20));
                continue;
            }
            // 已播 ≈ 总时长（真结束）；或未知时长且重试超限（兜底）。
            if !known_duration && seek_eof_retries < SEEK_EOF_MAX_RETRIES {
                seek_eof_retries += 1;
                std::thread::sleep(std::time::Duration::from_millis(20));
                continue;
            }
            shared.eof.store(true, Ordering::SeqCst);
            // 结束 seek 过渡（EOF 兜底）：避免状态机停在 Draining/
            // Refilling 导致永久静音。
            output.end_seek();
            break;
        }
        // 成功读到数据：重置 EOF 重试计数（下次读到 0 重新计数）。
        seek_eof_retries = 0;
        // 拼上上一轮残留字节，保证 f32 样本按 4 字节对齐解析。
        let data: Vec<u8> = if carry.is_empty() {
            buf[..n].to_vec()
        } else {
            let mut d = std::mem::take(&mut carry);
            d.extend_from_slice(&buf[..n]);
            d
        };
        let aligned = data.len() / 4 * 4;
        carry = data[aligned..].to_vec();
        // 应用音量
        let vol = *shared.volume.lock().unwrap_or_else(|e| e.into_inner());
        let ch_usize = ch as usize;
        let mut pcm: Vec<f32> = Vec::with_capacity(aligned / 4);
        for b in data[..aligned].chunks_exact(4) {
            let v = f32::from_le_bytes([b[0], b[1], b[2], b[3]]) * vol;
            pcm.push(v);
        }
        // 应用 DSP（与 symphonia 路径一致，串联）
        let dsp_json = shared.dsp_params.lock().map(|p| p.clone()).unwrap_or(serde_json::Value::Null);
        dsp.set_params(DspParams::from_json(&dsp_json));
        dsp.set_engine(true);
        // 把播放器音量传给 DSP（供动态等响度用）
        dsp.set_volume(vol);
        let cam_on = dsp.camilla_active();
        if let Ok((td, ba)) = shared.coloring.lock().map(|g| *g) {
            dsp.set_coloring(td, ba);
        }
        // 串联：前置段 → Camilla（若开）→ 后置段
        dsp.process_pre(&mut pcm, ch_usize);
        if cam_on {
            if let Some(yaml) = shared.camilla_yaml.lock().ok().and_then(|g| g.clone()) {
                if let Err(e) = camilla_engine.set_yaml(&yaml) {
                    eprintln!("[camilla/ffmpeg] set_yaml 失败: {e}");
                }
            }
            if let Ok((td, ba)) = shared.coloring.lock().map(|g| *g) {
                camilla_engine.set_coloring(td, ba);
            }
            camilla_engine.process_interleaved(&mut pcm, ch_usize);
        }
        dsp.process_post(&mut pcm, ch_usize);
        frames_written += pcm.len() as u64 / ch.max(1) as u64;
        shared.frames_out.store(frames_written, Ordering::SeqCst);
        // 可视化旁路
        viz.feed(&pcm, ch_usize);
        if let Some(child) = pwcat.as_mut() {
            if let Some(si) = child.stdin.as_mut() {
                let mut bytes = Vec::with_capacity(pcm.len() * 4);
                for s in &pcm { bytes.extend_from_slice(&s.to_le_bytes()); }
                let _ = si.write_all(&bytes);
            }
        } else {
            if frames_written % 200000 < pcm.len() as u64 / ch as u64 {
            }
            output.write(&pcm, in_rate, ch);
        }
        // seek 后第一块新数据写完：恢复 Playing。
        // 必须放在 if/else 之外：pwcat 分支不经过 output.write，若只在 else
        // 清除，just_seeked 永远为 true、状态机永久停在 Refilling → 静音。
        if just_seeked {
            output.mark_playing();
            just_seeked = false;
        }
    }

    drop(ff_out);
    let _ = ff.kill();
    let _ = ff.wait();
    Ok(())
}
