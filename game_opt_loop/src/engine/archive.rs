//! Pareto 优胜档案库
//!
//! 档案是演化的记忆: 下一代变异的亲本从这里取, 跨代去重也靠这里。
//!
//! 两条硬规矩:
//! 1. **指纹去重**。同构算子不重复占用真机 60s 回放的名额。
//! 2. **来源不可混淆**。真机测量与成本模型估计在同一个档案里共存是允许的
//!    (dry-run 与真机联调会先后写同一份档案), 但每条都带 `source`,
//!    任何汇总都必须先按来源分组, 不能把估计值和实测值混起来算。

use crate::engine::pareto::{self, Objectives};
use crate::engine::{Candidate, EvalReport, MetricSource};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::Path;

/// 档案里的一条记录。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArchiveEntry {
    pub id: String,
    pub generation: u32,
    pub track: String,
    pub parent_id: Option<String>,
    pub fingerprint: String,
    pub origin: crate::engine::CandidateOrigin,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
    pub report: EvalReport,
    /// 写入时刻 (RFC3339)。
    pub recorded_at: String,
    /// 算子源码。档案要能独立复现, 不能只存一个指纹。
    pub shader_source: String,
}

impl ArchiveEntry {
    pub fn objectives(&self) -> Objectives {
        Objectives {
            latency_ms: self.report.metrics.operator_latency_ms,
            power_watt: self.report.metrics.power_watt,
            psnr_db: self.report.metrics.psnr_db,
        }
    }
}

/// 档案库本体。落盘格式即 `contracts/pareto_archive.schema.json`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParetoArchive {
    pub version: u32,
    pub track: String,
    /// 该赛道的耗时硬预算, 写进档案以便离线复核判据。
    pub budget_ms: f32,
    /// 该赛道的画质基线。
    pub quality_baseline_db: f32,
    pub entries: Vec<ArchiveEntry>,
}

