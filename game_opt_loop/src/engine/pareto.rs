//! 三目标 Pareto 支配排序
//!
//! 目标向量 = `[延迟 ms ↓, 整机功耗 W ↓, 画质 dB ↑]`。
//!
//! 一个关键设计选择: **PSNR 既是约束也是目标, 但两者作用的位置不同。**
//! - 作为**约束**: PSNR < 基线的个体在进排序之前就被剔除, 它不是「差一点的解」,
//!   而是「不合格品」—— 否则一个 0.1ms 但糊成一片的算子会永远霸占前沿一角,
//!   还会把变异器往牺牲画质换帧率的方向带 (§2 红线 3 明令禁止)。
//! - 作为**目标**: 在合格品之间, 更高的 PSNR 值得用延迟去换, 所以它参与支配比较。

/// 三目标向量。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Objectives {
    /// 越小越好。
    pub latency_ms: f32,
    /// 越小越好。
    pub power_watt: f32,
    /// 越大越好。
    pub psnr_db: f32,
}

/// 浮点比较容差。低于这个差异视为打平 —— 真机噪声下 0.001ms 的"优势"没有意义。
const EPS_LATENCY: f32 = 5e-3;
const EPS_POWER: f32 = 1e-2;
const EPS_PSNR: f32 = 1e-2;

impl Objectives {
    /// self 是否 **支配** other: 每一维都不差, 且至少一维确实更好。
    pub fn dominates(&self, other: &Objectives) -> bool {
        let lat_worse = self.latency_ms > other.latency_ms + EPS_LATENCY;
        let pow_worse = self.power_watt > other.power_watt + EPS_POWER;
        let psnr_worse = self.psnr_db < other.psnr_db - EPS_PSNR;
        if lat_worse || pow_worse || psnr_worse {
            return false;
        }

        let lat_better = self.latency_ms < other.latency_ms - EPS_LATENCY;
        let pow_better = self.power_watt < other.power_watt - EPS_POWER;
        let psnr_better = self.psnr_db > other.psnr_db + EPS_PSNR;
        lat_better || pow_better || psnr_better
    }
}

/// 计算非支配前沿 (rank 0) 的下标。
pub fn front_indices(points: &[Objectives]) -> Vec<usize> {
    (0..points.len())
        .filter(|&i| !points.iter().enumerate().any(|(j, p)| j != i && p.dominates(&points[i])))
        .collect()
}

/// 非支配分层排序 (NSGA-II 的 fast-non-dominated-sort)。
///
/// 返回每一层的下标列表, 第 0 层就是 Pareto 前沿。分层用于选亲本:
/// 只拿前沿做亲本会过早收敛, 带上次一层能保住多样性。
pub fn non_dominated_sort(points: &[Objectives]) -> Vec<Vec<usize>> {
    let n = points.len();
    let mut dominated_by: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut domination_count = vec![0usize; n];
    let mut fronts: Vec<Vec<usize>> = Vec::new();
    let mut current: Vec<usize> = Vec::new();

    for p in 0..n {
        for q in 0..n {
            if p == q {
                continue;
            }
            if points[p].dominates(&points[q]) {
                dominated_by[p].push(q);
            } else if points[q].dominates(&points[p]) {
                domination_count[p] += 1;
            }
        }
        if domination_count[p] == 0 {
            current.push(p);
        }
    }

    while !current.is_empty() {
        let mut next = Vec::new();
        for &p in &current {
            for &q in &dominated_by[p] {
                domination_count[q] -= 1;
                if domination_count[q] == 0 {
                    next.push(q);
                }
            }
        }
        fronts.push(std::mem::take(&mut current));
        next.sort_unstable();
        current = next;
    }

    fronts
}

