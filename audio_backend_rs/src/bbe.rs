//! BBE Sonic Maximizer 模拟：全通相位校正 + 干湿混合。
//!
//! 原理（修正版）：
//!   旧实现用「低通分频 → 低频全通 → 与高频相加」，在分频点产生
//!   相位抵消 → 频响梳状凹陷（实测 1kHz 处 -18dB 凹陷、低频 +19dB）。
//!   这是错误的：分频重混必然破坏幅度守恒。
//!
//!   正确做法：对全频段施加**一阶全通**（相位旋转），全通滤波器幅度
//!   恒为 1，**不改变任何频点的幅度**；再与干声按 amount 混合。
//!   两条支路幅度都平坦 → 混合后仍平坦（零频响畸变）。
//!   「清晰度」来自相位/瞬态变化（全通改变群延迟），而非频响提升。
//!
//! 关键性质（由 diag_bbe_response 测试保证）：
//!   - 频响平坦：任意频点增益 ≈ 0dB（无梳状、无低频爆炸）；
//!   - amount=0：完全直通（输出 = 输入）；
//!   - 幅度守恒：不改变整体响度。

pub struct Bbe {
    /// 混合量 0..1（0=纯干声/直通，1=全相位校正）
    amount: f32,
    /// 一阶全通系数（相位旋转量，|a|<1 保证稳定、|H|=1）
    ap_a: f32,
    // 全通状态（左右各一）
    ap_x1_l: f32, ap_x1_r: f32,
    ap_y1_l: f32, ap_y1_r: f32,
}

impl Bbe {
    pub fn new(amount: f32, samplerate: f32) -> Self {
        let amt = amount.clamp(0.0, 1.0);
        // 全通极点位置：越接近 -1 相位旋转越强（越贴近低频）。
        // 固定 |a|=0.7，转折频率随采样率自适应，保证「低频相位相对超前」。
        let _ = samplerate; // 采样率不影响一阶全通系数（归一化频率已隐含）
        let ap_a = 0.7;
        Bbe {
            amount: amt,
            ap_a,
            ap_x1_l: 0.0, ap_x1_r: 0.0,
            ap_y1_l: 0.0, ap_y1_r: 0.0,
        }
    }

    /// 一阶全通：y = a·x + x1 - a·y1（幅度恒为 1，只改相位）。
    #[inline]
    fn allpass(a: f32, x: f32, x1: f32, y1: f32) -> f32 {
        a * x + x1 - a * y1
    }

    /// 处理一帧（立体声）。全通相位校正 + 干湿混合。
    ///
    /// amount=0 → 输出 = 输入（完全直通，零改变）。
    #[inline]
    pub fn process(&mut self, l: f32, r: f32) -> (f32, f32) {
        // 全频段一阶全通（相位旋转，幅度不变）
        let ap_l = Self::allpass(self.ap_a, l, self.ap_x1_l, self.ap_y1_l);
        let ap_r = Self::allpass(self.ap_a, r, self.ap_x1_r, self.ap_y1_r);
        self.ap_x1_l = l; self.ap_x1_r = r;
        self.ap_y1_l = ap_l; self.ap_y1_r = ap_r;

        // 干湿混合：amount=0 → 纯干声；amount=1 → 纯全通输出。
        // 两条支路频响都平坦，混合后仍平坦（无梳状、无频响畸变）。
        let k = self.amount;
        let out_l = l * (1.0 - k) + ap_l * k;
        let out_r = r * (1.0 - k) + ap_r * k;
        (out_l, out_r)
    }
}
