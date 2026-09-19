//! DSP 参数定义（与 Python 下发的 JSON 对应）。
//!
//! 从 dsp.rs 拆出。全部字段可选，缺省用默认值。

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

/// 功能归属：判断某个功能应由 Camilla 还是 Rust 处理。
///
/// 规则（按用户要求）：**功能重叠时优先 Camilla**；Rust 只做 Camilla 没有的
/// （混响 / tube / BBE / 宽度 / 平衡 / crossfeed / ReplayGain）。
///
/// 这里集中表达归属，替代原先散落在 refresh_camilla_flag 里的一堆 `||` 判断。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Owner {
    /// 归 CamillaDSP 处理。
    Camilla,
    /// 归 Rust 内置链处理。
    Rust,
}

/// 返回 Camilla 是否应参与（任一 Camilla 归属的功能开启即参与）。
pub fn camilla_should_engage(p: &DspParams) -> bool {
    // 「启用 DSP」总开关关闭时，Camilla 同样不参与（与 Rust 旁通保持一致）。
    if !p.enabled {
        return false;
    }
    camilla_features(p).iter().any(|(_, on)| *on)
}

/// Camilla 归属的功能清单：(功能名, 是否开启)。
///
/// 只列 Camilla 能提供的功能；Rust 独有的不在此列。
pub fn camilla_features(p: &DspParams) -> Vec<(&'static str, bool)> {
    vec![
        ("pre_gain", p.pre_gain_db.abs() > 1e-6),
        ("eq", p.eq_enabled),
        ("peq", p.peq_enabled),
        ("convolution", p.convolution_enabled),
        ("bass", p.bass_enabled),
        ("treble", p.treble_gain_db.abs() > 1e-6),
        // 注：Loudness 不归 Camilla——由 Rust 动态实现（依赖播放器音量，
        // Camilla 拿不到音量）。见 dsp/loudness.rs 与 DspChain.rebuild_loudness。
        ("compressor", p.compressor_enabled),
        ("channel_matrix", p.channel_matrix != "off"),
        ("phase_invert", p.phase_invert),
    ]
}
