//! 动态等响度补偿（Loudness）——Rust 自研。
//!
//! 背景：Camilla 的 Loudness 不知道「播放器音量」，无法动态补偿。
//! 本模块利用 Rust 侧已知的音量，实现「随音量动态的两端补偿」，
//! 算法参照 ISO 226 等响曲线（低频非线性、高频线性），并加上限。
//!
//! 设计：
//! - 以「相对满幅(0dBFS)的衰减量 delta」为自变量；
//!   音量越低 → delta 越大 → 补偿越多。
//! - 低频：非线性（delta^1.2），因低频在低音量下迟钝更剧；
//! - 高频：线性，补偿较缓；
//! - 均有上限，避免过补导致失真。

/// 参考音量（dBFS）：视为「标准聆听」的响度。
/// 低于此值时开始补偿；高于此值补偿为 0。
const REFERENCE_DBFS: f32 = 0.0;

/// 低频补偿系数（dB / (delta^1.2)）。
const K_LOW: f32 = 0.45;
/// 高频补偿系数（dB / delta）。
const K_HIGH: f32 = 0.18;
/// 低频补偿上限（dB）。
const MAX_LOW_DB: f32 = 12.0;
/// 高频补偿上限（dB）。
const MAX_HIGH_DB: f32 = 6.0;
/// 低频补偿中心频率（Hz）。
const LOW_FREQ: f32 = 100.0;
/// 高频补偿中心频率（Hz）。
const HIGH_FREQ: f32 = 8000.0;

/// 补偿结果：低频/高频增益（dB，>=0）。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LoudnessComp {
    /// 低频提升（dB）
    pub low_db: f32,
    /// 高频提升（dB）
    pub high_db: f32,
}

impl LoudnessComp {
    pub const ZERO: Self = Self { low_db: 0.0, high_db: 0.0 };
}

/// 计算等响度补偿量。
///
/// - `volume`: 当前播放器音量（0.0~1.0）。
/// - `amount`: 用户设定的强度（0.0~1.0，0 关闭）。
///
/// 返回低频/高频应提升的 dB（均 >= 0）。
pub fn compute(volume: f32, amount: f32) -> LoudnessComp {
    let amt = amount.clamp(0.0, 1.0);
    if amt <= 1e-6 {
        return LoudnessComp::ZERO;
    }
    // 当前音量 → dBFS（vol=1.0 → 0dBFS；vol=0.5 → -6dBFS）
    let vol_db = 20.0 * volume.max(1e-4).log10();
    // 相对参考的衰减量（音量低于参考 → delta > 0）
    let delta = (REFERENCE_DBFS - vol_db).clamp(0.0, 40.0) * amt;
    if delta <= 1e-6 {
        return LoudnessComp::ZERO;
    }
    // 低频：非线性（delta^1.2）
    let low_db = (K_LOW * delta.powf(1.2)).min(MAX_LOW_DB);
    // 高频：线性
    let high_db = (K_HIGH * delta).min(MAX_HIGH_DB);
    LoudnessComp { low_db, high_db }
}

/// 低频补偿中心频率（供 DspChain 设置 Lowshelf）。
pub fn low_freq() -> f32 { LOW_FREQ }
/// 高频补偿中心频率（供 DspChain 设置 Highshelf）。
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
        // vol=1.0 → vol_db=0 → delta = REFERENCE(-6) - 0 = -6 → clamp 0 → 不补偿
        let c = compute(1.0, 1.0);
        assert!(c.low_db < 0.1 && c.high_db < 0.1, "满音量不该补偿: {c:?}");
    }

    #[test]
    fn low_volume_more_comp() {
        let mid = compute(0.5, 1.0);   // -6dBFS
        let low = compute(0.1, 1.0);   // -20dBFS
        assert!(low.low_db > mid.low_db, "音量越低补偿越多: {low:?} vs {mid:?}");
        assert!(low.high_db > mid.high_db);
    }

    #[test]
    fn low_freq_comp_more_than_high() {
        // 低频补偿应大于高频（低频更迟钝）
        let c = compute(0.1, 1.0);
        assert!(c.low_db > c.high_db, "低频应补得比高频多: {c:?}");
    }

    #[test]
    fn clamped_to_max() {
        // 极小音量 → 补偿有上限
        let c = compute(0.001, 1.0);
        assert!(c.low_db <= MAX_LOW_DB + 0.01);
        assert!(c.high_db <= MAX_HIGH_DB + 0.01);
    }
}

