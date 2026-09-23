//! 汇总统计的小工具。
//!
//! 只做展示层需要的那几个量, 并且**与 phonefarm 那侧的口径逐字对齐**:
//! 离散度 = (max - min) / median × 100。这个口径不是随便定的 ——
//! `loop_v1/*/report.json` 里已经落盘的 `dispersion_pct` 就是这么算的,
//! 面板自己算出来的数必须和报告里现成的数对得上, 否则同一个载体会出现两个"离散度"。

/// 中位数。空切片返回 `None`。
///
/// 偶数个取中间两个的均值 —— 与 phonefarm 报告一致 (见 `bw_median` 的
/// `7175.0` / `7350.0` 这类半整数, 那就是偶数样本取均值留下的痕迹)。
pub fn median(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mid = sorted.len() / 2;
    Some(if sorted.len().is_multiple_of(2) {
        (sorted[mid - 1] + sorted[mid]) / 2.0
    } else {
        sorted[mid]
    })
}

/// 离散度百分比: (max - min) / median × 100。
///
/// 样本不足 2 个时返回 `None` —— 一个点没有"离散度"这回事,
/// 返回 0 会让前端把"只跑了一轮"显示成"完美可复现"。
pub fn dispersion_pct(values: &[f64]) -> Option<f64> {
    if values.len() < 2 {
        return None;
    }
    let med = median(values)?;
    if med.abs() < f64::EPSILON {
        return None;
    }
    let max = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let min = values.iter().cloned().fold(f64::INFINITY, f64::min);
    Some((max - min) / med * 100.0)
}

/// 四舍五入到 n 位小数, 纯粹为了前端少显示一串浮点噪声。
pub fn round_to(value: f64, digits: u32) -> f64 {
    let factor = 10f64.powi(digits as i32);
    (value * factor).round() / factor
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn median_odd_and_even() {
        assert_eq!(median(&[3.0, 1.0, 2.0]), Some(2.0));
        assert_eq!(median(&[1.0, 2.0, 3.0, 4.0]), Some(2.5));
        assert_eq!(median(&[]), None);
    }

    /// 用 phonefarm 现成的落盘数字反推口径: refbench `frame_p50` 五轮
    /// 离散度报的是 0.2658%, 我们必须算出同一个数。
    #[test]
    fn dispersion_matches_phonefarm_report() {
        let values = [8.289, 8.2765, 8.267, 8.289, 8.274];
        let got = dispersion_pct(&values).expect("五个样本应算得出离散度");
        assert!(
            (got - 0.2658).abs() < 0.001,
            "离散度口径与 phonefarm 报告不一致: got {got}, 报告 0.2658"
        );
    }

    /// 原神世界臂 `frame_p95` 报的是 2.673%。
    #[test]
    fn dispersion_matches_genshin_report() {
        let values = [20.0028, 20.0268, 19.681, 20.2164, 20.0707];
        let got = dispersion_pct(&values).expect("五个样本应算得出离散度");
        assert!((got - 2.673).abs() < 0.001, "got {got}");
    }

    #[test]
    fn dispersion_needs_two_samples() {
        assert_eq!(dispersion_pct(&[]), None);
        assert_eq!(dispersion_pct(&[16.7]), None, "单轮不该被当成完美可复现");
    }

    #[test]
    fn dispersion_guards_zero_median() {
        assert_eq!(dispersion_pct(&[0.0, 0.0]), None);
    }

    #[test]
    fn round_to_trims_float_noise() {
        assert_eq!(round_to(2.67299999, 3), 2.673);
        assert_eq!(round_to(-0.10199999, 2), -0.1);
    }
}
