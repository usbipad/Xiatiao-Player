//! BBE Sonic Maximizer 模拟：全通相位校正 + 分频重混。
//!
//! 标准做法：全通滤波器（只改相位，不改幅度）实现「低频滞后」。
//! 高频直接通过（相位超前）→ 重混 → 瞬态更清晰。
//! （分频点 1kHz 附近会有相位抵消 → 中低频轻微凹陷，这是 BBE 特性。）

pub struct Bbe {
    amount: f32,
    lp_l: f32, lp_r: f32,
    lp_coef: f32,
    ap_a: f32,
    ap_x1_l: f32, ap_x1_r: f32,
    ap_y1_l: f32, ap_y1_r: f32,
}

impl Bbe {
    pub fn new(amount: f32, samplerate: f32) -> Self {
        let lp_coef = (1.0 - (-2.0 * std::f32::consts::PI * 1000.0 / samplerate).exp()).clamp(0.01, 0.9);
        let amt = amount.clamp(0.0, 1.0);
        let ap_a = -0.9 * amt;
        Bbe {
            amount: amt,
            lp_l: 0.0, lp_r: 0.0,
            lp_coef,
            ap_a,
            ap_x1_l: 0.0, ap_x1_r: 0.0,
            ap_y1_l: 0.0, ap_y1_r: 0.0,
        }
    }

    /// 一阶全通：y = a·x + x1 - a·y1（幅度恒为 1，只改相位）。
    #[inline]
    fn allpass(&self, x: f32, x1: f32, y1: f32) -> f32 {
        self.ap_a * x + x1 - self.ap_a * y1
    }

    /// 处理一帧（立体声）。全通相位校正 + 重混。
    #[inline]
    pub fn process(&mut self, l: f32, r: f32) -> (f32, f32) {
        // 分频
        self.lp_l += self.lp_coef * (l - self.lp_l);
        self.lp_r += self.lp_coef * (r - self.lp_r);
        let low_l = self.lp_l;
        let low_r = self.lp_r;
        let high_l = l - low_l;
        let high_r = r - low_r;

        // 低频过全通（相位滞后）
        let ap_l = self.allpass(low_l, self.ap_x1_l, self.ap_y1_l);
        let ap_r = self.allpass(low_r, self.ap_x1_r, self.ap_y1_r);
        self.ap_x1_l = low_l;
        self.ap_x1_r = low_r;
        self.ap_y1_l = ap_l;
        self.ap_y1_r = ap_r;

        // 重混：滞后低频 + 超前高频
        let out_l = ap_l + high_l;
        let out_r = ap_r + high_r;
        (out_l, out_r)
    }
}
