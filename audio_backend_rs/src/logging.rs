//! 轻量日志：给关键路径打带毫秒时间戳的 eprintln，便于定位切歌/切档延迟。
//!
//! 用法：`logts!("[engine] START_PLAY: {path}")` —— 自动加 `[HH:MM:SS.mmm]` 前缀。
//!
//! 级别：
//! - `logts!`   常规信息，正常启动也输出（切歌、seek、设备切换等关键事件）
//! - `logts_dbg!` 调试细节（IPC 原始报文、缓冲重建细节等），仅在
//!   `XIATIAO_DEBUG` 非空且非 0/false/no/off 时输出。

use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

/// 返回当前本地时间的 `HH:MM:SS.mmm`（用 UNIX 秒对齐，够定位用）。
pub fn now_hms() -> String {
    let d = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = d.as_secs();
    let ms = d.subsec_millis();
    let h = (secs / 3600) % 24;
    let m = (secs / 60) % 60;
    let s = secs % 60;
    format!("{:02}:{:02}:{:02}.{:03}", h, m, s, ms)
}

/// 是否处于调试模式（与 Python 侧 XIATIAO_DEBUG 语义一致）。
pub fn debug_enabled() -> bool {
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| {
        let v = std::env::var("XIATIAO_DEBUG").unwrap_or_default();
        let v = v.trim().to_ascii_lowercase();
        !matches!(v.as_str(), "" | "0" | "false" | "no" | "off")
    })
}

#[macro_export]
macro_rules! logts {
    ($($arg:tt)*) => {
        eprintln!("[{}] {}", $crate::logging::now_hms(), format!($($arg)*))
    };
}

/// 调试级别日志：仅 `XIATIAO_DEBUG` 开启时输出。
#[macro_export]
macro_rules! logts_dbg {
    ($($arg:tt)*) => {
        if $crate::logging::debug_enabled() {
            eprintln!("[{}] {}", $crate::logging::now_hms(), format!($($arg)*));
        }
    };
}
