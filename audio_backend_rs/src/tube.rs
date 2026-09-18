//! 电子管模拟：标准 soft saturation（平滑软限，无硬削）。
//!
//! soft_sat(x) = x / sqrt(1 + x²)：
//!   小信号 ≈ x（线性，不产生谐波）
//!   大信号 → ±1（平滑软限，无拐点）
//! drive 控制「干/湿混合量」（1→原声，3→最湿）。

/// 标准 soft saturation（平滑，无硬削）。
#[inline]
fn soft_sat(x: f32) -> f32 {
    x / (1.0 + x * x).sqrt()
}

pub struct Tube {
    /// 染色混合量 k（0=原声，1=全饱和信号）
    amount: f32,
}

impl Tube {
    pub fn new(drive: f32) -> Self {
        let d = drive.clamp(1.0, 3.0);
        // drive 1 → k=0（原声）；drive 3 → k=1（全湿）
        Tube { amount: ((d - 1.0) / 2.0).clamp(0.0, 1.0) }
    }

    /// 处理一帧（单样本）。
    #[inline]
    pub fn process(&self, x: f32) -> f32 {
        let k = self.amount;
        // 极轻非对称（偶次谐波，几乎不可闻）
        let xa = if x >= 0.0 { x } else { x * 0.98 };
        // 温和饱和（×1.5 让常用电平轻微进入非线性区）
        let sat = soft_sat(xa * 1.5);
        // 干湿混合（k=0 时纯干声）
        x * (1.0 - k) + sat * k
    }
}
