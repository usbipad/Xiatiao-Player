//! 过采样频响测试（同步 oversample.rs 的线性插值版）。

struct Oversample4x {
    lp1_l: f32, lp1_r: f32,
    lp2_l: f32, lp2_r: f32,
    coef: f32,
}

impl Oversample4x {
    fn new() -> Self {
        Oversample4x { lp1_l: 0.0, lp1_r: 0.0, lp2_l: 0.0, lp2_r: 0.0, coef: 0.9 }
    }
    fn upsample(&self, input: &[f32], output: &mut Vec<f32>) {
        output.clear();
        let frames = input.len() / 2;
        if frames == 0 { return; }
        for i in 0..frames {
            let l = input[i * 2]; let r = input[i * 2 + 1];
            let nl = if i + 1 < frames { input[(i + 1) * 2] } else { l };
            let nr = if i + 1 < frames { input[(i + 1) * 2 + 1] } else { r };
            for k in 0..4 {
                let t = k as f32 / 4.0;
                output.push(l + (nl - l) * t);
                output.push(r + (nr - r) * t);
            }
        }
    }
    fn downsample(&mut self, input: &[f32], output: &mut Vec<f32>) {
        output.clear();
        let frames = input.len() / 2;
        let mut i = 0;
        while i < frames {
            self.lp1_l += self.coef * (input[i * 2] - self.lp1_l);
            self.lp1_r += self.coef * (input[i * 2 + 1] - self.lp1_r);
            self.lp2_l += self.coef * (self.lp1_l - self.lp2_l);
            self.lp2_r += self.coef * (self.lp1_r - self.lp2_r);
            output.push(self.lp2_l);
            output.push(self.lp2_r);
            i += 4;
        }
    }
}

fn test_freq(freq: f32) {
    let sr = 48000.0f32;
    let n = 8192;
    let ov = Oversample4x::new();
    let mut input = Vec::with_capacity(n * 2);
    for i in 0..n {
        let v = (2.0 * std::f32::consts::PI * freq * i as f32 / sr).sin() * 0.5;
        input.push(v); input.push(v);
    }
    let mut up = Vec::new();
    ov.upsample(&input, &mut up);
    let mut ovm = Oversample4x::new();
    ovm.coef = ov.coef;
    let mut down = Vec::new();
    ovm.downsample(&up, &mut down);
    let mut out_peak = 0.0f32;
    for i in 1024..(down.len() / 2) {
        let v = down[i * 2].abs();
        if v > out_peak { out_peak = v; }
    }
    println!("{freq:>6}Hz: in=0.5 out={out_peak:.4} ({:.1} dB)", 20.0*(out_peak/0.5).max(1e-9).log10());
}

fn main() {
    for f in [100.0, 1000.0, 5000.0, 10000.0, 15000.0, 20000.0] {
        test_freq(f);
    }
}
