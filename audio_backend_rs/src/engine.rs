//! 播放引擎：symphonia/ffmpeg 解码 + pw-cat 输出（流式）。
//!
//! 输出层用 PipeWire 的 pw-cat 子进程：
//! - 解码得到原始采样率的 f32 PCM；
//! - 写入 pw-cat 的 stdin（--rate/--channels 用原始值）；
//! - 由 PipeWire 决定是否转换采样率（应用不重采样）。
//!
//! 音量在写之前对 PCM 乘（实时生效）。
//! 位置按已写入帧数估算（减去输出缓冲延迟）。

use std::fs::File;
use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::DecoderOptions;
use symphonia::core::formats::FormatOptions;
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

use crate::dsp::DspParams;
use crate::protocol::{Event, Request};

/// 输出缓冲延迟估算（秒）：pw-cat + PipeWire 的缓冲。
const OUTPUT_LATENCY_SECS: f64 = 0.10;

/// 应用标识：仅用于拼运行时目录，包名变更时改这一处。
const APP_ID: &str = "xiatiao";

/// 解析可视化 FIFO 路径（与 Python 侧保持一致）：
/// 1) 环境变量 XIATIAO_VIZ_FIFO 覆盖；
/// 2) $XDG_RUNTIME_DIR/<APP_ID>/viz.fifo（标准，tmpfs 内存）；
/// 3) /dev/shm/<APP_ID>/viz.fifo（纯内存兜底）；
/// 4) /tmp/<APP_ID>/viz.fifo（最终兜底）。
fn viz_fifo_path() -> String {
    if let Ok(p) = std::env::var("XIATIAO_VIZ_FIFO") {
        if !p.is_empty() {
            return p;
        }
    }
    if let Some(dir) = std::env::var_os("XDG_RUNTIME_DIR") {
        let mut pb = std::path::PathBuf::from(dir);
        pb.push(APP_ID);
        pb.push("viz.fifo");
        return pb.to_string_lossy().into_owned();
    }
    for base in ["/dev/shm", "/tmp"] {
        if std::path::Path::new(base).is_dir() {
            return format!("{base}/{APP_ID}/viz.fifo");
        }
    }
    format!("/tmp/{APP_ID}/viz.fifo")
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum State {
    Stopped,
    Playing,
    Paused,
}

impl State {
    fn as_str(&self) -> &'static str {
        match self {
            State::Stopped => "stopped",
            State::Playing => "playing",
            State::Paused => "paused",
        }
    }
}

/// 共享播放状态。
struct Shared {
    playing: AtomicBool,
    stop: AtomicBool,
    eof: AtomicBool,
    volume: Mutex<f32>,
    /// 源采样率
    in_rate: AtomicU64,
    /// 源声道数
    in_channels: AtomicU64,
    /// 已写出帧数（用于位置）
    frames_out: AtomicU64,
    /// seek 目标（毫秒）；u64::MAX 表示无请求
    seek_target_ms: AtomicU64,
    /// 总时长（毫秒）
    duration_ms: AtomicU64,
    /// DSP 参数（JSON，实时更新）
    dsp_params: Mutex<serde_json::Value>,
    /// 嵌入式 camillalib 配置（YAML 字符串，实时更新）
    camilla_yaml: Mutex<Option<String>>,
    /// 音色染色参数 (tube_drive, bbe_amount)，实时更新
    coloring: Mutex<(f32, f32)>,
    /// 全局 DSP 开关（运行时，由 SetEngine 控制）
    dsp_enabled: AtomicBool,
    _dsp_logged: AtomicU64,
    /// DSD 输出模式（auto/native/dop/pcm）。
    dsd_mode: Mutex<String>,
    /// 输出设备（PipeWire sink 名；空=默认）。
    output_device: Mutex<String>,
}

impl Shared {
    fn position_secs(&self) -> f64 {
        let f = self.frames_out.load(Ordering::SeqCst) as f64;
        let r = self.in_rate.load(Ordering::SeqCst).max(1) as f64;
        (f / r - OUTPUT_LATENCY_SECS).max(0.0)
    }
}

pub struct Engine {
    shared: Arc<Shared>,
    state: State,
    effect: String,
    decode_thread: Option<std::thread::JoinHandle<()>>,
    /// 应用级音频输出（常驻）：切歌复用，仅格式变化时内部重建 stream。
    output: Arc<crate::output::PipewireOutput>,
}

