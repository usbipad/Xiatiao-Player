//! DSP 音效处理管线。
//!
//! 模块划分：
//!   - params：DspParams / PeqBand / 归属表
//!   - biquad：双二阶滤波器
//!   - mod   ：DspChain（链调度与处理）
//!
//! 固定管线顺序（Rust 侧）：
//!   预增益 → 图形EQ → 低音 → 高音 → 立体声宽度 → 左右平衡 → 限幅器 → 输出
//!
//! 全局旁路：enabled=false 或 dsp_enabled=false 时 PCM 原样直通（BitPerfect）。
//! 归属规则：功能重叠时优先 Camilla（见 params::camilla_should_engage）。

mod biquad;
mod params;

pub use biquad::Biquad;
pub use params::{camilla_should_engage, DspParams, DspStage, PeqBand, EQ_BANDS, EQ_FREQS};

// ============================================================
// DSP 管线
// ============================================================

pub struct DspChain {
    in_rate: f32,
    params: DspParams,
    // PEQ 频段（左右声道各一套，动态数量）
    peq_l: Vec<Biquad>,
    peq_r: Vec<Biquad>,
    // 各模块的滤波器（左右声道各一套）
    eq_l: Vec<Biquad>,
    eq_r: Vec<Biquad>,
    bass_l: Biquad,
    bass_r: Biquad,
    treble_l: Biquad,
    treble_r: Biquad,
    loudness_l: Biquad,
    loudness_r: Biquad,
    // 限幅器增益（平滑）
    limiter_gain: f32,
    // 压缩器包络（平滑）
    comp_env: f32,
    // Crossfeed 延迟线（环形，左右各一）
    cf_buf_l: Vec<f32>,
    cf_buf_r: Vec<f32>,
    cf_pos: usize,
    // Crossfeed 低通（只混低频，减少梳状滤波）
    cf_lp_l: f32,
    cf_lp_r: f32,
    /// 上次的参数（用于判断是否变化）
    last_params_json: String,
    // 音色染色（电子管 / BBE），由 set_coloring 实时设置
    tube: Option<crate::tube::Tube>,
    bbe: Option<crate::bbe::Bbe>,
    // 混响（Rust 独有）
    reverb: crate::reverb::Reverb,
    /// 全局 DSP 开关（运行时，由 SetEngine 控制；false = 整段旁路）
    dsp_enabled: bool,
    /// Camilla 引擎是否参与串联。由归属表推导（见 refresh_camilla_flag）。
    camilla_enabled: bool,
    // ---- 宽度/平衡的参数平滑（避免拖动时块间跳变）----
    cur_width: f32,
    cur_bal_l: f32,
    cur_bal_r: f32,
    smooth_init: bool,
}

impl DspChain {
    pub fn new(sample_rate: u32) -> Self {
        let rate = sample_rate.max(1) as f32;
        DspChain {
            in_rate: rate,
            params: DspParams::default(),
            peq_l: Vec::new(),
            peq_r: Vec::new(),
            eq_l: (0..EQ_BANDS).map(|_| Biquad::identity()).collect(),
            eq_r: (0..EQ_BANDS).map(|_| Biquad::identity()).collect(),
            bass_l: Biquad::identity(),
            bass_r: Biquad::identity(),
            treble_l: Biquad::identity(),
            treble_r: Biquad::identity(),
            loudness_l: Biquad::identity(),
            loudness_r: Biquad::identity(),
            limiter_gain: 1.0,
            comp_env: 0.0,
            cf_buf_l: vec![0.0; 64],
            cf_buf_r: vec![0.0; 64],
            cf_pos: 0,
            cf_lp_l: 0.0,
            cf_lp_r: 0.0,
            last_params_json: String::new(),
            tube: None,
            bbe: None,
            reverb: crate::reverb::Reverb::new(rate),
            dsp_enabled: true,
            camilla_enabled: false,
            cur_width: 1.0,
            cur_bal_l: 1.0,
            cur_bal_r: 1.0,
            smooth_init: false,
        }
    }

