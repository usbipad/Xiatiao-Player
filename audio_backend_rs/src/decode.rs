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
                output: Arc<crate::output::PipewireOutput>) -> Result<(), String> {
    // symphonia 不支持的高压缩/DSD 格式交给 FFmpeg
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

        // seek
        let seek_ms = shared.seek_target_ms.swap(u64::MAX, Ordering::SeqCst);
        if seek_ms != u64::MAX {
            if let Err(e) = do_seek(&mut *format, seek_ms) {
                eprintln!("[engine] seek failed: {e}");
            } else {
                // 清空输出缓冲，从新位置继续
                output.flush();
                frames_written = (seek_ms as f64 / 1000.0 * in_rate as f64) as u64;
                output.reset_played_frames(frames_written);
                shared.frames_out.store(frames_written, Ordering::SeqCst);
                shared.eof.store(false, Ordering::SeqCst);
            }
        }

        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(_) => {
                shared.eof.store(true, Ordering::SeqCst);
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
                    }
                }
            }
            Err(_) => continue,
        }
    }

    Ok(())
}
