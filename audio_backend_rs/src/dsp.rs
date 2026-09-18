//! DSP 音效处理管线。
//!
//! 固定管线顺序：
//!   预增益 → 图形EQ(10段) → 低音增强 → 高音增强
//!          → 立体声宽度 → 左右平衡 → 限幅器 → 输出
//!
//! 全局旁路：enabled=false 时 PCM 原样直通（BitPerfect）。
//! 参数通过 IPC 的 SetDsp 命令传入（JSON），实时生效。

use serde::{Deserialize, Serialize};

/// EQ 段数（10 段图形 EQ）。
pub const EQ_BANDS: usize = 10;
/// 10 段图形 EQ 的中心频率。
pub const EQ_FREQS: [f32; EQ_BANDS] = [
    31.0, 62.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0,
];

/// DSP 处理阶段（串联时用）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DspStage {
    /// 前置段：ReplayGain（Camilla 之前）。
    Pre,
    /// 后置段：压缩/限幅/crossfeed/宽度/平衡/相位/矩阵（Camilla 之后）。
    Post,
}

/// PEQ 频段。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct PeqBand {
    pub freq: f32,
    pub gain: f32,
    pub q: f32,
    /// 类型："pk"（峰值）/"ls"（低架）/"hs"（高架）
    pub kind: String,
}

impl Default for PeqBand {
    fn default() -> Self {
        PeqBand { freq: 1000.0, gain: 0.0, q: 1.0, kind: "pk".to_string() }
    }
}

/// DSP 参数（对应 JSON）。全部可选，缺省用默认值。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct DspParams {
    /// 全局开关（false = 旁路直通）
    pub enabled: bool,
    /// 预增益（dB）
    pub pre_gain_db: f32,
    /// ReplayGain 开关
    pub replaygain_enabled: bool,
    /// ReplayGain 当前增益（dB，由 Python 侧按文件标签计算后下发）
    pub replaygain_db: f32,
    /// ReplayGain 前置增益（dB）
    pub replaygain_preamp_db: f32,
    /// Headroom（余量管理）开关
    pub headroom_enabled: bool,
    /// Headroom 衰减（dB，负值，如 -6.0）
    pub headroom_db: f32,
    /// 卷积（IR）开关
    pub convolution_enabled: bool,
    /// 卷积干声比 0.0~1.0
    pub convolution_dry: f32,
    /// 卷积湿声比 0.0~2.0
    pub convolution_wet: f32,
    /// 参数均衡器（PEQ）开关
    pub peq_enabled: bool,
    /// PEQ 频段：[{freq, gain, q, type}]，type: pk/ls/hs
    pub peq_bands: Vec<PeqBand>,
    /// 10 段图形 EQ 开关（保留兼容，默认关）
    pub eq_enabled: bool,
    /// 10 段图形 EQ 增益（dB）
    pub eq_gains: Vec<f32>,
    /// 低音增强：开关
    pub bass_enabled: bool,
    /// 低音增强：增益（dB）
    pub bass_gain_db: f32,
    /// 低音增强：中心频率（Hz）
    pub bass_freq: f32,
    /// Loudness（等响度）开关
    pub loudness_enabled: bool,
    /// Loudness 强度 0.0~1.0（0=关, 1=强）
    pub loudness_amount: f32,
    /// 高音增强：增益（dB）
    pub treble_gain_db: f32,
    /// 高音增强：中心频率（Hz）
    pub treble_freq: f32,
    /// Crossfeed 开关（耳机串扰）
    pub crossfeed_enabled: bool,
    /// Crossfeed 强度 0.0~1.0
    pub crossfeed_amount: f32,
    /// Crossfeed 延迟（毫秒，0.2~1.0）
    pub crossfeed_delay_ms: f32,
    /// 立体声宽度（0=单声道, 1=原始, 2=加宽）
    pub stereo_width: f32,
    /// 立体声宽度开关
    pub width_enabled: bool,
    /// 左右平衡（-1=全左, 0=居中, 1=全右）
    pub balance: f32,
    /// 压缩器开关
    pub compressor_enabled: bool,
    /// 压缩器阈值（dBFS，如 -18.0）
    pub compressor_threshold_db: f32,
    /// 压缩器压缩比（如 2.0 = 2:1）
    pub compressor_ratio: f32,
    /// 压缩器增益补偿（dB）
    pub compressor_makeup_db: f32,
    /// 限幅器阈值（dBFS，如 -1.0）
    pub limiter_threshold_db: f32,
    /// 限幅器开关
    pub limiter_enabled: bool,
    /// 全局相位翻转（反相）
    pub phase_invert: bool,
    /// 通道矩阵模式：off / swap / mono / left_both / right_both
    pub channel_matrix: String,
    // ---- 混响（Rust 独有，高质量 Freeverb + 预延迟 + 低切）----
    /// 混响开关
    pub reverb_enabled: bool,
    /// 干湿比 0.0~1.0（0=全干，1=全湿）
    pub reverb_mix: f32,
    /// 预延迟（毫秒，0~100）
    pub reverb_pre_delay_ms: f32,
    /// 衰减（房间大小感）0.0~1.0
    pub reverb_decay: f32,
    /// 高频阻尼 0.0~1.0（越大越闷）
    pub reverb_damping: f32,
    /// 立体声宽度 0.0~1.0
    pub reverb_width: f32,
    /// 低切（Hz，20~500），去混响低频浑浊
    pub reverb_low_cut_hz: f32,
    /// 调制深度 0.0~1.0（打散金属感，让混响更自然）
    pub reverb_mod_depth: f32,
}