    /// 运行时设置全局开关。Camilla 是否参与由归属表推导。
    pub fn set_engine(&mut self, dsp_enabled: bool) {
        self.dsp_enabled = dsp_enabled;
        self.refresh_camilla_flag();
    }

    /// 由归属表推导 Camilla 是否参与。
    ///
    /// 归属表集中在 params::camilla_should_engage，避免此处散落大量 `||`。
    pub fn refresh_camilla_flag(&mut self) {
        self.camilla_enabled = camilla_should_engage(&self.params);
    }

    /// 供引擎读取：Camilla 当前是否参与。
    pub fn camilla_active(&self) -> bool {
        self.camilla_enabled
    }

    /// 仅做 Camilla 之外的功能（供串联用）。
    pub fn process_split(&mut self, buf: &mut [f32], channels: usize, stage: DspStage) {
        match stage {
            DspStage::Pre => self.process_pre(buf, channels),
            DspStage::Post => self.process_post(buf, channels),
        }
    }

    /// 设置音色染色参数（tube_drive<=0 关电子管；bbe_amount<=0 关 BBE）。
    pub fn set_coloring(&mut self, tube_drive: f32, bbe_amount: f32) {
        let td = tube_drive.max(0.0);
        let ba = bbe_amount.clamp(0.0, 1.0);
        self.tube = if td > 1e-3 {
            Some(crate::tube::Tube::new(td))
        } else {
            None
        };
        self.bbe = if ba > 1e-3 {
            Some(crate::bbe::Bbe::new(ba, self.in_rate))
        } else {
            None
        };
    }

    /// 从 JSON 更新参数（仅当变化时重建滤波器）。
    pub fn set_params_json(&mut self, v: &serde_json::Value) {
        let s = v.to_string();
        if s == self.last_params_json {
            return; // 无变化，跳过
        }
        self.last_params_json = s;
        let p = DspParams::from_json(v);
        self.set_params(p);
    }

