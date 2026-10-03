//! 轻量日志：给关键路径打带毫秒时间戳的 eprintln，便于定位切歌/切档延迟。
//!
//! 用法：`logts!("[engine] START_PLAY: {path}")` —— 自动加 `[HH:MM:SS.mmm]` 前缀。

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

#[macro_export]
macro_rules! logts {
    ($($arg:tt)*) => {
        eprintln!("[{}] {}", $crate::logging::now_hms(), format!($($arg)*))
    };
}