impl Default for DspParams {
    fn default() -> Self {
        DspParams {
            enabled: false,
            pre_gain_db: 0.0,
            replaygain_enabled: false,
            replaygain_db: 0.0,
            replaygain_preamp_db: 0.0,
            headroom_enabled: false,
            headroom_db: 0.0,
            convolution_enabled: false,
            convolution_dry: 1.0,
            convolution_wet: 0.5,
            peq_enabled: false,
            peq_bands: Vec::new(),
            eq_enabled: false,
            eq_gains: vec![0.0; EQ_BANDS],
            bass_enabled: false,
            bass_gain_db: 0.0,
            bass_freq: 100.0,
            loudness_enabled: false,
            loudness_amount: 0.0,
            treble_gain_db: 0.0,
            treble_freq: 8000.0,
            crossfeed_enabled: false,
            crossfeed_amount: 0.3,
            crossfeed_delay_ms: 0.3,
            stereo_width: 1.0,
            width_enabled: false,
            balance: 0.0,
            compressor_enabled: false,
            compressor_threshold_db: -18.0,
            compressor_ratio: 2.0,
            compressor_makeup_db: 0.0,
            limiter_threshold_db: 0.0,
            limiter_enabled: false,
            phase_invert: false,
            channel_matrix: "off".to_string(),
            reverb_enabled: false,
            reverb_mix: 0.25,
            reverb_pre_delay_ms: 20.0,
            reverb_decay: 0.5,
            reverb_damping: 0.5,
            reverb_width: 1.0,
            reverb_low_cut_hz: 100.0,
            reverb_mod_depth: 0.5,
        }
    }
}

impl DspParams {
    pub fn from_json(v: &serde_json::Value) -> Self {
        serde_json::from_value(v.clone()).unwrap_or_default()
    }
}

// ============================================================
// 双二阶滤波器（峰值 EQ）
// ============================================================

/// RBJ 峰值滤波器（peaking EQ）系数。
#[derive(Clone, Copy)]
struct Biquad {
    b0: f32,
    b1: f32,
    b2: f32,
    a1: f32,
    a2: f32,
    // 状态
    x1: f32,
    x2: f32,
    y1: f32,
    y2: f32,
}

impl Biquad {
    fn identity() -> Self {
        Biquad { b0: 1.0, b1: 0.0, b2: 0.0, a1: 0.0, a2: 0.0, x1: 0.0, x2: 0.0, y1: 0.0, y2: 0.0 }
    }

    /// 峰值滤波器（gain dB, freq Hz, Q）。
    fn peaking(freq: f32, gain_db: f32, q: f32, sample_rate: f32) -> Self {
        let a = 10f32.powf(gain_db / 40.0);
        let w0 = 2.0 * std::f32::consts::PI * freq / sample_rate;
        let cos_w0 = w0.cos();
        let sin_w0 = w0.sin();
        let alpha = sin_w0 / (2.0 * q);
        let b0 = 1.0 + alpha * a;
        let b1 = -2.0 * cos_w0;
        let b2 = 1.0 - alpha * a;
        let a0 = 1.0 + alpha / a;
        let a1 = -2.0 * cos_w0;
        let a2 = 1.0 - alpha / a;
        Biquad {
            b0: b0 / a0,
            b1: b1 / a0,
            b2: b2 / a0,
            a1: a1 / a0,
            a2: a2 / a0,
            x1: 0.0, x2: 0.0, y1: 0.0, y2: 0.0,
        }
    }

