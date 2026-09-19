//! Symphonia 原生解码路径。
//!
//! 主解码路径；symphonia 不支持的格式（或 probe 失败）时回退到
//! decode_ffmpeg。原则：解码得到原始采样率，**不重采样**。

use std::fs::File;
use std::io::Write;
use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::Arc;

use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::DecoderOptions;
use symphonia::core::formats::FormatOptions;
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

use crate::decode_ffmpeg::{prefer_ffmpeg, run_playback_ffmpeg, spawn_pwcat};
use crate::shared::{viz_fifo_path, Shared};

/// 执行 seek（symphonia）。
fn do_seek(
    format: &mut dyn symphonia::core::formats::FormatReader,
    ms: u64,
) -> Result<(), String> {
    use symphonia::core::formats::SeekMode;
    use symphonia::core::formats::SeekTo;
    let time = symphonia::core::units::Time::from(ms as f64 / 1000.0);
    format
        .seek(SeekMode::Accurate, SeekTo::Time { time, track_id: None })
        .map_err(|e| format!("{e}"))?;
    Ok(())
}

/// 解码线程：用 symphonia 解码并写入输出层。
pub(crate) fn run_playback(path: &str, shared: Arc<Shared>,
                output: Arc<crate::output::AudioOut>) -> Result<(), String> {
    // ---- DSD 分流 ----
    // dsf/dff 在 native / dop 模式下，绕过 ffmpeg，直接读取原始 DSD 位流
    // 交给 ALSA 独占后端（DoP 封装或 Native 直通）。
    // pcm 模式（软解）与 auto 回退，保持现有 ffmpeg 路径。
    if crate::dsd::is_dsd_file(path) {
        let mode = shared
            .dsd_mode
            .lock()
            .map(|m| m.clone())
            .unwrap_or_else(|_| "auto".to_string());
        match mode.as_str() {
            "pcm" => {
                // 明确软解。
                eprintln!("[engine] DSD 软解路径（mode=pcm）：{path}");
                return run_playback_ffmpeg(path, shared, output);
            }
            "native" | "dop" | "auto" => {
                // native/dop：尝试直通（内部会做能力检查，不支持则报错）。
                // auto：优先尝试直通；若后端不是 ALSA（PipeWire）则无法直通，回退软解。
                let is_alsa = matches!(output.as_ref(), crate::output::AudioOut::Alsa(_));
                if is_alsa {
                    eprintln!("[engine] DSD 直通路径（mode={mode}）：{path}");
                    let r = run_playback_dsd(path, shared.clone(), Arc::clone(&output));
                    // auto 模式下直通失败（不支持/规格超限）→ 自动降级软解。
                    if r.is_err() && mode == "auto" {
                        eprintln!("[engine] DSD 直通不可用，自动降级为 PCM 软解");
                        return run_playback_ffmpeg(path, shared, output);
                    }
                    return r;
                } else {
                    // PipeWire 后端：无法直通。native/dop 显式选择时提示，auto 静默软解。
                    if mode != "auto" {
                        let msg = "DSD 直通需要 ALSA 独占输出：请在设置中选择具体输出设备（非“自动”）。".to_string();
                        eprintln!("[engine/dsd] {msg}");
                        shared.report_error(msg.clone());
                        return Err(msg);
                    }
                    eprintln!("[engine] DSD 软解路径（mode=auto，PipeWire 无法直通）：{path}");
                    return run_playback_ffmpeg(path, shared, output);
                }
            }
            _ => {
                eprintln!("[engine] DSD 未知模式 {mode}，按软解处理：{path}");
                return run_playback_ffmpeg(path, shared, output);
            }
        }
    }

    // symphonia 不支持的高压缩格式交给 FFmpeg
    if prefer_ffmpeg(path) {
        eprintln!("[engine] using ffmpeg decoder for {path}");
        return run_playback_ffmpeg(path, shared, output);
    }

    let file = File::open(Path::new(path)).map_err(|e| format!("open: {e}"))?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = Path::new(path).extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let probed = match symphonia::default::get_probe()
        .format(&hint, mss, &FormatOptions::default(), &MetadataOptions::default())
    {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[engine] symphonia probe failed ({e}), fallback to ffmpeg");
            return run_playback_ffmpeg(path, shared, output);
        }
    };
    let mut format = probed.format;
    let track = format
        .default_track()
        .ok_or_else(|| "no default track".to_string())?;
    let track_id = track.id;
    let mut decoder = symphonia::default::get_codecs()
        .make(&track.codec_params, &DecoderOptions::default())
        .map_err(|e| format!("decoder: {e}"))?;

    let in_rate = track.codec_params.sample_rate.unwrap_or(44100);
    let in_channels = track
        .codec_params
        .channels
        .map(|c| c.count())
        .unwrap_or(2)
        .max(1) as u32;
    shared.in_rate.store(in_rate as u64, Ordering::SeqCst);
    shared.in_channels.store(in_channels as u64, Ordering::SeqCst);
    let dur_ms = if let Some(nf) = track.codec_params.n_frames {
        (nf as f64 / in_rate as f64 * 1000.0) as u64
    } else {
        0
    };
    shared.duration_ms.store(dur_ms, Ordering::SeqCst);
    eprintln!("[engine] symphonia: {in_rate}Hz {in_channels}ch");

    // 输出层：默认**原生 PipeWire**（应用名正确、可控采样率跟随）；
    // 仅当显式设 XIATIAO_AUDIO_BACKEND=pwcat 时回退 pw-cat 子进程。
    let ch_out = in_channels.max(1) as u32;
    let rate_out = in_rate;
    let use_pwcat = std::env::var("XIATIAO_AUDIO_BACKEND")
        .map(|v| v.to_ascii_lowercase() == "pwcat")
        .unwrap_or(false);
    let mut pwcat: Option<std::process::Child> = if use_pwcat {
        match spawn_pwcat(rate_out, ch_out) {
            Ok(c) => { eprintln!("[engine] 使用 pw-cat 子进程输出"); Some(c) }
            Err(e) => { eprintln!("[engine] pw-cat 启动失败: {e}"); None }
        }
    } else {
        eprintln!("[engine] 使用原生 PipeWire 输出");
        None
    };

    // 可视化旁路
    let mut viz = crate::viz::VizTap::open(&viz_fifo_path(), in_rate);

    let mut sample_buf: Option<SampleBuffer<f32>> = None;
    let mut frames_written: u64 = 0;
    let mut dsp = crate::dsp::DspChain::new(in_rate);
    // 始终创建嵌入式 Camilla 引擎（运行时按开关决定是否参与）
    let mut camilla_engine = crate::camilla_engine::CamillaEngine::new(in_rate);
    loop {
        if shared.stop.load(Ordering::SeqCst) {
            break;
        }
        // 暂停：等待恢复（不写数据）
        while !shared.playing.load(Ordering::SeqCst) && !shared.stop.load(Ordering::SeqCst) {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        if shared.stop.load(Ordering::SeqCst) {
            break;
        }

        // seek：状态机 begin_seek → Draining（输出静音 + flush 硬件旧数据）；
        // mark_refilling → 清 ring + Refilling（旧数据彻底丢弃）。
        // 随后本轮解码出的第一块新数据写入 ring，写完后 mark_playing →
        // Playing（RT 恢复 pop）。
        let mut just_seeked = false;
        let seek_ms = shared.seek_target_ms.swap(u64::MAX, Ordering::SeqCst);
        if seek_ms != u64::MAX {
            if let Err(e) = do_seek(&mut *format, seek_ms) {
                eprintln!("[engine] seek failed: {e}");
                // 兜底：seek 失败时恢复 Playing，避免状态停在 Draining/
                // Refilling（尤其是 resume 时 engine 已提前置 Draining 的情况）
                // 导致永久静音。同时清 abort_write，避免后续 write 立即返回。
                output.clear_abort();
                output.end_seek();
            } else {
                // 清除 seek 时置的 abort_write（engine.rs Seek handler 置的），
                // 使后续 write 正常写入新数据。
                output.clear_abort();
                output.begin_seek();
                output.mark_refilling();
                just_seeked = true;
                frames_written = (seek_ms as f64 / 1000.0 * in_rate as f64) as u64;
                output.reset_played_frames(frames_written);
                shared.frames_out.store(frames_written, Ordering::SeqCst);
                shared.eof.store(false, Ordering::SeqCst);
                // seek 后重置 DSP 运行时状态（滤波器延迟/包络/平滑器），
                // 否则新位置的信号会与旧位置的滤波器状态不连续 → 衔接不自然
                // / 爆音。
                dsp.reset_state();
                // Camilla 也触发短淡入，避免跳转硬切。
                camilla_engine.trigger_fade();
                // 【关键】seek 后**重建 Camilla pipeline**：camillalib 没有
                // 滤波器状态的 reset API（FIR 卷积历史无法单独清），只能重建
                // 管线来丢弃卷积尾。否则 seek 后新数据接旧卷积尾 →
                // 「记忆音频」（**开音效**即 Camilla 跑，故出现；关音效正常）。
                if let Some(yaml) = shared.camilla_yaml.lock().ok().and_then(|g| g.clone()) {
                    camilla_engine.clear();
                    if let Err(e) = camilla_engine.set_yaml(&yaml) {
                        eprintln!("[camilla] seek 后重建失败: {e}");
                    }
                }
            }
        }

        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(_) => {
                shared.eof.store(true, Ordering::SeqCst);
                // 结束 seek 过渡：EOF 后不再有新数据写输出，若正处于 seek
                // 过渡（seeking=true）且 pending 未填够切换，write() 兜底不会
                // 被触发 → RT 永久 hold。主动清 seeking，让 RT 恢复 pop。
                output.end_seek();
                break;
            }
        };
        if packet.track_id() != track_id {
            continue;
        }
        match decoder.decode(&packet) {
            Ok(audio_buf) => {
                if sample_buf.is_none() {
                    let spec = *audio_buf.spec();
                    let cap = audio_buf.capacity() as u64;
                    sample_buf = Some(SampleBuffer::<f32>::new(cap, spec));
                }
                if let Some(buf) = &mut sample_buf {
                    buf.copy_interleaved_ref(audio_buf);
                    let vol = *shared.volume.lock().unwrap_or_else(|e| e.into_inner());
                    let ch = in_channels as usize;
                    // 先应用音量，再过 DSP
                    let mut pcm: Vec<f32> = buf.samples().iter().map(|s| s * vol).collect();
                    // Rust 侧参数与染色（每块同步一次）
                    let dsp_json = shared.dsp_params.lock().map(|p| p.clone()).unwrap_or(serde_json::Value::Null);
                    dsp.set_params_json(&dsp_json);
                    dsp.set_engine(true);
                    // 把播放器音量传给 DSP（供动态等响度用）
                    dsp.set_volume(vol);
                    // Camilla 是否参与：由参数自动推导
                    let cam_on = dsp.camilla_active();
                    if let Ok((td, ba)) = shared.coloring.lock().map(|g| *g) {
                        dsp.set_coloring(td, ba);
                    }
                    // 串联：前置段 → Camilla（若开）→ 后置段
                    dsp.process_pre(&mut pcm, ch);
                    if cam_on {
                        if let Some(yaml) = shared.camilla_yaml.lock().ok().and_then(|g| g.clone()) {
                            if let Err(e) = camilla_engine.set_yaml(&yaml) {
                                eprintln!("[camilla] set_yaml 失败: {e}");
                            }
                        }
                        if let Ok((td, ba)) = shared.coloring.lock().map(|g| *g) {
                            camilla_engine.set_coloring(td, ba);
                        }
                        camilla_engine.process_interleaved(&mut pcm, ch);
                    }
                    dsp.process_post(&mut pcm, ch);
                    // 调试统计（仅 XIATIAO_DSP_DEBUG=1）
                    if !cam_on && std::env::var_os("XIATIAO_DSP_DEBUG").is_some() {
                        let cnt = shared._dsp_logged.fetch_add(1, Ordering::SeqCst);
                        if cnt % 200 == 0 {
                            let mx = pcm.iter().fold(0.0f32, |a, &b| a.max(b.abs()));
                            let nan = pcm.iter().any(|x| x.is_nan());
                            eprintln!("[dsp-debug] block={cnt} peak={mx:.4} nan={nan} enabled={}", dsp.params().enabled);
                        }
                    }
                    frames_written += pcm.len() as u64 / ch.max(1) as u64;
                    shared.frames_out.store(frames_written, Ordering::SeqCst);
                    // 可视化旁路
                    viz.feed(&pcm, ch);
                    if let Some(child) = pwcat.as_mut() {
                        if let Some(si) = child.stdin.as_mut() {
                            let mut bytes = Vec::with_capacity(pcm.len() * 4);
                            for s in &pcm { bytes.extend_from_slice(&s.to_le_bytes()); }
                            let _ = si.write_all(&bytes);
                        }
                    } else {
                        output.write(&pcm, rate_out, ch_out);
                        // seek 后第一块新数据写完：恢复 Playing。
                        if just_seeked {
                            output.mark_playing();
                            just_seeked = false;
                        }
                    }
                }
            }
            Err(_) => continue,
        }
    }

    Ok(())
}

