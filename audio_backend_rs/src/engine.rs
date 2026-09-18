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

use crate::output::AudioOut;
use crate::protocol::{Event, Request};
use crate::shared::{State, Shared};

// 注：常量 / viz_fifo_path / State / Shared 已抽到 crate::shared；
// symphonia / ffmpeg 解码循环已抽到 crate::decode / crate::decode_ffmpeg。

pub struct Engine {
    shared: Arc<Shared>,
    state: State,
    effect: String,
    decode_thread: Option<std::thread::JoinHandle<()>>,
    /// 应用级音频输出（常驻）：切歌复用。
    /// 默认 PipeWire；指定 ALSA 设备后切换为 ALSA 独占。
    output: Arc<AudioOut>,
    /// 当前播放曲目路径（用于切换输出设备后自动续播）。
    current_path: Option<String>,
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
            output: Arc::new(AudioOut::Pipewire(crate::output::PipewireOutput::new())),
            current_path: None,
        }
    }

    /// 应用输出设备选择：
    ///   - name 为空 → 默认走 PipeWire（系统默认音频接口）；
    ///   - name 非空 → 切换到 ALSA 独占直连该 hw 设备。
    fn apply_output_backend(&mut self, name: &str) {
        // 记录切换前的播放状态，用于切换后自动续播。
        let was_playing = self.state == State::Playing;
        let resume_pos = self.position();
        let resume_path = self.current_path.clone();

        // 切换前先停掉当前解码/输出，避免残留线程持有旧后端。
        self.stop_internal();
        self.output.stop();
        if name.trim().is_empty() {
            self.output = Arc::new(AudioOut::Pipewire(crate::output::PipewireOutput::new()));
            eprintln!("[engine] 输出后端切换为 PipeWire（系统默认）");
        } else {
            self.output = Arc::new(AudioOut::Alsa(crate::output_alsa::AlsaOutput::new(name)));
            eprintln!("[engine] 输出后端切换为 ALSA 独占：{name}");
        }

        // 若切换前正在播放，则从原位置自动续播（避免切设备后静默停止）。
        if was_playing {
            if let Some(path) = resume_path {
                eprintln!("[engine] 切换设备后续播：{path} @ {resume_pos:.3}s");
                self.start_play(&path);
                if resume_pos > 0.5 {
                    let ms = (resume_pos * 1000.0) as u64;
                    self.shared.seek_target_ms.store(ms, Ordering::SeqCst);
                }
            }
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
    ///
    /// 合并两个来源：解码/引擎错误（shared）与输出后端运行时错误
    /// （如 ALSA 独占打开/配置失败）。
    pub fn take_error(&self) -> Option<String> {
        if let Some(e) = self.shared.take_error() {
            return Some(e);
        }
        self.output.take_error()
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
                self.shared.stop.store(true, Ordering::SeqCst);
                // 顺序关键：先停输出后端（写线程退出、stop 标志置位），
                // 使解码线程卡在 write 时能立即感知并返回；
                // 再 join 解码线程。否则 join 会因解码线程卡在 write 而死锁。
                self.output.stop();
                self.stop_internal();
                // 用户主动停止：清空当前曲目，切设备时不再续播。
                self.current_path = None;
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

                // 若当前正在播放 DSD 文件，切模式后重新加载，使新模式真正生效
                // （否则模式切换对已在播放的流无效，用户会以为"没生效"）。
                let is_dsd = self
                    .current_path
                    .as_deref()
                    .map(crate::dsd::is_dsd_file)
                    .unwrap_or(false);
                if is_dsd && self.state == State::Playing {
                    if let Some(path) = self.current_path.clone() {
                        let pos = self.position();
                        eprintln!("[engine] 切换 DSD 模式，重载当前文件：{path} @ {pos:.3}s");
                        self.start_play(&path);
                        if pos > 0.5 {
                            let ms = (pos * 1000.0) as u64;
                            self.shared.seek_target_ms.store(ms, Ordering::SeqCst);
                        }
                    }
                }
                Event::Ack { cmd: "set_dsd_mode".into() }
            }
            Request::SetOutputDevice { name } => {
                if let Ok(mut d) = self.shared.output_device.lock() {
                    *d = name.clone();
                }
                // 真正应用：空=默认走 PipeWire；非空=切换到 ALSA 独占设备。
                self.apply_output_backend(&name);
                eprintln!("[engine] 输出设备设为 '{name}'");
                Event::Ack { cmd: "set_output_device".into() }
            }
            Request::ListOutputDevices => {
                let devices = crate::output_alsa::list_devices()
                    .into_iter()
                    .map(|d| crate::protocol::DeviceInfo {
                        id: d.id,
                        description: d.description,
                    })
                    .collect();
                Event::OutputDevices { devices }
            }
            Request::QueryState => Event::State { state: self.state.as_str().to_string() },
            Request::Shutdown => {
                self.stop_internal();
                self.output.stop();
                Event::Ack { cmd: "shutdown".into() }
            }
        }
    }

    fn stop_internal(&mut self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.playing.store(false, Ordering::SeqCst);
        // 中断输出 write：让卡在 output.write 的解码线程立即退出，
        // 否则 join 会死锁（输出缓冲满、无人消费）。
        self.output.abort_write();
        if let Some(t) = self.decode_thread.take() {
            let _ = t.join();
        }
        self.shared.stop.store(false, Ordering::SeqCst);
        // 解码线程已退出，清除中断标志，供下次播放使用。
        self.output.clear_abort();
        self.state = State::Stopped;
    }

    fn start_play(&mut self, path: &str) {
        eprintln!("[engine] START_PLAY: {path}");
        // 记录当前曲目，供切换输出设备后续播。
        self.current_path = Some(path.to_string());
        self.stop_internal();
        // 确保中断标志已清除（stop_internal 已清，这里双保险）。
        self.output.clear_abort();
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
