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

/// 用 ffprobe 读取 (采样率, 声道, 时长秒)。
fn ffprobe_info(path: &str) -> (u32, u32, f64) {
    let out = crate::deps::command("ffprobe")
        .args([
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels:format=duration",
            "-of", "default=noprint_wrappers=1",
            path,
        ])
        .output();
    let mut rate = 44100u32;
    let mut channels = 2u32;
    let mut dur = 0.0f64;
    if let Ok(o) = out {
        let text = String::from_utf8_lossy(&o.stdout);
        for line in text.lines() {
            if let Some(v) = line.strip_prefix("sample_rate=") {
                rate = v.trim().parse().unwrap_or(rate);
            } else if let Some(v) = line.strip_prefix("channels=") {
                channels = v.trim().parse().unwrap_or(channels);
            } else if let Some(v) = line.strip_prefix("duration=") {
                dur = v.trim().parse().unwrap_or(0.0);
            }
        }
    }
    (rate, channels, dur)
}

/// 用 ffmpeg 解码到原始采样率 PCM，再转交输出层。
pub(crate) fn run_playback_ffmpeg(path: &str, shared: Arc<Shared>,
                        output: Arc<crate::output::PipewireOutput>) -> Result<(), String> {
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

    let (src_rate, in_channels, dur) = ffprobe_info(path);

    // 原则：ffmpeg 解码出什么采样率，就全程保持那个采样率，
    // 不做任何重采样/限幅/滤波。采样率转换交给 PipeWire/DAC。
    let in_rate = src_rate;
    eprintln!("[engine/ffmpeg] {in_rate}Hz {in_channels}ch dur={dur:.1}s");

    shared.in_rate.store(in_rate as u64, Ordering::SeqCst);
    shared.in_channels.store(in_channels as u64, Ordering::SeqCst);
    shared.duration_ms.store((dur * 1000.0) as u64, Ordering::SeqCst);
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
        eprintln!("[engine/ffmpeg] 使用原生 PipeWire 输出");
        None
    };
    // 启动 ffmpeg：不加 -ar / -af，用 ffmpeg 默认采样率输出（保持原样）。
    let mut ff = crate::deps::command("ffmpeg")
        .args(["-v", "error", "-nostdin"])
        .args(["-i", path])
        .args(["-f", "f32le", "-ac", &ch.to_string(), "-"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("spawn ffmpeg: {e}"))?;
    let mut ff_out = ff.stdout.take().ok_or("no ffmpeg stdout")?;

    let mut frames_written: u64 = 0;
    // 持久读取缓冲：一次读大块，并用 carry 保存跨块残留的非 4 字节尾部，
    // 保证每次处理的都是完整 f32 样本，避免字节错位导致杂音。
    let mut buf = vec![0u8; 131072];
    let mut carry: Vec<u8> = Vec::with_capacity(4);
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
            output.flush();

            let ss = seek_ms as f64 / 1000.0;
            ff = crate::deps::command("ffmpeg")
                .args(["-v", "error", "-nostdin", "-ss", &format!("{ss}")])
                .args(["-i", path])
                .args(["-f", "f32le", "-ac", &ch.to_string(), "-"])
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|e| format!("respawn ffmpeg: {e}"))?;
            ff_out = ff.stdout.take().ok_or("no ffmpeg stdout")?;
            frames_written = (ss * in_rate as f64) as u64;
            shared.frames_out.store(frames_written, Ordering::SeqCst);
            shared.eof.store(false, Ordering::SeqCst);
        }

        let n = ff_out.read(&mut buf).unwrap_or(0);
        if n == 0 {
            shared.eof.store(true, Ordering::SeqCst);
            break;
        }
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
            output.write(&pcm, in_rate, ch);
        }
    }

    drop(ff_out);
    let _ = ff.kill();
    let _ = ff.wait();
    Ok(())
}