    /// 更新参数（实时生效，重建滤波器系数）。
    pub fn set_params(&mut self, p: DspParams) {
        // PEQ：重建频段滤波器
        self.peq_l.clear();
        self.peq_r.clear();
        for band in &p.peq_bands {
            let bq_l = match band.kind.as_str() {
                "ls" => Biquad::lowshelf(band.freq, band.gain, self.in_rate),
                "hs" => Biquad::highshelf(band.freq, band.gain, self.in_rate),
                _ => Biquad::peaking(band.freq, band.gain, band.q.max(0.1), self.in_rate),
            };
            let bq_r = match band.kind.as_str() {
                "ls" => Biquad::lowshelf(band.freq, band.gain, self.in_rate),
                "hs" => Biquad::highshelf(band.freq, band.gain, self.in_rate),
                _ => Biquad::peaking(band.freq, band.gain, band.q.max(0.1), self.in_rate),
            };
            self.peq_l.push(bq_l);
            self.peq_r.push(bq_r);
        }
        // EQ：为每段重建滤波器（保留状态，只更新系数）
        for i in 0..EQ_BANDS {
            let g = p.eq_gains.get(i).copied().unwrap_or(0.0);
            let f = EQ_FREQS[i];
            let (x1, x2, y1, y2) = (self.eq_l[i].x1, self.eq_l[i].x2, self.eq_l[i].y1, self.eq_l[i].y2);
            let mut bq = Biquad::peaking(f, g, 1.0, self.in_rate);
            bq.x1 = x1; bq.x2 = x2; bq.y1 = y1; bq.y2 = y2;
            self.eq_l[i] = bq;
            let (x1, x2, y1, y2) = (self.eq_r[i].x1, self.eq_r[i].x2, self.eq_r[i].y1, self.eq_r[i].y2);
            let mut bq = Biquad::peaking(f, g, 1.0, self.in_rate);
            bq.x1 = x1; bq.x2 = x2; bq.y1 = y1; bq.y2 = y2;
            self.eq_r[i] = bq;
        }
        // 低音（lowshelf）
        let (x1, x2, y1, y2) = (self.bass_l.x1, self.bass_l.x2, self.bass_l.y1, self.bass_l.y2);
        let mut bl = Biquad::lowshelf(p.bass_freq, p.bass_gain_db, self.in_rate);
        bl.x1 = x1; bl.x2 = x2; bl.y1 = y1; bl.y2 = y2;
        self.bass_l = bl;
        let (x1, x2, y1, y2) = (self.bass_r.x1, self.bass_r.x2, self.bass_r.y1, self.bass_r.y2);
        let mut br = Biquad::lowshelf(p.bass_freq, p.bass_gain_db, self.in_rate);
        br.x1 = x1; br.x2 = x2; br.y1 = y1; br.y2 = y2;
        self.bass_r = br;
        // 高音（highshelf）
        let (x1, x2, y1, y2) = (self.treble_l.x1, self.treble_l.x2, self.treble_l.y1, self.treble_l.y2);
        let mut tl = Biquad::highshelf(p.treble_freq, p.treble_gain_db, self.in_rate);
        tl.x1 = x1; tl.x2 = x2; tl.y1 = y1; tl.y2 = y2;
        self.treble_l = tl;
        let (x1, x2, y1, y2) = (self.treble_r.x1, self.treble_r.x2, self.treble_r.y1, self.treble_r.y2);
        let mut tr = Biquad::highshelf(p.treble_freq, p.treble_gain_db, self.in_rate);
        tr.x1 = x1; tr.x2 = x2; tr.y1 = y1; tr.y2 = y2;
        self.treble_r = tr;
        // Loudness（低频提升，强度映射到增益 0~6dB）
        let loud_gain = if p.loudness_enabled { p.loudness_amount.clamp(0.0, 1.0) * 6.0 } else { 0.0 };
        let (x1, x2, y1, y2) = (self.loudness_l.x1, self.loudness_l.x2, self.loudness_l.y1, self.loudness_l.y2);
        let mut ll = Biquad::lowshelf(120.0, loud_gain, self.in_rate);
        ll.x1 = x1; ll.x2 = x2; ll.y1 = y1; ll.y2 = y2;
        self.loudness_l = ll;
        let (x1, x2, y1, y2) = (self.loudness_r.x1, self.loudness_r.x2, self.loudness_r.y1, self.loudness_r.y2);
        let mut lr = Biquad::lowshelf(120.0, loud_gain, self.in_rate);
        lr.x1 = x1; lr.x2 = x2; lr.y1 = y1; lr.y2 = y2;
        self.loudness_r = lr;

        self.params = p;
        // 参数变化后重新推导 Camilla 参与状态
        self.refresh_camilla_flag();
    }

    pub fn params(&self) -> &DspParams {
        &self.params
    }

    /// 处理一块交错立体声样本（原地）。兼容入口：未串联时整段跑。
    pub fn process_interleaved(&mut self, buf: &mut [f32], channels: usize) {
        self.process_pre(buf, channels);
        self.process_post(buf, channels);
    }

    /// 前置段：只做 ReplayGain（Camilla 之前）。
    pub fn process_pre(&mut self, buf: &mut [f32], channels: usize) {
        if !self.dsp_enabled || !self.params.enabled || channels != 2 {
            return;
        }
        let p = &self.params;
        // ReplayGain 增益（归 Rust，Camilla 无此功能）
        let rg = if p.replaygain_enabled {
            10f32.powf((p.replaygain_db + p.replaygain_preamp_db) / 20.0)
        } else {
            1.0
        };
        if (rg - 1.0).abs() > 1e-9 {
            for s in buf.iter_mut() {
                *s *= rg;
            }
        }
    }

