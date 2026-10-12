//! 游戏档案 (game profile): `phonefarm onboard` 给一个新游戏实测出来的接入结果。
//!
//! 一份档案回答「这个游戏怎么量」: 怎么进到游戏里 (进门策略与实际点过什么)、用什么负载
//! (只转视角的触控拖动, 净转角为零)、解析哪个渲染线程、可重复性实测到多少、基线是多少、
//! 有哪些坑。契约见 `contracts/game_profile.schema.json`。
//!
//! 分工: 档案由 phonefarm 在真机上**量出来** (设备动作、测量、可重复判定都在那边);
//! 本仓库只做两件事 —— 决定收不收 (只收 `status == PASS` 的), 以及 `optimize` 时把它
//! 用起来 (负载、渲染线程、坑、进门)。档案落在 `profiles/<包名>/`, 内置表认不出的
//! 包名靠它直接能跑。

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};

use super::games::{InputMethod, Pitfall, Target};
use std::borrow::Cow;

/// 档案里 optimize 用得到的那部分。其余字段 (逐轮明细、进门截图路径…) 留在原文件里当证据。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GameProfile {
    pub profile_version: u32,
    /// PASS / FAIL / NEEDS_HUMAN。
    pub status: String,
    pub package: String,
    #[serde(default)]
    pub version_name: Option<String>,
    #[serde(default)]
    pub created_at: Option<String>,
    pub workload: ProfileWorkload,
    #[serde(default)]
    pub entry: Value,
    #[serde(default)]
    pub repeatability: Value,
    #[serde(default)]
    pub baseline: Value,
    #[serde(default)]
    pub conditions: Value,
    #[serde(default)]
    pub pitfalls: Vec<ProfilePitfall>,
    #[serde(default)]
    pub needs_human: Vec<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProfileWorkload {
    /// 负载脚本, 相对档案所在目录。
    pub script: String,
    /// 每轮采集秒数。
    pub seconds: u32,
    #[serde(default)]
    pub render_comm: Option<String>,
    #[serde(default)]
    pub graphics_api: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProfilePitfall {
    pub id: String,
    pub symptom: String,
    pub workaround: String,
}

/// 读进来的档案 + 它在哪。
#[derive(Debug, Clone)]
pub struct Loaded {
    pub path: PathBuf,
    pub profile: GameProfile,
}

impl Loaded {
    /// 负载脚本的绝对路径 (phonefarm 是另一个进程, 相对路径的基准对不上)。
    pub fn workload_path(&self) -> PathBuf {
        let dir = self.path.parent().unwrap_or(Path::new("."));
        let p = dir.join(&self.profile.workload.script);
        std::fs::canonicalize(&p).unwrap_or(p)
    }

    pub fn frame_p95_cv(&self) -> Option<f64> {
        self.profile.repeatability["judge"]["frame_p95_cv"].as_f64()
    }
}

/// 档案落在 `<root>/<包名>/profile.json`。
#[derive(Debug, Clone)]
pub struct Store {
    pub root: PathBuf,
}

impl Default for Store {
    fn default() -> Self {
        Self {
            root: PathBuf::from("profiles"),
        }
    }
}

pub fn read(path: &Path) -> Result<GameProfile> {
    let raw =
        std::fs::read_to_string(path).with_context(|| format!("读不到 {}", path.display()))?;
    let p: GameProfile = serde_json::from_str(&raw)
        .with_context(|| format!("{} 不是一份游戏档案", path.display()))?;
    if p.profile_version != 1 {
        bail!(
            "{}: 档案版本 {} 不认识 (只认 1)",
            path.display(),
            p.profile_version
        );
    }
    Ok(p)
}

impl Store {
    pub fn path_of(&self, package: &str) -> PathBuf {
        self.root.join(package).join("profile.json")
    }

    /// 找这个包名的档案。没有就是 None; 有但读坏了是 Err —— 坏档案不该被悄悄当成「没有」。
    pub fn find(&self, package: &str) -> Result<Option<Loaded>> {
        let path = self.path_of(package);
        if !path.is_file() {
            return Ok(None);
        }
        let profile = read(&path)?;
        if profile.package != package {
            bail!(
                "{} 里写的包名是 {}, 不是 {package}",
                path.display(),
                profile.package
            );
        }
        Ok(Some(Loaded { path, profile }))
    }

    /// 收一份 onboard 产出的档案: 只收 PASS, 连同负载脚本一起拷进 `<root>/<包名>/`。
    pub fn install(&self, src_profile: &Path) -> Result<PathBuf> {
        let p = read(src_profile)?;
        if p.status != "PASS" {
            bail!(
                "档案状态是 {} 不是 PASS, 不收 (原因见 {} 的 repeatability / needs_human)",
                p.status,
                src_profile.display()
            );
        }
        let src_dir = src_profile.parent().unwrap_or(Path::new("."));
        let wl = src_dir.join(&p.workload.script);
        if !wl.is_file() {
            bail!("档案引用的负载脚本不在: {}", wl.display());
        }
        let dst_dir = self.root.join(&p.package);
        std::fs::create_dir_all(&dst_dir)?;
        std::fs::copy(&wl, dst_dir.join(&p.workload.script))
            .with_context(|| format!("拷负载脚本到 {} 失败", dst_dir.display()))?;
        let dst = dst_dir.join("profile.json");
        std::fs::copy(src_profile, &dst)?;
        Ok(dst)
    }
}

/// 档案的摘要, 跟着目标进 caps.json / report.json —— 读报告的人要能看出
/// 「负载是从哪份档案来的、那份档案量到的离散度是多少」。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProfileRef {
    pub path: String,
    pub status: String,
    pub created_at: Option<String>,
    pub version_name: Option<String>,
    pub frame_p95_cv: Option<f64>,
    pub baseline: Value,
    pub entry_taps: Option<u64>,
}