impl Engine {
    pub fn new() -> Self {
        Engine {
            shared: Arc::new(Shared {
                playing: AtomicBool::new(false),
                stop: AtomicBool::new(false),
                eof: AtomicBool::new(false),
                volume: Mutex::new(1.0),
                in_rate: AtomicU64::new(44100),
                in_channels: AtomicU64::new(2),
                frames_out: AtomicU64::new(0),
                seek_target_ms: AtomicU64::new(u64::MAX),
                duration_ms: AtomicU64::new(0),
                dsp_params: Mutex::new(serde_json::Value::Null),
                camilla_yaml: Mutex::new(None),
                coloring: Mutex::new((0.0, 0.0)),
                dsp_enabled: AtomicBool::new(true),
                _dsp_logged: AtomicU64::new(0),
                dsd_mode: Mutex::new("auto".to_string()),
                output_device: Mutex::new(String::new()),
            }),
            state: State::Stopped,
            effect: "off".to_string(),
            decode_thread: None,
            output: Arc::new(crate::output::PipewireOutput::new()),
        }
    }

    pub fn position(&self) -> f64 {
        // 进度基于「实际播放帧数」（RT 回调累加），而非解码/写入位置。
        // 这样暂停/恢复、缓冲余量都不会让进度跳动。
        let rate = self.shared.in_rate.load(Ordering::SeqCst).max(1) as f64;
        let played = self.output.played_frames() as f64;
        (played / rate).max(0.0)
    }

    pub fn state_str(&self) -> &'static str {
        self.state.as_str()
    }

    pub fn is_eof(&self) -> bool {
        self.shared.eof.load(Ordering::SeqCst)
    }

    pub fn duration_secs(&self) -> f64 {
        self.shared.duration_ms.load(Ordering::SeqCst) as f64 / 1000.0
    }

    pub fn handle(&mut self, req: Request) -> Event {
        match req {
            Request::Ping => Event::Pong,
            Request::Play { path } => {
                self.start_play(&path);
                Event::Ack { cmd: "play".into() }
            }
            Request::Pause => {
                eprintln!("[engine] PAUSE (played={} pos={:.3}s)", self.output.played_frames(), self.position());
                self.shared.playing.store(false, Ordering::SeqCst);
                // 不 flush：保留环形缓冲中尚未播放的数据，恢复时无缝续播，
                // 且进度（基于 played_frames）不会跳动。回调转为输出静音。
                self.output.pause();
                self.state = State::Paused;
                Event::State { state: "paused".into() }
            }
            Request::Resume => {
                eprintln!("[engine] RESUME (played={} pos={:.3}s)", self.output.played_frames(), self.position());
                self.shared.playing.store(true, Ordering::SeqCst);
                self.output.resume();
                self.state = State::Playing;
                Event::State { state: "playing".into() }
            }
            Request::Stop => {
                self.shared.playing.store(false, Ordering::SeqCst);
                self.output.flush();
                self.output.pause();
                self.stop_internal();
                Event::Ack { cmd: "stop".into() }
            }
            Request::Seek { seconds } => {
                eprintln!("[engine] SEEK → {:.3}s", seconds);
                let ms = (seconds.max(0.0) * 1000.0) as u64;
                self.shared.seek_target_ms.store(ms, Ordering::SeqCst);
                Event::Ack { cmd: "seek".into() }
            }
            Request::SetVolume { value } => {
                if let Ok(mut v) = self.shared.volume.lock() {
                    *v = value.clamp(0.0, 1.0) as f32;
                }
                Event::Ack { cmd: "set_volume".into() }
            }
            Request::SetEffect { preset } => {
                self.effect = preset;
                Event::Effect { preset: self.effect.clone() }
            }
            Request::SetDsp { params } => {
                if let Ok(mut p) = self.shared.dsp_params.lock() {
                    *p = params.clone();
                }
                Event::Dsp { params }
            }
            Request::SetCamillaYaml { yaml } => {
                if let Ok(mut slot) = self.shared.camilla_yaml.lock() {
                    *slot = Some(yaml);
                }
                Event::Ack { cmd: "set_camilla_yaml".into() }
            }
            Request::SetColoring { tube_drive, bbe_amount } => {
                if let Ok(mut slot) = self.shared.coloring.lock() {
                    *slot = (tube_drive, bbe_amount);
                }
                Event::Ack { cmd: "set_coloring".into() }
            }
            Request::SetEngine { .. } => {
                // Camilla 是否参与由 DSP 参数自动推导，此命令保留兼容
                Event::Ack { cmd: "set_engine".into() }
            }
            Request::ResetDsp => {
                // 重置为默认参数
                let default = crate::dsp::DspParams::default();
                let v = serde_json::to_value(&default).unwrap_or(serde_json::Value::Null);
                if let Ok(mut p) = self.shared.dsp_params.lock() {
                    *p = v.clone();
                }
                Event::Dsp { params: v }
            }
            Request::SetDsdMode { mode } => {
                if let Ok(mut m) = self.shared.dsd_mode.lock() {
                    *m = mode.clone();
                }
                eprintln!("[engine] DSD 模式设为 {mode}");
                Event::Ack { cmd: "set_dsd_mode".into() }
            }
            Request::SetOutputDevice { name } => {
                if let Ok(mut d) = self.shared.output_device.lock() {
                    *d = name.clone();
                }
                // 真正应用：重建 stream 并路由到指定 sink。
                self.output.set_output_device(&name);
                eprintln!("[engine] 输出设备设为 '{name}'");
                Event::Ack { cmd: "set_output_device".into() }
            }
            Request::QueryState => Event::State { state: self.state.as_str().to_string() },
            Request::Shutdown => {
                self.stop_internal();
                Event::Ack { cmd: "shutdown".into() }
            }
        }
    }

    fn stop_internal(&mut self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.playing.store(false, Ordering::SeqCst);
        if let Some(t) = self.decode_thread.take() {
            let _ = t.join();
        }
        self.shared.stop.store(false, Ordering::SeqCst);
        self.state = State::Stopped;
    }

    fn start_play(&mut self, path: &str) {
        eprintln!("[engine] START_PLAY: {path}");
        self.stop_internal();
        let shared = Arc::clone(&self.shared);
        shared.stop.store(false, Ordering::SeqCst);
        shared.eof.store(false, Ordering::SeqCst);
        shared.playing.store(true, Ordering::SeqCst);
        shared.frames_out.store(0, Ordering::SeqCst);
        // 切歌：清空输出缓冲（丢弃上一首残留），复用同一条 PipeWire stream；
        // 进度基准（played_frames）归零。
        self.output.flush();
        self.output.reset_played_frames(0);
        self.output.resume();
        let p = path.to_string();
        let sh = Arc::clone(&shared);
        let out = Arc::clone(&self.output);
        let handle = std::thread::spawn(move || {
            if let Err(e) = run_playback(&p, sh, out) {
                eprintln!("[engine] playback error: {e}");
            }
        });
        self.decode_thread = Some(handle);
        self.state = State::Playing;
    }
}

