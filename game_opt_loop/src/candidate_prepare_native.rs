//! Native host preparation integrity, not Android code-generation/GPU evidence.
use super::*;
use std::fs;

pub(super) const LAYER_INPUTS: [&str; 4] = [
    "vk_layer_refknobs.cpp",
    "artifact_stats.h",
    "copyprobe_copy.comp",
    "VkLayer_refknobs.json",
];
const TREE_ENTRIES: usize = 4096;
const TREE_BYTES: u64 = 512 * 1024 * 1024;
const LAYER: &str = "libVkLayer_refknobs.so";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct Entry {
    path: String,
    directory: bool,
    identity: Option<Identity>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Plan {
    pub state: String,
    pub reason: Option<String>,
    pub sdk_path: Option<PathBuf>,
    pub inventory: Vec<Entry>,
    pub inventory_sha256: String,
    pub layer_root: Option<PathBuf>,
    pub layer_inputs: BTreeMap<String, Identity>,
    pub layer_contract: String,
}
fn names(path: &Path, limit: usize) -> Result<Vec<String>> {
    Directory::open(path)?;
    let mut names = Vec::new();
    for entry in fs::read_dir(path)? {
        ensure!(names.len() < limit, "directory entry cap exceeded");
        names.push(
            entry?
                .file_name()
                .into_string()
                .map_err(|_| anyhow::anyhow!("non-UTF8 SDK entry"))?,
        );
    }
    names.sort();
    Ok(names)
}
pub(super) fn budget(start: Instant, ms: u64) -> Result<()> {
    ensure!(
        start.elapsed() < Duration::from_millis(ms),
        "whole preparation budget exhausted during snapshot"
    );
    Ok(())
}
fn tick(timer: Option<(Instant, u64)>) -> Result<()> {
    if let Some((start, ms)) = timer {
        budget(start, ms)?;
    }
    Ok(())
}
fn stream_timed(
    file: &mut File,
    mut output: Option<&mut File>,
    cap: u64,
    timer: Option<(Instant, u64)>,
) -> Result<Identity> {
    tick(timer)?;
    let size = file.metadata()?.len();
    ensure!(size <= cap, "file exceeds size cap");
    let mut hash = Sha256::new();
    let mut total = 0u64;
    let mut buf = [0; 65536];
    loop {
        tick(timer)?;
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        total = total.checked_add(n as u64).context("file byte overflow")?;
        ensure!(total <= cap, "file grew beyond size cap");
        hash.update(&buf[..n]);
        if let Some(dst) = output.as_deref_mut() {
            dst.write_all(&buf[..n])?;
        }
    }
    ensure!(
        total == size && file.metadata()?.len() == size,
        "file changed during snapshot"
    );
    tick(timer)?;
    Ok(Identity {
        sha256: format!("{:x}", hash.finalize()),
        bytes: total,
    })
}
fn contract_names(contract: &str) -> Result<BTreeSet<String>> {
    let mut expected: BTreeSet<_> = LAYER_INPUTS.iter().map(|s| s.to_string()).collect();
    match contract {
        "legacy4" => {}
        "ownership5" | "npuprobe6" => {
            expected.insert("dispatch_ownership.h".into());
            if contract == "npuprobe6" {
                expected.insert("npuprobe.cpp".into());
            }
        }
        _ => anyhow::bail!("unsupported layer input contract"),
    }
    Ok(expected)
}
fn source_contract(layer: &Directory) -> Result<String> {
    let names = layer.names_with_limit(9)?;
    let contract = if names.contains("npuprobe.cpp") {
        "npuprobe6"
    } else if names.contains("dispatch_ownership.h") {
        "ownership5"
    } else {
        "legacy4"
    };
    let mut allowed = contract_names(contract)?;
    // These standalone shader sources are not consumed by the dynamic native build.
    for name in ["upop.comp", "npu_pack.comp", "npu_unpack.comp"] {
        if names.contains(name) {
            allowed.insert(name.into());
        }
    }
    ensure!(names == allowed, "unknown/missing layer source entries");
    Ok(contract.into())
}
fn add_bytes(total: &mut u64, bytes: u64) -> Result<()> {
    *total = total
        .checked_add(bytes)
        .filter(|n| *n <= TREE_BYTES)
        .context("SDK actual byte cap exceeded")?;
    Ok(())
}
fn tree(
    src: &Path,
    dst: Option<&Path>,
    prefix: &str,
    depth: usize,
    entries: &mut Vec<Entry>,
    total: &mut u64,
    timer: Option<(Instant, u64)>,
) -> Result<()> {
    ensure!(depth <= 16, "SDK depth cap exceeded");
    if let Some((start, ms)) = timer {
        budget(start, ms)?;
    }
    let source = Directory::open(src)?;
    let target = dst.map(Directory::create).transpose()?;
    for name in names(src, TREE_ENTRIES)? {
        if let Some((start, ms)) = timer {
            budget(start, ms)?;
        }
        ensure!(entries.len() < TREE_ENTRIES, "SDK total entry cap exceeded");
        let path = if prefix.is_empty() {
            name.clone()
        } else {
            format!("{prefix}/{name}")
        };
        let metadata = fs::symlink_metadata(src.join(&name))?;
        ensure!(
            !metadata.file_type().is_symlink(),
            "SDK symlink layout unsupported: {path}"
        );
        if metadata.is_dir() {
            entries.push(Entry {
                path: path.clone(),
                directory: true,
                identity: None,
            });
            tree(
                &src.join(&name),
                dst.map(|p| p.join(&name)).as_deref(),
                &path,
                depth + 1,
                entries,
                total,
                timer,
            )?;
        } else {
            ensure!(metadata.is_file(), "SDK special entry unsupported: {path}");
            let mut input = source.file(&name, libc::O_RDONLY)?;
            let size = input.metadata()?.len();
            ensure!(
                size <= TREE_BYTES && total.checked_add(size).is_some_and(|n| n <= TREE_BYTES),
                "SDK byte cap exceeded"
            );
            let id = if let Some(target) = &target {
                let mut output =
                    target.file(&name, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
                let id = stream_timed(&mut input, Some(&mut output), TREE_BYTES, timer)?;
                output.sync_all()?;
                ensure!(
                    unsafe { libc::fchmod(output.as_raw_fd(), 0o500) } == 0,
                    "cannot freeze SDK file"
                );
                id
            } else {
                stream_timed(&mut input, None, TREE_BYTES, timer)?
            };
            add_bytes(total, id.bytes)?;
            entries.push(Entry {
                path,
                directory: false,
                identity: Some(id),
            });
        }
    }
    if let Some(target) = target {
        target.0.sync_all()?;
        ensure!(
            unsafe { libc::fchmod(target.0.as_raw_fd(), 0o500) } == 0,
            "cannot freeze SDK directory"
        );
    }
    if let Some((start, ms)) = timer {
        budget(start, ms)?;
    }
    Ok(())
}
pub(super) fn snapshot(
    ndk: &Path,
    layer: Option<&Path>,
    session: &Path,
    start: Instant,
    ms: u64,
) -> Plan {
    let base = Plan {
        state: "BLOCKED".into(),
        reason: None,
        sdk_path: None,
        inventory: vec![],
        inventory_sha256: sha(b"[]"),
        layer_root: layer.map(Path::to_owned),
        layer_inputs: BTreeMap::new(),
        layer_contract: "not_selected".into(),
    };
    if fs::symlink_metadata(ndk).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound) {
        return Plan {
            state: "MISSING".into(),
            reason: Some("MISSING_ANDROID_NDK".into()),
            ..base
        };
    }
    let result = (|| {
        budget(start, ms)?;
        let layer = layer.context(
            "present SDK requires --native-layer-input-root; PF source root is not inferred",
        )?;
        let layer_dir = Directory::open(layer)?;
        let layer_contract = source_contract(&layer_dir)?;
        let mut layer_inputs = BTreeMap::new();
        let frozen = Directory::create(&session.join("native-layer-inputs"))?;
        for name in contract_names(&layer_contract)? {
            let bytes = layer_dir.read(&name, 4 * 1024 * 1024)?;
            frozen.write_new(&name, &bytes)?;
            layer_inputs.insert(name, identity(&bytes));
            budget(start, ms)?;
        }
        frozen.0.sync_all()?;
        let sdk = session.join("ndk.snapshot");
        let mut inventory = Vec::new();
        let mut total = 0;
        tree(
            ndk,
            Some(&sdk),
            "",
            0,
            &mut inventory,
            &mut total,
            Some((start, ms)),
        )?;
        let inventory_bytes = encode(&inventory)?;
        ensure!(
            inventory_bytes.len() <= META_CAP,
            "SDK metadata cap exceeded"
        );
        Ok(Plan {
            state: "READY".into(),
            reason: None,
            sdk_path: Some(sdk),
            inventory_sha256: sha(&inventory_bytes),
            inventory,
            layer_root: Some(layer.to_owned()),
            layer_inputs,
            layer_contract,
        })
    })();
    result.unwrap_or_else(|e: anyhow::Error| Plan {
        reason: Some(format!("BLOCKED_NATIVE_INPUTS: {e:#}")),
        ..base
    })
}
pub(super) fn check(
    plan: &Plan,
    session: &Path,
    original_layer: bool,
    timer: Option<(Instant, u64)>,
) -> Result<()> {
    tick(timer)?;
    ensure!(
        plan.state == "READY" && plan.reason.is_none(),
        "native plan not ready"
    );
    let sdk = session.join("ndk.snapshot");
    ensure!(
        plan.sdk_path.as_ref() == Some(&sdk),
        "SDK snapshot path outside session"
    );
    let mut entries = Vec::new();
    let mut total = 0;
    tree(&sdk, None, "", 0, &mut entries, &mut total, timer)?;
    ensure!(
        entries == plan.inventory && sha(&encode(&entries)?) == plan.inventory_sha256,
        "SDK snapshot inventory/bytes changed"
    );
    let expected = contract_names(&plan.layer_contract)?;
    let frozen = Directory::open(&session.join("native-layer-inputs"))?;
    ensure!(
        frozen.names()? == expected
            && plan.layer_inputs.keys().cloned().collect::<BTreeSet<_>>() == expected,
        "unexpected native source input set"
    );
    for (name, id) in &plan.layer_inputs {
        tick(timer)?;
        ensure!(
            identity(&frozen.read(name, 4 * 1024 * 1024)?) == *id,
            "frozen layer source changed"
        );
        if original_layer {
            let original =
                Directory::open(plan.layer_root.as_deref().context("layer root missing")?)?;
            ensure!(
                source_contract(&original)? == plan.layer_contract,
                "original layer contract changed"
            );
            ensure!(
                identity(&original.read(name, 4 * 1024 * 1024)?) == *id,
                "declared original layer source changed before/during build"
            );
        }
    }
    tick(timer)?;
    Ok(())
}
fn provisional(v: &Value) -> Result<()> {
    ensure!(
        v["process_supervision"] == "inherited_group"
            && v["requires_outer_quiescence"] == true
            && v["outer_quiescence_verified"] == false
            && v["deployable"] == false
            && v["device_validation"] == "not_run",
        "PF must retain provisional inherited-group scope"
    );
    Ok(())
}
fn policy(
    descriptor: &Value,
    adaptation_descriptor: &Value,
    adaptation_id: &str,
    contract: &str,
) -> Result<()> {
    let expected_origin = json!({"adaptation_id":adaptation_id,"state":"verified_offline","deployable":false,"device_validation":"not_run","semantic_equivalence":"not_run","qualification":"not_run","source_spirv_association":"caller_supplied_unverified","descriptor":adaptation_descriptor});
    let mut flags = vec![
        "-O2",
        "-Wall",
        "-fPIC",
        "-std=c++17",
        "-fno-exceptions",
        "-fno-rtti",
        "-static-libstdc++",
        "-Wl,--exclude-libs,ALL",
        "-DRK_DYNAMIC_UPOP_BUILD=1",
    ];
    if contract == "npuprobe6" {
        flags.extend(["-I<snapshot>", "-shared", "-Wl,-z,defs", "-llog", "-ldl"]);
    } else {
        // Retain the original v1 descriptor flags for legacy4/ownership5 artifacts.
        flags.extend(["-shared", "-I<snapshot>", "-llog"]);
    }
    ensure!(
        descriptor["schema_version"] == 1
            && descriptor["target"] == "android-arm64-v8a-api30"
            && descriptor["abi"] == adaptation_descriptor["adapted_abi"]
            && descriptor["shader_flags"] == json!(["--target-env=vulkan1.0", "-O"])
            && descriptor["native_flags"] == json!(flags)
            && descriptor["adaptation"] == expected_origin,
        "native fixed target/flags/ABI/adaptation policy differs"
    );
    Ok(())
}
pub(super) fn artifact(
    session: &Path,
    plan: &Plan,
    adapted: &Path,
    build: &Value,
    verify: Option<&Value>,
    original_layer: bool,
    timer: Option<(Instant, u64)>,
) -> Result<Value> {
    check(plan, session, original_layer, timer)?;
    provisional(build)?;
    let id = build["artifact_id"]
        .as_str()
        .context("native artifact ID missing")?;
    ensure!(
        id.starts_with("upop-") && id.len() == 69 && id[5..].bytes().all(|x| x.is_ascii_hexdigit()),
        "invalid native artifact identity"
    );
    let path = session.join("native").join(id);
    ensure!(
        build["artifact"] == path_string(&path)?,
        "native artifact outside session"
    );
    let dir = Directory::open(&path)?;
    let descriptor_bytes = dir.read("descriptor.json", META_CAP)?;
    let manifest_bytes = dir.read("manifest.json", META_CAP)?;
    let descriptor: Value = serde_json::from_slice(&descriptor_bytes)?;
    let manifest: Value = serde_json::from_slice(&manifest_bytes)?;
    ensure!(
        id == format!("upop-{}", sha(&descriptor_bytes))
            && manifest["descriptor"] == descriptor
            && manifest["artifact_id"] == id
            && manifest["schema_version"] == 1
            && manifest["state"] == "built_offline_outer_quiescence_required"
            && manifest["device_validation"] == "not_run"
            && manifest["deployable"] == false
            && descriptor["process_supervision"] == "inherited_group"
            && manifest["checks"]["candidate_route"] == "adapted_spirv",
        "native descriptor/state binding invalid"
    );
    let files = manifest["files"]
        .as_object()
        .context("native manifest files missing")?;
    ensure!(
        files.len() <= 32 && files.contains_key(LAYER) && files.contains_key("descriptor.json"),
        "invalid native file set"
    );
    let mut required_inputs: BTreeSet<String> = [
        "candidate.comp",
        "candidate.spv",
        "copyprobe_copy.spv",
        "copy_spv.h",
        "original_candidate.spv",
        "adaptation_descriptor.json",
        "adaptation_manifest.json",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    required_inputs.extend(contract_names(&plan.layer_contract)?);
    let extra_files = [
        "artifact_metadata.h",
        "descriptor.json",
        LAYER,
        "candidate_compile.log",
        "copy_compile.log",
        "layer_compile.log",
        "exports.log",
        "dependencies.log",
    ];
    let required_files: BTreeSet<_> = required_inputs
        .iter()
        .cloned()
        .chain(extra_files.into_iter().map(str::to_owned))
        .collect();
    ensure!(
        files.keys().cloned().collect::<BTreeSet<_>>() == required_files,
        "native manifest must retain complete builder file set"
    );
    let mut expected: Vec<_> = files.keys().cloned().collect();
    expected.push("manifest.json".into());
    expected.sort();
    ensure!(
        names(&path, 33)? == expected,
        "untracked/missing native output"
    );
    let mut hashes = BTreeMap::new();
    for (name, hash) in files {
        ensure!(
            Path::new(name).components().count() == 1
                && matches!(
                    Path::new(name).components().next(),
                    Some(Component::Normal(_))
                ),
            "unsafe native filename"
        );
        let cap = SPV_CAP as u64;
        let mut input = dir.file(name, libc::O_RDONLY)?;
        let current = stream_timed(&mut input, None, cap, timer)?;
        ensure!(
            hash == &json!(current.sha256),
            "native file changed: {name}"
        );
        hashes.insert(name.clone(), current);
    }
    let inputs = descriptor["inputs"]
        .as_object()
        .context("native inputs missing")?;
    ensure!(
        inputs.keys().cloned().collect::<BTreeSet<_>>() == required_inputs
            && inputs
                .iter()
                .all(|(name, hash)| files.get(name) == Some(hash)),
        "native descriptor inputs differ from actual file hashes"
    );
    for (name, expected) in &plan.layer_inputs {
        ensure!(
            inputs.get(name) == Some(&json!(expected.sha256)) && hashes.get(name) == Some(expected),
            "PF compiled source differs from frozen expectation: {name}"
        );
    }
    tick(timer)?;
    let adapted_dir = Directory::open(adapted)?;
    for (stored, original) in [
        ("candidate.comp", "original.comp"),
        ("candidate.spv", "adapted.spv"),
        ("original_candidate.spv", "original.spv"),
        ("adaptation_descriptor.json", "descriptor.json"),
        ("adaptation_manifest.json", "manifest.json"),
    ] {
        let source = adapted_dir.read(original, SPV_CAP)?;
        ensure!(
            dir.read(stored, SPV_CAP)? == source
                && inputs.get(stored) == Some(&json!(sha(&source))),
            "native imported adaptation differs: {stored}"
        );
    }
    let adaptation_bytes = adapted_dir.read("descriptor.json", META_CAP)?;
    let adaptation_descriptor: Value = serde_json::from_slice(&adaptation_bytes)?;
    policy(
        &descriptor,
        &adaptation_descriptor,
        &sha(&adaptation_bytes),
        &plan.layer_contract,
    )?;
    let tools = descriptor["tools"]
        .as_object()
        .context("native tools missing")?;
    ensure!(tools.len() == 4, "native tool set differs");
    let sdk = plan.sdk_path.as_deref().context("SDK snapshot missing")?;
    let tag = if sdk.join("toolchains/llvm/prebuilt/darwin-x86_64").is_dir() {
        "darwin-x86_64"
    } else {
        "linux-x86_64"
    };
    for (name, relative) in [
        ("glslc", format!("shader-tools/{tag}/glslc")),
        (
            "clang++",
            format!("toolchains/llvm/prebuilt/{tag}/bin/aarch64-linux-android30-clang++"),
        ),
        ("nm", format!("toolchains/llvm/prebuilt/{tag}/bin/llvm-nm")),
        (
            "readelf",
            format!("toolchains/llvm/prebuilt/{tag}/bin/llvm-readelf"),
        ),
    ] {
        let entry = plan
            .inventory
            .iter()
            .find(|e| e.path == relative && !e.directory)
            .context("native tool absent from SDK inventory")?;
        let tool = tools.get(name).context("expected native tool missing")?;
        ensure!(
            tool["path"] == path_string(&sdk.join(relative))?
                && tool["sha256"]
                    == entry
                        .identity
                        .as_ref()
                        .context("tool identity missing")?
                        .sha256
                && tool["version"].as_str().is_some_and(|v| !v.is_empty()),
            "native tool differs from SDK snapshot: {name}"
        );
    }
    ensure!(
        manifest["layer_sha256"] == files[LAYER],
        "layer identity differs"
    );
    if let Some(v) = verify {
        provisional(v)?;
        ensure!(
            v["artifact_id"] == id
                && v["layer_sha256"] == files[LAYER]
                && v["state"] == manifest["state"]
                && v["files_verified"] == files.len(),
            "native verification receipt differs"
        );
    }
    tick(timer)?;
    Ok(
        json!({"layer_contract":plan.layer_contract,"artifact_id":id,"artifact":path,"descriptor":identity(&descriptor_bytes),"manifest":identity(&manifest_bytes),"files":hashes,"layer":hashes[LAYER],"sdk_inventory_sha256":plan.inventory_sha256,"layer_inputs":plan.layer_inputs,"outer_build_group_absent":true,"outer_verify_group_absent":verify.is_some(),"build_receipt":build,"pf_receipt":verify,"host_artifact_integrity_accepted":verify.is_some(),"android_codegen_validated":false,"real_ndk_validation":"not_run","device_validation":"not_run","deployable":false,"source_spirv_association":"caller_supplied_unverified","claim_scope":"local_artifact_integrity_and_process_supervision_not_toolchain_authenticity_or_GPU_correctness"}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Temp(PathBuf);
    impl Temp {
        fn new() -> Self {
            let base = if cfg!(target_os = "macos") {
                "/private/tmp"
            } else {
                "/tmp"
            };
            let p = Path::new(base).join(format!(
                "gol-sdk-snapshot-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&p).unwrap();
            for dir in ["sdk", "layer", "session"] {
                fs::create_dir(p.join(dir)).unwrap();
            }
            for name in LAYER_INPUTS {
                fs::write(p.join("layer").join(name), b"frozen expectation").unwrap();
            }
            Self(p)
        }
        fn plan(&self, start: Instant, ms: u64) -> Plan {
            snapshot(
                &self.0.join("sdk"),
                Some(&self.0.join("layer")),
                &self.0.join("session"),
                start,
                ms,
            )
        }
    }
    fn writable(path: &Path) {
        if let Ok(m) = fs::symlink_metadata(path) {
            if m.is_dir() {
                unsafe {
                    libc::chmod(
                        CString::new(path.as_os_str().as_encoded_bytes())
                            .unwrap()
                            .as_ptr(),
                        0o700,
                    )
                };
                for e in fs::read_dir(path).unwrap() {
                    writable(&e.unwrap().path());
                }
            }
        }
    }
    impl Drop for Temp {
        fn drop(&mut self) {
            writable(&self.0);
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    #[test]
    fn regular_sdk_snapshot_uses_private_bytes_and_rejects_mutation() {
        let t = Temp::new();
        fs::write(t.0.join("sdk/compiler"), b"original helper").unwrap();
        let p = t.plan(Instant::now(), 30_000);
        assert_eq!(p.state, "READY");
        fs::write(t.0.join("sdk/compiler"), b"caller changed").unwrap();
        check(&p, &t.0.join("session"), true, None).unwrap();
        let copied = t.0.join("session/ndk.snapshot/compiler");
        assert_eq!(fs::read(&copied).unwrap(), b"original helper");
        unsafe {
            libc::chmod(
                CString::new(copied.as_os_str().as_encoded_bytes())
                    .unwrap()
                    .as_ptr(),
                0o700,
            )
        };
        fs::write(copied, b"private changed").unwrap();
        assert!(check(&p, &t.0.join("session"), false, None).is_err());
    }
    #[test]
    fn sdk_symlink_sparse_oversize_and_snapshot_budget_are_explicit_blocked() {
        let t = Temp::new();
        std::os::unix::fs::symlink("../layer", t.0.join("sdk/alias")).unwrap();
        assert!(t
            .plan(Instant::now(), 30_000)
            .reason
            .unwrap()
            .contains("symlink"));
        let t = Temp::new();
        File::create(t.0.join("sdk/oversize"))
            .unwrap()
            .set_len(TREE_BYTES + 1)
            .unwrap();
        assert!(t
            .plan(Instant::now(), 30_000)
            .reason
            .unwrap()
            .contains("byte cap"));
        let t = Temp::new();
        assert!(t
            .plan(Instant::now() - Duration::from_secs(1), 50)
            .reason
            .unwrap()
            .contains("budget"));
    }
    #[test]
    fn input_contract_presence_extras_and_post_execution_deadline_are_checked() {
        for contract in ["legacy4", "ownership5", "npuprobe6"] {
            let t = Temp::new();
            for name in contract_names(contract).unwrap() {
                fs::write(t.0.join("layer").join(name), b"expected source").unwrap();
            }
            if contract == "npuprobe6" {
                for name in ["upop.comp", "npu_pack.comp", "npu_unpack.comp"] {
                    fs::write(t.0.join("layer").join(name), b"unused shader").unwrap();
                }
            }
            let p = t.plan(Instant::now(), 30_000);
            assert_eq!(p.state, "READY", "{}: {:?}", contract, p.reason);
            assert_eq!(p.layer_contract, contract);
            check(&p, &t.0.join("session"), true, None).unwrap();
            assert!(check(
                &p,
                &t.0.join("session"),
                true,
                Some((Instant::now() - Duration::from_secs(1), 50))
            )
            .is_err());
            fs::remove_file(t.0.join("layer/vk_layer_refknobs.cpp")).unwrap();
            assert!(check(&p, &t.0.join("session"), true, None).is_err());
        }
        let t = Temp::new();
        fs::write(t.0.join("layer/unknown.h"), b"not a declared input").unwrap();
        assert!(t
            .plan(Instant::now(), 30_000)
            .reason
            .unwrap()
            .contains("unknown/missing"));
        let t = Temp::new();
        fs::write(t.0.join("sdk/input"), b"bounded bytes").unwrap();
        let mut f = File::open(t.0.join("sdk/input")).unwrap();
        assert!(stream_timed(
            &mut f,
            None,
            TREE_BYTES,
            Some((Instant::now() - Duration::from_secs(1), 50))
        )
        .is_err());
        let mut total = TREE_BYTES - 1;
        assert!(add_bytes(&mut total, 2).is_err());
        assert_eq!(total, TREE_BYTES - 1);
        let mut total = u64::MAX;
        assert!(add_bytes(&mut total, 1).is_err());
    }
    #[test]
    fn known_native_policy_rejects_metadata_contradictions() {
        let adaptation = json!({"adapted_abi":{"version":1}});
        let origin = json!({"adaptation_id":"id","state":"verified_offline","deployable":false,"device_validation":"not_run","semantic_equivalence":"not_run","qualification":"not_run","source_spirv_association":"caller_supplied_unverified","descriptor":adaptation});
        let d = json!({"schema_version":1,"target":"android-arm64-v8a-api30","abi":adaptation["adapted_abi"],"shader_flags":["--target-env=vulkan1.0","-O"],"native_flags":["-O2","-Wall","-fPIC","-std=c++17","-fno-exceptions","-fno-rtti","-static-libstdc++","-Wl,--exclude-libs,ALL","-DRK_DYNAMIC_UPOP_BUILD=1","-shared","-I<snapshot>","-llog"],"adaptation":origin});
        policy(&d, &adaptation, "id", "legacy4").unwrap();
        policy(&d, &adaptation, "id", "ownership5").unwrap();
        assert!(policy(&d, &adaptation, "id", "npuprobe6").is_err());
        let mut current = d.clone();
        let flags = current["native_flags"].as_array_mut().unwrap();
        flags.swap(9, 10);
        flags.insert(11, json!("-Wl,-z,defs"));
        flags.push(json!("-ldl"));
        policy(&current, &adaptation, "id", "npuprobe6").unwrap();
        for field in [
            "schema_version",
            "target",
            "abi",
            "shader_flags",
            "native_flags",
            "adaptation",
        ] {
            let mut bad = d.clone();
            bad[field] = Value::Null;
            assert!(
                policy(&bad, &adaptation, "id", "ownership5").is_err(),
                "{field}"
            );
        }
        let mut bad = d;
        bad["adaptation"]["semantic_equivalence"] = json!("proven");
        assert!(policy(&bad, &adaptation, "id", "ownership5").is_err());
    }
}