/// 把档案用到目标上: 负载、渲染线程、坑。只用 PASS 的档案; 不是 PASS 时目标原样不动,
/// 返回 false 由调用方说明原因。
pub fn apply(target: &mut Target, loaded: &Loaded) -> bool {
    let p = &loaded.profile;
    if p.status != "PASS" {
        return false;
    }
    let wl = loaded.workload_path();
    target.workload.script = Some(wl.display().to_string());
    target.workload.script_present = wl.is_file();
    target.workload.seconds = p.workload.seconds.max(1);
    target.workload.input = InputMethod::Touch;
    target.workload.render_comm = p.workload.render_comm.clone();
    for pf in &p.pitfalls {
        if !target.pitfalls.iter().any(|x| x.id == pf.id.as_str()) {
            target.pitfalls.push(Pitfall {
                id: Cow::Owned(pf.id.clone()),
                symptom: Cow::Owned(pf.symptom.clone()),
                workaround: Cow::Owned(pf.workaround.clone()),
            });
        }
    }
    target.resolved_via = format!(
        "{} + 游戏档案 {}",
        target.resolved_via,
        loaded.path.display()
    );
    target.profile = Some(ProfileRef {
        path: loaded.path.display().to_string(),
        status: p.status.clone(),
        created_at: p.created_at.clone(),
        version_name: p.version_name.clone(),
        frame_p95_cv: loaded.frame_p95_cv(),
        baseline: p.baseline.clone(),
        entry_taps: p.entry["taps"].as_u64(),
    });
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::optimize::games::{resolve, BoxKind, CarrierRoots};

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("gol_profile_{name}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn sample(status: &str, pkg: &str) -> Value {
        serde_json::json!({
            "profile_version": 1, "status": status, "package": pkg, "version_name": "1.0",
            "workload": {"script": "workload.json", "seconds": 30, "render_comm": "RHIThread", "graphics_api": "vulkan"},
            "entry": {"taps": 2}, "repeatability": {"judge": {"verdict": status, "frame_p95_cv": 0.012}},
            "baseline": {"fps_mean": 59.8}, "conditions": {},
            "pitfalls": [{"id": "render_thread_not_unity", "symptom": "s", "workaround": "w"}],
            "needs_human": []
        })
    }

    fn write_src(dir: &Path, v: &Value) -> PathBuf {
        std::fs::write(dir.join("workload.json"), "[]").unwrap();
        let p = dir.join("profile.json");
        std::fs::write(&p, serde_json::to_string(v).unwrap()).unwrap();
        p
    }

    fn no_roots() -> CarrierRoots {
        CarrierRoots {
            carriers_dir: "/nonexistent/c".into(),
            siblings_dir: "/nonexistent/s".into(),
        }
    }

    #[test]
    fn install_takes_only_pass_profiles_and_find_reads_them_back() {
        let src = tmp("src");
        let store = Store { root: tmp("store") };
        let bad = write_src(&src, &sample("FAIL", "com.example.game"));
        assert!(store.install(&bad).is_err(), "FAIL 的档案不收");
        let good = write_src(&src, &sample("PASS", "com.example.game"));
        let dst = store.install(&good).unwrap();
        assert!(dst.is_file());
        let l = store.find("com.example.game").unwrap().unwrap();
        assert_eq!(l.profile.workload.render_comm.as_deref(), Some("RHIThread"));
        assert!(l.workload_path().is_file());
        assert!(store.find("com.other").unwrap().is_none());
    }

    #[test]
    fn a_profile_turns_an_unknown_package_into_a_runnable_target() {
        let src = tmp("apply");
        let store = Store {
            root: tmp("apply_store"),
        };
        store
            .install(&write_src(&src, &sample("PASS", "com.example.game")))
            .unwrap();
        let mut t = resolve("com.example.game", None, &no_roots());
        assert!(!t.workload.script_present, "没有档案时黑盒目标没有负载");
        let l = store.find("com.example.game").unwrap().unwrap();
        assert!(apply(&mut t, &l));
        assert_eq!(t.box_kind, BoxKind::BlackBox);
        assert!(t.workload.script_present);
        assert_eq!(t.workload.seconds, 30);
        assert_eq!(t.workload.render_comm.as_deref(), Some("RHIThread"));
        assert!(t.pitfalls.iter().any(|p| p.id == "render_thread_not_unity"));
        assert!(t.profile.as_ref().unwrap().frame_p95_cv.is_some());
    }

    #[test]
    fn a_non_pass_profile_changes_nothing() {
        let dir = tmp("nonpass");
        let p = write_src(&dir, &sample("NEEDS_HUMAN", "com.example.game"));
        let loaded = Loaded {
            path: p.clone(),
            profile: read(&p).unwrap(),
        };
        let mut t = resolve("com.example.game", None, &no_roots());
        assert!(!apply(&mut t, &loaded));
        assert!(t.profile.is_none());
        assert!(t.workload.script.is_none());
    }

    #[test]
    fn package_mismatch_is_an_error_not_a_miss() {
        let store = Store {
            root: tmp("mismatch"),
        };
        let d = store.root.join("com.a.b");
        std::fs::create_dir_all(&d).unwrap();
        write_src(&d, &sample("PASS", "com.c.d"));
        assert!(store.find("com.a.b").is_err());
    }
}
