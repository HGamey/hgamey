//! Replayable local compiler evidence. No external process, model or device call.

use crate::engine::validator::{GateReport, Limits, Validator};
use crate::engine::{fingerprint, CandidateOrigin};
use crate::tracks::{self, TrackSpec};
use anyhow::{bail, ensure, Context, Result};
use clap::Subcommand;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::path::{Component, Path, PathBuf};

const JSON_CAP: usize = 1024 * 1024;
const SPV_CAP: usize = 4 * 1024 * 1024;
const BINARY_CAP: u64 = 512 * 1024 * 1024;
const SCHEMA: &str = "game_opt_loop/candidate_compilation@1";

#[derive(Debug, Subcommand)]
pub enum Action {
    /// Run the real static gate/Naga compiler and freeze its inputs and outputs.
    Compile {
        #[arg(long)]
        candidate: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    /// Recompile frozen bytes and check the complete local evidence chain.
    Verify {
        #[arg(long)]
        artifact: PathBuf,
        /// Identity previously frozen in the compile receipt/campaign.
        #[arg(long)]
        expected_identity: String,
    },
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CandidateInput {
    id: String,
    generation: u32,
    track: String,
    parent_id: Option<String>,
    budget_ms: f32,
    shader_source: String,
    fingerprint: String,
    origin: CandidateOrigin,
    #[serde(skip_serializing_if = "Option::is_none")]
    notes: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct FileIdentity {
    sha256: String,
    bytes: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct CompilerIdentity {
    crate_version: String,
    compiler: String,
    configured_naga_features: Vec<String>,
    claim_scope: String,
    executable_sha256: String,
    executable_bytes: u64,
    embedded_files: BTreeMap<String, FileIdentity>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Descriptor {
    schema: String,
    candidate_id: String,
    candidate_generation: u32,
    track: String,
    gate_passed: bool,
    files: BTreeMap<String, FileIdentity>,
    compiler_identity: CompilerIdentity,
    source_spirv_association: String,
    claim_scope: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema: String,
    state: String,
    complete: bool,
    frozen_identity: String,
    files: BTreeMap<String, FileIdentity>,
    proof_flags: ProofFlags,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProofFlags {
    pub gpu_validated: bool,
    pub semantic_equivalence_proven: bool,
    pub quality_verified: bool,
    pub real_game_optimization_proven: bool,
    pub deployable: bool,
    pub promotion_allowed: bool,
}

#[derive(Debug, Serialize)]
pub struct Report {
    pub schema: String,
    pub state: String,
    pub gate_passed: bool,
    pub frozen_identity: String,
    pub candidate_id: String,
    pub track: String,
    pub descriptor_sha256: String,
    pub manifest_sha256: String,
    pub adapter_input: Option<Value>,
    pub proof_flags: ProofFlags,
    pub claim_scope: String,
}

pub fn run(action: Action) -> Result<()> {
    let report = match action {
        Action::Compile { candidate, out } => compile(&candidate, &out)?,
        Action::Verify {
            artifact,
            expected_identity,
        } => verify(&artifact, &expected_identity)?,
    };
    println!("{}", serde_json::to_string_pretty(&report)?);
    ensure!(
        report.gate_passed,
        "static gate rejected candidate; no adapter input"
    );
    Ok(())
}

pub fn compile(candidate_path: &Path, out: &Path) -> Result<Report> {
    let bytes = read_path(candidate_path, JSON_CAP)?;
    let candidate = parse_candidate(&bytes)?;
    let track = track(&candidate)?;
    let limits = Limits::default();
    let gate = Validator::with_limits(limits).gate(&candidate.shader_source, track);
    let (mut files, descriptor) = materialize(&bytes, &candidate, track, limits, &gate)?;
    let descriptor_bytes = encode(&descriptor)?;
    files.insert("descriptor.json".into(), descriptor_bytes.clone());
    let manifest = Manifest {
        schema: SCHEMA.into(),
        state: if gate.passed {
            "host_compiled"
        } else {
            "gate_rejected"
        }
        .into(),
        complete: true,
        frozen_identity: hash(&descriptor_bytes),
        files: identities(&files),
        proof_flags: ProofFlags::default(),
    };
    let manifest_bytes = encode(&manifest)?;
    // No output directory exists until all bounded inputs and gate output are ready.
    let dir = Directory::create(out)?;
    for (name, bytes) in &files {
        dir.write_new(name, bytes)?;
    }
    dir.write_new("manifest.json", &manifest_bytes)?;
    dir.0.sync_all()?;
    Ok(report(
        &descriptor,
        &descriptor_bytes,
        &manifest_bytes,
        false,
    ))
}

pub fn verify(artifact: &Path, expected_identity: &str) -> Result<Report> {
    ensure!(
        expected_identity.len() == 64
            && expected_identity
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "expected identity must be lowercase SHA-256"
    );
    let dir = Directory::open(artifact)?;
    let manifest_bytes = dir.read("manifest.json", JSON_CAP)?;
    let manifest: Manifest = decode_canonical(&manifest_bytes)?;
    ensure!(
        manifest.schema == SCHEMA && manifest.complete,
        "incomplete or unknown manifest"
    );
    ensure!(
        manifest.frozen_identity == expected_identity,
        "artifact differs from previously frozen identity"
    );
    ensure!(
        manifest.proof_flags == ProofFlags::default(),
        "unsupported proof flags"
    );
    let names = dir.names()?;
    let expected_names: BTreeSet<_> = manifest
        .files
        .keys()
        .cloned()
        .chain(std::iter::once("manifest.json".into()))
        .collect();
    ensure!(
        names == expected_names,
        "missing or unexpected artifact files"
    );
    let mut snapshot = BTreeMap::new();
    for (name, identity) in &manifest.files {
        ensure!(allowed_file(name), "unknown manifest file: {name}");
        let bytes = dir.read(
            name,
            if name == "original.spv" {
                SPV_CAP
            } else {
                JSON_CAP
            },
        )?;
        ensure!(
            &identify(&bytes) == identity,
            "artifact hash/size mismatch: {name}"
        );
        snapshot.insert(name.clone(), bytes);
    }
    let descriptor_bytes = required(&snapshot, "descriptor.json")?;
    let descriptor: Descriptor = decode_canonical(descriptor_bytes)?;
    ensure!(
        manifest.frozen_identity == hash(descriptor_bytes),
        "frozen identity mismatch"
    );
    ensure!(descriptor.schema == SCHEMA, "unknown descriptor schema");
    let candidate_bytes = required(&snapshot, "candidate.json")?;
    let candidate = parse_candidate(candidate_bytes)?;
    let track = track(&candidate)?;
    let limits = Limits::default();
    // These source bytes were read once, hashed once, and are the exact executed snapshot.
    ensure!(
        required(&snapshot, "original.comp")? == candidate.shader_source.as_bytes(),
        "candidate/source association mismatch"
    );
    let gate = Validator::with_limits(limits).gate(&candidate.shader_source, track);
    let (expected_files, expected_descriptor) =
        materialize(candidate_bytes, &candidate, track, limits, &gate)?;
    ensure!(
        encode(&expected_descriptor)? == descriptor_bytes,
        "compiler/rules/gate/identity replay mismatch"
    );
    let final_report = report(&descriptor, descriptor_bytes, &manifest_bytes, true);
    let mut actual_files = snapshot;
    actual_files.remove("descriptor.json");
    ensure!(
        actual_files == expected_files,
        "gate output or exact SPIR-V replay mismatch"
    );
    ensure!(
        manifest.state
            == if gate.passed {
                "host_compiled"
            } else {
                "gate_rejected"
            },
        "manifest state disagrees with actual gate"
    );
    Ok(final_report)
}

fn materialize(
    raw_candidate: &[u8],
    candidate: &CandidateInput,
    track: &TrackSpec,
    limits: Limits,
    gate: &GateReport,
) -> Result<(BTreeMap<String, Vec<u8>>, Descriptor)> {
    let mut files = BTreeMap::from([
        ("candidate.json".into(), raw_candidate.to_vec()),
        (
            "original.comp".into(),
            candidate.shader_source.as_bytes().to_vec(),
        ),
        ("rules.json".into(), encode(&rules(track, limits))?),
        ("gate.json".into(), encode(&normalized_gate(gate))?),
    ]);
    if gate.passed {
        let words = gate
            .spirv
            .as_ref()
            .context("passed gate omitted actual SPIR-V")?;
        let bytes: Vec<u8> = words.iter().flat_map(|word| word.to_le_bytes()).collect();
        ensure!(
            !bytes.is_empty() && bytes.len() <= SPV_CAP,
            "invalid SPIR-V size"
        );
        ensure!(
            gate.spirv_bytes == Some(bytes.len()),
            "gate SPIR-V size mismatch"
        );
        files.insert("original.spv".into(), bytes);
    } else {
        ensure!(gate.spirv.is_none(), "failed gate emitted SPIR-V");
    }
    let descriptor = Descriptor {
        schema: SCHEMA.into(),
        candidate_id: candidate.id.clone(),
        candidate_generation: candidate.generation,
        track: candidate.track.clone(),
        gate_passed: gate.passed,
        files: identities(&files),
        compiler_identity: compiler_identity()?,
        source_spirv_association: "repository_gate_compiler_replayable".into(),
        claim_scope: "local_compiler_integrity_not_authenticated_attestation".into(),
    };
    Ok((files, descriptor))
}

fn report(
    descriptor: &Descriptor,
    descriptor_bytes: &[u8],
    manifest_bytes: &[u8],
    replay: bool,
) -> Report {
    let descriptor_sha = hash(descriptor_bytes);
    let manifest_sha = hash(manifest_bytes);
    let adapter_input = descriptor.gate_passed.then(|| {
        json!({
            "source_path": "original.comp", "original_spirv_path": "original.spv",
            "source_sha256": descriptor.files["original.comp"].sha256,
            "original_spirv_sha256": descriptor.files["original.spv"].sha256,
            "candidate_json_sha256": descriptor.files["candidate.json"].sha256,
            "descriptor_sha256": descriptor_sha, "manifest_sha256": manifest_sha,
            "frozen_identity": descriptor_sha,
            "source_spirv_association": "repository_gate_compiler_replayable",
            "trust_scope": "local_integrity_not_authenticated_attestation"
        })
    });
    Report {
        schema: SCHEMA.into(),
        state: match (descriptor.gate_passed, replay) {
            (true, true) => "host_compiled_replay_match",
            (true, false) => "host_compiled",
            (false, true) => "gate_rejected_replay_match",
            (false, false) => "gate_rejected",
        }
        .into(),
        gate_passed: descriptor.gate_passed,
        frozen_identity: descriptor_sha.clone(),
        candidate_id: descriptor.candidate_id.clone(),
        track: descriptor.track.clone(),
        descriptor_sha256: descriptor_sha,
        manifest_sha256: manifest_sha,
        adapter_input,
        proof_flags: ProofFlags::default(),
        claim_scope: "static_gate_and_local_compilation_only".into(),
    }
}

fn parse_candidate(bytes: &[u8]) -> Result<CandidateInput> {
    let candidate: CandidateInput =
        serde_json::from_slice(bytes).context("strict candidate JSON")?;
    let value: Value = serde_json::from_slice(bytes)?;
    ensure!(value.get("parent_id").is_some(), "missing parent_id field");
    ensure!(
        !candidate.id.trim().is_empty() && candidate.id.len() <= 256,
        "invalid candidate id"
    );
    ensure!(
        candidate
            .parent_id
            .as_ref()
            .is_none_or(|id| !id.trim().is_empty() && id.len() <= 256),
        "invalid parent id"
    );
    ensure!(
        candidate.budget_ms.is_finite() && candidate.budget_ms > 0.0,
        "invalid budget"
    );
    ensure!(
        candidate.shader_source.len() <= Limits::default().max_source_bytes,
        "source size exceeded"
    );
    ensure!(
        candidate.fingerprint == fingerprint(&candidate.shader_source),
        "dedup fingerprint mismatch"
    );
    Ok(candidate)
}

fn track(candidate: &CandidateInput) -> Result<&'static TrackSpec> {
    tracks::spec_by_id(&candidate.track).context("unknown track")
}

fn normalized_gate(gate: &GateReport) -> Value {
    json!({"schema": "game_opt_loop/static_gate@1", "passed": gate.passed,
        "violations": gate.violations, "features": gate.features, "spirv_bytes": gate.spirv_bytes})
}

fn rules(track: &TrackSpec, limits: Limits) -> Value {
    json!({"schema": "game_opt_loop/compilation_rules@1",
        "limits": {"max_workgroup_invocations": limits.max_workgroup_invocations,
            "max_workgroup_bytes": limits.max_workgroup_bytes, "max_loop_trip": limits.max_loop_trip,
            "max_source_bytes": limits.max_source_bytes},
        "track": {"id": track.id, "display": track.display, "budget_ms": track.budget_ms,
            "quality_baseline_db": track.quality_baseline_db, "power_baseline_watt": track.power_baseline_watt,
            "mutation_slot": track.mutation_slot, "objective": track.objective, "hints": track.hints,
            "bindings": track.bindings.iter().map(|b| json!({"binding":b.binding,"name":b.name,
                "access":b.access,"role":b.role,"required":b.required,"must_read":b.must_read})).collect::<Vec<_>>()},
        "compiler_policy": {"frontend":"naga_glsl", "validation_flags":"all",
            "capabilities":"all_static_not_device_caps", "spv_options":"naga_default",
            "word_encoding":"little_endian_u32"}})
}

fn compiler_identity() -> Result<CompilerIdentity> {
    let executable = std::env::current_exe().context("current executable")?;
    let (executable_sha256, executable_bytes) = hash_path(&executable, BINARY_CAP)?;
    let embedded: [(&str, &[u8]); 7] = [
        ("Cargo.toml", include_bytes!("../Cargo.toml")),
        ("Cargo.lock", include_bytes!("../Cargo.lock")),
        (
            "src/engine/validator.rs",
            include_bytes!("engine/validator.rs"),
        ),
        (
            "src/engine/analysis.rs",
            include_bytes!("engine/analysis.rs"),
        ),
        ("src/engine/mod.rs", include_bytes!("engine/mod.rs")),
        ("src/tracks/mod.rs", include_bytes!("tracks/mod.rs")),
        (
            "src/candidate_artifact.rs",
            include_bytes!("candidate_artifact.rs"),
        ),
    ];
    Ok(CompilerIdentity {
        crate_version: crate::VERSION.into(),
        compiler: "repository_validator_naga_glsl_to_spirv".into(),
        configured_naga_features: vec!["glsl-in".into(), "wgsl-in".into(), "spv-out".into()],
        claim_scope: "on_disk_running_executable_and_embedded_sources_not_toolchain_authentication"
            .into(),
        executable_sha256,
        executable_bytes,
        embedded_files: embedded
            .into_iter()
            .map(|(name, bytes)| (name.into(), identify(bytes)))
            .collect(),
    })
}

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn identify(bytes: &[u8]) -> FileIdentity {
    FileIdentity {
        sha256: hash(bytes),
        bytes: bytes.len(),
    }
}
fn identities(files: &BTreeMap<String, Vec<u8>>) -> BTreeMap<String, FileIdentity> {
    files
        .iter()
        .map(|(name, bytes)| (name.clone(), identify(bytes)))
        .collect()
}
fn encode(value: &impl Serialize) -> Result<Vec<u8>> {
    // Canonical map order is independent of Rust struct declaration order.
    Ok(serde_json::to_vec_pretty(&serde_json::to_value(value)?)?)
}
fn decode_canonical<T: for<'de> Deserialize<'de> + Serialize>(bytes: &[u8]) -> Result<T> {
    let value: T = serde_json::from_slice(bytes)?;
    ensure!(
        encode(&value)? == bytes,
        "noncanonical or incomplete record"
    );
    Ok(value)
}
fn required<'a>(files: &'a BTreeMap<String, Vec<u8>>, name: &str) -> Result<&'a [u8]> {
    files
        .get(name)
        .map(Vec::as_slice)
        .with_context(|| format!("missing {name}"))
}
fn allowed_file(name: &str) -> bool {
    matches!(
        name,
        "candidate.json"
            | "original.comp"
            | "original.spv"
            | "rules.json"
            | "gate.json"
            | "descriptor.json"
    )
}

// Directory-descriptor traversal also rejects symlink ancestors. File contents are
// bounded snapshots; verification never reopens a source for gate execution.
pub(crate) struct Directory(pub(crate) File);
impl Directory {
    pub(crate) fn open(path: &Path) -> Result<Self> {
        let absolute = if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir()?.join(path)
        };
        let root = CString::new("/")?;
        let mut dir = Self(open_at(
            libc::AT_FDCWD,
            &root,
            libc::O_RDONLY | libc::O_DIRECTORY,
            0,
        )?);
        for component in absolute.components() {
            match component {
                Component::RootDir | Component::CurDir => {}
                Component::Normal(name) => {
                    let name = c_name(name.as_encoded_bytes())?;
                    dir = Self(open_at(
                        dir.0.as_raw_fd(),
                        &name,
                        libc::O_RDONLY | libc::O_DIRECTORY,
                        0,
                    )?);
                }
                _ => bail!("parent traversal or unsupported path component"),
            }
        }
        Ok(dir)
    }
    pub(crate) fn create(path: &Path) -> Result<Self> {
        let parent = Self::open(
            path.parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or(Path::new(".")),
        )?;
        let name = path
            .file_name()
            .context("output directory needs a filename")?;
        let name = c_name(name.as_encoded_bytes())?;
        // SAFETY: validated NUL-terminated name and live directory descriptor.
        if unsafe { libc::mkdirat(parent.0.as_raw_fd(), name.as_ptr(), 0o700) } != 0 {
            return Err(std::io::Error::last_os_error()).context("output must be a new directory");
        }
        let dir = Self(open_at(
            parent.0.as_raw_fd(),
            &name,
            libc::O_RDONLY | libc::O_DIRECTORY,
            0,
        )?);
        parent.0.sync_all()?;
        Ok(dir)
    }
    pub(crate) fn file(&self, name: &str, flags: i32) -> Result<File> {
        let name = c_name(name.as_bytes())?;
        let file = open_at(self.0.as_raw_fd(), &name, flags | libc::O_NONBLOCK, 0o600)?;
        ensure!(file.metadata()?.is_file(), "only regular files allowed");
        Ok(file)
    }
    pub(crate) fn read(&self, name: &str, cap: usize) -> Result<Vec<u8>> {
        let file = self.file(name, libc::O_RDONLY)?;
        ensure!(
            file.metadata()?.len() <= cap as u64,
            "file exceeds size cap: {name}"
        );
        let mut bytes = Vec::new();
        file.take(cap as u64 + 1).read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= cap, "file exceeds size cap: {name}");
        Ok(bytes)
    }
    pub(crate) fn write_new(&self, name: &str, bytes: &[u8]) -> Result<()> {
        let mut file = self.file(name, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        Ok(())
    }
    pub(crate) fn names(&self) -> Result<BTreeSet<String>> {
        // SAFETY: dup creates an owned fd for fdopendir; closedir releases it.
        let fd = unsafe { libc::dup(self.0.as_raw_fd()) };
        ensure!(fd >= 0, "cannot duplicate directory fd");
        let stream = unsafe { libc::fdopendir(fd) };
        if stream.is_null() {
            unsafe {
                libc::close(fd);
            }
            bail!("cannot list artifact directory");
        }
        let mut names = BTreeSet::new();
        let result = (|| {
            // dup shares the directory offset: every inventory must restart.
            unsafe { libc::rewinddir(stream) };
            loop {
                #[cfg(target_os = "macos")]
                let errno = unsafe { libc::__error() };
                #[cfg(not(target_os = "macos"))]
                let errno = unsafe { libc::__errno_location() };
                unsafe { *errno = 0 };
                let entry = unsafe { libc::readdir(stream) };
                if entry.is_null() {
                    ensure!(unsafe { *errno } == 0, "directory inventory failed: {}", std::io::Error::last_os_error());
                    break;
                }
                let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_str()?;
                if name != "." && name != ".." {
                    ensure!(names.len() < 7, "too many artifact entries");
                    names.insert(name.to_owned());
                }
            }
            Ok(names)
        })();
        unsafe {
            libc::closedir(stream);
        }
        result
    }
}
fn c_name(bytes: &[u8]) -> Result<CString> {
    ensure!(
        !bytes.is_empty() && bytes != b"." && bytes != b".." && !bytes.contains(&b'/'),
        "invalid flat filename"
    );
    Ok(CString::new(bytes)?)
}
fn open_at(fd: i32, name: &CStr, flags: i32, mode: libc::mode_t) -> Result<File> {
    // SAFETY: CStr is terminated and fd is either a live directory or AT_FDCWD.
    let raw = unsafe {
        libc::openat(
            fd,
            name.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            mode as libc::c_uint,
        )
    };
    if raw < 0 {
        return Err(std::io::Error::last_os_error()).context("openat refused path");
    }
    Ok(unsafe { File::from_raw_fd(raw) })
}
fn parent_and_name(path: &Path) -> Result<(Directory, String)> {
    let dir = Directory::open(
        path.parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new(".")),
    )?;
    let name = path
        .file_name()
        .context("file name missing")?
        .to_str()
        .context("non-UTF8 filename")?
        .to_owned();
    Ok((dir, name))
}
pub(crate) fn read_path(path: &Path, cap: usize) -> Result<Vec<u8>> {
    let (dir, name) = parent_and_name(path)?;
    dir.read(&name, cap)
}
fn hash_path(path: &Path, cap: u64) -> Result<(String, u64)> {
    let (dir, name) = parent_and_name(path)?;
    let mut file = dir.file(&name, libc::O_RDONLY)?;
    let expected_size = file.metadata()?.len();
    ensure!(expected_size <= cap, "executable exceeds size cap");
    let mut digest = Sha256::new();
    let mut total = 0;
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        total += read as u64;
        ensure!(total <= cap, "executable exceeds size cap");
        digest.update(&buffer[..read]);
    }
    ensure!(
        total == expected_size && file.metadata()?.len() == expected_size,
        "executable changed during snapshot"
    );
    Ok((format!("{:x}", digest.finalize()), total))
}
