//! 播放引擎的共享状态与常量。
//!
//! 从 engine.rs 拆出：`Shared` 是解码线程与 RT 回调之间共享的状态，
//! 也被 decode / decode_ffmpeg 模块使用，故独立成模块。

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

/// 输出缓冲延迟估算（秒）：pw-cat + PipeWire 的缓冲。
pub(crate) const OUTPUT_LATENCY_SECS: f64 = 0.10;

/// 应用标识：仅用于拼运行时目录，包名变更时改这一处。
const APP_ID: &str = "xiatiao";

/// 解析可视化 FIFO 路径（与 Python 侧保持一致）：
/// 1) 环境变量 XIATIAO_VIZ_FIFO 覆盖；
/// 2) $XDG_RUNTIME_DIR/<APP_ID>/viz.fifo（标准，tmpfs 内存）；
/// 3) /dev/shm/<APP_ID>/viz.fifo（纯内存兜底）；
/// 4) /tmp/<APP_ID>/viz.fifo（最终兜底）。
pub(crate) fn viz_fifo_path() -> String {
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
pub(crate) enum State {
    Stopped,
    Playing,
    Paused,
}

impl State {
    pub(crate) fn as_str(&self) -> &'static str {
        match self {
            State::Stopped => "stopped",
            State::Playing => "playing",
            State::Paused => "paused",
        }
    }
}

/// 共享播放状态。
pub(crate) struct Shared {
    pub(crate) playing: AtomicBool,
    pub(crate) stop: AtomicBool,
    pub(crate) eof: AtomicBool,
    pub(crate) volume: Mutex<f32>,
    /// 源采样率
    pub(crate) in_rate: AtomicU64,
    /// 源声道数
    pub(crate) in_channels: AtomicU64,
    /// 已写出帧数（用于位置）
    pub(crate) frames_out: AtomicU64,
    /// seek 目标（毫秒）；u64::MAX 表示无请求
    pub(crate) seek_target_ms: AtomicU64,
    /// 总时长（毫秒）
    pub(crate) duration_ms: AtomicU64,
    /// DSP 参数（JSON，实时更新）
    pub(crate) dsp_params: Mutex<serde_json::Value>,
    /// 嵌入式 camillalib 配置（YAML 字符串，实时更新）
    pub(crate) camilla_yaml: Mutex<Option<String>>,
    /// 音色染色参数 (tube_drive, bbe_amount)，实时更新
    pub(crate) coloring: Mutex<(f32, f32)>,
    /// 全局 DSP 开关（运行时，由 SetEngine 控制）
    pub(crate) dsp_enabled: AtomicBool,
    pub(crate) _dsp_logged: AtomicU64,
    /// DSD 输出模式（auto/native/dop/pcm）。
    pub(crate) dsd_mode: Mutex<String>,
    /// 输出设备（PipeWire sink 名；空=默认）。
    pub(crate) output_device: Mutex<String>,
    /// 待发送给客户端的错误（解码线程写入，推送线程读取后经 IPC 发出）。
    pub(crate) pending_error: Mutex<Option<String>>,
}

impl Shared {
    pub(crate) fn position_secs(&self) -> f64 {
        let f = self.frames_out.load(Ordering::SeqCst) as f64;
        let r = self.in_rate.load(Ordering::SeqCst).max(1) as f64;
        (f / r - OUTPUT_LATENCY_SECS).max(0.0)
    }

    /// 记录一条待发送给客户端的错误（解码线程调用）。
    pub(crate) fn report_error(&self, msg: impl Into<String>) {
        if let Ok(mut slot) = self.pending_error.lock() {
            *slot = Some(msg.into());
        }
    }

    /// 取出并清空待发送错误（推送线程调用）。
    pub(crate) fn take_error(&self) -> Option<String> {
        self.pending_error.lock().ok().and_then(|mut s| s.take())
    }
}
