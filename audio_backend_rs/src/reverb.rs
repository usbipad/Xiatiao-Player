//! 高质量混响（Freeverb 架构 + 预延迟 + 低切）。
//!
//! 结构：
//!   输入 → 预延迟 → [8 梳状（含阻尼 + 反馈衰减）] → 串行 [4 全通] → 立体声宽度 → 干湿混合
//!
//! 设计参考 Freeverb（Jezar 的经典开源算法）并做专业增强：
//!   - 预延迟（Pre-delay）：干声与混响之间留间隔，提升清晰度；
//!   - 低切（Low-cut）：去掉混响低频浑浊（专业混响必备）；
//!   - 阻尼（Damping）：高频随时间衰减，模拟真实空间吸声；
//!   - 立体声扩展：左右梳状延迟偏移，声场更宽。

/// 梳状滤波器延迟长度（采样，44.1kHz 基准，素数长度避免梳状叠加）。
const COMB_TUNING: [usize; 8] = [1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617];
/// 全通滤波器延迟长度（采样）。
const ALLPASS_TUNING: [usize; 4] = [556, 441, 341, 225];
/// 立体声扩展：右声道相对左声道的延迟偏移（采样，Freeverb 经典值）。
const STEREO_SPREAD: usize = 23;

/// 单个梳状滤波器（含阻尼低通 + 反馈衰减）。
struct Comb {
    buf: Vec<f32>,
    pos: usize,
    /// 阻尼低通状态
    filter_state: f32,
    /// 反馈系数（由 room_size / decay 决定）
    feedback: f32,
    /// 阻尼系数（高频衰减量）
    damp: f32,
}

impl Comb {
    fn new(size: usize) -> Self {
        Comb {
            buf: vec![0.0; size.max(1)],
            pos: 0,
            filter_state: 0.0,
            feedback: 0.5,
            damp: 0.5,
        }
    }

    fn set(&mut self, feedback: f32, damp: f32) {
        self.feedback = feedback.clamp(0.0, 0.98);
        self.damp = damp.clamp(0.0, 0.99);
    }

    /// 处理一个样本：阻尼低通 + 反馈。
    ///
    /// `mod_offset`：调制偏移（采样数，含小数，做线性插值），
    /// 让读取位置轻微游走，打散固定驻波 → 减少金属感。
    #[inline]
    fn process(&mut self, x: f32, mod_offset: f32) -> f32 {
        let n = self.buf.len();
        // 读取位置 = 当前位置 + 调制偏移（回绕）
        let mut rp = self.pos as f32 + mod_offset;
        while rp < 0.0 { rp += n as f32; }
        while rp >= n as f32 { rp -= n as f32; }
        let i0 = rp as usize % n;
        let i1 = (i0 + 1) % n;
        let frac = rp - rp.floor();
        // 线性插值读取
        let out = self.buf[i0] * (1.0 - frac) + self.buf[i1] * frac;
        // 阻尼：高频随时间衰减（低通滤波反馈信号）
        self.filter_state = out * (1.0 - self.damp) + self.filter_state * self.damp;
        // 写入：输入 + 反馈（衰减）
        self.buf[self.pos] = x + self.filter_state * self.feedback;
        self.pos = (self.pos + 1) % n;
        out
    }
}

/// 单个全通滤波器（扩散，只改相位）。
struct Allpass {
    buf: Vec<f32>,
    pos: usize,
    feedback: f32,
}

impl Allpass {
    fn new(size: usize) -> Self {
        Allpass { buf: vec![0.0; size.max(1)], pos: 0, feedback: 0.5 }
    }

    #[inline]
    fn process(&mut self, x: f32) -> f32 {
        let buf_out = self.buf[self.pos];
        let out = -x + buf_out;
        self.buf[self.pos] = x + buf_out * self.feedback;
        self.pos = (self.pos + 1) % self.buf.len();
        out
    }
}

/// 一阶高通（低切，去混响低频浑浊）。
#[derive(Clone, Copy)]
struct HighPass {
    prev_x: f32,
    prev_y: f32,
    coef: f32,
}

impl HighPass {
    fn new(cutoff_hz: f32, rate: f32) -> Self {
        // 一阶高通：y[n] = a*(y[n-1] + x[n] - x[n-1])
        let rc = 1.0 / (2.0 * std::f32::consts::PI * cutoff_hz.max(1.0));
        let dt = 1.0 / rate.max(1.0);
        let a = rc / (rc + dt);
        HighPass { prev_x: 0.0, prev_y: 0.0, coef: a }
    }

