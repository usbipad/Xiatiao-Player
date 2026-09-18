//! 播放引擎：symphonia/ffmpeg 解码 + pw-cat 输出（流式）。
//!
//! 输出层用 PipeWire 的 pw-cat 子进程：
//! - 解码得到原始采样率的 f32 PCM；
//! - 写入 pw-cat 的 stdin（--rate/--channels 用原始值）；
//! - 由 PipeWire 决定是否转换采样率（应用不重采样）。
//!
//! 音量在写之前对 PCM 乘（实时生效）。
//! 位置按已写入帧数估算（减去输出缓冲延迟）。

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use crate::protocol::{Event, Request};
use crate::shared::{State, Shared};

// 注：常量 / viz_fifo_path / State / Shared 已抽到 crate::shared；
// symphonia / ffmpeg 解码循环已抽到 crate::decode / crate::decode_ffmpeg。

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
                pending_error: Mutex::new(None),
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

    /// 取出并清空待发送给客户端的错误（推送线程调用）。
    pub fn take_error(&self) -> Option<String> {
        self.shared.take_error()
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
            if let Err(e) = crate::decode::run_playback(&p, sh, out) {
                eprintln!("[engine] playback error: {e}");
            }
        });
        self.decode_thread = Some(handle);
        self.state = State::Playing;
    }
}