/// 拥挤距离 (NSGA-II)。同一层里距离越大越稀疏, 优先选它当亲本以保住多样性。
pub fn crowding_distance(points: &[Objectives], front: &[usize]) -> Vec<f32> {
    let m = front.len();
    let mut dist = vec![0.0f32; m];
    if m <= 2 {
        return vec![f32::INFINITY; m];
    }

    // 三个目标各扫一遍。方向无关紧要: 拥挤距离只看间距。
    let extractors: [fn(&Objectives) -> f32; 3] = [
        |o| o.latency_ms,
        |o| o.power_watt,
        |o| o.psnr_db,
    ];

    for get in extractors {
        let mut order: Vec<usize> = (0..m).collect();
        order.sort_by(|&a, &b| {
            get(&points[front[a]])
                .partial_cmp(&get(&points[front[b]]))
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        dist[order[0]] = f32::INFINITY;
        dist[order[m - 1]] = f32::INFINITY;

        let lo = get(&points[front[order[0]]]);
        let hi = get(&points[front[order[m - 1]]]);
        let span = hi - lo;
        if span.abs() < f32::EPSILON {
            continue;
        }

        for k in 1..m - 1 {
            let prev = get(&points[front[order[k - 1]]]);
            let next = get(&points[front[order[k + 1]]]);
            dist[order[k]] += (next - prev) / span;
        }
    }

    dist
}

#[cfg(test)]
mod tests {
    use super::*;

    fn o(latency: f32, power: f32, psnr: f32) -> Objectives {
        Objectives {
            latency_ms: latency,
            power_watt: power,
            psnr_db: psnr,
        }
    }

    #[test]
    fn strictly_better_on_every_axis_dominates() {
        assert!(o(0.5, 4.5, 39.0).dominates(&o(1.0, 4.9, 38.0)));
        assert!(!o(1.0, 4.9, 38.0).dominates(&o(0.5, 4.5, 39.0)));
    }

    #[test]
    fn a_single_improvement_with_no_regression_dominates() {
        assert!(o(0.5, 4.5, 39.0).dominates(&o(1.0, 4.5, 39.0)));
    }

    /// 画质换来的延迟劣势 —— 典型的权衡, 谁也不支配谁。
    #[test]
    fn a_genuine_trade_off_is_mutually_non_dominated() {
        let sharp_but_slow = o(1.4, 4.9, 39.5);
        let fast_but_soft = o(0.4, 4.4, 37.6);
        assert!(!sharp_but_slow.dominates(&fast_but_soft));
        assert!(!fast_but_soft.dominates(&sharp_but_slow));
    }

    #[test]
    fn identical_points_do_not_dominate_each_other() {
        let a = o(1.0, 4.8, 38.5);
        assert!(!a.dominates(&a));
    }

    /// 差异小于容差视为打平 —— 真机噪声下 0.001ms 的"优势"不是优势。
    #[test]
    fn differences_within_noise_do_not_count_as_domination() {
        assert!(!o(1.0000, 4.80, 38.50).dominates(&o(1.0001, 4.80, 38.50)));
    }

    #[test]
    fn front_keeps_only_non_dominated_points() {
        let pts = vec![
            o(0.4, 4.4, 37.6), // 最快
            o(1.4, 4.9, 39.5), // 最好画质
            o(0.9, 4.6, 38.6), // 折中
            o(1.5, 5.1, 38.0), // 被折中点全面碾压
        ];
        let front = front_indices(&pts);
        assert_eq!(front, vec![0, 1, 2]);
    }

    #[test]
    fn layers_are_ordered_and_partition_every_point() {
        let pts = vec![
            o(0.4, 4.4, 39.0), // 前沿
            o(0.8, 4.6, 38.0), // 被 0 支配
            o(1.2, 4.9, 37.5), // 被 0 和 1 支配
        ];
        let fronts = non_dominated_sort(&pts);
        assert_eq!(fronts, vec![vec![0], vec![1], vec![2]]);
        let total: usize = fronts.iter().map(|f| f.len()).sum();
        assert_eq!(total, pts.len());
    }

    #[test]
    fn every_point_lands_in_exactly_one_layer() {
        let pts: Vec<Objectives> = (0..20)
            .map(|i| {
                let t = i as f32;
                o(0.3 + t * 0.06, 4.3 + t * 0.03, 37.4 + (t * 0.7).sin() * 1.5)
            })
            .collect();
        let fronts = non_dominated_sort(&pts);
        let mut seen: Vec<usize> = fronts.iter().flatten().copied().collect();
        seen.sort_unstable();
        seen.dedup();
        assert_eq!(seen.len(), pts.len());
    }

    #[test]
    fn crowding_marks_the_extremes_as_maximally_sparse() {
        let pts = vec![
            o(0.4, 4.4, 37.6),
            o(0.9, 4.6, 38.6),
            o(1.4, 4.9, 39.5),
        ];
        let front = vec![0usize, 1, 2];
        let d = crowding_distance(&pts, &front);
        assert!(d[0].is_infinite() && d[2].is_infinite());
        assert!(d[1].is_finite());
    }
}