    #[inline]
    fn process(&mut self, x: f32) -> f32 {
        let y = self.coef * (self.prev_y + x - self.prev_x);
        self.prev_x = x;
        self.prev_y = y;
        y
    }
}

/// 预延迟（环形缓冲）。
struct PreDelay {
    buf: Vec<f32>,
    pos: usize,
    len: usize,
}

impl PreDelay {
    fn new(max_frames: usize) -> Self {
        PreDelay { buf: vec![0.0; max_frames.max(1)], pos: 0, len: 0 }
    }

    fn set_frames(&mut self, n: usize) {
        self.len = n.min(self.buf.len().saturating_sub(1));
    }

    #[inline]
    fn process(&mut self, x: f32) -> f32 {
        let out = self.buf[(self.pos + self.buf.len() - self.len) % self.buf.len()];
        self.buf[self.pos] = x;
        self.pos = (self.pos + 1) % self.buf.len();
        out
    }
}

/// 立体声混响（高质量 Freeverb 架构 + 预延迟 + 低切）。
pub struct Reverb {
    rate: f32,
    // 参数
    mix: f32,
    room_size: f32,
    damping: f32,
    width: f32,
    low_cut_hz: f32,
    pre_delay_ms: f32,
    /// 调制深度（0~1）：越大，梳状延迟游走越多，金属感越少
    mod_depth: f32,
    /// LFO 相位（0~1 循环）
    lfo_phase: f32,
    /// LFO 步进（每样本，约 0.5Hz）
    lfo_inc: f32,
    // 左右各一套梳状 + 全通
    comb_l: Vec<Comb>,
    comb_r: Vec<Comb>,
    allpass_l: Vec<Allpass>,
    allpass_r: Vec<Allpass>,
    pre_l: PreDelay,
    pre_r: PreDelay,
    hp_l: HighPass,
    hp_r: HighPass,
}

impl Reverb {
    pub fn new(rate: f32) -> Self {
        let sr = rate.max(1.0);
        // 按采样率缩放延迟长度（基准 44.1kHz）
        let scale = (sr / 44100.0).max(1.0);
        let mut comb_l = Vec::with_capacity(8);
        let mut comb_r = Vec::with_capacity(8);
        for &n in COMB_TUNING.iter() {
            comb_l.push(Comb::new((n as f32 * scale) as usize));
            comb_r.push(Comb::new(((n + STEREO_SPREAD) as f32 * scale) as usize));
        }
        let mut allpass_l = Vec::with_capacity(4);
        let mut allpass_r = Vec::with_capacity(4);
        for &n in ALLPASS_TUNING.iter() {
            allpass_l.push(Allpass::new((n as f32 * scale) as usize));
            allpass_r.push(Allpass::new(((n + STEREO_SPREAD) as f32 * scale) as usize));
        }
        let pre_max = ((100.0 / 1000.0) * sr) as usize + 1;
        Reverb {
            rate: sr,
            mix: 0.3,
            room_size: 0.5,
            damping: 0.5,
            width: 1.0,
            low_cut_hz: 100.0,
            pre_delay_ms: 20.0,
            mod_depth: 0.5,
            lfo_phase: 0.0,
            lfo_inc: 0.5 / sr.max(1.0),
            comb_l, comb_r, allpass_l, allpass_r,
            pre_l: PreDelay::new(pre_max),
            pre_r: PreDelay::new(pre_max),
            hp_l: HighPass::new(100.0, sr),
            hp_r: HighPass::new(100.0, sr),
        }
    }

