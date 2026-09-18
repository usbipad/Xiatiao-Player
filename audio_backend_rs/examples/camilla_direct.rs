//! 直接调 Pipeline::process_chunk 验证 PEQ 增益（绕开线程/设备）。
//!
//! 输入 1kHz 正弦幅度 0.1，PEQ +6dB @1kHz → 输出应约 0.2。

use std::sync::Arc;

use camillalib::audiochunk::AudioChunk;
use camillalib::config;
use camillalib::pipeline::Pipeline;
use camillalib::ProcessingParameters;

fn main() {
    let cfg_path = std::env::args().nth(1)
        .unwrap_or_else(|| "examples/camilla_pipeline.yml".to_string());

    // 1) 加载配置
    let conf = match config::load_config(&cfg_path) {
        Ok(c) => c,
        Err(e) => { eprintln!("加载配置失败: {e}"); std::process::exit(1); }
    };
    let samplerate = conf.devices.samplerate;
    let chunksize = conf.devices.chunksize;
    println!("[direct] 采样率={samplerate} chunksize={chunksize}");

    // 2) 建 pipeline
    let params = Arc::new(ProcessingParameters::default());
    let mut pipeline = Pipeline::from_config(conf, params);
    println!("[direct] pipeline 构建成功");

    // 3) 构造输入：1kHz 正弦，幅度 0.1，双声道
    let freq = 1000.0_f64;
    let amp = 0.1_f64;
    let mut ch0 = Vec::with_capacity(chunksize);
    let mut ch1 = Vec::with_capacity(chunksize);
    for i in 0..chunksize {
        let v = amp * (2.0 * std::f64::consts::PI * freq * i as f64 / samplerate as f64).sin();
        ch0.push(v);
        ch1.push(v);
    }
    let in_peak = amp;
    let waveforms = vec![ch0, ch1];
    let chunk = AudioChunk::new(waveforms, in_peak, -in_peak, chunksize, chunksize);
    println!("[direct] 输入峰值 = {in_peak:.4}");

    // 4) 处理（跑几次，让滤波器状态稳定）
    let mut out = pipeline.process_chunk(chunk);
    for _ in 0..5 {
        // 再喂几块同样的，收敛
        let mut ch0 = Vec::with_capacity(chunksize);
        let mut ch1 = Vec::with_capacity(chunksize);
        for i in 0..chunksize {
            let v = amp * (2.0 * std::f64::consts::PI * freq * i as f64 / samplerate as f64).sin();
            ch0.push(v); ch1.push(v);
        }
        out = pipeline.process_chunk(AudioChunk::new(vec![ch0, ch1], in_peak, -in_peak, chunksize, chunksize));
    }

    // 5) 取输出峰值
    let mut out_peak: f64 = 0.0;
    for ch in &out.waveforms {
        for &s in ch {
            if s.abs() > out_peak { out_peak = s.abs(); }
        }
    }
    println!("[direct] 输出峰值 = {out_peak:.4}");
    let gain_db = 20.0 * (out_peak / in_peak).log10();
    println!("[direct] 增益 = {:.2} dB（预期约 +6dB）", gain_db);
    if (gain_db - 6.0).abs() < 1.5 {
        println!("=== PEQ 验证通过 ===");
    } else {
        println!("=== 增益不符预期，需排查 ===");
    }
}