/// DSD 直通播放：流式读取原始 DSD 位流，按 DoP 经 ALSA 独占后端输出。
///
/// 前提：输出后端必须是 ALSA 独占（指定了输出设备）。若仍是 PipeWire，
/// 无法原生直通，返回明确错误（由上层提示用户选择 ALSA 设备）。
fn run_playback_dsd(path: &str, shared: Arc<Shared>,
                    output: Arc<crate::output::AudioOut>) -> Result<(), String> {
    // DSD 直通只支持 ALSA 独占后端。
    let alsa = match output.as_ref() {
        crate::output::AudioOut::Alsa(a) => a,
        crate::output::AudioOut::Pipewire(_) => {
            let msg = "DSD 直通需要 ALSA 独占输出：请在设置中选择具体输出设备（非“自动”）。".to_string();
            eprintln!("[engine/dsd] {msg}");
            shared.report_error(msg.clone());
            return Err(msg);
        }
    };

    // 当前 DSD 模式（native / dop / auto）。
    let dsd_mode = shared
        .dsd_mode
        .lock()
        .map(|m| m.clone())
        .unwrap_or_else(|_| "auto".to_string());

    // 流式打开（只读头部 + 定位 data 区）。
    let mut reader = crate::dsd::DsdReader::open(path)?;
    let channels = reader.channels.max(1);
    let dsd_rate = reader.dsd_rate;
    // DoP 的 PCM 采样率 = DSD 率 / 16。
    let pcm_rate = (dsd_rate / 16).max(44_100);

    eprintln!(
        "[engine/dsd] 流式解析：mode={} DSD rate={} ch={} DoP PCM rate={} frames={}",
        dsd_mode, dsd_rate, channels, pcm_rate, reader.frames
    );

    // ---- 模式决策：native 还是 dop ----
    // native：需要设备原生支持 DSD 格式（DSD_U32_LE 等），不支持则明确报错。
    // dop：把 DSD 打包成 PCM 传输，兼容性最好。
    // auto：优先 native，不支持则 dop。
    let use_native = match dsd_mode.as_str() {
        "native" => true,
        "dop" => false,
        // auto：探测设备是否支持原生 DSD。
        _ => crate::output_alsa::device_supports_dsd(alsa.device(), dsd_rate, channels),
    };

    if use_native {
        // Native 能力检查：设备必须支持 DSD 格式。
        if !crate::output_alsa::device_supports_dsd(alsa.device(), dsd_rate, channels) {
            let msg = format!(
                "所选设备不支持原生 DSD 直通（Native）。\n\
                 请改用“DoP”或“转 PCM（软解）”模式播放。\n\
                 （文件：DSD{}，设备：{}）",
                (dsd_rate as f64 / 44100.0).round() as u32,
                alsa.device()
            );
            eprintln!("[engine/dsd] {msg}");
            shared.report_error(msg.clone());
            return Err(msg);
        }
    } else {
        // DoP 规格预检：所需 PCM 采样率不得超过设备上限。
        if let Some(max_rate) = crate::output_alsa::device_max_rate(alsa.device()) {
            if pcm_rate > max_rate {
                let dsd_factor = (dsd_rate as f64 / 44100.0).round() as u32;
                let msg = format!(
                    "当前 DSD 文件的 DoP 采样率（{} Hz）超出所选设备上限（{} Hz）。\n\
                     该文件规格为 DSD{}，无法通过 DoP 直通；请改用“转 PCM（软解）”模式播放。",
                    pcm_rate, max_rate, dsd_factor
                );
                eprintln!("[engine/dsd] {msg}");
                shared.report_error(msg.clone());
                return Err(msg);
            }
        }
    }

    // 更新共享元信息（进度 / 时长基准）。
    // 关键：in_rate 存**输出层实际帧率**，供 engine.position 计算
    // （played_frames / in_rate = 秒）。
    //   - DoP：输出是 PCM，帧率 = pcm_rate（DSD/16）；
    //   - Native：输出是 DSD 帧，帧率 = dsd_rate。
    // 时长单独按 dsd_rate 算（见下），不受影响。
    let out_rate = if use_native { dsd_rate } else { pcm_rate };
    shared.in_rate.store(out_rate as u64, Ordering::SeqCst);
    shared.in_channels.store(channels as u64, Ordering::SeqCst);
    let dur_ms = if dsd_rate > 0 {
        (reader.frames as f64 / dsd_rate as f64 * 1000.0) as u64
    } else {
        0
    };
    shared.duration_ms.store(dur_ms, Ordering::SeqCst);

    // 块大小：native 用 4 字节对齐（DSD_U32），dop 用 2*channels 对齐。
    let unit = if use_native {
        4
    } else {
        channels as usize * 2
    };
    let mut chunk_size = 1 << 20;
    chunk_size -= chunk_size % unit;
    let mut buf = vec![0u8; chunk_size];
    let mut marker_phase: usize = 0;

    let mut frames_out: u64 = 0;
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

        // ---- seek 处理 ----
        // DSD 是顺序字节流，按固定字节率定位到目标帧。
        // DSD 直通不走 PCM 状态机（采样率/帧数语义不同）。
        let seek_ms = shared.seek_target_ms.swap(u64::MAX, Ordering::SeqCst);
        if seek_ms != u64::MAX {
            let target_sec = seek_ms as f64 / 1000.0;
            let target_frame = (target_sec * dsd_rate as f64) as u64;
            // 关键：先立即把进度基准设到目标（基于请求的 target_frame），
            // 让 UI 立刻反映目标位置，避免 seek 文件 IO 期间显示旧位置
            // （表现为“先跳回旧位置再跳回目标”）。
            let out_rate = if use_native { dsd_rate } else { pcm_rate };
            let out_frames_req = if dsd_rate > 0 {
                (target_frame as f64 / dsd_rate as f64 * out_rate as f64) as u64
            } else {
                0
            };
            alsa.reset_played_frames(out_frames_req);

            match reader.seek_to_frame(target_frame) {
                Ok(actual_frame) => {
                    // DSD 直通不走 PCM 状态机（避免采样率/帧数语义混淆）：
                    // 仅清 abort_write + flush ALSA 缓冲，丢弃 seek 前残留。
                    output.clear_abort();
                    // 清空输出缓冲，丢弃 seek 前残留。
                    alsa.flush();
                    // seek 完成后再校正一次（实际帧可能因字节对齐略有出入）。
                    let out_frames = if dsd_rate > 0 {
                        (actual_frame as f64 / dsd_rate as f64 * out_rate as f64) as u64
                    } else {
                        0
                    };
                    alsa.reset_played_frames(out_frames);
                    frames_out = actual_frame;
                    shared.frames_out.store(frames_out, Ordering::SeqCst);
                    marker_phase = 0;
                    eprintln!(
                        "[engine/dsd] SEEK → {:.3}s (frame={actual_frame}, out_frames={out_frames})",
                        target_sec
                    );
                }
                Err(e) => {
                    eprintln!("[engine/dsd] seek 失败: {e}");
                    // 兜底：seek 失败时恢复 Playing + 清 abort_write。
                    output.clear_abort();
                    output.end_seek();
                }
            }
        }

        let n = reader.read_chunk(&mut buf)?;
        if n == 0 {
            break;
        }
        let usable = n - (n % unit);
        if usable == 0 {
            break;
        }
        if use_native {
            alsa.write_native_chunk(&buf[..usable], channels, dsd_rate);
        } else {
            alsa.write_dop_chunk(&buf[..usable], channels, pcm_rate, &mut marker_phase);
        }
        frames_out += (usable as u64 * 8) / channels as u64;
        shared.frames_out.store(frames_out, Ordering::SeqCst);

        // 检测写线程是否已失败退出（如设备占用、格式不支持）。
        // 失败则中止并返回错误，供上层（auto 模式）降级到软解。
        if alsa.is_failed() {
            let msg = alsa
                .take_error()
                .unwrap_or_else(|| "DSD 直通输出失败".to_string());
            return Err(msg);
        }
    }

    shared.eof.store(true, Ordering::SeqCst);
    Ok(())
}
