//! 卷积对齐分析：脉冲延迟测试 + 真实音频干湿混合。
//!
//! 用法：cargo run --example conv_test -- <IR> <输入.wav> <输出前缀>

#[path = "../src/tube.rs"]
mod tube;
#[path = "../src/bbe.rs"]
mod bbe;
#[path = "../src/convolution.rs"]
mod convolution;
#[path = "../src/reverb.rs"]
mod reverb;
#[path = "../src/dsp.rs"]
mod dsp;

use std::io::{Read, Write};
use dsp::{DspChain, DspParams};

const CH: usize = 2;
const RATE: u32 = 48000;

fn read_wav_f32(path: &str) -> Vec<f32> {
    let mut data = Vec::new();
    std::fs::File::open(path).unwrap().read_to_end(&mut data).unwrap();
    let mut pos = 12usize;
    while pos + 8 <= data.len() {
        let id = [data[pos], data[pos+1], data[pos+2], data[pos+3]];
        let size = u32::from_le_bytes([data[pos+4], data[pos+5], data[pos+6], data[pos+7]]) as usize;
        if &id == b"data" {
            let start = pos + 8;
            let end = (start + size).min(data.len());
            let mut out = Vec::with_capacity((end - start) / 2);
            let mut i = start;
            while i + 2 <= end {
                out.push(i16::from_le_bytes([data[i], data[i+1]]) as f32 / 32768.0);
                i += 2;
            }
            return out;
        }
        pos += 8 + size + (size & 1);
    }
    Vec::new()
}

fn write_raw(path: &str, data: &[f32]) {
    let bytes: Vec<u8> = data.iter().flat_map(|s| s.to_le_bytes()).collect();
    std::fs::File::create(path).unwrap().write_all(&bytes).unwrap();
}

fn new_dsp(ir_path: &str, dry: f32, wet: f32) -> DspChain {
    let mut dsp = DspChain::new(RATE);
    let (ir_l, ir_r) = reverb::load_ir_wav(ir_path).unwrap();
    dsp.set_convolution_ir(&ir_l, &ir_r).ok();
    let mut p = DspParams::default();
    p.enabled = true;
    p.convolution_enabled = true;
    p.convolution_dry = dry;
    p.convolution_wet = wet;
    dsp.set_params(p);
    dsp
}

/// 喂 1000 处脉冲，返回 (首非零, 峰值)
fn pulse_peak(ir: &str, dry: f32, wet: f32) -> (Option<usize>, usize) {
    let mut dsp = new_dsp(ir, dry, wet);
    let total = 4000usize;
    let mut buf = vec![0.0f32; total * CH];
    buf[1000 * CH] = 1.0; buf[1000 * CH + 1] = 1.0;
    let mut off = 0usize;
    while off < total { let e=(off+512).min(total); dsp.process_interleaved(&mut buf[off*CH..e*CH],CH); off=e; }
    let mut first = None; let mut peak=0usize; let mut pv=0.0f32;
    for i in 0..total { let v=buf[i*CH].abs(); if first.is_none() && v>1e-5 {first=Some(i);} if v>pv {pv=v; peak=i;} }
    (first, peak)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let ir = args.get(1).map(|s| s.as_str())
        .unwrap_or("/tmp/ir.wav");
    let in_path = args.get(2).map(|s| s.as_str()).unwrap_or("/tmp/conv_src.wav");
    let prefix = args.get(3).map(|s| s.as_str()).unwrap_or("/tmp/conv_out");

    let (df, dp) = pulse_peak(ir, 1.0, 0.0);
    let (wf, wp) = pulse_peak(ir, 0.0, 1.0);
    println!("[conv_test] 脉冲@1000 纯干: 首非零@{:?} 峰值@{}", df, dp);
    println!("[conv_test] 脉冲@1000 纯湿: 首非零@{:?} 峰值@{}", wf, wp);

    let input = read_wav_f32(in_path);
    println!("[conv_test] 输入 {} 帧", input.len() / CH);
    for (dry, wet, name) in [(1.0, 0.0, "dry"), (0.0, 1.0, "wet"), (0.5, 0.5, "mix")] {
        let mut dsp = new_dsp(ir, dry, wet);
        let mut buf = input.clone();
        let total = buf.len() / CH;
        let mut off = 0usize;
        while off < total { let e=(off+512).min(total); dsp.process_interleaved(&mut buf[off*CH..e*CH],CH); off=e; }
        write_raw(&format!("{prefix}_{name}.raw"), &buf);
    }
    println!("[conv_test] 已写出 {prefix}_dry/wet/mix.raw");
}