    /// 更新参数（实时生效，不重建缓冲）。
    pub fn set_params(&mut self, mix: f32, room_size: f32, damping: f32,
                      width: f32, low_cut_hz: f32, pre_delay_ms: f32, mod_depth: f32) {
        self.mix = mix.clamp(0.0, 1.0);
        self.room_size = room_size.clamp(0.0, 1.0);
        self.damping = damping.clamp(0.0, 1.0);
        self.width = width.clamp(0.0, 1.0);
        self.pre_delay_ms = pre_delay_ms.clamp(0.0, 100.0);
        self.mod_depth = mod_depth.clamp(0.0, 1.0);

        // 反馈系数：Freeverb 标准 —— 0.28 + room_size*0.7（0.28~0.98）
        // 起点低避免「过密金属声」，范围大给足调节空间
        let feedback = 0.28 + self.room_size * 0.7;
        let damp = self.damping * 0.4;
        for c in self.comb_l.iter_mut() { c.set(feedback, damp); }
        for c in self.comb_r.iter_mut() { c.set(feedback, damp); }

        // 预延迟
        let pre_frames = ((self.pre_delay_ms / 1000.0) * self.rate) as usize;
        self.pre_l.set_frames(pre_frames);
        self.pre_r.set_frames(pre_frames);

        // 低切（变化时才重建系数）
        if (low_cut_hz - self.low_cut_hz).abs() > 0.5 {
            self.low_cut_hz = low_cut_hz.clamp(20.0, 500.0);
            self.hp_l = HighPass::new(self.low_cut_hz, self.rate);
            self.hp_r = HighPass::new(self.low_cut_hz, self.rate);
        }
    }

    /// 清空所有内部缓冲与状态（保留参数）。用于开关切换时避免旧状态爆音。
    pub fn reset(&mut self) {
        for c in self.comb_l.iter_mut() {
            c.buf.iter_mut().for_each(|s| *s = 0.0);
            c.pos = 0;
            c.filter_state = 0.0;
        }
        for c in self.comb_r.iter_mut() {
            c.buf.iter_mut().for_each(|s| *s = 0.0);
            c.pos = 0;
            c.filter_state = 0.0;
        }
        for a in self.allpass_l.iter_mut() {
            a.buf.iter_mut().for_each(|s| *s = 0.0);
            a.pos = 0;
        }
        for a in self.allpass_r.iter_mut() {
            a.buf.iter_mut().for_each(|s| *s = 0.0);
            a.pos = 0;
        }
        for p in [&mut self.pre_l, &mut self.pre_r] {
            p.buf.iter_mut().for_each(|s| *s = 0.0);
            p.pos = 0;
        }
        self.hp_l.prev_x = 0.0; self.hp_l.prev_y = 0.0;
        self.hp_r.prev_x = 0.0; self.hp_r.prev_y = 0.0;
        self.lfo_phase = 0.0;
    }

    /// 处理一帧（立体声）。返回 (out_l, out_r)。
    #[inline]
    pub fn process(&mut self, l: f32, r: f32) -> (f32, f32) {
        // 预延迟
        let dl = self.pre_l.process(l);
        let dr = self.pre_r.process(r);

        // 低切
        let il = self.hp_l.process(dl);
        let ir = self.hp_r.process(dr);

        // LFO 推进（0.5Hz，低频率轻微调制）
        self.lfo_phase += self.lfo_inc;
        if self.lfo_phase >= 1.0 { self.lfo_phase -= 1.0; }
        let lfo = (self.lfo_phase * 2.0 * std::f32::consts::PI).sin();
        // 调制偏移：深度映射到 ±2 采样，每梳状给不同相位偏移（i*0.13）打散
        let base_mod = lfo * self.mod_depth * 2.0;

        // 8 梳状并联（左右各一套）
        let mut out_l = 0.0f32;
        let mut out_r = 0.0f32;
        for i in 0..self.comb_l.len() {
            let ph = (i as f32 * 0.13).fract() * 2.0 - 1.0;
            let mo = base_mod * (1.0 + ph * 0.5);
            out_l += self.comb_l[i].process(il, mo);
            out_r += self.comb_r[i].process(ir, -mo);
        }
        // 归一化：8 路相加，除以 8 避免电平爆（Freeverb 标准做法）
        out_l *= 1.0 / 8.0;
        out_r *= 1.0 / 8.0;

        // 4 全通串联（扩散）；全通有 0.5 固定增益，补偿回来
        for i in 0..self.allpass_l.len() {
            out_l = self.allpass_l[i].process(out_l) * 0.5;
            out_r = self.allpass_r[i].process(out_r) * 0.5;
        }

        // 立体声宽度（M/S 调整）
        let mid = (out_l + out_r) * 0.5;
        let side = (out_l - out_r) * 0.5 * self.width;
        let wet_l = mid + side;
        let wet_r = mid - side;

        // 干湿混合
        let dry = 1.0 - self.mix;
        let wet = self.mix;
        (l * dry + wet_l * wet, r * dry + wet_r * wet)
    }
}
