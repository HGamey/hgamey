//! Finite local compiler -> PF adapter preparation. Never a device/deployment driver.
use crate::candidate_artifact::{read_path, Directory, ProofFlags};
use anyhow::{ensure, Context, Result};
use clap::Subcommand;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::CString;
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SCHEMA: &str = "game_opt_loop/candidate_prepare@1";
const META_CAP: usize = 2 * 1024 * 1024;
const RECEIPT_CAP: usize = 64 * 1024;
const SPV_CAP: usize = 16 * 1024 * 1024;
const TOOL_CAP: u64 = 512 * 1024 * 1024;
const NAMES: [&str; 5] = [
    "compile",
    "verify_compile",
    "adapt",
    "verify_adaptation",
    "native_build",
];
// Fixed receiver code, never caller-provided shell. The actual argv stays separate.
const GATE: &str = r#"program=$1; permit=$2; token=$3; shift 3; n=0; while [ ! -f "$permit" ]; do [ "$n" -lt 500 ] || exit 91; n=$((n+1)); sleep .02; done; [ "$(cat "$permit")" = "$token" ] || exit 92; exec "$program" "$@""#;

#[derive(Debug, Subcommand)]
pub enum Action {
    /// Execute actual compilation/replay and explicit PF offline adaptation.
    Run {
        #[arg(long)]
        candidate: PathBuf,
        #[arg(long)]
        phonefarm_bin: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long, requires = "ndk")]
        build: bool,
        #[arg(long, requires = "build")]
        ndk: Option<PathBuf>,
        #[arg(long, default_value_t = 30_000)]
        stage_wall_ms: u64,
        #[arg(long, default_value_t = 180_000)]
        whole_wall_ms: u64,
        #[arg(long, default_value_t = 1_048_576)]
        stream_bytes: usize,
        /// Mechanism fixtures retain synthetic provenance and cannot grant qualification.
        #[arg(long)]
        synthetic: bool,
    },
    /// Inspect current integrity; never execute, resume or retry an unknown stage.
    Inspect {
        #[arg(long)]
        session: PathBuf,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Identity {
    sha256: String,
    bytes: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Inputs {
    candidate: Identity,
    gol: Identity,
    phonefarm: Identity,
    stage_wall_ms: u64,
    whole_wall_ms: u64,
    stream_bytes: usize,
    build: bool,
    ndk: Option<PathBuf>,
    provenance: String,
    implementation_sha256: String,
    gate_sha256: String,
    execution_scope: String,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
enum State {
    Pending,
    InFlight,
    Completed,
    Failed,
    Unknown,
    Blocked,
    NotRun,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Stage {
    name: String,
    state: State,
    argv: Vec<String>,
    tool_sha256: Option<String>,
    pid: Option<u32>,
    group_absent: Option<bool>,
    elapsed_ms: Option<u64>,
    exit_code: Option<i32>,
    signal: Option<i32>,
    stdout: Option<Identity>,
    stderr: Option<Identity>,
    result: Option<Value>,
    error: Option<String>,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    schema: String,
    status: String,
    frozen_inputs_sha256: String,
    inputs: Inputs,
    stages: Vec<Stage>,
    prepared_receipt: Option<Identity>,
    proof_flags: ProofFlags,
}
struct Driver {
    path: PathBuf,
    dir: Directory,
    journal: Journal,
    start: Instant,
    checkpoint: u32,
}
struct Output {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    code: Option<i32>,
    signal: Option<i32>,
    group_absent: bool,
    error: Option<String>,
}
struct OwnedChild {
    child: std::process::Child,
    armed: bool,
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if self.armed {
            stop_and_reap(&mut self.child);
        }
    }
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CompileReceipt {
    schema: String,
    state: String,
    gate_passed: bool,
    frozen_identity: String,
    candidate_id: String,
    track: String,
    descriptor_sha256: String,
    manifest_sha256: String,
    adapter_input: Option<Value>,
    proof_flags: ProofFlags,
    claim_scope: String,
}

fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn identity(bytes: &[u8]) -> Identity {
    Identity {
        sha256: sha(bytes),
        bytes: bytes.len() as u64,
    }
}
fn encode(v: &impl Serialize) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec_pretty(&serde_json::to_value(v)?)?)
}
fn absolute(path: &Path) -> Result<PathBuf> {
    ensure!(
        !path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::Prefix(_))),
        "parent traversal is unsupported"
    );
    Ok(if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    })
}
fn path_string(path: &Path) -> Result<String> {
    Ok(path.to_str().context("non-UTF8 path")?.to_owned())
}
fn file_hash(path: &Path, cap: u64) -> Result<Identity> {
    let parent = Directory::open(path.parent().context("file parent")?)?;
    let mut file = parent.file(
        path.file_name()
            .and_then(|v| v.to_str())
            .context("filename")?,
        libc::O_RDONLY,
    )?;
    stream(&mut file, None, cap)
}
fn stream(file: &mut File, mut output: Option<&mut File>, cap: u64) -> Result<Identity> {
    let size = file.metadata()?.len();
    ensure!(size <= cap, "file exceeds size cap");
    let mut hash = Sha256::new();
    let mut total = 0;
    let mut buf = [0; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        total += n as u64;
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
    Ok(Identity {
        sha256: format!("{:x}", hash.finalize()),
        bytes: total,
    })
}
fn copy_tool(path: &Path, dir: &Directory, name: &str) -> Result<Identity> {
    let parent = Directory::open(path.parent().context("tool parent")?)?;
    let mut input = parent.file(
        path.file_name()
            .and_then(|v| v.to_str())
            .context("tool filename")?,
        libc::O_RDONLY,
    )?;
    let mut output = dir.file(name, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
    let id = stream(&mut input, Some(&mut output), TOOL_CAP)?;
    ensure!(id.bytes > 0, "empty executable");
    output.sync_all()?;
    ensure!(
        unsafe { libc::fchmod(output.as_raw_fd(), 0o500) } == 0,
        "cannot freeze executable permissions"
    );
    dir.0.sync_all()?;
    Ok(id)
}
fn publish_permit(dir: &Directory, name: &str, bytes: &[u8]) -> Result<()> {
    let pending = format!(".{name}.pending");
    dir.write_new(&pending, bytes)?;
    let pending = CString::new(pending)?;
    let published = CString::new(name)?;
    // A create-only hard link publishes the fully written, fsynced bytes at once.
    // The bootstrap can never observe an empty just-created permit file.
    ensure!(
        unsafe {
            libc::linkat(
                dir.0.as_raw_fd(),
                pending.as_ptr(),
                dir.0.as_raw_fd(),
                published.as_ptr(),
                0,
            )
        } == 0,
        "cannot atomically publish new execution permit"
    );
    ensure!(
        unsafe { libc::unlinkat(dir.0.as_raw_fd(), pending.as_ptr(), 0) } == 0,
        "cannot remove permit staging link"
    );
    dir.0.sync_all()?;
    Ok(())
}

impl Driver {
    fn checkpoint(&mut self) -> Result<()> {
        self.checkpoint += 1;
        let tmp = format!(".checkpoint-{}", self.checkpoint);
        let bytes = encode(&self.journal)?;
        ensure!(bytes.len() <= META_CAP, "journal size cap");
        self.dir.write_new(&tmp, &bytes)?;
        let tmp = CString::new(tmp)?;
        let target = CString::new("journal.json")?;
        ensure!(
            unsafe {
                libc::renameat(
                    self.dir.0.as_raw_fd(),
                    tmp.as_ptr(),
                    self.dir.0.as_raw_fd(),
                    target.as_ptr(),
                )
            } == 0,
            "atomic checkpoint failed"
        );
        self.dir.0.sync_all()?;
        Ok(())
    }
    fn command(&mut self, index: usize, gol: bool, args: Vec<String>) -> Result<Output> {
        ensure!(
            self.journal.stages[index].state == State::Pending,
            "stage cannot be replayed"
        );
        let tool = if gol { "gol.bin" } else { "phonefarm.bin" };
        let expected = if gol {
            &self.journal.inputs.gol
        } else {
            &self.journal.inputs.phonefarm
        };
        ensure!(
            file_hash(&self.path.join(tool), TOOL_CAP)? == *expected,
            "frozen executable changed"
        );
        let remaining = Duration::from_millis(self.journal.inputs.whole_wall_ms)
            .checked_sub(self.start.elapsed())
            .context("whole preparation budget exhausted")?;
        let wall = remaining.min(Duration::from_millis(self.journal.inputs.stage_wall_ms));
        let stage = &mut self.journal.stages[index];
        stage.state = State::InFlight;
        stage.argv = args.clone();
        stage.tool_sha256 = Some(expected.sha256.clone());
        self.checkpoint()?;
        let permit_name = format!("{}.permit", NAMES[index]);
        let token = self.journal.frozen_inputs_sha256.clone();
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", GATE, "candidate-prepare-gate"])
            .arg(self.path.join(tool))
            .arg(self.path.join(&permit_name))
            .arg(&token)
            .args(args)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .current_dir(&self.path)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        let begin = Instant::now();
        let mut owned = OwnedChild {
            child: command.spawn().context("cannot start controlled stage")?,
            armed: true,
        };
        self.journal.stages[index].pid = Some(owned.child.id());
        self.checkpoint()?;
        // Permission to execute follows the durable owned PID/group record.
        publish_permit(&self.dir, &permit_name, format!("{token}\n").as_bytes())?;
        let wall = wall.saturating_sub(begin.elapsed());
        let out = bounded_child(&mut owned.child, wall, self.journal.inputs.stream_bytes);
        owned.armed = !out.group_absent;
        let elapsed = begin.elapsed().as_millis().min(u64::MAX as u128) as u64;
        let stage = &mut self.journal.stages[index];
        stage.elapsed_ms = Some(elapsed);
        stage.exit_code = out.code;
        stage.signal = out.signal;
        stage.group_absent = Some(out.group_absent);
        self.dir
            .write_new(&format!("{}.stdout.log", NAMES[index]), &out.stdout)?;
        self.dir
            .write_new(&format!("{}.stderr.log", NAMES[index]), &out.stderr)?;
        stage.stdout = Some(identity(&out.stdout));
        stage.stderr = Some(identity(&out.stderr));
        if let Some(error) = &out.error {
            stage.error = Some(error.clone());
            stage.state = State::Unknown;
            self.journal.status = "UNKNOWN".into();
        }
        self.checkpoint()?;
        Ok(out)
    }
    fn finish(&mut self, index: usize, result: Result<Value>) -> Result<Value> {
        match result {
            Ok(value) => {
                self.journal.stages[index].state = State::Completed;
                self.journal.stages[index].result = Some(value.clone());
                self.checkpoint()?;
                Ok(value)
            }
            Err(error) => {
                let stage = &mut self.journal.stages[index];
                if stage.state != State::Unknown {
                    stage.state = State::Failed;
                    self.journal.status = "FAILED".into();
                }
                stage.error = Some(format!("{error:#}"));
                self.checkpoint()?;
                Err(error)
            }
        }
    }
}

fn group_absent(pid: u32) -> bool {
    (unsafe { libc::kill(-(pid as i32), 0) }) < 0
        && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
}
fn drain(pipe: &mut impl Read, bytes: &mut Vec<u8>, cap: usize) -> Result<bool> {
    let mut buffer = [0; 8192];
    for _ in 0..16 {
        match pipe.read(&mut buffer) {
            Ok(0) => return Ok(true),
            Ok(n) => {
                let room = cap - bytes.len();
                bytes.extend_from_slice(&buffer[..n.min(room)]);
                ensure!(n <= room, "stdout/stderr byte cap exceeded");
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return Ok(false),
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e.into()),
        }
    }
    Ok(false)
}
fn nonblocking(fd: i32) -> Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    ensure!(
        flags >= 0 && unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } >= 0,
        "cannot make output bounded/nonblocking"
    );
    Ok(())
}
fn stop_and_reap(child: &mut std::process::Child) {
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let cleanup = Instant::now();
    while cleanup.elapsed() < Duration::from_secs(2) {
        match child.try_wait() {
            Ok(Some(_)) | Err(_) => break,
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
        }
    }
}
fn bounded_child(child: &mut std::process::Child, wall: Duration, cap: usize) -> Output {
    let mut output = Output {
        stdout: vec![],
        stderr: vec![],
        code: None,
        signal: None,
        group_absent: false,
        error: None,
    };
    let pid = child.id();
    let begin = Instant::now();
    let result = (|| -> Result<()> {
        let mut stdout = child.stdout.take().context("stdout missing")?;
        let mut stderr = child.stderr.take().context("stderr missing")?;
        nonblocking(stdout.as_raw_fd())?;
        nonblocking(stderr.as_raw_fd())?;
        let mut status = None;
        let (mut out_done, mut err_done) = (false, false);
        loop {
            if !out_done {
                out_done = drain(&mut stdout, &mut output.stdout, cap)?;
            }
            if !err_done {
                err_done = drain(&mut stderr, &mut output.stderr, cap)?;
            }
            if status.is_none() {
                status = child.try_wait()?;
            }
            ensure!(begin.elapsed() < wall, "stage/whole wall budget exceeded");
            if let Some(status) = status {
                output.code = status.code();
                output.signal = status.signal();
                if out_done && err_done {
                    ensure!(
                        group_absent(pid),
                        "launcher exited but controlled group remains live/unknown"
                    );
                    return Ok(());
                }
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    })();
    if let Err(error) = result {
        output.error = Some(format!("{error:#}"));
        stop_and_reap(child);
        if let Ok(Some(status)) = child.try_wait() {
            output.code = status.code();
            output.signal = status.signal();
        }
    }
    output.group_absent = group_absent(pid);
    output
}
fn successful(output: &Output) -> Result<()> {
    ensure!(
        output.error.is_none() && output.group_absent,
        "execution unknown: {}",
        output.error.as_deref().unwrap_or("group absent unproven")
    );
    ensure!(
        output.code == Some(0) && output.signal.is_none(),
        "command failed {:?}/{:?}: {}",
        output.code,
        output.signal,
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}
fn receipt(output: &Output) -> Result<Value> {
    successful(output)?;
    ensure!(output.stdout.len() <= RECEIPT_CAP, "receipt size cap");
    Ok(serde_json::from_slice(&output.stdout)?)
}
fn compilation(path: &Path, value: &Value, inputs: &Inputs, replay: bool) -> Result<()> {
    let r: CompileReceipt = serde_json::from_value(value.clone())?;
    ensure!(
        r.schema == "game_opt_loop/candidate_compilation@1"
            && r.gate_passed
            && r.proof_flags == ProofFlags::default(),
        "compiler did not pass static gate with limited proof scope"
    );
    ensure!(
        r.state
            == if replay {
                "host_compiled_replay_match"
            } else {
                "host_compiled"
            },
        "unknown compiler stage state"
    );
    let dir = Directory::open(path)?;
    let descriptor = dir.read("descriptor.json", META_CAP)?;
    let manifest = dir.read("manifest.json", META_CAP)?;
    ensure!(
        sha(&descriptor) == r.descriptor_sha256
            && r.frozen_identity == r.descriptor_sha256
            && sha(&manifest) == r.manifest_sha256,
        "compiler receipt identity mismatch"
    );
    let d: Value = serde_json::from_slice(&descriptor)?;
    let m: Value = serde_json::from_slice(&manifest)?;
    ensure!(
        m["complete"] == true
            && m["frozen_identity"] == r.frozen_identity
            && m["proof_flags"] == serde_json::to_value(ProofFlags::default())?,
        "compiler manifest incomplete/proof scope invalid"
    );
    ensure!(
        d["compiler_identity"]["executable_sha256"] == inputs.gol.sha256
            && d["compiler_identity"]["executable_bytes"] == inputs.gol.bytes,
        "compiler ran different executable bytes"
    );
    let files = m["files"]
        .as_object()
        .context("compiler file table missing")?;
    let expected: BTreeSet<String> = [
        "candidate.json",
        "original.comp",
        "original.spv",
        "rules.json",
        "gate.json",
        "descriptor.json",
        "manifest.json",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    ensure!(
        dir.names()? == expected
            && files
                .keys()
                .cloned()
                .chain(std::iter::once("manifest.json".into()))
                .collect::<BTreeSet<_>>()
                == expected,
        "compiler file set invalid"
    );
    for (name, id) in files {
        let b = dir.read(
            name,
            if name == "original.spv" {
                SPV_CAP
            } else {
                META_CAP
            },
        )?;
        ensure!(
            id["sha256"] == sha(&b) && id["bytes"] == b.len() as u64,
            "compiler file changed: {name}"
        );
    }
    ensure!(
        identity(&dir.read("candidate.json", META_CAP)?) == inputs.candidate,
        "compiler candidate differs from frozen caller snapshot"
    );
    let a = r.adapter_input.context("no compiler adapter input")?;
    ensure!(
        a["source_path"] == "original.comp"
            && a["original_spirv_path"] == "original.spv"
            && a["source_sha256"] == sha(&dir.read("original.comp", META_CAP)?)
            && a["original_spirv_sha256"] == sha(&dir.read("original.spv", SPV_CAP)?),
        "compiler adapter export differs"
    );
    Ok(())
}
fn adaptation(path: &Path, value: &Value, compile_path: &Path) -> Result<Value> {
    ensure!(
        value["state"] == "verified_offline"
            && value["deployable"] == false
            && value["device_validation"] == "not_run"
            && value["semantic_equivalence"] == "not_run"
            && value["qualification"] == "not_run"
            && value["source_spirv_association"] == "caller_supplied_unverified",
        "adapter upgraded/omitted offline gates"
    );
    let dir = Directory::open(path)?;
    let expected = [
        "original.comp",
        "original.spv",
        "adapted.spv",
        "descriptor.json",
        "manifest.json",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    ensure!(dir.names()? == expected, "adapter file set differs");
    let mut files = BTreeMap::new();
    for name in [
        "original.comp",
        "original.spv",
        "adapted.spv",
        "descriptor.json",
        "manifest.json",
    ] {
        files.insert(
            name,
            dir.read(
                name,
                if name.ends_with(".spv") {
                    SPV_CAP
                } else {
                    RECEIPT_CAP
                },
            )?,
        );
    }
    let d: Value = serde_json::from_slice(&files["descriptor.json"])?;
    let m: Value = serde_json::from_slice(&files["manifest.json"])?;
    let id = sha(&files["descriptor.json"]);
    ensure!(
        value["adaptation_id"] == id
            && value["descriptor"] == d
            && m["adaptation_id"] == id
            && m["schema_version"] == 1
            && m["state"] == "adapted_offline",
        "adapter descriptor/manifest identity mismatch"
    );
    ensure!(
        d["protocol_version"] == 1
            && d["transform"] == "direct_rgba8_storage_to_combined_fetch_lod0"
            && d["source_spirv_association"] == "caller_supplied_unverified",
        "unknown adapter protocol/association"
    );
    ensure!(
        m["deployable"] == false
            && m["device_validation"] == "not_run"
            && m["semantic_equivalence"] == "not_run"
            && m["qualification"] == "not_run"
            && m["source_spirv_association"] == "caller_supplied_unverified",
        "adapter manifest proof scope invalid"
    );
    let hashes = m["files"]
        .as_object()
        .context("adapter file hashes missing")?;
    ensure!(
        hashes.keys().map(String::as_str).collect::<BTreeSet<_>>()
            == [
                "original.comp",
                "original.spv",
                "adapted.spv",
                "descriptor.json"
            ]
            .into_iter()
            .collect(),
        "incomplete adapter file hashes"
    );
    for (name, h) in hashes {
        ensure!(
            h == &sha(&files[name.as_str()]),
            "adapter file hash mismatch: {name}"
        );
    }
    for (field, name) in [
        ("original_source_sha256", "original.comp"),
        ("original_spirv_sha256", "original.spv"),
        ("adapted_spirv_sha256", "adapted.spv"),
    ] {
        ensure!(
            d[field] == sha(&files[name]),
            "adapter descriptor file mismatch"
        );
    }
    let compiled = Directory::open(compile_path)?;
    ensure!(
        files["original.comp"] == compiled.read("original.comp", META_CAP)?
            && files["original.spv"] == compiled.read("original.spv", SPV_CAP)?,
        "adapter original source/SPIR-V does not equal GOL export"
    );
    Ok(
        json!({"adaptation_id":id,"descriptor_sha256":id,"manifest_sha256":sha(&files["manifest.json"]),"files":files.iter().map(|(name,b)|((*name).to_owned(),identity(b))).collect::<BTreeMap<_,_>>(),"local_compiler_export_bytes_equal":true,"source_spirv_association":"caller_supplied_unverified","compiler_binding_scope":"local_replayed_compilation_not_authenticated_attestation"}),
    )
}

fn chain(driver: &mut Driver) -> Result<Value> {
    let compile_path = driver.path.join("compiled");
    let out = driver.command(
        0,
        true,
        vec![
            "candidate-artifact".into(),
            "compile".into(),
            "--candidate".into(),
            path_string(&driver.path.join("candidate.json"))?,
            "--out".into(),
            path_string(&compile_path)?,
        ],
    )?;
    let compiled = driver.finish(
        0,
        (|| {
            let v = receipt(&out)?;
            compilation(&compile_path, &v, &driver.journal.inputs, false)?;
            Ok(v)
        })(),
    )?;
    let frozen = compiled["frozen_identity"]
        .as_str()
        .context("frozen compiler identity")?;
    let out = driver.command(
        1,
        true,
        vec![
            "candidate-artifact".into(),
            "verify".into(),
            "--artifact".into(),
            path_string(&compile_path)?,
            "--expected-identity".into(),
            frozen.into(),
        ],
    )?;
    let replay = driver.finish(
        1,
        (|| {
            let v = receipt(&out)?;
            compilation(&compile_path, &v, &driver.journal.inputs, true)?;
            ensure!(
                v["frozen_identity"] == frozen,
                "compiler replay identity differs"
            );
            Ok(v)
        })(),
    )?;
    let adaptation_root = driver.path.join("adapted");
    Directory::create(&adaptation_root)?;
    let out = driver.command(
        2,
        false,
        vec![
            "gray-artifact".into(),
            "adapt-storage".into(),
            "--source".into(),
            path_string(&compile_path.join("original.comp"))?,
            "--spirv".into(),
            path_string(&compile_path.join("original.spv"))?,
            "--out".into(),
            path_string(&adaptation_root)?,
        ],
    )?;
    let adapted = driver.finish(
        2,
        (|| {
            let v = receipt(&out)?;
            let id = v["adaptation_id"].as_str().context("adapter identity")?;
            ensure!(
                id.len() == 64
                    && id
                        .bytes()
                        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()),
                "invalid adaptation id"
            );
            let path = adaptation_root.join(format!("storage-{id}"));
            ensure!(
                v["path"] == path_string(&path)?,
                "adapter returned path outside owned output"
            );
            adaptation(&path, &v, &compile_path)?;
            Ok(v)
        })(),
    )?;
    let path = PathBuf::from(adapted["path"].as_str().context("adapter path")?);
    let out = driver.command(
        3,
        false,
        vec![
            "gray-artifact".into(),
            "verify-adaptation".into(),
            "--artifact".into(),
            path_string(&path)?,
        ],
    )?;
    let binding = driver.finish(
        3,
        (|| {
            let v = receipt(&out)?;
            ensure!(v == adapted, "adapter replay receipt differs");
            adaptation(&path, &v, &compile_path)
        })(),
    )?;
    let mut status = "PREPARED_OFFLINE";
    if driver.journal.inputs.build {
        let ndk = driver
            .journal
            .inputs
            .ndk
            .clone()
            .context("build missing explicit SDK path")?;
        match std::fs::symlink_metadata(&ndk) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                // Freeze the caller observation, then probe controlled absence. Reusing
                // the mutable caller path could race a newly installed helper into a new group.
                let controlled_ndk = driver.path.join("controlled-empty-sdk");
                let sdk = Directory::create(&controlled_ndk)?;
                ensure!(
                    sdk.names()?.is_empty(),
                    "controlled missing SDK is not empty"
                );
                ensure!(
                    unsafe { libc::fchmod(sdk.0.as_raw_fd(), 0o500) } == 0,
                    "cannot freeze empty SDK permissions"
                );
                sdk.0.sync_all()?;
                let out = driver.command(
                    4,
                    false,
                    vec![
                        "gray-artifact".into(),
                        "build".into(),
                        "--adapted-artifact".into(),
                        path_string(&path)?,
                        "--out".into(),
                        path_string(&driver.path.join("native"))?,
                        "--ndk".into(),
                        path_string(&controlled_ndk)?,
                        "--timeout-seconds".into(),
                        "1".into(),
                    ],
                )?;
                let result = (|| {
                    ensure!(
                        out.error.is_none() && out.group_absent,
                        "build capability execution unknown"
                    );
                    ensure!(out.code.is_some_and(|c| c != 0) && out.signal.is_none() && String::from_utf8_lossy(&out.stderr).contains("missing tool") && String::from_utf8_lossy(&out.stderr).contains("install an Android NDK explicitly"), "expected actual missing-SDK refusal; successful/unexpected build not accepted");
                    ensure!(
                        !driver.path.join("native/manifest.json").exists(),
                        "unexpected successful native manifest"
                    );
                    ensure!(sdk.names()?.is_empty(), "controlled missing SDK changed");
                    adaptation(&path, &adapted, &compile_path)?;
                    Ok(
                        json!({"reason":"MISSING_ANDROID_NDK","native_build_accepted":false,"tool_execution":"missing_tool_refusal","device_validation":"not_run","requested_ndk":ndk,"requested_ndk_observation":"not_found_at_probe_time","actual_ndk":controlled_ndk,"probe_scope":"controlled_empty_sdk_not_caller_installation_or_native_build","empty_sdk_inventory_sha256":sha(b"[]")}),
                    )
                })();
                match result {
                    Ok(value) => {
                        // A negative capability probe is never a completed native build,
                        // including if the host stops before the final receipt is written.
                        let stage = &mut driver.journal.stages[4];
                        stage.state = State::Blocked;
                        stage.result = Some(value);
                        stage.error = Some("MISSING_ANDROID_NDK".into());
                        driver.checkpoint()?;
                    }
                    Err(error) => {
                        driver.finish(4, Err(error))?;
                    }
                }
            }
            _ => {
                driver.journal.stages[4].state = State::NotRun;
                driver.journal.stages[4].error = Some("BLOCKED_PROCESS_GROUP_CONTRACT: SDK path present/unknown; PF helper groups are not supervised by this wrapper".into());
            }
        }
        status = "BLOCKED";
    } else {
        driver.journal.stages[4].state = State::NotRun;
        driver.journal.stages[4].error = Some("native build not requested".into());
    }
    let rules = identity(&Directory::open(&compile_path)?.read("rules.json", META_CAP)?);
    Ok(prepared(
        &driver.journal,
        status,
        frozen,
        rules,
        replay,
        binding,
    ))
}

fn prepared(
    journal: &Journal,
    status: &str,
    frozen: &str,
    rules: Identity,
    replay: Value,
    binding: Value,
) -> Value {
    json!({"schema":SCHEMA,"status":status,"offline_compilation_adapter_complete":true,"preparation_chain_complete":status=="PREPARED_OFFLINE","frozen_inputs_sha256":journal.frozen_inputs_sha256,"candidate_frozen_identity":frozen,"candidate_json":journal.inputs.candidate,"rules":rules,"gol_executable":journal.inputs.gol,"phonefarm_executable":journal.inputs.phonefarm,"compiler_receipt":replay,"adapter_binding":binding,"provenance":journal.inputs.provenance,"native_build_state":journal.stages[4].state,"native_build_reason":journal.stages[4].error,"proof_flags":ProofFlags::default(),"claim_scope":"offline_static_compilation_and_adapter_integrity_only_not_full_candidate_driver"})
}

fn execute(candidate: &Path, pf: &Path, out: &Path, mut inputs: Inputs) -> Result<Value> {
    let start = Instant::now();
    let candidate = read_path(candidate, 1024 * 1024)?;
    inputs.candidate = identity(&candidate);
    let path = absolute(out)?;
    let dir = Directory::create(&path)?;
    dir.write_new("candidate.json", &candidate)?;
    inputs.gol = copy_tool(&std::env::current_exe()?, &dir, "gol.bin")?;
    inputs.phonefarm = copy_tool(&absolute(pf)?, &dir, "phonefarm.bin")?;
    let stages = NAMES
        .into_iter()
        .map(|name| Stage {
            name: name.into(),
            state: State::Pending,
            argv: vec![],
            tool_sha256: None,
            pid: None,
            group_absent: None,
            elapsed_ms: None,
            exit_code: None,
            signal: None,
            stdout: None,
            stderr: None,
            result: None,
            error: None,
        })
        .collect();
    let mut driver = Driver {
        path,
        dir,
        start,
        checkpoint: 0,
        journal: Journal {
            schema: SCHEMA.into(),
            status: "RUNNING".into(),
            frozen_inputs_sha256: sha(&encode(&inputs)?),
            inputs,
            stages,
            prepared_receipt: None,
            proof_flags: ProofFlags::default(),
        },
    };
    driver.checkpoint()?;
    match chain(&mut driver) {
        Ok(v) => {
            let bytes = encode(&v)?;
            driver.dir.write_new("prepared.json", &bytes)?;
            driver.journal.prepared_receipt = Some(identity(&bytes));
            driver.journal.status = v["status"].as_str().context("prepared status")?.into();
            driver.checkpoint()?;
            Ok(v)
        }
        Err(e) => {
            if driver.journal.status == "RUNNING" {
                driver.journal.status = "FAILED".into();
            }
            for stage in &mut driver.journal.stages {
                if stage.state == State::InFlight {
                    stage.state = State::Unknown;
                    driver.journal.status = "UNKNOWN".into();
                    stage.error = Some(format!("{e:#}"));
                }
            }
            if driver.journal.status == "FAILED"
                && !driver
                    .journal
                    .stages
                    .iter()
                    .any(|s| s.state == State::Failed)
            {
                if let Some(stage) = driver
                    .journal
                    .stages
                    .iter_mut()
                    .find(|s| s.state == State::Pending)
                {
                    stage.state = State::Failed;
                    stage.error = Some(format!("{e:#}"));
                }
            }
            driver.checkpoint()?;
            Ok(
                json!({"schema":SCHEMA,"status":driver.journal.status,"offline_compilation_adapter_complete":driver.journal.stages[..4].iter().all(|s|s.state==State::Completed),"preparation_chain_complete":false,"session":driver.path,"error":format!("{e:#}"),"proof_flags":ProofFlags::default(),"provenance":driver.journal.inputs.provenance,"unknown_execution_retry_allowed":false}),
            )
        }
    }
}

fn inspect(path: &Path) -> Result<Value> {
    let path = absolute(path)?;
    let dir = Directory::open(&path)?;
    let bytes = dir.read("journal.json", META_CAP)?;
    let mut journal: Journal = serde_json::from_slice(&bytes)?;
    ensure!(
        journal.schema == SCHEMA
            && journal.stages.len() == NAMES.len()
            && journal.proof_flags == ProofFlags::default()
            && journal.frozen_inputs_sha256 == sha(&encode(&journal.inputs)?),
        "invalid journal/inputs/proof scope"
    );
    ensure!(
        matches!(
            journal.status.as_str(),
            "RUNNING" | "FAILED" | "UNKNOWN" | "BLOCKED" | "PREPARED_OFFLINE"
        ),
        "unknown journal status"
    );
    ensure!(
        (50..=120_000).contains(&journal.inputs.stage_wall_ms)
            && (50..=600_000).contains(&journal.inputs.whole_wall_ms)
            && (1024..=1_048_576).contains(&journal.inputs.stream_bytes)
            && journal.inputs.build == journal.inputs.ndk.is_some()
            && matches!(
                journal.inputs.provenance.as_str(),
                "synthetic_mechanism_fixture"
                    | "caller_supplied_local_candidate_not_real_game_attestation"
            ),
        "invalid frozen policy/provenance"
    );
    if matches!(journal.status.as_str(), "PREPARED_OFFLINE" | "BLOCKED") {
        ensure!(
            journal.prepared_receipt.is_some(),
            "final state lacks prepared receipt"
        );
    }
    ensure!(
        identity(&dir.read("candidate.json", META_CAP)?) == journal.inputs.candidate
            && file_hash(&path.join("gol.bin"), TOOL_CAP)? == journal.inputs.gol
            && file_hash(&path.join("phonefarm.bin"), TOOL_CAP)? == journal.inputs.phonefarm,
        "frozen inputs changed"
    );
    let mut unknown = false;
    for (i, stage) in journal.stages.iter_mut().enumerate() {
        ensure!(stage.name == NAMES[i], "stage order/name altered");
        if stage.state == State::InFlight {
            stage.state = State::Unknown;
            unknown = true;
        }
        for (suffix, id) in [("stdout.log", &stage.stdout), ("stderr.log", &stage.stderr)] {
            if let Some(id) = id {
                ensure!(
                    identity(&dir.read(
                        &format!("{}.{}", stage.name, suffix),
                        journal.inputs.stream_bytes
                    )?) == *id,
                    "stage log changed"
                );
            }
        }
        if stage.state == State::Completed || stage.state == State::Blocked {
            ensure!(
                stage.group_absent == Some(true)
                    && stage.result.is_some()
                    && stage.stdout.is_some()
                    && stage.stderr.is_some(),
                "completed stage missing execution receipt"
            );
            ensure!(
                stage.pid.is_some_and(|p| p > 0)
                    && stage.signal.is_none()
                    && if stage.state == State::Blocked {
                        i == 4 && stage.exit_code.is_some_and(|c| c != 0)
                    } else {
                        stage.exit_code == Some(0)
                    },
                "completed stage contradicts actual process result"
            );
        }
    }
    if let Some(id) = &journal.prepared_receipt {
        ensure!(
            journal.stages[..4]
                .iter()
                .all(|s| s.state == State::Completed)
                && !unknown,
            "prepared receipt without complete chain"
        );
        let bytes = dir.read("prepared.json", META_CAP)?;
        ensure!(identity(&bytes) == *id, "prepared receipt changed");
        let v: Value = serde_json::from_slice(&bytes)?;
        ensure!(
            v["status"] == journal.status
                && v["offline_compilation_adapter_complete"] == true
                && v["preparation_chain_complete"] == !journal.inputs.build
                && (journal.status == "BLOCKED") == journal.inputs.build,
            "prepared outcome contradicts required stage policy"
        );
        ensure!(
            v["proof_flags"] == serde_json::to_value(ProofFlags::default())?
                && v["frozen_inputs_sha256"] == journal.frozen_inputs_sha256,
            "prepared proof scope/input binding invalid"
        );
        let compile_path = path.join("compiled");
        compilation(
            &compile_path,
            journal.stages[1]
                .result
                .as_ref()
                .context("missing compiler replay")?,
            &journal.inputs,
            true,
        )?;
        let adapted = journal.stages[2]
            .result
            .as_ref()
            .context("missing adaptation receipt")?;
        let adapted_path = PathBuf::from(adapted["path"].as_str().context("adapted path")?);
        ensure!(
            adapted_path.parent() == Some(path.join("adapted").as_path()),
            "adapted path outside session"
        );
        let current = adaptation(&adapted_path, adapted, &compile_path)?;
        ensure!(
            v["adapter_binding"] == current && journal.stages[3].result == Some(current),
            "adapter binding changed"
        );
        let native = &journal.stages[4];
        ensure!(
            if journal.inputs.build {
                (native.state == State::Blocked && native.error.as_deref() == Some("MISSING_ANDROID_NDK"))
                    || (native.state == State::NotRun && native.error.as_deref() == Some("BLOCKED_PROCESS_GROUP_CONTRACT: SDK path present/unknown; PF helper groups are not supervised by this wrapper"))
            } else {
                native.state == State::NotRun
                    && native.error.as_deref() == Some("native build not requested")
            },
            "native outcome contradicts frozen build policy"
        );
        let replay = journal.stages[1]
            .result
            .clone()
            .context("missing compiler replay")?;
        let frozen = replay["frozen_identity"]
            .as_str()
            .context("frozen compiler identity")?
            .to_owned();
        let expected = prepared(
            &journal,
            &journal.status,
            &frozen,
            identity(&Directory::open(&compile_path)?.read("rules.json", META_CAP)?),
            replay,
            journal.stages[3]
                .result
                .clone()
                .context("missing adapter binding")?,
        );
        ensure!(
            v == expected,
            "prepared receipt differs from current journal/artifact bindings"
        );
    }
    if unknown || journal.status == "RUNNING" {
        journal.status = "UNKNOWN".into();
    }
    Ok(
        json!({"schema":SCHEMA,"status":journal.status,"journal":journal,"inspection_scope":"current_local_integrity_only_no_commands_or_new_gate_replay","unknown_execution_retry_allowed":false,"proof_flags":ProofFlags::default()}),
    )
}

pub fn run(action: Action) -> Result<()> {
    let value = match action {
        Action::Inspect { session } => inspect(&session)?,
        Action::Run {
            candidate,
            phonefarm_bin,
            out,
            build,
            ndk,
            stage_wall_ms,
            whole_wall_ms,
            stream_bytes,
            synthetic,
        } => {
            ensure!(
                (50..=120_000).contains(&stage_wall_ms)
                    && (50..=600_000).contains(&whole_wall_ms)
                    && (1024..=1_048_576).contains(&stream_bytes),
                "budgets outside receiver limits"
            );
            ensure!(
                build == ndk.is_some(),
                "build requires explicit SDK path and vice versa"
            );
            let blank = Identity {
                sha256: String::new(),
                bytes: 0,
            };
            execute(&candidate, &phonefarm_bin, &out, Inputs { candidate: blank.clone(), gol: blank.clone(), phonefarm: blank, stage_wall_ms, whole_wall_ms, stream_bytes, build, ndk: ndk.map(|p| absolute(&p)).transpose()?, provenance: if synthetic { "synthetic_mechanism_fixture" } else { "caller_supplied_local_candidate_not_real_game_attestation" }.into(), implementation_sha256: sha(include_bytes!("candidate_prepare.rs")), gate_sha256: sha(GATE.as_bytes()), execution_scope: "trusted_local_binaries_and_OS_shell_process_groups_not_sandbox_or_remote_attestation".into() })?
        }
    };
    println!("{}", serde_json::to_string_pretty(&value)?);
    ensure!(
        value["status"] == "PREPARED_OFFLINE",
        "preparation is {}; see retained session",
        value["status"]
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    #[test]
    fn repeated_sdk_inventory_does_not_reuse_eof_offset() {
        let base = if cfg!(target_os = "macos") {
            "/private/tmp"
        } else {
            "/tmp"
        };
        let path = Path::new(base).join(format!("gol-prepare-inventory-{}", std::process::id()));
        let dir = Directory::create(&path).unwrap();
        assert!(dir.names().unwrap().is_empty());
        dir.write_new("unexpected-tool", b"not an SDK").unwrap();
        let expected = ["unexpected-tool".to_owned()].into_iter().collect();
        assert_eq!(dir.names().unwrap(), expected);
        assert_eq!(dir.names().unwrap(), expected);
        std::fs::remove_dir_all(path).unwrap();
    }

    #[test]
    fn permit_readers_observe_only_complete_bytes_and_publication_is_create_only() {
        let base = if cfg!(target_os = "macos") {
            "/private/tmp"
        } else {
            "/tmp"
        };
        let path = Path::new(base).join(format!("gol-prepare-permit-{}", std::process::id()));
        let dir = Directory::create(&path).unwrap();
        let bytes = vec![b'p'; 1024 * 1024];
        let done = Arc::new(AtomicBool::new(false));
        let reader_path = path.join("permit");
        let reader_done = Arc::clone(&done);
        let reader_bytes = bytes.clone();
        let reader = std::thread::spawn(move || {
            while !reader_done.load(Ordering::SeqCst) {
                match std::fs::read(&reader_path) {
                    Ok(actual) => assert_eq!(actual, reader_bytes),
                    Err(e) => assert_eq!(e.kind(), std::io::ErrorKind::NotFound),
                }
            }
            assert_eq!(std::fs::read(reader_path).unwrap(), reader_bytes);
        });
        publish_permit(&dir, "permit", &bytes).unwrap();
        assert!(publish_permit(&dir, "permit", b"replacement").is_err());
        done.store(true, Ordering::SeqCst);
        reader.join().unwrap();
        assert_eq!(dir.read("permit", bytes.len()).unwrap(), bytes);
        std::fs::remove_dir_all(path).unwrap();
    }
}