    /// 后置段：压缩/限幅/crossfeed/宽度/平衡/相位/矩阵（Camilla 之后）。
    /// 归 Camilla 的功能（预增益/PEQ/图形EQ/低音/高音/Loudness）在此跳过。
    pub fn process_post(&mut self, buf: &mut [f32], channels: usize) {
        // 全局旁路：原样直通
        if !self.dsp_enabled || !self.params.enabled || channels != 2 {
            return;
        }

        let p = &self.params;
        let headroom = if p.headroom_enabled { 10f32.powf(p.headroom_db / 20.0) } else { 1.0 };
        // Camilla 参与时：预增益/Loudness 归 Camilla，Rust 只保留 headroom。
        let cam = self.camilla_enabled;
        let loud_comp = if !cam && p.loudness_enabled {
            10f32.powf(-(p.loudness_amount.clamp(0.0, 1.0) * 6.0 * 0.5) / 20.0)
        } else {
            1.0
        };
        let pre_gain_lin = if cam { 1.0 } else { 10f32.powf(p.pre_gain_db / 20.0) };
        let pre = pre_gain_lin * headroom * loud_comp;
        let target_width = if p.width_enabled { p.stereo_width.clamp(0.0, 2.0) } else { 1.0 };
        let target_balance = p.balance.clamp(-1.0, 1.0);
        let target_bal_l = if target_balance > 0.0 { 1.0 - target_balance } else { 1.0 };
        let target_bal_r = if target_balance < 0.0 { 1.0 + target_balance } else { 1.0 };
        if !self.smooth_init {
            self.cur_width = target_width;
            self.cur_bal_l = target_bal_l;
            self.cur_bal_r = target_bal_r;
            self.smooth_init = true;
        }
        let lim_thresh = 10f32.powf(p.limiter_threshold_db / 20.0);
        // 压缩/限幅：Camilla 参与时由 Camilla 处理，Rust 跳过
        let lim_on = !cam && p.limiter_enabled;
        let comp_on = !cam && p.compressor_enabled;
        let comp_thresh = 10f32.powf(p.compressor_threshold_db / 20.0);
        let comp_ratio = p.compressor_ratio.clamp(1.0, 20.0);
        let comp_makeup = 10f32.powf(p.compressor_makeup_db / 20.0);
        let comp_attack = 0.005;
        let comp_release = 0.0005;
        let eq_on = p.eq_enabled && !cam;
        let peq_on = p.peq_enabled && !cam;
        let bass_on = p.bass_enabled && !cam;
        let treble_on = !cam;
        let cf_on = p.crossfeed_enabled;
        let cf_amt = p.crossfeed_amount.clamp(0.0, 1.0);
        let cf_delay = ((p.crossfeed_delay_ms.max(0.0) / 1000.0) * self.in_rate) as usize;
        let cf_delay = cf_delay.min(self.cf_buf_l.len().saturating_sub(1));
        let loud_on = !cam && p.loudness_enabled && p.loudness_amount > 0.0;
        let phase_inv = !cam && p.phase_invert;
        let ch_mode = if cam { "off".to_string() } else { p.channel_matrix.clone() };
        let reverb_on = p.reverb_enabled;
        if reverb_on {
            self.reverb.set_params(
                p.reverb_mix, p.reverb_decay, p.reverb_damping,
                p.reverb_width, p.reverb_low_cut_hz, p.reverb_pre_delay_ms,
                p.reverb_mod_depth,
            );
        }
        let mut tube = self.tube.take();
        let mut bbe = self.bbe.take();
        let smooth_coef = 1.0 - (-1.0f32 / (0.015 * self.in_rate)).exp();
        for frame in buf.chunks_mut(2) {
            let mut l = frame[0] * pre;
            let mut r = frame[1] * pre;
            self.cur_width += (target_width - self.cur_width) * smooth_coef;
            self.cur_bal_l += (target_bal_l - self.cur_bal_l) * smooth_coef;
            self.cur_bal_r += (target_bal_r - self.cur_bal_r) * smooth_coef;

            // 音色染色：先电子管，再 BBE（Camilla 参与时跳过）
            if !cam {
                if let Some(t) = tube.as_ref() {
                    l = t.process(l);
                    r = t.process(r);
                }
                if let Some(b) = bbe.as_mut() {
                    let (nl, nr) = b.process(l, r);
                    l = nl;
                    r = nr;
                }
            }

            // 通道矩阵
            match ch_mode.as_str() {
                "swap" => { std::mem::swap(&mut l, &mut r); }
                "mono" => { let m = (l + r) * 0.5; l = m; r = m; }
                "left_both" => { r = l; }
                "right_both" => { l = r; }
                _ => {}
            }
            if phase_inv {
                l = -l;
                r = -r;
            }
            if peq_on {
                for i in 0..self.peq_l.len() {
                    l = self.peq_l[i].process(l);
                    r = self.peq_r[i].process(r);
                }
            }
            if eq_on {
                for i in 0..EQ_BANDS {
                    l = self.eq_l[i].process(l);
                    r = self.eq_r[i].process(r);
                }
            }
            if bass_on {
                l = self.bass_l.process(l);
                r = self.bass_r.process(r);
            }
            if treble_on {
                l = self.treble_l.process(l);
                r = self.treble_r.process(r);
            }
            if loud_on {
                l = self.loudness_l.process(l);
                r = self.loudness_r.process(r);
            }
            if (self.cur_width - 1.0).abs() > 1e-6 {
                let m = (l + r) * 0.5;
                let s = (l - r) * 0.5 * self.cur_width;
                l = m + s;
                r = m - s;
            }
            l *= self.cur_bal_l;
            r *= self.cur_bal_r;
            if reverb_on {
                let (rl, rr) = self.reverb.process(l, r);
                l = rl;
                r = rr;
            }
            if cf_on {
                let n = self.cf_buf_l.len();
                let idx = (self.cf_pos + n - cf_delay) % n;
                let cl = self.cf_buf_l[idx];
                let cr = self.cf_buf_r[idx];
                self.cf_buf_l[self.cf_pos] = l;
                self.cf_buf_r[self.cf_pos] = r;
                self.cf_pos = (self.cf_pos + 1) % n;
                self.cf_lp_l += 0.3 * (cr - self.cf_lp_l);
                self.cf_lp_r += 0.3 * (cl - self.cf_lp_r);
                l += self.cf_lp_l * cf_amt;
                r += self.cf_lp_r * cf_amt;
            }
            if comp_on {
                let peak = l.abs().max(r.abs());
                let coef = if peak > self.comp_env { comp_attack } else { comp_release };
                self.comp_env += (peak - self.comp_env) * coef;
                if self.comp_env > comp_thresh && comp_thresh > 0.0 {
                    let over_db = 20.0 * (self.comp_env / comp_thresh).log10();
                    let reduced_db = over_db * (1.0 - 1.0 / comp_ratio);
                    let gain = 10f32.powf(-reduced_db / 20.0) * comp_makeup;
                    l *= gain;
                    r *= gain;
                } else {
                    l *= comp_makeup;
                    r *= comp_makeup;
                }
            }
            if lim_on && lim_thresh > 0.0 {
                let peak = l.abs().max(r.abs());
                let target_gain = if peak > lim_thresh { lim_thresh / peak } else { 1.0 };
                let coef = if target_gain < self.limiter_gain { 0.05 } else { 0.0005 };
                self.limiter_gain += (target_gain - self.limiter_gain) * coef;
                self.limiter_gain = self.limiter_gain.clamp(0.0, 1.0);
                l *= self.limiter_gain;
                r *= self.limiter_gain;
            }

            frame[0] = l;
            frame[1] = r;
        }
        self.tube = tube;
        self.bbe = bbe;
    }
}
