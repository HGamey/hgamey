//! Finite consumers of the existing phonefarm artifact and attributed receiver contracts.
use super::*;
use std::collections::{BTreeMap, BTreeSet};

pub(super) const PROFILE: &str = "kgsl_queued_origin_spf_v1";
const SOURCE: &[u8] = include_bytes!("../tools/contracts/phonefarm-render-reader-v1.c");
const ARTIFACT: [&str; 5] = [
    "source.c",
    "reader.elf",
    "build.json",
    "compiler.stdout",
    "compiler.stderr",
];
const RECEIVER_NATIVE: [&str; 5] = [
    "render_reader.source.c",
    "render_reader.elf",
    "render_reader.build.json",
    "render_reader.compiler.stdout",
    "render_reader.compiler.stderr",
];
pub(super) fn implementation_hash() -> String {
    sha(&[
        include_bytes!("system_case_blackbox.rs").as_slice(),
        include_bytes!("system_case_blackbox_contract.rs").as_slice(),
        SOURCE,
    ]
    .concat())
}
pub(super) fn package(s: &str) -> bool {
    s.len() <= 128
        && s.contains('.')
        && s.split('.').all(|part| {
            !part.is_empty()
                && part.as_bytes()[0].is_ascii_alphabetic()
                && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
        })
}
fn inventory(d: &Dir) -> Result<BTreeSet<String>> {
    let fd = unsafe { libc::dup(d.0.as_raw_fd()) };
    ensure!(fd >= 0, "native inventory dup");
    let stream = unsafe { libc::fdopendir(fd) };
    if stream.is_null() {
        unsafe {
            libc::close(fd);
        }
        bail!("native inventory open");
    }
    let mut names = BTreeSet::new();
    let result = (|| -> Result<()> {
        loop {
            errno::set_errno(errno::Errno(0));
            let entry = unsafe { libc::readdir(stream) };
            if entry.is_null() {
                ensure!(errno::errno().0 == 0, "native inventory read error");
                break;
            }
            let n = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) }.to_str()?;
            if n != "." && n != ".." {
                names.insert(n.to_owned());
            }
            ensure!(names.len() <= 5, "extra native artifact input");
        }
        Ok(())
    })();
    let closed = unsafe { libc::closedir(stream) };
    result?;
    ensure!(closed == 0, "native inventory close");
    Ok(names)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Compiler {
    path: String,
    sha256: String,
    clang_path: String,
    clang_sha256: String,
    package_sha256: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Execution {
    exit: i32,
    stdout_sha256: String,
    stderr_sha256: String,
    process_group_absent: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Build {
    schema: String,
    status: String,
    target: String,
    ndk_revision: String,
    flags: Vec<String>,
    source_sha256: String,
    elf_sha256: String,
    compiler: Compiler,
    execution: Execution,
    source_authentication: String,
    device_run: bool,
    installed_kernel_source_verified: bool,
    game_proof: bool,
}
pub(super) fn validate_artifact(files: &BTreeMap<String, Vec<u8>>) -> Result<()> {
    let get = |n| {
        files
            .get(&format!("native-reader/{n}"))
            .context("missing native artifact")
    };
    let b: Build = serde_json::from_slice(get("build.json")?)?;
    let source = get("source.c")?;
    let elf = get("reader.elf")?;
    ensure!(
        source.as_slice() == SOURCE
            && source.len() <= 128 * 1024
            && get("build.json")?.len() <= 128 * 1024
            && get("compiler.stdout")?.len() <= 128 * 1024
            && get("compiler.stderr")?.len() <= 128 * 1024
            && elf.len() <= 1024 * 1024
            && b.schema == "phonefarm/render_reader_build@1"
            && b.status == "BUILT"
            && b.target == "aarch64-linux-android30"
            && b.ndk_revision == "28.2.13676358"
            && b.flags
                == [
                    "-std=c11",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-fPIE",
                    "-pie",
                    "-Wl,-z,relro,-z,now"
                ]
            && b.source_sha256 == sha(source)
            && b.elf_sha256 == sha(elf)
            && b.execution.exit == 0
            && b.execution.process_group_absent
            && b.execution.stdout_sha256 == sha(get("compiler.stdout")?)
            && b.execution.stderr_sha256 == sha(get("compiler.stderr")?)
            && b.source_authentication == "local_build_receipt_not_independent_authentication"
            && !b.device_run
            && !b.installed_kernel_source_verified
            && !b.game_proof,
        "native reader source/build/log/flags contract mismatch"
    );
    ensure!(
        [
            &b.compiler.sha256,
            &b.compiler.clang_sha256,
            &b.compiler.package_sha256
        ]
        .iter()
        .all(|s| digest(s, 64))
            && Path::new(&b.compiler.path).is_absolute()
            && Path::new(&b.compiler.clang_path).is_absolute(),
        "native compiler identity missing"
    );
    ensure!(
        elf.len() >= 64
            && &elf[..6] == b"\x7fELF\x02\x01"
            && u16::from_le_bytes([elf[16], elf[17]]) == 3
            && u16::from_le_bytes([elf[18], elf[19]]) == 183,
        "native reader requires ELF64LE AArch64 PIE"
    );
    Ok(())
}
pub(super) fn artifact(path: &Path) -> Result<BTreeMap<String, Vec<u8>>> {
    let d = Dir::open(path)?;
    ensure!(
        inventory(&d)? == ARTIFACT.into_iter().map(String::from).collect(),
        "native artifact inventory mismatch"
    );
    let files = ARTIFACT
        .into_iter()
        .map(|n| {
            Ok((
                format!("native-reader/{n}"),
                d.read(
                    n,
                    if n == "reader.elf" {
                        1024 * 1024
                    } else {
                        128 * 1024
                    },
                )?,
            ))
        })
        .collect::<Result<_>>()?;
    validate_artifact(&files)?;
    Ok(files)
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Thread {
    tid: u32,
    start_ticks: u64,
    comm: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Scope {
    profile_id: String,
    package: String,
    boot_id: String,
    pid: u32,
    process_start_ticks: u64,
    uid: u32,
    package_metadata_sha256: String,
    kernel_version_sha256: String,
    render_thread: Thread,
    threads: BTreeMap<u32, Thread>,
    device: String,
    event_formats_sha256: BTreeMap<String, String>,
    fork_parent_key: String,
    clock_capabilities_sha256: String,
    cpus: BTreeSet<u32>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    trace_sha256: String,
    before_sha256: String,
    after_sha256: String,
    final_sha256: String,
    submits_per_group: usize,
    active_contexts: BTreeSet<u32>,
    caller_tids: BTreeSet<u32>,
    executor_counts: BTreeMap<u32, usize>,
    selected_packets: usize,
    boundary_submits: usize,
    excluded_queued_classes: usize,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Render {
    schema: String,
    scope: Scope,
    submits_per_group: usize,
    active_contexts: BTreeSet<u32>,
    caller_tids: BTreeSet<u32>,
    baselines: Vec<Record>,
}
fn fixed_ref(dir: &Path, value: &Value, file: &str, cap: usize) -> Result<Vec<u8>> {
    ensure!(
        value.as_object().is_some_and(|o| o.len() == 2) && value["path"] == file,
        "receiver reference is not fixed {file}"
    );
    let bytes = read_path(&dir.join(file), cap)?;
    ensure!(
        value["sha256"] == sha(&bytes),
        "receiver reference bytes changed: {file}"
    );
    Ok(bytes)
}
fn json_file(dir: &Path, n: &str) -> Result<Value> {
    Ok(serde_json::from_slice(&read_path(&dir.join(n), JSON_CAP)?)?)
}
fn record_valid(r: &Record, f: &Render) -> bool {
    r.submits_per_group == f.submits_per_group
        && r.active_contexts == f.active_contexts
        && r.caller_tids == f.caller_tids
        && r.selected_packets > 0
        && r.selected_packets <= 250000
        && r.boundary_submits <= 250000
        && r.excluded_queued_classes <= 250000
        && r.executor_counts.len() <= 250000
        && r.executor_counts
            .keys()
            .all(|n| *n > 0 && *n <= i32::MAX as u32)
        && r.executor_counts
            .values()
            .try_fold(0usize, |a, n| a.checked_add(*n))
            == Some(r.selected_packets)
        && [
            &r.trace_sha256,
            &r.before_sha256,
            &r.after_sha256,
            &r.final_sha256,
        ]
        .iter()
        .all(|s| digest(s, 64))
}
fn receiver_render(root: &Path, p: &Profile) -> Result<Render> {
    let dir = root.join("receiver");
    let conditions = json_file(&dir, "conditions.json")?;
    ensure!(
        conditions["schema"] == "phonefarm/confirmation_conditions@3"
            && conditions["render_comm"] == p.render_comm
            && conditions["policy"]["version"] == 2
            && conditions["policy"]["temperature_profile_id"] == "loaded_steady_v1",
        "v2 receiver conditions/profile mismatch; no legacy fallback"
    );
    let reg = &conditions["render_attribution"];
    ensure!(
        reg.as_object().is_some_and(|o| o.len() == 3)
            && reg["profile_id"] == PROFILE
            && reg["package"] == p.package,
        "receiver target package/queued registration mismatch"
    );
    let initial = fixed_ref(
        &dir,
        &reg["initial_witness"],
        "render-initial.raw.tsv",
        JSON_CAP,
    )?;
    ensure!(
        initial.starts_with(b"#phonefarm_render_witness v2\n") && initial.ends_with(b"#end\n"),
        "complete initial carrier witness required"
    );
    let admission = json_file(&dir, "admission.json")?;
    ensure!(
        admission["schema"] == "phonefarm/confirmation_baseline_admission@2"
            && admission["runs"].as_array().is_some_and(|a| a.len() == 3),
        "v2 requires exactly3 attributed admission runs"
    );
    fixed_ref(&dir, &admission["conditions"], "conditions.json", JSON_CAP)?;
    fixed_ref(
        &dir,
        &admission["baseline_artifact"],
        "baseline.json",
        JSON_CAP,
    )?;
    let raw = fixed_ref(
        &dir,
        &admission["render_contract"],
        "render-contract.json",
        JSON_CAP,
    )?;
    let f: Render = serde_json::from_slice(&raw)?;
    let s = &f.scope;
    let events = [
        "adreno_cmdbatch_queued",
        "adreno_cmdbatch_submitted",
        "kgsl_context_create",
        "kgsl_context_detach",
        "kgsl_context_destroy",
        "sched_process_fork",
        "sched_process_exit",
    ]
    .into_iter()
    .map(String::from)
    .collect::<BTreeSet<_>>();
    ensure!(
        f.schema == "phonefarm/frozen_render_attribution@1"
            && s.profile_id == PROFILE
            && s.package == p.package
            && s.boot_id == conditions["device_boot_id"]
            && s.pid > 0
            && s.process_start_ticks > 0
            && s.render_thread.comm == p.render_comm
            && s.threads.get(&s.render_thread.tid) == Some(&s.render_thread)
            && !s.threads.is_empty()
            && s.threads.len() <= 256
            && s.threads
                .iter()
                .all(|(n, t)| *n == t.tid && *n > 0 && t.start_ticks > 0 && t.comm.len() <= 15)
            && !s.cpus.is_empty()
            && s.cpus.len() <= 256
            && safe(&s.device)
            && matches!(s.fork_parent_key.as_str(), "pid" | "parent_pid")
            && s.event_formats_sha256
                .keys()
                .cloned()
                .collect::<BTreeSet<_>>()
                == events
            && s.event_formats_sha256.values().all(|v| digest(v, 64))
            && [
                &s.package_metadata_sha256,
                &s.kernel_version_sha256,
                &s.clock_capabilities_sha256
            ]
            .iter()
            .all(|v| digest(v, 64))
            && (1..=6).contains(&f.submits_per_group)
            && !f.active_contexts.is_empty()
            && f.active_contexts.len() <= 256
            && f.active_contexts.iter().all(|n| *n > 0)
            && !f.caller_tids.is_empty()
            && f.caller_tids.iter().all(|n| s.threads.contains_key(n))
            && f.baselines.len() == 3
            && f.baselines.iter().all(|r| record_valid(r, &f)),
        "frozen render scope/grouping/epochs unsupported"
    );
    let plan = json_file(&dir, "plan.json")?;
    let mut seen = BTreeSet::new();
    for r in &f.baselines {
        for hash in [
            &r.trace_sha256,
            &r.before_sha256,
            &r.after_sha256,
            &r.final_sha256,
        ] {
            ensure!(
                seen.insert(hash)
                    && plan["exploration_artifact_sha256"]
                        .as_array()
                        .is_some_and(|a| a.contains(&json!(hash))),
                "baseline origin raw independence/exclusion missing"
            );
        }
    }
    let contract: Value = serde_json::from_slice(&fixed_ref(
        &dir,
        &conditions["thermal_contract"],
        "thermal-evidence.json",
        JSON_CAP,
    )?)?;
    ensure!(contract.is_object(), "thermal evidence missing");
    let thermal = &plan["temperature_contract"];
    let t: crate::confirmation::TemperatureContract = serde_json::from_value(thermal.clone())?;
    ensure!(
        t.profile_id == "loaded_steady_v1"
            && t.receiver_policy_version == 2
            && digest(&t.profile_sha256, 64)
            && digest(&t.reference_sha256, 64)
            && t.profile_sha256 == conditions["policy"]["temperature_profile_sha256"]
            && t.reference_sha256 == conditions["policy"]["thermal_reference_sha256"]
            && t.thermal_contract_sha256 == conditions["thermal_contract"]["sha256"]
            && conditions["policy"]["temp_cap_c"].is_null()
            && conditions["policy"]["cool_c"].is_null(),
        "receiver frozen thermal identities mismatch"
    );
    let sampling: Value =
        serde_json::from_slice(&read_path(&dir.join("sampling-contract.json"), JSON_CAP)?)?;
    ensure!(
        conditions["sampling_contract_sha256"]
            == sha(&read_path(&dir.join("sampling-contract.json"), JSON_CAP)?)
            && sampling["render_attribution_profile_id"] == PROFILE
            && sampling["render_reader_build_sha256"]
                == sha(&read_path(
                    &root.join("native-reader/build.json"),
                    128 * 1024
                )?)
            && sampling["render_reader_elf_sha256"]
                == sha(&read_path(
                    &root.join("native-reader/reader.elf"),
                    1024 * 1024
                )?),
        "receiver native sampling bytes differ from planned artifact"
    );
    for (caller, receiver) in ARTIFACT.into_iter().zip(RECEIVER_NATIVE) {
        let cap = if caller == "reader.elf" {
            1024 * 1024
        } else {
            128 * 1024
        };
        ensure!(
            read_path(&root.join("native-reader").join(caller), cap)?
                == read_path(&dir.join(receiver), cap)?,
            "receiver native snapshot differs: {receiver}"
        );
    }
    Ok(f)
}
pub(super) fn receiver_plan(root: &Path, p: &Profile) -> Result<()> {
    receiver_render(root, p)?;
    Ok(())
}
pub(super) fn receiver_samples(root: &Path, p: &Profile) -> Result<()> {
    let f = receiver_render(root, p)?;
    let dir = root.join("receiver");
    let samples = json_file(&dir, "samples.json")?;
    let frozen = json_file(root, "frozen.json")?;
    let current_plan = json_file(&dir, "plan.json")?;
    ensure!(
        frozen["plan"] == current_plan
            && samples["conditions_sha256"] == frozen["plan"]["conditions_sha256"]
            && samples["candidate_artifact_sha256"] == frozen["plan"]["candidate_artifact_sha256"],
        "receiver changed the registered plan before heldout consumption"
    );
    let pairs = samples["pairs"].as_array().context("missing pair array")?;
    ensure!(
        pairs.len() == p.pairs && samples["frozen_plan_sha256"] == frozen["sha256"],
        "v2 exact frozen pairs required"
    );
    let plan = json_file(&dir, "plan.json")?;
    let mut raw_seen = plan["exploration_artifact_sha256"]
        .as_array()
        .context("missing exploration")?
        .iter()
        .filter_map(Value::as_str)
        .map(String::from)
        .collect::<BTreeSet<_>>();
    let render_hash = sha(&read_path(&dir.join("render-contract.json"), JSON_CAP)?);
    for (i, pair) in pairs.iter().enumerate() {
        ensure!(
            pair["pair_id"] == format!("{}_p{i:02}", p.case_id),
            "v2 pair order changed"
        );
        for role in ["a", "b"] {
            let run = format!("{}_{role}{i:02}", p.case_id);
            let report = format!("{run}-report.json");
            ensure!(
                pair[format!("{role}_run_id")] == run
                    && (pair[format!("{role}_report")] == report
                        || pair[format!("{role}_report")]
                            == dir.join(&report).to_str().context("UTF8 report")?),
                "v2 report/run path not frozen"
            );
            let bytes = read_path(&dir.join(&report), JSON_CAP)?;
            ensure!(
                pair[format!("{role}_report_sha256")] == sha(&bytes),
                "v2 report bytes changed"
            );
            let v: Value = serde_json::from_slice(&bytes)?;
            let binding = &v["confirmation_binding"];
            ensure!(
                v["schema"] == "phonefarm/confirmation_observation@2"
                    && v["status"] == "VALID_OBSERVATION"
                    && v["source"] == "phonefarm"
                    && v["qualification"]["stage"] == "experiment"
                    && v["qualification"]["eligible"] == false
                    && matches!(
                        v["qualification"]["evidence_origin"].as_str(),
                        Some("live_device" | "recorded_device")
                    )
                    && v.get("qualification_only").is_none_or(|v| v == false)
                    && proof_false(&v)
                    && v["sample_qualification"]["gates"]["target_command_origin_verified"] == true
                    && v["sample_qualification"]["gates"]["frozen_grouping_verified"] == true
                    && binding["render_contract_sha256"] == render_hash
                    && binding["run_id"] == run
                    && binding["conditions_sha256"] == plan["conditions_sha256"]
                    && binding["frozen_plan_sha256"] == frozen["sha256"],
                "v2 queued origin/grouping qualification missing or changed"
            );
            let d = &v["target_game_attribution"];
            let scope: Scope = serde_json::from_value(d["scope"].clone())?;
            let r: Record = serde_json::from_value(d["record"].clone())?;
            ensure!(
                d["profile_id"] == PROFILE
                    && scope == f.scope
                    && record_valid(&r, &f)
                    && [
                        "display_verified",
                        "context_process_ownership_verified",
                        "installed_kernel_source_verified",
                        "continuous_foreground_verified",
                        "quality_verified"
                    ]
                    .iter()
                    .all(|k| d[*k] == false),
                "v2 scope/SPF/caller/context changed or unsupported attestation"
            );
            ensure!(d["origin_scope"] == "original target-TGID queued commands uniquely joined to measurement submitted packets; all executors; per-capture opaque context generation"
                && d.as_object().is_some_and(|o| o.len()==9), "unknown target origin diagnostic shape");
            for key in [
                "trace_sha256",
                "env_sha256",
                "thermal_admission_sha256",
                "thermal_window_sha256",
            ] {
                let h = binding["raw_artifacts"][key]
                    .as_str()
                    .context("missing independent raw binding")?;
                ensure!(
                    digest(h, 64) && raw_seen.insert(h.to_owned()),
                    "missing/reused trace/ENV/thermal raw evidence"
                );
            }
            ensure!(
                binding["raw_artifacts"]["thermal_sampler_exit_sha256"]
                    .as_str()
                    .is_some_and(|h| digest(h, 64)),
                "missing thermal sampler exit binding"
            );
            let hashes = [
                (
                    &r.before_sha256,
                    "render-before.raw.tsv",
                    "render_before_sha256",
                ),
                (
                    &r.after_sha256,
                    "render-after.raw.tsv",
                    "render_after_sha256",
                ),
                (
                    &r.final_sha256,
                    "render-final.raw.tsv",
                    "render_final_sha256",
                ),
            ];
            ensure!(
                binding["raw_artifacts"]["trace_sha256"] == r.trace_sha256,
                "origin trace binding differs"
            );
            for (hash, suffix, key) in hashes {
                let raw = read_path(&dir.join(format!("{run}-{suffix}")), JSON_CAP)?;
                ensure!(
                    binding["raw_artifacts"][key] == *hash
                        && sha(&raw) == *hash
                        && raw_seen.insert(hash.clone()),
                    "origin witness missing/changed/reused after baseline freeze"
                );
            }
        }
    }
    Ok(())
}
pub(super) fn receiver_artifacts(root: &Path) -> Result<Vec<String>> {
    let mut n = vec![
        "conditions.json",
        "admission.json",
        "render-contract.json",
        "render-initial.raw.tsv",
        "sampling-contract.json",
        "thermal-evidence.json",
    ]
    .into_iter()
    .map(|n| format!("receiver/{n}"))
    .collect::<Vec<_>>();
    n.extend(RECEIVER_NATIVE.into_iter().map(|n| format!("receiver/{n}")));
    let samples = json_file(&root.join("receiver"), "samples.json")?;
    let pairs = samples["pairs"].as_array().context("missing v2 pairs")?;
    ensure!(pairs.len() <= 32, "too many v2 pairs");
    for pair in pairs {
        for role in ["a", "b"] {
            let run = pair[format!("{role}_run_id")]
                .as_str()
                .context("missing run")?;
            ensure!(safe(run), "unsafe run");
            n.push(format!("receiver/{run}-report.json"));
            for suffix in [
                "render-before.raw.tsv",
                "render-after.raw.tsv",
                "render-final.raw.tsv",
            ] {
                n.push(format!("receiver/{run}-{suffix}"));
            }
        }
    }
    Ok(n)
}
