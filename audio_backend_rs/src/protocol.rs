//! IPC 协议：请求命令与事件。

use serde::{Deserialize, Serialize};

/// 客户端 -> 后端 的请求。
#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    Ping,
    Play { path: String },
    Pause,
    Resume,
    Stop,
    Seek { seconds: f64 },
    SetVolume { value: f64 },
    SetEffect { preset: String },
    /// 完整的 DSP 参数（JSON 对象），实时生效。
    SetDsp { params: serde_json::Value },
    /// 嵌入式 camillalib 配置（YAML 字符串），实时生效。
    SetCamillaYaml { yaml: String },
    /// 运行时切换引擎参与：camilla=true 时 Camilla 引擎参与串联。
    SetEngine { camilla: bool },
    /// 音色染色参数（电子管 drive / BBE amount），实时生效。
    SetColoring { tube_drive: f32, bbe_amount: f32 },
    /// 重置 DSP 到默认参数，返回默认参数。
    ResetDsp,
    /// 设置 DSD 输出模式（auto/native/dop/pcm）。
    SetDsdMode { mode: String },
    /// 设置输出设备（ALSA hw 设备名；空=默认走 PipeWire）。
    SetOutputDevice { name: String },
    /// 枚举可用的 ALSA 硬件输出设备。
    ListOutputDevices,
    /// 加载卷积 IR（wav 文件路径），后续播放生效。

    /// 清除已加载的卷积 IR。

    QueryState,
    Shutdown,
}

/// 后端 -> 客户端 的事件（含命令应答）。
#[derive(Debug, Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum Event {
    Ready,
    Pong,
    Ack { cmd: String },
    Error { message: String },
    Position { sec: f64 },
    Duration { sec: f64 },
    State { state: String },
    EndOfStream,
    AudioInfo { info: serde_json::Value },
    Effect { preset: String },
    Dsp { params: serde_json::Value },
    /// 输出设备列表：[{"id":"hw:...","description":"..."}]。
    OutputDevices { devices: Vec<DeviceInfo> },
}

/// 输出设备信息。
#[derive(Debug, Serialize, Clone)]
pub struct DeviceInfo {
    pub id: String,
    pub description: String,
}

impl Event {
    pub fn error(msg: &str) -> Self {
        Event::Error { message: msg.to_string() }
    }
}