    /// 低架滤波器（low shelf）。
    fn lowshelf(freq: f32, gain_db: f32, sample_rate: f32) -> Self {
        let a = 10f32.powf(gain_db / 40.0);
        let w0 = 2.0 * std::f32::consts::PI * freq / sample_rate;
        let cos_w0 = w0.cos();
        let sin_w0 = w0.sin();
        let alpha = sin_w0 / 2.0 * (2.0f32).sqrt();
        let two_sqrt_a_alpha = 2.0 * a.sqrt() * alpha;
        let b0 = a * ((a + 1.0) - (a - 1.0) * cos_w0 + two_sqrt_a_alpha);
        let b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cos_w0);
        let b2 = a * ((a + 1.0) - (a - 1.0) * cos_w0 - two_sqrt_a_alpha);
        let a0 = (a + 1.0) + (a - 1.0) * cos_w0 + two_sqrt_a_alpha;
        let a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cos_w0);
        let a2 = (a + 1.0) + (a - 1.0) * cos_w0 - two_sqrt_a_alpha;
        Biquad {
            b0: b0 / a0,
            b1: b1 / a0,
            b2: b2 / a0,
            a1: a1 / a0,
            a2: a2 / a0,
            x1: 0.0, x2: 0.0, y1: 0.0, y2: 0.0,
        }
    }

    /// 高架滤波器（high shelf）。
    fn highshelf(freq: f32, gain_db: f32, sample_rate: f32) -> Self {
        let a = 10f32.powf(gain_db / 40.0);
        let w0 = 2.0 * std::f32::consts::PI * freq / sample_rate;
        let cos_w0 = w0.cos();
        let sin_w0 = w0.sin();
        let alpha = sin_w0 / 2.0 * (2.0f32).sqrt();
        let two_sqrt_a_alpha = 2.0 * a.sqrt() * alpha;
        let b0 = a * ((a + 1.0) + (a - 1.0) * cos_w0 + two_sqrt_a_alpha);
        let b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cos_w0);
        let b2 = a * ((a + 1.0) + (a - 1.0) * cos_w0 - two_sqrt_a_alpha);
        let a0 = (a + 1.0) - (a - 1.0) * cos_w0 + two_sqrt_a_alpha;
        let a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cos_w0);
        let a2 = (a + 1.0) - (a - 1.0) * cos_w0 - two_sqrt_a_alpha;
        Biquad {
            b0: b0 / a0,
            b1: b1 / a0,
            b2: b2 / a0,
            a1: a1 / a0,
            a2: a2 / a0,
            x1: 0.0, x2: 0.0, y1: 0.0, y2: 0.0,
        }
    }

    #[inline]
    fn process(&mut self, x: f32) -> f32 {
        let y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
            - self.a1 * self.y1 - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y
    }
}

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
    /// Camilla 引擎是否参与串联。
    /// 由参数自动推导（任一 Camilla 功能开着即为 true）：
    /// 预增益 / 图形EQ / PEQ / 卷积 / 低音 / 高音 / Loudness。
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

    /// 运行时设置全局开关。Camilla 是否参与由参数自动推导（见 refresh_camilla_flag）。
    pub fn set_engine(&mut self, dsp_enabled: bool) {
        self.dsp_enabled = dsp_enabled;
        self.refresh_camilla_flag();
    }

    /// 由当前参数自动推导 Camilla 是否参与。
    /// 任一 Camilla 功能开着，即视为参与（避免与 Rust 重复处理）。
    pub fn refresh_camilla_flag(&mut self) {
        let p = &self.params;
        self.camilla_enabled = p.pre_gain_db.abs() > 1e-6
            || p.eq_enabled
            || p.peq_enabled
            || p.convolution_enabled
            || p.bass_enabled
            || (p.treble_gain_db.abs() > 1e-6)
            || p.loudness_enabled
            // 以下归 Camilla 优先：开了就让 Camilla 参与（Rust 跳过）
            // 注：limiter_enabled 默认开，不纳入，否则 Camilla 会总参与
            // 注：width/balance 归 Rust（需平滑拖动），不纳入
            || p.compressor_enabled
            || (p.channel_matrix != "off")
            || p.phase_invert;
    }

    /// 供引擎读取：Camilla 当前是否参与。
    pub fn camilla_active(&self) -> bool {
        self.camilla_enabled
    }

    /// 仅做 Camilla 之外的功能（供串联用）。
    /// 当 camilla_enabled=true 时，只跑后置段：压缩/限幅/crossfeed/宽度/平衡/相位/矩阵。
    /// 前置段（ReplayGain）也在此处理。
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
        // EQ：为每段重建滤波器
        for i in 0..EQ_BANDS {
            let g = p.eq_gains.get(i).copied().unwrap_or(0.0);
            let f = EQ_FREQS[i];
            // 保留状态，只更新系数
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
        // Loudness（低频提升，强度映射到增益 0~6dB，加高频轻微提升）
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
        // 未参与时：保留原行为（含 pre_gain 与 loudness 补偿）。
        let cam = self.camilla_enabled;
        let loud_comp = if !cam && p.loudness_enabled {
            10f32.powf(-(p.loudness_amount.clamp(0.0, 1.0) * 6.0 * 0.5) / 20.0)
        } else {
            1.0
        };
        let pre_gain_lin = if cam { 1.0 } else { 10f32.powf(p.pre_gain_db / 20.0) };
        let pre = pre_gain_lin * headroom * loud_comp;
        // 宽度/平衡归 Rust：逐样本处理 + 参数平滑（避免拖动时块间跳变）。
        let target_width = if p.width_enabled { p.stereo_width.clamp(0.0, 2.0) } else { 1.0 };
        let target_balance = p.balance.clamp(-1.0, 1.0);
        // 目标左右增益（平衡）：balance>0 减左，balance<0 减右
        let target_bal_l = if target_balance > 0.0 { 1.0 - target_balance } else { 1.0 };
        let target_bal_r = if target_balance < 0.0 { 1.0 + target_balance } else { 1.0 };
        // 首次：直接吸附到目标，避免启动时从 1.0 滑入
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
        // 攻击/释放系数（约 10ms / 200ms）
        let comp_attack = 0.005;
        let comp_release = 0.0005;
        // Camilla 参与时：EQ/PEQ/低音 归 Camilla，Rust 跳过
        let eq_on = p.eq_enabled && !cam;
        let peq_on = p.peq_enabled && !cam;
        let bass_on = p.bass_enabled && !cam;
        let treble_on = !cam;
        // Crossfeed 参数
        let cf_on = p.crossfeed_enabled;
        let cf_amt = p.crossfeed_amount.clamp(0.0, 1.0);
        let cf_delay = ((p.crossfeed_delay_ms.max(0.0) / 1000.0) * self.in_rate) as usize;
        let cf_delay = cf_delay.min(self.cf_buf_l.len().saturating_sub(1));
        let loud_on = !cam && p.loudness_enabled && p.loudness_amount > 0.0;

        // 相位/矩阵：Camilla 参与时由 Camilla 处理，Rust 跳过
        let phase_inv = !cam && p.phase_invert;
        let ch_mode = if cam { "off".to_string() } else { p.channel_matrix.clone() };
        // 混响：更新参数（实时），开关决定是否处理
        let reverb_on = p.reverb_enabled;
        if reverb_on {
            self.reverb.set_params(
                p.reverb_mix, p.reverb_decay, p.reverb_damping,
                p.reverb_width, p.reverb_low_cut_hz, p.reverb_pre_delay_ms,
                p.reverb_mod_depth,
            );
        }
        // 染色（电子管 / BBE）——只在非 camilla 的内置链里生效
        let mut tube = self.tube.take();
        let mut bbe = self.bbe.take();
        // 平滑系数：约 15ms 时间常数（44.1k 下）
        let smooth_coef = 1.0 - (-1.0f32 / (0.015 * self.in_rate)).exp();
        for frame in buf.chunks_mut(2) {
            let mut l = frame[0] * pre;
            let mut r = frame[1] * pre;
            // 宽度/平衡当前值逐样本朝目标逼近（平滑过渡）
            self.cur_width += (target_width - self.cur_width) * smooth_coef;
            self.cur_bal_l += (target_bal_l - self.cur_bal_l) * smooth_coef;
            self.cur_bal_r += (target_bal_r - self.cur_bal_r) * smooth_coef;

            // 音色染色：先电子管（非线性），再 BBE（相位）。
            // Camilla 参与时跳过（由 CamillaEngine 的染色路径负责，位于卷积之后）。
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

            // 通道矩阵（与前端取值对齐：off/swap/mono/left_both/right_both）
            match ch_mode.as_str() {
                "swap" => { std::mem::swap(&mut l, &mut r); }
                "mono" => { let m = (l + r) * 0.5; l = m; r = m; }
                "left_both" => { r = l; }
                "right_both" => { l = r; }
                _ => {}
            }
            // 全局相位翻转
            if phase_inv {
                l = -l;
                r = -r;
            }

            // 参数均衡器（PEQ）
            if peq_on {
                for i in 0..self.peq_l.len() {
                    l = self.peq_l[i].process(l);
                    r = self.peq_r[i].process(r);
                }
            }
            // 10 段图形 EQ（独立开关）
            if eq_on {
                for i in 0..EQ_BANDS {
                    l = self.eq_l[i].process(l);
                    r = self.eq_r[i].process(r);
                }
            }
            // 低音增强（独立开关）
            if bass_on {
                l = self.bass_l.process(l);
                r = self.bass_r.process(r);
            }
            // 高音增强（归 Camilla 时跳过）
            if treble_on {
                l = self.treble_l.process(l);
                r = self.treble_r.process(r);
            }
            // Loudness（等响度低频补偿）
            if loud_on {
                l = self.loudness_l.process(l);
                r = self.loudness_r.process(r);
            }
            // 立体声宽度（M/S）——用平滑值
            if (self.cur_width - 1.0).abs() > 1e-6 {
                let m = (l + r) * 0.5;
                let s = (l - r) * 0.5 * self.cur_width;
                l = m + s;
                r = m - s;
            }
            // 左右平衡——用平滑值
            l *= self.cur_bal_l;
            r *= self.cur_bal_r;
            // 混响（Rust 独有，放平衡后、输出前）
            if reverb_on {
                let (rl, rr) = self.reverb.process(l, r);
                l = rl;
                r = rr;
            }
            // Crossfeed（耳机串扰：对侧延迟信号混入本侧）
            if cf_on {
                let n = self.cf_buf_l.len();
                // 读取延迟后的对侧信号
                let idx = (self.cf_pos + n - cf_delay) % n;
                let cl = self.cf_buf_l[idx];
                let cr = self.cf_buf_r[idx];
                // 写入当前样本
                self.cf_buf_l[self.cf_pos] = l;
                self.cf_buf_r[self.cf_pos] = r;
                self.cf_pos = (self.cf_pos + 1) % n;
                // 低通（一阶，系数 0.3 ≈ 700Hz 截止，只混低频）
                self.cf_lp_l += 0.3 * (cr - self.cf_lp_l);
                self.cf_lp_r += 0.3 * (cl - self.cf_lp_r);
                // 混入：左 += 右延迟（低通）* amount；右 += 左延迟（低通）* amount
                l += self.cf_lp_l * cf_amt;
                r += self.cf_lp_r * cf_amt;
            }
            // 压缩器（简单峰值压缩）
            if comp_on {
                let peak = l.abs().max(r.abs());
                // 包络跟踪
                let coef = if peak > self.comp_env { comp_attack } else { comp_release };
                self.comp_env += (peak - self.comp_env) * coef;
                // 超过阈值则压缩
                if self.comp_env > comp_thresh && comp_thresh > 0.0 {
                    // 计算超出的 dB 数，按压缩比缩减
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
            // 限幅器（平滑 attack/release，只压峰值）
            if lim_on && lim_thresh > 0.0 {
                let peak = l.abs().max(r.abs());
                // 目标增益：超过阈值则需衰减
                let target_gain = if peak > lim_thresh {
                    lim_thresh / peak
                } else {
                    1.0
                };
                // 平滑：attack 较快（0.05），release 较慢（0.0005）
                let coef = if target_gain < self.limiter_gain { 0.05 } else { 0.0005 };
                self.limiter_gain += (target_gain - self.limiter_gain) * coef;
                self.limiter_gain = self.limiter_gain.clamp(0.0, 1.0);
                l *= self.limiter_gain;
                r *= self.limiter_gain;
            }

            frame[0] = l;
            frame[1] = r;
        }
        // 归还染色器（保持状态）
        self.tube = tube;
        self.bbe = bbe;
    }
}
