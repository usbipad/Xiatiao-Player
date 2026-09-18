//! 4x 过采样（线性插值 + 低通下采样），用于非线性染色抗混叠。
//!
//! 线性插值：保留原样本 + 插值点，**不砍高频**。
//! 下采样：低通（截止 = 原 Nyquist），抽取。

/// 4x 过采样器（线性插值 + 低通）。
pub struct Oversample2x {
    // 下采样低通（级联两级单极点）
    lp1_l: f32, lp1_r: f32,
    lp2_l: f32, lp2_r: f32,
    coef: f32,
}

impl Oversample2x {
    pub fn new() -> Self {
        // 4x：原 Nyquist 在过采样域是 1/4；低通截止略高于 1/4（保原高频）
        // 单极点低通系数（越大截止越高）。0.9 → 通带到 20k 仅 -3dB
        Oversample2x {
            lp1_l: 0.0, lp1_r: 0.0,
            lp2_l: 0.0, lp2_r: 0.0,
            coef: 0.9,
        }
    }

    /// 4x 上采样：输入 n 帧，输出 4n 帧（线性插值，保留原样本）。
    pub fn upsample(&self, input: &[f32], output: &mut Vec<f32>) {
        output.clear();
        let frames = input.len() / 2;
        if frames == 0 { return; }
        for i in 0..frames {
            let l = input[i * 2];
            let r = input[i * 2 + 1];
            let nl = if i + 1 < frames { input[(i + 1) * 2] } else { l };
            let nr = if i + 1 < frames { input[(i + 1) * 2 + 1] } else { r };
            // 4 个点：原样本 + 3 个插值（含下一原样本前的 3/4）
            for k in 0..4 {
                let t = k as f32 / 4.0;
                output.push(l + (nl - l) * t);
                output.push(r + (nr - r) * t);
            }
        }
    }

    /// 4x 下采样：输入 4n 帧，输出 n 帧（低通 + 抽取）。
    pub fn downsample(&mut self, input: &[f32], output: &mut Vec<f32>) {
        output.clear();
        let frames = input.len() / 2;
        let mut i = 0;
        while i < frames {
            // 级联两级单极点低通
            self.lp1_l += self.coef * (input[i * 2] - self.lp1_l);
            self.lp1_r += self.coef * (input[i * 2 + 1] - self.lp1_r);
            self.lp2_l += self.coef * (self.lp1_l - self.lp2_l);
            self.lp2_r += self.coef * (self.lp1_r - self.lp2_r);
            output.push(self.lp2_l);
            output.push(self.lp2_r);
            i += 4; // 抽取 4
        }
    }
}
