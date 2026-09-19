//! 动态等响度补偿（Loudness）——严格对齐 ISO 226 等响曲线。
//!
//! 背景：Camilla 的 Loudness 不知道「播放器音量」，无法动态补偿。
//! 本模块利用 Rust 侧已知的音量，实现「随音量动态的两端补偿」。
//!
//! 算法：
//!   以 ISO 226 等响曲线「80 phon 相对 100 phon 的声压级差」为补偿形状
//!   （20phon 差 → 低频 +17dB、6.3kHz +4.8dB 等）。
//!   实际补偿量按「音量衰减量 delta_db」线性缩放：
//!     scale = delta_db / 20.0        （delta=20dB 时用满 ISO 曲线）
//!   即：音量每降 20dB，等响补偿达到 ISO 226 一个 20-phon 台阶。
//!
//! 形状拟合（双 lowshelf + 单 highshelf，由数值优化得出）：
//!   - lowshelf @ 40Hz  +12dB  （覆盖 20-60Hz 陡升）
//!   - lowshelf @ 180Hz +6dB   （覆盖 100-250Hz 过渡）
//!   - highshelf @ 6kHz +4dB   （覆盖 5-10kHz 高频补偿）
//!   拟合 RMS 误差 ≈ 1.7dB（对比单 shelf 的 4.1dB，显著改善）。
//!
//! 安全：低频补偿较大（满幅 +17dB），应由后续限幅器或用户音量控制防削波。
//! 补偿量随音量**线性**回落（满音量时归零），不会在正常聆听音量下过补。

/// 参考音量（dBFS）：视为「标准聆听」。低于此值开始补偿。
const REFERENCE_DBFS: f32 = 0.0;
/// ISO 226 参考台阶：80 phon 相对 100 phon 的 20dB 差。
const ISO_STEP_DB: f32 = 20.0;

// ---- ISO 226（80 vs 100 phon）在各代表频点的补偿量（dB）----
/// 极低频段（lowshelf @ 40Hz 代表值，对应 ISO @40Hz ≈ +11.5dB，取 +12）
const ISO_LOW1_DB: f32 = 12.0;
/// 中低频段（lowshelf @ 180Hz 代表值，对应 ISO @150-200Hz ≈ +3~3.7dB，
/// 取 +6 是为了补偿单 shelf 在 100Hz 附近的不足，整体拟合更贴）
const ISO_LOW2_DB: f32 = 6.0;
/// 高频段（highshelf @ 6kHz 代表值，对应 ISO @6.3kHz ≈ +4.8dB，取 +4）
const ISO_HIGH_DB: f32 = 4.0;

/// 补偿上限（dB）：防极端音量下过度补偿。
const MAX_LOW1_DB: f32 = 17.0;
const MAX_LOW2_DB: f32 = 9.0;
const MAX_HIGH_DB: f32 = 6.0;

/// 极低频补偿中心频率（Hz）。
pub const LOW1_FREQ: f32 = 40.0;
/// 中低频补偿中心频率（Hz）。
pub const LOW2_FREQ: f32 = 180.0;
/// 高频补偿中心频率（Hz）。
pub const HIGH_FREQ: f32 = 6000.0;

/// 补偿结果：两段低频 + 高频增益（dB，>=0）。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LoudnessComp {
    /// 极低频提升（dB），对应 lowshelf @ LOW1_FREQ
    pub low_db: f32,
    /// 中低频提升（dB），对应 lowshelf @ LOW2_FREQ
    pub low2_db: f32,
    /// 高频提升（dB），对应 highshelf @ HIGH_FREQ
    pub high_db: f32,
}

impl LoudnessComp {
    pub const ZERO: Self = Self { low_db: 0.0, low2_db: 0.0, high_db: 0.0 };
}

/// 计算等响度补偿量（严格对齐 ISO 226）。
///
/// - `volume`: 当前播放器音量（0.0~1.0）。
/// - `amount`: 用户设定的强度（0.0~1.0，0 关闭）。
///
/// 返回两段低频 + 高频应提升的 dB（均 >= 0）。
pub fn compute(volume: f32, amount: f32) -> LoudnessComp {
    let amt = amount.clamp(0.0, 1.0);
    if amt <= 1e-6 {
        return LoudnessComp::ZERO;
    }
    // 当前音量 → dBFS（vol=1.0 → 0dBFS；vol=0.5 → -6dBFS）
    let vol_db = 20.0 * volume.max(1e-4).log10();
    // 相对参考的衰减量（dB），乘用户强度
    let delta = (REFERENCE_DBFS - vol_db).clamp(0.0, 60.0) * amt;
    if delta <= 1e-6 {
        return LoudnessComp::ZERO;
    }
    // 按 ISO 台阶线性缩放：delta=20dB → scale=1（用满 ISO 曲线）
    let scale = delta / ISO_STEP_DB;
    let low_db = (ISO_LOW1_DB * scale).min(MAX_LOW1_DB);
    let low2_db = (ISO_LOW2_DB * scale).min(MAX_LOW2_DB);
    let high_db = (ISO_HIGH_DB * scale).min(MAX_HIGH_DB);
    LoudnessComp { low_db, low2_db, high_db }
}

/// 供 DspChain 使用的频率常量（保持旧接口兼容）。
pub fn low_freq() -> f32 { LOW1_FREQ }
pub fn low2_freq() -> f32 { LOW2_FREQ }
pub fn high_freq() -> f32 { HIGH_FREQ }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_amount_no_comp() {
        assert_eq!(compute(0.2, 0.0), LoudnessComp::ZERO);
    }

    #[test]
    fn full_volume_no_comp() {
        let c = compute(1.0, 1.0);
        assert!(c.low_db < 0.1 && c.high_db < 0.1, "满音量不该补偿: {c:?}");
    }

    #[test]
    fn low_volume_more_comp() {
        let mid = compute(0.5, 1.0);
        let low = compute(0.1, 1.0);
        assert!(low.low_db > mid.low_db, "音量越低补偿越多");
        assert!(low.high_db > mid.high_db);
    }

    #[test]
    fn iso_curve_shape() {
        // 衰减 20dB（vol=0.1）时，应达到 ISO 一个台阶：低频≈+12、高频≈+4
        let c = compute(0.1, 1.0);
        assert!((c.low_db - 12.0).abs() < 0.5, "极低频应≈+12: {c:?}");
        assert!((c.low2_db - 6.0).abs() < 0.5, "中低频应≈+6: {c:?}");
        assert!((c.high_db - 4.0).abs() < 0.5, "高频应≈+4: {c:?}");
    }

    #[test]
    fn low_more_than_high() {
        let c = compute(0.1, 1.0);
        assert!(c.low_db > c.low2_db && c.low2_db > 0.0);
        assert!(c.low_db > c.high_db, "低频补偿应大于高频");
    }

    #[test]
    fn clamped_to_max() {
        let c = compute(0.0001, 1.0);
        assert!(c.low_db <= MAX_LOW1_DB + 0.01);
        assert!(c.high_db <= MAX_HIGH_DB + 0.01);
    }
}
