//! 最小验证：camillalib 作为库能否编译、核心类型是否可用。
//!
//! 目标不是跑音频，而是验证「嵌入 camillalib」的第一步：
//!   1. 依赖树能否解析、编译
//!   2. 核心类型（ProcessingParameters 等）能否引用
//!
//! 编译：cargo build --example camilla_embed

fn main() {
    // 1) 核心处理参数（fader 音量/静音）
    let params = camillalib::ProcessingParameters::new(
        &[0.0, 0.0, 0.0, 0.0, 0.0],
        &[false, false, false, false, false],
    );
    println!("ProcessingParameters 创建成功");
    println!("  target_volume(fader0) = {}", params.target_volume(0));

    // 2) 采样格式类型
    let fmt = camillalib::config::FileSampleFormat::F32_LE;
    println!("FileSampleFormat::F32LE = {:?}", fmt);

    // 3) 支持的设备类型（纯函数，不碰硬件）
    let (playback, capture) = camillalib::list_supported_devices();
    println!("支持的输出设备: {:?}", playback);
    println!("支持的输入设备: {:?}", capture);

    // 4) 状态枚举
    let st = camillalib::ProcessingState::Inactive;
    println!("ProcessingState = {}", st);

    println!("=== camillalib 嵌入验证通过 ===");
}