/// 启动一个 pw-cat 子进程，声明原始采样率/声道，返回其 stdin。
///
/// 用 --properties 覆盖 PipeWire 客户端属性：默认 pw-cat 会在系统媒体控件 /
/// 音量面板里显示为 "pw-cat"，这里改成播放器名（application.name / node.name）。
fn spawn_pwcat(rate: u32, channels: u32) -> Result<std::process::Child, String> {
    use std::process::{Command, Stdio};
    let props = "{ application.name = \"Xiatiao Player\" application.process.binary = \"xiatiao-player\" media.role = \"Music\" node.name = \"Xiatiao Player\" }";
    // PIPEWIRE_PROPS：PipeWire 客户端连接时读取的进程级属性。
    // 用来覆盖 pw-cat 硬编码的 client 名（--properties 只改 node 名，改不了 client 名）。
    let pw_env_props = "{ application.name = \"Xiatiao Player\" application.process.binary = \"xiatiao-player\" node.name = \"Xiatiao Player\" }";
    let child = Command::new("pw-cat")
        .env("PIPEWIRE_PROPS", pw_env_props)
        .args([
            "--playback",
            "--raw",
            "--format", "f32",
            "--rate", &rate.to_string(),
            "--channels", &channels.to_string(),
            "--properties", props,
            "-",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("spawn pw-cat: {e}"))?;
    Ok(child)
}

/// 解码线程：解码并写入 pw-cat。
fn run_playback(path: &str, shared: Arc<Shared>,
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

    // 输出层（DSP 由进程内 DspChain + 可选 CamillaEngine 处理）：
    // 默认原生 PipeWire（client.name = Xiatiao Player）；XIATIAO_AUDIO_BACKEND=pwcat 回退子进程。
    let ch_out = in_channels.max(1) as u32;
    let rate_out = in_rate;

    // 输出方式：默认 pw-cat 子进程（与历史上工作正常的路径一致）；
    // 设 XIATIAO_AUDIO_BACKEND=pipewire 才走原生 PipeWire 接口。
    let use_pwcat = std::env::var("XIATIAO_AUDIO_BACKEND")
        .map(|v| v.to_ascii_lowercase() != "pipewire")
        .unwrap_or(true);
    let mut pwcat: Option<std::process::Child> = if use_pwcat {
        match spawn_pwcat(rate_out, ch_out) {
            Ok(c) => { eprintln!("[engine] 使用 pw-cat 子进程输出"); Some(c) }
            Err(e) => { eprintln!("[engine] pw-cat 启动失败: {e}"); None }
        }
    } else {
        eprintln!("[engine] 使用原生 PipeWire 输出");
        None
    };

    // 可视化旁路：环境变量 XIATIAO_VIZ_FIFO 指定 FIFO，未设则用 $XDG_RUNTIME_DIR/xiatiao/viz.fifo
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
                // 进度基准同步到 seek 位置
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
                    // Camilla 是否参与：由参数自动推导（任一 Camilla 功能开着即参与）
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
                    // 调试用：仅在 XIATIAO_DSP_DEBUG=1 时统计并打印（默认关闭，避免
                    // 在音频回调路径上对整块 buffer 做 fold/any 的无谓遍历）。
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
                    // 可视化旁路：只搬运，不阻塞（FIFO 满/无人读则丢帧）
                    viz.feed(&pcm, ch);
                    if let Some(child) = pwcat.as_mut() {
                        // 对照路径：写 f32le 到 pw-cat stdin
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

/// 执行 seek。
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

// ============================================================
// FFmpeg 子进程解码（APE / WavPack / DSD 等）
// ============================================================

fn prefer_ffmpeg(path: &str) -> bool {
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    matches!(ext.as_str(), "ape" | "wv" | "dsf" | "dff" | "mpc" | "tta")
}

/// 用 ffprobe 读取 (采样率, 声道, 时长秒)。
fn ffprobe_info(path: &str) -> (u32, u32, f64) {
    use std::process::Command;
    let out = Command::new("ffprobe")
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

/// 用 ffmpeg 解码到原始采样率 PCM，再转交 pw-cat。
fn run_playback_ffmpeg(path: &str, shared: Arc<Shared>,
                        output: Arc<crate::output::PipewireOutput>) -> Result<(), String> {
    use std::io::Read as _;
    use std::process::{Command, Stdio};

    let (src_rate, in_channels, dur) = ffprobe_info(path);

    // 原则：ffmpeg 解码出什么采样率，就**全程保持**那个采样率，
    // 不做任何重采样/限幅/滤波（无损、简洁）。采样率转换交给 PipeWire/DAC。
    let in_rate = src_rate;
    eprintln!("[engine/ffmpeg] {in_rate}Hz {in_channels}ch dur={dur:.1}s");

    shared.in_rate.store(in_rate as u64, Ordering::SeqCst);
    shared.in_channels.store(in_channels as u64, Ordering::SeqCst);
    shared.duration_ms.store((dur * 1000.0) as u64, Ordering::SeqCst);
    let ch = in_channels.max(1) as u32;

    // 输出层复用 Engine 级常驻 output（DSP 由进程内 DspChain + CamillaEngine 处理）
    // 可视化旁路：FFmpeg 路径同样接入（DSD/APE/WavPack 等走这里）
    let mut viz = crate::viz::VizTap::open(&viz_fifo_path(), in_rate);
    // DSP 处理链（ffmpeg 路径也要过 DSP）
    let mut dsp = crate::dsp::DspChain::new(in_rate);
    // 始终创建嵌入式 Camilla 引擎（运行时按开关决定是否参与）
    let mut camilla_engine = crate::camilla_engine::CamillaEngine::new(in_rate);
    // 输出方式：默认 pw-cat 子进程；XIATIAO_AUDIO_BACKEND=pipewire 才走原生。
    let use_pwcat = std::env::var("XIATIAO_AUDIO_BACKEND")
        .map(|v| v.to_ascii_lowercase() != "pipewire")
        .unwrap_or(true);
    let mut pwcat: Option<std::process::Child> = if use_pwcat {
        match spawn_pwcat(in_rate, ch) {
            Ok(c) => { eprintln!("[engine/ffmpeg] 使用 pw-cat 子进程输出"); Some(c) }
            Err(e) => { eprintln!("[engine/ffmpeg] pw-cat 启动失败: {e}"); None }
        }
    } else {
        None
    };
    // 启动 ffmpeg：**不加 -ar / -af**，用 ffmpeg 默认采样率输出（保持原样）。
    let mut ff = Command::new("ffmpeg")
        .args(["-v", "error", "-nostdin"])
        .args(["-i", path])
        .args(["-f", "f32le", "-ac", &ch.to_string(), "-"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("spawn ffmpeg: {e}"))?;
    let mut ff_out = ff.stdout.take().ok_or("no ffmpeg stdout")?;

    let mut frames_written: u64 = 0;
    // 持久读取缓冲：一次读大块（128KB），并用 carry 保存跨块残留的
    // 非 4 字节尾部，保证每次处理的都是完整 f32 样本，避免字节错位导致杂音。
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
            ff = Command::new("ffmpeg")
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
        // （read 返回的 n 不保证是 4 的倍数，直接 chunks_exact 会丢字节→错位。）
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
        // Camilla 是否参与：由参数自动推导（任一 Camilla 功能开着即参与）
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
        // 可视化旁路：只搬运，不阻塞（FIFO 满/无人读则丢帧）
        viz.feed(&pcm, ch_usize);
        if let Some(child) = pwcat.as_mut() {
            // 写 f32le 到 pw-cat stdin
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
