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
mod loudness;
mod params;

pub use biquad::Biquad;
pub use params::{camilla_should_engage, DspParams, DspStage, EQ_BANDS, EQ_FREQS};

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
    // 动态等响度：极低频补偿（第二段 lowshelf @ LOW2? 不，@LOW1）
    // 注：loudness_l/r 为极低频段（@40Hz），loudness_low2_l/r 为中低频段（@180Hz）
    loudness_low2_l: Biquad,
    loudness_low2_r: Biquad,
    // 动态等响度：高频补偿
    loudness_high_l: Biquad,
    loudness_high_r: Biquad,
    /// 当前播放器音量（0.0~1.0），由 set_volume 实时更新，供动态等响度用
    volume: f32,
    /// 上次计算等响度时的音量（变化才重建滤波器，省开销）
    loudness_last_vol: f32,
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
    // ---- 预增益/余量（pre）的平滑值 ----
    // 总开关切换或 pre 目标变化时，从当前值平滑过渡到目标，
    // 避免「旁路(1.0) ↔ 目标增益」瞬间阶跃产生爆音。
    cur_pre: f32,
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
            loudness_low2_l: Biquad::identity(),
            loudness_low2_r: Biquad::identity(),
            loudness_high_l: Biquad::identity(),
            loudness_high_r: Biquad::identity(),
            volume: 1.0,
            loudness_last_vol: -1.0,
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
            cur_pre: 1.0,
        }
    }

    /// 更新当前播放器音量（供动态等响度用）。
    pub fn set_volume(&mut self, v: f32) {
        self.volume = v.clamp(0.0, 1.0);
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

    /// 清空所有运行时状态（延迟/包络/平滑器），保留滤波器系数与参数。
    ///
    /// 用于「启用 DSP」总开关切换时：避免关闭期间冻结的旧状态在重开瞬间
    /// 与当前信号不连续，产生瞬态失真（炒豆声）。
    pub fn reset_state(&mut self) {
        for bq in self.peq_l.iter_mut() { bq.reset(); }
        for bq in self.peq_r.iter_mut() { bq.reset(); }
        for bq in self.eq_l.iter_mut() { bq.reset(); }
        for bq in self.eq_r.iter_mut() { bq.reset(); }
        self.bass_l.reset(); self.bass_r.reset();
        self.treble_l.reset(); self.treble_r.reset();
        self.loudness_l.reset(); self.loudness_r.reset();
        self.loudness_low2_l.reset(); self.loudness_low2_r.reset();
        self.loudness_high_l.reset(); self.loudness_high_r.reset();
        // 压缩/限幅包络
        self.comp_env = 0.0;
        self.limiter_gain = 1.0;
        // crossfeed 缓冲
        self.cf_buf_l.iter_mut().for_each(|s| *s = 0.0);
        self.cf_buf_r.iter_mut().for_each(|s| *s = 0.0);
        self.cf_pos = 0;
        self.cf_lp_l = 0.0;
        self.cf_lp_r = 0.0;
        // 宽度/平衡平滑器：复位为旁路值（1.0/1.0），但**保持平滑器运行态**
        // （不置 smooth_init=false）。否则下一帧会直接硬跳到目标值，
        // 总开关重开瞬间仍会阶跃 → 爆音。
        self.cur_width = 1.0;
        self.cur_bal_l = 1.0;
        self.cur_bal_r = 1.0;
        // 预增益平滑器：复位为旁路值 1.0，重开时从 1.0 平滑到目标，
        // 避免「直通 ↔ 带增益」瞬间阶跃 → 爆音。
        self.cur_pre = 1.0;
        // 混响内部缓冲
        self.reverb.reset();
    }

    /// 更新参数（实时生效，重建滤波器系数）。
    pub fn set_params(&mut self, p: DspParams) {
        // 记录旧的全局开关：用于检测「总开关切换」→ 重置状态。
        let was_enabled = self.params.enabled;
        let now_enabled = p.enabled;
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
        // 关键：检测「滤波器系数是否实际变化」。系数变化时若保留旧滤波器状态，
        // 会因「旧状态与新系数不匹配」产生瞬态爆音（切音效时常见）。
        // 故系数变化时重置对应滤波器状态（重置是安全的，最多损失一个块尾）。
        let eq_changed = self.params.eq_gains != p.eq_gains
            || self.params.eq_enabled != p.eq_enabled;
        let peq_changed = self.params.peq_enabled != p.peq_enabled
            || serde_json::to_string(&self.params.peq_bands).unwrap_or_default()
                != serde_json::to_string(&p.peq_bands).unwrap_or_default();
        let bass_changed = self.params.bass_enabled != p.bass_enabled
            || self.params.bass_gain_db != p.bass_gain_db
            || self.params.bass_freq != p.bass_freq;
        let treble_changed = self.params.treble_gain_db != p.treble_gain_db
            || self.params.treble_freq != p.treble_freq
            || self.params.bass_enabled != p.bass_enabled;
        let loud_changed = self.params.loudness_enabled != p.loudness_enabled
            || self.params.loudness_amount != p.loudness_amount;

        // EQ：为每段重建滤波器（系数变化时重置状态）
        for i in 0..EQ_BANDS {
            let g = p.eq_gains.get(i).copied().unwrap_or(0.0);
            let f = EQ_FREQS[i];
            let mut bq = Biquad::peaking(f, g, 1.0, self.in_rate);
            if !eq_changed {
                bq.x1 = self.eq_l[i].x1; bq.x2 = self.eq_l[i].x2;
                bq.y1 = self.eq_l[i].y1; bq.y2 = self.eq_l[i].y2;
            }
            self.eq_l[i] = bq;
            let mut bq = Biquad::peaking(f, g, 1.0, self.in_rate);
            if !eq_changed {
                bq.x1 = self.eq_r[i].x1; bq.x2 = self.eq_r[i].x2;
                bq.y1 = self.eq_r[i].y1; bq.y2 = self.eq_r[i].y2;
            }
            self.eq_r[i] = bq;
        }
        // 低音（lowshelf）
        let mut bl = Biquad::lowshelf(p.bass_freq, p.bass_gain_db, self.in_rate);
        if !bass_changed {
            bl.x1 = self.bass_l.x1; bl.x2 = self.bass_l.x2;
            bl.y1 = self.bass_l.y1; bl.y2 = self.bass_l.y2;
        }
        self.bass_l = bl;
        let mut br = Biquad::lowshelf(p.bass_freq, p.bass_gain_db, self.in_rate);
        if !bass_changed {
            br.x1 = self.bass_r.x1; br.x2 = self.bass_r.x2;
            br.y1 = self.bass_r.y1; br.y2 = self.bass_r.y2;
        }
        self.bass_r = br;
        // 高音（highshelf）
        let mut tl = Biquad::highshelf(p.treble_freq, p.treble_gain_db, self.in_rate);
        if !treble_changed {
            tl.x1 = self.treble_l.x1; tl.x2 = self.treble_l.x2;
            tl.y1 = self.treble_l.y1; tl.y2 = self.treble_l.y2;
        }
        self.treble_l = tl;
        let mut tr = Biquad::highshelf(p.treble_freq, p.treble_gain_db, self.in_rate);
        if !treble_changed {
            tr.x1 = self.treble_r.x1; tr.x2 = self.treble_r.x2;
            tr.y1 = self.treble_r.y1; tr.y2 = self.treble_r.y2;
        }
        self.treble_r = tr;
        self.params = p;
        // 总开关切换（开↔关）或关键滤波器系数变化：重置全部运行时状态。
        // - 总开关切换：关闭期间 process_post 直接 return，状态被冻结；
        //   重开时沿用旧状态会与当前信号不连续 → 炒豆声。
        // - 滤波器系数大变（切音效）：旧状态与新系数不匹配 → 瞬态爆音。
        if was_enabled != now_enabled || eq_changed || peq_changed
            || bass_changed || treble_changed || loud_changed {
            self.reset_state();
        }
        // 参数更新后：按当前音量重建等响度（算法见 loudness 模块）。
        // 音量后续变化会在 process_post 里重建。
        self.rebuild_loudness();
        // 参数变化后重新推导 Camilla 参与状态
        self.refresh_camilla_flag();
    }

    pub fn params(&self) -> &DspParams {
        &self.params
    }

    /// 按当前音量 + 参数重建等响度滤波器（低/高频 shelf）。
    /// 仅参数开启等响度时生效；否则置为 identity（直通）。
    fn rebuild_loudness(&mut self) {
        let p = &self.params;
        if !p.loudness_enabled || p.loudness_amount <= 1e-6 {
            self.loudness_l = Biquad::identity();
            self.loudness_r = Biquad::identity();
            self.loudness_low2_l = Biquad::identity();
            self.loudness_low2_r = Biquad::identity();
            self.loudness_high_l = Biquad::identity();
            self.loudness_high_r = Biquad::identity();
            self.loudness_last_vol = self.volume;
            return;
        }
        let comp = loudness::compute(self.volume, p.loudness_amount);
        // 极低频 Lowshelf @40Hz（保留状态，避免爆音）
        let (x1, x2, y1, y2) = (self.loudness_l.x1, self.loudness_l.x2, self.loudness_l.y1, self.loudness_l.y2);
        let mut ll = Biquad::lowshelf(loudness::low_freq(), comp.low_db, self.in_rate);
        ll.x1 = x1; ll.x2 = x2; ll.y1 = y1; ll.y2 = y2;
        self.loudness_l = ll;
        let (x1, x2, y1, y2) = (self.loudness_r.x1, self.loudness_r.x2, self.loudness_r.y1, self.loudness_r.y2);
        let mut lr = Biquad::lowshelf(loudness::low_freq(), comp.low_db, self.in_rate);
        lr.x1 = x1; lr.x2 = x2; lr.y1 = y1; lr.y2 = y2;
        self.loudness_r = lr;
        // 中低频 Lowshelf @180Hz
        let (x1, x2, y1, y2) = (self.loudness_low2_l.x1, self.loudness_low2_l.x2, self.loudness_low2_l.y1, self.loudness_low2_l.y2);
        let mut l2l = Biquad::lowshelf(loudness::low2_freq(), comp.low2_db, self.in_rate);
        l2l.x1 = x1; l2l.x2 = x2; l2l.y1 = y1; l2l.y2 = y2;
        self.loudness_low2_l = l2l;
        let (x1, x2, y1, y2) = (self.loudness_low2_r.x1, self.loudness_low2_r.x2, self.loudness_low2_r.y1, self.loudness_low2_r.y2);
        let mut l2r = Biquad::lowshelf(loudness::low2_freq(), comp.low2_db, self.in_rate);
        l2r.x1 = x1; l2r.x2 = x2; l2r.y1 = y1; l2r.y2 = y2;
        self.loudness_low2_r = l2r;
        // 高频 Highshelf
        let (x1, x2, y1, y2) = (self.loudness_high_l.x1, self.loudness_high_l.x2, self.loudness_high_l.y1, self.loudness_high_l.y2);
        let mut hl = Biquad::highshelf(loudness::high_freq(), comp.high_db, self.in_rate);
        hl.x1 = x1; hl.x2 = x2; hl.y1 = y1; hl.y2 = y2;
        self.loudness_high_l = hl;
        let (x1, x2, y1, y2) = (self.loudness_high_r.x1, self.loudness_high_r.x2, self.loudness_high_r.y1, self.loudness_high_r.y2);
        let mut hr = Biquad::highshelf(loudness::high_freq(), comp.high_db, self.in_rate);
        hr.x1 = x1; hr.x2 = x2; hr.y1 = y1; hr.y2 = y2;
        self.loudness_high_r = hr;
        self.loudness_last_vol = self.volume;
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

        // 动态等响度：音量变化时重建滤波器（须在借 self.params 之前做）。
        if self.params.loudness_enabled && self.params.loudness_amount > 0.0
            && (self.volume - self.loudness_last_vol).abs() > 0.005
        {
            self.rebuild_loudness();
        }

        let p = &self.params;
        // 增益/余量组开关：关闭时预增益与余量都旁路。
        let gain_on = p.gain_enabled;
        let headroom = if gain_on { 10f32.powf(p.headroom_db / 20.0) } else { 1.0 };
        // Camilla 参与时：预增益归 Camilla，Rust 只保留 headroom。
        let cam = self.camilla_enabled;
        let pre_gain_lin = if cam || !gain_on { 1.0 } else { 10f32.powf(p.pre_gain_db / 20.0) };
        let pre = pre_gain_lin * headroom;
        // 「启用立体声宽度」开关同时控制宽度与平衡（二者在 UI 同属一组）。
        // 关闭时：宽度复位 1.0、平衡复位 0.0。
        let target_width = if p.width_enabled { p.stereo_width.clamp(0.0, 2.0) } else { 1.0 };
        let target_balance = if p.width_enabled { p.balance.clamp(-1.0, 1.0) } else { 0.0 };
        let target_bal_l = if target_balance > 0.0 { 1.0 - target_balance } else { 1.0 };
        let target_bal_r = if target_balance < 0.0 { 1.0 + target_balance } else { 1.0 };
        // 注：不再对 cur_width/bal 做「首帧直接取目标」的硬跳。
        // 首次从初始化值 1.0 平滑到目标（15ms 过渡）完全安全，
        // 且能避免首帧/总开关切换瞬间的阶跃爆音。
        let lim_thresh = 10f32.powf(p.limiter_threshold_db / 20.0);
        // 压缩/限幅：Camilla 参与时由 Camilla 处理，Rust 跳过
        let lim_on = !cam && p.limiter_enabled;
        let comp_on = !cam && p.compressor_enabled;
        let comp_thresh = 10f32.powf(p.compressor_threshold_db / 20.0);
        let comp_ratio = p.compressor_ratio.clamp(1.0, 20.0);
        let comp_makeup = 10f32.powf(p.compressor_makeup_db / 20.0);
        // 压缩器包络时间常数（每样本系数）。
        // 注：Rust DspParams 无 compressor_attack/release 字段（Python 传但
        // 结构体未含），为不改协议，用固定合理值：attack 5ms、release 60ms。
        // coef = 1 - exp(-1/(τ·fs))。旧代码 attack=0.005/release=0.0005 的
        // release 过快，包络几乎不保持，导致对周期信号几乎不压缩。
        let comp_attack = 1.0 - (-1.0f32 / (0.005 * self.in_rate)).exp();
        let comp_release = 1.0 - (-1.0f32 / (0.060 * self.in_rate)).exp();
        let eq_on = p.eq_enabled && !cam;
        let peq_on = p.peq_enabled && !cam;
        // 低音/高音同组：开关（bass_enabled）统一控制二者。
        let bass_on = p.bass_enabled && !cam;
        let treble_on = p.bass_enabled && !cam;
        let cf_on = p.crossfeed_enabled;
        let cf_amt = p.crossfeed_amount.clamp(0.0, 1.0);
        let cf_delay = ((p.crossfeed_delay_ms.max(0.0) / 1000.0) * self.in_rate) as usize;
        let cf_delay = cf_delay.min(self.cf_buf_l.len().saturating_sub(1));
        // 动态等响度归 Rust（不依赖 Camilla；用播放器音量驱动）。
        let loud_on = p.loudness_enabled && p.loudness_amount > 0.0;
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
        // 预增益平滑：更慢的时间常数（30ms），让总开关切换/预设切换时
        // 增益从旁路值(1.0)平滑过渡到目标，彻底消除阶跃爆音。
        let pre_coef = 1.0 - (-1.0f32 / (0.030 * self.in_rate)).exp();
        for frame in buf.chunks_mut(2) {
            // 逐样本平滑逼近目标增益（替代原 `frame[0] * pre` 的硬阶跃）
            self.cur_pre += (pre - self.cur_pre) * pre_coef;
            let mut l = frame[0] * self.cur_pre;
            let mut r = frame[1] * self.cur_pre;
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
                // 极低频 → 中低频 → 高频（三段级联整形，贴合 ISO 226）
                l = self.loudness_l.process(l);
                r = self.loudness_r.process(r);
                l = self.loudness_low2_l.process(l);
                r = self.loudness_low2_r.process(r);
                l = self.loudness_high_l.process(l);
                r = self.loudness_high_r.process(r);
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
                // 用「整流后的峰值」估计包络：对正弦等周期信号更可靠。
                // 平滑：上升用 attack，下降用 release（标准压缩器包络跟随）。
                let peak = l.abs().max(r.abs());
                let coef = if peak > self.comp_env { comp_attack } else { comp_release };
                self.comp_env += (peak - self.comp_env) * coef;
                // 增益计算用「平滑包络」比阈值：超阈值部分按压缩比衰减。
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

#[cfg(test)]
mod tests {
    use super::*;

    /// 测某音量下、某频点的增益（dB，相对输入）。
    fn gain_at(chain: &mut DspChain, freq: f32, rate: f32) -> f32 {
        let frames = 24000usize;
        let mut buf = Vec::with_capacity(frames * 2);
        for i in 0..frames {
            let s = (2.0 * std::f32::consts::PI * freq * (i as f32 / rate)).sin() * 0.25;
            buf.push(s);
            buf.push(s);
        }
        let in_rms = {
            let st = frames / 4;
            let mut sum = 0.0f32;
            let mut n = 0usize;
            for i in st..frames {
                sum += buf[i * 2] * buf[i * 2];
                n += 1;
            }
            (sum / n as f32).sqrt()
        };
        chain.process_interleaved(&mut buf, 2);
        let out_rms = {
            let st = frames / 4;
            let mut sum = 0.0f32;
            let mut n = 0usize;
            for i in st..frames {
                sum += buf[i * 2] * buf[i * 2];
                n += 1;
            }
            (sum / n as f32).sqrt()
        };
        20.0 * (out_rms / in_rms.max(1e-9)).log10()
    }

    /// 动态等响度：音量越低，两端补偿越多。
    #[test]
    fn loudness_dynamic_by_volume() {
        let rate = 48000.0f32;
        let mut params = DspParams::default();
        params.enabled = true;
        params.loudness_enabled = true;
        params.loudness_amount = 1.0;

        let measure = |vol: f32| -> (f32, f32) {
            let mut chain = DspChain::new(48000);
            chain.set_volume(vol);
            chain.set_params(params.clone());
            (gain_at(&mut chain, 80.0, rate), gain_at(&mut chain, 10000.0, rate))
        };

        let (low_full, high_full) = measure(1.0);
        let (low_mid, high_mid) = measure(0.5);
        let (low_low, high_low) = measure(0.1);
        println!("音量1.0: 低频{low_full:+.2}dB 高频{high_full:+.2}dB");
        println!("音量0.5: 低频{low_mid:+.2}dB 高频{high_mid:+.2}dB");
        println!("音量0.1: 低频{low_low:+.2}dB 高频{high_low:+.2}dB");
        assert!(low_low > low_mid + 0.5, "音量越低低频补偿越多");
        assert!(low_mid > low_full + 0.5, "音量中等比满音量补偿多");
        assert!(high_low > high_full, "高频也应随音量降低而补偿");
    }

    /// 总开关切换不应产生阶跃（爆音）。
    ///
    /// 场景：preset 带 +6dB 预增益（pre=2.0）与宽度 1.8。
    /// 先以 enabled=false 跑一段（直通），再 enabled=true 开启，
    /// 检查开启瞬间相邻样本差有界（平滑过渡而非阶跃）。
    #[test]
    fn toggle_no_pop() {
        let rate = 48000.0f32;
        let mut chain = DspChain::new(48000);

        // 关：直通。跑一段并记录切换前最后一个输出样本。
        let mut off = DspParams::default();
        off.enabled = false;
        chain.set_params(off);
        let mut pre_buf = vec![0.3f32; 2048 * 2];
        chain.process_interleaved(&mut pre_buf, 2);
        let last_before = pre_buf[pre_buf.len() - 2];

        // 开：带余量 -6dB（pre=0.5，Rust 侧生效）+ 宽度 1.8（均为会阶跃的参数）。
        // 注：不用 pre_gain_db——它归 Camilla（camilla_should_engage 会命中），
        // 单独跑 DspChain 时 Camilla 未串联，pre_gain 不生效。
        // headroom 由 Rust 处理，可独立验证平滑。
        let mut on = DspParams::default();
        on.enabled = true;
        on.gain_enabled = true;
        on.headroom_db = -6.0;
        on.width_enabled = true;
        on.stereo_width = 1.8;
        chain.set_params(on);

        // 持续 DC 0.3 输入，观察输出是否平滑
        let mut buf = vec![0.3f32; 4096 * 2];
        chain.process_interleaved(&mut buf, 2);

        // 真正的「爆音」指标：跨越切换点的样本连续性 + 块内相邻样本跳变。
        // 关键：必须把「切换前最后样本」与「切换后第一样本」对比——
        // 这才是爆音发生的瞬间。DC 输入下，平滑过渡该处差值应极小；
        // 阶跃则是「0.3 → 0.15」这种整段突变。
        let mut max_jump = (buf[0] - last_before).abs();
        let mut prev = buf[0];
        for &s in buf.iter().step_by(2) {
            max_jump = max_jump.max((s - prev).abs());
            prev = s;
        }
        assert!(max_jump < 0.05,
                "总开关切换出现样本跳变（爆音）：max_jump={max_jump:.5}（应 < 0.05）");

        // 且最终应收敛到目标增益（-6dB headroom → 0.3*0.5=0.15）附近
        let tail = buf[buf.len() - 200];
        assert!((tail - 0.15).abs() < 0.02,
                "余量增益未收敛到目标：尾值={tail:.4}（期望≈0.15）");
    }

    // ============================================================
    // 测量工具（声学验证）：频响 / 谐波失真 / 梳状凹陷
    // ============================================================

    /// 用正弦扫频测链的频响（相对 1kHz 归一，dB）。
    fn measure_chain(chain: &mut DspChain, freqs: &[f32], rate: f32) -> Vec<(f32, f32)> {
        let frames = 24000usize;
        let mut out = Vec::new();
        for &f in freqs {
            let mut buf = Vec::with_capacity(frames * 2);
            for i in 0..frames {
                let s = (2.0 * std::f32::consts::PI * f * (i as f32 / rate)).sin() * 0.25;
                buf.push(s); buf.push(s);
            }
            let in_rms = {
                let st = frames / 4; let mut sum = 0.0f32; let mut n = 0usize;
                for i in st..frames { sum += buf[i*2]*buf[i*2]; n += 1; }
                (sum / n as f32).sqrt()
            };
            chain.process_interleaved(&mut buf, 2);
            let out_rms = {
                let st = frames / 4; let mut sum = 0.0f32; let mut n = 0usize;
                for i in st..frames { sum += buf[i*2]*buf[i*2]; n += 1; }
                (sum / n as f32).sqrt()
            };
            let db = 20.0 * (out_rms / in_rms.max(1e-9)).log10();
            out.push((f, db));
        }
        // 以 1kHz 归一
        let ref_db = out.iter().min_by(|a,b|
            (a.0-1000.0).abs().partial_cmp(&(b.0-1000.0).abs()).unwrap())
            .map(|x| x.1).unwrap_or(0.0);
        out.iter().map(|(f,d)| (*f, d - ref_db)).collect()
    }

    /// 测 THD+N（总谐波失真）：输入纯正弦，输出中非基波分量占比。
    ///
    /// 简化实现：用 Goertzel 提取基波能量，总能量减基波 = 失真能量。
    fn measure_thd(chain: &mut DspChain, freq: f32, rate: f32) -> f32 {
        let frames = 48000usize;
        let mut buf = Vec::with_capacity(frames * 2);
        for i in 0..frames {
            let s = (2.0 * std::f32::consts::PI * freq * (i as f32 / rate)).sin() * 0.3;
            buf.push(s); buf.push(s);
        }
        chain.process_interleaved(&mut buf, 2);
        let st = frames / 2;
        let n = (frames - st) as f32;
        // 总能量
        let mut total = 0.0f32;
        for i in st..frames { total += buf[i*2]*buf[i*2]; }
        total /= n;
        // 基波能量（Goertzel）
        let w = 2.0 * std::f32::consts::PI * freq / rate;
        let (mut re, mut im) = (0.0f32, 0.0f32);
        for i in st..frames {
            let ang = w * i as f32;
            re += buf[i*2] * ang.cos();
            im += buf[i*2] * ang.sin();
        }
        let fund = (re*re + im*im) / (n*n) * 2.0;
        let dist = (total - fund).max(0.0);
        if total <= 1e-12 { return 0.0; }
        10.0 * (dist / fund.max(1e-12)).log10()
    }

    /// 诊断：BBE 打开时的频响（找相位抵消/梳状凹陷）+ THD。
    #[test]
    fn diag_bbe_response() {
        let rate = 48000.0f32;
        let freqs = [20.0, 50.0, 100.0, 200.0, 400.0, 700.0, 1000.0, 1500.0, 2000.0,
                     3000.0, 5000.0, 8000.0, 12000.0, 16000.0];
        let mut p = DspParams::default();
        p.enabled = true;
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        chain.set_coloring(0.0, 1.0); // tube 关，bbe 全开（染色独立于 DspParams）
        let resp = measure_chain(&mut chain, &freqs, rate);
        print!("[BBE amount=1.0 响应] ");
        for (f,d) in &resp { print!("{f:.0}={d:+.1} "); }
        println!();
        // 梳状凹陷检测：相邻频点落差 > 3dB 视为可疑
        let mut max_dip = 0.0f32;
        for w in resp.windows(2) {
            let jump = (w[0].1 - w[1].1).abs();
            if jump > max_dip { max_dip = jump; }
        }
        println!("[BBE] 相邻频点最大落差 = {max_dip:.1}dB（>3dB 疑梳状滤波）");

        let mut chain2 = DspChain::new(48000);
        chain2.set_params(DspParams { enabled: true, ..Default::default() });
        chain2.set_coloring(0.0, 1.0);
        let thd = measure_thd(&mut chain2, 1000.0, rate);
        println!("[BBE] 1kHz THD+N = {thd:.1}dB");
    }

    /// 诊断：Tube 打开时的 THD + 电平变化。
    #[test]
    fn diag_tube_distortion() {
        let rate = 48000.0f32;
        // 电平变化
        let mut p = DspParams::default();
        p.enabled = true;
        let mut chain = DspChain::new(48000);
        chain.set_params(p.clone());
        chain.set_coloring(3.0, 0.0); // tube drive=3, bbe 关
        let resp = measure_chain(&mut chain, &[1000.0], rate);
        println!("[Tube drive=3] 1kHz 增益 = {:+.2}dB（应≈0，不应整体变响）", resp[0].1);
        let thd = measure_thd(&mut chain, 1000.0, rate);
        println!("[Tube drive=3] 1kHz THD+N = {thd:.1}dB（应有偶次谐波，但不过高）");

        // drive=1（最小）应几乎无染色
        let mut chain1 = DspChain::new(48000);
        chain1.set_params(DspParams { enabled: true, ..Default::default() });
        chain1.set_coloring(1.0, 0.0);
        let thd1 = measure_thd(&mut chain1, 1000.0, rate);
        println!("[Tube drive=1] 1kHz THD+N = {thd1:.1}dB（应很低，接近直通）");
    }

    /// 诊断：Loudness 实际补偿曲线（对比 ISO 226 目标）。
    #[test]
    fn diag_loudness_curve() {
        let rate = 48000.0f32;
        // ISO 226 等响曲线（相对 1kHz）：80 phon（低音量聆听）相对 100 phon 的
        // 补偿量近似值（低频 +10~20dB，高频 +2~6dB）。这里给出参考目标。
        // 测各音量下，几个关键频点的提升量。
        let freqs = [30.0, 60.0, 100.0, 300.0, 1000.0, 3000.0, 8000.0, 12000.0];
        for vol in [0.5f32, 0.2, 0.1] {
            let mut p = DspParams::default();
            p.enabled = true;
            p.loudness_enabled = true;
            p.loudness_amount = 1.0;
            let mut chain = DspChain::new(48000);
            chain.set_volume(vol);
            chain.set_params(p);
            let resp = measure_chain(&mut chain, &freqs, rate);
            let vol_db = 20.0 * vol.log10();
            print!("[Loudness vol={vol} ({vol_db:+.0}dBFS)] ");
            for (f, d) in &resp { print!("{f:.0}={d:+.1} "); }
            println!();
        }
        // 期望：低频（30-100Hz）补偿 > 高频（8-12k）> 中频（1k≈0 基准）
    }

    /// 诊断：染色组合（Tube+BBE 同时开）整体频响是否仍平坦。
    #[test]
    fn diag_coloring_combined() {
        let rate = 48000.0f32;
        let freqs = [100.0, 1000.0, 5000.0, 10000.0, 18000.0];
        let mut p = DspParams::default();
        p.enabled = true;
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        chain.set_coloring(3.0, 1.0); // tube drive=3 + bbe 全开
        let resp = measure_chain(&mut chain, &freqs, rate);
        print!("[Tube+BBE 组合] ");
        for (f, d) in &resp { print!("{f:.0}={d:+.1} "); }
        println!();
        let mut max_dev = 0.0f32;
        for (_, d) in &resp { max_dev = max_dev.max(d.abs()); }
        println!("[Tube+BBE] 最大频响偏差 = {max_dev:.1}dB（应 < 2dB）");
        let thd = measure_thd(&mut chain, 1000.0, rate);
        println!("[Tube+BBE] 1kHz THD+N = {thd:.1}dB");
    }

    /// 诊断：混响——冲激响应能量/衰减时间（RT60 估算）。
    #[test]
    fn diag_reverb() {
        let rate = 48000.0f32;
        let mut p = DspParams::default();
        p.enabled = true;
        p.reverb_enabled = true;
        p.reverb_mix = 1.0;      // 全湿，便于测混响本身
        p.reverb_decay = 0.7;
        p.reverb_damping = 0.5;
        p.reverb_pre_delay_ms = 20.0;
        p.reverb_low_cut_hz = 100.0;
        p.reverb_mod_depth = 0.3;
        p.reverb_width = 1.0;
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        // 冲激响应：单样本 1.0，其余 0
        let frames = 48000usize; // 1s
        let mut buf = vec![0.0f32; frames * 2];
        buf[0] = 1.0; buf[1] = 1.0;
        chain.process_interleaved(&mut buf, 2);
        // 分段能量
        let seg = frames / 10;
        let mut energies = Vec::new();
        for s in 0..10 {
            let mut e = 0.0f32;
            for i in (s*seg)..((s+1)*seg) {
                e += buf[i*2]*buf[i*2] + buf[i*2+1]*buf[i*2+1];
            }
            energies.push(e);
        }
        print!("[Reverb IR 分段能量] ");
        for (i,e) in energies.iter().enumerate() { print!("{:.0}ms={e:.4} ", i as f32*100.0); }
        println!();
        // 峰值 + 早期反射是否可闻
        let peak = buf.iter().fold(0.0f32, |a,&b| a.max(b.abs()));
        println!("[Reverb] 冲激响应峰值 = {peak:.4}（应>0.01，否则混响过弱）");
        // 尾段能量（应逐渐衰减但非 0）
        let tail: f32 = buf[frames*2-frames/5..].iter().map(|x| x*x).sum();
        println!("[Reverb] 末段(0.8-1s)能量 = {tail:.5}（应>0，混响尾巴存在）");
    }

    /// 诊断：压缩器——输入超阈值时是否压住。
    #[test]
    fn diag_compressor() {
        let rate = 48000.0f32;
        let mut p = DspParams::default();
        p.enabled = true;
        p.compressor_enabled = true;
        p.compressor_threshold_db = -12.0;
        p.compressor_ratio = 4.0;
        p.compressor_makeup_db = 0.0;
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        // 输入 0.5（-6dBFS，超阈值 -12dB）正弦
        let frames = 48000usize;
        let mut buf = Vec::with_capacity(frames*2);
        for i in 0..frames {
            let s = (2.0*std::f32::consts::PI*1000.0*(i as f32/rate)).sin()*0.5;
            buf.push(s); buf.push(s);
        }
        let in_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=buf[i*2]*buf[i*2]; } (s/(frames/2) as f32).sqrt() };
        chain.process_interleaved(&mut buf, 2);
        let out_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=buf[i*2]*buf[i*2]; } (s/(frames/2) as f32).sqrt() };
        let g = 20.0*(out_rms/in_rms).log10();
        println!("[Compressor] 输入0.5(-6dB,超阈值-12dB) 增益 = {g:+.1}dB（应<0，压住）");
    }

    /// 诊断：限幅器——超阈值时是否限到阈值。
    #[test]
    fn diag_limiter() {
        let rate = 48000.0f32;
        let mut p = DspParams::default();
        p.enabled = true;
        p.limiter_enabled = true;
        p.limiter_threshold_db = -6.0; // 阈值 0.5
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        let frames = 96000usize;
        let mut buf = Vec::with_capacity(frames*2);
        for i in 0..frames {
            let s = (2.0*std::f32::consts::PI*1000.0*(i as f32/rate)).sin()*0.9; // 0.9 > 阈值0.5
            buf.push(s); buf.push(s);
        }
        chain.process_interleaved(&mut buf, 2);
        let peak = buf[frames..].iter().fold(0.0f32, |a,&b| a.max(b.abs()));
        println!("[Limiter] 输入峰值0.9, 阈值-6dB(0.5) → 输出峰值 = {peak:.3}（应≈0.5）");
    }

    /// 诊断：Crossfeed——应只交叉低频、保持总能量。
    #[test]
    fn diag_crossfeed() {
        let rate = 48000.0f32;
        let mut p = DspParams::default();
        p.enabled = true;
        p.crossfeed_enabled = true;
        p.crossfeed_amount = 1.0;
        p.crossfeed_delay_ms = 0.3;
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        // 左声道给信号，右声道静音 → 右声道应收到串扰
        let frames = 24000usize;
        let mut buf = vec![0.0f32; frames*2];
        for i in 0..frames {
            buf[i*2] = (2.0*std::f32::consts::PI*500.0*(i as f32/rate)).sin()*0.5; // 左=500Hz
            // 右=0
        }
        chain.process_interleaved(&mut buf, 2);
        let l_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=buf[i*2]*buf[i*2]; } (s/(frames/2) as f32).sqrt() };
        let r_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=buf[i*2+1]*buf[i*2+1]; } (s/(frames/2) as f32).sqrt() };
        println!("[Crossfeed] 左输入0.5 → L={l_rms:.4} R={r_rms:.4}（R应>0=有串扰，且<R<L）");
    }

    /// 诊断：相位翻转——输出应与输入反相（频谱一致）。
    #[test]
    fn diag_phase_invert() {
        let rate = 48000.0f32;
        let mut p = DspParams::default();
        p.enabled = true;
        p.phase_invert = true;
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        let frames = 8192usize;
        let mut buf = Vec::with_capacity(frames*2);
        for i in 0..frames {
            let s = (2.0*std::f32::consts::PI*1000.0*(i as f32/rate)).sin()*0.5;
            buf.push(s); buf.push(s);
        }
        let orig = buf.clone();
        chain.process_interleaved(&mut buf, 2);
        // 稳定段：输出应 ≈ -输入
        let mut max_err = 0.0f32;
        for i in (frames/2)..frames {
            max_err = max_err.max((buf[i*2] + orig[i*2]).abs());
        }
        println!("[Phase] 反相后 输出+输入 最大偏差 = {max_err:.5}（应≈0）");
    }

    /// 诊断：立体声宽度——width=2 侧信号增强，width=0 变单声道。
    #[test]
    fn diag_width_balance() {
        let rate = 48000.0f32;
        let frames = 8192usize;
        let mk = || {
            let mut b = Vec::with_capacity(frames*2);
            for i in 0..frames {
                let l = (2.0*std::f32::consts::PI*1000.0*(i as f32/rate)).sin()*0.3;
                let r = -(2.0*std::f32::consts::PI*1000.0*(i as f32/rate)).sin()*0.3; // 反相=纯侧信号
                b.push(l); b.push(r);
            }
            b
        };
        let rms = |b: &[f32]| { let mut s=0.0f32; for i in (frames/2)..frames { s+=b[i*2]*b[i*2]+b[i*2+1]*b[i*2+1]; } (s/(2*(frames/2)) as f32).sqrt() };
        // width=2（加宽）
        let mut p = DspParams::default();
        p.enabled = true; p.width_enabled = true; p.stereo_width = 2.0; p.balance = 0.0;
        let mut c = DspChain::new(48000); c.set_params(p);
        let mut b = mk(); let in_rms = rms(&b); c.process_interleaved(&mut b, 2);
        let w2 = rms(&b);
        // width=1（原始）
        let mut p1 = DspParams::default();
        p1.enabled = true; p1.width_enabled = true; p1.stereo_width = 1.0;
        let mut c1 = DspChain::new(48000); c1.set_params(p1);
        let mut b1 = mk(); c1.process_interleaved(&mut b1, 2);
        let w1 = rms(&b1);
        // width=0（单声道，侧信号消失）
        let mut p0 = DspParams::default();
        p0.enabled = true; p0.width_enabled = true; p0.stereo_width = 0.0;
        let mut c0 = DspChain::new(48000); c0.set_params(p0);
        let mut b0 = mk(); c0.process_interleaved(&mut b0, 2);
        let w0 = rms(&b0);
        println!("[Width] 纯侧信号: width2={w2:.4} width1={w1:.4} width0={w0:.4}（应 w2>w1>w0≈0）");
        // 平衡：全左
        let mut pb = DspParams::default();
        pb.enabled = true; pb.width_enabled = true; pb.stereo_width = 1.0; pb.balance = 1.0;
        let mut cb = DspChain::new(48000); cb.set_params(pb);
        let mut bb = mk(); cb.process_interleaved(&mut bb, 2);
        let l_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=bb[i*2]*bb[i*2]; } (s/(frames/2) as f32).sqrt() };
        let r_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=bb[i*2+1]*bb[i*2+1]; } (s/(frames/2) as f32).sqrt() };
        println!("[Balance] balance=1(全右): L={l_rms:.4} R={r_rms:.4}（应 R>L，L≈0）");
    }

    /// 诊断：通道矩阵——swap/mono/left_both/right_both。
    #[test]
    fn diag_channel_matrix() {
        let rate = 48000.0f32;
        let frames = 4096usize;
        // 左=1000Hz, 右=2000Hz（可区分）
        let mk = || {
            let mut b = Vec::with_capacity(frames*2);
            for i in 0..frames {
                b.push((2.0*std::f32::consts::PI*1000.0*(i as f32/rate)).sin()*0.3);
                b.push((2.0*std::f32::consts::PI*2000.0*(i as f32/rate)).sin()*0.3);
            }
            b
        };
        let run = |mode: &str| -> Vec<f32> {
            let mut p = DspParams::default();
            p.enabled = true; p.channel_matrix = mode.to_string();
            let mut c = DspChain::new(48000); c.set_params(p);
            let mut b = mk(); c.process_interleaved(&mut b, 2); b
        };
        // swap: L↔R，应 左轨变成原右轨
        let sw = run("swap");
        let l_after = sw[frames]; // 稳定段某点
        println!("[Matrix swap] 处理前后(单点) L: {:.4} → {:.4}", mk()[frames], l_after);
        // mono: L=R，左右相同
        let mo = run("mono");
        let mut max_diff = 0.0f32;
        for i in (frames/2)..frames { max_diff = max_diff.max((mo[i*2]-mo[i*2+1]).abs()); }
        println!("[Matrix mono] L-R 最大差 = {max_diff:.5}（应≈0，左右相同）");
        // left_both: R=L
        let lb = run("left_both");
        let mut max_diff2 = 0.0f32;
        for i in (frames/2)..frames { max_diff2 = max_diff2.max((lb[i*2]-lb[i*2+1]).abs()); }
        println!("[Matrix left_both] L-R 最大差 = {max_diff2:.5}（应≈0）");
    }

    /// 诊断：ReplayGain——增益应精确。
    #[test]
    fn diag_replaygain() {
        let rate = 48000.0f32;
        let mut p = DspParams::default();
        p.enabled = true;
        p.replaygain_enabled = true;
        p.replaygain_db = 6.0;      // +6dB → ×2
        p.replaygain_preamp_db = 0.0;
        let mut chain = DspChain::new(48000);
        chain.set_params(p);
        let frames = 8192usize;
        let mut buf = Vec::with_capacity(frames*2);
        for i in 0..frames {
            let s = (2.0*std::f32::consts::PI*1000.0*(i as f32/rate)).sin()*0.1;
            buf.push(s); buf.push(s);
        }
        let in_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=buf[i*2]*buf[i*2]; } (s/(frames/2) as f32).sqrt() };
        chain.process_interleaved(&mut buf, 2);
        let out_rms = { let mut s=0.0f32; for i in (frames/2)..frames { s+=buf[i*2]*buf[i*2]; } (s/(frames/2) as f32).sqrt() };
        let g = 20.0*(out_rms/in_rms).log10();
        println!("[ReplayGain] +6dB 设置 → 实测 {g:+.2}dB（应≈+6）");
    }

    /// 真实音频分析：读 FLAC → 中途切音效 → 检测输出爆点（相邻样本跳变）。
    ///
    /// 复现主界面切音效爆音：对同一段真实音乐，在固定块处切换
    /// 滤波器参数（模拟换预设），扫描输出的「相邻样本最大跳变」。
    /// 正常情况下音乐相邻样本差很小（连续信号）；爆音处会出现尖峰。
    ///
    /// 需设置环境变量 XIATIAO_TEST_FLAC 指向测试音频；未设置则跳过。
    #[test]
    fn analyze_switch_pop_real_audio() {
        let path = match std::env::var("XIATIAO_TEST_FLAC") {
            Ok(p) => p,
            Err(_) => { println!("[分析] 未设置 XIATIAO_TEST_FLAC，跳过"); return; }
        };
        use symphonia::core::audio::SampleBuffer;
        use symphonia::core::codecs::DecoderOptions;
        use symphonia::core::formats::FormatOptions;
        use symphonia::core::io::MediaSourceStream;
        use symphonia::core::meta::MetadataOptions;
        use symphonia::core::probe::Hint;
        let file = match std::fs::File::open(&path) { Ok(f) => f, Err(e) => { println!("打开失败: {e}"); return; } };
        let mss = MediaSourceStream::new(Box::new(file), Default::default());
        let mut hint = Hint::new(); hint.with_extension("flac");
        let probed = match symphonia::default::get_probe().format(&hint, mss, &FormatOptions::default(), &MetadataOptions::default()) { Ok(p) => p, Err(e) => { println!("probe 失败: {e}"); return; } };
        let mut format = probed.format;
        let track = match format.default_track() { Some(t) => t, None => { println!("无音轨"); return; } };
        let track_id = track.id;
        let sr = track.codec_params.sample_rate.unwrap_or(44100);
        let mut decoder = match symphonia::default::get_codecs().make(&track.codec_params, &DecoderOptions::default()) { Ok(d) => d, Err(e) => { println!("解码器失败: {e}"); return; } };

        let mut chain = DspChain::new(sr);
        let mut p1 = DspParams::default();
        p1.enabled = true;
        chain.set_params(p1);
        // 切换目标：开 EQ（系数变化，模拟换预设）
        let mut p2 = DspParams::default();
        p2.enabled = true;
        p2.eq_enabled = true;
        p2.eq_gains = vec![0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,6.0,6.0];
        p2.bass_enabled = true;
        p2.bass_gain_db = 6.0;

        let switch_block = 40usize;
        let mut sample_buf: Option<SampleBuffer<f32>> = None;
        let mut block = 0usize;
        // 记录全流程输出（用于跳变扫描）
        let mut all_out: Vec<f32> = Vec::new();
        let mut switch_pos = 0usize;
        loop {
            let packet = match format.next_packet() { Ok(p) => p, Err(_) => break };
            if packet.track_id() != track_id { continue; }
            let ab = match decoder.decode(&packet) { Ok(b) => b, Err(_) => continue };
            if sample_buf.is_none() {
                let spec = *ab.spec();
                sample_buf = Some(SampleBuffer::<f32>::new(ab.capacity() as u64, spec));
            }
            let buf = sample_buf.as_mut().unwrap();
            buf.copy_interleaved_ref(ab);
            let mut pcm: Vec<f32> = buf.samples().to_vec();
            if block == switch_block {
                switch_pos = all_out.len();
                chain.set_params(p2.clone());
            }
            chain.process_interleaved(&mut pcm, 2);
            all_out.extend_from_slice(&pcm);
            block += 1;
            if block > 120 { break; }
        }
        // 扫描相邻样本最大跳变（左声道），定位爆点
        let mut max_jump = 0.0f32;
        let mut max_at = 0usize;
        for i in (2..all_out.len()).step_by(2) {
            let d = (all_out[i] - all_out[i-2]).abs();
            if d > max_jump { max_jump = d; max_at = i; }
        }
        let switch_samp = switch_pos;
        println!("[分析] 样本数={} 切换点样本={} ", all_out.len(), switch_samp);
        println!("[分析] 全程相邻样本最大跳变 = {max_jump:.5} @样本{max_at}（切换点附近={})", (max_at as isize - switch_samp as isize).abs() < 4096);
        // 切换点附近的局部跳变
        let lo = switch_samp.saturating_sub(4096);
        let hi = (switch_samp + 4096).min(all_out.len());
        let mut local_jump = 0.0f32;
        for i in ((lo+2)..hi).step_by(2) {
            let d = (all_out[i] - all_out[i-2]).abs();
            if d > local_jump { local_jump = d; }
        }
        println!("[分析] 切换点±4096 样本内最大跳变 = {local_jump:.5}");
        // 对比：远离切换点的正常段落跳变
        let mut norm_jump = 0.0f32;
        for i in (2..lo.min(all_out.len())).step_by(2) {
            let d = (all_out[i] - all_out[i-2]).abs();
            if d > norm_jump { norm_jump = d; }
        }
        println!("[分析] 切换前正常段最大跳变 = {norm_jump:.5}");
        println!("[分析] 切换处跳变 / 正常跳变 = {:.1}x（>3x 说明切换处有爆点）", local_jump / norm_jump.max(1e-9));
    }

    /// 诊断：过采样器本身的失真（染色前的抗混叠质量）。
    #[test]
    fn diag_oversample_quality() {
        let rate = 48000.0f32;
        // 高频正弦过 4x 上采样→下采样，应尽量保真（THD 低、电平准）
        let freqs = [1000.0f32, 8000.0, 15000.0, 18000.0];
        let mut os = crate::oversample::Oversample2x::new();
        for &f in &freqs {
            let frames = 24000usize;
            let mut input = Vec::with_capacity(frames*2);
            for i in 0..frames {
                let s = (2.0*std::f32::consts::PI*f*(i as f32/rate)).sin()*0.3;
                input.push(s); input.push(s);
            }
            let mut up = Vec::new(); os.upsample(&input, &mut up);
            let mut down = Vec::new(); os.downsample(&up, &mut down);
            let st = frames/2; let n = (frames-st) as f32;
            let mut in_e = 0.0f32; let mut out_e = 0.0f32;
            for i in st..frames { in_e += input[i*2]*input[i*2]; out_e += down[i*2]*down[i*2]; }
            let g = 10.0*(out_e/in_e.max(1e-12)).log10();
            println!("[OS] {f:.0}Hz 往返增益 = {g:+.2}dB（应≈0；大偏差=插值/抗混叠劣化）");
        }
    }
}
