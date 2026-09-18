//! 选项B验证：用 camillalib 跑完整处理管线（Stdin → PEQ → Stdout）。
//!
//! 复刻 bin.rs 的启动流程：
//!   processing 线程 + capture 线程 + playback 线程 + barrier(4)
//!
//! 用法：
//!   cargo run --example camilla_pipeline -- examples/camilla_pipeline.yml < in.raw > out.raw

use std::sync::{Arc, Barrier};

use camillalib::{ProcessingParameters, audiodevice, config, processing};
use camillalib::{CaptureStatus, PlaybackStatus};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let cfg_path = args.get(1).map(|s| s.as_str()).unwrap_or("examples/camilla_pipeline.yml");

    // 1) 加载配置
    let conf = match config::load_config(cfg_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("加载配置失败: {e}");
            std::process::exit(1);
        }
    };
    println!("[example] 配置加载成功: devices={:?}", conf.devices.samplerate);

    // 2) 通道 + barrier(4)
    let (tx_cap, rx_cap) = crossbeam_channel::unbounded();
    let (tx_pb, rx_pb) = crossbeam_channel::unbounded();
    let (tx_status, _rx_status) = crossbeam_channel::unbounded();
    let (tx_command, rx_command) = crossbeam_channel::unbounded();
    let (_tx_pipeconf, rx_pipeconf) = crossbeam_channel::unbounded();
    let barrier = Arc::new(Barrier::new(4));

    let params = Arc::new(ProcessingParameters::new(
        &[0.0, 0.0, 0.0, 0.0, 0.0],
        &[false, false, false, false, false],
    ));

    // 状态结构
    let capture_status = Arc::new(parking_lot::RwLock::new(CaptureStatus {
        update_interval: 1000,
        measured_samplerate: conf.devices.samplerate,
        signal_range: 0.0,
        signal_rms: camillalib::utils::countertimer::ValueHistory::new(10, 100),
        signal_peak: camillalib::utils::countertimer::ValueHistory::new(10, 100),
        state: camillalib::ProcessingState::Starting,
        rate_adjust: 1.0,
        used_channels: vec![true, true],
    }));
    let playback_status = Arc::new(parking_lot::RwLock::new(PlaybackStatus {
        update_interval: 1000,
        clipped_samples: 0,
        buffer_level: 0,
        signal_rms: camillalib::utils::countertimer::ValueHistory::new(10, 100),
        signal_peak: camillalib::utils::countertimer::ValueHistory::new(10, 100),
    }));

    // 3) processing 线程
    processing::run_processing(
        conf.clone(),
        Arc::clone(&barrier),
        tx_pb,
        rx_cap,
        rx_pipeconf,
        Arc::clone(&params),
    );
    println!("[example] processing 线程已启动");

    // 4) playback 线程
    let mut pb = audiodevice::new_playback_device(conf.clone().devices);
    let _pb_handle = match pb.start(
        rx_pb,
        Arc::clone(&barrier),
        tx_status.clone(),
        Arc::clone(&playback_status),
    ) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("playback 启动失败: {e}");
            std::process::exit(1);
        }
    };
    println!("[example] playback 线程已启动");

    // 5) capture 线程
    let mut cap = audiodevice::new_capture_device(conf.clone().devices);
    let _cap_handle = match cap.start(
        tx_cap,
        Arc::clone(&barrier),
        tx_status.clone(),
        rx_command,
        Arc::clone(&capture_status),
        Arc::clone(&params),
    ) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("capture 启动失败: {e}");
            std::process::exit(1);
        }
    };
    println!("[example] capture 线程已启动，开始处理");

    // 6) 等 capture 线程退出（Stdin EOF 后 capture 发 EndOfStream，链式退出）
    let _ = _cap_handle.join();
    println!("[example] 处理完成");
}