impl ParetoArchive {
    pub fn new(track: &str, budget_ms: f32, quality_baseline_db: f32) -> Self {
        Self {
            version: 1,
            track: track.to_string(),
            budget_ms,
            quality_baseline_db,
            entries: Vec::new(),
        }
    }

    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path)
            .with_context(|| format!("读取档案失败: {}", path.display()))?;
        serde_json::from_str(&raw).with_context(|| format!("解析档案失败: {}", path.display()))
    }

    /// 存在就读, 不存在就开一份新的。
    pub fn load_or_new(path: &Path, track: &str, budget_ms: f32, baseline_db: f32) -> Self {
        Self::load(path).unwrap_or_else(|_| Self::new(track, budget_ms, baseline_db))
    }

    /// 原子落盘: 先写临时文件再 rename, 中途断电不会留下半截 JSON。
    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)
                .with_context(|| format!("创建档案目录失败: {}", dir.display()))?;
        }
        let tmp = path.with_extension("json.tmp");
        let body = serde_json::to_string_pretty(self)?;
        std::fs::write(&tmp, body).with_context(|| format!("写入 {} 失败", tmp.display()))?;
        std::fs::rename(&tmp, path)
            .with_context(|| format!("原子替换 {} 失败", path.display()))?;
        Ok(())
    }

    pub fn contains_fingerprint(&self, fingerprint: &str) -> bool {
        self.entries.iter().any(|e| e.fingerprint == fingerprint)
    }

    /// 收录一条结果。指纹重复则跳过, 返回是否真的写入。
    pub fn insert(&mut self, candidate: &Candidate, report: EvalReport, now: String) -> bool {
        if self.contains_fingerprint(&candidate.fingerprint) {
            return false;
        }
        self.entries.push(ArchiveEntry {
            id: candidate.id.clone(),
            generation: candidate.generation,
            track: candidate.track.clone(),
            parent_id: candidate.parent_id.clone(),
            fingerprint: candidate.fingerprint.clone(),
            origin: candidate.origin,
            notes: candidate.notes.clone(),
            report,
            recorded_at: now,
            shader_source: candidate.shader_source.clone(),
        });
        true
    }

    /// 有资格参与前沿计算的记录 (通过全部硬约束的)。
    pub fn admissible(&self) -> Vec<&ArchiveEntry> {
        self.entries
            .iter()
            .filter(|e| e.report.is_admissible(self.budget_ms, self.quality_baseline_db))
            .collect()
    }

    /// 当前 Pareto 前沿。
    pub fn front(&self) -> Vec<&ArchiveEntry> {
        let admissible = self.admissible();
        let points: Vec<Objectives> = admissible.iter().map(|e| e.objectives()).collect();
        pareto::front_indices(&points)
            .into_iter()
            .map(|i| admissible[i])
            .collect()
    }

    /// 选下一代的亲本。
    ///
    /// 先取前沿, 不够 `want` 个就往下一层取, 同层内按拥挤距离从稀疏到密集排,
    /// 保住多样性 —— 只盯着前沿最优那一点会过早收敛。
    pub fn select_parents(&self, want: usize) -> Vec<&ArchiveEntry> {
        let admissible = self.admissible();
        if admissible.is_empty() {
            return Vec::new();
        }
        let points: Vec<Objectives> = admissible.iter().map(|e| e.objectives()).collect();

        let mut picked = Vec::new();
        for layer in pareto::non_dominated_sort(&points) {
            if picked.len() >= want {
                break;
            }
            let dist = pareto::crowding_distance(&points, &layer);
            let mut ranked: Vec<(usize, f32)> =
                layer.iter().copied().zip(dist.iter().copied()).collect();
            ranked.sort_by(|a, b| {
                b.1.partial_cmp(&a.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    // 平手时用下标定序, 保证结果可复现。
                    .then(a.0.cmp(&b.0))
            });
            for (idx, _) in ranked {
                if picked.len() >= want {
                    break;
                }
                picked.push(admissible[idx]);
            }
        }
        picked
    }

    /// 只按真机测量数据算的前沿。对外结论只能引用这个。
    pub fn measured_front(&self) -> Vec<&ArchiveEntry> {
        self.front()
            .into_iter()
            .filter(|e| e.report.source.is_measured())
            .collect()
    }

    pub fn count_by_source(&self, source: MetricSource) -> usize {
        self.entries
            .iter()
            .filter(|e| e.report.source == source)
            .count()
    }

    /// 可行域内历史最优 PSNR —— Gate 4 那条累计序列的每代取值。
    pub fn best_psnr(&self) -> Option<f32> {
        self.admissible()
            .iter()
            .map(|e| e.report.metrics.psnr_db)
            .fold(None, |acc: Option<f32>, v| Some(acc.map_or(v, |a| a.max(v))))
    }

    /// 可行域内历史最低延迟。
    pub fn best_latency(&self) -> Option<f32> {
        self.admissible()
            .iter()
            .map(|e| e.report.metrics.operator_latency_ms)
            .fold(None, |acc: Option<f32>, v| Some(acc.map_or(v, |a| a.min(v))))
    }

    pub fn fingerprints(&self) -> HashSet<String> {
        self.entries.iter().map(|e| e.fingerprint.clone()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::{CandidateOrigin, EvalStatus, Metrics, Verdict};

    fn entry(
        id: &str,
        gen: u32,
        latency: f32,
        power: f32,
        psnr: f32,
        source: MetricSource,
    ) -> (Candidate, EvalReport) {
        let cand = Candidate::new(
            id,
            gen,
            "sr",
            None,
            1.5,
            // 指纹刻意忽略注释, 所以测试样本必须在**代码**上不同,
            // 只换注释的两份源码本来就该被判为同一个算子。
            format!("#version 450\nvoid main() {{ int marker_{id} = 1; }}"),
            CandidateOrigin::LocalMutation,
            None,
        );
        let report = EvalReport {
            candidate_id: id.to_string(),
            status: EvalStatus::Pass,
            metrics: Metrics {
                operator_latency_ms: latency,
                fps_p95_ms: 16.7,
                power_watt: power,
                psnr_db: psnr,
            },
            verdict: Verdict {
                is_pareto_improvement: false,
                p_value: if source.is_measured() { 0.002 } else { 1.0 },
            },
            source,
        };
        (cand, report)
    }

    fn archive_with(
        rows: &[(&str, f32, f32, f32, MetricSource)],
    ) -> ParetoArchive {
        let mut a = ParetoArchive::new("sr", 1.5, 37.286);
        for (i, (id, lat, pow, psnr, src)) in rows.iter().enumerate() {
            let (c, r) = entry(id, i as u32, *lat, *pow, *psnr, *src);
            a.insert(&c, r, "2026-09-21T00:00:00Z".into());
        }
        a
    }

    #[test]
    fn duplicate_fingerprints_are_rejected() {
        let mut a = ParetoArchive::new("sr", 1.5, 37.286);
        let (c, r) = entry("a", 0, 1.0, 4.8, 38.0, MetricSource::StaticCostModel);
        assert!(a.insert(&c, r.clone(), "t".into()));
        // 同一份源码再来一次 → 指纹相同 → 不应重复占名额。
        assert!(!a.insert(&c, r, "t".into()));
        assert_eq!(a.entries.len(), 1);
    }

    #[test]
    fn candidates_over_budget_never_reach_the_front() {
        let a = archive_with(&[
            ("fast", 0.9, 4.7, 38.5, MetricSource::StaticCostModel),
            ("slow", 2.4, 4.6, 40.0, MetricSource::StaticCostModel),
        ]);
        let front: Vec<&str> = a.front().iter().map(|e| e.id.as_str()).collect();
        assert_eq!(front, vec!["fast"]);
    }

    /// 画质低于基线的算子哪怕快得离谱也不许进前沿 —— 否则变异器会学会偷画质。
    #[test]
    fn candidates_below_the_quality_floor_never_reach_the_front() {
        let a = archive_with(&[
            ("sharp", 1.2, 4.8, 38.4, MetricSource::StaticCostModel),
            ("blurry_but_instant", 0.1, 4.3, 35.0, MetricSource::StaticCostModel),
        ]);
        let front: Vec<&str> = a.front().iter().map(|e| e.id.as_str()).collect();
        assert_eq!(front, vec!["sharp"]);
    }

    #[test]
    fn front_keeps_genuine_trade_offs() {
        let a = archive_with(&[
            ("fast_soft", 0.40, 4.40, 37.60, MetricSource::StaticCostModel),
            ("balanced", 0.90, 4.60, 38.60, MetricSource::StaticCostModel),
            ("sharp_slow", 1.40, 4.90, 39.50, MetricSource::StaticCostModel),
            ("dominated", 1.45, 5.00, 38.00, MetricSource::StaticCostModel),
        ]);
        let mut front: Vec<&str> = a.front().iter().map(|e| e.id.as_str()).collect();
        front.sort_unstable();
        assert_eq!(front, vec!["balanced", "fast_soft", "sharp_slow"]);
    }

    #[test]
    fn measured_front_excludes_simulated_entries() {
        let a = archive_with(&[
            ("sim", 0.50, 4.45, 38.20, MetricSource::StaticCostModel),
            ("real", 0.95, 4.62, 38.90, MetricSource::Phonefarm),
        ]);
        assert_eq!(a.front().len(), 2);
        let measured: Vec<&str> = a.measured_front().iter().map(|e| e.id.as_str()).collect();
        assert_eq!(measured, vec!["real"]);
        assert_eq!(a.count_by_source(MetricSource::StaticCostModel), 1);
        assert_eq!(a.count_by_source(MetricSource::Phonefarm), 1);
    }

    #[test]
    fn parent_selection_is_deterministic_and_prefers_the_front() {
        let a = archive_with(&[
            ("fast_soft", 0.40, 4.40, 37.60, MetricSource::StaticCostModel),
            ("balanced", 0.90, 4.60, 38.60, MetricSource::StaticCostModel),
            ("sharp_slow", 1.40, 4.90, 39.50, MetricSource::StaticCostModel),
            ("dominated", 1.45, 5.00, 38.00, MetricSource::StaticCostModel),
        ]);
        let pick = |n| -> Vec<String> {
            a.select_parents(n).iter().map(|e| e.id.clone()).collect()
        };
        assert_eq!(pick(3), pick(3), "亲本选择必须可复现");
        let three = pick(3);
        assert!(!three.contains(&"dominated".to_string()));
        // 要得比前沿还多时才下探到被支配层。
        assert!(pick(4).contains(&"dominated".to_string()));
    }

    #[test]
    fn empty_archive_yields_no_parents() {
        let a = ParetoArchive::new("sr", 1.5, 37.286);
        assert!(a.select_parents(4).is_empty());
        assert!(a.best_psnr().is_none());
    }

    #[test]
    fn round_trips_through_disk_atomically() {
        let dir = std::env::temp_dir().join(format!("gol_archive_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("archive.json");

        let a = archive_with(&[
            ("x", 0.8, 4.5, 38.4, MetricSource::StaticCostModel),
            ("y", 1.1, 4.7, 39.0, MetricSource::Phonefarm),
        ]);
        a.save(&path).unwrap();

        let b = ParetoArchive::load(&path).unwrap();
        assert_eq!(b.entries.len(), 2);
        assert_eq!(b.track, "sr");
        assert_eq!(b.budget_ms, 1.5);
        assert_eq!(b.entries[1].report.source, MetricSource::Phonefarm);
        // 源码必须一起落盘, 档案才能独立复现。
        assert!(b.entries[0].shader_source.contains("#version 450"));
        // 临时文件不该留下。
        assert!(!path.with_extension("json.tmp").exists());

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn best_metrics_track_the_admissible_set_only() {
        let a = archive_with(&[
            ("ok", 0.90, 4.60, 38.60, MetricSource::StaticCostModel),
            ("over_budget", 0.10, 4.20, 41.00, MetricSource::StaticCostModel),
        ]);
        let mut a = a;
        a.entries[1].report.metrics.operator_latency_ms = 9.0;
        assert_eq!(a.best_psnr(), Some(38.60));
        assert_eq!(a.best_latency(), Some(0.90));
    }
}
